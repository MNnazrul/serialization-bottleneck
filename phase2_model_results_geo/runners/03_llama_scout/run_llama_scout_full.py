"""
Domain 1 — FULL Llama 4 Scout query run (all 300 polygons x 9 properties).

This is Experiment 1, Phase 2 (model queries) for Llama 4 Scout, a Primary model.
Same harness as the validated V4-Flash run; only the provider/model/non-thinking
mechanism differ.

Per Experiment 1 protocol (serialization_experiment_1.pdf):
  - 300 polygons x 9 properties = 2,700 queries
  - Llama 4 Scout via OpenRouter (Table 1: Llama 4 Scout = API/OpenRouter)
  - NON-thinking mode (PDF: all models except V4-Pro are non-thinking).
    Llama 4 Scout has NO thinking/reasoning mode -> non-thinking by default;
    no disable param or "/no_think" token needed (prompt stays pristine).
  - temperature 0, max_tokens 512, no system prompt, zero-shot
  - retry: exponential backoff, 3 retries (delays 2s, 4s, 8s) on timeout /
    rate-limit / connection / 5xx errors; record model + provider + timestamp

Key: OPENROUTER_API_KEY (this run uses OpenRouter, NOT the DeepSeek key).

v8 of run_llama_scout_full.py. Same model and same query protocol; it reads
new_data_v7/geometry_exp1_dataset.json.

The suffix names the DATASET, not this model's own run count: v6 was
new_data_v5 across every model, v7 was new_data_v7, and v8 is
new_data_v7.

WHAT new_data_v7 CHANGES, and what it means for these numbers

  * THE MEASUREMENT BALANCE IS v6's, UNCHANGED. Integer coordinates in
    [0,1000] at every tier; shape mix 50/25/25 convex/concave/irregular;
    orientation 50/50; offset_band 22/38/32/8 and fill_band 14/37/36/13 under
    the same 30-cell joint quota per shape class. Each of those is identical in
    all three tiers, exactly as in v6. The grading band is therefore the same
    WIDTH as in the v7 run, so accuracy levels are comparable in aggregate -
    but the 300 polygons are newly generated and carry new object_ids, so there
    is no record-for-record join between this run and the v7 one.

  * FOUR GENERATOR FAMILIES, EXACTLY 25 PER TIER. v6's generator mix drifted
    hard across tiers - `convex` ran 27/9/4 and `convex_sharp` 23/41/46 - so a
    convex polygon in v6's hard tier was 92% likely to come from one
    construction and in its simple tier only 46% likely. Any tier difference
    measured on the convex half was therefore partly a difference in which
    construction built it, not in difficulty. v7 fixes all four families at 25
    per tier, the way phase1_dataset_graph fixes its five graph families at 20,
    and spreads each family across 7-10 cells of the balance grid so family and
    cell stay independent.

  * SIZE IS FLAT ACROSS TIERS BY CONSTRUCTION, NOT BY LUCK. PDF 6.2 divides
    bbox and centroid error by the bbox diagonal, so a systematically larger
    tier draws a systematically wider grading band and "difficulty" stops
    meaning difficulty. v7 deals extent and aspect as FIXED PAIRS off a
    100-rung ladder and hands every tier the identical multiset of pairs.
    Tier spread in mean bbox diagonal: 0.01%, against v6's 1.29%. In mean bbox
    area: 0.04%, against 3.87%.

    The trade runs the other way for `area` - 0.59% against v6's 0.05% - and is
    deliberate. `area`, `perimeter`, `aspect_ratio` and `edge_length_variance`
    are graded by RELATIVE error and are scale-free, so their magnitude never
    feeds back into the band. The diagonal does.

  * VERTEX COUNT IS SPREAD, NOT CLUMPED. v6's hard tier put 14 of its 100
    records on exactly 20 vertices and never reached 3 of the 21 counts
    available to it. v7's hard tier reaches all 21 with at most 5 records on
    any one count. Any result conditioned on vertex count is on a different
    footing here: the hard tier is no longer dominated by its own floor.

  * NEW RECORD FIELDS, AND TWO REMOVED. Added: `family` (which of the four
    constructions built the polygon), `target_extent` and `target_aspect` (the
    size-ladder rungs it consumed), `vertex_target` and `vertex_target_met`.
    Removed: `limacon_bias` and `projective_squeeze`, which were v6 generator
    parameters and do not exist in new_data_v7.

  * PAIRING: none. 300 records are 300 independent polygons, as in v6. Phase 3
    bootstraps records, and must not cluster.

  * THE DATASET SHIPS ITS GENERATOR. new_data_v7/generate_polygons_v7.py is
    seeded, deterministic, and exits non-zero if any balance check or any
    property verification fails; new_data_v7/README.md documents the design.
    new_data_v7 shipped two JSON files and no code, which is why the v7 runners
    could not tell you how their dataset was built.

Records are written to ../03_results/<model folder>/, matching the results_v7/
layout rather than scattering one file per model folder.

Differences from v1:
  1. Dataset. v1's DEFAULT_DATASET pointed at a folder that does not exist in
     this repo, so the default never resolved.
  2. finish_reason is captured. v1 never read resp.choices[0].finish_reason,
     so all 2,700 of its records carry null and Phase 3's truncation rate came
     from a token-count fallback.
  3. failure_type labels a TRUNCATED-BUT-PARSED answer - a generation cut off
     at the cap that still contains a parseable number is still truncated, and
     the number it yields came from a half-finished computation.
  4. Every record stores the max_tokens it ran under, and --object-ids allows
     re-probing one stuck (polygon, property) without touching the rest.

JSON mode was already present in v1. Its MECHANISM changed in v8: the
request now sends a response_format json_schema (structured outputs)
instead of json_object, because OpenRouter stopped routing json_object for
this model - only a BYOK-gated provider advertises it, so every query 404'd.
The schema only constrains the envelope, not the answer; the prompt and the
question text are unchanged. Full reasoning and the measured alternatives
are at the response_format assignment in run().

run_llama_scout_full.py is unchanged and still runs the original dataset.

Results go to ../03_results/<model folder>/; logs stay beside this runner:
  llama_scout_results.jsonl  -- append-only, resume-safe, one record/query
  llama_scout_results.json   -- JSON array export (latest record per pair)
  (llama_scout_results.jsonl/.json are the earlier run; untouched)
  logs/llama_scout_v8_<timestamp>.log -- per-query log (one file per run)

Resume: re-running skips already-logged (object_id, property) pairs, so a crash
loses nothing. Fail-fast (--fail-fast) stops at the first error/parse failure.

NOTHING runs on import. Execute explicitly:
  python run_llama_scout_full_v7.py              # full run, concurrent
  python run_llama_scout_full_v7.py --fail-fast # sequential, stop at first failure
  python run_llama_scout_full.py --dry-run       # build prompts, no API calls
  python run_llama_scout_full.py --limit 5       # smoke test: first 5 polygons
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
REPO_ROOT = HERE.parent.parent
# Re-pathed for the exp1_geo layout so this folder runs on its own:
#   ../../01_dataset/   the 300 polygons this run queries
#   ../../03_results/   where this runner appends its records
#   ./logs/             this runner's own debug log
# Nothing reaches back into phase2_model_results/.
# Paths for the split layout. This runner lives at
#   phase2_model_results_geo/runners/<model>/
# so the repository root is three levels up.
REPO_ROOT = HERE.parent.parent.parent
DATASET_DIR = REPO_ROOT / "phase1_dataset_geo"
RESULTS_ROOT = HERE.parent.parent / "results"
DEFAULT_DATASET = DATASET_DIR / "geometry_exp1_dataset.json"
# Each dataset gets its own result files. Resume keys on (object_id,
# property), and ids repeat across datasets while describing different
# polygons, so sharing one JSONL between two datasets would blend them.
# Results live in the shared results_v8/ tree, one folder per model, rather than
# beside this runner the way v1-v5 wrote them. One place to look for a v8 result,
# and the gemini run stops being the odd one out with its own results/ subfolder.
# Nothing is duplicated: this is where the file IS, not a copy of it.
RESULTS_DIR = RESULTS_ROOT / HERE.name
LOG_DIR = HERE / "logs"
DEFAULT_JSONL = RESULTS_DIR / "llama_scout_results.jsonl"
DEFAULT_JSON = RESULTS_DIR / "llama_scout_results.json"

# Module logger; handlers attached in setup_logging(). tenacity logs retries here.
LOG = logging.getLogger("llama_scout")


def setup_logging(log_dir: Path) -> Path:
    """Log to a timestamped file (DEBUG, every query) AND console (INFO).
    Each run keeps its own file. Returns the file path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"llama_scout_{stamp}.log"

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
    p = argparse.ArgumentParser(description="Full Llama 4 Scout query run over all 300 polygons.")
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--jsonl-output", type=Path, default=DEFAULT_JSONL)
    p.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    p.add_argument("--log-dir", type=Path, default=LOG_DIR)
    p.add_argument("--provider", choices=list(PROVIDERS), default="openrouter")
    p.add_argument("--model", default="meta-llama/llama-4-scout",
                   help="Llama 4 Scout via OpenRouter (PDF Table 1: Llama 4 Scout = API/OpenRouter).")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=512)   # PDF setting
    # 2, not 5. OpenRouter serves llama-4-scout from a shared upstream pool
    # (DeepInfra), and at 5 the v5 smoke test lost 22 of 45 queries to
    # 429 engine_overloaded - the per-query retry ladder is 2/4/8s per the
    # protocol, and the pool stays overloaded longer than that. The same 22
    # queries succeeded 22/22 on a re-run at 2. Concurrency is not a protocol
    # parameter (PDF section 2 fixes temperature, max_tokens and the retry
    # schedule, not the request rate), so lowering it changes nothing about
    # what is asked or how a reply is graded - only how hard the shared pool
    # is pushed. Raise it with --concurrency if the pool is quiet.
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--thinking", choices=["disabled", "enabled"], default="disabled",
                   help="Llama 4 Scout is natively non-thinking (no reasoning/CoT mode).")
    p.add_argument("--json-mode", choices=["on", "off"], default="on",
                   help="Force JSON-only output via a response_format json_schema "
                        "(structured outputs). Suppresses the model's content-level "
                        "chain-of-thought so it emits a direct answer within 512 "
                        "tokens. Default on. Turning it OFF costs ~40x the output "
                        "tokens and truncates most answers - see the note at the "
                        "response_format assignment before doing so.")
    p.add_argument("--properties", nargs="+", choices=PROPERTIES, default=list(PROPERTIES),
                   help="Subset of properties to query (default: all 9). "
                        "Use to re-run a single property, e.g. --properties perimeter.")
    p.add_argument("--fail-fast", action="store_true",
                   help="Stop at the FIRST failure (API error OR parse failure), print "
                        "full details, exit. Sequential so 'first' is exact. Re-run resumes.")
    # Llama 4 Scout OpenRouter prices ($ per 1M tokens).
    p.add_argument("--price-in", type=float, default=0.10)
    p.add_argument("--price-out", type=float, default=0.30)
    p.add_argument("--limit", type=int, default=None,
                   help="Only the first N polygons (smoke test). Resume still applies.")
    p.add_argument("--force", action="store_true",
                   help="Re-run every pair, even ones already logged.")
    p.add_argument("--object-ids", nargs="+", default=None,
                   help="Restrict the run to these object_ids. Combine with --force "
                        "to re-query a pair already logged as parsed.")
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


def bbox_diagonal(row: dict[str, Any]) -> float:
    """Bounding-box diagonal, the normalizer for bbox/centroid point error.
    Stored in metadata by every generator here; computed from the ground-truth
    bbox if a dataset omits it, with the same 3-decimal rounding."""
    diag = row.get("metadata", {}).get("bbox_diagonal")
    if diag is not None:
        return float(diag)
    min_x, min_y, max_x, max_y = (float(v) for v in row["properties"]["bbox"])
    return round(math.hypot(max_x - min_x, max_y - min_y), 3)


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
        abs_err, rel = normalized_point_error(parsed, truth, bbox_diagonal(row))
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
    """`reasoning_truncated` if the GENERATION hit the token limit,
    `parse_failure` if the output could not be parsed, else None.

    Truncation is checked first and independently of `ok`: an answer cut off
    at the cap that still contains a parseable number is still truncated, and
    the number it yields came from a half-finished computation."""
    truncated = finish_reason == "length" or (
        completion_tokens is not None and max_tokens is not None
        and completion_tokens >= max_tokens
    )
    if truncated:
        return "reasoning_truncated"
    return None if ok else "parse_failure"


def make_record(row, property_name, raw_output, parsed, ok, model, provider,
                temperature, prompt, usage, finish_reason=None,
                max_tokens=512) -> dict[str, Any]:
    rec = {
        "object_id": row["object_id"],
        "tier": row["tier"],
        "shape_type": row["shape_type"],
        # new_data_v7 design fields, carried so Phase 3 need not re-join against
        # the dataset file.
        #
        # `offset_band` and `fill_band` are the load-bearing pair: the first is
        # the band of the centroid's distance from the bbox centre, the second
        # the band of how much of its bounding box the polygon fills. Both are
        # held flat across tiers (22/38/32/8 and 14/37/36/13), so neither the
        # bbox-centre answer for centroid nor the bbox-area / ellipse-area
        # answer for area gets easier as the tier gets harder.
        #
        # `vertex_mean_offset_norm` is the SECOND centroid shortcut - the
        # distance from the coordinate mean. Carrying both offsets lets Phase 3
        # score either guess directly.
        #
        # `family` is new here and is the field that distinguishes this dataset
        # from v6: four constructions, exactly 25 per tier each. v6 carried
        # `generator` for the same purpose but let it drift 27/9/4 across
        # tiers. Both fields are carried and they are equal in v7, so a query
        # written against either one works.
        #
        # `target_extent` and `target_aspect` are the size-ladder rungs this
        # record consumed. Every tier consumes the identical multiset of
        # (extent, aspect) PAIRS, which is what holds the bbox diagonal flat
        # across tiers - the quantity PDF 6.2 divides centroid and bbox error
        # by. Carrying them lets Phase 3 re-verify that flatness from the
        # results alone, without opening the dataset file.
        #
        # `limacon_bias` and `projective_squeeze` are GONE. They were v6
        # generator parameters; new_data_v7 does not have them, and a runner
        # that asked for them would write 2,700 nulls.
        #
        # There is no shape_id: one record is one polygon and one independent
        # observation. Phase 3 must not cluster these.
        "offset_band": row.get("metadata", {}).get("offset_band"),
        "fill_band": row.get("metadata", {}).get("fill_band"),
        "centroid_offset_norm": row.get("metadata", {}).get("centroid_offset_norm"),
        "vertex_mean_offset_norm":
            row.get("metadata", {}).get("vertex_mean_offset_norm"),
        "fill_ratio": row.get("metadata", {}).get("fill_ratio"),
        "generator": row.get("metadata", {}).get("generator"),
        "family": row.get("metadata", {}).get("family"),
        "target_extent": row.get("metadata", {}).get("target_extent"),
        "target_aspect": row.get("metadata", {}).get("target_aspect"),
        "vertex_target": row.get("metadata", {}).get("vertex_target"),
        "vertex_target_met": row.get("metadata", {}).get("vertex_target_met"),
        "coordinate_type": row.get("metadata", {}).get("coordinate_type"),
        "num_vertices": row["num_vertices"],
        "property": property_name,
        "property_locality": "local" if property_name in LOCAL_PROPERTIES else "global",
        "ground_truth": row["properties"][property_name],
        "prompt": prompt,
        "raw_model_output": raw_output,
        "parsed_answer": parsed,
        "parse_success": ok,
        "finish_reason": finish_reason,
        "max_tokens": max_tokens,
        "failure_type": classify_failure(ok, finish_reason,
                                         usage.get("completion_tokens"), max_tokens),
        "model": model,
        "provider": provider,                  # PDF §2: record provider for every call
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


# PDF §2: retry on timeout or rate-limit errors, initial delay 2s, multiplier 2,
# 3 retries. wait_exponential(multiplier=2, min=2) -> 2s, 4s, 8s. Retry only the
# transient error classes (timeout / rate-limit / connection / 5xx), not 4xx like
# a bad request or auth error.
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
        kwargs["response_format"] = response_format
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
    LOG.info("LLAMA-4-SCOUT FULL RUN start (v8 / new_data_v7, integer-coordinate dataset)")
    LOG.info("provider=%s model=%s temp=%s max_tokens=%s concurrency=%s",
             args.provider, args.model, args.temperature, args.max_tokens, args.concurrency)
    LOG.info("dataset=%s", args.dataset)
    LOG.info("log file=%s", log_path)

    dataset = load_dataset(args.dataset)
    if args.object_ids:
        wanted = set(args.object_ids)
        dataset = [r for r in dataset if r["object_id"] in wanted]
        missing = wanted - {r["object_id"] for r in dataset}
        if missing:
            sys.exit(f"object_id not in dataset: {sorted(missing)}")
        LOG.info("object-ids filter -> %d polygons", len(dataset))
    completed = set() if args.force else done_pairs(args.jsonl_output, args.retry_parse_failures)
    tasks = build_tasks(dataset, args.properties, completed, args.limit, args.force)

    LOG.info("polygons=%d props=%d | planned new queries=%d | skipping=%d",
             len(dataset), len(args.properties), len(tasks), len(completed))
    print(f"Provider: {args.provider}  |  Model: {args.model}  |  thinking: {args.thinking}")
    print(f"Polygons: {len(dataset)} x {len(PROPERTIES)} props = {len(dataset)*len(PROPERTIES)} total")
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

    # Llama 4 Scout is a standard instruct MoE model with NO thinking/reasoning
    # mode (the "reasoning" on its model page = visual/image reasoning capability,
    # not a chain-of-thought mode). So it is non-thinking by default: no reasoning
    # param, no "/no_think" token needed. We send nothing extra; the prompt stays
    # pristine and identical to the base template.
    extra_body = None
    think_suffix = ""
    # Llama has no thinking mode, but it ignores "respond with only JSON" and
    # writes content-level chain-of-thought (overflows 512 tokens on hard
    # polygons -> no answer). Constraining the response format forces a direct
    # JSON object and suppresses the CoT. It enforces the format the prompt
    # already asks for; the question text is unchanged.
    #
    # WHY json_schema AND NOT json_object (changed in v8, 2026-09-15)
    #
    # v7 and earlier sent {"type": "json_object"}. That stopped working: every
    # query in the first v8 smoke run came back
    #
    #   404 - No endpoints found for meta-llama/llama-4-scout.
    #   routing_funnel: Initial Endpoints 3 -> Filter by Parameters 1
    #                   -> Add BYOK Endpoints 0
    #
    # Read the funnel right to left. Three providers serve this model;
    # requesting a parameter only one of them advertises drops the pool to that
    # one; that one is Google, whose endpoint needs bring-your-own-key
    # credentials this account does not have; so zero endpoints remain and
    # OpenRouter answers 404. Checked against
    # /api/v1/models/meta-llama/llama-4-scout/endpoints:
    #
    #   DeepInfra   structured_outputs      NO response_format
    #   Novita      neither                 NO response_format
    #   Google      response_format         BYOK only
    #
    # Nothing in this runner changed - the v7 run of 2026-09-14 completed 46/46
    # against the identical request. The provider mix moved underneath it.
    #
    # json_schema maps to `structured_outputs`, which DeepInfra DOES advertise,
    # so routing succeeds and the short-answer behaviour comes back. Measured on
    # the same 3 polygons, all nine properties:
    #
    #   json_object      404 on every query, 0 records
    #   json-mode off    27/27 run but 21/27 parse, 18 hit the 512 cap,
    #                    452 output tokens/query against v7's 11 - the model
    #                    writes "## Step 1: Understand the Problem" and runs out
    #                    of room before answering. A different experiment, not a
    #                    noisier one, so this is NOT the fallback to reach for.
    #   json_schema      clean JSON in 6-22 tokens, finish_reason=stop
    #
    # The schema is deliberately permissive: `"answer": {}` accepts any JSON
    # type, which it has to, because the nine properties answer with an int
    # (vertex_count), a bool (convex), a string (orientation), a 4- or 2-element
    # array (bbox, centroid) and floats (area, perimeter, aspect_ratio,
    # edge_length_variance). Pinning a type per property would constrain what
    # the model may say about its own answer, which is a protocol change;
    # constraining only the envelope is not.
    #
    # `--json-mode off` is left in place as an escape hatch, but read the table
    # above before using it.
    response_format = (
        {
            "type": "json_schema",
            "json_schema": {
                "name": "answer",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"answer": {}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            },
        }
        if args.json_mode == "on" else None
    )
    LOG.info("thinking=%s (Llama natively non-thinking) json_mode=%s fail_fast=%s",
             args.thinking, args.json_mode, args.fail_fast)

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
        prompt = build_prompt(row["wkt"], prop) + think_suffix
        raw, usage, latency, finish_reason = await call_model(
            client, args.model, prompt, args.temperature, args.max_tokens,
            extra_body, response_format
        )
        parsed, ok = parse_answer(raw, prop)
        rec = make_record(row, prop, raw, parsed, ok, args.model, args.provider,
                          args.temperature, prompt, usage,
                          finish_reason, args.max_tokens)
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
    print("LLAMA-4-SCOUT FULL RUN SUMMARY (real usage from API)")
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
