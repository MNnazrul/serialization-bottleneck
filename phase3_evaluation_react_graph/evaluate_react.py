"""
Phase 3 — Experiment 2 (ReAct) evaluation, Domain 2 (Graphs).

Reads the Experiment-2 Phase-2 logs in place and produces the same kind of
deliverables as `../phase3_evaluation_graph/` — an accuracy readout, the
local-vs-global contrast, figures, a failure-mode pass, and a saved summary —
but the comparison here is **ReAct vs a matched single-call zero-shot baseline**
rather than zero-shot vs ground-truth difficulty.

Inputs (read straight from the repo, nothing copied):
  ReAct arm      : ../phase2_react_graph/<NN_model>/<key>_results.json
  zero-shot arm  : ../phase2_react_graph/<NN_model>/<key>_zeroshot_results.json
                   (matched control, same session; Experiment 1's method exactly)
  fallback       : ../phase2_model_results_graph/<NN_model>/<key>_results.json
                   (Experiment 1's committed zero-shot, used only where the
                   matched control was not run — models whose API credits ran
                   out mid-experiment)

Outputs (created here):
  results/evaluation_summary.txt
  results/comparison_by_property.csv
  results/react_failure_analysis.json
  results/per_tier_accuracy.csv
  figures/fig1_react_vs_zeroshot_by_property.png
  figures/fig2_locality_delta.png
  figures/fig3_react_finish_rate.png
  figures/fig4_steps_breakdown.png

  python phase3_evaluation_react_graph/evaluate_react.py
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REACT_DIR = ROOT / "phase2_react_graph"
EXP1_DIR = ROOT / "phase2_model_results_graph"
RESULTS = HERE / "results"
FIGS = HERE / "figures"
RESULTS.mkdir(exist_ok=True)
FIGS.mkdir(exist_ok=True)

BOOTSTRAP_N = 10_000
RNG_SEED = 0

# (folder, key, label, full episode count for coverage reporting)
MODELS = [
    ("01_v4flash", "v4flash", "V4-Flash", 1800),
    ("02_qwen3", "qwen3", "Qwen3-32B", 1800),
    ("03_llama_scout", "llama_scout", "Llama4-Scout", 1800),
    ("04_gemini", "gemini", "Gemini2.5-FL", 1800),
    ("05_gpt", "gpt", "GPT-4.1-mini", 1800),
    ("06_v4pro_nonthinking", "v4pro_nonthinking", "V4-Pro*", 360),
]

# Same 6 properties Experiment 1's Phase 3 evaluates since PR #9: triangle_count
# and avg_clustering are dropped (exactly 0 on every bipartite graph, and
# is_bipartite is 50/50 per tier, so "always 0" scored a free ~50%). The
# zero-shot logs still contain them; they are filtered out on load.
PROP_ORDER = [
    "degree_of_node_0", "edge_count",              # local
    "is_bipartite", "is_planar",
    "diameter", "chromatic_number",                # global
]
LOCAL = {"degree_of_node_0", "edge_count"}


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

DATASET = json.loads((ROOT / "phase1_dataset_graph" / "graph_exp1_dataset.json").read_text())
EDGE_LISTS = {g["object_id"]: g["edge_list"] for g in DATASET}


def load(path: Path) -> list[dict]:
    """Records for the evaluated properties. Refuses records produced against a
    different dataset version: object_ids were reused when PR #9 regenerated
    the graphs, so a stale log would otherwise be scored against new ground truth."""
    if not path.exists():
        return []
    recs = [r for r in json.loads(path.read_text()) if r["property"] in PROP_ORDER]
    stale = [r["object_id"] for r in recs if EDGE_LISTS.get(r["object_id"], "\0") not in r["prompt"]]
    if stale:
        raise SystemExit(f"{path}: {len(stale)} record(s) were run on a different graph than the "
                         f"current dataset (e.g. {stale[0]}). Re-run Phase 2 for this model.")
    return recs


def is_correct(rec: dict) -> bool:
    """Same grading as Experiment 1: exact for int/bool, 1% relative for the
    one numeric-tolerance property."""
    if rec["property"] == "avg_clustering":
        return bool(rec.get("correct_1pct"))
    return bool(rec.get("correct"))


def arm(folder: str, key: str):
    """Return (react_records, zeroshot_records, zeroshot_source)."""
    react = load(REACT_DIR / folder / f"{key}_results.json")
    zs = load(REACT_DIR / folder / f"{key}_zeroshot_results.json")
    src = "matched (same session)"
    if not zs:
        zs = load(EXP1_DIR / folder / f"{key}_results.json")
        src = "Experiment 1 committed"
    return react, zs, src


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def acc(records: list[dict], prop: str | None = None, locality: str | None = None) -> tuple[float, int]:
    bits = [
        is_correct(r) for r in records
        if (prop is None or r["property"] == prop)
        and (locality is None
             or (locality == "local") == (r["property"] in LOCAL))
    ]
    if not bits:
        return float("nan"), 0
    return sum(bits) / len(bits), len(bits)


def delta_ci(react: list[dict], zs: list[dict], locality: str | None,
             n: int = BOOTSTRAP_N, seed: int = RNG_SEED) -> tuple[float, float, float]:
    """Bootstrap CI for (ReAct acc - zero-shot acc) over the matching records,
    resampling each arm independently (same recipe as
    ../phase3_evaluation_graph/ Cell 9's gap_ci)."""
    def bits(recs):
        return np.array([
            is_correct(r) for r in recs
            if locality is None
            or (locality == "local") == (r["property"] in LOCAL)
        ], dtype=float)

    a, b = bits(react), bits(zs)
    if len(a) == 0 or len(b) == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    da = rng.choice(a, (n, len(a)), replace=True).mean(axis=1)
    db = rng.choice(b, (n, len(b)), replace=True).mean(axis=1)
    diff = da - db
    return float(a.mean() - b.mean()), float(np.percentile(diff, 2.5)), float(np.percentile(diff, 97.5))


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
      ", ".join(r["label"] for r in rows_present))


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

with (RESULTS / "comparison_by_property.csv").open("w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["model", "zeroshot_source", "property", "locality",
                "n_react", "react_acc", "react_finish_rate",
                "n_zeroshot", "zeroshot_acc", "delta_pp"])
    for r in rows_present:
        for p in PROP_ORDER:
            ra, rn = acc(r["react"], p)
            za, zn = acc(r["zs"], p)
            fin = np.mean([bool(x.get("react_finished")) for x in r["react"] if x["property"] == p] or [np.nan])
            w.writerow([r["label"], r["src"], p,
                        "local" if p in LOCAL else "global",
                        rn, f"{ra:.4f}", f"{fin:.4f}",
                        zn, f"{za:.4f}",
                        f"{100 * (ra - za):+.1f}" if rn and zn else ""])


# ---------------------------------------------------------------------------
# Summary text
# ---------------------------------------------------------------------------

lines: list[str] = []
def out(s: str = "") -> None:
    lines.append(s)

out("EXPERIMENT 2 (ReAct) - GRAPH DOMAIN - EVALUATION SUMMARY")
out("=" * 60)
out("Task: recover graph properties from text-serialized (edge-list) graphs with a")
out("      ReAct Thought/Action/Observation loop over structural tools")
out("      (Neighbors / Degree / HasEdge / Nodes / EdgeCount / Finish),")
out("      vs a matched single-call zero-shot baseline (Experiment 1's method).")
out("Grading: identical to Experiment 1 (exact match; all evaluated properties are")
out("         int/bool). Evaluated: the same 6 properties as Experiment 1's Phase 3")
out("         (triangle_count / avg_clustering dropped). ReAct episodes that never")
out("         reach Finish are scored incorrect (no CoT-SC back-off).")
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

out("PER-PROPERTY (ReAct acc  ->  zero-shot acc   [ReAct finish rate])")
for r in rows_present:
    out(f"  {r['label']}  ({r['src']})")
    for p in PROP_ORDER:
        ra, rn = acc(r["react"], p)
        za, zn = acc(r["zs"], p)
        fin = np.mean([bool(x.get("react_finished")) for x in r["react"] if x["property"] == p] or [np.nan])
        loc = "L" if p in LOCAL else "G"
        out(f"    [{loc}] {p:18s} {100*ra:5.1f}% -> {100*za:5.1f}%   fin {100*fin:3.0f}%   d {100*(ra-za):+5.1f}pp")
    out("")

# ---------------------------------------------------------------------------
# Per-tier accuracy, with class-balanced scoring for the boolean properties
# ---------------------------------------------------------------------------
# If the positive rate of is_planar / is_bipartite differs between tiers, plain
# accuracy rewards a constant "false" guess more in the low-positive tiers. On
# the pre-PR#9 dataset is_planar was 52% true on simple vs ~20% on medium/hard,
# which put Llama4-Scout zero-shot's medium tier above simple. PR #9 makes both
# booleans exactly 50/50 per tier, so on the full 300 balanced == plain; the
# balanced score is kept as a guard, and it still corrects any skew left in the
# V4-Pro* 60-graph subsample. Each boolean property is scored as the mean of its
# true-class and false-class accuracy within each tier and within each model's
# own graph set. Tier accuracy is the mean over the evaluated properties.

TIERS = ("simple", "medium", "hard")
BOOL_PROPS = {"is_bipartite", "is_planar"}


def tier_accuracy(records: list[dict], tier: str, balanced: bool = True) -> float:
    per_prop = []
    for p in PROP_ORDER:
        recs = [r for r in records if r["tier"] == tier and r["property"] == p]
        if not recs:
            continue
        if balanced and p in BOOL_PROPS:
            classes = [[is_correct(r) for r in recs if r["ground_truth"] is v] for v in (True, False)]
            per_prop.append(np.mean([np.mean(c) for c in classes if c]))
        else:
            per_prop.append(np.mean([is_correct(r) for r in recs]))
    return float(np.mean(per_prop)) * 100 if per_prop else float("nan")


def tier_gap_ci(records: list[dict], easier: str, harder: str,
                n: int = 2000, seed: int = RNG_SEED) -> tuple[float, float, float]:
    """(harder - easier) tier accuracy in pp, with a 95% bootstrap CI that
    resamples whole graphs within each tier."""
    by_tier = {t: {} for t in (easier, harder)}
    for r in records:
        if r["tier"] in by_tier:
            by_tier[r["tier"]].setdefault(r["object_id"], []).append(r)
    rng = np.random.default_rng(seed)

    def score(tier, ids):
        return tier_accuracy([r for i in ids for r in by_tier[tier][i]], tier)

    ids = {t: list(by_tier[t]) for t in by_tier}
    obs = score(harder, ids[harder]) - score(easier, ids[easier])
    diffs = [score(harder, rng.choice(ids[harder], len(ids[harder])))
             - score(easier, rng.choice(ids[easier], len(ids[easier]))) for _ in range(n)]
    return obs, float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def tier_row(records: list[dict], balanced: bool = True) -> list[float]:
    accs = [tier_accuracy(records, t, balanced) for t in TIERS]
    overall = float(np.mean(accs))
    return accs + [overall]


TIER_ARMS = [("ReAct", lambda r: r["react"]),
             ("zero-shot (matched)", lambda r: r["zs"]),
             ("zero-shot (Experiment 1)",
              lambda r: load(EXP1_DIR / r["folder"] / f"{r['key']}_results.json"))]

with (RESULTS / "per_tier_accuracy.csv").open("w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["arm", "model", "scoring", "simple", "medium", "hard", "overall", "monotonic"])
    for arm_name, get in TIER_ARMS:
        for r in rows_present:
            recs = get(r)
            for balanced in (True, False):
                s, m_, h, o = tier_row(recs, balanced)
                w.writerow([arm_name, r["label"], "balanced_bool" if balanced else "plain",
                            f"{s:.1f}", f"{m_:.1f}", f"{h:.1f}", f"{o:.1f}", s >= m_ >= h])

out("PER-TIER ACCURACY  (class-balanced is_bipartite / is_planar; see per_tier_accuracy.csv)")
def positives(graphs: list[dict]) -> str:
    return "   ".join(
        p + " " + "/".join(f"{sum(1 for g in graphs if g['tier'] == t and g['properties'][p])}"
                           f" of {sum(1 for g in graphs if g['tier'] == t)}" for t in TIERS)
        for p in sorted(BOOL_PROPS))


_sub_ids = set(json.loads((REACT_DIR / "06_v4pro_nonthinking" / "subsample_v4pro_nonthinking.json")
                          .read_text())["object_ids"])
out("  Positives per tier (simple/medium/hard):")
out("    full 300   " + positives(DATASET))
out("    V4-Pro* 60 " + positives([g for g in DATASET if g["object_id"] in _sub_ids]))
for arm_name, get in TIER_ARMS:
    out(f"  {arm_name}")
    out(f"    {'model':13s}  {'simple':>6s} {'medium':>6s} {'hard':>6s}  {'overall':>7s}   plain-acc simple/medium/hard")
    for r in sorted(rows_present, key=lambda r: -tier_row(get(r))[3]):
        s, m_, h, o = tier_row(get(r))
        ps, pm, ph, _ = tier_row(get(r), balanced=False)
        out(f"    {r['label']:13s}  {s:5.1f}% {m_:5.1f}% {h:5.1f}%  {o:6.1f}%   {ps:4.1f}/{pm:4.1f}/{ph:4.1f}")
        for easier, harder, a, b in (("simple", "medium", s, m_), ("medium", "hard", m_, h)):
            if b > a:
                d, lo, hi = tier_gap_ci(get(r), easier, harder)
                verdict = "within noise" if lo <= 0 <= hi else "SIGNIFICANT"
                out(f"      ^ not monotonic: {harder} - {easier} = {d:+.1f}pp, "
                    f"95% CI [{lo:+.1f},{hi:+.1f}] ({verdict})")
out("  V4-Pro* is scored on its own 60-graph subsample; compare it to the full-300")
out("  models with that caveat.")
# Euler's bound: a simple planar graph has m <= 3n - 6. Both n and m are in the
# serialization header, so for graphs with m > 3n - 6 is_planar is decidable
# without reading any edge. PR #9's denser large graphs make this more common
# in harder tiers, which lets a ReAct model answer Nodes[] -> EdgeCount[] ->
# Finish[false] and lifts hard-tier is_planar above medium. Reported, not
# filtered: excluding these graphs would diverge from Experiment 1's scoring.
_euler = [sum(1 for g in DATASET if g["tier"] == t and g["num_edges"] > 3 * g["num_nodes"] - 6)
          for t in TIERS]
out(f"  is_planar decidable from the header alone (m > 3n-6, so provably non-planar):")
out(f"    simple/medium/hard = {_euler[0]}/{_euler[1]}/{_euler[2]} of 100 graphs. ReAct models use")
out("    this shortcut (Nodes[] -> EdgeCount[] -> Finish[false]), which raises")
out("    hard-tier is_planar above medium; see the per-property rows above.")
out("")

out("MECHANISM")
out("  - Local properties (degree_of_node_0, edge_count) are one tool call away:")
out("    ReAct issues Degree[0] / EdgeCount[] and finishes in ~2 steps, removing")
out("    the zero-shot miscount.")
out("  - Global properties need an aggregate over the whole structure. The model")
out("    spends its step budget on one-at-a-time HasEdge / Neighbors probes and")
out("    often never reaches Finish (see finish rates above); zero-shot at least")
out("    guesses from the full edge list in context. This is the paper's own")
out("    trade-off (Table 2: the structural constraint raises reasoning-error rate).")
out("  - The action parser accepts decorated but valid actions (`Action 3: [Finish[4]]`,")
out("    `**Action 3:** ...`, backticks). The pre-PR#9 run's strict parser rejected")
out("    them, which caused a spurious Gemini edge_count drop; this run used the")
out("    fixed parser from the start (records carry action_parser=2).")
out("")

# steps stats for the ReAct arm
out("REACT LOOP BEHAVIOUR (per model)")
for r in rows_present:
    steps = np.array([x.get("react_steps", 0) for x in r["react"]])
    fin = np.array([bool(x.get("react_finished")) for x in r["react"]])
    correct = np.array([is_correct(x) for x in r["react"]])
    out(f"  {r['label']:13s} finish {100*fin.mean():4.0f}%   mean steps {steps.mean():4.1f}"
        f"   mean steps|correct {steps[correct].mean() if correct.any() else float('nan'):4.1f}"
        f"   no_finish {100*(~fin).mean():4.0f}%")
out("")

# failure taxonomy for the debug model
FAIL = next((r for r in rows_present if r["key"] == "v4flash"), rows_present[0] if rows_present else None)
if FAIL:
    fa = []
    for rec in FAIL["react"]:
        if is_correct(rec):
            continue
        if not rec.get("react_finished"):
            label = "no_finish_local" if rec["property"] in LOCAL else "no_finish_global"
        elif not rec.get("parse_success"):
            label = "unparseable_finish"
        else:
            label = "wrong_finish"
        n_invalid = sum(1 for t in rec.get("react_tool_calls", []) if t.get("action") == "invalid")
        fa.append({
            "object_id": rec["object_id"], "property": rec["property"],
            "locality": "local" if rec["property"] in LOCAL else "global",
            "ground_truth": rec["ground_truth"], "parsed_answer": rec.get("parsed_answer"),
            "react_steps": rec.get("react_steps"), "react_finished": rec.get("react_finished"),
            "invalid_actions": n_invalid,
            "heuristic_label": label, "manual_label": None,
        })
    (RESULTS / "react_failure_analysis.json").write_text(json.dumps(fa, indent=2) + "\n")
    from collections import Counter
    c = Counter(x["heuristic_label"] for x in fa)
    tot = sum(c.values()) or 1
    out(f"{FAIL['label']} REACT FAILURE MODES (heuristic, n={sum(c.values())}, manual review pending)")
    for k, v in c.most_common():
        out(f"  {k:20s} {v:4d}  ({100*v/tot:.0f}%)")
    out("")

_partial = [r["label"] for r in rows_present if r["partial"]]
_fallback = [r["label"] for r in rows_present if r["src"] != "matched (same session)"]
if _partial or _fallback:
    out("NOTE")
    if _partial:
        out(f"  Partial ReAct coverage (stopped on API credit exhaustion): {', '.join(_partial)}.")
        out("  Re-run the runners to complete; treat those rows' CIs as wider than printed.")
    if _fallback:
        out(f"  Zero-shot column is Experiment 1's committed run (not a same-session")
        out(f"  matched control) for: {', '.join(_fallback)}.")
else:
    out("NOTE")
    out("  All six models have full ReAct coverage and a matched, same-session")
    out("  zero-shot control. V4-Pro* is the 60-graph 20% subsample, as in Experiment 1.")

(RESULTS / "evaluation_summary.txt").write_text("\n".join(lines) + "\n")
print("\n".join(lines))


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

plt.rcParams.update({"figure.dpi": 110, "font.size": 9, "axes.grid": True,
                     "grid.alpha": 0.25, "axes.axisbelow": True})
C_ZS, C_RE = "#7a7a7a", "#2b6cb0"
short = {
    "degree_of_node_0": "deg(0)", "edge_count": "edges", "is_bipartite": "bipartite",
    "is_planar": "planar", "triangle_count": "triangles", "diameter": "diameter",
    "chromatic_number": "chromatic", "avg_clustering": "clustering",
}
SHORT = [short[p] for p in PROP_ORDER]


def fig1():
    m = rows_present
    fig, axes = plt.subplots(1, len(m), figsize=(3.6 * len(m), 3.4), sharey=True, squeeze=False)
    x = np.arange(len(PROP_ORDER))
    for ax, r in zip(axes[0], m):
        za = [acc(r["zs"], p)[0] * 100 for p in PROP_ORDER]
        ra = [acc(r["react"], p)[0] * 100 for p in PROP_ORDER]
        ax.bar(x - 0.2, za, 0.4, label="zero-shot", color=C_ZS)
        ax.bar(x + 0.2, ra, 0.4, label="ReAct", color=C_RE)
        ax.axvline(1.5, color="k", lw=0.8, ls=":")
        ax.set_title(r["label"] + ("  (partial)" if r["partial"] else ""))
        ax.set_xticks(x)
        ax.set_xticklabels(SHORT, rotation=40, ha="right", fontsize=7)
        ax.set_ylim(0, 105)
    axes[0][0].set_ylabel("accuracy (%)")
    axes[0][0].legend(loc="upper right", fontsize=8)
    fig.suptitle("Fig 1 — ReAct vs zero-shot accuracy by property  (dotted line: local | global)")
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
        ax.bar(x + (i - 0.5) * 0.4, vals, 0.4,
               yerr=[lo, hi], capsize=3,
               label=loc, color=("#2f855a" if loc == "local" else "#c05621"))
    ax.axhline(0, color="k", lw=1)
    ax.set_xticks(x)
    ax.set_xticklabels([r["label"] + ("\n(partial)" if r["partial"] else "") for r in m], fontsize=8)
    ax.set_ylabel("ReAct − zero-shot  (percentage points)")
    ax.legend()
    ax.set_title("Fig 2 — ReAct effect on accuracy, by locality  (95% bootstrap CI)")
    fig.tight_layout()
    fig.savefig(FIGS / "fig2_locality_delta.png", bbox_inches="tight")
    plt.close(fig)


def fig3():
    m = rows_present
    fig, ax = plt.subplots(figsize=(9, 3.6))
    x = np.arange(len(PROP_ORDER))
    w = 0.8 / len(m)
    for i, r in enumerate(m):
        fr = [np.mean([bool(z.get("react_finished")) for z in r["react"] if z["property"] == p] or [np.nan]) * 100
              for p in PROP_ORDER]
        ax.bar(x + (i - (len(m) - 1) / 2) * w, fr, w, label=r["label"])
    ax.axvline(1.5, color="k", lw=0.8, ls=":")
    ax.set_xticks(x)
    ax.set_xticklabels(SHORT, rotation=40, ha="right", fontsize=7)
    ax.set_ylabel("ReAct episodes reaching Finish (%)")
    ax.set_ylim(0, 105)
    ax.legend(fontsize=8)
    ax.set_title("Fig 3 — ReAct finish rate by property  (dotted line: local | global)")
    fig.tight_layout()
    fig.savefig(FIGS / "fig3_react_finish_rate.png", bbox_inches="tight")
    plt.close(fig)


def fig4():
    m = rows_present
    fig, axes = plt.subplots(1, len(m), figsize=(3.4 * len(m), 3.2), sharey=True, squeeze=False)
    for ax, r in zip(axes[0], m):
        cats = {
            "finish, correct": [x["react_steps"] for x in r["react"] if x.get("react_finished") and is_correct(x)],
            "finish, wrong": [x["react_steps"] for x in r["react"] if x.get("react_finished") and not is_correct(x)],
            "no finish": [x["react_steps"] for x in r["react"] if not x.get("react_finished")],
        }
        ax.hist([v or [0] for v in cats.values()], bins=range(1, 18),
                stacked=True, label=list(cats), color=["#2f855a", "#c05621", "#a0aec0"])
        ax.set_title(r["label"] + ("  (partial)" if r["partial"] else ""))
        ax.set_xlabel("react_steps")
    axes[0][0].set_ylabel("episodes")
    axes[0][-1].legend(fontsize=8)
    fig.suptitle("Fig 4 — steps per episode, by outcome")
    fig.tight_layout()
    fig.savefig(FIGS / "fig4_steps_breakdown.png", bbox_inches="tight")
    plt.close(fig)


if rows_present:
    fig1(); fig2(); fig3(); fig4()
    print(f"\nwrote {len(list(FIGS.glob('*.png')))} figures to {FIGS}")
    print(f"wrote {RESULTS/'evaluation_summary.txt'}")
    print(f"wrote {RESULTS/'comparison_by_property.csv'}")
else:
    print("no ReAct results found under phase2_react_graph/ — run Phase 2 first")
