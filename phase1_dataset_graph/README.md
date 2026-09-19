# Phase 1 — Domain 2 (Graphs) Dataset Generation

Documentation for `Graph_Experiment1_Phase1.ipynb`: what it builds, how it
builds it, what it guarantees, and what a person porting this to a new domain
has to replace.

Reference spec: `serialization_experiment_1.pdf`, Section 4.

**Setup first.** Virtual environment, dependencies (including `networkx`,
added to the root `requirements.txt` for this domain), and API keys are
covered once in the [root README](../Readme.md#setup). This document assumes
that is done.

---

## 1. What Phase 1 produces

Phase 1 is the **data generation and ground-truth stage**. It emits no model
calls — it only builds the benchmark that Phase 2 queries.

| File | Contents |
|------|----------|
| `graph_exp1_dataset.json` | 300 graph records: edge-list string + 8 ground-truth properties + metadata |
| `graph_exp1_summary.json` | Summary statistics (counts, per-tier distributions, bipartite/planar balance, cross-family overlap breakdown) |
| `spotcheck_exp1_graph.png` | 3×8 grid, one graph per (tier × family/quadrant column), for visual sanity |

This is the second domain of Experiment 1, sibling to
[`phase1_dataset/`](../phase1_dataset/README.md) (Geometry). It follows the
same methodology — tiered rejection sampling against a named validity rule
set, a presentation-independent/presentation-dependent property split,
independent verification from the serialized string — adapted to graphs.

### Design matrix

| Tier | Count | Nodes | Purpose |
|------|-------|-------|---------|
| simple | 100 | 6–15  | Baseline; small enough for manual verification |
| medium | 100 | 16–40 | Core measurement |
| hard   | 100 | 41–80 | Stress test |

**Revision note.** `is_bipartite` and `is_planar` are each generated as an
**exact 50/50 split, in every tier**, instead of the PDF's literal "aim for
approximately 25–35%" target — and **all 5 Table-7 families are exactly
equal in size** (20/tier each), tighter than even the PDF's own "aim for
18–22" guidance. Both come from one design: every family covers whichever
(`is_bipartite`, `is_planar`) quadrant combinations make sense for it,
instead of concentrating "True" into one or two dedicated families:

| Family | Q1 (bip∧planar) | Q2 (bip∧¬planar) | Q3 (¬bip∧planar) | Q4 (¬bip∧¬planar) | Total |
|---|---|---|---|---|---|
| `random_bipartite` | 10 | 10 | — | — | 20 |
| `random_planar` | — | — | 20 | — | 20 |
| `erdos_renyi` | 5 | 5 | 2 | 8 | 20 |
| `barabasi_albert` | 5 | 5 | 2 | 8 | 20 |
| `watts_strogatz` | 5 | 5 | 1 | 9 | 20 |
| **Quadrant total** | **25** | **25** | **25** | **25** | **100** |

`random_bipartite` stays true to its name (always bipartite — Q1/Q2 only);
`random_planar` stays true to its name (always planar — Q3 only);
`erdos_renyi`/`barabasi_albert`/`watts_strogatz` cover everything else, each
with its own characteristic way of hitting every quadrant (§2) — some
`barabasi_albert` graphs are genuinely bipartite, some `watts_strogatz`
graphs are genuinely planar, by deliberate design rather than incidental
chance. `is_bipartite` (= Q1+Q2 = 50) and `is_planar` (= Q1+Q3 = 50) are
exactly 50/50 in every tier as a direct consequence — counting every graph
regardless of family, not just within any one "designed" family.

**Why this design, not a simpler one.** Two earlier iterations were tried
and superseded:

1. Balance only *within* `random_bipartite`/`random_planar` (each 50/50
   internally, other 3 families untouched at their natural, unequal sizes).
   This left `is_planar`'s *overall* true-rate swinging by tier (42% simple
   / 13% medium / 10% hard), because small sparse graphs from the other 4
   families are incidentally planar far more often than large ones —
   exactly the majority-class-guessing artifact this whole redesign exists
   to remove, one level down.
2. Force every incidental leak back to `False` after generation (add edges
   until the "wrong" property flips), keeping `random_bipartite`/
   `random_planar` as the sole source of `True` and family sizes unequal
   (50/25/9/8/8). This closed the tier-flatness gap but left family sizes
   far from the PDF's balance target, and structurally *can't* produce
   cross-family correlation (an `erdos_renyi` graph could never be
   bipartite or planar under that design, by construction).

The current design resolves both at once: spreading `True` across all 5
families removes the family-size/boolean-balance tradeoff entirely (there
is no longer a "dedicated" family that must grow to supply an exact count),
and it produces genuine cross-family correlation as a side effect, not a
bug.

**`chromatic_number` and `triangle_count` are also balanced, not just
`is_bipartite`/`is_planar`.** Both are mathematically *derived* from
`is_bipartite`, not independent: every bipartite graph has `triangle_count
= 0` (bipartite means no odd cycles, and a triangle is a 3-cycle) and every
connected bipartite graph with an edge has `chromatic_number = 2`
(2-colorable iff bipartite). Since `is_bipartite=True` is locked at exactly
50/tier, `chromatic_number = 2` and `triangle_count = 0` are locked at
*at least* 50/tier too — that's not a generation artifact, it's a
consequence of the boolean balance above, and can't be changed without
undoing it. What *is* controllable is the other 50 (the non-bipartite
half):

- **`chromatic_number`** is split evenly across 3, 4, 5, 6 (~12–13 each,
  50 total) instead of whatever falls out naturally (an earlier build
  concentrated heavily on 3/4 and rarely reached 5, never 6). Q3
  (¬bipartite∧planar, capped at ≤4 by the four-color theorem) supplies
  values 3 and 4; Q4 (¬bipartite∧¬planar, no such cap) supplies 5 and 6.
- **`triangle_count`** is spread widely instead of clustering near 0 (an
  earlier build had ~53% of every tier at exactly 0 counting only the
  locked bipartite half, but the *non-bipartite* half was itself narrow
  and skewed low). Now it ranges from single digits to the hundreds within
  a tier, scaled to that tier's node-count range.

This needed real construction changes for Q3/Q4, described in §2 Cell 5 —
`random_planar`'s Q3 slots and all of `erdos_renyi`/`barabasi_albert`/
`watts_strogatz`'s Q4 slots switch to chromatic-number-exact generators
(losing their distinct structural "flavor" for *those* slots specifically,
since precise chromatic control needs a shared technique); `erdos_renyi`/
`barabasi_albert`/`watts_strogatz`'s (much smaller) Q3 slots keep their
original flavored construction unchanged, since it already reliably
produces `chromatic_number=3` and can stay as extra diversity within that
bucket. Family sizes are untouched by any of this — still exactly 20/tier
each.

### The 8 properties (Table 8)

| Property | Type | Locality | Ground Truth | Eval |
|----------|------|----------|---------------|------|
| `degree_of_node_0` | int | local | `G.degree(0)` | exact |
| `edge_count` | int | local | `G.number_of_edges()` | exact |
| `triangle_count` | int | global | `sum(nx.triangles(G).values()) // 3` | exact |
| `is_bipartite` | bool | global | `nx.is_bipartite(G)` | exact |
| `is_planar` | bool | global | `nx.check_planarity(G)[0]` | exact |
| `diameter` | int | global | `nx.diameter(G)` | exact |
| `chromatic_number` | int | global | exact computation (§4) | exact |
| `avg_clustering` | float | global | `nx.average_clustering(G)` | rel. error |

`degree_of_node_0` and `edge_count` are "local" because both are readable
from a bounded window of the edge-list string: scan for lines containing `0`
as an endpoint, or read/count lines. The other six require reasoning over
the whole structure — this local/global split is the axis the experiment is
built to measure (implicit-structure bottleneck).

---

## 2. Notebook walkthrough

The notebook has 18 cells in 8 sections. Cells must run in order — later
cells depend on names defined earlier, and cell 11 is the one that actually
runs generation.

### Cell 1 — Phase 0: environment
Installs `networkx` + `scipy` + `matplotlib`, imports, prints the NetworkX
version. Written for Colab (`!pip install`), works locally too.

### Cell 3 — Section 1: tiers, family/quadrant plan, validity

`TIERS` holds `(vmin, vmax)` node-count bounds per tier — no coordinate axis
here, unlike geometry; a graph's complexity axis is node count alone.

`FAMILY_QUADRANT_PLAN` is the design-matrix table above, as data: for each
family, how many of its 20/tier instances go to each quadrant it covers.
Three `assert`s check the plan is internally consistent (every family sums
to 20, every quadrant sums to 25, the grand total is 100) before generation
ever runs. `check_validity(G, vmin, vmax)` is the Section 4.2 gate,
returning `(True, None)` or `(False, reason)`:

| Rule | Rejection reason | Check |
|------|-------------------|-------|
| 1 | `node_count_out_of_range` | `vmin <= n <= vmax` |
| 2 | `self_loop` | no self-loops |
| 3 | *(structural)* | no multi-edges — guaranteed by using `nx.Graph`, never `nx.MultiGraph` |
| 4 | `not_connected` | single connected component |
| 5 | `too_few_edges` | `m >= n - 1` |
| 6 | `too_many_edges` | `m <= C(n,2)/2` |

### Cell 5 — Section 2: the quadrant generators, by family

Every generator is a **rejection sampler or a construction that retries on
a fresh `n`** when its target quadrant can't be hit: draw `n` and
family/quadrant-specific parameters, build a candidate, run
`check_validity` plus the quadrant's own boolean checks, retry up to
`max_tries` (300–3000 depending on the generator).

**Generic (used directly by `random_bipartite`/`random_planar`, and reused
as `erdos_renyi`'s own Q1/Q2 flavor — independent edge probability is
already Erdos-Renyi's defining trait):**

- **`gen_bipartite_planar`** (Q1) — a bipartite spanning tree with extra
  cross-partition edges added one at a time, kept only if
  `nx.check_planarity` still passes. *Why constructive, not rejection
  sampling:* bipartite planar graphs are capped at `m <= 2n-4` edges, a much
  tighter bound than general planar graphs (`3n-6`), so a randomly drawn
  bipartite graph is planar only by chance, and that chance drops fast as
  `n` grows. Measured before committing to this method: pure rejection
  sampling succeeded only 37/75 times across the 3 tiers, against 75/75 for
  construction, and ran ~12x slower even at that reduced rate.
- **`gen_bipartite_nonplanar`** (Q2) — `nx.bipartite.random_graph(n1, n2,
  p)` with `n1 + n2 = n` and the ratio **between the two partitions**,
  `n1/n2`, constrained to `[0.3, 0.7]` (PDF Table 7 — a ratio between
  partition sizes, not either partition's share of `n`; found by exact
  integer search over `n`'s valid splits rather than rounding a continuous
  target, since rounding can overshoot the bound for small `n`), density
  `p in [0.2, 0.45]`, rejection-sampled for `is_planar() == False`.
- **`gen_nonbipartite_planar`** (Q3 building block) — Delaunay triangulation
  of random points (always planar — any subgraph of a planar graph is
  planar too), thinned toward a target edge count within the `3n-6` bound
  (reverting any removal that would disconnect the graph), rejection-sampled
  for `is_bipartite() == False`. `random_planar`'s 20 Q3 slots don't call
  this directly any more — see `gen_q3_chromatic_exact` below, which
  rejection-samples on top of it for an exact chromatic number.
- **`gen_nonbipartite_nonplanar`** (Q4 building block) — Erdos-Renyi
  (expected degree 4–7) / Barabasi-Albert (`m in {3,4}`) / Watts-Strogatz
  (`k in {4,6}`, `p in {0.1,0.3,0.5}`) at densities chosen so the rejection
  condition (`not bipartite and not planar`) is easy to satisfy,
  rejection-sampled for both booleans `False`. No longer called by
  `erdos_renyi`/`barabasi_albert`/`watts_strogatz`'s Q4 slots (25/tier) —
  see `gen_clique_anchor_chromatic` below, which replaced it there to get
  an exact chromatic number (this function is kept only as documentation
  of what Q4 originally looked like, and because `gen_q3_chromatic_exact`
  above still needs its Q3 counterpart).

**Chromatic-number-exact generators**, used to spread `chromatic_number`
evenly across 3/4/5/6 among the non-bipartite half (see the Design Matrix
note above) — both verify the target was actually hit via
`exact_chromatic_number` (Cell 7) and retry on mismatch, rather than
trusting construction alone:

- **`gen_q3_chromatic_exact(rng, vmin, vmax, target_k)`** — calls
  `gen_nonbipartite_planar` and rejection-samples on the result's exact
  chromatic number until it matches `target_k` (3 or 4). Reuses the
  existing Delaunay generator rather than a fresh construction: Delaunay
  graphs are already naturally triangle-rich and vary a lot in density, so
  this gives good triangle-count diversity for free. *Why not a
  clique-anchor construction here too:* tested and rejected — attaching
  even a few "bonus" cliques (see below) to a large clique-anchor graph
  reliably breaks planarity once `n` gets into the medium/hard range (the
  added structure creates a K5/K3,3 minor), so it can't be used wherever
  planarity must hold.
- **`gen_clique_anchor_chromatic(rng, vmin, vmax, k)`** — a `K_k` anchor
  clique (forcing the clique number, hence chromatic number, to at least
  `k`) with the remaining nodes attached as a random tree, plus a random
  number of **disjoint** bonus `k`-cliques grafted onto separate tree nodes
  to vary triangle count without changing the chromatic number (gluing
  same-size cliques by bridges is a classical "block graph" construction
  with chromatic number equal to the largest block — verified after
  construction here, not just assumed, since blocks sharing more than one
  node by chance can break this). `K5`/`K6` anchors are automatically
  non-planar (Kuratowski), so this satisfies Q4's not-planar requirement
  for free — used for `erdos_renyi`/`barabasi_albert`/`watts_strogatz`'s
  Q4 slots, targeting `k=5` and `k=6`.

**`erdos_renyi`'s own Q3 flavor** — `gen_er_nonbip_planar`: a
**uniformly-random-attachment tree** (each new node attaches to a uniformly
random existing node — the ER-flavored way to grow a tree, as opposed to
preferential attachment) plus one intra-partition edge, kept only if the
graph is still planar. Trees are always bipartite; in a connected bipartite
graph every path between two same-side vertices has even length, so closing
one with an extra edge forces an odd cycle — deterministic, no rejection
sampling needed on the property itself.

**`barabasi_albert`'s own quadrant flavors**, all built on preferential
attachment:

- **`gen_ba_bip_planar`** (Q1) — `m=1` Barabasi-Albert is always a **tree**,
  hence always bipartite and planar, with zero forcing needed.
- **`gen_ba_bip_nonplanar`** (Q2) — a hub-skewed bipartite graph: a spanning
  tree biased toward a small set of "hub" nodes on each side, then extra
  edges added preferentially to those hubs until non-planar — mimicking
  BA's hub-heavy degree distribution in a genuinely bipartite graph
  (networkx has no native bipartite BA variant).
- **`gen_ba_nonbip_planar`** (Q3) — a BA tree (`m=1`, preferential
  attachment, unlike `erdos_renyi`'s uniform-attachment tree above) plus
  one intra-partition edge, kept only if still planar.

**`watts_strogatz`'s own quadrant flavors**, all exploiting **cycle
parity**: an even-length cycle is automatically bipartite (alternating
parity) and planar; an odd-length cycle is automatically non-bipartite (one
odd cycle: itself) and planar. WS's own signature move (rewiring) is
layered on top, kept only while the target quadrant still holds:

- **`gen_ws_bip_planar`** (Q1) — even cycle + a few cross-parity rewires,
  each kept only if planarity survives.
- **`gen_ws_bip_nonplanar`** (Q2) — even cycle + cross-parity rewires added
  until non-planar.
- **`gen_ws_nonbip_planar`** (Q3) — a plain odd cycle — non-bipartite and
  planar with no rewiring needed at all.

All generators pass the shared `rng` (a single `random.Random(seed)`
instance) directly as NetworkX's `seed` parameter — NetworkX accepts a
`random.Random` instance natively, so one seed stream drives every
generator's parameter draws, every planarity-check retry, and the later
relabeling step, exactly as geometry drives everything from one `rng`.

### Cell 7 — Section 3: chromatic number

NetworkX has no exact chromatic-number function. The implementation follows
the PDF's specified fallback chain:

1. **Clique certificate, fast path** — if the greedy `DSATUR` upper bound
   equals the size of a maximum clique (`nx.find_cliques`), the chromatic
   number is certified immediately: a clique of size *k* forces at least *k*
   colors, and greedy already achieves *k*.
2. **Exact backtracking, bounded** — otherwise, search increasing *k* with
   DSATUR vertex ordering and color-symmetry breaking (never open a color
   number more than 1 past the highest used so far), time-boxed at 15s per
   graph.
3. **Uncertified fallback** — if the time box is hit before the gap closes,
   the graph's chromatic number is excluded from evaluation
   (`chromatic_number_certified: false` in metadata) rather than reporting
   an approximate value, per the PDF: *"Do not use approximate values."*

Verified against 7 known graphs (K5, Petersen, C5, C6, K3,3, K4, Star_10)
before trusting it on the dataset — execution checklist item 2. On the
committed run: **0 of 300 graphs are uncertified** — every chromatic number
closed via the clique certificate or the time-boxed backtracking search.

### Cell 9 — Section 3 (continued): ground truth and the record builder

**The step order here is a correctness requirement**, the same principle as
geometry's winding-reversal ordering:

1. **`compute_presentation_independent(G)`** — the 6 properties that do not
   depend on node labeling: `triangle_count`, `is_bipartite`, `is_planar`,
   `diameter`, `chromatic_number`, `avg_clustering`.
2. **`randomize_labeling(G, rng)`** — relabel nodes with a random permutation
   of `0..n-1`.
3. **`to_edge_list_string(G)`** — serialize the *relabeled* graph, edges
   sorted `(min(u,v), max(u,v))` then lexicographically (Section 4.3).
4. **`degree_of_node_0`, `edge_count`** — read from the relabeled graph, so
   the label matches the string the model will actually see.

**Why the relabeling step exists.** The PDF does not explicitly ask for it,
but it is required by the same principle the geometry README states
explicitly: *"Any domain with a property that depends on how the object is
written down needs [compute-invariant → randomize-presentation → serialize →
read-presentation-dependent-property]."* Without it, node `0` would be
whatever label the generator's internals happened to assign — most visibly,
Barabási–Albert's earliest nodes are structurally the hubs — so
`degree_of_node_0` would measure a generator artifact instead of genuine
serialization-reading ability. This is the graph-domain analogue of
geometry's `maybe_reverse` (winding direction).

**Record schema:**

```json
{
  "object_id": "graph_simple_erdos_renyi_001",
  "tier": "simple",
  "family": "erdos_renyi",
  "num_nodes": 9,
  "num_edges": 8,
  "edge_list": "GRAPH (n=9, m=8):\n0 6\n1 3\n2 5\n2 8\n3 4\n3 5\n3 6\n3 7",
  "properties": {
    "triangle_count": 0,
    "is_bipartite": true,
    "is_planar": true,
    "diameter": 5,
    "chromatic_number": 2,
    "avg_clustering": 0.0,
    "degree_of_node_0": 1,
    "edge_count": 8
  },
  "metadata": {
    "generation_params": {"n1": 3, "n2": 6, "extra_edges": 0},
    "random_seed": 42,
    "clique_number": 2,
    "chromatic_number_certified": true,
    "is_connected": true
  }
}
```

`object_id` format: `graph_{tier}_{family}_{index:03d}`. Index restarts at
001 within each (tier, family) group and runs through that family's
quadrants in a fixed order (Q1 then Q2 then Q3 then Q4, whichever the
family covers) — e.g. `erdos_renyi`'s indices 1–5 are Q1 (bip∧planar), 6–10
are Q2, 11–12 are Q3, 13–20 are Q4.

### Cell 11 — Section 4: build the dataset and summary

`build_dataset(seed=42)` iterates tier → family → quadrant (in
`QUADRANT_ORDER`) → index, calling `QUADRANT_GENERATORS[family][quadrant]`
for each slot in `FAMILY_QUADRANT_PLAN`. Generation failure is **fatal**:
if a generator exhausts its retry budget and returns `None`, `build_dataset`
raises rather than emitting a short dataset.

`summarize(records)` produces `graph_exp1_summary.json`: `total`,
`counts_by_tier_family`, `bipartite_overall`, `planar_overall`,
`boolean_balance_by_tier` (the direct per-tier true/false count for both
properties, across all families — the check that there is exact 50/50
balance and not just balance within one designed family),
`quadrant_by_family_by_tier` (each family's own bip/planar quadrant
breakdown per tier — the check that the cross-family correlation landed
exactly where planned), `distribution_by_tier` (min/max/mean/median/std for
`num_nodes`, `num_edges`, `triangle_count`, `diameter`, `chromatic_number`,
`avg_clustering`, `edge_list_length`), and the chromatic-number uncertified
count/ids. `stats_for` computes population std (divide by *n*), same as
geometry.

### Cell 13 — Section 5: independent verification

Re-parses each record **from the stored edge-list string** with
`parse_edge_list` (plain-text/regex, no NetworkX) and recomputes properties
with hand-written implementations — BFS-based bipartiteness and diameter,
adjacency-set triangle counting, hand-rolled clustering coefficient — then
compares against stored ground truth. On the committed run: **0 mismatches
across 300 graphs, 6 properties each** (1,800 checks, plus header
cross-checks).

**Known gap**, same discipline as geometry's own documented gap: this covers
6 of 8 properties. `is_planar` and `chromatic_number` are not independently
re-derived — an independent planarity test (Boyer–Myrvold) and an
independent exact-coloring implementation are both substantial undertakings
on their own. Worth closing in a port.

### Cell 15 — Section 6: visual spot-check

Plots one graph per (tier × column) — 24 graphs — into a 3×8 grid using
`nx.draw` with a spring layout, saves `spotcheck_exp1_graph.png` at 110 dpi.
Each of the 5 families gets one column per quadrant it covers
(`random_bipartite`: Q1+Q2; `random_planar`: Q3 only;
`erdos_renyi`/`barabasi_albert`/`watts_strogatz`: their Q1 slice plus one
extra column each to make the cross-family overlap and each family's
natural quadrant visible). Titles show each graph's actual
`is_bipartite`/`is_planar` values. Eyeball check: `barabasi_albert`'s Q1
column should show a visible hub/star pattern (it's a preferential-
attachment tree), `watts_strogatz`'s Q1/Q3 columns should look like clean
rings, `random_planar` a non-crossing mesh. Not an automated assertion.

### Cell 17 — Section 8: Colab download

Same `try/except` pattern as geometry — downloads on Colab, no-ops locally
(files already on disk).

---

## 3. Reproducibility

- Single seed, `SEED = 42`, passed to one `random.Random(seed)` instance that
  drives every generator's parameter draws, the node relabeling, and is
  passed directly to NetworkX as its `seed` argument.
- The seed is stored in **every record** (`metadata.random_seed`).
- Same seed + same NetworkX/SciPy version ⇒ byte-identical dataset (hull tie
  breaking in `scipy.spatial.Delaunay` and NetworkX's RNG consumption order
  are library internals). The notebook prints `networkx.__version__` in cell
  1 for this reason — record it when reporting results.
- Cells must run **in order**. Cell 11 depends on every definition above it;
  cells 13 and 15 depend on the in-memory `records` list from cell 11.
- Notebook outputs are stripped in the committed version — the JSON
  artifacts in this folder are the record of what was produced.

## 4. Observed dataset characteristics (seed 42)

From `graph_exp1_summary.json`:

- **bipartite_overall = 150 / 300 (50.0%)**, **planar_overall = 150 / 300
  (50.0%)** — both *exactly* 50/50 in every tier (verified directly against
  `boolean_balance_by_tier`, counting every graph regardless of family).
- **counts_by_tier_family = 20/20/20/20/20** — all 5 families exactly equal,
  in every tier.
- **chromatic_number_uncertified_count = 0** — every graph's chromatic
  number closed via the clique certificate or the time-boxed backtracking
  search, even with the added chromatic-exact construction work (median
  time per generated graph well under a second; 300 graphs generate in
  ~5s total).
- **chromatic_number = {2: 50, 3: 12, 4: 13, 5: 12, 6: 13}, identical in
  every tier** — exactly the target split: 2 locked to the bipartite half,
  3/4/5/6 spread evenly across the non-bipartite half. Before this fix,
  chromatic_number among non-bipartite graphs was concentrated on 3/4
  (~20/~25 each) with 5 rare (3-6/tier) and 6 never observed at all.
- **triangle_count spread is now wide and tier-scaled** instead of
  clustering near 0: among non-bipartite graphs, min/median/max is
  0/13/23 (simple), 0/25/123 (medium), 0/60/266 (hard) — verified directly
  in the notebook output, not just inferred from the mean.
- **Cross-family overlap is real and exactly as planned** —
  `quadrant_by_family_by_tier` for the simple tier:

  | Family | bip∧planar | bip∧¬planar | ¬bip∧planar | ¬bip∧¬planar |
  |---|---|---|---|---|
  | random_bipartite | 10 | 10 | 0 | 0 |
  | random_planar | 0 | 0 | 20 | 0 |
  | erdos_renyi | 5 | 5 | 2 | 8 |
  | barabasi_albert | 5 | 5 | 2 | 8 |
  | watts_strogatz | 5 | 5 | 1 | 9 |

  Identical in medium and hard tiers. `erdos_renyi`/`barabasi_albert`/
  `watts_strogatz` each genuinely span all 4 quadrants; `random_bipartite`/
  `random_planar` stay confined to the quadrants consistent with their name.
- **Scale separation across tiers** is large and intentional:

  | Tier | mean nodes | mean edges | mean triangles | mean edge-list length |
  |------|-----------|-----------|-----------------|------------------------|
  | simple | 11.44 | 17.51 | 6.13 | 95 chars |
  | medium | 29.31 | 52.66 | 15.52 | 300 chars |
  | hard   | 61.26 | 128.27 | 33.16 | 747 chars |

  `chromatic_number`'s mean is now identical across tiers (3.26 in all
  three) by construction, since the 2/3/4/5/6 split is the same in every
  tier — a direct consequence of the balance above, not a coincidence.
- `avg_clustering` decreases with tier (0.18 → 0.15 → 0.13 mean) — larger,
  sparser graphs have proportionally fewer closed triangles per node.

## 5. How Phase 2 consumes this

Mirrors the geometry contract exactly (see
[`phase2_model_results/README.md`](../phase2_model_results/README.md) §1 for
the pattern). Phase 2 runners read `graph_exp1_dataset.json` and use exactly
four things per record:

| Field | Used for |
|-------|----------|
| `object_id` | resume key — `(object_id, property)` pairs already in the JSONL are skipped |
| `edge_list` | the serialized string embedded in the prompt |
| `properties[prop]` | ground truth for scoring the model's answer |
| `metadata.chromatic_number_certified` | when `false`, the `(object_id, "chromatic_number")` pair is excluded from querying/evaluation entirely, per the PDF |

`tier` and `family` ride along into result records for slicing at analysis
time. Everything else in `metadata` is provenance.

For the V4-Pro (non-thinking) companion run's 20% subsample, see
[`../phase2_model_results_graph/06_v4pro_nonthinking/build_subsample.py`](../phase2_model_results_graph/06_v4pro_nonthinking/build_subsample.py) —
it mirrors this file's quadrant structure so the subsample stays exactly
50/50 on both booleans too, not just the full dataset.

---

## 6. Porting this to a new domain

What is domain-specific here and what is not — same split as geometry's own
porting section:

**Reusable structure — keep the shape, change the content:**

- The tier system (3 difficulty levels defined by a complexity parameter).
- The category split within each tier (5 equally-sized structural families
  here; 3 for geometry — the count is domain-specific, the *pattern* of
  named, equally-sized generation categories is not).
- When a boolean ground-truth property needs exact balance and no single
  family can produce both classes: spread the property across **all**
  families instead of concentrating it in one or two "designed" ones, with
  each family covering whichever combinations make sense for it. This
  avoids the tradeoff between family-size balance and boolean balance
  entirely (a dedicated family forced to grow to hit an exact count breaks
  size balance; correcting incidental leaks after generation keeps sizes
  balanced but produces no genuine cross-family diversity). It costs more
  generator variants (one per family per quadrant it covers, ideally each
  with that family's own structural flavor) but removes the tradeoff
  instead of just accepting one side of it.
- Rejection sampling against an explicit, enumerated validity rule set,
  where every rejection has a named reason.
- Compute-invariant-properties → randomize-presentation → serialize →
  read-presentation-dependent-property. Any domain with a property that
  depends on how the object is written down needs this ordering — here,
  node-label identity; for geometry, winding direction.
- Independent verification from the serialized string with an
  implementation that does not share code with the generator.
- The record schema and the summary statistics block.
- Time-boxing an expensive-but-not-always-tractable ground-truth
  computation, with an explicit "exclude and record why" fallback rather
  than reporting an approximate value.

**Domain-specific — must be rewritten:**

| Component | Graph version | What a port replaces it with |
|-----------|----------------|-------------------------------|
| Object type | NetworkX `Graph` | the new domain's object |
| Serialization | edge list `GRAPH (n=.., m=..): ...` | WKT, SMILES, CSV row, … |
| `TIERS` | node-count ranges | the domain's complexity axis |
| Generators | 5 families x up to 4 quadrants each, family-flavored (12 generator functions total) | domain-appropriate samplers |
| `check_validity` | the 6 graph rules | the domain's validity rules |
| Property functions | triangles, bipartiteness, planarity, diameter, chromatic number, clustering | the domain's Table-8-equivalent properties |
| Presentation randomizer | `randomize_labeling` (node relabeling) | whatever presentation choice is arbitrary in that domain |
| Independent verifier | BFS/adjacency-set hand implementations | an independent reimplementation for the new format |

See `../phase1_dataset/README.md` for the Geometry-domain side of the port,
and the root [Readme.md](../Readme.md) for the full three-domain pipeline.
