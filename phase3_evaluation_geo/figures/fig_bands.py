#!/usr/bin/env python3
"""
Two figures on the tolerance question for this run (dataset new_data_v7).

PDF Table 13 defines three numeric tolerance levels and labels each:

    Strict    <= 1%   "Correct computation"
    Moderate  <= 5%   "Approximately correct"
    Lenient   <= 10%  "Coarse estimation"

PDF 6.3 metric 1 requires all three be reported for numeric properties. The
committed eval_v8 output headlines the 5% band alone, and a single band cannot
serve nine properties: at 5% bbox saturates at 100 and centroid is won by a
shortcut, while area, perimeter and edge_length_variance sit on the floor.

  fig_A_bands_all_three.png
      Every property at all three bands, so the band choice below is visible
      rather than asserted. Lines are accuracy POOLED over the five models
      (n = 500 per tier per band); the shaded ribbon is the min-to-max spread
      across the individual models, so a pooled line drawn through wildly
      disagreeing models cannot look like consensus.

  fig_B_primary_band.png
      One band per property -- the band where that property is neither on the
      floor nor on the ceiling -- with the five models drawn separately. This
      is the reporting choice fig_A justifies. Selecting a primary band is
      compliant with 6.3 only while all three remain in the results table,
      which they do: the raw logs carry correct_1pct / _5pct / _10pct on every
      numeric record.

The three non-numeric properties take no tolerance parameter. vertex_count is
integer, convex is boolean, orientation is categorical; all three are exact
match under Table 13 and are drawn as EXACT in both figures.

Reads the raw per-query logs in ../03_results/ rather than accuracy_matrix.json,
which stores the 5% band only.

NOTHING runs on import.  python fig_bands.py
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
LOCAL = {"vertex_count", "bbox", "orientation"}

# Properties in PDF Table 4 order, with the grading field each one takes and
# the primary band fig_B reports it at.
EXACT = "correct"
PROPS = [
    # (property, is_numeric, primary band key, why this band)
    ("vertex_count",         False, EXACT,            "spec: integer property"),
    ("bbox",                 True,  "correct_1pct",   "5% saturates at 100"),
    ("orientation",          False, EXACT,            "spec: categorical"),
    ("centroid",             True,  "correct_1pct",   "5% is won by the coord-mean shortcut"),
    ("area",                 True,  "correct_10pct",  "1% and 5% are both on the floor"),
    ("perimeter",            True,  "correct_10pct",  "1% and 5% are both on the floor"),
    ("convex",               False, EXACT,            "spec: boolean"),
    ("aspect_ratio",         True,  "correct_10pct",  "1% and 5% are both on the floor"),
    ("edge_length_variance", True,  "correct_10pct",  "1% and 5% are both on the floor"),
]

BANDS = [
    ("correct_1pct",  "1%  strict (correct computation)",   "#1f77b4", "o", "-"),
    ("correct_5pct",  "5%  moderate (approximately correct)", "#ff7f0e", "s", "--"),
    ("correct_10pct", "10% lenient (coarse estimation)",    "#2ca02c", "^", ":"),
]

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

BAND_LABEL = {EXACT: "EXACT", "correct_1pct": "1%",
              "correct_5pct": "5%", "correct_10pct": "10%"}


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


def per_tier(latest: dict, prop: str, field: str) -> list[float]:
    buckets = collections.defaultdict(list)
    for (_, p), rec in latest.items():
        if p == prop:
            buckets[rec["tier"]].append(bool(rec.get(field)))
    return [100.0 * sum(buckets[t]) / len(buckets[t]) for t in TIERS]


def pooled(data: dict, prop: str, field: str) -> list[float]:
    """Accuracy over all five models' queries at once: n = 500 per tier."""
    buckets = collections.defaultdict(list)
    for latest in data.values():
        for (_, p), rec in latest.items():
            if p == prop:
                buckets[rec["tier"]].append(bool(rec.get(field)))
    return [100.0 * sum(buckets[t]) / len(buckets[t]) for t in TIERS]


def mono(v: list[float]) -> bool:
    return v[0] >= v[1] >= v[2]


def fig_a(data: dict) -> None:
    """Every property at all three bands."""
    fig, axes = plt.subplots(3, 3, figsize=(16, 12.5), sharex=True)
    x = range(3)

    for ax, (prop, numeric, _, _) in zip(axes.flat, PROPS):
        if numeric:
            for field, label, colour, marker, style in BANDS:
                y = pooled(data, prop, field)
                spread = [per_tier(data[f], prop, field) for f, *_ in MODELS]
                lo = [min(s[i] for s in spread) for i in range(3)]
                hi = [max(s[i] for s in spread) for i in range(3)]
                ax.fill_between(list(x), lo, hi, color=colour, alpha=0.12)
                n_mono = sum(mono(s) for s in spread)
                ax.plot(x, y, marker=marker, color=colour, linestyle=style,
                        linewidth=2.0, markersize=6,
                        label=f"{label}   [{n_mono}/5 models monotone]")
        else:
            y = pooled(data, prop, EXACT)
            spread = [per_tier(data[f], prop, EXACT) for f, *_ in MODELS]
            lo = [min(s[i] for s in spread) for i in range(3)]
            hi = [max(s[i] for s in spread) for i in range(3)]
            ax.fill_between(list(x), lo, hi, color="#555555", alpha=0.12)
            n_mono = sum(mono(s) for s in spread)
            ax.plot(x, y, marker="o", color="#333333", linewidth=2.0,
                    markersize=6,
                    label=f"exact match   [{n_mono}/5 models monotone]")

        ax.set_facecolor("#eaf2fb" if prop in LOCAL else "#ffffff")
        kind = "local" if prop in LOCAL else "global"
        ax.set_title(f"{prop}   ({kind})", fontsize=12)
        ax.set_xticks(list(x))
        ax.set_xticklabels(TIERS)
        ax.set_ylim(-3, 103)
        ax.grid(alpha=0.3, linestyle=":")
        ax.set_ylabel("accuracy (%)")
        # bbox and vertex_count sit near the top of the axis; a fixed
        # upper-right legend lands on the lines there.
        top_heavy = max(pooled(data, prop, BANDS[-1][0] if numeric else EXACT)) > 60
        ax.legend(fontsize=7.5, framealpha=0.9,
                  loc="lower left" if top_heavy else "upper right")

    fig.suptitle(
        "Choosing the tolerance band — new_data_v7, all three levels of PDF Table 13\n"
        "Line = accuracy pooled over the 5 models (n=500 per tier).  Ribbon = min-to-max across the individual models.\n"
        "A band is unusable where the line is pinned at 100 (ceiling) or at 0 (floor): there is nothing left to decline.\n"
        "Blue panels are the three local properties of PDF Table 4; those three take exact match, not a tolerance.",
        fontsize=12.5)
    fig.tight_layout(rect=(0, 0, 1, 0.915))
    path = OUT / "fig_A_bands_all_three.png"
    fig.savefig(path, dpi=150)
    print(f"wrote {path}")


def fig_b(data: dict) -> None:
    """One band per property, five models drawn separately."""
    fig, axes = plt.subplots(3, 3, figsize=(15.5, 12.5), sharex=True)
    x = range(3)

    for ax, (prop, _, field, why) in zip(axes.flat, PROPS):
        n_mono = 0
        peak = 0.0
        for folder, name, colour, marker in MODELS:
            y = per_tier(data[folder], prop, field)
            n_mono += mono(y)
            peak = max(peak, max(y))
            ax.plot(x, y, marker=marker, color=colour, label=name,
                    linewidth=1.9, markersize=6)

        ax.set_facecolor("#eaf2fb" if prop in LOCAL else "#ffffff")
        ax.set_ylim(-3, 103) if peak >= 25 else ax.set_ylim(-0.6, peak * 1.35)
        kind = "local" if prop in LOCAL else "global"
        ax.set_title(f"{prop}   [{BAND_LABEL[field]}, {kind}]   {n_mono}/5 monotone\n{why}",
                     fontsize=10.5)
        ax.set_xticks(list(x))
        ax.set_xticklabels(TIERS)
        ax.grid(alpha=0.3, linestyle=":")
        ax.set_ylabel("accuracy (%)")

    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=5, frameon=False,
               bbox_to_anchor=(0.5, 0.004))
    fig.suptitle(
        "Property recovery at the band that fits each property — new_data_v7\n"
        "All three bands stay in the results table per PDF 6.3; this figure reports the one that is neither floored nor saturated.\n"
        "Panels whose peak is under 25% are drawn on a zoomed axis — a flat line near zero there is a floor, not missing data.\n"
        "Blue panels are the three local properties of PDF Table 4.",
        fontsize=12.5)
    fig.tight_layout(rect=(0, 0.042, 1, 0.912))
    path = OUT / "fig_B_primary_band.png"
    fig.savefig(path, dpi=150)
    print(f"wrote {path}")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    data = {folder: load(folder) for folder, *_ in MODELS}
    fig_a(data)
    fig_b(data)

    print()
    print("fig_B numbers — per-model accuracy at each property's primary band")
    head = "property (band)".ljust(30) + "".join(n[:13].rjust(17) for _, n, _, _ in MODELS)
    print(head)
    for prop, _, field, _ in PROPS:
        line = f"{prop} [{BAND_LABEL[field]}]".ljust(30)
        for folder, *_ in MODELS:
            y = per_tier(data[folder], prop, field)
            line += (("OK " if mono(y) else "XX ") + "%.0f/%.0f/%.0f" % tuple(y)).rjust(17)
        print(line)


if __name__ == "__main__":
    main()
