"""
cases.py
---------
Test cases: loading a case file, filling in each case's input (its fabricated transcript and
its sections from the Section Tally), the answer key's rules, and validating a case.

A case is {"case_id", "input", "ground_truth", "schedule_from_tally"?}; the model only ever
sees `input` (render.to_task()); `ground_truth` is read only by scoring.py.
"""
import json
import re
from pathlib import Path

import fabricate
import section_tally
from didcomm_schema import MESSAGE_TYPE, SECTION_FIELDS
from input_format import validate_input


# ── Test cases and their answer keys ──
# The answer key is `rules`: what any correct cart must satisfy (RULES below). `examples`
# gives carts that must pass ("valid") and must fail ("invalid"); a cart is a list of crns.

TALLY_KEYS = {"named_courses", "courses", "whole"}
_COURSE_IN_TEXT = re.compile(r"\b[A-Z]{2,4}\d[A-Z0-9]{4}\b")


def named_courses(data: dict) -> list:
    """Every course an input names: each requirement's course_options, course_groups and
    recommended_courses, each course with a prerequisites entry, and each course id written
    in the prompt. In first-seen order. (A course_ranges family is not expanded: a case adds
    the family's offered courses with schedule_from_tally's `courses`.)"""
    named = [c for r in data["program_requirements"]
             for c in r["course_options"] + [c for g in r.get("course_groups", []) for c in g]
             + r.get("recommended_courses", [])]
    named += [p["course_id"] for p in data.get("prerequisites", [])]
    named += _COURSE_IN_TEXT.findall(data["prompt"])
    return list(dict.fromkeys(named))

def is_whole(case: dict) -> bool:
    """True when the model reads the whole Section Tally from the session's cached opening."""
    return bool((case.get("schedule_from_tally") or {}).get("whole"))


def resolve_case(case: dict) -> dict:
    """A case takes its schedule from the Section Tally instead of writing it out:
        "schedule_from_tally": {"named_courses": true, "courses": [...]}
            every section of every course the input names (named_courses()), plus of the
            optional `courses` (e.g. a course the prompt names only by title), listed in the
            prompt, so the model reads everything in one input (what every case file says;
            the run uses it with --named-courses)
        "schedule_from_tally": {"whole": true}
            the whole tally, read from the session's opening turn (TallySession); the
            checker sees every section (the default run switches every case to this)
    This fills in input.schedule from the Section Tally export (section_tally.TALLY_CSV),
    so everything after it (validation, database, scoring) sees an ordinary input object.
    A case whose input names a `program` is first built from degree_requirements.json, with
    its transcript fabricated from the case's spec (fabricate.build_input())."""
    if "program" in case.get("input", {}):
        case = {**case, "input": fabricate.build_input(case["input"])}
    ref = case.get("schedule_from_tally")
    if ref is None:
        return case
    name = case.get("case_id", "<no id>")
    if not isinstance(ref, dict) or set(ref) - TALLY_KEYS:
        raise ValueError(f"{name}: schedule_from_tally takes named_courses (and courses), "
                         f"or whole, nothing else")
    if ref.get("whole") and set(ref) != {"whole"}:
        raise ValueError(f"{name}: with whole, the model reads every section; give nothing else")
    if not (ref.get("named_courses") or ref.get("whole")):
        raise ValueError(f"{name}: schedule_from_tally needs named_courses: true or whole: true")
    if "schedule" in case["input"]:
        raise ValueError(f"{name}: give input.schedule or schedule_from_tally, not both")
    data = {key: value for key, value in case["input"].items() if key != "prompt"}
    if ref.get("whole"):
        data["schedule"] = section_tally.full_schedule()
    else:
        data["schedule"] = section_tally.course_schedule(
            named_courses(case["input"]) + list(ref.get("courses", [])))
    data["prompt"] = case["input"]["prompt"]
    return {**case, "input": data}


def load_test_cases(path="paired_cases.json", task_ids=None, whole=False) -> list:
    """The cases in a file (only `task_ids`, if given), with any schedule_from_tally filled in.
    Cases are chosen BEFORE the tally is read, so a case that does not use it never fails on it.
    whole=True runs every tally-based case with the whole Section Tally (the session; main()
    does this unless --named-courses) in place of the portion its file names; the case file
    itself is unchanged."""
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if task_ids:
        cases = [case for case in cases if case.get("case_id") in task_ids]
    if whole:
        cases = [{**case, "schedule_from_tally": {"whole": True}} if "schedule_from_tally" in case
                 else case for case in cases]
    return [resolve_case(case) for case in cases]


RULES = {
    "count": "number of sections, {min, max}",
    "credits": "total credits of the cart, {min, max}",
    "courses_count_toward": "req_ids: every section's course must move one of these unmet requirements forward",
    "no_days": "days no meeting may fall on, e.g. [\"Friday\"]",
    "starts_before": "every meeting starts before this time, e.g. \"5:00 PM\"",
    "ends_by": "every meeting ends by this time",
    "modality_in": "allowed modalities",
    "must_include_courses": "course ids the cart must contain",
    "courses_in": "course ids: every section's course must be one of these",
    "busy": "the student's fixed commitments, [{days, start_time, end_time}]: no meeting may overlap one",
}
# Always checked as well: the student_id and term_code, and that no section is waitlisted
# (no request asks to be put on a waitlist).


def _item(entry) -> dict:
    """An answer-key entry as a section object: a bare crn becomes {"crn": crn}."""
    return {"crn": entry} if isinstance(entry, str) else dict(entry)


def _full_item(entry, by_crn: dict) -> dict:
    """An answer-key entry as the message must write it: the offered section's fields
    (SECTION_FIELDS) copied in, then the entry's own credit_hours and waitlist. A
    variable-credit section gets credit_hours only from the entry."""
    item = _item(entry)
    section = by_crn.get(item["crn"])
    if section is None:              # validate_case() reports it
        return item
    full = {field: section[field] for field in SECTION_FIELDS if field != "credit_hours"}
    if not isinstance(section["credit_hours"], dict):
        full["credit_hours"] = section["credit_hours"]
    return {**full, **item}


def expected_message(case: dict, cart: list) -> str:
    """The register message for this cart; entries may be crns or section objects."""
    data = case["input"]
    by_crn = {s["crn"]: s for s in data["schedule"]["sections"]}
    return json.dumps({"id": "n/a", "type": MESSAGE_TYPE, "body": {
        "student_id": data["student"]["student_id"], "term_code": data["schedule"]["term_code"],
        "sections": [_full_item(entry, by_crn) for entry in cart]}})


# The two variants of a counterfactual pair (pairs.py).
PAIR_VARIANTS = ("a", "b")


def validate_case(case) -> list:
    """Returns a list of problems with a test case; empty means it is sound."""
    if not isinstance(case, dict):
        return ["case is not a JSON object"]
    problems = [f"{field} is missing" for field in ("case_id", "input", "ground_truth")
                if field not in case]
    if problems:
        return problems
    problems = [f"input: {p}" for p in validate_input(case["input"])]
    if problems:
        return problems

    truth = case["ground_truth"]
    offered = {s["crn"] for s in case["input"]["schedule"]["sections"]}
    if "rules" not in truth:
        return ["ground_truth has no rules"]
    unknown = sorted(set(truth["rules"]) - set(RULES))
    if unknown:
        problems.append(f"unknown rules {unknown}; known: {sorted(RULES)}")
    req_ids = {r["req_id"] for r in case["input"]["program_requirements"]}
    for req in truth["rules"].get("courses_count_toward", []):
        if req not in req_ids:
            problems.append(f"rule courses_count_toward names {req}, which is not a requirement")
    pair = truth.get("pair")
    if pair is not None and not (isinstance(pair, dict) and pair.get("id") and pair.get("variant") in PAIR_VARIANTS):
        problems.append(f"ground_truth.pair needs an id and a variant in {list(PAIR_VARIANTS)}")
    examples = truth.get("examples", {})
    if not examples.get("valid"):
        problems.append("a rules answer key needs at least one valid example cart")
    carts = [[_item(entry) for entry in cart] for cart in examples.get("valid", []) + examples.get("invalid", [])]
    for cart in carts:
        if not cart:
            problems.append("ground_truth has an empty cart")
        for item in cart:
            if item["crn"] not in offered:
                problems.append(f"ground_truth names crn {item['crn']}, which is not in the schedule")
    return problems
