"""
run_semantic_eval.py
---------------------
End-to-end runner: reads test_cases.jsonl (hidden input/expected pairs),
sends only the `task` half to the model, and scores the response with the
full 4-tier pipeline (registration.py's Tiers 1-3 + our new Tier 4).

The model NEVER sees `ground_truth` -- only `task` is passed to generate().
`ground_truth` is loaded separately and used only here, after generation,
per harness.py's own design comment on GroundTruth.

HOW TO RUN:
    python old_schema/run_semantic_eval.py --backend openrouter --model anthropic/claude-sonnet-5

Requires OPENROUTER_API_KEY to be set, and test_cases.jsonl in this folder.
Appends full results (including the verdict) to semantic_eval_results.jsonl.
"""

import argparse
import json
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE.parent))   # harness.py is in the repo root
from harness import TaskSpec, OllamaBackend, OpenRouterBackend, generate, log_result
import registration as reg
from evaluate_with_semantics import evaluate_full


def load_test_cases(path=str(HERE / "test_cases.jsonl")):
    cases = []
    with open(path) as f:
        for line in f:
            if line.strip():
                cases.append(json.loads(line))
    return cases


def build_db_for_case(task_inputs_json):
    """Seeds an in-memory registration DB from a test case's `inputs` field."""
    sections = json.loads(task_inputs_json)
    seed_sql = ""
    for s in sections:
        seed_sql += (
            f"INSERT INTO sections VALUES "
            f"('{s['section_id']}', '{s['course_id']}', "
            f"{s['seats_total']}, {s['seats_taken']}, '{s['meets']}');\n"
        )
    return reg.make_db(seed_sql)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["ollama", "openrouter"], default="openrouter")
    parser.add_argument("--model", required=True)
    parser.add_argument("--test-file", default=str(HERE / "test_cases.jsonl"))
    parser.add_argument("--output", default=str(HERE / "semantic_eval_results.jsonl"))
    args = parser.parse_args()

    backend = OpenRouterBackend() if args.backend == "openrouter" else OllamaBackend()
    cases = load_test_cases(args.test_file)

    for case in cases:
        task_dict = case["task"]
        ground_truth = case["ground_truth"]

        task = TaskSpec(
            task_id=task_dict["task_id"],
            domain=task_dict["domain"],
            inputs=task_dict["inputs"],
            intent=task_dict["intent"],
            output_contract='Respond with {"type": "registration/enroll", "id": "<uuid>", "body": {"student_id": "<id>", "section_id": "<id>"}}',
        )

        result = generate(backend, args.model, task, run_index=0)

        if not result.ok:
            verdict = {"outcome": "generation_failed", "detail": result.error}
        else:
            conn = build_db_for_case(task_dict["inputs"])
            verdict = evaluate_full(conn, result.raw_output, ground_truth["expected"])

        print(f"{task.task_id}: {verdict['outcome']:20s} | {verdict.get('detail', '')}")
        log_result(result, verdict=verdict, path=args.output)

    print(f"\nDone. Full results in {args.output}")


if __name__ == "__main__":
    main()
