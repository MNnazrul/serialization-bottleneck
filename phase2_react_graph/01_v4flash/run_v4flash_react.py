"""
Experiment 2 (ReAct) — DeepSeek-V4-Flash, Domain 2 (Graphs).

Same task as ../../phase2_model_results_graph/01_v4flash/ (300 graphs x 8
properties, uncertified chromatic_number pairs excluded -> 2,399 episodes),
but each question is answered with an interleaved Thought / Action /
Observation loop (Yao et al., 2023) instead of a single zero-shot call. The
answer given by Finish[...] is scored by the exact Experiment-1 machinery.

V4-Flash is the debug-first model here too -- run this one first to validate
the harness before the other five.

  - DeepSeek-V4-Flash, NON-thinking -> extra_body={"thinking": {"type": "disabled"}}
  - temperature 0, max_tokens 512 per turn, max 15 steps per episode
  - Provider: DeepSeek direct (api.deepseek.com), key DEEPSEEK_API_KEY

Output stays in this folder:
  v4flash_results.jsonl / v4flash_results.json / logs/v4flash_<timestamp>.log

Resume is by (object_id, property) exactly as Experiment 1 -- re-running skips
logged pairs. NOTHING runs on import.

  python run_v4flash_react.py --dry-run              # build prompts, no API calls
  python run_v4flash_react.py --limit 5 --fail-fast  # smoke test: 5 graphs x 8
  python run_v4flash_react.py                        # full run, concurrent

Shared logic: ../_common/react_graph_harness.py (which reuses
../../phase2_model_results_graph/_common/graph_harness.py unchanged).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "_common"))

from react_graph_harness import ModelConfig, main  # noqa: E402

CONFIG = ModelConfig(
    key="v4flash",
    label="V4-FLASH",
    default_provider="deepseek",
    default_model="deepseek-v4-flash",
    default_price_in=0.14,
    default_price_out=0.28,
    thinking_mode="deepseek",
    supports_json_mode=False,
    default_subset="none",
    model_help="DeepSeek-direct: deepseek-v4-flash (default).",
)

if __name__ == "__main__":
    main(CONFIG, HERE)
