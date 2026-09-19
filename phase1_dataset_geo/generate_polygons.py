"""
Phase 1 (Geometry) dataset generator, v7 — equal-family, band-balanced polygons.

Seventh geometry dataset in this repo. The others, in order:

  phase1_dataset/   the pilot. PDF Table 2 as written (int[0,100] simple/medium,
                    float2dp[0,1000] hard).
  new_data/         first rebalance attempt; dropped Table 2's coordinate row.
  new_data_v2/      Table 2 as written, rebuilt. See SPEC_3.2_MAPPING.md.
  new_data_v3/      coordinate row dropped again. See DESIGN_NOTES.md.
  new_data_v4/      crossed-encoding matched pairs. See REBUILD_DESIGN.md.
  new_data_v5/      intermediate; superseded by v6.
  new_data_v6/      all-integer [0,1000] in every tier, shape 50/25/25,
                    orientation 50/50, joint (offset-band x fill-band) quota
                    identical in all three tiers. No generator script survives.

WHY THIS ONE EXISTS
===================

v6 got the *measurement* balance right and it is preserved here verbatim
(section 1). What it did not have is what PR #9's graph dataset
(`phase1_dataset_graph/`, KMobin555) established as the standard for this
project:

  1. Every generator family gets an EXACT, EQUAL quota in every tier.
     v6's generator quota drifts hard across tiers —

        generator       simple  medium  hard
        convex              27       9     4
        convex_sharp        23      41    46
        concave             25      25    25
        irregular           25      25    25

     so any tier difference measured on a convex polygon is partly a
     difference in which convex *construction* was used, not in difficulty.
     PR #9 fixes the same class of leak by giving all 5 graph families
     exactly 20 per tier. v7 gives all 4 polygon families exactly 25 per tier.

  2. Every family covers whichever cells of the balance grid make sense for
     it, rather than one family being the sole source of a property value.
     PR #9's phrasing: "spreading True across all 5 families removes the
     family-size/boolean-balance tradeoff entirely." Here, both convex
     families appear in 8 of the 10 convex cells (section 1,
     FAMILY_CELL_PLAN), so the (offset-band, fill-band) marginal is not a
     restatement of the family marginal.

  3. Plan consistency is asserted before generation runs, not checked after.

  4. The dataset ships with a generator script, a summary with full balance
     tables, a README, and a spotcheck image. v6 shipped two JSON files.

WHAT IS HELD FIXED, AND HOW
===========================

Hard constraints — enforced exactly, asserted after the build:

  family            25 / 25 / 25 / 25 in every tier
  shape_type        convex 50, concave 25, irregular 25 in every tier
  convex property   50 True / 50 False in every tier (= shape_type)
  orientation       50 ccw / 50 cw in every tier
  offset_band       22 / 38 / 32 / 8 in every tier          (v6 quota, verbatim)
  fill_band         14 / 37 / 36 / 13 in every tier         (v6 quota, verbatim)
  joint cell        the full 30-cell (shape x offset x fill) table, identical
                    in every tier                            (v6 quota, verbatim)
  extent            a fixed 100-rung ladder, the SAME multiset in every tier
  aspect            a fixed 100-rung ladder, the SAME multiset in every tier

The last two are why bbox diagonal and aspect ratio have the same distribution
in all three tiers — by construction, not in expectation. The tier axis then
varies vertex count and nothing else, which is the point: PDF 6.2's tolerance
bands scale with the bbox diagonal, so a tier that is systematically larger
gets a systematically wider grading band and "difficulty" stops meaning
difficulty. This is v4's gate G2 and v6 inherited it; v7 keeps it.

Ladder rungs are dealt to slots, and a slot that cannot be built at its rung
hands the rung back to the pool and draws another. The multiset is consumed
exactly either way, so the tier marginal is unaffected by which slot got
which rung.

Soft constraint — targeted, not forced:

  vertex_count      dealt from a per-tier ladder that spreads the tier's range
                    evenly, but the integer lattice does not always admit the
                    target at a given extent (a 40-vertex convex lattice
                    polygon needs roughly a 42x44 box; at extent 0.09 on a
                    1000-wide grid, stretched to aspect 0.22, the short side is
                    ~20 units and no such polygon exists). When the target is
                    unreachable the sampler walks it down toward the tier
                    floor and records both the target and what was achieved.
                    The achieved distribution is reported in the summary.

  This is declared rather than hidden. v6 did not target vertex count at all
  and its hard tier piled up at 20-22; v7's targeting flattens it as far as
  the lattice allows, and the summary says how far that was.

PROPERTIES (9, PDF Table 4)
===========================

  vertex_count, bbox, centroid, area, perimeter, convex, aspect_ratio,
  edge_length_variance, orientation

All computed with shapely from the polygon that is actually serialized, then
re-derived independently from the emitted WKT string in verify() and compared.

Run:  python generate_polygons_v7.py
Out:  geometry_exp1_dataset.json, geometry_exp1_summary.json, spotcheck_v7.png
"""

import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import shapely
from shapely import affinity
from shapely.geometry import Polygon

OUT_DIR = Path(__file__).resolve().parent
SEED = 42


# =============================================================================
# 1. Tiers, families, bands, and the balance plan
# =============================================================================

# Structural axis only. Coordinate encoding does NOT move across tiers —
# every tier is integers in [0, 1000]. This is v6's "protocol_int" design and
# it is kept: Table 2 bundles a structural axis with an encoding axis, and
# REBUILD_DESIGN.md section 0(b) shows the sibling domains (graphs Table 6,
# tabular Table 10) vary structural size only. Geometry now matches them.
TIERS = {
    #          vmin vmax
    "simple": (3,   8),
    "medium": (10,  20),
    "hard":   (20,  40),
}
TIER_ORDER = ["simple", "medium", "hard"]
PER_TIER = 100

COORD_LO, COORD_HI = 0.0, 1000.0
COORD_TYPE = "integer"
COORD_SPAN = COORD_HI - COORD_LO

# --- families -----------------------------------------------------------------
# Four generator families, 25 per tier each — the PR #9 rule. `shape_type` is
# the PDF Table 3 category the family reports; two distinct convex
# constructions share the `convex` category, which is how the convex half gets
# 50 records per tier without one construction supplying all of them.
FAMILIES = ["convex_smooth", "convex_sharp", "concave", "irregular"]
PER_FAMILY_PER_TIER = 25
FAMILY_SHAPE = {
    "convex_smooth": "convex",
    "convex_sharp":  "convex",
    "concave":       "concave",
    "irregular":     "irregular",
}
SHAPES = ["convex", "concave", "irregular"]

# --- the two balance axes -----------------------------------------------------
# offset = |centroid - bbox centre| / bbox diagonal.
#   Why it is balanced: an answer of "the bbox centre" scores as correct for
#   `centroid` under PDF 6.2 whenever this is below the tolerance, without
#   computing anything. Letting it drift with tier means the grading band gets
#   more permissive exactly where the task is meant to get harder.
# fill = area / bbox area.
#   Why it is balanced: "half the bbox area" is the matching free ride for
#   `area`, and the same argument applies.
OFFSET_BANDS = [(0.00, 0.02), (0.02, 0.04), (0.04, 0.07), (0.07, 10.0)]
FILL_BANDS = [(0.00, 0.52), (0.52, 0.62), (0.62, 0.70), (0.70, 0.76)]
MAX_FILL = FILL_BANDS[-1][1]

# v6's joint quota, verbatim. Keys are (offset_band, fill_band); values are
# how many of that tier's records of that shape land in the cell.
#
# The empty cells are not omissions. A convex polygon cannot have a low fill
# ratio — its area is its hull's area — so `convex` has no fill_band 0 cell;
# a dented polygon rarely holds its centroid within 2% of the bbox centre
# while also filling more than 70% of it, so `concave`/`irregular` have no
# (0,0) or high-offset/high-fill cells. The quota is feasible geometry, which
# is why it survives from v6 unchanged.
JOINT_QUOTA = {
    "convex": {
        (0, 1): 5, (0, 2): 5, (0, 3): 4,
        (1, 1): 6, (1, 2): 8, (1, 3): 4,
        (2, 1): 6, (2, 2): 7, (2, 3): 3,
        (3, 2): 2,
    },
    "concave": {
        (0, 1): 2, (0, 2): 2,
        (1, 0): 2, (1, 1): 4, (1, 2): 3, (1, 3): 1,
        (2, 0): 2, (2, 1): 4, (2, 2): 2,
        (3, 0): 3,
    },
    "irregular": {
        (0, 1): 2, (0, 2): 2,
        (1, 0): 2, (1, 1): 4, (1, 2): 3, (1, 3): 1,
        (2, 0): 2, (2, 1): 4, (2, 2): 2,
        (3, 0): 3,
    },
}

# The convex cells split between the two convex families. Each entry is
# (convex_smooth, convex_sharp) and the pair sums to that cell's quota above.
#
# The split is weighted, not even: `convex_sharp` is triangle-like and reaches
# the high-offset / low-fill corner, `convex_smooth` is a rounded 4-6-gon and
# reaches the high-fill / low-offset corner. Both still appear in 8 of the 10
# cells, so a model cannot read the family off the cell or the cell off the
# family — PR #9's cross-family-overlap property, restated for geometry.
# `convex_sharp` carries NO fill_band 3 slot. That is measured, not stylistic:
# the family is a rounded triangle, a triangle's area is at most half its
# bounding box, and rounding the corners buys back only so much. Over 1,000
# valid simple-tier draws it reached fill_band 3 eight times (0.8%) and the
# specific cell (2, 3) three times (0.3%) — reachable, but slow enough that a
# (2, 3) slot dominates the build's whole cost for one record. Its fill_band 2
# slots stay, since that band comes up in roughly 3-7% of draws.
FAMILY_CELL_PLAN = {
    (0, 1): (2, 3),
    (0, 2): (3, 2),
    (0, 3): (4, 0),
    (1, 1): (2, 4),
    (1, 2): (3, 5),
    (1, 3): (4, 0),
    (2, 1): (1, 5),
    (2, 2): (3, 4),
    (2, 3): (3, 0),
    (3, 2): (0, 2),
}

# --- plan consistency, asserted before anything is generated -----------------
for _s in SHAPES:
    _want = {"convex": 50, "concave": 25, "irregular": 25}[_s]
    assert sum(JOINT_QUOTA[_s].values()) == _want, (_s, sum(JOINT_QUOTA[_s].values()))
assert sum(sum(q.values()) for q in JOINT_QUOTA.values()) == PER_TIER
assert set(FAMILY_CELL_PLAN) == set(JOINT_QUOTA["convex"])
for _cell, (_sm, _sh) in FAMILY_CELL_PLAN.items():
    assert _sm + _sh == JOINT_QUOTA["convex"][_cell], _cell
assert sum(sm for sm, _ in FAMILY_CELL_PLAN.values()) == PER_FAMILY_PER_TIER
assert sum(sh for _, sh in FAMILY_CELL_PLAN.values()) == PER_FAMILY_PER_TIER

_OFF_TOTALS = Counter()
_FILL_TOTALS = Counter()
for _s in SHAPES:
    for (_ob, _fb), _n in JOINT_QUOTA[_s].items():
        _OFF_TOTALS[_ob] += _n
        _FILL_TOTALS[_fb] += _n
assert dict(_OFF_TOTALS) == {0: 22, 1: 38, 2: 32, 3: 8}, dict(_OFF_TOTALS)
assert dict(_FILL_TOTALS) == {0: 14, 1: 37, 2: 36, 3: 13}, dict(_FILL_TOTALS)

# --- the size ladders ---------------------------------------------------------
# Prescribed, not drawn. Drawing extent uniformly per record looks equivalent
# and is not: the hard tier rejects small-extent draws far more often (fewer
# lattice points to carry 20-40 distinct vertices), so the accepted sample
# drifts large and the median bbox diagonal rises with tier. Dealing every
# tier the same ladder makes the distribution identical across tiers by
# construction. Bounds are v6's measured range (extent 0.089 to 0.83).
EXTENT_LO, EXTENT_HI = 0.09, 0.84
ASPECT_LO, ASPECT_HI = 0.22, 4.45


def _log_ladder(lo, hi, n):
    """n rungs, geometrically spaced. Geometric rather than linear because both
    quantities are ratios — a factor of 2 means the same thing at either end."""
    return [math.exp(math.log(lo) + (math.log(hi) - math.log(lo)) * i / (n - 1))
            for i in range(n)]


EXTENT_LADDER = _log_ladder(EXTENT_LO, EXTENT_HI, PER_TIER)
ASPECT_LADDER = _log_ladder(ASPECT_LO, ASPECT_HI, PER_TIER)

# The two ladders are dealt as FIXED PAIRS, not independently.
#
# Extent alone does not determine the bounding-box diagonal — the render
# pipeline stretches to the target aspect and then scales the LONGEST side to
# extent * 1000, so
#
#     diag = extent * 1000 * sqrt(1 + min(aspect, 1/aspect)^2)
#
# and the diagonal is a function of the PAIR. Consuming the two ladders
# independently gives each tier the same extent multiset and the same aspect
# multiset but a different pairing, and the diagonal distribution then differs
# between tiers — measured on the first build, median diagonal 317.8 / 301.1 /
# 311.6, a 5% tier spread in the very quantity PDF 6.2's tolerance bands are
# divided by. Freezing the pairing removes it by construction: every tier
# consumes the identical multiset of (extent, aspect) pairs, so the identical
# multiset of diagonals.
#
# The pairing itself is one fixed permutation, drawn from its own generator so
# it does not move when the build seed changes.
_PAIR_RNG = random.Random(20260915)
_paired_aspects = list(ASPECT_LADDER)
_PAIR_RNG.shuffle(_paired_aspects)
SIZE_LADDER = list(zip(EXTENT_LADDER, _paired_aspects))
assert len(SIZE_LADDER) == PER_TIER

# The pairs are further split into one pool PER FILL BAND, and a slot may only
# draw from its own band's pool.
#
# Why. `area` = fill_ratio * bbox_area, and both factors are already flat
# across tiers on their own. What is not automatically flat is their
# CORRELATION: if the hard tier happens to spend its small-extent rungs on its
# fill_band 3 slots and the simple tier spends them on fill_band 0, the two
# tiers hold the same multiset of bbox areas and the same multiset of fill
# ratios but a different multiset of products. Measured on the second build,
# mean target extent by fill band ran 0.284/0.339/0.353/0.344 in simple
# against 0.399/0.348/0.334/0.249 in hard, and tier mean area moved 4%.
#
# Freezing the split makes the (fill band -> size) pairing identical in every
# tier, so the area distribution is identical too. Pool sizes are exactly the
# fill-band quota, so the union is still the whole ladder and the extent and
# aspect marginals are untouched.
#
# The practical cost is that a slot which cannot be built at any rung in its
# own pool cannot borrow from another band's. Pools are sized 14/37/36/13 and
# every band spans the full extent range, so there is room to retry; the build
# asserts the pools empty at the end either way.
_pool_order = list(range(PER_TIER))
_PAIR_RNG.shuffle(_pool_order)
FILL_BAND_QUOTA = dict(_FILL_TOTALS)
SIZE_POOLS = {}
_cursor = 0
for _fb in sorted(FILL_BAND_QUOTA):
    _take = FILL_BAND_QUOTA[_fb]
    SIZE_POOLS[_fb] = [SIZE_LADDER[i] for i in _pool_order[_cursor:_cursor + _take]]
    _cursor += _take
assert _cursor == PER_TIER
assert sum(len(v) for v in SIZE_POOLS.values()) == PER_TIER
assert sorted(p for v in SIZE_POOLS.values() for p in v) == sorted(SIZE_LADDER)


def pair_diagonal(extent, aspect):
    """The bbox diagonal a (extent, aspect) pair produces, in grid units."""
    short = min(aspect, 1.0 / aspect)
    return extent * COORD_SPAN * math.sqrt(1.0 + short * short)

# Vertex-count targets: spread each tier's range as evenly as 100 slots allow.
#
# The simple tier's ladder starts at 4, not at Table 2's 3, and that is forced
# by the quota rather than chosen. A triangle's area is at most half its
# bounding box, so a triangle is always in fill_band 0 (fill < 0.52) — and
# `convex` has no fill_band 0 cell, because a convex polygon cannot have a low
# fill ratio in general and the band was never allocated one. A triangle is
# convex by definition. So no triangle satisfies any cell of the table, at any
# offset band, and the ladder must not deal one. v6 reached the same place
# empirically: its simple tier has no 3-vertex polygon either. The tier's
# declared range stays 3-8 (the record-level rule 6 check is unchanged); what
# changes is that the sampler is not asked for something the quota forbids.
LADDER_VMIN = {"simple": 4, "medium": 10, "hard": 20}


def _vertex_ladder(vmin, vmax, n):
    values = list(range(vmin, vmax + 1))
    return [values[i * len(values) // n] for i in range(n)]


VERTEX_LADDER = {t: _vertex_ladder(LADDER_VMIN[t], hi, PER_TIER)
                 for t, (lo, hi) in TIERS.items()}
for _t in TIER_ORDER:
    assert len(VERTEX_LADDER[_t]) == PER_TIER
    assert TIERS[_t][0] <= min(VERTEX_LADDER[_t]) <= max(VERTEX_LADDER[_t]) <= TIERS[_t][1]

# The smallest vertex count that can reach a given fill band, per FAMILY.
# Every number here was measured by sampling the family at each vertex count
# and recording which cells it reached; none is a guess.
#
#   convex_smooth  a rounded 4-6-gon. A quadrilateral already reaches 0.76,
#                  so 4 everywhere above fill_band 0.
#   convex_sharp   a rounded triangle. At 4 vertices it reached ONLY fill
#                  bands 0 and 1 in 140/140 draws; fill_band 2 first appears
#                  at 5 and is only workable from 6.
#   concave /      a 3-gon is convex by definition, so 4 is the floor. At
#   irregular      exactly 4 the family reached ONLY fill_band 0 in 140/140
#                  draws — a dented quadrilateral cannot fill its box — so
#                  every band above 0 needs 5.
#
# These floors are what the vertex-target dealer in tier_slots() respects.
# Getting them wrong does not corrupt the dataset (the sampler would just walk
# the target away and the balance checks still pass), it flattens the vertex
# distribution less — which is exactly what the first build did: 3 records at
# 4 vertices against a target of 20.
MIN_VERTICES_FOR_FILL = {
    "convex_smooth": {0: 3, 1: 4, 2: 4, 3: 4},
    "convex_sharp":  {0: 3, 1: 4, 2: 6, 3: 8},
    "concave":       {0: 4, 1: 5, 2: 5, 3: 5},
    "irregular":     {0: 4, 1: 5, 2: 5, 3: 5},
}


def min_vertices(family, fill_band):
    return MIN_VERTICES_FOR_FILL[family][fill_band]

# The vertex ladder must be able to serve the floors above. Checked here, per
# tier, before generation: for every floor f, the number of slots demanding
# n >= f must not exceed the number of ladder rungs at or above f.
def _check_ladder_feasible():
    demand = Counter()
    for cell, (n_sm, n_sh) in FAMILY_CELL_PLAN.items():
        demand[min_vertices("convex_smooth", cell[1])] += n_sm
        demand[min_vertices("convex_sharp", cell[1])] += n_sh
    for shape in ("concave", "irregular"):
        for (ob, fb), n in JOINT_QUOTA[shape].items():
            demand[min_vertices(shape, fb)] += n
    assert sum(demand.values()) == PER_TIER, dict(demand)
    for tier in TIER_ORDER:
        rungs = VERTEX_LADDER[tier]
        for floor in sorted(demand):
            need = sum(n for f, n in demand.items() if f >= floor)
            have = sum(1 for v in rungs if v >= floor)
            assert need <= have, (
                f"{tier}: {need} slots need n >= {floor} but the ladder "
                f"offers only {have}")


_check_ladder_feasible()

MAX_TRIES = 4000          # per (slot, extent rung) attempt
MAX_RUNG_SWAPS = 40       # how many rungs a slot may try before widening
MIN_AREA = 10.0           # PDF 3.2 rule 3
MIN_FILL = 0.05           # PDF 3.2 rule 5b — slivers


# =============================================================================
# 2. Core helpers
# =============================================================================

def is_convex(poly):
    """A polygon equals its own convex hull only if it has no dents.
    normalize() makes the comparison ignore vertex order and start point."""
    return poly.normalize().equals(poly.convex_hull.normalize())


def centroid_offset_norm(poly):
    """|centroid - bbox centre| in bbox-diagonal units. The quantity that says
    whether `centroid` can be answered from `bbox` alone."""
    minx, miny, maxx, maxy = poly.bounds
    diag = math.dist((minx, miny), (maxx, maxy))
    if diag <= 0:
        return 0.0
    cx, cy = poly.centroid.coords[0]
    return math.dist((cx, cy), ((minx + maxx) / 2.0, (miny + maxy) / 2.0)) / diag


def fill_ratio(poly):
    minx, miny, maxx, maxy = poly.bounds
    a = (maxx - minx) * (maxy - miny)
    return poly.area / a if a > 0 else 0.0


def vertex_mean_offset_norm(poly):
    """|mean of the serialized vertices - centroid| in bbox-diagonal units.
    The other free ride on `centroid`: averaging the coordinates in the WKT
    string. Reported per record so Phase 3 can stratify on it."""
    minx, miny, maxx, maxy = poly.bounds
    diag = math.dist((minx, miny), (maxx, maxy))
    if diag <= 0:
        return 0.0
    ring = list(poly.exterior.coords)[:-1]
    vm = (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))
    return math.dist(vm, poly.centroid.coords[0]) / diag


def band_of(value, bands):
    for i, (lo, hi) in enumerate(bands):
        if lo <= value < hi:
            return i
    return len(bands) - 1 if value >= bands[-1][0] else None


def offset_band_of(poly):
    return band_of(centroid_offset_norm(poly), OFFSET_BANDS)


def fill_band_of(poly):
    f = fill_ratio(poly)
    if f >= MAX_FILL or f < FILL_BANDS[0][0]:
        return None
    return band_of(f, FILL_BANDS)


def check_validity(poly, vmin, vmax):
    """PDF 3.2's validity rules. Returns (True, None) or (False, reason)."""
    if poly is None or poly.is_empty:
        return False, "empty"
    if not poly.is_valid:                                    # rule 1
        return False, "not_valid"
    if not poly.is_simple:                                   # rule 2
        return False, "not_simple"
    if poly.area <= MIN_AREA:                                # rule 3
        return False, "area_too_small"

    coords = list(poly.exterior.coords)[:-1]
    n = len(coords)
    if not (vmin <= n <= vmax):                              # rule 6
        return False, "vertex_count_out_of_range"
    for x, y in coords:                                      # rule 7
        if not (COORD_LO <= x <= COORD_HI and COORD_LO <= y <= COORD_HI):
            return False, "out_of_bounds"
    for i in range(n):                                       # rule 4
        if coords[i] == coords[(i + 1) % n]:
            return False, "duplicate_adjacent"
    for i in range(n):                                       # rule 5
        ax, ay = coords[i]
        bx, by = coords[(i + 1) % n]
        cx, cy = coords[(i + 2) % n]
        if abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) < 1e-9:
            return False, "collinear"

    f = fill_ratio(poly)
    if f < MIN_FILL:                                         # rule 5b — slivers
        return False, "sliver_low_fill"
    return True, None


# =============================================================================
# 3. Base shapes — built about the origin, sized later
# =============================================================================

MIN_ANGLE_GAP_FRAC = 0.35


def _spread_angles(rng, n, min_gap_frac=MIN_ANGLE_GAP_FRAC):
    """n angles round the circle with a guaranteed minimum separation.

    Sorting n independent uniform angles is the obvious sampler, and the
    smallest of n uniform gaps shrinks like 1/n^2: at 40 vertices two of them
    are almost always closer together than one lattice unit, and rule 4
    rejects the draw. Equal spacing plus bounded jitter keeps the angular
    irregularity while putting a floor under the gap. The floor is a function
    of n alone."""
    step = 2 * math.pi / n
    half = (1.0 - min_gap_frac) * step / 2.0
    phase = rng.uniform(0, 2 * math.pi)
    return sorted((phase + i * step + rng.uniform(-half, half)) % (2 * math.pi)
                  for i in range(n))


def support_rounded_kgon(rng, n, k, beta, rho, jitter, angle_gap=0.0):
    """n points on a smooth rounded k-gon, built from its support function.

    A convex body can be written by its support function h(theta); the
    boundary point in direction u is p = h*u + h'*u'. Taking h as a softmax
    over k corner directions gives a rounded k-gon that is strictly convex
    everywhere — no flat sides, so rule 5's collinearity test is never tripped
    before rounding — with exactly the n vertices asked for.

      k       corner count.  k=3 is what reaches a large centroid offset;
                             k=5,6 is what reaches a high fill ratio.
      beta    corner sharpness. High = closer to a true polygon.
      rho     base rounding radius. Low = sharper corners.
      jitter  corner-angle spread. Breaks the k-fold symmetry.

    Why k matters for the balance grid: a centrally symmetric convex body puts
    its centroid on its bbox centre, so offset_band 2 and 3 are unreachable
    for large even k. Triangle-likeness is the only way a convex polygon holds
    its centroid off the bbox centre, and it costs fill ratio. That tension is
    exactly what FAMILY_CELL_PLAN splits between the two convex families."""
    phi = rng.uniform(0, 2 * math.pi)
    corners = []
    for i in range(k):
        a = phi + 2 * math.pi * i / k + rng.uniform(-jitter, jitter) * 2 * math.pi / k
        rad = rng.uniform(0.85, 1.15)
        corners.append((rad * math.cos(a), rad * math.sin(a)))

    pts = []
    for t in _spread_angles(rng, n, angle_gap):
        ux, uy = math.cos(t), math.sin(t)
        vx, vy = -math.sin(t), math.cos(t)
        dots = [cx * ux + cy * uy for cx, cy in corners]
        mx = max(dots)
        ws = [math.exp(beta * (d - mx)) for d in dots]
        total = sum(ws)
        ws = [w / total for w in ws]
        h = mx + math.log(total) / beta + rho
        hp = sum(w * (cx * vx + cy * vy) for w, (cx, cy) in zip(ws, corners))
        pts.append((h * ux + hp * vx, h * uy + hp * vy))
    return pts


def base_radial(rng, n, shrink_lo, shrink_hi, b1, b3, angle_jitter=0.0):
    """Star / radial polygon carrying a low-harmonic asymmetry envelope.

        r(theta) = (1 + b1*cos(theta - p1) + b3*cos(3*(theta - p3))) * shrink

    Both envelope terms are odd harmonics, so they make the outline asymmetric
    about both bbox axes and push the centroid off the bbox centre — the same
    job k=3 does for the convex half. Without them, random radii average out
    over 20-40 vertices and the shape drifts back toward a disc, pinning every
    record in offset_band 0.

    `shrink` is the per-vertex radius draw that creates the dents, and its
    width is what sets the fill ratio: a wide (0.25, 0.85) draw cuts deep
    notches and lands in fill_band 0, a narrow (0.72, 0.95) draw grazes and
    lands in fill_band 2-3. `angle_jitter` clusters the angles, which is what
    makes `irregular` erratic rather than starlike."""
    p1 = rng.uniform(0, 2 * math.pi)
    p3 = rng.uniform(0, 2 * math.pi)
    angles = _spread_angles(rng, n)
    if angle_jitter:
        step = 2 * math.pi / n
        j = min(angle_jitter, 0.4 * MIN_ANGLE_GAP_FRAC * step)
        angles = sorted(a + rng.uniform(-j, j) for a in angles)
    pts = []
    for t in angles:
        r = 1.0 + b1 * math.cos(t - p1) + b3 * math.cos(3 * (t - p3))
        r = max(r, 0.05) * rng.uniform(shrink_lo, shrink_hi)
        pts.append((r * math.cos(t), r * math.sin(t)))
    return pts


def base_dented(rng, n, k, beta, rho, jitter, depth):
    """Convex rounded k-gon with one vertex pushed inside the chord joining its
    neighbours — exactly one reflex vertex, so `convex` flips to False by
    construction rather than by sampling. Not used by the shipped families
    (concave/irregular are radial), kept because it is the minimal-edit
    construction the graph dataset uses for its boolean flips and it is the
    fallback when a radial draw cannot reach a very high fill band."""
    pts = support_rounded_kgon(rng, n, k, beta, rho, jitter)
    i = rng.randrange(n)
    ax, ay = pts[(i - 1) % n]
    bx, by = pts[(i + 1) % n]
    mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
    gx = sum(q[0] for q in pts) / n
    gy = sum(q[1] for q in pts) / n
    pts[i] = (mx + depth * (gx - mx), my + depth * (gy - my))
    return pts


# =============================================================================
# 4. Family parameter draws, biased toward the target cell
# =============================================================================
#
# Every family is a rejection sampler: draw parameters, build, round to the
# lattice, test. Biasing the draw toward the requested (offset_band, fill_band)
# does not change what is accepted — the band test after rounding is the only
# gate — it only changes how many attempts that takes. The binding cells are
# convex (3, 2) and concave/irregular (3, 0), which need strong asymmetry, and
# convex (0, 3), which needs near-central symmetry at high fill.

def _draw_params(rng, family, ob, fb):
    """Parameters for one attempt at one cell, as a dict for the family's base
    builder. `ob`/`fb` are the target bands."""
    if family == "convex_smooth":
        # Rounded 4-6-gon. High k is centrally symmetric — high fill, low
        # offset — so k is pulled down as the offset target rises and pushed
        # up as the fill target rises.
        k_pool = {0: [5, 6], 1: [4, 5, 6], 2: [4, 5], 3: [4]}[ob]
        if fb >= 3:
            k_pool = [k for k in k_pool if k >= 5] or [5]
        return dict(
            k=rng.choice(k_pool),
            beta=rng.uniform(5.0, 16.0),
            rho=rng.uniform(0.18, 0.55) if fb >= 2 else rng.uniform(0.08, 0.35),
            jitter=rng.uniform(0.05, 0.25) if ob <= 1 else rng.uniform(0.25, 0.55),
        )
    if family == "convex_sharp":
        # Triangle-like. k=3 is the only convex shape that holds its centroid
        # far from its bbox centre, and that is what buys offset_band 2-3.
        return dict(
            k=3,
            beta=rng.uniform(14.0, 34.0) if ob >= 2 else rng.uniform(8.0, 22.0),
            rho=rng.uniform(0.03, 0.18) if fb <= 1 else rng.uniform(0.15, 0.45),
            jitter=rng.uniform(0.30, 0.70) if ob >= 2 else rng.uniform(0.08, 0.40),
        )
    # concave / irregular: radial. The shrink window sets the fill band, the
    # b1/b3 harmonics set the offset band.
    shrink = {
        0: (0.22, 0.80),      # deep notches  -> fill < 0.52
        1: (0.42, 0.90),
        2: (0.60, 0.95),
        3: (0.74, 0.99),      # grazing dents -> fill 0.70-0.76
    }[fb]
    b1 = {0: (0.02, 0.10), 1: (0.08, 0.20), 2: (0.16, 0.32), 3: (0.28, 0.50)}[ob]
    return dict(
        shrink_lo=shrink[0], shrink_hi=shrink[1],
        b1=rng.uniform(*b1),
        b3=rng.uniform(0.04, 0.16),
        angle_jitter=(0.85 * math.pi / 40) if family == "irregular" else 0.0,
    )


def _build_base(rng, family, n, params):
    if family in ("convex_smooth", "convex_sharp"):
        return support_rounded_kgon(rng, n, **params)
    p = dict(params)
    if family == "irregular":
        p["angle_jitter"] = 0.85 * math.pi / n
    else:
        p.pop("angle_jitter", None)
        p["angle_jitter"] = 0.0
    return base_radial(rng, n, **p)


# =============================================================================
# 5. Render pipeline — stretch to aspect, scale to extent, snap to the lattice
# =============================================================================

def lattice_repair(poly):
    """Drop the vertices that quantization has made redundant.

    Two rounded vertices landing on the same lattice point (rule 4) and three
    landing on one lattice line (rule 5) are artifacts of the quantum, not
    features of the shape: the continuous shape had distinct, non-collinear
    vertices there and the grid cannot express them. Removing them yields the
    polygon the grid can actually represent, with no change to the outline."""
    pts = list(poly.exterior.coords)[:-1]

    out = []
    for p in pts:
        if not out or p != out[-1]:
            out.append(p)
    while len(out) > 3 and out[0] == out[-1]:
        out.pop()

    changed = True
    while changed and len(out) > 3:
        changed = False
        m = len(out)
        for i in range(m):
            ax, ay = out[(i - 1) % m]
            bx, by = out[i]
            cx, cy = out[(i + 1) % m]
            if abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) < 1e-9:
                out.pop(i)
                changed = True
                break

    if len(out) < 3:
        return None
    repaired = Polygon(out)
    if not repaired.is_valid or repaired.is_empty or repaired.area <= 0:
        return None
    return repaired


def render(pts, aspect, extent, place):
    """Continuous points -> integer polygon on the [0, 1000] grid.

    Rotate, stretch to the target aspect, scale so the longest bbox side is
    `extent` * 1000, place at `place` within the box, round, repair.

    Rounding a convex outline to a coarse grid does not stay convex: each
    vertex moves by up to half a lattice unit in an arbitrary direction, which
    is enough to turn a shallow corner inward. The convex families take the
    hull of the rounded vertices, which is the projection back onto the convex
    lattice polygons and which discharges rules 4 and 5 outright."""
    poly = Polygon(pts)
    if not poly.is_valid or poly.is_empty or poly.area <= 0:
        return None

    minx, miny, maxx, maxy = poly.bounds
    w, h = maxx - minx, maxy - miny
    if w <= 0 or h <= 0:
        return None
    poly = affinity.scale(poly, xfact=aspect / (w / h), yfact=1.0, origin="centroid")

    minx, miny, maxx, maxy = poly.bounds
    longest = max(maxx - minx, maxy - miny)
    if longest <= 0:
        return None
    k = (extent * COORD_SPAN) / longest
    poly = affinity.scale(poly, xfact=k, yfact=k, origin=(0, 0))

    minx, miny, maxx, maxy = poly.bounds
    w, h = maxx - minx, maxy - miny
    if w > COORD_SPAN or h > COORD_SPAN:
        return None
    fx, fy = place
    poly = affinity.translate(poly,
                              xoff=COORD_LO + fx * (COORD_SPAN - w) - minx,
                              yoff=COORD_LO + fy * (COORD_SPAN - h) - miny)

    snapped = Polygon([(round(x), round(y)) for x, y in list(poly.exterior.coords)[:-1]])
    if not snapped.is_valid or snapped.is_empty or snapped.area <= 0:
        return None
    return snapped


def finish_convex(snapped):
    hull = snapped.convex_hull
    if hull.geom_type != "Polygon" or hull.is_empty or hull.area <= 0:
        return None
    return hull


# =============================================================================
# 6. The slot plan — who gets which cell, which winding, which ladder rung
# =============================================================================

def tier_slots(tier, rng):
    """The 100 (family, shape, offset_band, fill_band, winding, vertex_target)
    slots of one tier.

    Cells come from JOINT_QUOTA and, for the convex half, FAMILY_CELL_PLAN.
    Winding is dealt to exactly 50 ccw / 50 cw across the tier and shuffled
    against the cells, so orientation carries no information about any other
    factor. Vertex targets come from the tier's ladder, likewise shuffled."""
    slots = []
    for cell, (n_smooth, n_sharp) in FAMILY_CELL_PLAN.items():
        for _ in range(n_smooth):
            slots.append(["convex_smooth", "convex", cell[0], cell[1]])
        for _ in range(n_sharp):
            slots.append(["convex_sharp", "convex", cell[0], cell[1]])
    for shape in ("concave", "irregular"):
        for cell, n in JOINT_QUOTA[shape].items():
            for _ in range(n):
                slots.append([shape, shape, cell[0], cell[1]])

    assert len(slots) == PER_TIER
    fam_counts = Counter(s[0] for s in slots)
    assert all(fam_counts[f] == PER_FAMILY_PER_TIER for f in FAMILIES), fam_counts

    windings = ["ccw"] * (PER_TIER // 2) + ["cw"] * (PER_TIER // 2)
    rng.shuffle(windings)
    rng.shuffle(slots)

    # Vertex targets are dealt feasibility-first: a slot whose cell needs a
    # high minimum vertex count picks before one that will take anything.
    # Dealing at random instead leaves the fussy slots holding targets their
    # cell cannot reach, and the sampler then silently walks those targets
    # away — which is how a "balanced" vertex ladder turns into a skewed one.
    # The ladder multiset is consumed exactly either way.
    pool = sorted(VERTEX_LADDER[tier])
    order = sorted(range(len(slots)),
                   key=lambda i: -min_vertices(slots[i][0], slots[i][3]))
    targets = [None] * len(slots)
    for i in order:
        floor = min_vertices(slots[i][0], slots[i][3])
        choices = [j for j, v in enumerate(pool) if v >= floor]
        if not choices:
            raise RuntimeError(f"{tier}: vertex ladder cannot serve a slot "
                               f"needing n >= {floor}")
        j = rng.choice(choices)
        targets[i] = pool.pop(j)
    assert not pool

    return [
        {"family": f, "shape_type": s, "offset_band": ob, "fill_band": fb,
         "winding": w, "vertex_target": vt, "vertex_floor": min_vertices(f, fb)}
        for (f, s, ob, fb), w, vt in zip(slots, windings, targets)
    ]


# =============================================================================
# 7. Generation
# =============================================================================

def make_candidate(rng, family, tier, n_target, aspect, extent, delta, budget,
                   want_exact=False):
    """Build ONE valid polygon for this family at roughly this vertex count, and
    report which cell of the balance grid it landed in. It is not asked to land
    in a particular cell — that is the caller's job (see build_tier).

    Why it is built this way. Demanding a specific (offset_band, fill_band)
    inside the sampler and discarding everything else throws away ~95% of
    perfectly good polygons: measured on the first build, 2,800 of every 3,000
    draws died on the exact-vertex-count test alone, before any band test ran.
    Handing every valid polygon back to the caller, which files it against
    whichever slot still wants that cell, turns rejection sampling into
    collection and is what makes the build finish.

    `delta` is the adaptive oversample table. The convex families lose vertices
    to the hull and both families lose them to lattice repair, and how many
    they lose depends on the family, the vertex count and the extent — it is
    not predictable in closed form. Drawing the overshoot at random (the first
    build) means the right overshoot comes up by luck; learning it from the
    last few outcomes means it comes up almost every time.

    `want_exact` makes the vertex count a hard filter inside the loop rather
    than something the caller checks afterwards. Without it the function
    returns the FIRST valid polygon it builds, whatever its vertex count, and
    a caller that needs an exact count then rejects almost everything it is
    handed — measured, that is what pinned the achieved vertex distribution to
    whatever the sampler naturally produces (simple tier: 3 records at 4
    vertices against a target of 20) while the adaptive table never got the
    chance to converge.

    Returns (poly, params, n_have, offset_band, fill_band, attempts) or None.
    """
    vmin, vmax = TIERS[tier]
    convex_family = family in ("convex_smooth", "convex_sharp")
    key = (family, tier, n_target)
    d = delta.get(key, 2 if not convex_family else max(2, n_target // 2))

    for attempt in range(1, budget + 1):
        n_draw = n_target + max(0, int(d)) + rng.randint(-1, 2)
        if n_draw < 3 or n_draw > 200:
            n_draw = max(3, min(200, n_draw))
        params = _draw_params(rng, family, rng.randrange(len(OFFSET_BANDS)),
                              rng.randrange(len(FILL_BANDS)))
        try:
            pts = _build_base(rng, family, n_draw, params)
        except (ValueError, ZeroDivisionError, OverflowError):
            continue
        place = (rng.uniform(0.02, 0.98), rng.uniform(0.02, 0.98))
        snapped = render(pts, aspect, extent, place)
        if snapped is None:
            continue
        if convex_family:
            snapped = finish_convex(snapped)
            if snapped is None:
                continue
        poly = lattice_repair(snapped)
        if poly is None:
            continue

        n_have = len(poly.exterior.coords) - 1
        # Feedback: the overshoot that was needed this time, smoothed.
        d += 0.35 * ((n_draw - n_have) - d)
        delta[key] = d

        if not (vmin <= n_have <= vmax):
            continue
        if want_exact and n_have != n_target:
            continue
        ok, _ = check_validity(poly, vmin, vmax)
        if not ok:
            continue
        if is_convex(poly) != convex_family:
            continue
        fb = fill_band_of(poly)
        if fb is None:
            continue
        ob = offset_band_of(poly)
        if ob is None:
            continue
        return poly, params, n_have, ob, fb, attempt
    return None


# How hard a slot is to satisfy: high offset band and extreme fill band are
# the corners of the grid, and a slot in one of them should get first pick of
# the ladder rungs rather than whatever the easy slots left behind.
def _fussiness(slot):
    fb = slot["fill_band"]
    return (slot["offset_band"] + (3 if fb in (0, 3) else fb), slot["vertex_target"])


BUDGET_STRICT = 4000      # candidate draws per rung pair, pass 1
BUDGET_RELAXED = 3500     # pass 2
STRICT_STALL = 400        # consecutive misses before pass 1 gives up
MAX_ROUNDS = 260000


def build_tier(tier, rng, log):
    """Fill one tier's 100 slots, consuming the extent and aspect ladders
    exactly.

    Two passes. Pass 1 requires a candidate to match an open slot on family,
    cell AND vertex target exactly, so the vertex ladder is honoured wherever
    the lattice allows it. Pass 2 takes what is left and matches on family and
    cell only, giving each remaining slot the candidate with the nearest
    vertex count. The summary reports how many records came from each pass.

    Ladder rungs are drawn from a pool and returned unused if the attempt
    fails, so the multiset a tier consumes is fixed no matter which slot got
    which rung. That is what makes the three tiers' size distributions
    identical rather than merely similar.
    """
    slots = tier_slots(tier, rng)
    pools = {fb: list(v) for fb, v in SIZE_POOLS.items()}
    for v in pools.values():
        rng.shuffle(v)

    results = [None] * len(slots)
    open_idx = set(range(len(slots)))
    delta = {}
    rounds = 0

    for strict in (True, False):
        budget = BUDGET_STRICT if strict else BUDGET_RELAXED
        stall = 0
        while open_idx:
            rounds += 1
            if rounds > MAX_ROUNDS:
                raise RuntimeError(f"{tier}: giving up with {len(open_idx)} slots open")
            cur = max(open_idx, key=lambda i: _fussiness(slots[i]))
            s = slots[cur]
            pool = pools[s["fill_band"]]
            if not pool:
                raise RuntimeError(f"{tier}: fill_band {s['fill_band']} pool empty "
                                   f"with slots still open")
            pi = rng.randrange(len(pool))
            extent, aspect = pool[pi]

            cand = make_candidate(rng, s["family"], tier, s["vertex_target"],
                                  aspect, extent, delta, budget,
                                  want_exact=strict)
            if cand is None:
                stall += 1
                if strict and stall > STRICT_STALL:
                    break          # hand the rest to the relaxed pass
                continue
            poly, params, n_have, ob, fb, attempts = cand

            # The rung came out of fill band `s["fill_band"]`'s pool, so the
            # candidate may only be filed against a slot in that same band —
            # otherwise the per-band size pools leak into each other and the
            # correlation this split exists to freeze comes back.
            matches = [i for i in open_idx
                       if slots[i]["family"] == s["family"]
                       and slots[i]["offset_band"] == ob
                       and slots[i]["fill_band"] == fb
                       and fb == s["fill_band"]
                       and n_have >= slots[i]["vertex_floor"]]
            if strict:
                matches = [i for i in matches if slots[i]["vertex_target"] == n_have]
            if not matches:
                stall += 1
                if strict and stall > STRICT_STALL:
                    break
                continue
            stall = 0
            pick = min(matches, key=lambda i: abs(slots[i]["vertex_target"] - n_have))

            pool.pop(pi)
            open_idx.discard(pick)
            results[pick] = (slots[pick], poly, params, extent, aspect, attempts,
                             n_have, "strict" if strict else "relaxed")
            log.append({"tier": tier, "family": s["family"], "cell": (ob, fb),
                        "vertex_target": slots[pick]["vertex_target"],
                        "vertex_achieved": n_have, "attempts": attempts,
                        "pass": "strict" if strict else "relaxed"})
        if not open_idx:
            break

    if open_idx:
        raise RuntimeError(f"{tier}: {len(open_idx)} slots unsatisfiable: "
                           f"{[slots[i] for i in sorted(open_idx)][:3]}")
    assert not any(pools.values()), {k: len(v) for k, v in pools.items()}
    return results


# =============================================================================
# 8. Properties and records
# =============================================================================

def compute_orientation_independent(poly):
    """The 8 properties that do not depend on winding direction, plus the bbox
    diagonal Phase 3 uses to normalize centroid and bbox errors."""
    minx, miny, maxx, maxy = poly.bounds
    cx, cy = poly.centroid.coords[0]

    ring = list(poly.exterior.coords)
    edges = [math.dist(ring[i], ring[i + 1]) for i in range(len(ring) - 1)]
    mean_e = sum(edges) / len(edges)
    edge_var = sum((L - mean_e) ** 2 for L in edges) / len(edges)
    w, h = maxx - minx, maxy - miny

    props = {
        "vertex_count": len(ring) - 1,
        "bbox": [round(minx, 4), round(miny, 4), round(maxx, 4), round(maxy, 4)],
        "centroid": [round(cx, 4), round(cy, 4)],
        "area": round(poly.area, 4),
        "perimeter": round(poly.length, 4),
        "convex": bool(is_convex(poly)),
        "aspect_ratio": round(w / h, 4) if h else None,
        "edge_length_variance": round(edge_var, 4),
    }
    return props, math.dist((minx, miny), (maxx, maxy))


def set_winding(poly, want):
    current = "ccw" if shapely.is_ccw(poly.exterior) else "cw"
    if current == want:
        return poly, False
    return Polygon(list(poly.exterior.coords)[::-1]), True


def to_wkt(poly):
    parts = [f"{int(round(x))} {int(round(y))}" for x, y in poly.exterior.coords]
    return "POLYGON((" + ", ".join(parts) + "))"


def build_record(poly, slot, tier, index, extent, aspect, attempts, n_target,
                 params, pass_):
    """PDF 3.2 fixes the order: compute every orientation-independent property,
    THEN rewind the ring, THEN read orientation off the ring that is actually
    serialized. Doing it the other way round reports the winding of a polygon
    the model never sees."""
    props, bbox_diag = compute_orientation_independent(poly)      # step 1
    poly2, reversed_ = set_winding(poly, slot["winding"])         # step 2
    wkt = to_wkt(poly2)                                           # step 3
    props["orientation"] = "ccw" if shapely.is_ccw(poly2.exterior) else "cw"

    off = centroid_offset_norm(poly2)
    fill = fill_ratio(poly2)
    return {
        "object_id": f"poly_{tier}_{slot['shape_type']}_{index:03d}",
        "tier": tier,
        "shape_type": slot["shape_type"],
        "num_vertices": props["vertex_count"],
        "wkt": wkt,
        "properties": props,
        "metadata": {
            "coordinate_type": COORD_TYPE,
            "coordinate_range": [COORD_LO, COORD_HI],
            "is_valid": bool(poly2.is_valid),
            "is_simple": bool(poly2.is_simple),
            "orientation_was_reversed": reversed_,
            "random_seed": SEED,
            "bbox_diagonal": round(bbox_diag, 4),
            # --- the two balanced axes, and the band each record was built for
            "centroid_offset_norm": round(off, 4),
            "offset_band": slot["offset_band"],
            "fill_ratio": round(fill, 4),
            "fill_band": slot["fill_band"],
            # --- the other free ride on `centroid`, reported for stratifying
            "vertex_mean_offset_norm": round(vertex_mean_offset_norm(poly2), 4),
            # --- the prescribed ladder rungs this record consumed
            "target_extent": round(extent, 4),
            "target_aspect": round(aspect, 4),
            "target_area_fraction": round(poly2.area / (COORD_SPAN ** 2), 5),
            # --- family and provenance
            "generator": slot["family"],
            "family": slot["family"],
            "vertex_target": slot["vertex_target"],
            "vertex_target_met": slot["vertex_target"] == props["vertex_count"],
            "generation_attempts": attempts,
            "assignment_pass": pass_,
            "generator_params": {k: (round(v, 4) if isinstance(v, float) else v)
                                 for k, v in params.items()},
        },
    }


def build_dataset(seed=SEED):
    rng = random.Random(seed)
    records = []
    log = []
    for tier in TIER_ORDER:
        built = build_tier(tier, rng, log)
        # Records are emitted grouped by shape_type and numbered within the
        # group, matching v6's object_id scheme.
        by_shape = defaultdict(list)
        for item in built:
            by_shape[item[0]["shape_type"]].append(item)
        for shape in SHAPES:
            for i, (slot, poly, params, extent, aspect, attempts, n_ach, pass_) in \
                    enumerate(by_shape[shape], start=1):
                records.append(build_record(poly, slot, tier, i, extent, aspect,
                                            attempts, n_ach, params, pass_))
        print(f"  {tier}: {len(built)} records", flush=True)
    return records, log


# =============================================================================
# 9. Summary
# =============================================================================

def stats_for(values, ndigits=2):
    v = sorted(values)
    return {
        "min": round(v[0], ndigits),
        "max": round(v[-1], ndigits),
        "mean": round(statistics.fmean(v), ndigits),
        "median": round(statistics.median(v), ndigits),
        "std": round(statistics.pstdev(v), ndigits) if len(v) > 1 else 0.0,
    }


def summarize(records, log):
    by_tier = {t: [r for r in records if r["tier"] == t] for t in TIER_ORDER}

    def tier_counter(key):
        return {t: dict(sorted(Counter(key(r) for r in by_tier[t]).items(),
                               key=lambda kv: str(kv[0])))
                for t in TIER_ORDER}

    joint = {}
    for t in TIER_ORDER:
        cells = Counter((r["shape_type"], r["metadata"]["offset_band"],
                         r["metadata"]["fill_band"]) for r in by_tier[t])
        joint[t] = {f"{s}|{ob},{fb}": n for (s, ob, fb), n in sorted(cells.items())}

    dist = {}
    for t in TIER_ORDER:
        R = by_tier[t]
        dist[t] = {
            "vertex_count": stats_for([r["num_vertices"] for r in R]),
            "area": stats_for([r["properties"]["area"] for r in R]),
            "perimeter": stats_for([r["properties"]["perimeter"] for r in R]),
            "bbox_diagonal": stats_for([r["metadata"]["bbox_diagonal"] for r in R]),
            "aspect_ratio": stats_for([r["properties"]["aspect_ratio"] for r in R]),
            "edge_length_variance": stats_for(
                [r["properties"]["edge_length_variance"] for r in R]),
            "centroid_offset_norm": stats_for(
                [r["metadata"]["centroid_offset_norm"] for r in R], 4),
            "fill_ratio": stats_for([r["metadata"]["fill_ratio"] for r in R], 4),
            "vertex_mean_offset_norm": stats_for(
                [r["metadata"]["vertex_mean_offset_norm"] for r in R], 4),
            "wkt_length": stats_for([float(len(r["wkt"])) for r in R]),
        }

    met = Counter((r["tier"], r["metadata"]["vertex_target_met"]) for r in records)
    return {
        "version": 7,
        "seed": SEED,
        "shapely_version": shapely.__version__,
        "total": len(records),
        "tier_design": "protocol_int",
        "family_design": "equal_quota_25_per_family_per_tier",
        "shape_design": "balanced_50_25_25",
        "tiers": {t: {"vertices": list(TIERS[t]),
                      "coordinate_range": [COORD_LO, COORD_HI],
                      "coordinate_type": COORD_TYPE} for t in TIER_ORDER},
        "families": FAMILIES,
        "family_shape_map": FAMILY_SHAPE,
        "joint_band_quota": {s: {f"{ob},{fb}": n for (ob, fb), n in q.items()}
                             for s, q in JOINT_QUOTA.items()},
        "family_cell_plan": {f"{ob},{fb}": {"convex_smooth": sm, "convex_sharp": sh}
                             for (ob, fb), (sm, sh) in FAMILY_CELL_PLAN.items()},
        "min_vertices_for_fill": {f: {str(k): v for k, v in d.items()}
                                  for f, d in MIN_VERTICES_FOR_FILL.items()},
        "band_edges": {"offset": [list(b) for b in OFFSET_BANDS],
                       "fill": [list(b) for b in FILL_BANDS]},
        "size_ladder": {
            "extent_range": [round(EXTENT_LO, 4), round(EXTENT_HI, 4)],
            "aspect_range": [round(ASPECT_LO, 4), round(ASPECT_HI, 4)],
            "rungs_per_tier": PER_TIER,
            "pairing_seed": 20260915,
            "note": "extent and aspect are dealt as fixed pairs; every tier "
                    "consumes the identical multiset of pairs, hence the "
                    "identical multiset of bbox diagonals",
            "implied_diagonal": stats_for([pair_diagonal(e, a) for e, a in SIZE_LADDER]),
            "pool_sizes_by_fill_band": {str(k): len(v) for k, v in SIZE_POOLS.items()},
        },
        "counts_by_tier_shape": tier_counter(lambda r: r["shape_type"]),
        "counts_by_tier_family": tier_counter(lambda r: r["metadata"]["family"]),
        "convex_overall": sum(1 for r in records if r["properties"]["convex"]),
        "convex_by_tier": tier_counter(lambda r: r["properties"]["convex"]),
        "orientation_balance": tier_counter(lambda r: r["properties"]["orientation"]),
        "offset_bands_by_tier": tier_counter(lambda r: r["metadata"]["offset_band"]),
        "fill_bands_by_tier": tier_counter(lambda r: r["metadata"]["fill_band"]),
        "joint_cells_by_tier": joint,
        "vertex_counts_by_tier": tier_counter(lambda r: r["num_vertices"]),
        "vertex_target_met_by_tier": {
            t: {"met": met[(t, True)], "walked_down": met[(t, False)]}
            for t in TIER_ORDER},
        "distribution_by_tier": dist,
        "generation_attempts": {
            "total": sum(e["attempts"] for e in log),
            "max_for_one_record": max(e["attempts"] for e in log),
            "median": statistics.median([e["attempts"] for e in log]),
        },
        # Answer-without-reading floors, and the expectations carried over from
        # v6. See section 9b for how each is scored and why the aspect_ratio one
        # differs from the number v6 published.
        "baselines": all_baselines(records),
        "predictions": PREDICTIONS,
    }


# =============================================================================
# 9b. Published floors — what an answer that never reads the polygon scores
# =============================================================================
#
# Every accuracy in Phase 3 has to be read against the score a model would get
# by ignoring the WKT string and emitting a fixed guess. Publishing those rates
# HERE, in the dataset, rather than letting the evaluator invent them, is what
# makes them pre-registered: they are a function of the corpus alone and no
# model output enters them.
#
# GRADED THE WAY THE MODEL IS GRADED. Each floor is scored with the identical
# rule run_*_full_v8.py applies to a model's answer:
#
#   scalar properties        rel = |guess - truth| / |truth|,  correct if <= tol
#   bbox / centroid          point error / bbox diagonal,      correct if <= tol
#   convex / orientation     exact match
#   vertex_count             exact match
#
# This is worth stating because new_data_v6 did NOT do it for one property.
# Its `aspect_ratio_const_1.0` floor was computed as |1 - a| — an absolute
# difference on a quantity the runner grades by RELATIVE error. The two agree
# only near a = 1, and the published v6 floor is understated: recomputed under
# the runner's rule, v6's 10% simple-tier floor is 0.12, not the 0.10 it
# published. v7 uses the runner's rule throughout, so a model's margin over the
# floor is a comparison between two numbers that mean the same thing.
#
# NO FLOOR IS PUBLISHED FOR bbox OR edge_length_variance. There is no fixed
# guess for either that is defensible on this corpus, and inventing one would
# put a number in a figure that nothing stands behind. They are reported
# without a floor instead.

def _rel_err(guess, truth):
    """The runner's relative_scalar_error, verbatim."""
    ae = abs(float(guess) - float(truth))
    den = abs(float(truth))
    return ae if den == 0 else ae / den


def _bbox_wh(rec):
    b = rec["properties"]["bbox"]
    return b[2] - b[0], b[3] - b[1]


def baselines_for(records, tol):
    """{shortcut name: rate} over one tier's records at one tolerance."""
    n = len(records)
    if not n:
        return {}

    def frac(pred):
        return round(sum(1 for r in records if pred(r)) / n, 4)

    # --- exact-match properties: the majority class is the best fixed guess ---
    n_convex = sum(1 for r in records if r["properties"]["convex"])
    convex_majority = max(n_convex, n - n_convex) / n
    orients = [r["properties"]["orientation"] for r in records]
    orient_majority = max(orients.count("ccw"), orients.count("cw")) / n

    # The best fixed vertex-count guess is the tier's modal count. v6 published
    # `vertex_count_plus_one` instead, which is the true count plus one and is
    # therefore wrong on every record by construction - a floor of 0.0 that
    # tells the reader nothing. The modal guess is the floor that actually
    # bites, and it is exactly the quantity v7's flat vertex ladder is meant to
    # push down: a tier that piles up on one count hands a guesser that count.
    # Both are published, the old one for continuity with the v6 evaluation.
    counts = Counter(r["num_vertices"] for r in records)
    modal_rate = max(counts.values()) / n

    out = {
        "convex_majority": round(convex_majority, 4),
        "orientation_majority": round(orient_majority, 4),
        "vertex_count_modal": round(modal_rate, 4),
        "vertex_count_plus_one": 0.0,
        "centroid_bbox_center":
            frac(lambda r: r["metadata"]["centroid_offset_norm"] <= tol),
        "centroid_vertex_mean":
            frac(lambda r: r["metadata"]["vertex_mean_offset_norm"] <= tol),
        "aspect_ratio_const_1.0":
            frac(lambda r: _rel_err(1.0, r["properties"]["aspect_ratio"]) <= tol),
    }

    def bbox_area(r):
        w, h = _bbox_wh(r)
        return w * h

    def bbox_perim(r):
        w, h = _bbox_wh(r)
        return 2 * (w + h)

    out["area_bbox_area"] = frac(
        lambda r: _rel_err(bbox_area(r), r["properties"]["area"]) <= tol)
    # The largest ellipse that fits the bounding box has area pi/4 of it. On a
    # corpus of roundish many-vertex polygons this is a good guess, which is
    # precisely why it has to be published rather than left for a model to find
    # quietly.
    out["area_ellipse_in_bbox"] = frac(
        lambda r: _rel_err(math.pi / 4 * bbox_area(r), r["properties"]["area"]) <= tol)
    out["perimeter_bbox_perimeter"] = frac(
        lambda r: _rel_err(bbox_perim(r), r["properties"]["perimeter"]) <= tol)
    return out


BASELINE_TOLERANCES = {"tol_0.01": 0.01, "tol_0.05": 0.05, "tol_0.1": 0.10,
                       "tol_0.25": 0.25}


def all_baselines(records):
    by_tier = {t: [r for r in records if r["tier"] == t] for t in TIER_ORDER}
    return {
        key: {t: baselines_for(by_tier[t], tol) for t in TIER_ORDER}
        for key, tol in BASELINE_TOLERANCES.items()
    }


# Carried over VERBATIM from new_data_v6's summary. It is NOT a fresh
# pre-registration: v7's model runs were already complete when this block was
# added to the generator, so a prediction written now could have been shaped by
# them. It is kept because the property set and the tier axis are unchanged
# from v6, which makes it a continuity check worth printing - and it is labelled
# so no reader mistakes it for a sealed prediction about v7.
PREDICTIONS = {
    "provenance": (
        "copied unchanged from new_data_v6/geometry_exp1_summary.json. NOT "
        "re-registered for v7: the v8 model runs had already finished when this "
        "block was written. Read prediction-vs-actual as continuity with the v6 "
        "expectations, not as a pre-registration test."
    ),
    "tier_effect_expected": {
        "vertex_count": "monotone decrease (per-vertex pass; report strict and n/n+1-tolerant)",
        "bbox": "monotone decrease driven by serialization length (min/max over more numbers)",
        "perimeter": "monotone decrease (sum of n square roots)",
        "area": "monotone decrease on the continuous metric; pass rate at 5% low",
        "edge_length_variance": "monotone decrease; pass rate near floor",
        "centroid": "at 5% of the diagonal this is a coarse task; report the 1% "
                    "tolerance and the baseline-subtracted accuracy; no strong "
                    "tier trend is expected",
        "aspect_ratio": "weak decrease; depends on bbox recovery only",
        "convex": "flat at the 50% majority baseline for models that do not run "
                  "the cross-product test",
        "orientation": "flat at the 50% majority baseline; no model attempts the "
                       "signed-area test",
    },
    "local_vs_global": "local properties (vertex_count, bbox) above global ones at every tier",
    "metrics": [
        "pass rate at 1/5/10/25% relative tolerance",
        "median absolute relative error",
        "baseline-subtracted accuracy",
        "off-by-one rate for vertex_count",
    ],
    "v7_specific_note": (
        "v7 changes two things that bear on these expectations. The bbox "
        "diagonal is now flat across tiers to 0.01%, so a tier difference in "
        "bbox or centroid can no longer come from a wider grading band. And the "
        "hard tier's vertex counts are spread across all 21 values instead of "
        "piling on 20, so `vertex_count_modal` - the floor a guesser gets - "
        "falls with tier rather than rising."
    ),
}


# =============================================================================
# 10. Independent verification — recompute everything from the WKT string
# =============================================================================

def parse_wkt(wkt):
    body = wkt[wkt.index("((") + 2: wkt.rindex("))")]
    return [tuple(float(v) for v in p.strip().split()) for p in body.split(",")]


def _shoelace_signed(pts):
    s = 0.0
    for i in range(len(pts) - 1):
        s += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return s / 2.0


def _centroid(pts):
    a = _shoelace_signed(pts)
    if a == 0:
        return None
    cx = cy = 0.0
    for i in range(len(pts) - 1):
        cross = pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
        cx += (pts[i][0] + pts[i + 1][0]) * cross
        cy += (pts[i][1] + pts[i + 1][1]) * cross
    return cx / (6 * a), cy / (6 * a)


def _is_convex(pts):
    ring = pts[:-1]
    n = len(ring)
    signs = []
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        cx, cy = ring[(i + 2) % n]
        cr = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        if cr != 0:
            signs.append(cr > 0)
    return len(set(signs)) == 1


def verify(records):
    """Re-derive all 9 properties from the serialized string alone, with plain
    arithmetic rather than shapely, and compare. Nothing here reads the
    polygon object the generator held — if the WKT and the properties ever
    disagree, this is what catches it."""
    problems = []
    for r in records:
        pts = parse_wkt(r["wkt"])
        p = r["properties"]
        ring = pts[:-1]

        if pts[0] != pts[-1]:
            problems.append((r["object_id"], "ring_not_closed", None, None))
        if len(ring) != p["vertex_count"]:
            problems.append((r["object_id"], "vertex_count", len(ring), p["vertex_count"]))

        xs = [q[0] for q in ring]
        ys = [q[1] for q in ring]
        bbox = [min(xs), min(ys), max(xs), max(ys)]
        if [round(v, 4) for v in bbox] != p["bbox"]:
            problems.append((r["object_id"], "bbox", bbox, p["bbox"]))

        area = abs(_shoelace_signed(pts))
        if abs(area - p["area"]) > 0.01:
            problems.append((r["object_id"], "area", area, p["area"]))

        per = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        if abs(per - p["perimeter"]) > 0.01:
            problems.append((r["object_id"], "perimeter", per, p["perimeter"]))

        c = _centroid(pts)
        if c is None or max(abs(c[0] - p["centroid"][0]), abs(c[1] - p["centroid"][1])) > 0.01:
            problems.append((r["object_id"], "centroid", c, p["centroid"]))

        if _is_convex(pts) != p["convex"]:
            problems.append((r["object_id"], "convex", _is_convex(pts), p["convex"]))

        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if h and abs(w / h - p["aspect_ratio"]) > 1e-3:
            problems.append((r["object_id"], "aspect_ratio", w / h, p["aspect_ratio"]))

        edges = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        me = sum(edges) / len(edges)
        ev = sum((L - me) ** 2 for L in edges) / len(edges)
        if abs(ev - p["edge_length_variance"]) > 0.05:
            problems.append((r["object_id"], "edge_length_variance", ev,
                             p["edge_length_variance"]))

        orient = "ccw" if _shoelace_signed(pts) > 0 else "cw"
        if orient != p["orientation"]:
            problems.append((r["object_id"], "orientation", orient, p["orientation"]))

        # Validity, re-checked from the string.
        poly = Polygon(ring)
        ok, reason = check_validity(poly, *TIERS[r["tier"]])
        if not ok:
            problems.append((r["object_id"], f"validity:{reason}", None, None))

        # Band membership, re-checked from the string.
        if offset_band_of(poly) != r["metadata"]["offset_band"]:
            problems.append((r["object_id"], "offset_band",
                             offset_band_of(poly), r["metadata"]["offset_band"]))
        if fill_band_of(poly) != r["metadata"]["fill_band"]:
            problems.append((r["object_id"], "fill_band",
                             fill_band_of(poly), r["metadata"]["fill_band"]))
    return problems


def check_balance(records, summary):
    """The hard constraints, asserted against the emitted records."""
    fails = []

    def want(label, got, expected):
        if got != expected:
            fails.append(f"{label}: got {got}, want {expected}")

    for t in TIER_ORDER:
        R = [r for r in records if r["tier"] == t]
        want(f"{t} total", len(R), PER_TIER)
        want(f"{t} family",
             dict(sorted(Counter(r["metadata"]["family"] for r in R).items())),
             {f: PER_FAMILY_PER_TIER for f in sorted(FAMILIES)})
        want(f"{t} shape",
             dict(sorted(Counter(r["shape_type"] for r in R).items())),
             {"concave": 25, "convex": 50, "irregular": 25})
        want(f"{t} orientation",
             dict(sorted(Counter(r["properties"]["orientation"] for r in R).items())),
             {"ccw": 50, "cw": 50})
        want(f"{t} convex property",
             dict(sorted(Counter(r["properties"]["convex"] for r in R).items(),
                         key=lambda kv: str(kv[0]))),
             {False: 50, True: 50})
        want(f"{t} offset bands",
             dict(sorted(Counter(r["metadata"]["offset_band"] for r in R).items())),
             {0: 22, 1: 38, 2: 32, 3: 8})
        want(f"{t} fill bands",
             dict(sorted(Counter(r["metadata"]["fill_band"] for r in R).items())),
             {0: 14, 1: 37, 2: 36, 3: 13})
        for shape in SHAPES:
            got = Counter((r["metadata"]["offset_band"], r["metadata"]["fill_band"])
                          for r in R if r["shape_type"] == shape)
            want(f"{t} joint/{shape}", dict(sorted(got.items())),
                 dict(sorted(JOINT_QUOTA[shape].items())))

    # The size ladder: every tier must consume the same multiset of
    # (extent, aspect) PAIRS, which is what makes the bbox-diagonal
    # distribution identical rather than merely similar.
    ref_pairs = ref_diag = None
    for t in TIER_ORDER:
        R = [r for r in records if r["tier"] == t]
        pr = sorted((r["metadata"]["target_extent"], r["metadata"]["target_aspect"])
                    for r in R)
        dg = sorted(round(pair_diagonal(e, a), 6) for e, a in pr)
        if ref_pairs is None:
            ref_pairs, ref_diag = pr, dg
            continue
        if pr != ref_pairs:
            fails.append(f"{t}: size ladder pairs differ from {TIER_ORDER[0]}")
        if dg != ref_diag:
            fails.append(f"{t}: implied bbox diagonals differ from {TIER_ORDER[0]}")

    # And the same multiset within every fill band, which is what keeps the
    # fill-ratio/size correlation — and therefore `area` — flat across tiers.
    ref_band = None
    for t in TIER_ORDER:
        R = [r for r in records if r["tier"] == t]
        per = {}
        for fb in sorted(FILL_BAND_QUOTA):
            per[fb] = sorted((r["metadata"]["target_extent"], r["metadata"]["target_aspect"])
                             for r in R if r["metadata"]["fill_band"] == fb)
        if ref_band is None:
            ref_band = per
        elif per != ref_band:
            bad = [fb for fb in per if per[fb] != ref_band[fb]]
            fails.append(f"{t}: size pool differs from {TIER_ORDER[0]} in fill bands {bad}")
    return fails


# =============================================================================
# 11. Spotcheck image
# =============================================================================

def spotcheck(records, path):
    """One polygon per (tier x family), drawn zoomed to its own bounding box.

    Zoomed rather than shown inside the full [0, 1000] box: the size ladder
    puts most polygons at an extent of 0.1-0.3, so on a full-box axis they are
    a few pixels across and the picture proves nothing. The title carries the
    quantities the box would have shown — extent, position, bands — and the
    outline is what the eye is actually here to check.

    The representative picked for each cell is the one whose vertex count is
    closest to that cell's median, so the grid shows typical records rather
    than whichever happened to be emitted first."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib missing; skipping spotcheck", file=sys.stderr)
        return False

    fig, axes = plt.subplots(len(TIER_ORDER), len(FAMILIES),
                             figsize=(3.6 * len(FAMILIES), 3.9 * len(TIER_ORDER)))
    for ri, tier in enumerate(TIER_ORDER):
        for ci, fam in enumerate(FAMILIES):
            ax = axes[ri][ci]
            pool = [r for r in records
                    if r["tier"] == tier and r["metadata"]["family"] == fam]
            if not pool:
                ax.axis("off")
                continue
            mid = statistics.median([r["num_vertices"] for r in pool])
            pick = min(pool, key=lambda r: abs(r["num_vertices"] - mid))

            pts = parse_wkt(pick["wkt"])
            xs = [q[0] for q in pts]
            ys = [q[1] for q in pts]
            ax.fill(xs, ys, alpha=0.35, edgecolor="black", linewidth=1.3)
            ax.plot(xs, ys, "o", ms=3, color="black")

            b = pick["properties"]["bbox"]
            cx, cy = pick["properties"]["centroid"]
            ax.plot([cx], [cy], "x", ms=9, color="crimson", mew=2)
            ax.plot([(b[0] + b[2]) / 2], [(b[1] + b[3]) / 2], "+", ms=9,
                    color="darkblue", mew=2)

            pad = 0.08 * max(b[2] - b[0], b[3] - b[1], 1.0)
            ax.set_xlim(b[0] - pad, b[2] + pad)
            ax.set_ylim(b[1] - pad, b[3] + pad)
            ax.set_aspect("equal")
            ax.tick_params(labelsize=6)

            m = pick["metadata"]
            ax.set_title(
                f"{tier} / {fam}   n={pick['num_vertices']}\n"
                f"offset_b={m['offset_band']} ({m['centroid_offset_norm']:.3f})   "
                f"fill_b={m['fill_band']} ({m['fill_ratio']:.2f})\n"
                f"{pick['properties']['orientation']}   extent={m['target_extent']:.2f}   "
                f"aspect={m['target_aspect']:.2f}",
                fontsize=8)

    fig.suptitle("new_data_v7 - one polygon per (tier x family), zoomed to its bbox\n"
                 "x = true centroid,  + = bbox centre  (their gap is the offset band)",
                 fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return True


# =============================================================================
# 12. Main
# =============================================================================

def main():
    if "--spotcheck-only" in sys.argv:
        recs = json.loads((OUT_DIR / "geometry_exp1_dataset.json").read_text())
        spotcheck(recs, OUT_DIR / "spotcheck_v7.png")
        print("  spotcheck_v7.png redrawn")
        return 0

    if "--summary-only" in sys.argv:
        # Rewrite the summary from the dataset already on disk, leaving the
        # dataset itself untouched. Used when a block is ADDED to the summary
        # (the baselines and predictions of section 9b were) and a full rebuild
        # would be eight minutes spent reproducing a file byte for byte.
        #
        # generation_attempts cannot be recomputed without the build log, so the
        # existing values are carried across rather than dropped or faked.
        ds_path = OUT_DIR / "geometry_exp1_dataset.json"
        sm_path = OUT_DIR / "geometry_exp1_summary.json"
        recs = json.loads(ds_path.read_text())
        prev = json.loads(sm_path.read_text()) if sm_path.exists() else {}
        fake_log = [{"attempts": 1}]
        summary = summarize(recs, fake_log)
        if "generation_attempts" in prev:
            summary["generation_attempts"] = prev["generation_attempts"]
        fails = check_balance(recs, summary)
        problems = verify(recs)
        for f in fails:
            print(f"  BALANCE FAIL  {f}", file=sys.stderr)
        for oid, what, got, want_ in problems[:20]:
            print(f"  VERIFY FAIL  {oid}  {what}  got={got} want={want_}", file=sys.stderr)
        summary["verification"] = {"balance_failures": len(fails),
                                   "property_mismatches": len(problems)}
        sm_path.write_text(json.dumps(summary, indent=2))
        print(f"  {sm_path.name} rewritten from {len(recs)} records "
              f"(dataset untouched)")
        print(f"  balance failures    : {len(fails)}")
        print(f"  property mismatches : {len(problems)}")
        return 1 if (fails or problems) else 0
    print("Generating 300 polygons (v7)...", flush=True)
    records, log = build_dataset(SEED)
    assert len(records) == 3 * PER_TIER, len(records)

    print("Summarizing...", flush=True)
    summary = summarize(records, log)

    print("Checking balance...", flush=True)
    fails = check_balance(records, summary)
    for f in fails:
        print(f"  BALANCE FAIL  {f}", file=sys.stderr)

    print("Verifying properties from WKT...", flush=True)
    problems = verify(records)
    for oid, what, got, want_ in problems[:40]:
        print(f"  VERIFY FAIL  {oid}  {what}  got={got} want={want_}", file=sys.stderr)
    if len(problems) > 40:
        print(f"  ... and {len(problems) - 40} more", file=sys.stderr)

    ds_path = OUT_DIR / "geometry_exp1_dataset.json"
    sm_path = OUT_DIR / "geometry_exp1_summary.json"
    summary["verification"] = {"balance_failures": len(fails),
                               "property_mismatches": len(problems)}
    ds_path.write_text(json.dumps(records, indent=2))
    sm_path.write_text(json.dumps(summary, indent=2))
    ok = spotcheck(records, OUT_DIR / "spotcheck_v7.png")

    print()
    print(f"  {ds_path.name}  {len(records)} records")
    print(f"  {sm_path.name}")
    if ok:
        print("  spotcheck_v7.png")
    print()
    print(f"  balance failures    : {len(fails)}")
    print(f"  property mismatches : {len(problems)}")
    print(f"  total attempts      : {summary['generation_attempts']['total']}")
    print(f"  worst single record : {summary['generation_attempts']['max_for_one_record']}")
    return 1 if (fails or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
