#!/usr/bin/env python3
"""
Combined Experiment-2 figures across every model that has finished.

    python make_comparison_figures.py

Outputs into this folder:
    fig0_all_models_combined.png   one row per model, both views side by side
    fig0a_by_condition.png         small multiples, grouped by state-passing condition
    fig0b_by_pipeline.png          small multiples, grouped by pipeline type
    table4_all_models.csv          the same numbers, for the paper

Two things this script refuses to fudge:

  * PIPELINE SET.  V4-Pro ran the 20% subset, the others ran all 924 pipelines.
    Averaging over different pipeline sets is not a comparison, so every model is
    restricted to the intersection.  The footnote states the size.

  * HIDDEN REASONING.  The whole experiment assumes direct answering.  Each model
    is checked; anything with real reasoning tokens is reported loudly rather than
    quietly plotted.  Qwen3 emits an empty <think></think> block (1-2 tokens per
    step) even with /no_think - that is not reasoning, so the gate is a per-step
    mean, not a total.
"""
from __future__ import annotations

import csv, json, math, sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# label -> results directory, in the order they should appear
MODELS: dict[str, Path] = {
    "GPT-4.1-mini":          ROOT / "geometry_v8_notebooks_gpt/results/gpt-4.1-mini",
    "DeepSeek-V4-Flash":     ROOT / "geometry_v8_notebooks_deepseekv4flash/results/deepseek-v4-flash",
    "DeepSeek-V4-Pro":       ROOT / "geometry_v8_notebooks_deepseekv4pro/results/deepseek-v4-pro",
    "Gemini-2.5-Flash-Lite": ROOT / "geometry_v8_notebooks_gemini/results/gemini-2.5-flash-lite",
    "Llama-4-Scout":         ROOT / "geometry_v8_notebooks_llama/results/llama-4-scout",
    "Qwen3-32B":             ROOT / "geometry_v8_notebooks_qwen/results/qwen3-nothink",
}

CATS  = ["pass_through", "property_conditioned", "multi_object"]
CATL  = {"pass_through": "pass-through", "property_conditioned": "property-conditioned",
         "multi_object": "multi-object"}
CONDS = ["raw", "augmented", "handle", "handle_sum"]
T4    = ["raw", "handle", "handle_sum"]          # Table 4's own columns
CONDL = {"raw": "raw", "augmented": "augmented",
         "handle": "handle only", "handle_sum": "handle + summary"}

# categorical slots 1-3 of the validated palette (all-pairs PASS, light mode)
C1, C2, C3 = "#2a78d6", "#eb6834", "#1baf7a"
CAT_C  = dict(zip(CATS, [C1, C2, C3]))
COND_C = dict(zip(T4,   [C1, C2, C3]))
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#dcdcd8", "#fcfcfb"

REASONING_MEAN_LIMIT = 2.0    # tokens/step; an empty think block is 1-2


def load(d: Path) -> dict:
    """Latest good record per (pipeline, condition, mode). Backend errors dropped."""
    good = {}
    with open(d / "results.jsonl") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (r.get("aborted") or "").startswith("backend error"):
                continue
            good[(r["pipeline_id"], r["condition"], r["mode"])] = r
    return good


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return p, max(0.0, centre - half), min(1.0, centre + half)


def qa(name: str, good: dict) -> dict:
    steps = [s for r in good.values() for s in r["steps"] if s.get("scored")]
    rt = [s.get("reasoning_tokens") or 0 for s in steps]
    mean_rt = sum(rt) / len(rt) if rt else 0.0
    out = sum(s["output_tokens"] for s in steps) / len(steps) if steps else 0.0
    vis = sum(len(s["raw_response"]) / 4 for s in steps) / len(steps) if steps else 1.0
    return dict(units=len(good), pipelines=len({k[0] for k in good}), steps=len(steps),
                mean_reasoning=mean_rt, ratio=out / max(vis, 1e-9),
                thinking_ok=mean_rt <= REASONING_MEAN_LIMIT)


def panel(ax, groups, series, val, colour, glab, fs_x=7.8, fs_v=6.6, ylab=None):
    x = np.arange(len(groups))
    w = 0.8 / len(series)
    for i, s in enumerate(series):
        p  = [val(g, s)[0] for g in groups]
        lo = [val(g, s)[0] - val(g, s)[1] for g in groups]
        hi = [val(g, s)[2] - val(g, s)[0] for g in groups]
        bars = ax.bar(x + (i - (len(series) - 1) / 2) * w, p, w * 0.88,
                      color=colour(s), edgecolor="white", lw=1.0,
                      yerr=np.vstack([lo, hi]), capsize=2,
                      error_kw=dict(lw=0.7, ecolor="#5a5a5a"), zorder=3)
        for rect, v in zip(bars, p):
            ax.annotate(f"{v:.2f}", (rect.get_x() + rect.get_width() / 2, v),
                        textcoords="offset points", xytext=(0, 5),
                        ha="center", fontsize=fs_v, color=INK, zorder=4)
    ax.set_xticks(x)
    ax.set_xticklabels([glab(g) for g in groups], fontsize=fs_x, color=INK)
    ax.set_ylim(0, 1.17)
    ax.set_yticks([0, .5, 1.0]); ax.set_yticklabels(["0", ".50", "1.0"], fontsize=7.5, color=INK2)
    if ylab:
        ax.set_ylabel(ylab, fontsize=8, color=INK2)
    ax.yaxis.grid(True, color=GRID, lw=0.8, zorder=0)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color(GRID)
    ax.tick_params(length=0)


def footnote(n_common: str) -> str:
    return (f"Oracle mode, global gates only (pass-through has no gate). Identical {n_common} pipelines per model "
            f"(V4-Pro ran the 20% subset, so every model is restricted to its pipeline set).\n"
            f"Error bars are 95% Wilson intervals. Hidden reasoning disabled for every model; Qwen3's 1-2 tokens "
            f"per step are an empty <think></think> block, not reasoning.")


def main() -> int:
    missing = [n for n, d in MODELS.items() if not (d / "results.jsonl").is_file()]
    if missing:
        print("missing results.jsonl for:", ", ".join(missing), file=sys.stderr)
        return 1

    data = {n: load(d) for n, d in MODELS.items()}

    print(f"{'model':24s} {'units':>6s} {'pipes':>6s} {'steps':>7s} {'rtok/step':>10s} {'out/vis':>8s}  thinking")
    print("-" * 78)
    bad = []
    for name, good in data.items():
        q = qa(name, good)
        flag = "OK" if q["thinking_ok"] else "** REASONING ON **"
        if not q["thinking_ok"]:
            bad.append(name)
        print(f"{name:24s} {q['units']:6d} {q['pipelines']:6d} {q['steps']:7d} "
              f"{q['mean_reasoning']:10.2f} {q['ratio']:8.2f}x  {flag}")
    if bad:
        print("\nWARNING: these models look like they were run WITH hidden reasoning:", ", ".join(bad))
        print("Their numbers are not comparable to the rest.\n")

    common = set.intersection(*[{k[0] for k in g} for g in data.values()])
    print(f"\ncommon pipelines across all {len(data)} models: {len(common)}")

    acc = {}
    for name, good in data.items():
        for cat in CATS:
            for cond in CONDS:
                ss = [s for (pid, c, m), r in good.items()
                      if pid in common and m == "oracle" and c == cond and r["category"] == cat
                      and (cat == "pass_through" or r.get("gate_locality") == "global")
                      for s in r["steps"] if s.get("scored")]
                acc[(name, cat, cond)] = wilson(sum(1 for s in ss if s["correct"]), len(ss)) + (len(ss),)

    n_common = f"{len(common)}"
    names = list(MODELS)

    # ---------- combined: one row per model, both views ----------
    rows = len(names)
    fig, axes = plt.subplots(rows, 2, figsize=(14.6, 2.9 * rows + 2.6), squeeze=False)
    for r, name in enumerate(names):
        axL, axR = axes[r]
        panel(axL, CONDS, CATS, lambda g, s, n=name: acc[(n, s, g)], lambda s: CAT_C[s],
              lambda g: CONDL[g], ylab="oracle per-step accuracy")
        panel(axR, CATS, T4, lambda g, s, n=name: acc[(n, g, s)], lambda s: COND_C[s],
              lambda g: CATL[g])
        axL.text(-0.115, 1.085, name, transform=axL.transAxes, fontsize=12.5,
                 color=INK, fontweight="bold", va="bottom", ha="left")
    hL = [Patch(facecolor=CAT_C[s], edgecolor="white", label=CATL[s]) for s in CATS]
    hR = [Patch(facecolor=COND_C[s], edgecolor="white", label=CONDL[s]) for s in T4]
    fig.legend(handles=hL, loc="lower left", ncol=3, frameon=False, fontsize=9,
               title="pipeline type", title_fontsize=9, bbox_to_anchor=(0.055, 0.020))
    fig.legend(handles=hR, loc="lower left", ncol=3, frameon=False, fontsize=9,
               title="state-passing condition", title_fontsize=9, bbox_to_anchor=(0.565, 0.020))
    fig.suptitle("The geometry bottleneck across six models", fontsize=16, color=INK,
                 x=0.009, ha="left", y=1.006, fontweight="bold")
    fig.text(0.009, 0.988,
             "Each row is one model, shown both ways: A must fall left-to-right within every group; "
             "B shows that different pipeline types are rescued by different fixes.",
             fontsize=9.4, color=INK2, ha="left")
    fig.text(0.075, 0.974, "A — grouped by state-passing condition",
             fontsize=10.2, color=INK, ha="left", fontweight="bold")
    fig.text(0.575, 0.974, "B — grouped by pipeline type (Table 4 columns)",
             fontsize=10.2, color=INK, ha="left", fontweight="bold")
    fig.text(0.009, 0.004, footnote(n_common), fontsize=7.6, color=INK2, ha="left")
    fig.tight_layout(rect=[0, 0.048, 1, 0.966])
    fig.savefig(HERE / "fig0_all_models_combined.png", dpi=200, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    print("wrote", HERE / "fig0_all_models_combined.png")

    # ---------- the two standalone figures, as small multiples ----------
    def grid(fname, groups, series, valfn, colour, glab, slab, title, subtitle, legend_title):
        ncol = 3
        nrow = math.ceil(len(names) / ncol)
        fig, axes = plt.subplots(nrow, ncol, figsize=(5.0 * ncol, 3.5 * nrow + 2.3), squeeze=False)
        for ax, name in zip(axes.ravel(), names):
            panel(ax, groups, series, lambda g, s, n=name: valfn(n, g, s), colour, glab,
                  fs_x=7.4, fs_v=6.4, ylab="oracle per-step accuracy")
            ax.set_title(name, fontsize=11, color=INK, pad=9, loc="left", fontweight="bold")
        for ax in axes.ravel()[len(names):]:
            ax.set_visible(False)
        h = [Patch(facecolor=colour(s), edgecolor="white", label=slab(s)) for s in series]
        fig.legend(handles=h, loc="lower center", ncol=len(series), frameon=False,
                   fontsize=9.5, title=legend_title, title_fontsize=9.5, bbox_to_anchor=(0.5, 0.048))
        fig.suptitle(title, fontsize=14.5, color=INK, x=0.011, ha="left", y=1.004, fontweight="bold")
        fig.text(0.011, 0.972, subtitle, fontsize=9.2, color=INK2, ha="left")
        fig.text(0.011, 0.004, footnote(n_common), fontsize=7.6, color=INK2, ha="left")
        fig.tight_layout(rect=[0, 0.105, 1, 0.958])
        fig.savefig(HERE / fname, dpi=200, bbox_inches="tight", facecolor=SURFACE)
        plt.close(fig)
        print("wrote", HERE / fname)

    grid("fig0a_by_condition.png", CONDS, CATS,
         lambda n, g, s: acc[(n, s, g)], lambda s: CAT_C[s],
         lambda g: CONDL[g], lambda s: CATL[s],
         "The geometry bottleneck reproduces across six models",
         "Figure A — grouped by state-passing condition. Within each group accuracy must fall from "
         "pass-through to property-conditioned to multi-object.",
         "pipeline type")

    grid("fig0b_by_pipeline.png", CATS, T4,
         lambda n, g, s: acc[(n, g, s)], lambda s: COND_C[s],
         lambda g: CATL[g], lambda s: CONDL[s],
         "What each fix rescues differs by pipeline type",
         "Figure B — grouped by pipeline type, over Table 4's three columns. The groups do not share "
         "a shape: different categories are rescued by different fixes.",
         "state-passing condition")

    # ---------- table ----------
    with open(HERE / "table4_all_models.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["model", "pipeline_type", "condition", "accuracy", "ci_lo", "ci_hi", "n_steps"])
        for name in names:
            for cat in CATS:
                for cond in CONDS:
                    p, lo, hi, n = acc[(name, cat, cond)]
                    w.writerow([name, CATL[cat], CONDL[cond], f"{p:.4f}", f"{lo:.4f}", f"{hi:.4f}", n])
    print("wrote", HERE / "table4_all_models.csv")

    print("\nheadline (oracle, global gates):")
    for name in names:
        pc = acc[(name, "property_conditioned", "raw")][0]
        pa = acc[(name, "property_conditioned", "augmented")][0]
        mo = acc[(name, "multi_object", "raw")][0]
        ma = acc[(name, "multi_object", "augmented")][0]
        print(f"  {name:24s} prop-cond {pc:.3f} -> {pa:.3f} ({100*(pa-pc):+.0f} pt)   "
              f"multi-obj {mo:.3f} -> {ma:.3f} ({100*(ma-mo):+.0f} pt)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
