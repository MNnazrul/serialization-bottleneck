"""
Experiment 2 — ZERO-SHOT control arm for GPT, Domain 2 (Graphs).

The matched baseline for the ReAct run in this folder: the SAME models, the
SAME 300-graph dataset, the SAME 8 questions and scoring, but Experiment 1's
single direct-answer call ("Respond with only {\"answer\": <your answer>}")
instead of a Thought/Action/Observation loop. Run in the same session as the
ReAct arm so the comparison is not confounded by model-snapshot drift.

This is Experiment 1's method exactly -- it imports the Experiment-1 graph
harness unchanged and only changes the output filename (`gpt_zeroshot_*`)
so it sits beside the ReAct results without colliding.

  python run_gpt_zeroshot.py --dry-run
  python run_gpt_zeroshot.py --limit 5 --fail-fast
  python run_gpt_zeroshot.py

Output: gpt_zeroshot_results.jsonl / gpt_zeroshot_results.json /
        logs/gpt_zeroshot_<timestamp>.log
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "phase2_model_results_graph" / "_common"))

from graph_harness import ModelConfig, main  # noqa: E402

CONFIG = ModelConfig(
    key="gpt_zeroshot",
    label="GPT (zero-shot)",
    default_provider="openai",
    default_model="gpt-4.1-mini",
    default_price_in=0.40,
    default_price_out=1.60,
    thinking_mode="none",
    supports_json_mode=True,
    default_json_mode="on",
    default_subset="none",
    model_help="GPT-4.1 Mini via OpenAI.",
)

if __name__ == "__main__":
    main(CONFIG, HERE)
