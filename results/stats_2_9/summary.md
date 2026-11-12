# Task 2.9: statistics on the finalized results

Data: `main.csv` from `results/FINALIZED_RESULTS/analysis/` (6600 calls; 13 `generation_failed` calls dropped, leaving 6587). 11 models, 200 cases, efforts low / medium / high. Correct means `semantic_ok` = 1. All tables are in this folder as CSV.

Cross-check: accuracy recomputed from `main.csv` matches `main_sessions.csv` for 33 of 33 sessions.

## 1. Effort vs accuracy (table_2_effort.csv)

Pooled over all models: low 63.1%, medium 70.4%, high 68.3%. Pooling mixes models, so the test is run within each model: Cochran's Q on the same cases at the three efforts, Holm-adjusted across the 11 models. table_2_effort.csv also has the team's exact McNemar p for low vs medium and medium vs high (pipeline/stats.py).

| Model | Low | Medium | High | Best | p (Holm) |
|---|---|---|---|---|---|
| gemini-4-argon | 83.5% | 87.0% | 89.5% | high | 0.008 |
| gemini-3.5-flash-lite | 58.5% | 83.6% | 81.5% | medium | < 0.001 |
| nemotron-3.5-super-vl | 88.5% | 82.0% | 85.0% | low | 0.113 |
| qwen3.8-flash | 78.5% | 80.0% | 78.5% | medium | 1.000 |
| gpt-6.1-terra | 81.0% | 80.0% | 80.5% | low | 1.000 |
| gemini-3.8-flash | 62.0% | 79.5% | 71.5% | medium | < 0.001 |
| gpt-6.1-sol | 73.2% | 78.8% | 69.7% | medium | < 0.001 |
| gpt-6-luna | 78.5% | 76.5% | 80.0% | high | 1.000 |
| qwen3.8-27b | 30.9% | 64.4% | 60.8% | medium | < 0.001 |
| nemotron-3-ultra-550b-a55b | 34.0% | 34.0% | 26.0% | low | 0.367 |
| nemotron-3.5-lightning | 24.0% | 27.5% | 28.0% | high | 1.000 |

Significant effort effect (Holm p < 0.05) for 5 of 11 models: gemini-4-argon, gemini-3.5-flash-lite, gemini-3.8-flash, gpt-6.1-sol, qwen3.8-27b.

## 2. Latency vs accuracy (table_3_latency.csv)

Spearman correlation between `latency_ms` and correctness within each model x effort session (33 sessions, Holm-adjusted). Negative means slower answers were more often wrong. Significant negative: 6; significant positive: 1; not significant: 26.

Across sessions, the typical median latency was 12.2 s for right answers and 13.6 s for wrong answers.

## 3. Thinking (reasoning tokens) vs accuracy (table_4_reasoning_tokens.csv)

Same method with `reasoning_tokens` (the guide says to compare thinking by tokens, not characters). Significant negative: 11; significant positive: 1; not significant or no data: 21 of 33 sessions.

## 4. Case type vs accuracy (table_5_groups*.csv, table_6_checks.csv)

| Group | Calls | Accuracy (95% Wilson CI) | Chance | Lift over chance |
|---|---|---|---|---|
| Request constraints | 1054 | 56.1% (53.1%-59.0%) | 26.9% | +29.2 pts |
| Days | 527 | 56.4% (52.1%-60.5%) | 21.2% | +35.2 pts |
| Completeness of the answer | 528 | 65.7% (61.6%-69.6%) | 26.7% | +39.0 pts |
| Course and section identity | 792 | 69.4% (66.1%-72.6%) | 44.8% | +24.7 pts |
| Capacity | 656 | 71.2% (67.6%-74.5%) | 18.4% | +52.8 pts |
| Student record | 1710 | 71.5% (69.3%-73.6%) | 33.2% | +38.3 pts |
| Temporal reasoning | 1320 | 72.3% (69.8%-74.6%) | 36.4% | +35.8 pts |

Chi-square test of group vs correctness, pooled: chi2 = 124.4, dof = 6, p = 2e-24. Within single models, group matters significantly (Holm p < 0.05) for 7 of 11 models.

Hardest single checks (follow-up only: each check has just 4 to 6 cases, so these are uncertain):

- K8 Ambiguous request -> ask or guess: 17.4% (chance 26.1%, 4 cases)
- K4 Several constraints at once: 23.5% (chance 24.9%, 4 cases)
- I5 Negation ("not section X"): 25.8% (chance 50.8%, 4 cases)
- T9 AM/PM and noon traps: 38.6% (chance 32.0%, 4 cases)
- O3 Accepts any of several correct answers: 43.9% (chance 37.3%, 4 cases)

## 5. Case difficulty vs accuracy (table_7_case_features.csv, table_7b_by_rules.csv)

Each case counted once: its accuracy over all sessions that answered it, against features of the case (Spearman over the cases, Holm-adjusted).

| Feature | rho | p (Holm) |
|---|---|---|
| rules in the request (`n_rules`) | -0.12 | 0.357 |
| words in the request (`prompt_words`) | -0.09 | 0.617 |
| courses on the transcript (`transcript_courses`) | +0.03 | 1.000 |
| random-guess odds (`chance`) | +0.05 | 1.000 |

Mean case accuracy by number of rules in the request:

| Rules | Cases | Mean accuracy |
|---|---|---|
| 1 | 2 | 95.5% |
| 2 | 77 | 72.6% |
| 3 | 100 | 63.5% |
| 4 | 15 | 71.1% |
| 5 | 6 | 41.9% |

## Limits

- One sample per case per effort; the 3-sample stability run is in `check.csv` and is not used here.
- Tyler revised the D-category cases after this run (PR #7); these results use the earlier D cases.
- Latency includes provider queueing. The free Nemotron models are rate-limited and Lightning's run was resumed, so their latencies are not comparable with paid models.
- OpenAI and Gemini return a reasoning summary; token counts are used for comparison.
- Correlation is not causation: harder cases can make a model both slower and more often wrong.
- Spearman on a 0/1 outcome is a rank correlation; it says whether wrong answers tend to be slower, not by how much.
