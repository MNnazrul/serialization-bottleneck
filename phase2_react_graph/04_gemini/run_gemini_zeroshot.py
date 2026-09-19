"""
Experiment 2 — ZERO-SHOT control arm for GEMINI, Domain 2 (Graphs).

The matched baseline for the ReAct run in this folder: the SAME models, the
SAME 300-graph dataset, the SAME 8 questions and scoring, but Experiment 1's
single direct-answer call ("Respond with only {\"answer\": <your answer>}")
instead of a Thought/Action/Observation loop. Run in the same session as the
ReAct arm so the comparison is not confounded by model-snapshot drift.

This is Experiment 1's method exactly -- it imports the Experiment-1 graph
harness unchanged and only changes the output filename (`gemini_zeroshot_*`)
so it sits beside the ReAct results without colliding.

  python run_gemini_zeroshot.py --dry-run
  python run_gemini_zeroshot.py --limit 5 --fail-fast
  python run_gemini_zeroshot.py

Output: gemini_zeroshot_results.jsonl / gemini_zeroshot_results.json /
        logs/gemini_zeroshot_<timestamp>.log
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "phase2_model_results_graph" / "_common"))

from graph_harness import ModelConfig, main  # noqa: E402

CONFIG = ModelConfig(
    key="gemini_zeroshot",
    label="GEMINI (zero-shot)",
    default_provider="google",
    default_model="gemini-2.5-flash-lite",
    default_price_in=0.10,
    default_price_out=0.40,
    thinking_mode="google",
    supports_json_mode=True,
    default_json_mode="on",
    default_subset="none",
    model_help="Gemini 2.5 Flash-Lite via Google API.",
)

if __name__ == "__main__":
    main(CONFIG, HERE)
