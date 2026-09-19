"""
Phase 3 figures -- PDF Section 8 deliverable item 7, adapted to a single
domain (the PDF's "heatmap of local-global gap across domains and models"
collapses its domain axis to one, so it's rendered here as property x tier
accuracy per model instead -- still a local-global-relevant view, just at
finer granularity since there's no second domain to compare against).

    1. Bar chart of accuracy by property, colored by tier, one per model.
    2. Scatter of per-object correctness vs. row count, for a few global
       properties, one per model.
    3. Heatmap of accuracy by property x tier, one per model.

Called from evaluate.py via --figures, or directly:
    python figures.py --report evaluation_report.json --results-dir ../phase2_model_results
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
TIERS = ["simple", "medium", "hard"]
TIER_COLORS = {"simple": "#4C72B0", "medium": "#DD8452", "hard": "#C44E52"}
SCATTER_PROPERTIES = ["correlation", "is_monotonic", "has_outlier"]
BOOLEAN_PROPERTIES = ["func_dependency", "has_outlier", "is_monotonic"]


def bar_chart_by_property(model: str, model_report: dict, outdir: Path) -> Path:
    properties = sorted(model_report["by_property"].keys())
    fig, ax = plt.subplots(figsize=(max(6, len(properties) * 1.1), 4.5))
    width = 0.25
    x = np.arange(len(properties))

    for i, tier in enumerate(TIERS):
        heights = []
        for prop in properties:
            tier_stat = model_report["by_property"][prop]["by_tier"].get(tier)
            heights.append(tier_stat["mean"] if tier_stat else 0.0)
        ax.bar(x + (i - 1) * width, heights, width, label=tier, color=TIER_COLORS[tier])

    ax.set_xticks(x)
    ax.set_xticklabels(properties, rotation=30, ha="right")
    ax.set_ylabel("Accuracy")
    ax.set_ylim(0, 1.02)
    ax.set_title(f"{model}: accuracy by property x tier")
    ax.legend(title="tier")
    ax.axhline(0.7, color="gray", linestyle="--", linewidth=0.8, alpha=0.6)
    if "correlation" in properties:
        fig.text(0.01, 0.01,
                  "correlation bars: planted pairs only (control pairs excluded -- "
                  "their difficulty is tier-dependent by construction, see by_control)",
                  fontsize=7, color="#666666")
    fig.tight_layout()
    path = outdir / f"{model}_accuracy_by_property_tier.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def scatter_complexity(model: str, records: list[dict], outdir: Path) -> Path:
    props_present = [p for p in SCATTER_PROPERTIES if any(r["property"] == p for r in records)]
    if not props_present:
        props_present = sorted({r["property"] for r in records if r["property_locality"] == "global"})[:3]
    if not props_present:
        return None

    fig, axes = plt.subplots(1, len(props_present), figsize=(4.5 * len(props_present), 4), squeeze=False)
    for ax, prop in zip(axes[0], props_present):
        prop_records = [r for r in records if r["property"] == prop]
        rows = np.array([r.get("n_rows", 0) for r in prop_records])
        correct = np.array([1.0 if r["correct"] else 0.0 for r in prop_records])
        jitter = (np.random.default_rng(0).random(len(correct)) - 0.5) * 0.08
        ax.scatter(rows, correct + jitter, alpha=0.35, s=14)

        if len(rows) > 1:
            bins = np.linspace(rows.min(), rows.max(), 6)
            bin_idx = np.digitize(rows, bins)
            bin_means = [correct[bin_idx == b].mean() for b in range(1, len(bins)) if (bin_idx == b).any()]
            bin_centers = [(bins[b - 1] + bins[b]) / 2 for b in range(1, len(bins)) if (bin_idx == b).any()]
            ax.plot(bin_centers, bin_means, color="black", linewidth=1.5, marker="o", label="binned mean")

        ax.set_title(prop)
        ax.set_xlabel("row count")
        ax.set_ylabel("correct (jittered)")
        ax.set_ylim(-0.15, 1.15)

    fig.suptitle(f"{model}: accuracy vs. table row count")
    fig.tight_layout()
    path = outdir / f"{model}_accuracy_vs_complexity.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def heatmap_property_tier(model: str, model_report: dict, outdir: Path) -> Path:
    properties = sorted(model_report["by_property"].keys())
    matrix = np.full((len(properties), len(TIERS)), np.nan)
    for i, prop in enumerate(properties):
        for j, tier in enumerate(TIERS):
            stat = model_report["by_property"][prop]["by_tier"].get(tier)
            if stat:
                matrix[i, j] = stat["mean"]

    fig, ax = plt.subplots(figsize=(5, max(3, len(properties) * 0.5)))
    im = ax.imshow(matrix, cmap="RdYlGn", vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(TIERS)))
    ax.set_xticklabels(TIERS)
    ax.set_yticks(range(len(properties)))
    ax.set_yticklabels(properties)
    for i in range(len(properties)):
        for j in range(len(TIERS)):
            if not np.isnan(matrix[i, j]):
                ax.text(j, i, f"{matrix[i, j]:.2f}", ha="center", va="center", fontsize=8)
    fig.colorbar(im, ax=ax, label="accuracy")
    ax.set_title(f"{model}: accuracy heatmap")
    fig.tight_layout()
    path = outdir / f"{model}_heatmap.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def bool_raw_vs_balanced(model: str, model_report: dict, outdir: Path) -> Path:
    """For each yes/no property: raw accuracy vs. balanced accuracy vs. the
    tier-majority baseline (a model that reads nothing). Where raw sits at
    the baseline and balanced sits at ~0.5, the property is measuring the
    ground-truth base rate, not the model."""
    props = [p for p in BOOLEAN_PROPERTIES if p in model_report["by_property"]]
    baseline = (model_report.get("tier_majority_baseline") or {}).get("by_property_tier", {})
    fig, ax = plt.subplots(figsize=(max(6, len(props) * 1.7), 4.5))
    width = 0.27
    x = np.arange(len(props))

    raw, bal, base = [], [], []
    for p in props:
        m = model_report["by_property"][p]
        raw.append((m["accuracy"] or {}).get("mean", np.nan))
        ba = m.get("balanced_accuracy")
        bal.append(ba["balanced_accuracy"] if isinstance(ba, dict) and ba.get("balanced_accuracy") is not None else np.nan)
        cells = [baseline[f"{p}/{t}"]["baseline_accuracy"] for t in TIERS if f"{p}/{t}" in baseline]
        cells = [c for c in cells if c is not None]
        base.append(float(np.mean(cells)) if cells else np.nan)

    ax.bar(x - width, raw, width, label="raw accuracy", color="#4C72B0")
    ax.bar(x, bal, width, label="balanced accuracy", color="#55A868")
    ax.bar(x + width, base, width, label="tier-majority baseline", color="#C44E52", alpha=0.7)
    ax.axhline(0.5, color="gray", linestyle="--", linewidth=0.8, alpha=0.7)

    for i, p in enumerate(props):
        if model_report["by_property"][p].get("degenerate_guess"):
            ax.text(i, 1.03, "degenerate", ha="center", fontsize=8, color="#C44E52")

    ax.set_xticks(x)
    ax.set_xticklabels(props, rotation=20, ha="right")
    ax.set_ylabel("score")
    ax.set_ylim(0, 1.12)
    ax.set_title(f"{model}: yes/no properties -- raw accuracy vs. the base rate", fontsize=11)
    ax.legend()
    fig.tight_layout()
    path = outdir / f"{model}_bool_raw_vs_balanced.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def make_all_figures(report: dict, all_model_records: dict[str, list[dict]], outdir: Path) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for model, model_report in report["per_model"].items():
        paths.append(bar_chart_by_property(model, model_report, outdir))
        p = scatter_complexity(model, all_model_records[model], outdir)
        if p:
            paths.append(p)
        paths.append(heatmap_property_tier(model, model_report, outdir))
        paths.append(bool_raw_vs_balanced(model, model_report, outdir))
    return paths


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--report", type=Path, default=HERE / "evaluation_report.json")
    p.add_argument("--results", nargs="*", default=None, help="model=path pairs, same as evaluate.py")
    p.add_argument("--outdir", type=Path, default=HERE / "figures")
    args = p.parse_args()

    if not args.report.exists():
        raise SystemExit(f"{args.report} not found -- run evaluate.py first")
    report = json.loads(args.report.read_text())

    if not args.results:
        raise SystemExit("Pass --results model=path ... (same records used to build the report)")
    all_model_records = {}
    for spec in args.results:
        model, path = spec.split("=", 1)
        all_model_records[model] = json.loads(Path(path).read_text())

    paths = make_all_figures(report, all_model_records, args.outdir)
    print(f"Wrote {len(paths)} figures to {args.outdir}")


if __name__ == "__main__":
    main()
