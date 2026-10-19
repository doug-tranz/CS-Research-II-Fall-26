# registration.py — registration domain testbed for the CART message (PROVISIONAL).
# Digests a DIDComm message; scores it on: syntactic + schema (the rules in
# didcomm_schema.py) → consistency (invariants) → semantic (intent, stubbed here;
# run_semantic_eval.py fills it in).
#
# Also holds the INPUT FORMAT (README.md, "The input format"): validating an input
# object, a degree audit over its transcript and requirements, and seeding the
# checker's database from it so the model and the checker see the same situation.
import json
import sqlite3
from pathlib import Path
from jsonschema import Draft202012Validator
from didcomm_schema import validate_message   # Tiers 1-2: registration_message.schema.json

SCHEMA = """
CREATE TABLE sections (
    section_id  TEXT PRIMARY KEY,
    course_id   TEXT NOT NULL,
    seats_total INTEGER NOT NULL,
    seats_taken INTEGER NOT NULL
);
CREATE TABLE meetings (                -- one row per day a section meets
    section_id TEXT NOT NULL,
    day        TEXT NOT NULL,          -- "Monday" ... "Sunday"
    start_min  INTEGER NOT NULL,       -- minutes after midnight
    end_min    INTEGER NOT NULL,
    FOREIGN KEY (section_id) REFERENCES sections(section_id)
);
CREATE TABLE term (                   -- the term this database's sections are offered in (input format)
    term_code TEXT NOT NULL
);
CREATE TABLE section_credits (        -- credits a section allows; min = max for a fixed-credit section
    section_id  TEXT PRIMARY KEY,
    min_credits REAL NOT NULL,
    max_credits REAL NOT NULL
);
CREATE TABLE waitlists (
    section_id TEXT PRIMARY KEY,
    capacity   INTEGER NOT NULL,
    enrolled   INTEGER NOT NULL
);
CREATE TABLE cancelled_sections (     -- sections the registrar cancelled (input format)
    section_id TEXT PRIMARY KEY
);
CREATE TABLE section_details (        -- what the message must copy, beyond `sections` (input format)
    section_id TEXT PRIMARY KEY,
    section    TEXT NOT NULL,          -- section number, e.g. "1"
    campus     TEXT NOT NULL,
    modality   TEXT NOT NULL,
    meetings   TEXT NOT NULL           -- the section's meetings, as JSON
);
CREATE TABLE prereq_missing (          -- prerequisites THIS student has not met (input format)
    course_id     TEXT NOT NULL,       -- the offered course
    requires      TEXT NOT NULL,       -- the course it needs first
    minimum_grade TEXT NOT NULL
);
"""

def make_db() -> sqlite3.Connection:
    """An empty in-memory checker database; seed_db() fills it from an input object."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

def _meetings(conn, section: str) -> list:
    """A section's meetings as (day, start_min, end_min) rows from the checker database."""
    return conn.execute(
        "SELECT day, start_min, end_min FROM meetings WHERE section_id=?", (section,)).fetchall()

def _overlap(a: list, b: list):
    """The first day two sets of meetings share with overlapping time ranges, else None."""
    for day_a, start_a, end_a in a:
        for day_b, start_b, end_b in b:
            if day_a == day_b and start_a < end_b and start_b < end_a:
                return day_a
    return None

def _same_text(a, b) -> bool:
    """Equality for copied text fields, ignoring case and surrounding spaces."""
    return str(a).strip().casefold() == str(b).strip().casefold()

def _meeting_key(m: dict) -> tuple:
    """A meeting's days and times, ignoring day order and a leading zero ('9:30 AM' = '09:30 AM')."""
    times = (minutes(m["start_time"]), minutes(m["end_time"])) if "start_time" in m else ()
    return tuple(sorted(m["days"])), times

def _meetings_differ(copied: list, offered: list):
    """Why the message's meetings are not the offered ones, else None. Days and times must
    match; `type` and `location` may be left out, but must match when given."""
    if sorted(map(_meeting_key, copied)) != sorted(map(_meeting_key, offered)):
        return "days or times differ"
    for m in copied:
        same = [o for o in offered if _meeting_key(o) == _meeting_key(m)]
        if not any(all(_same_text(m[k], o.get(k, "")) for k in ("type", "location") if k in m)
                   for o in same):
            return "a meeting's type or location differs"
    return None

def _copy_differs(conn, item: dict, course: str, seats_total: int, seats_taken: int):
    """The first field the message copied wrongly from the offered section, else None."""
    details = conn.execute("SELECT section, campus, modality, meetings FROM section_details "
                           "WHERE section_id=?", (item["crn"],)).fetchone()
    if details is None:
        return "the checker has no details for it"
    section, campus, modality, meetings = details
    for field, offered in (("course_id", course), ("section", section), ("campus", campus),
                           ("modality", modality)):
        if not _same_text(item[field], offered):
            return f"{field} {item[field]!r} is not the offered section's ({offered!r})"
    for field, offered in (("seats_total", seats_total), ("seats_taken", seats_taken)):
        if item[field] != offered:
            return f"{field} {item[field]} is not the offered section's ({offered})"
    differs = _meetings_differ(item["meetings"], json.loads(meetings))
    if differs:
        return f"meetings do not match the offered section's: {differs}"
    return None

def consistency_checks(conn, body: dict) -> list:
    """Tier 3, every invariant in order: the term, then for each cart section its existence,
    copied fields, cancellation, seat (or waitlist), credits, prerequisites, and the cart's
    own rules (one section per course, no overlaps). Each check is {"check", "section",
    "requires", "found", "passed", "reason"}; "reason" is the failure text check_consistency()
    reports, None on a pass. A section that does not exist gets no further checks. Assumes
    body already passed schema validation."""
    checks = []

    def check(name, section, requires, found, reason=None):
        checks.append({"check": name, "section": section, "requires": requires, "found": found,
                       "passed": reason is None, "reason": reason})

    term = conn.execute("SELECT term_code FROM term").fetchone()
    if term:
        check("term", None, f"term_code is the term being registered for ({term[0]})", f"term_code {body['term_code']}",
              None if body["term_code"] == term[0] else
              f"term_code {body['term_code']} is not the term being registered for ({term[0]})")
    cart_course = {}                      # course -> the cart section that holds it
    cart_times = {}                       # cart section -> its structured meetings
    for item in body["sections"]:
        section = item["crn"]
        row = conn.execute(
            "SELECT course_id, seats_total, seats_taken FROM sections WHERE section_id=?",
            (section,)).fetchone()
        check("exists", section, f"crn {section} is a section offered this term",
              f"offered: {row[0]}" if row else "no section has this crn",
              None if row else f"section {section} does not exist")
        if row is None:
            continue
        course, seats_total, seats_taken = row
        wrong = _copy_differs(conn, item, course, seats_total, seats_taken)
        check("copied_fields", section, "course_id, section, campus, modality, seats and meetings are copied exactly "
              "from the offered section", wrong or "all match", f"section {section}: {wrong}" if wrong else None)
        cancelled = conn.execute("SELECT 1 FROM cancelled_sections WHERE section_id=?", (section,)).fetchone()
        check("not_cancelled", section, "the section is not cancelled", "cancelled" if cancelled else "not cancelled",
              f"section {section} is cancelled" if cancelled else None)
        seats = f"{seats_taken}/{seats_total} seats taken"
        if item.get("waitlist"):
            waitlist = conn.execute("SELECT capacity, enrolled FROM waitlists WHERE section_id=?",
                                    (section,)).fetchone()
            found = seats + (f", waitlist {waitlist[1]}/{waitlist[0]}" if waitlist else ", no waitlist")
            reason = (f"section {section} has open seats ({seats_taken}/{seats_total}), so it cannot be waitlisted"
                      if seats_taken < seats_total else
                      f"section {section} is full and has no waitlist" if waitlist is None else
                      f"the waitlist for section {section} is full ({waitlist[1]}/{waitlist[0]})"
                      if waitlist[1] >= waitlist[0] else None)
            check("waitlist", section, "a waitlisted section is full and its waitlist has room", found, reason)
        else:
            check("seat", section, "the section has a free seat", seats,
                  f"section {section} is full ({seats_taken}/{seats_total})" if seats_taken >= seats_total else None)
        allowed = conn.execute("SELECT min_credits, max_credits FROM section_credits WHERE section_id=?",
                               (section,)).fetchone()
        chosen = item.get("credit_hours")
        if allowed:
            low, high = allowed
            allowed_text = f"{low:g}" if low == high else f"{low:g}-{high:g}"
            reason = (f"section {section} is variable-credit ({low:g}-{high:g}); the message must give credit_hours"
                      if low != high and chosen is None else
                      f"section {section}: credit_hours {chosen:g} is not {allowed_text}"
                      if chosen is not None and not low <= chosen <= high else None)
            check("credits", section, f"credit_hours is allowed for this section ({allowed_text})",
                  f"credit_hours {chosen:g}" if chosen is not None else "credit_hours not given", reason)
        unmet = conn.execute(
            "SELECT requires, minimum_grade FROM prereq_missing WHERE course_id=?", (course,)).fetchone()
        check("prerequisites", section, f"the transcript shows every prerequisite of {course}",
              f"missing {unmet[0]} ({unmet[1]} or better)" if unmet else "no unmet prerequisite",
              f"section {section}: {course} needs prerequisite {unmet[0]} ({unmet[1]} or better), "
              f"which the transcript does not show" if unmet else None)
        # A time conflict is two meetings on a shared day whose time ranges overlap.
        times = _meetings(conn, section)
        # Invariants that only exist for a cart: its sections against each other.
        check("one_section_per_course", section, f"no other section of {course} is in the cart",
              f"also {cart_course[course]}" if course in cart_course else "the only one",
              f"two sections of {course} in the cart: {cart_course[course]} and {section}"
              if course in cart_course else None)
        clash = next(((other, day) for other, other_times in cart_times.items()
                      for day in [_overlap(times, other_times)] if day), None)
        check("no_time_conflict", section, "no meeting overlaps another section in the cart",
              f"overlaps {clash[0]} on {clash[1]}" if clash else
              ("no overlap" if cart_times else "the first section in the cart"),
              f"time conflict inside the cart: {clash[0]} and {section} on {clash[1]}" if clash else None)
        cart_course.setdefault(course, section)
        cart_times[section] = times
    return checks


def check_consistency(conn, body: dict):
    """Returns (ok, reason): the first failed invariant of consistency_checks(), else 'consistent'.
    Assumes body already passed schema validation."""
    failed = next((c for c in consistency_checks(conn, body) if not c["passed"]), None)
    return (False, failed["reason"]) if failed else (True, "consistent")

def evaluate(conn, raw_output: str) -> dict:
    # Tier 1: syntactic — is it a DIDComm plaintext message?
    # Tier 2: schema — is it OUR register message, with a well-formed cart?
    """Tiers 1-3 for one raw model output against this database. A message that passes all
    three comes back as consistent__semantic_TBD; run_semantic_eval.py adds Tier 4."""
    checked = validate_message(raw_output)
    shown = {"tier1_checks": checked["tier1_checks"], "tier2_checks": checked["tier2_checks"]}
    if checked["outcome"] != "schema_ok":
        return {"outcome": checked["outcome"], "detail": checked["detail"], **shown, "tier3_checks": []}
    # Tier 3: consistency — invariants (every one is listed; the first failure is the reason)
    checks = consistency_checks(conn, checked["body"])
    tier3 = [{k: v for k, v in c.items() if k != "reason"} for c in checks]
    failed = next((c for c in checks if not c["passed"]), None)
    if failed:
        return {"outcome": "consistency_error", "detail": failed["reason"], **shown, "tier3_checks": tier3}
    # Tier 4: semantic — STUB here; run_semantic_eval.py adds it on top
    return {"outcome": "consistent__semantic_TBD",
            "detail": "invariants hold", **shown, "tier3_checks": tier3}


# ── Input format: one JSON object per registration situation ──
INPUT_SCHEMA = json.loads(
    Path(__file__).with_name("registration_input.schema.json").read_text(encoding="utf-8"))
Draft202012Validator.check_schema(INPUT_SCHEMA)
_INPUT = Draft202012Validator(INPUT_SCHEMA)

GRADE_ORDER = ["A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-"]   # highest to lowest
GRADE_POINTS = dict(zip(GRADE_ORDER, [4.0, 3.7, 3.3, 3.0, 2.7, 2.3, 2.0, 1.7, 1.3, 1.0, 0.7]), F=0.0)
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

def validate_input(data) -> list:
    """Returns a list of problems; empty means the input object is valid."""
    found = sorted(_INPUT.iter_errors(data), key=lambda e: list(map(str, e.absolute_path)))
    problems = [f"{'/'.join(map(str, e.absolute_path)) or '<input>'}: {e.message}" for e in found]
    if problems:
        return problems          # the checks below assume the shape is right

    sections = data["schedule"]["sections"]
    for crn in _duplicates(s["crn"] for s in sections):
        problems.append(f"crn {crn} is used by more than one section")
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

def meets_minimum(grade: str, minimum: str) -> bool:
    """Is `grade` at or above `minimum`? PROVISIONAL (spec open item 5): accepted
    transfer credit (`TA`) meets every minimum. `F` and `W` meet none."""
    if grade == "TA":
        return True
    return grade in GRADE_ORDER and GRADE_ORDER.index(grade) <= GRADE_ORDER.index(minimum)

def _rank(grade: str) -> int:             # lower is better; TA ranks with the best
    return -1 if grade == "TA" else GRADE_ORDER.index(grade) if grade in GRADE_ORDER else 99

def _canonical(data: dict):
    """Maps a course id to one id per course_equivalents group (spec open item 6)."""
    same = {c: group[0] for group in data.get("course_equivalents", []) for c in group}
    return lambda course_id: same.get(course_id, course_id)

def catalog_attributes(data: dict, course_id: str) -> set:
    """A course's Rowan Core / Rowan Experience designations from `course_attributes`,
    looked up under its own id and under any equivalent id."""
    canon, table = _canonical(data), data.get("course_attributes", {})
    return {a for listed, attrs in table.items() if canon(listed) == canon(course_id) for a in attrs}

def _attempts(data: dict) -> list:
    """Every graded attempt on the transcript. In-progress courses have no grade, so none."""
    canon, transcript = _canonical(data), data["transcript"]
    rows = [(c, True) for c in transcript["completed_courses"]] + \
           [(c, False) for c in transcript["transfer_courses"]]
    return [{"id": i, "course": canon(c["course_id"]), "grade": c["grade"],
             "credits": c["credit_hours"], "institutional": institutional,
             "attributes": set(c.get("attributes", [])) | catalog_attributes(data, c["course_id"])}
            for i, (c, institutional) in enumerate(rows)]

def _counted(pool: list, repeatable: set) -> list:
    """The best attempt of each course, plus every attempt of a repeatable course."""
    best, every = {}, []
    for a in pool:
        if a["course"] in repeatable:
            every.append(a)
        elif a["course"] not in best or _rank(a["grade"]) < _rank(best[a["course"]]["grade"]):
            best[a["course"]] = a
    return sorted(every + list(best.values()), key=lambda a: a["id"])

def in_range(course: str, course_range: dict, canon) -> bool:
    """Is the course in a family like 'any 300+ level PHYS course'? (See course_range in the schema.)"""
    subject, discipline, number = course[:-5], course[-5:-3], course[-3:]
    if subject != course_range["subject"] or not number.isdigit():
        return False
    if course_range.get("discipline", discipline) != discipline:
        return False
    if not course_range.get("min_number", 0) <= int(number) <= course_range.get("max_number", 999):
        return False
    return course not in {canon(c) for c in course_range.get("exclude", [])}

def _matches(requirement: dict, course: str, attributes, canon) -> bool:
    """Does this course (with these attributes) fit the requirement's rule? Grades, credits and
    exclusivity are handled by the caller."""
    rule = requirement["rule"]
    if rule == "credits_with_attribute":
        return requirement["attribute"] in attributes
    if rule == "credits_any":
        return True
    if rule == "one_of_groups":
        return course in {canon(c) for group in requirement["course_groups"] for c in group}
    return (course in {canon(c) for c in requirement["course_options"]}
            or any(in_range(course, cr, canon) for cr in requirement.get("course_ranges", [])))

def waived(data: dict, requirement: dict) -> bool:
    """The guide's waivers: the requirement is met with no course when the student's entry
    matches any condition in waived_if."""
    entry, condition = data["student"].get("entry", {}), requirement.get("waived_if", {})
    if condition.get("nj_associate_degree") and entry.get("nj_associate_degree"):
        return True
    needed = condition.get("transfer_credits_at_entry")
    return needed is not None and entry.get("transfer_credits_at_entry", 0) >= needed

def audit(data: dict) -> dict:
    """Degree audit: which requirements and graduation rules the transcript satisfies.

    Requirements are read in order. One course may count toward several requirements,
    unless both are marked `exclusive`; then the earlier requirement takes it.
    """
    canon, attempts = _canonical(data), _attempts(data)
    taken_exclusively, major_used, requirements = set(), set(), {}
    for r in data["program_requirements"]:
        if waived(data, r):
            requirements[r["req_id"]] = {"rule": r["rule"], "satisfied": True, "waived": True}
            continue
        repeatable = {canon(c) for c in r.get("repeatable", [])}
        pool = _counted([a for a in attempts
                         if meets_minimum(a["grade"], r["minimum_grade"])
                         and _matches(r, a["course"], a["attributes"], canon)
                         and not (r.get("exclusive") and a["id"] in taken_exclusively)], repeatable)
        result = {"rule": r["rule"]}
        if r["rule"] == "all_of":
            have = {a["course"] for a in pool}
            result["missing"] = sorted({canon(c) for c in r["course_options"]} - have)
            result["satisfied"], used = not result["missing"], pool
        elif r["rule"] == "any_of":
            result["satisfied"], used = bool(pool), sorted(pool, key=lambda a: _rank(a["grade"]))[:1]
        elif r["rule"] == "one_of_groups":
            have = {a["course"] for a in pool}
            groups = [sorted({canon(c) for c in g}) for g in r["course_groups"]]
            closest = min(groups, key=lambda g: len(set(g) - have))      # first group on a tie
            result["missing"] = sorted(set(closest) - have)
            result["satisfied"] = not result["missing"]
            used = [a for a in pool if a["course"] in closest]
        else:
            caps = [({canon(c) for c in cap["course_options"]}, cap["max_credits"])
                    for cap in r.get("credit_caps", [])]
            cap_used, earned, used = [0] * len(caps), 0, []
            for a in pool:
                credits = a["credits"]
                for i, (courses, limit) in enumerate(caps):
                    if a["course"] in courses:
                        credits = min(credits, limit - cap_used[i])
                for i, (courses, _) in enumerate(caps):
                    if a["course"] in courses:
                        cap_used[i] += credits
                if credits > 0:
                    if earned < r["credit_hours"]:
                        used.append(a)        # an exclusive requirement takes only what it needs
                    earned += credits
            result.update(earned_credits=earned,
                          remaining_credits=max(0, r["credit_hours"] - earned),
                          satisfied=earned >= r["credit_hours"])
            if caps:
                result["cap_remaining"] = [max(0, limit - u) for (_, limit), u in zip(caps, cap_used)]
        if r.get("exclusive"):
            taken_exclusively |= {a["id"] for a in used}
        if r.get("guide_section") == "major":
            major_used |= {a["id"] for a in used}
        requirements[r["req_id"]] = result

    # Graduation-wide rules (spec open item 7). Quality points and GPA are derived here,
    # never stored. PROVISIONAL: a repeated course counts once, by its best attempt.
    repeatable = {canon(c) for r in data["program_requirements"] for c in r.get("repeatable", [])}
    earned = _counted([a for a in attempts if meets_minimum(a["grade"], "D-")], repeatable)
    graded = _counted([a for a in attempts if a["institutional"] and a["grade"] in GRADE_POINTS], repeatable)
    gpa_credits = sum(a["credits"] for a in graded)
    totals = {
        "total_credits": sum(a["credits"] for a in earned),
        "residency_credits": sum(a["credits"] for a in earned if a["institutional"]),
        "minimum_gpa": round(sum(GRADE_POINTS[a["grade"]] * a["credits"] for a in graded) / gpa_credits, 3)
                       if gpa_credits else None,
        "major_gpa": _major_gpa(data, attempts, repeatable),
    }
    rules = data.get("graduation_rules", {})
    graduation = {name: {"value": value, "required": rules.get(name),
                         "satisfied": None if name not in rules else
                                      value is not None and value >= rules[name]}
                  for name, value in totals.items()}
    return {"requirements": requirements, "graduation": graduation}

def _major_gpa(data: dict, attempts: list, repeatable: set):
    """GPA over every Rowan-graded attempt (best attempt per course, F included) at a course
    that a guide_section 'major' requirement names, the rule behind 'an overall GPA of 2.5 or
    better in all courses that satisfy major requirements'. None when there is none."""
    canon = _canonical(data)
    major = [r for r in data["program_requirements"]
             if r.get("guide_section") == "major" and r["rule"] != "credits_any"]
    graded = _counted([a for a in attempts if a["institutional"] and a["grade"] in GRADE_POINTS
                       and any(_matches(r, a["course"], a["attributes"], canon) for r in major)], repeatable)
    credits = sum(a["credits"] for a in graded)
    return round(sum(GRADE_POINTS[a["grade"]] * a["credits"] for a in graded) / credits, 3) if credits else None

def counts_toward(data: dict, course_id: str, attributes=()) -> list:
    """The req_ids of UNMET requirements that taking this course would move forward."""
    canon, course = _canonical(data), _canonical(data)(course_id)
    attributes = set(attributes) | catalog_attributes(data, course_id)
    status = audit(data)["requirements"]
    progressing = []
    for r in data["program_requirements"]:
        if status[r["req_id"]]["satisfied"] or not _matches(r, course, attributes, canon):
            continue
        capped = [remaining for cap, remaining in zip(r.get("credit_caps", []),
                                                      status[r["req_id"]].get("cap_remaining", []))
                  if course in {canon(c) for c in cap["course_options"]}]
        if capped and min(capped) <= 0:
            continue                      # its credits would not count: the cap is used up
        earned = any(a["course"] == course and meets_minimum(a["grade"], r["minimum_grade"])
                     for a in _attempts(data))
        if not earned or course in {canon(c) for c in r.get("repeatable", [])}:
            progressing.append(r["req_id"])
    return progressing

def missing_prerequisites(data: dict, course_id: str) -> list:
    """(required course, minimum grade) pairs the transcript does not satisfy (spec open
    item 4). PROVISIONAL: a prerequisite is a consistency rule, and a course still in
    progress does not satisfy one, because it has no grade yet."""
    canon, attempts = _canonical(data), _attempts(data)
    missing = []
    for p in data.get("prerequisites", []):
        if canon(p["course_id"]) != canon(course_id):
            continue
        for needed in p["requires"]:
            if not any(a["course"] == canon(needed) and meets_minimum(a["grade"], p["minimum_grade"])
                       for a in attempts):
                missing.append((needed, p["minimum_grade"]))
    return missing

def seed_db(data: dict) -> sqlite3.Connection:
    """The checker's database for one input object. section_id is the crn."""
    conn = make_db()
    conn.execute("INSERT INTO term VALUES (?)", (data["schedule"]["term_code"],))
    for s in data["schedule"]["sections"]:
        credits = s["credit_hours"]
        low, high = (credits["min"], credits["max"]) if isinstance(credits, dict) else (credits, credits)
        conn.execute("INSERT INTO section_credits VALUES (?, ?, ?)", (s["crn"], low, high))
        if s.get("waitlist"):
            conn.execute("INSERT INTO waitlists VALUES (?, ?, ?)",
                         (s["crn"], s["waitlist"]["capacity"], s["waitlist"]["enrolled"]))
        conn.execute("INSERT INTO sections VALUES (?, ?, ?, ?)",
                     (s["crn"], s["course_id"], s["seats_total"], s["seats_taken"]))
        conn.execute("INSERT INTO section_details VALUES (?, ?, ?, ?, ?)",
                     (s["crn"], s["section"], s["campus"], s["modality"], json.dumps(s["meetings"])))
        if s.get("cancelled"):
            conn.execute("INSERT INTO cancelled_sections VALUES (?)", (s["crn"],))
        for m in s["meetings"]:
            if "start_time" not in m:     # time to be arranged: nothing to conflict with
                continue
            conn.executemany(
                "INSERT INTO meetings VALUES (?, ?, ?, ?)",
                [(s["crn"], day, minutes(m["start_time"]), minutes(m["end_time"]))
                 for day in m["days"]])
    for course in {s["course_id"] for s in data["schedule"]["sections"]}:
        conn.executemany("INSERT INTO prereq_missing VALUES (?, ?, ?)",
                         [(course, needed, grade) for needed, grade in missing_prerequisites(data, course)])
    conn.commit()
    return conn
