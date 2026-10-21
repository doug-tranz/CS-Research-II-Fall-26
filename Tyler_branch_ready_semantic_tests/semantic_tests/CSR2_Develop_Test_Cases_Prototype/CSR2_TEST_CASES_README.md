# CS Research II — Registration reasoning test cases (draft prototype)

## Basis

These are **new synthetic registration records** using Daquan's `registration_input.schema.json` (Draft 2020-12; from the `Daquan-1` branch, copied on the `Tyler` branch) and Tyler's existing `run_semantic_cases_aneesh.py` adapter. They extend, rather than replace, Tyler's existing `SEM-001` through `SEM-020` cases.

**Validation performed:** All 12 `input` objects passed Daquan's exact schema (verified Git blob SHA `9eff55fdd6f533c70496f693f8916c76f9f323b7`). The existing runner's dry-run/smoke-check logic passed both the 12-case suite and the 6-case demonstration, using an equivalent copy of Aneesh's current provisional consistency evaluator. Eight output-level probes also matched the evaluator's current behavior. **No new LLM inference has been performed on these new cases.**

## Files

| File | Purpose |
|---|---|
| `csr2_reasoning_demo_6.jsonl` | First prototype demo: 3 counterfactual pairs / 6 cases |
| `csr2_reasoning_cases_12.jsonl` | Complete new suite: 6 counterfactual pairs / 12 cases |
| `csr2_model_inputs_only.jsonl` | Only valid, model-visible schema instances; **no hidden labels** |
| `csr2_example_model_input_RC007.json` | One human-readable full input, suitable for a manual test |
| `csr2_reasoning_case_index.csv` | Cases, reasoning dimensions, correct choices, negative controls |
| `create_csr2_reasoning_cases.py` | Reproducibly regenerate the JSONL and index CSV |
| `csr2_output_validation_probes.jsonl` | 8 fixed output examples for parser/evaluator testing **(not LLM input)** |
| `check_csr2_output_probes.py` | Check 8 outputs against current evaluator, without calling a model |

Each case record contains `case_id`, `scenario_id`, `variation`, `semantic_dimension`, `input`, `ground_truth` and `review_status`. **Only `input` should be passed to the LLM.** The runner does this automatically and adds the DIDComm system prompt and output contract.

The fixtures use hypothetical courses and students. They are schema-valid demonstrations, **not verified real Section Tally offerings**. Ground-truth interpretations are candidate labels (`candidate_needs_human_review`) and should be verified by your team before inclusion in reported benchmark results.

## Reasoning families

| Pair | Cases | Reasoning demanded | Expected selection(s) |
|---|---|---|---|
| Conditional fallback | RC-001 / RC-002 | Compare seat counts, respect mandatory in-person, apply first-choice/fallback policy | `41002` / `41001` |
| Transfer + completed credits | RC-003 / RC-004 | Sum qualifying transcript credits across two arrays vs 9-credit degree requirement | `42001` / `42002` |
| In-progress course deduplication | RC-005 / RC-006 | Read courses in progress and choose alternate if already taking target course | `43002` / `43001` |
| Time-interval overlap | RC-007 / RC-008 | Compare scheduled intervals against a non-negotiable lab; overlaps need not have identical start/end times | `44002` / `44001` |
| Nested AND/OR requirements | RC-009 / RC-010 | Evaluate two allowed AND clauses connected by OR; multiple CRNs count as correct | `45002` **or** `45003` / `45001` **or** `45004` |
| Credit-load arithmetic | RC-011 / RC-012 | Sum in-progress credit hours and add exactly 3 vs 4 credits to hit total | `46001` / `46002` |

For every test, `ground_truth.acceptable_crns` contains valid responses, and `wrong_but_consistent_crns` contains open, existing sections that should be marked semantically wrong. RC-001 additionally has a **full** first-choice section as an unscored consistency-negative control. Multi-acceptable cases deliberately avoid string-exact output matching.

## Run the small prototype on Windows Git Bash

1. Extract this package and copy its files into:
   `C:\Users\tyler\CS-Research-II-Fall-26\Tyler_branch_ready_semantic_tests\semantic_tests`
2. Use your existing `harness.py`, `registration.py`, `run_semantic_cases_aneesh.py`, `registration_input.schema.json`, `requests` and `jsonschema` packages.
3. Change to that folder:

   ```bash
   cd ~/CS-Research-II-Fall-26/Tyler_branch_ready_semantic_tests/semantic_tests
   ```

4. Run fixture checks without any LLM or API costs:

   ```bash
   python run_semantic_cases_aneesh.py --dry-run --test-file csr2_reasoning_demo_6.jsonl --schema registration_input.schema.json
   ```
   Expected: `PASS: 6 cases; expected CRNs succeed and wrong, DB-consistent CRNs fail semantics.`

5. Run the demo with your previously installed local Llama model (Ollama must be running):

   ```bash
   python run_semantic_cases_aneesh.py --model llama3.1:8b --samples 1 --test-file csr2_reasoning_demo_6.jsonl --schema registration_input.schema.json --output csr2_llama_demo_results.jsonl
   ```

   Optionally use `run_semantic_cases_openrouter_free_diagnostic.py` with your currently working `:free` cloud model:

   ```bash
   python run_semantic_cases_openrouter_free_diagnostic.py --model nvidia/nemotron-3-ultra-550b-a55b:free --samples 1 --test-file csr2_reasoning_demo_6.jsonl --schema registration_input.schema.json --output csr2_nemotron_demo_results.jsonl
   ```

   Free endpoints can rate-limit or change. Never commit an API key. Each run uses its own output path, because the scripts **append** results rather than overwrite.

6. To run all 12 cases, change `--test-file` to `csr2_reasoning_cases_12.jsonl` and choose another unique `--output` name.

7. Run optional syntax and format probes (no model or API needed):

   ```bash
   python check_csr2_output_probes.py
   ```

   The last two probes intentionally show a **known validator gap**: incorrect DIDComm `type` and absent `id` are accepted today, even though they should fail a future strict envelope validation step.

## Limits and research notes

- The v0.3 input schema describes records, not all university academic policies. Where a degree or workflow rule is needed, the case makes it explicit in the `prompt` and/or `program_requirements`.
- The current `registration.py` checks existence, seat capacity, duplicate enrollments, and exact matching meeting-slot codes. It does **not** calculate interval overlaps or infer all transcript/degree rules. Such errors are **semantic** in the current prototype and are detected by the human-authored gold labels, not the database checker.
- The dry run checks fixture consistency and ground truth, **not LLM reasoning quality**. Only an actual model-generated results file supports success-rate claims.
- Prompt fidelity, decoding settings, model/token costs, and provider failure rates should be reported alongside semantic results. Do not combine first-attempt and retry results without explaining the retry policy.
- For a proper model comparison, run the same cases, prompts, and scorer under both model backends and review labeling before publishing.
