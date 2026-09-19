"""
Phase 2 -- ReAct query harness for the tabular (Domain 3) benchmark,
Experiment 2.

Reuses Experiment 1's tabular harness (`../
harness.py`) directly for everything that must stay identical across
experiments: dataset loading, the prompt template, answer parsing, scoring,
and the API client. This module adds only what Experiment 2 changes: the
ReAct Thought/Action/Observation loop over read-only structural tools, and a
matched zero-shot control run in the same session (Experiment 1's method,
called again, so session/mode drift cannot explain any ReAct-vs-zero-shot
delta).

See REACT_LOOP_GUIDELINE.txt (../../phase3_tabular_evaluation/react/) for the
full contract this module implements: the tool set, the stopping rule, the
scoring policy, and the exact record schema Phase 3 reads.

One invocation of a model's run.py runs BOTH arms back to back, in one
process, against the same task list -- this is what makes the zero-shot
control "matched" (same session, same day, same API keys) rather than a
separate script run whenever someone gets around to it.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
EXP1_DIR = HERE
sys.path.insert(0, str(EXP1_DIR))

import harness as exp1  # noqa: E402  -- Experiment 1's tabular harness module

# ----------------------------------------------------------------------
# Tool set -- REACT_LOOP_GUIDELINE.txt Section 2. Read-only accessors over
# the SAME table already in the prompt; no tool computes an aggregate a
# question is asking for (no Corr[], Skew[], IsMonotonic[], HasOutlier[]).
# ----------------------------------------------------------------------

TOOL_NAMES = ("RowCount", "ColumnNames", "ColumnDtype", "GetColumn", "GetCell", "NullCount", "Head", "Finish")

TOOLS_DESC = """\
RowCount[]                 -> int         number of data rows (no header)
ColumnNames[]              -> list[str]   header, in order
ColumnDtype[col]           -> str         "numeric" | "categorical" | "datetime"
GetColumn[col]              -> list        every value in that column, in row order
GetCell[row, col]           -> value       single cell (row is 0-indexed data row)
NullCount[]                 -> int         total missing cells in the table
Head[k]                     -> rows        first k rows as CSV (default k=5)
Finish[answer]              -> ends the episode; `answer` is graded"""

MAX_STEPS_DEFAULT = 12


def _parse_table(csv_string: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(csv_string))
    rows = list(reader)
    header = reader.fieldnames or []
    return header, rows


def _cell_repr(v: Optional[str]) -> str:
    return "null" if v is None or v == "" else v


def run_tool(name: str, arg_str: str, header: list[str], rows: list[dict[str, str]], csv_string: str) -> str:
    """Execute one tool call and return its Observation text. Raises
    ValueError on bad arguments -- the caller turns that into an
    'invalid action' Observation, same as a malformed Action."""
    if name == "RowCount":
        return str(len(rows))

    if name == "ColumnNames":
        return json.dumps(header)

    if name == "GetColumn":
        col = arg_str.strip().strip("'\"")
        if col not in header:
            raise ValueError(f"unknown column {col!r}")
        return json.dumps([_cell_repr(r.get(col)) for r in rows])

    if name == "GetCell":
        parts = [p.strip().strip("'\"") for p in arg_str.split(",")]
        if len(parts) != 2:
            raise ValueError(f"GetCell needs 2 args, got {arg_str!r}")
        row_s, col = parts
        if col not in header:
            raise ValueError(f"unknown column {col!r}")
        try:
            row_i = int(row_s)
        except ValueError:
            raise ValueError(f"row index must be an integer, got {row_s!r}")
        if not (0 <= row_i < len(rows)):
            raise ValueError(f"row {row_i} out of range (0..{len(rows) - 1})")
        return _cell_repr(rows[row_i].get(col))

    if name == "NullCount":
        return str(sum(1 for r in rows for v in r.values() if v is None or v == ""))

    if name == "Head":
        k = 5
        if arg_str.strip():
            try:
                k = int(arg_str.strip())
            except ValueError:
                raise ValueError(f"Head arg must be an integer, got {arg_str!r}")
        lines = csv_string.splitlines()
        return "\n".join(lines[: 1 + max(k, 0)])

    raise ValueError(f"unknown tool {name!r}")


ACTION_RE = re.compile(r"Action:\s*([A-Za-z]+)\s*\[(.*?)\]", re.DOTALL)


def parse_action(text: str) -> Optional[tuple[str, str]]:
    """Returns (tool_name, arg_string) for the LAST Action in the turn, or
    None if no well-formed Action[...] is present."""
    matches = ACTION_RE.findall(text)
    if not matches:
        return None
    name, args = matches[-1]
    if name not in TOOL_NAMES:
        return None
    return name, args.strip()


def build_react_prompt(csv_string: str, question: str) -> str:
    return f"""You are given a data table in CSV format. Answer the question below by \
reasoning step by step and using the tools listed, one at a time, in a \
Thought/Action/Observation loop.

At each turn, output exactly one Thought and one Action, in this format:

Thought: <your reasoning about what to look up next>
Action: <ToolName>[<args>]

After each Action you will be given an Observation with that tool's result, \
then you continue the loop. When you have enough information to answer, finish with:

Thought: I now have enough information to answer.
Action: Finish[<answer>]

Available tools:
{TOOLS_DESC}

Format example (syntax only -- not a worked example of this task):
Thought: I need to know how many rows there are.
Action: RowCount[]

Table:
{csv_string}

Question:
{question}

Begin."""


# ----------------------------------------------------------------------
# ReAct episode
# ----------------------------------------------------------------------


async def call_chat(client, cfg: "exp1.ModelConfig", messages: list[dict], max_tokens: int) -> dict:
    """Same call shape as exp1.call_model, but over a growing multi-turn
    transcript instead of a single user prompt. Reuses exp1's retry policy
    by wrapping with the same decorator semantics inline (tenacity is
    applied at the exp1.call_model layer for the zero-shot arm; here we
    retry manually with the same exception set since messages, not a single
    prompt string, must be threaded through)."""
    from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

    extra_body: dict = {}
    if cfg.thinking_disable:
        cfg.thinking_disable(extra_body)

    kwargs: dict[str, Any] = dict(
        model=cfg.model,
        messages=messages,
        temperature=cfg.temperature,
        max_tokens=max_tokens,
    )
    if extra_body:
        kwargs["extra_body"] = extra_body
    if cfg.provider == "openrouter":
        kwargs["extra_headers"] = {"X-Title": "serialization-tabular-research", "HTTP-Referer": "https://localhost"}

    async for attempt in AsyncRetrying(
        retry=exp1.retry_if_exception_type(exp1.RETRYABLE),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    ):
        with attempt:
            resp = await client.chat.completions.create(**kwargs)

    choice = resp.choices[0]
    usage = resp.usage
    return {
        "raw_text": choice.message.content or "",
        "finish_reason": choice.finish_reason,
        "prompt_tokens": usage.prompt_tokens if usage else 0,
        "completion_tokens": usage.completion_tokens if usage else 0,
    }


async def run_react_episode(
    client, cfg: "exp1.ModelConfig", task: dict, max_steps: int, react_max_tokens: int
) -> dict:
    header, rows = _parse_table(task["csv"])
    columns_meta: dict = task["columns_meta"]
    question = exp1.build_question(
        {"property": task["property"], "columns": task["columns"], "question": task.get("question")}
    )
    prompt = build_react_prompt(task["csv"], question)
    messages = [{"role": "user", "content": prompt}]

    tool_calls: list[dict] = []
    finished = False
    final_answer_text = ""
    total_in = total_out = 0
    last_finish_reason = None

    for _ in range(max_steps):
        result = await call_chat(client, cfg, messages, react_max_tokens)
        total_in += result["prompt_tokens"]
        total_out += result["completion_tokens"]
        last_finish_reason = result["finish_reason"]
        assistant_text = result["raw_text"]
        messages.append({"role": "assistant", "content": assistant_text})

        parsed_action = parse_action(assistant_text)
        if parsed_action is None:
            tool_calls.append({"action": "invalid", "args": {}, "observation_len": None})
            obs = f"Observation: invalid action, expected one of: {', '.join(TOOL_NAMES)}"
            messages.append({"role": "user", "content": obs})
            continue

        name, arg_str = parsed_action
        if name == "Finish":
            tool_calls.append({"action": "Finish", "args": {"raw": arg_str}, "observation_len": 0})
            final_answer_text = arg_str
            finished = True
            break

        try:
            if name == "ColumnDtype":
                col = arg_str.strip().strip("'\"")
                if col not in columns_meta:
                    raise ValueError(f"unknown column {col!r}")
                obs_value = columns_meta[col]["dtype"]
            else:
                obs_value = run_tool(name, arg_str, header, rows, task["csv"])
        except ValueError as e:
            tool_calls.append({"action": "invalid", "args": {"raw": arg_str}, "observation_len": None})
            obs = f"Observation: invalid action, {e}"
            messages.append({"role": "user", "content": obs})
            continue

        tool_calls.append(
            {"action": name, "args": {"raw": arg_str}, "observation_len": len(str(obs_value))}
        )
        messages.append({"role": "user", "content": f"Observation: {obs_value}"})

    parsed, parse_success = (None, False)
    if finished:
        parsed, parse_success = exp1.parse_answer(final_answer_text, task["property"])
    metrics = exp1.score(task["property"], task["ground_truth"], parsed, parse_success)

    return {
        "object_id": task["object_id"],
        "tier": task["tier"],
        "source": task["source"],
        "n_rows": task["n_rows"],
        "n_cols": task["n_cols"],
        "query_id": task["query_id"],
        "property": task["property"],
        "columns": task["columns"],
        "property_locality": task["locality"],
        "is_control": task["is_control"],
        "ground_truth": task["ground_truth"],
        "raw_model_output": final_answer_text if finished else "",
        "parsed_answer": parsed,
        "parse_success": parse_success,
        "model": cfg.model,
        "provider": cfg.provider,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "react_steps": len(tool_calls),
        "react_finished": finished,
        "react_tool_calls": tool_calls,
        "react_transcript": "\n\n".join(f"[{m['role']}] {m['content']}" for m in messages),
        "prompt_tokens": total_in,
        "completion_tokens": total_out,
        "finish_reason": last_finish_reason,
        **metrics,
    }


# ----------------------------------------------------------------------
# Zero-shot (matched control) episode -- literally Experiment 1's method,
# called again in this same process/session.
# ----------------------------------------------------------------------


async def run_zeroshot_episode(client, cfg: "exp1.ModelConfig", task: dict) -> dict:
    query = {"property": task["property"], "columns": task["columns"], "question": task.get("question")}
    prompt = exp1.build_prompt(task["csv"], query, cfg.prompt_suffix)
    result = await exp1.call_model(client, cfg, prompt)
    parsed, parse_success = exp1.parse_answer(result["raw_text"], task["property"])
    metrics = exp1.score(task["property"], task["ground_truth"], parsed, parse_success)
    return {
        "object_id": task["object_id"],
        "tier": task["tier"],
        "source": task["source"],
        "n_rows": task["n_rows"],
        "n_cols": task["n_cols"],
        "query_id": task["query_id"],
        "property": task["property"],
        "columns": task["columns"],
        "property_locality": task["locality"],
        "is_control": task["is_control"],
        "ground_truth": task["ground_truth"],
        "raw_model_output": result["raw_text"],
        "parsed_answer": parsed,
        "parse_success": parse_success,
        "model": cfg.model,
        "provider": cfg.provider,
        "temperature": cfg.temperature,
        "finish_reason": result["finish_reason"],
        "prompt_tokens": result["prompt_tokens"],
        "completion_tokens": result["completion_tokens"],
        "latency_s": round(result["latency"], 3),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **metrics,
    }


# ----------------------------------------------------------------------
# Task construction (adds csv/columns_meta on top of exp1's task dict)
# ----------------------------------------------------------------------


def build_tasks(records: list[dict], properties_filter: Optional[set[str]], limit: Optional[int]) -> list[dict]:
    tasks = []
    selected = records[:limit] if limit is not None else records
    for r in selected:
        for q in r["queries"]:
            if properties_filter and q["property"] not in properties_filter:
                continue
            tasks.append(
                {
                    "object_id": r["object_id"],
                    "tier": r["tier"],
                    "source": r["source"],
                    "n_rows": r["n_rows"],
                    "n_cols": r["n_cols"],
                    "query_id": q["query_id"],
                    "property": q["property"],
                    "columns": q["columns"],
                    "locality": q["locality"],
                    "ground_truth": q["ground_truth"],
                    "is_control": q.get("is_control"),
                    "question": q.get("question"),
                    "csv": r["csv"],
                    "columns_meta": r["columns"],
                }
            )
    return tasks


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------


async def run_arm(
    episode_fn, client, cfg, tasks: list[dict], already_done: set[tuple[str, str]],
    jsonl_path: Path, concurrency: int, lock: asyncio.Lock, **episode_kwargs
) -> tuple[list[dict], list[dict]]:
    semaphore = asyncio.Semaphore(concurrency)
    pending = [t for t in tasks if (t["object_id"], t["query_id"]) not in already_done]

    async def one(task):
        async with semaphore:
            try:
                record = await episode_fn(client, cfg, task, **episode_kwargs)
            except Exception as e:  # noqa: BLE001
                return {"task": task, "error": str(e)}
            async with lock:
                with jsonl_path.open("a") as f:
                    f.write(json.dumps(record) + "\n")
            return record

    raw = await asyncio.gather(*[one(t) for t in pending])
    successes = [r for r in raw if "error" not in r]
    failures = [r["task"] for r in raw if "error" in r]
    return successes, failures


def parse_args(cfg) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=f"Phase 2 ReAct + matched zero-shot runner for {cfg.name}")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True, help="Directory for this model's output files")
    p.add_argument("--properties", type=str, default=None)
    p.add_argument("--limit", type=int, default=None, help="First N table objects only (smoke test)")
    p.add_argument("--subset", type=str, default=cfg.default_subset or "none", help="Subset file of object_ids (relative to cwd), or 'none'")
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument("--max-steps", type=int, default=MAX_STEPS_DEFAULT)
    p.add_argument("--react-max-tokens", type=int, default=None, help="Defaults to cfg.max_tokens")
    p.add_argument("--force", action="store_true")
    p.add_argument("--with-zeroshot", action="store_true", help="Also run the matched single-call zero-shot control (off by default)")
    p.add_argument("--skip-react", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--price-in", type=float, default=cfg.price_in)
    p.add_argument("--price-out", type=float, default=cfg.price_out)
    return p.parse_args()


def main(cfg) -> None:
    from dotenv import load_dotenv

    load_dotenv()
    args = parse_args(cfg)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    react_jsonl = args.out_dir / f"{cfg.name}_results.jsonl"
    react_json = args.out_dir / f"{cfg.name}_results.json"
    zs_jsonl = args.out_dir / f"{cfg.name}_zeroshot_results.jsonl"
    zs_json = args.out_dir / f"{cfg.name}_zeroshot_results.json"

    records = exp1.load_dataset(args.dataset)
    records = exp1.apply_subset(records, None if args.subset in (None, "none") else Path(args.subset))
    properties_filter = set(args.properties.split(",")) if args.properties else None
    tasks = build_tasks(records, properties_filter, args.limit)
    print(f"Built {len(tasks)} task(s) from {len(records)} table record(s).")

    if args.dry_run:
        t = tasks[0]
        question = exp1.build_question({"property": t["property"], "columns": t["columns"], "question": t.get("question")})
        print("[dry-run] sample ReAct initial prompt:")
        print("-" * 60)
        print(build_react_prompt(t["csv"], question))
        print("-" * 60)
        print(f"[dry-run] {len(tasks)} tasks planned, no API calls made.")
        return

    react_max_tokens = args.react_max_tokens or cfg.max_tokens
    client = exp1.make_client(cfg)
    lock = asyncio.Lock()
    concurrency = args.concurrency or cfg.concurrency

    async def _run():
        react_successes = zs_successes = []
        react_failures = zs_failures = []

        if not args.skip_react:
            already = set() if args.force else exp1.done_pairs(react_jsonl, False)
            react_successes, react_failures = await run_arm(
                run_react_episode, client, cfg, tasks, already, react_jsonl, concurrency, lock,
                max_steps=args.max_steps, react_max_tokens=react_max_tokens,
            )
            exp1.write_json_export(react_jsonl, react_json)

        if args.with_zeroshot:
            already = set() if args.force else exp1.done_pairs(zs_jsonl, False)
            zs_successes, zs_failures = await run_arm(
                run_zeroshot_episode, client, cfg, tasks, already, zs_jsonl, concurrency, lock,
            )
            exp1.write_json_export(zs_jsonl, zs_json)

        return react_successes, react_failures, zs_successes, zs_failures

    react_successes, react_failures, zs_successes, zs_failures = asyncio.run(_run())

    def _summarize(label, successes, failures):
        if not successes and not failures:
            return
        total_in = sum(r["prompt_tokens"] for r in successes)
        total_out = sum(r["completion_tokens"] for r in successes)
        cost = total_in / 1e6 * args.price_in + total_out / 1e6 * args.price_out
        n_correct = sum(1 for r in successes if r.get("correct"))
        n_finished = sum(1 for r in successes if r.get("react_finished", True))
        print(
            f"\n=== {cfg.name} {label} complete ===\n"
            f"Episodes: {len(successes)} | API failures: {len(failures)}\n"
            f"Correct (moderate tol.): {n_correct}/{len(successes)}"
            + (f" | Finished: {n_finished}/{len(successes)}" if label == "ReAct" else "")
            + f"\nTokens: {total_in} in / {total_out} out | Cost: ${cost:.4f}"
        )
        if failures:
            print(f"{len(failures)} episode(s) failed with API errors -- re-run to retry them.")

    _summarize("ReAct", react_successes, react_failures)
    _summarize("zero-shot", zs_successes, zs_failures)
