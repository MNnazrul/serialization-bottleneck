"""
Builds the 20% stratified subsample used by the DeepSeek-V4-Pro run (PDF
Table 1: 20% coverage). Stratified by tier and source (synthetic/real) --
the tabular domain's analogue of geometry's (tier, shape_type) strata --
so the subsample preserves each tier's synthetic/real balance rather than
letting a plain random 20% skew toward whichever is more numerous.

Usage (run from this folder):
    python build_subsample.py --dry-run    # print the allocation table
    python build_subsample.py --verify     # regenerate and diff against the committed file
    python build_subsample.py              # write it (refuses to clobber; --force to overwrite)
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATASET_PATH = HERE.parent.parent / "phase1_tabular_dataset" / "tabular_exp1_dataset.json"
OUTPUT_PATH = HERE / "subsample_v4pro_thinking.json"

RATE = 0.2
SEED = 42
TIER_ORDER = ["simple", "medium", "hard"]
SOURCE_ORDER = ["synthetic", "real"]


def largest_remainder(counts: dict[str, int], quota: int) -> dict[str, int]:
    """Allocate `quota` slots across `counts` proportional to each key's
    share, using the largest-remainder method so allocations sum exactly
    to `quota` even when exact proportions aren't integers."""
    total = sum(counts.values())
    if total == 0:
        return {k: 0 for k in counts}
    exact = {k: quota * v / total for k, v in counts.items()}
    floors = {k: int(exact[k]) for k in counts}
    remainder = quota - sum(floors.values())
    fractional = sorted(counts.keys(), key=lambda k: exact[k] - floors[k], reverse=True)
    for k in fractional[:remainder]:
        floors[k] += 1
    return floors


def build_subsample(seed: int = SEED, rate: float = RATE) -> dict:
    records = json.loads(DATASET_PATH.read_text())
    n_total = len(records)
    n_target = round(n_total * rate)

    by_tier: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_tier[r["tier"]].append(r)

    tier_counts = {t: len(by_tier[t]) for t in TIER_ORDER}
    tier_quota = largest_remainder(tier_counts, n_target)

    rng = random.Random(seed)
    chosen_ids: list[str] = []
    allocation: dict[str, dict[str, int]] = {}

    for tier in TIER_ORDER:
        tier_records = by_tier[tier]
        by_source: dict[str, list[str]] = defaultdict(list)
        for r in tier_records:
            by_source[r["source"]].append(r["object_id"])

        source_counts = {s: len(by_source.get(s, [])) for s in SOURCE_ORDER}
        source_quota = largest_remainder(source_counts, tier_quota[tier])
        allocation[tier] = source_quota

        for source in SOURCE_ORDER:
            pool = by_source.get(source, [])
            k = min(source_quota[source], len(pool))
            chosen_ids.extend(rng.sample(pool, k))

    chosen_ids = sorted(chosen_ids)
    return {
        "purpose": "20% stratified subsample for the DeepSeek-V4-Pro (thinking mode) run",
        "seed": seed,
        "rate": rate,
        "n_tables": len(chosen_ids),
        "allocation": allocation,
        "object_ids": chosen_ids,
    }


def verify_composition(subsample: dict) -> dict:
    records = {r["object_id"]: r for r in json.loads(DATASET_PATH.read_text())}
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for oid in subsample["object_ids"]:
        r = records[oid]
        counts[r["tier"]][r["source"]] += 1
    return {t: dict(v) for t, v in counts.items()}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--seed", type=int, default=SEED)
    args = p.parse_args()

    subsample = build_subsample(seed=args.seed)
    composition = verify_composition(subsample)

    print(f"n_tables={subsample['n_tables']} (target {round(300 * RATE)})")
    print("allocation (tier -> source -> count):")
    for tier in TIER_ORDER:
        print(f"  {tier}: {composition.get(tier, {})}")

    if args.dry_run:
        return

    if args.verify:
        if not OUTPUT_PATH.exists():
            print(f"No committed file at {OUTPUT_PATH}")
            raise SystemExit(1)
        committed = json.loads(OUTPUT_PATH.read_text())
        match = committed["object_ids"] == subsample["object_ids"]
        print(f"Regenerated ids match committed file: {match}")
        if not match:
            raise SystemExit(1)
        return

    if OUTPUT_PATH.exists() and not args.force:
        print(f"{OUTPUT_PATH} already exists -- pass --force to overwrite")
        raise SystemExit(1)

    OUTPUT_PATH.write_text(json.dumps(subsample, indent=2))
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
