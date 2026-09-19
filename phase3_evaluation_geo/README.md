# `phase3_evaluation_geo` — Experiment 1, Domain 1 (Geometry), Phase 3

Scores the Phase-2 answers against the Phase-1 ground truth, and draws the
figures. **Fully offline** — no API keys, no cost. Reads
`../phase1_dataset_geo/` and `../phase2_model_results_geo/results/`.

Reference spec: `../serialization_experiment_1.pdf`, Section 6.

---

## Running

```bash
pip install numpy scipy matplotlib

cd evaluation
python eval.py --model all              # all six models + cross_model/
python eval.py --model 05_gpt           # one
python eval.py --model all --skip 01_v4flash

cd ../figures
python fig_grid.py                      # 5% band, one panel per model
python fig_grid_bands.py                # each property at the band that fits it
python fig_tol1pct.py                   # everything at the strict 1% band
python fig_bands.py                     # all three bands, and the primary-band pick
python fig_area_perimeter_diagnosis.py  # why area and perimeter are flat
```

**Run `eval.py` before the figure scripts** — `fig_grid.py` reads each model's
`accuracy_matrix.json`.

## What it produces

```
evaluation/<model>/accuracy_matrix.json   every number, machine-readable
evaluation/<model>/console_output.txt     the full report as printed
evaluation/<model>/fig1..fig9.png         per-model figures
evaluation/cross_model/                   two cross-model figures
figures/*.png                             six cross-model grid and diagnostic figures
```

Everything under `evaluation/<model>/`, `evaluation/cross_model/` and
`figures/*.png` is **generated** and safe to delete — it all rebuilds from the
code here.

---

## The headline result

Local-vs-global gap, PDF §6.3's primary bottleneck signal. §9 calls a gap under
15 pp "smaller than expected":

```
model                     local   global      gap    coverage
DeepSeek-V4-Flash          66.3     33.4   +33.0 pp   full
Qwen3-32B                  59.5     26.4   +33.1 pp   full
Llama-4-Scout              51.3     20.8   +30.6 pp   full
Gemini-2.5-Flash-Lite      83.8     27.2   +56.6 pp   full
GPT-4.1-mini               84.3     27.1   +57.2 pp   full
DeepSeek-V4-Pro (20%)      59.2     30.0   +29.2 pp   20% subsample
```

Cross-model Spearman **ρ = 0.882** (0.800–0.996): the difficulty ordering of
properties is model-independent, which is what makes it a property of the
serialization rather than of any one model. V4-Pro vs V4-Flash is **0.996** —
same provider, same ordering, 6× the parameters.

**Scale does not close the gap.** V4-Pro's +29.2 pp sits in the same band as a
32B open model's.

---

## Four things to know before reading the numbers

**1. V4-Pro ran on 20 polygons per tier, not 100.** One polygon moves a tier
accuracy by 5 percentage points there and 1 point elsewhere. `eval.py` prints
this warning in its own report, and the figure titles carry `n=20/tier`. Never
compare its per-tier column to the other five without it.

**2. `vertex_count` mostly measures instruction-following, not counting.**
WKT closes the ring, so `POLYGON((a,b,c,a))` lists four coordinates for three
vertices, and the prompt says *"Do not count the repeated closing coordinate"*
(PDF Table 5). Answering n+1 means the model counted correctly and ignored the
instruction:

```
              exact    +1 (counted the closing coord)   total within +-1
V4-Flash        33               38                          71
Qwen3-32B       23               42                          65
Llama4-Scout    14               42                          56
Gemini2.5-FL    71                0                          71
GPT-4.1-mini    69               20                          89
V4-Pro (20%)    18               69                          87
```

V4-Pro looks worst on `vertex_count` and is **second-best at actually
counting**. Since `vertex_count` is one of only two *local* properties, this
drags its whole local average — and therefore its gap — down for a reason that
is not geometric. The split is drawn in
`figures/property-recovery-accuracy-by-tier-and-model-fitted-bands.png`.

**3. `area` and `perimeter` accuracy here is not evidence of computation.**
A model answering a constant multiple of the bounding box, reading no vertices,
scores 57/59/64 on area and 98/84/64 on perimeter at the 10% band. Every model
scores **below** those ceilings. The dataset publishes only the weaker `k=1`
version of these floors, so `eval.py`'s margin-over-floor numbers for those two
are **optimistic**. Stated again in `eval.py`'s own docstring.

**4. Report `centroid` at the 1% band.** At 5% it is won by answering the mean
of the serialized coordinates, whose own pass rate *rises* 62 / 84 / 88 across
tiers — which inverts the centroid gradient through a Simpson's paradox.

---

## What `eval.py` reports, per model

Following PDF §6.3:

| section | what |
|---|---|
| Run integrity | records, distinct polygons, parse failures, truncation, band coverage |
| **P1** | per-property accuracy by tier, with bootstrap p and Holm correction |
| P1b | the dataset's own expectations, printed against what was observed |
| **P2 / P3** | local vs global, per tier and pooled — the primary signal |
| P4 | centroid by offset band — the shortcut control |
| P5 | accuracy against the answer-without-reading floor |
| P6 | all three tolerance levels (PDF Table 13), not just 5% |
| P7 | boolean precision / recall / confusion matrix (§6.2) |

Statistics per §6.4: 95% bootstrap confidence intervals, 10,000 resamples,
two-sided bootstrap tests, Holm-corrected across the nine properties.

---

## Pipeline

```
../phase1_dataset_geo/            the 300 polygons + ground truth
        |
../phase2_model_results_geo/      six models queried -> results/
        |
phase3_evaluation_geo/            <- you are here: score and draw
```
