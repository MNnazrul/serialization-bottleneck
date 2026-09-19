"""
Phase 3 evaluation for the exp1_geo run  (dataset new_data_v7, five models).

    python eval.py --model 05_gpt      -> 04_evaluation/05_gpt/
    python eval.py --model all         -> every model
    python eval.py --model all --skip 03_llama_scout

This is eval.py from phase2_model_results/, with one change: the paths point
inside exp1_geo instead of back into the main tree, so this folder runs on its
own. Statistics, figures and report text are untouched.

    dataset   ../01_dataset/geometry_exp1_dataset.json
    summary   ../01_dataset/geometry_exp1_summary.json
    results   ../03_results/<model>/<model>_results.jsonl
    output    ./<model>/   and ./cross_model/ for the two cross-model figures

NOTE ON THE VERSION NUMBERS. The suffix names the DATASET, not the run count,
and they are off by one: these v8 runners/results are the new_data_v7
generation. See ../README.md.

WHAT THE FLOORS IN THIS REPORT DO AND DO NOT COVER

new_data_v7 publishes, as the answer-without-reading floor for area, the rate at
which "answer the whole bounding-box area" scores -- which is 0/0/0, and is not
the bar that matters. The bar that matters is the best CONSTANT MULTIPLE of the
box, and on this dataset that scores 57/59/64 at the 10% band for area and
98/84/64 for perimeter, above every model in this run. This script scores
against what the dataset publishes, so its margin-over-floor numbers for those
two properties are OPTIMISTIC. The corrected floors are computed in
../../new_data_v8/ and in eval_v9.py; the difference is the reason that rebuild
exists. Do not read area or perimeter accuracy here as evidence of computation.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")               # write files; never open a window
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
# Paths are rewritten for the exp1_geo layout: this folder is self-contained,
# so the dataset, the results and this script all live under one root and
# nothing reaches back into phase2_model_results/.
#
#   exp1_geo/01_dataset/    the 300 polygons + summary
#   exp1_geo/03_results/    <model>/<model>_results.jsonl
#   exp1_geo/04_evaluation/ this script; output lands in <model>/ beside it
# Split layout: this file lives at phase3_evaluation_geo/evaluation/.
REPO_ROOT = HERE.parent.parent
DATASET = REPO_ROOT / "phase1_dataset_geo" / "geometry_exp1_dataset.json"
SUMMARY = REPO_ROOT / "phase1_dataset_geo" / "geometry_exp1_summary.json"

# folder -> (results .jsonl basename, display label). The file lives in
# results_v8/<folder>/, one tree for every model.
# Evaluation OUTPUT still lands in ../04_evaluation/<folder>/, beside the model it is about.
RESULTS_ROOT = REPO_ROOT / "phase2_model_results_geo" / "results"
# folder -> (results filename, display label, expected record count)
#
# 06_v4pro_nonthinking is the odd one out and deliberately so: PDF Section 3
# runs V4-Pro on a 20% stratified subsample, so it has 60 polygons x 9
# properties = 540 records where the other five have 300 x 9 = 2700. It is a
# complete run of a smaller sample, not a partial run of the full one, and the
# completeness check below uses its own target rather than 2700.
#
# WHAT THAT MEANS WHEN READING ITS NUMBERS. Its per-tier n is 20, not 100, so
# one polygon moves a tier accuracy by 5 percentage points instead of 1, and
# its confidence intervals are correspondingly wider. Every per-tier comparison
# against the other five has to carry that; the report prints the n so it
# cannot be lost.
MODELS = {
    "01_v4flash":           ("v4flash_results.jsonl",           "DeepSeek-V4-Flash",     2700),
    "02_qwen3":             ("qwen3_results.jsonl",             "Qwen3-32B",             2700),
    "03_llama_scout":       ("llama_scout_results.jsonl",       "Llama-4-Scout",         2700),
    "04_gemini":            ("gemini_results.jsonl",            "Gemini-2.5-Flash-Lite", 2700),
    "05_gpt":               ("gpt_results.jsonl",               "GPT-4.1-mini",          2700),
    "06_v4pro_nonthinking": ("v4pro_nonthinking_results.jsonl", "DeepSeek-V4-Pro (20%)",  540),
}


def expected_records(folder: str) -> int:
    """How many records a COMPLETE run of this model has. 2700 for the five
    full-coverage models, 540 for the 20% subsample arm."""
    return MODELS[folder][2]


def results_path(folder: str) -> Path:
    return RESULTS_ROOT / folder / MODELS[folder][0]

TIERS = ["simple", "medium", "hard"]
BANDS = [0, 1, 2, 3]
BAND_EDGES = ["0.00-0.02", "0.02-0.04", "0.04-0.07", "> 0.07"]
FILL_BAND_EDGES = ["< 0.52", "0.52-0.62", "0.62-0.70", "0.70-0.76"]
PROP_ORDER = [
    "vertex_count", "bbox",                                    # local
    "centroid", "area", "perimeter", "convex", "orientation",
    "aspect_ratio", "edge_length_variance",                    # global
]
LOCAL = {"vertex_count", "bbox"}
NUMERIC = {"bbox", "centroid", "area", "perimeter", "aspect_ratio",
           "edge_length_variance"}

# property -> the keys under summary["baselines"][tol][tier] that give
# answer-without-reading rates for that property.
#
# A LIST, not a single key, because new_data_v7 publishes more than one way to
# answer some properties without reading: the bbox centre and the coordinate
# mean for centroid, the bbox area and the inscribed ellipse for area. A model
# only has to find whichever works best, so the floor it must clear is the MAX
# over its shortcuts - using the first or the mean would set the bar too low.
# Each shortcut is also reported separately, since which one a dataset makes
# easy is part of what the number means.
#
# bbox and edge_length_variance are ABSENT on purpose: this dataset publishes no
# floor for them, and inventing one would put a number in a figure that no
# pre-registration stands behind. (v5 also had none for area or perimeter; v6
# does, which is why those two moved out of the no-floor list.)
BASELINE_KEYS = {
    "convex":       ["convex_majority"],
    "orientation":  ["orientation_majority"],
    # `vertex_count_modal` is new in new_data_v7 and is the one that bites: the
    # rate a guesser gets by always answering the tier's most common vertex
    # count. `vertex_count_plus_one` is kept only so the v7 evaluation's column
    # still exists; it is 0.0 everywhere by construction and never sets the max.
    "vertex_count": ["vertex_count_modal", "vertex_count_plus_one"],
    "centroid":     ["centroid_bbox_center", "centroid_vertex_mean"],
    "area":         ["area_bbox_area", "area_ellipse_in_bbox"],
    "perimeter":    ["perimeter_bbox_perimeter"],
    "aspect_ratio": ["aspect_ratio_const_1.0"],
}
# new_data_v7 publishes a 25% column too, so the fourth tolerance now has a
# floor instead of being reported against nothing.
TOL_SUMMARY_KEY = {"correct_1pct": "tol_0.01", "correct_5pct": "tol_0.05",
                   "correct_10pct": "tol_0.1", "rel_25pct": "tol_0.25"}
HEADLINE_TOL = "correct_5pct"        # PDF Table 13 "Moderate"

# PDF Table 13 plus the 25% level the dataset's own metrics block asks for.
# 25% has no stored flag - the runners record 1/5/10 - so it is recomputed from
# relative_error and is None wherever that field is missing.
TOLERANCES = [("correct_1pct", "1%"), ("correct_5pct", "5%"),
              ("correct_10pct", "10%"), ("rel_25pct", "25%")]
EXTRA_TOL = {"rel_25pct": 0.25}

BOOLEAN = ["convex"]           # PDF 6.2: accuracy, precision, recall, 2x2, base rate
CATEGORICAL = ["orientation"]  # PDF 6.2: accuracy and confusion matrix

TIER_COLOR = {"simple": "#4C9F70", "medium": "#E1A730", "hard": "#C0504D"}
BAND_COLOR = {0: "#B8C4CC", 1: "#B8C4CC", 2: "#B8C4CC", 3: "#0077B6"}
FLOOR_COLOR = "#C0504D"
THRESHOLD_COLOR = FLOOR_COLOR
MODEL_COLOR = {
    "DeepSeek-V4-Flash":     "#0077B6",
    "Qwen3-32B":             "#E1A730",
    "Llama-4-Scout":         "#7B4EA8",
    "Gemini-2.5-Flash-Lite": "#4C9F70",
    "GPT-4.1-mini":          "#C0504D",
}
DPI = 130
MIN_BIN = 8                  # records needed before a vertex-count bin is drawn

BOOTSTRAP_N = 10000
CI_LEVEL = 95
RNG_SEED = 0
EXPECTED_RECORDS = 2700          # the five full-coverage models; see MODELS for the 20% arm


# =============================================================================
# Loading
# =============================================================================

def load_records(path: Path) -> list[dict]:
    """Last record per (object_id, property).

    The log is append-only and a stuck pair may have been re-queried at a larger
    max_tokens; keeping the last occurrence is what makes the re-query count and
    the superseded attempt not count. Both remain in the file.
    """
    latest: dict[tuple[str, str], dict] = {}
    with path.open() as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                latest[(r["object_id"], r["property"])] = r
    return list(latest.values())


def correct_bit(r) -> int:
    """One correctness bit per record: numeric -> within 5%, exact -> exact.
    A parse failure or a missing flag counts as incorrect, never as missing."""
    if r["property"] in NUMERIC:
        return 1 if r.get(HEADLINE_TOL) is True else 0
    return 1 if r.get("correct") is True else 0


def tol_bit(r, key):
    """Correctness at one tolerance. The stored flags cover 1/5/10%; 25% is
    recomputed from relative_error, and is None when that field is absent so a
    missing measurement is never silently scored as a failure."""
    if key in EXTRA_TOL:
        e = r.get("relative_error")
        if not isinstance(e, (int, float)) or not math.isfinite(e):
            return None
        return 1 if e <= EXTRA_TOL[key] else 0
    return 1 if r.get(key) is True else 0


# =============================================================================
# Bootstrap
#
# Plain resampling of records, NOT the cluster bootstrap eval_v5.py uses. There
# the 300 records were 150 shapes serialized twice, so records within a shape
# were correlated and resampling them independently gave intervals that were too
# narrow. Here each record is its own polygon: there is nothing to cluster on,
# and clustering on object_id would just be a slower way of doing the same thing.
# =============================================================================

def acc(recs):
    if not recs:
        return None
    return sum(correct_bit(r) for r in recs) / len(recs)


def bootstrap_means(recs, n=BOOTSTRAP_N, seed=RNG_SEED):
    if not recs:
        return np.empty(0)
    b = np.asarray([correct_bit(r) for r in recs], dtype=float)
    rng = np.random.default_rng(seed)
    draw = rng.integers(0, b.size, size=(n, b.size))
    return b[draw].mean(axis=1)


def ci(recs, n=BOOTSTRAP_N, seed=RNG_SEED):
    m = bootstrap_means(recs, n, seed)
    if m.size == 0:
        return (None, None)
    lo = float(np.percentile(m, (100 - CI_LEVEL) / 2))
    hi = float(np.percentile(m, 100 - (100 - CI_LEVEL) / 2))
    return (lo, hi)


def bootstrap_diff(recs_a, recs_b, n=BOOTSTRAP_N, seed=RNG_SEED):
    """Two-sided test of acc(a) - acc(b). The two arms are disjoint sets of
    polygons, so they are resampled independently."""
    if not recs_a or not recs_b:
        return (None, None)
    da = bootstrap_means(recs_a, n, seed)
    db = bootstrap_means(recs_b, n, seed + 1)
    diff = da - db
    effect = 100.0 * (acc(recs_a) - acc(recs_b))
    p = 2.0 * min(float((diff <= 0).mean()), float((diff >= 0).mean()))
    return (effect, min(1.0, p))


def bootstrap_vs_floor(recs, floor, n=BOOTSTRAP_N, seed=RNG_SEED):
    """One-sided: is accuracy ABOVE the answer-without-reading floor?

    The floor is a fixed number computed from the dataset, not an estimate from
    a second sample, so only the accuracy side is resampled. One-sided because
    the question is never 'does the model do worse than guessing' - falling
    below the floor and matching it are the same finding, which is that nothing
    has been demonstrated."""
    if not recs or floor is None:
        return (None, None)
    m = bootstrap_means(recs, n, seed)
    effect = 100.0 * (acc(recs) - floor)
    return (effect, float((m <= floor).mean()))


def holm(pairs):
    """Holm-Bonferroni. pairs = [(name, p), ...] -> {name: (p, p_adj, reject)}.

    Applied within each family of tests below, not across the whole script:
    the families answer different questions and pooling them would penalise
    every finding for the existence of the others.
    """
    live = [(nm, p) for nm, p in pairs if p is not None]
    m = len(live)
    out = {nm: (None, None, None) for nm, p in pairs}
    running = 0.0
    for rank, (nm, p) in enumerate(sorted(live, key=lambda t: t[1])):
        adj = min(1.0, max(running, (m - rank) * p))
        running = adj
        out[nm] = (p, adj, adj < 0.05)
    return out


def pct(x, d=1):
    return "  n/a" if x is None else f"{100*x:.{d}f}"


def floors_for(base, prop, tier, tol=HEADLINE_TOL):
    """{shortcut name: published rate} for one property, or {} where the dataset
    publishes none (bbox, edge_length_variance)."""
    keys = BASELINE_KEYS.get(prop)
    if not keys or tol not in TOL_SUMMARY_KEY:
        return {}
    cell = base[TOL_SUMMARY_KEY[tol]][tier]
    return {k: cell[k] for k in keys if k in cell}


def floor_for(base, prop, tier, tol=HEADLINE_TOL):
    """The best published non-answer for this cell - the bar a model has to
    clear to have shown anything. See BASELINE_KEYS for why this is a max."""
    fl = floors_for(base, prop, tier, tol)
    return max(fl.values()) if fl else None


# =============================================================================
# Report
# =============================================================================

def evaluate(folder: str, out_root: Path | None = None) -> dict:
    _rel, label, _n_expected = MODELS[folder]
    path = results_path(folder)
    if not path.exists():
        raise SystemExit(f"results not found: {path}")
    recs = load_records(path)
    out_dir = (out_root or HERE) / folder
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = json.loads(SUMMARY.read_text())
    base = summary["baselines"]
    preds = summary.get("predictions", {})

    lines: list[str] = []

    def say(s=""):
        print(s)
        lines.append(s)

    say("=" * 78)
    say(f"PHASE 3 EVALUATION - {label}   (new_data_v7, integer coordinates)")
    say("=" * 78)

    # ---------------------------------------------------------------- integrity
    trunc = [r for r in recs if r.get("failure_type") == "reasoning_truncated"]
    pfail = [r for r in recs if r.get("failure_type") == "parse_failure"]
    unparsed = [r for r in recs if not r.get("parse_success")]
    budgets = Counter(r.get("max_tokens") for r in recs)
    band_n = Counter(r.get("offset_band") for r in recs)
    fill_n = Counter(r.get("fill_band") for r in recs)
    gens = Counter(r.get("generator") for r in recs)

    say()
    say("RUN INTEGRITY")
    say("-" * 78)
    n_expected = expected_records(folder)
    n_polys = n_expected // len(PROP_ORDER)
    say(f"  records (latest per pair) : {len(recs)}  (expected {n_expected})")
    if n_expected != EXPECTED_RECORDS:
        say(f"  *** 20% SUBSAMPLE ARM: {n_polys} polygons, not 300. Per-tier n is "
            f"{n_polys // len(TIERS)}, so one polygon is "
            f"{100 / max(n_polys // len(TIERS), 1):.0f} percentage points. ***")
    say(f"  distinct polygons         : {len({r['object_id'] for r in recs})}  (expected 300)")
    say(f"  offset_band 0/1/2/3       : "
        f"{band_n.get(0,0)} / {band_n.get(1,0)} / {band_n.get(2,0)} / {band_n.get(3,0)}")
    say(f"  fill_band   0/1/2/3       : "
        f"{fill_n.get(0,0)} / {fill_n.get(1,0)} / {fill_n.get(2,0)} / {fill_n.get(3,0)}")
    say(f"  generators                : {dict(sorted(gens.items(), key=lambda t: str(t[0])))}")
    say(f"  parse failures            : {len(pfail)}")
    say(f"  truncated (any parse)     : {len(trunc)}")
    say(f"  unparsed for any reason   : {len(unparsed)}")
    say(f"  max_tokens used           : "
        f"{dict(sorted(budgets.items(), key=lambda t: (t[0] is None, t[0])))}")
    if len(recs) != n_expected:
        say(f"  !! INCOMPLETE RUN - every number below is over {len(recs)} records,")
        say(f"     not {n_expected}, and is not comparable with a complete model.")
    if len(budgets) > 1:
        say("  note: a re-queried pair ran at a larger budget; the record stores it.")
    if None in band_n:
        say("  !! some records carry no offset_band - they came from a runner")
        say("     built for a different dataset. The centroid section below is")
        say("     not trustworthy for those.")

    def sub(prop=None, tier=None, band=None, fill=None, local=None, src=None,
            family=None):
        out = recs if src is None else src
        if prop:
            out = [r for r in out if r["property"] == prop]
        if tier:
            out = [r for r in out if r["tier"] == tier]
        if band is not None:
            out = [r for r in out if r.get("offset_band") == band]
        if fill is not None:
            out = [r for r in out if r.get("fill_band") == fill]
        if family is not None:
            # `family` is new in new_data_v7. Fall back to `generator`, which
            # the runners also carry and which is equal to it in this dataset.
            out = [r for r in out
                   if (r.get("family") or r.get("generator")) == family]
        if local is True:
            out = [r for r in out if r["property"] in LOCAL]
        if local is False:
            out = [r for r in out if r["property"] not in LOCAL]
        return out

    # ------------------------------------------------- P1: monotonicity by tier
    say()
    say("P1 (PRIMARY)  per-property accuracy by tier")
    say("-" * 78)
    say("  Monotone means simple >= medium >= hard. Tier here is vertex count")
    say("  ALONE - the coordinate format is the same in all three, which is the")
    say("  whole point of this dataset. p is a two-sided bootstrap on")
    say("  simple-minus-hard, Holm-corrected across the 9 properties.")
    say()
    say(f'    {"property":24s} {"simple":>8s} {"medium":>8s} {"hard":>8s}   '
        f'{"s-h (pp)":>9s} {"p":>7s} {"p_Holm":>8s}  monotone')
    matrix, p1_tests = {}, []
    for prop in PROP_ORDER:
        cells = [acc(sub(prop=prop, tier=t)) for t in TIERS]
        matrix[prop] = cells
        eff, p = bootstrap_diff(sub(prop=prop, tier="simple"),
                                sub(prop=prop, tier="hard"))
        p1_tests.append((prop, p))
        matrix[(prop, "effect")] = eff
    h1 = holm(p1_tests)
    mono_props = []
    for prop in PROP_ORDER:
        cells = matrix[prop]
        eff = matrix[(prop, "effect")]
        _, adj, rej = h1[prop]
        mono = all(a is not None and b is not None and a >= b - 1e-12
                   for a, b in zip(cells, cells[1:]))
        if mono:
            mono_props.append(prop)
        say(f'    {prop:24s} {pct(cells[0]):>8s} {pct(cells[1]):>8s} '
            f'{pct(cells[2]):>8s}   {("  n/a" if eff is None else f"{eff:+8.1f}"):>9s} '
            f'{("n/a" if h1[prop][0] is None else f"{h1[prop][0]:.4f}"):>7s} '
            f'{("n/a" if adj is None else f"{adj:.4f}"):>8s}  '
            f'{"yes" if mono else "NO"}{"  *" if rej else ""}')
    sig = [nm for nm, (p, adj, rej) in h1.items() if rej]
    say()
    say(f"  monotone in {len(mono_props)}/{len(PROP_ORDER)} properties: "
        f"{', '.join(mono_props) if mono_props else '(none)'}")
    say(f"  simple-vs-hard significant after Holm in {len(sig)}/{len(p1_tests)}: "
        f"{', '.join(sorted(sig)) if sig else '(none)'}")
    say("  Monotone-but-not-significant is the common case and means the")
    say("  ordering held while the data cannot rule out chance; it is not the")
    say("  same finding as a significant decline.")

    # ------------------------- P1b: the expectations carried over from v6
    #
    # Deliberately NOT titled "pre-registered". The v7 evaluation could say that
    # because new_data_v6's predictions block was written before its models ran.
    # new_data_v7's block is a verbatim copy of it, added to the generator after
    # the v8 runs had already finished, and the block says so in its own
    # `provenance` field. Printing it as a sealed prediction would be a claim
    # the file itself does not make.
    say()
    say("P1b  expectations carried over from new_data_v6  (NOT a v7 pre-registration)")
    say("-" * 78)
    tier_pred = preds.get("tier_effect_expected", {})
    prov = preds.get("provenance")
    if prov:
        say("  provenance: " + prov)
        say()
    if not tier_pred:
        say("  (summary file carries no predictions block)")
    else:
        say(f'    {"property":24s} {"observed":>10s}  prediction')
        for prop in PROP_ORDER:
            text = tier_pred.get(prop)
            if text is None:
                continue
            cells = matrix[prop]
            if all(c is not None for c in cells):
                drop = 100 * (cells[0] - cells[2])
                obs = f"{drop:+.1f} pp"
            else:
                obs = "n/a"
            say(f'    {prop:24s} {obs:>10s}  {text[:60]}')
        note = preds.get("note")
        if note:
            say()
            say(f"  note from the dataset: {note}")

    # ------------------------------------------------ P2/P3: local vs global
    say()
    say("P2 / P3  local (readable) vs global (computed)")
    say("-" * 78)
    say("  The dataset predicts: " + str(preds.get("local_vs_global", "n/a")))
    say("  PDF 6.3 treats this gap as the primary bottleneck signal; section 9")
    say("  calls a gap under 15 pp 'smaller than expected'.")
    say()
    say(f'    {"tier":>8s} {"local":>8s} {"global":>8s} {"gap (pp)":>9s} '
        f'{"p":>7s} {"p_Holm":>8s}')
    lg_tests = []
    for t in TIERS:
        eff, p = bootstrap_diff(sub(tier=t, local=True), sub(tier=t, local=False))
        lg_tests.append((t, p))
    h23 = holm(lg_tests)
    for t in TIERS:
        L, G = sub(tier=t, local=True), sub(tier=t, local=False)
        eff, p = bootstrap_diff(L, G)
        _, adj, rej = h23[t]
        say(f'    {t:>8s} {pct(acc(L)):>8s} {pct(acc(G)):>8s} '
            f'{("n/a" if eff is None else f"{eff:+8.1f}"):>9s} '
            f'{("n/a" if p is None else f"{p:.4f}"):>7s} '
            f'{("n/a" if adj is None else f"{adj:.4f}"):>8s}')
    overall_gap = (None if acc(sub(local=True)) is None or acc(sub(local=False)) is None
                   else acc(sub(local=True)) - acc(sub(local=False)))
    say(f'    {"ALL":>8s} {pct(acc(sub(local=True))):>8s} '
        f'{pct(acc(sub(local=False))):>8s} '
        f'{("n/a" if overall_gap is None else f"{100*overall_gap:+8.1f}"):>9s}')

    # -------------------------------------------------- P4: centroid by band
    say()
    say("P4  centroid by offset band  (this dataset's shortcut control)")
    say("-" * 78)
    say("  Band = the quartile of the centroid's distance from the bbox centre,")
    say("  in bbox-diagonal units. In band 0 the bbox centre IS the centroid to")
    say("  within 2%, so answering the bbox centre scores. In band 3 it is more")
    say("  than 7% away and the centroid has to be computed. Band 3 is primary.")
    say("  Bands are held at 26/26/25/23 per tier, so a tier difference in")
    say("  centroid accuracy cannot come from the shortcut getting easier.")
    say()
    say(f'    {"band":>6s} {"offset":>12s} {"n":>5s} {"acc":>8s} {"95% CI":>18s}')
    band_acc = {}
    for b in BANDS:
        rs = sub(prop="centroid", band=b)
        band_acc[b] = acc(rs)
        lo, hi = ci(rs)
        say(f'    {b:>6d} {BAND_EDGES[b]:>12s} {len(rs):5d} {pct(acc(rs)):>8s} '
            f'{f"[{pct(lo)}, {pct(hi)}]":>18s}')
    eff, p = bootstrap_diff(sub(prop="centroid", band=0),
                            sub(prop="centroid", band=3))
    say(f'  free ride band 0 - band 3 = '
        f'{("n/a" if eff is None else f"{eff:+.1f} pp")}'
        f'   p = {("n/a" if p is None else f"{p:.4f}")}')
    say("  A large positive gap means most of the centroid score was the")
    say("  bbox-centre shortcut rather than a computed centroid.")
    say()
    say("  band 3 only, by tier (the honest centroid number):")
    say(f'    {"tier":>8s} {"n":>5s} {"acc":>8s} {"95% CI":>18s}')
    for t in TIERS:
        rs = sub(prop="centroid", tier=t, band=3)
        lo, hi = ci(rs)
        say(f'    {t:>8s} {len(rs):5d} {pct(acc(rs)):>8s} '
            f'{f"[{pct(lo)}, {pct(hi)}]":>18s}')
    say("  n is ~8 per tier in band 3, so these three points carry very wide")
    say("  intervals and a swing between them is not by itself an effect.")

    say()
    say("  the two centroid shortcuts, as published by the dataset:")
    say(f'    {"tier":>8s} {"bbox centre":>12s} {"coord mean":>11s} {"model":>8s}')
    for t in TIERS:
        fl = floors_for(base, "centroid", t)
        say(f'    {t:>8s} {pct(fl.get("centroid_bbox_center")):>12s} '
            f'{pct(fl.get("centroid_vertex_mean")):>11s} '
            f'{pct(acc(sub(prop="centroid", tier=t))):>8s}')
    say("  new_data_v7 publishes both; a model has only to find whichever works")
    say("  better, so the bar it must clear is the larger of the two.")
    say()
    say("  READ THIS. In new_data_v7 the COORD-MEAN shortcut is the larger one,")
    say("  and unlike every other balanced quantity in this dataset it RISES")
    say("  with tier (0.62 / 0.84 / 0.88 at 5%, against 0.35 / 0.29 / 0.37 in")
    say("  new_data_v6). v7's samplers space vertices evenly round the outline,")
    say("  and the mean of an evenly spaced ring converges on the true centroid")
    say("  as the count grows - so this shortcut gets BETTER exactly where the")
    say("  task is meant to get harder. offset_band does not catch it: that")
    say("  balances the OTHER shortcut, distance from the bbox centre.")
    say("  Any hard-tier centroid accuracy below the coord-mean rate is a")
    say("  model underperforming an answer that never read the polygon.")
    for t in TIERS:
        fl = floors_for(base, "centroid", t)
        vm = fl.get("centroid_vertex_mean")
        a = acc(sub(prop="centroid", tier=t))
        if vm is not None and a is not None:
            verdict = "ABOVE" if a > vm else "BELOW"
            say(f"    {t:>8s}: model {pct(a)}% vs coord-mean {pct(vm)}%  -> {verdict}")

    # ------------------------------------------------ P4b: fill bands / area
    say()
    say("P4b  area and perimeter by fill band  (the v6-run artefact this fixes)")
    say("-" * 78)
    say("  fill_band is the quartile of how much of its bounding box the polygon")
    say("  fills (edges 0.52 / 0.62 / 0.70). It is held at 14/37/36/13 in every")
    say("  tier. In the previous run one model answered roughly the bbox area on")
    say("  simple polygons and roughly the inscribed-ellipse area on hard ones,")
    say("  and round many-vertex polygons made the ellipse guess work - which")
    say("  showed up as a tier effect it was not. Reading area across fill bands")
    say("  separates the two.")
    say()
    say(f'    {"property":24s} ' +
        " ".join(f'{"band "+str(b):>10s}' for b in BANDS) + f' {"n/band":>8s}')
    fill_table = {}
    for prop in ("area", "perimeter", "aspect_ratio"):
        cells, ns = [], []
        for b in BANDS:
            rs = sub(prop=prop, fill=b)
            fill_table[(prop, b)] = acc(rs)
            cells.append(pct(acc(rs)))
            ns.append(len(rs))
        say(f'    {prop:24s} ' + " ".join(f'{c:>10s}' for c in cells) +
            f' {str(ns[0]):>8s}')
    eff, pv = bootstrap_diff(sub(prop="area", fill=3), sub(prop="area", fill=0))
    say(f'  area: fullest band 3 - emptiest band 0 = '
        f'{("n/a" if eff is None else f"{eff:+.1f} pp")}'
        f'   p = {("n/a" if pv is None else f"{pv:.4f}")}')
    say("  A large positive gap means the bbox-area or ellipse-area guess is")
    say("  doing the work on the polygons that happen to fill their box.")

    # ------------------------------------- P5: accuracy against the floor
    say()
    say("P5  accuracy against the answer-without-reading floor")
    say("-" * 78)
    say("  new_data_v7 publishes, per tier, the rate at which each fixed")
    say("  non-answer scores. Where a property has more than one - the bbox")
    say("  centre and the coordinate mean for centroid, the bbox area and the")
    say("  inscribed ellipse for area - the floor below is the LARGER, because")
    say("  a model only has to find whichever works. Not exceeding it is NO")
    say("  EVIDENCE OF COMPUTATION, which is a different statement from a low")
    say("  score.")
    say()
    say("  It publishes NO floor for bbox or edge_length_variance. Those are")
    say("  listed below without one rather than against an invented number.")
    say()
    say("  the published shortcuts, at 5%, by tier:")
    seen = set()
    for prop in PROP_ORDER:
        for t in TIERS:
            for k, v in floors_for(base, prop, t).items():
                seen.add(k)
    say(f'    {"shortcut":28s} ' + " ".join(f"{t:>8s}" for t in TIERS))
    for k in sorted(seen):
        row = []
        for t in TIERS:
            hit = [v for prop in PROP_ORDER
                   for kk, v in floors_for(base, prop, t).items() if kk == k]
            row.append(pct(hit[0]) if hit else "  n/a")
        say(f'    {k:28s} ' + " ".join(f"{c:>8s}" for c in row))
    say()
    say(f'    {"property":24s} {"tier":>7s} {"acc":>7s} {"floor":>7s} '
        f'{"margin":>8s} {"p":>7s} {"p_Holm":>8s}')
    floor_tests, below, no_floor = [], [], []
    for prop in PROP_ORDER:
        for t in TIERS:
            f = floor_for(base, prop, t)
            if f is None:
                continue
            _, p = bootstrap_vs_floor(sub(prop=prop, tier=t), f)
            floor_tests.append((f"{prop}/{t}", p))
    hf = holm(floor_tests)
    for prop in PROP_ORDER:
        f0 = floor_for(base, prop, TIERS[0])
        if f0 is None:
            no_floor.append(prop)
            for t in TIERS:
                a = acc(sub(prop=prop, tier=t))
                say(f'    {prop:24s} {t:>7s} {pct(a):>7s} {"none":>7s} '
                    f'{"n/a":>8s} {"n/a":>7s} {"n/a":>8s}')
            continue
        for t in TIERS:
            a = acc(sub(prop=prop, tier=t))
            f = floor_for(base, prop, t)
            eff, _ = bootstrap_vs_floor(sub(prop=prop, tier=t), f)
            p, adj, rej = hf[f"{prop}/{t}"]
            mark = "" if (rej and a is not None and a > f) else "   <- no evidence"
            if a is not None and not (rej and a > f):
                below.append(f"{prop}/{t}")
            say(f'    {prop:24s} {t:>7s} {pct(a):>7s} {pct(f):>7s} '
                f'{("n/a" if eff is None else f"{eff:+8.1f}"):>8s} '
                f'{("n/a" if p is None else f"{p:.4f}"):>7s} '
                f'{("n/a" if adj is None else f"{adj:.4f}"):>8s}{mark}')
    say()
    say(f"  not shown to exceed the floor in {len(below)}/{len(floor_tests)} "
        f"(property, tier) cells with a published floor")
    say(f"  no floor published for: {', '.join(no_floor) if no_floor else '(none)'}")
    say()
    say("  baseline-subtracted accuracy, pooled over tiers "
        "(one of the four metrics the dataset asks for by name):")
    say(f'    {"property":24s} {"acc":>7s} {"floor":>7s} {"acc - floor":>12s}')
    adjusted = {}
    for prop in PROP_ORDER:
        a = acc(sub(prop=prop))
        fs = [floor_for(base, prop, t) for t in TIERS]
        f = float(np.mean(fs)) if all(v is not None for v in fs) else None
        adjusted[prop] = None if (a is None or f is None) else a - f
        say(f'    {prop:24s} {pct(a):>7s} '
            f'{("none" if f is None else pct(f)):>7s} '
            f'{("n/a" if adjusted[prop] is None else f"{100*adjusted[prop]:+12.1f}"):>12s}')

    # --------------------------------- P6: vertex_count off-by-one
    say()
    say("P6  vertex_count: exact vs off-by-one")
    say("-" * 78)
    say("  The dataset's metrics block asks for this by name. A model that")
    say("  miscounts by one has parsed the ring and lost the closing-vertex")
    say("  convention; a model that is far off has not counted at all. Those")
    say("  are different failures and one pass rate hides the difference.")
    say()
    say(f'    {"tier":>8s} {"n":>5s} {"exact":>8s} {"off-by-1":>9s} '
        f'{"within 1":>9s} {"|err| med":>10s}')
    vc_rows = {}
    for t in TIERS + ["ALL"]:
        rs = sub(prop="vertex_count") if t == "ALL" else sub(prop="vertex_count", tier=t)
        exact = off1 = 0
        errs = []
        for r in rs:
            gt, pa = r.get("ground_truth"), r.get("parsed_answer")
            if not isinstance(pa, (int, float)) or isinstance(pa, bool):
                continue
            d = abs(float(pa) - float(gt))
            errs.append(d)
            if d == 0:
                exact += 1
            elif d == 1:
                off1 += 1
        n = len(rs)
        row = {"n": n,
               "exact": exact / n if n else None,
               "off_by_one": off1 / n if n else None,
               "within_one": (exact + off1) / n if n else None,
               "median_abs_err": float(np.median(errs)) if errs else None}
        vc_rows[t] = row
        say(f'    {t:>8s} {n:5d} {pct(row["exact"]):>8s} '
            f'{pct(row["off_by_one"]):>9s} {pct(row["within_one"]):>9s} '
            f'{("n/a" if row["median_abs_err"] is None else f"{row['median_abs_err']:.1f}"):>10s}')

    # --------------------------- PDF 6.3.1 + dataset metrics: all tolerances
    say()
    say("PDF 6.3(1)  numeric accuracy at every tolerance level")
    say("-" * 78)
    say("  Strict 1% = correct computation, Moderate 5% = approximately correct,")
    say("  Lenient 10% = coarse estimation (Table 13). 25% is the extra level")
    say("  this dataset's metrics block asks for; it has no stored flag and is")
    say("  recomputed from relative_error, so it is n/a where that is missing.")
    say("  The headline numbers above are the 5% column.")
    say()
    say(f'    {"property":24s} ' +
        " ".join(f'{lab+" "+t[:3]:>11s}' for _, lab in TOLERANCES for t in TIERS))
    tol_table = {}
    for prop in sorted(NUMERIC):
        row = []
        for key, _lab in TOLERANCES:
            for t in TIERS:
                rs = sub(prop=prop, tier=t)
                bits = [tol_bit(r, key) for r in rs]
                bits = [b for b in bits if b is not None]
                v = (sum(bits) / len(bits)) if bits else None
                tol_table[(prop, key, t)] = v
                row.append(pct(v))
        say(f'    {prop:24s} ' + " ".join(f'{v:>11s}' for v in row))

    # --------------------------------- PDF 6.3.4: median and p90 relative error
    say()
    say("PDF 6.3(4)  relative error, median and 90th percentile")
    say("-" * 78)
    say("  Accuracy is a threshold on this distribution; the quantiles say how")
    say("  wrong the wrong answers are, which a pass rate cannot. The median is")
    say("  the second metric the dataset's block asks for by name.")
    say()
    say(f'    {"property":24s} ' +
        " ".join(f'{t+" med":>11s} {t+" p90":>11s}' for t in TIERS))
    err_table = {}
    for prop in sorted(NUMERIC):
        cells = []
        for t in TIERS:
            e = [r["relative_error"] for r in sub(prop=prop, tier=t)
                 if isinstance(r.get("relative_error"), (int, float))
                 and math.isfinite(r["relative_error"])]
            med = float(np.median(e)) if e else None
            p90 = float(np.percentile(e, 90)) if e else None
            err_table[(prop, t)] = {"median": med, "p90": p90}
            cells += [f"{med:.4f}" if med is not None else "n/a",
                      f"{p90:.4f}" if p90 is not None else "n/a"]
        say(f'    {prop:24s} ' + " ".join(f'{c:>11s}' for c in cells))

    # ------------------- PDF 6.2 / 6.3.5: boolean + categorical confusion matrices
    say()
    say("PDF 6.2 / 6.3(5)  boolean and categorical properties")
    say("-" * 78)
    conf = {}
    for prop in BOOLEAN:
        rs = sub(prop=prop)
        tp = sum(1 for r in rs if r["ground_truth"] is True and r.get("parsed_answer") is True)
        fp = sum(1 for r in rs if r["ground_truth"] is False and r.get("parsed_answer") is True)
        fn = sum(1 for r in rs if r["ground_truth"] is True and r.get("parsed_answer") is not True)
        tn = sum(1 for r in rs if r["ground_truth"] is False and r.get("parsed_answer") is not True)
        base_rate = sum(1 for r in rs if r["ground_truth"] is True) / len(rs) if rs else None
        prec = tp / (tp + fp) if (tp + fp) else None
        recl = tp / (tp + fn) if (tp + fn) else None
        conf[prop] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "base_rate": base_rate,
                      "precision": prec, "recall": recl, "accuracy": acc(rs)}
        say(f"  {prop}   base rate (fraction True) = {pct(base_rate)}%   "
            f"accuracy = {pct(acc(rs))}%")
        say(f'      {"":14s} {"pred True":>10s} {"pred False":>11s}')
        say(f'      {"true True":14s} {tp:>10d} {fn:>11d}')
        say(f'      {"true False":14s} {fp:>10d} {tn:>11d}')
        say(f"      precision = {pct(prec)}%   recall = {pct(recl)}%")
        say("      A model answering one constant scores the base rate. Precision")
        say("      and recall separate 'always False' from a real decision.")
    for prop in CATEGORICAL:
        rs = sub(prop=prop)
        cells = Counter((r["ground_truth"], r.get("parsed_answer")) for r in rs)
        labels = sorted({r["ground_truth"] for r in rs})
        predl = sorted({str(r.get("parsed_answer")) for r in rs})
        conf[prop] = {f"{a}->{b}": n for (a, b), n in cells.items()}
        say()
        say(f"  {prop}   accuracy = {pct(acc(rs))}%")
        say(f'      {"true \\ pred":14s} ' + " ".join(f"{p:>10s}" for p in predl))
        for a in labels:
            say(f'      {str(a):14s} ' +
                " ".join(f'{cells.get((a, None if p == "None" else p), 0):>10d}'
                         for p in predl))

    # ------------------------------------------ P4c: generator families
    # Only interpretable because new_data_v7 fixes every family at exactly 25
    # per tier. In new_data_v6 the convex half was 27/9/4 `convex` against
    # 23/41/46 `convex_sharp` across tiers, so a per-family accuracy there was
    # confounded with tier and this section could not have been written.
    say()
    say("P4c  accuracy by generator family  (25 per tier each, by construction)")
    say("-" * 78)
    fams = sorted({(r.get("family") or r.get("generator")) for r in recs
                   if (r.get("family") or r.get("generator")) is not None})
    family_table = {}
    if not fams:
        say("  no family/generator field on these records - skipped")
    else:
        say(f'    {"family":16s} ' + " ".join(f"{t:>10s}" for t in TIERS) +
            f' {"overall":>10s}')
        for fam in fams:
            cells = []
            for t in TIERS:
                a = acc(sub(family=fam, tier=t))
                family_table[(fam, t)] = a
                cells.append(pct(a))
            ov = acc(sub(family=fam))
            family_table[(fam, "all")] = ov
            say(f'    {fam:16s} ' + " ".join(f"{c:>10s}" for c in cells) +
                f' {pct(ov):>10s}')
        say("  A spread ACROSS families at one tier is a shape-difficulty")
        say("  effect, not a serialization effect - the families differ in")
        say("  outline, not in how they are written down. A family that moves")
        say("  differently across tiers than its siblings is the interesting")
        say("  case, and is what an equal quota makes visible.")

    # ---------------------------------------------------------------- figures
    make_figures(label, out_dir, sub, base, matrix, band_acc, vc_rows,
                 fill_table, family_table, fams, say)

    # ---------------------------------------------------------------- outputs
    payload = {
        "model": label,
        "folder": folder,
        # Inherited bug, fixed here: eval_v6 hard-coded "new_data_v5" and
        # eval_v7 copied it forward, so every committed v7 accuracy_matrix.json
        # names the dataset one version behind the one it actually evaluated.
        # Read off DATASET so it cannot drift again.
        "dataset": DATASET.parent.name,
        "records": len(recs),
        "complete": len(recs) == n_expected,
        "n_expected": n_expected,
        "coverage": "20% stratified subsample" if n_expected != EXPECTED_RECORDS else "full",
        "parse_failures": len(pfail),
        "truncated": len(trunc),
        "max_tokens": {str(k): v for k, v in budgets.items()},
        "accuracy_by_tier_property": {
            prop: dict(zip(TIERS, matrix[prop])) for prop in PROP_ORDER},
        "overall": acc(recs),
        "local": acc(sub(local=True)),
        "global": acc(sub(local=False)),
        "local_global_gap": overall_gap,
        "centroid_by_offset_band": {str(b): band_acc[b] for b in BANDS},
        "by_fill_band": {f"{p}|{b}": v for (p, b), v in fill_table.items()},
        "by_family": {f"{f}|{t}": v for (f, t), v in family_table.items()},
        "floors_at_5pct": {prop: {t: floors_for(base, prop, t) for t in TIERS}
                           for prop in PROP_ORDER},
        "centroid_band3_by_tier": {
            t: acc(sub(prop="centroid", tier=t, band=3)) for t in TIERS},
        "baseline_subtracted": adjusted,
        "no_floor_published": no_floor,
        "not_above_floor": below,
        "vertex_count_offby": vc_rows,
        "tolerance_levels": {f"{p}|{k}|{t}": v for (p, k, t), v in tol_table.items()},
        "relative_error_quantiles": {f"{p}|{t}": v for (p, t), v in err_table.items()},
        "confusion": conf,
    }
    (out_dir / "accuracy_matrix.json").write_text(json.dumps(payload, indent=2))
    (out_dir / "console_output.txt").write_text("\n".join(lines) + "\n")
    say()
    say(f"wrote {out_dir / 'accuracy_matrix.json'}")
    say(f"wrote {out_dir / 'console_output.txt'}")
    return payload


# =============================================================================
# Figures
#
# No panel is split by encoding, because this dataset has only one. eval_v5.py
# split every figure in two for new_data_v4, where the 5% band forgave ~1
# trailing digit under encoding A and ~4 under B; pooling them there averaged
# over a three-order-of-magnitude difference in what counted as correct. Here
# the format is constant and a split would be two copies of one picture.
# =============================================================================

def make_figures(label, out_dir, sub, base, matrix, band_acc, vc_rows,
                 fill_table, family_table, fams, say):
    say()
    say("FIGURES")
    say("-" * 78)
    saved = []

    def done(fig, name):
        path = out_dir / name
        fig.savefig(path, dpi=DPI, bbox_inches="tight")
        plt.close(fig)
        saved.append(name)
        say(f"  wrote {name}")

    n_local = sum(1 for p in PROP_ORDER if p in LOCAL)
    x = np.arange(len(PROP_ORDER))

    # ---- FIG 1: accuracy by property x tier -------------------------------
    fig, ax = plt.subplots(figsize=(14, 6.0))
    w = 0.26
    for i, t in enumerate(TIERS):
        vals = [100 * (matrix[p][i] or 0) for p in PROP_ORDER]
        ax.bar(x + (i - 1) * w, vals, w, label=t, color=TIER_COLOR[t], zorder=3)
    for xi, prop in enumerate(PROP_ORDER):
        for i, t in enumerate(TIERS):
            f = floor_for(base, prop, t)
            if f is None:
                continue
            ax.plot([xi + (i - 1) * w - w / 2, xi + (i - 1) * w + w / 2],
                    [100 * f] * 2, color=FLOOR_COLOR, lw=1.6, zorder=5)
    ax.axvspan(-0.5, n_local - 0.5, color="#3A86FF", alpha=0.09, zorder=1)
    ax.axhline(50, color="gray", ls="--", lw=0.8, zorder=2)
    ax.set_ylim(0, 126)          # headroom for the legend and the annotations
    ax.set_yticks([0, 20, 40, 60, 80, 100])
    ax.set_ylabel("accuracy %")
    ax.set_xticks(x)
    ax.set_xticklabels([p.replace("_", "\n") for p in PROP_ORDER], fontsize=9)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    # The local/global split and the 50% mark are labelled in the plot rather
    # than only in the legend, so the panel still reads on its own when it is
    # lifted into a slide or a paper away from its caption. The earlier eval/
    # figure did this and dropping it made the same chart harder to read.
    ax.text((n_local - 1) / 2, 108, "<- local", color="#3A86FF", fontsize=10,
            style="italic", ha="center", zorder=6)
    ax.text(n_local + 1.2, 108, "global ->", color="gray",
            fontsize=10, style="italic", ha="center", zorder=6)
    ax.text(len(PROP_ORDER) - 0.45, 51, "50%", color="gray", fontsize=9,
            ha="right", va="bottom", zorder=6)
    # One legend, on the right, the way the earlier eval/ figure had it. Two
    # legends put the second one on top of the tallest bars and the "<- local"
    # annotation, which is worse than the crowding it was meant to fix.
    handles = [mpatches.Patch(color=TIER_COLOR[t], label=t) for t in TIERS]
    handles += [mpatches.Patch(color="#DCE9FB", label="local properties"),
                plt.Line2D([], [], color=FLOOR_COLOR, lw=1.6,
                           label="answer-without-reading floor")]
    ax.legend(handles=handles, title="tier", fontsize=9, title_fontsize=9,
              loc="upper right", framealpha=0.95, edgecolor="lightgrey")
    fig.suptitle(
        f"Property-recovery accuracy by tier - {label}  "
        f"(v8 / {DATASET.parent.name})\n"
        "Blue shading = local properties (read off text);  no shading = global "
        "(must compute)\n"
        "Numeric properties graded @5% tolerance; boolean / categorical = exact "
        "match.  Red rule = answer-without-reading floor\n"
        "(area, perimeter and edge_length_variance have no published floor, so "
        "they carry no rule)",
        fontsize=11, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.86])
    done(fig, "fig1_accuracy_by_property.png")

    # ---- FIG 2: tier trend for the computed globals -----------------------
    computed = ["area", "perimeter", "centroid", "aspect_ratio",
                "edge_length_variance"]
    fig, axes = plt.subplots(1, len(computed), figsize=(18, 4.2), sharey=True)
    for ax, prop in zip(axes, computed):
        ys = [100 * (matrix[prop][i] or 0) for i in range(3)]
        ax.plot([0, 1, 2], ys, color="#0077B6", lw=2.2, marker="o",
                markersize=7, zorder=3)
        fl = [floor_for(base, prop, t) for t in TIERS]
        if all(v is not None for v in fl):
            ax.plot([0, 1, 2], [100 * v for v in fl], color=FLOOR_COLOR,
                    lw=1.4, ls=":", zorder=2, label="floor")
            ax.legend(fontsize=8, loc="upper right")
        ax.set_xticks([0, 1, 2])
        ax.set_xticklabels(TIERS, fontsize=9)
        ax.set_title(prop.replace("_", " "), fontsize=10)
        ax.set_ylim(0, 100)
        ax.grid(alpha=0.25, zorder=0)
    axes[0].set_ylabel("accuracy %")
    fig.suptitle(
        f"Computed global properties vs tier - {label}   (new_data_v7)\n"
        "Tier is vertex count alone; the coordinate format is identical in all "
        "three, so a slope here cannot be a grading-band artefact.",
        fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.86])
    done(fig, "fig2_accuracy_vs_tier.png")

    # ---- FIG 3: margin over the floor -------------------------------------
    props = [p for p in PROP_ORDER if floor_for(base, p, TIERS[0]) is not None]
    fig, ax = plt.subplots(figsize=(12, 5.0))
    xs = np.arange(len(props))
    w = 0.26
    for i, t in enumerate(TIERS):
        vals = []
        for prop in props:
            a = acc(sub(prop=prop, tier=t))
            f = floor_for(base, prop, t)
            vals.append(0.0 if (a is None or f is None) else 100 * (a - f))
        ax.bar(xs + (i - 1) * w, vals, w, label=t, color=TIER_COLOR[t], zorder=3)
    ax.axhline(0, color="black", lw=1.2, zorder=4)
    ax.set_xticks(xs)
    ax.set_xticklabels([p.replace("_", "\n") for p in props], fontsize=9)
    ax.set_ylabel("accuracy - floor  (pp)")
    ax.grid(axis="y", alpha=0.25, zorder=0)
    ax.legend(fontsize=9)
    fig.suptitle(
        f"Baseline-subtracted accuracy - {label}   (new_data_v7)\n"
        "Zero means the model did no better than a fixed non-answer. Only the "
        "five properties this dataset publishes a floor for can appear.",
        fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.87])
    done(fig, "fig3_margin_over_floor.png")

    # ---- FIG 4: centroid by offset band -----------------------------------
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))
    accs = [band_acc[b] for b in BANDS]
    cis = [ci(sub(prop="centroid", band=b)) for b in BANDS]
    err = [[100 * ((a or 0) - (lo or 0)) for a, (lo, hi) in zip(accs, cis)],
           [100 * ((hi or 0) - (a or 0)) for a, (lo, hi) in zip(accs, cis)]]
    axes[0].bar(BANDS, [100 * (a or 0) for a in accs], 0.6, yerr=err,
                color=[BAND_COLOR[b] for b in BANDS], capsize=4, zorder=3)
    axes[0].set_xticks(BANDS)
    axes[0].set_xticklabels(
        [f"band {b}\n{BAND_EDGES[b]}" for b in BANDS], fontsize=9)
    axes[0].set_ylabel("centroid accuracy %")
    axes[0].set_ylim(0, 100)
    axes[0].grid(axis="y", alpha=0.25, zorder=0)
    axes[0].set_title("band 3 is the subset that requires computing", fontsize=10)
    cells = [sub(prop="centroid", tier=t, band=3) for t in TIERS]
    ns = [len(c) for c in cells]
    lo_hi = [ci(c) for c in cells]
    ys = [100 * (acc(c) or 0) for c in cells]
    yerr = [[y - 100 * (lo or 0) for y, (lo, hi) in zip(ys, lo_hi)],
            [100 * (hi or 0) - y for y, (lo, hi) in zip(ys, lo_hi)]]
    axes[1].errorbar([0, 1, 2], ys, yerr=yerr, color="#0077B6", lw=2.0,
                     marker="o", markersize=7, capsize=4)
    axes[1].set_xticks([0, 1, 2])
    # ~23 records per point. Printing n on the axis keeps a swing between two
    # points from being read as an effect.
    axes[1].set_xticklabels([f"{t}\nn={n}" for t, n in zip(TIERS, ns)])
    # Both published shortcuts, so the reader can see which bar the model is
    # actually failing to beat rather than only that it failed to beat one.
    for key, style, name in (("centroid_bbox_center", "--", "bbox centre"),
                             ("centroid_vertex_mean", ":", "coord mean")):
        ys2 = [100 * floors_for(base, "centroid", t).get(key, 0) for t in TIERS]
        axes[1].plot([0, 1, 2], ys2, color=THRESHOLD_COLOR, ls=style, lw=1.4,
                     label=f"{name} (all bands)")
    axes[1].set_ylim(0, 100)
    axes[1].grid(alpha=0.25)
    axes[1].legend(fontsize=7, loc="upper right")
    axes[1].set_title("band 3 only, by tier (95% CI)", fontsize=10)
    fig.suptitle(
        f"Centroid: how much of the score is a shortcut - {label}\n"
        "Band 0 = the bbox centre is within 2% of the true centroid; band 3 = it "
        "is more than 7% away.\n"
        "Dashed / dotted = the rate each published non-answer scores over all "
        "bands.",
        fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.86])
    done(fig, "fig4_centroid_offset_bands.png")

    # ---- FIG 5: accuracy vs vertex count, + vertex_count off-by-one --------
    fig, axes = plt.subplots(1, 4, figsize=(19, 4.2))
    for ax, prop in zip(axes[:3], ["vertex_count", "centroid", "perimeter"]):
        rs = sub(prop=prop)
        bins = defaultdict(list)
        for r in rs:
            bins[min(40, 5 * round(r["num_vertices"] / 5))].append(correct_bit(r))
        # Bins at the extremes hold one or two records, where a single answer
        # moves the line by 50 pp. Drop them rather than draw a spike that
        # reads as an effect.
        ks = [k for k in sorted(bins) if len(bins[k]) >= MIN_BIN]
        ax.plot(ks, [100 * np.mean(bins[k]) for k in ks], color="#0077B6",
                lw=2.0, marker="o", markersize=5)
        ax.set_title(prop.replace("_", " "), fontsize=10)
        ax.set_xlabel(f"vertex count (binned by 5, n<{MIN_BIN} dropped)")
        ax.grid(alpha=0.25)
        ax.set_ylim(0, 105)
    axes[0].set_ylabel("accuracy %")
    ax = axes[3]
    xs = np.arange(len(TIERS))
    ex = [100 * (vc_rows[t]["exact"] or 0) for t in TIERS]
    o1 = [100 * (vc_rows[t]["off_by_one"] or 0) for t in TIERS]
    ax.bar(xs, ex, 0.55, label="exact", color="#4C9F70", zorder=3)
    ax.bar(xs, o1, 0.55, bottom=ex, label="off by one", color="#E1A730", zorder=3)
    ax.set_xticks(xs)
    ax.set_xticklabels(TIERS, fontsize=9)
    ax.set_ylim(0, 105)
    ax.set_title("vertex_count: exact vs off-by-one", fontsize=10)
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    fig.suptitle(
        f"Accuracy vs vertex count - {label}   (new_data_v7)\n"
        "Vertex count IS the tier axis here, so these panels are the tier effect "
        "at finer resolution. The last panel separates miscounting by one - a "
        "closing-vertex convention error - from not counting at all.",
        fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.86])
    done(fig, "fig5_accuracy_vs_vertexcount.png")

    # ---- FIG 6: relative-error distributions ------------------------------
    props6 = ["area", "perimeter", "centroid", "aspect_ratio"]
    fig, axes = plt.subplots(1, len(props6), figsize=(16, 3.8))
    for ax, prop in zip(axes, props6):
        for t in TIERS:
            errs = [r["relative_error"] for r in sub(prop=prop, tier=t)
                    if isinstance(r.get("relative_error"), (int, float))
                    and math.isfinite(r["relative_error"]) and r["relative_error"] > 0]
            if not errs:
                continue
            ax.hist(np.log10(errs), bins=25, histtype="step", lw=1.8,
                    color=TIER_COLOR[t], label=t)
        ax.axvline(math.log10(0.05), color=FLOOR_COLOR, ls="--", lw=1.2)
        ax.set_title(prop.replace("_", " "), fontsize=10)
        ax.set_xlabel("log10 relative error")
        ax.grid(alpha=0.2)
    axes[0].set_ylabel("records")
    axes[0].legend(fontsize=8)
    fig.suptitle(
        f"Relative-error distributions by tier - {label}   (new_data_v7)\n"
        "Dashed line = the 5% grading threshold. Mass to its right is graded "
        "wrong; how far right says how wrong.",
        fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.84])
    done(fig, "fig6_error_distributions.png")

    # ---- FIG 7: the fill-band axis, which is new in this dataset ----------
    # Reading area across fill bands is the whole reason fill_band exists: in
    # the previous run a model's area accuracy rose with tier only because
    # round many-vertex polygons made an ellipse-area guess work, and that reads
    # as a tier effect until you hold fill constant.
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.2), sharey=True)
    for ax, prop in zip(axes, ["area", "perimeter", "aspect_ratio"]):
        ys = [100 * (fill_table.get((prop, b)) or 0) for b in BANDS]
        ax.bar(BANDS, ys, 0.6, color="#0077B6", zorder=3)
        fl = [floor_for(base, prop, t) for t in TIERS]
        if all(v is not None for v in fl):
            f = 100 * float(np.mean(fl))
            ax.axhline(f, color=FLOOR_COLOR, ls="--", lw=1.3, zorder=4,
                       label="best published non-answer")
            ax.legend(fontsize=7, loc="upper left")
        ax.set_xticks(BANDS)
        ax.set_xticklabels([f"band {b}\n{FILL_BAND_EDGES[b]}" for b in BANDS],
                           fontsize=8)
        ax.set_title(prop.replace("_", " "), fontsize=10)
        ax.grid(axis="y", alpha=0.25, zorder=0)
    axes[0].set_ylabel("accuracy %")
    axes[0].set_ylim(0, max(30, axes[0].get_ylim()[1]))
    fig.suptitle(
        f"Accuracy by fill band - {label}   (new_data_v7)\n"
        "fill_band = the quartile of how much of its bounding box the polygon "
        "fills, held at 14/37/36/13 in every tier.\n"
        "A rise to the right means a bbox-area or ellipse-area guess is doing "
        "the work, not a computed area.",
        fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.84])
    done(fig, "fig7_accuracy_by_fill_band.png")

    # ---- FIG 8: the generator-family axis, new in this dataset -------------
    # new_data_v7 fixes all four families at exactly 25 per tier. That equal
    # quota is what makes this figure readable: in new_data_v6 the convex half
    # ran 27/9/4 `convex` against 23/41/46 `convex_sharp` across tiers, so a
    # bar here would have mixed "this shape is harder" with "this tier happens
    # to contain more of this shape".
    if fams:
        fig, ax = plt.subplots(figsize=(13, 5.6))
        w = 0.8 / len(TIERS)
        xf = np.arange(len(fams))
        for k, t in enumerate(TIERS):
            ys = [100 * (family_table.get((f, t)) or 0) for f in fams]
            ax.bar(xf + k * w - 0.4 + w / 2, ys, w, label=t,
                   color=TIER_COLOR[t], zorder=3)
        ax.set_xticks(xf)
        ax.set_xticklabels([f.replace("_", "\n") for f in fams], fontsize=9)
        ax.set_ylabel("accuracy % (all 9 properties pooled)")
        ax.grid(axis="y", alpha=0.25, zorder=0)
        ax.legend(title="tier", fontsize=8)
        fig.suptitle(
            f"Accuracy by generator family - {label}   (new_data_v7)\n"
            "Each family is exactly 25 of every tier's 100 polygons,\n"
            "so a gap between bars is a difference between SHAPES, not "
            "between serializations.",
            fontsize=11, fontweight="bold")
        fig.tight_layout(rect=[0, 0, 1, 0.90])
        done(fig, "fig8_accuracy_by_family.png")

    # ---- FIG 9: the centroid shortcut that gets easier with tier -----------
    # Published floors against measured accuracy. This figure exists because
    # new_data_v7 has a leak new_data_v6 did not: the coordinate-mean answer
    # for centroid rises steeply with tier, so the honest bar for hard-tier
    # centroid is far above the 50% a reader might assume.
    fl_names = ["centroid_bbox_center", "centroid_vertex_mean"]
    fl_series = {k: [floors_for(base, "centroid", t).get(k) for t in TIERS]
                 for k in fl_names}
    if any(all(v is not None for v in vs) for vs in fl_series.values()):
        fig, ax = plt.subplots(figsize=(11, 5.6))
        xt = np.arange(len(TIERS))
        styles = {"centroid_bbox_center": ("--", "#C0504D", "bbox-centre guess"),
                  "centroid_vertex_mean": (":", "#7B4EA8", "coordinate-mean guess")}
        for k, vs in fl_series.items():
            if all(v is not None for v in vs):
                ls, c, lab = styles[k]
                ax.plot(xt, [100 * v for v in vs], ls, color=c, lw=2,
                        marker="o", ms=5, label=lab, zorder=3)
        model_y = [acc(sub(prop="centroid", tier=t)) for t in TIERS]
        ax.plot(xt, [100 * (v or 0) for v in model_y], "-", color="#0077B6",
                lw=2.4, marker="s", ms=7, label=f"{label} (measured)", zorder=4)
        ax.set_xticks(xt)
        ax.set_xticklabels(TIERS)
        ax.set_ylabel("centroid accuracy % at 5% of the bbox diagonal")
        ax.set_ylim(0, 100)
        ax.grid(axis="y", alpha=0.25, zorder=0)
        ax.legend(fontsize=8, loc="best")
        fig.suptitle(
            f"Centroid vs both published floors - {label}\n"
            "The coordinate-mean guess RISES with tier in new_data_v7:\n"
            "evenly spaced vertices average toward the true centroid.\n"
            "Blue below dotted = losing to an answer that never read the polygon.",
            fontsize=11, fontweight="bold")
        fig.tight_layout(rect=[0, 0, 1, 0.88])
        done(fig, "fig9_centroid_vs_floors.png")

    return saved


# =============================================================================
# Cross-model
# =============================================================================

def _spearman(a, b):
    """Spearman rho by hand - ranks, then Pearson on the ranks.

    Ties are averaged, which matters here: several properties sit at exactly
    50.0% (the majority base rate) in more than one model, and giving them
    distinct ranks would invent an ordering the data does not contain.
    """
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    ra, rb = np.asarray(rank(a)), np.asarray(rank(b))
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])


def cross_model_report(done):
    """PDF 6.3 aggregate metrics across models.

    The rank correlation answers a different question from the accuracy table:
    not how well each model does, but whether they find the SAME properties
    hard. A high rho says the difficulty ordering is a property of the task
    rather than of any one model, which is what a bottleneck claim needs.
    """
    print(f'{"model":24s} {"overall":>8s} {"local":>8s} {"global":>8s} '
          f'{"cent b3":>8s} {"L-G gap":>8s} {"floor fails":>12s}')
    for d in done:
        gap = d["local_global_gap"]
        print(f'{d["model"]:24s} {pct(d["overall"]):>8s} {pct(d["local"]):>8s} '
              f'{pct(d["global"]):>8s} '
              f'{pct(d["centroid_by_offset_band"]["3"]):>8s} '
              f'{("n/a" if gap is None else f"{100*gap:+.1f}"):>8s} '
              f'{len(d["not_above_floor"]):>12d}')
    print()
    print("  PDF 6.3 local-global gap is the primary bottleneck signal; section 9")
    print("  calls a gap under 15 pp 'smaller than expected'.")
    print("  'cent b3' is centroid accuracy in offset band 3 - the subset where")
    print("  the bbox-centre shortcut does not score.")

    print()
    print("  monotone simple >= medium >= hard, per property and model")
    print(f'    {"property":24s} ' + " ".join(f"{d['model'][:12]:>13s}" for d in done))
    for prop in PROP_ORDER:
        row = []
        for d in done:
            cells = [d["accuracy_by_tier_property"][prop][t] for t in TIERS]
            if any(c is None for c in cells):
                row.append("n/a")
            else:
                row.append("yes" if all(a >= b - 1e-12
                                        for a, b in zip(cells, cells[1:])) else "NO")
        print(f'    {prop:24s} ' + " ".join(f"{c:>13s}" for c in row))

    vecs = {}
    for d in done:
        m = d["accuracy_by_tier_property"]
        vecs[d["model"]] = [
            float(np.mean([m[p][t] for t in TIERS if m[p][t] is not None]))
            for p in PROP_ORDER]
    print()
    print("  PDF 6.3  cross-model consistency: Spearman rho of per-property accuracy")
    names = list(vecs)
    print(f'    {"":24s} ' + " ".join(f"{n[:12]:>13s}" for n in names))
    rhos = []
    for a in names:
        row = []
        for b in names:
            r = 1.0 if a == b else _spearman(vecs[a], vecs[b])
            if a != b and r is not None:
                rhos.append(r)
            row.append("   n/a" if r is None else f"{r:6.3f}")
        print(f'    {a:24s} ' + " ".join(f"{c:>13s}" for c in row))
    if rhos:
        print(f"    mean off-diagonal rho = {np.mean(rhos):.3f}  "
              f"(min {min(rhos):.3f}, max {max(rhos):.3f})")
        print("    High rho = the difficulty ordering of properties is")
        print("    model-independent, i.e. a property of the serialization, not")
        print("    of any one model's weaknesses.")


def make_cross_model_figures(done, out_dir, base):
    out_dir.mkdir(parents=True, exist_ok=True)
    labels = [d["model"] for d in done]
    x = np.arange(len(PROP_ORDER))
    n_local = sum(1 for p in PROP_ORDER if p in LOCAL)
    saved = []

    def acc_of(d, prop):
        vals = [d["accuracy_by_tier_property"][prop][t] for t in TIERS]
        vals = [v for v in vals if v is not None]
        return float(np.mean(vals)) if vals else None

    # ---- accuracy by property, all models ---------------------------------
    fig, ax = plt.subplots(figsize=(15, 6))
    w = 0.8 / len(done)
    for i, d in enumerate(done):
        vals = [100 * (acc_of(d, p) or 0) for p in PROP_ORDER]
        ax.bar(x + (i - (len(done) - 1) / 2) * w, vals, w, label=d["model"],
               color=MODEL_COLOR.get(d["model"], "#888"), zorder=3)
    for xi, prop in enumerate(PROP_ORDER):
        fs = [floor_for(base, prop, t) for t in TIERS]
        if any(v is None for v in fs):
            continue
        f = 100 * float(np.mean(fs))
        ax.plot([xi - 0.44, xi + 0.44], [f, f], color=FLOOR_COLOR, lw=1.8, zorder=6)
    ax.axvspan(-0.5, n_local - 0.5, color="#3A86FF", alpha=0.09, zorder=1)
    ax.axhline(50, color="gray", ls="--", lw=0.8, zorder=2)
    ax.set_ylim(0, 108)
    ax.set_ylabel("accuracy %")
    ax.set_xticks(x)
    ax.set_xticklabels([p.replace("_", "\n") for p in PROP_ORDER], fontsize=10)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    handles = [mpatches.Patch(color=MODEL_COLOR.get(l, "#888"), label=l)
               for l in labels]
    handles += [mpatches.Patch(color="#3A86FF", alpha=0.25, label="local (readable)"),
                plt.Line2D([], [], color=FLOOR_COLOR, lw=1.8,
                           label="answer-without-reading floor")]
    ax.legend(handles=handles, fontsize=9, loc="upper right", ncol=2,
              framealpha=0.95, edgecolor="lightgrey")
    fig.suptitle(
        "Property-recovery accuracy, all models - v8 run on new_data_v7\n"
        "Bars pooled over tiers. Red rule = the published rate for a fixed "
        "non-answer; a bar at or below it is no evidence of computation.\n"
        "Blue band = the two properties the protocol classifies as local.",
        fontsize=13, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.89])
    path = out_dir / "cross_model_accuracy_by_property.png"
    fig.savefig(path, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    saved.append(path)

    # ---- complexity gradient: one small panel per property ----------------
    fig, axes = plt.subplots(2, 5, figsize=(20, 7), sharey=True)
    for ax, prop in zip(axes.ravel(), PROP_ORDER):
        for d in done:
            ys = [100 * (d["accuracy_by_tier_property"][prop][t] or 0) for t in TIERS]
            ax.plot([0, 1, 2], ys, color=MODEL_COLOR.get(d["model"], "#888"),
                    lw=2.0, marker="o", markersize=5, label=d["model"], zorder=3)
        fs = [floor_for(base, prop, t) for t in TIERS]
        if all(v is not None for v in fs):
            ax.plot([0, 1, 2], [100 * v for v in fs], color=FLOOR_COLOR,
                    lw=1.2, ls=":", zorder=2)
        ax.set_xticks([0, 1, 2])
        ax.set_xticklabels(TIERS, fontsize=9)
        ax.set_ylim(0, 105)
        ax.grid(alpha=0.25, zorder=0)
        ax.set_title(prop.replace("_", " ")
                     + ("  [local]" if prop in LOCAL else ""), fontsize=10)
    axes.ravel()[-1].axis("off")
    axes[0][0].set_ylabel("accuracy %")
    axes[1][0].set_ylabel("accuracy %")
    axes.ravel()[-1].legend(
        handles=[mpatches.Patch(color=MODEL_COLOR.get(l, "#888"), label=l)
                 for l in labels]
        + [plt.Line2D([], [], color=FLOOR_COLOR, lw=1.2, ls=":", label="floor")],
        fontsize=10, loc="center", frameon=False)
    fig.suptitle(
        "Complexity gradient by property, all models - v8 run on new_data_v7\n"
        "Tier is vertex count alone and the coordinate format is identical "
        "across all three, so a slope here cannot be a grading-band artefact - "
        "which is the one thing the v5 run could not rule out.",
        fontsize=13, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.89])
    path = out_dir / "cross_model_complexity_gradient.png"
    fig.savefig(path, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    saved.append(path)
    return saved


def main() -> int:
    p = argparse.ArgumentParser(description="Phase 3 evaluation for the v7 runs.")
    p.add_argument("--model", nargs="+", required=True,
                   help="Model folder(s), or 'all'.")
    p.add_argument("--skip", nargs="+", default=[], help="Folders to exclude.")
    p.add_argument("--allow-incomplete", action="store_true",
                   help=f"Evaluate a model whose run has fewer than "
                        f"its expected record count. Off by default: a partial "
                        f"run's accuracy is over whatever happened to finish, "
                        f"which is not a random subset when the failures were "
                        f"rate limits.")
    args = p.parse_args()

    if not DATASET.exists() or not SUMMARY.exists():
        sys.exit(f"dataset or summary missing under {DATASET.parent}")

    wanted = list(MODELS) if "all" in args.model else args.model
    unknown = set(wanted) - set(MODELS)
    if unknown:
        sys.exit(f"unknown model folder(s): {sorted(unknown)}")
    wanted = [m for m in wanted if m not in set(args.skip)]

    done = []
    for folder in wanted:
        path = results_path(folder)
        if not path.exists():
            print(f"skip {folder}: no results file yet")
            continue
        n = len(load_records(path))
        if n < expected_records(folder) and not args.allow_incomplete:
            print(f"skip {folder}: {n}/{expected_records(folder)} records - run not "
                  f"finished (--allow-incomplete to evaluate anyway)")
            continue
        done.append(evaluate(folder))
        print()

    if len(done) > 1:
        print("=" * 78)
        print("CROSS-MODEL SUMMARY")
        print("=" * 78)
        cross_model_report(done)
        base = json.loads(SUMMARY.read_text())["baselines"]
        for path in make_cross_model_figures(done, HERE / "cross_model", base):
            print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
