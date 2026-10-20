"""
section_tally.py
-----------------
The "portion of the school's class registration" in the pipeline. Rowan's
Section Tally export has one CSV row per section; this reads that export as a
whole and turns its rows into input-format sections (README.md, "The input
format"), so a case carries real sections instead of invented ones.

The data is ONE file, TALLY_CSV (section_tally_fall2026.csv, next to this
script): the export exactly as downloaded, renamed.

HOW TO RUN:
    python section_tally.py check
        converts every section and validates the result against the input schema

FROM CODE (run_semantic_eval.py uses these when it loads a case):
    course_schedule(["CS04114", "CS06205"])   every section of these courses
                                              ("schedule_from_tally": {"named_courses": true})
    full_schedule()                           every section in the tally
                                              ("schedule_from_tally": {"whole": true})

Cancelled sections are left out unless a function says otherwise; where they are kept they
carry "cancelled": true, and registering for one is a consistency error.
"""

import argparse
import csv
import re
import sys
from functools import lru_cache
from pathlib import Path

TALLY_CSV = Path(__file__).with_name("section_tally_fall2026.csv")

DAYS = {"M": "Monday", "T": "Tuesday", "W": "Wednesday", "R": "Thursday",
        "F": "Friday", "S": "Saturday", "U": "Sunday"}
# The last two digits of a term code. Only Fall is confirmed (202640 runs 01-SEP to 17-DEC);
# add the others when a tally for that term shows its dates.
TERM_SEASONS = {"40": "Fall"}
SESSION_MODALITY = {"Online": "online", "Hybrid": "hybrid"}   # Day, Evening, Weekend -> in-person

# One meeting: day letters, then an optional time range, location and (type).
# A meeting can have days and a room but no time (time to be arranged).
_SEGMENT_START = re.compile(r"(?:^|(?<=\s))(?=[MTWRFSU]+\s{2,})")
_SEGMENT = re.compile(r"^(?P<days>[MTWRFSU]+)\s+"
                      r"(?:(?P<start>\d\d:\d\d [AP]M) - (?P<end>\d\d:\d\d [AP]M))?\s*"
                      r"(?P<location>.*?)\s*(?:\((?P<type>[^()]*)\))?$")


def parse_meetings(text: str):
    """'MW      09:30 AM - 10:45 AM Business Hall 101 (Class)' -> (input-format meetings, note).
    Blank or 'N/A' -> no meetings (asynchronous online, or arranged). Text with no days at
    all (one section lists only rooms) cannot be a meeting; it comes back as the note."""
    if text.strip() in ("", "N/A"):
        return [], None
    meetings, leftover = [], []
    for chunk in filter(None, (c.strip() for c in _SEGMENT_START.split(text))):
        m = _SEGMENT.match(chunk)
        if not m:
            leftover.append(" ".join(chunk.split()))
            continue
        meeting = {"days": [DAYS[d] for d in m["days"]]}
        if m["start"]:
            meeting["start_time"], meeting["end_time"] = m["start"], m["end"]
        if m["location"]:
            meeting["location"] = m["location"]
        if m["type"]:
            meeting["type"] = m["type"]
        meetings.append(meeting)
    return meetings, ("; ".join(leftover) or None)


def parse_credits(text: str):
    """'3.00' -> 3; '1.00 to 9.00' -> {'min': 1, 'max': 9} (a variable-credit section)."""
    def number(s):
        value = float(s)
        return int(value) if value.is_integer() else value
    low, _, high = text.partition(" to ")
    return {"min": number(low), "max": number(high)} if high else number(low)


def split_title(title: str):
    """'PRINCIPLES OF ACCTG I(RCB students only.)' -> ('PRINCIPLES OF ACCTG I', 'RCB students only.').
    The note in the closing parentheses can be a restriction, a topic or a former name."""
    if not title.endswith(")"):
        return title, None
    depth = 0
    for i in range(len(title) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(title[i], 0)
        if depth == 0:
            return (title[:i].rstrip(), title[i + 1:-1].strip()) if i else (title, None)
    return title, None


def term_name(code: str) -> str:
    """'202640' -> 'Fall 2026'."""
    season = TERM_SEASONS.get(code[4:])
    if not season:
        raise ValueError(f"unknown term code {code}: add its season to TERM_SEASONS")
    return f"{season} {code[:4]}"


def to_section(row: dict) -> dict:
    """One tally row -> one input-format section."""
    title, note = split_title(row["Title"])
    meetings, meeting_note = parse_meetings(row["Meeting Time"])
    section = {
        "crn": row["CRN"],
        "course_id": row["Subj"] + row["Course#"],
        "section": row["Sect"],
        "title": title,
        "credit_hours": parse_credits(row["Hours"]),
        "seats_total": int(row["Max"]),
        # Over-enrolled sections (666 of them) are capped at Max, so they stay full.
        "seats_taken": min(int(row["Enrolled"]), int(row["Max"])),
        "campus": row["Campus"],
        "modality": SESSION_MODALITY.get(row["Session"], "in-person"),
        "session": row["Session"],
        "meetings": meetings,
        "instructor": row["Instructor(s)"],
        "part_of_term": row["Part of Term"],
    }
    if note:
        section["title_note"] = note
    if meeting_note:
        section["meeting_note"] = meeting_note
    if int(row["Wait Cap"]) > 0:
        section["waitlist"] = {"capacity": int(row["Wait Cap"]), "enrolled": int(row["Wait Enrolled"])}
    if int(row["Max Reserved"]) > 0:
        section["reserved_seats"] = {"total": int(row["Max Reserved"]), "remaining": int(row["Left Reserved"])}
    room = row["Room Cap"]
    if room.rstrip("*").isdigit():
        section["room_capacity"] = int(room.rstrip("*"))
    if room.endswith("*"):            # 296 sections; what the tally's asterisk means is not documented
        section["room_capacity_marked"] = True
    if row["Cancelled"] == "true":
        section["cancelled"] = True
    return section


@lru_cache(maxsize=None)
def read_tally(path: str = str(TALLY_CSV)) -> tuple:
    """Every section row of the export, in export order. Read once per run (about 20 ms)."""
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"no Section Tally at {file}: save the export there "
                                f"(see section_tally.py)")
    with open(file, encoding="utf-8-sig", newline="") as f:
        return tuple(row for row in csv.DictReader(f) if row["CRN"])   # skip the export's blank row


def _schedule(rows: list) -> dict:
    """Converted rows as one input-format `schedule`: the term, its code and the sections."""
    codes = {row["Term Code"] for row in rows}
    if len(codes) > 1:
        raise ValueError(f"rows from more than one term: {sorted(codes)}")
    code = codes.pop() if codes else ""
    return {"term": term_name(code) if code else "", "term_code": code,
            "sections": [to_section(row) for row in rows]}


def course_schedule(courses, path: Path = TALLY_CSV) -> dict:
    """Every section of these courses, in export order, cancelled ones left out. A course
    with no section this term is skipped: a requirement may list courses not offered."""
    wanted = set(courses)
    rows = [row for row in read_tally(str(path))
            if row["Subj"] + row["Course#"] in wanted and row["Cancelled"] != "true"]
    if not rows:
        raise ValueError(f"none of {sorted(wanted)} has a section in the tally")
    return _schedule(rows)


def full_schedule(path: Path = TALLY_CSV) -> dict:
    """Every section in the export, cancelled ones included (flagged), as one `schedule`.
    This is the checker's view in a session, where the model reads the whole tally."""
    return _schedule(list(read_tally(str(path))))


def check(path: Path = TALLY_CSV) -> int:
    """Converts every section, including cancelled ones, and validates each subject's sections
    as an input object's schedule. Returns the number of subjects with problems (a missing
    or empty export counts as a failure)."""
    import registration as reg
    template = {"student": {"student_id": "S000", "degree": "x", "major": "x", "program_id": "x",
                            "catalog_year": "Fall 2026"},
                "transcript": {"completed_courses": [], "transfer_courses": [], "courses_in_progress": []},
                "program_requirements": [], "prompt": "x"}
    try:
        rows = read_tally(str(path))
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1
    subjects = sorted({row["Subj"] for row in rows})
    if not subjects:
        print(f"FAIL: {path} has no sections")
        return 1
    failed, sections = 0, 0
    for subject in subjects:
        try:
            schedule = _schedule([row for row in rows if row["Subj"] == subject])
            problems = reg.validate_input({**template, "schedule": schedule})
            reg.seed_db({**template, "schedule": schedule})
        except Exception as e:                    # report every subject, then fail
            problems = [str(e)]
        sections += len(schedule["sections"]) if not problems else 0
        if problems:
            failed += 1
            print(f"{subject}: FAIL")
            for p in problems[:5]:
                print(f"    - {p}")
    print(f"{path.name}: {len(subjects) - failed}/{len(subjects)} subjects valid, {sections} sections.")
    return failed


def main():
    """Command line: `check` converts and validates every section."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="convert and validate every section in the export")
    parser.parse_args()
    sys.exit(1 if check() else 0)


if __name__ == "__main__":
    main()
