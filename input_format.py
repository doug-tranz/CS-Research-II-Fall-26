"""
input_format.py
----------------
The input format (docs/input-format.md): one JSON object per registration situation.
validate_input() checks it against registration_input.schema.json plus the rules a schema
cannot express. minutes() parses the format's clock times.
"""
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

INPUT_SCHEMA = json.loads(
    Path(__file__).with_name("registration_input.schema.json").read_text(encoding="utf-8"))
Draft202012Validator.check_schema(INPUT_SCHEMA)
_INPUT = Draft202012Validator(INPUT_SCHEMA)
_SCHEDULE = Draft202012Validator({"$ref": "#/$defs/schedule", "$defs": INPUT_SCHEMA["$defs"]})

CREDIT_RULES = ("credits_from", "credits_with_attribute", "credits_any")

def minutes(clock: str) -> int:
    """'1:05 PM' -> 785 (minutes after midnight)."""
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

# The schedule's findings, by a fingerprint of its contents. In a session every case carries
# the same 5,129-section schedule; checking it once instead of once per case is most of
# --check's run time. Keyed by content, so a changed schedule is never served a stale result.
_SCHEDULE_FINDINGS = {}


def _schedule_findings(schedule) -> tuple:
    """(schema errors as (path, problem) pairs, duplicate-crn problems, per-section problems)
    for one schedule; the last two only when its shape is valid."""
    try:
        key = hashlib.sha256(json.dumps(schedule, sort_keys=True).encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        key = None                                   # not plain JSON: check it, don't cache it
    if key in _SCHEDULE_FINDINGS:
        return _SCHEDULE_FINDINGS[key]
    errors = [(["schedule"] + list(map(str, e.absolute_path)), e.message) for e in _SCHEDULE.iter_errors(schedule)]
    crns, per_section = [], []
    if not errors:
        sections = schedule["sections"]
        crns = [f"crn {crn} is used by more than one section" for crn in _duplicates(s["crn"] for s in sections)]
        for s in sections:
            if s["seats_taken"] > s["seats_total"]:
                per_section.append(f"crn {s['crn']}: seats_taken {s['seats_taken']} "
                                   f"is more than seats_total {s['seats_total']}")
            credits = s["credit_hours"]
            if isinstance(credits, dict) and credits["min"] > credits["max"]:
                per_section.append(f"crn {s['crn']}: credit_hours min is more than max")
            for m in s["meetings"]:
                if "start_time" in m and minutes(m["end_time"]) <= minutes(m["start_time"]):
                    per_section.append(f"crn {s['crn']}: meeting ends ({m['end_time']}) "
                                       f"at or before it starts ({m['start_time']})")
    found = (errors, crns, per_section)
    if key is not None:
        if len(_SCHEDULE_FINDINGS) > 512:
            _SCHEDULE_FINDINGS.clear()
        _SCHEDULE_FINDINGS[key] = found
    return found


def validate_input(data) -> list:
    """Returns a list of problems; empty means the input object is valid.

    The schedule is checked on its own (_schedule_findings, cached by content) and the rest of
    the object against the schema with the schedule set aside; the schema has no rule linking
    the schedule to another field, so this finds exactly what checking the whole object does."""
    split = isinstance(data, dict) and "schedule" in data
    rest = {**data, "schedule": {}} if split else data
    errors = [(list(map(str, e.absolute_path)), e.message) for e in _INPUT.iter_errors(rest)
              if not (split and list(e.absolute_path)[:1] == ["schedule"])]
    schedule_errors, crns, per_section = _schedule_findings(data["schedule"]) if split else ([], [], [])
    found = sorted(errors + schedule_errors, key=lambda e: e[0])
    problems = [f"{'/'.join(path) or '<input>'}: {message}" for path, message in found]
    if problems:
        return problems          # the checks below assume the shape is right

    problems += crns
    for req_id in _duplicates(r["req_id"] for r in data["program_requirements"]):
        problems.append(f"req_id {req_id} is used by more than one requirement")
    for course in _duplicates(p["course_id"] for p in data.get("prerequisites", [])):
        problems.append(f"course {course} has more than one prerequisites entry")
    for course in _duplicates(c for group in data.get("course_equivalents", []) for c in group):
        problems.append(f"course {course} is in more than one course_equivalents group")
    for r in data["program_requirements"]:
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
    any_course = [r["req_id"] for r in data["program_requirements"] if r["rule"] == "credits_any"]
    if any_course and any_course[-1] != data["program_requirements"][-1]["req_id"]:
        problems.append(f"requirement {any_course[-1]}: credits_any (free electives) must come last, "
                        f"so every exclusive requirement takes its courses first")
    guide = data.get("program_guide")
    if guide:
        low = sum(sec["credit_hours"]["min"] if isinstance(sec["credit_hours"], dict) else sec["credit_hours"]
                  for sec in guide["sections"])
        high = sum(sec["credit_hours"]["max"] if isinstance(sec["credit_hours"], dict) else sec["credit_hours"]
                   for sec in guide["sections"])
        if not low <= guide["total_credits"] <= high:
            problems.append(f"program_guide: sections add up to {low:g}-{high:g} credits, "
                            f"not total_credits {guide['total_credits']:g}")
    return problems + per_section
