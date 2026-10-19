"""
run_semantic_eval.py
---------------------
The evaluation pipeline: reads a file of test cases, builds each case's prompt
from the input format (README.md, "The input format"), sends it to a model,
and scores the answer through all four tiers:

  Tiers 1-2  syntactic + schema   didcomm_schema.validate_message()  } via
  Tier 3     consistency          registration.check_consistency()   } registration.evaluate()
  Tier 4     semantic             check_rules(), below

The model NEVER sees `ground_truth`. It is loaded separately and used only
here, after generation, so it can never leak into the prompt.

A case file is an indented JSON array of cases. Each names its degree program and
fabricates its transcript (fabricate.py), and carries a rules answer key:
    {"case_id": "marketing-bs-1", "input": {"program": "marketing-bs", ...},
     "ground_truth": {"rules": {...}, "examples": {...}, "implicit_relationship": "..."}}

THE SESSION (the default): the whole Section Tally is the session's first turn, cached,
and every case is then its own request after it (TallySession, below). Each case sees
the tally and its own request, never another case or its answer. --named-courses runs
the earlier way instead: no session, and each case's prompt lists only the sections of
the courses it names.

HOW TO RUN:
    python run_semantic_eval.py --model anthropic/claude-opus-5.5
    python run_semantic_eval.py --model anthropic/claude-opus-5.5 --task-ids marketing-bs-1 -n 5
    python run_semantic_eval.py --model anthropic/claude-opus-5.5 --named-courses
    python run_semantic_eval.py --check      # scores every answer key; no model, no cost

Requires OPENROUTER_API_KEY to be set.
Appends full results (including the verdict) to semantic_eval_results.json,
an indented JSON array with one object per generation.
"""

import argparse
import hashlib
import subprocess
import csv
import io
from datetime import datetime, timezone
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from harness import TaskSpec, OpenRouterBackend, generate, parse_didcomm, _context_label as shared_context_info
import registration as reg
import section_tally
import fabricate
from didcomm_schema import OUTPUT_CONTRACT, CRN_CONTRACT, MESSAGE_TYPE, SECTION_FIELDS

RENDERINGS = ("json", "csv", "nl")


# ── Tier 4: semantic check ──
# Tier 3 only checks invariants: does each section exist, is there a seat, no time
# conflict, prerequisites met. It never checks whether the model chose the RIGHT sections
# for what the intent asked. A cart can pass every consistency check and still be WRONG;
# check_rules() (below) catches that against the case's answer key.

def _normalize(value):
    """Case- and whitespace-insensitive."""
    return str(value).strip().lower()


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


# ── The session's opening: the Section Tally, in one of four versions (--tally) ──
#   1 raw           the export as downloaded: every row, every column
#   2 trimmed       converted rows, columns no check uses left out (TALLY_FIELDS)
#   3 raw-open      every column, but full and cancelled sections left out
#   4 trimmed-open  converted rows, trimmed columns, full and cancelled sections left out
# The checker always holds all 5,129 sections, so the answer keys are the same in every
# version (a correct cart never needs a full or cancelled section: waitlists are not asked for).
TALLY_VARIANTS = {"raw": (True, False), "trimmed": (False, False), "raw-open": (True, True),
                  "trimmed-open": (False, True)}

# Told to the model in a session, after the output contract.
WHOLE_CONTRACT = (
    " The sections offered this term are listed in the Section Tally at the start of this "
    "conversation; only sections in it exist. Copy a section's fields from its row there: crn, "
    "course_id, section, seats_total, seats_taken, campus and modality as written, and "
    "meetings as a list with each meeting's days (full day names), start_time and end_time."
)
RAW_CONTRACT = (
    " The sections offered this term are the rows of the Section Tally export at the start of "
    "this conversation; only sections in it exist. Build each section entry from its row: crn = "
    "CRN; course_id = Subj followed by Course# (CS + 04114 = CS04114); section = Sect; seats_total "
    "= Max; seats_taken = Enrolled (or Max, if Enrolled is higher); campus = Campus; modality = "
    "\"online\" when Session is Online, \"hybrid\" when it is Hybrid, otherwise \"in-person\"; "
    "credit_hours from Hours (3.00 = 3; a range such as 1.00 to 3.00 means variable credit); "
    "meetings from Meeting Time: each group such as 'MW 09:30 AM - 10:45 AM Business Hall 101 "
    "(Class)' is one meeting with its days as full day names (M Monday, T Tuesday, W Wednesday, "
    "R Thursday, F Friday, S Saturday, U Sunday), start_time and end_time; N/A means no meetings. "
    "A section is full when Enrolled >= Max, and cannot be registered for when Cancelled is true."
)
OPEN_NOTE = " Full and cancelled sections are not listed."

# The columns of the trimmed tally: what a model needs to choose a section and to copy it into
# the message. Fields no check uses (instructor, part_of_term, room capacity, session, reserved
# seats) are left out; with full and cancelled sections gone, waitlist and cancelled go too.
TALLY_FIELDS = ("crn", "course_id", "section", "title", "title_note", "credit_hours",
                "seats_total", "seats_taken", "waitlist", "campus", "modality", "meetings",
                "cancelled")
OPEN_FIELDS = tuple(f for f in TALLY_FIELDS if f not in ("waitlist", "cancelled"))


def _is_open(s: dict) -> bool:
    """Neither cancelled nor full."""
    return not s.get("cancelled") and s["seats_taken"] < s["seats_total"]


def render_tally(schedule: dict, open_only: bool = False) -> str:
    """The trimmed Section Tally as the session's shared opening: a short key, then one CSV row
    per section. Built once per session and sent byte-identical with every case."""
    sections = [s for s in schedule["sections"] if _is_open(s)] if open_only else schedule["sections"]
    fields = OPEN_FIELDS if open_only else TALLY_FIELDS
    rows = [{field: s.get(field, "") for field in fields} for s in sections]
    which = "Every open section" if open_only else "Every section"
    key = (
        f"SECTION TALLY: {schedule['term']} (term_code {schedule['term_code']}). {which} "
        f"offered this term, {len(rows):,} rows, one per section, from the registrar's Section "
        "Tally. Columns: crn (the section's id); course_id; section; title; title_note (a "
        "restriction, topic or former name); credit_hours (a range such as 1-12 means "
        "variable credit); seats_total and seats_taken"
        + (" " if open_only else " (full when seats_taken >= seats_total); waitlist (its capacity "
                                 "and how many are on it); ")
        + "campus; modality (in-person, online or hybrid); meetings (each as 'Days start-end, "
        "location (type)', several separated by '; '; 'none scheduled' means no set times)"
        + (". Full and cancelled sections are not listed.\n\n" if open_only else
           "; cancelled (True means it cannot be registered for).\n\n"))
    return key + _table("sections", rows)


def render_raw(open_only: bool = False) -> str:
    """The Section Tally export itself as the session's opening: the CSV as downloaded, or
    without its full and cancelled rows."""
    text = section_tally.TALLY_CSV.read_text(encoding="utf-8")
    if open_only:
        rows = [r for r in section_tally.read_tally()
                if r["Cancelled"] != "true" and int(r["Enrolled"]) < int(r["Max"])]
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0]), quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        text = out.getvalue()
    which = "its open sections only (full and cancelled rows removed)" if open_only else "every row"
    return ("SECTION TALLY EXPORT: the registrar's Section Tally for this term as a CSV file, "
            f"{which}. One row per section.\n\n" + text)


class TallySession:
    """The pipeline's session (the default run):
        start  read the Section Tally once and build the opening turn in the chosen version
               (TALLY_VARIANTS)
        run    each case is its own call: the opening turn (cached), the model's fixed
               acknowledgement (harness.SESSION_ACK), then that case's request
        close  report what the session cost and how much of the input came from the cache (the
               results file's session summary keeps the same totals; run_cases() writes it)
    The API keeps no state, so every call resends the opening and the provider's cache keeps
    it cheap. Calls share nothing but the opening, so a case never sees another case or its
    answer."""

    def __init__(self, ttl: str = "5m", variant: str = "trimmed"):
        """ttl: cache lifetime; variant: a TALLY_VARIANTS key."""
        self.ttl, self.variant, self.text = ttl, variant, ""
        self.raw, self.open_only = TALLY_VARIANTS[variant]
        self.contract = (RAW_CONTRACT if self.raw else WHOLE_CONTRACT) + (OPEN_NOTE if self.open_only else "")
        self.id = f"{variant}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        self.calls = self.prompt = self.cached = self.written = 0
        self.cost = 0.0

    def __enter__(self):
        """Reads the Section Tally once and builds the opening turn."""
        schedule = section_tally.full_schedule()
        self.text = render_raw(self.open_only) if self.raw else render_tally(schedule, self.open_only)
        rows = sum(1 for s in schedule["sections"] if _is_open(s)) if self.open_only else len(schedule["sections"])
        print(f"session {self.id} started: Section Tally ({self.variant}), {rows:,} sections, "
              f"{len(self.text):,} characters, cache ttl {self.ttl}")
        return self

    def record(self, result) -> None:
        """Adds one call's tokens and cost to the session's totals."""
        self.calls += 1
        self.prompt += result.prompt_tokens
        self.cached += result.cached_tokens
        self.written += result.cache_write_tokens
        self.cost += result.usd_cost

    def __exit__(self, *exc):
        """Prints the session's totals."""
        share = f"{100 * self.cached / self.prompt:.0f}%" if self.prompt else "-"
        average = self.cost / self.calls if self.calls else 0.0
        print(f"session closed: {self.calls} model calls, {self.prompt:,} input tokens "
              f"({self.written:,} written to the cache, {self.cached:,} read from it: {share}), "
              f"${self.cost:.4f} in all, ${average:.4f} per prompt")
        return False


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


def load_test_cases(path="input_format_cases.json", task_ids=None, whole=False) -> list:
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


def _bounds_text(bounds: dict, unit: str) -> str:
    """{"min": 1, "max": 1} -> 'exactly 1 section'; an open end reads 'at least' / 'at most'."""
    low, high = bounds.get("min"), bounds.get("max")
    plural = lambda n: f"{n:g} {unit}{'' if n == 1 else 's'}"
    if low is not None and low == high:
        return f"exactly {plural(low)}"
    if low is not None and high is not None:
        return f"{low:g} to {plural(high)}"
    return f"at least {plural(low)}" if low is not None else f"at most {plural(high)}"


def _rule_meeting(crn: str, m: dict) -> str:
    """'43945 Mon/Wed 9:30 AM-10:45 AM', or '43945 no set times'."""
    days = "/".join(d[:3] for d in m["days"])
    return f"{crn} {days} {m['start_time']}-{m['end_time']}" if "start_time" in m else f"{crn} no set times"


# ── Why a Tier 4 check failed, in words ──

def _section_name(s: dict) -> str:
    """'40961 (PHIL09120 Ethics)': a section's crn, course and title."""
    title = (s.get("title") or "").strip().title()
    return f"{s['crn']} ({s['course_id']}{' ' + title if title else ''})"


def _req_name(data: dict, req_id: str) -> str:
    """'Logic Requirement (PHIL-LOGIC)': a requirement's title, else its category."""
    r = next((r for r in data["program_requirements"] if r["req_id"] == req_id), None)
    label = (r.get("title") or r["category"].split(": ", 1)[-1]) if r else req_id
    return f"{label} ({req_id})"


def _names(items) -> str:
    """'A', 'A and B', 'A, B and C'."""
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else "none"


def _not_counting_why(data: dict, s: dict, wanted: set, status: dict) -> str:
    """Why a section's course does not move any wanted requirement forward."""
    canon = reg._canonical(data)
    course = canon(s["course_id"])
    toward = reg.counts_toward(data, s["course_id"], s.get("attributes", []))
    taken = [c for c in data["transcript"]["completed_courses"] + data["transcript"]["transfer_courses"]
             if canon(c["course_id"]) == course]
    parts = [f"{_section_name(s)} counts toward "
             + (_names(_req_name(data, r) for r in toward) if toward else "no unfinished requirement")
             + f", not {_names(_req_name(data, r) for r in sorted(wanted))}."]
    reqs = [r for r in data["program_requirements"] if r["req_id"] in wanted]
    attrs = reg.catalog_attributes(data, s["course_id"]) | set(s.get("attributes", []))
    done = [r["req_id"] for r in data["program_requirements"]
            if r["req_id"] not in wanted and r["req_id"] not in toward and status.get(r["req_id"], {}).get("satisfied")
            and r["rule"] != "credits_any" and reg._matches(r, course, attrs, canon)]
    if done:
        parts.append(f"It would have counted toward {_names(_req_name(data, r) for r in done)}, but "
                     f"{'that requirement is' if len(done) == 1 else 'those requirements are'} already complete "
                     f"on the transcript, so it no longer helps.")
    if not any(reg._matches(r, course, attrs, canon) for r in reqs):
        parts.append(f"{s['course_id']} is not one of the courses {_names(sorted(wanted))} "
                     f"{'accepts' if len(wanted) == 1 else 'accept'}.")
    elif taken:
        grades = ", ".join(c["grade"] for c in taken)
        parts.append(f"It would fit, but the transcript already has {s['course_id']} ({grades}) and it is not repeatable, "
                     f"so taking it again earns nothing more.")
    met = [r for r in sorted(wanted) if status.get(r, {}).get("satisfied")]
    if met:
        parts.append(f"{_names(_req_name(data, r) for r in met)} is already complete on the transcript.")
    return " ".join(parts)


def _would_count(data: dict, wanted: set, offered: set, limit: int = 5) -> list:
    """Up to `limit` offered courses the wanted requirements name that would count toward them."""
    canon = reg._canonical(data)
    named = [c for r in data["program_requirements"] if r["req_id"] in wanted for c in r["course_options"]]
    out = []
    for c in dict.fromkeys(named):
        if canon(c) in offered and wanted & set(reg.counts_toward(data, c)):
            out.append(c)
        if len(out) >= limit:
            break
    return out


def _busy_overlap(m: dict, b: dict) -> int:
    """Minutes a meeting overlaps a commitment on one shared day (0 when they do not overlap)."""
    if "start_time" not in m or not set(m["days"]) & set(b["days"]):
        return 0
    start = max(reg.minutes(m["start_time"]), reg.minutes(b["start_time"]))
    end = min(reg.minutes(m["end_time"]), reg.minutes(b["end_time"]))
    return max(0, end - start)


def intended(case: dict) -> str:
    """The case's intended reasoning: the start of its implicit_relationship, before the list of
    correct sections the generator appends."""
    text = case["ground_truth"].get("implicit_relationship", "")
    return re.split(r" \d+ correct (?:section|cart)", text)[0].strip()


def check_rules(body: dict, case: dict) -> dict:
    """Tier 4 for a rules answer key. Assumes Tiers 1-3 passed, so every crn exists. The
    verdict's tier4_checks lists every check made, in order: the rule, what it requires in
    words, what the cart has, and whether it passed; a failed check also says `why`, in words:
    what the model chose, why that breaks the rule, and what would have satisfied it."""
    data, rules = case["input"], case["ground_truth"]["rules"]
    by_crn = {s["crn"]: s for s in data["schedule"]["sections"]}
    chosen = [(item, by_crn[item["crn"]]) for item in body["sections"]]
    meetings = [(item["crn"], m) for item, s in chosen for m in s["meetings"]]
    times = "; ".join(_rule_meeting(crn, m) for crn, m in meetings) or "no meetings"
    violated, checks = {}, []

    def check(rule, requires, found, broken, why=None):
        """Records one check; `broken` is the failure detail, or None when it passed; `why`
        explains a failure in words."""
        entry = {"rule": rule, "requires": requires, "found": found, "passed": broken is None}
        if broken is not None:
            entry["why"] = why or broken
            violated[rule] = broken
        checks.append(entry)

    sid, term = data["student"]["student_id"], data["schedule"]["term_code"]
    check("student_id", f"the message is for student {sid}", f"student_id {body.get('student_id')!r}",
          None if _normalize(body.get("student_id")) == _normalize(sid) else f"expected {sid}",
          f"The message registers {body.get('student_id')!r}, but the request is from student {sid}, so it would "
          f"enroll the wrong person.")
    check("term_code", f"the message is for term {term}", f"term_code {body.get('term_code')!r}",
          None if body.get("term_code") == term else f"expected {term}",
          f"The message is for term {body.get('term_code')!r}, but registration is for term {term}.")
    names = _names(_section_name(s) for _, s in chosen)
    if "count" in rules:
        low, high = rules["count"].get("min", 0), rules["count"].get("max", 10 ** 6)
        many = len(chosen) > high
        check("count", f"{_bounds_text(rules['count'], 'section')} in the cart",
              f"{len(chosen)}: {', '.join(item['crn'] for item, _ in chosen) or 'none'}",
              None if low <= len(chosen) <= high else f"{len(chosen)} sections, wanted {rules['count']}",
              f"The request calls for {_bounds_text(rules['count'], 'section')}, but the cart has {len(chosen)} "
              f"({names}). " + ("Registering for more than was asked adds classes the student did not request."
                                if many else "The cart leaves out a class the request asked for."))
    if "credits" in rules:
        # an entry's own credit_hours (Tier 3 has checked it is allowed), else the section's
        # credits (the low end of a variable-credit range)
        each = [(item["crn"], item.get("credit_hours", s["credit_hours"]["min"] if isinstance(s["credit_hours"], dict)
                                       else s["credit_hours"])) for item, s in chosen]
        total = sum(c for _, c in each)
        low, high = rules["credits"].get("min", 0), rules["credits"].get("max", 10 ** 6)
        over = total - high if total > high else 0
        check("credits", f"{_bounds_text(rules['credits'], 'credit')} in all",
              f"{total:g} credits ({' + '.join(f'{crn}: {c:g}' for crn, c in each)})",
              None if low <= total <= high else f"{total:g} credits, wanted {rules['credits']}",
              f"The cart adds up to {total:g} credits ({' + '.join(f'{crn} {c:g}' for crn, c in each)}), "
              + (f"{over:g} over the student's limit of {high:g}; a section that only fit if both did should "
                 f"have been left out." if over else f"short of the {low:g} the request needs."))
    if "courses_count_toward" in rules:
        wanted = set(rules["courses_count_toward"])
        helps = [(s, reg.counts_toward(data, s["course_id"], s.get("attributes", []))) for _, s in chosen]
        no = [s for s, toward in helps if not wanted & set(toward)]
        why = None
        if no:
            status = reg.audit(data)["requirements"]
            offered = {s["course_id"] for s in data["schedule"]["sections"]}
            good = _would_count(data, wanted, offered)
            why = (" ".join(_not_counting_why(data, s, wanted, status) for s in no)
                   + (f" Offered courses that would count: {', '.join(good)}." if good else ""))
        check("courses_count_toward",
              f"every course moves one of these unfinished requirements forward: {', '.join(sorted(wanted))}",
              "; ".join(f"{s['crn']} ({s['course_id']}) counts toward {', '.join(toward) or 'no unfinished requirement'}"
                        for s, toward in helps),
              f"{[s['crn'] for s in no]} do not move {sorted(wanted)} forward" if no else None, why)
    if "no_days" in rules:
        week = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        in_week = lambda days: [d for d in week if d in days]
        bad = sorted({crn for crn, m in meetings if set(m["days"]) & set(rules["no_days"])})
        hits = [f"{_rule_meeting(crn, m)} meets on {_names(in_week(set(m['days']) & set(rules['no_days'])))}"
                for crn, m in meetings if set(m["days"]) & set(rules["no_days"])]
        allowed = [d for d in week if d not in rules["no_days"]]
        rule_text = (f"the student can only come {_names(d + 's' for d in allowed)}" if len(allowed) <= 3
                     else f"the request rules out {_names(d + 's' for d in in_week(rules['no_days']))}")
        check("no_days", f"no meeting on {', '.join(rules['no_days'])}", times,
              f"{bad} meet on {rules['no_days']}" if bad else None,
              f"{'; '.join(hits)}, but {rule_text}. A section meeting only on allowed days was needed.")
    if "starts_before" in rules:
        limit = reg.minutes(rules["starts_before"])
        late = [(crn, m) for crn, m in meetings if "start_time" in m and reg.minutes(m["start_time"]) >= limit]
        check("starts_before", f"every meeting starts before {rules['starts_before']}", times,
              f"{sorted({c for c, _ in late})} start at or after {rules['starts_before']}" if late else None,
              "; ".join(f"{_rule_meeting(crn, m)} starts at {m['start_time']}" for crn, m in late)
              + f", but the request wants nothing starting at {rules['starts_before']} or later.")
    if "ends_by" in rules:
        limit = reg.minutes(rules["ends_by"])
        late = [(crn, m) for crn, m in meetings if "end_time" in m and reg.minutes(m["end_time"]) > limit]
        check("ends_by", f"every meeting ends by {rules['ends_by']}", times,
              f"{sorted({c for c, _ in late})} end after {rules['ends_by']}" if late else None,
              "; ".join(f"{_rule_meeting(crn, m)} ends at {m['end_time']}, "
                        f"{reg.minutes(m['end_time']) - limit} minutes past {rules['ends_by']}" for crn, m in late)
              + ". The request rules out anything ending later than that.")
    if "modality_in" in rules:
        wrong = [s for _, s in chosen if s["modality"] not in rules["modality_in"]]
        check("modality_in", f"every section is {' or '.join(rules['modality_in'])}",
              "; ".join(f"{s['crn']} {s['modality']}" for _, s in chosen),
              f"{[s['crn'] for s in wrong]} are not {rules['modality_in']}" if wrong else None,
              "; ".join(f"{_section_name(s)} is {s['modality']}" for s in wrong)
              + f", but the request asks for {' or '.join(rules['modality_in'])} only. "
              f"A different section of the same course with that modality was needed.")
    bad = [item["crn"] for item, _ in chosen if item.get("waitlist")]
    check("waitlist", "no section is waitlisted (no request asks for a waitlist)",
          f"waitlisted: {', '.join(bad)}" if bad else "nothing waitlisted",
          f"{bad} waitlisted, but the student did not ask to be waitlisted" if bad else None,
          f"The message joins the waitlist of {_names(bad)}. The request never asked for a waitlist, so a "
          f"section with an open seat (or the request's fallback) was wanted instead.")
    courses = {s["course_id"] for _, s in chosen}
    if "must_include_courses" in rules:
        missing = sorted(set(rules["must_include_courses"]) - courses)
        offered = {}
        for s in data["schedule"]["sections"]:
            offered.setdefault(s["course_id"], []).append(s)
        check("must_include_courses", f"the cart contains {', '.join(rules['must_include_courses'])}",
              f"courses: {', '.join(sorted(courses)) or 'none'}", f"missing {missing}" if missing else None,
              f"The request names {_names(missing)}, but the cart has no section of "
              f"{'it' if len(missing) == 1 else 'them'} (it has {_names(sorted(courses))}). "
              + " ".join(f"{c} has {sum(1 for s in offered.get(c, []) if s['seats_taken'] < s['seats_total'] and not s.get('cancelled'))} "
                         f"open section(s) this term." for c in missing))
    if "courses_in" in rules:
        extra = sorted(courses - set(rules["courses_in"]))
        check("courses_in", f"every course is one of {', '.join(rules['courses_in'])}",
              f"courses: {', '.join(sorted(courses)) or 'none'}",
              f"{extra} are not among {rules['courses_in']}" if extra else None,
              f"The cart takes {_names(_section_name(s) for _, s in chosen if s['course_id'] in extra)}, but for "
              f"this student only {' or '.join(rules['courses_in'])} is right (see why_wrong for the reason). "
              f"The model chose a course the request mentions or allows in general, without applying the "
              f"condition that rules it out here.")
    if "busy" in rules:
        # an overlap is a shared day with intersecting time ranges; touching end to start is not one
        hits = [(crn, m, b, _busy_overlap(m, b)) for crn, m in meetings for b in rules["busy"] if _busy_overlap(m, b)]
        blocks = "; ".join(f"{'/'.join(d[:3] for d in b['days'])} {b['start_time']}-{b['end_time']}"
                           for b in rules["busy"])
        check("busy", f"no meeting overlaps the student's fixed commitments ({blocks})", times,
              f"{sorted({h[0] for h in hits})} overlap the student's fixed commitments" if hits else None,
              "; ".join(f"{_rule_meeting(crn, m)} overlaps the commitment "
                        f"{'/'.join(d[:3] for d in b['days'])} {b['start_time']}-{b['end_time']} by {mins} minutes on "
                        f"{_names(sorted(set(m['days']) & set(b['days'])))}" for crn, m, b, mins in hits)
              + ". The times do not have to match exactly: any shared minute is a clash.")

    # deviation_score: the share of the checks this cart broke (0 = every rule held). The checks are
    # the key's rules plus student_id, term_code and waitlist.
    checked = len(rules) + 3
    if violated:
        failed = [c for c in checks if not c["passed"]]
        why_wrong = (f"{len(failed)} of {len(checks)} Tier 4 checks failed ({_names(c['rule'] for c in failed)}). "
                     f"What the request needed: {intended(case)}")
        return {"outcome": "semantic_error", "detail": f"rules broken: {violated}",
                "deviation_score": len(violated) / checked, "violated": sorted(violated),
                "tier4_checks": checks, "why_wrong": why_wrong}
    return {"outcome": "semantic_ok", "detail": "satisfies every rule", "deviation_score": 0.0,
            "tier4_checks": checks}


def evaluate_case(case: dict, raw_output: str, conn=None) -> dict:
    """Tiers 1-4 for a test case: Tiers 1-3 (registration.evaluate()), then, only when they
    pass, the answer key's rules. `conn` may be the case's database from an earlier call:
    scoring only reads it."""
    conn = conn or reg.seed_db(case["input"])
    base = reg.evaluate(conn, raw_output)
    if base["outcome"] != "consistent__semantic_TBD":
        verdict = dict(base)         # failed at Tier 1-3: keep that verdict as it is
    else:
        body, _ = parse_didcomm(raw_output)
        verdict = {**check_rules(body, case), "tier3_detail": base["detail"],
                   **{k: base[k] for k in ("tier1_checks", "tier2_checks", "tier3_checks")}}
    tag = error_type(verdict, case)
    if tag:
        verdict["error_type"] = tag
    return _with_tiers(verdict)


# What a semantic error in a pair family means: the family names the reasoning the case tests.
FAMILY_ERRORS = {"full_fallback": "fallback_not_applied", "in_progress": "in_progress_course_ignored",
                 "transfer": "transfer_credit_ignored", "busy_block": "time_overlap_missed",
                 "nested_groups": "and_or_requirement_misread", "credit_limit": "credit_arithmetic",
                 "modality": "modality_ignored", "days": "meeting_days_ignored",
                 "grade_threshold": "minimum_grade_misread", "withdrawal": "withdrawal_counted_as_credit",
                 "retake": "minimum_grade_misread", "passed": "already_passed_course_retaken",
                 "fallback": "fallback_not_applied"}
# Otherwise a semantic error is named by its first failed rule, a consistency error by its first
# failed Tier 3 check, and a format error by its first failed Tier 1-2 check.
RULE_ERRORS = {"student_id": "wrong_student", "term_code": "wrong_term", "count": "wrong_number_of_sections",
               "credits": "credit_arithmetic", "courses_count_toward": "requirement_mismatch",
               "no_days": "meeting_days_ignored", "starts_before": "time_limit_ignored",
               "ends_by": "time_limit_ignored", "modality_in": "modality_ignored", "waitlist": "unrequested_waitlist",
               "must_include_courses": "named_course_missing", "courses_in": "wrong_course", "busy": "time_overlap_missed"}
CHECK_ERRORS = {"term": "wrong_term", "exists": "nonexistent_section", "copied_fields": "section_details_miscopied",
                "not_cancelled": "cancelled_section", "seat": "full_section", "waitlist": "invalid_waitlist",
                "credits": "invalid_credit_hours", "prerequisites": "prerequisite_missing",
                "one_section_per_course": "two_sections_of_one_course", "no_time_conflict": "time_conflict_in_cart",
                "json": "not_json", "envelope": "not_didcomm", "type": "wrong_message_type",
                "body": "malformed_body", "sections": "malformed_section_entry", "unique_crns": "duplicate_crn",
                "other": "schema_violation"}


def error_type(verdict: dict, case: dict) -> str | None:
    """A short tag for what went wrong, for counting failures by kind; None when correct. A
    semantic error in a case whose type tests one kind of reasoning is named for it (a failed
    transfer case is transfer_credit_ignored, unless only the student id or term was wrong)."""
    outcome = verdict["outcome"]
    if outcome == "semantic_ok":
        return None
    if outcome == "generation_failed":
        return "call_failed"
    if outcome == "semantic_error":
        failed = [c["rule"] for c in verdict.get("tier4_checks", []) if not c["passed"]]
        kind = (case.get("ground_truth") or {}).get("case_type")
        if kind in FAMILY_ERRORS and set(failed) - {"student_id", "term_code"}:
            return FAMILY_ERRORS[kind]
        return RULE_ERRORS.get(failed[0], failed[0]) if failed else "semantic_error"
    tier = {"syntactic_error": 1, "schema_error": 2, "consistency_error": 3}.get(outcome)
    failed = [c["check"] for c in verdict.get(f"tier{tier}_checks", []) if not c["passed"]] if tier else []
    return CHECK_ERRORS.get(failed[0], failed[0]) if failed else outcome


def call_error_type(verdict: dict, finish_reason: str | None, native_finish_reason: str | None) -> str | None:
    """What the call's stop reason says about a failure, when it explains it better than the
    verdict: an answer cut off at max_tokens, or a refusal."""
    if verdict["outcome"] == "semantic_ok":
        return None
    if native_finish_reason == "refusal" or finish_reason == "content_filter":
        return "refused"
    if finish_reason == "length" and verdict["outcome"] in ("syntactic_error", "schema_error"):
        return "truncated_at_max_tokens"
    return None


# The tier each failing outcome stops at; semantic_ok passes all four.
FAILED_AT = {"syntactic_error": 1, "schema_error": 2, "consistency_error": 3, "semantic_error": 4}
TIER_NAMES = {1: "syntactic (JSON, DIDComm envelope)", 2: "schema (our register message)",
              3: "consistency (invariants against the schedule and transcript)", 4: "semantic (the request's rules)"}


# A verdict's keys in the order grading happens: the result, the per-tier overview, then each
# tier's own fields from Tier 1 to Tier 4, and Tier 4's score last.
VERDICT_ORDER = ("outcome", "detail", "error_type", "why_wrong", "tiers", "tier1_checks", "tier2_checks", "tier3_detail", "tier3_checks",
                 "tier4_summary", "tier4_checks", "deviation_score", "violated")


def _with_tiers(verdict: dict) -> dict:
    """The verdict with a `tiers` overview (what each tier checks, and passed / failed / not
    reached with its check count), its keys in VERDICT_ORDER (any other key after them)."""
    stop = FAILED_AT.get(verdict["outcome"], 5)
    tiers = {}
    for n, name in TIER_NAMES.items():
        checks = verdict.get(f"tier{n}_checks", [])
        state = "failed" if n == stop else "passed" if n < stop else "not reached"
        counts = f" ({sum(c['passed'] for c in checks)}/{len(checks)} checks passed)" if checks else ""
        tiers[f"tier{n}"] = f"{name}: {state}{counts}"
    full = {**verdict, "tiers": tiers, **{f"tier{n}_checks": verdict.get(f"tier{n}_checks", []) for n in TIER_NAMES}}
    ordered = {k: full[k] for k in VERDICT_ORDER if k in full}
    return {**ordered, **{k: v for k, v in full.items() if k not in ordered}}

def self_check(case: dict) -> list:
    """Scores a case's own answer key, with no model. Returns problems; empty means it behaves."""
    problems = validate_case(case)
    if problems:
        return problems
    truth = case["ground_truth"]
    conn = reg.seed_db(case["input"])                  # one database for every example
    for cart in truth.get("examples", {}).get("valid", []):
        verdict = evaluate_case(case, expected_message(case, cart), conn)
        if verdict["outcome"] != "semantic_ok":
            problems.append(f"valid example {cart} scores {verdict['outcome']}: {verdict['detail']}")
    for cart in truth.get("examples", {}).get("invalid", []):
        if evaluate_case(case, expected_message(case, cart), conn)["outcome"] == "semantic_ok":
            problems.append(f"invalid example {cart} scores semantic_ok")
    return problems


# ── Counterfactual pairs ──
# Two cases that are the same but for one detail (the prompt or the transcript), chosen so the
# detail flips the answer. ground_truth.pair = {"id", "variant": "a" | "b", "changed"}. A pair
# passes a run only when both variants are semantic_ok in that run: getting one right by luck
# (or by ignoring the detail) does not count.
PAIR_VARIANTS = ("a", "b")


def pairs_of(cases: list) -> dict:
    """pair id -> {variant: case_id}, for every case with ground_truth.pair."""
    pairs = {}
    for case in cases:
        pair = case.get("ground_truth", {}).get("pair")
        if pair:
            pairs.setdefault(pair["id"], {})[pair["variant"]] = case["case_id"]
    return pairs


def _rows_changed(a: dict, b: dict) -> int:
    """How many transcript rows differ between two transcripts (a row edited counts once). A
    row is its list, course and grade: the term a fabricator files it under does not count."""
    def rows(t):
        return {(name, r["course_id"], r.get("grade")) for name in ("completed_courses", "transfer_courses",
                                                                    "courses_in_progress") for r in t[name]}
    ra, rb = rows(a), rows(b)
    return max(len(ra - rb), len(rb - ra))


def check_pairs(cases: list) -> list:
    """Problems with the file's pairs: each has both variants, they differ in exactly one place
    (the prompt, or one transcript row), and the detail matters, i.e. each variant rejects at
    least one cart its twin accepts."""
    by_id, problems = {case["case_id"]: case for case in cases}, []
    for pair_id, variants in pairs_of(cases).items():
        if set(variants) != set(PAIR_VARIANTS):
            problems.append(f"pair {pair_id} has variants {sorted(variants)}, wants {list(PAIR_VARIANTS)}")
            continue
        a, b = by_id[variants["a"]]["input"], by_id[variants["b"]]["input"]
        changed = (a["prompt"] != b["prompt"]) + _rows_changed(a["transcript"], b["transcript"])
        if changed != 1:
            problems.append(f"pair {pair_id}: its cases differ in {changed} places (prompt and transcript rows), "
                            f"wants exactly 1")
        for here, twin in (("a", "b"), ("b", "a")):
            case, other = by_id[variants[here]], by_id[variants[twin]]
            conn = reg.seed_db(case["input"])
            carts = other["ground_truth"].get("examples", {}).get("valid", [])
            if all(evaluate_case(case, expected_message(case, cart), conn)["outcome"] == "semantic_ok"
                   for cart in carts):
                problems.append(f"pair {pair_id}: {case['case_id']} accepts every valid cart of "
                                f"{other['case_id']}, so the changed detail does not change the answer")
    return problems


def pair_stats(pairs: dict, outcomes: dict, weak: set = frozenset()) -> dict:
    """Paired accuracy as data, for a session summary: passed and scored pairs with a 95%
    interval, the same for strong pairs only (those no single answer can pass, i.e. not in
    `weak`), incomplete pairs, and passed/scored by family; {} when no pair was run."""
    passed, total, incomplete, families, strong = 0, 0, 0, {}, [0, 0]
    runs = sorted({run for _, run in outcomes})
    for pair_id, variants in sorted(pairs.items()):
        for run in runs:
            got = [outcomes.get((variants.get(v), run)) for v in PAIR_VARIANTS]
            if got == [None, None]:
                continue
            if None in got or "generation_failed" in got:
                incomplete += 1
                continue
            ok = all(outcome == "semantic_ok" for outcome in got)
            family = families.setdefault(pair_id.split("/")[0], [0, 0])
            family[0] += ok
            family[1] += 1
            passed += ok
            total += 1
            if pair_id not in weak:
                strong[0] += ok
                strong[1] += 1
    if not total and not incomplete:
        return {}
    return {"passed": passed, "scored": total, "rate": round(passed / total, 3) if total else None,
            "ci95": wilson(passed, total), "strong_pairs_only": _rate(*strong),
            "weak_pairs_scored": total - strong[1], "incomplete": incomplete,
            "by_family": {name: f"{ok}/{n}" for name, (ok, n) in sorted(families.items())}}


def pair_summary(pairs: dict, outcomes: dict) -> list:
    """Lines reporting paired accuracy. outcomes: (case_id, run_index) -> outcome. A pair is
    scored once per run index both its variants have; the family is the pair id's first part.
    A pair whose call failed in that run (generation_failed) says nothing about the model, so it
    is counted as incomplete, not as a miss."""
    passed, total, incomplete, families = 0, 0, 0, {}
    runs = sorted({run for _, run in outcomes})
    for pair_id, variants in sorted(pairs.items()):
        for run in runs:
            got = [outcomes.get((variants.get(v), run)) for v in PAIR_VARIANTS]
            if got == [None, None]:
                continue
            if None in got or "generation_failed" in got:
                incomplete += 1
                continue
            ok = all(outcome == "semantic_ok" for outcome in got)
            family = families.setdefault(pair_id.split("/")[0], [0, 0])
            family[0] += ok
            family[1] += 1
            passed += ok
            total += 1
    note = f"; {incomplete} incomplete (a call failed or a variant was not run), not scored" if incomplete else ""
    if not total:
        return [f"Paired accuracy: no complete pairs{note}"] if incomplete else []
    lines = [f"Paired accuracy: {passed}/{total} pairs with both variants semantic_ok ({100 * passed / total:.0f}%){note}"]
    lines += [f"    {name:18s} {ok}/{n}" for name, (ok, n) in sorted(families.items())]
    return lines


def _answer(row: dict) -> list:
    """The sections a result row's message registered for, as 'crn (course)'; [] if none."""
    body = (row.get("parsed_output") or {}).get("body") if isinstance(row.get("parsed_output"), dict) else None
    sections = body.get("sections") if isinstance(body, dict) else None
    if not isinstance(sections, list):
        return []
    return [f"{s.get('crn')} ({s.get('course_id')})" if isinstance(s, dict) else str(s) for s in sections]


def _relation(here: dict, twin: dict | None, pair: dict, twin_id: str) -> dict:
    """pair_result for one row: how its result relates to its twin's in the same run. Only
    what the row and its twin do not already say: the twin's id, whether the two answers are
    the same, the pair's verdict, and an explanation (which names the twin's outcome)."""
    out = {"pair_id": pair["id"], "twin_id": twin_id}
    changed, where = pair.get("changed"), (pair.get("difference") or {}).get("where") or "one detail"
    mine = here["verdict"]["outcome"]
    if twin is None or "generation_failed" in (mine, twin["verdict"]["outcome"]):
        why = "its twin was not run in this session" if twin is None else "a call failed"
        return {**out, "pair_passed": None, "explanation": f"Incomplete: {why}, so the pair is not scored."}
    theirs = twin["verdict"]["outcome"]
    a, b = _answer(here), _answer(twin)
    same = bool(a) and sorted(a) == sorted(b)
    ok_here, ok_twin = mine == "semantic_ok", theirs == "semantic_ok"
    if ok_here and ok_twin and same:
        text = (f"Both cases are correct with the same answer, which is valid under both versions of the {where} "
                f"({changed}). Pair passed, but weakly: an answer that ignores the change would pass too, so "
                f"this pair does not show the model used the changed detail.")
    elif ok_here and ok_twin:
        text = (f"Both cases are correct and the answer changed with the {where} ({changed}): "
                f"the model used the changed detail. Pair passed.")
    elif ok_here or ok_twin:
        right, wrong = ("this case", "its twin") if ok_here else ("its twin", "this case")
        text = (f"Only {right} is correct; {wrong} is a {theirs if ok_here else mine}. Pair failed. "
                + (f"The model gave the same answer to both, so it ignored the change in the {where} and was "
                   f"right only where that answer happened to fit." if same else
                   f"The model changed its answer between the two, but not to a correct one in {wrong}."))
    else:
        text = (f"Neither case is correct (this: {mine}, twin: {theirs}). Pair failed"
                + (", with the same answer to both: the change in the " + where + " was ignored." if same else "."))
    return {**out, "same_answer_as_twin": same, "pair_passed": ok_here and ok_twin, "explanation": text}


def relate_pairs(case_file: str, results: list) -> int:
    """Sets pair_result on every row of a pair case (in place): related to its twin's row in the
    same session and run. Returns how many rows it set."""
    meta = {c["case_id"]: c["ground_truth"]["pair"] for c in json.loads(Path(case_file).read_text(encoding="utf-8"))
            if c.get("ground_truth", {}).get("pair")}
    members = {}
    for cid, p in meta.items():
        members.setdefault(p["id"], []).append(cid)
    key = lambda r, task: (r.get("session_id"), r["run_index"], task)
    index = {key(r, r["task_id"]): r for r in results}
    set_ = 0
    for row in results:
        pair = meta.get(row["task_id"])
        if not pair:
            continue
        twin_id = next(cid for cid in members[pair["id"]] if cid != row["task_id"])
        row["pair_result"] = _relation(row, index.get(key(row, twin_id)), pair, twin_id)
        set_ += 1
    return set_


def pair_report(case_file: str, results_file: str) -> None:
    """Paired accuracy for every session in a results file, from its rows' task ids and verdicts."""
    pairs = pairs_of(json.loads(Path(case_file).read_text(encoding="utf-8")))
    sessions, results = load_results(results_file)
    for s in sessions:
        outcomes = {(r["task_id"], r["run_index"]): r["verdict"]["outcome"]
                    for r in results if r.get("session_id") == s["session_id"]}
        lines = pair_summary(pairs, outcomes)
        print(f"{s['session_id']} ({s.get('model')}, tally {s.get('tally_variant') or 'none'}):\n  "
              + ("\n  ".join(lines) if lines else "no pairs in this session"))

# ── What the model sees: three renderings of the SAME state (spec open item 8) ──

def _meeting_text(m: dict, joiner: str = "/", to: str = "-") -> str:
    """'Monday/Wednesday 09:30 AM-10:45 AM, Robinson Hall 207 (Class)'."""
    when = f"{m['start_time']}{to}{m['end_time']}" if "start_time" in m else "time to be arranged"
    where = f", {m['location']}" if "location" in m else ""
    kind = f" ({m['type']})" if "type" in m else ""
    return f"{joiner.join(m['days'])} {when}{where}{kind}"


def _credits_text(credits) -> str:
    """'3', or '1-3' for a variable-credit range."""
    return f"{credits['min']}-{credits['max']}" if isinstance(credits, dict) else f"{credits:g}"


def _cell(key: str, value) -> str:
    """One CSV cell. Lists are joined with '|'; nested rows with '; '."""
    if key == "meetings":
        return "; ".join(_meeting_text(m) for m in value) or "none scheduled"
    if key == "course_groups":
        return " or ".join("+".join(group) for group in value)
    if key == "course_ranges":
        return "; ".join(_range_text(cr) for cr in value)
    if key == "credit_caps":
        return "; ".join(f"max {cap['max_credits']:g} from {'|'.join(cap['course_options'])}" for cap in value)
    if key == "credit_hours":
        return _credits_text(value)
    if isinstance(value, list):
        return "|".join(map(str, value))
    if isinstance(value, dict):                       # waitlist, reserved_seats
        return "; ".join(f"{k} {v}" for k, v in value.items())
    return str(value)


def _range_text(cr: dict) -> str:
    """{'subject': 'PHYS', 'min_number': 300} -> 'any PHYS course numbered 300 or higher'."""
    subject = cr["subject"] + (f" {cr['discipline']}" if "discipline" in cr else "")
    low, high = cr.get("min_number"), cr.get("max_number")
    span = (f" numbered {low}-{high}" if low is not None and high is not None else
            f" numbered {low} or higher" if low is not None else
            f" numbered {high} or lower" if high is not None else "")
    text = f"any {subject} course{span}"
    return text + (f" except {', '.join(cr['exclude'])}" if cr.get("exclude") else "")


def _guide_rows(guide: dict) -> list:
    """The guide's own fields as one row, then a row per section."""
    head = {k: v for k, v in guide.items() if k not in ("sections", "rowan_core", "rowan_experience")}
    if "rowan_core" in guide:
        head["rowan_core_literacies"] = guide["rowan_core"]["literacies"]
        if "min_credits_per_literacy" in guide["rowan_core"]:
            head["min_credits_per_literacy"] = guide["rowan_core"]["min_credits_per_literacy"]
    if "rowan_experience" in guide:
        head["rowan_experience_attributes"] = guide["rowan_experience"]["attributes"]
    return [head]


def _table(name: str, rows: list) -> str:
    """One named CSV table. The columns are every key any row has, in first-seen order."""
    columns = list(dict.fromkeys(key for row in rows for key in row))
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(key, row[key]) if key in row else "" for key in columns])
    return f"# {name}\n{out.getvalue()}"


def render_csv(state: dict) -> str:
    """The state as CSV tables (--rendering csv): one table per input field, the sections last."""
    transcript = state["transcript"]
    tables = [
        ("student", [state["student"]]),
        ("program_guide", _guide_rows(state["program_guide"]) if "program_guide" in state else []),
        ("program_guide_sections", state.get("program_guide", {}).get("sections", [])),
        ("completed_courses", transcript["completed_courses"]),
        ("transfer_courses", transcript["transfer_courses"]),
        ("courses_in_progress", transcript["courses_in_progress"]),
        ("program_requirements", state["program_requirements"]),
        ("course_attributes", [{"course_id": c, "attributes": a}
                               for c, a in state.get("course_attributes", {}).items()]),
        ("prerequisites", state.get("prerequisites", [])),
        ("course_equivalents", [{"same_course": group} for group in state.get("course_equivalents", [])]),
        ("graduation_rules", [state["graduation_rules"]] if "graduation_rules" in state else []),
        (f"sections offered in {state['schedule']['term']}", state["schedule"].get("sections", [])),
    ]
    return "\n".join(_table(name, rows) for name, rows in tables if rows)


def _taken_text(c: dict) -> str:
    """One transcript row as a phrase, for render_nl."""
    where = f" at {c['campus']}" if "campus" in c else ""
    title = f" ({c['title']})" if c.get("title") else ""
    tags = f", attributes {', '.join(c['attributes'])}" if c.get("attributes") else ""
    return f"{c['course_id']}{title}, {c['credit_hours']:g} credits{where}{tags}"


def _requirement_text(r: dict) -> str:
    """One requirement as a sentence, for render_nl: what it needs, then every modifier
    (repeatable, caps, exclusive, guide section, recommendations, waivers, notes)."""
    options = ", ".join(r["course_options"] + [_range_text(cr) for cr in r.get("course_ranges", [])])
    need = {
        "all_of": f"every one of: {options}",
        "any_of": f"at least one of: {options}",
        "credits_from": f"{r['credit_hours']:g} credits from: {options}",
        "credits_with_attribute": f"{r['credit_hours']:g} credits of courses with attribute {r.get('attribute')}",
        "one_of_groups": "every course of one of these groups: " +
                         "; or ".join(" + ".join(g) for g in r.get("course_groups", [])),
        "credits_any": f"{r['credit_hours']:g} credits of any courses not used by another exclusive requirement",
    }[r["rule"]]
    text = (f"- {r['req_id']} ({r.get('title', r['category'])}; {r['category']}; "
            f"{r['credit_hours']:g} credits): needs {need}, each with {r['minimum_grade']} or better.")
    if r.get("repeatable"):
        text += f" Credits count every time for: {', '.join(r['repeatable'])}."
    for cap in r.get("credit_caps", []):
        text += f" At most {cap['max_credits']:g} credits count from: {', '.join(cap['course_options'])}."
    if r.get("exclusive"):
        text += " A course used here cannot be used by another exclusive requirement."
    if r.get("guide_section"):
        text += f" Program guide section: {r['guide_section']}."
    if r.get("recommended_courses"):
        text += f" Recommended by the major: {', '.join(r['recommended_courses'])}."
    if r.get("waived_if"):
        conditions = []
        if r["waived_if"].get("nj_associate_degree"):
            conditions.append("the student earned an A.A./A.S. at a NJ community/county college")
        if "transfer_credits_at_entry" in r["waived_if"]:
            conditions.append(f"the student transferred {r['waived_if']['transfer_credits_at_entry']:g} "
                              f"or more credits at entry")
        text += f" Waived if {' or '.join(conditions)}."
    if r.get("notes"):
        text += f" Note: {r['notes']}."
    return text


def _section_text(s: dict) -> str:
    """One offered section as a sentence, for render_nl."""
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
    """The state as plain-text sentences (--rendering nl)."""
    student, transcript = state["student"], state["transcript"]
    minor = f", with a minor in {student['minor']}" if "minor" in student else ""
    lines = [f"Student {student['student_id']} is pursuing a {student['degree']} in {student['major']}"
             f"{minor} (program {student['program_id']}, catalog year {student['catalog_year']})."]
    entry = student.get("entry")
    if entry:
        text = "They entered Rowan as a first-year student." if entry["type"] == "first_year" else \
               "They entered Rowan as a transfer student"
        if entry["type"] == "transfer":
            details = []
            if "transfer_credits_at_entry" in entry:
                details.append(f"with {entry['transfer_credits_at_entry']:g} transfer credits")
            if "nj_associate_degree" in entry:
                details.append("with" + ("" if entry["nj_associate_degree"] else "out") +
                               " an A.A./A.S. from a NJ community/county college")
            text += (" " + " and ".join(details) if details else "") + "."
        lines.append(text)
    guide = state.get("program_guide")
    if guide:
        lines += ["", f"Program guide: {guide['program_name']}"
                      + (f", {guide['department']}" if guide.get("department") else "")
                      + f" (effective {guide['effective_term']}), {guide['total_credits']:g} credits in all:"]
        lines += [f"- {sec.get('title', sec['section'])} ({sec['section']}): {_credits_text(sec['credit_hours'])} credits."
                  + (f" {sec['notes']}" if sec.get("notes") else "") for sec in guide["sections"]]
        if guide.get("rowan_core"):
            lines.append(f"Rowan Core literacies required: {', '.join(guide['rowan_core']['literacies'])}"
                         + (f", at least {guide['rowan_core']['min_credits_per_literacy']:g} credits each"
                            if "min_credits_per_literacy" in guide["rowan_core"] else "") + ".")
        if guide.get("rowan_experience"):
            lines.append(f"Rowan Experience attributes required: {', '.join(guide['rowan_experience']['attributes'])}.")
        lines += [f"Program rule: {rule}" for rule in guide.get("program_rules", [])]
    lines.append("")
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
    if state.get("course_attributes"):
        lines += ["", "Course attributes (Rowan Core literacies and Rowan Experience designations):"]
        lines += [f"- {c}: {', '.join(a)}" for c, a in state["course_attributes"].items()]
    if state.get("course_equivalents"):
        lines += ["", "Equivalent courses (the same course under different ids):"]
        lines += [f"- {', '.join(group)}" for group in state["course_equivalents"]]
    if state.get("graduation_rules"):
        rules = state["graduation_rules"]
        named = {"total_credits": "{:g} total credits", "minimum_gpa": "a GPA of at least {:g}",
                 "residency_credits": "{:g} credits earned at this institution",
                 "major_gpa": "a GPA of at least {:g} in the courses that satisfy major requirements",
                 "minimum_grade": "a grade of {} or better in any course that applies to the degree"}
        lines += ["", "To graduate the student needs " +
                  ", ".join(named[k].format(v) for k, v in rules.items()) + "."]
    lines += ["", f"Term: {state['schedule']['term']} (term_code {state['schedule']['term_code']})."]
    if "sections" in state["schedule"]:
        lines += [f"Sections offered in {state['schedule']['term']}:"]
        lines += [_section_text(s) for s in state["schedule"]["sections"]] or ["- none"]
    return "\n".join(lines)


def to_task(case: dict, rendering: str = "json", session: "TallySession | None" = None) -> TaskSpec:
    """What the model sees: the whole input object, never the answer key.
    `prompt` becomes the intent; the rest is the known state. In a session the sections are
    not in the case's own text: they are the session's opening turn."""
    data = case["input"]
    state = {key: value for key, value in data.items() if key != "prompt"}
    if is_whole(case):                        # the sections are the session's opening
        state["schedule"] = {"term": data["schedule"]["term"], "term_code": data["schedule"]["term_code"]}
    if is_whole(case) and session is None:
        raise ValueError(f"{case['case_id']} reads the whole tally: run it inside a TallySession")
    if rendering == "json":
        inputs = json.dumps(state)
    elif rendering == "csv":      # harness.py labels the state "JSON", so say what it really is
        inputs = "(The state below is given as CSV tables, not JSON.)\n" + render_csv(state)
    else:
        inputs = "(The state below is given as plain text, not JSON.)\n" + render_nl(state)
    return TaskSpec(
        task_id=case["case_id"],
        inputs=inputs,
        intent=data["prompt"],
        output_contract=OUTPUT_CONTRACT + CRN_CONTRACT + (session.contract if is_whole(case) else ""),
        shared_context=session.text if is_whole(case) else "",
        shared_context_ttl=session.ttl if is_whole(case) else "5m",
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


# ── The results file: {"sessions": [summary, ...], "results": [row, ...]} ──
# A session summary holds what every call of one run shares (model, tally version, cached
# opening, output contract) and its totals; a result row holds only what is its own and names
# its session. Nothing is stored twice, except deviation_score (also averaged in the summary).

# Fields that are the same for every row of a session: kept once, in its summary.
SESSION_FIELDS = ("model", "tally_variant", "shared_context", "session_cost_usd", "session_avg_cost_per_prompt_usd")
# Duplicates dropped from a row: prompt_sent (prompt_readable holds it, parsed), ok (= error is null).
ROW_DROPPED = ("prompt_sent", "ok")
# Verdict fields that repeat others: tiers.tier3, the failed tier4_checks, and tiers.tier4's count.
VERDICT_DROPPED = ("tier3_detail", "violated", "tier4_summary")
ROW_ORDER = ("task_id", "session_id", "run_index", "ts", "case_hashes", "prompt_readable", "parsed_output",
             "raw_output", "error", "finish_reason", "native_finish_reason", "served_model", "provider",
             "generation_id", "prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens",
             "reasoning_tokens", "first_token_ms", "latency_ms", "usd_cost", "verdict", "pair_result")


def answer_text(row: dict) -> str:
    """The model's answer as the scorer reads it: raw_output when the row keeps it (it did not
    parse as JSON), else the parsed message, serialized."""
    return row["raw_output"] if "raw_output" in row else json.dumps(row["parsed_output"])


def slim_row(row: dict) -> dict:
    """A result row without its session's shared fields or duplicates: the raw answer is kept
    only when it is not JSON (otherwise parsed_output holds it), and the output contract only
    where it differs from the session's (the caller passes that check). Idempotent."""
    out = {k: v for k, v in row.items() if k not in SESSION_FIELDS + ROW_DROPPED}
    if out.get("parsed_output") is not None and "raw_output" in out:
        try:
            if json.loads(out["raw_output"]) == out["parsed_output"]:
                del out["raw_output"]
        except (json.JSONDecodeError, TypeError):
            pass
    if out.get("parsed_output") is None:
        out.pop("parsed_output", None)
    if out.get("raw_output") == "" and out.get("error") is not None:
        del out["raw_output"]                 # a failed call returned nothing; error says why
    if isinstance(out.get("verdict"), dict):
        out["verdict"] = {k: v for k, v in out["verdict"].items() if k not in VERDICT_DROPPED}
    ordered = {k: out[k] for k in ROW_ORDER if k in out}
    return {**ordered, **{k: v for k, v in out.items() if k not in ordered}}


def load_results(path: str) -> tuple:
    """(sessions, results) from a results file, [] and [] if there is none. An older file (a
    bare list of rows, each carrying its session's fields) is converted: each session's shared
    fields become its summary's, and rows without a session are grouped by model."""
    file = Path(path)
    if not file.exists():
        return [], []
    data = json.loads(file.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data.get("sessions", []), data.get("results", [])
    sessions = {}
    for row in data:
        sid = row.get("session_id") or f"named-{row.get('model', 'unknown')}"
        row["session_id"] = sid
        s = sessions.setdefault(sid, {"session_id": sid})
        for field in ("model", "tally_variant", "shared_context"):
            s.setdefault(field, row.get(field))
        contract = (row.get("prompt_readable") or {}).get("output_contract")
        if contract is not None:
            s.setdefault("output_contract", contract)
    return list(sessions.values()), data


def save_results(path: str, sessions: list, results: list) -> None:
    """Writes the results file; every row is slimmed, and a row's output contract is dropped
    when it is its session's."""
    contract = {s["session_id"]: s.get("output_contract") for s in sessions}
    rows = []
    for row in results:
        row = slim_row(row)
        readable = row.get("prompt_readable")
        if isinstance(readable, dict) and readable.get("output_contract") == contract.get(row.get("session_id")):
            row["prompt_readable"] = {k: v for k, v in readable.items() if k != "output_contract"}
        rows.append(row)
    Path(path).write_text(json.dumps({"sessions": sessions, "results": rows}, indent=2) + "\n", encoding="utf-8")


def wilson(k: int, n: int, z: float = 1.96) -> list | None:
    """The 95% Wilson score interval for k successes in n trials, as [low, high] rounded to 3."""
    if not n:
        return None
    p, d = k / n, 1 + z * z / n
    centre, half = (p + z * z / (2 * n)) / d, z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def _rate(k: int, n: int) -> dict:
    """{"k/n", rate, 95% interval} for a summary."""
    return {"count": f"{k}/{n}", "rate": round(k / n, 3) if n else None, "ci95": wilson(k, n)}


def _cost_breakdown(rows: list, correct: int, passed_pairs: int | None, earlier: list) -> dict:
    """What the session cost, split into the one-time cache write of the shared opening and the
    marginal cost of one case, plus cost per correct answer and per passed pair. The split is an
    estimate: OpenRouter reports one cost per call, so the marginal cost is the median cost of
    the calls that only read the cache, and the setup cost is what the writing calls cost above it."""
    ok = [r for r in rows if r.get("error") is None]
    cost = sum(r.get("usd_cost", 0) or 0 for r in rows)
    reads = sorted(r["usd_cost"] for r in ok if not r.get("cache_write_tokens") and r.get("cached_tokens"))
    marginal = reads[len(reads) // 2] if reads else None
    writes = [r["usd_cost"] for r in ok if r.get("cache_write_tokens")]
    setup = round(max(0.0, sum(writes) - len(writes) * marginal), 6) if writes and marginal is not None else None
    earlier_usd = sum(e.get("cost_usd", 0) for e in earlier)
    out = {"total_usd": round(cost, 6), "avg_per_call_usd": round(cost / len(rows), 6) if rows else None,
           "setup_usd_est": setup, "per_case_marginal_usd_est": round(marginal, 6) if marginal is not None else None,
           "per_correct_answer_usd": round(cost / correct, 6) if correct else None}
    if passed_pairs is not None:
        out["per_passed_pair_usd"] = round(cost / passed_pairs, 6) if passed_pairs else None
    if earlier:
        out["earlier_attempts_usd"] = round(earlier_usd, 6)
        out["total_spent_usd"] = round(cost + earlier_usd, 6)
    out["note"] = ("All costs are what OpenRouter billed for the original model calls; re-grading a results "
                   "file (--rescore) makes no model call and costs nothing.")
    return out


def summarize_session(meta: dict, rows: list, pairs: dict, cases: dict | None = None) -> dict:
    """A session's summary: its shared settings (meta) and what its rows add up to. cases: the
    case file's raw cases by id, for pair flags, case types and chance baselines."""
    cases = cases or {}
    calls = len(rows)
    outcomes, errors, finish, served = {}, {}, {}, {}
    for r in rows:
        v = r["verdict"]
        outcomes[v["outcome"]] = outcomes.get(v["outcome"], 0) + 1
        if v.get("error_type"):
            errors[v["error_type"]] = errors.get(v["error_type"], 0) + 1
        if r.get("finish_reason") or r.get("native_finish_reason"):
            key = f"{r.get('finish_reason')} ({r.get('native_finish_reason')})"
            finish[key] = finish.get(key, 0) + 1
        if r.get("provider") or r.get("served_model"):
            key = f"{r.get('served_model')} via {r.get('provider')}"
            served[key] = served.get(key, 0) + 1
    scored = [r["verdict"]["deviation_score"] for r in rows if "deviation_score" in r["verdict"]]
    tokens = {k: sum(r.get(k, 0) or 0 for r in rows)
              for k in ("prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens")}
    tokens["reasoning_tokens"] = (sum(r.get("reasoning_tokens") or 0 for r in rows)
                                  if any("reasoning_tokens" in r for r in rows) else "not recorded")
    ok = [r for r in rows if r.get("error") is None]
    correct = outcomes.get("semantic_ok", 0)
    by_type = {}
    for r in ok:
        kind = (cases.get(r["task_id"], {}).get("ground_truth") or {}).get("case_type")
        if kind:
            t = by_type.setdefault(kind, [0, 0])
            t[0] += r["verdict"]["outcome"] == "semantic_ok"
            t[1] += 1
    chance = [cases[r["task_id"]]["ground_truth"]["difficulty"]["chance"] for r in ok
              if (cases.get(r["task_id"], {}).get("ground_truth") or {}).get("difficulty")]
    first = [r["first_token_ms"] for r in ok if r.get("first_token_ms") is not None]
    weak = {pid for pid in pairs if any((cases.get(cid, {}).get("ground_truth") or {}).get("pair", {}).get("has_shared_answer")
                                        for cid in pairs[pid].values())}
    stats = pair_stats(pairs, {(r["task_id"], r["run_index"]): r["verdict"]["outcome"] for r in rows}, weak)
    sampling = meta.get("samples_per_case")
    summary = {
        **{k: v for k, v in meta.items() if v is not None and k != "earlier_attempts"},
        "started": min((r["ts"] for r in rows), default=None), "ended": max((r["ts"] for r in rows), default=None),
        "cases": sorted({r["task_id"] for r in rows}), "calls": calls,
        "generation_failures": calls - len(ok),
        "outcomes": dict(sorted(outcomes.items())),
        "semantic_ok": _rate(correct, len(ok)),
        "semantic_ok_by_case_type": {k: f"{a}/{n}" for k, (a, n) in sorted(by_type.items())},
        "mean_chance_baseline": round(sum(chance) / len(chance), 4) if chance else None,
        "mean_deviation_score": round(sum(scored) / len(scored), 4) if scored else None,
        "error_types": dict(sorted(errors.items(), key=lambda x: -x[1])),
        "finish_reasons": finish or "not recorded (calls made before 2026-10-19 streaming)",
        "served_by": served or "not recorded (calls made before 2026-10-19 streaming)",
        "tokens": {**tokens, "cache_read_share": f"{100 * tokens['cached_tokens'] / tokens['prompt_tokens']:.0f}%"
                   if tokens["prompt_tokens"] else None},
        "cost": _cost_breakdown(rows, correct, stats.get("passed") if stats else None, meta.get("earlier_attempts", [])),
        "latency": {"mean_total_ms": round(sum(r["latency_ms"] for r in ok) / len(ok)) if ok else None,
                    "mean_first_token_ms": round(sum(first) / len(first)) if first else None},
    }
    if meta.get("earlier_attempts"):
        summary["earlier_attempts"] = meta["earlier_attempts"]
    if stats:
        summary["paired_accuracy"] = stats
    if sampling is not None and sampling < 3:
        summary["sampling_note"] = (f"{sampling} sample(s) per case: intervals are wide, and a difference between "
                                    f"sessions can be chance. Use -n 3 or more, and --compare for a paired test.")
    return summary


SUMMARY_TOTALS = ("started", "ended", "cases", "calls", "generation_failures", "outcomes", "semantic_ok",
                  "semantic_ok_by_case_type", "mean_chance_baseline", "mean_deviation_score", "error_types",
                  "finish_reasons", "served_by", "tokens", "cost", "latency", "paired_accuracy", "sampling_note",
                  # older summary layouts
                  "cost_usd", "avg_cost_per_call_usd", "mean_latency_ms")


def rebuild_summaries(case_file: str, sessions: list, results: list) -> list:
    """Every session's summary recomputed from its rows (settings kept, totals refreshed)."""
    raw = json.loads(Path(case_file).read_text(encoding="utf-8")) if Path(case_file).exists() else []
    pairs, cases = pairs_of(raw), {c["case_id"]: c for c in raw}
    out = []
    for s in sessions:
        meta = {k: v for k, v in s.items() if k not in SUMMARY_TOTALS}
        out.append(summarize_session(meta, [r for r in results if r.get("session_id") == s["session_id"]],
                                     pairs, cases))
    return out


def log_readable(result, verdict: dict, path: str, meta: dict, extra: dict | None = None) -> None:
    """Appends one result row to the results file, adding its session's summary (meta: the
    settings the run shares) the first time; totals are filled in when the run ends. extra:
    more row fields (the case's hashes)."""
    sessions, results = load_results(path)
    if not any(s["session_id"] == meta["session_id"] for s in sessions):
        sessions.append(dict(meta))
    row = readable_row(asdict(result), verdict)
    row.update(session_id=meta["session_id"], **(extra or {}))
    results.append(row)
    save_results(path, sessions, results)


def file_hash(path: str) -> str:
    """The first 16 hex digits of a file's sha256."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def case_hashes(case: dict) -> dict:
    """Hashes of what the model saw (the case's input, as the case file writes it) and of its
    answer key's rules, so a result can be matched to the exact case version it was run on."""
    digest = lambda obj: hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return {"input": digest(case.get("input")), "rules": digest((case.get("ground_truth") or {}).get("rules"))}


def code_version() -> dict:
    """The git commit the code ran from, and whether the working tree had uncommitted changes
    (then the commit alone does not reproduce the run; the changed files are listed)."""
    def git(*args):
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True, timeout=20,
                                  cwd=Path(__file__).parent).stdout.rstrip("\n")
        except (OSError, subprocess.SubprocessError):
            return ""
    commit = git("rev-parse", "HEAD").strip()
    # porcelain lines are "XY path": two status letters (either may be a space), a space, the path
    changed = [line[3:] for line in git("status", "--porcelain").splitlines() if line.strip()]
    return {"commit": commit or None, "branch": git("rev-parse", "--abbrev-ref", "HEAD").strip() or None,
            "uncommitted_changes": bool(changed), "changed_files": changed}


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
    pairs = pairs_of(cases)
    if pairs:
        problems = check_pairs(cases)
        for p in problems:
            print(f"    - {p}")
        print(f"{len(pairs) - len({p.split(':')[0] for p in problems})}/{len(pairs)} pairs sound.")
        failed += len(problems)
    return failed


def main():
    """Command line. --check scores every answer key and exits; otherwise every case is validated
    first (nothing is spent on a broken case), then run in a session or, with --named-courses,
    without one."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="required unless --check")
    parser.add_argument("--test-file", default="input_format_cases.json")
    parser.add_argument("--output", default="semantic_eval_results.json")
    parser.add_argument("--task-ids", nargs="+", help="run only these cases, e.g. case_012 case_013")
    parser.add_argument("-n", type=int, default=1, help="samples per case")
    parser.add_argument("--rendering", choices=RENDERINGS, default="json",
                        help="how a case's state is shown to the model")
    parser.add_argument("--check", action="store_true",
                        help="score each case's own answer key and exit; no model call")
    parser.add_argument("--named-courses", action="store_true",
                        help="no session: each case's prompt lists only the sections of the courses "
                             "it names (the earlier pipeline)")
    parser.add_argument("--tally", choices=list(TALLY_VARIANTS), default="trimmed",
                        help="the session's opening: 1 raw (the CSV as downloaded), 2 trimmed (converted, "
                             "unused columns dropped), 3 raw-open (raw, full and cancelled sections "
                             "dropped), 4 trimmed-open (both)")
    parser.add_argument("--cache-ttl", choices=["5m", "1h"], default="5m",
                        help="how long the session's cached opening lives between calls")
    parser.add_argument("--pair-report", action="store_true",
                        help="print paired accuracy per model from --output against --test-file's pairs and exit")
    parser.add_argument("--rescore", action="store_true",
                        help="re-grade the saved answers in --output with the current scorer and rewrite their "
                             "verdicts; no model call")
    parser.add_argument("--max-tokens", type=int, help="cap on output tokens per call (default: the provider's)")
    parser.add_argument("--effort", choices=["minimal", "low", "medium", "high", "xhigh"],
                        help="reasoning effort sent to the model (default: the model's own; recorded either way)")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"),
                        help="compare two results files on the cases and runs both have (McNemar's test); "
                             "no model call")
    args = parser.parse_args()

    if args.pair_report:
        pair_report(args.test_file, args.output)
        return
    if args.rescore:
        rescore(args.test_file, args.output)
        return
    if args.compare:
        compare(args.test_file, *args.compare)
        return
    cases = load_test_cases(args.test_file, args.task_ids, whole=not args.named_courses)
    if args.check:
        sys.exit(1 if check_cases(cases) else 0)
    if not args.model:
        parser.error("--model is required unless --check is given")

    backend = OpenRouterBackend(max_tokens=args.max_tokens, effort=args.effort)
    for case in cases:                       # every case is sound before anything is spent
        problems = validate_case(case)
        if problems:
            raise ValueError(f"{case.get('case_id')} is not a valid test case: {problems}")
    if any(is_whole(case) for case in cases):
        with TallySession(args.cache_ttl, args.tally) as session:
            run_cases(cases, backend, args, session)
    else:
        run_cases(cases, backend, args)


def mcnemar(only_a: int, only_b: int) -> float:
    """Two-sided exact McNemar p-value: of the items where A and B disagree, how surprising the
    split is if neither is really better (a binomial test at p = 0.5)."""
    from math import comb
    n = only_a + only_b
    if not n:
        return 1.0
    tail = sum(comb(n, i) for i in range(min(only_a, only_b) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def compare(case_file: str, file_a: str, file_b: str) -> None:
    """McNemar's test between two results files, on the (case, run) items both have with no
    failed call: per case, and per pair when the cases are pairs. Prints the counts and p."""
    def outcomes(path):
        sessions, results = load_results(path)
        label = ", ".join(f"{s.get('model')} / {s.get('tally_variant') or 'named'}" for s in sessions)
        return label, {(r["task_id"], r["run_index"]): r["verdict"]["outcome"] for r in results}
    (la, a), (lb, b) = outcomes(file_a), outcomes(file_b)
    keys = sorted(k for k in set(a) & set(b) if "generation_failed" not in (a[k], b[k]))
    ok = lambda o: o == "semantic_ok"
    both, only_a = sum(ok(a[k]) and ok(b[k]) for k in keys), sum(ok(a[k]) and not ok(b[k]) for k in keys)
    only_b, neither = sum(ok(b[k]) and not ok(a[k]) for k in keys), sum(not ok(a[k]) and not ok(b[k]) for k in keys)
    print(f"A = {file_a} ({la})\nB = {file_b} ({lb})\n{len(keys)} shared case runs")
    print(f"  both right {both}, only A {only_a}, only B {only_b}, neither {neither}")
    print(f"  per case: A {both + only_a}/{len(keys)}, B {both + only_b}/{len(keys)}, "
          f"McNemar p = {mcnemar(only_a, only_b):.3f}")
    pairs = pairs_of(json.loads(Path(case_file).read_text(encoding="utf-8")))
    runs = sorted({run for _, run in keys})
    pa, pb = [], []
    for variants in pairs.values():
        for run in runs:
            ks = [(variants.get(v), run) for v in PAIR_VARIANTS]
            if all(k in keys for k in ks):
                pa.append(all(ok(a[k]) for k in ks))
                pb.append(all(ok(b[k]) for k in ks))
    if pa:
        oa, ob = sum(x and not y for x, y in zip(pa, pb)), sum(y and not x for x, y in zip(pa, pb))
        print(f"  per pair: A {sum(pa)}/{len(pa)}, B {sum(pb)}/{len(pb)}, only A {oa}, only B {ob}, "
              f"McNemar p = {mcnemar(oa, ob):.3f}")
    print("  p < 0.05 suggests a real difference; with few disagreements no test can show one.")


def rescore(case_file: str, results_file: str) -> None:
    """Re-grades every saved answer in a results file with the current scorer (all four tiers),
    rewrites its verdict and pair_result, and rebuilds the session summaries. A row whose call
    failed keeps its verdict. Each case is graded against the schedule its session used: the
    whole tally when the session had one, else the named courses. Prints any outcome that
    changed. An older file (a bare list of rows) is rewritten in the current layout."""
    sessions, results = load_results(results_file)
    variant = {s["session_id"]: s.get("tally_variant") for s in sessions}
    ids = {row["task_id"] for row in results}
    whole = load_test_cases(case_file, ids, whole=True) if any(variant.get(r["session_id"]) for r in results) else []
    named = load_test_cases(case_file, ids) if any(not variant.get(r["session_id"]) for r in results) else []
    by_id = {"whole": {c["case_id"]: c for c in whole}, "named": {c["case_id"]: c for c in named}}
    changed = graded = 0
    for row in results:
        if row.get("error") is not None:
            row["verdict"].setdefault("error_type", "call_failed")
            continue
        case = by_id["whole" if variant.get(row["session_id"]) else "named"][row["task_id"]]
        verdict = evaluate_case(case, answer_text(row))
        tag = call_error_type(verdict, row.get("finish_reason"), row.get("native_finish_reason"))
        if tag:
            verdict = _with_tiers({**verdict, "error_type": tag})
        if verdict["outcome"] != row["verdict"]["outcome"]:
            changed += 1
            print(f"{row['task_id']} run {row['run_index']}: {row['verdict']['outcome']} -> {verdict['outcome']}")
        row["verdict"] = verdict
        graded += 1
    related = relate_pairs(case_file, results)
    save_results(results_file, rebuild_summaries(case_file, sessions, results), results)
    print(f"rescored {graded} rows in {results_file}; {changed} outcomes changed"
          + (f"; pair_result on {related} rows" if related else ""))


def run_cases(cases: list, backend, args, session: TallySession | None = None) -> None:
    """Runs every case n times: generate, score through all four tiers, print one line, and
    append the row to the results file. At the end, sets each pair row's pair_result and writes
    the session's summary (settings and totals) into the file, then prints the totals and
    paired accuracy."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    first = to_task(cases[0], args.rendering, session) if cases else None
    raw_cases = {c["case_id"]: c for c in json.loads(Path(args.test_file).read_text(encoding="utf-8"))}
    meta = {"session_id": session.id if session else f"named-{stamp}", "model": args.model,
            "test_file": args.test_file, "test_file_sha256": file_hash(args.test_file),
            "code_version": code_version(), "tally_variant": session.variant if session else None,
            "rendering": args.rendering, "samples_per_case": args.n,
            "cache_ttl": session.ttl if session else None,
            "generation_settings": backend.settings,
            "shared_context": shared_context_info(first) if first and first.shared_context else None,
            "output_contract": first.output_contract if first else None}
    if args.n < 3:
        print(f"note: {args.n} sample(s) per case; use -n 3 or more to compare sessions reliably")
    passed, runs, cost, outcomes = 0, 0, 0.0, {}
    for case in cases:
        task = to_task(case, args.rendering, session)
        hashes = case_hashes(raw_cases.get(case["case_id"], case))

        for run_index in range(args.n):
            result = generate(backend, args.model, task, run_index=run_index)
            if result.ok:
                verdict = evaluate_case(case, result.raw_output)
                tag = call_error_type(verdict, result.finish_reason, result.native_finish_reason)
                if tag:
                    verdict = _with_tiers({**verdict, "error_type": tag})
            else:
                verdict = {"outcome": "generation_failed", "detail": result.error, "error_type": "call_failed"}

            if session:
                session.record(result)
            passed += verdict["outcome"] == "semantic_ok"
            outcomes[(task.task_id, run_index)] = verdict["outcome"]
            runs += 1
            cost += result.usd_cost
            label = task.task_id if args.n == 1 else f"{task.task_id} run {run_index}"
            cache = (f" [cache: {result.cached_tokens:,} read, {result.cache_write_tokens:,} "
                     f"written, ${result.usd_cost:.4f}]") if is_whole(case) else ""
            print(f"{label}: {verdict['outcome']:20s} | {verdict.get('detail', '')}{cache}")
            log_readable(result, verdict, args.output, meta, {"case_hashes": hashes})

    sessions, results = load_results(args.output)
    if pairs_of(cases):
        relate_pairs(args.test_file, results)     # each pair row's pair_result
    save_results(args.output, rebuild_summaries(args.test_file, sessions, results), results)
    print(f"\n{passed}/{runs} semantic_ok on {args.model}. Cost ${cost:.4f}. "
          f"Full results in {args.output} (session {meta['session_id']})")
    for line in pair_summary(pairs_of(cases), outcomes):
        print(line)


if __name__ == "__main__":
    main()
