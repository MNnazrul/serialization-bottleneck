#!/usr/bin/env python3
"""
Domain 1 — run every model's v8 query run at once, one worker process per model.

Launches the five v8 runners as independent subprocesses and waits for all of
them. Each child is a separate Python process with its own provider, its own API
key, its own output files and its own log directory, so there is no shared state
between them and nothing to serialize:

    01_v4flash      run_v4flash_full.py       DeepSeek
    02_qwen3        run_qwen3_full.py         OpenRouter
    03_llama_scout  run_llama_scout_full.py   OpenRouter
    04_gemini       run_gemini_full.py        Google
    05_gpt          run_gpt_full.py           OpenAI

All five read new_data_v7/geometry_exp1_dataset.json - 300 polygons, INTEGER
coordinates in [0,1000] at every tier, with both shortcut axes held flat across
tiers exactly as v6 held them: offset_band (centroid vs bbox centre) 22/38/32/8
and fill_band (how much of its box the polygon fills) 14/37/36/13, under one
30-cell joint quota per shape class.

What v7 adds on top of that balance, and why this run is not just a re-roll of
the v7 run:

  * All four generator families are fixed at exactly 25 per tier. v6's mix
    drifted 27/9/4 and 23/41/46 across tiers, so a tier contrast on the convex
    half was partly a contrast between constructions.
  * Extent and aspect are dealt as fixed PAIRS off one ladder, the identical
    multiset to every tier. Mean bbox diagonal now varies 0.01% across tiers
    against v6's 1.29% - and that is the quantity PDF 6.2 divides centroid and
    bbox error by, so it is the one size quantity that feeds back into the
    grading band.
  * The hard tier's vertex counts reach all 21 available values with at most 5
    records on any one, against v6's 18 values and 14 on the floor.

The polygons are newly generated and carry new object_ids, so results here do
not join record-for-record onto the v7 run. The grading band is the same width,
so accuracy levels are comparable in aggregate.

06_v4pro_nonthinking is deliberately NOT included - it has no v8 runner.

Results land in ../03_results/<model folder>/, one folder per model, the same
layout results_v7/ uses.

Each child's stdout and stderr go to logs_all/<model>_<stamp>.out rather than to
the terminal: five tqdm bars writing to one terminal overwrite each other and
the result is unreadable. This script prints its own combined progress instead,
counted from each model's results .jsonl, which every runner appends to exactly
once per completed query. So progress here is a count of work actually recorded,
not of requests attempted.

Resume is inherited from the runners, not reimplemented: a child skips
(object_id, property) pairs already in its .jsonl. Re-running this script after
a partial run therefore continues it, and a model that finished is a no-op.

NOTHING runs on import. Execute explicitly:

  python run_all.py --smoke              # SMOKE TEST FIRST: 5 polygons each
  python run_all.py                      # full run, all five models
  python run_all.py --limit 20           # any other polygon count
  python run_all.py --dry-run            # build prompts, no API calls
  python run_all.py --only 05_gpt 02_qwen3
  python run_all.py --skip 03_llama_scout
  python run_all.py -- --force           # everything after -- goes to each runner

A smoke run writes into the same .jsonl the full run uses, which is the point:
its 45 records per model are real records and the full run resumes past them
rather than repeating them. Delete the .jsonl only if you want those 45 queries
asked again.

Ctrl-C terminates every child, then waits for it to exit. Work already written
to a .jsonl survives.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
# Re-pathed for exp1_geo: dataset in ../01_dataset, results in ../03_results.
# Split layout: this file lives at phase2_model_results_geo/runners/.
REPO_ROOT = HERE.parent.parent
DATASET = REPO_ROOT / "phase1_dataset_geo" / "geometry_exp1_dataset.json"
LOG_DIR = HERE / "logs_all"

PROPERTIES = 9          # PDF §2: 9 properties per polygon
SMOKE_POLYGONS = 5      # --smoke: 5 polygons x 9 properties = 45 queries/model

# (folder, runner filename, results .jsonl relative to THIS file, provider)
# Every v8 runner writes into the shared results_v8/<folder>/ tree, so unlike v5
# there is no per-model exception to account for here.
MODELS = [
    ("01_v4flash",     "run_v4flash_full.py",     "../results/01_v4flash/v4flash_results.jsonl",             "deepseek"),
    ("02_qwen3",       "run_qwen3_full.py",       "../results/02_qwen3/qwen3_results.jsonl",               "openrouter"),
    ("03_llama_scout", "run_llama_scout_full.py", "../results/03_llama_scout/llama_scout_results.jsonl",         "openrouter"),
    ("04_gemini",      "run_gemini_full.py",      "../results/04_gemini/gemini_results.jsonl",      "google"),
    ("05_gpt",         "run_gpt_full.py",         "../results/05_gpt/gpt_results.jsonl",                 "openai"),
]

# Two of the five share an OpenRouter account. Starting all five in the same
# instant puts both of those into their first burst together, which is when a
# shared upstream pool is most likely to answer 429. A few seconds between
# launches costs nothing on a run measured in minutes.
DEFAULT_STAGGER = 3.0

POLL_SECONDS = 5.0


def count_lines(path: Path) -> int:
    """Completed queries for one model, or 0 before its file exists."""
    try:
        with path.open("rb") as fh:
            return sum(1 for line in fh if line.strip())
    except FileNotFoundError:
        return 0


def expected_queries(limit: int | None) -> int:
    try:
        raw = json.loads(DATASET.read_text())
    except FileNotFoundError:
        return 0
    rows = raw["objects"] if isinstance(raw, dict) and "objects" in raw else raw
    n = len(rows) if limit is None else min(limit, len(rows))
    return n * PROPERTIES


def build_command(folder: str, runner: str, passthrough: list[str],
                  limit: int | None, dry_run: bool) -> list[str]:
    cmd = [sys.executable, runner]
    if limit is not None:
        cmd += ["--limit", str(limit)]
    if dry_run:
        cmd.append("--dry-run")
    return cmd + passthrough


def main() -> int:
    p = argparse.ArgumentParser(
        description="Run every model's v8 query run, one worker per model.",
        epilog="Arguments after -- are passed through to every runner unchanged.")
    p.add_argument("--only", nargs="+", metavar="FOLDER",
                   help="Run only these model folders (e.g. 05_gpt 02_qwen3).")
    p.add_argument("--skip", nargs="+", metavar="FOLDER",
                   help="Run every model except these folders.")
    p.add_argument("--limit", type=int, default=None,
                   help="Only the first N polygons, per model.")
    p.add_argument("--smoke", action="store_true",
                   help=f"Smoke test: {SMOKE_POLYGONS} polygons per model "
                        f"({SMOKE_POLYGONS * PROPERTIES} queries each). Shorthand "
                        f"for --limit {SMOKE_POLYGONS}; run this before a full run.")
    p.add_argument("--dry-run", action="store_true",
                   help="Build prompts in every runner; no API calls.")
    p.add_argument("--stagger", type=float, default=DEFAULT_STAGGER,
                   help=f"Seconds between launches (default {DEFAULT_STAGGER}).")
    p.add_argument("--log-dir", type=Path, default=LOG_DIR)
    args, passthrough = p.parse_known_args()
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

    if args.smoke:
        # Refuse rather than pick one: --smoke --limit 50 has two readings and
        # guessing wrong silently costs either coverage or money.
        if args.limit is not None and args.limit != SMOKE_POLYGONS:
            sys.exit(f"--smoke means --limit {SMOKE_POLYGONS}; you also passed "
                     f"--limit {args.limit}. Use one or the other.")
        args.limit = SMOKE_POLYGONS

    selected = MODELS
    known = {m[0] for m in MODELS}
    for flag, names in (("--only", args.only), ("--skip", args.skip)):
        if names:
            unknown = set(names) - known
            if unknown:
                sys.exit(f"{flag}: unknown model folder(s) {sorted(unknown)}. "
                         f"Known: {sorted(known)}")
    if args.only:
        selected = [m for m in MODELS if m[0] in set(args.only)]
    if args.skip:
        selected = [m for m in selected if m[0] not in set(args.skip)]
    if not selected:
        sys.exit("no models selected")

    if not DATASET.exists():
        # Unlike new_data_v6, which shipped as two JSON files with no generator
        # in the repo, new_data_v7 ships its generator - so the fix here is a
        # command rather than "restore the file from somewhere". The generator
        # is seeded and deterministic, so a rebuild reproduces the dataset the
        # earlier runs used, byte for byte.
        sys.exit(f"dataset not found: {DATASET}\n"
                 f"rebuild it:  cd {DATASET.parent} && python generate_polygons_v7.py")

    for folder, runner, _jsonl, _prov in selected:
        path = HERE / folder / runner
        if not path.exists():
            sys.exit(f"runner not found: {path}")

    args.log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = expected_queries(args.limit)

    print("=" * 72)
    mode = f"SMOKE ({args.limit} polygons)" if args.limit is not None else "FULL"
    print(f"RUN ALL v8  |  {mode}  |  {len(selected)} workers  |  "
          f"dataset: {DATASET.parent.name}")
    print(f"expected per model: {target} queries "
          f"({target // PROPERTIES} polygons x {PROPERTIES} properties)")
    if passthrough:
        print(f"passthrough args : {' '.join(passthrough)}")
    print(f"child logs       : {args.log_dir}")
    print("=" * 72)

    procs = []
    for i, (folder, runner, jsonl, provider) in enumerate(selected):
        out_path = args.log_dir / f"{folder}_{stamp}.out"
        out_fh = out_path.open("w")
        cmd = build_command(folder, runner, passthrough, args.limit, args.dry_run)
        # start_new_session so a Ctrl-C in this terminal reaches only this
        # script; children are then stopped deliberately, in order, below.
        proc = subprocess.Popen(
            cmd, cwd=HERE / folder, stdout=out_fh, stderr=subprocess.STDOUT,
            start_new_session=True)
        procs.append({
            "folder": folder, "provider": provider, "proc": proc,
            "jsonl": HERE / jsonl, "out": out_path, "fh": out_fh,
            "start_count": count_lines(HERE / jsonl),
        })
        print(f"  started {folder:15s} pid {proc.pid:<7d} -> {out_path.name}")
        if i < len(selected) - 1 and args.stagger > 0:
            time.sleep(args.stagger)

    print("-" * 72)
    t0 = time.time()
    interrupted = False
    try:
        while any(e["proc"].poll() is None for e in procs):
            time.sleep(POLL_SECONDS)
            cells = []
            for e in procs:
                done = count_lines(e["jsonl"])
                state = "run" if e["proc"].poll() is None else \
                        ("ok" if e["proc"].returncode == 0 else "ERR")
                cells.append(f"{e['folder'][3:]}:{done}/{target}[{state}]")
            elapsed = int(time.time() - t0)
            print(f"  [{elapsed // 60:02d}:{elapsed % 60:02d}] " + "  ".join(cells),
                  flush=True)
    except KeyboardInterrupt:
        interrupted = True
        print("\n  interrupt - stopping children (work already written is kept)")
        for e in procs:
            if e["proc"].poll() is None:
                try:
                    os.killpg(os.getpgid(e["proc"].pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    e["proc"].terminate()
        for e in procs:
            try:
                e["proc"].wait(timeout=30)
            except subprocess.TimeoutExpired:
                e["proc"].kill()

    for e in procs:
        e["proc"].wait()
        e["fh"].close()

    print("=" * 72)
    print("SUMMARY" + ("  (smoke)" if args.limit is not None else ""))
    print("=" * 72)
    print(f'{"model":16s} {"exit":>5s} {"records":>9s} {"new":>7s} '
          f'{"missing":>8s}  log')
    problems = []
    for e in procs:
        rc = e["proc"].returncode
        done = count_lines(e["jsonl"])
        new = done - e["start_count"]
        missing = max(0, target - done)
        # A runner exits 0 even when individual queries failed - it logs them
        # and tells you to re-run. Treat a short .jsonl as a problem regardless
        # of exit code, so a partial run is never reported as a clean one.
        if rc != 0 or (missing and not args.dry_run):
            problems.append((e["folder"], rc, missing))
        print(f'{e["folder"]:16s} {rc:5d} {done:9d} {new:7d} {missing:8d}  '
              f'{e["out"].name}')

    if args.dry_run:
        print("\ndry-run: no API calls were made, no records written.")
        return 0
    if interrupted:
        print("\nINTERRUPTED. Re-run the same command to continue - each runner "
              "skips what it already logged.")
        return 130
    if problems:
        print("\nINCOMPLETE:")
        for folder, rc, missing in problems:
            why = f"exit {rc}" if rc != 0 else f"{missing} queries not recorded"
            print(f"  {folder:16s} {why}")
        print("\nRe-run the same command - logged queries skip, failed ones "
              "retry. Read the child log above for the cause; a 429 from a "
              "shared provider pool usually clears on the next pass.")
        return 1
    print("\nAll models complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
