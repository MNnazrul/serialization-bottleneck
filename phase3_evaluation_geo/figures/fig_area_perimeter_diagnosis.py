#!/usr/bin/env python3
"""
Why area and perimeter do not fall simple -> medium -> hard.  (v8 / new_data_v7)

Four panels, each one test. The claim they build to is:

    For area and perimeter the model never reads the polygon. It reads the
    bounding box. new_data_v7 holds the bounding box IDENTICAL across the
    three tiers -- deliberately, that is its balance -- so the tier axis has
    nothing to act on, and the accuracy that is left is driven by drift in the
    model's own multiplier rather than by complexity.

Panel 1  What the dataset holds flat. Median per tier, as a percent spread.
         bbox_area 0.00%, bbox_perimeter 0.12%, fill_ratio 0.49%, area 2.29%,
         perimeter 3.94% -- against n_vertices 133% and wkt_len 120%.

Panel 2  Whether the answer contains the polygon at all. Divide each answer by
         its own bounding-box size, and correlate what is left against the
         truth divided by the same box. A model running the shoelace formula
         must score near +1. Observed: -0.13 to +0.29.

Panel 3  The multiplier k = answer / box, per tier, against the true k, which
         the dataset holds flat (dashed line). Every model's k drifts; the
         true value does not move. A line crossing the dashed line produces an
         accuracy PEAK, which is what breaks monotonicity.

Panel 4  |k_model - k_true| against accuracy, one point per
         (model x property x tier). The distance explains accuracy better
         (r = -0.47) than the tier index does (r = -0.29).

Reads the raw v8 logs and new_data_v7 directly. No eval output is used, so
nothing here can inherit a choice made in eval.py.

NOTHING runs on import.  python fig_area_perimeter_diagnosis.py
"""

from __future__ import annotations

import json
import math
import statistics as st
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
# Re-pathed for the exp1_geo layout so this folder runs on its own:
#   ../01_dataset/  the 300 polygons
#   ../03_results/  <model>/<model>_results.jsonl
# Output lands here, beside this script.
# Split layout: this file lives at phase3_evaluation_geo/figures/.
REPO_ROOT = HERE.parent.parent
OUT = HERE

TIERS = ["simple", "medium", "hard"]
PROPS = ["area", "perimeter"]
# 06_v4pro_nonthinking ran on the 20% stratified subsample (60 polygons,
# PDF Section 3), so its per-tier n is 20 where the other five have 100:
# one polygon is 5 percentage points in its series and 1 point in theirs.
MODELS = [
    ("V4-Flash", "01_v4flash/v4flash_results.jsonl", "#1f77b4", "o"),
    ("Qwen3", "02_qwen3/qwen3_results.jsonl", "#ff7f0e", "s"),
    ("Llama", "03_llama_scout/llama_scout_results.jsonl", "#2ca02c", "^"),
    ("Gemini", "04_gemini/gemini_results.jsonl", "#d62728", "D"),
    ("GPT", "05_gpt/gpt_results.jsonl", "#9467bd", "v"),
    ("V4-Pro (20%)", "06_v4pro_nonthinking/v4pro_nonthinking_results.jsonl", "#8C564B", "P"),
]


def dataset() -> dict:
    path = REPO_ROOT / "phase1_dataset_geo" / "geometry_exp1_dataset.json"
    return {r["object_id"]: r for r in json.loads(path.read_text())}


def load(rel: str, prop: str) -> dict:
    """Latest record per object for one property."""
    seen: dict = {}
    with (REPO_ROOT / "phase2_model_results_geo" / "results" / rel).open() as fh:
        for line in fh:
            rec = json.loads(line)
            if rec["property"] == prop:
                seen[rec["object_id"]] = rec
    return seen


def box_size(rec: dict, prop: str) -> float:
    """The bounding-box quantity that matches the property: area or perimeter."""
    b = rec["properties"]["bbox"]
    w, h = b[2] - b[0], b[3] - b[1]
    return w * h if prop == "area" else 2 * (w + h)


def corr(xs, ys) -> float:
    mx, my = st.mean(xs), st.mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
    return num / den if den else float("nan")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ds = dataset()
    answers = {(m, p): load(rel, p) for m, rel, _, _ in MODELS for p in PROPS}

    fig, axes = plt.subplots(2, 2, figsize=(15.5, 11.5))

    # -- Panel 1: what the dataset holds flat -------------------------------
    ax = axes[0, 0]
    quantities = {}
    for r in ds.values():
        p, b = r["properties"], r["properties"]["bbox"]
        w, h = b[2] - b[0], b[3] - b[1]
        vals = {"bbox area": w * h, "bbox perimeter": 2 * (w + h),
                "fill ratio": p["area"] / (w * h), "area": p["area"],
                "perimeter": p["perimeter"], "vertex count": p["vertex_count"],
                "WKT length": len(r["wkt"])}
        for k, v in vals.items():
            quantities.setdefault(k, {}).setdefault(r["tier"], []).append(v)
    names, spreads = [], []
    for k, per_tier in quantities.items():
        med = [st.median(per_tier[t]) for t in TIERS]
        names.append(k)
        spreads.append(100 * (max(med) - min(med)) / st.mean(med))
    order = sorted(range(len(names)), key=lambda i: spreads[i])
    names = [names[i] for i in order]
    spreads = [spreads[i] for i in order]
    colours = ["#2F6F4E" if s < 5 else "#C0504D" for s in spreads]
    ax.barh(names, [max(s, 0.02) for s in spreads], color=colours, zorder=3)
    ax.set_xscale("log")
    ax.set_xlabel("spread of the tier medians, % of the mean  (log scale)")
    ax.axvline(5, color="grey", ls="--", lw=1)
    for y, s in enumerate(spreads):
        ax.text(max(s, 0.02) * 1.25, y, f"{s:.2f}%", va="center", fontsize=8.5)
    ax.set_title("1. What new_data_v7 holds flat across tiers\n"
                 "green = flat by design;  only vertex count and WKT length move",
                 fontsize=11, fontweight="bold")
    ax.grid(axis="x", alpha=0.25, zorder=0)

    # -- Panel 2: does the answer contain the polygon? ----------------------
    ax = axes[0, 1]
    width = 0.38
    for j, prop in enumerate(PROPS):
        vals = []
        for name, _, _, _ in MODELS:
            seen = answers[(name, prop)]
            xs, ys = [], []
            for oid, rec in seen.items():
                a = rec.get("parsed_answer")
                if not isinstance(a, (int, float)):
                    continue
                base = box_size(ds[oid], prop)
                if base <= 0:
                    continue
                xs.append(float(a) / base)
                ys.append(ds[oid]["properties"][prop] / base)
            vals.append(corr(xs, ys))
        pos = [i + (j - 0.5) * width for i in range(len(MODELS))]
        ax.bar(pos, vals, width, label=prop,
               color="#3A86FF" if prop == "area" else "#B8860B", zorder=3)
    ax.axhline(0, color="#333", lw=1)
    ax.axhline(1.0, color="#2F6F4E", ls="--", lw=1.2)
    ax.text(len(MODELS) - 0.5, 0.96, "what a model that COMPUTED the answer would score",
            ha="right", va="top", fontsize=8.5, color="#2F6F4E")
    ax.set_xticks(range(len(MODELS)))
    ax.set_xticklabels([m for m, _, _, _ in MODELS], fontsize=9)
    ax.set_ylim(-0.25, 1.1)
    ax.set_ylabel("corr( answer / box ,  truth / box )")
    ax.legend(fontsize=9, loc="upper left")
    ax.grid(axis="y", alpha=0.25, zorder=0)
    ax.set_title("2. Divide the bounding box out — is the polygon still in the answer?\n"
                 "No. The residual carries no information about the actual shape.",
                 fontsize=11, fontweight="bold")

    # -- Panel 3: the multiplier drifts, the truth does not -----------------
    ax = axes[1, 0]
    for prop, style in zip(PROPS, ["-", "--"]):
        for name, _, colour, marker in MODELS:
            seen = answers[(name, prop)]
            ks = []
            for t in TIERS:
                vals = [float(seen[o]["parsed_answer"]) / box_size(ds[o], prop)
                        for o in seen if seen[o]["tier"] == t
                        and isinstance(seen[o].get("parsed_answer"), (int, float))]
                ks.append(st.median(vals))
            ax.plot(range(3), ks, style, color=colour, marker=marker, markersize=5,
                    linewidth=1.6, alpha=0.9,
                    label=f"{name} {prop}" if prop == "area" else None)
        true_k = [st.median([ds[o]["properties"][prop] / box_size(ds[o], prop)
                             for o in ds if ds[o]["tier"] == t]) for t in TIERS]
        ax.plot(range(3), true_k, ":", color="black", linewidth=2.6)
        ax.text(2.04, true_k[2], f"  TRUE k, {prop}\n  (flat by design)",
                fontsize=8.5, va="center", fontweight="bold")
    ax.set_xticks(range(3))
    ax.set_xticklabels(TIERS)
    ax.set_xlim(-0.15, 2.75)
    ax.set_ylabel("k  =  value / bounding-box size")
    ax.legend(fontsize=7.5, ncol=3, loc="lower left", title="solid = area, dashed = perimeter",
              title_fontsize=7.5, framealpha=0.92)
    ax.grid(alpha=0.25)
    ax.set_title("3. The model's multiplier drifts; the true one does not\n"
                 "a line crossing the dotted truth makes accuracy PEAK, not fall",
                 fontsize=11, fontweight="bold")

    # -- Panel 4: distance to truth explains accuracy -----------------------
    ax = axes[1, 1]
    gaps, accs = [], []
    tier_idx = []
    for prop, marker in zip(PROPS, ["o", "s"]):
        for name, _, colour, _ in MODELS:
            seen = answers[(name, prop)]
            for i, t in enumerate(TIERS):
                oids = [o for o in seen if seen[o]["tier"] == t]
                kt = st.median([ds[o]["properties"][prop] / box_size(ds[o], prop) for o in oids])
                km = st.median([float(seen[o]["parsed_answer"]) / box_size(ds[o], prop)
                                for o in oids
                                if isinstance(seen[o].get("parsed_answer"), (int, float))])
                acc = 100 * sum(bool(seen[o]["correct_10pct"]) for o in oids) / len(oids)
                gaps.append(abs(km - kt))
                accs.append(acc)
                tier_idx.append(i)
                ax.scatter(abs(km - kt), acc, color=colour, marker=marker, s=58,
                           edgecolor="white", linewidth=0.6, zorder=3)
    ax.set_xlabel("| k_model  -  k_true |   (distance from the answer the data asks for)")
    ax.set_ylabel("accuracy @10% (%)")
    ax.grid(alpha=0.25, zorder=0)
    ax.set_title("4. Distance to the true multiplier beats tier as an explanation\n"
                 f"r = {corr(gaps, accs):+.2f} distance  vs  "
                 f"r = {corr(tier_idx, accs):+.2f} tier index\n"
                 "n = 30 cells;  circles = area, squares = perimeter",
                 fontsize=10.5, fontweight="bold")

    fig.suptitle(
        "Why `area` and `perimeter` do not fall simple -> medium -> hard   "
        "(new_data_v7, 5 models)\n"
        "For these two properties the model answers from the BOUNDING BOX, not from the polygon — "
        "and the bounding box is identical in all three tiers by design.\n"
        "The tier axis therefore has nothing to act on; what is left is drift in the model's own "
        "multiplier, which can rise, fall, or cross the truth.",
        fontsize=12.5, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.925))
    path = OUT / "why-area-perimeter-are-flat.png"
    fig.savefig(path, dpi=140)
    plt.close(fig)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
