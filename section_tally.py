"""
section_tally.py
-----------------
The "portion of the school's class registration" in the pipeline. Rowan's
Section Tally export has one CSV row per section; this reads that export as a
whole and turns the rows a test case asks for into input-format sections
(README.md, "The input format"), so a case can carry real sections instead of
invented ones.

The data is ONE file, TALLY_CSV (section_tally_fall2026.csv, next to this
script): the export exactly as downloaded, renamed.

HOW TO RUN:
    python section_tally.py check
        converts every section and validates the result against the input schema
    python section_tally.py show CS --crns 41836 41837
        prints those sections in the input format
    python section_tally.py split
        optional: writes section_tally/<SUBJ>.csv, one file per subject, for browsing.
        The pipeline never reads these.

FROM CODE:
    from section_tally import load_schedule, select_schedule
    schedule = load_schedule("CS", "MATH")                         # every CS and MATH section
    schedule = select_schedule(crns=["42090"], courses=["CS04114"])  # a chosen portion

IN A TEST CASE (input_format_cases.json), in place of writing input.schedule out:
    "schedule_from_tally": {"search": true}
        the model gets NO list of sections; it must look them up with the
        search_sections tool (SEARCH_TOOL), over the whole tally
    "schedule_from_tally": {"crns": ["42089", "42090"], "courses": ["CS04114"]}
        a hand-picked portion, listed in the prompt
run_semantic_eval.py fills in the schedule from TALLY_CSV when it loads the case.

Cancelled sections are left out unless include_cancelled=True; then they carry
"cancelled": true, and registering for one is a consistency error.
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

TALLY_CSV = Path(__file__).with_name("section_tally_fall2026.csv")
SPLIT_DIR = Path(__file__).with_name("section_tally")    # only for `split`; never read

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


def split(source: Path = TALLY_CSV, out_dir: Path = SPLIT_DIR) -> dict:
    """Optional, for browsing: writes one CSV per subject, with the export's columns.
    Returns {subject: rows}. The pipeline does not read these files."""
    by_subject = defaultdict(list)
    for row in read_tally(str(source)):
        by_subject[row["Subj"]].append(row)
    out_dir.mkdir(exist_ok=True)
    for subject, rows in by_subject.items():
        with open(out_dir / f"{subject}.csv", "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]), quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
    return {subject: len(rows) for subject, rows in sorted(by_subject.items())}


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


def load_schedule(*subjects, crns=None, include_cancelled=False, path: Path = TALLY_CSV) -> dict:
    """The input format's `schedule` for these subjects, optionally only the given crns."""
    wanted_subjects = set(subjects)
    rows = [row for row in read_tally(str(path)) if row["Subj"] in wanted_subjects]
    if not rows:
        raise ValueError(f"no sections for {', '.join(subjects)} in the tally")
    if crns is not None:
        wanted = set(crns)
        rows = [row for row in rows if row["CRN"] in wanted]
        missing = wanted - {row["CRN"] for row in rows}
        if missing:
            raise ValueError(f"crns not in {', '.join(subjects)}: {sorted(missing)}")
    if not include_cancelled:
        rows = [row for row in rows if row["Cancelled"] != "true"]
    return _schedule(rows)


def _schedule(rows: list) -> dict:
    codes = {row["Term Code"] for row in rows}
    if len(codes) > 1:
        raise ValueError(f"rows from more than one term: {sorted(codes)}")
    code = codes.pop() if codes else ""
    return {"term": term_name(code) if code else "", "term_code": code,
            "sections": [to_section(row) for row in rows]}


@lru_cache(maxsize=None)
def _crn_index(path: str) -> dict:
    """crn -> row, over the whole export."""
    return {row["CRN"]: row for row in read_tally(path)}


def select_schedule(crns=(), courses=(), include_cancelled=False, path: Path = TALLY_CSV) -> dict:
    """A chosen portion of the tally as a `schedule`: the sections named by crn, in the order
    given, then every section of each named course, in export order. A course's cancelled
    sections are left out unless include_cancelled; a crn named on purpose must not be
    cancelled unless include_cancelled."""
    index = _crn_index(str(path))
    missing = [crn for crn in crns if crn not in index]
    if missing:
        raise ValueError(f"crns not in the tally: {missing}")
    rows = [index[crn] for crn in crns]
    cancelled = [row["CRN"] for row in rows if row["Cancelled"] == "true"]
    if cancelled and not include_cancelled:
        raise ValueError(f"crns {cancelled} are cancelled; set include_cancelled to offer them")
    for course in courses:
        found = [row for row in read_tally(str(path)) if row["Subj"] + row["Course#"] == course]
        if not found:
            raise ValueError(f"course {course} is not in the tally")
        have = {row["CRN"] for row in rows}
        rows += [row for row in found if row["CRN"] not in have
                 and (include_cancelled or row["Cancelled"] != "true")]
    if not rows:
        raise ValueError("no sections selected")
    return _schedule(rows)


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
    This is the checker's view when the model searches the tally itself."""
    return _schedule(list(read_tally(str(path))))


# ── The search tool: how a model looks sections up itself ──
SEARCH_PAGE = 40
SEARCH_ARGS = ("course_id", "subject", "title_contains", "open_only", "page")
SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_sections",
        "description": ("Search the Section Tally: every section offered in the term being registered "
                        "for. Give at least one of course_id, subject or title_contains. Returns matching "
                        f"sections ({SEARCH_PAGE} per page) with their crn, seats, credits and meeting "
                        "times. Titles are abbreviated in the tally (e.g. 'DES-ANLYS ALGORITHMS'), so "
                        "searching by course_id or subject is more reliable than by title. Cancelled "
                        "sections are not listed."),
        "parameters": {
            "type": "object",
            "properties": {
                "course_id": {"type": "string", "description": "Exact course id, e.g. CS04114"},
                "subject": {"type": "string", "description": "Subject code, e.g. CS or MATH"},
                "title_contains": {"type": "string",
                                   "description": "Words that must all appear in the title, e.g. operating systems"},
                "open_only": {"type": "boolean", "description": "Only sections with a free seat. Default false."},
                "page": {"type": "integer", "minimum": 1, "description": "Which page of results. Default 1."},
            },
            "additionalProperties": False,
        },
    },
}


def search_sections(course_id=None, subject=None, title_contains=None, open_only=False, page=1,
                    path: Path = TALLY_CSV) -> dict:
    """The search behind SEARCH_TOOL. Raises ValueError for a search with no filter."""
    if not any((course_id, subject, title_contains)):
        raise ValueError("give at least one of course_id, subject or title_contains")
    rows = [row for row in read_tally(str(path)) if row["Cancelled"] != "true"]
    if course_id:
        wanted = str(course_id).replace(" ", "").upper()
        rows = [row for row in rows if row["Subj"] + row["Course#"] == wanted]
    if subject:
        rows = [row for row in rows if row["Subj"] == str(subject).strip().upper()]
    if title_contains:
        words = str(title_contains).upper().split()
        rows = [row for row in rows if all(word in row["Title"].upper() for word in words)]
    sections = [to_section(row) for row in rows]
    if open_only:
        sections = [s for s in sections if s["seats_taken"] < s["seats_total"]]
    page = max(1, int(page or 1))
    start = (page - 1) * SEARCH_PAGE
    shown = sections[start:start + SEARCH_PAGE]
    result = {"matches": len(sections), "page": page, "sections": shown}
    if start + SEARCH_PAGE < len(sections):
        result["note"] = (f"showing {start + 1}-{start + len(shown)} of {len(sections)}; "
                          f"ask for page {page + 1} for more, or narrow the search")
    return result


def run_search_tool(name: str, arguments: dict):
    """For harness.generate_with_tools(): (content for the model, summary for the log).
    A bad call comes back to the model as an error it can correct, not as a crash."""
    try:
        if name != "search_sections":
            raise ValueError(f"unknown tool {name!r}; the only tool is search_sections")
        unknown = sorted(set(arguments) - set(SEARCH_ARGS))
        if unknown:
            raise ValueError(f"unknown arguments {unknown}; allowed: {list(SEARCH_ARGS)}")
        result = search_sections(**arguments)
    except (ValueError, TypeError) as e:
        return json.dumps({"error": str(e)}), {"error": str(e)}
    return json.dumps(result), {"matches": result["matches"], "page": result["page"],
                                "returned_crns": [s["crn"] for s in result["sections"]]}


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
            schedule = load_schedule(subject, include_cancelled=True, path=path)
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
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="convert and validate every section in the export")
    p = sub.add_parser("show", help="print sections in the input format")
    p.add_argument("subjects", nargs="+")
    p.add_argument("--crns", nargs="+")
    p.add_argument("--include-cancelled", action="store_true")
    sub.add_parser("split", help=f"optional: write {SPLIT_DIR.name}/<SUBJ>.csv for browsing")
    args = parser.parse_args()

    if args.command == "split":
        counts = split()
        print(f"{len(counts)} subjects, {sum(counts.values())} sections -> {SPLIT_DIR.name}/ "
              f"(for browsing; the pipeline reads {TALLY_CSV.name})")
    elif args.command == "check":
        sys.exit(1 if check() else 0)
    else:
        print(json.dumps(load_schedule(*args.subjects, crns=args.crns,
                                       include_cancelled=args.include_cancelled), indent=2))


if __name__ == "__main__":
    main()
