"""
Phase 1 -- Domain 3 (Tabular Data) dataset generation.

Builds the Experiment 1 tabular benchmark: 300 tables -- 250 synthetic with
planted statistical properties, 50 drawn from real open-license sources --
serialized as CSV, with ground truth for the 8 properties in PDF Section 5.4
(Table 11) and a 6-10 query selection per table (Section 5.4, "Property
query specification").

Ported from the Domain 1 (geometry) Phase 1 design documented alongside this
project: three complexity tiers, rejection sampling against an explicit,
named validity rule set, ground truth computed empirically from the final
generated object (not asserted from generation intent), and independent
re-verification from the serialized string using a separate code path.

Real-data tables (PDF 5.2) require a human to source and license-check
actual open datasets -- that step is not automated here. See
`real_data_sources/README.md` for how to supply them. Until populated, the
dataset is generated synthetic-only and the summary records this plainly.

Run directly to build the dataset:
    python dataset.py
"""

from __future__ import annotations

import csv
import io
import json
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

# ----------------------------------------------------------------------
# Section 0: constants
# ----------------------------------------------------------------------

SEED = 42
HERE = Path(__file__).resolve().parent

DATASET_PATH = HERE / "tabular_exp1_dataset.json"
SUMMARY_PATH = HERE / "tabular_exp1_summary.json"
SPOTCHECK_PATH = HERE / "spotcheck_exp1_tabular.txt"

REAL_DATA_DIR = HERE / "real_data_sources"
REAL_MANIFEST_PATH = REAL_DATA_DIR / "manifest.json"

COL_LETTERS = "ABCDEFGHIJKL"  # up to 12 columns -> col_A .. col_L
CORR_TARGETS = [-0.9, -0.7, -0.5, 0.5, 0.7, 0.9]
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # excludes I/O (easily confused with 1/0)

# name -> (row range, col range, n_synthetic, n_real). 100 objects/tier,
# 250/50 synthetic/real overall, matching PDF Table 10 and Section 5.2.
TIERS: dict[str, dict[str, Any]] = {
    "simple": {"rows": (8, 15), "cols": (4, 6), "n_synthetic": 83, "n_real": 17},
    "medium": {"rows": (16, 30), "cols": (6, 9), "n_synthetic": 83, "n_real": 17},
    "hard": {"rows": (31, 50), "cols": (9, 12), "n_synthetic": 84, "n_real": 16},
}

MAX_NULL_FRACTION = 0.05
MAX_TRIES = 500
QUERIES_PER_TABLE = (6, 10)  # inclusive range, PDF 5.4

# has_outlier ground truth: a value is an outlier iff it falls beyond the
# Tukey fence Q1 - k*IQR .. Q3 + k*IQR. k=3 is the standard "extreme /
# far-out" cutoff. This replaces the earlier "|z| > 3 sample-std" rule,
# which is mathematically unsatisfiable on small tables -- the largest
# possible |z-score| in n points is (n-1)/sqrt(n), which is below 3 for
# every n <= 10, so no simple-tier table (8-15 rows) could ever be True.
# IQR fences are well-defined for n >= 4 and don't degrade with n.
IQR_OUTLIER_K = 3.0

# Planted correlations must actually be correlated: reject a generated
# table whose planted pair's realized |r| falls below this (unlucky noise
# draw). Control pairs must be near-zero: only ask a control query when an
# unrelated pair with |r| below CONTROL_MAX_ABS_R exists.
PLANTED_MIN_ABS_R = 0.30
CONTROL_MAX_ABS_R = 0.20

# is_monotonic only, by explicit request: the "yes" rate deliberately rises
# across tiers instead of staying flat at 50/50 like the other two boolean
# properties. This means a model that answers "no" most of the time (as
# both qwen3 and llama_scout do here) will score higher on simple than on
# hard purely from this base-rate shift, not from any change in the model's
# actual behavior across tiers -- i.e. per-tier accuracy on is_monotonic is
# NOT a clean difficulty signal once this is in place; use balanced
# accuracy (flat ~0.5-0.6 regardless of tier) to see the model's real,
# tier-independent skill on this property.
IS_MONOTONIC_TARGET_RATE = {"simple": 0.25, "medium": 0.50, "hard": 0.75}

PROPERTY_LOCALITY = {
    "row_count": "local",
    "column_dtype": "local",
    "null_count": "local",
    "correlation": "global",
    "is_monotonic": "global",
    "has_outlier": "global",
    "skewness": "global",
    "func_dependency": "global",
}


# ----------------------------------------------------------------------
# Section 1: core helpers -- validity, dtype inference, rounding
# ----------------------------------------------------------------------


def round_numeric(value: float, subtype: str) -> float | int:
    """Round a raw value to its column's display precision. int columns
    carry no decimals; float columns carry up to 2, matching Section 5.3."""
    if subtype == "int":
        return int(round(value))
    return round(float(value), 2)


def make_category_pool(k: int, rng: random.Random) -> list[str]:
    """k distinct, semantically-empty category codes for one categorical
    column. Drawn fresh per column (not from one fixed shared vocabulary)
    so different columns and tables don't all echo the same literal
    strings -- avoids a repetitive "cat_1, cat_2, ..." look while staying
    just as free of real-world domain cueing as a fixed placeholder would
    be (PDF 5.2 item 6's rationale for generic column names applies
    equally to category values)."""
    pool: set[str] = set()
    while len(pool) < k:
        pool.add("".join(rng.choices(CODE_ALPHABET, k=3)))
    return sorted(pool)


def infer_dtype_from_values(values: list) -> str:
    """Infer {numeric, categorical, datetime} from realized cell values,
    independent of how the column was planned -- mirrors how a reader of
    the serialized CSV (with no schema) would have to guess."""
    non_null = [v for v in values if v is not None and v == v]  # drop NaN
    if not non_null:
        return "categorical"
    numeric_ok = 0
    for v in non_null:
        try:
            float(v)
            numeric_ok += 1
        except (TypeError, ValueError):
            pass
    if numeric_ok == len(non_null):
        return "numeric"
    date_ok = 0
    for v in non_null:
        try:
            pd.to_datetime(str(v), format="%Y-%m-%d")
            date_ok += 1
        except (ValueError, TypeError):
            pass
    if date_ok == len(non_null):
        return "datetime"
    return "categorical"


def validity_check(df: pd.DataFrame, tier: str) -> tuple[bool, Optional[str]]:
    """PDF 5.2 validity constraints. Returns (ok, reason)."""
    n_rows, n_cols = df.shape
    lo_rows, hi_rows = TIERS[tier]["rows"]
    lo_cols, hi_cols = TIERS[tier]["cols"]

    if not (lo_rows <= n_rows <= hi_rows):
        return False, "row_count_out_of_range"
    if not (lo_cols <= n_cols <= hi_cols):
        return False, "col_count_out_of_range"
    if any(df[c].isnull().all() for c in df.columns):
        return False, "empty_column"
    if df.isnull().all(axis=1).any():
        return False, "empty_row"
    numeric_cols = [
        c for c in df.columns if infer_dtype_from_values(df[c].tolist()) == "numeric"
    ]
    if len(numeric_cols) < 2:
        return False, "insufficient_numeric_columns"
    total_cells = n_rows * n_cols
    null_cells = int(df.isnull().sum().sum())
    if null_cells / total_cells > MAX_NULL_FRACTION:
        return False, "too_many_nulls"
    return True, None


# ----------------------------------------------------------------------
# Section 2: synthetic table planning
# ----------------------------------------------------------------------


@dataclass
class TablePlan:
    tier: str
    n_rows: int
    columns: list[str]
    col_type: dict[str, str] = field(default_factory=dict)  # numeric/categorical/datetime
    col_subtype: dict[str, str] = field(default_factory=dict)  # int/float for numeric
    corr_pairs: list[tuple[str, str, float]] = field(default_factory=list)
    monotonic_cols: list[tuple[str, str]] = field(default_factory=list)  # (col, direction)
    outlier_cols: list[str] = field(default_factory=list)
    fd_pair: Optional[tuple[str, str, str]] = None  # (a, b, kind)


def plan_table(tier: str, rng: random.Random) -> TablePlan:
    lo_r, hi_r = TIERS[tier]["rows"]
    lo_c, hi_c = TIERS[tier]["cols"]
    n_rows = rng.randint(lo_r, hi_r)
    n_cols = rng.randint(lo_c, hi_c)

    has_datetime = rng.random() < 0.3 and n_cols >= 4
    n_datetime = 1 if has_datetime else 0
    remaining = n_cols - n_datetime
    if remaining <= 3:
        n_numeric = max(2, remaining)
    else:
        n_numeric = rng.randint(2, remaining - 1)
    n_categorical = remaining - n_numeric

    type_slots = (
        ["numeric"] * n_numeric + ["categorical"] * n_categorical + ["datetime"] * n_datetime
    )
    rng.shuffle(type_slots)
    columns = [f"col_{COL_LETTERS[i]}" for i in range(n_cols)]

    plan = TablePlan(tier=tier, n_rows=n_rows, columns=columns)
    for col, ctype in zip(columns, type_slots):
        plan.col_type[col] = ctype
        if ctype == "numeric":
            plan.col_subtype[col] = "int" if rng.random() < 0.5 else "float"

    numeric_pool = [c for c in columns if plan.col_type[c] == "numeric"]
    categorical_pool = [c for c in columns if plan.col_type[c] == "categorical"]
    rng.shuffle(numeric_pool)
    rng.shuffle(categorical_pool)

    # Correlation pairs: disjoint columns, drawn from the numeric pool.
    n_corr = rng.randint(0, min(3, len(numeric_pool) // 2))
    for _ in range(n_corr):
        if len(numeric_pool) < 2:
            break
        a, b = numeric_pool.pop(), numeric_pool.pop()
        target_r = rng.choice(CORR_TARGETS)
        plan.corr_pairs.append((a, b, target_r))

    # Monotonic columns: disjoint from correlation columns. Weighted toward
    # planting at least one, so the balancer (select_queries) reliably has
    # a genuine monotonic column to ask a True query about.
    if numeric_pool:
        n_mono = min(rng.choices([0, 1, 2], weights=[18, 57, 25])[0], len(numeric_pool))
    else:
        n_mono = 0
    for _ in range(n_mono):
        if not numeric_pool:
            break
        col = numeric_pool.pop()
        direction = rng.choice(["increasing", "decreasing"])
        plan.monotonic_cols.append((col, direction))

    # Functional dependency: same-type pair (numeric-linear or
    # categorical-lookup), disjoint from correlation/monotonic roles.
    # ~65% of tables so the balancer has planted True material to draw on.
    if rng.random() < 0.65:
        if len(categorical_pool) >= 2 and rng.random() < 0.5:
            a, b = categorical_pool.pop(), categorical_pool.pop()
            plan.fd_pair = (a, b, "categorical_lookup")
        elif len(numeric_pool) >= 2:
            a, b = numeric_pool.pop(), numeric_pool.pop()
            plan.fd_pair = (a, b, "numeric_linear")
        elif len(categorical_pool) >= 2:
            a, b = categorical_pool.pop(), categorical_pool.pop()
            plan.fd_pair = (a, b, "categorical_lookup")

    # Outliers: any numeric column EXCEPT a planted-monotonic one (an
    # injected outlier would break its monotonicity, shrinking the pool of
    # genuine is_monotonic=True columns the balancer can draw on). Ground
    # truth is still measured after the fact. Weighted toward planting at
    # least one so has_outlier=True material is reliably available.
    mono_cols = {c for c, _ in plan.monotonic_cols}
    out_pool = [c for c in columns if plan.col_type[c] == "numeric" and c not in mono_cols]
    if out_pool:
        n_out = min(rng.choices([0, 1, 2], weights=[25, 55, 20])[0], len(out_pool))
    else:
        n_out = 0
    plan.outlier_cols = rng.sample(out_pool, n_out) if n_out else []

    return plan


# ----------------------------------------------------------------------
# Section 3: value generation
# ----------------------------------------------------------------------


def _rescale(arr: np.ndarray, rng: random.Random) -> np.ndarray:
    lo = rng.uniform(-500, 500)
    span = rng.uniform(10, 500)
    z = (arr - arr.mean()) / (arr.std() if arr.std() > 1e-9 else 1.0)
    return lo + (z - z.min()) / (z.max() - z.min() + 1e-9) * span


def generate_values(plan: TablePlan, rng: random.Random, np_rng: np.random.Generator) -> dict[str, list]:
    n = plan.n_rows
    data: dict[str, list] = {}

    # Datetime columns first -- no dependency on anything else.
    for col in plan.columns:
        if plan.col_type[col] == "datetime":
            start = date(2020, 1, 1) + timedelta(days=rng.randint(0, 1500))
            step_choices = [1, 2, 3, 7]
            step = rng.choice(step_choices)
            data[col] = [
                (start + timedelta(days=i * step + rng.randint(0, 2))).isoformat()
                for i in range(n)
            ]

    # Correlation pairs: joint generation, ground truth is the realized
    # sample correlation (computed later, on rounded values), not the
    # planted target -- per PDF 5.2 item 2.
    for a, b, target_r in plan.corr_pairs:
        x = np_rng.normal(0, 1, n)
        eps = np_rng.normal(0, 1, n)
        y = target_r * x + np.sqrt(max(0.0, 1 - target_r**2)) * eps
        x = _rescale(x, rng)
        y = _rescale(y, rng)
        data[a] = [round_numeric(v, plan.col_subtype[a]) for v in x]
        data[b] = [round_numeric(v, plan.col_subtype[b]) for v in y]

    # Monotonic columns: sorted cumulative increments, per PDF 5.2 item 3.
    for col, direction in plan.monotonic_cols:
        start = rng.uniform(-100, 100)
        increments = [rng.uniform(0.1, 8.0) for _ in range(n)]
        vals = np.cumsum([start] + increments)[:n]
        if direction == "decreasing":
            vals = vals[::-1]
        data[col] = [round_numeric(v, plan.col_subtype[col]) for v in vals]

    # Functional-dependency source column: generated as an ordinary
    # independent column; the target is derived from it afterward.
    fd_a = fd_b = fd_kind = None
    if plan.fd_pair:
        fd_a, fd_b, fd_kind = plan.fd_pair

    # Remaining plain numeric / categorical columns (including fd source).
    for col in plan.columns:
        if col in data:
            continue
        if col == fd_b:
            continue  # derived below, after col_a is finalized
        ctype = plan.col_type[col]
        if ctype == "numeric":
            lo = rng.uniform(-200, 200)
            hi = lo + rng.uniform(10, 400)
            # "uniform"/"normal" both have ~0 population skewness, so their
            # *sample* skewness is pure small-n estimator noise: it shrinks
            # toward 0 as the tier's row count grows, which made the
            # skewness query silently easier on hard tables (larger n, most
            # values near 0, easier to land inside the abs-OR-rel tolerance)
            # without the model doing anything differently. "skewed" plants
            # a real, tier-independent population skew via a lognormal, so
            # a genuinely nonzero target is available regardless of n.
            dist = rng.choice(["uniform", "normal", "skewed"])
            if dist == "uniform":
                vals = np_rng.uniform(lo, hi, n)
            elif dist == "normal":
                mid = (lo + hi) / 2
                vals = np.clip(np_rng.normal(mid, (hi - lo) / 6, n), lo, hi)
            else:  # skewed
                sigma = rng.uniform(0.25, 0.5)
                raw = np_rng.lognormal(mean=0.0, sigma=sigma, size=n)
                if rng.random() < 0.5:
                    raw = -raw  # mirror -> left-skew instead of right-skew
                # affine rescale into [lo, hi]; skewness is invariant to this
                rmin, rmax = raw.min(), raw.max()
                vals = lo + (raw - rmin) / (rmax - rmin + 1e-12) * (hi - lo)
            data[col] = [round_numeric(v, plan.col_subtype[col]) for v in vals]
        elif ctype == "categorical":
            k = rng.randint(3, 8)
            cats = make_category_pool(k, rng)
            weights = [rng.random() + 0.2 for _ in cats]
            data[col] = list(np_rng.choice(cats, size=n, p=np.array(weights) / sum(weights)))

    # Functional-dependency target column, derived from the (already
    # rounded/finalized) source so the relation is exactly reproducible
    # from what ends up in the CSV.
    if plan.fd_pair:
        source_vals = data[fd_a]
        if fd_kind == "numeric_linear":
            m = rng.choice([2, 3, -2, 0.5, -0.5, 1.5])
            b = rng.uniform(-20, 20)
            sub = plan.col_subtype[fd_b]
            data[fd_b] = [round_numeric(m * v + b, sub) for v in source_vals]
        else:  # categorical_lookup
            uniq = sorted(set(source_vals))
            k_b = rng.randint(2, 6)
            b_cats = make_category_pool(k_b, rng)
            lookup = {u: rng.choice(b_cats) for u in uniq}
            data[fd_b] = [lookup[v] for v in source_vals]

    return data


def inject_outliers(data: dict[str, list], plan: TablePlan, rng: random.Random) -> dict:
    """PDF 5.2 item 4: 1-3 outliers per chosen column, each placed well
    beyond the column's (pre-outlier) Tukey fence so it registers under
    ref_has_outlier's Q3 + k*IQR / Q1 - k*IQR rule. Injected at 4-8 IQRs
    past the relevant quartile -- far enough that it stays outside the
    fence even after its own presence widens the quartiles slightly.
    Records positions for provenance."""
    outlier_positions: dict[str, list[int]] = {}
    for col in plan.outlier_cols:
        vals = data[col]
        arr = np.array(vals, dtype=float)
        if len(arr) < 4:
            continue
        q1, q3 = np.percentile(arr, 25), np.percentile(arr, 75)
        iqr = q3 - q1
        if iqr < 1e-9:
            # near-constant column: fall back to a std-based spread
            spread = arr.std(ddof=1)
            if spread < 1e-9:
                continue
            iqr, q1, q3 = spread, arr.mean(), arr.mean()
        n_out = rng.randint(1, 3)
        positions = rng.sample(range(len(vals)), min(n_out, len(vals)))
        sub = plan.col_subtype[col]
        # k is centered a bit above the old fixed [5, 9] range on small
        # tables: 1-3 outliers among a handful of rows (simple tier) pull
        # the *post-injection* quartiles outward more than they do among
        # 30-50 rows (hard tier), so the same nominal k=5-9 registered as a
        # smaller measured "IQRs past the fence" once has_outlier's ground
        # truth re-measures quartiles on the final (post-injection) data --
        # making planted outliers on hard tables read as more obviously
        # extreme than on simple ones for no reason but row count. The
        # 10/n boost compensates so the *measured* extremity distribution
        # ends up roughly n-invariant (calibrated by simulation).
        k_center = 7.0 + 10.0 / len(vals)
        for pos in positions:
            k = rng.uniform(k_center - 2.0, k_center + 2.0)
            sign = rng.choice([-1, 1])
            base = q3 if sign > 0 else q1
            new_val = round_numeric(base + sign * k * iqr, sub)
            data[col][pos] = new_val
        outlier_positions[col] = sorted(positions)
    return outlier_positions


def inject_nulls(data: dict[str, list], plan: TablePlan, rng: random.Random) -> int:
    """PDF 5.2 validity item 4 / generation note: plant a small number of
    nulls, capped so the table stays under MAX_NULL_FRACTION, and never
    empty out an entire row or column."""
    n_rows = plan.n_rows
    n_cols = len(plan.columns)
    total_cells = n_rows * n_cols
    max_nulls = int(total_cells * MAX_NULL_FRACTION)
    if max_nulls == 0 or rng.random() > 0.6:
        return 0
    n_nulls = rng.randint(1, max_nulls)
    col_null_count = {c: 0 for c in plan.columns}
    row_null_count = [0] * n_rows
    placed = 0
    attempts = 0
    while placed < n_nulls and attempts < n_nulls * 20:
        attempts += 1
        col = rng.choice(plan.columns)
        row = rng.randrange(n_rows)
        if data[col][row] is None:
            continue
        if col_null_count[col] + 1 >= n_rows:  # never empty a whole column
            continue
        if row_null_count[row] + 1 >= n_cols:  # never empty a whole row
            continue
        data[col][row] = None
        col_null_count[col] += 1
        row_null_count[row] += 1
        placed += 1
    return placed


def build_synthetic_table(tier: str, rng: random.Random, np_rng: np.random.Generator) -> tuple[pd.DataFrame, TablePlan, dict]:
    for _ in range(MAX_TRIES):
        plan = plan_table(tier, rng)
        data = generate_values(plan, rng, np_rng)
        outlier_positions = inject_outliers(data, plan, rng)
        n_nulls = inject_nulls(data, plan, rng)
        df = pd.DataFrame(data, columns=plan.columns)
        ok, reason = validity_check(df, tier)
        if not ok:
            continue
        # A planted correlation that realized near zero (unlucky noise draw,
        # or an injected outlier flattening it) is indistinguishable from a
        # control -- regenerate rather than ship a mislabeled "planted" pair.
        if any(
            abs(ref_correlation(df, a, b)) < PLANTED_MIN_ABS_R
            for a, b, _ in plan.corr_pairs
        ):
            continue
        provenance = {"outlier_positions": outlier_positions, "n_nulls": n_nulls}
        return df, plan, provenance
    raise RuntimeError(f"Failed to generate a valid {tier} synthetic table after {MAX_TRIES} tries")


# ----------------------------------------------------------------------
# Section 4: ground truth (reference implementation, pandas/scipy-based)
# ----------------------------------------------------------------------


def ref_row_count(df: pd.DataFrame) -> int:
    return int(len(df))


def ref_null_count(df: pd.DataFrame) -> int:
    return int(df.isnull().sum().sum())


def ref_column_dtype(df: pd.DataFrame, col: str) -> str:
    return infer_dtype_from_values(df[col].tolist())


def ref_correlation(df: pd.DataFrame, a: str, b: str) -> float:
    return float(df[a].astype(float).corr(df[b].astype(float)))


def ref_is_monotonic(df: pd.DataFrame, col: str) -> bool:
    s = df[col].dropna()
    if ref_column_dtype(df, col) == "datetime":
        s = pd.to_datetime(s)
    else:
        s = s.astype(float)
    return bool(s.is_monotonic_increasing or s.is_monotonic_decreasing)


def ref_has_outlier(df: pd.DataFrame, col: str) -> bool:
    """True iff some value lies beyond the Tukey fence Q1 - k*IQR .. Q3 +
    k*IQR (k = IQR_OUTLIER_K = 3, the standard "extreme outlier" cutoff).
    Quartiles use linear interpolation (numpy/pandas default)."""
    s = df[col].dropna().astype(float)
    if len(s) < 4:
        return False
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    if iqr < 1e-9:
        return False
    lo, hi = q1 - IQR_OUTLIER_K * iqr, q3 + IQR_OUTLIER_K * iqr
    return bool(((s < lo) | (s > hi)).any())


def describe_outliers(df: pd.DataFrame, col: str) -> str:
    """Human-readable detail for a has_outlier=True column: which row(s)
    and value(s) fell outside the Tukey fence, and what the fence was --
    so a reader of the spot-check preview sees *why* a column was flagged,
    not just a bare True. Row numbers are 1-indexed data rows (header not
    counted), matching how a person would count the CSV."""
    s = df[col].dropna().astype(float)
    if len(s) < 4:
        return ""
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    if iqr < 1e-9:
        return ""
    lo, hi = q1 - IQR_OUTLIER_K * iqr, q3 + IQR_OUTLIER_K * iqr
    hits = [
        f"row {pos + 1}={v:g} (outside [{lo:.2f}, {hi:.2f}])"
        for pos, v in s.items()
        if v < lo or v > hi
    ]
    if not hits:
        return ""
    return (
        f"Q1={q1:.2f}, Q3={q3:.2f}, IQR={iqr:.2f}, so the k={IQR_OUTLIER_K:g} Tukey "
        f"fence is [{lo:.2f}, {hi:.2f}] -- " + "; ".join(hits)
    )


def ref_skewness(df: pd.DataFrame, col: str) -> float:
    s = df[col].dropna().astype(float)
    if len(s) < 3 or s.std() < 1e-9:
        return 0.0
    return float(scipy_stats.skew(s.to_numpy(), bias=False))


def ref_func_dependency(df: pd.DataFrame, a: str, b: str) -> bool:
    sub = df[[a, b]].dropna()
    if sub[a].nunique() < 2:
        return False
    return bool(sub.groupby(a)[b].nunique().max() == 1)


# ----------------------------------------------------------------------
# Section 5: CSV serialization
# ----------------------------------------------------------------------


def to_csv_string(df: pd.DataFrame) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(df.columns.tolist())
    for _, row in df.iterrows():
        formatted = []
        for v in row:
            if v is None or v != v:  # NaN check
                formatted.append("")
            elif isinstance(v, float):
                formatted.append(f"{v:.2f}" if not float(v).is_integer() else f"{v:.2f}")
            else:
                formatted.append(str(v))
        writer.writerow(formatted)
    return buf.getvalue()


# ----------------------------------------------------------------------
# Section 6: query selection (PDF 5.4)
# ----------------------------------------------------------------------


class BooleanBalancer:
    """Keeps the ground-truth true/false split of each yes/no property at
    ~50% *within every tier*.

    The raw base rates are both imbalanced and tier-dependent (a planted
    functional dependency almost always holds; a random column is almost
    never monotonic; small samples rarely clear an outlier fence). Left
    alone, a model that always answers one way scores a different fixed
    number per tier, which reads as a spurious difficulty gradient. This
    tracks the running count per (tier, property) and tells select_queries
    which polarity to look for next; select_queries then picks a real
    column/pair from the table whose *empirically measured* value matches
    (ground truth is still never asserted from generation intent)."""

    def __init__(self) -> None:
        self.counts: dict[tuple[str, str], dict[bool, int]] = defaultdict(
            lambda: {True: 0, False: 0}
        )

    def want(self, tier: str, prop: str, rng: random.Random, target: float = 0.5) -> bool:
        """Steer the running true/false count toward `target` (default
        50/50). `is_monotonic` is deliberately called with a tier-dependent
        target below (see IS_MONOTONIC_TARGET_RATE) -- everything else
        stays at the default 50/50."""
        c = self.counts[(tier, prop)]
        total = c[True] + c[False]
        if total == 0:
            return rng.random() < target
        current_rate = c[True] / total
        if current_rate < target:
            return True
        if current_rate > target:
            return False
        return rng.random() < target

    def record(self, tier: str, prop: str, value: bool) -> None:
        self.counts[(tier, prop)][bool(value)] += 1

    def base_rates(self) -> dict:
        return {
            f"{prop}/{tier}": {
                "n": c[True] + c[False],
                "true_rate": c[True] / (c[True] + c[False]) if (c[True] + c[False]) else None,
            }
            for (tier, prop), c in sorted(self.counts.items())
        }


def _monotonic_candidate(df: pd.DataFrame, cols: list[str], want: bool,
                         plan: TablePlan, rng: random.Random) -> Optional[str]:
    planted = [c for c, _ in plan.monotonic_cols]
    ordered = ([c for c in planted if c in cols] if want else []) + rng.sample(cols, len(cols))
    for c in ordered:
        if ref_is_monotonic(df, c) == want:
            return c
    return None


def _outlier_extremity(df: pd.DataFrame, col: str) -> Optional[float]:
    """How far the column's most extreme value sits past its Tukey fence,
    in IQRs. > 0 means ref_has_outlier is True; the larger, the less
    ambiguous. None if the column can't be assessed."""
    s = df[col].dropna().astype(float)
    if len(s) < 4:
        return None
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    if iqr < 1e-9:
        return None
    hi_over = (s.max() - (q3 + IQR_OUTLIER_K * iqr)) / iqr
    lo_over = ((q1 - IQR_OUTLIER_K * iqr) - s.min()) / iqr
    return float(max(hi_over, lo_over))


def _skew_candidate(df: pd.DataFrame, cols: list[str], rng: random.Random) -> Optional[str]:
    """Prefer a column with a clear, non-trivial |skewness| (>= 0.5) over
    one that's near-zero by small-n sampling noise -- otherwise the
    skewness query's target quietly washes out toward 0 on larger
    (harder-tier) tables regardless of what's actually in the column. Ground
    truth is still whatever the chosen column measures, never asserted;
    falls back to the least-trivial column available if none clears 0.5."""
    best, best_abs = None, -1.0
    for c in rng.sample(cols, len(cols)):
        sk = ref_skewness(df, c)
        if sk != sk:  # NaN guard (constant column)
            continue
        if abs(sk) >= 0.5:
            return c
        if abs(sk) > best_abs:
            best, best_abs = c, abs(sk)
    return best


def _outlier_candidate(df: pd.DataFrame, cols: list[str], want: bool,
                       plan: TablePlan, rng: random.Random) -> Optional[str]:
    """A column that is *unambiguously* an outlier case (or not), so the
    k=3 ground truth is never knife-edge: for True the nearest value must
    clear the fence by >= 1 IQR, for False it must stay >= 1 IQR inside
    it. Columns sitting in the ambiguous band are skipped."""
    ordered = ([c for c in plan.outlier_cols if c in cols] if want else []) + rng.sample(cols, len(cols))
    for c in ordered:
        ext = _outlier_extremity(df, c)
        if ext is None:
            continue
        if want and ext >= 1.0:
            return c
        if not want and ext <= -1.0:
            return c
    return None


def _fd_candidate(df: pd.DataFrame, plan: TablePlan, want: bool,
                  rng: random.Random) -> Optional[tuple[str, str]]:
    """A (a, b) pair whose measured func_dependency == want. For True,
    prefer the planted pair. For False, require `a` to have repeated
    values (otherwise every b is trivially 'determined by' a unique a)."""
    if want and plan.fd_pair:
        a, b, _ = plan.fd_pair
        if ref_func_dependency(df, a, b):
            return (a, b)
    cols = plan.columns
    pairs = [(a, b) for a in cols for b in cols if a != b]
    rng.shuffle(pairs)
    for a, b in pairs:
        col_a = df[a].dropna()
        if col_a.nunique() < 2:
            continue
        if not want and col_a.nunique() == len(col_a):
            continue  # a is all-unique -> trivially an FD, not a useful False
        if ref_func_dependency(df, a, b) == want:
            return (a, b)
    return None


def _abs_r(df: pd.DataFrame, a: str, b: str) -> float:
    r = abs(ref_correlation(df, a, b))
    return r if r == r else 1.0  # NaN (constant column) -> treat as unusable


def _uncorrelated_pair(df: pd.DataFrame, cols: list[str],
                       rng: random.Random) -> Optional[tuple[str, str, float]]:
    """A pair with |r| < CONTROL_MAX_ABS_R, taken as the first hit in a
    shuffled scan rather than the single most-uncorrelated pair among all
    C(k,2) pairs in the table. Always taking the argmin made the control
    target's |r| shrink toward 0 as column count (and so pair count) grew
    with tier -- an order-statistics effect: the min of more samples is
    systematically more extreme -- the same mechanism the skewness fix
    addressed. First-match keeps the chosen |r| roughly tier-independent
    (any qualifying pair, not the most extreme one) while leaving the
    existence condition (some pair clears the threshold) unchanged."""
    pairs = [(a, b) for i, a in enumerate(cols) for b in cols[i + 1:]]
    for a, b in rng.sample(pairs, len(pairs)):
        r = _abs_r(df, a, b)
        if r < CONTROL_MAX_ABS_R:
            return (a, b, r)
    return None


def select_queries(df: pd.DataFrame, plan: TablePlan, rng: random.Random,
                   balancer: BooleanBalancer) -> list[dict]:
    tier = plan.tier
    numeric_cols = [c for c in plan.columns if ref_column_dtype(df, c) == "numeric"]
    all_cols = plan.columns
    core: list[dict] = []
    extras: list[dict] = []

    def entry(prop: str, cols: list[str], gt, is_control: Optional[bool] = None) -> dict:
        e = {
            "query_id": prop if not cols else f"{prop}__{'__'.join(cols)}",
            "property": prop,
            "columns": cols,
            "locality": PROPERTY_LOCALITY[prop],
            "ground_truth": gt,
        }
        if is_control is not None:
            e["is_control"] = is_control
        return e

    def add_bool(prop: str, cols: list[str], gt) -> None:
        core.append(entry(prop, cols, bool(gt)))
        balancer.record(tier, prop, bool(gt))

    # --- core: always kept, never trimmed -----------------------------
    core.append(entry("row_count", [], ref_row_count(df)))
    core.append(entry("null_count", [], ref_null_count(df)))

    # column_dtype: cover up to 2 distinct realized types.
    dtype_targets, seen = [], set()
    for c in rng.sample(all_cols, len(all_cols)):
        t = ref_column_dtype(df, c)
        if t not in seen:
            dtype_targets.append(c)
            seen.add(t)
        if len(dtype_targets) == 2:
            break
    for c in dtype_targets or all_cols[:1]:
        core.append(entry("column_dtype", [c], ref_column_dtype(df, c)))

    # correlation: the planted pair (guaranteed |r| >= PLANTED_MIN_ABS_R by
    # build_synthetic_table) if present, else the most-correlated available
    # pair -- but never the functional-dependency pair, whose r is a
    # trivial +/-1 and which is already covered by its own func_dependency
    # query. If that leaves nothing, the correlation core query is dropped
    # and `extras` fills the table back to 10.
    fd_cols = {plan.fd_pair[0], plan.fd_pair[1]} if plan.fd_pair else set()
    if plan.corr_pairs:
        a, b, _ = plan.corr_pairs[0]
        core.append(entry("correlation", [a, b], round(ref_correlation(df, a, b), 4), is_control=False))
    else:
        pool = [c for c in numeric_cols if c not in fd_cols]
        pairs = [(x, y) for i, x in enumerate(pool) for y in pool[i + 1:]]
        scored = [(rr if rr < 1.0 else 0.0, x, y) for x, y in pairs for rr in (_abs_r(df, x, y),)]
        if scored:
            r, x, y = max(scored)
            core.append(entry("correlation", [x, y], round(ref_correlation(df, x, y), 4),
                              is_control=False if r >= PLANTED_MIN_ABS_R else None))

    # is_monotonic / has_outlier / func_dependency: polarity chosen by the
    # balancer, then a column/pair whose *measured* value matches is found.
    want = balancer.want(tier, "is_monotonic", rng, target=IS_MONOTONIC_TARGET_RATE[tier])
    col = (_monotonic_candidate(df, numeric_cols, want, plan, rng)
           or _monotonic_candidate(df, numeric_cols, not want, plan, rng))
    if col is not None:
        add_bool("is_monotonic", [col], ref_is_monotonic(df, col))

    want = balancer.want(tier, "has_outlier", rng)
    col = (_outlier_candidate(df, numeric_cols, want, plan, rng)
           or _outlier_candidate(df, numeric_cols, not want, plan, rng))
    if col is not None:
        add_bool("has_outlier", [col], ref_has_outlier(df, col))

    if numeric_cols:
        c = _skew_candidate(df, numeric_cols, rng) or rng.choice(numeric_cols)
        core.append(entry("skewness", [c], round(ref_skewness(df, c), 4)))

    want = balancer.want(tier, "func_dependency", rng)
    pair = (_fd_candidate(df, plan, want, rng)
            or _fd_candidate(df, plan, not want, rng))
    if pair is not None:
        add_bool("func_dependency", list(pair), ref_func_dependency(df, *pair))

    # --- extras: fill up to the max, trimmed first -------------------
    used_corr = {tuple(sorted(q["columns"])) for q in core if q["property"] == "correlation"}
    ctrl = _uncorrelated_pair(df, numeric_cols, rng)
    if ctrl and tuple(sorted(ctrl[:2])) not in used_corr:
        a, b, _ = ctrl
        extras.append(entry("correlation", [a, b], round(ref_correlation(df, a, b), 4), is_control=True))

    dtype_seen = {q["columns"][0] for q in core if q["property"] == "column_dtype"}
    for c in rng.sample(all_cols, len(all_cols)):
        if c not in dtype_seen:
            extras.append(entry("column_dtype", [c], ref_column_dtype(df, c)))
            break

    for c in rng.sample(numeric_cols, len(numeric_cols)):
        if not any(q["property"] == "skewness" and q["columns"] == [c] for q in core + extras):
            extras.append(entry("skewness", [c], round(ref_skewness(df, c), 4)))
            break

    _, hi = QUERIES_PER_TABLE
    return (core + extras)[:max(hi, len(core))]


# ----------------------------------------------------------------------
# Section 7: record builder
# ----------------------------------------------------------------------


def build_record(df: pd.DataFrame, plan: TablePlan, provenance: dict, object_id: str,
                 source: str, rng: random.Random, balancer: BooleanBalancer) -> dict:
    csv_string = to_csv_string(df)
    queries = select_queries(df, plan, rng, balancer)

    columns_meta = {}
    for c in plan.columns:
        columns_meta[c] = {
            "dtype": ref_column_dtype(df, c),
            "subtype": plan.col_subtype.get(c),
        }

    planted = {
        "correlations": [
            {"columns": [a, b], "target_r": r, "realized_r": round(ref_correlation(df, a, b), 4)}
            for a, b, r in plan.corr_pairs
        ],
        "monotonic": [{"column": c, "direction": d} for c, d in plan.monotonic_cols],
        "outliers": [
            {"column": c, "n": len(pos), "positions": pos}
            for c, pos in provenance.get("outlier_positions", {}).items()
        ],
        "func_dependency": (
            {"a": plan.fd_pair[0], "b": plan.fd_pair[1], "kind": plan.fd_pair[2]}
            if plan.fd_pair
            else None
        ),
    }

    return {
        "object_id": object_id,
        "tier": plan.tier,
        "source": source,
        "n_rows": int(df.shape[0]),
        "n_cols": int(df.shape[1]),
        "csv": csv_string,
        "columns": columns_meta,
        "queries": queries,
        "metadata": {
            "random_seed": SEED,
            "planted": planted,
            "null_count": ref_null_count(df),
            "n_queries": len(queries),
        },
    }


# ----------------------------------------------------------------------
# Section 8: real-data ingestion (pluggable, not auto-fetched)
# ----------------------------------------------------------------------


def load_real_tables(tier: str, n_needed: int, rng: random.Random, start_index: int,
                     balancer: BooleanBalancer) -> list[dict]:
    """Ingest researcher-supplied real tables from real_data_sources/.

    We do not fetch datasets from the internet here: picking a genuinely
    open-license, non-benchmark, unseen-by-training source requires a human
    to check the license and provenance (PDF 5.2's own requirements list is
    exactly this: license, novelty, manual verification). Until files are
    present, this returns an empty list and the caller backfills with
    synthetic tables so the tier still reaches 100 objects.
    """
    if not REAL_MANIFEST_PATH.exists():
        return []
    manifest = json.loads(REAL_MANIFEST_PATH.read_text())
    entries = [e for e in manifest if e.get("tier") == tier]
    rng.shuffle(entries)
    records = []
    for i, entry in enumerate(entries[:n_needed]):
        csv_path = REAL_DATA_DIR / entry["filename"]
        if not csv_path.exists():
            continue
        raw = pd.read_csv(csv_path)
        lo_r, hi_r = TIERS[tier]["rows"]
        lo_c, hi_c = TIERS[tier]["cols"]
        if len(raw.columns) > hi_c:
            keep_cols = rng.sample(list(raw.columns), hi_c)
            raw = raw[keep_cols]
        n_sample = min(len(raw), rng.randint(lo_r, hi_r))
        raw = raw.sample(n=n_sample, random_state=SEED).reset_index(drop=True)
        rename_map = {orig: f"col_{COL_LETTERS[j]}" for j, orig in enumerate(raw.columns)}
        raw = raw.rename(columns=rename_map)

        plan = TablePlan(tier=tier, n_rows=len(raw), columns=list(raw.columns))
        for c in raw.columns:
            dtype = infer_dtype_from_values(raw[c].tolist())
            plan.col_type[c] = dtype
            if dtype == "numeric":
                is_int = raw[c].dropna().apply(lambda v: float(v).is_integer()).all()
                plan.col_subtype[c] = "int" if is_int else "float"

        ok, reason = validity_check(raw, tier)
        if not ok:
            continue

        object_id = f"table_{tier}_real_{start_index + i + 1:03d}"
        record = build_record(raw, plan, {}, object_id, "real", rng, balancer)
        record["metadata"]["real_source"] = {
            "name": entry.get("name"),
            "source_url": entry.get("source_url"),
            "license": entry.get("license"),
            "original_filename": entry.get("filename"),
        }
        records.append(record)
    return records


# ----------------------------------------------------------------------
# Section 9: dataset assembly
# ----------------------------------------------------------------------


def build_dataset(seed: int = SEED) -> tuple[list[dict], BooleanBalancer]:
    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)
    records: list[dict] = []
    balancer = BooleanBalancer()

    for tier, cfg in TIERS.items():
        n_real_target = cfg["n_real"]
        n_synthetic_target = cfg["n_synthetic"]

        real_records = load_real_tables(tier, n_real_target, rng, 0, balancer)
        n_real_got = len(real_records)
        # Backfill any missing real slots with synthetic tables so every
        # tier still reaches exactly 100 objects.
        n_synthetic_needed = n_synthetic_target + (n_real_target - n_real_got)

        synthetic_records = []
        for i in range(n_synthetic_needed):
            df, plan, provenance = build_synthetic_table(tier, rng, np_rng)
            object_id = f"table_{tier}_synthetic_{i + 1:03d}"
            record = build_record(df, plan, provenance, object_id, "synthetic", rng, balancer)
            synthetic_records.append(record)

        records.extend(real_records)
        records.extend(synthetic_records)

    return records, balancer


# ----------------------------------------------------------------------
# Section 10: summary statistics
# ----------------------------------------------------------------------


def stats_for(values: list[float]) -> dict:
    if not values:
        return {"min": None, "max": None, "mean": None, "median": None, "std": None}
    return {
        "min": min(values),
        "max": max(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
    }


def summarize(records: list[dict]) -> dict:
    total = len(records)
    counts_by_tier_source: dict[str, dict[str, int]] = {}
    for r in records:
        counts_by_tier_source.setdefault(r["tier"], {"synthetic": 0, "real": 0})
        counts_by_tier_source[r["tier"]][r["source"]] += 1

    distribution_by_tier = {}
    for tier in TIERS:
        tier_records = [r for r in records if r["tier"] == tier]
        distribution_by_tier[tier] = {
            "n_rows": stats_for([r["n_rows"] for r in tier_records]),
            "n_cols": stats_for([r["n_cols"] for r in tier_records]),
            "n_queries": stats_for([r["metadata"]["n_queries"] for r in tier_records]),
            "csv_length": stats_for([len(r["csv"]) for r in tier_records]),
            "null_count": stats_for([r["metadata"]["null_count"] for r in tier_records]),
        }

    # Boolean base rates -- overall AND per tier. The per-tier split is the
    # one that matters: if it drifts, a fixed-answer model shows a fake
    # difficulty gradient (see BooleanBalancer). These should all sit near
    # 0.50 and be flat across tiers.
    boolean_props = ["is_monotonic", "has_outlier", "func_dependency"]
    base_rates = {}
    base_rates_by_tier = {}
    for prop in boolean_props:
        vals = [q["ground_truth"] for r in records for q in r["queries"] if q["property"] == prop]
        base_rates[prop] = {
            "n": len(vals),
            "true_rate": (sum(vals) / len(vals)) if vals else None,
        }
        for tier in TIERS:
            tv = [
                q["ground_truth"] for r in records if r["tier"] == tier
                for q in r["queries"] if q["property"] == prop
            ]
            base_rates_by_tier[f"{prop}/{tier}"] = {
                "n": len(tv),
                "true_rate": (sum(tv) / len(tv)) if tv else None,
            }

    n_queries_total = sum(r["metadata"]["n_queries"] for r in records)
    corr_control_r = [
        abs(q["ground_truth"])
        for r in records for q in r["queries"]
        if q["property"] == "correlation" and q.get("is_control")
    ]
    corr_planted_r = [
        abs(q["ground_truth"])
        for r in records for q in r["queries"]
        if q["property"] == "correlation" and q.get("is_control") is False
    ]

    return {
        "total": total,
        "counts_by_tier_source": counts_by_tier_source,
        "distribution_by_tier": distribution_by_tier,
        "boolean_base_rates": base_rates,
        "boolean_base_rates_by_tier": base_rates_by_tier,
        "total_queries": n_queries_total,
        "correlation_queries": {
            "planted": len(corr_planted_r),
            "control": len(corr_control_r),
            "planted_abs_r_min": min(corr_planted_r) if corr_planted_r else None,
            "control_abs_r_max": max(corr_control_r) if corr_control_r else None,
            "control_abs_r_mean": statistics.fmean(corr_control_r) if corr_control_r else None,
        },
        "real_data_populated": REAL_MANIFEST_PATH.exists(),
        "random_seed": SEED,
    }


# ----------------------------------------------------------------------
# Section 11: independent verification (does not reuse generation code)
# ----------------------------------------------------------------------


def parse_csv_manual(csv_string: str) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(csv_string))
    rows = list(reader)
    return rows[0], rows[1:]


def verify_column_dtype(values: list[str]) -> str:
    non_null = [v for v in values if v != ""]
    if not non_null:
        return "categorical"
    try:
        for v in non_null:
            float(v)
        return "numeric"
    except ValueError:
        pass
    date_ok = True
    for v in non_null:
        parts = v.split("-")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            date_ok = False
            break
    return "datetime" if date_ok else "categorical"


def verify_correlation(col_a: list[str], col_b: list[str]) -> Optional[float]:
    pairs = [
        (float(a), float(b)) for a, b in zip(col_a, col_b) if a != "" and b != ""
    ]
    if len(pairs) < 2:
        return None
    xs = np.array([p[0] for p in pairs])
    ys = np.array([p[1] for p in pairs])
    if xs.std() < 1e-9 or ys.std() < 1e-9:
        return 0.0
    xm, ym = xs - xs.mean(), ys - ys.mean()
    return float(np.sum(xm * ym) / np.sqrt(np.sum(xm**2) * np.sum(ym**2)))


def verify_is_monotonic(values: list[str]) -> bool:
    nums = [float(v) for v in values if v != ""]
    if len(nums) < 2:
        return True
    inc = all(nums[i] <= nums[i + 1] for i in range(len(nums) - 1))
    dec = all(nums[i] >= nums[i + 1] for i in range(len(nums) - 1))
    return inc or dec


def verify_has_outlier(values: list[str]) -> bool:
    """Independent re-derivation of ref_has_outlier: Tukey fence at
    k=IQR_OUTLIER_K, quartiles by linear interpolation (matches
    numpy/pandas default), no pandas."""
    nums = sorted(float(v) for v in values if v != "")
    n = len(nums)
    if n < 4:
        return False

    def quantile(p: float) -> float:
        idx = p * (n - 1)
        lo = int(idx)
        frac = idx - lo
        if lo + 1 >= n:
            return nums[lo]
        return nums[lo] * (1 - frac) + nums[lo + 1] * frac

    q1, q3 = quantile(0.25), quantile(0.75)
    iqr = q3 - q1
    if iqr < 1e-9:
        return False
    lo_fence, hi_fence = q1 - IQR_OUTLIER_K * iqr, q3 + IQR_OUTLIER_K * iqr
    return any(v < lo_fence or v > hi_fence for v in nums)


def verify_skewness(values: list[str]) -> float:
    nums = [float(v) for v in values if v != ""]
    n = len(nums)
    if n < 3:
        return 0.0
    mean = sum(nums) / n
    m2 = sum((v - mean) ** 2 for v in nums) / n
    m3 = sum((v - mean) ** 3 for v in nums) / n
    if m2 < 1e-12:
        return 0.0
    g1 = m3 / (m2**1.5)
    return (n * (n - 1)) ** 0.5 / (n - 2) * g1 if n > 2 else g1


def verify_func_dependency(col_a: list[str], col_b: list[str]) -> bool:
    groups: dict[str, set] = {}
    for a, b in zip(col_a, col_b):
        if a == "" or b == "":
            continue
        groups.setdefault(a, set()).add(b)
    if len(groups) < 2:
        return False
    return all(len(v) == 1 for v in groups.values())


def run_verification(records: list[dict], sample_size: int = 60) -> dict:
    rng = random.Random(SEED)
    sample = rng.sample(records, min(sample_size, len(records)))
    mismatches = []
    checked = {"row_count": 0, "null_count": 0, "column_dtype": 0, "correlation": 0,
               "is_monotonic": 0, "has_outlier": 0, "skewness": 0, "func_dependency": 0}

    for rec in sample:
        header, rows = parse_csv_manual(rec["csv"])
        col_index = {name: i for i, name in enumerate(header)}
        columns_raw = {name: [row[i] for row in rows] for name, i in col_index.items()}

        for q in rec["queries"]:
            prop = q["property"]
            gt = q["ground_truth"]
            if prop == "row_count":
                got = len(rows)
                checked[prop] += 1
                if got != gt:
                    mismatches.append((rec["object_id"], prop, gt, got))
            elif prop == "null_count":
                got = sum(1 for row in rows for cell in row if cell == "")
                checked[prop] += 1
                if got != gt:
                    mismatches.append((rec["object_id"], prop, gt, got))
            elif prop == "column_dtype":
                col = q["columns"][0]
                got = verify_column_dtype(columns_raw[col])
                checked[prop] += 1
                if got != gt:
                    mismatches.append((rec["object_id"], prop, gt, got))
            elif prop == "correlation":
                a, b = q["columns"]
                got = verify_correlation(columns_raw[a], columns_raw[b])
                checked[prop] += 1
                if got is not None and abs(got - gt) > 0.03:
                    mismatches.append((rec["object_id"], prop, gt, got))
            elif prop == "is_monotonic":
                col = q["columns"][0]
                got = verify_is_monotonic(columns_raw[col])
                checked[prop] += 1
                if got != gt:
                    mismatches.append((rec["object_id"], prop, gt, got))
            elif prop == "has_outlier":
                col = q["columns"][0]
                got = verify_has_outlier(columns_raw[col])
                checked[prop] += 1
                if got != gt:
                    mismatches.append((rec["object_id"], prop, gt, got))
            elif prop == "skewness":
                col = q["columns"][0]
                got = verify_skewness(columns_raw[col])
                checked[prop] += 1
                tol = max(0.08, 0.05 * abs(gt))
                if abs(got - gt) > tol:
                    mismatches.append((rec["object_id"], prop, gt, got))
            elif prop == "func_dependency":
                a, b = q["columns"]
                got = verify_func_dependency(columns_raw[a], columns_raw[b])
                checked[prop] += 1
                if got != gt:
                    mismatches.append((rec["object_id"], prop, gt, got))

    return {
        "n_tables_sampled": len(sample),
        "checked_counts": checked,
        "n_mismatches": len(mismatches),
        "mismatches": mismatches[:30],
    }


# ----------------------------------------------------------------------
# Section 12: spot-check preview
# ----------------------------------------------------------------------


SPOTCHECK_LEGEND = """\
======================================================================
LEGEND -- how to read this file
======================================================================
This is a human-readable preview of a few sample tables from
tabular_exp1_dataset.json -- one block per (tier, source) combination
currently populated (real-data blocks are absent until
real_data_sources/manifest.json exists; see that folder's README).
The CSV shown under each header is exactly the text a model receives.
Ground truth for every property below is always recomputed from that
CSV after the fact -- never taken from what the generator originally
planted -- so a "monotonic" column can still end up is_monotonic=False
if something (e.g. an injected outlier) broke it afterward, and a
"correlated" pair's ground-truth r is the realized sample correlation,
not the target the generator aimed for.

Properties (locality noted in brackets):
  row_count        [local]  data rows, header not counted
  column_dtype     [local]  {numeric, categorical, datetime}, inferred
                             from one column's realized cell values
  null_count       [local]  total blank cells, whole table
  correlation      [global] Pearson r between two numeric columns.
                             "[control]" = an unrelated pair asked
                             alongside a real correlated one (when the
                             table has one) to catch models that always
                             guess "correlated" regardless of the data;
                             "[planted]" = the pair that really was
                             constructed to correlate
  is_monotonic     [global] True iff every consecutive value in the
                             column is non-decreasing, or every one is
                             non-increasing. One out-of-order value
                             (including an injected outlier) is enough
                             to make this False even if the rest of the
                             column climbs or falls steadily throughout
  has_outlier      [global] True iff some value falls outside the
                             column's own Tukey fence: compute Q1 and Q3
                             (the 25th and 75th percentiles) and
                             IQR = Q3 - Q1, then flag anything below
                             Q1 - 3*IQR or above Q3 + 3*IQR. This is the
                             standard "extreme outlier" cutoff; unlike a
                             mean +/- 3*std rule it is well-defined even
                             on short columns (a single extreme point
                             can't inflate the IQR out of its own reach
                             the way it inflates the standard deviation).
                             When True below, the column's Q1/Q3/IQR, the
                             resulting fence, and the exact row/value that
                             crossed it are all shown
  skewness         [global] Pearson's moment skewness of a numeric
                             column (~0 symmetric, >0 long right tail,
                             <0 long left tail) -- a rough direction
                             hint is appended below, purely descriptive
  func_dependency  [global] True iff every row sharing the same value
                             in column A also shares the same value in
                             column B (B is fully determined by A)

"local" properties are readable from one cell/column in isolation;
"global" ones require aggregating the whole column -- comparing model
accuracy on these two groups is this project's central diagnostic (see
phase3_tabular_evaluation/evaluate.py's local-global gap).
======================================================================
"""


def _annotate_query(q: dict, df: pd.DataFrame) -> str:
    """Extra human-readable detail for one query line, beyond the bare
    ground truth -- so has_outlier=True shows which value tripped it,
    skewness shows a direction hint, and correlation shows whether it's
    the planted pair or a control."""
    prop, cols, gt = q["property"], q["columns"], q["ground_truth"]

    if prop == "has_outlier" and gt is True:
        detail = describe_outliers(df, cols[0])
        return f"  <- {detail}" if detail else ""

    if prop == "skewness":
        if gt > 0.2:
            return "  <- right-skewed (long tail toward high values)"
        if gt < -0.2:
            return "  <- left-skewed (long tail toward low values)"
        return "  <- ~symmetric"

    if prop == "correlation" and "is_control" in q:
        return "  [control]" if q["is_control"] else "  [planted]"

    return ""


def write_spotcheck(records: list[dict], path: Path) -> None:
    lines = [SPOTCHECK_LEGEND]
    for tier in TIERS:
        for source in ("synthetic", "real"):
            match = next((r for r in records if r["tier"] == tier and r["source"] == source), None)
            if match is None:
                continue
            df = pd.read_csv(io.StringIO(match["csv"]))
            lines.append(f"=== {match['object_id']} (tier={tier}, source={source}, "
                         f"{match['n_rows']} rows x {match['n_cols']} cols) ===")
            lines.append(match["csv"])
            lines.append(f"queries ({len(match['queries'])}):")
            for q in match["queries"]:
                lines.append(f"  {q['query_id']}: {q['ground_truth']}{_annotate_query(q, df)}")
            lines.append("")
    path.write_text("\n".join(lines))


# ----------------------------------------------------------------------
# Section 13: main
# ----------------------------------------------------------------------


def main() -> None:
    print(f"pandas {pd.__version__}, numpy {np.__version__}, seed={SEED}")

    if not REAL_MANIFEST_PATH.exists():
        print(f"[note] No real-data manifest at {REAL_MANIFEST_PATH.relative_to(HERE)} -- "
              f"generating synthetic-only. See real_data_sources/README.md.")

    records, _balancer = build_dataset(seed=SEED)
    print(f"Built {len(records)} table records "
          f"({sum(1 for r in records if r['source'] == 'synthetic')} synthetic, "
          f"{sum(1 for r in records if r['source'] == 'real')} real)")

    DATASET_PATH.write_text(json.dumps(records, indent=2))
    print(f"Wrote {DATASET_PATH.relative_to(HERE)}")

    summary = summarize(records)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    print(f"Wrote {SUMMARY_PATH.relative_to(HERE)}")

    print("Boolean base rates by tier (target ~0.50, flat):")
    for key, v in summary["boolean_base_rates_by_tier"].items():
        rate = v["true_rate"]
        print(f"  {key:28s} n={v['n']:3d}  true_rate={rate:.2f}" if rate is not None else f"  {key:28s} n=0")
    cq = summary["correlation_queries"]
    print(f"correlation: planted min|r|={cq['planted_abs_r_min']}, "
          f"control max|r|={cq['control_abs_r_max']}, control mean|r|={cq['control_abs_r_mean']}")

    verification = run_verification(records)
    print(f"Verification: sampled {verification['n_tables_sampled']} tables, "
          f"{sum(verification['checked_counts'].values())} property checks, "
          f"{verification['n_mismatches']} mismatches")
    if verification["n_mismatches"]:
        for m in verification["mismatches"][:10]:
            print("  mismatch:", m)

    write_spotcheck(records, SPOTCHECK_PATH)
    print(f"Wrote {SPOTCHECK_PATH.relative_to(HERE)}")

    n_queries = [r["metadata"]["n_queries"] for r in records]
    print(f"Queries per table: min={min(n_queries)} max={max(n_queries)} "
          f"mean={statistics.fmean(n_queries):.2f}")


if __name__ == "__main__":
    main()
