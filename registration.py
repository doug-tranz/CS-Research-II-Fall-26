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
    seats_taken INTEGER NOT NULL,
    meets       TEXT NOT NULL          -- provisional slot code, e.g. "MW14", "TR13"; '' if it uses `meetings`
);
CREATE TABLE meetings (                -- structured times (input format); one row per day a section meets
    section_id TEXT NOT NULL,
    day        TEXT NOT NULL,          -- "Monday" ... "Sunday"
    start_min  INTEGER NOT NULL,       -- minutes after midnight
    end_min    INTEGER NOT NULL,
    FOREIGN KEY (section_id) REFERENCES sections(section_id)
);
CREATE TABLE enrollments (
    student_id TEXT NOT NULL,
    section_id TEXT NOT NULL,
    PRIMARY KEY (student_id, section_id),
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

def make_db(seed_sql: str = "") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    if seed_sql:
        conn.executescript(seed_sql)
    conn.commit()
    return conn

def _meetings(conn, section: str) -> list:
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

def check_consistency(conn, body: dict):
    """Returns (ok, reason). Assumes body already passed schema validation."""
    student = body["student_id"]
    cart_course, cart_slot = {}, {}       # course / slot -> the cart section that holds it
    cart_times = {}                       # cart section -> its structured meetings
    enrolled_times = {row[0]: _meetings(conn, row[0]) for row in conn.execute(
        "SELECT section_id FROM enrollments WHERE student_id=?", (student,))}
    term = conn.execute("SELECT term_code FROM term").fetchone()
    if term and body["term_code"] != term[0]:
        return False, f"term_code {body['term_code']} is not the term being registered for ({term[0]})"
    for item in body["sections"]:
        section = item["crn"]
        row = conn.execute(
            "SELECT course_id, seats_total, seats_taken, meets FROM sections WHERE section_id=?",
            (section,)).fetchone()
        if row is None:
            return False, f"section {section} does not exist"
        course, seats_total, seats_taken, meets = row
        wrong = _copy_differs(conn, item, course, seats_total, seats_taken)
        if wrong:
            return False, f"section {section}: {wrong}"
        if conn.execute("SELECT 1 FROM enrollments WHERE student_id=? AND section_id=?",
                        (student, section)).fetchone():
            return False, f"already enrolled in {section}"
        if conn.execute("SELECT 1 FROM cancelled_sections WHERE section_id=?", (section,)).fetchone():
            return False, f"section {section} is cancelled"
        if item.get("waitlist"):
            waitlist = conn.execute("SELECT capacity, enrolled FROM waitlists WHERE section_id=?",
                                    (section,)).fetchone()
            if seats_taken < seats_total:
                return False, (f"section {section} has open seats ({seats_taken}/{seats_total}), "
                               f"so it cannot be waitlisted")
            if waitlist is None:
                return False, f"section {section} is full and has no waitlist"
            if waitlist[1] >= waitlist[0]:
                return False, f"the waitlist for section {section} is full ({waitlist[1]}/{waitlist[0]})"
        elif seats_taken >= seats_total:
            return False, f"section {section} is full ({seats_taken}/{seats_total})"
        allowed = conn.execute("SELECT min_credits, max_credits FROM section_credits WHERE section_id=?",
                               (section,)).fetchone()
        chosen = item.get("credit_hours")
        if allowed:
            low, high = allowed
            if low != high and chosen is None:
                return False, (f"section {section} is variable-credit ({low:g}-{high:g}); "
                               f"the message must give credit_hours")
            if chosen is not None and not low <= chosen <= high:
                allowed_text = f"{low:g}" if low == high else f"{low:g}-{high:g}"
                return False, f"section {section}: credit_hours {chosen:g} is not {allowed_text}"
        unmet = conn.execute(
            "SELECT requires, minimum_grade FROM prereq_missing WHERE course_id=?", (course,)).fetchone()
        if unmet:
            return False, (f"section {section}: {course} needs prerequisite {unmet[0]} "
                           f"({unmet[1]} or better), which the transcript does not show")
        # Time conflicts are found two ways: equal slot codes (sections seeded with `meets`),
        # or overlapping time ranges on a shared day (sections seeded with `meetings`).
        times = _meetings(conn, section)
        conflict = conn.execute("""
            SELECT e.section_id FROM enrollments e
            JOIN sections s ON s.section_id = e.section_id
            WHERE e.student_id=? AND s.meets=? AND s.meets != ''
        """, (student, meets)).fetchone()
        if conflict:
            return False, f"time conflict with {conflict[0]} at {meets}"
        for other, other_times in enrolled_times.items():
            day = _overlap(times, other_times)
            if day:
                return False, f"time conflict with {other} on {day}"
        # Invariants that only exist for a cart: its sections against each other.
        if course in cart_course:
            return False, f"two sections of {course} in the cart: {cart_course[course]} and {section}"
        if meets and meets in cart_slot:
            return False, f"time conflict inside the cart: {cart_slot[meets]} and {section} at {meets}"
        for other, other_times in cart_times.items():
            day = _overlap(times, other_times)
            if day:
                return False, f"time conflict inside the cart: {other} and {section} on {day}"
        cart_course[course], cart_times[section] = section, times
        if meets:
            cart_slot[meets] = section
    return True, "consistent"

def evaluate(conn, raw_output: str) -> dict:
    # Tier 1: syntactic — is it a DIDComm plaintext message?
    # Tier 2: schema — is it OUR register message, with a well-formed cart?
    checked = validate_message(raw_output)
    if checked["outcome"] != "schema_ok":
        return {"outcome": checked["outcome"], "detail": checked["detail"]}
    # Tier 3: consistency — invariants
    ok, reason = check_consistency(conn, checked["body"])
    if not ok:
        return {"outcome": "consistency_error", "detail": reason}
    # Tier 4: semantic — STUB here; run_semantic_eval.py adds it on top
    return {"outcome": "consistent__semantic_TBD",
            "detail": "invariants hold"}


# ── Input format: one JSON object per registration situation ──
INPUT_SCHEMA = json.loads(
    Path(__file__).with_name("registration_input.schema.json").read_text(encoding="utf-8"))
Draft202012Validator.check_schema(INPUT_SCHEMA)
_INPUT = Draft202012Validator(INPUT_SCHEMA)

GRADE_ORDER = ["A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-"]   # highest to lowest
GRADE_POINTS = dict(zip(GRADE_ORDER, [4.0, 3.7, 3.3, 3.0, 2.7, 2.3, 2.0, 1.7, 1.3, 1.0, 0.7]), F=0.0)
CREDIT_RULES = ("credits_from", "credits_with_attribute")

def minutes(clock: str) -> int:
    """'1:05 PM' -> 785 (minutes after midnight)."""
    time, half = clock.split()
    hour, minute = map(int, time.split(":"))
    return (hour % 12 + (12 if half == "PM" else 0)) * 60 + minute

def _duplicates(values) -> list:
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

def _attempts(data: dict) -> list:
    """Every graded attempt on the transcript. In-progress courses have no grade, so none."""
    canon, transcript = _canonical(data), data["transcript"]
    rows = [(c, True) for c in transcript["completed_courses"]] + \
           [(c, False) for c in transcript["transfer_courses"]]
    return [{"id": i, "course": canon(c["course_id"]), "grade": c["grade"],
             "credits": c["credit_hours"], "institutional": institutional,
             "attributes": set(c.get("attributes", []))}
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

def _matches(requirement: dict, course: str, attributes, canon) -> bool:
    if requirement["rule"] == "credits_with_attribute":
        return requirement["attribute"] in attributes
    return course in {canon(c) for c in requirement["course_options"]}

def audit(data: dict) -> dict:
    """Degree audit: which requirements and graduation rules the transcript satisfies.

    Requirements are read in order. One course may count toward several requirements,
    unless both are marked `exclusive`; then the earlier requirement takes it.
    """
    canon, attempts = _canonical(data), _attempts(data)
    taken_exclusively, requirements = set(), {}
    for r in data["program_requirements"]:
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
        if r.get("exclusive"):
            taken_exclusively |= {a["id"] for a in used}
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
    }
    rules = data.get("graduation_rules", {})
    graduation = {name: {"value": value, "required": rules.get(name),
                         "satisfied": None if name not in rules else
                                      value is not None and value >= rules[name]}
                  for name, value in totals.items()}
    return {"requirements": requirements, "graduation": graduation}

def counts_toward(data: dict, course_id: str, attributes=()) -> list:
    """The req_ids of UNMET requirements that taking this course would move forward."""
    canon, course = _canonical(data), _canonical(data)(course_id)
    status = audit(data)["requirements"]
    progressing = []
    for r in data["program_requirements"]:
        if status[r["req_id"]]["satisfied"] or not _matches(r, course, set(attributes), canon):
            continue
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
        conn.execute("INSERT INTO sections VALUES (?, ?, ?, ?, '')",
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
