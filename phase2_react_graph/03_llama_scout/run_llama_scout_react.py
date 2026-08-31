"""
Experiment 2 (ReAct) — Llama 4 Scout, Domain 2 (Graphs).

Same task as ../../phase2_model_results_graph/03_llama_scout/ (2,399 episodes),
answered with a Thought / Action / Observation loop (Yao et al., 2023) instead
of a single zero-shot call. Finish[...] is scored by the Experiment-1
machinery.

  - Llama 4 Scout via OpenRouter. No thinking mode ("n/a (native)") -- nothing
    is sent to disable it.
  - JSON mode is NOT used here (unlike the Experiment-1 runner): ReAct output
    is free-form Thought/Action text, not a single JSON object, so
    response_format cannot apply.
  - temperature 0, max_tokens 512 per turn, max 15 steps per episode

Key required: OPENROUTER_API_KEY.

Output: llama_scout_results.jsonl / llama_scout_results.json /
        logs/llama_scout_<timestamp>.log

  python run_llama_scout_react.py --dry-run
  python run_llama_scout_react.py --limit 5 --fail-fast
  python run_llama_scout_react.py

Shared logic: ../_common/react_graph_harness.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "_common"))

from react_graph_harness import ModelConfig, main  # noqa: E402

CONFIG = ModelConfig(
    key="llama_scout",
    label="LLAMA-SCOUT",
    default_provider="openrouter",
    default_model="meta-llama/llama-4-scout",
    default_price_in=0.10,
    default_price_out=0.30,
    thinking_mode="none",
    supports_json_mode=False,
    default_subset="none",
    model_help="Llama 4 Scout via OpenRouter (PDF Table 1).",
)

if __name__ == "__main__":
    main(CONFIG, HERE)
