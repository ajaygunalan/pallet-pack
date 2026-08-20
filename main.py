#!/usr/bin/env python3
"""The whole pipeline in one command: solve -> verify -> render.

    uv run main.py cases/acceptance_1.json

Writes plan.json and plan.html next to the current directory; prints the
build plan and the independent verifier's verdict. Exit code is the
verifier's — the solver never grades its own homework: verify.py
runs as a real separate process.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import viewer
from solver import PATTERN_CHOICES, TIME_BUDGET, load_problem, pack, patterns_for

HERE = Path(__file__).parent


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("case")
    ap.add_argument("-o", "--outdir", default=".")
    ap.add_argument("--time-budget", type=float, default=TIME_BUDGET,
                    help="search time budget in seconds")
    ap.add_argument("--pattern", choices=PATTERN_CHOICES,
                    default="auto",
                    help="stacking pattern: tiled = classic tight tiling, "
                         "platform = spread the tallest boxes to carry the "
                         "upper pallet, auto = try both, the score picks")
    args = ap.parse_args()
    patterns = patterns_for(args.pattern)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    plan_path = outdir / f"plan_{args.pattern}.json"
    html_path = outdir / f"plan_{args.pattern}.html"

    t0 = time.monotonic()
    plan = pack(load_problem(args.case), args.time_budget, patterns=patterns)
    plan["case_name"] = Path(args.case).stem
    plan["verdict"]["pattern_requested"] = args.pattern
    plan["verdict"]["time_budget_s"] = args.time_budget
    plan["verdict"]["solve_seconds"] = round(time.monotonic() - t0, 1)
    plan_path.write_text(json.dumps(plan, indent=1))

    print(f"#  | box id | x, y, z (mm)      | rotated | sits on")
    for r in plan["placements"]:
        print(f"{r['sequence']:<2} | {r['id']:<6} | "
              f"{r['x']:>4}, {r['y']:>3}, {r['z']:>3}  | "
              f"{str(r['rotated']).lower():<7} | {', '.join(r['sits_on'])}")
    if plan["leftovers"]:
        print(f"leftovers: {', '.join(plan['leftovers'])}")
    print(f"solved in {plan['verdict']['solve_seconds']}s "
          f"(beam widths {plan['verdict']['beam_widths_run']}) "
          f"-> {plan_path}")

    print("\n--- verifier (verify.py, separate process) ---", flush=True)
    r = subprocess.run([sys.executable, str(HERE / "verify.py"),
                        str(plan_path), args.case])

    viewer.render(plan, str(html_path))
    print(f"\n3D step-through -> {html_path}")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
