"""
run_semantic_eval.py
---------------------
The evaluation pipeline: reads a file of test cases, builds each case's prompt
from the input format (README.md, "The input format"), sends it to a model,
and scores the answer through all four tiers:

  Tiers 1-2  syntactic + schema   didcomm_schema.validate_message()  } via
  Tier 3     consistency          registration.check_consistency()   } registration.evaluate()
  Tier 4     semantic             check_semantic(), below

(This file also holds what used to be semantic_check.py and
evaluate_with_semantics.py.)

The model NEVER sees `ground_truth`. It is loaded separately and used only
here, after generation, per harness.py's own design comment on GroundTruth.

A case file is an indented JSON array of cases, each one input object
(student, transcript, program requirements, schedule, prompt) wrapped with a
case_id and an answer key:
    {"case_id": "tal_001", "input": {...},
     "ground_truth": {"acceptable_crns": ["12345"], "implicit_relationship": "..."}}

HOW TO RUN:
    python run_semantic_eval.py --model anthropic/claude-opus-5.5
    python run_semantic_eval.py --model anthropic/claude-opus-5.5 --task-ids tal_001 -n 5
    python run_semantic_eval.py --model anthropic/claude-opus-5.5 --rendering nl
    python run_semantic_eval.py --check      # scores every answer key; no model, no cost

Requires OPENROUTER_API_KEY to be set (or --backend ollama).
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
from harness import TaskSpec, OllamaBackend, OpenRouterBackend, generate, generate_with_tools, parse_didcomm
import registration as reg
import section_tally
from didcomm_schema import OUTPUT_CONTRACT, CRN_CONTRACT, MESSAGE_TYPE

RENDERINGS = ("json", "csv", "nl")


# ── Tier 4: semantic check ──
# Tier 3 only checks invariants: does each section exist, is there a seat, no time
# conflict, prerequisites met. It never checks whether the model chose the CORRECT
# student and sections for what the intent asked. A cart can pass every consistency
# check and still be simply WRONG; this tier catches that, by comparing the body
# against a known-correct message. The cart is compared as a SET: order does not matter.
# Per section: the crn must match, `waitlist` must match (absent = false), and
# `credit_hours` must match when the expected message gives it.

def _normalize(value):
    """Case- and whitespace-insensitive."""
    return str(value).strip().lower()


def _compare_sections(actual, expected) -> dict:
    """What differs between two carts of section objects; {} when they agree."""
    have = {_normalize(item.get("crn")): item for item in actual if isinstance(item, dict)}
    want = {_normalize(item["crn"]): item for item in expected}
    problems = {}
    if sorted(want) != sorted(have):
        problems["missing"] = sorted(set(want) - set(have))
        problems["unexpected"] = sorted(set(have) - set(want))
    for crn in sorted(set(want) & set(have)):
        if bool(want[crn].get("waitlist")) != bool(have[crn].get("waitlist")):
            problems.setdefault("waitlist", {})[crn] = {
                "expected": bool(want[crn].get("waitlist")), "actual": bool(have[crn].get("waitlist"))}
        if "credit_hours" in want[crn] and have[crn].get("credit_hours") != want[crn]["credit_hours"]:
            problems.setdefault("credit_hours", {})[crn] = {
                "expected": want[crn]["credit_hours"], "actual": have[crn].get("credit_hours")}
    return {key: value for key, value in problems.items() if value}


def check_semantic(body: dict, expected_raw: str) -> dict:
    """
    body: the model's parsed DIDComm body (already passed schema + consistency)
    expected_raw: a known-correct DIDComm message, as a JSON string

    Returns {"outcome": "semantic_ok" | "semantic_error", "detail": str,
             "deviation_score": float | None}
    deviation_score: fraction of expected fields that didn't match (0.0 = perfect).
    """
    if not expected_raw:
        return {"outcome": "semantic_error",
                "detail": "no ground truth available for this task_id — cannot score intent",
                "deviation_score": None}

    expected_body, err = parse_didcomm(expected_raw)
    if expected_body is None:
        return {"outcome": "semantic_error",
                "detail": f"ground truth itself failed to parse: {err}",
                "deviation_score": None}

    mismatches = {}
    for key, expected_value in expected_body.items():
        actual_value = body.get(key)
        if key == "sections":
            differs = _compare_sections(actual_value or [], expected_value)
            if differs:
                mismatches[key] = differs
        elif _normalize(actual_value) != _normalize(expected_value):
            mismatches[key] = {"expected": expected_value, "actual": actual_value}

    deviation_score = len(mismatches) / max(len(expected_body), 1)
    if mismatches:
        return {"outcome": "semantic_error",
                "detail": f"field mismatch vs. ground truth: {mismatches}",
                "deviation_score": deviation_score}
    return {"outcome": "semantic_ok", "detail": "matches ground truth exactly", "deviation_score": 0.0}


# ── Tiers 1-4 for one answer ──

def evaluate_full(conn, raw_output: str, expected_raw: str = "") -> dict:
    """registration.evaluate() (Tiers 1-3), then Tier 4 ONLY when Tiers 1-3 passed."""
    base = reg.evaluate(conn, raw_output)
    if base["outcome"] != "consistent__semantic_TBD":
        return base                  # failed at Tier 1-3: keep that verdict as it is

    body, _ = parse_didcomm(raw_output)
    semantic_result = check_semantic(body, expected_raw)
    return {
        "outcome": semantic_result["outcome"],
        "detail": semantic_result["detail"],
        "deviation_score": semantic_result["deviation_score"],
        "tier3_detail": base["detail"],  # keep the consistency reasoning too
    }


# ── Test cases and their answer keys ──
# The answer key is a SET of valid outcomes, given one of two ways:
#   acceptable_crns / acceptable_carts  every correct cart, listed. `acceptable_crns` lists
#       sections that are each a correct one-section cart; `acceptable_carts` lists whole
#       carts. A section is written as its crn ("41836"), or as an object when the answer
#       needs more: {"crn": "45510", "credit_hours": 3} or {"crn": "41909", "waitlist": true}.
#   rules  what any correct cart must satisfy (RULES below), for requests with too many
#       correct carts to list, e.g. "two classes that count toward my degree". With rules,
#       `examples` gives carts that must pass ("valid") and must fail ("invalid").

TALLY_KEYS = {"crns", "courses", "include_cancelled", "search"}

# Told to the model in search mode, after the output contract.
SEARCH_CONTRACT = (
    " The sections offered this term are NOT listed above. Look them up with the "
    "search_sections tool, which searches the whole Section Tally for the term; only "
    "sections it returns exist. Search as often as you need, then reply with the message."
)


def is_search(case: dict) -> bool:
    """True when the model must find sections itself with the search tool."""
    return bool((case.get("schedule_from_tally") or {}).get("search"))


def resolve_case(case: dict) -> dict:
    """A case may take its schedule from the Section Tally instead of writing it out:
        "schedule_from_tally": {"search": true}
            the model gets no list of sections and must search the whole tally itself;
            the checker sees every section (input.schedule = the whole tally)
        "schedule_from_tally": {"crns": [...], "courses": [...], "include_cancelled": false}
            a hand-picked portion, listed in the prompt
    This fills in input.schedule from the Section Tally export (section_tally.TALLY_CSV),
    so everything after it (validation, database, scoring) sees an ordinary input object."""
    ref = case.get("schedule_from_tally")
    if ref is None:
        return case
    name = case.get("case_id", "<no id>")
    if not isinstance(ref, dict) or set(ref) - TALLY_KEYS:
        raise ValueError(f"{name}: schedule_from_tally takes search, or crns and/or courses "
                         f"(and include_cancelled), nothing else")
    if ref.get("search") and set(ref) != {"search"}:
        raise ValueError(f"{name}: with search, the model picks the sections; give no crns or courses")
    if not ref.get("search") and not (ref.get("crns") or ref.get("courses")):
        raise ValueError(f"{name}: schedule_from_tally needs search: true, or crns and/or courses")
    if "schedule" in case["input"]:
        raise ValueError(f"{name}: give input.schedule or schedule_from_tally, not both")
    data = {key: value for key, value in case["input"].items() if key != "prompt"}
    data["schedule"] = (section_tally.full_schedule() if ref.get("search")
                        else section_tally.select_schedule(**ref))
    data["prompt"] = case["input"]["prompt"]
    return {**case, "input": data}


def load_test_cases(path="input_format_cases.json", task_ids=None) -> list:
    """The cases in a file (only `task_ids`, if given), with any schedule_from_tally filled in.
    Cases are chosen BEFORE the tally is read, so a case that does not use it never fails on it."""
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if task_ids:
        cases = [case for case in cases if case.get("case_id") in task_ids]
    return [resolve_case(case) for case in cases]


RULES = {
    "count": "number of sections, {min, max}",
    "credits": "total credits, {min, max}",
    "courses_count_toward": "req_ids: every section's course must move one of these unmet requirements forward",
    "no_days": "days no meeting may fall on, e.g. [\"Friday\"]",
    "starts_before": "every meeting starts before this time, e.g. \"5:00 PM\"",
    "ends_by": "every meeting ends by this time",
    "modality_in": "allowed modalities",
    "campus_in": "allowed campuses",
    "waitlist": "true if the student allowed waitlisting; default false",
    "must_include": "sections (crns or section objects) the cart must contain",
    "must_include_courses": "course ids the cart must contain",
    "must_exclude_courses": "course ids the cart must not contain",
}


def _item(entry) -> dict:
    return {"crn": entry} if isinstance(entry, str) else dict(entry)


def acceptable_carts(case: dict) -> list:
    """Every correct cart for a case, each as a list of section objects."""
    truth = case["ground_truth"]
    carts = [[_item(entry)] for entry in truth.get("acceptable_crns", [])]
    return carts + [[_item(entry) for entry in cart] for cart in truth.get("acceptable_carts", [])]


def expected_message(case: dict, cart: list) -> str:
    """The register message for this cart; entries may be crns or section objects."""
    data = case["input"]
    return json.dumps({"id": "n/a", "type": MESSAGE_TYPE, "body": {
        "student_id": data["student"]["student_id"], "term_code": data["schedule"]["term_code"],
        "sections": [_item(entry) for entry in cart]}})


def validate_case(case) -> list:
    """Returns a list of problems with a test case; empty means it is sound."""
    if not isinstance(case, dict):
        return ["case is not a JSON object"]
    problems = [f"{field} is missing" for field in ("case_id", "input", "ground_truth")
                if field not in case]
    if problems:
        return problems
    problems = [f"input: {p}" for p in reg.validate_input(case["input"])]
    if problems:
        return problems

    truth = case["ground_truth"]
    offered = {s["crn"] for s in case["input"]["schedule"]["sections"]}
    carts = acceptable_carts(case)
    if "rules" in truth:
        unknown = sorted(set(truth["rules"]) - set(RULES))
        if unknown:
            problems.append(f"unknown rules {unknown}; known: {sorted(RULES)}")
        req_ids = {r["req_id"] for r in case["input"]["program_requirements"]}
        for req in truth["rules"].get("courses_count_toward", []):
            if req not in req_ids:
                problems.append(f"rule courses_count_toward names {req}, which is not a requirement")
        examples = truth.get("examples", {})
        if not examples.get("valid"):
            problems.append("a rules answer key needs at least one valid example cart")
        carts = carts + examples.get("valid", []) + examples.get("invalid", [])
        carts = [[_item(entry) for entry in cart] for cart in carts]
    elif not carts:
        problems.append("ground_truth lists no acceptable_crns, acceptable_carts or rules")
    for cart in carts:
        if not cart:
            problems.append("ground_truth has an empty cart")
        for item in cart:
            if item["crn"] not in offered:
                problems.append(f"ground_truth names crn {item['crn']}, which is not in the schedule")
    return problems


def _credits(item: dict, section: dict):
    if "credit_hours" in item:
        return item["credit_hours"]
    credits = section["credit_hours"]
    return credits["min"] if isinstance(credits, dict) else credits


def check_rules(body: dict, case: dict) -> dict:
    """Tier 4 for a rules answer key. Assumes Tiers 1-3 passed, so every crn exists."""
    data, rules = case["input"], case["ground_truth"]["rules"]
    by_crn = {s["crn"]: s for s in data["schedule"]["sections"]}
    chosen = [(item, by_crn[item["crn"]]) for item in body["sections"]]
    meetings = [(item["crn"], m) for item, s in chosen for m in s["meetings"]]
    violated = {}

    if _normalize(body.get("student_id")) != _normalize(data["student"]["student_id"]):
        violated["student_id"] = f"expected {data['student']['student_id']}"
    if body.get("term_code") != data["schedule"]["term_code"]:
        violated["term_code"] = f"expected {data['schedule']['term_code']}"
    if "count" in rules:
        low, high = rules["count"].get("min", 0), rules["count"].get("max", 10 ** 6)
        if not low <= len(chosen) <= high:
            violated["count"] = f"{len(chosen)} sections, wanted {rules['count']}"
    if "credits" in rules:
        total = sum(_credits(item, s) for item, s in chosen)
        low, high = rules["credits"].get("min", 0), rules["credits"].get("max", 10 ** 6)
        if not low <= total <= high:
            violated["credits"] = f"{total:g} credits, wanted {rules['credits']}"
    if "courses_count_toward" in rules:
        wanted = set(rules["courses_count_toward"])
        no = [s["crn"] for _, s in chosen
              if not wanted & set(reg.counts_toward(data, s["course_id"], s.get("attributes", [])))]
        if no:
            violated["courses_count_toward"] = f"{no} do not move {sorted(wanted)} forward"
    if "no_days" in rules:
        bad = sorted({crn for crn, m in meetings if set(m["days"]) & set(rules["no_days"])})
        if bad:
            violated["no_days"] = f"{bad} meet on {rules['no_days']}"
    if "starts_before" in rules:
        limit = reg.minutes(rules["starts_before"])
        bad = sorted({crn for crn, m in meetings if "start_time" in m and reg.minutes(m["start_time"]) >= limit})
        if bad:
            violated["starts_before"] = f"{bad} start at or after {rules['starts_before']}"
    if "ends_by" in rules:
        limit = reg.minutes(rules["ends_by"])
        bad = sorted({crn for crn, m in meetings if "end_time" in m and reg.minutes(m["end_time"]) > limit})
        if bad:
            violated["ends_by"] = f"{bad} end after {rules['ends_by']}"
    for key, field in (("modality_in", "modality"), ("campus_in", "campus")):
        if key in rules:
            bad = [s["crn"] for _, s in chosen if s[field] not in rules[key]]
            if bad:
                violated[key] = f"{bad} are not {rules[key]}"
    if not rules.get("waitlist", False):
        bad = [item["crn"] for item, _ in chosen if item.get("waitlist")]
        if bad:
            violated["waitlist"] = f"{bad} waitlisted, but the student did not ask to be waitlisted"
    if "must_include" in rules:
        differs = _compare_sections(body["sections"], [_item(e) for e in rules["must_include"]])
        differs.pop("unexpected", None)                   # other sections are allowed
        if differs.get("missing") == []:
            differs.pop("missing")
        if differs:
            violated["must_include"] = differs
    courses = {s["course_id"] for _, s in chosen}
    if "must_include_courses" in rules and set(rules["must_include_courses"]) - courses:
        violated["must_include_courses"] = f"missing {sorted(set(rules['must_include_courses']) - courses)}"
    if "must_exclude_courses" in rules and set(rules["must_exclude_courses"]) & courses:
        violated["must_exclude_courses"] = f"has {sorted(set(rules['must_exclude_courses']) & courses)}"

    checked = len(rules) + 2 + ("waitlist" not in rules)      # + student_id, term_code, waitlist default
    if violated:
        return {"outcome": "semantic_error", "detail": f"rules broken: {violated}",
                "deviation_score": len(violated) / checked, "violated": sorted(violated)}
    return {"outcome": "semantic_ok", "detail": "satisfies every rule", "deviation_score": 0.0}


def evaluate_case(case: dict, raw_output: str) -> dict:
    """Tiers 1-4 for a test case. Tier 4 passes when the cart equals ANY acceptable cart, or,
    for a rules answer key, when it satisfies every rule."""
    conn = reg.seed_db(case["input"])                  # read-only from here: seed once
    if "rules" in case["ground_truth"]:
        base = reg.evaluate(conn, raw_output)
        if base["outcome"] != "consistent__semantic_TBD":
            return base
        body, _ = parse_didcomm(raw_output)
        return {**check_rules(body, case), "tier3_detail": base["detail"]}
    verdicts = [evaluate_full(conn, raw_output, expected_message(case, cart))
                for cart in acceptable_carts(case)]
    for verdict in verdicts:
        if verdict["outcome"] != "semantic_error":
            return verdict           # semantic_ok, or a Tier 1-3 failure (same for every cart)
    return min(verdicts, key=lambda v: v["deviation_score"])   # the closest acceptable cart


def self_check(case: dict) -> list:
    """Scores a case's own answer key, with no model. Returns problems; empty means it behaves."""
    problems = validate_case(case)
    if problems:
        return problems
    truth = case["ground_truth"]
    if "rules" in truth:
        for cart in truth.get("examples", {}).get("valid", []):
            verdict = evaluate_case(case, expected_message(case, cart))
            if verdict["outcome"] != "semantic_ok":
                problems.append(f"valid example {cart} scores {verdict['outcome']}: {verdict['detail']}")
        for cart in truth.get("examples", {}).get("invalid", []):
            if evaluate_case(case, expected_message(case, cart))["outcome"] == "semantic_ok":
                problems.append(f"invalid example {cart} scores semantic_ok")
        return problems
    carts = acceptable_carts(case)
    for cart in carts:
        verdict = evaluate_case(case, expected_message(case, cart))
        if verdict["outcome"] != "semantic_ok":
            problems.append(f"acceptable cart {cart} scores {verdict['outcome']}: {verdict['detail']}")
    # When every answer is a single section, no other single section may pass. (Skipped for a
    # whole-tally schedule: thousands of sections, and an exact-match key cannot pass another.)
    sections = case["input"]["schedule"]["sections"]
    if all(len(cart) == 1 for cart in carts) and len(sections) <= 200:
        accepted = {cart[0]["crn"] for cart in carts}
        for s in sections:
            if s["crn"] not in accepted:
                verdict = evaluate_case(case, expected_message(case, [s["crn"]]))
                if verdict["outcome"] == "semantic_ok":
                    problems.append(f"crn {s['crn']} is not in the answer key but scores semantic_ok")
    return problems


# ── What the model sees: three renderings of the SAME state (spec open item 8) ──

def _meeting_text(m: dict, joiner: str = "/", to: str = "-") -> str:
    """'Monday/Wednesday 09:30 AM-10:45 AM, Robinson Hall 207 (Class)'."""
    when = f"{m['start_time']}{to}{m['end_time']}" if "start_time" in m else "time to be arranged"
    where = f", {m['location']}" if "location" in m else ""
    kind = f" ({m['type']})" if "type" in m else ""
    return f"{joiner.join(m['days'])} {when}{where}{kind}"


def _credits_text(credits) -> str:
    return f"{credits['min']}-{credits['max']}" if isinstance(credits, dict) else f"{credits:g}"


def _cell(key: str, value) -> str:
    """One CSV cell. Lists are joined with '|'; nested rows with '; '."""
    if key == "meetings":
        return "; ".join(_meeting_text(m) for m in value) or "none scheduled"
    if key == "credit_caps":
        return "; ".join(f"max {cap['max_credits']:g} from {'|'.join(cap['course_options'])}" for cap in value)
    if key == "credit_hours":
        return _credits_text(value)
    if isinstance(value, list):
        return "|".join(map(str, value))
    if isinstance(value, dict):                       # waitlist, reserved_seats
        return "; ".join(f"{k} {v}" for k, v in value.items())
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
        (f"sections offered in {state['schedule']['term']}", state["schedule"].get("sections", [])),
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
    note = f" ({s['title_note']})" if s.get("title_note") else ""
    times = "; ".join(_meeting_text(m, " and ", " to ") for m in s["meetings"])
    extra = []
    if s.get("instructor"):
        extra.append(f"Instructor: {s['instructor']}.")
    if s.get("waitlist"):
        extra.append(f"Waitlist: {s['waitlist']['enrolled']} of {s['waitlist']['capacity']}.")
    if s.get("reserved_seats"):
        extra.append(f"Reserved seats: {s['reserved_seats']['remaining']} of {s['reserved_seats']['total']} left.")
    if s.get("attributes"):
        extra.append(f"Attributes: {', '.join(s['attributes'])}.")
    if s.get("cancelled"):
        extra.append("CANCELLED.")
    session = f" ({s['session']})" if s.get("session") else ""
    return (f"- CRN {s['crn']}: {s['course_id']} section {s['section']}{title}{note}, "
            f"{_credits_text(s['credit_hours'])} credits, {s['modality']}{session} at {s['campus']}, "
            f"{times or 'no scheduled meeting times'}. "
            f"{s['seats_taken']} of {s['seats_total']} seats are taken." + "".join(f" {e}" for e in extra))


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
    lines += ["", f"Term: {state['schedule']['term']} (term_code {state['schedule']['term_code']})."]
    if "sections" in state["schedule"]:
        lines += [f"Sections offered in {state['schedule']['term']}:"]
        lines += [_section_text(s) for s in state["schedule"]["sections"]] or ["- none"]
    return "\n".join(lines)


def to_task(case: dict, rendering: str = "json") -> TaskSpec:
    """What the model sees: the whole input object, never the answer key.
    `prompt` becomes the intent; the rest is the known state."""
    data = case["input"]
    state = {key: value for key, value in data.items() if key != "prompt"}
    if is_search(case):              # the model finds sections itself: send only the term
        state["schedule"] = {"term": data["schedule"]["term"], "term_code": data["schedule"]["term_code"]}
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
        output_contract=OUTPUT_CONTRACT + CRN_CONTRACT + (SEARCH_CONTRACT if is_search(case) else ""),
    )


# ── Logging ──

def readable_prompt(prompt_sent: str) -> dict:
    """prompt_sent split into its parts, for reading: the known state (an object when it was
    sent as JSON, otherwise its lines), the intent, and the output contract.
    The layout is harness.build_messages(): 'Known state (JSON):\\n<state>\\n\\nIntent: <intent>\\n\\n<contract>'."""
    head = "Known state (JSON):\n"
    if not prompt_sent.startswith(head) or "\n\nIntent: " not in prompt_sent:
        return {"text": prompt_sent.splitlines()}
    state, _, rest = prompt_sent[len(head):].partition("\n\nIntent: ")
    intent, _, contract = rest.partition("\n\n")
    try:
        state = json.loads(state)
    except json.JSONDecodeError:
        state = state.splitlines()           # the csv and nl renderings
    return {"known_state": state, "intent": intent, "output_contract": contract}


def readable_row(row: dict, verdict: dict) -> dict:
    """Adds the prompt and the model's message as parsed objects, right after the verbatim
    strings they come from; prompt_sent and raw_output stay exactly as sent and received."""
    try:
        parsed = json.loads(row["raw_output"])
    except json.JSONDecodeError:
        parsed = None
    out = {}
    for key, value in row.items():
        if key in ("prompt_readable", "parsed_output", "verdict"):
            continue
        out[key] = value
        if key == "prompt_sent":
            out["prompt_readable"] = readable_prompt(value)
        if key == "raw_output":
            out["parsed_output"] = parsed
    out["verdict"] = verdict
    return out


def log_readable(result, verdict: dict, path: str) -> None:
    """Like harness.log_result(), but keeps the file as one indented JSON array."""
    file = Path(path)
    rows = json.loads(file.read_text(encoding="utf-8")) if file.exists() else []
    rows.append(readable_row(asdict(result), verdict))
    file.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


# ── Runner ──

def check_cases(cases: list) -> int:
    """Scores every case's own answer key. No model call. Returns the failure count."""
    failed = 0
    for case in cases:
        problems = self_check(case)
        failed += bool(problems)
        print(f"{case.get('case_id', '<no id>')}: {'FAIL' if problems else 'ok'}")
        for p in problems:
            print(f"    - {p}")
    print(f"\n{len(cases) - failed}/{len(cases)} cases sound.")
    return failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["ollama", "openrouter"], default="openrouter")
    parser.add_argument("--model", help="required unless --check")
    parser.add_argument("--test-file", default="input_format_cases.json")
    parser.add_argument("--output", default="semantic_eval_results.json")
    parser.add_argument("--task-ids", nargs="+", help="run only these cases, e.g. tal_001 tal_002")
    parser.add_argument("-n", type=int, default=1, help="samples per case")
    parser.add_argument("--rendering", choices=RENDERINGS, default="json",
                        help="how a case's state is shown to the model")
    parser.add_argument("--check", action="store_true",
                        help="score each case's own answer key and exit; no model call")
    args = parser.parse_args()

    cases = load_test_cases(args.test_file, args.task_ids)
    if args.check:
        sys.exit(1 if check_cases(cases) else 0)
    if not args.model:
        parser.error("--model is required unless --check is given")

    backend = OpenRouterBackend() if args.backend == "openrouter" else OllamaBackend()
    passed, runs, cost = 0, 0, 0.0

    for case in cases:
        problems = validate_case(case)
        if problems:
            raise ValueError(f"{case.get('case_id')} is not a valid test case: {problems}")
        task = to_task(case, args.rendering)

        for run_index in range(args.n):
            if is_search(case):
                result = generate_with_tools(backend, args.model, task, [section_tally.SEARCH_TOOL],
                                             section_tally.run_search_tool, run_index=run_index)
            else:
                result = generate(backend, args.model, task, run_index=run_index)
            if result.ok:
                verdict = evaluate_case(case, result.raw_output)
            else:
                verdict = {"outcome": "generation_failed", "detail": result.error}

            passed += verdict["outcome"] == "semantic_ok"
            runs += 1
            cost += result.usd_cost
            label = task.task_id if args.n == 1 else f"{task.task_id} run {run_index}"
            searches = f" [{len(result.steps)} searches]" if is_search(case) else ""
            print(f"{label}: {verdict['outcome']:20s} | {verdict.get('detail', '')}{searches}")
            log_readable(result, verdict, args.output)

    print(f"\n{passed}/{runs} semantic_ok on {args.model}. Cost ${cost:.4f}. "
          f"Full results in {args.output}")


if __name__ == "__main__":
    main()
