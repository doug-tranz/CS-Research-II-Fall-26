"""
run_semantic_eval.py
---------------------
End-to-end runner: reads test_cases.jsonl (hidden input/expected pairs),
sends only the `task` half to the model, and scores the response with the
full 4-tier pipeline (registration.py's Tiers 1-3 + our new Tier 4).

This is the CART version (README.md, "The cart message"): the model is asked
for the register message in didcomm_schema.OUTPUT_CONTRACT. The single-section
version is old_schema/run_semantic_eval.py.

The model NEVER sees `ground_truth` -- only `task` is passed to generate().
`ground_truth` is loaded separately and used only here, after generation,
per harness.py's own design comment on GroundTruth.

HOW TO RUN:
    python run_semantic_eval.py --backend openrouter --model anthropic/claude-sonnet-5
    python run_semantic_eval.py --model anthropic/claude-sonnet-5 --test-file cart_test_cases.jsonl
    python run_semantic_eval.py --model anthropic/claude-sonnet-5 --test-file cart_test_cases.jsonl --task-ids cart-006 cart-007

Requires OPENROUTER_API_KEY to be set, and test_cases.jsonl in the same folder.
Appends full results (including the verdict) to semantic_eval_results.json,
an indented JSON array with one object per generation.
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from harness import TaskSpec, OllamaBackend, OpenRouterBackend, generate
import registration as reg
from evaluate_with_semantics import evaluate_full
from didcomm_schema import OUTPUT_CONTRACT


def load_test_cases(path="test_cases.jsonl"):
    cases = []
    with open(path, encoding="utf-8") as f:
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


def readable_row(row: dict, verdict: dict) -> dict:
    """Adds the model's message as a parsed object; raw_output stays verbatim."""
    try:
        parsed = json.loads(row["raw_output"])
    except json.JSONDecodeError:
        parsed = None
    return {**row, "parsed_output": parsed, "verdict": verdict}


def log_readable(result, verdict: dict, path: str) -> None:
    """Like harness.log_result(), but keeps the file as one indented JSON array."""
    file = Path(path)
    rows = json.loads(file.read_text(encoding="utf-8")) if file.exists() else []
    rows.append(readable_row(asdict(result), verdict))
    file.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["ollama", "openrouter"], default="openrouter")
    parser.add_argument("--model", required=True)
    parser.add_argument("--test-file", default="test_cases.jsonl")
    parser.add_argument("--output", default="semantic_eval_results.json")
    parser.add_argument("--task-ids", nargs="+", help="run only these cases, e.g. cart-006 cart-007")
    args = parser.parse_args()

    backend = OpenRouterBackend() if args.backend == "openrouter" else OllamaBackend()
    cases = load_test_cases(args.test_file)
    if args.task_ids:
        cases = [c for c in cases if c["task"]["task_id"] in args.task_ids]
    passed, cost = 0, 0.0

    for case in cases:
        task_dict = case["task"]
        ground_truth = case["ground_truth"]

        task = TaskSpec(
            task_id=task_dict["task_id"],
            domain=task_dict["domain"],
            inputs=task_dict["inputs"],
            intent=task_dict["intent"],
            output_contract=OUTPUT_CONTRACT,
        )

        result = generate(backend, args.model, task, run_index=0)

        if not result.ok:
            verdict = {"outcome": "generation_failed", "detail": result.error}
        else:
            conn = build_db_for_case(task_dict["inputs"])
            verdict = evaluate_full(conn, result.raw_output, ground_truth["expected"])

        passed += verdict["outcome"] == "semantic_ok"
        cost += result.usd_cost
        print(f"{task.task_id}: {verdict['outcome']:20s} | {verdict.get('detail', '')}")
        log_readable(result, verdict, args.output)

    print(f"\n{passed}/{len(cases)} semantic_ok on {args.model}. Cost ${cost:.4f}. "
          f"Full results in {args.output}")


if __name__ == "__main__":
    main()
