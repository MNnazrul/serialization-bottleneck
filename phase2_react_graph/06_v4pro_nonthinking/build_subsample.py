"""
Build the 20% stratified subsample used by the V4-Pro (non-thinking) run.

Per serialization_experiment_1.pdf, Section 2 (inference parameters):

    "Run on a 20% random subsample of all objects, stratified by tier and
     shape/type category so that the subsample preserves the distributional
     balance of the full dataset."

Adapted to this domain's quadrant structure (see
../../phase1_dataset_graph/README.md S1/S2): the 300-graph dataset is built
so that all 5 Table-7 families are exactly 20/tier, and every (is_bipartite,
is_planar) quadrant is exactly 25/tier, spread across whichever families
cover it -- `random_bipartite` (Q1+Q2 only), `random_planar` (Q3 only), and
`erdos_renyi`/`barabasi_albert`/`watts_strogatz` (all 4 quadrants each). A
subsample must preserve that quadrant structure, or the same imbalance
reappears one level down: an earlier, quadrant-unaware subsample drew 4/4
True for one tier's random_bipartite quota and 4/4 True for another's
random_planar quota, 0 False either time -- exactly why V4-Pro's per-tier
is_bipartite/is_planar accuracy looked erratic against the other 5 models
(each queried on the full, genuinely balanced 300-graph dataset).

Algorithm
---------
1. Group the dataset by (tier, family, quadrant) -- quadrant is derived
   from (is_bipartite, is_planar) directly, not read off a label: Q1 =
   bipartite AND planar, Q2 = bipartite AND NOT planar, Q3 = NOT bipartite
   AND planar, Q4 = NOT bipartite AND NOT planar.
2. Per tier, per quadrant: take the **floor** of `quadrant_total * rate`
   (quadrant_total is always 25, so at the default 20% rate that's
   `floor(5.0) = 5`), then split that 5-slot budget across whichever
   families cover the quadrant by **largest remainder**, weighted by each
   family's own share of the quadrant. This is a quadrant-level floor, not
   a per-family-cell floor -- flooring each small family/quadrant cell
   independently (e.g. `erdos_renyi`'s Q3 slice, only 2/tier) rounds several
   cells to 0 unevenly and silently breaks the boolean balance; flooring the
   quadrant *total* first and distributing downward preserves it exactly.
3. Because all 4 quadrants get the same 5-slot budget, the subsample is
   exactly 50/50 on both booleans in every tier by construction:
   `is_bipartite` true = Q1+Q2 = 5+5 = 10, false = Q3+Q4 = 5+5 = 10;
   `is_planar` true = Q1+Q3 = 5+5 = 10, false = Q2+Q4 = 5+5 = 10. No
   further balancing step is needed -- the quadrant symmetry *is* the
   balance. (Committed allocation: Q1 -> random_bipartite 2, erdos_renyi 1,
   barabasi_albert 1, watts_strogatz 1; Q2 -> same as Q1; Q3 ->
   random_planar 4, erdos_renyi 1, barabasi_albert 0, watts_strogatz 0;
   Q4 -> erdos_renyi 2, barabasi_albert 1, watts_strogatz 2. Family totals:
   random_bipartite 4, random_planar 4, erdos_renyi 5, barabasi_albert 3,
   watts_strogatz 4 -- 20/tier, 60 total.)
4. Draw each stratum's slots with `random.Random(seed).sample()`, iterating
   tiers in order (simple, medium, hard), quadrants in order (Q1, Q2, Q3,
   Q4), and families within each quadrant in the dataset's own family order
   (random_bipartite, random_planar, erdos_renyi, barabasi_albert,
   watts_strogatz). Order matters: it fixes the RNG's consumption sequence,
   which is what makes the result reproducible.
5. Validate, then write the ids sorted.

Usage
-----
  python build_subsample.py                 # write the subsample (refuses to clobber)
  python build_subsample.py --verify        # compare against the existing file, no write
  python build_subsample.py --dry-run       # print the plan, no write
  python build_subsample.py --force         # overwrite an existing file
  python build_subsample.py --rate 0.1 --seed 7 --output other.json
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
DEFAULT_DATASET = HERE.parent.parent / "phase1_dataset_graph" / "graph_exp1_dataset.json"
DEFAULT_OUTPUT = HERE / "subsample_v4pro_nonthinking.json"

TIERS = ("simple", "medium", "hard")
QUADRANTS = ("Q1", "Q2", "Q3", "Q4")
# Family order matters for RNG draw order and largest-remainder tie-breaks --
# matches the Phase-1 notebook's FAMILY_QUADRANT_PLAN order.
FAMILIES = ("random_bipartite", "random_planar", "erdos_renyi", "barabasi_albert", "watts_strogatz")

N_PROPERTIES = 8        # queries per graph, used for the n_queries field
DEFAULT_RATE = 0.2
DEFAULT_SEED = 42

PURPOSE = "DeepSeek-V4-Pro non-thinking companion run, 20% stratified subsample (PDF Section 2: 20% coverage)"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build the 20% quadrant-stratified subsample of the Phase-1 graph dataset.")
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET,
                   help="Phase-1 dataset JSON (default: ../../phase1_dataset_graph/...).")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                   help="Where to write the subsample file.")
    p.add_argument("--rate", type=float, default=DEFAULT_RATE,
                   help="Sampling rate (default 0.2 = 20%%).")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED,
                   help="RNG seed (default 42, the value recorded in the committed file).")
    p.add_argument("--verify", action="store_true",
                   help="Compare the generated ids against --output and exit; never writes.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the allocation and counts without writing.")
    p.add_argument("--force", action="store_true",
                   help="Overwrite --output if it already exists.")
    return p.parse_args()


def load_dataset(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"dataset must be a JSON list: {path}")
    seen = set()
    for row in data:
        oid = row.get("object_id")
        if oid is None:
            raise ValueError("record missing object_id")
        if oid in seen:
            raise ValueError(f"duplicate object_id: {oid}")
        seen.add(oid)
        for key in ("tier", "family", "properties"):
            if key not in row:
                raise ValueError(f"{oid} missing {key}")
    return data


def quadrant_of(row: dict[str, Any]) -> str:
    bip = row["properties"]["is_bipartite"]
    plan = row["properties"]["is_planar"]
    if bip and plan:
        return "Q1"
    if bip and not plan:
        return "Q2"
    if not bip and plan:
        return "Q3"
    return "Q4"


def group_by_stratum(dataset: list[dict[str, Any]]) -> dict[tuple[str, str, str], list[str]]:
    """Bucket object_ids by (tier, family, quadrant), preserving dataset order."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for row in dataset:
        groups.setdefault((row["tier"], row["family"], quadrant_of(row)), []).append(row["object_id"])
    return groups


def allocate(groups: dict[tuple[str, str, str], list[str]], rate: float
             ) -> dict[tuple[str, str, str], int]:
    """Floor each quadrant's total share, split across its families by
    largest remainder (see module docstring)."""
    quota: dict[tuple[str, str, str], int] = {}
    for tier in TIERS:
        for quad in QUADRANTS:
            fams_in_quad = [f for f in FAMILIES if (tier, f, quad) in groups]
            if not fams_in_quad:
                raise ValueError(f"no families cover {tier}/{quad}")
            quad_total = sum(len(groups[(tier, f, quad)]) for f in fams_in_quad)
            target = int(quad_total * rate)  # floor, per module docstring
            exact = {f: len(groups[(tier, f, quad)]) * rate for f in fams_in_quad}
            base = {f: int(exact[f]) for f in fams_in_quad}
            leftover = target - sum(base.values())
            ranked = sorted(fams_in_quad, key=lambda f: (-(exact[f] - base[f]), FAMILIES.index(f)))
            for f in ranked[:leftover]:
                base[f] += 1
            for f in fams_in_quad:
                if base[f] > len(groups[(tier, f, quad)]):
                    raise ValueError(f"quota {base[f]} exceeds {tier}/{f}/{quad} group size")
                quota[(tier, f, quad)] = base[f]
    return quota


def draw(groups, quota, seed: int) -> list[str]:
    """Sample each stratum in a fixed order so the seed fully determines the result."""
    rng = random.Random(seed)
    picked: list[str] = []
    for tier in TIERS:
        for quad in QUADRANTS:
            for f in FAMILIES:
                key = (tier, f, quad)
                if key not in quota:
                    continue
                picked.extend(rng.sample(groups[key], quota[key]))
    return sorted(picked)


def validate(ids, dataset, quota) -> None:
    """Re-derive the composition from the drawn ids instead of trusting the draw."""
    by_id = {r["object_id"]: r for r in dataset}
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate ids in subsample")
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ValueError(f"ids not in dataset: {unknown[:5]}")

    actual: dict[tuple[str, str, str], int] = {}
    for i in ids:
        row = by_id[i]
        key = (row["tier"], row["family"], quadrant_of(row))
        actual[key] = actual.get(key, 0) + 1
    expected = {k: v for k, v in quota.items() if v > 0}  # zero-quota strata draw nothing
    if actual != expected:
        raise ValueError(f"composition mismatch: got {actual}, expected {expected}")

    # Direct check that the subsample is genuinely 50/50 on both booleans,
    # in every tier -- the whole point of this rewrite.
    for tier in TIERS:
        rows = [by_id[i] for i in ids if by_id[i]["tier"] == tier]
        n_bip = sum(1 for r in rows if r["properties"]["is_bipartite"])
        n_plan = sum(1 for r in rows if r["properties"]["is_planar"])
        half = len(rows) // 2
        if n_bip != half or (len(rows) - n_bip) != half:
            raise ValueError(f"{tier}: is_bipartite not balanced ({n_bip} true / {len(rows) - n_bip} false)")
        if n_plan != half or (len(rows) - n_plan) != half:
            raise ValueError(f"{tier}: is_planar not balanced ({n_plan} true / {len(rows) - n_plan} false)")


def print_plan(groups, quota, rate) -> None:
    print(f"Stratified allocation at rate {rate:g} (floor per quadrant, split by largest remainder):\n")
    for tier in TIERS:
        print(f"  {tier}:")
        for quad in QUADRANTS:
            cells = ", ".join(f"{f}={quota[(tier, f, quad)]}/{len(groups[(tier, f, quad)])}"
                               for f in FAMILIES if (tier, f, quad) in quota)
            total = sum(v for (t, f, q), v in quota.items() if t == tier and q == quad)
            print(f"    {quad} (target {total}): {cells}")
        fam_totals = {f: sum(v for (t, fam, q), v in quota.items() if t == tier and fam == f) for f in FAMILIES}
        print(f"    family totals: {fam_totals}")
    grand = sum(quota.values())
    pool = sum(len(v) for v in groups.values())
    print(f"\n  selected {grand} of {pool} graphs ({100 * grand / pool:.1f}%)")
    print(f"  queries : {grand} x {N_PROPERTIES} properties = {grand * N_PROPERTIES}")


def main() -> None:
    args = parse_args()

    dataset = load_dataset(args.dataset)
    groups = group_by_stratum(dataset)
    quota = allocate(groups, args.rate)
    ids = draw(groups, quota, args.seed)
    validate(ids, dataset, quota)

    print(f"dataset : {args.dataset}  ({len(dataset)} graphs)")
    print(f"seed    : {args.seed}\n")
    print_plan(groups, quota, args.rate)

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
        "seed": args.seed,
        "rate": args.rate,
        "n_graphs": len(ids),
        "n_queries": len(ids) * N_PROPERTIES,
        "object_ids": ids,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(blob, indent=2) + "\n")
    print(f"\nWrote {len(ids)} object_ids -> {args.output}")


if __name__ == "__main__":
    main()
