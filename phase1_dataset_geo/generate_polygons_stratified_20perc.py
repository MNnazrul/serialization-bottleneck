"""
Build the 20% stratified subsample of the Phase-1 geometry dataset.

PDF serialization_experiment_1.pdf, Section 3 (inference parameters,
DeepSeek-V4-Pro):

    "Run on a 20% random subsample of all objects, stratified by tier and
     shape/type category so that the subsample preserves the distributional
     balance of the full dataset."

and Section 10, step 8: "Run on the 20% stratified subsample across all
domains."

This script only BUILDS the subsample -- it writes a list of object_ids. No
runner is included here.

METHOD, and where it comes from
-------------------------------
Adapted from the graph domain's build_subsample.py (PR #10), which makes one
load-bearing point: floor the GROUP total first and distribute downward,
rather than flooring each small family cell independently. Flooring cells
independently rounds several of them to zero unevenly and silently breaks the
balance the dataset was built to hold.

The graph version groups by (tier, family, quadrant), where quadrant is derived
from two booleans. The geometry analogue is (tier, shape_type, family), and
shape_type plays the quadrant role:

    shape_type IS the `convex` ground-truth property.
        convex     -> convex = True    (50 per tier)
        concave    -> convex = False   (25 per tier)
        irregular  -> convex = False   (25 per tier)

So flooring at shape_type level preserves the boolean by construction, the same
way quadrant symmetry did in the graph domain:

    convex True  = 10 per tier
    convex False = 5 (concave) + 5 (irregular) = 10 per tier

ONE THING THE GRAPH VERSION DID NOT HAVE TO HANDLE
--------------------------------------------------
In the graph domain BOTH balanced booleans (is_bipartite, is_planar) fell out
of the quadrant symmetry for free. Here only `convex` does. `orientation` is
50/50 per tier in the full dataset, but it is NOT 50/50 within each shape_type:

    simple:  concave 16 ccw / 9 cw      irregular 12 ccw / 13 cw
    medium:  concave 12 ccw / 13 cw     irregular 15 ccw / 10 cw
    hard:    concave 13 ccw / 12 cw     irregular 11 ccw / 14 cw

Left alone, a shape-only stratification lets orientation drift, and orientation
is graded by exact match against a 50% majority baseline (PDF 6.2) -- a 13/7
subsample would move that baseline to 65% and make the property unreadable.

So orientation is carried as a fourth stratification level: allocated
proportionally inside each (tier, shape_type, family) cell by largest
remainder, then corrected so every tier lands exactly 50/50. The correction
only ever moves a slot between the two orientations of the SAME cell, so the
tier / shape_type / family composition is untouched by it.

ALLOCATION AT THE DEFAULT 20% RATE
----------------------------------
    per tier   convex 50 -> 10   (convex_sharp 5, convex_smooth 5)
               concave 25 ->  5
               irregular 25 -> 5
               ------------------
               20 per tier, 60 total = exactly 20% of 300

    guaranteed per tier:  convex 10 True / 10 False
                          orientation 10 ccw / 10 cw

Usage
-----
  python generate_polygons_stratified_20perc.py              # write (refuses to clobber)
  python generate_polygons_stratified_20perc.py --dry-run    # print the plan, no write
  python generate_polygons_stratified_20perc.py --verify     # compare against the existing file
  python generate_polygons_stratified_20perc.py --force      # overwrite
  python generate_polygons_stratified_20perc.py --rate 0.1 --seed 7 --output other.json
  python generate_polygons_stratified_20perc.py --emit-dataset subsample_dataset.json
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_DATASET = HERE / "geometry_exp1_dataset.json"
DEFAULT_OUTPUT = HERE / "subsample_20perc.json"

TIERS = ("simple", "medium", "hard")
# Order matters twice: it fixes the RNG consumption sequence (so the seed fully
# determines the draw) and it breaks largest-remainder ties deterministically.
SHAPES = ("convex", "concave", "irregular")
FAMILIES = ("convex_sharp", "convex_smooth", "concave", "irregular")
ORIENTATIONS = ("ccw", "cw")

N_PROPERTIES = 9          # PDF Table 4: 9 properties per polygon
DEFAULT_RATE = 0.2
DEFAULT_SEED = 42

PURPOSE = ("DeepSeek-V4-Pro thinking-mode run, 20% stratified subsample "
           "(PDF Section 3: 20% coverage, stratified by tier and shape/type category)")


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def load_dataset(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"dataset must be a JSON list: {path}")
    seen: set[str] = set()
    for row in data:
        oid = row.get("object_id")
        if oid is None:
            raise ValueError("record missing object_id")
        if oid in seen:
            raise ValueError(f"duplicate object_id: {oid}")
        seen.add(oid)
        for key in ("tier", "shape_type", "properties", "metadata"):
            if key not in row:
                raise ValueError(f"{oid} missing {key}")
        if row["shape_type"] == "convex" and not row["properties"]["convex"]:
            raise ValueError(f"{oid}: shape_type convex but convex property False")
        if row["shape_type"] != "convex" and row["properties"]["convex"]:
            raise ValueError(f"{oid}: shape_type {row['shape_type']} but convex property True")
    return data


def family_of(row: dict[str, Any]) -> str:
    return row["metadata"].get("family") or row["metadata"].get("generator")


def group_by_stratum(dataset) -> dict[tuple[str, str, str, str], list[str]]:
    """Bucket object_ids by (tier, shape_type, family, orientation), in dataset order."""
    groups: dict[tuple[str, str, str, str], list[str]] = {}
    for row in dataset:
        key = (row["tier"], row["shape_type"], family_of(row), row["properties"]["orientation"])
        groups.setdefault(key, []).append(row["object_id"])
    return groups


# ---------------------------------------------------------------------------
# allocation
# ---------------------------------------------------------------------------
def largest_remainder(exact: dict[str, float], target: int, order: tuple) -> dict[str, int]:
    """Give each key floor(exact), then hand the leftover slots to the largest
    fractional parts. Ties break on `order`, so the result is deterministic."""
    base = {k: int(exact[k]) for k in exact}
    leftover = target - sum(base.values())
    if leftover < 0:
        raise ValueError(f"target {target} below the sum of floors {sum(base.values())}")
    ranked = sorted(exact, key=lambda k: (-(exact[k] - base[k]), order.index(k)))
    for k in ranked[:leftover]:
        base[k] += 1
    return base


def allocate(groups, rate: float) -> dict[tuple[str, str, str, str], int]:
    """Three nested levels, each flooring the total above it (see module docstring):
    shape_type -> family -> orientation, then a tier-level orientation correction."""
    quota: dict[tuple[str, str, str, str], int] = {}

    for tier in TIERS:
        # --- level 1: shape_type. Flooring here is what pins `convex` 50/50.
        shape_budget: dict[str, int] = {}
        for shape in SHAPES:
            total = sum(len(v) for k, v in groups.items() if k[0] == tier and k[1] == shape)
            if total == 0:
                raise ValueError(f"no records for {tier}/{shape}")
            shape_budget[shape] = int(total * rate)          # floor

        for shape in SHAPES:
            fams = [f for f in FAMILIES
                    if any(k[:3] == (tier, shape, f) for k in groups)]
            if not fams:
                raise ValueError(f"no families under {tier}/{shape}")

            # --- level 2: family, by largest remainder inside the shape budget
            fam_size = {f: sum(len(v) for k, v in groups.items() if k[:3] == (tier, shape, f))
                        for f in fams}
            fam_quota = largest_remainder({f: fam_size[f] * rate for f in fams},
                                          shape_budget[shape], FAMILIES)

            # --- level 3: orientation, proportional inside each family cell
            for f in fams:
                ors = [o for o in ORIENTATIONS if (tier, shape, f, o) in groups]
                sizes = {o: len(groups[(tier, shape, f, o)]) for o in ors}
                got = largest_remainder({o: sizes[o] * rate for o in ors},
                                        fam_quota[f], ORIENTATIONS)
                for o in ors:
                    if got[o] > sizes[o]:
                        raise ValueError(f"quota {got[o]} exceeds {tier}/{shape}/{f}/{o}")
                    quota[(tier, shape, f, o)] = got[o]

        _correct_orientation(groups, quota, tier)

    return quota


def _correct_orientation(groups, quota, tier: str) -> None:
    """Move slots between the two orientations of one cell until the tier is
    exactly 50/50. Only ever trades ccw<->cw inside the SAME cell, so the
    tier/shape/family composition set above is preserved exactly.

    A tier with an odd slot count cannot split 50/50; that is reported by
    validate() rather than silently rounded here."""
    cells = [k for k in quota if k[0] == tier]
    total = sum(quota[k] for k in cells)
    if total % 2:
        return                                   # odd: validate() will flag it
    want = total // 2

    for _ in range(total):                       # bounded; each pass moves one slot
        have = sum(quota[k] for k in cells if k[3] == "ccw")
        if have == want:
            return
        frm, to = ("ccw", "cw") if have > want else ("cw", "ccw")
        for shape in SHAPES:                     # fixed order -> deterministic
            for f in FAMILIES:
                a, b = (tier, shape, f, frm), (tier, shape, f, to)
                if a in quota and b in quota and quota[a] > 0 \
                        and quota[b] < len(groups.get(b, [])):
                    quota[a] -= 1
                    quota[b] += 1
                    break
            else:
                continue
            break
        else:
            return                               # no legal trade left


# ---------------------------------------------------------------------------
# draw and validate
# ---------------------------------------------------------------------------
def draw(groups, quota, seed: int) -> list[str]:
    """Sample each stratum in a fixed order so the seed fully determines the result."""
    rng = random.Random(seed)
    picked: list[str] = []
    for tier in TIERS:
        for shape in SHAPES:
            for f in FAMILIES:
                for o in ORIENTATIONS:
                    key = (tier, shape, f, o)
                    if quota.get(key):
                        picked.extend(rng.sample(groups[key], quota[key]))
    return sorted(picked)


def validate(ids, dataset, quota, rate) -> None:
    """Re-derive the composition from the drawn ids rather than trusting the draw."""
    by_id = {r["object_id"]: r for r in dataset}
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate ids in subsample")
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ValueError(f"ids not in dataset: {unknown[:5]}")

    actual: dict[tuple, int] = {}
    for i in ids:
        r = by_id[i]
        key = (r["tier"], r["shape_type"], family_of(r), r["properties"]["orientation"])
        actual[key] = actual.get(key, 0) + 1
    expected = {k: v for k, v in quota.items() if v > 0}
    if actual != expected:
        raise ValueError(f"composition mismatch: got {actual}, expected {expected}")

    for tier in TIERS:
        rows = [by_id[i] for i in ids if by_id[i]["tier"] == tier]
        n = len(rows)

        # the two balanced axes the full dataset holds 50/50 per tier
        n_convex = sum(1 for r in rows if r["properties"]["convex"])
        if n_convex * 2 != n:
            raise ValueError(f"{tier}: convex not 50/50 "
                             f"({n_convex} True / {n - n_convex} False of {n})")
        n_ccw = sum(1 for r in rows if r["properties"]["orientation"] == "ccw")
        if n_ccw * 2 != n:
            raise ValueError(f"{tier}: orientation not 50/50 "
                             f"({n_ccw} ccw / {n - n_ccw} cw of {n})")

        # shape_type proportions must match the full tier
        full = [r for r in dataset if r["tier"] == tier]
        for shape in SHAPES:
            want = int(sum(1 for r in full if r["shape_type"] == shape) * rate)
            got = sum(1 for r in rows if r["shape_type"] == shape)
            if got != want:
                raise ValueError(f"{tier}/{shape}: {got} selected, expected {want}")


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def print_plan(groups, quota, rate, dataset) -> None:
    print(f"Stratified allocation at rate {rate:g}")
    print("  (floor per shape_type, split across families by largest remainder,")
    print("   orientation proportional then corrected to 50/50 per tier)\n")
    for tier in TIERS:
        print(f"  {tier}:")
        for shape in SHAPES:
            cells = []
            for f in FAMILIES:
                per_o = {o: quota.get((tier, shape, f, o)) for o in ORIENTATIONS
                         if (tier, shape, f, o) in quota}
                if per_o:
                    pool = sum(len(groups[(tier, shape, f, o)]) for o in per_o)
                    cells.append(f"{f}={sum(per_o.values())}/{pool} "
                                 f"({'/'.join(f'{o}:{c}' for o, c in per_o.items())})")
            tot = sum(v for k, v in quota.items() if k[0] == tier and k[1] == shape)
            pool = sum(len(v) for k, v in groups.items() if k[0] == tier and k[1] == shape)
            print(f"    {shape:10s} {tot:2d}/{pool:3d}   " + "  ".join(cells))
        rows_t = sum(v for k, v in quota.items() if k[0] == tier)
        n_ccw = sum(v for k, v in quota.items() if k[0] == tier and k[3] == "ccw")
        n_cvx = sum(v for k, v in quota.items() if k[0] == tier and k[1] == "convex")
        print(f"    tier total {rows_t}   convex {n_cvx} True / {rows_t - n_cvx} False"
              f"   orientation {n_ccw} ccw / {rows_t - n_ccw} cw")
    grand = sum(quota.values())
    print(f"\n  selected {grand} of {len(dataset)} polygons "
          f"({100 * grand / len(dataset):.1f}%)")
    print(f"  queries  : {grand} x {N_PROPERTIES} properties = {grand * N_PROPERTIES}")


def print_balance_check(ids, dataset) -> None:
    """Axes the PDF does not require, reported so any drift is visible."""
    by_id = {r["object_id"]: r for r in dataset}
    rows = [by_id[i] for i in ids]
    print("\n  carried axes (not required by the PDF, reported for transparency):")
    for label, get in (("offset_band", lambda r: r["metadata"].get("offset_band")),
                       ("fill_band", lambda r: r["metadata"].get("fill_band"))):
        sub = Counter(get(r) for r in rows)
        full = Counter(get(r) for r in dataset)
        n_s, n_f = len(rows), len(dataset)
        cells = "  ".join(
            f"{b}: {100 * sub[b] / n_s:4.1f}% vs {100 * full[b] / n_f:4.1f}%"
            for b in sorted(full))
        print(f"    {label:12s} {cells}   (subsample vs full)")
    print(f"    {'vertex count':12s} mean {sum(r['properties']['vertex_count'] for r in rows) / n_s:5.1f} "
          f"vs {sum(r['properties']['vertex_count'] for r in dataset) / n_f:5.1f}")


# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build the 20% stratified subsample of the Phase-1 geometry dataset.")
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--rate", type=float, default=DEFAULT_RATE,
                   help="Sampling rate (default 0.2 = 20%%).")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED,
                   help="RNG seed (default 42).")
    p.add_argument("--verify", action="store_true",
                   help="Compare regenerated ids against --output and exit; never writes.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the allocation without writing.")
    p.add_argument("--force", action="store_true",
                   help="Overwrite --output if it exists.")
    p.add_argument("--emit-dataset", type=Path, default=None,
                   help="Also write the selected records as a standalone dataset JSON, "
                        "in the same shape as the full dataset.")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    dataset = load_dataset(args.dataset)
    groups = group_by_stratum(dataset)
    quota = allocate(groups, args.rate)
    ids = draw(groups, quota, args.seed)
    validate(ids, dataset, quota, args.rate)

    print(f"dataset : {args.dataset}  ({len(dataset)} polygons)")
    print(f"seed    : {args.seed}\n")
    print_plan(groups, quota, args.rate, dataset)
    print_balance_check(ids, dataset)

    if args.verify:
        if not args.output.exists():
            print(f"\nVERIFY FAILED: {args.output} does not exist.")
            raise SystemExit(1)
        existing = json.loads(args.output.read_text())
        old = set(existing["object_ids"] if isinstance(existing, dict) else existing)
        new = set(ids)
        if old == new:
            print(f"\nVERIFY OK: regenerated ids match {args.output.name} ({len(new)}/{len(new)}).")
            return
        print(f"\nVERIFY FAILED: {len(old & new)}/{len(old)} ids match.")
        for label, diff in (("only in file", old - new), ("only in regenerated", new - old)):
            if diff:
                print(f"  {label}: {sorted(diff)[:5]}{' ...' if len(diff) > 5 else ''}")
        raise SystemExit(1)

    if args.dry_run:
        print(f"\n[dry-run] would write {len(ids)} ids to {args.output}")
        return

    if args.output.exists() and not args.force:
        print(f"\n{args.output} already exists. Use --verify to compare, --force to overwrite.")
        raise SystemExit(1)

    blob = {
        "purpose": PURPOSE,
        "source_dataset": args.dataset.name,
        "seed": args.seed,
        "rate": args.rate,
        "stratified_by": ["tier", "shape_type", "family", "orientation"],
        "n_polygons": len(ids),
        "n_queries": len(ids) * N_PROPERTIES,
        "object_ids": ids,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(blob, indent=2) + "\n")
    print(f"\nWrote {len(ids)} object_ids -> {args.output}")

    if args.emit_dataset:
        chosen = set(ids)
        subset = [r for r in dataset if r["object_id"] in chosen]
        args.emit_dataset.write_text(json.dumps(subset, indent=1) + "\n")
        print(f"Wrote {len(subset)} records      -> {args.emit_dataset}")


if __name__ == "__main__":
    main()
