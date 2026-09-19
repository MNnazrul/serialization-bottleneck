"""
Repair ReAct episodes damaged by the action-parser bug (fixed 2026-09-16).

The original `_ACTION_RE` only accepted `Action N: Name[arg]`. Decorated but
valid actions -- `Action 15: [Finish[117]]`, `**Action 3:** HasEdge[1, 2]`,
`Action 11: `Neighbors[0]`` -- were answered with "Invalid action", so correct
Finish calls became no_finish episodes. The fixed regex lives in
react_graph_harness.py; this script brings the logged results in line with it
WITHOUT re-running the whole experiment.

For every logged ReAct episode it rebuilds the model's per-step text from the
stored transcript and re-parses each step with the fixed parser. At the FIRST
step where the fixed parser disagrees with what the run did:

  * the step is a Finish  -> REPLAY (offline, no API). Everything before it is
    byte-identical to what the fixed harness would have seen, and the fixed
    harness stops at Finish, so the corrected record is exact. Token counts
    are left as logged (they include the wasted post-Finish turns).
  * the step is a tool    -> RERUN. The observation would have differed, so
    the rest of the episode is unknowable offline; the pair is written to
    `<key>_repair_pairs.json` for `run_<key>_react.py --only-pairs`.

Corrected/re-run records are APPENDED to the JSONL (latest record per pair
wins, as everywhere else in the harness), so the original records stay in the
log. Replayed records carry `"repair": "action_parse_replay"`.

  python phase2_react_graph/_common/repair_action_parse.py            # report only
  python phase2_react_graph/_common/repair_action_parse.py --apply    # append replays, write pairs files
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import react_graph_harness as rh  # noqa: E402
from react_graph_harness import gh  # noqa: E402

ROOT = HERE.parent
DATASET = ROOT.parent / "phase1_dataset_graph" / "graph_exp1_dataset.json"
MODELS = [
    ("01_v4flash", "v4flash"), ("02_qwen3", "qwen3"), ("03_llama_scout", "llama_scout"),
    ("04_gemini", "gemini"), ("05_gpt", "gpt"), ("06_v4pro_nonthinking", "v4pro_nonthinking"),
]

# The pre-fix parser, kept verbatim so the replay knows what the run actually did.
_OLD_ACTION_RE = re.compile(r"Action\s*\d*\s*:\s*([A-Za-z_]+)\s*\[(.*?)\]", re.IGNORECASE | re.DOTALL)
# run_episode joins turns as: text + "\nObservation k: <one line>\nThought k+1:" + " " + text ...
_STEP_SPLIT_RE = re.compile(r"\nObservation \d+: [^\n]*\nThought \d+:")


def old_parse(text: str) -> tuple[str | None, str]:
    matches = list(_OLD_ACTION_RE.finditer(text or ""))
    if not matches:
        return None, (text or "").strip()[-80:]
    return rh._ACTION_ALIASES.get(matches[-1].group(1).lower()), matches[-1].group(2).strip()


def step_texts(rec: dict) -> list[str] | None:
    body = rec["raw_model_output"]
    if not body.startswith(rec["prompt"]):
        return None
    return [s.strip() for s in _STEP_SPLIT_RE.split(body[len(rec["prompt"]):])]


def classify(rec: dict) -> tuple[str, int | None]:
    """('unchanged'|'replay'|'rerun'|'unverifiable', index of first divergent step)."""
    steps = step_texts(rec)
    calls = rec.get("react_tool_calls", [])
    if steps is None or len(steps) != len(calls):
        return "unverifiable", None
    for i, (text, call) in enumerate(zip(steps, calls)):
        old = old_parse(text)
        logged = "finish" if call["action"] == "Finish" else call["action"]
        if (old[0] or "invalid") != logged:
            return "unverifiable", None
        new = rh.parse_action(text)
        if old[0] is None and new[0] is None:
            continue
        if old == new:
            continue
        return ("replay" if new[0] == "finish" else "rerun"), i
    return "unchanged", None


def replay_record(rec: dict, row: dict, i: int) -> dict:
    steps = step_texts(rec)
    action, arg = rh.parse_action(steps[i])
    assert action == "finish"
    parsed, ok = gh.normalize_answer(arg, rec["property"])
    calls = rec["react_tool_calls"][:i] + [{"step": i + 1, "action": "Finish", "arg": arg, "observation": None}]
    # transcript up to and including step i's text
    body = rec["raw_model_output"][len(rec["prompt"]):]
    cuts = [m.start() for m in _STEP_SPLIT_RE.finditer(body)]
    transcript = rec["prompt"] + (body[:cuts[i]] if i < len(cuts) else body)
    ep = {
        "prompt": rec["prompt"], "transcript": transcript.strip(), "parsed": parsed, "ok": ok,
        "finished": True, "steps": i + 1, "tool_calls": calls,
        "usage": {"prompt_tokens": rec.get("prompt_tokens") or 0,
                  "completion_tokens": rec.get("completion_tokens") or 0},
        "finish_reason": rec.get("finish_reason"),
    }
    new = rh.make_react_record(row, rec["property"], ep, rec["model"], rec["provider"],
                               rec["temperature"], 10**9)
    new["repair"] = "action_parse_replay"
    return new


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="Append replayed records to the JSONL, refresh the JSON export, write pairs files.")
    args = ap.parse_args()

    rows = {r["object_id"]: r for r in json.loads(DATASET.read_text())}
    for folder, key in MODELS:
        jsonl = ROOT / folder / f"{key}_results.jsonl"
        if not jsonl.exists():
            continue
        latest = gh.read_latest_records(jsonl)
        buckets: dict[str, list] = {"unchanged": [], "replay": [], "rerun": [], "unverifiable": []}
        for rec in latest.values():
            if rec.get("action_parser", 1) >= 2:   # replayed or re-run with the fixed parser
                buckets["unchanged"].append(rec)
                continue
            kind, i = classify(rec)
            buckets[kind].append((rec, i) if kind == "replay" else rec)

        replays = [replay_record(rec, rows[rec["object_id"]], i) for rec, i in buckets["replay"]]
        flipped = sum(1 for r in replays if gh_correct(r))
        pairs = sorted([r["object_id"], r["property"]] for r in buckets["rerun"])
        print(f"{key:18s} episodes={len(latest):5d}  unchanged={len(buckets['unchanged']):5d}  "
              f"replay={len(replays):4d} (now correct {flipped})  rerun={len(pairs):4d}  "
              f"unverifiable={len(buckets['unverifiable'])}")

        if not args.apply:
            continue
        if replays:
            with jsonl.open("a") as h:
                for r in replays:
                    h.write(json.dumps(r, ensure_ascii=True) + "\n")
            gh.write_json_export(jsonl, jsonl.with_suffix(".json"))
        pairs_path = ROOT / folder / f"{key}_repair_pairs.json"
        if pairs:
            pairs_path.write_text(json.dumps(pairs, indent=1) + "\n")
            print(f"{'':18s} -> {pairs_path.relative_to(ROOT.parent)}  "
                  f"(run: python {folder}/run_{key}_react.py --only-pairs {pairs_path.name})")


def gh_correct(rec: dict) -> bool:
    return bool(rec.get("correct_1pct") if rec["property"] == "avg_clustering" else rec.get("correct"))


if __name__ == "__main__":
    main()
