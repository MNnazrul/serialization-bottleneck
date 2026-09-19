#!/usr/bin/env python3
"""
fig_grid.py's layout, redrawn at the tolerance band that fits each property.

Same figure, one change: the grading band. fig_grid.py grades every numeric
property at 5%. PDF Table 13 defines three bands and labels them --

    Strict    <= 1%   "Correct computation"
    Moderate  <= 5%   "Approximately correct"
    Lenient   <= 10%  "Coarse estimation"

-- and 6.3 metric 1 requires all three be reported. One band cannot serve nine
properties, and at 5% two failure modes hide the tier effect:

  CEILING. bbox at 5% reads 100 / 100 / 100 for V4-Flash and 96-100 for
  Gemini. The error is divided by the bbox diagonal (PDF 6.2), so a 3-unit
  miss on a 795-unit diagonal is 0.4% and passes. At 1% the same logs read
  90 / 74 / 59 for Llama and 94 / 88 / 82 for Qwen.

  FLOOR. area, perimeter and edge_length_variance sit at 0-12% at 5% for most
  models. Nothing at the floor can decline. At 10% they carry a slope.

centroid is the third case and is neither: at 5% it is won by answering the
mean of the serialized coordinates, a shortcut whose own pass rate RISES
0.62 / 0.84 / 0.88 across tiers in new_data_v7 (evenly spaced vertices average
toward the true centroid as the count grows). At 1% that shortcut stops
scoring. The band is a reporting fix only -- the leak is in the generator and
is not fixed here.

Bands used, and why, are printed under each property name in every panel.
Choosing a primary band per property is compliant with 6.3 while all three
stay in the results table, which they do: every numeric record in
results_v8/*.jsonl carries correct_1pct, correct_5pct and correct_10pct.

Reads the raw per-query logs rather than ../04_evaluation/<model>/accuracy_matrix.json, which
stores the 5% band only. That is the one thing this script cannot take from
the committed evaluation.

Local is {vertex_count, bbox} here, matching eval.py's own local/global
split and therefore fig_grid.py. new_data_v7/README.md calls orientation
local too; that disagreement is the repo's, and is left alone rather than
silently resolved inside a figure script.

NOTHING runs on import.  python fig_grid_bands.py
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
# Paths rewritten for the exp1_geo layout: this folder is self-contained.
#   ../01_dataset/     the 300 polygons
#   ../03_results/     <model>/<model>_results.jsonl
#   ../04_evaluation/  <model>/accuracy_matrix.json  (run eval.py first)
# Output lands here, beside this script.
# Split layout: this file lives at phase3_evaluation_geo/figures/.
REPO_ROOT = HERE.parent.parent
RESULTS = REPO_ROOT / "phase2_model_results_geo" / "results"
OUT = HERE

# (results folder, panel title) in model-folder order
# Six models. 06_v4pro_nonthinking ran on the 20% stratified subsample
# (60 polygons, PDF Section 3), so its per-tier n is 20 where the other five
# have 100. One polygon is 5 percentage points in its panel and 1 point in
# theirs -- its title says so, because a bar chart cannot show it.
PANELS = [
    ("01_v4flash", "V4-Flash"),
    ("02_qwen3", "Qwen3-32B"),
    ("03_llama_scout", "Llama4-Scout"),
    ("04_gemini", "Gemini2.5-FL"),
    ("05_gpt", "GPT-4.1-mini"),
    ("06_v4pro_nonthinking", "V4-Pro (20%)"),
]
SUBSAMPLE_PANELS = {"06_v4pro_nonthinking"}

PROP_ORDER = ["vertex_count", "bbox", "centroid", "area", "perimeter",
              "convex", "orientation", "aspect_ratio", "edge_length_variance"]
LOCAL = {"vertex_count", "bbox"}
TIERS = ["simple", "medium", "hard"]
TIER_COLORS = {"simple": "#4C9F70", "medium": "#E1A730", "hard": "#C0504D"}
THRESHOLD_COLOR = "#C0504D"

EXACT = "correct"
# property -> (correctness field, short band label drawn on the x axis)
BAND = {
    "vertex_count":         (EXACT,           "exact"),
    "bbox":                 ("correct_1pct",  "@1%"),
    "centroid":             ("correct_1pct",  "@1%"),
    "area":                 ("correct_10pct", "@10%"),
    "perimeter":            ("correct_10pct", "@10%"),
    "convex":               (EXACT,           "exact"),
    "orientation":          (EXACT,           "exact"),
    "aspect_ratio":         ("correct_10pct", "@10%"),
    "edge_length_variance": ("correct_10pct", "@10%"),
}


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


def by_tier(latest: dict, prop: str) -> dict[str, float]:
    field, _ = BAND[prop]
    buckets = collections.defaultdict(list)
    for (_, p), rec in latest.items():
        if p == prop:
            buckets[rec["tier"]].append(bool(rec.get(field)))
    return {t: 100.0 * sum(buckets[t]) / len(buckets[t]) for t in TIERS}


def pooled(latest: dict, props: set[str]) -> float:
    """Accuracy over every query of those properties, each at its own band."""
    hits = total = 0
    for (_, p), rec in latest.items():
        if p not in props:
            continue
        field, _ = BAND[p]
        hits += bool(rec.get(field))
        total += 1
    return 100.0 * hits / total


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    loaded = [(folder, title, load(folder)) for folder, title in PANELS]

    n_local = sum(1 for p in PROP_ORDER if p in LOCAL)
    x = np.arange(len(PROP_ORDER))
    w = 0.26

    fig, axes = plt.subplots(3, 3, figsize=(19, 14.5))
    flat = axes.ravel()

    for ax, (folder, title, latest) in zip(flat, loaded):
        acc = {p: by_tier(latest, p) for p in PROP_ORDER}
        for i, t in enumerate(TIERS):
            vals = [acc[p][t] for p in PROP_ORDER]
            ax.bar(x + (i - 1) * w, vals, w, label=t, color=TIER_COLORS[t], zorder=3)

        # A bar group that fell simple >= medium >= hard is marked, so the
        # figure answers the monotonicity question without a second table.
        for xi, p in enumerate(PROP_ORDER):
            v = [acc[p][t] for t in TIERS]
            if v[0] >= v[1] >= v[2]:
                ax.text(xi, max(v) + 3.0, "↓", ha="center", va="bottom",
                        fontsize=11, color="#2F6F4E", fontweight="bold")

        ax.axvspan(-0.5, n_local - 0.5, color="#3A86FF", alpha=0.09, zorder=1)
        ax.text(n_local / 2 - 0.5, 104, "<- local", ha="center", va="bottom",
                fontsize=8, color="#3A86FF", style="italic")
        ax.text((n_local + len(PROP_ORDER)) / 2 - 0.5, 104, "global ->",
                ha="center", va="bottom", fontsize=8, color="grey", style="italic")
        ax.axhline(50, color="gray", ls="--", lw=0.8, zorder=2)
        ax.text(len(PROP_ORDER) - 0.4, 51.5, "50%", ha="right", va="bottom",
                fontsize=7, color="gray")
        ax.set_xticks(x)
        ax.set_xticklabels([f"{p.replace('_', chr(10))}\n{BAND[p][1]}"
                            for p in PROP_ORDER], fontsize=7.5)
        ax.set_ylim(0, 112)
        n_mono = sum(1 for p in PROP_ORDER
                     if acc[p]["simple"] >= acc[p]["medium"] >= acc[p]["hard"])
        note = "   n=20/tier" if folder in SUBSAMPLE_PANELS else ""
        ax.set_title(f"{title}   ({n_mono}/9 monotone \u2193){note}",
                     fontsize=11, fontweight="bold")
        handles = [mpatches.Patch(color=TIER_COLORS[t], label=t) for t in TIERS]
        handles += [mpatches.Patch(color="#DCE9FB", label="local properties"),
                    plt.Line2D([], [], color="#2F6F4E", marker=r"$\downarrow$",
                               ls="none", label="simple ≥ medium ≥ hard")]
        ax.legend(handles=handles, title="tier", fontsize=7.5, title_fontsize=8,
                  loc="upper right", framealpha=0.9, edgecolor="lightgrey")

    # ---- sixth cell: the local-global gap, at these same bands --------------
    ax = flat[len(loaded)]
    titles = [t for _, t, _ in loaded]
    xs = np.arange(len(titles))
    glob_props = set(PROP_ORDER) - LOCAL
    loc = [pooled(latest, LOCAL) for _, _, latest in loaded]
    glo = [pooled(latest, glob_props) for _, _, latest in loaded]
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
    ax.legend(handles=[mpatches.Patch(color="#3A86FF", label="local"),
                       mpatches.Patch(color="#B8C4CC", label="global"),
                       plt.Line2D([], [], color=THRESHOLD_COLOR, ls="--", lw=1.0,
                                  label="15 pp - PDF §9 calls a smaller\n"
                                        "gap 'smaller than expected'")],
              fontsize=8, loc="upper right", framealpha=0.95,
              edgecolor="lightgrey")
    ax.set_title("Local vs global at these bands, pooled over tiers\n"
                 "(V4-Pro on the 20% subsample: n=20/tier)", fontsize=11, fontweight="bold")

    # ---- eighth cell: the vertex_count off-by-one ---------------------------
    # vertex_count is one of only two LOCAL properties, so whatever happens to
    # it moves the local-global gap directly. And what happens to it is mostly
    # not counting: WKT closes the ring, so POLYGON((a,b,c,a)) lists four
    # coordinates for three vertices, and the prompt says in as many words
    # "Do not count the repeated closing coordinate" (PDF Table 5). A model
    # that answers n+1 counted correctly and ignored the instruction.
    #
    # Splitting the bar makes that separable. V4-Pro answers n+1 on 68% of
    # polygons -- the highest of the six -- which is why its vertex_count reads
    # 15/20/20 while its counting is in fact the most accurate here.
    ax = flat[len(loaded) + 1]
    xs = np.arange(len(loaded))
    exact, offby1 = [], []
    for folder, title, latest in loaded:
        recs = [r for (o, p), r in latest.items() if p == "vertex_count"]
        n = len(recs)
        ex = sum(1 for r in recs if r.get("correct"))
        o1 = sum(1 for r in recs
                 if isinstance(r.get("parsed_answer"), (int, float))
                 and int(r["parsed_answer"]) - int(r["ground_truth"]) == 1)
        exact.append(100 * ex / n)
        offby1.append(100 * o1 / n)
    ax.bar(xs, exact, 0.6, label="exact", color="#4C9F70", zorder=3)
    ax.bar(xs, offby1, 0.6, bottom=exact, label="off by +1 (counted the\nclosing coordinate)",
           color="#E1A730", zorder=3)
    for xi, (e, o) in enumerate(zip(exact, offby1)):
        ax.text(xi, e + o + 1.5, f"{e + o:.0f}", ha="center", fontsize=8, fontweight="bold")
        ax.text(xi, e / 2, f"{e:.0f}", ha="center", va="center", fontsize=8, color="white")
    ax.set_xticks(xs)
    ax.set_xticklabels([t.replace("-", "-\n", 1) for _, t, _ in loaded], fontsize=7.5)
    ax.set_ylim(0, 112)
    ax.set_ylabel("vertex_count %", fontsize=10)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    ax.legend(fontsize=7.5, loc="upper left", framealpha=0.95, edgecolor="lightgrey")
    ax.set_title("vertex_count: counting vs following the instruction\n"
                 "green = exact;  amber = counted the repeated closing coordinate",
                 fontsize=10.5, fontweight="bold")

    # ---- ninth cell: unused --------------------------------------------------
    flat[len(loaded) + 2].axis("off")

    for ax in axes[:, 0]:
        ax.set_ylabel("accuracy %", fontsize=10)

    fig.suptitle(
        "Property-recovery accuracy by tier and model  (new_data_v7, six models, "
        "all models direct-answer)\n"
        "GRADED AT THE BAND THAT FITS EACH PROPERTY, printed under its name - "
        "not at a single 5% band for all nine\n"
        "bbox and centroid @1% (5% saturates bbox and is won by the coord-mean "
        "shortcut on centroid);  area, perimeter, aspect_ratio, "
        "edge_length_variance @10% (1% and 5% are on the floor)\n"
        "Blue shading = local properties (read off text);  no shading = global "
        "(must compute).  Tier is vertex count alone - coordinates are integers "
        "in [0,1000] in all three tiers",
        fontsize=11.5, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.885])
    path = OUT / "property-recovery-accuracy-by-tier-and-model-fitted-bands.png"
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {path}")

    print()
    print("bars behind the figure")
    head = "property (band)".ljust(28) + "".join(t[:12].rjust(16) for _, t, _ in loaded)
    print(head)
    for p in PROP_ORDER:
        line = f"{p} {BAND[p][1]}".ljust(28)
        for _, _, latest in loaded:
            a = by_tier(latest, p)
            v = [a[t] for t in TIERS]
            flag = "OK " if v[0] >= v[1] >= v[2] else "XX "
            line += (flag + "%.0f/%.0f/%.0f" % tuple(v)).rjust(16)
        print(line)
    print()
    print("local vs global at these bands")
    for (_, title, _), l, g in zip(loaded, loc, glo):
        print(f"  {title:<14} local {l:5.1f}   global {g:5.1f}   gap {l - g:+5.1f} pp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
