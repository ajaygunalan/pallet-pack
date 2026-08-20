#!/usr/bin/env python3
"""3D step-through viewer: plan.json -> self-contained plan.html.

One Mesh3d + wire outline per box, grouped per SKU in the legend (click a
legend entry to hide/show that SKU); a slider replays the build order
(visibility toggling — the frames API renders 3D meshes badly); camera preset
buttons for isometric / top / front / side views; a panel with the solver's
key parameters and the verdict.

    uv run viewer.py plan.json -o plan.html
"""
from __future__ import annotations

import argparse
import json

import plotly.graph_objects as go

import solver

PALETTE = ["#e63946", "#457b9d", "#2a9d8f", "#e9c46a", "#9b5de5",
           "#f4a261", "#00b4d8", "#ef476f", "#8ac926", "#6d597a"]

CAMERAS = {
    "isometric": dict(eye=dict(x=1.5, y=1.3, z=0.9)),
    "top":       dict(eye=dict(x=0, y=0, z=2.2), up=dict(x=0, y=1, z=0)),
    "front":     dict(eye=dict(x=0, y=-2.0, z=0.3)),
    "side":      dict(eye=dict(x=2.2, y=0, z=0.3)),
}


# Boxes that touch share the exact same face plane, which makes WebGL
# flicker between them at some angles (z-fighting). Drawing every box inset
# by this much — display only, never in the data — keeps faces apart.
VISUAL_GAP = 1.0


def _inset(x, y, z, dx, dy, dz):
    """Shrink a box by the visual gap on every side (display only)."""
    g = min(VISUAL_GAP, dx / 4, dy / 4, dz / 4)
    return x + g, y + g, z + g, dx - 2 * g, dy - 2 * g, dz - 2 * g


def box_mesh(x, y, z, dx, dy, dz, color, name, hover, group, showlegend):
    x, y, z, dx, dy, dz = _inset(x, y, z, dx, dy, dz)
    xs = [x, x + dx, x + dx, x, x, x + dx, x + dx, x]
    ys = [y, y, y + dy, y + dy, y, y, y + dy, y + dy]
    zs = [z] * 4 + [z + dz] * 4
    return go.Mesh3d(
        x=xs, y=ys, z=zs,
        i=[0, 0, 0, 0, 4, 4, 0, 0, 1, 1, 3, 3],
        j=[1, 2, 4, 3, 5, 6, 1, 5, 2, 6, 2, 6],
        k=[2, 3, 5, 7, 6, 7, 5, 4, 6, 5, 6, 7],
        color=color, opacity=1.0, flatshading=True,
        name=name, legendgroup=group, showlegend=showlegend,
        hovertext=hover, hoverinfo="text")


def box_wire(x, y, z, dx, dy, dz, group):
    x, y, z, dx, dy, dz = _inset(x, y, z, dx, dy, dz)
    c = [(x, y, z), (x + dx, y, z), (x + dx, y + dy, z), (x, y + dy, z),
         (x, y, z + dz), (x + dx, y, z + dz), (x + dx, y + dy, z + dz),
         (x, y + dy, z + dz)]
    path = [0, 1, 2, 3, 0, 4, 5, 1, 5, 6, 2, 6, 7, 3, 7, 4]
    return go.Scatter3d(
        x=[c[i][0] for i in path], y=[c[i][1] for i in path],
        z=[c[i][2] for i in path], mode="lines",
        line=dict(color="rgba(15,15,15,0.8)", width=3.5),
        legendgroup=group, showlegend=False, hoverinfo="skip")


def params_text(plan):
    v = plan["verdict"]
    lines = []
    if "case_name" in plan:
        lines.append(f"<b>{plan['case_name']}</b>")
    if "pattern_requested" in v:
        lines.append(f"pattern requested: {v['pattern_requested']}"
                     + (f" (won: {v['pattern']})" if v.get("pattern") else ""))
    if "time_budget_s" in v:
        lines.append(f"time budget {v['time_budget_s']:g} s"
                     + (f", used {v['solve_seconds']:g} s"
                        if "solve_seconds" in v else ""))
    lines += [
        "",
        "<b>solver parameters</b>",
        f"support area ≥ {float(solver.SUPPORT_FRAC):.0%} of base",
        "+ two opposite edges supported",
        f"supporter needs ≥ {float(solver.SUPPORTER_MIN_FRAC):.0%} contact",
        f"flat top tolerance {solver.FLAT_TOL} mm",
        f"stackable: contact ≥ {float(solver.STACK_AREA_FRAC):.0%}"
        f" + spread ≥ {float(solver.STACK_SPREAD_FRAC):.0%} of deck",
        "",
        "<b>this plan</b>",
        f"top contact {v['top_coverage_pct']}%, spread {v['top_spread_pct']}%",
        f"footprint used {v['footprint_used_pct']}%",
        f"beam widths run {v.get('beam_widths_run', '-')}"
        + (f", {v['solve_seconds']}s" if "solve_seconds" in v else ""),
    ]
    return "<br>".join(lines)


def render(plan: dict, out_path: str):
    pallet = plan["case"]["pallet"]
    L, W, deck = pallet["length"], pallet["width"], pallet["deck_height"]
    skus = plan["case"]["skus"]
    dims = {s["sku"]: s["dims"] for s in skus}
    colors = {s["sku"]: PALETTE[i % len(PALETTE)] for i, s in enumerate(skus)}

    fig = go.Figure()
    fig.add_trace(box_mesh(0, 0, -deck, L, W, deck, "#b08968",
                           f"pallet {L}×{W}",
                           f"pallet {L}x{W}, deck {deck} mm",
                           "pallet", True))
    fig.add_trace(box_wire(0, 0, -deck, L, W, deck, "pallet"))

    rows = sorted(plan["placements"], key=lambda r: r["sequence"])
    seen_sku = set()
    for r in rows:
        l, w, h = dims[r["sku"]]
        dx, dy = (l, w) if r["rotated"] else (w, l)
        hover = (f"<b>{r['id']}</b>  x={r['x']} y={r['y']} z={r['z']}<br>"
                 f"{dx}×{dy}×{h} mm"
                 f"{' (rotated)' if r['rotated'] else ''}<br>"
                 f"sits on: {', '.join(r['sits_on'])}")
        first = r["sku"] not in seen_sku
        seen_sku.add(r["sku"])
        fig.add_trace(box_mesh(r["x"], r["y"], r["z"], dx, dy, h,
                               colors[r["sku"]],
                               f"{r['sku']}  {l}×{w}×{h}",
                               hover, r["sku"], first))
        fig.add_trace(box_wire(r["x"], r["y"], r["z"], dx, dy, h, r["sku"]))

    n = len(rows)
    steps = []
    for k in range(n + 1):
        vis = [True, True] + [i < 2 * k for i in range(2 * n)]
        label = "pallet" if k == 0 else f"{k}: {rows[k - 1]['id']}"
        steps.append(dict(method="restyle", args=[{"visible": vis}],
                          label=label))

    v = plan["verdict"]
    case_tag = f"{plan['case_name']} — " if "case_name" in plan else ""
    pat_tag = (f" — pattern {v['pattern_requested']}"
               if "pattern_requested" in v else "")
    title = (f"{case_tag}{v['placed']}/{v['total']} boxes placed — "
             f"{'STACKABLE ✓' if v['stackable'] else 'NOT stackable'} "
             f"— height {v['stack_height_mm']} mm"
             + (f" — leftovers: {', '.join(plan['leftovers'])}"
                if plan["leftovers"] else "") + pat_tag)

    fig.update_layout(
        title=dict(text=title, x=0.5),
        sliders=[dict(active=n, steps=steps, x=0.05, len=0.9,
                      currentvalue=dict(prefix="build step: "))],
        updatemenus=[dict(
            type="buttons", direction="right", showactive=True,
            x=0.5, xanchor="center", y=1.12, yanchor="top",
            buttons=[dict(label=name, method="relayout",
                          args=[{"scene.camera": cam}])
                     for name, cam in CAMERAS.items()])],
        legend=dict(x=0.99, y=0.9, xanchor="right",
                    bgcolor="rgba(255,255,255,0.7)",
                    bordercolor="#999", borderwidth=1,
                    groupclick="togglegroup"),
        annotations=[dict(text=params_text(plan), showarrow=False,
                          xref="paper", yref="paper", x=0.01, y=0.02,
                          xanchor="left", yanchor="bottom", align="left",
                          font=dict(size=11, family="monospace"),
                          bgcolor="rgba(255,255,255,0.75)",
                          bordercolor="#999", borderwidth=1)],
        scene=dict(aspectmode="data", camera=CAMERAS["isometric"],
                   xaxis_title="X (mm)", yaxis_title="Y (mm)",
                   zaxis_title="Z (mm)"),
        margin=dict(l=0, r=0, t=90, b=0))
    fig.write_html(out_path, include_plotlyjs=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("plan")
    ap.add_argument("-o", "--out", default="plan.html")
    args = ap.parse_args()
    with open(args.plan) as f:
        plan = json.load(f)
    render(plan, args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
