# BIRD-style semantic cases integrated with Daquan + Aneesh

**Basis:**
- Daquan-1 — [`registration_input.schema.json`](https://github.com/doug-tranz/CS-Research-II-Fall-26/blob/Daquan-1/registration_input.schema.json)
- Aneesh-Anand — [`input_format_specv2.md`](https://github.com/doug-tranz/CS-Research-II-Fall-26/blob/Aneesh-Anand/input_format_specv2.md), `harness.py`, and `registration.py`
- Inspiration: CogComp/BIRD's controlled scenario + conditions + hidden label evaluation (not BIRD's Bayesian probability code)

## Deliverables

- `semantic_cases_daquan_aneesh.jsonl` — 20 candidate tests, 10 paired scenario families. Each line contains `input` and separate `ground_truth`.
- `run_semantic_cases_aneesh.py` — adapter that imports Aneesh's **existing** `harness.py` and `registration.py`. It doesn't overwrite them.
- `case_summary.json` — grouping counts.
- `build_cases.py` — deterministic script that authored the test fixtures.

All student identifiers and section data in this package are synthetic.

## Use with the team's code

1. Check out `Aneesh-Anand`, or copy its `harness.py` and `registration.py` into a working folder. Save the test JSONL and `run_semantic_cases_aneesh.py` **beside those two modules**.
2. Download `registration_input.schema.json` from Daquan-1 into the same folder.
3. Install dependencies: `python -m pip install requests jsonschema`.
4. Check case fixtures first (no model needed):

   ```powershell
   python run_semantic_cases_aneesh.py --dry-run --schema registration_input.schema.json
   ```

5. If Ollama is running and `llama3.1:8b` is installed, generate and score actual messages:

   ```powershell
   python run_semantic_cases_aneesh.py --model llama3.1:8b --samples 3 --schema registration_input.schema.json
   ```

6. Read `semantic_benchmark_results.jsonl`. Each generated message is logged with a verdict and scenario group.

**Do not send the full JSONL to the model.** The runner only passes `case['input']` and its `prompt` to `TaskSpec`. The answer key, labels, and notes stay outside the generation prompt.

## Existing-interface bridge and limits

**Daquan/Aneesh newer input format:** `schedule.sections[].crn`, structured `meetings`, transcript, and degree requirements.

**Existing Aneesh runtime:** `{student_id, section_id}` in a DIDComm body, SQLite `sections.section_id`, and `meets` as a single legacy slot code. We populate the legacy `section_id` with the CRN, and format meetings into opaque strings for the legacy database. The model is instructed to put the CRN in `body.section_id`. This is a compatibility bridge **not** a finalized protocol decision.

The existing `registration.check_consistency()` detects section existence, capacity, duplicated enrollment, and equal slot-code conflicts. The adapter does not implement real interval-overlap conflict checks, full graduation requirements, transcript-aware repeat constraints, or CRN-native transaction semantics. These examples are deliberately constructed with fresh enrollment tables and open sections so both the correct and wrong choices are DB-consistent under that existing implementation.

**Gold scoring:** one or more `acceptable_crns` expresses semantically correct options. Each test also supplies at least one `wrong_but_consistent_crns` to demonstrate a valid but intent-wrong transaction. The semantic scorer does not compare envelope IDs or require bytewise equality to a single JSON output.

**Validation:** `--dry-run` checks that each expected choice is a success and each wrong-but-consistent choice is a semantic error *after* the existing registration consistency tier. `--schema` validates `input` against Daquan's exact schema. Dry-run results do **not** represent LLM performance. Labels remain **candidate data**, marked `candidate_needs_human_review`; the team should verify each pair before using it in published research.

## BIRD-inspired test pairs

| Scenario family | Controlled reversal | What the LLM must notice |
|---|---|---|
| campus | Glassboro ↔ Camden | Campus request in natural language |
| modality | online ↔ in-person | Required modality |
| weekday | MW ↔ TR | Meeting days |
| time_of_day | morning ↔ afternoon | Meeting-time preference |
| negation | avoid CRN 35001 ↔ avoid CRN 35002 | Negative condition |
| grade_threshold | completed course D ↔ B | Minimum passing requirement C- |
| requirement_goal | CS elective credit ↔ mathematics course | Degree requirement course_options |
| priority | online mandatory ↔ morning mandatory | Hard constraint vs preference |
| compound_constraints | online+afternoon ↔ in-person+morning | Multiple simultaneous requirements |
| withdrawal_credit | W ↔ B | Withdrawn credit isn't earned |

## Suggested metrics

- `Overall transaction success rate = success / all generations`.
- `Semantic accuracy given consistency = success / (success + semantic_error)`.
- `Paired condition accuracy = scenario pairs where both variants are correct / total scenario pairs` (aggregate per model, and specify repetition policy).
- Keep separate counts for `generation_failed`, `syntactic_error`, `schema_error`, and `consistency_error`.
- Report latency, tokens, and (when available) USD per successful transaction separately from correctness.

### Research-method caveats

- Paired variants sometimes alter `prompt`, and sometimes alter the recorded student transcript; the exact changed dimension is identified per case.
- The current semantic checker is an explicit goal-label comparator. It does **not** independently interpret ambiguous user intent; the labels must be human-reviewed.
- This is a **candidate benchmark extension** aligned with the teammate specifications, not a claim of running a new model experiment or a claim that either teammate already uses these fixtures.
