"""
Experiment 2 - Geometry runtime (v2).  Written identically by the Phase 1 and
Phase 2 notebooks so pipeline construction and pipeline execution share one
definition of every operation, condition and scoring rule.

Changes from v1 (see PHASE1_README):
  * No auxiliary read tools in the four primary conditions.  Table 3 defines
    handle-only as "object id; no property information"; raw and augmented
    must force the model to reconstruct properties from text.  A `describe`
    read tool exists only for Experiment 4 and is switched on explicitly.
  * Gated steps name the admissible tools.  A call to any other tool is a
    DEFERRAL (the model tried to make the runtime compute the property) and is
    scored separately from a wrong decision.
  * buffer uses mitre joins so the vertex count, and therefore the tier, is
    preserved along a pipeline.
"""
import math
import random
import re

from shapely import affinity, delaunay_triangles, voronoi_polygons
from shapely import wkt as shapely_wkt
from shapely.geometry import MultiPoint, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import nearest_points as _nearest_points

WKT_PRECISION = 0            # integer grid: every object, initial or intermediate, is serialized with integer coordinates

# ===========================================================================
# Polygon generator (identical to Experiment 1 Phase 1 v3)
# ===========================================================================


# A tier is (vmin, vmax, coord_lo, coord_hi, coord_type).  coord_type "int"
# rounds to integers, "float2" to two decimals.
TIERS = {"simple": (3, 8, 0.0, 1000.0, "int"),      # protocol vertex ranges, integer grid in every tier
         "medium": (10, 20, 0.0, 1000.0, "int"),
         "hard":   (20, 40, 0.0, 1000.0, "int")}
PRECISION = WKT_PRECISION
# Target area is a FRACTION of the coordinate span squared, log-uniform, so it
# is matched across tiers whether or not the tiers share a coordinate range.
AREA_FRAC_LO, AREA_FRAC_HI = 0.004, 0.12
# Aspect-ratio targets (bbox width / height), log-uniform.  Irregular polygons
# are elongated (stretch 2-5 on one axis, either axis) as in the protocol.
ASPECT_RANGE = {"convex": (0.5, 2.0), "concave": (0.5, 2.0), "irregular": (2.0, 5.0)}
MIN_FILL = 0.05                              # area / bbox area
MIN_HULL_DEFICIT = 0.03                      # non-convex polygons must miss >= 3% of their hull area


# ---------------------------------------------------------------------------
# Unit-scale shape generators.  All take (rng, n, bias) and return a Polygon
# centred near the origin.  `bias` in [0, 0.5] pushes the centroid away from
# the bbox centre in a random direction (limacon r = 1 + bias*cos(theta)).
# ---------------------------------------------------------------------------

def _angles(rng, n, alpha=0.7):
    """n angles around the circle.  Gaps are a mix of a uniform share and a
    random exponential share, so the minimum gap is at least (1-alpha)*2pi/n
    (no near-coincident vertices) while edge lengths still vary a lot."""
    e = [rng.expovariate(1.0) for _ in range(n)]
    tot = sum(e)
    gaps = [2 * math.pi * ((1 - alpha) / n + alpha * ei / tot) for ei in e]
    start = rng.uniform(0, 2 * math.pi)
    ang, t = [], start
    for g in gaps[:-1]:
        t += g
        ang.append(t % (2 * math.pi))
    ang.append(start)
    return sorted(ang)


def _limacon(theta, bias, phi):
    return 1.0 + bias * math.cos(theta - phi)


def unit_convex(rng, n, bias):
    """Vertices on the convex limacon r = 1 + b cos(t), b <= 0.5, in angular
    order: exact vertex count, guaranteed convex."""
    ang = _angles(rng, n)
    if ang is None:
        return None
    phi = rng.uniform(0, 2 * math.pi)
    return Polygon([(_limacon(t, bias, phi) * math.cos(t),
                     _limacon(t, bias, phi) * math.sin(t)) for t in ang])


def unit_concave(rng, n, bias):
    """Star-shaped by angular sweep with random radii; mild dents."""
    ang = _angles(rng, n)
    if ang is None:
        return None
    phi = rng.uniform(0, 2 * math.pi)
    rmin = rng.uniform(0.40, 0.75)
    return Polygon([(_limacon(t, bias, phi) * rng.uniform(rmin, 1.0) * math.cos(t),
                     _limacon(t, bias, phi) * rng.uniform(rmin, 1.0) * math.sin(t)) for t in ang])


def unit_irregular(rng, n, bias):
    """Protocol class 'irregular/elongated': radial generation with non-uniform
    vertex spacing and radii, followed (in place()) by an affine stretch of
    2-5 on one axis.  No spikes: every vertex sits on the same body, so the
    bbox diagonal is set by the body and not by a few outliers."""
    ang = _angles(rng, n, alpha=0.9)          # strongly non-uniform spacing
    phi = rng.uniform(0, 2 * math.pi)
    rmin = rng.uniform(0.45, 0.85)
    pts = []
    for t in ang:
        r = rng.uniform(rmin, 1.0) * _limacon(t, bias, phi)
        pts.append((r * math.cos(t), r * math.sin(t)))
    return Polygon(pts)


def unit_convex_sharp(rng, n, bias):
    """Points spread over the rounded corners of a random triangle or
    quadrilateral: exactly n vertices, convex, and as 'pointed' as `bias`
    makes it (bias 0 = nearly round, bias 0.5 = nearly the base polygon).
    Round shapes with many vertices have their centroid glued to the bbox
    centre; this generator is what lets a 40-vertex convex polygon have a
    centroid well away from it."""
    m = rng.choice([3, 3, 4])
    base = None
    for _ in range(30):
        pts = [(math.cos(t), math.sin(t)) for t in _angles(rng, m, alpha=0.6)]
        b = Polygon(pts)
        if b.is_valid and b.area > 0.4:
            base = b
            break
    if base is None:
        return None
    r = 0.95 - 1.4 * bias                        # corner radius 0.25..0.95 (tighter collapses on an integer grid)
    ring = list(base.exterior.coords)[:-1]
    arcs = []
    for i in range(m):
        p0, p1, p2 = ring[i - 1], ring[i], ring[(i + 1) % m]
        a0 = math.atan2(p1[1] - p0[1], p1[0] - p0[0]) - math.pi / 2   # outward normal of edge in
        a1 = math.atan2(p2[1] - p1[1], p2[0] - p1[0]) - math.pi / 2   # outward normal of edge out
        turn = (a1 - a0) % (2 * math.pi)
        arcs.append((p1, a0, turn))
    total = sum(a[2] for a in arcs)
    pts = []
    # allocate vertices to arcs in proportion to turn angle, at least one each
    alloc = [1] * m
    for _ in range(n - m):
        u = rng.uniform(0, total); acc = 0
        for i, a in enumerate(arcs):
            acc += a[2]
            if u <= acc:
                alloc[i] += 1
                break
        else:
            alloc[-1] += 1
    for (c, a0, turn), k in zip(arcs, alloc):
        # evenly spaced along the arc with jitter, so no two vertices collapse on the grid
        for i in range(k):
            t = (i + 0.5 + rng.uniform(-0.3, 0.3)) / k
            ang = a0 + t * turn
            pts.append((c[0] + r * math.cos(ang), c[1] + r * math.sin(ang)))
    hull = Polygon(pts).convex_hull
    if hull.geom_type != "Polygon" or len(hull.exterior.coords) - 1 != n:
        return None
    return hull


def projective_squeeze(poly, k, phi):
    """(x, y) -> (x, y) / (1 + k x) after rotating by -phi.  Projective maps
    preserve convexity and simplicity; this one turns a round shape into an
    egg and moves the centroid away from the bbox centre by an amount that
    grows with k.  The polygon is first normalised so |coord| <= 1."""
    ring = list(poly.exterior.coords)[:-1]
    R = max(math.hypot(x, y) for x, y in ring)
    c, s = math.cos(phi), math.sin(phi)
    out = []
    for x, y in ring:
        x, y = x / R, y / R
        u, v = c * x + s * y, -s * x + c * y
        d = 1 + k * u
        u, v = u / d, v / d
        out.append((c * u - s * v, s * u + c * v))
    return Polygon(out)


GENERATORS = {"convex": unit_convex, "concave": unit_concave, "irregular": unit_irregular}


# ---------------------------------------------------------------------------
# Placement: stretch to target aspect, rescale to target area, translate into
# bounds, round to PRECISION.  The rounded polygon is what gets validated.
# ---------------------------------------------------------------------------

def _round(v, ctype):
    return float(round(v)) if ctype == "int" else round(v, PRECISION)


def place(poly, target_area, target_aspect, rng, tier="hard"):
    vmin, vmax, lo, hi, ctype = TIERS[tier]
    if poly is None or poly.is_empty or poly.area <= 0:
        return None
    minx, miny, maxx, maxy = poly.bounds
    w, h = maxx - minx, maxy - miny
    if w <= 0 or h <= 0:
        return None
    poly = affinity.scale(poly, xfact=target_aspect / (w / h), yfact=1.0, origin="centroid")
    s = math.sqrt(target_area / poly.area)
    poly = affinity.scale(poly, xfact=s, yfact=s, origin="centroid")
    minx, miny, maxx, maxy = poly.bounds
    if (maxx - minx) >= (hi - lo) or (maxy - miny) >= (hi - lo):
        return None
    poly = affinity.translate(poly, xoff=rng.uniform(lo - minx, hi - maxx),
                                    yoff=rng.uniform(lo - miny, hi - maxy))
    return Polygon([(_round(x, ctype), _round(y, ctype)) for x, y in list(poly.exterior.coords)[:-1]])


# ---------------------------------------------------------------------------
# Validity gate.  Every rejection has a named reason.
# ---------------------------------------------------------------------------

def is_convex(poly, tol=1e-9):
    a = poly.area
    return a > 0 and abs(poly.convex_hull.area - a) <= tol * max(a, 1.0)


def check_validity(poly, tier, expected_convex):
    vmin, vmax, lo, hi, ctype = TIERS[tier]
    if poly is None:
        return False, "generation_failed"
    if not poly.is_valid:
        return False, "not_valid"
    if not poly.is_simple:
        return False, "not_simple"
    ring = list(poly.exterior.coords)[:-1]
    n = len(ring)
    if not (vmin <= n <= vmax):
        return False, "vertex_count_out_of_range"
    for i in range(n):
        if ring[i] == ring[(i + 1) % n]:
            return False, "duplicate_adjacent"
    crosses = []
    for i in range(n):
        (x0, y0), (x1, y1), (x2, y2) = ring[i - 1], ring[i], ring[(i + 1) % n]
        crosses.append((x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1))
    if min(abs(c) for c in crosses) < 1e-3:
        return False, "collinear"
    sign_convex = all(c > 0 for c in crosses) or all(c < 0 for c in crosses)
    if poly.area <= 10:
        return False, "area_too_small"
    minx, miny, maxx, maxy = poly.bounds
    if min(minx, miny) < lo or max(maxx, maxy) > hi:
        return False, "out_of_bounds"
    fr = poly.area / ((maxx - minx) * (maxy - miny))
    if fr < MIN_FILL:
        return False, "sliver_low_fill"
    if fr >= FILL_MAX:
        return False, "too_round_high_fill"
    if is_convex(poly) != expected_convex or sign_convex != expected_convex:
        return False, "convexity_mismatch"
    if not expected_convex and 1 - poly.area / poly.convex_hull.area < MIN_HULL_DEFICIT:
        return False, "dent_too_shallow"
    return True, None


# ---------------------------------------------------------------------------
# Nuisance quantities used for cross-tier matching.
# ---------------------------------------------------------------------------

def bbox_diagonal(poly):
    minx, miny, maxx, maxy = poly.bounds
    return math.hypot(maxx - minx, maxy - miny)


def centroid_offset_norm(poly):
    """Distance from the true centroid to the bbox centre, / bbox diagonal.
    This is the quantity that decides whether 'answer the middle of the box'
    passes a centroid question at a given tolerance."""
    minx, miny, maxx, maxy = poly.bounds
    c = poly.centroid
    return math.hypot(c.x - (minx + maxx) / 2, c.y - (miny + maxy) / 2) / bbox_diagonal(poly)


def fill_ratio(poly):
    minx, miny, maxx, maxy = poly.bounds
    return poly.area / ((maxx - minx) * (maxy - miny))


# Guessability bands.  Each (tier, shape) cell must contain the same number of
# polygons in each band, so no tier is more guessable than another.
#   offset band: distance centroid -> bbox centre, / bbox diagonal (centroid guessability)
#   fill band:   area / bbox area (area and perimeter guessability from the bbox)
OFFSET_BANDS = [(0.00, 0.02), (0.02, 0.04), (0.04, 0.07), (0.07, 10.0)]
FILL_BANDS = [(0.0, 0.52), (0.52, 0.62), (0.62, 0.70), (0.70, 0.76)]
FILL_MAX = 0.76                              # near-ellipses (fill -> pi/4) are excluded in every tier


def fill_band(fr):
    for i, (lo, hi) in enumerate(FILL_BANDS):
        if lo <= fr < hi:
            return i
    return len(FILL_BANDS) - 1


# Joint (offset band, fill band) cells and their share of each shape cell.
# Only cells reachable in EVERY tier are used (measured on the integer grid:
# e.g. a 40-vertex convex polygon cannot have both a far-off centroid and a
# low fill).  The same shares apply in all three tiers, so no tier is more
# guessable than another on either quantity.
JOINT_SHARES = {
    # convex: a 20-40 vertex convex polygon on the integer grid cannot have fill < 0.52,
    # so convex cells never use fill band 0 in any tier (triangles appear in the irregular class).
    "convex":    {(0, 1): 5, (0, 2): 5, (0, 3): 4, (1, 1): 6, (1, 2): 8, (1, 3): 4, (2, 1): 6, (2, 2): 7, (2, 3): 3, (3, 2): 2},
    "concave":   {(0, 1): 2, (0, 2): 2, (1, 0): 2, (1, 1): 4, (1, 2): 3, (1, 3): 1, (2, 0): 2, (2, 1): 4, (2, 2): 2, (3, 0): 3},
    "irregular": {(0, 1): 2, (0, 2): 2, (1, 0): 2, (1, 1): 4, (1, 2): 3, (1, 3): 1, (2, 0): 2, (2, 1): 4, (2, 2): 2, (3, 0): 3},
}


def joint_quota(shape, n):
    """Scale JOINT_SHARES[shape] to n slots (largest-remainder rounding)."""
    sh = JOINT_SHARES[shape]; tot = sum(sh.values())
    raw = {k: v * n / tot for k, v in sh.items()}
    q = {k: int(v) for k, v in raw.items()}
    for k, _ in sorted(raw.items(), key=lambda kv: -(kv[1] - int(kv[1])))[: n - sum(q.values())]:
        q[k] += 1
    return q


def joint_band(poly):
    return (offset_band(centroid_offset_norm(poly)), fill_band(fill_ratio(poly)))


def offset_band(d):
    for i, (lo, hi) in enumerate(OFFSET_BANDS):
        if lo <= d < hi:
            return i
    return len(OFFSET_BANDS) - 1


def span(tier):
    return TIERS[tier][3] - TIERS[tier][2]


def draw_targets(rng, shape):
    """(area fraction of span^2, aspect ratio) from the shared distributions."""
    af = math.exp(rng.uniform(math.log(AREA_FRAC_LO), math.log(AREA_FRAC_HI)))
    lo, hi = ASPECT_RANGE[shape]
    ar = math.exp(rng.uniform(math.log(lo), math.log(hi)))
    if shape == "irregular" and rng.random() < 0.5:
        ar = 1.0 / ar                      # stretch along either axis
    return af, ar


def make_one(rng, shape, tier, target_area, target_aspect, tries=300):
    """Rejection-sample one valid polygon of the given shape class and tier.
    The two shape knobs (limacon bias, projective squeeze) are drawn at random
    on every attempt from ranges shared across tiers.  Returns (poly, knobs,
    rejection_reasons)."""
    vmin, vmax = TIERS[tier][:2]
    reasons = {}
    for _ in range(tries):
        n = rng.randint(vmin, vmax)
        bias = rng.uniform(0.0, 0.5)
        squeeze = rng.uniform(0.0, 0.85)
        if shape == "convex" and rng.random() < 0.6:
            unit = unit_convex_sharp(rng, n, bias); gen = "convex_sharp"
        else:
            unit = GENERATORS[shape](rng, n, bias); gen = shape
        if unit is None:
            reasons["generation_failed"] = reasons.get("generation_failed", 0) + 1
            continue
        unit = projective_squeeze(unit, squeeze, rng.uniform(0, 2 * math.pi))
        p = place(unit, target_area, target_aspect, rng, tier)
        ok, why = check_validity(p, tier, shape == "convex")
        if ok:
            p = orient(p, 1.0)   # canonical ccw; Phase 1 flips exactly half
            return p, {"generator": gen, "bias": round(bias, 4), "squeeze": round(squeeze, 4)}, reasons
        reasons[why] = reasons.get(why, 0) + 1
    return None, None, reasons


# ===========================================================================
# The twelve specification operations
# ===========================================================================

def op_convex_hull(p):        return p.convex_hull
def op_buffer(p, distance):   return p.buffer(distance, join_style="mitre", mitre_limit=5.0)
def op_simplify(p, tol):      return p.simplify(tol, preserve_topology=True)
def op_rotate(p, angle):      return affinity.rotate(p, angle, origin="centroid")
def op_translate(p, dx, dy):  return affinity.translate(p, xoff=dx, yoff=dy)
def op_scale(p, factor):      return affinity.scale(p, xfact=factor, yfact=factor, origin="centroid")
def op_intersection(a, b):    return a.intersection(b)
def op_union(a, b):           return a.union(b)
def op_centroid(p):           return p.centroid
def op_triangulate(p):        return delaunay_triangles(MultiPoint(_verts(p)))
def op_voronoi(p):            return voronoi_polygons(MultiPoint(_verts(p)))
def op_nearest_points(a, b):  return _nearest_points(a, b)

def op_bounding_box(p):
    a, b, c, d = p.bounds
    return Polygon([(a, b), (c, b), (c, d), (a, d)])

def _verts(p):
    return list(p.exterior.coords)[:-1]


CHAIN_UNARY, CHAIN_BINARY, TERMINAL = "chain_unary", "chain_binary", "terminal"

OPS = {
    "convex_hull":    (op_convex_hull,    CHAIN_UNARY,  1, []),
    "buffer":         (op_buffer,         CHAIN_UNARY,  1, ["distance"]),
    "simplify":       (op_simplify,       CHAIN_UNARY,  1, ["tolerance"]),
    "bounding_box":   (op_bounding_box,   CHAIN_UNARY,  1, []),
    "rotate":         (op_rotate,         CHAIN_UNARY,  1, ["angle"]),
    "translate":      (op_translate,      CHAIN_UNARY,  1, ["dx", "dy"]),
    "scale":          (op_scale,          CHAIN_UNARY,  1, ["factor"]),
    "intersection":   (op_intersection,   CHAIN_BINARY, 2, []),
    "union":          (op_union,          CHAIN_BINARY, 2, []),
    "centroid":       (op_centroid,       TERMINAL,     1, []),
    "triangulate":    (op_triangulate,    TERMINAL,     1, []),
    "voronoi":        (op_voronoi,        TERMINAL,     1, []),
    "nearest_points": (op_nearest_points, TERMINAL,     2, []),
}
SPEC_OPS      = list(OPS)
CHAINABLE_OPS = [k for k, v in OPS.items() if v[1] in (CHAIN_UNARY, CHAIN_BINARY)]
TERMINAL_OPS  = [k for k, v in OPS.items() if v[1] == TERMINAL]
DESCRIBE_TOOL = "describe"          # Experiment 4 only

def n_operands(t):  return OPS[t][2]
def arg_names(t):   return OPS[t][3]
def is_terminal(t): return OPS[t][1] == TERMINAL


def to_wkt(poly, precision=WKT_PRECISION):
    if precision == 0:
        body = ", ".join(f"{int(round(x))} {int(round(y))}" for x, y in poly.exterior.coords)
    else:
        body = ", ".join(f"{x:.{precision}f} {y:.{precision}f}" for x, y in poly.exterior.coords)
    return f"POLYGON(({body}))"


def snap(poly):
    """Round to serialization precision so the stored form is the computed form."""
    return shapely_wkt.loads(to_wkt(poly))


def is_runtime_valid(poly):
    try:
        return (poly is not None and not poly.is_empty and poly.geom_type == "Polygon"
                and poly.is_valid and poly.is_simple and poly.area > 0)
    except Exception:
        return False


def apply_op(tool, operands, args, strict=False):
    """strict=True (pipeline construction): a non-Polygon result is an error
    rather than being replaced by its convex hull."""
    fn, klass, n_ops, names = OPS[tool]
    if len(operands) != n_ops:
        raise ValueError(f"{tool} takes exactly {n_ops} object argument(s), got {len(operands)}")
    if len(args) != len(names):
        raise ValueError(f"{tool} takes exactly {len(names)} numeric argument(s), got {len(args)}")
    out = fn(*operands, *args)
    if klass == TERMINAL:
        return out
    if out.geom_type != "Polygon":
        if strict:
            raise ValueError(f"{tool} produced {out.geom_type}")
        out = out.convex_hull
    return snap(orient(out, 1.0))


# ===========================================================================
# Properties and the deterministic summary (Table 3)
# ===========================================================================

def properties(poly):
    minx, miny, maxx, maxy = poly.bounds
    ring = list(poly.exterior.coords)
    edges = [math.dist(ring[i], ring[i + 1]) for i in range(len(ring) - 1)]
    mean_e = sum(edges) / len(edges)
    w, h = maxx - minx, maxy - miny
    return {
        "vertex_count": len(ring) - 1,
        "area": round(poly.area, 4),
        "perimeter": round(poly.length, 4),
        "width": round(w, 4),
        "height": round(h, 4),
        "aspect_ratio": round(w / h, 4),
        "edge_length_variance": round(sum((e - mean_e) ** 2 for e in edges) / len(edges), 4),
        "bbox": [round(minx, 4), round(miny, 4), round(maxx, 4), round(maxy, 4)],
        "centroid": [round(poly.centroid.x, 4), round(poly.centroid.y, 4)],
        "centroid_y": round(poly.centroid.y, 4),
        "convex": bool(is_convex(poly)),
        "is_simple": bool(poly.is_simple),
    }


GATE_VALUE = {"area": lambda q: q["area"], "perimeter": lambda q: q["perimeter"],
              "aspect_ratio": lambda q: q["aspect_ratio"],
              "edge_length_variance": lambda q: q["edge_length_variance"],
              "centroid_y": lambda q: q["centroid_y"],
              "width": lambda q: q["width"], "vertex_count": lambda q: q["vertex_count"]}
GATE_PHRASE = {"area": "area", "perimeter": "perimeter",
               "aspect_ratio": "aspect ratio (bounding-box width divided by bounding-box height)",
               "edge_length_variance": "variance of the edge lengths",
               "centroid_y": "northernmost centroid (largest centroid y coordinate)",
               "width": "horizontal extent (bounding-box width)", "vertex_count": "number of vertices"}


def summary_line(poly):
    """The deterministic property summary of Table 3.  It states PRIMITIVE
    measurements.  Composite quantities the pipeline may ask about (the aspect
    ratio = width / height) are deliberately not pre-computed: an augmented or
    handle+summary step still has to combine two stated numbers.  That is what a
    real framework summary looks like, and it means those conditions measure
    read-and-use rather than read-and-copy."""
    q = properties(poly)
    return ("[Properties]\n"
            f"  area = {q['area']}\n"
            f"  perimeter = {q['perimeter']}\n"
            f"  width = {q['width']}\n"
            f"  height = {q['height']}\n"
            f"  edge_length_variance = {q['edge_length_variance']}\n"
            f"  vertex_count = {q['vertex_count']}\n"
            f"  centroid = ({q['centroid'][0]}, {q['centroid'][1]})\n"
            f"  bbox = ({q['bbox'][0]}, {q['bbox'][1]}, {q['bbox'][2]}, {q['bbox'][3]})\n"
            f"  convex = {q['convex']}\n"
            f"  is_simple = {q['is_simple']}")


# ===========================================================================
# The four state-passing conditions (Table 3) and the two propagation modes
# ===========================================================================

CONDITIONS = ["raw", "augmented", "handle", "handle_sum"]
MODES = ["cascading", "oracle"]
TEXT_CONDITIONS = {"raw", "augmented"}
HANDLE_CONDITIONS = {"handle", "handle_sum"}


def operand_labels(visible_handles, condition):
    if condition in HANDLE_CONDITIONS:
        return {h: h for h in visible_handles}
    return {h: f"Polygon {chr(65 + i)}" for i, h in enumerate(visible_handles)}


def render_state(visible, condition):
    labels = operand_labels([h for h, _ in visible], condition)
    parts = []
    for h, p in visible:
        lab = labels[h]
        if condition == "raw":
            parts.append(f"{lab}:\n{to_wkt(p)}")
        elif condition == "augmented":
            parts.append(f"{lab}:\n{to_wkt(p)}\n{summary_line(p)}")
        elif condition == "handle":
            parts.append(lab)
        elif condition == "handle_sum":
            parts.append(f"{lab}\n{summary_line(p)}")
        else:
            raise ValueError(condition)
    return "\n\n".join(parts)


def render_instruction(template, visible_handles, condition):
    labels = operand_labels(visible_handles, condition)
    return template.format(**{f"o{i}": labels[h] for i, h in enumerate(visible_handles)})


def object_matches(ref, expected_poly, expected_handle, condition):
    """handle conditions: the id must match.  text conditions: the WKT must
    parse and agree with the expected geometry to 1% (area and bounds)."""
    if ref is None:
        return False, "missing"
    s = str(ref).strip()
    if condition in HANDLE_CONDITIONS:
        return (s == expected_handle), ("ok" if s == expected_handle else "wrong_handle")
    m = re.search(r"POLYGON\s*\(\(.*?\)\)", s, re.S | re.I)
    if not m:
        return False, "not_wkt"
    try:
        got = shapely_wkt.loads(m.group(0))
    except Exception:
        return False, "unparseable_wkt"
    if got.is_empty or not got.is_valid:
        return False, "invalid_wkt"
    if abs(got.area - expected_poly.area) / max(abs(expected_poly.area), 1e-9) > 0.01:
        return False, "wrong_geometry"
    b1, b2 = got.bounds, expected_poly.bounds
    span = max(b2[2] - b2[0], b2[3] - b2[1], 1e-9)
    if max(abs(x - y) for x, y in zip(b1, b2)) / span > 0.01:
        return False, "wrong_geometry"
    return True, "ok"
