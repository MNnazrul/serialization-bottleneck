# Real-data tables (PDF Section 5.2)

`dataset.py` generates the tabular benchmark synthetic-only until this
folder is populated. Sourcing real tables requires a human to check license
and novelty — that step is intentionally not automated (no URLs are fetched
or guessed by the generator).

## Requirements (from the PDF)

- License permits redistribution: CC0, CC-BY, or public domain.
- Not a common ML benchmark (avoid Iris, Titanic, Wine, Boston Housing,
  Adult Census, etc.) — minimizes the chance a model has memorized it.
- At least 2 numeric columns, no fully-empty rows/columns.

## How to add tables

1. Drop raw CSV files into this folder (e.g. `real_data_sources/foo.csv`).
2. Add one entry per file to `manifest.json` in this folder:

```json
[
  {
    "filename": "foo.csv",
    "tier": "simple",
    "name": "Human-readable dataset name",
    "source_url": "https://...",
    "license": "CC0"
  }
]
```

`tier` controls which tier's row/column-range slot the table competes for
(`simple`, `medium`, or `hard` — see `TIERS` in `dataset.py`). You can supply
more than one candidate per tier; `dataset.py` shuffles and takes the first
`n_real` that pass validity after subsampling.

3. Re-run `python dataset.py`. Columns are automatically renamed to
   `col_A`, `col_B`, ... before serialization (no need to do this yourself).
   Rows are subsampled to fit the tier's size range.
4. Manually verify ground-truth properties for at least 10 of the 50 real
   tables (PDF requirement) — spot-check a few rows and a few property
   values against the source CSV by hand.

Until this folder has a populated `manifest.json`, every tier's real-table
quota is silently backfilled with additional synthetic tables so the
dataset still totals 300 objects (100/tier) — see `tabular_exp1_summary.json`
-> `real_data_populated` to check which mode a given dataset build used.
