"""
Phase 2 -- shared query harness for the tabular (Domain 3) benchmark.

One harness, six model configurations -- not six duplicated scripts. This
is the fix for the documented lesson from the geometry domain's Phase 2
(six ~700-line near-identical runners, drifted schema, duplicated bugs):
here every model's runner is a ~30-line `config.py` plus a 5-line `run.py`
that calls into this module. Fix a bug once, it is fixed for all six.

Pipeline, one query per (table, query_id) pair from the Phase 1 dataset's
`queries` list:

    load .env -> load + validate dataset -> (optional) apply subset
        -> read JSONL, compute already-done (object_id, query_id) pairs
        -> build task list -> async fan-out, semaphore-capped
        -> call model, retry on transient errors -> parse answer
        -> score against ground truth -> append JSONL
        -> export deduplicated JSON -> print cost summary

Every provider is reached through the OpenAI-compatible AsyncOpenAI client;
only base_url, the API key, and a small thinking-disable mechanism differ
per provider (see ModelConfig). This mirrors the geometry Phase 2 harness's
single design choice that let one client serve five vendors.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

HERE = Path(__file__).resolve().parent

PROVIDERS = {
    "deepseek": "https://api.deepseek.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openai": "https://api.openai.com/v1",
}

RETRYABLE = (APITimeoutError, RateLimitError, APIConnectionError, InternalServerError)

# ----------------------------------------------------------------------
# Prompt template and question text -- PDF Section 5.5 / Table 12.
# Byte-identical across all six models; only the (optional) /no_think
# suffix for Qwen3 differs, applied in build_prompt via cfg.prompt_suffix.
# ----------------------------------------------------------------------

TEMPLATE = """You are given a data table in CSV format.

Table:
{csv}

Question:
{question}

Respond with only a JSON object in this format:
{{"answer": <your answer>}}"""

QUESTIONS: dict[str, str] = {
    "row_count": "How many rows does this table have? Do not count the header row.",
    "column_dtype": 'What is the data type of column {col}? Answer "numeric", "categorical", or "datetime".',
    "null_count": "How many null or missing values are in this table in total?",
    "correlation": "What is the Pearson correlation coefficient between {col_a} and {col_b}?",
    "is_monotonic": "Is column {col} monotonically increasing or decreasing? Answer true or false.",
    "has_outlier": "Does column {col} contain any outliers, i.e. any value below Q1 - 3*IQR or above Q3 + 3*IQR (where IQR = Q3 - Q1 is the interquartile range)? Answer true or false.",
    "skewness": "What is the skewness of column {col}?",
    "func_dependency": "Is column {col_b} a deterministic function of column {col_a}? Answer true or false.",
}

NUMERIC_PROPERTIES = {"correlation", "skewness", "column_agg"}
INTEGER_PROPERTIES = {"row_count", "null_count", "column_count"}
BOOLEAN_PROPERTIES = {"is_monotonic", "has_outlier", "func_dependency"}
CATEGORICAL_PROPERTIES = {"column_dtype"}

TOLERANCES = {"strict": 0.01, "moderate": 0.05, "lenient": 0.10}


def build_question(query: dict) -> str:
    # Tiered queries (add_tiered_queries.py) carry their own fully-formed question text.
    if query.get("question"):
        return query["question"]
    cols = query["columns"]
    template = QUESTIONS[query["property"]]
    if query["property"] == "column_dtype":
        return template.format(col=cols[0])
    if query["property"] in ("correlation", "func_dependency"):
        return template.format(col_a=cols[0], col_b=cols[1])
    if cols:
        return template.format(col=cols[0])
    return template


def build_prompt(csv_string: str, query: dict, prompt_suffix: str = "") -> str:
    question = build_question(query)
    prompt = TEMPLATE.format(csv=csv_string, question=question)
    return prompt + prompt_suffix


# ----------------------------------------------------------------------
# Model configuration -- the only thing that differs between the six runs.
# ----------------------------------------------------------------------


@dataclass
class ModelConfig:
    name: str  # short id, used in filenames/logs, e.g. "v4flash"
    model: str  # provider-side model id
    provider: str  # key into PROVIDERS
    api_key_env: str
    price_in: float  # $ / 1M input tokens
    price_out: float  # $ / 1M output tokens
    temperature: float = 0.0
    max_tokens: int = 512
    concurrency: int = 5
    json_mode: bool = False
    thinking_disable: Optional[Callable[[dict], None]] = None  # mutates extra_body in place
    prompt_suffix: str = ""  # e.g. " /no_think" for Qwen3
    default_subset: Optional[str] = None  # path (relative to this config's dir) to a subset file


def disable_thinking_deepseek(extra_body: dict) -> None:
    extra_body["thinking"] = {"type": "disabled"}


def disable_thinking_openrouter(extra_body: dict) -> None:
    extra_body["reasoning"] = {"enabled": False}


def disable_thinking_gemini(extra_body: dict) -> None:
    extra_body["reasoning_effort"] = "none"


# ----------------------------------------------------------------------
# Dataset loading / validation
# ----------------------------------------------------------------------

REQUIRED_RECORD_FIELDS = ("object_id", "tier", "source", "n_rows", "n_cols", "csv", "columns", "queries")


def load_dataset(path: Path) -> list[dict]:
    records = json.loads(path.read_text())
    if len(records) != 300:
        logging.warning("Expected 300 table records, found %d", len(records))
    seen_ids = set()
    for r in records:
        for f in REQUIRED_RECORD_FIELDS:
            if f not in r:
                raise ValueError(f"Record missing required field {f!r}: {r.get('object_id')}")
        if r["object_id"] in seen_ids:
            raise ValueError(f"Duplicate object_id: {r['object_id']}")
        seen_ids.add(r["object_id"])
        for q in r["queries"]:
            if "query_id" not in q or "property" not in q or "ground_truth" not in q:
                raise ValueError(f"Malformed query in {r['object_id']}: {q}")
    return records


def apply_subset(records: list[dict], subset_path: Optional[Path]) -> list[dict]:
    if subset_path is None:
        return records
    data = json.loads(subset_path.read_text())
    ids = set(data["object_ids"])
    by_id = {r["object_id"]: r for r in records}
    missing = ids - by_id.keys()
    if missing:
        raise ValueError(f"Subset references {len(missing)} object_ids not in dataset: {sorted(missing)[:5]}...")
    return [by_id[i] for i in ids if i in by_id]


# ----------------------------------------------------------------------
# Task construction and resume
# ----------------------------------------------------------------------


def done_pairs(jsonl_path: Path, retry_parse_failures: bool) -> set[tuple[str, str]]:
    if not jsonl_path.exists():
        return set()
    done = set()
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            key = (rec["object_id"], rec["query_id"])
            if retry_parse_failures and not rec.get("parse_success", False):
                continue
            done.add(key)
    return done


def build_tasks(
    records: list[dict],
    properties_filter: Optional[set[str]],
    already_done: set[tuple[str, str]],
    limit: Optional[int],
    prompt_suffix: str,
) -> list[dict]:
    tasks = []
    selected_records = records[:limit] if limit is not None else records
    for r in selected_records:
        for q in r["queries"]:
            if properties_filter and q["property"] not in properties_filter:
                continue
            key = (r["object_id"], q["query_id"])
            if key in already_done:
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
                    "prompt": build_prompt(r["csv"], q, prompt_suffix),
                }
            )
    return tasks


# ----------------------------------------------------------------------
# Answer parsing -- PDF 6.1, adapted to Table 12's answer types.
# ----------------------------------------------------------------------

JSON_BLOCK_RE = re.compile(r"\{.*?\}", re.DOTALL)
FIRST_NUMBER_RE = re.compile(r"-?\d+\.?\d*")


def _extract_json_answer(text: str) -> tuple[Any, bool]:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "answer" in obj:
            return obj["answer"], True
    except (json.JSONDecodeError, TypeError):
        pass
    for match in JSON_BLOCK_RE.findall(text):
        try:
            obj = json.loads(match)
            if isinstance(obj, dict) and "answer" in obj:
                return obj["answer"], True
        except json.JSONDecodeError:
            continue
    return None, False


def normalize_answer(raw_value: Any, raw_text: str, prop: str) -> tuple[Any, bool]:
    """Returns (normalized_value, success)."""
    if prop in NUMERIC_PROPERTIES:
        if isinstance(raw_value, bool):
            return None, False
        if isinstance(raw_value, (int, float)):
            return float(raw_value), True
        m = FIRST_NUMBER_RE.search(str(raw_value) if raw_value is not None else raw_text)
        if m:
            try:
                return float(m.group().replace(",", "")), True
            except ValueError:
                return None, False
        return None, False

    if prop in INTEGER_PROPERTIES:
        if isinstance(raw_value, bool):
            return None, False
        if isinstance(raw_value, (int, float)) and float(raw_value).is_integer():
            return int(raw_value), True
        m = FIRST_NUMBER_RE.search(str(raw_value) if raw_value is not None else raw_text)
        if m:
            try:
                v = float(m.group())
                if v.is_integer():
                    return int(v), True
            except ValueError:
                pass
        return None, False

    if prop in BOOLEAN_PROPERTIES:
        if isinstance(raw_value, bool):
            return raw_value, True
        s = str(raw_value if raw_value is not None else raw_text).strip().lower()
        if s in ("true", "yes"):
            return True, True
        if s in ("false", "no"):
            return False, True
        m = re.search(r"\b(true|yes|false|no)\b", s)
        if m:
            return m.group(1) in ("true", "yes"), True
        return None, False

    if prop in CATEGORICAL_PROPERTIES:
        s = str(raw_value if raw_value is not None else raw_text).strip().lower()
        for candidate in ("numeric", "categorical", "datetime"):
            if candidate in s:
                return candidate, True
        return None, False

    return None, False


def parse_answer(raw_text: str, prop: str) -> tuple[Any, bool]:
    raw_value, found_json = _extract_json_answer(raw_text)
    if found_json:
        norm, ok = normalize_answer(raw_value, raw_text, prop)
        if ok:
            return norm, True
    norm, ok = normalize_answer(None, raw_text, prop)
    return norm, ok


def classify_failure(finish_reason: Optional[str], completion_tokens: int, max_tokens: int, parse_success: bool) -> Optional[str]:
    if parse_success:
        return None
    if finish_reason == "length" or completion_tokens >= max_tokens:
        return "reasoning_truncated"
    return "parse_failure"


# ----------------------------------------------------------------------
# Scoring -- PDF 6.2
# ----------------------------------------------------------------------


def score(prop: str, ground_truth: Any, parsed: Any, parse_success: bool) -> dict:
    if not parse_success or parsed is None:
        base = {"correct": False}
        if prop in NUMERIC_PROPERTIES | INTEGER_PROPERTIES:
            base.update(
                absolute_error=None,
                relative_error=None,
                correct_1pct=False,
                correct_5pct=False,
                correct_10pct=False,
            )
        return base

    if prop in NUMERIC_PROPERTIES:
        abs_err = abs(parsed - ground_truth)
        rel_err = abs_err if abs(ground_truth) < 0.01 else abs_err / abs(ground_truth)
        return {
            "absolute_error": abs_err,
            "relative_error": rel_err,
            "correct_1pct": rel_err <= TOLERANCES["strict"],
            "correct_5pct": rel_err <= TOLERANCES["moderate"],
            "correct_10pct": rel_err <= TOLERANCES["lenient"],
            "correct": rel_err <= TOLERANCES["moderate"],
        }

    if prop in INTEGER_PROPERTIES:
        abs_err = abs(parsed - ground_truth)
        exact = parsed == ground_truth
        rel_err = abs_err if abs(ground_truth) < 0.01 else abs_err / abs(ground_truth)
        return {
            "absolute_error": abs_err,
            "relative_error": rel_err,
            "correct_1pct": exact,
            "correct_5pct": exact,
            "correct_10pct": exact,
            "correct": exact,
        }

    # boolean / categorical: exact match only
    return {"correct": parsed == ground_truth}


# ----------------------------------------------------------------------
# API call
# ----------------------------------------------------------------------


def make_client(cfg: ModelConfig) -> AsyncOpenAI:
    api_key = os.environ.get(cfg.api_key_env)
    if not api_key:
        raise RuntimeError(f"{cfg.api_key_env} not set")
    base_url = PROVIDERS[cfg.provider]
    return AsyncOpenAI(api_key=api_key, base_url=base_url)


@retry(
    retry=retry_if_exception_type(RETRYABLE),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
async def call_model(client: AsyncOpenAI, cfg: ModelConfig, prompt: str) -> dict:
    extra_body: dict = {}
    if cfg.thinking_disable:
        cfg.thinking_disable(extra_body)

    kwargs: dict[str, Any] = dict(
        model=cfg.model,
        messages=[{"role": "user", "content": prompt}],
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
    )
    if extra_body:
        kwargs["extra_body"] = extra_body
    if cfg.json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if cfg.provider == "openrouter":
        kwargs["extra_headers"] = {"X-Title": "serialization-tabular-research", "HTTP-Referer": "https://localhost"}

    t0 = time.monotonic()
    resp = await client.chat.completions.create(**kwargs)
    latency = time.monotonic() - t0

    choice = resp.choices[0]
    usage = resp.usage
    return {
        "raw_text": choice.message.content or "",
        "finish_reason": choice.finish_reason,
        "prompt_tokens": usage.prompt_tokens if usage else 0,
        "completion_tokens": usage.completion_tokens if usage else 0,
        "latency": latency,
    }


# ----------------------------------------------------------------------
# JSONL append / JSON export
# ----------------------------------------------------------------------


async def append_jsonl(path: Path, record: dict, lock: asyncio.Lock) -> None:
    async with lock:
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")


def write_json_export(jsonl_path: Path, json_path: Path) -> int:
    if not jsonl_path.exists():
        json_path.write_text("[]")
        return 0
    latest: dict[tuple[str, str], dict] = {}
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            latest[(rec["object_id"], rec["query_id"])] = rec
    records = list(latest.values())
    json_path.write_text(json.dumps(records, indent=2))
    return len(records)


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------


def setup_logging(log_dir: Path, name: str) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"{name}_{ts}.log"

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fh = logging.FileHandler(log_path)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(sh)

    return logger


async def run_one(client, cfg: ModelConfig, task: dict, semaphore, jsonl_path, lock, logger) -> Optional[dict]:
    async with semaphore:
        try:
            result = await call_model(client, cfg, task["prompt"])
        except Exception as e:  # noqa: BLE001 -- surfaced via failures list, not swallowed
            logger.error("API error on %s/%s: %s", task["object_id"], task["query_id"], e)
            return {"task": task, "error": str(e)}

        parsed, parse_success = parse_answer(result["raw_text"], task["property"])
        failure_type = classify_failure(
            result["finish_reason"], result["completion_tokens"], cfg.max_tokens, parse_success
        )
        metrics = score(task["property"], task["ground_truth"], parsed, parse_success)

        record = {
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
            "prompt": task["prompt"],
            "raw_model_output": result["raw_text"],
            "parsed_answer": parsed,
            "parse_success": parse_success,
            "failure_type": failure_type,
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
        await append_jsonl(jsonl_path, record, lock)
        logger.debug(
            "%s/%s parse=%s correct=%s tokens_in=%d tokens_out=%d",
            task["object_id"], task["query_id"], parse_success, metrics.get("correct"),
            result["prompt_tokens"], result["completion_tokens"],
        )
        return record


async def run_async(cfg: ModelConfig, args: argparse.Namespace, tasks: list[dict], jsonl_path: Path, logger: logging.Logger) -> tuple[list[dict], list[dict]]:
    client = make_client(cfg)
    semaphore = asyncio.Semaphore(args.concurrency or cfg.concurrency)
    lock = asyncio.Lock()

    if args.fail_fast:
        results = []
        for task in tasks:
            record = await run_one(client, cfg, task, semaphore, jsonl_path, lock, logger)
            if record is None:
                continue
            if "error" in record:
                print("\n=== FAIL-FAST: API error ===")
                print(json.dumps({k: v for k, v in task.items() if k != "prompt"}, indent=2, default=str))
                print("error:", record["error"])
                sys.exit(1)
            if not record["parse_success"]:
                print("\n=== FAIL-FAST: parse failure ===")
                print(json.dumps({k: v for k, v in record.items() if k not in ("prompt",)}, indent=2, default=str))
                sys.exit(1)
            results.append(record)
        return results, []

    coros = [run_one(client, cfg, task, semaphore, jsonl_path, lock, logger) for task in tasks]
    raw_results = await asyncio.gather(*coros)
    successes = [r for r in raw_results if r and "error" not in r]
    failures = [r["task"] for r in raw_results if r and "error" in r]
    return successes, failures


def parse_args(cfg: ModelConfig) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=f"Phase 2 query runner for {cfg.name}")
    p.add_argument("--dataset", type=Path, required=True, help="Path to tabular_exp1_dataset.json")
    p.add_argument("--subset", type=str, default=cfg.default_subset or "none")
    p.add_argument("--jsonl-output", type=Path, required=True)
    p.add_argument("--json-output", type=Path, required=True)
    p.add_argument("--log-dir", type=Path, default=None)
    p.add_argument("--properties", type=str, default=None, help="Comma-separated property names to restrict to")
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument("--limit", type=int, default=None, help="First N table objects only (smoke test)")
    p.add_argument("--force", action="store_true", help="Re-run even already-logged pairs")
    p.add_argument("--retry-parse-failures", action="store_true")
    p.add_argument("--fail-fast", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--price-in", type=float, default=cfg.price_in)
    p.add_argument("--price-out", type=float, default=cfg.price_out)
    return p.parse_args()


def main(cfg: ModelConfig) -> None:
    load_dotenv()
    args = parse_args(cfg)
    log_dir = args.log_dir or (Path.cwd() / "logs")
    logger = setup_logging(log_dir, cfg.name)

    records = load_dataset(args.dataset)
    subset_path = None if args.subset in (None, "none") else Path(args.subset)
    records = apply_subset(records, subset_path)
    logger.info("Loaded %d table records (subset=%s)", len(records), subset_path)

    properties_filter = set(args.properties.split(",")) if args.properties else None
    already_done = set() if args.force else done_pairs(args.jsonl_output, args.retry_parse_failures)
    tasks = build_tasks(records, properties_filter, already_done, args.limit, cfg.prompt_suffix)

    logger.info("Planned new queries: %d (skipping %d already done)", len(tasks), len(already_done))
    print(f"Planned new queries: {len(tasks)} (skipping {len(already_done)} already done)")

    if not tasks:
        write_json_export(args.jsonl_output, args.json_output)
        return

    if args.dry_run:
        print(f"[dry-run] {len(tasks)} tasks built, no API calls made. Sample prompts:")
        for t in tasks[:3]:
            print("-" * 60)
            print(f"{t['object_id']} / {t['query_id']} (ground truth: {t['ground_truth']!r})")
            print(t["prompt"])
        return

    args.jsonl_output.parent.mkdir(parents=True, exist_ok=True)
    successes, failures = asyncio.run(run_async(cfg, args, tasks, args.jsonl_output, logger))

    n_exported = write_json_export(args.jsonl_output, args.json_output)
    logger.info("Exported %d deduplicated records to %s", n_exported, args.json_output)

    total_in = sum(r["prompt_tokens"] for r in successes)
    total_out = sum(r["completion_tokens"] for r in successes)
    cost = total_in / 1e6 * args.price_in + total_out / 1e6 * args.price_out
    n_correct = sum(1 for r in successes if r.get("correct"))
    n_parsed = sum(1 for r in successes if r["parse_success"])

    summary = (
        f"\n=== {cfg.name} run complete ===\n"
        f"Queries attempted: {len(successes)} | API failures: {len(failures)}\n"
        f"Parse success: {n_parsed}/{len(successes)} | Correct (moderate tol.): {n_correct}/{len(successes)}\n"
        f"Tokens: {total_in} in / {total_out} out | Cost: ${cost:.4f}\n"
        f"JSONL: {args.jsonl_output} | JSON export: {args.json_output} ({n_exported} records)"
    )
    print(summary)
    logger.info(summary.replace("\n", " | "))

    if failures:
        print(f"\n{len(failures)} queries failed with API errors -- re-run the same command to retry them:")
        for t in failures[:10]:
            print(f"  {t['object_id']} / {t['query_id']}")
