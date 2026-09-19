#!/usr/bin/env python3
"""
Strict-tolerance (1%) view of this run (dataset new_data_v7).

PDF Table 13 defines three numeric tolerance levels and labels them:

    Strict    <= 1%   "Correct computation"
    Moderate  <= 5%   "Approximately correct"
    Lenient   <= 10%  "Coarse estimation"

PDF 6.3 metric 1 requires accuracy at ALL THREE for numeric properties. The
committed eval_v8 output headlines the 5% band only. This figure draws the 1%
band -- the one the spec labels "correct computation" -- because the complexity
gradient is a claim about computation, not about landing in the neighbourhood.

The three non-numeric properties have no tolerance parameter in the spec:
vertex_count is an integer property (exact match), convex is boolean, and
orientation is categorical. Their panels carry the exact-match number and are
marked EXACT in the panel title, so the figure is not read as though a 1% band
were applied to them.

Computed from the raw per-query logs in ../03_results/, not from
accuracy_matrix.json, because that file stores only the 5% band.

NOTHING runs on import.  python fig_tol1pct.py
"""

from __future__ import annotations

import collections
import json
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
RESULTS = REPO_ROOT / "phase2_model_results_geo" / "results"
OUT = HERE

TIERS = ["simple", "medium", "hard"]

# (property, grading rule shown in the title)
PROPS = [
    ("vertex_count", "EXACT"),
    ("bbox", "1%"),
    ("orientation", "EXACT"),
    ("centroid", "1%"),
    ("area", "1%"),
    ("perimeter", "1%"),
    ("convex", "EXACT"),
    ("aspect_ratio", "1%"),
    ("edge_length_variance", "1%"),
]

LOCAL = {"vertex_count", "bbox", "orientation"}

# Below this peak accuracy a panel carries no readable gradient: every model
# is on the floor and the tier differences are single answers. Those panels
# get their own zoomed axis and a pink ground so the figure does not imply
# the 1% band is a usable band for them.
FLOOR_ZOOM_BELOW = 25.0

# (results folder, display name, colour, marker)
# 06_v4pro_nonthinking ran on the 20% stratified subsample (60 polygons,
# PDF Section 3), so its per-tier n is 20 where the other five have 100:
# one polygon is 5 percentage points in its series and 1 point in theirs.
MODELS = [
    ("01_v4flash", "DeepSeek-V4-Flash", "#1f77b4", "o"),
    ("02_qwen3", "Qwen3-32B", "#ff7f0e", "s"),
    ("03_llama_scout", "Llama-4-Scout", "#2ca02c", "^"),
    ("04_gemini", "Gemini-2.5-Flash-Lite", "#d62728", "D"),
    ("05_gpt", "GPT-4.1-mini", "#9467bd", "v"),
    ("06_v4pro_nonthinking", "V4-Pro (20%)", "#8C564B", "P"),
]


def load(folder: str) -> dict:
    """Latest record per (object_id, property) from that model's results log."""
    hits = list((RESULTS / folder).glob("*_results.jsonl"))
    if len(hits) != 1:
        raise SystemExit(f"expected one results jsonl in {folder}, found {len(hits)}")
    latest: dict = {}
    with hits[0].open() as fh:
        for line in fh:
            rec = json.loads(line)
            latest[(rec["object_id"], rec["property"])] = rec
    return latest


def accuracy(latest: dict, prop: str, rule: str) -> list[float]:
    """Percent correct per tier under the stated grading rule."""
    buckets = collections.defaultdict(list)
    field = "correct" if rule == "EXACT" else "correct_1pct"
    for (_, p), rec in latest.items():
        if p != prop:
            continue
        buckets[rec["tier"]].append(bool(rec.get(field)))
    return [100.0 * sum(buckets[t]) / len(buckets[t]) for t in TIERS]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    data = {folder: load(folder) for folder, *_ in MODELS}

    fig, axes = plt.subplots(3, 3, figsize=(15, 12), sharex=True)
    x = range(3)

    for ax, (prop, rule) in zip(axes.flat, PROPS):
        mono_count = 0
        for folder, name, colour, marker in MODELS:
            y = accuracy(data[folder], prop, rule)
            if y[0] >= y[1] >= y[2]:
                mono_count += 1
            ax.plot(x, y, marker=marker, color=colour, label=name,
                    linewidth=1.9, markersize=6)

        tint = "#eaf2fb" if prop in LOCAL else "#ffffff"
        ax.set_facecolor(tint)
        locality = "local" if prop in LOCAL else "global"

        # A property pinned to the floor is unreadable on a 0-100 axis, and
        # "unreadable" is itself the finding. Zoom those panels and say so in
        # the title, so a flat line near zero is not mistaken for missing data.
        peak = max(max(accuracy(data[f], prop, rule)) for f, *_ in MODELS)
        floored = peak < FLOOR_ZOOM_BELOW
        if floored:
            ax.set_ylim(-0.4, max(peak * 1.35, 5))
            ax.set_facecolor("#fbeeee")
        else:
            ax.set_ylim(-3, 103)

        note = "  <- AT FLOOR, axis zoomed" if floored else ""
        ax.set_title(
            f"{prop}  [{rule}, {locality}]{note}\nmonotone in {mono_count}/5 models",
            fontsize=11)
        ax.set_xticks(list(x))
        ax.set_xticklabels(TIERS)
        ax.grid(alpha=0.3, linestyle=":")
        ax.set_ylabel("accuracy (%)")

    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=5, frameon=False,
               bbox_to_anchor=(0.5, 0.005))
    fig.suptitle(
        "Property recovery at the STRICT tolerance — new_data_v7\n"
        "PDF Table 13: <=1% relative error = \"correct computation\". "
        "Integer, boolean and categorical properties are graded by exact match "
        "(no tolerance applies).\n"
        "Blue panels are the three local properties of PDF Table 4. "
        "PINK panels are on the floor at 1% and are drawn on a zoomed axis.",
        fontsize=13)
    fig.tight_layout(rect=(0, 0.045, 1, 0.925))

    path = OUT / "accuracy_by_tier_strict_1pct.png"
    fig.savefig(path, dpi=150)
    print(f"wrote {path}")

    # The numbers behind the panels, so the figure can be checked by hand.
    print()
    header = "property".ljust(24) + "".join(n[:13].rjust(17) for _, n, _, _ in MODELS)
    print(header)
    for prop, rule in PROPS:
        line = f"{prop} [{rule}]".ljust(24)
        for folder, *_ in MODELS:
            y = accuracy(data[folder], prop, rule)
            flag = "OK " if y[0] >= y[1] >= y[2] else "XX "
            line += (flag + "%.0f/%.0f/%.0f" % tuple(y)).rjust(17)
        print(line)


if __name__ == "__main__":
    main()
