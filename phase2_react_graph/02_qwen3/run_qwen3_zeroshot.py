"""
Experiment 2 — ZERO-SHOT control arm for QWEN3, Domain 2 (Graphs).

The matched baseline for the ReAct run in this folder: the SAME models, the
SAME 300-graph dataset, the SAME 8 questions and scoring, but Experiment 1's
single direct-answer call ("Respond with only {\"answer\": <your answer>}")
instead of a Thought/Action/Observation loop. Run in the same session as the
ReAct arm so the comparison is not confounded by model-snapshot drift.

This is Experiment 1's method exactly -- it imports the Experiment-1 graph
harness unchanged and only changes the output filename (`qwen3_zeroshot_*`)
so it sits beside the ReAct results without colliding.

  python run_qwen3_zeroshot.py --dry-run
  python run_qwen3_zeroshot.py --limit 5 --fail-fast
  python run_qwen3_zeroshot.py

Output: qwen3_zeroshot_results.jsonl / qwen3_zeroshot_results.json /
        logs/qwen3_zeroshot_<timestamp>.log
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "phase2_model_results_graph" / "_common"))

from graph_harness import ModelConfig, main  # noqa: E402

CONFIG = ModelConfig(
    key="qwen3_zeroshot",
    label="QWEN3 (zero-shot)",
    default_provider="openrouter",
    default_model="qwen/qwen3-32b",
    default_price_in=0.08,
    default_price_out=0.28,
    thinking_mode="openrouter_qwen",
    supports_json_mode=False,
    default_json_mode="off",
    default_subset="none",
    model_help="Qwen3-32B via OpenRouter.",
)

if __name__ == "__main__":
    main(CONFIG, HERE)
