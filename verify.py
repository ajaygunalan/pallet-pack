#!/usr/bin/env python3
"""Referee for pallet build plans.

Judges a finished build plan against the five rules, mechanically, without
knowing how it was produced. Runs on bare python3:

    python3 verify.py plan.json case.json

Exit code 0 = rules 1-4 PASS (rule 5, LEVEL TOP, is reported, not fatal:
a stable-but-not-stackable best effort is a legitimate answer).

stdlib only, by design: this file must never import solver.py,
numpy, or anything the solver's geometry could leak through. All geometry
here is exact integer/Fraction arithmetic on axis-aligned boxes.
"""
from __future__ import annotations

import json
import math
import sys
from fractions import Fraction

# Thresholds: same VALUES as solver.py (thresholds shared, implementation
# deliberately not). Kept as exact rationals - no epsilons anywhere.
SUPPORT_FRAC = Fraction(70, 100)        # SUPPORT (a): min supported base area
SUPPORTER_MIN_FRAC = Fraction(25, 100)  # a box below counts as a supporter
                                        #   only at >= 25% base contact [Schuster]
FLAT_TOL = 10                           # mm: top counts as "level" within this
# LEVEL TOP: a rigid upper pallet needs flat contact with enough area AND
# spread to its edges (hull of the contact), not most of its area touched:
STACK_AREA_FRAC = Fraction(20, 100)     # min deck fraction in top-level contact
STACK_SPREAD_FRAC = Fraction(60, 100)   # min deck fraction under the contact's hull


def snap(v):
    """Round a millimeter value to the nearest whole millimeter."""
    return int(math.floor(float(v) + 0.5))


class Violation(Exception):
    def __init__(self, rule, box_id, reason):
        super().__init__(f"FAIL rule={rule} box={box_id}: {reason}")
        self.rule = rule


# ---------------------------------------------------------------- loading

def load_boxes(plan, case):
    """Return (pallet dict, boxes in sequence order, expected ids).

    A box is a dict with exact int extents; rotated=True means the first
    given dimension lies along X.
    """
    pallet = {
        "L": snap(case["pallet"]["length"]),
        "W": snap(case["pallet"]["width"]),
        "max_h": (None if case["pallet"].get("max_stack_height") is None
                  else snap(case["pallet"]["max_stack_height"])),
    }
    skus = {}
    expected = set()
    weights_given = all(s.get("weight") is not None for s in case["skus"])
    for s in case["skus"]:
        dims = tuple(snap(d) for d in s["dims"])
        skus[s["sku"]] = (dims, s)
        for i in range(1, s["count"] + 1):
            expected.add(f"{s['sku']}-{i}")

    boxes = []
    for row in sorted(plan["placements"], key=lambda r: r["sequence"]):
        if row["sku"] not in skus:
            raise Violation(4, row["id"], "unknown SKU")
        (l, w, h), s = skus[row["sku"]]
        lx, ly = (l, w) if row["rotated"] else (w, l)
        # mass: real weights (snapped to integer grams, exactly as the solver
        # ingests them) only if every SKU has one; else volume proxy
        mass = snap(s["weight"] * 1000) if weights_given else l * w * h
        for c in ("x", "y", "z"):
            if row[c] != int(row[c]):
                raise Violation(1, row["id"], f"{c}={row[c]} is not an integer mm")
        boxes.append({
            "id": row["id"], "seq": row["sequence"],
            "x": int(row["x"]), "y": int(row["y"]), "z": int(row["z"]),
            "lx": lx, "ly": ly, "h": h, "mass": mass,
            "sits_on": row.get("sits_on"),
        })
    return pallet, boxes, expected


# ------------------------------------------------------- rectangle helpers

def overlap_rect(a, b):
    """Intersection of two boxes' XY footprints, or None."""
    x0 = max(a["x"], b["x"]); x1 = min(a["x"] + a["lx"], b["x"] + b["lx"])
    y0 = max(a["y"], b["y"]); y1 = min(a["y"] + a["ly"], b["y"] + b["ly"])
    if x0 < x1 and y0 < y1:
        return (x0, y0, x1, y1)
    return None


def contact_rects(box, others):
    """XY rectangles where `box`'s base touches a top face at exactly its z."""
    rects = []
    for p in others:
        if p is box or p["z"] + p["h"] != box["z"]:
            continue
        r = overlap_rect(box, p)
        if r:
            rects.append((p, r))
    return rects


def qualified_supporters(box, others):
    """Supporters per Schuster: contact >= 25% of the supported box's base."""
    base = box["lx"] * box["ly"]
    out = []
    for p, (x0, y0, x1, y1) in contact_rects(box, others):
        if Fraction((x1 - x0) * (y1 - y0)) >= SUPPORTER_MIN_FRAC * base:
            out.append((p, (x0, y0, x1, y1)))
    return out


# ------------------------------------------------ convex hull (no scipy)

def convex_hull(points):
    """Monotone chain. Returns CCW hull; handles 1 point / collinear sets
    (returns the point or segment endpoints) without erroring."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    return hull if len(hull) >= 3 else [pts[0], pts[-1]]  # collinear -> segment


def hull_contains(hull, px, py):
    """Inclusive point-in-convex-polygon; exact (px, py may be Fractions)."""
    if not hull:
        return False
    if len(hull) == 1:
        return px == hull[0][0] and py == hull[0][1]
    if len(hull) == 2:
        (ax, ay), (bx, by) = hull
        cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
        return (cross == 0 and min(ax, bx) <= px <= max(ax, bx)
                and min(ay, by) <= py <= max(ay, by))
    for i in range(len(hull)):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % len(hull)]
        if (bx - ax) * (py - ay) - (by - ay) * (px - ax) < 0:
            return False
    return True


# ------------------------------------------------------------- the rules

def check_geometry(pallet, boxes):
    """Rule 1: inside pallet bounds, under height cap, no overlaps."""
    for b in boxes:
        if b["x"] < 0 or b["y"] < 0 or b["z"] < 0 \
                or b["x"] + b["lx"] > pallet["L"] or b["y"] + b["ly"] > pallet["W"]:
            raise Violation(1, b["id"], "outside pallet bounds")
        if pallet["max_h"] is not None and b["z"] + b["h"] > pallet["max_h"]:
            raise Violation(1, b["id"], f"exceeds height cap {pallet['max_h']} mm")
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if a["z"] < b["z"] + b["h"] and b["z"] < a["z"] + a["h"] \
                    and overlap_rect(a, b):
                raise Violation(1, b["id"], f"overlaps {a['id']}")


def check_support(pallet, boxes):
    """Rule 2: SUPPORT (a) area fraction + (b) opposite edges; sits_on honest."""
    for b in boxes:
        base = b["lx"] * b["ly"]
        if b["z"] == 0:
            supporters = ["deck"]
        else:
            rects = contact_rects(b, boxes)
            area = sum((x1 - x0) * (y1 - y0) for _, (x0, y0, x1, y1) in rects)
            if Fraction(area) < SUPPORT_FRAC * base:
                raise Violation(2, b["id"],
                                f"supported area {area}/{base} below "
                                f"{float(SUPPORT_FRAC):.0%}")
            # (b) opposite edges: some contact touching each of two opposite
            # edge lines of the base, along at least one axis (no diving boards)
            xmin = any(r[0] == b["x"] for _, r in rects)
            xmax = any(r[2] == b["x"] + b["lx"] for _, r in rects)
            ymin = any(r[1] == b["y"] for _, r in rects)
            ymax = any(r[3] == b["y"] + b["ly"] for _, r in rects)
            if not ((xmin and xmax) or (ymin and ymax)):
                raise Violation(2, b["id"],
                                "no pair of opposite edges supported (diving board)")
            supporters = sorted(p["id"] for p, _ in qualified_supporters(b, boxes))
            if not supporters:
                raise Violation(2, b["id"],
                                "no supporter reaches 25% base contact")
        if b["sits_on"] is not None and sorted(b["sits_on"]) != supporters:
            raise Violation(2, b["id"],
                            f"sits_on says {sorted(b['sits_on'])}, "
                            f"geometry says {supporters}")


def check_balance(boxes):
    """Rule 3: replay box by box; at every step, every box's combined COM
    (itself + everything transitively above) stays inside the support it
    receives from below."""
    for k in range(1, len(boxes) + 1):
        prefix = boxes[:k]
        rests_on = {b["id"]: [p for p, _ in qualified_supporters(b, prefix)]
                    for b in prefix}
        for b in prefix:
            group = _group_above(b, prefix, rests_on)
            msum = sum(g["mass"] for g in group)
            comx = sum(g["mass"] * Fraction(2 * g["x"] + g["lx"], 2) for g in group) / msum
            comy = sum(g["mass"] * Fraction(2 * g["y"] + g["ly"], 2) for g in group) / msum
            if b["z"] == 0:
                corners = [(b["x"], b["y"]), (b["x"] + b["lx"], b["y"]),
                           (b["x"], b["y"] + b["ly"]),
                           (b["x"] + b["lx"], b["y"] + b["ly"])]
            else:
                corners = []
                for _, (x0, y0, x1, y1) in qualified_supporters(b, prefix):
                    corners += [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
            if not hull_contains(convex_hull(corners), comx, comy):
                raise Violation(
                    3, b["id"],
                    f"after placing {prefix[-1]['id']} (step {k}), combined COM "
                    f"({float(comx):.1f}, {float(comy):.1f}) of {len(group)} "
                    f"box(es) falls outside {b['id']}'s support")


def _group_above(b, boxes, rests_on):
    """b plus every box transitively resting on it (qualified supporters)."""
    group, frontier = {b["id"]: b}, [b]
    while frontier:
        cur = frontier.pop()
        for q in boxes:
            if q["id"] not in group and cur in rests_on[q["id"]]:
                group[q["id"]] = q
                frontier.append(q)
    return list(group.values())


def check_completeness(plan, boxes, expected):
    """Rule 4: placed + leftover = given, no duplicates, nothing vanishes."""
    placed = [b["id"] for b in boxes]
    leftovers = list(plan.get("leftovers", []))
    seen = placed + leftovers
    if len(seen) != len(set(seen)):
        dup = sorted({i for i in seen if seen.count(i) > 1})[0]
        raise Violation(4, dup, "appears more than once")
    if set(seen) != expected:
        missing = sorted(expected - set(seen)) + sorted(set(seen) - expected)
        raise Violation(4, missing[0], "placed + leftovers != given boxes")


def _skyline(pallet, boxes):
    """Exact skyline via coordinate compression: yields (cell area, height)."""
    xs = sorted({0, pallet["L"], *(b["x"] for b in boxes),
                 *(b["x"] + b["lx"] for b in boxes)})
    ys = sorted({0, pallet["W"], *(b["y"] for b in boxes),
                 *(b["y"] + b["ly"] for b in boxes)})
    for x0, x1 in zip(xs, xs[1:]):
        for y0, y1 in zip(ys, ys[1:]):
            h = max((b["z"] + b["h"] for b in boxes
                     if b["x"] <= x0 and x1 <= b["x"] + b["lx"]
                     and b["y"] <= y0 and y1 <= b["y"] + b["ly"]), default=0)
            yield (x1 - x0) * (y1 - y0), h


def _polygon_area2(hull):
    """Twice the polygon area (shoelace), integer-exact."""
    if len(hull) < 3:
        return 0
    return abs(sum(hull[i][0] * hull[(i + 1) % len(hull)][1]
                   - hull[(i + 1) % len(hull)][0] * hull[i][1]
                   for i in range(len(hull))))


def top_coverage(pallet, boxes):
    """Rule 5 (LEVEL TOP): the upper pallet's contact = the skyline cells
    within FLAT_TOL of the maximum height. Returns (area fraction,
    spread fraction = hull of the contact over the deck, hmax)."""
    if not boxes:
        return Fraction(0), Fraction(0), 0
    deck = pallet["L"] * pallet["W"]
    hmax = max(b["z"] + b["h"] for b in boxes)
    area = sum(a for a, h in _skyline(pallet, boxes) if h >= hmax - FLAT_TOL)
    corners = []
    for b in boxes:
        if b["z"] + b["h"] >= hmax - FLAT_TOL:
            corners += [(b["x"], b["y"]), (b["x"] + b["lx"], b["y"]),
                        (b["x"], b["y"] + b["ly"]),
                        (b["x"] + b["lx"], b["y"] + b["ly"])]
    spread2 = _polygon_area2(convex_hull(corners)) if corners else 0
    return Fraction(area, deck), Fraction(spread2, 2 * deck), hmax


def footprint_used(pallet, boxes):
    """Fraction of the deck covered by at least one box (projected)."""
    if not boxes:
        return Fraction(0)
    area = sum(a for a, h in _skyline(pallet, boxes) if h > 0)
    return Fraction(area, pallet["L"] * pallet["W"])


# --------------------------------------------------------------- verdict

def verify(plan, case):
    """Run all rules. Returns the verdict dict; raises Violation on rules 1-4."""
    pallet, boxes, expected = load_boxes(plan, case)
    check_geometry(pallet, boxes)
    check_support(pallet, boxes)
    check_balance(boxes)
    check_completeness(plan, boxes, expected)
    cover, spread, hmax = top_coverage(pallet, boxes)
    return {
        "static_support": "PASS",
        "stackable": cover >= STACK_AREA_FRAC and spread >= STACK_SPREAD_FRAC,
        "top_coverage_pct": round(float(cover) * 100, 1),
        "top_spread_pct": round(float(spread) * 100, 1),
        "footprint_used_pct": round(float(footprint_used(pallet, boxes)) * 100, 1),
        "stack_height_mm": hmax,
        "placed": len(boxes),
        "total": len(expected),
        "leftovers": sorted(plan.get("leftovers", [])),
    }


def main(argv):
    if len(argv) != 3:
        print("usage: python3 verify.py plan.json case.json", file=sys.stderr)
        return 2
    with open(argv[1]) as f:
        plan = json.load(f)
    with open(argv[2]) as f:
        case = json.load(f)
    try:
        v = verify(plan, case)
    except Violation as e:
        print(e)
        return 1
    print("PASS")
    print(f"stackable: {'yes' if v['stackable'] else 'NO'} "
          f"(top contact {v['top_coverage_pct']}% of deck, need "
          f"{float(STACK_AREA_FRAC) * 100:.0f}%; contact spread "
          f"{v['top_spread_pct']}%, need {float(STACK_SPREAD_FRAC) * 100:.0f}%)")
    print(f"placed: {v['placed']}/{v['total']}   "
          f"leftovers: {', '.join(v['leftovers']) or '-'}")
    print(f"stack height: {v['stack_height_mm']} mm above deck")
    print(f"footprint used: {v['footprint_used_pct']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
