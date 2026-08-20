"""Test suite: the stability-check diagrams from the README as fixtures — transcribed,
not invented — plus golden-plan regression and the case sweep."""
import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

import verify

HERE = Path(__file__).parent


# ------------------------------------------------------------ helpers

def make_case(pallet_l, pallet_w, skus, deck=100, max_h=None):
    return {
        "pallet": {"length": pallet_l, "width": pallet_w,
                   "deck_height": deck, "max_stack_height": max_h},
        "skus": [{"sku": code, "dims": list(dims), "count": n, "weight": None}
                 for code, dims, n in skus],
    }


def make_plan(rows, leftovers=()):
    return {
        "placements": [
            {"sequence": i + 1, "id": rid, "sku": rid.split("-")[0],
             "x": x, "y": y, "z": z, "rotated": rot}
            for i, (rid, x, y, z, rot) in enumerate(rows)],
        "leftovers": list(leftovers),
    }


TWO_BOX_CASE = make_case(400, 300, [("X", (200, 300, 100), 2)])


# ---------------------------------------------- checkpoint 1: the verifier

def test_verifier_passes_legal_plan():
    plan = make_plan([("X-1", 0, 0, 0, True), ("X-2", 200, 0, 0, True)])
    v = verify.verify(plan, TWO_BOX_CASE)
    assert v["static_support"] == "PASS"
    assert v["stackable"] is True          # flat 100-high top over full deck
    assert v["placed"] == 2 and v["leftovers"] == []
    assert v["stack_height_mm"] == 100
    assert v["footprint_used_pct"] == 100.0


def test_verifier_catches_bad_plans():
    def rule_of(plan, case):
        with pytest.raises(verify.Violation) as e:
            verify.verify(plan, case)
        return e.value.rule

    # overlap -> rule 1
    assert rule_of(make_plan([("X-1", 0, 0, 0, True), ("X-2", 100, 0, 0, True)]),
                   TWO_BOX_CASE) == 1
    # overhang past the deck edge -> rule 1
    assert rule_of(make_plan([("X-1", 0, 0, 0, True), ("X-2", 300, 0, 0, True)]),
                   TWO_BOX_CASE) == 1
    # floater (50 mm air gap) -> rule 2
    assert rule_of(make_plan([("X-1", 0, 0, 0, True), ("X-2", 0, 0, 150, True)]),
                   TWO_BOX_CASE) == 2
    # vanished box (placed one, declared no leftovers) -> rule 4
    assert rule_of(make_plan([("X-1", 0, 0, 0, True)]), TWO_BOX_CASE) == 4
    # height cap -> rule 1
    capped = make_case(400, 300, [("X", (200, 300, 100), 2)], max_h=150)
    assert rule_of(make_plan([("X-1", 0, 0, 0, True), ("X-2", 0, 0, 100, True)]),
                   capped) == 1


def test_verifier_diving_board():
    # SUPPORT (b): 81% area, COM inside — but no opposite edge pair.
    case = make_case(300, 300, [("S", (90, 90, 100), 1), ("T", (100, 100, 50), 1)])
    plan = make_plan([("S-1", 100, 100, 0, True), ("T-1", 95, 95, 100, True)])
    with pytest.raises(verify.Violation) as e:
        verify.verify(plan, case)
    assert e.value.rule == 2 and "diving board" in str(e.value)


def test_verifier_jenga_staircase():
    # BALANCE: each joint passes SUPPORT (70% contact, Y edges),
    # but placing the 4th stair puts B's group COM (x=110) past A's support.
    case = make_case(400, 300, [("J", (100, 100, 100), 4)])
    plan = make_plan([("J-1", 0, 0, 0, True), ("J-2", 30, 0, 100, True),
                      ("J-3", 60, 0, 200, True), ("J-4", 90, 0, 300, True)])
    with pytest.raises(verify.Violation) as e:
        verify.verify(plan, case)
    assert e.value.rule == 3 and "J-2" in str(e.value)
    # ...and the same stack minus the top stair stands (rule 3 passes)
    ok = make_plan([("J-1", 0, 0, 0, True), ("J-2", 30, 0, 100, True),
                    ("J-3", 60, 0, 200, True)], leftovers=["J-4"])
    assert verify.verify(ok, case)["static_support"] == "PASS"


def test_verifier_sits_on_crosscheck():
    plan = make_plan([("X-1", 0, 0, 0, True), ("X-2", 200, 0, 0, True)])
    plan["placements"][1]["sits_on"] = ["X-1"]      # lie: it sits on the deck
    with pytest.raises(verify.Violation) as e:
        verify.verify(plan, TWO_BOX_CASE)
    assert e.value.rule == 2


def test_verifier_cli():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        plan_p, case_p = Path(d) / "plan.json", Path(d) / "case.json"
        plan_p.write_text(json.dumps(
            make_plan([("X-1", 0, 0, 0, True), ("X-2", 200, 0, 0, True)])))
        case_p.write_text(json.dumps(TWO_BOX_CASE))
        r = subprocess.run([sys.executable, str(HERE / "verify.py"),
                            str(plan_p), str(case_p)],
                           capture_output=True, text=True)
        assert r.returncode == 0 and r.stdout.startswith("PASS")


# ------------------------------------------- checkpoint 2: heightmap + checks

import numpy as np  # noqa: E402  (solver-side tests only; verify stays clean)

import solver


def state_with(pallet_l, pallet_w, skus, places):
    """Build a State by force-placing (sku_code, rotated, x, y) rows."""
    prob = solver.load_problem(make_case(pallet_l, pallet_w, skus))
    st = solver.State(prob)
    by_code = {s.code: s for s in prob.skus}
    for code, rot, x, y in places:
        sku = by_code[code]
        lx, ly = solver.footprint(sku, rot)
        z = int(st.hm[x:x + lx, y:y + ly].max())
        st.place(sku, rot, x, y, z)
    return st, by_code


def test_landing_rule():
    # Landing rule: z is derived from the tallest cell under the footprint —
    # floating is impossible by construction.
    st, sk = state_with(1200, 800, [("D", (370, 298, 220), 1),
                                    ("C", (388, 280, 192), 1)],
                        [("D", True, 0, 0), ("C", True, 370, 0)])
    assert st.hm[0, 0] == 220 and st.hm[400, 0] == 192
    # a box halfway on D and C lands at 220 (the tallest cell), never 192:
    # its z is the max under the footprint, exactly the see-saw case
    assert int(st.hm[267:473, 0:198].max()) == 220
    # and anchor-aligned spots flush with D's right edge land fully on D
    spots = dict(((x, y), z) for x, y, z in
                 solver.candidate_spots(st, 206, 198))
    assert spots[(164, 0)] == 220


def test_seesaw_rejected():
    # SUPPORT (a): half on D (220), half over C (192) -> 28 mm air
    # gap under one half; ~50% supported area is below the 70% bar.
    st, sk = state_with(1200, 800, [("D", (370, 298, 220), 1),
                                    ("C", (388, 280, 192), 1),
                                    ("N", (206, 198, 278), 1)],
                        [("D", True, 0, 0), ("C", True, 370, 0)])
    assert not solver.support_ok(st.hm, 267, 0, 206, 198, 220)  # straddling
    assert solver.support_ok(st.hm, 100, 0, 206, 198, 220)      # fully on D


def test_diving_board_rejected():
    # SUPPORT (b): 81% area, COM inside, but the contact strip is
    # inset from every edge of the base -> no opposite edge pair.
    st, _ = state_with(300, 300, [("S", (90, 90, 100), 1)],
                       [("S", True, 100, 100)])
    assert not solver.support_ok(st.hm, 95, 95, 100, 100, 100)
    # same area flush with both X edges (a bridge) is fine
    st2, _ = state_with(300, 300, [("P", (40, 100, 100), 2)],
                        [("P", True, 0, 0), ("P", True, 60, 0)])
    assert solver.support_ok(st2.hm, 0, 0, 100, 100, 100)


def test_jenga_staircase_rejected():
    # BALANCE: three 30%-overhang stairs stand; the fourth makes
    # B's group COM leave A's support -> the check must reject it.
    st, sk = state_with(400, 300, [("J", (100, 100, 100), 4)],
                        [("J", True, 0, 0), ("J", True, 30, 0),
                         ("J", True, 60, 0)])
    probe = solver.Placement(0, "J", 0, 90, 0, 300, True, 100, 100, 100,
                             sk["J"].mass, ())
    assert not solver.balance_ok(st.placements, probe)
    # the third stair itself was legal (already placed above); re-verify:
    st2, _ = state_with(400, 300, [("J", (100, 100, 100), 4)],
                        [("J", True, 0, 0), ("J", True, 30, 0)])
    probe3 = solver.Placement(0, "J", 0, 60, 0, 200, True, 100, 100, 100,
                              sk["J"].mass, ())
    assert solver.balance_ok(st2.placements, probe3)


def test_level_top():
    # LEVEL TOP on a 6-cell deck: flat 220 passes, jagged fails.
    st, _ = state_with(6, 1, [("F", (6, 1, 220), 1)], [("F", True, 0, 0)])
    assert st.snapshot_score()[0] == 1
    st2, _ = state_with(6, 1, [("F", (3, 1, 220), 1), ("G", (2, 1, 192), 1),
                               ("H", (1, 1, 165), 1)],
                        [("F", True, 0, 0), ("G", True, 3, 0),
                         ("H", True, 5, 0)])
    assert list(st2.hm[:, 0]) == [220, 220, 220, 192, 192, 165]
    assert st2.snapshot_score()[0] == 0     # 3/6 cells at top < 60%


def test_decimal_snap():
    # Decimal inputs are rounded once at load; every later number is int.
    case = make_case(400, 300, [("X", (200.4, 299.5, 99.7), 1)])
    prob = solver.load_problem(case)
    s = prob.skus[0]
    assert (s.l, s.w, s.h) == (200, 300, 100)
    assert prob.case["skus"][0]["dims"] == [200, 300, 100]
    assert verify.snap(200.4) == 200 and verify.snap(299.5) == 300


# ------------------------------------- checkpoints 3+4: greedy, beam (BSG)

def test_red_blue_green_walkthrough():
    # Textbook walkthrough: 4-cell pallet; RED and BLUE (2 cells, 200 tall)
    # tile the deck flat; GREEN (1 cell, 100) would wreck the top -> the
    # snapshot rule leaves it out as a leftover. Centered RED (copy M) dies.
    case = make_case(4, 1, [("RED", (2, 1, 200), 1), ("BLUE", (2, 1, 200), 1),
                            ("GREEN", (1, 1, 100), 1)])
    prob = solver.load_problem(case)
    plan = solver.pack(prob, time_budget=None, widths=(2,))
    assert plan["leftovers"] == ["GREEN-1"]
    assert plan["verdict"]["stackable"] is True
    tops = {(r["x"], r["z"]) for r in plan["placements"]}
    assert tops == {(0, 0), (2, 0)}         # RED+BLUE side by side, flat 200


def test_beam_w1_golden():
    # Golden regression: greedy IS solve(width=1); the beam
    # machinery (twin deletion, streaming, restarts) must never change it.
    prob = solver.load_problem(str(HERE / "cases/acceptance_1.json"))
    plan = solver.pack(prob, time_budget=None, widths=(1,))
    golden = json.loads((HERE / "cases/golden_w1.json").read_text())
    assert plan == golden


def test_determinism():
    # same case solved twice -> bit-for-bit identical plan (no randomness)
    case = make_case(500, 400, [("P", (250, 200, 120), 2),
                                ("Q", (200, 150, 100), 2)])
    prob = solver.load_problem(case)
    a = solver.pack(prob, time_budget=None, widths=(1, 2))
    b = solver.pack(prob, time_budget=None, widths=(1, 2))
    assert a == b


def test_acceptance_case(tmp_path):
    # The original task, solved; then the INDEPENDENT verifier
    # judge it in a real separate process. The four SKU heights
    # (278/165/192/220) never meet at one level, so a tiled layer can't be
    # stackable — the platform pattern spreads the six A-boxes to carry the
    # upper pallet at 278 mm and tucks B/C/D below that plane: 10/10 AND
    # stackable.
    prob = solver.load_problem(str(HERE / "cases/acceptance_1.json"))
    plan = solver.pack(prob, time_budget=None, widths=(1,))
    plan_p = tmp_path / "plan.json"
    plan_p.write_text(json.dumps(plan))
    r = subprocess.run([sys.executable, str(HERE / "verify.py"),
                        str(plan_p), str(HERE / "cases/acceptance_1.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("PASS")
    assert plan["verdict"]["placed"] == 10
    assert plan["verdict"]["stackable"] is True
    # solver and verifier must agree on the stackability numbers
    assert f"top contact {plan['verdict']['top_coverage_pct']}%" in r.stdout


def test_narrow_tower_not_stackable():
    # flat but clumped: two X boxes side by side cover half the deck in one
    # corner — plenty of contact area, hull too narrow for an upper pallet.
    case = make_case(800, 300, [("X", (200, 300, 100), 2)])
    prob = solver.load_problem(case)
    st = solver.State(prob)
    for x in (0, 200):
        st.place(prob.skus[0], True, x, 0, 0)
    assert st.snapshot_score()[0] == 0      # area 50% ok, spread 50% < 60%
    v = verify.verify(make_plan([("X-1", 0, 0, 0, True),
                                 ("X-2", 200, 0, 0, True)]), case)
    assert v["stackable"] is False and v["top_spread_pct"] == 50.0


def test_case_sweep(tmp_path):
    """Generalization: four stress cases beyond the acceptance case, each
    solved (w=1) and judged by the verifier in-process. Expectations pin the
    behavior each case exists to exercise."""
    expect = {
        # perfect layers exist; every box placed, top stackable
        "uniform_brick":   dict(placed=12, leftovers=0, stackable=True,
                                widths=(1,)),
        # height cap: 12 fit under 600 mm, 8 honest leftovers, flat top.
        # Greedy (w=1) only finds 9 — the w=4 beam recovers the full
        # 3-layer lattice: this case is the beam earning its keep.
        "overflow_capped": dict(placed=12, leftovers=8, stackable=True,
                                widths=(1, 2, 4)),
        # a full two-layer build: heavy 200-layer, light 150-layer on top
        "two_layer_mixed": dict(placed=10, leftovers=0, stackable=True,
                                widths=(1,)),
        # real weights: 25 kg small boxes vs 2 kg giants; all placed, stable
        "heavy_small_light_giant": dict(placed=6, leftovers=0, stackable=True,
                                        widths=(1,)),
    }
    for name, exp in expect.items():
        case = json.loads((HERE / f"cases/{name}.json").read_text())
        plan = solver.pack(solver.load_problem(case),
                           time_budget=None, widths=exp["widths"])
        v = verify.verify(plan, case)     # raises Violation on rules 1-4
        assert v["placed"] == exp["placed"], name
        assert len(plan["leftovers"]) == exp["leftovers"], name
        assert v["stackable"] is exp["stackable"], name
        if case["pallet"]["max_stack_height"]:
            assert v["stack_height_mm"] <= case["pallet"]["max_stack_height"]


def test_tripwire_stdlib_only():
    """verify.py must stay runnable on bare python3."""
    tree = ast.parse((HERE / "verify.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), \
        f"verify.py imports non-stdlib modules: {imported - set(sys.stdlib_module_names)}"
