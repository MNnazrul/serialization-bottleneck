"""
Experiment 2 (ReAct) — shared Phase-2 loop for Domain 2 (Graphs).

This is an *additional evaluation experiment* layered on top of Experiment 1's
Domain-2 task: the same 300-graph dataset, the same 8 questions, the same
ground truth, and the same scoring — but each question is answered with an
interleaved Thought / Action / Observation loop (Yao et al., 2023,
"ReAct: Synergizing Reasoning and Acting in Language Models") instead of a
single zero-shot call.

Nothing here re-implements Experiment 1's machinery. This module imports the
Experiment-1 graph harness and reuses it wholesale:

    ../../phase2_model_results_graph/_common/graph_harness.py

Reused unchanged: dataset load/validate, subset filtering, the
(object_id, property) resume index, the uncertified-chromatic_number
exclusion, answer normalization, metric computation, failure classification,
the retry policy's exception set, the provider table, client construction, the
thinking-disable mechanism, logging, JSON export, the cost summary, and the
CLI surface. Only the call itself changes: one API call per question becomes a
capped Thought/Action/Observation loop with structural query tools over the
serialized graph.

The record schema is a superset of Experiment 1's — every field Phase 3 reads
(`property`, `parse_success`, `correct`, `correct_1pct`, ...) is present and
computed by the same `add_metrics`, plus a few ReAct-specific fields
(`method`, `react_steps`, `react_finished`, `react_tool_calls`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from tqdm import tqdm

# --- reuse the Experiment-1 Domain-2 harness (no edits to it) ----------------
_EXP1_COMMON = Path(__file__).resolve().parent.parent.parent / "phase2_model_results_graph" / "_common"
if str(_EXP1_COMMON) not in sys.path:
    sys.path.insert(0, str(_EXP1_COMMON))

import graph_harness as gh  # noqa: E402  (the Experiment-1 harness)
from graph_harness import ModelConfig  # noqa: E402,F401  (re-exported for the runners)


# ---------------------------------------------------------------------------
# The graph, read straight off the serialization
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(r"GRAPH\s*\(\s*n\s*=\s*(\d+)\s*,\s*m\s*=\s*(\d+)\s*\)", re.IGNORECASE)
_INT_RE = re.compile(r"-?\d+")


def parse_edge_list(edge_list: str) -> tuple[int, int, dict[int, set[int]]]:
    """`GRAPH (n=.., m=..):` header + `u v` lines -> (n, m, adjacency).

    n and m come from the header when present (that header *is* the
    serialization's own answer to `edge_count`); if it is malformed, n falls
    back to max-node-id + 1 and m to the parsed edge count.
    """
    n: int | None = None
    m: int | None = None
    edges: list[tuple[int, int]] = []
    for raw in edge_list.splitlines():
        line = raw.strip()
        if not line:
            continue
        head = _HEADER_RE.search(line)
        if head:
            n, m = int(head.group(1)), int(head.group(2))
            continue
        nums = _INT_RE.findall(line)
        if len(nums) >= 2:
            edges.append((int(nums[0]), int(nums[1])))
    if n is None:
        n = max((max(u, v) for u, v in edges), default=-1) + 1
    if m is None:
        m = len(edges)
    adj: dict[int, set[int]] = {i: set() for i in range(n)}
    for u, v in edges:
        if 0 <= u < n and 0 <= v < n and u != v:
            adj[u].add(v)
            adj[v].add(u)
    return n, m, adj


# ---------------------------------------------------------------------------
# Action space: structural queries + Finish (the paper's search/lookup/finish)
# ---------------------------------------------------------------------------

VALID_ACTIONS = "Neighbors[n], Degree[n], HasEdge[u, v], Nodes[], EdgeCount[], Finish[answer]"
_INVALID_OBS = f"Invalid action. Valid actions: {VALID_ACTIONS}."

_ACTION_RE = re.compile(r"Action\s*\d*\s*:\s*([A-Za-z_]+)\s*\[(.*?)\]", re.IGNORECASE | re.DOTALL)
# The prompt primes the next turn with "Thought N:"; models often echo that
# label back. Strip one leading occurrence so the transcript is not doubled.
_LEADING_THOUGHT_RE = re.compile(r"^\s*Thought\s*\d*\s*:\s*", re.IGNORECASE)
_ACTION_ALIASES = {
    "neighbors": "neighbors", "neighbours": "neighbors", "neighbor": "neighbors",
    "degree": "degree",
    "hasedge": "hasedge", "has_edge": "hasedge", "edge": "hasedge",
    "nodes": "nodes", "nodelist": "nodes",
    "edgecount": "edgecount", "edge_count": "edgecount", "numedges": "edgecount",
    "finish": "finish", "answer": "finish",
}


def parse_action(text: str) -> tuple[str | None, str]:
    """Return (canonical_action, arg) from the LAST `Action: Name[arg]` in the
    model's turn, or (None, raw_tail) if nothing matches."""
    matches = list(_ACTION_RE.finditer(text or ""))
    if not matches:
        return None, (text or "").strip()[-80:]
    name, arg = matches[-1].group(1), matches[-1].group(2)
    return _ACTION_ALIASES.get(name.lower()), arg.strip()


def execute_tool(action: str | None, arg: str, n: int, adj: dict[int, set[int]], m: int) -> str:
    """Run one structural query against the parsed graph and return its
    Observation text. No property is computed for the model — only raw
    structure is read back, the same way the paper's search[] reads a wiki
    page rather than answering the question."""
    if action == "nodes":
        return f"Nodes: {list(range(n))}  ({n} nodes total)."
    if action == "edgecount":
        return f"The graph has {m} edges."
    if action in ("neighbors", "degree"):
        nums = _INT_RE.findall(arg)
        if not nums:
            return f"Could not read a node id from '{arg}'. Give an integer 0..{n - 1}."
        k = int(nums[0])
        if not 0 <= k < n:
            return f"Node {k} is not in the graph (nodes are 0..{n - 1})."
        if action == "degree":
            return f"Node {k} has degree {len(adj[k])}."
        return f"Node {k} has neighbors: {sorted(adj[k])}."
    if action == "hasedge":
        nums = _INT_RE.findall(arg)
        if len(nums) < 2:
            return "HasEdge needs two node ids, e.g. HasEdge[1, 2]."
        u, v = int(nums[0]), int(nums[1])
        for x in (u, v):
            if not 0 <= x < n:
                return f"Node {x} is not in the graph (nodes are 0..{n - 1})."
        return f"Edge ({u}, {v}) is {'present' if v in adj[u] else 'not present'}."
    return _INVALID_OBS


# ---------------------------------------------------------------------------
# Prompt: instructions + two hand-written exemplars (paper, Appendix C)
# ---------------------------------------------------------------------------

REACT_INSTRUCTIONS = f"""You are given an undirected graph as an edge list and one question about it.
Answer it by working in a loop:
  Thought N: reason about what to check next.
  Action N: exactly one action from the list below, in square brackets.
  Observation N: the result (given back to you).
Repeat until you can answer, then use Finish.

Actions:
  Neighbors[n]    - the sorted list of nodes adjacent to node n
  Degree[n]       - the number of edges incident to node n
  HasEdge[u, v]   - whether the edge (u, v) is in the graph
  Nodes[]         - the list of all node ids
  EdgeCount[]     - the number of edges in the graph
  Finish[answer]  - stop and report the final answer

Nodes are integers 0..N-1. Finish takes exactly one value: an integer for a
count, degree, diameter or chromatic number; a decimal for an average
clustering coefficient; true or false for a yes/no question. Put nothing else
inside Finish[...]."""

_EXEMPLAR_1 = """Graph:
GRAPH (n=4, m=3):
0 1
0 2
2 3

Question: What is the degree of node 0 in this graph?
Thought 1: I need the number of edges incident to node 0. I can read that directly.
Action 1: Degree[0]
Observation 1: Node 0 has degree 2.
Thought 2: Node 0 has degree 2, so the answer is 2.
Action 2: Finish[2]"""

_EXEMPLAR_2 = """Graph:
GRAPH (n=5, m=5):
0 1
0 2
1 2
1 3
3 4

Question: How many triangles are in this graph?
Thought 1: A triangle is three nodes all pairwise adjacent. I will take each node's neighbours and test whether neighbour pairs are also joined. Start at node 0.
Action 1: Neighbors[0]
Observation 1: Node 0 has neighbors: [1, 2].
Thought 2: Node 0's neighbours are 1 and 2. If edge (1, 2) exists then {0, 1, 2} is a triangle.
Action 2: HasEdge[1, 2]
Observation 2: Edge (1, 2) is present.
Thought 3: {0, 1, 2} is a triangle. Now check node 1's other neighbours for more triangles.
Action 3: Neighbors[1]
Observation 3: Node 1 has neighbors: [0, 2, 3].
Thought 4: The untested neighbour pair on node 1 is (2, 3). Check it.
Action 4: HasEdge[2, 3]
Observation 4: Edge (2, 3) is not present.
Thought 5: No triangle through (1, 2, 3). Node 3 only reaches 1 and 4, and node 4 only reaches 3, so there are no further triangles. Total: 1.
Action 5: Finish[1]"""


def build_react_prompt(edge_list: str, question: str) -> str:
    return (
        REACT_INSTRUCTIONS
        + "\n\nHere are two worked examples.\n\nExample 1\n"
        + _EXEMPLAR_1
        + "\n\nExample 2\n"
        + _EXEMPLAR_2
        + "\n\nNow answer this one, in the same format.\n\nGraph:\n"
        + edge_list
        + f"\n\nQuestion: {question}\nThought 1:"
    )


# ---------------------------------------------------------------------------
# One model turn (same retry policy as graph_harness.make_call_model, plus a
# stop sequence so the model cannot hallucinate its own Observation lines)
# ---------------------------------------------------------------------------

def make_call_turn(log: logging.Logger):
    @retry(
        retry=retry_if_exception_type(gh.RETRYABLE),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
        before_sleep=before_sleep_log(log, logging.WARNING),
    )
    async def call_turn(client, model, messages, temperature, max_tokens,
                        extra_body=None, reasoning_effort=None):
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "extra_body": extra_body or {},
            "stop": ["\nObservation"],
        }
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        resp = await client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        content = choice.message.content or ""
        usage = {}
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
            }
        return content, usage, choice.finish_reason

    return call_turn


# ---------------------------------------------------------------------------
# The episode: a capped Thought/Action/Observation loop for one (graph, prop)
# ---------------------------------------------------------------------------

async def run_episode(client, model, row, prop, temperature, max_tokens, max_steps,
                      extra_body, reasoning_effort, think_suffix, call_turn,
                      log: logging.Logger) -> dict[str, Any]:
    n, m, adj = parse_edge_list(row["edge_list"])
    initial_prompt = build_react_prompt(row["edge_list"], gh.QUESTIONS[prop])
    messages = [{"role": "user", "content": initial_prompt + think_suffix}]
    transcript = initial_prompt

    usage_tot = {"prompt_tokens": 0, "completion_tokens": 0}
    tool_calls: list[dict[str, Any]] = []
    parsed: Any = None
    ok = finished = False
    finish_reason: str | None = None
    steps = 0

    for i in range(max_steps):
        text, usage, finish_reason = await call_turn(
            client, model, messages, temperature, max_tokens, extra_body, reasoning_effort
        )
        usage_tot["prompt_tokens"] += usage.get("prompt_tokens") or 0
        usage_tot["completion_tokens"] += usage.get("completion_tokens") or 0
        text = _LEADING_THOUGHT_RE.sub("", (text or "").strip(), count=1)
        steps = i + 1
        transcript += " " + text

        action, arg = parse_action(text)
        if action == "finish":
            parsed, ok = gh.normalize_answer(arg, prop)
            finished = True
            tool_calls.append({"step": steps, "action": "Finish", "arg": arg, "observation": None})
            log.debug("  step %d Finish[%s] -> parsed=%r ok=%s", steps, arg[:40], parsed, ok)
            break

        obs = _INVALID_OBS if action is None else execute_tool(action, arg, n, adj, m)
        tool_calls.append({"step": steps, "action": action or "invalid", "arg": arg, "observation": obs})
        obs_block = f"Observation {steps}: {obs}"
        next_label = f"\nThought {steps + 1}:"
        transcript += "\n" + obs_block + next_label
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": obs_block + next_label})
        log.debug("  step %d %s[%s] -> %s", steps, action or "INVALID", arg[:40], obs[:90])

    transcript = re.sub(r"\nThought \d+:\s*$", "", transcript).strip()
    return {
        "prompt": initial_prompt,
        "transcript": transcript,
        "parsed": parsed,
        "ok": ok,
        "finished": finished,
        "steps": steps,
        "tool_calls": tool_calls,
        "usage": usage_tot,
        "finish_reason": finish_reason,
    }


def make_react_record(row, prop, ep: dict[str, Any], model: str, provider: str,
                      temperature: float, max_tokens: int) -> dict[str, Any]:
    """Experiment-1's record fields (so Phase 3 reads it unchanged) plus the
    ReAct-specific ones. Metrics come from the reused `gh.add_metrics`."""
    ok = ep["ok"]
    if ok:
        failure_type = None
    elif not ep["finished"]:
        failure_type = "no_finish"
    else:
        failure_type = gh.classify_failure(
            ok, ep["finish_reason"], ep["usage"].get("completion_tokens"), max_tokens
        )

    rec = {
        "object_id": row["object_id"],
        "tier": row["tier"],
        "family": row["family"],
        "num_nodes": row["num_nodes"],
        "num_edges": row["num_edges"],
        "property": prop,
        "property_locality": "local" if prop in gh.LOCAL_PROPERTIES else "global",
        "ground_truth": row["properties"][prop],
        "prompt": ep["prompt"],
        "raw_model_output": ep["transcript"],
        "parsed_answer": ep["parsed"],
        "parse_success": ok,
        "finish_reason": ep["finish_reason"],
        "failure_type": failure_type,
        "method": "react",
        "react_steps": ep["steps"],
        "react_finished": ep["finished"],
        "react_tool_calls": ep["tool_calls"],
        "model": model,
        "provider": provider,
        "temperature": temperature,
        "prompt_tokens": ep["usage"].get("prompt_tokens"),
        "completion_tokens": ep["usage"].get("completion_tokens"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    gh.add_metrics(rec, ep["parsed"], ok)
    return rec


# ---------------------------------------------------------------------------
# CLI + run  (same surface as graph_harness, plus --max-steps)
# ---------------------------------------------------------------------------

def build_arg_parser(config: ModelConfig, here: Path):
    p = gh.build_arg_parser(config, here)
    p.description = f"ReAct (Experiment 2) {config.label} run over the 300-graph dataset."
    p.add_argument("--max-steps", type=int, default=15,
                   help="Max Thought/Action/Observation steps before an episode gives up (default 15).")
    return p


async def run(args, config: ModelConfig) -> None:
    load_dotenv()
    log_path, log = gh.setup_logging(args.log_dir, config.key, f"react_{config.key}")
    call_turn = make_call_turn(log)

    log.info("=" * 56)
    log.info("%s ReAct RUN start (Experiment 2, Domain 2)", config.label)
    log.info("provider=%s model=%s temp=%s max_tokens=%s max_steps=%s concurrency=%s",
             args.provider, args.model, args.temperature, args.max_tokens,
             args.max_steps, args.concurrency)
    log.info("dataset=%s log=%s", args.dataset, log_path)

    dataset = gh.load_dataset(args.dataset, log)
    dataset = gh.apply_subset(dataset, args.subset)
    completed = set() if args.force else gh.done_pairs(args.jsonl_output, args.retry_parse_failures)
    tasks = gh.build_tasks(dataset, args.properties, completed, args.limit, args.force)

    log.info("graphs=%d props=%d | planned episodes=%d | skipping=%d",
             len(dataset), len(args.properties), len(tasks), len(completed))
    print(f"Provider: {args.provider}  |  Model: {args.model}  |  thinking: {args.thinking}")
    print(f"Method: ReAct  |  max steps/episode: {args.max_steps}")
    print(f"Graphs: {len(dataset)} x {len(args.properties)} props "
          f"(uncertified chromatic_number pairs excluded)")
    print(f"Planned new episodes: {len(tasks)}"
          + (f"  (skipping {len(completed)} already done)" if completed else ""))
    print(f"Log file: {log_path}")

    if args.dry_run:
        for row, prop in tasks[:3]:
            print(f"\n--- {row['object_id']} / {prop} ---")
            print(build_react_prompt(row["edge_list"], gh.QUESTIONS[prop]))
        log.info("[dry-run] %d episodes would run. No API calls.", len(tasks))
        print(f"\n[dry-run] {len(tasks)} episodes would run. No API calls.")
        return

    base_url, key_env = gh.PROVIDERS[args.provider]
    api_key = os.getenv(key_env)
    if not api_key:
        raise RuntimeError(f"{key_env} not set. Add it to .env.")

    args.jsonl_output.parent.mkdir(parents=True, exist_ok=True)
    client = gh.make_client(base_url, api_key, args.provider)
    sem = asyncio.Semaphore(max(1, args.concurrency))
    write_lock = asyncio.Lock()
    extra_body, reasoning_effort, think_suffix = gh.resolve_thinking(config, args)
    log.info("thinking=%s extra_body=%s reasoning_effort=%s think_suffix=%r fail_fast=%s",
             args.thinking, extra_body, reasoning_effort, think_suffix, args.fail_fast)

    totals = {"prompt_tokens": 0, "completion_tokens": 0, "parsed_ok": 0, "n": 0}
    failures: list[tuple[str, str, str]] = []

    def write_record(rec, usage, ok):
        totals["prompt_tokens"] += usage.get("prompt_tokens") or 0
        totals["completion_tokens"] += usage.get("completion_tokens") or 0
        totals["parsed_ok"] += int(ok)
        totals["n"] += 1
        with args.jsonl_output.open("a") as h:
            h.write(json.dumps(rec, ensure_ascii=True) + "\n")
            h.flush()

    async def run_one(row, prop):
        ep = await run_episode(client, args.model, row, prop, args.temperature,
                               args.max_tokens, args.max_steps, extra_body,
                               reasoning_effort, think_suffix, call_turn, log)
        rec = make_react_record(row, prop, ep, args.model, args.provider,
                                args.temperature, args.max_tokens)
        log.debug("EPISODE %-30s %-18s finished=%s steps=%d parse=%s ans=%s in=%d out=%d",
                  row["object_id"], prop, ep["finished"], ep["steps"], ep["ok"],
                  repr(ep["parsed"])[:40], ep["usage"]["prompt_tokens"], ep["usage"]["completion_tokens"])
        return rec, ep

    # ---- FAIL-FAST: sequential; stop at first API error OR parse failure ----
    if args.fail_fast:
        log.info("FAIL-FAST mode: sequential, stop at first failure.")
        progress = tqdm(total=len(tasks), unit="episode")
        for row, prop in tasks:
            try:
                rec, ep = await run_one(row, prop)
            except Exception as exc:  # noqa: BLE001
                progress.close()
                gh.stop_on_failure(row, prop, "API ERROR", f"{type(exc).__name__}: {exc}",
                                   None, log_path, totals, log)
                return
            write_record(rec, ep["usage"], ep["ok"])
            if not ep["ok"]:
                gh.write_json_export(args.jsonl_output, args.json_output)
                progress.close()
                detail = "no Finish within max steps" if not ep["finished"] else f"could not parse {prop}"
                gh.stop_on_failure(row, prop, "PARSE FAILURE", detail,
                                   ep["transcript"], log_path, totals, log)
                return
            progress.update(1)
        progress.close()
        gh.write_json_export(args.jsonl_output, args.json_output)
        gh.print_summary(totals, args, config)
        print("\nAll episodes completed with NO failures. (fail-fast)")
        log.info("FAIL-FAST end | completed=%d tokens in=%d out=%d",
                 totals["n"], totals["prompt_tokens"], totals["completion_tokens"])
        return

    # ---- NORMAL: concurrent; one failure does not abort the batch ----
    progress = tqdm(total=len(tasks), unit="episode")

    async def tracked(row, prop):
        try:
            async with sem:
                rec, ep = await run_one(row, prop)
            async with write_lock:
                write_record(rec, ep["usage"], ep["ok"])
            if not ep["ok"]:
                log.warning("UNRESOLVED %-30s %-18s finished=%s steps=%d",
                            row["object_id"], prop, ep["finished"], ep["steps"])
        except Exception as exc:  # noqa: BLE001
            async with write_lock:
                failures.append((row["object_id"], prop, type(exc).__name__))
            log.error("FAIL %-30s %-18s %s: %s", row["object_id"], prop, type(exc).__name__, exc)
        finally:
            progress.update(1)

    await asyncio.gather(*(tracked(row, prop) for row, prop in tasks))
    progress.close()

    gh.write_json_export(args.jsonl_output, args.json_output)
    gh.print_summary(totals, args, config)
    gh.report_failures(failures, log)
    log.info("RUN end | completed=%d failed=%d tokens in=%d out=%d",
             totals["n"], len(failures), totals["prompt_tokens"], totals["completion_tokens"])
    log.info("full log saved -> %s", log_path)


def main(config: ModelConfig, here: Path) -> None:
    args = build_arg_parser(config, here).parse_args()
    try:
        asyncio.run(run(args, config))
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
