#!/usr/bin/env python3
"""Pallet packing solver.

Reads a case (pallet size + box list), searches for an arrangement that is
stable and can carry a second pallet on top, and writes the build plan.

The file is organized top to bottom:

  1. CONSTANTS    thresholds used by the stability and stackability checks
  2. INPUT        loading a case; the SKU, Problem, and Placement records
  3. CHECKS       the two stability checks: SUPPORT and BALANCE
  4. CANDIDATES   the shortlist of positions worth trying for a box
  5. STATE        one half-finished pallet: heightmap + placed + remaining
  6. ROLLOUT      finishes a half-built pallet, so it can be judged by
                  its ending
  7. SEARCH       the beam: w competing futures, best ending wins
  8. OUTPUT       writing the plan file; command-line entry

Deterministic: the same input always produces the same plan.

    uv run solver.py cases/acceptance_1.json -o plan.json
"""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from fractions import Fraction
from hashlib import blake2b

import numpy as np

# ============================================================================
# 1. CONSTANTS
#
# The numeric thresholds used by the checks in this file: how much of a
# box's base must be supported, what counts as a flat top, the beam widths,
# the time budget. verify.py declares the same values separately, so the
# verifier never depends on this file.
# ============================================================================
SUPPORT_FRAC = Fraction(70, 100)        # SUPPORT (a): min supported base area
SUPPORTER_MIN_FRAC = Fraction(25, 100)  # supporter needs >= 25% base contact
FLAT_TOL = 10                           # mm: "level" tolerance for the top
# LEVEL TOP: the upper pallet is a RIGID slab, so it does not need most of
# its area touched — it needs flat contact that is SPREAD to its edges:
STACK_AREA_FRAC = Fraction(20, 100)     # min deck fraction in top-level contact
STACK_SPREAD_FRAC = Fraction(60, 100)   # min deck fraction covered by the
                                        #   convex hull of that contact
BEAM_WIDTHS = (1, 2, 4, 8)              # restart schedule for the search
TIME_BUDGET = 60.0                      # seconds, whole pack() run


def snap(v):
    """Round a millimeter value to the nearest whole millimeter."""
    return int(math.floor(float(v) + 0.5))


# ============================================================================
# 2. INPUT
#
# load_problem() reads a case file (pallet size + box types with counts and
# optional weights) and returns a Problem. A SKU is one box type; a
# Placement is one placed box — one row of the final build plan.
# ============================================================================

@dataclass(frozen=True)
class SKU:
    idx: int                # position in input order (determinism anchor)
    code: str
    l: int; w: int; h: int  # snapped mm
    count: int
    mass: int               # grams if all weights given, else volume proxy


@dataclass(frozen=True)
class Problem:
    L: int; W: int          # pallet deck, mm
    max_h: int | None       # cap on load height above the deck (z + h)
    skus: tuple[SKU, ...]
    case: dict              # snapped echo of the input, for the plan file


@dataclass(frozen=True)
class Placement:
    seq: int
    code: str; instance: int
    x: int; y: int; z: int
    rotated: bool
    lx: int; ly: int; h: int
    mass: int
    sits_on: tuple[str, ...]

    @property
    def id(self):
        return box_id(self.code, self.instance)


def box_id(code: str, instance: int) -> str:
    """The one place the '<SKU>-<n>' box id format is written."""
    return f"{code}-{instance}"


def load_problem(path_or_dict) -> Problem:
    """Ingest a case file; the ONLY place decimals are snapped to the grid."""
    case = path_or_dict
    if not isinstance(case, dict):
        with open(case) as f:
            case = json.load(f)
    p = case["pallet"]
    weights_given = all(s.get("weight") is not None for s in case["skus"])
    skus = []
    for i, s in enumerate(case["skus"]):
        l, w, h = (snap(d) for d in s["dims"])
        mass = snap(s["weight"] * 1000) if weights_given else l * w * h
        skus.append(SKU(i, s["sku"], l, w, h, int(s["count"]), mass))
    snapped = {
        "pallet": {"length": snap(p["length"]), "width": snap(p["width"]),
                   "deck_height": snap(p["deck_height"]),
                   "max_stack_height": None if p.get("max_stack_height") is None
                   else snap(p["max_stack_height"])},
        "skus": [{"sku": s.code, "dims": [s.l, s.w, s.h], "count": s.count,
                  "weight": case["skus"][s.idx].get("weight")} for s in skus],
    }
    sp = snapped["pallet"]
    return Problem(sp["length"], sp["width"], sp["max_stack_height"],
                   tuple(skus), snapped)


def footprint(sku: SKU, rotated: bool) -> tuple[int, int]:
    """rotated=True: first given dimension along X."""
    return (sku.l, sku.w) if rotated else (sku.w, sku.l)


# ============================================================================
# 3. CHECKS: SUPPORT AND BALANCE
#
# The two checks that decide whether a placement is legal. support_ok():
# enough of the box's base rests on what is below, touching two opposite
# edges. balance_ok(): for every box in the stack, the combined center of
# mass of that box plus everything above it stays inside the area holding
# it up. A placement that fails either check is discarded.
# ============================================================================

def support_ok(hm, x, y, lx, ly, z) -> bool:
    """SUPPORT check: (a) enough of the base rests at landing height,
    (b) contact touches two opposite edges of the base along some axis."""
    if z == 0:
        return True                     # the deck supports fully
    contact = hm[x:x + lx, y:y + ly] == z
    if int(contact.sum()) < SUPPORT_FRAC * (lx * ly):
        return False                    # (a) see-saw
    return bool((contact[0].any() and contact[-1].any())
                or (contact[:, 0].any() and contact[:, -1].any()))  # (b)


def _contact_rect(a: Placement, x, y, lx, ly):
    """XY intersection of placement a's footprint with a base rect, or None."""
    x0, x1 = max(a.x, x), min(a.x + a.lx, x + lx)
    y0, y1 = max(a.y, y), min(a.y + a.ly, y + ly)
    return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None


def _supporters(placements, x, y, lx, ly, z):
    """Boxes whose top is exactly z under the base, with >= 25% base contact."""
    out = []
    for p in placements:
        if p.z + p.h != z:
            continue
        r = _contact_rect(p, x, y, lx, ly)
        if r and Fraction((r[2] - r[0]) * (r[3] - r[1])) >= SUPPORTER_MIN_FRAC * (lx * ly):
            out.append((p, r))
    return out


def convex_hull(points):
    """Monotone chain; survives collinear/degenerate input (returns the
    point or segment endpoints) — this is why scipy was dropped."""
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
    return hull if len(hull) >= 3 else [pts[0], pts[-1]]


def hull_contains(hull, px, py) -> bool:
    """Inclusive point-in-convex-polygon; px/py may be Fractions — exact."""
    if not hull:
        return False
    if len(hull) == 1:
        return px == hull[0][0] and py == hull[0][1]
    if len(hull) == 2:
        (ax, ay), (bx, by) = hull
        return ((bx - ax) * (py - ay) - (by - ay) * (px - ax) == 0
                and min(ax, bx) <= px <= max(ax, bx)
                and min(ay, by) <= py <= max(ay, by))
    for i in range(len(hull)):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % len(hull)]
        if (bx - ax) * (py - ay) - (by - ay) * (px - ax) < 0:
            return False
    return True


def polygon_area2(hull) -> int:
    """Twice the polygon area (shoelace) — kept doubled to stay integer."""
    if len(hull) < 3:
        return 0
    s = 0
    for i in range(len(hull)):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % len(hull)]
        s += ax * by - bx * ay
    return abs(s)


def top_support(placements, hmax):
    """What the upper pallet would rest on: boxes whose top is within
    FLAT_TOL of the maximum. Returns (contact area, 2x hull area) — the
    hull spread is what stops a flat but narrow/clumped platform from
    counting as stackable (a rigid pallet on it would see-saw)."""
    tops = [p for p in placements if p.z + p.h >= hmax - FLAT_TOL]
    area = sum(p.lx * p.ly for p in tops)
    corners = []
    for p in tops:
        corners += [(p.x, p.y), (p.x + p.lx, p.y),
                    (p.x, p.y + p.ly), (p.x + p.lx, p.y + p.ly)]
    spread2 = polygon_area2(convex_hull(corners)) if corners else 0
    return area, spread2


def balance_ok(placements, new: Placement) -> bool:
    """BALANCE check: for every box, the combined COM of it plus
    everything transitively above stays inside the support it receives from
    below. Only boxes below the new box can be affected, so only that chain
    is rechecked."""
    stack = list(placements) + [new]
    index = {id(p): j for j, p in enumerate(stack)}
    below = {i: [] for i in range(len(stack))}   # i rests on below[i]
    for i, b in enumerate(stack):
        if b.z > 0:
            # a box never qualifies as its own supporter: its top is h above
            # its own base, so the z filter in _supporters excludes it
            sup = _supporters(stack, b.x, b.y, b.lx, b.ly, b.z)
            below[i] = [index[id(p)] for p, _ in sup]
            if not below[i]:
                return False                     # nothing qualifies as support

    # downset: the new box and everything transitively beneath it
    downset, frontier = {len(stack) - 1}, [len(stack) - 1]
    while frontier:
        for j in below[frontier.pop()]:
            if j not in downset:
                downset.add(j)
                frontier.append(j)

    above = {i: [] for i in range(len(stack))}
    for i, js in below.items():
        for j in js:
            above[j].append(i)

    for i in sorted(downset):
        b = stack[i]
        group, front = {i}, [i]
        while front:
            for j in above[front.pop()]:
                if j not in group:
                    group.add(j)
                    front.append(j)
        msum = sum(stack[j].mass for j in group)
        comx = Fraction(sum(stack[j].mass * (2 * stack[j].x + stack[j].lx)
                            for j in group), 2 * msum)
        comy = Fraction(sum(stack[j].mass * (2 * stack[j].y + stack[j].ly)
                            for j in group), 2 * msum)
        if b.z == 0:
            corners = [(b.x, b.y), (b.x + b.lx, b.y),
                       (b.x, b.y + b.ly), (b.x + b.lx, b.y + b.ly)]
        else:
            corners = []
            for j in below[i]:
                r = _contact_rect(stack[j], b.x, b.y, b.lx, b.ly)
                corners += [(r[0], r[1]), (r[2], r[1]), (r[0], r[3]), (r[2], r[3])]
        if not hull_contains(convex_hull(corners), comx, comy):
            return False
    return True


# ============================================================================
# 4. CANDIDATES — collapsing a million possible (x, y) positions for the
#    next box into the dozen actually worth trying
#
# In any tight packing every box ends up flush against something — the
# pallet edge or another box — so that is the rule: only flush positions
# are tried. _axis_anchors() collects the flush positions along one axis:
# run on X it returns the few x values where the box would sit against a
# deck edge or against a placed box's side; run on Y, the same for y.
# candidate_spots() then crosses the two lists into a small grid of (x, y)
# spots and looks up each spot's height z from the heightmap — the box
# lands on whatever is tallest beneath it, so z is never a choice.
# ============================================================================

def _axis_anchors(intervals, size, limit):
    """The flush positions on one axis, for a box of this size: against
    either deck edge, or against either side of every placed box.
    Positions that would hang off the deck are dropped."""
    vals = {0, limit - size}
    for a0, a1 in intervals:
        vals.update((a0, a1, a0 - size, a1 - size))
    return [v for v in sorted(vals) if 0 <= v <= limit - size]


def candidate_spots(state: "State", lx, ly):
    """Every (x, y) pair from the X and Y anchor lists, each with its
    landing height z looked up from the heightmap. (The per-row column max
    keeps the lookup cheap — the grid is never rescanned per spot.)"""
    xs = _axis_anchors([(p.x, p.x + p.lx) for p in state.placements], lx, state.problem.L)
    ys = _axis_anchors([(p.y, p.y + p.ly) for p in state.placements], ly, state.problem.W)
    out = []
    for x in xs:
        colmax = state.hm[x:x + lx].max(axis=0)
        for y in ys:
            out.append((x, y, int(colmax[y:y + ly].max())))
    return out


# ============================================================================
# 5. STATE — one half-finished pallet; the search keeps several alive at
#    once, each a different possible future
#
# A State holds three things: the heightmap (what the terrain looks like),
# the placements so far (how it got there), and the remaining counts (what
# is still in the pile). The beam search refuses to commit to a single
# future, so each candidate future needs its own private copy of all
# three. copy() forks a world — "what if the next box went there
# instead?"; place() advances one world by one box, updating all three
# fields together; snapshot_score() grades a world: is the top stackable,
# how much volume is on, how flat is the top.
# ============================================================================

class State:
    """One beam copy = one alternative world: heightmap + placements +
    remaining counts. Copy is a memcpy; placements are frozen and shared."""
    __slots__ = ("problem", "hm", "placements", "remaining", "placed_per_sku",
                 "hmax", "volume", "used")

    def __init__(self, problem: Problem):
        self.problem = problem
        self.hm = np.zeros((problem.L, problem.W), dtype=np.int16)
        self.placements: list[Placement] = []
        self.remaining = [s.count for s in problem.skus]
        self.placed_per_sku = [0] * len(problem.skus)
        self.hmax = 0
        self.volume = 0
        self.used = 0                   # deck cells covered by at least one box

    def copy(self) -> "State":
        c = State.__new__(State)
        c.problem = self.problem
        c.hm = self.hm.copy()
        c.placements = list(self.placements)
        c.remaining = list(self.remaining)
        c.placed_per_sku = list(self.placed_per_sku)
        c.hmax, c.volume, c.used = self.hmax, self.volume, self.used
        return c

    def key(self) -> bytes:
        """Twin key: same heightmap + same placed multiset —
        different routes to one stack collapse."""
        return blake2b(self.hm.tobytes() + repr(self.remaining).encode(),
                       digest_size=16).digest()

    def place(self, sku: SKU, rotated: bool, x: int, y: int, z: int) -> Placement:
        lx, ly = footprint(sku, rotated)
        sits_on = (("deck",) if z == 0 else tuple(sorted(
            p.id for p, _ in _supporters(self.placements, x, y, lx, ly, z))))
        self.placed_per_sku[sku.idx] += 1
        p = Placement(len(self.placements) + 1, sku.code,
                      self.placed_per_sku[sku.idx], x, y, z, rotated,
                      lx, ly, sku.h, sku.mass, sits_on)
        self.placements.append(p)
        self.remaining[sku.idx] -= 1
        self.used += int((self.hm[x:x + lx, y:y + ly] == 0).sum())
        self.hm[x:x + lx, y:y + ly] = z + sku.h
        self.hmax = max(self.hmax, z + sku.h)
        self.volume += sku.l * sku.w * sku.h
        return p

    def _top_stats(self):
        """The LEVEL TOP numbers, computed in ONE place: (top contact area,
        2x hull spread, passes-both-thresholds)."""
        area, spread2 = top_support(self.placements, self.hmax)
        deck = self.problem.L * self.problem.W
        valid = (area >= STACK_AREA_FRAC * deck
                 and spread2 >= STACK_SPREAD_FRAC * 2 * deck)
        return area, spread2, valid

    def snapshot_score(self):
        """(valid, volume, top area): valid iff the top passes LEVEL TOP —
        flat contact (within FLAT_TOL of hmax) with enough area AND enough
        spread for a rigid upper pallet. Top
        contact area breaks volume ties, so of two full packings the one
        with the fuller, flatter top wins."""
        if not self.placements:
            return (0, 0, 0)
        area, _, valid = self._top_stats()
        return (int(valid), self.volume, area)

    def metrics(self):
        area, spread2, valid = self._top_stats()
        deck = self.problem.L * self.problem.W
        return {"stackable": bool(valid),
                "top_coverage_pct": round(area / deck * 100, 1),
                "top_spread_pct": round(spread2 / (2 * deck) * 100, 1),
                "footprint_used_pct": round(self.used / deck * 100, 1),
                "stack_height_mm": self.hmax}


# ============================================================================
# 6. ROLLOUT — finishing a half-built pallet greedily, so the search can
#    judge it by where it ends up
#
# A half-built pallet cannot be judged by looking at it — only by its
# ending. rollout() plays the remaining boxes to the end with no lookahead
# and remembers the best moment passed along the way; the final plan may
# stop at that peak and call the rest leftovers. Each single move is
# place_best(): try the candidate spots in preference order — keep the top
# low, land low, hug the corner — and take the first that passes both
# checks. A rollout can run in two patterns, and the search grades both
# endings: "tiled" packs heaviest-first, tight from a corner; "platform"
# (_place_spread()) spreads the tallest boxes out like table legs to carry
# a second pallet, keeping every other box below that height.
# ============================================================================

def _sku_order(problem):
    """Heaviest first; input order breaks ties."""
    return sorted(range(len(problem.skus)),
                  key=lambda i: (-problem.skus[i].mass, i))


def _fits_cap(problem, z, h):
    return problem.max_h is None or z + h <= problem.max_h


def fitting_spots(state: "State", sku: SKU):
    """Every candidate spot for `sku` that fits the deck and the height
    cap, in deterministic order: unrotated first, then candidate_spots()
    order. Yields (rotated, lx, ly, x, y, z). This is the ONE place the
    rotation loop, the deck-fit test, and the cap test live — the two
    stability checks are applied later, by the caller. A square box is
    tried unrotated only: its rotated twin lands identically."""
    rots = (False,) if sku.l == sku.w else (False, True)
    for rotated in rots:
        lx, ly = footprint(sku, rotated)
        if lx > state.problem.L or ly > state.problem.W:
            continue
        for x, y, z in candidate_spots(state, lx, ly):
            if _fits_cap(state.problem, z, sku.h):
                yield rotated, lx, ly, x, y, z


def passes_checks(state: "State", sku: SKU, rotated: bool, x, y, z) -> bool:
    """Both stability checks: SUPPORT, then BALANCE — filters, never
    penalties. The height cap is applied at candidate generation instead."""
    lx, ly = footprint(sku, rotated)
    if not support_ok(state.hm, x, y, lx, ly, z):
        return False
    probe = Placement(0, sku.code, 0, x, y, z, rotated,
                      lx, ly, sku.h, sku.mass, ())
    return balance_ok(state.placements, probe)


def place_best(state: State, sku: SKU, top_cap=None) -> bool:
    """One greedy placement of `sku` at its best spot that passes both checks, or False.
    Candidates are sorted by the cheap preference key first, then checked
    lazily — the checks usually run only a handful of times per step.
    `top_cap` (platform pattern) refuses spots that would poke above it."""
    cands = []
    for rotated, lx, ly, x, y, z in fitting_spots(state, sku):
        if top_cap is not None and z + sku.h > top_cap:
            continue
        # preference order: finish the current level (lowest new
        # top), land low (COM lowest), then corner-anchored tight tiling.
        # "COM most centered" is deliberately not a spot key — it conflicts
        # with corner anchoring (it scatters boxes across the deck), and
        # centering is already enforced where it matters by the BALANCE check.
        new_top = max(state.hmax, z + sku.h)
        cands.append((new_top, z, x, y, rotated))
    cands.sort()
    for _, z, x, y, rotated in cands:
        if passes_checks(state, sku, rotated, x, y, z):
            state.place(sku, rotated, x, y, z)
            return True
    return False


def _place_spread(state: State, sku: SKU, top_h: int, platform) -> bool:
    """Platform pattern, phase A: place one box whose top lands EXACTLY at
    `top_h`, at the spot that spreads the platform widest — perimeter spots
    first, then maximum distance from the other platform boxes. This is what
    puts support under the upper pallet's edges and corners."""
    cands = []
    for rotated, lx, ly, x, y, z in fitting_spots(state, sku):
        if z + sku.h != top_h:
            continue
        perim = int(x == 0 or y == 0 or x + lx == state.problem.L
                    or y + ly == state.problem.W)
        mind = min(((2 * x + lx - 2 * q.x - q.lx) ** 2
                    + (2 * y + ly - 2 * q.y - q.ly) ** 2
                    for q in platform), default=0)
        cands.append((-perim, -mind, x, y, rotated, z))
    cands.sort()
    for _, _, x, y, rotated, z in cands:
        if passes_checks(state, sku, rotated, x, y, z):
            platform.append(state.place(sku, rotated, x, y, z))
            return True
    return False


def _greedy_fill(state: State, track, order, top_cap=None):
    """The greedy inner loop: heaviest first, set-aside boxes retried every
    pass after the stack changes, until a pass places nothing."""
    progress = True
    while progress:
        progress = False
        for si in order:
            while state.remaining[si] > 0:
                if not place_best(state, state.problem.skus[si], top_cap):
                    break
                progress = True
                track()


def rollout(state: State, pattern: str = "tiled"):
    """Finish the packing greedily, tracking the best valid
    snapshot along the way. Mutates `state`; returns (best score, prefix
    length, metrics at best).

    Two stacking patterns, both banked by the search (best snapshot wins):
      tiled    — corner-tiled, volume-first
      platform — spread the tallest boxes to carry the upper pallet at one
                 height, then tuck everything else strictly below that plane
    The pattern is the search's EVALUATOR, not its objective: each ending is
    judged by the same score (stackable first, then volume) either way.
    """
    best = [state.snapshot_score(), len(state.placements), state.metrics()]

    def track():
        score = state.snapshot_score()
        if score > best[0]:
            best[0], best[1], best[2] = score, len(state.placements), state.metrics()

    order = _sku_order(state.problem)
    if pattern == "platform":
        top_h = max([state.hmax] + [s.h for s in state.problem.skus
                                    if state.remaining[s.idx] > 0])
        platform = [p for p in state.placements if p.z + p.h == top_h]
        for si in order:                            # phase A: build the platform
            while state.remaining[si] > 0:
                if not _place_spread(state, state.problem.skus[si], top_h, platform):
                    break
                track()
        _greedy_fill(state, track, order, top_cap=top_h)  # phase B: fill below
    else:
        _greedy_fill(state, track, order)
    return best[0], best[1], best[2]


# ============================================================================
# 7. SEARCH — keeping w disagreeing futures alive instead of trusting the
#    first good-looking move (beam search with greedy rollouts, "BSG")
#
# A placement can look fine now and quietly ruin the packing three boxes
# later — greedy alone would never notice. So solve() branches: for each
# kept State it forks one child world per legal next placement, grades
# every child by its rollout() ending, deletes twins (different routes to
# the same stack add nothing), and keeps only the w best. One plan beats
# another by being stackable, then by placing more volume, then by the
# flatter top. pack() reruns solve() at w = 1, 2, 4, 8 while the time
# budget lasts — w = 1 IS plain greedy — and returns the best plan seen
# anywhere along the way.
# ============================================================================

class _Bank:
    """Best complete plan seen across all rollouts, runs, and widths."""

    def __init__(self):
        self.score = (-1, -1)
        self.rows: tuple[Placement, ...] = ()
        self.metrics = {}

    def offer(self, score, rows, metrics):
        if score > self.score:
            self.score, self.rows, self.metrics = score, tuple(rows), metrics


PATTERNS = ("tiled", "platform")        # rollout patterns; order breaks ties
PATTERN_CHOICES = ("auto",) + PATTERNS  # the --pattern CLI vocabulary


def patterns_for(choice: str) -> tuple[str, ...]:
    """A --pattern CLI value -> the patterns tuple pack() should run."""
    return PATTERNS if choice == "auto" else (choice,)


def solve(problem: Problem, width: int, deadline=None, memo=None, bank=None,
          patterns=PATTERNS) -> _Bank:
    """One BSG run at fixed beam width; width=1 is bare greedy."""
    memo = {} if memo is None else memo
    bank = _Bank() if bank is None else bank

    def expired():
        return deadline is not None and time.monotonic() > deadline

    def evaluate(child_state, k):
        hit = memo.get(k)
        if hit is None:
            for pattern in patterns:                 # first wins score ties
                scratch = child_state.copy()
                score, n, metrics = rollout(scratch, pattern)
                cand = (score, tuple(scratch.placements[:n]),
                        dict(metrics, pattern=pattern))
                if hit is None or cand[0] > hit[0]:
                    hit = cand
            memo[k] = hit
        bank.offer(*hit)
        return hit[0]

    root = State(problem)
    evaluate(root, root.key())          # pure-greedy baseline, always banked
    beam = [root]
    while beam and not expired():
        seen = set()
        kept = []                       # top-w children: (score, order, state)
        n_children = 0
        for copy in beam:
            for si, sku in enumerate(problem.skus):
                if copy.remaining[si] == 0:
                    continue
                for rotated, lx, ly, x, y, z in fitting_spots(copy, sku):
                    if expired():
                        return bank
                    if not passes_checks(copy, sku, rotated, x, y, z):
                        continue
                    child = copy.copy()
                    child.place(sku, rotated, x, y, z)
                    k = child.key()
                    if k in seen:       # twin deletion
                        continue
                    seen.add(k)
                    score = evaluate(child, k)
                    n_children += 1
                    kept.append((score, -n_children, child))
                    kept.sort(key=lambda t: (t[0], t[1]), reverse=True)
                    del kept[width:]
        beam = [st for _, _, st in kept]
    return bank


def pack(problem: Problem, time_budget=TIME_BUDGET, widths=BEAM_WIDTHS,
         patterns=PATTERNS) -> dict:
    """Restart schedule: full runs at w = 1, 2, 4, 8 while the
    budget lasts; the best plan from any run wins. Returns the plan dict.
    `patterns` restricts the rollout patterns — e.g. ("tiled",) forces the
    classic tight tiling even when a platform plan would score higher."""
    deadline = None if time_budget is None else time.monotonic() + time_budget
    memo, bank = {}, _Bank()
    widths_run = []
    for w in widths:
        if deadline is not None and time.monotonic() > deadline:
            break
        solve(problem, w, deadline, memo, bank, patterns)
        widths_run.append(w)
    return build_plan(problem, bank, widths_run)


# ============================================================================
# 8. OUTPUT
#
# build_plan() turns the winning placements into the plan dictionary that
# is written as plan.json; main() below is the command-line entry.
# ============================================================================

def build_plan(problem: Problem, bank: _Bank, widths_run) -> dict:
    placed_per_sku = {s.code: 0 for s in problem.skus}
    rows = []
    for p in bank.rows:
        placed_per_sku[p.code] += 1
        rows.append({"sequence": p.seq, "id": p.id, "sku": p.code,
                     "x": p.x, "y": p.y, "z": p.z, "rotated": p.rotated,
                     "sits_on": list(p.sits_on)})
    leftovers = [box_id(s.code, i) for s in problem.skus
                 for i in range(placed_per_sku[s.code] + 1, s.count + 1)]
    total = sum(s.count for s in problem.skus)
    return {
        "case": problem.case,
        "placements": rows,
        "leftovers": leftovers,
        "verdict": {"static_support": "PASS",  # by construction; verifier re-judges
                    **bank.metrics,
                    "placed": len(rows), "total": total,
                    "beam_widths_run": widths_run},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("case")
    ap.add_argument("-o", "--out", default="plan.json")
    ap.add_argument("--time-budget", type=float, default=TIME_BUDGET,
                    help="search time budget in seconds")
    ap.add_argument("--pattern", choices=PATTERN_CHOICES,
                    default="auto", help="restrict the stacking pattern")
    args = ap.parse_args()
    patterns = patterns_for(args.pattern)
    problem = load_problem(args.case)
    t0 = time.monotonic()
    plan = pack(problem, args.time_budget, patterns=patterns)
    plan["verdict"]["solve_seconds"] = round(time.monotonic() - t0, 1)
    with open(args.out, "w") as f:
        json.dump(plan, f, indent=1)
    v = plan["verdict"]
    print(f"placed {v['placed']}/{v['total']}, "
          f"stackable={v['stackable']}, height {v['stack_height_mm']} mm, "
          f"widths run {v['beam_widths_run']}, {v['solve_seconds']}s -> {args.out}")


if __name__ == "__main__":
    main()
