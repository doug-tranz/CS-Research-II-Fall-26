# The results file

# Read the results

`run_semantic_eval.py` prints one line per case (one per sample when `-n` is above 1), then
a total:

```
pair-transfer-philosophy-ba-b: semantic_ok          | satisfies every rule

5/6 semantic_ok on anthropic/claude-sonnet-5.5. Cost $1.4941. Full results in pairs_sonnet_trimmed.json (session trimmed-20261019T225009Z)
Paired accuracy: 2/3 pairs with both variants semantic_ok (67%)
```

| Outcome | Tier | Meaning |
|---|---|---|
| `syntactic_error` | 1 | Not a DIDComm message: not valid JSON (e.g. the model wrapped it in a ```` ```json ```` code block, or returned nothing), or no `id`, or a malformed `type` |
| `schema_error` | 2 | A DIDComm message, but not our register message: wrong `type`, or the body isn't exactly `student_id`, `term_code` and a `sections` list of section objects |
| `consistency_error` | 3 | Breaks a rule, e.g. full section, time conflict, unmet prerequisite |
| `semantic_error` | 4 | Passes every rule, but the cart is not one the intent asked for |
| `semantic_ok` | 4 | Correct |
| `GENERATION FAILED` / `generation_failed` | | The call itself failed; the error message says why |

Where the full results go: `run_semantic_eval.py` → `semantic_eval_results.json` (or
`--output`), one indented JSON object, `{"sessions": [...], "results": [...]}`. Each run of the
script is one session. What every call of a session shares is written once, in its summary;
each result row keeps only what is its own and names its `session_id`. Nothing is stored twice,
except `deviation_score` (per row, and averaged in the summary).

**`sessions[]`, one summary per run.**
- **Settings (to repeat the run):** `session_id`, `model`, `test_file` and `test_file_sha256`,
  `code_version` (git `commit`, `branch`, and `uncommitted_changes` with the `changed_files`;
  with uncommitted changes the commit alone does not reproduce the run), `tally_variant` (null
  with `--named-courses`), `rendering`, `samples_per_case`, `cache_ttl`,
  `generation_settings` (exactly what every call sent: `temperature`, `stream`, `usage`, and
  `max_tokens` / `reasoning.effort` when `--max-tokens` / `--effort` are given; otherwise the
  provider's defaults apply), `shared_context` (the cached opening's size and hash) and
  `output_contract`.
- **Correctness:** `cases`, `calls`, `generation_failures`, `outcomes`; `semantic_ok` as
  `{count, rate, ci95}` (a 95% Wilson interval, over calls that did not fail);
  `semantic_ok_by_case_type`; `mean_chance_baseline` (the average chance of a correct answer by
  picking a plausible answer at random, from each case's `difficulty`; compare it to the rate);
  `mean_deviation_score`; `error_types` (failures counted by kind, see below); and
  `paired_accuracy` when the session ran pairs (`passed`, `scored`, `rate`, `ci95`,
  `strong_pairs_only` with its own interval, `weak_pairs_scored`, `incomplete`, `by_family`).
  With fewer than 3 samples per case, a `sampling_note` warns that differences may be chance.
- **The calls:** `finish_reasons` (stop reasons counted, e.g. `stop (end_turn)`: 6),
  `served_by` (model and provider OpenRouter actually used, counted), `tokens` (prompt, cached,
  cache-write, completion, `reasoning_tokens`, `cache_read_share`), and `latency`
  (`mean_total_ms`, `mean_first_token_ms`).
- **Cost:** `cost.total_usd`; `avg_per_call_usd`; `setup_usd_est` (the one-time cache write of
  the shared opening) and `per_case_marginal_usd_est` (one more case once the cache is warm).
  These two are estimates: OpenRouter reports one cost per call, so the marginal cost is the
  median cost of cache-reading calls, and setup is what the writing call cost above that.
  Also `per_correct_answer_usd` and `per_passed_pair_usd`. `--rescore` makes no model call
  and costs nothing.

**`results[]`, one row per call:**
- **Identity:** `task_id`, `session_id`, `run_index`, `ts`, `case_hashes` (`input`: a hash of
  what the model saw; `rules`: a hash of the answer key's rules), so a result can be tied to the
  exact case version it ran on.
- **What was sent:** `prompt_readable`, the case's own turn as `known_state` and `intent`; the
  output contract is in the summary, and stays in a row only if it differed.
- **What came back:** `parsed_output`, the model's message. `raw_output` (the verbatim text) is
  kept only when it is not JSON, e.g. wrapped in a code fence, so the parsed copy would be lossy.
- **The call:** `error` (null when the call succeeded); `finish_reason` and
  `native_finish_reason` (why generation stopped: `stop`/`end_turn`, `length`/`max_tokens` =
  cut off, `refusal`); `served_model`, `provider` and `generation_id` (OpenRouter's id, for its
  dashboard); `prompt_tokens`, `cached_tokens`, `cache_write_tokens`, `completion_tokens`,
  `reasoning_tokens` (thinking, part of completion), `first_token_ms` (time to the first streamed
  token, thinking or answer), `latency_ms`, `usd_cost`.
- **Grading:** `verdict`, with `error_type` on a failure. For a pair case, also `pair_result`:
  `pair_id`, `twin_id`, `same_answer_as_twin`, `pair_passed`, and an `explanation` naming the
  twin's outcome.

**`error_type`** names what went wrong, so failures can be counted.
- **A failed pair case** is named for the reasoning its family tests: `fallback_not_applied`,
  `in_progress_course_ignored`, `transfer_credit_ignored`, `time_overlap_missed`,
  `and_or_requirement_misread`, `credit_arithmetic`, `modality_ignored`, `meeting_days_ignored`,
  `minimum_grade_misread`, `withdrawal_counted_as_credit`.
- **Any other semantic error** is named for its first broken rule: `wrong_number_of_sections`,
  `requirement_mismatch`, `named_course_missing`, `time_limit_ignored`, `wrong_course`, and so on.
- **A consistency error** is named for its first failed Tier 3 check: `full_section`,
  `time_conflict_in_cart`, `prerequisite_missing`, `section_details_miscopied`, and so on.
- **A format error** is named for its first failed Tier 1–2 check: `not_json`, `not_didcomm`,
  `malformed_section_entry`, and so on.
- **From the call itself:** `truncated_at_max_tokens` (a format error from an answer cut off at
  `max_tokens`), `refused`, and `call_failed`.

**Comparing sessions:** `python run_semantic_eval.py --test-file <cases> --compare A.json B.json`
runs McNemar's exact test on the case runs both files have, per case and per pair. It prints
how often only A or only B was right, and p. With one sample per case and a handful of cases,
expect p near 1: run `-n 3` or more on more cases before claiming a difference.

**Sessions run before 2026-10-19's streaming harness** (the four `pairs_sonnet_*.json`) have no
finish reasons, providers, thinking tokens or first-token times. Their `generation_settings`
and `code_version` (the commit, with uncommitted changes that were not recorded) were filled in
afterwards from what is known about those runs.

Dropped as duplicates: `ok` (= `error` is null), `prompt_sent`, the per-row copies of the
session's fields. Older verdicts' `tier3_detail` and `violated` (restated in `tiers` and the
check lists) are no longer produced and are dropped from an older file. `--rescore` rewrites an older file (a bare array of
rows) in this layout.
