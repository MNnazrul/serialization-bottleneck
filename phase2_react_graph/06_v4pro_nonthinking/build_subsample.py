"""
Build the 20% stratified subsample used by the V4-Pro (non-thinking) run.

Per serialization_experiment_1.pdf, Section 2 (inference parameters):

    "Run on a 20% random subsample of all objects, stratified by tier and
     shape/type category so that the subsample preserves the distributional
     balance of the full dataset."

Same procedure as ../../phase2_model_results/06_v4pro_nonthinking/build_subsample.py
(Geometry domain), adapted to this domain's (tier, family) strata instead of
(tier, shape_type).

Algorithm
---------
1. Group the dataset by (tier, family) -- 15 groups. Per-tier family sizes
   are 20/19/19/22/20 (erdos_renyi/barabasi_albert/watts_strogatz/
   random_bipartite/random_planar), matching graph_exp1_summary.json.
2. Per tier, allocate the tier's quota across families by **largest
   remainder** at 20%: exact quotas 4.0/3.8/3.8/4.4/4.0, floors 4/3/3/4/4=18,
   2 leftover slots go to the largest fractional parts (barabasi_albert .8,
   then watts_strogatz .8) -> 4/4/4/4/4, exactly 20 per tier, 60 total.
   Allocating *within* each tier is what guarantees equal tier counts.
3. Draw each group's slots with `random.Random(seed).sample()`, iterating
   tiers in order (simple, medium, hard) and families in the dataset's own
   generation order (erdos_renyi, barabasi_albert, watts_strogatz,
   random_bipartite, random_planar). Order matters: it fixes the RNG's
   consumption sequence, which is what makes the result reproducible.
4. Validate, then write the ids sorted.

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

# Iteration order is part of the algorithm, not a display choice -- changing it
# changes which graphs are drawn for a given seed. Matches the Phase-1 notebook's
# FAMILIES order.
TIERS = ("simple", "medium", "hard")
FAMILIES = ("erdos_renyi", "barabasi_albert", "watts_strogatz", "random_bipartite", "random_planar")

N_PROPERTIES = 8        # queries per graph, used for the n_queries field
DEFAULT_RATE = 0.2
DEFAULT_SEED = 42

PURPOSE = "DeepSeek-V4-Pro non-thinking companion run, 20% stratified subsample (PDF Section 2: 20% coverage)"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build the 20% tier/family-stratified subsample of the Phase-1 graph dataset.")
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
        for key in ("tier", "family"):
            if key not in row:
                raise ValueError(f"{oid} missing {key}")
    return data


def group_by_stratum(dataset: list[dict[str, Any]]) -> dict[tuple[str, str], list[str]]:
    """Bucket object_ids by (tier, family), preserving dataset order."""
    groups: dict[tuple[str, str], list[str]] = {}
    for row in dataset:
        groups.setdefault((row["tier"], row["family"]), []).append(row["object_id"])

    missing = [(t, f) for t in TIERS for f in FAMILIES if (t, f) not in groups]
    if missing:
        raise ValueError(f"dataset has no graphs for strata: {missing}")
    unexpected = set(groups) - {(t, f) for t in TIERS for f in FAMILIES}
    if unexpected:
        raise ValueError(f"dataset has unexpected strata: {sorted(unexpected)}")
    return groups


def allocate(groups: dict[tuple[str, str], list[str]], rate: float) -> dict[tuple[str, str], int]:
    """Largest-remainder allocation, computed per tier (see module docstring)."""
    quota: dict[tuple[str, str], int] = {}
    for tier in TIERS:
        exact = {f: len(groups[(tier, f)]) * rate for f in FAMILIES}
        base = {f: int(exact[f]) for f in FAMILIES}
        tier_total = round(sum(exact.values()))
        leftover = tier_total - sum(base.values())
        ranked = sorted(FAMILIES, key=lambda f: (-(exact[f] - base[f]), FAMILIES.index(f)))
        for family in ranked[:leftover]:
            base[family] += 1
        for family in FAMILIES:
            if base[family] > len(groups[(tier, family)]):
                raise ValueError(f"quota {base[family]} exceeds {tier}/{family} group size")
            quota[(tier, family)] = base[family]
    return quota


def draw(groups, quota, seed: int) -> list[str]:
    """Sample each stratum in a fixed order so the seed fully determines the result."""
    rng = random.Random(seed)
    picked: list[str] = []
    for tier in TIERS:
        for family in FAMILIES:
            picked.extend(rng.sample(groups[(tier, family)], quota[(tier, family)]))
    return sorted(picked)


def validate(ids, dataset, quota) -> None:
    """Re-derive the composition from the drawn ids instead of trusting the draw."""
    by_id = {r["object_id"]: r for r in dataset}
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate ids in subsample")
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ValueError(f"ids not in dataset: {unknown[:5]}")

    actual: dict[tuple[str, str], int] = {}
    for i in ids:
        row = by_id[i]
        key = (row["tier"], row["family"])
        actual[key] = actual.get(key, 0) + 1
    if actual != quota:
        raise ValueError(f"composition mismatch: got {actual}, expected {quota}")


def print_plan(groups, quota, rate) -> None:
    print(f"Stratified allocation at rate {rate:g}:\n")
    print(f"  {'tier':8s} " + " ".join(f"{f:>17s}" for f in FAMILIES) + f" {'total':>8s}")
    for tier in TIERS:
        cells = " ".join(f"{quota[(tier, f)]:>4d}/{len(groups[(tier, f)]):<12d}" for f in FAMILIES)
        total = sum(quota[(tier, f)] for f in FAMILIES)
        print(f"  {tier:8s} {cells} {total:>8d}")
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
