"""
input_format.py
---------------
Validation of the INPUT FORMAT: one JSON object per registration situation (student,
transcript, program requirements, schedule, prompt; README.md, "The input format").

Two layers:
  1. registration_input.schema.json (JSON Schema 2020-12): the shape of every field.
  2. validate_input(), below: the rules a schema cannot say, e.g. "no crn twice" or "a
     meeting ends after it starts".

Every case carries the whole Section Tally (5,129 sections) as its schedule, so the schedule
is by far the slowest part to validate. It is the same object for every case
(section_tally.full_schedule() is cached), so its result is remembered: see _schedule_problems().
"""
import json
from jsonschema import Draft202012Validator

from .paths import INPUT_SCHEMA as INPUT_SCHEMA_PATH

INPUT_SCHEMA = json.loads(INPUT_SCHEMA_PATH.read_text(encoding="utf-8"))
Draft202012Validator.check_schema(INPUT_SCHEMA)
_INPUT = Draft202012Validator(INPUT_SCHEMA)

# The requirement rules that count credits (the others count courses).
CREDIT_RULES = ("credits_from", "credits_with_attribute", "credits_any")


def minutes(clock: str) -> int:
    """'1:05 PM' -> 785 (minutes after midnight). Used by every time check in the pipeline."""
    time, half = clock.split()
    hour, minute = map(int, time.split(":"))
    return (hour % 12 + (12 if half == "PM" else 0)) * 60 + minute


def _duplicates(values) -> list:
    """Values that occur more than once, in first-seen order."""
    seen, repeated = set(), []
    for v in values:
        if v in seen and v not in repeated:
            repeated.append(v)
        seen.add(v)
    return repeated


def _schema_problems(data) -> list:
    """Layer 1: every JSON Schema violation as '<path>: <reason>', sorted by path."""
    found = sorted(_INPUT.iter_errors(data), key=lambda e: list(map(str, e.absolute_path)))
    return [f"{'/'.join(map(str, e.absolute_path)) or '<input>'}: {e.message}" for e in found]


def _section_problems(sections: list) -> list:
    """Layer 2 for each section on its own: seats, credit range, meeting times."""
    problems = []
    for s in sections:
        if s["seats_taken"] > s["seats_total"]:
            problems.append(f"crn {s['crn']}: seats_taken {s['seats_taken']} "
                            f"is more than seats_total {s['seats_total']}")
        credits = s["credit_hours"]
        if isinstance(credits, dict) and credits["min"] > credits["max"]:
            problems.append(f"crn {s['crn']}: credit_hours min is more than max")
        for m in s["meetings"]:
            if "start_time" in m and minutes(m["end_time"]) <= minutes(m["start_time"]):
                problems.append(f"crn {s['crn']}: meeting ends ({m['end_time']}) "
                                f"at or before it starts ({m['start_time']})")
    return problems


# Schedules already found to have no problems, by id(). The object itself is kept as the value
# so its id cannot be reused by another object. Schedules are read-only once built.
_CLEAN_SCHEDULES: dict = {}


def _schedule_is_clean(schedule) -> bool:
    """True when this exact schedule object has been validated before and had no problems."""
    return _CLEAN_SCHEDULES.get(id(schedule)) is schedule


def validate_input(data) -> list:
    """Returns a list of problems; empty means the input object is valid.

    The schedule's sections are checked independently of the rest of the object (the schema
    validates each section on its own, and no rule below compares a section with another
    field), so a schedule that was clean once is not checked again: the rest is validated with
    an empty section list in its place. The problems reported are the same either way."""
    schedule = data.get("schedule") if isinstance(data, dict) else None
    if isinstance(schedule, dict) and _schedule_is_clean(schedule):
        return _validate({**data, "schedule": {**schedule, "sections": []}})
    problems = _validate(data)
    if not problems and isinstance(schedule, dict):
        _CLEAN_SCHEDULES[id(schedule)] = schedule
    return problems


def _validate(data) -> list:
    """Both layers on one input object (validate_input() decides what to pass in)."""
    problems = _schema_problems(data)
    if problems:
        return problems          # the checks below assume the shape is right

    # Layer 2, in a fixed order: duplicates, requirements, the guide, then each section.
    sections = data["schedule"]["sections"]
    problems = [f"crn {crn} is used by more than one section" for crn in _duplicates(s["crn"] for s in sections)]
    for req_id in _duplicates(r["req_id"] for r in data["program_requirements"]):
        problems.append(f"req_id {req_id} is used by more than one requirement")
    for course in _duplicates(p["course_id"] for p in data.get("prerequisites", [])):
        problems.append(f"course {course} has more than one prerequisites entry")
    for course in _duplicates(c for group in data.get("course_equivalents", []) for c in group):
        problems.append(f"course {course} is in more than one course_equivalents group")
    problems += _requirement_problems(data["program_requirements"])
    problems += _guide_problems(data.get("program_guide"))
    return problems + _section_problems(sections)


def _requirement_problems(requirements: list) -> list:
    """Each requirement's fields fit its rule; free electives (credits_any) come last."""
    problems = []
    for r in requirements:
        if (r["rule"] == "credits_with_attribute") != ("attribute" in r):
            problems.append(f"requirement {r['req_id']}: `attribute` goes with the "
                            f"credits_with_attribute rule, and only with it")
        if "credit_caps" in r and r["rule"] not in CREDIT_RULES:
            problems.append(f"requirement {r['req_id']}: credit_caps only applies to the credit rules")
        if (r["rule"] == "one_of_groups") != ("course_groups" in r):
            problems.append(f"requirement {r['req_id']}: `course_groups` goes with the "
                            f"one_of_groups rule, and only with it")
        if "course_ranges" in r and r["rule"] not in ("any_of", "credits_from"):
            problems.append(f"requirement {r['req_id']}: course_ranges only applies to any_of and credits_from")
        if r["rule"] in ("one_of_groups", "credits_any", "credits_with_attribute") and r["course_options"]:
            problems.append(f"requirement {r['req_id']}: course_options must be empty for {r['rule']}")
        if r["rule"] in ("all_of", "any_of", "credits_from") and not (r["course_options"] or r.get("course_ranges")):
            problems.append(f"requirement {r['req_id']}: {r['rule']} needs course_options or course_ranges")
        for cr in r.get("course_ranges", []):
            if cr.get("min_number", 0) > cr.get("max_number", 999):
                problems.append(f"requirement {r['req_id']}: a course range's min_number is more than its max_number")
    any_course = [r["req_id"] for r in requirements if r["rule"] == "credits_any"]
    if any_course and any_course[-1] != requirements[-1]["req_id"]:
        problems.append(f"requirement {any_course[-1]}: credits_any (free electives) must come last, "
                        f"so every exclusive requirement takes its courses first")
    return problems


def _guide_problems(guide) -> list:
    """The program guide's sections add up to its total_credits (a range when a section varies)."""
    if not guide:
        return []
    low = sum(sec["credit_hours"]["min"] if isinstance(sec["credit_hours"], dict) else sec["credit_hours"]
              for sec in guide["sections"])
    high = sum(sec["credit_hours"]["max"] if isinstance(sec["credit_hours"], dict) else sec["credit_hours"]
               for sec in guide["sections"])
    if not low <= guide["total_credits"] <= high:
        return [f"program_guide: sections add up to {low:g}-{high:g} credits, "
                f"not total_credits {guide['total_credits']:g}"]
    return []
