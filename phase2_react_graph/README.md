# Experiment 2 — ReAct on Domain 2 (Graphs)

An **additional evaluation experiment** on top of Experiment 1's graph domain.
Same 300-graph dataset, same 8 questions, same ground truth, same scoring — but
each question is answered with an interleaved **Thought / Action / Observation**
loop (Yao et al., 2023, *"ReAct: Synergizing Reasoning and Acting in Language
Models"*, `2210.03629v3.pdf`) instead of a single zero-shot call.

The question this answers: Experiment 1 shows a model loses accuracy reading a
property off a serialized graph. Does letting it *act* on the structure —
querying neighbours, degrees and edges step by step — recover that accuracy?

**Nothing here re-implements Experiment 1.** `_common/react_graph_harness.py`
imports [`../phase2_model_results_graph/_common/graph_harness.py`](../phase2_model_results_graph/_common/graph_harness.py)
and reuses it wholesale — dataset loading/validation, subset filtering, the
`(object_id, property)` resume index, the uncertified-`chromatic_number`
exclusion, `normalize_answer`, `add_metrics`, `classify_failure`, the retry
policy, the provider table, client construction, the thinking-disable
mechanism, logging, JSON export, the cost summary, and the CLI. Only the call
changes: one API call per question becomes a capped ReAct loop. No file
outside `phase2_react_graph/` is modified.

**No API calls have been made yet.** The harness is validated offline
(`_common/selftest_react.py`, all six `--dry-run`). Running the models needs
your keys in `.env` (root README §"API keys") and is a paid operation.

---

## 1. Folder layout

| Folder | Model | Coverage | Episodes |
|--------|-------|----------|----------|
| `01_v4flash/` | DeepSeek-V4-Flash | full 300 | 2,399 |
| `02_qwen3/` | Qwen3-32B | full 300 | 2,399 |
| `03_llama_scout/` | Llama 4 Scout | full 300 | 2,399 |
| `04_gemini/` | Gemini 2.5 Flash-Lite | full 300 | 2,399 |
| `05_gpt/` | GPT-4.1 Mini | full 300 | 2,399 |
| `06_v4pro_nonthinking/` | DeepSeek-V4-Pro, thinking OFF | 20% subsample (60) | 480 |
| `_common/` | *(not a model)* | — | shared harness + offline self-test |

Same models, providers, prices, coverage, and 20%-subsample selection as
[`../phase2_model_results_graph/`](../phase2_model_results_graph/README.md).
2,399 = 300 × 8 − 1 uncertified `chromatic_number` pair (0 on the committed
dataset — the count is 2,399 only because `build_tasks` still runs the check).
`06`'s `build_subsample.py` and `subsample_v4pro_nonthinking.json` are copied
verbatim from the Experiment-1 graph folder, so the same 60 graphs are used
(seed 42); `python build_subsample.py --verify` confirms it.

An "episode" is one ReAct loop for one `(graph, property)` pair. Each model
folder writes, on first run:

```
<key>_results.jsonl            append-only, one line per episode
<key>_results.json             JSON array, latest record per (object_id, property)
logs/<key>_<timestamp>.log     per-run log  (logs/ is gitignored)
```

`<key>` matches Experiment 1 (`v4flash`, `qwen3`, ...) so results
cross-reference by name; the paths differ, so there is no collision.

---

## 2. The ReAct loop (`_common/react_graph_harness.py`)

```
parse the edge_list -> adjacency  (no networkx: the tools just read the serialization)
build the initial prompt (instructions + 2 exemplars + this graph + question)
loop, up to --max-steps (default 15):
    call the model, stop=["\nObservation"]     # it emits only Thought + Action
    parse the LAST "Action i: Name[arg]" line
    Finish[x]  -> normalize_answer(x, property); stop
    tool       -> run it, append "Observation i: ..." , continue
    unparsable -> append an "invalid action" observation, continue
score Finish's answer with the reused add_metrics; write one record
```

Turns are alternating chat messages (first user = full prompt, then
assistant / user / assistant / ...). The `stop` sequence is the paper's setup
— the model never writes its own observations. If the step cap is hit with no
`Finish`, the episode is recorded `parse_success: false`,
`failure_type: "no_finish"`, `correct: false` (an unresolved question is
*incorrect*, not dropped — same discipline as Experiment 1; the paper's CoT-SC
back-off is deliberately **not** used).

### Action space — structural queries + Finish

The analog of the paper's `search` / `lookup` / `finish`. Every tool reads
**raw structure only**; no graph property is computed for the model.

| Action | Observation |
|--------|-------------|
| `Neighbors[n]` | `Node n has neighbors: [...]` — or `Node n is not in the graph (nodes are 0..N-1).` |
| `Degree[n]` | `Node n has degree D.` |
| `HasEdge[u, v]` | `Edge (u, v) is present.` / `... is not present.` |
| `Nodes[]` | `Nodes: [0, ..., N-1]  (N nodes total).` |
| `EdgeCount[]` | `The graph has M edges.` (the `GRAPH (n=.., m=..)` header) |
| `Finish[x]` | ends the episode; `x` goes through the Experiment-1 `normalize_answer` |

Action-name matching is case-insensitive and tolerates a few aliases
(`has_edge`, `edge_count`, ...). An unknown or malformed action is not fatal —
the model gets an "invalid action" observation listing the valid set and may
recover (matches the paper tolerating a bad step).

### The prompt

`instructions + 2 hand-written exemplars + "Graph:\n{edge_list}\n\nQuestion:
{question}\nThought 1:"`. Few-shot, faithful to the paper's Appendix C
`Thought i / Action i / Observation i` format. The two exemplars are fixed
tiny graphs: one local property (`Degree[0]` → `Finish`), one global
(`triangle_count` via `Neighbors` / `HasEdge` → `Finish`). See
`REACT_INSTRUCTIONS`, `_EXEMPLAR_1`, `_EXEMPLAR_2` in the harness.

Zero-shot ablation, or fewer/more exemplars: not exposed as a flag — edit the
constants if you want to compare.

---

## 3. Per-model configuration

Copied field-for-field from the six Experiment-1 graph `ModelConfig` blocks —
model id, provider, prices, thinking-disable mechanism, coverage,
`06`'s subset. Shared: **temperature 0, max_tokens 512 per turn, max 15 steps,
concurrency 5, thinking disabled, no system prompt**.

| | 01 V4-Flash | 02 Qwen3-32B | 03 Llama-4 Scout | 04 Gemini 2.5 FL | 05 GPT-4.1 Mini | 06 V4-Pro (no-think) |
|---|---|---|---|---|---|---|
| model id | `deepseek-v4-flash` | `qwen/qwen3-32b` | `meta-llama/llama-4-scout` | `gemini-2.5-flash-lite` | `gpt-4.1-mini` | `deepseek-v4-pro` |
| provider | deepseek | openrouter | openrouter | google | openai | deepseek |
| key env | `DEEPSEEK_API_KEY` | `OPENROUTER_API_KEY` | `OPENROUTER_API_KEY` | `GEMINI_API_KEY` | `OPENAI_API_KEY` | `DEEPSEEK_API_KEY` |
| thinking off via | `extra_body thinking:disabled` | `extra_body reasoning:false` **+** `/no_think` | native | `reasoning_effort="none"` | native | `extra_body thinking:disabled` |
| coverage | 300 | 300 | 300 | 300 | 300 | 60 (20%) |
| price in/out ($/1M) | 0.14 / 0.28 | 0.08 / 0.28 | 0.10 / 0.30 | 0.10 / 0.40 | 0.40 / 1.60 | 0.435 / 0.87 |

**Difference from Experiment 1:** `03`/`04` used `response_format`
JSON-object mode there; it is dropped here (all six `supports_json_mode=False`)
because ReAct output is free-form Thought/Action text, not one JSON object. The
per-turn `stop` sequence bounds each turn instead. `06` is off the PDF's
literal thinking-mode-V4-Pro spec, exactly mirroring Experiment 1's own
documented deviation, so the two experiments stay comparable model-for-model.

---

## 4. Running

Same three-step pattern as Experiment 1, from inside each model folder. Every
run is resumable by `(object_id, property)` — re-issue the same command after
an interruption and it continues.

```bash
cd phase2_react_graph/01_v4flash
python run_v4flash_react.py --dry-run                 # prints the ReAct prompt + episode count, no API calls
python run_v4flash_react.py --limit 5 --fail-fast     # 5 graphs x 8 props, sequential, stop at first failure
python run_v4flash_react.py                            # full run, concurrent, resumable
```

Repeat for `02`–`05` (each with its provider's key). `06`:

```bash
cd phase2_react_graph/06_v4pro_nonthinking
python build_subsample.py --verify          # confirm the 60 ids match Experiment 1
python run_v4pro_nonthinking_react.py --dry-run
python run_v4pro_nonthinking_react.py
```

After a run, commit `<key>_results.json` and `<key>_results.jsonl`, exactly as
`phase2_model_results_graph/`.

### Flags

The full Experiment-1 CLI (`--dataset`, `--subset`, `--jsonl-output`,
`--json-output`, `--log-dir`, `--provider`, `--model`, `--temperature`,
`--max-tokens`, `--concurrency`, `--thinking`, `--properties`, `--fail-fast`,
`--price-in`/`--price-out`, `--limit`, `--force`, `--retry-parse-failures`,
`--dry-run`) plus:

| Flag | Default | Purpose |
|------|---------|---------|
| `--max-steps` | `8` | Thought/Action/Observation steps before an episode gives up |

`--json-mode` is **not** present (no model uses it here).

### Rough cost

A ReAct episode is roughly **30–100× the input tokens and 50–150× the output
tokens** of an Experiment-1 single call (an ~800–1,800-token prompt re-sent and
growing over 3–8 turns). Very approximate full-run totals — **measure with
`--limit 5` before committing to a full run**:

| Model | Full-run episodes | ≈ cost |
|-------|-------------------|--------|
| 01 V4-Flash | 2,399 | ~$2–6 |
| 02 Qwen3-32B | 2,399 | ~$1.5–5 |
| 03 Llama-4 Scout | 2,399 | ~$2–6 |
| 04 Gemini 2.5 FL | 2,399 | ~$2–6 |
| 05 GPT-4.1 Mini | 2,399 | ~$7–20 |
| 06 V4-Pro (no-think) | 480 | ~$1.5–4 |

The printed cost summary at the end of each run reports the **real** token
usage and cost from the API.

---

## 5. Result record shape

Every Experiment-1 field is present and computed by the same code, so
[`../phase3_evaluation_graph/`](../phase3_evaluation_graph/README.md) reads
these records unchanged (it already uses `.get()` for optional fields). ReAct
adds four fields:

```jsonc
{
  // --- identical to Experiment 1 (copied from the Phase-1 record) ---
  "object_id": "graph_simple_erdos_renyi_001",
  "tier": "simple", "family": "erdos_renyi",
  "num_nodes": 13, "num_edges": 19,
  "property": "triangle_count",
  "property_locality": "global",
  "ground_truth": 4,
  "prompt": "You are given an undirected graph ...\nThought 1:",   // the initial ReAct prompt
  "raw_model_output": "... full Thought/Action/Observation transcript ...",
  "parsed_answer": 4,
  "parse_success": true,
  "finish_reason": "stop",
  "failure_type": null,          // "no_finish" | "parse_failure" | "reasoning_truncated" | null
  "model": "deepseek-v4-flash", "provider": "deepseek", "temperature": 0.0,
  "prompt_tokens": 5120,         // summed across all turns of the episode
  "completion_tokens": 340,      // summed across all turns
  "timestamp": "...",
  "absolute_error": 0, "correct": true,   // same per-property metric fields as Experiment 1

  // --- ReAct-specific ---
  "method": "react",
  "react_steps": 4,             // Thought/Action/Observation steps taken
  "react_finished": true,       // did the model call Finish[...] within --max-steps
  "react_tool_calls": [ { "step": 1, "action": "neighbors", "arg": "0", "observation": "Node 0 has neighbors: [8, 9]." }, ... ]
}
```

`failure_type: "no_finish"` is new to this experiment (step cap hit without a
`Finish`). `parse_failure` here means `Finish[...]` was reached but its
argument did not normalize to a valid answer for that property.

---

## 6. Verification (all offline, no API key)

```bash
python phase2_react_graph/_common/selftest_react.py     # 32 checks: tools, loop, step cap, scoring
cd phase2_react_graph/01_v4flash && python run_v4flash_react.py --dry-run
cd phase2_react_graph/06_v4pro_nonthinking && python build_subsample.py --verify
```

`git diff --stat` on any pre-existing file is empty — this experiment is
purely additive.

---

## 7. Zero-shot control arm

Each model folder also has `run_<key>_zeroshot.py` — the matched baseline:
the **same** models, dataset, questions and scoring, but Experiment 1's single
direct-answer call (`Respond with only {"answer": <your answer>}`) instead of
the ReAct loop. It imports the Experiment-1 harness
(`../phase2_model_results_graph/_common/graph_harness.py`) unchanged and only
renames its output to `<key>_zeroshot_results.json[l]`, so it sits beside the
ReAct results without colliding. Run it in the same session as the ReAct arm so
the comparison is not confounded by model-snapshot drift.

```bash
cd phase2_react_graph/01_v4flash
python run_v4flash_zeroshot.py --dry-run
python run_v4flash_zeroshot.py
```

This is the same method Experiment 1 already ran, reproduced here only to give
Experiment 2 a self-contained, same-session baseline. Where credits stop a
matched run, `../phase3_evaluation_react_graph/` falls back to Experiment 1's
committed zero-shot results for that model.

## 8. Phase 3

[`../phase3_evaluation_react_graph/`](../phase3_evaluation_react_graph/README.md)
scores ReAct against the zero-shot arm — per property, per locality, with
bootstrap CIs and figures. The `method`, `react_steps`, `react_finished` and
`react_tool_calls` fields feed that analysis (accuracy vs. step count, finish
rate, failure taxonomy).
