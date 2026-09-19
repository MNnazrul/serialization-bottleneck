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
transcript. The committed outputs in `results/` and `figures/` are the product
of the 2026-09-18 run on the PR #9 dataset. The script exits with an error if
any result record was produced against a different dataset version. To
regenerate them
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
| `results/per_tier_accuracy.csv` | arm (ReAct / matched zero-shot / Experiment-1 zero-shot) × model × scoring (`balanced_bool` / `plain`): simple / medium / hard / overall accuracy + monotonic flag |
| `results/react_failure_analysis.json` | per unresolved V4-Flash ReAct episode: heuristic label (`no_finish_global`, `wrong_finish`, `unparseable_finish`, …) + blank `manual_label` |
| `figures/fig1_react_vs_zeroshot_by_property.png` | grouped bars, zero-shot vs ReAct accuracy, 6 evaluated properties, one panel per model |
| `figures/fig2_locality_delta.png` | ReAct − zero-shot accuracy (pp), local vs global, per model, 95% CI |
| `figures/fig3_react_finish_rate.png` | fraction of ReAct episodes that reach `Finish`, by property |
| `figures/fig4_steps_breakdown.png` | `react_steps` histogram per episode, split finish-correct / finish-wrong / no-finish |

## Grading

Identical to Experiment 1 (PR #9): the same **6 properties** are evaluated,
all by exact match. `triangle_count` and `avg_clustering` are dropped because
both are exactly 0 on every bipartite graph, and with `is_bipartite` at 50/50
per tier "always 0" would score a free ~50%. A ReAct episode that never emits
`Finish` within `--max-steps` is scored **incorrect** (the paper's CoT-SC
back-off is deliberately not used — see `../phase2_react_graph/README.md`).

### Per-tier accuracy: class-balanced booleans

On the pre-PR#9 dataset, the positive rate of `is_planar` differed by tier:
52% true on simple, 21% / 20% on medium / hard, because other families produce
planar graphs at small node counts. Plain accuracy then rewarded a constant
"false" answer more on the harder tiers. That put Llama4-Scout zero-shot's
medium tier (48.8%) above simple (46.8%). PR #9 fixes the data: both booleans
are exactly 50/50 per tier.

The per-tier section still scores each boolean property as the mean of its
true-class and false-class accuracy, computed within each tier and within each
model's own graph set. On the full 300 this now equals plain accuracy. It is
kept as a guard, and it corrects any skew left in the V4-Pro* 60-graph
subsample (the summary prints each set's positives per tier). Tier accuracy is
the mean over the 6 evaluated properties; overall is the mean over the 3 tiers. **Result on the PR #9 dataset (2026-09-18 run).** Both zero-shot arms (matched
control and Experiment 1) are monotonic (simple ≥ medium ≥ hard) for all six
models. ReAct is monotonic for Llama4-Scout, GPT-4.1-mini and Qwen3-32B. For
V4-Flash, V4-Pro\* and Gemini, hard scores +0.8 to +1.7pp above medium. Every
such gap has a 95% graph-level bootstrap CI that includes 0, and the summary
prints it next to the row. The cause is identified: a simple planar graph has
m ≤ 3n−6, and the header gives n and m. So `is_planar` is decidable from the
header alone for 0 / 8 / 19 graphs in simple / medium / hard. ReAct models use
this shortcut (`Nodes[]` → `EdgeCount[]` → `Finish[false]`), which lifts
hard-tier `is_planar` above medium. Excluding those graphs removes most of the
gap. That exclusion is not applied, because it would diverge from Experiment
1's scoring. A dataset-level fix (non-planar graphs constrained to m ≤ 3n−6)
belongs in Phase 1.

## Headline result

ReAct with structural query tools **recovers accuracy on the two local
properties** (`degree_of_node_0`, `edge_count` — one `Degree[0]` / `EdgeCount[]`
call and `Finish`) **but loses accuracy on the global properties**: the model
spends its step budget on one-at-a-time `HasEdge` / `Neighbors` probes and
usually never reaches an aggregate answer, whereas the single-call baseline at
least guesses from the whole edge list in context. On the PR #9 dataset the
direction holds for all six models: local +0.3 to +4.7pp, global −3.3 to
−17.3pp. Two edges are not significant. V4-Flash's local gain is small because
its zero-shot local reads are already 99–100%. Llama4-Scout's global loss has a
CI of [−7.0, +0.5]; it finishes 83% of episodes, far more than the other models.
The pre-PR#9 Gemini exception (local −14.3pp) came from a harness
action-parser bug; see `../phase2_react_graph/README.md` §9. See
`results/evaluation_summary.txt` for the numbers.

## Known limitations

- **Failure labels are heuristic**, same discipline as Experiment 1 —
  `manual_label` is left `null` for the review pass.
- No cross-domain decision readout here; Experiment 2 has only been run on the
  graph domain so far.
