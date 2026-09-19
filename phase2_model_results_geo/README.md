# `phase2_model_results_geo` — Experiment 1, Domain 1 (Geometry), Phase 2

Queries six models on the 300-polygon dataset and records every answer.
**13,500 + 540 = 14,040 graded answers**, already committed in `results/`.

**You do not need to run this** to reproduce the analysis — `results/` is
populated and `../phase3_evaluation_geo/` reads it directly. Re-running costs
money and appends to the same files.

Reference spec: `../serialization_experiment_1.pdf`, Sections 2, 3, 5.

---

## Layout

```
runners/<model>/run_<model>_full.py   one query runner per model
runners/run_all.py                    orchestrator: one child process per model
runners/<model>/logs/                 per-run debug log, one file per invocation
runners/logs_all/                     child stdout/stderr from run_all.py
results/<model>/<model>_results.jsonl append-only, one record per query
results/<model>/<model>_results.json  the same data as a single array
```

## The six models

| runner | model | provider | key | coverage |
|---|---|---|---|---|
| `01_v4flash` | `deepseek-v4-flash` | DeepSeek | `DEEPSEEK_API_KEY` | 300 polygons |
| `02_qwen3` | `qwen/qwen3-32b` | OpenRouter | `OPENROUTER_API_KEY` | 300 |
| `03_llama_scout` | `meta-llama/llama-4-scout` | OpenRouter | `OPENROUTER_API_KEY` | 300 |
| `04_gemini` | `gemini-2.5-flash-lite` | Google | `GEMINI_API_KEY` | 300 |
| `05_gpt` | `gpt-4.1-mini` | OpenAI | `OPENAI_API_KEY` | 300 |
| `06_v4pro_nonthinking` | `deepseek-v4-pro` | DeepSeek | `DEEPSEEK_API_KEY` | **60 (20% subsample)** |

Settings, per PDF §2: **temperature 0**, **non-thinking**, **max_tokens 512**,
no system prompt, zero-shot direct answer, one query per (polygon, property).

`300 × 9 = 2700` records per model for the first five; `60 × 9 = 540` for
V4-Pro.

---

## Running

```bash
pip install openai python-dotenv tenacity tqdm

cp ../.env.example .env        # or put .env at the repository root
                              # then fill in the keys you need

cd runners
python run_all.py --dry-run   # build every prompt, call nothing
python run_all.py --smoke     # 5 polygons x 9 properties per model
python run_all.py             # full run: 2700 queries x 5 models
```

`run_all.py` launches one child process per model, each with its own provider,
key and output file, so a failure in one cannot corrupt another. Child output
goes to `runners/logs_all/`, not the terminal.

```bash
python run_all.py --only 05_gpt 02_qwen3
python run_all.py --skip 03_llama_scout
python run_all.py -- --force          # everything after -- goes to each runner
```

### `.env`

`load_dotenv()` searches **upward** from each runner, so a `.env` in this
folder and one at the repository root both work — but the nearer one wins and
the other silently does nothing. Keep one.

A missing key fails at startup with `<KEY_NAME> not set. Add it to .env.`
before any request is sent, so a typo costs nothing.

### Resume — this is what makes a re-run safe

Every runner appends to its `.jsonl` and **skips `(object_id, property)` pairs
it has already logged**. So:

- re-running after a crash continues where it stopped
- a finished model is a no-op
- `--dry-run` on a complete run reports **"0 queries would be sent"**, which is
  also the fastest check that the paths resolve

A failed query is **not** written as a record, so a partial run leaves no
phantom rows to clean up.

### `06_v4pro_nonthinking` — the 20% arm

Not part of `run_all.py`. It queries only the 60-polygon stratified subsample
built in `../phase1_dataset_geo/`, per PDF §3.

```bash
cd runners/06_v4pro_nonthinking
python run_v4pro_nonthinking_full.py --dry-run
python run_v4pro_nonthinking_full.py --limit 3     # 27 queries
python run_v4pro_nonthinking_full.py               # 540 queries
```

**One deliberate departure from the PDF.** §3 specifies V4-Pro as the
*thinking-mode* arm (provider-default thinking, max_tokens 2048). This runner is
the **non-thinking** companion at 512 tokens, matching the other five exactly.

That is the point of the arm rather than an oversight: thinking-mode V4-Pro
would confound two changes at once — a bigger model **and** a different
inference regime. Holding temperature, token budget, prompt and parser identical
leaves model size as the only variable. Every record stores its own
`max_tokens` and `temperature`, so this arm and a future thinking-mode arm stay
separable.

---

## The record schema

```
object_id, tier, shape_type, num_vertices        the polygon
property, property_locality, ground_truth        the question
prompt                                           exactly what was sent, WKT included
raw_model_output, parsed_answer, parse_success   what came back
finish_reason, max_tokens, failure_type          harness state
model, provider, temperature, timestamp          the call
absolute_error, relative_error                   numeric properties
correct_1pct, correct_5pct, correct_10pct        numeric (PDF Table 13)
correct                                          integer / boolean / categorical
offset_band, fill_band, centroid_offset_norm,
vertex_mean_offset_norm, fill_ratio, family, ... dataset design fields, carried
                                                 so Phase 3 need not re-join
```

Those trailing design fields matter: without them the evaluation cannot tell an
absent field from a genuine zero, and silently disables the centroid shortcut
analysis. All six runners carry them.

---

## What this run found

```
model                     records   parse failures   truncated
DeepSeek-V4-Flash            2700                8           8
Qwen3-32B                    2700                0           0
Llama-4-Scout                2700                0           0
Gemini-2.5-Flash-Lite        2700                0          25
GPT-4.1-mini                 2700                0           0
DeepSeek-V4-Pro (20%)         540                0           0
```

All well under PDF §6.1's 15%-per-property investigate threshold.

Scores are in `../phase3_evaluation_geo/`.
