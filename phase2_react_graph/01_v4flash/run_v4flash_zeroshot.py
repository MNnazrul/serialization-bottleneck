"""
Experiment 2 — ZERO-SHOT control arm for V4-FLASH, Domain 2 (Graphs).

The matched baseline for the ReAct run in this folder: the SAME models, the
SAME 300-graph dataset, the SAME 8 questions and scoring, but Experiment 1's
single direct-answer call ("Respond with only {\"answer\": <your answer>}")
instead of a Thought/Action/Observation loop. Run in the same session as the
ReAct arm so the comparison is not confounded by model-snapshot drift.

This is Experiment 1's method exactly -- it imports the Experiment-1 graph
harness unchanged and only changes the output filename (`v4flash_zeroshot_*`)
so it sits beside the ReAct results without colliding.

  python run_v4flash_zeroshot.py --dry-run
  python run_v4flash_zeroshot.py --limit 5 --fail-fast
  python run_v4flash_zeroshot.py

Output: v4flash_zeroshot_results.jsonl / v4flash_zeroshot_results.json /
        logs/v4flash_zeroshot_<timestamp>.log
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "phase2_model_results_graph" / "_common"))

from graph_harness import ModelConfig, main  # noqa: E402

CONFIG = ModelConfig(
    key="v4flash_zeroshot",
    label="V4-FLASH (zero-shot)",
    default_provider="deepseek",
    default_model="deepseek-v4-flash",
    default_price_in=0.14,
    default_price_out=0.28,
    thinking_mode="deepseek",
    supports_json_mode=False,
    default_json_mode="off",
    default_subset="none",
    model_help="DeepSeek-direct: deepseek-v4-flash.",
)

if __name__ == "__main__":
    main(CONFIG, HERE)
