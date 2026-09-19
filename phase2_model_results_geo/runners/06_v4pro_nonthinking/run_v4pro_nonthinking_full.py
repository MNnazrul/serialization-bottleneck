"""
DeepSeek-V4-Pro, NON-THINKING -- Phase 2 query run over the 20% stratified
subsample (60 polygons x 9 properties = 540 queries).

THE SUBSAMPLE IS NOT DRAWN HERE. It is read from
../../01_dataset/subsample_20perc.json, which
../../01_dataset/generate_polygons_stratified_20perc.py produced and validated:
20 polygons per tier, and inside every tier exactly 10 convex / 10 non-convex
and 10 ccw / 10 cw, so the graded baselines for `convex` and `orientation` are
the same 50% they are on the full 300. Drawing it here instead would put the
sampling decision inside the query harness, where nothing re-checks it.

SETTINGS -- and one deliberate departure from the PDF
-----------------------------------------------------
  temperature   0      (PDF Section 3)
  max_tokens    512    matches the five non-thinking models in this run
  thinking      DISABLED
  system prompt none, zero-shot, direct answer

PDF Section 3 specifies V4-Pro as the THINKING-mode arm: provider-default
thinking configuration and max_tokens 2048, "thinking traces can be long".
This runner is the NON-THINKING companion instead, at 512 tokens, matching the
other five models exactly.

That is a departure, and it is the point of the arm rather than an oversight.
Run thinking-mode V4-Pro and any difference against the other five confounds
two changes at once: a bigger model AND a different inference regime. Holding
temperature, token budget, prompt and parser identical to the other five leaves
model size as the only variable, so this arm answers "does a 1.6T model recover
these properties better than a 284B one, under the same conditions?" -- which
the thinking-mode arm cannot answer on its own.

Every record stores the max_tokens and temperature it ran under, so this arm
and a future thinking-mode arm stay separable after the fact.

WHAT TO EXPECT
--------------
540 queries, not 2700. Do not compare this arm's per-tier counts against the
five full runs without noting n=20 per tier here versus n=100 there: a one-
polygon difference is 5 percentage points, not 1.

OUTPUT
  ../../03_results/06_v4pro_nonthinking/v4pro_nonthinking_results.jsonl
  ../../03_results/06_v4pro_nonthinking/v4pro_nonthinking_results.json
  ./logs/v4pro_nonthinking_<stamp>.log

USAGE
  python run_v4pro_nonthinking_full.py --dry-run     # build prompts, call nothing
  python run_v4pro_nonthinking_full.py --limit 3     # smoke test: 3 polygons = 27 queries
  python run_v4pro_nonthinking_full.py               # full arm: 540 queries
"""

from __future__ import annotations

import argparse
import asyncio
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


HERE = Path(__file__).resolve().parent
# Dataset lives in the Phase-1 folder; this script only writes inside its own folder.
# The dataset lives at the repo root. v1 pointed at a sibling folder that does
# not exist in this repo, so its default never resolved.
REPO_ROOT = HERE.parent.parent
# Re-pathed for the exp1_geo layout so this folder runs on its own:
#   ../../01_dataset/   the full dataset AND the 20% subsample id list
#   ../../03_results/   where this runner appends its records
# Paths for the split layout. This runner lives at
#   phase2_model_results_geo/runners/<model>/
# so the repository root is three levels up.
REPO_ROOT = HERE.parent.parent.parent
DATASET_DIR = REPO_ROOT / "phase1_dataset_geo"
RESULTS_ROOT = HERE.parent.parent / "results"
DEFAULT_DATASET = DATASET_DIR / "geometry_exp1_dataset.json"
DEFAULT_SUBSET = DATASET_DIR / "subsample_20perc.json"   # built by generate_polygons_stratified_20perc.py
RESULTS_DIR = RESULTS_ROOT / HERE.name
LOG_DIR = HERE / "logs"
DEFAULT_JSONL = RESULTS_DIR / "v4pro_nonthinking_results.jsonl"
DEFAULT_JSON = RESULTS_DIR / "v4pro_nonthinking_results.json"

# Module logger; handlers attached in setup_logging(). tenacity logs retries here.
LOG = logging.getLogger("v4pro_nonthinking")


def setup_logging(log_dir: Path) -> Path:
    """Log to a timestamped file (DEBUG, every query) AND console (INFO).
    Each run keeps its own file. Returns the file path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"v4pro_nonthinking_{stamp}.log"

    LOG.setLevel(logging.DEBUG)
    LOG.handlers.clear()
    LOG.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    LOG.addHandler(fh)

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    LOG.addHandler(ch)
    return log_path


# Provider presets: (base_url, env var for api key)
PROVIDERS = {
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
}

# All 9 Experiment-1 geometry properties.
PROPERTIES = (
    "vertex_count", "bbox", "centroid", "area", "perimeter",
    "convex", "orientation", "aspect_ratio", "edge_length_variance",
)
LOCAL_PROPERTIES = {"vertex_count", "bbox"}
SCALAR_REL_PROPERTIES = {"area", "perimeter", "aspect_ratio", "edge_length_variance"}

TEMPLATE = """You are given a polygon in WKT format.

Polygon:
{wkt}

Question:
{question}

Respond with only a JSON object in this format:
{{"answer": <your answer>}}"""

QUESTIONS = {
    "vertex_count": (
        "How many vertices does this polygon have? "
        "Do not count the repeated closing coordinate."
    ),
    "bbox": (
        "What is the axis-aligned bounding box of this polygon? "
        "Give [min_x, min_y, max_x, max_y]."
    ),
    "centroid": "What is the centroid of this polygon? Give [x, y].",
    "area": "What is the area of this polygon?",
    "perimeter": "What is the perimeter of this polygon?",
    "convex": "Is this polygon convex? Answer true or false.",
    "orientation": (
        "Is the exterior ring of this polygon oriented clockwise or counter-clockwise? "
        'Answer "cw" or "ccw".'
    ),
    "aspect_ratio": (
        "What is the aspect ratio of this polygon's bounding box "
        "(width divided by height)?"
    ),
    "edge_length_variance": "What is the variance of the edge lengths of this polygon?",
}

NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def build_prompt(wkt: str, property_name: str) -> str:
    return TEMPLATE.format(wkt=wkt, question=QUESTIONS[property_name])


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="V4-Pro NON-THINKING query run over the 20% subsample.")
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--subset", type=Path, default=DEFAULT_SUBSET,
                   help="20%% stratified subsample file (same 60 as the thinking run). "
                        "Only its object_ids are queried. Pass 'none' for full 300.")
    p.add_argument("--json-mode", choices=["on", "off"], default="on",
                   help="Force JSON-only output via response_format="
                        "{'type':'json_object'}. Default on. v1 had no such flag, "
                        "and thinking=disabled does not stop V4-Pro writing "
                        "content-level chain-of-thought: on the smoke test it wrote "
                        "out the full shoelace derivation for centroid, perimeter "
                        "and edge_length_variance, hit the 512-token cap on 11 of "
                        "45 queries and lost 4 answers outright, while the median "
                        "query used 9 tokens. Every other runner in this domain "
                        "sets it; matching them is what makes the runs comparable.")
    p.add_argument("--object-ids", nargs="+", default=None,
                   help="Restrict the run to these object_ids (must already be in "
                        "the subsample). Combine with --force and --max-tokens to "
                        "re-probe one stuck (polygon, property).")
    p.add_argument("--jsonl-output", type=Path, default=DEFAULT_JSONL)
    p.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    p.add_argument("--log-dir", type=Path, default=LOG_DIR)
    p.add_argument("--provider", choices=list(PROVIDERS), default="deepseek")
    p.add_argument("--model", default="deepseek-v4-pro",
                   help="DeepSeek-direct: deepseek-v4-pro. OpenRouter: deepseek/deepseek-v4-pro.")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=512,
                   help="PDF §2 non-thinking cap = 512. Non-thinking answers are a single "
                        "JSON line (peers used avg 9-17, max <=500 output tokens), so 512 "
                        "is ample. This is the whole point of the run: no chain-of-thought, "
                        "so no large token budget is needed (contrast the thinking run's 128k).")
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--thinking", choices=["disabled", "enabled"], default="disabled",
                   help="This run = NON-THINKING -> disabled (default). "
                        "The thinking companion run lives in ../phase_2_domain1_v4pro.")
    p.add_argument("--properties", nargs="+", choices=PROPERTIES, default=list(PROPERTIES),
                   help="Subset of properties to query (default: all 9).")
    p.add_argument("--fail-fast", action="store_true",
                   help="Stop at the FIRST failure (API error OR parse failure), print "
                        "full details, exit. Sequential so 'first' is exact. Re-run resumes.")
    # Real DeepSeek-direct V4-Pro prices ($ per 1M tokens).
    p.add_argument("--price-in", type=float, default=0.435)
    p.add_argument("--price-out", type=float, default=0.87)
    p.add_argument("--limit", type=int, default=None,
                   help="Only the first N polygons (smoke test). Resume still applies.")
    p.add_argument("--force", action="store_true",
                   help="Re-run every pair, even ones already logged.")
    p.add_argument("--retry-parse-failures", action="store_true",
                   help="Also re-query pairs logged with parse_success=false.")
    p.add_argument("--dry-run", action="store_true",
                   help="Build prompts/task list without calling the API.")
    return p.parse_args()


def load_dataset(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"dataset must be a JSON list: {path}")
    if len(data) != 300:
        LOG.warning("expected 300 polygons, found %d in %s", len(data), path)
    seen = set()
    for row in data:
        oid = row.get("object_id")
        if oid in seen:
            raise ValueError(f"duplicate object_id: {oid}")
        seen.add(oid)
        missing = {"object_id", "tier", "shape_type", "num_vertices", "wkt", "properties"} - set(row)
        if missing:
            raise ValueError(f"{oid} missing keys: {sorted(missing)}")
    return data


def apply_subset(dataset: list[dict[str, Any]], subset_path: Path) -> list[dict[str, Any]]:
    """Keep only the polygons whose object_id is in the subset file (20% stratified
    subsample, same 60 as the thinking run). Pass subset_path == None / 'none' for
    the full set."""
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


def normalize_answer(answer: Any, property_name: str) -> tuple[Any, bool]:
    if property_name == "vertex_count":
        if isinstance(answer, bool):
            return None, False
        if isinstance(answer, int):
            return answer, True
        if isinstance(answer, float) and answer.is_integer():
            return int(answer), True
        if isinstance(answer, str):
            m = NUMBER_RE.search(answer)
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

    if property_name in {"bbox", "centroid"}:
        expected_len = 4 if property_name == "bbox" else 2
        if isinstance(answer, (list, tuple)):
            raw = answer
        elif isinstance(answer, str):
            raw = NUMBER_RE.findall(answer.replace(",", " "))
        else:
            return None, False
        values: list[float] = []
        for item in raw:
            if isinstance(item, bool):
                return None, False
            try:
                v = float(item)
            except (TypeError, ValueError):
                return None, False
            if not math.isfinite(v):
                return None, False
            values.append(v)
        if len(values) != expected_len:
            return None, False
        return values, True

    if property_name == "convex":
        if isinstance(answer, bool):
            return answer, True
        if isinstance(answer, str):
            low = answer.strip().lower()
            if re.search(r"\b(true|yes)\b", low):
                return True, True
            if re.search(r"\b(false|no)\b", low):
                return False, True
        return None, False

    if property_name == "orientation":
        if isinstance(answer, str):
            low = answer.strip().lower()
            if re.search(r"\bccw\b|counter-?clockwise", low):
                return "ccw", True
            if re.search(r"\bcw\b|clockwise", low):
                return "cw", True
        return None, False

    raise ValueError(f"unknown property: {property_name}")


def parse_answer(raw: str, property_name: str) -> tuple[Any, bool]:
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
    ans, ok = normalize_answer(stripped, property_name)
    return (ans, True) if ok else (None, False)


def relative_scalar_error(predicted: Any, truth: Any) -> tuple[float, float]:
    abs_err = abs(float(predicted) - float(truth))
    denom = abs(float(truth))
    rel = abs_err if denom == 0 else abs_err / denom
    return abs_err, rel


def normalized_point_error(predicted: Any, truth: Any, diag: float) -> tuple[float, float]:
    if not isinstance(predicted, list) or len(predicted) != len(truth):
        return math.nan, math.nan
    deltas = [float(p) - float(t) for p, t in zip(predicted, truth)]
    abs_err = math.hypot(deltas[0], deltas[1]) if len(deltas) == 2 else max(abs(d) for d in deltas)
    norm = abs_err / diag if diag else abs_err
    return abs_err, norm


def add_metrics(rec: dict[str, Any], row: dict[str, Any], parsed: Any, ok: bool) -> None:
    name = rec["property"]
    truth = rec["ground_truth"]

    if not ok:
        if name in {"convex", "orientation"}:
            rec["correct"] = False
        elif name == "vertex_count":
            rec["absolute_error"] = None
            rec["correct"] = False
            rec["off_by_one"] = False
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

    if name in {"bbox", "centroid"}:
        abs_err, rel = normalized_point_error(parsed, truth, float(row["metadata"]["bbox_diagonal"]))
        rec["absolute_error"] = abs_err
        rec["relative_error"] = rel
        rec["correct_1pct"] = rel <= 0.01
        rec["correct_5pct"] = rel <= 0.05
        rec["correct_10pct"] = rel <= 0.10
        return

    if name == "vertex_count":
        abs_err = abs(int(parsed) - int(truth))
        rec["absolute_error"] = abs_err
        rec["correct"] = abs_err == 0
        rec["off_by_one"] = abs_err == 1
        return

    if name in {"convex", "orientation"}:
        rec["correct"] = parsed == truth
        return

    raise ValueError(f"unknown property: {name}")


def classify_failure(ok, finish_reason, completion_tokens, max_tokens):
    """PDF §6.1: a non-parseable output is `reasoning_truncated` if it hit the
    an output that hit the token limit is `reasoning_truncated` WHETHER OR NOT
    it parsed; an unparseable one that did not hit the limit is `parse_failure`.
    Both are incorrect. Returns None only for an untruncated, parseable answer."""
    # Order matters. v1 tested `ok` first and returned None on any successful
    # parse, so a generation that hit the cap mid-arithmetic but still ended up
    # containing a number was filed as an ordinary wrong answer.
    truncated = finish_reason == "length" or (
        completion_tokens is not None and max_tokens is not None
        and completion_tokens >= max_tokens
    )
    if truncated:
        return "reasoning_truncated"
    return None if ok else "parse_failure"


def make_record(row, property_name, raw_output, parsed, ok, model, provider,
                temperature, prompt, usage, finish_reason, max_tokens) -> dict[str, Any]:
    rec = {
        "object_id": row["object_id"],
        "tier": row["tier"],
        "shape_type": row["shape_type"],
        "num_vertices": row["num_vertices"],
        # Dataset design fields, carried so Phase 3 need not re-join against the
        # dataset file. The other five runners in this folder all carry these;
        # without them eval.py reports "offset_band 0/0/0/0" and disables the
        # centroid shortcut analysis for this arm, because it cannot tell an
        # absent field from a genuine zero.
        #
        # offset_band / centroid_offset_norm  -> the bbox-centre centroid answer
        # vertex_mean_offset_norm             -> the coordinate-mean centroid answer
        # fill_band / fill_ratio              -> the bbox-area answer for `area`
        # family / generator                  -> which construction made the polygon
        "offset_band": row.get("metadata", {}).get("offset_band"),
        "fill_band": row.get("metadata", {}).get("fill_band"),
        "centroid_offset_norm": row.get("metadata", {}).get("centroid_offset_norm"),
        "vertex_mean_offset_norm": row.get("metadata", {}).get("vertex_mean_offset_norm"),
        "fill_ratio": row.get("metadata", {}).get("fill_ratio"),
        "generator": row.get("metadata", {}).get("generator"),
        "family": row.get("metadata", {}).get("family"),
        "target_extent": row.get("metadata", {}).get("target_extent"),
        "target_aspect": row.get("metadata", {}).get("target_aspect"),
        "vertex_target": row.get("metadata", {}).get("vertex_target"),
        "vertex_target_met": row.get("metadata", {}).get("vertex_target_met"),
        "coordinate_type": row.get("metadata", {}).get("coordinate_type"),
        "property": property_name,
        "property_locality": "local" if property_name in LOCAL_PROPERTIES else "global",
        "ground_truth": row["properties"][property_name],
        "prompt": prompt,
        "raw_model_output": raw_output,
        "parsed_answer": parsed,
        "parse_success": ok,
        "finish_reason": finish_reason,
        # Stored per record so a pair re-queried at a larger budget is visible
        # in the data, not only in the shell history.
        "max_tokens": max_tokens,
        "failure_type": classify_failure(ok, finish_reason,
                                         usage.get("completion_tokens"), max_tokens),
        "model": model,
        "provider": provider,                  # PDF §2: record provider for every call
        "thinking": "disabled",                # this run is NON-THINKING
        "temperature": temperature,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    add_metrics(rec, row, parsed, ok)
    return rec


def make_client(base_url: str, api_key: str, provider: str) -> AsyncOpenAI:
    # OpenRouter attribution headers apply ONLY to the openrouter provider.
    # The deepseek run sends nothing OpenRouter-related.
    default_headers = {}
    if provider == "openrouter":
        default_headers["X-Title"] = os.getenv("OPENROUTER_APP_NAME", "pilot-geo")
        if os.getenv("OPENROUTER_SITE_URL"):
            default_headers["HTTP-Referer"] = os.environ["OPENROUTER_SITE_URL"]
    return AsyncOpenAI(base_url=base_url, api_key=api_key, default_headers=default_headers)


# PDF §2: retry on timeout / rate-limit errors, initial delay 2s, multiplier 2,
# 3 retries (2s, 4s, 8s). Retry only transient classes, not 4xx.
RETRYABLE = (APITimeoutError, RateLimitError, APIConnectionError, InternalServerError)


@retry(
    retry=retry_if_exception_type(RETRYABLE),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    stop=stop_after_attempt(4),                 # 1 initial + 3 retries
    reraise=True,
    before_sleep=before_sleep_log(LOG, logging.WARNING),
)
async def call_model(client, model, prompt, temperature, max_tokens,
                     extra_body=None, response_format=None):
    start = time.perf_counter()
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "extra_body": extra_body or {},
    }
    if response_format is not None:
        kwargs["response_format"] = response_format   # suppress content-level CoT
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


def build_tasks(dataset, properties, completed, limit, force):
    rows = dataset[:limit] if limit is not None else dataset
    tasks = []
    for row in rows:
        for prop in properties:
            if not force and (row["object_id"], prop) in completed:
                continue
            tasks.append((row, prop))
    return tasks


async def run(args) -> None:
    load_dotenv()
    log_path = setup_logging(args.log_dir)
    LOG.info("=" * 56)
    LOG.info("V4-PRO NON-THINKING RUN start")
    LOG.info("provider=%s model=%s temp=%s max_tokens=%s concurrency=%s",
             args.provider, args.model, args.temperature, args.max_tokens, args.concurrency)
    LOG.info("dataset=%s", args.dataset)
    LOG.info("log file=%s", log_path)

    dataset = load_dataset(args.dataset)
    dataset = apply_subset(dataset, args.subset)   # the 20% stratified subsample
    if args.object_ids:
        wanted = set(args.object_ids)
        dataset = [r for r in dataset if r["object_id"] in wanted]
        found = {r["object_id"] for r in dataset}
        if wanted - found:
            sys.exit(f"object_id not in the subsample: {sorted(wanted - found)}")
        LOG.info("object-ids filter -> %d polygons: %s", len(dataset), sorted(found))
    LOG.info("subset=%s -> %d polygons", args.subset, len(dataset))
    completed = set() if args.force else done_pairs(args.jsonl_output, args.retry_parse_failures)
    tasks = build_tasks(dataset, args.properties, completed, args.limit, args.force)

    LOG.info("polygons=%d props=%d | planned new queries=%d | skipping=%d",
             len(dataset), len(args.properties), len(tasks), len(completed))
    print(f"Provider: {args.provider}  |  Model: {args.model}  |  thinking: {args.thinking}")
    print(f"Polygons: {len(dataset)} (subset) x {len(PROPERTIES)} props = {len(dataset)*len(PROPERTIES)} total")
    print(f"Planned new queries: {len(tasks)}"
          + (f"  (skipping {len(completed)} already done)" if completed else ""))
    print(f"Log file: {log_path}")

    if args.dry_run:
        for row, prop in tasks[:3]:
            print(f"\n--- {row['object_id']} / {prop} ---")
            print(build_prompt(row["wkt"], prop))
        LOG.info("[dry-run] %d queries would be sent. No API calls.", len(tasks))
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

    extra_body = None
    if args.provider == "deepseek":
        extra_body = {"thinking": {"type": args.thinking}}   # disabled for this run
    # thinking=disabled silences the THINKING channel only; the model still
    # writes chain-of-thought as ordinary content. JSON mode is what stops that.
    response_format = {"type": "json_object"} if args.json_mode == "on" else None
    LOG.info("thinking=%s extra_body=%s json_mode=%s fail_fast=%s",
             args.thinking, extra_body, args.json_mode, args.fail_fast)

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
        prompt = build_prompt(row["wkt"], prop)
        raw, usage, latency, finish_reason = await call_model(
            client, args.model, prompt, args.temperature, args.max_tokens,
            extra_body, response_format
        )
        parsed, ok = parse_answer(raw, prop)
        rec = make_record(row, prop, raw, parsed, ok, args.model, args.provider,
                          args.temperature, prompt, usage, finish_reason, args.max_tokens)
        LOG.debug("OK  %-28s %-22s parse=%s in=%s out=%s %.2fs ans=%s",
                  row["object_id"], prop, ok,
                  usage.get("prompt_tokens"), usage.get("completion_tokens"),
                  latency, repr(parsed)[:60])
        return rec, ok, raw, usage

    # ---- FAIL-FAST: sequential; stop at first error OR parse failure ----
    if args.fail_fast:
        LOG.info("FAIL-FAST mode: sequential, stop at first failure.")
        progress = tqdm(total=len(tasks), unit="query")
        for row, prop in tasks:
            try:
                rec, ok, raw, usage = await run_one(row, prop)
            except Exception as exc:
                progress.close()
                stop_on_failure(row, prop, "API ERROR",
                                f"{type(exc).__name__}: {exc}", None, log_path, totals)
                return
            if not ok:
                write_record(rec, usage, ok)
                write_json_export(args.jsonl_output, args.json_output)
                progress.close()
                stop_on_failure(row, prop, "PARSE FAILURE",
                                f"could not parse {prop}", raw, log_path, totals)
                return
            write_record(rec, usage, ok)
            progress.update(1)
        progress.close()
        write_json_export(args.jsonl_output, args.json_output)
        print_summary(totals, args)
        print("\nAll queries completed with NO failures. (fail-fast)")
        LOG.info("FAIL-FAST end | completed=%d tokens in=%d out=%d",
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
                LOG.warning("PARSE-FAIL %-28s %-22s raw=%s",
                            row["object_id"], prop, repr(raw)[:120])
        except Exception as exc:  # noqa: BLE001
            async with write_lock:
                failures.append((row["object_id"], prop, type(exc).__name__))
            LOG.error("FAIL %-28s %-22s %s: %s",
                      row["object_id"], prop, type(exc).__name__, exc)
        finally:
            progress.update(1)

    await asyncio.gather(*(tracked(row, prop) for row, prop in tasks))
    progress.close()

    write_json_export(args.jsonl_output, args.json_output)
    print_summary(totals, args)
    report_failures(failures)
    LOG.info("RUN end | completed=%d failed=%d tokens in=%d out=%d",
             totals["n"], len(failures), totals["prompt_tokens"], totals["completion_tokens"])
    LOG.info("full log saved -> %s", log_path)


def stop_on_failure(row, prop, kind, detail, raw, log_path, totals) -> None:
    LOG.error("STOP at first failure | %s | %s / %s | %s",
              kind, row["object_id"], prop, detail)
    print("\n" + "X" * 60)
    print(f"STOPPED at first failure  ({kind})")
    print("X" * 60)
    print(f"  object_id : {row['object_id']}")
    print(f"  tier      : {row['tier']}   shape: {row['shape_type']}   "
          f"vertices: {row['num_vertices']}")
    print(f"  property  : {prop}")
    print(f"  ground_truth: {row['properties'][prop]}")
    print(f"  detail    : {detail}")
    if raw is not None:
        print(f"  raw_model_output: {raw!r}")
    print(f"  wkt       : {row['wkt']}")
    print("-" * 60)
    print(f"  queries OK before stop: {totals['n']}  "
          f"(in={totals['prompt_tokens']} out={totals['completion_tokens']} tokens)")
    print(f"  log file  : {log_path}")
    print("X" * 60)
    print("Fix the issue, then re-run the SAME command:")
    print("  successful queries skip, the rest resume from here.")
    print("  (parse failures: add --retry-parse-failures to re-query them too.)")


def report_failures(failures) -> None:
    if not failures:
        print("\nAll queries completed. No failures.")
        return
    LOG.error("%d queries failed (not logged): %s",
              len(failures), [f"{o}/{p}" for o, p, _ in failures])
    print("\n" + "!" * 56)
    print(f"{len(failures)} queries FAILED (not logged) - re-run to retry ONLY these:")
    for oid, prop, err in failures[:30]:
        print(f"  {oid} / {prop}  ({err})")
    if len(failures) > 30:
        print(f"  ... and {len(failures) - 30} more")
    print("!" * 56)
    print("Re-run the same command - logged queries skip, failed ones retry.")


def write_json_export(jsonl_path: Path, json_path: Path) -> None:
    """Export JSONL as a JSON array, keeping the LATEST record per (object_id, property)."""
    if not jsonl_path.exists():
        return
    latest = read_latest_records(jsonl_path)
    records = sorted(latest.values(), key=lambda r: (r["object_id"], r["property"]))
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(records, indent=2, ensure_ascii=True) + "\n")


def print_summary(totals, args) -> None:
    n = totals["n"]
    if n == 0:
        print("\nNo new queries ran (all already logged). Use --force to re-run.")
        return
    pin, pout = totals["prompt_tokens"], totals["completion_tokens"]
    cost = pin / 1e6 * args.price_in + pout / 1e6 * args.price_out
    print("\n" + "=" * 56)
    print("V4-PRO NON-THINKING RUN SUMMARY (real usage from API)")
    print("=" * 56)
    print(f"queries run            : {n}")
    print(f"parse success          : {totals['parsed_ok']}/{n}"
          f"  ({100*totals['parsed_ok']/n:.0f}%)")
    print(f"input tokens (total)   : {pin:,}  (avg {pin/n:.0f}/query)")
    print(f"output tokens (total)  : {pout:,}  (avg {pout/n:.0f}/query)")
    print(f"price                  : ${args.price_in}/1M in, ${args.price_out}/1M out")
    print(f"REAL cost              : ${cost:.4f}")
    print("=" * 56)
    print(f"jsonl: {args.jsonl_output}")
    print(f"json : {args.json_output}")
    LOG.info("COST n=%d parse_ok=%d in_tok=%d out_tok=%d real_cost=$%.4f",
             n, totals["parsed_ok"], pin, pout, cost)


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
