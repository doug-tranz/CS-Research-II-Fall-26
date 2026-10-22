"""
checker.py
-----------
Tiers 1-3 for one model answer against one registration situation.

  Tiers 1-2  syntactic + schema   didcomm_schema.validate_message()
  Tier 3     consistency          consistency_checks(), against a checker database

seed_db() builds the checker database (in-memory SQLite) from an input object, so the model
and the checker see the same situation. evaluate() runs Tiers 1-3 and returns
CONSISTENT when all three pass; scoring.py adds Tier 4.
"""
import json
import sqlite3

from audit import canonical, missing_prerequisites
from didcomm_schema import validate_message   # Tiers 1-2: registration_message.schema.json
from input_format import minutes

# What evaluate() returns when Tiers 1-3 pass; scoring.py replaces it with the Tier 4 verdict.
CONSISTENT = "consistent__semantic_TBD"

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
    "requires", "found", "passed", "reason"}; "reason" is the failure text evaluate()
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

def evaluate(conn, raw_output: str) -> dict:
    # Tier 1: syntactic — is it a DIDComm plaintext message?
    # Tier 2: schema — is it OUR register message, with a well-formed cart?
    """Tiers 1-3 for one raw model output against this database. A message that passes all
    three comes back as CONSISTENT; scoring.py adds Tier 4."""
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
    # Tier 4: semantic — not here; scoring.py adds it on top
    return {"outcome": CONSISTENT,
            "detail": "invariants hold", **shown, "tier3_checks": tier3}


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
    # Only a course with a prerequisites entry (under its own id or an equivalent) can miss one.
    canon = canonical(data)
    gated = {canon(p["course_id"]) for p in data.get("prerequisites", [])}
    for course in {s["course_id"] for s in data["schedule"]["sections"]}:
        if canon(course) not in gated:
            continue
        conn.executemany("INSERT INTO prereq_missing VALUES (?, ?, ?)",
                         [(course, needed, grade) for needed, grade in missing_prerequisites(data, course)])
    conn.commit()
    return conn
