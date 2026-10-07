"""
run_semantic_eval.py
---------------------
End-to-end runner: reads a test-case file (hidden input/expected pairs),
sends only the model-visible half to the model, and scores the response with
the full 4-tier pipeline (registration.py's Tiers 1-3 + our new Tier 4).

This is the CART version (README.md, "The cart message"): the model is asked
for the register message in didcomm_schema.OUTPUT_CONTRACT. The single-section
version is old_schema/run_semantic_eval.py.

The model NEVER sees `ground_truth`. It is loaded separately and used only
here, after generation, per harness.py's own design comment on GroundTruth.

TWO KINDS OF CASE FILE:
  .jsonl  one case per line; `task.inputs` is a bare list of sections.
  .json   an indented array of cases in the full input format (README.md,
          "The input format"): student, transcript, program requirements,
          schedule and prompt, each wrapped with a case_id and an answer key.

HOW TO RUN:
    python run_semantic_eval.py --backend openrouter --model anthropic/claude-sonnet-5
    python run_semantic_eval.py --model anthropic/claude-sonnet-5 --test-file cart_test_cases.jsonl --task-ids cart-006 cart-007
    python run_semantic_eval.py --model anthropic/claude-opus-5.5 --test-file input_format_cases.json --task-ids sem_004 -n 5
    python run_semantic_eval.py --model anthropic/claude-opus-5.5 --test-file input_format_cases.json --rendering nl
    python run_semantic_eval.py --test-file input_format_cases.json --check     # no model, no cost

Requires OPENROUTER_API_KEY to be set, and test_cases.jsonl in the same folder.
Appends full results (including the verdict) to semantic_eval_results.json,
an indented JSON array with one object per generation.
"""

import argparse
import csv
import io
import json
import sys
from dataclasses import asdict
from pathlib import Path
from harness import TaskSpec, OllamaBackend, OpenRouterBackend, generate
import registration as reg
from evaluate_with_semantics import evaluate_full, evaluate_case, validate_case, self_check
from didcomm_schema import OUTPUT_CONTRACT, CRN_CONTRACT

RENDERINGS = ("json", "csv", "nl")


def load_test_cases(path="test_cases.jsonl"):
    if str(path).endswith(".json"):          # indented array of input-format cases
        return json.loads(Path(path).read_text(encoding="utf-8"))
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                cases.append(json.loads(line))
    return cases


def case_id(case: dict) -> str:
    return case["case_id"] if "input" in case else case["task"]["task_id"]


def build_db_for_case(task_inputs_json):
    """Seeds an in-memory registration DB from a .jsonl test case's `inputs` field."""
    sections = json.loads(task_inputs_json)
    seed_sql = ""
    for s in sections:
        seed_sql += (
            f"INSERT INTO sections VALUES "
            f"('{s['section_id']}', '{s['course_id']}', "
            f"{s['seats_total']}, {s['seats_taken']}, '{s['meets']}');\n"
        )
    return reg.make_db(seed_sql)


# ── Input-format cases: three renderings of the SAME state (spec open item 8) ──

def _meeting_text(m: dict) -> str:
    kind = f" ({m['type']})" if "type" in m else ""
    return f"{'/'.join(m['days'])} {m['start_time']}-{m['end_time']}{kind}"


def _credits_text(credits) -> str:
    return f"{credits['min']}-{credits['max']}" if isinstance(credits, dict) else f"{credits:g}"


def _cell(key: str, value) -> str:
    """One CSV cell. Lists are joined with '|'; nested rows with '; '."""
    if key == "meetings":
        return "; ".join(_meeting_text(m) for m in value) or "none (asynchronous)"
    if key == "credit_caps":
        return "; ".join(f"max {cap['max_credits']:g} from {'|'.join(cap['course_options'])}" for cap in value)
    if key == "credit_hours":
        return _credits_text(value)
    if isinstance(value, list):
        return "|".join(map(str, value))
    return str(value)


def _table(name: str, rows: list) -> str:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(key, row[key]) if key in row else "" for key in columns])
    return f"# {name}\n{out.getvalue()}"


def render_csv(state: dict) -> str:
    transcript = state["transcript"]
    tables = [
        ("student", [state["student"]]),
        ("completed_courses", transcript["completed_courses"]),
        ("transfer_courses", transcript["transfer_courses"]),
        ("courses_in_progress", transcript["courses_in_progress"]),
        ("program_requirements", state["program_requirements"]),
        ("prerequisites", state.get("prerequisites", [])),
        ("course_equivalents", [{"same_course": group} for group in state.get("course_equivalents", [])]),
        ("graduation_rules", [state["graduation_rules"]] if "graduation_rules" in state else []),
        (f"sections offered in {state['schedule']['term']}", state["schedule"]["sections"]),
    ]
    return "\n".join(_table(name, rows) for name, rows in tables if rows)


def _taken_text(c: dict) -> str:
    where = f" at {c['campus']}" if "campus" in c else ""
    title = f" ({c['title']})" if c.get("title") else ""
    tags = f", attributes {', '.join(c['attributes'])}" if c.get("attributes") else ""
    return f"{c['course_id']}{title}, {c['credit_hours']:g} credits{where}{tags}"


def _requirement_text(r: dict) -> str:
    options = ", ".join(r["course_options"])
    need = {
        "all_of": f"every one of: {options}",
        "any_of": f"at least one of: {options}",
        "credits_from": f"{r['credit_hours']:g} credits from: {options}",
        "credits_with_attribute": f"{r['credit_hours']:g} credits of courses with attribute {r.get('attribute')}",
    }[r["rule"]]
    text = (f"- {r['req_id']} ({r.get('title', r['category'])}; {r['category']}; "
            f"{r['credit_hours']:g} credits): needs {need}, each with {r['minimum_grade']} or better.")
    if r.get("repeatable"):
        text += f" Credits count every time for: {', '.join(r['repeatable'])}."
    for cap in r.get("credit_caps", []):
        text += f" At most {cap['max_credits']:g} credits count from: {', '.join(cap['course_options'])}."
    if r.get("exclusive"):
        text += " A course used here cannot be used by another exclusive requirement."
    if r.get("notes"):
        text += f" Note: {r['notes']}."
    return text


def _section_text(s: dict) -> str:
    title = f", {s['title']}" if s.get("title") else ""
    times = "; ".join(f"{' and '.join(m['days'])} {m['start_time']} to {m['end_time']}"
                      + (f" ({m['type']})" if "type" in m else "") for m in s["meetings"])
    tags = f" Attributes: {', '.join(s['attributes'])}." if s.get("attributes") else ""
    return (f"- CRN {s['crn']}: {s['course_id']} section {s['section']}{title}, "
            f"{_credits_text(s['credit_hours'])} credits, {s['modality']} at {s['campus']}, "
            f"{times or 'no meeting times (asynchronous)'}. "
            f"{s['seats_taken']} of {s['seats_total']} seats are taken.{tags}")


def render_nl(state: dict) -> str:
    student, transcript = state["student"], state["transcript"]
    minor = f", with a minor in {student['minor']}" if "minor" in student else ""
    lines = [f"Student {student['student_id']} is pursuing a {student['degree']} in {student['major']}"
             f"{minor} (program {student['program_id']}, catalog year {student['catalog_year']}).", ""]
    lines.append("Completed courses:")
    lines += [f"- {c['term']}: {_taken_text(c)}, grade {c['grade']}." for c in transcript["completed_courses"]] or ["- none"]
    lines.append("Transfer courses:")
    lines += [f"- From {c['institution']}: {_taken_text(c)}, grade {c['grade']}."
              for c in transcript["transfer_courses"]] or ["- none"]
    lines.append("Courses in progress (no grade yet):")
    lines += [f"- {c['term']}: {_taken_text(c)}." for c in transcript["courses_in_progress"]] or ["- none"]
    lines += ["", "Program requirements:"]
    lines += [_requirement_text(r) for r in state["program_requirements"]] or ["- none"]
    if state.get("prerequisites"):
        lines += ["", "Prerequisites:"]
        lines += [f"- {p['course_id']} requires {', '.join(p['requires'])} with {p['minimum_grade']} or better."
                  for p in state["prerequisites"]]
    if state.get("course_equivalents"):
        lines += ["", "Equivalent courses (the same course under different ids):"]
        lines += [f"- {', '.join(group)}" for group in state["course_equivalents"]]
    if state.get("graduation_rules"):
        rules = state["graduation_rules"]
        named = {"total_credits": "{:g} total credits", "minimum_gpa": "a GPA of at least {:g}",
                 "residency_credits": "{:g} credits earned at this institution"}
        lines += ["", "To graduate the student needs " +
                  ", ".join(named[k].format(v) for k, v in rules.items()) + "."]
    lines += ["", f"Sections offered in {state['schedule']['term']}:"]
    lines += [_section_text(s) for s in state["schedule"]["sections"]] or ["- none"]
    return "\n".join(lines)


def to_task(case: dict, rendering: str = "json") -> TaskSpec:
    """What the model sees for an input-format case: the whole input object, never the
    answer key. `prompt` becomes the intent; the rest is the known state."""
    data = case["input"]
    state = {key: value for key, value in data.items() if key != "prompt"}
    if rendering == "json":
        inputs = json.dumps(state)
    elif rendering == "csv":      # harness.py labels the state "JSON", so say what it really is
        inputs = "(The state below is given as CSV tables, not JSON.)\n" + render_csv(state)
    else:
        inputs = "(The state below is given as plain text, not JSON.)\n" + render_nl(state)
    return TaskSpec(
        task_id=case["case_id"],
        domain="registration",
        inputs=inputs,
        intent=data["prompt"],
        output_contract=OUTPUT_CONTRACT + CRN_CONTRACT,
    )


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


def check_cases(cases: list) -> int:
    """Scores every input-format case's own answer key. No model call. Returns the failure count."""
    failed = 0
    for case in cases:
        problems = self_check(case) if "input" in case else ["not an input-format case (.json)"]
        failed += bool(problems)
        print(f"{case_id(case)}: {'FAIL' if problems else 'ok'}")
        for p in problems:
            print(f"    - {p}")
    print(f"\n{len(cases) - failed}/{len(cases)} cases sound.")
    return failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["ollama", "openrouter"], default="openrouter")
    parser.add_argument("--model", help="required unless --check")
    parser.add_argument("--test-file", default="test_cases.jsonl")
    parser.add_argument("--output", default="semantic_eval_results.json")
    parser.add_argument("--task-ids", nargs="+", help="run only these cases, e.g. cart-006 cart-007")
    parser.add_argument("-n", type=int, default=1, help="samples per case")
    parser.add_argument("--rendering", choices=RENDERINGS, default="json",
                        help="how an input-format case's state is shown to the model")
    parser.add_argument("--check", action="store_true",
                        help="score each input-format case's own answer key and exit; no model call")
    args = parser.parse_args()

    cases = load_test_cases(args.test_file)
    if args.task_ids:
        cases = [c for c in cases if case_id(c) in args.task_ids]
    if args.check:
        sys.exit(1 if check_cases(cases) else 0)
    if not args.model:
        parser.error("--model is required unless --check is given")

    backend = OpenRouterBackend() if args.backend == "openrouter" else OllamaBackend()
    passed, runs, cost = 0, 0, 0.0

    for case in cases:
        wrapped = "input" in case            # input-format case
        if wrapped:
            problems = validate_case(case)
            if problems:
                raise ValueError(f"{case_id(case)} is not a valid input-format case: {problems}")
            task = to_task(case, args.rendering)
        else:
            task_dict = case["task"]
            task = TaskSpec(
                task_id=task_dict["task_id"],
                domain=task_dict["domain"],
                inputs=task_dict["inputs"],
                intent=task_dict["intent"],
                output_contract=OUTPUT_CONTRACT,
            )

        for run_index in range(args.n):
            result = generate(backend, args.model, task, run_index=run_index)

            if not result.ok:
                verdict = {"outcome": "generation_failed", "detail": result.error}
            elif wrapped:
                verdict = evaluate_case(case, result.raw_output)
            else:
                conn = build_db_for_case(task_dict["inputs"])
                verdict = evaluate_full(conn, result.raw_output, case["ground_truth"]["expected"])

            passed += verdict["outcome"] == "semantic_ok"
            runs += 1
            cost += result.usd_cost
            label = task.task_id if args.n == 1 else f"{task.task_id} run {run_index}"
            print(f"{label}: {verdict['outcome']:20s} | {verdict.get('detail', '')}")
            log_readable(result, verdict, args.output)

    print(f"\n{passed}/{runs} semantic_ok on {args.model}. Cost ${cost:.4f}. "
          f"Full results in {args.output}")


if __name__ == "__main__":
    main()
