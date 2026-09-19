# Experiment 2 — the geometry serialization bottleneck

Six models run the same 924 multi-step geometry pipelines under four
state-passing conditions. The question: when a step's branch depends on a
property the model must reconstruct from a serialized polygon (area, perimeter,
aspect ratio), does it get the branch right — and does stating the property fix it?

The answer reproduces across every model tested: raw serialization scores
0.04–0.50 on global-property gates, and stating the property lifts it to
0.48–1.00. The gap is 45–66 points and does not shrink with model size — a 1.6T
MoE (V4-Pro) scores *worse* on raw than a much smaller sibling (V4-Flash).

---

## Layout

```
exp1(pipeline)_exp2(react)_geo/
├── .env.example                  API keys — copy to .env, never commit
├── exp2_phase1/                  dataset builder
│   └── Geometry_Experiment2_Phase1.ipynb
└── exp2_phase2/
    ├── geometry_v8_notebooks_<model>/
    │   ├── Geometry_Experiment2_Phase2.ipynb   run the model  (name varies, see table)
    │   ├── Geometry_Experiment2_Phase3_Evaluation.ipynb   score + per-model figures
    │   ├── exp2_runtime.py                     written by the Phase-2 notebook
    │   ├── geometry_exp2_pipelines.json        the dataset
    │   └── results/<run-tag>/                  results.jsonl, results.json, manifest.json,
    │                                           summary_report.txt, errors.md, llm_io.jsonl
    └── model_comparison/
        └── make_comparison_figures.py          all-model figures + table
```

Each model folder is self-contained: its own dataset copy, its own `results/`.
Nothing reads across folders except `model_comparison/`.

| Folder | Phase-2 notebook | `USE_MODEL` | Dataset |
|---|---|---|---|
| `geometry_v8_notebooks_gpt` | `Geometry_Experiment2_Phase2.ipynb` | `gpt-4.1-mini` | full (924) |
| `geometry_v8_notebooks_deepseekv4flash` | `Geometry_Experiment2_Phase2.ipynb` | `deepseek-v4-flash` | full (924) |
| `geometry_v8_notebooks_deepseekv4pro` | `exp2_phase2_v4pro.ipynb` | `deepseek-v4-pro` | **20% subset (186)** |
| `geometry_v8_notebooks_gemini` | `Geometry_Experiment2_Phase2.ipynb` | `gemini-2.5-flash-lite` | full (924) |
| `geometry_v8_notebooks_llama` | `exp2_phase2_llama.ipynb` | `llama-4-scout` | full (924) |
| `geometry_v8_notebooks_qwen` | `exp2_phase2_qwen_nothink.ipynb` | `qwen3-nothink` | full (924) |

> Every notebook's `USE_MODEL` matches its folder. If you reuse a folder's
> notebook for a different model, change `USE_MODEL` back afterwards — the
> results already in `results/` were produced by the model named in each run's
> `manifest.json`, which is the authoritative record, not the notebook's current
> state.

---

## Running a model

1. **Keys.** Copy `.env.example` to `.env`. One `.env` at the repo-folder root or at
   `exp2_phase2/` covers every model folder below it — the config cell searches
   the notebook's folder and up to two parents.

2. **Pick the model.** In the Phase-2 notebook, the config cell has a registry.
   Change one line:

   ```python
   USE_MODEL = "gpt-4.1-mini"     # <- the only line you change
   ```

   Everything else — provider, endpoint, key name, thinking switch, max_tokens,
   output folder — is derived from the preset. `USE_MODEL = "mock"` runs the whole
   harness offline with no API calls, which is the cheapest way to check the setup.

3. **Smoke first.** Set `SMOKE = 3`, Restart Kernel → Run All. 24 units, under a
   minute. Confirm the banner, then check reasoning is actually off:

   ```python
   import json
   rows = [json.loads(l) for l in open("results/<run-tag>/results.jsonl")]
   rt = [s.get("reasoning_tokens") or 0 for r in rows for s in r["steps"] if s.get("scored")]
   print(sum(rt), max(rt))      # want 0; Qwen3 gives 1-2 (empty <think></think>)
   ```

4. **Full run.** `SMOKE = 0`, Run All. 7,392 units (1,488 for the 20% subset).
   Roughly 1–3 hours depending on the provider.

Interrupting is safe. `results.jsonl` is append-only and flushed per unit, so
re-running skips finished work:

```
924 pipelines x 4 conditions x 2 modes = 7392 units; 4486 already done; running 2906
```

A 402 / insufficient-balance error, or a rate limit that survives all retries,
stops the whole run rather than burning the remaining budget against a dead
account. Finished units are kept.

---

## ⚠️ Paths you must change on another machine

Everything below is **relative** and works as-is *provided Jupyter's working
directory is the notebook's own folder* — which is the default for Jupyter Lab
and classic Jupyter. Read this section if you use VS Code, Colab, or run from a
different directory.

### 1. Phase-3 evaluation notebooks — `RESULTS`

`Geometry_Experiment2_Phase3_Evaluation.ipynb` in each model folder has:

```python
RESULTS = "results/<run-tag>/results.json"   # relative to this notebook
```

| Folder | Current value |
|---|---|
| `..._gpt` | `results/gpt-4.1-mini/results.json` |
| `..._deepseekv4flash` | `results/deepseek-v4-flash/results.json` |
| `..._deepseekv4pro` | `results/deepseek-v4-pro/results.json` |
| `..._gemini` | `results/gemini-2.5-flash-lite/results.json` |
| `..._llama` | `results/llama-4-scout/results.json` |
| `..._qwen` | `results/qwen3-nothink/results.json` |

**When to change it:**

* **VS Code**, whose working directory is the workspace root, not the notebook
  folder. Either set `"jupyter.notebookFileRoot": "${fileDirname}"` in settings,
  or make the path absolute for your machine:
  ```python
  RESULTS = "/your/path/exp1(pipeline)_exp2(react)_geo/exp2_phase2/geometry_v8_notebooks_gpt/results/gpt-4.1-mini/results.json"
  ```
* **You scored a different run** — point it at that run tag, e.g.
  `results/qwen3-openrouter/results.json`.
* **`results/` was not committed.** It is large; if the repo ships without it,
  re-run Phase 2 first or point `RESULTS` at wherever you downloaded the data.

The evaluation notebook also writes `fig*.png` into the folder it runs from.

### 2. Phase-2 notebooks — `DATASET`

```python
DATASET = "geometry_exp2_pipelines.json"             # or ..._20pct.json for V4-Pro
```

Relative to the notebook folder; each folder carries its own copy. Change it
only if you move the dataset, or if your working directory is not the notebook's
folder (same VS Code caveat).

Output goes to `results/<RUN_TAG>/`, and `RUN_TAG` is derived from `USE_MODEL`,
so each model writes to its own folder automatically — you do not set it by hand.
The `mkdir` is relative too, so a wrong working directory creates `results/`
somewhere unexpected rather than failing loudly. Check the banner:

```
model=gpt-4.1-mini  workers=4  max_tokens=4096  out=results/gpt-4.1-mini/
```

### 3. Phase-1 notebook

`exp2_phase1/Geometry_Experiment2_Phase1.ipynb` writes the dataset next to
itself. If you rebuild it, copy the output into each model folder — the model
folders do **not** read from `exp2_phase1/`.

### 4. `model_comparison/make_comparison_figures.py`

Resolves its own location, so it needs no editing:

```python
HERE = Path(__file__).resolve().parent     # exp2_phase2/model_comparison
ROOT = HERE.parent                         # exp2_phase2
```

Run it from anywhere:

```bash
python exp2_phase2/model_comparison/make_comparison_figures.py
```

To add or drop a model, edit the `MODELS` dict at the top — label → results
directory, relative to `ROOT`.

### 5. `exp2_runtime.py`

Written by the Phase-2 notebook's `%%writefile` cell into the current working
directory, then imported. Never edit it by hand — edit the notebook cell. If
the import fails, the working directory is wrong (see §1).

---

## Comparison figures

```bash
python exp2_phase2/model_comparison/make_comparison_figures.py
```

Prints a QA table first and refuses to hide two things:

* **Pipeline set.** V4-Pro ran the 20% subset; the others ran all 924. Averaging
  over different pipeline sets is not a comparison, so every model is restricted
  to the intersection (186 pipelines). The footnote states the size.
* **Hidden reasoning.** Every model is checked for reasoning tokens. Anything run
  with thinking on is reported loudly, not quietly plotted.

```
model                     units  pipes   steps  rtok/step  out/vis  thinking
GPT-4.1-mini               7392    924   33544       0.00     1.56x  OK
DeepSeek-V4-Flash          7392    924   33552       0.00     1.75x  OK
DeepSeek-V4-Pro            1488    186    6730       0.00     1.69x  OK
Gemini-2.5-Flash-Lite      7392    924   33077       0.00     1.84x  OK
Llama-4-Scout              7392    924   30803       0.00     1.60x  OK
Qwen3-32B                  7392    924   33080       0.88     2.17x  OK
```

Outputs `fig0_all_models_combined.png`, `fig0a_by_condition.png`,
`fig0b_by_pipeline.png`, `table4_all_models.csv`.

---

## Provider notes (why two models are special)

Hidden reasoning must be **off** — the protocol is direct-answer. Two models
needed work beyond a config flag, and both are worth knowing before you re-run:

**Qwen3-32B.** OpenRouter's `reasoning.enabled=False` is honoured by SiliconFlow
(measured 0 reasoning tokens) but **ignored by DeepInfra** (618 / 310 / 278 / 286
tokens across four flag variants, including `require_parameters=True`).
OpenRouter routes between them per request, so the flag alone gives a silent mix.
The preset therefore also appends `" /no_think"` to the system prompt — Qwen3's
own soft switch, measured to work on both providers. **Qwen is the one model that
does not see the byte-identical prompt**; state this in any write-up.

**Llama-4-Scout.** Non-reasoning model, so no thinking switch is needed. What is
pinned instead is **quantization**: `novita/bf16` with `allow_fallbacks=False`.
DeepInfra serves the same model at fp8, and this experiment measures numeric
reasoning — precisely what quantization could degrade. Without the pin a busy
provider silently reroutes part of the run to fp8.

---

## Stray folders

Not every `results/` subfolder is a real run:

* `*__smoke`, `*__smoke2` — 24-unit smoke tests. Safe to delete.
* `..._gpt/results/gemini-2.5-flash-lite`, `..._llama/results/gemini-2.5-flash-lite`
  — partial copies (4,837–4,872 units, no `manifest.json`). The complete Gemini
  run is in `..._gemini/`.
* `..._qwen/results/qwen3-openrouter`, `qwen3-openrouter_old` — an earlier Qwen
  run collected **without** `/no_think`, with 120 units contaminated by a
  DeepInfra reroute. Superseded by `qwen3-nothink`. Do not merge them: different
  prompt, not comparable.

A real run has `manifest.json` **and** `summary_report.txt` — those are written by
`export()`, which only runs on completion.
