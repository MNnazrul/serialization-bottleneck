"""
Replace every table's queries with difficulty-tiered ones.

The original benchmark asked the same wording at every tier and only the table
size changed, so a "hard" question was often no harder than a "simple" one.
Here each of 9 property families has three question variants and a table gets
the variant matching its tier (simple table -> simple question, and so on):

    property         simple                 medium                        hard
    row_count        rows in table          rows where c = v              rows where c = v and f > t
    null_count       nulls in one column    nulls in whole table          nulls in rows where c = v
    column_count     number of columns      number of numeric columns     numeric columns with a null
    is_monotonic     one column             ... within rows where c = v   ... within c = v and f > t
    has_outlier      one column             ... within rows where c = v   any of 3 named columns
    func_dependency  A -> B                 A -> B within rows c = v      (A, B) -> C
    correlation      r, whole table         r within rows c = v           r within c = v and f > t
    skewness         whole column           within rows c = v             within c = v and f > t
    column_agg       max of a column        mean of X where c = v         mean of Z where c = v, f > t

Every property keeps one answer type across tiers (integer / boolean / number),
so scoring is unchanged. Boolean questions are balanced within each
tier (50% true; has_outlier 20% because outliers are rare) (best effort; the achieved rates are printed). Ground truth comes from
dataset.py's reference functions run on the filtered table.

The tables themselves are untouched. Idempotent and seeded per table:

    python build_tiered_queries.py
"""

from __future__ import annotations

import io
import json
import random
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

import dataset as D

HERE = Path(__file__).resolve().parent
DATASET_PATH = HERE / "tabular_exp1_dataset.json"
SEED = 42
TRIES = 300
NOTE = " Ignore missing values."
LOCAL = {"row_count", "null_count", "column_count"}
BOOL_PROPS = {"is_monotonic", "has_outlier", "func_dependency"}
# has_outlier: true is rare in the data (only planted columns), so 50% is unreachable at
# simple tier; use the same ~20% at every tier so the base rate does not vary with tier.
TARGET_TRUE_RATE = {"has_outlier": 0.2}
PROPS = ["row_count", "null_count", "column_count", "is_monotonic", "has_outlier",
         "func_dependency", "correlation", "skewness", "column_agg"]


def g(x: float) -> str:
    return f"{x:g}"


class Ctx:
    def __init__(self, rec: dict, rng: random.Random):
        self.rec, self.rng = rec, rng
        self.df = pd.read_csv(io.StringIO(rec["csv"]))
        meta = rec["columns"]
        self.cats = [c for c, m in meta.items() if m["dtype"] == "categorical"]
        self.nums = [c for c, m in meta.items() if m["dtype"] == "numeric"]
        self.dates = [c for c, m in meta.items() if m["dtype"] == "datetime"]
        self.n = len(self.df)

    def has_null(self, *cols) -> bool:
        return any(self.df[c].isna().any() for c in cols)

    def group(self, min_rows: int):
        """random (col, value, mask) with at least min_rows matching rows"""
        opts = []
        for c in self.cats:
            for v, k in self.df[c].value_counts().items():
                if min_rows <= k <= self.n - 1:
                    opts.append((c, str(v)))
        if not opts:
            return None
        c, v = self.rng.choice(opts)
        return c, v, (self.df[c].astype(str) == v)

    def threshold(self, f: str) -> float:
        return round(float(self.df[f].median()), 2)


def out(prop, cols, truth, question, params, tier):
    return dict(query_id=prop, property=prop, columns=cols, locality="local" if prop in LOCAL else "global",
                ground_truth=truth, question=question, params=params, variant=tier)


def q_row_count(x: Ctx, tier: str):
    if tier == "simple":
        return out("row_count", [], x.n, "How many rows does this table have? Do not count the header row.", {}, tier)
    gr = x.group(2)
    if not gr:
        return None
    c, v, m = gr
    if tier == "medium":
        return out("row_count", [c], int(m.sum()), f'How many rows have {c} equal to "{v}"?', {"col": c, "value": v}, tier)
    if not x.nums:
        return None
    f = x.rng.choice(x.nums)
    t = x.threshold(f)
    k = int((m & (x.df[f] > t)).sum())
    if k < 1:
        return None
    return out("row_count", [c, f], k, f'How many rows have {c} equal to "{v}" and {f} greater than {g(t)}?',
               {"cat": c, "value": v, "filter": f, "threshold": t}, tier)


def q_null_count(x: Ctx, tier: str):
    if tier == "simple":
        withnull = [c for c in x.df.columns if x.df[c].isna().any()]
        c = x.rng.choice(withnull or list(x.df.columns))
        return out("null_count", [c], int(x.df[c].isna().sum()), f"How many missing values are in column {c}?", {"col": c}, tier)
    if tier == "medium":
        return out("null_count", [], int(x.df.isna().sum().sum()),
                   "How many null or missing values are in this table in total?", {}, tier)
    gr = x.group(2)
    if not gr:
        return None
    c, v, m = gr
    return out("null_count", [c], int(x.df[m].isna().sum().sum()),
               f'How many missing cells are there in total across the rows where {c} equals "{v}"?',
               {"cat": c, "value": v}, tier)


def q_column_count(x: Ctx, tier: str):
    if tier == "simple":
        return out("column_count", [], int(x.df.shape[1]), "How many columns does this table have?", {}, tier)
    if tier == "medium":
        return out("column_count", [], len(x.nums),
                   "How many columns contain numbers (not dates and not text codes)?", {}, tier)
    k = sum(1 for c in x.nums if x.df[c].isna().any())
    return out("column_count", [], k,
               "How many columns contain numbers (not dates and not text codes) and have at least one missing value?", {}, tier)


def q_is_monotonic(x: Ctx, tier: str):
    cols = x.nums + x.dates
    if not cols:
        return None
    X = x.rng.choice(cols)
    if tier == "simple":
        q = f"Is column {X} monotonically increasing or decreasing? Answer true or false."
        if x.has_null(X):
            q += NOTE
        return out("is_monotonic", [X], D.ref_is_monotonic(x.df, X), q, {"col": X}, tier)
    gr = x.group(4)
    if not gr:
        return None
    c, v, m = gr
    if tier == "medium":
        sub = x.df[m]
        q = (f'Considering only the rows where {c} equals "{v}" (keep their original order), is column {X} '
             "monotonically increasing or decreasing? Answer true or false.")
        cs = [c, X]
        prm = {"cat": c, "value": v, "col": X}
    else:
        if not x.nums:
            return None
        f = x.rng.choice(x.nums)
        t = x.threshold(f)
        sub = x.df[m & (x.df[f] > t)]
        if len(sub) < 3:
            return None
        q = (f'Considering only the rows where {c} equals "{v}" and {f} is greater than {g(t)} (keep their '
             f"original order), is column {X} monotonically increasing or decreasing? Answer true or false.")
        cs = [c, f, X]
        prm = {"cat": c, "value": v, "filter": f, "threshold": t, "col": X}
    if sub[X].dropna().shape[0] < 3:
        return None
    if x.has_null(*cs):
        q += NOTE
    return out("is_monotonic", cs, D.ref_is_monotonic(sub, X), q, prm, tier)


def _outlier_text(col):
    return (f"any value below Q1 - 3*IQR or above Q3 + 3*IQR (where IQR = Q3 - Q1 is the interquartile range)")


def q_has_outlier(x: Ctx, tier: str):
    if not x.nums:
        return None
    if tier == "simple":
        X = x.rng.choice(x.nums)
        q = f"Does column {X} contain any outliers, i.e. {_outlier_text(X)}? Answer true or false."
        if x.has_null(X):
            q += NOTE
        return out("has_outlier", [X], D.ref_has_outlier(x.df, X), q, {"col": X}, tier)
    if tier == "medium":
        gr = x.group(6)
        if not gr:
            return None
        c, v, m = gr
        X = x.rng.choice(x.nums)
        q = (f'Considering only the rows where {c} equals "{v}", does column {X} contain any outliers, i.e. '
             f"{_outlier_text(X)} computed on those rows only? Answer true or false.")
        if x.has_null(c, X):
            q += NOTE
        return out("has_outlier", [c, X], D.ref_has_outlier(x.df[m], X), q, {"cat": c, "value": v, "col": X}, tier)
    k = min(3, len(x.nums))
    cols = x.rng.sample(x.nums, k)
    names = ", ".join(cols[:-1]) + (" or " if k > 1 else "") + cols[-1]
    q = (f"Does any of the columns {names} contain an outlier, i.e. {_outlier_text(cols)} "
         "(computed separately for each column)? Answer true or false.")
    if x.has_null(*cols):
        q += NOTE
    return out("has_outlier", cols, any(D.ref_has_outlier(x.df, c) for c in cols), q, {"cols": cols}, tier)


def _two_cols(x: Ctx):
    cols = list(x.df.columns)
    if len(cols) < 2:
        return None
    return x.rng.sample(cols, 2)


def q_func_dependency(x: Ctx, tier: str):
    if tier == "simple":
        p = _two_cols(x)
        if not p:
            return None
        a, b = p
        q = f"Is column {b} a deterministic function of column {a}? Answer true or false."
        if x.has_null(a, b):
            q += NOTE
        return out("func_dependency", [a, b], D.ref_func_dependency(x.df, a, b), q, {"a": a, "b": b}, tier)
    if tier == "medium":
        gr = x.group(5)
        p = _two_cols(x)
        if not gr or not p:
            return None
        c, v, m = gr
        a, b = p
        q = (f'Considering only the rows where {c} equals "{v}", is column {b} a deterministic function of '
             f"column {a}? Answer true or false.")
        if x.has_null(c, a, b):
            q += NOTE
        return out("func_dependency", [c, a, b], D.ref_func_dependency(x.df[m], a, b), q,
                   {"cat": c, "value": v, "a": a, "b": b}, tier)
    cols = list(x.df.columns)
    if len(cols) < 3:
        return None
    a1, a2, b = x.rng.sample(cols, 3)
    sub = x.df[[a1, a2, b]].dropna()
    if len(sub) < 4:
        return None
    truth = bool(sub.groupby([a1, a2])[b].nunique().max() == 1) and sub[[a1, a2]].drop_duplicates().shape[0] >= 2
    q = (f"Is column {b} a deterministic function of the combination of columns {a1} and {a2} "
         f"(every distinct pair of values in {a1} and {a2} maps to exactly one value of {b})? Answer true or false.")
    if x.has_null(a1, a2, b):
        q += NOTE
    return out("func_dependency", [a1, a2, b], truth, q, {"a1": a1, "a2": a2, "b": b}, tier)


def _corr(sub, a, b):
    s = sub[[a, b]].dropna()
    if len(s) < 4 or s[a].std() < 1e-9 or s[b].std() < 1e-9:
        return None
    return round(float(s[a].corr(s[b])), 4)


def q_correlation(x: Ctx, tier: str):
    if len(x.nums) < 2:
        return None
    a, b = x.rng.sample(x.nums, 2)
    base = f"What is the Pearson correlation coefficient between {a} and {b}"
    if tier == "simple":
        r = _corr(x.df, a, b)
        q, cs, prm, sub = base + "?", [a, b], {"a": a, "b": b}, x.df
    else:
        gr = x.group(6 if tier == "medium" else 5)
        if not gr:
            return None
        c, v, m = gr
        if tier == "medium":
            sub = x.df[m]
            q = base + f' within the rows where {c} equals "{v}"?'
            cs, prm = [c, a, b], {"cat": c, "value": v, "a": a, "b": b}
        else:
            others = [n for n in x.nums if n not in (a, b)] or x.nums
            f = x.rng.choice(others)
            t = x.threshold(f)
            sub = x.df[m & (x.df[f] > t)]
            q = base + f' within the rows where {c} equals "{v}" and {f} is greater than {g(t)}?'
            cs, prm = [c, f, a, b], {"cat": c, "value": v, "filter": f, "threshold": t, "a": a, "b": b}
        r = _corr(sub, a, b)
    if r is None:
        return None
    if x.has_null(*cs):
        q += NOTE
    return out("correlation", cs, r, q, prm, tier)


def q_skewness(x: Ctx, tier: str):
    if not x.nums:
        return None
    X = x.rng.choice(x.nums)
    base = f"What is the skewness of column {X}"
    if tier == "simple":
        sub, q, cs, prm = x.df, base + "?", [X], {"col": X}
    else:
        gr = x.group(5)
        if not gr:
            return None
        c, v, m = gr
        if tier == "medium":
            sub, q = x.df[m], base + f' within the rows where {c} equals "{v}"?'
            cs, prm = [c, X], {"cat": c, "value": v, "col": X}
        else:
            others = [n for n in x.nums if n != X] or x.nums
            f = x.rng.choice(others)
            t = x.threshold(f)
            sub = x.df[m & (x.df[f] > t)]
            q = base + f' within the rows where {c} equals "{v}" and {f} is greater than {g(t)}?'
            cs, prm = [c, f, X], {"cat": c, "value": v, "filter": f, "threshold": t, "col": X}
    s = sub[X].dropna()
    if len(s) < 4 or s.std() < 1e-9:
        return None
    if x.has_null(*cs):
        q += NOTE
    return out("skewness", cs, round(D.ref_skewness(sub, X), 4), q, prm, tier)


def q_column_agg(x: Ctx, tier: str):
    if not x.nums:
        return None
    X = x.rng.choice(x.nums)
    if tier == "simple":
        q = f"What is the maximum value in column {X}?"
        if x.has_null(X):
            q += NOTE
        return out("column_agg", [X], float(x.df[X].max()), q, {"col": X}, tier)
    gr = x.group(3)
    if not gr:
        return None
    c, v, m = gr
    if tier == "medium":
        sel = x.df.loc[m, X].dropna()
        if len(sel) < 2:
            return None
        q = f'What is the mean of column {X} over the rows where {c} equals "{v}"? Round to 2 decimals.'
        cs, prm = [c, X], {"cat": c, "value": v, "col": X}
    else:
        if len(x.nums) < 2:
            return None
        f, Z = x.rng.sample(x.nums, 2)
        t = x.threshold(f)
        sel = x.df.loc[m & (x.df[f] > t), Z].dropna()
        if len(sel) < 3:
            return None
        q = (f'Among the rows where {c} is "{v}" and {f} is greater than {g(t)}, what is the mean of {Z}? '
             "Round to 2 decimals.")
        cs, prm = [c, f, Z], {"cat": c, "value": v, "filter": f, "threshold": t, "target": Z}
    if x.has_null(*cs):
        q += NOTE
    return out("column_agg", cs, round(float(sel.mean()), 2), q, prm, tier)


BUILDERS = {"row_count": q_row_count, "null_count": q_null_count, "column_count": q_column_count,
            "is_monotonic": q_is_monotonic, "has_outlier": q_has_outlier, "func_dependency": q_func_dependency,
            "correlation": q_correlation, "skewness": q_skewness, "column_agg": q_column_agg}


def build_query(rec: dict, prop: str, want: bool | None):
    x = Ctx(rec, random.Random(f"{SEED}-{rec['object_id']}-{prop}"))
    fallback = None
    for _ in range(TRIES):
        q = BUILDERS[prop](x, rec["tier"])
        if q is None:
            continue
        if want is None or q["ground_truth"] == want:
            return q
        fallback = fallback or q
    return fallback


def main() -> None:
    records = json.loads(DATASET_PATH.read_text())
    toggle = defaultdict(int)
    missing = defaultdict(list)
    for rec in records:
        queries = []
        for prop in PROPS:
            want = None
            if prop in BOOL_PROPS:
                toggle[(rec["tier"], prop)] += 1
                k, r = toggle[(rec["tier"], prop)], TARGET_TRUE_RATE.get(prop, 0.5)
                want = int(k * r) > int((k - 1) * r)
            q = build_query(rec, prop, want)
            if q is None:
                missing[prop].append(rec["object_id"])
            else:
                queries.append(q)
        rec["queries"] = queries
        rec["metadata"]["n_queries"] = len(queries)
    DATASET_PATH.write_text(json.dumps(records, indent=2))

    print(f"queries: {sum(len(r['queries']) for r in records)} over {len(records)} tables")
    print("tables missing a property:", {k: len(v) for k, v in missing.items()} or "none")
    print("boolean true-rate by tier:")
    for prop in sorted(BOOL_PROPS):
        rates = []
        for t in ("simple", "medium", "hard"):
            gts = [q["ground_truth"] for r in records if r["tier"] == t for q in r["queries"] if q["property"] == prop]
            rates.append(f"{t} {np.mean(gts):.0%} (n={len(gts)})" if gts else f"{t} -")
        print(f"  {prop:16s}" + " | ".join(rates))


if __name__ == "__main__":
    main()
