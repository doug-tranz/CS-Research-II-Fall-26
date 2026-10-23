"""
cases.py
--------
Test cases: loading a case file, filling in each case's full input object, and the answer key
format.

The case file (data/paired_cases.json, written by generate_pairs.py) is an indented JSON array of
counterfactual pairs of cases. Each case names its degree program, fabricates its transcript
(fabricate.py), and carries a rules answer key and its pair:

    {"case_id": "pair-transfer-philosophy-ba-a",
     "input": {"program": "philosophy-ba", "student": {...}, "transcript": {"fabricate": {...}},
               "prompt": "..."},
     "candidate_courses": [...],
     "ground_truth": {"rules": {...}, "examples": {"valid": [...], "invalid": [...]},
                      "implicit_relationship": "...",
                      "pair": {"id": "transfer/philosophy-ba", "variant": "a", ...}}}

The model NEVER sees `ground_truth`: render.to_task() builds the prompt from `input` only.
"""
import hashlib
import json
from pathlib import Path

from . import fabricate
from . import section_tally
from .didcomm_schema import MESSAGE_TYPE, SECTION_FIELDS
from .input_format import validate_input
from .paths import CASES

# The answer key's vocabulary: what any correct cart must satisfy (scoring.check_rules()
# checks each one). Always checked as well: the student_id and term_code, and that no section
# is waitlisted (no request asks to be put on a waitlist).
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

# The two halves of a counterfactual pair (see pairs.py).
PAIR_VARIANTS = ("a", "b")


# ── Loading ──

def resolve_case(case: dict) -> dict:
    """The full input object of a case: built from its degree program (degree_requirements.json)
    with its transcript fabricated from the case's spec (fabricate.build_input()), and the whole
    Section Tally as its schedule, so validation, the checker's database and scoring see every
    section, as the model does in the session's opening. (`candidate_courses` is metadata for the
    generator and the chance baseline; nothing here reads it.)"""
    data = fabricate.build_input(case["input"])
    if "schedule" in data:
        raise ValueError(f"{case.get('case_id', '<no id>')}: the schedule is the whole Section Tally; "
                         f"the case must not write one out")
    data = {key: value for key, value in data.items() if key != "prompt"}
    data["schedule"] = section_tally.full_schedule()
    data["prompt"] = case["input"]["prompt"]     # last, as in the input format
    return {**case, "input": data}


def read_cases(path=CASES) -> list:
    """The cases in a file as written (not resolved): cheap, for ids, pairs and metadata."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_test_cases(path=CASES, task_ids=None) -> list:
    """The cases in a file (only `task_ids`, if given), each resolved to its full input object."""
    cases = read_cases(path)
    if task_ids:
        cases = [case for case in cases if case.get("case_id") in task_ids]
    return [resolve_case(case) for case in cases]


# ── The answer key's carts as messages ──

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
    """The register message for this cart, exactly as a correct model answer would write it;
    entries may be crns or section objects. Used to score the answer key's own examples."""
    data = case["input"]
    by_crn = {s["crn"]: s for s in data["schedule"]["sections"]}
    return json.dumps({"id": "n/a", "type": MESSAGE_TYPE, "body": {
        "student_id": data["student"]["student_id"], "term_code": data["schedule"]["term_code"],
        "sections": [_full_item(entry, by_crn) for entry in cart]}})


# ── Is a case sound? ──

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


# ── Tying a result to the exact case version ──

def case_hashes(case: dict) -> dict:
    """Hashes of what the model saw (the case's input, as the case file writes it) and of its
    answer key's rules, so a result can be matched to the exact case version it was run on."""
    digest = lambda obj: hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return {"input": digest(case.get("input")), "rules": digest((case.get("ground_truth") or {}).get("rules"))}


def case_subjects(raw_case: dict) -> set:
    """The subjects a case's answer can come from: those of its candidate courses and of every
    section its answer key names. Used by --subjects auto to shrink the opening for dev runs."""
    by_crn = {s["crn"]: s for s in section_tally.full_schedule()["sections"]}
    courses = set(raw_case.get("candidate_courses", []))
    examples = (raw_case.get("ground_truth") or {}).get("examples", {})
    for cart in examples.get("valid", []) + examples.get("invalid", []):
        courses |= {by_crn[_item(e)["crn"]]["course_id"] for e in cart if _item(e)["crn"] in by_crn}
    return {section_tally.subject_of(c) for c in courses}
