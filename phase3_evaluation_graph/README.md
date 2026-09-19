# Phase 3 — Evaluation (Experiment 1, Domain 2: Graphs)

Evaluation layer for Experiment 1's graph domain. Consumes the Phase 2 model
result logs and produces the accuracy tables, statistical tests, failure
analysis, figures, and the Section 9 decision readout.

**Pipeline position**

```
phase1_dataset_graph/    ->  phase2_model_results_graph/  ->  phase3_evaluation_graph/
300 edge-list graphs         6 models x 8 properties           tables, tests, figures,
+ ground truth                query logs (.json/.jsonl)        decision readout on 6
                                                                (triangle_count/avg_clustering
                                                                 dropped here, see below)
```

Phase 3 reads Phase 2's `*_results.json` logs **in place** from
`../phase2_model_results_graph/`. Nothing is copied or duplicated into this
folder.

**No production data exists yet.** As of this commit, none of the six models
have been queried against a real API (see
`../phase2_model_results_graph/README.md`) — `phase2_model_results_graph/`
has no `*_results.json` files. This notebook's logic was built and verified
against synthetic mock data (schema-correct, realistic error rates, not
committed anywhere in this repo) so that it is ready to run the moment real
Phase 2 output exists; running it for real is just re-executing the cells
against that output.

---

## What is evaluated

| | |
|---|---|
| Objects | 300 graphs (100 simple / 100 medium / 100 hard) |
| Properties | 6 — 2 local (`degree_of_node_0`, `edge_count`) + 4 global (see note below) |
| Models | 5 capped direct-answer models @ 1,800 queries each |
| | DeepSeek-V4-Pro (non-thinking) @ 360 queries (20% stratified subsample) |
| Grading | every remaining property is exact match (integer or boolean) — no numeric-tolerance property remains, see below |
| Statistics | 10,000-resample bootstrap CIs and two-sided tests, seed 0 |

**Local vs global** is the central contrast, same as the geometry domain:
local properties can be read straight off the edge-list text (scan for lines
containing node `0`; count/read the header), global properties must be
computed from the whole graph structure.

**`triangle_count` and `avg_clustering` are dropped from this evaluation
entirely — not re-weighted, not rebalanced, excluded.** The PDF's Table 8
names 8 properties for this domain; this notebook evaluates 6 of them. Both
dropped properties are mathematically guaranteed to be exactly 0 for every
bipartite graph (a triangle is an odd cycle; bipartite graphs have none;
clustering measures triangle density, so it's the same fact twice), and
since `is_bipartite` is deliberately locked at exactly 50/50 in every tier
(`../phase1_dataset_graph/README.md` §1), that handed a blind "always answer
0" strategy a free, tier-flat ~50%+ accuracy floor with no relation to
actual counting or computing ability — V4-Flash was shown to exploit exactly
this (accuracy *rising* with tier as it defaulted to "0" more often on
bigger graphs it couldn't trace by hand). Rebalancing the non-bipartite
half's distribution (done first, see Known Limitations below) fixed the
*pattern* other models showed but not the underlying free-win floor, which
is locked by `is_bipartite`'s own balance and can't be touched without
reopening it. Dropping both properties removes the floor entirely; nothing
in Phase 1 or Phase 2 changed to do this — Cell 3 filters both properties'
records out of `RECS` before any table or figure is built, so the raw
Phase 2 logs on disk still contain them, just unused here.

**This is also why no numeric-tolerance property remains in this domain.**
`avg_clustering` was Graph's *only* relative-error-graded property (Geometry
has 6: `bbox`, `centroid`, `area`, `perimeter`, `aspect_ratio`,
`edge_length_variance`); every other graph property is int or bool with no
meaningful "close but not exact" — `is_bipartite` is either right or wrong,
`chromatic_number` is either right or wrong. Cells 5/6 (the tolerance-band
sweep and median/p90 relative-error tables) and Fig 5 (error-distribution
KDE) are kept as explicit no-op placeholders rather than deleted, so the
notebook's cell numbering stays stable and comparable to earlier committed
runs — they print a one-line note and produce nothing, since there's no
numeric-tolerance property left to report on.

---

## How to run

The notebook `Graph_Experiment1_Phase3.ipynb` contains the same code split
into cells, adapted from
[`../phase3_evaluation/Geometry_Experiment1_Phase3.ipynb`](../phase3_evaluation/Geometry_Experiment1_Phase3.ipynb)
— same statistical machinery (bootstrap CIs, gap analysis, Spearman
consistency, significance tests), this domain's property set and figures.
In Colab, if `../phase2_model_results_graph/` is not present it falls back
to a manual file-upload prompt for the six result logs.

```bash
source .venv/bin/activate   # from the repo root
jupyter notebook phase3_evaluation_graph/Graph_Experiment1_Phase3.ipynb
```

Cell 2 will report "no results found" until at least one model's
`*_results.json` exists under `../phase2_model_results_graph/<NN_model>/`.

---

## Outputs (`results/`, `figures/`)

Created on first run — neither folder is committed yet, since no run has
produced real output.

| File | Contents |
|---|---|
| `results/evaluation_summary.txt` | Headline findings and the Section 9 decision |
| `results/v4flash_failure_analysis.json` | Per-failure heuristic labels + blank `manual_label` field |
| `figures/fig1_accuracy_by_property.png` | Accuracy by property and tier, one panel per model (6 properties) |
| `figures/fig2_accuracy_vs_complexity.png` | Accuracy vs tier for the 4 computed global properties (`is_bipartite`, `is_planar`, `diameter`, `chromatic_number`) |
| `figures/fig3_local_global_gap_heatmap.png` | Local−global gap, models × tier |
| `figures/fig4_accuracy_vs_nodecount.png` | Accuracy vs node count for the same 4 global properties |
| `figures/fig5_baseline_comparison.png` | **Repurposed.** Was `avg_clustering`'s relative-error KDE (that property is dropped, see above); now a gradient-filled bar chart of accuracy vs. majority-class baseline for `is_bipartite`/`is_planar`, built from Cell 7's `CONF` data — each bar's color/label shows its margin above baseline (pale/grey near 0pp, deep green the further above), the dashed line marks the baseline itself, and the shaded band below it is the "no-skill zone"; a bar that barely clears the band (e.g. Qwen3-32B / Llama4-Scout on bipartiteness, +0pp) has no real skill on that property, it's just tracking the majority class |

---

## What differs from the Geometry domain's Phase 3

Everything *structural* (bootstrap methodology, CI level, significance
tests, the local/global gap definition, the Section 9 decision thresholds)
is unchanged — Section 6 of the PDF states these apply uniformly across all
three domains. What's genuinely different:

| | Geometry | Graphs |
|---|---|---|
| Properties | 9 (2 local, 7 global) | 6 (2 local, 4 global) — PDF names 8; `triangle_count`/`avg_clustering` dropped, see above |
| Numeric-tolerance properties | 6 | 0 (was 1, `avg_clustering`, before it was dropped) |
| Boolean properties | 1 (`convex`) | 2 (`is_bipartite`, `is_planar`) |
| Categorical properties | 1 (`orientation`, cw/ccw) | 0 |
| Complexity axis | vertex count (3–40) | node count (6–80) |
| Category field | `shape_type` (3 values) | `family` (5 values) |
| Confusion-matrix cell (7) | 1 boolean + 1 categorical | 2 boolean, same treatment |
| Fig 5 layout | 2×3 grid (6 numeric properties) | single panel (1 numeric property) |

**Intentionally *not* carried over:** Geometry's `vertex_count`
off-by-one-from-the-closing-coordinate adjustment (Fig 3's footnote
mechanism, `offbyplus1_rate`/`adjusted_gap` in Geometry's Cell 13). That is a
specific artifact of WKT's repeated closing coordinate, with no equivalent in
an edge list, so there's nothing analogous to adjust the *gap figure* for.

**A different off-by-one *does* apply to this domain's failure taxonomy,
though:** `diameter` has two common definitions — edges on the longest
shortest path (this dataset's ground truth, `nx.diameter`) vs. nodes on that
path (= edges + 1). Cell 14's `classify_failure()` labels a `diameter` miss
of exactly `absolute_error == 1` as `off_by_one_definition` rather than
`arithmetic_error`, the direct graph-domain instance of Table 14's general
category (whose only PDF-given example is geometry's closing-coordinate
case).

---

## Known limitations (same caveats as Geometry, where applicable)

- **Not yet run against real data.** Every table/figure/number this notebook
  would produce is unverified against an actual model until Phase 2 runs
  complete. The mock-data test run (not committed) only confirms the *code*
  is correct — it says nothing about what the real models will actually do.
- **Failure labels are heuristic**, same discipline as Geometry:
  `classify_failure()` in Cell 14 is a first-pass labeller to speed up the
  manual pass the protocol requires. `manual_label` is deliberately left
  `null` in the exported JSON.
- **This is the second of three domains.** The Section 9 rule needs at least
  2 of 3 domains, so the final go/no-go is deferred until this notebook has
  real results *and* the tabular domain is evaluated.
- **`is_bipartite`/`is_planar`'s accuracy-rises-with-tier artifact went
  through 3 fix iterations before landing on the current design** (see
  `../phase1_dataset_graph/README.md` §1 for the full history):
  1. Balance only *within* `random_bipartite`/`random_planar` (each 50/50
     internally). Left `is_planar`'s *overall* true-rate swinging by tier
     (42%/13%/10%), since the other 4 families incidentally produced planar
     graphs far more often at small `n` than large `n`.
  2. Force every incidental leak back to `False` after generation, keeping
     `random_bipartite`/`random_planar` as the sole source of `True` (family
     sizes 50/25/9/8/8). Fixed tier-flatness but left family sizes far from
     the PDF's balance target, and structurally forbade any cross-family
     correlation.
  3. **Current: spread `True` across all 5 families**, each exactly 20/tier,
     each covering whichever (`is_bipartite`, `is_planar`) quadrants make
     sense for it — `random_bipartite`/`random_planar` stay true to their
     names, `erdos_renyi`/`barabasi_albert`/`watts_strogatz` each
     authentically span all 4 quadrants in their own structural flavor.
     Both properties are exactly 50/50 in every tier, family sizes are
     exactly equal, and genuine cross-family correlation exists (some
     `barabasi_albert` graphs really are bipartite, some `watts_strogatz`
     graphs really are planar).

  The result: Qwen3-32B's and Llama4-Scout's `is_bipartite` accuracy is now
  **exactly 50.0% in every tier** (simple/medium/hard) — the pure
  majority-class baseline at a true 50/50 ground-truth split, with zero
  tier-to-tier drift. `is_planar` shows a small remaining bump on the
  simple tier for several models (e.g. Qwen3-32B 64%→50%→50%,
  GPT-4.1-mini 76%→60%→51%) — with ground truth verified exactly flat
  50/50 in every tier, this is now attributable to the models' own
  answering behavior (e.g. attempting more non-default answers on smaller
  graphs), not to the dataset.
- **V4-Pro (non-thinking)'s subsample needed the same fix, one layer down.**
  A dataset being exactly 50/50 overall doesn't guarantee a random *20%
  draw* from it preserves that balance — an early quadrant-unaware
  subsample drew 4/4 True for one tier's `random_bipartite` quota and 4/4
  True for another's `random_planar` quota, 0 False either time.
  `build_subsample.py` now floors each (`is_bipartite`, `is_planar`)
  quadrant's total share (25 → 5 per tier) and splits it across whichever
  families cover that quadrant by largest remainder, giving the subsample
  the same exact 50/50 balance as the full dataset (verified: 10 True / 10
  False per property per tier, 60/300 total, same as before this fix).
- **`chromatic_number` and `triangle_count` had the same tier-inconsistency
  problem, for a different reason: they're mathematically *derived* from
  `is_bipartite`, not independent of it.** Every bipartite graph has
  `triangle_count = 0` and `chromatic_number = 2` — so once `is_bipartite`
  was locked to exactly 50/tier, those two values were locked to at least
  50/tier too, and the *non-bipartite* half's chromatic_number
  (concentrated on 3/4, rarely 5, never 6) and triangle_count (narrow,
  clustered low) still gave several models a canned-default strategy that
  paid off more on harder tiers: several models converged to guessing
  chromatic_number=4 (or, for V4-Flash's triangle_count, defaulting to 0)
  far more often on medium/hard graphs than simple ones, and since those
  values represented a large chunk of the ground truth, high-volume
  blind guessing swept up more raw hits there than a more hesitant, actually
  -reasoned guess did on simple graphs — see
  `../phase1_dataset_graph/README.md` §1 for the full mechanism. Phase 1
  now spreads chromatic_number evenly across 3/4/5/6 (~12–13 each) among
  the non-bipartite half via two new chromatic-number-exact generators, and
  triangle_count now spans a wide, tier-scaled range instead of clustering
  low. Result: chromatic_number's tier pattern is now sensibly monotonic
  for every model checked (e.g. Qwen3-32B was 31%→24%→31%, now
  22%→17%→15%); Qwen3-32B and Llama4-Scout's triangle_count also flipped
  to the expected decreasing-with-difficulty direction. V4-Flash's
  triangle_count accuracy still *rises* with tier (30%→42%→51%) — verified
  this is now purely the mathematically-unavoidable part: its "guess 0"
  rate climbs from 31 to 86 (of 100) as graphs get harder, nearly
  saturating the tier's locked ~54-graph triangle-free (bipartite) pool by
  the hard tier (51/54 captured) — this can't be balanced away without
  reopening the `is_bipartite` 50/50 split itself.
- **Final resolution: `triangle_count` and `avg_clustering` are dropped from
  this evaluation entirely, `chromatic_number` is kept.** The rebalancing
  above fixed `chromatic_number`'s exploitable *pattern* (no model converged
  on a "guess 2" default in practice, even though the same ~50% floor
  exists there in principle) — so it stays. `triangle_count`/
  `avg_clustering` had a *confirmed, actively exploited* free-win floor
  (V4-Flash) that dataset rebalancing structurally cannot remove without
  reopening `is_bipartite`'s own balance, which was decided against. Both
  properties are simply excluded from Phase 3's tables and figures (Cell 3
  filters them out of `RECS`); Phase 1 and Phase 2 are untouched — the raw
  per-model result logs still contain `triangle_count`/`avg_clustering`
  answers on disk, they're just not read here. See "What is evaluated"
  above for the full reasoning.

---

## Source notebook

Adapted from [`../phase3_evaluation/Geometry_Experiment1_Phase3.ipynb`](../phase3_evaluation/Geometry_Experiment1_Phase3.ipynb).
See that notebook's own README for the original Colab source link.
