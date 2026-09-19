"""
Experiment 2 (ReAct) — Gemini 2.5 Flash-Lite, Domain 2 (Graphs).

Same task as ../../phase2_model_results_graph/04_gemini/ (2,399 episodes),
answered with a Thought / Action / Observation loop (Yao et al., 2023) instead
of a single zero-shot call. Finish[...] is scored by the Experiment-1
machinery.

  - NON-thinking: Gemini's OpenAI-compat endpoint disables it via
    reasoning_effort="none".
  - JSON mode is NOT used here (unlike the Experiment-1 runner): ReAct output
    is free-form Thought/Action text. The per-turn stop sequence
    ("\\nObservation") plus max_tokens 512 keeps each turn bounded instead.
  - temperature 0, max_tokens 512 per turn, max 15 steps per episode
  - Provider: Google (generativelanguage.googleapis.com OpenAI-compat),
    key GEMINI_API_KEY

Output: gemini_results.jsonl / gemini_results.json / logs/gemini_<timestamp>.log

  python run_gemini_react.py --dry-run
  python run_gemini_react.py --limit 5 --fail-fast
  python run_gemini_react.py

Shared logic: ../_common/react_graph_harness.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "_common"))

from react_graph_harness import ModelConfig, main  # noqa: E402

CONFIG = ModelConfig(
    key="gemini",
    label="GEMINI",
    default_provider="google",
    default_model="gemini-2.5-flash-lite",
    default_price_in=0.10,
    default_price_out=0.40,
    thinking_mode="google",
    supports_json_mode=False,
    default_subset="none",
    model_help="Gemini 2.5 Flash-Lite via Google API (PDF Table 1).",
)

if __name__ == "__main__":
    main(CONFIG, HERE)
