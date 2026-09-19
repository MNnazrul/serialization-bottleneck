"""
Domain 2 (Graphs) — shared Phase 2 query harness.

One harness, six configurations. This module holds everything that does not
vary by model: dataset loading, subset filtering, resume, prompt/parsing,
metrics, retry policy, async fan-out, JSONL/JSON export, cost accounting, and
the CLI surface. Each `run_<model>_full.py` in the sibling model folders is a
config block of ~40 lines (model id, provider, thinking mechanism, JSON mode,
prices, coverage) that calls `main(CONFIG)` here.

This follows the porting advice in ../../phase2_model_results/README.md §10:
"If you port, fix the duplication first... roughly 60 lines per model instead
of 700." The geometry domain's six runners are ~99% byte-identical to each
other (verified: same TEMPLATE/QUESTIONS MD5 across all six); this domain
does not repeat that.

Per Experiment 1 protocol (serialization_experiment_1.pdf, Section 4):
  - 300 graphs x 8 properties = 2,400 queries per full-coverage model
  - temperature 0, max_tokens 512, no system prompt, zero-shot
  - retry: exponential backoff (2s, 4s, 8s), transient errors only
  - one JSON template for all properties, all models
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    NotFoundError,
    RateLimitError,
)
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Domain-fixed constants (Section 4 of the PDF). Identical for all six models.
# ---------------------------------------------------------------------------

PROVIDERS = {
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "google": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
}

PROPERTIES = (
    "degree_of_node_0", "edge_count", "triangle_count", "is_bipartite",
    "is_planar", "diameter", "chromatic_number", "avg_clustering",
)
LOCAL_PROPERTIES = {"degree_of_node_0", "edge_count"}
INTEGER_PROPERTIES = {"degree_of_node_0", "edge_count", "triangle_count", "diameter", "chromatic_number"}
SCALAR_REL_PROPERTIES = {"avg_clustering"}
BOOLEAN_PROPERTIES = {"is_bipartite", "is_planar"}

TEMPLATE = """You are given an undirected graph as an edge list.

Graph:
{edge_list}

Question:
{question}

Respond with only a JSON object in this format:
{{"answer": <your answer>}}"""

QUESTIONS = {
    "degree_of_node_0": "What is the degree of node 0 in this graph?",
    "edge_count": "How many edges does this graph have?",
    "triangle_count": "How many triangles are in this graph?",
    "is_bipartite": "Is this graph bipartite? Answer true or false.",
    "is_planar": "Is this graph planar? Answer true or false.",
    "diameter": "What is the diameter of this graph?",
    "chromatic_number": "What is the chromatic number of this graph?",
    "avg_clustering": "What is the average clustering coefficient of this graph?",
}

NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
# degree_of_node_0's question text names the node by number ("node 0"), so a
# prose fallback answer like "the degree of node 0 is 4" would otherwise have
# its first-number scan collide with that "0" instead of the actual answer.
NODE0_RE = re.compile(r"\bnode\s*#?\s*_?0\b", re.IGNORECASE)

REQUIRED_DATASET_KEYS = {"object_id", "tier", "family", "num_nodes", "num_edges", "edge_list", "properties", "metadata"}

# PDF §2: retry on timeout / rate-limit / connection / 5xx only, not 4xx.
RETRYABLE = (APITimeoutError, RateLimitError, APIConnectionError, InternalServerError)


def build_prompt(edge_list: str, property_name: str) -> str:
    return TEMPLATE.format(edge_list=edge_list, question=QUESTIONS[property_name])


# ---------------------------------------------------------------------------
# Per-model configuration. Each run_<model>_full.py builds one of these.
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ModelConfig:
    key: str                       # short id, e.g. "v4flash" -- used for filenames/logger name
    label: str                     # display label, e.g. "V4-FLASH"
    default_provider: str
    default_model: str
    default_price_in: float        # $ / 1M input tokens
    default_price_out: float       # $ / 1M output tokens
    thinking_mode: str             # "deepseek" | "openrouter_qwen" | "google" | "none"
    supports_json_mode: bool = False
    default_json_mode: str = "off"
    default_subset: str = "none"   # "none" or a path to a subsample file
    default_max_tokens: int = 512
    model_help: str = ""
    extra_prompt_suffix: str = ""  # appended after think_suffix; model-specific nudge, not a shared-template change


def default_dataset_path(here: Path) -> Path:
    return here.parent.parent / "phase1_dataset_graph" / "graph_exp1_dataset.json"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_dir: Path, key: str, logger_name: str) -> tuple[Path, logging.Logger]:
    """Log to a timestamped file (DEBUG, every query) AND console (INFO)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"{key}_{stamp}.log"

    log = logging.getLogger(logger_name)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)
    return log_path, log


# ---------------------------------------------------------------------------
# Dataset loading, subsetting, resume
# ---------------------------------------------------------------------------

def load_dataset(path: Path, log: logging.Logger) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"dataset must be a JSON list: {path}")
    if len(data) != 300:
        log.warning("expected 300 graphs, found %d in %s", len(data), path)
    seen = set()
    for row in data:
        oid = row.get("object_id")
        if oid in seen:
            raise ValueError(f"duplicate object_id: {oid}")
        seen.add(oid)
        missing = REQUIRED_DATASET_KEYS - set(row)
        if missing:
            raise ValueError(f"{oid} missing keys: {sorted(missing)}")
    return data


def apply_subset(dataset: list[dict[str, Any]], subset_path: Path | str | None) -> list[dict[str, Any]]:
    """Keep only the graphs whose object_id is in the subset file. Pass
    subset_path == None / 'none' for the full set."""
    if subset_path is None or str(subset_path).lower() == "none":
        return dataset
    blob = json.loads(Path(subset_path).read_text())
    ids = set(blob["object_ids"] if isinstance(blob, dict) else blob)
    if not ids:
        raise ValueError(f"no object_ids in {subset_path}")
    rows = [r for r in dataset if r["object_id"] in ids]
    found = {r["object_id"] for r in rows}
    if found != ids:
        raise ValueError(f"subset ids not in dataset: {sorted(ids - found)[:5]}")
    return rows


def read_latest_records(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    if not path.exists():
        return latest
    with path.open() as h:
        for line in h:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            latest[(rec["object_id"], rec["property"])] = rec
    return latest


def done_pairs(path: Path, retry_parse_failures: bool) -> set[tuple[str, str]]:
    """Pairs to SKIP. A pair is done if logged AND (parsed ok, or not retrying
    parse failures). Crash leaves un-logged pairs; --retry-parse-failures frees
    parse-failed pairs too."""
    done: set[tuple[str, str]] = set()
    for key, rec in read_latest_records(path).items():
        if retry_parse_failures and not rec.get("parse_success", False):
            continue
        done.add(key)
    return done


def build_tasks(dataset, properties, completed, limit, force):
    """One task per (graph, property), skipping already-done pairs and pairs
    whose chromatic_number is uncertified (PDF: excluded from evaluation)."""
    rows = dataset[:limit] if limit is not None else dataset
    tasks = []
    for row in rows:
        for prop in properties:
            if prop == "chromatic_number" and not row["metadata"].get("chromatic_number_certified", True):
                continue
            if not force and (row["object_id"], prop) in completed:
                continue
            tasks.append((row, prop))
    return tasks


# ---------------------------------------------------------------------------
# Answer parsing and normalization
# ---------------------------------------------------------------------------

def normalize_answer(answer: Any, property_name: str) -> tuple[Any, bool]:
    if property_name in INTEGER_PROPERTIES:
        if isinstance(answer, bool):
            return None, False
        if isinstance(answer, int):
            return answer, True
        if isinstance(answer, float) and answer.is_integer():
            return int(answer), True
        if isinstance(answer, str):
            text = NODE0_RE.sub("", answer) if property_name == "degree_of_node_0" else answer
            m = NUMBER_RE.search(text)
            if m:
                v = float(m.group())
                if v.is_integer():
                    return int(v), True
        return None, False

    if property_name in SCALAR_REL_PROPERTIES:
        if isinstance(answer, bool):
            return None, False
        if isinstance(answer, (int, float)):
            return float(answer), math.isfinite(float(answer))
        if isinstance(answer, str):
            m = NUMBER_RE.search(answer.replace(",", ""))
            if m:
                v = float(m.group())
                return v, math.isfinite(v)
        return None, False

    if property_name in BOOLEAN_PROPERTIES:
        if isinstance(answer, bool):
            return answer, True
        if isinstance(answer, str):
            low = answer.strip().lower()
            if re.search(r"\b(true|yes)\b", low):
                return True, True
            if re.search(r"\b(false|no)\b", low):
                return False, True
        return None, False

    raise ValueError(f"unknown property: {property_name}")


def is_truncated(finish_reason: str | None, completion_tokens: int | None, max_tokens: int) -> bool:
    return finish_reason == "length" or (completion_tokens is not None and completion_tokens >= max_tokens)


def parse_answer(raw: str, property_name: str, truncated: bool = False) -> tuple[Any, bool]:
    stripped = raw.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict) and "answer" in parsed:
            ans, ok = normalize_answer(parsed["answer"], property_name)
            if ok:
                return ans, True
    except json.JSONDecodeError:
        pass
    for m in re.finditer(r"\{.*?\}", stripped, re.DOTALL):
        try:
            parsed = json.loads(m.group())
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "answer" in parsed:
            ans, ok = normalize_answer(parsed["answer"], property_name)
            if ok:
                return ans, True
    if truncated:
        # The response hit the token cap before finishing, and neither JSON
        # step above found an answer. Do NOT run the raw-string fallback here:
        # its "extract the first number / true-false-yes-no word" heuristic
        # exists for a model that gave a complete answer outside a JSON
        # wrapper, not for a chain-of-thought cut off mid-sentence. A
        # truncated trace routinely contains numbers that are not the answer
        # -- e.g. "...the graph has 13 nodes and I need to count triangles by
        # checking..." would otherwise have its "13" mis-read as a
        # triangle_count answer. Falling through here keeps parse_success
        # False so classify_failure correctly labels this
        # reasoning_truncated, instead of silently scoring a guess as data.
        return None, False
    ans, ok = normalize_answer(stripped, property_name)
    return (ans, True) if ok else (None, False)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def relative_scalar_error(predicted: Any, truth: Any) -> tuple[float, float]:
    # PDF S6.2, Eq. 1: use absolute error in place of relative error whenever
    # |truth| < 0.01, not just when truth is exactly 0 -- avoids blowing up the
    # relative error on avg_clustering values that are merely close to zero
    # (e.g. sparse/tree-like graphs), not just exactly zero.
    abs_err = abs(float(predicted) - float(truth))
    denom = abs(float(truth))
    rel = abs_err if denom < 0.01 else abs_err / denom
    return abs_err, rel


def add_metrics(rec: dict[str, Any], parsed: Any, ok: bool) -> None:
    name = rec["property"]
    truth = rec["ground_truth"]

    if not ok:
        if name in BOOLEAN_PROPERTIES:
            rec["correct"] = False
        elif name in INTEGER_PROPERTIES:
            rec["absolute_error"] = None
            rec["correct"] = False
        else:
            rec["absolute_error"] = None
            rec["relative_error"] = None
            rec["correct_1pct"] = rec["correct_5pct"] = rec["correct_10pct"] = False
        return

    if name in SCALAR_REL_PROPERTIES:
        abs_err, rel = relative_scalar_error(parsed, truth)
        rec["absolute_error"] = abs_err
        rec["relative_error"] = rel
        rec["correct_1pct"] = rel <= 0.01
        rec["correct_5pct"] = rel <= 0.05
        rec["correct_10pct"] = rel <= 0.10
        return

    if name in INTEGER_PROPERTIES:
        abs_err = abs(int(parsed) - int(truth))
        rec["absolute_error"] = abs_err
        rec["correct"] = abs_err == 0
        return

    if name in BOOLEAN_PROPERTIES:
        rec["correct"] = parsed == truth
        return

    raise ValueError(f"unknown property: {name}")


def classify_failure(ok, finish_reason, completion_tokens, max_tokens):
    """PDF §6.1: a non-parseable output is `reasoning_truncated` if it hit the
    token limit (finish_reason=length or completion_tokens>=max_tokens), else
    `parse_failure`. Both are treated as incorrect. None when parsed ok."""
    if ok:
        return None
    return "reasoning_truncated" if is_truncated(finish_reason, completion_tokens, max_tokens) else "parse_failure"


def make_record(row, property_name, raw_output, parsed, ok, model, provider,
                temperature, prompt, usage, finish_reason, max_tokens) -> dict[str, Any]:
    rec = {
        "object_id": row["object_id"],
        "tier": row["tier"],
        "family": row["family"],
        "num_nodes": row["num_nodes"],
        "num_edges": row["num_edges"],
        "property": property_name,
        "property_locality": "local" if property_name in LOCAL_PROPERTIES else "global",
        "ground_truth": row["properties"][property_name],
        "prompt": prompt,
        "raw_model_output": raw_output,
        "parsed_answer": parsed,
        "parse_success": ok,
        "finish_reason": finish_reason,
        "failure_type": classify_failure(ok, finish_reason, usage.get("completion_tokens"), max_tokens),
        "model": model,
        "provider": provider,
        "temperature": temperature,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    add_metrics(rec, parsed, ok)
    return rec


# ---------------------------------------------------------------------------
# API client and call
# ---------------------------------------------------------------------------

def make_client(base_url: str, api_key: str, provider: str) -> AsyncOpenAI:
    default_headers = {}
    if provider == "openrouter":
        default_headers["X-Title"] = os.getenv("OPENROUTER_APP_NAME", "pilot-graph")
        if os.getenv("OPENROUTER_SITE_URL"):
            default_headers["HTTP-Referer"] = os.environ["OPENROUTER_SITE_URL"]
    return AsyncOpenAI(base_url=base_url, api_key=api_key, default_headers=default_headers)


def make_call_model(log: logging.Logger):
    @retry(
        retry=retry_if_exception_type(RETRYABLE),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        stop=stop_after_attempt(4),                 # 1 initial + 3 retries
        reraise=True,
        before_sleep=before_sleep_log(log, logging.WARNING),
    )
    async def call_model(client, model, prompt, temperature, max_tokens,
                         extra_body=None, reasoning_effort=None, response_format=None):
        start = time.perf_counter()
        kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "extra_body": extra_body or {},
        }
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        if response_format is not None:
            kwargs["response_format"] = response_format
        try:
            resp = await client.chat.completions.create(**kwargs)
        except NotFoundError:
            # Some OpenRouter-routed models have provider endpoints that don't
            # declare response_format support at all (the request gets
            # filtered out of the routing pool entirely, not rejected with a
            # useful error) -- combined with response_format this produces a
            # routing-level 404 ("No endpoints found for <model>"), which
            # looks like the model itself is unavailable but isn't: the same
            # model/provider works fine without response_format. Retry once
            # without it; parse_answer()'s regex fallback still extracts the
            # JSON object from a plain-text completion, so this doesn't
            # relax what counts as a successfully parsed answer.
            if response_format is None:
                raise
            log.warning("NotFoundError with response_format set for %s -- "
                        "retrying once without it", model)
            kwargs.pop("response_format")
            resp = await client.chat.completions.create(**kwargs)
        latency = time.perf_counter() - start
        content = resp.choices[0].message.content or ""
        finish_reason = resp.choices[0].finish_reason
        usage = {}
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
            }
        return content, usage, latency, finish_reason
    return call_model


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser(config: ModelConfig, here: Path) -> argparse.ArgumentParser:
    default_dataset = default_dataset_path(here)
    default_jsonl = here / f"{config.key}_results.jsonl"
    default_json = here / f"{config.key}_results.json"
    default_log_dir = here / "logs"
    default_subset = config.default_subset
    if default_subset not in (None, "none") and not Path(default_subset).is_absolute():
        default_subset = here / default_subset

    p = argparse.ArgumentParser(description=f"Full {config.label} query run over the 300-graph dataset.")
    p.add_argument("--dataset", type=Path, default=default_dataset)
    p.add_argument("--subset", default=default_subset,
                   help="Subset file of object_ids, or 'none' for the full 300.")
    p.add_argument("--jsonl-output", type=Path, default=default_jsonl)
    p.add_argument("--json-output", type=Path, default=default_json)
    p.add_argument("--log-dir", type=Path, default=default_log_dir)
    p.add_argument("--provider", choices=list(PROVIDERS), default=config.default_provider)
    p.add_argument("--model", default=config.default_model, help=config.model_help)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=config.default_max_tokens)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--thinking", choices=["disabled", "enabled"], default="disabled")
    if config.supports_json_mode:
        p.add_argument("--json-mode", choices=["on", "off"], default=config.default_json_mode,
                       help="Force JSON-only output via response_format. Default on.")
    p.add_argument("--properties", nargs="+", choices=PROPERTIES, default=list(PROPERTIES),
                   help="Subset of properties to query (default: all 8).")
    p.add_argument("--fail-fast", action="store_true",
                   help="Stop at the FIRST failure (API error OR parse failure), print "
                        "full details, exit. Sequential so 'first' is exact. Re-run resumes.")
    p.add_argument("--price-in", type=float, default=config.default_price_in)
    p.add_argument("--price-out", type=float, default=config.default_price_out)
    p.add_argument("--limit", type=int, default=None,
                   help="Only the first N graphs (smoke test). Resume still applies.")
    p.add_argument("--force", action="store_true", help="Re-run every pair, even ones already logged.")
    p.add_argument("--retry-parse-failures", action="store_true",
                   help="Also re-query pairs logged with parse_success=false.")
    p.add_argument("--dry-run", action="store_true", help="Build prompts/task list without calling the API.")
    return p


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def write_json_export(jsonl_path: Path, json_path: Path) -> None:
    """Export JSONL as a JSON array, keeping the LATEST record per (object_id, property)."""
    if not jsonl_path.exists():
        return
    latest = read_latest_records(jsonl_path)
    records = sorted(latest.values(), key=lambda r: (r["object_id"], r["property"]))
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(records, indent=2, ensure_ascii=True) + "\n")


def print_summary(totals, args, config: ModelConfig) -> None:
    n = totals["n"]
    if n == 0:
        print("\nNo new queries ran (all already logged). Use --force to re-run.")
        return
    pin, pout = totals["prompt_tokens"], totals["completion_tokens"]
    cost = pin / 1e6 * args.price_in + pout / 1e6 * args.price_out
    print("\n" + "=" * 56)
    print(f"{config.label} FULL RUN SUMMARY (real usage from API)")
    print("=" * 56)
    print(f"queries run            : {n}")
    print(f"parse success          : {totals['parsed_ok']}/{n}  ({100*totals['parsed_ok']/n:.0f}%)")
    print(f"input tokens (total)   : {pin:,}  (avg {pin/n:.0f}/query)")
    print(f"output tokens (total)  : {pout:,}  (avg {pout/n:.0f}/query)")
    print(f"price                  : ${args.price_in}/1M in, ${args.price_out}/1M out")
    print(f"REAL cost              : ${cost:.4f}")
    print("=" * 56)
    print(f"jsonl: {args.jsonl_output}")
    print(f"json : {args.json_output}")


def stop_on_failure(row, prop, kind, detail, raw, log_path, totals, log: logging.Logger) -> None:
    log.error("STOP at first failure | %s | %s / %s | %s", kind, row["object_id"], prop, detail)
    print("\n" + "X" * 60)
    print(f"STOPPED at first failure  ({kind})")
    print("X" * 60)
    print(f"  object_id : {row['object_id']}")
    print(f"  tier      : {row['tier']}   family: {row['family']}   "
          f"nodes: {row['num_nodes']}   edges: {row['num_edges']}")
    print(f"  property  : {prop}")
    print(f"  ground_truth: {row['properties'][prop]}")
    print(f"  detail    : {detail}")
    if raw is not None:
        print(f"  raw_model_output: {raw!r}")
    print(f"  edge_list : {row['edge_list'][:200]}{'...' if len(row['edge_list']) > 200 else ''}")
    print("-" * 60)
    print(f"  queries OK before stop: {totals['n']}  "
          f"(in={totals['prompt_tokens']} out={totals['completion_tokens']} tokens)")
    print(f"  log file  : {log_path}")
    print("X" * 60)
    print("Fix the issue, then re-run the SAME command:")
    print("  successful queries skip, the rest resume from here.")
    print("  (parse failures: add --retry-parse-failures to re-query them too.)")


def report_failures(failures, log: logging.Logger) -> None:
    if not failures:
        print("\nAll queries completed. No failures.")
        return
    log.error("%d queries failed (not logged): %s", len(failures), [f"{o}/{p}" for o, p, _ in failures])
    print("\n" + "!" * 56)
    print(f"{len(failures)} queries FAILED (not logged) - re-run to retry ONLY these:")
    for oid, prop, err in failures[:30]:
        print(f"  {oid} / {prop}  ({err})")
    if len(failures) > 30:
        print(f"  ... and {len(failures) - 30} more")
    print("!" * 56)
    print("Re-run the same command - logged queries skip, failed ones retry.")


def resolve_thinking(config: ModelConfig, args) -> tuple[dict | None, str | None, str]:
    """Returns (extra_body, reasoning_effort, prompt_suffix) for the configured
    thinking-disable mechanism. See phase2_model_results/README.md §4 "Two
    mechanisms, easy to confuse" for why this differs by provider."""
    if config.thinking_mode == "deepseek":
        return {"thinking": {"type": args.thinking}}, None, ""
    if config.thinking_mode == "openrouter_qwen":
        extra_body = {"reasoning": {"enabled": args.thinking == "enabled"}}
        suffix = " /no_think" if args.thinking == "disabled" else ""
        return extra_body, None, suffix
    if config.thinking_mode == "google":
        reasoning_effort = "none" if args.thinking == "disabled" else None
        return None, reasoning_effort, ""
    if config.thinking_mode == "none":
        return None, None, ""
    raise ValueError(f"unknown thinking_mode: {config.thinking_mode}")


async def run(args, config: ModelConfig, log_dir_key: str) -> None:
    load_dotenv()
    logger_name = f"graph_{config.key}"
    log_path, log = setup_logging(args.log_dir, config.key, logger_name)
    call_model = make_call_model(log)

    log.info("=" * 56)
    log.info("%s FULL RUN start", config.label)
    log.info("provider=%s model=%s temp=%s max_tokens=%s concurrency=%s",
             args.provider, args.model, args.temperature, args.max_tokens, args.concurrency)
    log.info("dataset=%s", args.dataset)
    log.info("log file=%s", log_path)

    dataset = load_dataset(args.dataset, log)
    dataset = apply_subset(dataset, args.subset)
    log.info("subset=%s -> %d graphs", args.subset, len(dataset))
    completed = set() if args.force else done_pairs(args.jsonl_output, args.retry_parse_failures)
    tasks = build_tasks(dataset, args.properties, completed, args.limit, args.force)

    log.info("graphs=%d props=%d | planned new queries=%d | skipping=%d",
             len(dataset), len(args.properties), len(tasks), len(completed))
    print(f"Provider: {args.provider}  |  Model: {args.model}  |  thinking: {args.thinking}")
    print(f"Graphs: {len(dataset)} x {len(args.properties)} props "
          f"(uncertified chromatic_number pairs excluded)")
    print(f"Planned new queries: {len(tasks)}"
          + (f"  (skipping {len(completed)} already done)" if completed else ""))
    print(f"Log file: {log_path}")

    if args.dry_run:
        for row, prop in tasks[:3]:
            print(f"\n--- {row['object_id']} / {prop} ---")
            print(build_prompt(row["edge_list"], prop))
        log.info("[dry-run] %d queries would be sent. No API calls.", len(tasks))
        print(f"\n[dry-run] {len(tasks)} queries would be sent. No API calls.")
        return

    base_url, key_env = PROVIDERS[args.provider]
    api_key = os.getenv(key_env)
    if not api_key:
        raise RuntimeError(f"{key_env} not set. Add it to .env.")

    args.jsonl_output.parent.mkdir(parents=True, exist_ok=True)
    client = make_client(base_url, api_key, args.provider)
    sem = asyncio.Semaphore(max(1, args.concurrency))
    write_lock = asyncio.Lock()

    extra_body, reasoning_effort, think_suffix = resolve_thinking(config, args)
    prompt_suffix = think_suffix + config.extra_prompt_suffix
    response_format = None
    if config.supports_json_mode and getattr(args, "json_mode", "off") == "on":
        response_format = {"type": "json_object"}
    log.info("thinking=%s extra_body=%s reasoning_effort=%s prompt_suffix=%r json_mode=%s fail_fast=%s",
             args.thinking, extra_body, reasoning_effort, prompt_suffix,
             getattr(args, "json_mode", None), args.fail_fast)

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
        prompt = build_prompt(row["edge_list"], prop) + prompt_suffix
        raw, usage, latency, finish_reason = await call_model(
            client, args.model, prompt, args.temperature, args.max_tokens,
            extra_body, reasoning_effort, response_format
        )
        truncated = is_truncated(finish_reason, usage.get("completion_tokens"), args.max_tokens)
        parsed, ok = parse_answer(raw, prop, truncated=truncated)
        rec = make_record(row, prop, raw, parsed, ok, args.model, args.provider,
                          args.temperature, prompt, usage, finish_reason, args.max_tokens)
        log.debug("OK  %-28s %-18s parse=%s in=%s out=%s %.2fs ans=%s",
                  row["object_id"], prop, ok,
                  usage.get("prompt_tokens"), usage.get("completion_tokens"),
                  latency, repr(parsed)[:60])
        return rec, ok, raw, usage

    # ---- FAIL-FAST: sequential; stop at first error OR parse failure ----
    if args.fail_fast:
        log.info("FAIL-FAST mode: sequential, stop at first failure.")
        progress = tqdm(total=len(tasks), unit="query")
        for row, prop in tasks:
            try:
                rec, ok, raw, usage = await run_one(row, prop)
            except Exception as exc:
                progress.close()
                stop_on_failure(row, prop, "API ERROR", f"{type(exc).__name__}: {exc}", None, log_path, totals, log)
                return
            if not ok:
                write_record(rec, usage, ok)
                write_json_export(args.jsonl_output, args.json_output)
                progress.close()
                stop_on_failure(row, prop, "PARSE FAILURE", f"could not parse {prop}", raw, log_path, totals, log)
                return
            write_record(rec, usage, ok)
            progress.update(1)
        progress.close()
        write_json_export(args.jsonl_output, args.json_output)
        print_summary(totals, args, config)
        print("\nAll queries completed with NO failures. (fail-fast)")
        log.info("FAIL-FAST end | completed=%d tokens in=%d out=%d",
                 totals["n"], totals["prompt_tokens"], totals["completion_tokens"])
        return

    # ---- NORMAL: concurrent; one failure does not abort the batch ----
    progress = tqdm(total=len(tasks), unit="query")

    async def tracked(row, prop):
        try:
            async with sem:
                rec, ok, raw, usage = await run_one(row, prop)
            async with write_lock:
                write_record(rec, usage, ok)
            if not ok:
                log.warning("PARSE-FAIL %-28s %-18s raw=%s", row["object_id"], prop, repr(raw)[:120])
        except Exception as exc:  # noqa: BLE001
            async with write_lock:
                failures.append((row["object_id"], prop, type(exc).__name__))
            log.error("FAIL %-28s %-18s %s: %s", row["object_id"], prop, type(exc).__name__, exc)
        finally:
            progress.update(1)

    await asyncio.gather(*(tracked(row, prop) for row, prop in tasks))
    progress.close()

    write_json_export(args.jsonl_output, args.json_output)
    print_summary(totals, args, config)
    report_failures(failures, log)
    log.info("RUN end | completed=%d failed=%d tokens in=%d out=%d",
             totals["n"], len(failures), totals["prompt_tokens"], totals["completion_tokens"])
    log.info("full log saved -> %s", log_path)


def main(config: ModelConfig, here: Path) -> None:
    parser = build_arg_parser(config, here)
    args = parser.parse_args()
    try:
        asyncio.run(run(args, config, config.key))
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
