#!/usr/bin/env python3
"""
Property-recovery accuracy grid for this run (dataset new_data_v7).

Same layout as fig_grid_v2.py and fig_grid_v7.py so all three can be compared
panel for panel: one panel per model, bars grouped by tier within each property,
blue band over the two properties PDF Table 4 classifies as local.

  1. FIVE MODELS, NOT SIX. 06_v4pro_nonthinking was not part of this run, so it has no
     results here. Rather than leave the sixth cell blank it carries the finding
     the five panels are evidence for: local minus global accuracy per model.
     That gap is PDF 6.3's primary bottleneck signal and section 9 calls a gap
     under 15 pp "smaller than expected".

  2. THE TIER AXIS IS CLEANER THAN IT WAS. In new_data_v6 the bbox diagonal
     varied 1.29% across tiers, and PDF 6.2 divides bbox and centroid error by
     it, so the hard tier drew a slightly wider grading band. new_data_v7 holds
     it flat to 0.01%. A tier slope in the bbox or centroid group of these bars
     is therefore a property of the model, not of the band.

This figure shows accuracy only. The answer-without-reading floors are NOT drawn
here; they are in each model's folder under ../04_evaluation/, in the P5 table of
console_output.txt, in fig3_margin_over_floor.png, and for centroid specifically
in fig9_centroid_vs_floors.png.

They matter more in this dataset than in the last one. new_data_v7 has a
shortcut leak new_data_v6 did not: answering centroid with the MEAN OF THE
SERIALIZED COORDINATES scores 0.62 / 0.84 / 0.88 by tier at 5%, against
0.35 / 0.29 / 0.37 in new_data_v6, because evenly spaced vertices average toward
the true centroid as the count grows. So in the centroid group below, a hard-tier
bar has to clear 88% before it is evidence of anything. A tall bar is not the
same as a computed answer.

new_data_v7 also balances fill_band and fixes all four generator families at 25
per tier; neither axis is shown here. See each model's
fig7_accuracy_by_fill_band.png and fig8_accuracy_by_family.png.

Reads each model's committed ../04_evaluation/<model>/accuracy_matrix.json rather than
re-deriving anything, so this figure and that model's own evaluation cannot
disagree. Run eval.py first.

NOTHING runs on import.  python fig_grid.py
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
# Paths rewritten for the exp1_geo layout: this folder is self-contained.
#   ../01_dataset/     the 300 polygons
#   ../03_results/     <model>/<model>_results.jsonl
#   ../04_evaluation/  <model>/accuracy_matrix.json  (run eval.py first)
# Output lands here, beside this script.
# Split layout: this file lives at phase3_evaluation_geo/figures/.
REPO_ROOT = HERE.parent.parent
OUT = HERE
# (folder with the eval, panel title) in model-folder order
PANELS = [
    ("01_v4flash/eval_v8",     "V4-Flash"),
    ("02_qwen3/eval_v8",       "Qwen3-32B"),
    ("03_llama_scout/eval_v8", "Llama4-Scout"),
    ("04_gemini/eval_v8",      "Gemini2.5-FL"),
    ("05_gpt/eval_v8",         "GPT-4.1-mini"),
]

PROP_ORDER = ["vertex_count", "bbox", "centroid", "area", "perimeter",
              "convex", "orientation", "aspect_ratio", "edge_length_variance"]
LOCAL = {"vertex_count", "bbox"}
TIERS = ["simple", "medium", "hard"]
TIER_COLORS = {"simple": "#4C9F70", "medium": "#E1A730", "hard": "#C0504D"}
# Used only for the 15 pp threshold line in the last panel. The
# answer-without-reading floors this colour used to draw are no longer
# on this figure - see the module docstring.
THRESHOLD_COLOR = "#C0504D"

def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    loaded = []
    for rel, title in PANELS:
        path = HERE.parent / "evaluation" / rel.split("/")[0] / "accuracy_matrix.json"
        if not path.exists():
            raise SystemExit(f"missing {path}\nRun: python ../04_evaluation/eval.py --model all")
        loaded.append((title, json.loads(path.read_text())))

    n_local = sum(1 for p in PROP_ORDER if p in LOCAL)
    x = np.arange(len(PROP_ORDER))
    w = 0.26

    fig, axes = plt.subplots(2, 3, figsize=(19, 9.5))
    flat = axes.ravel()

    for ax, (title, payload) in zip(flat, loaded):
        acc = payload["accuracy_by_tier_property"]
        for i, t in enumerate(TIERS):
            vals = [100 * (acc[p][t] or 0) for p in PROP_ORDER]
            ax.bar(x + (i - 1) * w, vals, w, label=t, color=TIER_COLORS[t], zorder=3)
        ax.axvspan(-0.5, n_local - 0.5, color="#3A86FF", alpha=0.09, zorder=1)
        ax.text(n_local / 2 - 0.5, 104, "<- local", ha="center", va="bottom",
                fontsize=8, color="#3A86FF", style="italic")
        ax.text((n_local + len(PROP_ORDER)) / 2 - 0.5, 104, "global ->",
                ha="center", va="bottom", fontsize=8, color="grey", style="italic")
        ax.axhline(50, color="gray", ls="--", lw=0.8, zorder=2)
        ax.text(len(PROP_ORDER) - 0.4, 51.5, "50%", ha="right", va="bottom",
                fontsize=7, color="gray")
        ax.set_xticks(x)
        ax.set_xticklabels([p.replace("_", "\n") for p in PROP_ORDER], fontsize=8)
        ax.set_ylim(0, 112)
        # A panel whose run did not finish is not comparable with one that did,
        # so it says so in its own title rather than in a footnote.
        n_rec = payload.get("records")
        ax.set_title(title if payload.get("complete") else
                     f"{title}\n* INCOMPLETE RUN ({n_rec}/2700 queries)",
                     fontsize=11, fontweight="bold")
        handles = [mpatches.Patch(color=TIER_COLORS[t], label=t) for t in TIERS]
        handles += [mpatches.Patch(color="#DCE9FB", label="local properties")]
        ax.legend(handles=handles, title="tier", fontsize=8, title_fontsize=8,
                  loc="upper right", framealpha=0.9, edgecolor="lightgrey")

    # ---- sixth cell: the local-global gap the five panels are evidence for --
    ax = flat[len(loaded)]
    titles = [t for t, _ in loaded]
    xs = np.arange(len(titles))
    loc = [100 * (p["local"] or 0) for _, p in loaded]
    glo = [100 * (p["global"] or 0) for _, p in loaded]
    ax.bar(xs - 0.19, loc, 0.38, label="local", color="#3A86FF", zorder=3)
    ax.bar(xs + 0.19, glo, 0.38, label="global", color="#B8C4CC", zorder=3)
    for xi, (l, g) in enumerate(zip(loc, glo)):
        ax.annotate("", xy=(xi, g), xytext=(xi, l),
                    arrowprops=dict(arrowstyle="<->", color="#333", lw=1.1))
        ax.text(xi + 0.02, (l + g) / 2, f"{l - g:+.0f} pp", fontsize=8,
                ha="left", va="center", color="#333")
    ax.axhline(15, color=THRESHOLD_COLOR, ls="--", lw=1.0, zorder=2)
    ax.set_xticks(xs)
    ax.set_xticklabels([t.replace("-", "-\n") for t in titles], fontsize=8)
    ax.set_ylim(0, 126)
    ax.set_yticks([0, 20, 40, 60, 80, 100])
    ax.set_ylabel("accuracy %", fontsize=10)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    # The 15 pp threshold goes in the legend, not on the axes: as inline text it
    # lands on top of whichever model has the tallest bars.
    ax.legend(handles=[mpatches.Patch(color="#3A86FF", label="local"),
                       mpatches.Patch(color="#B8C4CC", label="global"),
                       plt.Line2D([], [], color=THRESHOLD_COLOR, ls="--", lw=1.0,
                                  label="15 pp - PDF \u00a79 calls a smaller\n"
                                        "gap 'smaller than expected'")],
              fontsize=8, loc="upper right", framealpha=0.95,
              edgecolor="lightgrey")
    ax.set_title("Local vs global, pooled over tiers\n"
                 "(06_v4pro has no v8 run)", fontsize=11, fontweight="bold")

    for ax in axes[:, 0]:
        ax.set_ylabel("accuracy %", fontsize=10)

    fig.suptitle(
        "Property-recovery accuracy by tier and model  (v8 / new_data_v7, "
        "all models direct-answer)\n"
        "Blue shading = local properties (read off text);  no shading = global "
        "(must compute)\n"
        "Numeric properties graded @5% tolerance; boolean / categorical = exact "
        "match\n"
        "Tier is vertex count alone - coordinates are integers in [0,1000] in all "
        "three tiers, so no bar difference here is a grading-band artefact",
        fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.89])
    path = OUT / "property-recovery-accuracy-by-tier-and-model.png"
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
