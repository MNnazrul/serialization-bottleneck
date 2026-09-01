# Phase 3 — Experiment 2 (ReAct) Evaluation, Domain 2 (Graphs)

Evaluation layer for Experiment 2. Consumes the Experiment-2 Phase-2 logs and
produces the same kind of deliverables as
[`../phase3_evaluation_graph/`](../phase3_evaluation_graph/README.md) — an
accuracy readout, the local-vs-global contrast, figures, a failure-mode pass,
and a saved summary — but the comparison here is **ReAct vs a matched
single-call zero-shot baseline**, not zero-shot vs difficulty.

```
phase2_react_graph/                      ->  phase3_evaluation_react_graph/
  ReAct arm: <key>_results.json               tables, ReAct-vs-zeroshot deltas,
  zero-shot arm: <key>_zeroshot_results.json  figures, failure analysis,
                                              evaluation_summary.txt
```

Inputs, read in place (nothing copied):

| Arm | Path | Notes |
|---|---|---|
| ReAct | `../phase2_react_graph/<NN>/<key>_results.json` | all six models, full coverage (300 graphs; V4-Pro* on the 60-graph subsample) |
| Zero-shot (matched) | `../phase2_react_graph/<NN>/<key>_zeroshot_results.json` | Experiment 1's method exactly, re-run in the same session as the ReAct arm — all six models |
| Zero-shot (fallback) | `../phase2_model_results_graph/<NN>/<key>_results.json` | Experiment 1's committed zero-shot; the script uses it only if a matched control file is missing |

## Run

```bash
source .venv/bin/activate      # from the repo root
python phase3_evaluation_react_graph/evaluate_react.py
```

The raw Phase-2 result files this reads (`../phase2_react_graph/<NN>/*_results.json[l]`)
are **gitignored** — each ReAct record carries a full Thought/Action/Observation
transcript, ~240 MB across the six models. The committed outputs in `results/`
and `figures/` are the frozen product of the 2026-08-30 run; to regenerate them
you first re-run the Phase-2 runners
(`../phase2_react_graph/<NN>/run_*_react.py` and `run_*_zeroshot.py`, resume-safe)
to rebuild those logs, then run this script.

Pure Python + numpy + matplotlib (in the root `requirements.txt`). Re-runnable
any time; it regenerates every output. Unlike the Experiment-1 domains this is
a script, not a notebook — the analysis is a single fixed comparison, so there
is nothing to step through interactively.

## Outputs

| File | Contents |
|---|---|
| `results/evaluation_summary.txt` | Headline (local vs global ReAct effect, per model, with 95% bootstrap CI), per-property table, mechanism, loop-behaviour stats, failure taxonomy |
| `results/comparison_by_property.csv` | model × property: n, ReAct acc, ReAct finish rate, zero-shot acc, delta |
| `results/react_failure_analysis.json` | per unresolved V4-Flash ReAct episode: heuristic label (`no_finish_global`, `wrong_finish`, `unparseable_finish`, …) + blank `manual_label` |
| `figures/fig1_react_vs_zeroshot_by_property.png` | grouped bars, zero-shot vs ReAct accuracy, 8 properties, one panel per model |
| `figures/fig2_locality_delta.png` | ReAct − zero-shot accuracy (pp), local vs global, per model, 95% CI |
| `figures/fig3_react_finish_rate.png` | fraction of ReAct episodes that reach `Finish`, by property |
| `figures/fig4_steps_breakdown.png` | `react_steps` histogram per episode, split finish-correct / finish-wrong / no-finish |

## Grading

Identical to Experiment 1: exact match for the integer and boolean properties,
1% relative error for `avg_clustering`. A ReAct episode that never emits
`Finish` within `--max-steps` is scored **incorrect** (the paper's CoT-SC
back-off is deliberately not used — see `../phase2_react_graph/README.md`).

## Headline result

ReAct with structural query tools **recovers accuracy on the two local
properties** (`degree_of_node_0`, `edge_count` — one `Degree[0]` / `EdgeCount[]`
call and `Finish`) **but loses accuracy on the global properties**: the model
spends its step budget on one-at-a-time `HasEdge` / `Neighbors` probes and
usually never reaches an aggregate answer, whereas the single-call baseline at
least guesses from the whole edge list in context. Loop overhead can also hurt
a format-fragile model on an otherwise trivial local read. See
`results/evaluation_summary.txt` for the numbers.

## Known limitations

- **Failure labels are heuristic**, same discipline as Experiment 1 —
  `manual_label` is left `null` for the review pass.
- No cross-domain decision readout here; Experiment 2 has only been run on the
  graph domain so far.
