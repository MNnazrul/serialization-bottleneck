"""
Phase 3 -- evaluation for the tabular (Domain 3) benchmark.

Reads Phase 2's deduplicated JSON result exports (one file per model,
`phase2_tabular_model_results/<NN_model>/<model>_results.json`), computes every
metric in PDF Section 6, and writes a single `evaluation_report.json` plus
a printed summary. This is domain-agnostic in structure -- it only reads
the record schema Phase 2 writes -- but the interpretation (§6.2's property
categories, §9's decision rules) is specific to this tabular property set.

Usage:
    python evaluate.py                          # auto-discover all results
    python evaluate.py --results v4flash=path/to/v4flash_results.json ...
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

import numpy as np
from scipy import stats as scipy_stats

HERE = Path(__file__).resolve().parent
PHASE2_DIR = HERE.parent / "phase2_tabular_model_results"
PHASE1_DATASET_PATH = HERE.parent / "phase1_tabular_dataset" / "tabular_exp1_dataset.json"
REPORT_PATH = HERE / "evaluation_report.json"

NUMERIC_PROPERTIES = {"correlation", "skewness", "column_agg"}
INTEGER_PROPERTIES = {"row_count", "null_count", "column_count"}
BOOLEAN_PROPERTIES = {"is_monotonic", "has_outlier", "func_dependency"}
CATEGORICAL_PROPERTIES = {"column_dtype"}
CATEGORICAL_CLASSES = ["numeric", "categorical", "datetime"]

TIERS = ["simple", "medium", "hard"]
N_RESAMPLES = 10_000
BOOTSTRAP_SEED = 0

# Numeric-scoring tolerances, mirrored from harness.py. A hit counts when the
# relative error is within tolerance OR the absolute error is within that same
# tolerance treated as an absolute band -- this keeps near-zero targets (e.g.
# control-pair correlations) from being scored as always-wrong for a tiny miss.
NUMERIC_TOLERANCES = {"correct_1pct": 0.01, "correct_5pct": 0.05, "correct_10pct": 0.10}

# A model whose predicted-"true" rate on a boolean property is outside this band
# is barely varying its answer -- its per-tier accuracy just tracks the
# ground-truth base rate. Flagged, and excluded from the complexity gradient.
DEGENERATE_POSITIVE_RATE_BAND = (0.10, 0.90)

# Balanced accuracy at or below this is statistically indistinguishable from a
# coin flip / always-one-answer strategy, so the per-tier "gradient" there is
# base-rate drift regardless of the mechanism.
NEAR_CHANCE_BALANCED_ACCURACY = 0.55


# ----------------------------------------------------------------------
# Discovery / loading
# ----------------------------------------------------------------------


def discover_results(base_dir: Path) -> dict[str, Path]:
    found = {}
    if not base_dir.exists():
        return found
    for sub in sorted(base_dir.iterdir()):
        if not sub.is_dir():
            continue
        for f in sub.glob("*_results.json"):
            model_name = f.stem.replace("_results", "")
            found[model_name] = f
    return found


def _n_rows_cols_lookup() -> dict[str, tuple[int, int]]:
    if not PHASE1_DATASET_PATH.exists():
        return {}
    records = json.loads(PHASE1_DATASET_PATH.read_text())
    return {r["object_id"]: (r["n_rows"], r["n_cols"]) for r in records}


def _rescore_numeric(r: dict) -> None:
    """Re-derive a numeric record's correct_* flags in place using the
    abs-OR-rel tolerance rule (harness.py `score()`), so runs collected
    before that rule are scored the same way without re-querying. Records
    already scored this way are unchanged."""
    if r.get("property") not in NUMERIC_PROPERTIES:
        return
    gt, parsed = r.get("ground_truth"), r.get("parsed_answer")
    if not r.get("parse_success") or not isinstance(parsed, (int, float)) or isinstance(parsed, bool):
        return
    abs_err = abs(parsed - gt)
    rel_err = abs_err if abs(gt) < 1e-9 else abs_err / abs(gt)
    r["absolute_error"] = abs_err
    r["relative_error"] = rel_err
    for flag, tol in NUMERIC_TOLERANCES.items():
        r[flag] = bool(rel_err <= tol or abs_err <= tol)
    r["correct"] = r["correct_5pct"]


def load_results(paths: dict[str, Path], rescore_numeric: bool = True) -> dict[str, list[dict]]:
    """Loads each model's result records, backfilling n_rows/n_cols from
    the Phase 1 dataset by object_id for any record that predates
    harness.py including those fields directly (fixes the
    accuracy-vs-complexity scatter figure for already-collected runs
    without needing to re-query any model). Also re-scores numeric
    records with the current abs-OR-rel tolerance rule unless disabled."""
    lookup = _n_rows_cols_lookup()
    out = {}
    for model, path in paths.items():
        records = json.loads(path.read_text())
        for r in records:
            if "n_rows" not in r or "n_cols" not in r:
                hit = lookup.get(r.get("object_id"))
                if hit:
                    r["n_rows"], r["n_cols"] = hit
            if rescore_numeric:
                _rescore_numeric(r)
        out[model] = records
    return out


# ----------------------------------------------------------------------
# Bootstrap statistics -- PDF 6.4
# ----------------------------------------------------------------------


def bootstrap_ci(values: np.ndarray, n_resamples: int = N_RESAMPLES, seed: int = BOOTSTRAP_SEED) -> Optional[dict]:
    if len(values) == 0:
        return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(values), size=(n_resamples, len(values)))
    resampled_means = values[idx].mean(axis=1)
    lo, hi = np.percentile(resampled_means, [2.5, 97.5])
    return {
        "mean": float(values.mean()),
        "ci_lo_95": float(lo),
        "ci_hi_95": float(hi),
        "n": int(len(values)),
    }


def bootstrap_test(a: np.ndarray, b: np.ndarray, n_resamples: int = N_RESAMPLES, seed: int = BOOTSTRAP_SEED) -> Optional[dict]:
    """Two-sided bootstrap test for a difference in means/proportions
    between two independent samples. Returns effect size in percentage
    points (a - b) and a two-sided p-value."""
    if len(a) == 0 or len(b) == 0:
        return None
    rng = np.random.default_rng(seed)
    idx_a = rng.integers(0, len(a), size=(n_resamples, len(a)))
    idx_b = rng.integers(0, len(b), size=(n_resamples, len(b)))
    diffs = a[idx_a].mean(axis=1) - b[idx_b].mean(axis=1)
    observed = float(a.mean() - b.mean())
    p = 2 * min((diffs <= 0).mean(), (diffs >= 0).mean())
    return {
        "effect_size_pp": observed * 100,
        "p_value": float(min(p, 1.0)),
        "n_a": int(len(a)),
        "n_b": int(len(b)),
    }


# ----------------------------------------------------------------------
# Per-property, per-tier, per-locality metrics -- PDF 6.2 / 6.3
# ----------------------------------------------------------------------


def _bool_array(records: list[dict], key: str) -> np.ndarray:
    return np.array([bool(r.get(key)) for r in records], dtype=float)


def _relative_error_stats(records: list[dict]) -> Optional[dict]:
    """Median / p90 relative error -- PDF 6.3 item 4 ('overall and per tier')."""
    rel_errors = np.array(
        [r["relative_error"] for r in records if r.get("parse_success") and r.get("relative_error") is not None]
    )
    if not len(rel_errors):
        return None
    return {
        "median": float(np.median(rel_errors)),
        "p90": float(np.percentile(rel_errors, 90)),
        "n": int(len(rel_errors)),
    }


# ----------------------------------------------------------------------
# Base-rate-robust scoring for yes/no + categorical properties
# ----------------------------------------------------------------------
#
# Raw accuracy on an imbalanced boolean property mostly measures how often
# the model's fixed guess matches the ground-truth base rate -- and because
# that base rate drifts across tiers (e.g. func_dependency is 76% "yes" on
# simple, 64% on hard), a model that never changes its answer still shows a
# per-tier "gradient". Balanced accuracy (mean of the two class recalls)
# pins any single-answer strategy at 0.50 on every tier, so what's left is
# real signal. It's None when a tier/property has only one class present
# (e.g. has_outlier on simple, which has zero "yes" cases).


def balanced_accuracy_bool(records: list[dict]) -> Optional[dict]:
    parsed = [
        r for r in records
        if r.get("parse_success") and isinstance(r.get("parsed_answer"), bool)
    ]
    pos = [r for r in parsed if r["ground_truth"] is True]
    neg = [r for r in parsed if r["ground_truth"] is False]
    all_pos = [r for r in records if r["ground_truth"] is True]
    all_neg = [r for r in records if r["ground_truth"] is False]
    if not all_pos or not all_neg:
        return None  # only one class present -- not interpretable either way
    recall_pos = (sum(1 for r in pos if r["parsed_answer"] is True) / len(all_pos))
    recall_neg = (sum(1 for r in neg if r["parsed_answer"] is False) / len(all_neg))
    return {
        "balanced_accuracy": 0.5 * (recall_pos + recall_neg),
        "recall_true": recall_pos,
        "recall_false": recall_neg,
        "n_true": len(all_pos),
        "n_false": len(all_neg),
    }


def balanced_accuracy_categorical(records: list[dict]) -> Optional[dict]:
    parsed = [r for r in records if r.get("parse_success")]
    classes = sorted({r["ground_truth"] for r in records})
    if len(classes) < 2:
        return None
    per_class = {}
    for c in classes:
        gt_c = [r for r in records if r["ground_truth"] == c]
        hit = sum(1 for r in parsed if r["ground_truth"] == c and r["parsed_answer"] == c)
        per_class[c] = hit / len(gt_c) if gt_c else None
    vals = [v for v in per_class.values() if v is not None]
    return {
        "balanced_accuracy": float(np.mean(vals)) if vals else None,
        "per_class_recall": per_class,
    }


def balanced_accuracy(records: list[dict], prop: str) -> Optional[dict]:
    if prop in BOOLEAN_PROPERTIES:
        return balanced_accuracy_bool(records)
    if prop in CATEGORICAL_PROPERTIES:
        return balanced_accuracy_categorical(records)
    return None


def predicted_positive_rate(records: list[dict]) -> Optional[float]:
    parsed = [r for r in records if r.get("parse_success") and isinstance(r.get("parsed_answer"), bool)]
    if not parsed:
        return None
    return sum(1 for r in parsed if r["parsed_answer"] is True) / len(parsed)


def tier_majority_baseline(records: list[dict]) -> dict:
    """Accuracy of a model that reads nothing but knows each table's tier
    and always answers the most common ground-truth value for that
    (property, tier) cell. Any real model needs to clear this."""
    cells: dict[tuple, list] = defaultdict(list)
    for r in records:
        cells[(r["property"], r["tier"])].append(r["ground_truth"])
    majority = {k: Counter(v).most_common(1)[0][0] for k, v in cells.items()}

    def scored(subset: list[dict]) -> Optional[float]:
        if not subset:
            return None
        return float(np.mean([r["ground_truth"] == majority[(r["property"], r["tier"])] for r in subset]))

    scopes = {
        "overall": records,
        "local": [r for r in records if r["property_locality"] == "local"],
        "global": [r for r in records if r["property_locality"] == "global"],
        "boolean": [r for r in records if r["property"] in BOOLEAN_PROPERTIES],
    }
    out = {name: scored(sub) for name, sub in scopes.items()}
    out["by_property_tier"] = {
        f"{prop}/{tier}": {
            "majority_answer": majority[(prop, tier)],
            "baseline_accuracy": scored([r for r in records if r["property"] == prop and r["tier"] == tier]),
        }
        for (prop, tier) in sorted(majority)
    }
    return out


CORRELATION_DISCRIMINATION_CUT = 0.30  # |r| threshold for "the model called it correlated"


def correlation_control_breakdown(records: list[dict]) -> dict:
    """Split correlation accuracy by is_control, and measure *discrimination*
    separately from exact-value accuracy.

    Since the rebalance, control queries are rejection-sampled to a realized
    \\|r\\| < 0.20 (genuinely uncorrelated) and planted ones to \\|r\\| >= 0.30,
    so 'did the model call the control ~0 and the planted pair clearly
    non-zero?' is now a meaningful, clean signal -- independent of whether
    it nailed the exact coefficient. `is_control` is False (planted), True
    (control), or absent (the best available pair, still < 0.30 -- neither)."""
    def grp(want):
        return [r for r in records if r.get("is_control") is want]

    def called_correlated_rate(subset):
        vals = [
            abs(r["parsed_answer"]) for r in subset
            if r.get("parse_success") and isinstance(r.get("parsed_answer"), (int, float))
            and not isinstance(r.get("parsed_answer"), bool)
        ]
        if not vals:
            return None
        return float(np.mean([v >= CORRELATION_DISCRIMINATION_CUT for v in vals]))

    planted, control, neither = grp(False), grp(True), grp(None)
    return {
        "planted": {
            "n": len(planted),
            "accuracy_5pct": bootstrap_ci(_bool_array(planted, "correct")),
            "called_correlated_rate": called_correlated_rate(planted),  # want high
        },
        "control": {
            "n": len(control),
            "accuracy_5pct": bootstrap_ci(_bool_array(control, "correct")),
            "called_correlated_rate": called_correlated_rate(control),  # want low
        },
        "neither": {"n": len(neither), "accuracy_5pct": bootstrap_ci(_bool_array(neither, "correct"))},
        "discrimination_gap": (
            (cr_p - cr_c)
            if (cr_p := called_correlated_rate(planted)) is not None
            and (cr_c := called_correlated_rate(control)) is not None
            else None
        ),
    }


def property_metrics(records: list[dict], prop: str) -> dict:
    result: dict[str, Any] = {"n": len(records)}
    correct = _bool_array(records, "correct")
    result["accuracy"] = bootstrap_ci(correct)

    parsed_flags = _bool_array(records, "parse_success")
    result["parse_success_rate"] = float(parsed_flags.mean()) if len(records) else None
    trunc = np.array([r.get("failure_type") == "reasoning_truncated" for r in records], dtype=float)
    result["reasoning_truncation_rate"] = float(trunc.mean()) if len(records) else None

    if prop in NUMERIC_PROPERTIES:
        result["tolerance_accuracy"] = {
            "strict_1pct": bootstrap_ci(_bool_array(records, "correct_1pct")),
            "moderate_5pct": bootstrap_ci(_bool_array(records, "correct_5pct")),
            "lenient_10pct": bootstrap_ci(_bool_array(records, "correct_10pct")),
        }
        result["relative_error"] = _relative_error_stats(records)
        if result["relative_error"] is not None:
            result["relative_error"]["by_tier"] = {
                tier: _relative_error_stats([r for r in records if r["tier"] == tier])
                for tier in TIERS
            }
        if prop == "correlation":
            result["by_control"] = correlation_control_breakdown(records)

    elif prop in INTEGER_PROPERTIES:
        result["tolerance_accuracy"] = {"exact": bootstrap_ci(correct)}

    elif prop in BOOLEAN_PROPERTIES:
        parsed = [r for r in records if r.get("parse_success")]
        tp = sum(1 for r in parsed if r["ground_truth"] is True and r["parsed_answer"] is True)
        fp = sum(1 for r in parsed if r["ground_truth"] is False and r["parsed_answer"] is True)
        fn = sum(1 for r in parsed if r["ground_truth"] is True and r["parsed_answer"] is False)
        tn = sum(1 for r in parsed if r["ground_truth"] is False and r["parsed_answer"] is False)
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        result["confusion_matrix"] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn}
        result["precision"] = precision
        result["recall"] = recall
        base_rate_vals = [bool(r["ground_truth"]) for r in records]
        result["base_rate_true"] = float(np.mean(base_rate_vals)) if base_rate_vals else None

        ppr = predicted_positive_rate(records)
        lo, hi = DEGENERATE_POSITIVE_RATE_BAND
        result["predicted_positive_rate"] = ppr
        ba = balanced_accuracy_bool(records)
        near_chance = ba is not None and ba["balanced_accuracy"] <= NEAR_CHANCE_BALANCED_ACCURACY
        result["degenerate_guess"] = (ppr is not None and not (lo <= ppr <= hi)) or near_chance
        result["near_chance"] = near_chance

    elif prop in CATEGORICAL_PROPERTIES:
        parsed = [r for r in records if r.get("parse_success")]
        matrix = {gt: {pred: 0 for pred in CATEGORICAL_CLASSES} for gt in CATEGORICAL_CLASSES}
        for r in parsed:
            gt, pred = r["ground_truth"], r["parsed_answer"]
            if gt in matrix and pred in matrix[gt]:
                matrix[gt][pred] += 1
        result["confusion_matrix"] = matrix

    if prop == "correlation":
        # Restrict the headline by-tier view to planted pairs only. Control
        # (near-zero, is_control=True) pairs get statistically easier to
        # exact-match as tier grows -- bigger tables shrink the sampling
        # variance of an independent pair's r toward 0 (SE(r) ~ 1/sqrt(n-3)),
        # so "clearly uncorrelated" is a genuinely easier call with more
        # rows, not a generation bug (confirmed: even a single random pair's
        # hit rate against a fixed threshold rises from ~22% on simple to
        # ~69% on hard). Mixing control questions into the tier comparison
        # therefore inflates hard's apparent accuracy for reasons that have
        # nothing to do with the model. Planted pairs are held tier-
        # invariant by construction (rejection-sampled to |r| >= 0.30
        # regardless of tier), so restricting to them here makes the tier
        # comparison apples-to-apples. `by_tier_all` keeps the unrestricted
        # view; `by_control` (below) already reports control's own numbers.
        result["by_tier_all"] = {
            tier: bootstrap_ci(_bool_array([r for r in records if r["tier"] == tier], "correct"))
            for tier in TIERS
        }
        result["by_tier"] = {
            tier: bootstrap_ci(_bool_array(
                [r for r in records if r["tier"] == tier and r.get("is_control") is False], "correct"))
            for tier in TIERS
        }
    else:
        result["by_tier"] = {
            tier: bootstrap_ci(_bool_array([r for r in records if r["tier"] == tier], "correct"))
            for tier in TIERS
        }

    if prop in BOOLEAN_PROPERTIES | CATEGORICAL_PROPERTIES:
        result["balanced_accuracy"] = balanced_accuracy(records, prop)
        result["balanced_accuracy_by_tier"] = {
            tier: balanced_accuracy([r for r in records if r["tier"] == tier], prop)
            for tier in TIERS
        }
    return result


def locality_metrics(records: list[dict]) -> dict:
    out = {}
    for locality in ("local", "global"):
        subset = [r for r in records if r["property_locality"] == locality]
        out[locality] = bootstrap_ci(_bool_array(subset, "correct"))
    return out


# ----------------------------------------------------------------------
# Aggregate metrics -- PDF 6.3 "Aggregate metrics"
# ----------------------------------------------------------------------


def _property_skill_score(records: list[dict], prop: str) -> Optional[float]:
    """One base-rate-robust number per property: balanced accuracy for
    boolean/categorical, raw accuracy for numeric/integer."""
    if not records:
        return None
    if prop in BOOLEAN_PROPERTIES | CATEGORICAL_PROPERTIES:
        ba = balanced_accuracy(records, prop)
        return ba["balanced_accuracy"] if ba else None
    return float(_bool_array(records, "correct").mean())


def _macro_skill(records: list[dict]) -> Optional[float]:
    scores = [
        s for prop in sorted({r["property"] for r in records})
        if (s := _property_skill_score([r for r in records if r["property"] == prop], prop)) is not None
    ]
    return float(np.mean(scores)) if scores else None


def local_global_gap(records: list[dict]) -> dict:
    local_recs = [r for r in records if r["property_locality"] == "local"]
    glob_recs = [r for r in records if r["property_locality"] == "global"]
    local = _bool_array(local_recs, "correct")
    glob = _bool_array(glob_recs, "correct")
    gap = (float(local.mean()) - float(glob.mean())) * 100 if len(local) and len(glob) else None

    # Base-rate-robust variant: macro-average each side's per-property skill
    # score (balanced accuracy for boolean/categorical, raw accuracy for
    # numeric/integer) so the gap isn't inflated by imbalanced yes/no props.
    local_macro = _macro_skill(local_recs)
    glob_macro = _macro_skill(glob_recs)
    gap_macro = (
        (local_macro - glob_macro) * 100
        if local_macro is not None and glob_macro is not None
        else None
    )
    return {
        "local_mean_accuracy": float(local.mean()) if len(local) else None,
        "global_mean_accuracy": float(glob.mean()) if len(glob) else None,
        "gap_pp": gap,
        "bootstrap_test": bootstrap_test(local, glob) if len(local) and len(glob) else None,
        "local_macro_skill": local_macro,
        "global_macro_skill": glob_macro,
        "gap_macro_skill_pp": gap_macro,
    }


def complexity_gradient(records: list[dict]) -> dict:
    by_prop: dict[str, dict[str, float]] = defaultdict(dict)
    for prop in sorted({r["property"] for r in records}):
        prop_records = [r for r in records if r["property"] == prop]
        tier_pool = prop_records
        if prop == "correlation":
            # Same restriction as property_metrics' by_tier: control pairs
            # get statistically easier to exact-match as tier grows (see
            # the comment there), so a simple-vs-hard comparison mixing them
            # in is n-confounded, not a difficulty signal.
            tier_pool = [r for r in prop_records if r.get("is_control") is False]
        simple_recs = [r for r in tier_pool if r["tier"] == "simple"]
        hard_recs = [r for r in tier_pool if r["tier"] == "hard"]
        simple = _bool_array(simple_recs, "correct")
        hard = _bool_array(hard_recs, "correct")
        if len(simple) and len(hard):
            entry = {
                "simple_accuracy": float(simple.mean()),
                "hard_accuracy": float(hard.mean()),
                "drop_pp": (float(simple.mean()) - float(hard.mean())) * 100,
            }
            if prop in BOOLEAN_PROPERTIES:
                # Raw accuracy here is contaminated by the ground-truth base
                # rate drifting between tiers; a degenerate guesser shows a
                # gradient without changing behaviour. Report the balanced
                # version and flag when the model isn't really answering.
                s_ba = balanced_accuracy(simple_recs, prop)
                h_ba = balanced_accuracy(hard_recs, prop)
                entry["simple_balanced_accuracy"] = s_ba["balanced_accuracy"] if s_ba else None
                entry["hard_balanced_accuracy"] = h_ba["balanced_accuracy"] if h_ba else None
                if s_ba and h_ba:
                    entry["drop_balanced_pp"] = (s_ba["balanced_accuracy"] - h_ba["balanced_accuracy"]) * 100
                else:
                    entry["drop_balanced_pp"] = None
                ppr = predicted_positive_rate(prop_records)
                lo, hi = DEGENERATE_POSITIVE_RATE_BAND
                whole = balanced_accuracy(prop_records, prop)
                near_chance = whole is not None and whole["balanced_accuracy"] <= NEAR_CHANCE_BALANCED_ACCURACY
                entry["degenerate_guess"] = (ppr is not None and not (lo <= ppr <= hi)) or near_chance
                entry["trust_raw_gradient"] = not entry["degenerate_guess"]
            by_prop[prop] = entry
    return dict(by_prop)


def cross_model_consistency(all_model_records: dict[str, list[dict]]) -> dict:
    """Spearman rank correlation of per-property mean accuracy across
    models -- PDF 6.3: 'high rank correlation indicates the difficulty
    ordering of properties is model-independent.'"""
    models = sorted(all_model_records.keys())
    if len(models) < 2:
        return {"note": "requires >= 2 models with results", "models_available": models}

    properties = sorted({r["property"] for recs in all_model_records.values() for r in recs})
    acc_matrix = {}
    for model in models:
        recs = all_model_records[model]
        acc_matrix[model] = [
            float(np.mean([r["correct"] for r in recs if r["property"] == p])) if any(r["property"] == p for r in recs) else np.nan
            for p in properties
        ]

    pairwise = {}
    for i, m1 in enumerate(models):
        for m2 in models[i + 1 :]:
            v1, v2 = np.array(acc_matrix[m1]), np.array(acc_matrix[m2])
            mask = ~(np.isnan(v1) | np.isnan(v2))
            if mask.sum() < 3:
                continue
            rho, p = scipy_stats.spearmanr(v1[mask], v2[mask])
            pairwise[f"{m1}__vs__{m2}"] = {"spearman_rho": float(rho), "p_value": float(p), "n_properties": int(mask.sum())}

    return {"properties": properties, "per_model_property_accuracy": acc_matrix, "pairwise_spearman": pairwise}


# ----------------------------------------------------------------------
# Decision rules -- PDF Section 9 (single-domain reading; the PDF's rule
# is phrased across 2-of-3 domains, which doesn't apply with one domain).
# ----------------------------------------------------------------------


def decision_rule_flags(model_report: dict) -> dict:
    # Only genuine global (aggregate-the-whole-column) properties count toward
    # the bottleneck signature -- not the local props, and not boolean props
    # where the model is just guessing (a degenerate guess failing on hard is
    # not evidence of a *reasoning* bottleneck, only of not trying).
    global_property_set = NUMERIC_PROPERTIES | BOOLEAN_PROPERTIES
    global_props_below_70_on_hard = [
        prop for prop, m in model_report["by_property"].items()
        if prop in global_property_set
        and not m.get("degenerate_guess")
        and (m["by_tier"].get("hard") or {}).get("mean") is not None
        and m["by_tier"]["hard"]["mean"] < 0.70
    ]
    local_above_85 = model_report["by_locality"]["local"] and model_report["by_locality"]["local"]["mean"] > 0.85
    all_above_85_hard = all(
        (m["by_tier"].get("hard") or {}).get("mean", 0) > 0.85 for m in model_report["by_property"].values()
    )
    max_parse_failure = max(
        (1 - (m["parse_success_rate"] or 1.0)) for m in model_report["by_property"].values()
    )
    return {
        "global_properties_below_70pct_on_hard": global_props_below_70_on_hard,
        "local_accuracy_above_85pct": local_above_85,
        "reads_as_implicit_structure_bottleneck": bool(global_props_below_70_on_hard) and local_above_85,
        "all_properties_above_85pct_on_hard": all_above_85_hard,
        "max_parse_failure_rate": max_parse_failure,
        "fix_harness_flag": max_parse_failure > 0.15,
        "note": "PDF Section 9's rule requires 2-of-3 domains; only the tabular domain exists here, so this is a single-domain reading, not the full decision.",
    }


# ----------------------------------------------------------------------
# Report assembly
# ----------------------------------------------------------------------


def build_model_report(model: str, records: list[dict]) -> dict:
    properties = sorted({r["property"] for r in records})
    by_property = {prop: property_metrics([r for r in records if r["property"] == prop], prop) for prop in properties}
    report = {
        "model": model,
        "n_records": len(records),
        "by_property": by_property,
        "by_locality": locality_metrics(records),
        "local_global_gap": local_global_gap(records),
        "complexity_gradient": complexity_gradient(records),
        "overall_accuracy": bootstrap_ci(_bool_array(records, "correct")),
        "overall_macro_skill": _macro_skill(records),
        "overall_parse_success_rate": float(_bool_array(records, "parse_success").mean()) if records else None,
        "tier_majority_baseline": tier_majority_baseline(records),
    }
    report["degenerate_boolean_properties"] = [
        prop for prop in by_property
        if by_property[prop].get("degenerate_guess")
    ]
    report["decision_rule_flags"] = decision_rule_flags(report)
    return report


def build_full_report(all_model_records: dict[str, list[dict]]) -> dict:
    return {
        "domain": "tabular",
        "models": sorted(all_model_records.keys()),
        "per_model": {model: build_model_report(model, recs) for model, recs in all_model_records.items()},
        "cross_model_consistency": cross_model_consistency(all_model_records),
    }


def print_summary(report: dict) -> None:
    for model, mr in report["per_model"].items():
        print(f"\n=== {model} ===")
        oa = mr["overall_accuracy"]
        print(f"  overall accuracy: {oa['mean']:.3f} [{oa['ci_lo_95']:.3f}, {oa['ci_hi_95']:.3f}]  n={oa['n']}")
        if mr.get("overall_macro_skill") is not None:
            print(f"  overall macro skill (balanced): {mr['overall_macro_skill']:.3f}")
        base = mr.get("tier_majority_baseline") or {}
        if base.get("overall") is not None:
            print(
                f"  tier-majority baseline (reads nothing): overall={base['overall']:.3f}"
                f"  global={base.get('global'):.3f}  boolean={base.get('boolean'):.3f}"
            )
        print(f"  parse success rate: {mr['overall_parse_success_rate']:.3f}")
        gap = mr["local_global_gap"]
        if gap["gap_pp"] is not None:
            bt = gap["bootstrap_test"]
            sig = f"p={bt['p_value']:.4f}" if bt else "n/a"
            print(f"  local-global gap: {gap['gap_pp']:.1f}pp (local={gap['local_mean_accuracy']:.3f}, global={gap['global_mean_accuracy']:.3f}, {sig})")
        if gap.get("gap_macro_skill_pp") is not None:
            print(
                f"  local-global gap (macro skill): {gap['gap_macro_skill_pp']:.1f}pp "
                f"(local={gap['local_macro_skill']:.3f}, global={gap['global_macro_skill']:.3f})"
            )
        print("  by property:")
        for prop, m in sorted(mr["by_property"].items()):
            acc = m["accuracy"]
            line = f"    {prop:18s} acc={acc['mean']:.3f}  n={acc['n']:4d}  parse_ok={m['parse_success_rate']:.3f}"
            if (m["parse_success_rate"] or 1.0) < 0.5:
                line += "  [acc unreliable: model answered <50%]"
            ba = m.get("balanced_accuracy")
            if isinstance(ba, dict) and ba.get("balanced_accuracy") is not None:
                line += f"  bal_acc={ba['balanced_accuracy']:.3f}"
            elif prop in BOOLEAN_PROPERTIES | CATEGORICAL_PROPERTIES and ba is None:
                line += "  bal_acc=n/a(one class)"
            if m.get("degenerate_guess"):
                line += f"  [DEGENERATE guess: {m['predicted_positive_rate']*100:.0f}% 'true']"
            print(line)
            bc = m.get("by_control")
            if bc and bc.get("discrimination_gap") is not None:
                p_hi = bc["planted"]["called_correlated_rate"]
                c_hi = bc["control"]["called_correlated_rate"]
                print(
                    f"      correlation discrimination: planted called |r|>=0.3 {p_hi*100:.0f}% "
                    f"vs control {c_hi*100:.0f}%  (gap {bc['discrimination_gap']*100:+.0f}pp; "
                    f"exact-value acc planted {bc['planted']['accuracy_5pct']['mean']*100:.0f}% "
                    f"/ control {bc['control']['accuracy_5pct']['mean']*100:.0f}%)"
                )
        if mr.get("degenerate_boolean_properties"):
            print(
                f"  [!] degenerate guessing on: {mr['degenerate_boolean_properties']} "
                f"-- per-tier accuracy there tracks the base rate, not difficulty; "
                f"use balanced accuracy and ignore the raw complexity gradient"
            )
        flags = mr["decision_rule_flags"]
        if flags["fix_harness_flag"]:
            print(f"  [!] parse failure rate exceeds 15% for at least one property -- fix harness before trusting these results")
        if flags["reads_as_implicit_structure_bottleneck"]:
            print(f"  [note] reads as an implicit-structure bottleneck: {flags['global_properties_below_70pct_on_hard']} below 70% on hard while local stays >85%")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--results",
        nargs="*",
        default=None,
        help="model=path pairs, e.g. v4flash=../phase2_tabular_model_results/01_v4flash/v4flash_results.json. "
        "If omitted, auto-discovers *_results.json under phase2_tabular_model_results/.",
    )
    p.add_argument("--output", type=Path, default=REPORT_PATH)
    p.add_argument("--figures", action="store_true", help="also render the PDF §8 figure set to figures/")
    p.add_argument(
        "--no-rescore-numeric",
        action="store_true",
        help="keep numeric records' original correct_* flags instead of re-deriving "
        "them with the abs-OR-rel tolerance rule (relevant only for runs collected "
        "before that rule landed in harness.py).",
    )
    args = p.parse_args()

    if args.results:
        paths = {}
        for spec in args.results:
            model, path = spec.split("=", 1)
            paths[model] = Path(path)
    else:
        paths = discover_results(PHASE2_DIR)

    if not paths:
        print(f"No result files found under {PHASE2_DIR}. Run Phase 2 first, or pass --results model=path ...")
        return

    print(f"Loading results for: {sorted(paths.keys())}")
    all_model_records = load_results(paths, rescore_numeric=not args.no_rescore_numeric)
    report = build_full_report(all_model_records)

    args.output.write_text(json.dumps(report, indent=2))
    print(f"\nWrote {args.output}")
    print_summary(report)

    if args.figures:
        from figures import make_all_figures

        paths = make_all_figures(report, all_model_records, HERE / "figures")
        print(f"\nWrote {len(paths)} figures to {HERE / 'figures'}")


if __name__ == "__main__":
    main()
