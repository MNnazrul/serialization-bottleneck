"""
Experiment 2 (ReAct) — GPT-4.1 Mini, Domain 2 (Graphs).

Same task as ../../phase2_model_results_graph/05_gpt/ (2,399 episodes),
answered with a Thought / Action / Observation loop (Yao et al., 2023) instead
of a single zero-shot call. Finish[...] is scored by the Experiment-1
machinery.

  - GPT-4.1 Mini, no thinking mode ("n/a (native)").
  - JSON mode is NOT used here (unlike the Experiment-1 runner): ReAct output
    is free-form Thought/Action text.
  - temperature 0, max_tokens 512 per turn, max 15 steps per episode
  - Provider: OpenAI (api.openai.com), key OPENAI_API_KEY

Output: gpt_results.jsonl / gpt_results.json / logs/gpt_<timestamp>.log

  python run_gpt_react.py --dry-run
  python run_gpt_react.py --limit 5 --fail-fast
  python run_gpt_react.py

Shared logic: ../_common/react_graph_harness.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "_common"))

from react_graph_harness import ModelConfig, main  # noqa: E402

CONFIG = ModelConfig(
    key="gpt",
    label="GPT",
    default_provider="openai",
    default_model="gpt-4.1-mini",
    default_price_in=0.40,
    default_price_out=1.60,
    thinking_mode="none",
    supports_json_mode=False,
    default_subset="none",
    model_help="GPT-4.1 Mini via OpenAI (PDF Table 1).",
)

if __name__ == "__main__":
    main(CONFIG, HERE)
