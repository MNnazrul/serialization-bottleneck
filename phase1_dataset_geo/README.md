# `phase1_dataset_geo` — Experiment 1, Domain 1 (Geometry), Phase 1

The 300-polygon dataset every model in this experiment is queried on, the
generator that built it, and the 20% stratified subsample used by the V4-Pro
arm.

**Fully offline.** No API keys, no network, no cost.

Reference spec: `../serialization_experiment_1.pdf`, Sections 3, 4.4, 7.

---

## Files

| file | what it is |
|---|---|
| `geometry_exp1_dataset.json` | **the dataset** — 300 records: WKT + 9 ground-truth properties + metadata |
| `geometry_exp1_summary.json` | balance tables, per-tier distributions, published answer-without-reading floors |
| `generate_polygons.py` | the generator. Seeded, deterministic, ships with the data |
| `spotcheck_v7.png` | one polygon per (tier × family), drawn |
| `subsample_20perc.json` | the 60 `object_id`s of the 20% stratified subsample |
| `subsample_20perc_dataset.json` | those same 60 records as a standalone dataset |
| `generate_polygons_stratified_20perc.py` | builds the subsample from the dataset |

## The design

| tier | count | vertices | coordinates |
|---|---|---|---|
| simple | 100 | 3–8 | integers in [0, 1000] |
| medium | 100 | 10–20 | integers in [0, 1000] |
| hard | 100 | 20–40 | integers in [0, 1000] |

The coordinate row does **not** change across tiers. PDF Table 2 pairs the hard
tier with 2-decimal floats while the other two get integers, which bundles a
structural axis with an encoding axis; here only vertex count varies, so a tier
difference is a structural one.

### The 9 properties (PDF Table 4)

| property | type | locality | ground truth |
|---|---|---|---|
| `vertex_count` | int | **local** | count the ring, minus the closing repeat |
| `bbox` | 4×float | **local** | min/max over the ring |
| `orientation` | str | global | sign of the shoelace sum |
| `centroid` | 2×float | global | area-weighted shoelace centroid |
| `area` | float | global | shoelace |
| `perimeter` | float | global | sum of edge lengths |
| `convex` | bool | global | polygon equals its own convex hull |
| `aspect_ratio` | float | global | bbox width ÷ bbox height |
| `edge_length_variance` | float | global | population variance of edge lengths |

`vertex_count` and `bbox` are readable from a bounded scan of the WKT string.
The rest need the whole ring at once. That split is the axis the experiment
measures.

---

## Running

```bash
pip install shapely matplotlib          # the only dependencies

# rebuild the dataset (several minutes; overwrites the committed files)
python generate_polygons.py
python generate_polygons.py --spotcheck-only    # redraw the image only
```

### The 20% subsample

PDF §3 specifies, for the V4-Pro arm: *"Run on a 20% random subsample of all
objects, stratified by tier and shape/type category so that the subsample
preserves the distributional balance of the full dataset."*

```bash
python generate_polygons_stratified_20perc.py --dry-run   # print the plan
python generate_polygons_stratified_20perc.py --verify    # check the committed file
python generate_polygons_stratified_20perc.py --force     # rebuild it
```

`--verify` regenerates from seed 42 and compares against `subsample_20perc.json`;
it should report **60/60 ids match**.

**The allocation**, and what it guarantees:

```
per tier   convex 50 -> 10   (convex_sharp 5, convex_smooth 5)
           concave 25 ->  5
           irregular 25 -> 5
           = 20 per tier, 60 total, exactly 20%

guaranteed per tier:  convex 10 True / 10 False
                      orientation 10 ccw / 10 cw
```

Method: floor the **shape_type total** first, then split across families by
largest remainder. Flooring each small family cell independently rounds several
to zero unevenly and breaks the balance. `shape_type` *is* the `convex`
property (`convex`→True, `concave`/`irregular`→False), so flooring there pins
that boolean for free.

`orientation` needed a fourth stratification level: it is 50/50 per tier
overall but skewed inside each shape (simple/concave is 16 ccw / 9 cw), and it
is graded by exact match against a 50% majority baseline — a 13/7 subsample
would move that baseline to 65% and make the property unreadable.

---

## Before trusting per-property results from this dataset

Two shortcuts are open here, and both are documented so results are not
over-read.

**1. `area` and `perimeter`.** A model answering a constant multiple of the
bounding box, without reading a single vertex, scores:

```
                    simple  medium   hard
area      @10%          57      59     64
perimeter @10%          98      84     64
```

Every model in `../phase2_model_results_geo/` scores **below** those ceilings on
both, so accuracy there is not evidence of computation. The perimeter ceiling
also *declines* across tiers, so part of any perimeter gradient is the shortcut
getting harder rather than the model getting worse.

`geometry_exp1_summary.json` publishes the weaker `k=1` version of these floors
("answer the whole bbox area"), which reads 0/0/0 — the evaluation scores
against that, so its margin-over-floor numbers for those two are optimistic.

**2. `centroid`.** Answering the mean of the serialized coordinates lands inside
the 5% band for 62 / 84 / 88 % of polygons by tier — *rising*, which inverts the
centroid gradient through a Simpson's paradox. Report centroid at the **1%**
band, where that shortcut stops scoring.

---

## Where this is used

```
phase1_dataset_geo/            <- you are here
  -> ../phase2_model_results_geo/   queries the models on it
  -> ../phase3_evaluation_geo/      scores those answers against it
```
