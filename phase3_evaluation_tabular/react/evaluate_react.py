"""
Phase 3 - Experiment 2 (ReAct) evaluation, Domain 3 (Tabular data).

Reads the Experiment-2 Phase-2 logs in place and produces the same kind of
deliverables as `../` (phase3_tabular_evaluation) - an accuracy readout, the
local-vs-global contrast, figures, a failure-mode pass, and a saved summary -
but the comparison here is **ReAct vs a matched single-call zero-shot baseline**
rather than zero-shot vs ground-truth difficulty.

This is the tabular counterpart of `../phase3_evaluation_react_graph/
evaluate_react.py`. Everything structural (bootstrap CIs, 10,000 resamples,
seed 0, the local/global gap definition, ReAct-never-finishes == incorrect,
no CoT-SC back-off) is unchanged - PDF Section 6 applies uniformly across
domains. What differs from the graph domain:

    graph                         tabular
    2 local / 6 global            3 local (row_count, column_dtype,
                                  null_count) / 5 global
    1 numeric-tolerance prop      2 (correlation, skewness)
    complexity axis = node count  row count (8-50)
    category field = family (5)   source (synthetic/real; currently
                                  300/300 synthetic - degenerate)
    -                             correlation queries: is_control False
                                  (planted, |r|>=0.30), True (control,
                                  |r|<0.20, rejection-sampled), or absent
                                  (best available pair, still <0.30)

Dataset note: as of the 2026-09-09 rebalance the three yes/no properties
(is_monotonic, has_outlier, func_dependency) sit at ~50% true per tier and
has_outlier uses a Tukey fence (Q1-3*IQR / Q3+3*IQR), not mean+-3*std.
Every table now carries exactly 10 queries (3,000 total; V4-Pro subsample
600). Balanced accuracy is still reported per boolean property as a
guardrail -- a ReAct arm that never finishes scores those as one class.

Inputs (read straight from the repo, nothing copied):
  ReAct arm      : ../../phase2_tabular_model_results/react/<NN_model>/<key>_results.json
  zero-shot arm  : ../../phase2_tabular_model_results/react/<NN_model>/<key>_zeroshot_results.json
                   (matched control, same session; Experiment 1's method exactly)
  fallback       : ../../phase2_tabular_model_results/<NN_model>/<key>_results.json
                   (Experiment 1's committed zero-shot, used only where the
                   matched control was not run - models whose API credits ran
                   out mid-experiment)

Outputs (created here):
  results/evaluation_summary.txt
  results/comparison_by_property.csv
  results/react_failure_analysis.json
  figures/fig1_react_vs_zeroshot_by_property.png
  figures/fig2_locality_delta.png
  figures/fig3_react_finish_rate.png
  figures/fig4_steps_breakdown.png
  figures/fig5_numeric_error_kde.png

  source .venv/bin/activate
  python phase3_tabular_evaluation/react/evaluate_react.py

Grading of a ReAct episode and its matched zero-shot record: see
REACT_LOOP_GUIDELINE.txt in this folder.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
REACT_DIR = ROOT / "phase2_tabular_model_results" / "react"
EXP1_DIR = ROOT / "phase2_tabular_model_results"  # fallback zero-shot
RESULTS = HERE / "results"
FIGS = HERE / "figures"
RESULTS.mkdir(exist_ok=True)
FIGS.mkdir(exist_ok=True)

BOOTSTRAP_N = 10_000
RNG_SEED = 0

# (folder, key, label, full episode count for coverage reporting)
# Full coverage = 300 tables x 10 queries = 3000; V4-Pro's 20% subsample
# is 60 tables x 10 = 600 (post-2026-09-09-rebalance counts).
MODELS = [
    ("01_v4flash", "v4flash", "V4-Flash", 3000),
    ("02_qwen3", "qwen3", "Qwen3-32B", 3000),
    ("03_llama_scout", "llama_scout", "Llama4-Scout", 3000),
    ("04_gemini", "gemini", "Gemini2.5-FL", 3000),
    ("05_gpt", "gpt", "GPT-4.1-mini", 3000),
    ("06_v4pro_thinking", "v4pro_thinking", "V4-Pro*", 600),
]

PROP_ORDER = [
    "row_count", "column_dtype", "null_count",              # local
    "correlation", "is_monotonic", "has_outlier",
    "skewness", "func_dependency",                          # global
]
LOCAL = {"row_count", "column_dtype", "null_count"}
NUMERIC_TOL = {"correlation", "skewness"}
N_LOCAL = 3  # dotted local|global divider sits at x = N_LOCAL - 0.5

SHORT = {
    "row_count": "rows", "column_dtype": "dtype", "null_count": "nulls",
    "correlation": "corr", "is_monotonic": "monotonic", "has_outlier": "outlier",
    "skewness": "skew", "func_dependency": "func_dep",
}
SHORT_LIST = [SHORT[p] for p in PROP_ORDER]


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load(path: Path) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text())
    jl = path.with_suffix(".jsonl")
    if jl.exists():
        return [json.loads(ln) for ln in jl.read_text().splitlines() if ln.strip()]
    return []


def is_local(rec: dict) -> bool:
    loc = rec.get("property_locality")
    if loc in ("local", "global"):
        return loc == "local"
    return rec["property"] in LOCAL


def is_correct(rec: dict) -> bool:
    """Same grading as Experiment 1's tabular Phase 3: exact match for the
    integer / boolean / categorical properties; 5% relative error (the
    "moderate" headline tolerance) for the two numeric-tolerance properties."""
    if rec["property"] in NUMERIC_TOL:
        return bool(rec.get("correct_5pct", rec.get("correct")))
    return bool(rec.get("correct"))


# Properties whose model-facing question changed in the 2026-09-09 dataset
# revision -- an Experiment 1 committed record for these is NOT a valid
# matched control (it answered a different question), so drop it from any
# fallback zero-shot arm.
QUESTION_CHANGED = {"has_outlier"}


def arm(folder: str, key: str):
    """Return (react_records, zeroshot_records, zeroshot_source)."""
    react = load(REACT_DIR / folder / f"{key}_results.json")
    zs = load(REACT_DIR / folder / f"{key}_zeroshot_results.json")
    src = "matched (same session)"
    if not zs:
        zs = [r for r in load(EXP1_DIR / folder / f"{key}_results.json")
              if r["property"] not in QUESTION_CHANGED]
        src = "Experiment 1 committed (has_outlier dropped: question changed)"
        if zs:
            print(f"  [warn] {key}: no matched zero-shot; falling back to Exp 1 committed "
                  f"results with {sorted(QUESTION_CHANGED)} excluded -- re-run for a clean pair")
    return react, zs, src


# ---------------------------------------------------------------------------
# Stats  (identical machinery to ../phase3_evaluation_react_graph/)
# ---------------------------------------------------------------------------

def _control_match(rec: dict, control: bool | None) -> bool:
    """control=None -> any; True -> is_control is True; False -> is_control
    is False (planted only, NOT the is_control-absent 'neither' pairs)."""
    if control is None:
        return True
    ic = rec.get("is_control")
    return ic is not None and bool(ic) == control


def acc(records: list[dict], prop: str | None = None, locality: str | None = None,
        tier: str | None = None, control: bool | None = None) -> tuple[float, int]:
    bits = []
    for r in records:
        if prop is not None and r["property"] != prop:
            continue
        if locality is not None and (locality == "local") != is_local(r):
            continue
        if tier is not None and r.get("tier") != tier:
            continue
        if not _control_match(r, control):
            continue
        bits.append(is_correct(r))
    if not bits:
        return float("nan"), 0
    return sum(bits) / len(bits), len(bits)


CORR_DISCRIM_CUT = 0.30  # |answer| at/above this == "the model called it correlated"


def called_corr_rate(records: list[dict], control: bool) -> float:
    """Fraction of correlation queries in the given is_control group where
    the model's answer had |value| >= CORR_DISCRIM_CUT. Want HIGH for
    planted (control=False), LOW for control (control=True). Independent of
    whether the exact coefficient was right."""
    vals = [
        abs(r["parsed_answer"]) for r in records
        if r["property"] == "correlation" and _control_match(r, control)
        and r.get("parse_success") and isinstance(r.get("parsed_answer"), (int, float))
        and not isinstance(r.get("parsed_answer"), bool)
    ]
    return float(np.mean([v >= CORR_DISCRIM_CUT for v in vals])) if vals else float("nan")


def balanced_acc(records: list[dict], prop: str) -> tuple[float, int, int]:
    """Mean of the two class recalls for a boolean property -- 0.50 for any
    single-answer strategy (incl. a ReAct arm that never finishes). NaN when
    a class is absent."""
    pos = [r for r in records if r["property"] == prop and r["ground_truth"] is True]
    neg = [r for r in records if r["property"] == prop and r["ground_truth"] is False]
    if not pos or not neg:
        return float("nan"), len(pos), len(neg)
    rec_pos = np.mean([is_correct(r) for r in pos])
    rec_neg = np.mean([is_correct(r) for r in neg])
    return float(0.5 * (rec_pos + rec_neg)), len(pos), len(neg)


def finish_rate(records: list[dict], prop: str | None = None,
                control: bool | None = None) -> float:
    vals = [
        bool(r.get("react_finished"))
        for r in records
        if (prop is None or r["property"] == prop) and _control_match(r, control)
    ]
    return float(np.mean(vals)) if vals else float("nan")


def delta_ci(react: list[dict], zs: list[dict], locality: str | None,
             n: int = BOOTSTRAP_N, seed: int = RNG_SEED) -> tuple[float, float, float]:
    """Bootstrap CI for (ReAct acc - zero-shot acc), resampling each arm
    independently (same recipe as the graph domain's gap_ci)."""
    def bits(recs):
        return np.array([
            is_correct(r) for r in recs
            if locality is None or (locality == "local") == is_local(r)
        ], dtype=float)

    a, b = bits(react), bits(zs)
    if len(a) == 0 or len(b) == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    da = rng.choice(a, (n, len(a)), replace=True).mean(axis=1)
    db = rng.choice(b, (n, len(b)), replace=True).mean(axis=1)
    diff = da - db
    return (float(a.mean() - b.mean()),
            float(np.percentile(diff, 2.5)),
            float(np.percentile(diff, 97.5)))


# ---------------------------------------------------------------------------
# Build the per-model comparison
# ---------------------------------------------------------------------------

rows_present = []
for folder, key, label, full_n in MODELS:
    react, zs, src = arm(folder, key)
    if not react:
        continue
    rows_present.append(dict(folder=folder, key=key, label=label, full_n=full_n,
                             react=react, zs=zs, src=src,
                             partial=len(react) < full_n))

print(f"{len(rows_present)} model(s) with ReAct results:",
      ", ".join(r["label"] for r in rows_present) or "(none)")


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

with (RESULTS / "comparison_by_property.csv").open("w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["model", "zeroshot_source", "property", "locality", "subset",
                "n_react", "react_acc", "react_finish_rate",
                "n_zeroshot", "zeroshot_acc", "delta_pp"])
    for r in rows_present:
        for p in PROP_ORDER:
            subsets = [("all", None)]
            if p == "correlation":
                subsets += [("planted", False), ("control", True)]
            for name, ctl in subsets:
                ra, rn = acc(r["react"], p, control=ctl)
                za, zn = acc(r["zs"], p, control=ctl)
                fin = finish_rate(r["react"], p, control=ctl)
                w.writerow([r["label"], r["src"], p,
                            "local" if p in LOCAL else "global", name,
                            rn, f"{ra:.4f}", f"{fin:.4f}",
                            zn, f"{za:.4f}",
                            f"{100 * (ra - za):+.1f}" if rn and zn else ""])


# ---------------------------------------------------------------------------
# Summary text
# ---------------------------------------------------------------------------

lines: list[str] = []


def out(s: str = "") -> None:
    lines.append(s)


out("EXPERIMENT 2 (ReAct) - TABULAR DOMAIN - EVALUATION SUMMARY")
out("=" * 60)
out("Task: recover 8 properties from text-serialized (CSV) tables with a")
out("      ReAct Thought/Action/Observation loop over structural tools")
out("      (RowCount / ColumnNames / ColumnDtype / GetColumn / GetCell / Finish),")
out("      vs a matched single-call zero-shot baseline (Experiment 1's method).")
out("Grading: identical to Experiment 1 tabular Phase 3 (exact match for")
out("         integer/boolean/categorical; correlation & skewness at 5% relative")
out("         error). ReAct episodes that never reach Finish are scored incorrect")
out("         (no CoT-SC back-off). See REACT_LOOP_GUIDELINE.txt.")
out("")
for r in rows_present:
    cov = f"{len(r['react'])}/{r['full_n']} episodes" + ("  [PARTIAL - credits]" if r["partial"] else "")
    out(f"  {r['label']:13s} {cov:30s} zero-shot baseline: {r['src']}")
out("")

out("HEADLINE: ReAct helps LOCAL properties, hurts GLOBAL ones.")
for r in rows_present:
    dl, dll, dlh = delta_ci(r["react"], r["zs"], "local")
    dg, dgl, dgh = delta_ci(r["react"], r["zs"], "global")
    out(f"  {r['label']:13s} local  {100*dl:+5.1f}pp [{100*dll:+.1f},{100*dlh:+.1f}]"
        f"    global {100*dg:+5.1f}pp [{100*dgl:+.1f},{100*dgh:+.1f}]")
out("")

out("GLOBAL-PROPERTY ReAct EFFECT BY TIER (pp; complexity axis = row count)")
for r in rows_present:
    cells = []
    for t in ("simple", "medium", "hard"):
        ga, gn = acc(r["react"], locality="global", tier=t)
        za, zn = acc(r["zs"], locality="global", tier=t)
        cells.append(f"{t:6s} {100*(ga-za):+6.1f}" if gn and zn else f"{t:6s}   n/a")
    out(f"  {r['label']:13s} " + "   ".join(cells))
out("")

BOOLEAN_PROPS = {"is_monotonic", "has_outlier", "func_dependency"}

out("PER-PROPERTY (ReAct acc  ->  zero-shot acc   [ReAct finish rate])")
out("  (bal = balanced accuracy, mean of the two class recalls; 0.50 = one-answer / never-finish)")
for r in rows_present:
    out(f"  {r['label']}  ({r['src']})")
    for p in PROP_ORDER:
        ra, rn = acc(r["react"], p)
        za, zn = acc(r["zs"], p)
        fin = finish_rate(r["react"], p)
        loc = "L" if p in LOCAL else "G"
        extra = ""
        if p in BOOLEAN_PROPS:
            rba, _, _ = balanced_acc(r["react"], p)
            zba, _, _ = balanced_acc(r["zs"], p)
            extra = f"   bal {100*rba:5.1f}% -> {100*zba:5.1f}%"
        out(f"    [{loc}] {p:16s} {100*ra:5.1f}% -> {100*za:5.1f}%   "
            f"fin {100*fin:3.0f}%   d {100*(ra-za):+5.1f}pp{extra}")
    # correlation planted vs control detail
    rp, npl = acc(r["react"], "correlation", control=False)
    rc, ncl = acc(r["react"], "correlation", control=True)
    zp, _ = acc(r["zs"], "correlation", control=False)
    zc, _ = acc(r["zs"], "correlation", control=True)
    out(f"        correlation  exact-value acc:  planted {100*rp:5.1f}% -> {100*zp:5.1f}%   "
        f"control {100*rc:5.1f}% -> {100*zc:5.1f}%")
    rdg = called_corr_rate(r["react"], False) - called_corr_rate(r["react"], True)
    zdg = called_corr_rate(r["zs"], False) - called_corr_rate(r["zs"], True)
    out(f"        correlation  discrimination (|answer|>=0.3 rate, planted-minus-control):  "
        f"ReAct {100*rdg:+.0f}pp   zero-shot {100*zdg:+.0f}pp")
    out("")

out("MECHANISM")
out("  - Local properties (row_count, column_dtype, null_count) are one tool")
out("    call away: ReAct issues RowCount[] / ColumnDtype[col] and finishes in")
out("    ~2 steps, removing the zero-shot miscount on wide/tall tables.")
out("  - Global properties need an aggregate over a whole column or the whole")
out("    table. The model spends its step budget on one-cell-at-a-time GetCell")
out("    probes (or a few GetColumn calls it then cannot aggregate in-context)")
out("    and often never reaches Finish; the single-call baseline at least")
out("    guesses from the full CSV in context. correlation and skewness are the")
out("    worst hit - both require reading every value in a column.")
out("  - Loop overhead can also hurt a format-fragile model on an otherwise")
out("    trivial local read (watch column_dtype).")
out("")

out("REACT LOOP BEHAVIOUR (per model)")
for r in rows_present:
    steps = np.array([x.get("react_steps", 0) for x in r["react"]], dtype=float)
    fin = np.array([bool(x.get("react_finished")) for x in r["react"]])
    correct = np.array([is_correct(x) for x in r["react"]])
    ms_correct = steps[correct].mean() if correct.any() else float("nan")
    out(f"  {r['label']:13s} finish {100*fin.mean():4.0f}%   mean steps {steps.mean():4.1f}"
        f"   mean steps|correct {ms_correct:4.1f}"
        f"   no_finish {100*(~fin).mean():4.0f}%")
out("")

# failure taxonomy for the debug model
FAIL = next((r for r in rows_present if r["key"] == "v4flash"),
            rows_present[0] if rows_present else None)
if FAIL:
    fa = []
    for rec in FAIL["react"]:
        if is_correct(rec):
            continue
        prop = rec["property"]
        local = prop in LOCAL
        if not rec.get("react_finished"):
            label = "no_finish_local" if local else "no_finish_global"
        elif not rec.get("parse_success"):
            label = "unparseable_finish"
        elif (prop == "correlation" and bool(rec.get("is_control"))
              and rec.get("parsed_answer") is not None
              and isinstance(rec.get("parsed_answer"), (int, float))
              and abs(rec["parsed_answer"]) > 0.30):
            label = "control_false_positive"
        else:
            label = "wrong_finish"
        n_invalid = sum(1 for t in rec.get("react_tool_calls", []) if t.get("action") == "invalid")
        n_getcell = sum(1 for t in rec.get("react_tool_calls", []) if t.get("action") == "GetCell")
        fa.append({
            "object_id": rec["object_id"], "property": prop,
            "locality": "local" if local else "global",
            "tier": rec.get("tier"), "columns": rec.get("columns"),
            "is_control": bool(rec.get("is_control")),
            "ground_truth": rec["ground_truth"], "parsed_answer": rec.get("parsed_answer"),
            "react_steps": rec.get("react_steps"), "react_finished": rec.get("react_finished"),
            "invalid_actions": n_invalid, "getcell_calls": n_getcell,
            "heuristic_label": label, "manual_label": None,
        })
    (RESULTS / "react_failure_analysis.json").write_text(json.dumps(fa, indent=2) + "\n")
    c = Counter(x["heuristic_label"] for x in fa)
    tot = sum(c.values()) or 1
    out(f"{FAIL['label']} REACT FAILURE MODES (heuristic, n={sum(c.values())}, manual review pending)")
    for k, v in c.most_common():
        out(f"  {k:22s} {v:4d}  ({100*v/tot:.0f}%)")
    out("")

out("NOTES")
out("  - Any model whose ReAct run stopped mid-batch on API credit exhaustion")
out("    has a PARTIAL row above and its zero-shot column falls back to")
out("    Experiment 1's committed tabular results (different session, JSON-mode")
out("    on for the models where Experiment 1 used it). Re-run the Phase-2")
out("    runners once credits are topped up for a clean matched pair.")
out("  - The dataset is currently 300/300 synthetic, so the synthetic-vs-real")
out("    contrast is not measurable; the `source` split is kept as a hook only.")
out("  - V4-Pro* in thinking mode already hit the token cap on 100% of")
out("    correlation/skewness in Experiment 1 zero-shot; expect its ReAct arm to")
out("    also rarely reach Finish on those two - its numeric rows will be thin.")
out("  - Failure labels are heuristic; manual_label is left null for the review pass.")
out("  - No cross-domain decision readout here; Experiment 2 has been run on the")
out("    graph and tabular domains only. The PDF Section 9 go/no-go stays deferred.")

(RESULTS / "evaluation_summary.txt").write_text("\n".join(lines) + "\n")
print("\n".join(lines))


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

plt.rcParams.update({"figure.dpi": 110, "font.size": 9, "axes.grid": True,
                     "grid.alpha": 0.25, "axes.axisbelow": True})
C_ZS, C_RE = "#7a7a7a", "#2b6cb0"
DIVIDER = N_LOCAL - 0.5


def fig1():
    m = rows_present
    fig, axes = plt.subplots(1, len(m), figsize=(3.6 * len(m), 3.4), sharey=True, squeeze=False)
    x = np.arange(len(PROP_ORDER))
    for ax, r in zip(axes[0], m):
        za = [acc(r["zs"], p)[0] * 100 for p in PROP_ORDER]
        ra = [acc(r["react"], p)[0] * 100 for p in PROP_ORDER]
        ax.bar(x - 0.2, za, 0.4, label="zero-shot", color=C_ZS)
        ax.bar(x + 0.2, ra, 0.4, label="ReAct", color=C_RE)
        ax.axvline(DIVIDER, color="k", lw=0.8, ls=":")
        ax.set_title(r["label"] + ("  (partial)" if r["partial"] else ""))
        ax.set_xticks(x)
        ax.set_xticklabels(SHORT_LIST, rotation=40, ha="right", fontsize=7)
        ax.set_ylim(0, 105)
    axes[0][0].set_ylabel("accuracy (%)")
    axes[0][0].legend(loc="upper right", fontsize=8)
    fig.suptitle("Fig 1 - ReAct vs zero-shot accuracy by property  (dotted line: local | global)")
    fig.tight_layout()
    fig.savefig(FIGS / "fig1_react_vs_zeroshot_by_property.png", bbox_inches="tight")
    plt.close(fig)


def fig2():
    m = rows_present
    fig, ax = plt.subplots(figsize=(1.3 * len(m) + 3, 3.6))
    x = np.arange(len(m))
    for i, loc in enumerate(("local", "global")):
        d = [delta_ci(r["react"], r["zs"], loc) for r in m]
        vals = [v[0] * 100 for v in d]
        lo = [(v[0] - v[1]) * 100 for v in d]
        hi = [(v[2] - v[0]) * 100 for v in d]
        ax.bar(x + (i - 0.5) * 0.4, vals, 0.4, yerr=[lo, hi], capsize=3,
               label=loc, color=("#2f855a" if loc == "local" else "#c05621"))
    ax.axhline(0, color="k", lw=1)
    ax.set_xticks(x)
    ax.set_xticklabels([r["label"] + ("\n(partial)" if r["partial"] else "") for r in m], fontsize=8)
    ax.set_ylabel("ReAct - zero-shot  (percentage points)")
    ax.legend()
    ax.set_title("Fig 2 - ReAct effect on accuracy, by locality  (95% bootstrap CI)")
    fig.tight_layout()
    fig.savefig(FIGS / "fig2_locality_delta.png", bbox_inches="tight")
    plt.close(fig)


def fig3():
    m = rows_present
    fig, ax = plt.subplots(figsize=(9, 3.6))
    x = np.arange(len(PROP_ORDER))
    w = 0.8 / max(len(m), 1)
    for i, r in enumerate(m):
        fr = [finish_rate(r["react"], p) * 100 for p in PROP_ORDER]
        ax.bar(x + (i - (len(m) - 1) / 2) * w, fr, w, label=r["label"])
    ax.axvline(DIVIDER, color="k", lw=0.8, ls=":")
    ax.set_xticks(x)
    ax.set_xticklabels(SHORT_LIST, rotation=40, ha="right", fontsize=7)
    ax.set_ylabel("ReAct episodes reaching Finish (%)")
    ax.set_ylim(0, 105)
    ax.legend(fontsize=8)
    ax.set_title("Fig 3 - ReAct finish rate by property  (dotted line: local | global)")
    fig.tight_layout()
    fig.savefig(FIGS / "fig3_react_finish_rate.png", bbox_inches="tight")
    plt.close(fig)


def fig4():
    m = rows_present
    fig, axes = plt.subplots(1, len(m), figsize=(3.4 * len(m), 3.2), sharey=True, squeeze=False)
    for ax, r in zip(axes[0], m):
        cats = {
            "finish, correct": [x.get("react_steps", 0) for x in r["react"]
                                if x.get("react_finished") and is_correct(x)],
            "finish, wrong": [x.get("react_steps", 0) for x in r["react"]
                              if x.get("react_finished") and not is_correct(x)],
            "no finish": [x.get("react_steps", 0) for x in r["react"]
                          if not x.get("react_finished")],
        }
        ax.hist([v or [0] for v in cats.values()], bins=range(1, 18),
                stacked=True, label=list(cats), color=["#2f855a", "#c05621", "#a0aec0"])
        ax.set_title(r["label"] + ("  (partial)" if r["partial"] else ""))
        ax.set_xlabel("react_steps")
    axes[0][0].set_ylabel("episodes")
    axes[0][-1].legend(fontsize=8)
    fig.suptitle("Fig 4 - steps per episode, by outcome")
    fig.tight_layout()
    fig.savefig(FIGS / "fig4_steps_breakdown.png", bbox_inches="tight")
    plt.close(fig)


def fig5():
    """Relative-error distribution for the two numeric-tolerance properties
    (graph domain had one panel; tabular has two)."""
    props = ["correlation", "skewness"]
    m = rows_present
    fig, axes = plt.subplots(len(m), 2, figsize=(8, 2.6 * len(m)), squeeze=False)
    try:
        from scipy.stats import gaussian_kde
        have_kde = True
    except Exception:
        have_kde = False
    xs = np.linspace(0, 2, 200)
    for row, r in zip(axes, m):
        for ax, p in zip(row, props):
            for recs, color, lab in ((r["zs"], C_ZS, "zero-shot"),
                                     (r["react"], C_RE, "ReAct")):
                errs = np.array([
                    min(float(x["relative_error"]), 2.0)
                    for x in recs
                    if x["property"] == p and x.get("relative_error") is not None
                ])
                errs = errs[np.isfinite(errs)]
                if len(errs) < 5:
                    continue
                if have_kde and errs.std() > 1e-6:
                    ax.plot(xs, gaussian_kde(errs)(xs), color=color, label=lab)
                else:
                    ax.hist(errs, bins=20, density=True, histtype="step", color=color, label=lab)
            ax.axvline(0.05, color="k", ls=":", lw=0.8)  # 5% tolerance
            ax.set_title(f"{r['label']} - {p}", fontsize=8)
            ax.set_xlabel("relative error (clipped at 2.0)")
        row[0].set_ylabel("density")
    axes[0][-1].legend(fontsize=8)
    fig.suptitle("Fig 5 - numeric-property error distribution, ReAct vs zero-shot  (dotted: 5% tol)")
    fig.tight_layout()
    fig.savefig(FIGS / "fig5_numeric_error_kde.png", bbox_inches="tight")
    plt.close(fig)


if rows_present:
    fig1(); fig2(); fig3(); fig4(); fig5()
    print(f"\nwrote {len(list(FIGS.glob('*.png')))} figures to {FIGS}")
    print(f"wrote {RESULTS / 'evaluation_summary.txt'}")
    print(f"wrote {RESULTS / 'comparison_by_property.csv'}")
    print(f"wrote {RESULTS / 'react_failure_analysis.json'}")
else:
    print("no ReAct results found under phase2_tabular_model_results/react/ - run Phase 2 first")
