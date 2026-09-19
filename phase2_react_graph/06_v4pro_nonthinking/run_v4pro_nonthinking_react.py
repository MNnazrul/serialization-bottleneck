"""
Experiment 2 (ReAct) — DeepSeek-V4-Pro NON-THINKING, Domain 2 (Graphs).

Same task and same 20% stratified subsample as
../../phase2_model_results_graph/06_v4pro_nonthinking/ (60 graphs x 8 = 480
episodes), answered with a Thought / Action / Observation loop (Yao et al.,
2023) instead of a single zero-shot call. Finish[...] is scored by the
Experiment-1 machinery.

`build_subsample.py` and `subsample_v4pro_nonthinking.json` are copied verbatim
from the Experiment-1 graph folder so the exact same 60 graphs are used, seed
42 -- run `python build_subsample.py --verify` to confirm.

This is OFF the PDF protocol in the same way Experiment 1's own 06 is (the PDF
specifies V4-Pro in *thinking* mode); it is kept non-thinking so Experiment 1
and Experiment 2 stay comparable model-for-model.

  - thinking DISABLED -> extra_body={"thinking": {"type": "disabled"}}
  - temperature 0, max_tokens 512 per turn, max 15 steps per episode
  - Provider: DeepSeek direct (api.deepseek.com), key DEEPSEEK_API_KEY

Output: v4pro_nonthinking_results.jsonl / v4pro_nonthinking_results.json /
        logs/v4pro_nonthinking_<timestamp>.log

  python run_v4pro_nonthinking_react.py --dry-run
  python run_v4pro_nonthinking_react.py --limit 5 --fail-fast
  python run_v4pro_nonthinking_react.py

Shared logic: ../_common/react_graph_harness.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "_common"))

from react_graph_harness import ModelConfig, main  # noqa: E402

CONFIG = ModelConfig(
    key="v4pro_nonthinking",
    label="V4-PRO-NONTHINKING",
    default_provider="deepseek",
    default_model="deepseek-v4-pro",
    default_price_in=0.435,
    default_price_out=0.87,
    thinking_mode="deepseek",
    supports_json_mode=False,
    default_subset="subsample_v4pro_nonthinking.json",
    default_max_tokens=512,
    model_help="DeepSeek-direct: deepseek-v4-pro (default).",
)

if __name__ == "__main__":
    main(CONFIG, HERE)
