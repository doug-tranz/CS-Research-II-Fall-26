# CS Research II — Task 1.9: Dynamically Build Test Cases

## Deliverable

`generate_dynamic_cases.py` generates *new, seeded, reproducible* batches of semantic registration problems in **Daquan's v0.3 JSON Schema** and **Aneesh's existing JSONL test-runner format**. It does not change `build_cases.py`, `harness.py`, or `registration.py`.

The deliverable contains:

| File | Use |
|---|---|
| `generate_dynamic_cases.py` | New configurable generator; source code |
| `test_dynamic_generator.py` | Offline unit tests for schema conformance, determinism, semantic rules |
| `dynamic_demo_6.jsonl` | Generated 6-case demonstration (seed 43; campus, grades, compound constraints) |
| `dynamic_semantic_cases.jsonl` | Generated 20-case batch (seed 42; all 10 families) |
| `*_index.csv` | Case IDs, labels and expected CRNs for human review |
| `*_manifest.json` | Seed, scenario types, SHA-256 content checksum |

Your original files remain unchanged. The ZIP does not include or overwrite the team schema or existing harness.

## Install into your existing Git Bash project

Open Git Bash, then:

```bash
cd ~/CS-Research-II-Fall-26/Tyler_branch_ready_semantic_tests/semantic_tests
python -m zipfile -e ~/Downloads/CSR2_Dynamic_Test_Generator.zip .
python -m pip install jsonschema requests
```

If the ZIP is saved somewhere other than Downloads, use its actual path. The extraction command places the ZIP's files in your current `semantic_tests` directory.

## Validate the supplied generated demonstration (no LLM calls)

```bash
python -m unittest test_dynamic_generator -v
python run_semantic_cases_aneesh.py \
  --dry-run \
  --test-file dynamic_demo_6.jsonl \
  --schema registration_input.schema.json
```

Expected fixture check: `PASS: 6 cases`.

## Generate new cases instead of replaying a fixed set

```bash
python generate_dynamic_cases.py \
  --seed 2026 \
  --pairs-per-family 2 \
  --schema registration_input.schema.json \
  --smoke-check \
  --output generated_seed2026.jsonl
```

This creates **40 cases** (2 instances × 10 families × 2 variants), plus `generated_seed2026_index.csv` and `generated_seed2026_manifest.json`.

**Seeded reproducibility:** identical seed + settings yields identical cases. A different seed changes student IDs, CRNs, course IDs, seat counts, section ordering, and titles while retaining the reasoning templates. An existing output is not overwritten unless you explicitly pass `--overwrite`.

To generate only a specific subset, for example a six-case demonstration:

```bash
python generate_dynamic_cases.py \
  --seed 100 \
  --families campus,grade_threshold,compound_intent \
  --schema registration_input.schema.json \
  --smoke-check \
  --output my_demo_6.jsonl
```

List all options with `python generate_dynamic_cases.py --help`.

## Run generated cases through the existing LLM harness

```bash
python run_semantic_cases_aneesh.py \
  --model llama3.1:8b \
  --samples 1 \
  --test-file my_demo_6.jsonl \
  --schema registration_input.schema.json \
  --output my_dynamic_llama_results.jsonl
```

The runner saves the model-generated transaction, prompt sent, timing/tokens, and evaluation verdict. It appends to the output file—use a new output filename when starting a new experiment. Requires running Ollama with the model installed.

## Reasoning families

1. Campus constraint (Glassboro vs Camden)
2. Delivery modality (online vs in-person)
3. Meeting-day constraints (Mon/Wed vs Tue/Thu)
4. Morning vs afternoon meeting time
5. Explicitly excluded section / negation
6. Transcript grade vs a program minimum grade
7. Mandatory condition vs optional preference
8. Compound Boolean constraints (two conditions simultaneously)
9. Earned credit arithmetic across completed courses and transfers
10. Already-in-progress course / duplicate avoidance

Every scenario has two controlled variants with different ground-truth answers. Every test case contains a complete, schema-valid `input` object and a separate `ground_truth` object with acceptable and wrong-but-DB-consistent CRNs. **Do not send `ground_truth` to the model.** The runner currently only sends `case['input']`.

## Scope limitations

- Synthetic student and section records only; not real Section Tally data or a production registrar.
- Each candidate section remains DB-consistent under Aneesh's provisional checker. The generator targets *semantic* errors; deliberately full sections and real time-interval overlaps require separate consistency-case support.
- The current evaluator checks student/CRN selection but does not require the exact DIDComm `type` or nonempty `id`. Do not present its outcome as strict DIDComm compliance.
- All ground-truth labels have `review_status=candidate_needs_human_review` and should be independently reviewed before research publication.
- Generation tests were run locally against the exact v0.3 schema, matched by Git blob SHA `9eff55fdd6f533c70496f693f8916c76f9f323b7`. Real LLM results require running the model command above.

## Suggested task evidence for Planner 1.9

Demonstrate two different seeds creating different CSV/JSONL data, a schema-checked dry run, and (optionally) one six-case model execution. Upload `generate_dynamic_cases.py`, its unit tests, and sample generated cases to your `Tyler` branch, and link that commit in Planner.
