"""
Experiment 2 (ReAct) — Qwen3-32B, Domain 2 (Graphs).

Same task as ../../phase2_model_results_graph/02_qwen3/ (2,399 episodes),
answered with a Thought / Action / Observation loop (Yao et al., 2023) instead
of a single zero-shot call. Finish[...] is scored by the Experiment-1
machinery.

  - Qwen3-32B via OpenRouter. NON-thinking: OpenRouter's unified reasoning
    param is not reliably honored by every upstream provider, so this also
    appends " /no_think" to the first message -- same exception as the
    Experiment-1 Qwen3 runner (see
    ../../phase2_model_results/README.md §3 "The one prompt exception").
  - temperature 0, max_tokens 512 per turn, max 15 steps per episode

Key required: OPENROUTER_API_KEY.

Output: qwen3_results.jsonl / qwen3_results.json / logs/qwen3_<timestamp>.log

  python run_qwen3_react.py --dry-run
  python run_qwen3_react.py --limit 5 --fail-fast
  python run_qwen3_react.py

Shared logic: ../_common/react_graph_harness.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "_common"))

from react_graph_harness import ModelConfig, main  # noqa: E402

CONFIG = ModelConfig(
    key="qwen3",
    label="QWEN3",
    default_provider="openrouter",
    default_model="qwen/qwen3-32b",
    default_price_in=0.08,
    default_price_out=0.28,
    thinking_mode="openrouter_qwen",
    supports_json_mode=False,
    default_subset="none",
    model_help="Qwen3-32B via OpenRouter (PDF Table 1: Qwen3-32B = API/OpenRouter).",
)

if __name__ == "__main__":
    main(CONFIG, HERE)
