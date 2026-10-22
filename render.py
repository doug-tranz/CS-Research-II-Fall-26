"""
render.py
----------
What the model sees. to_task() turns a case into a harness.TaskSpec: the input object (never
the answer key) in one of three renderings (json, csv, nl), the intent, and the output
contract. In a session the sections are the shared opening instead: the Section Tally in one
of four versions (TALLY_VARIANTS), built by render_tally() / render_raw().
"""
import csv
import io
import json

import section_tally
from cases import is_whole
from didcomm_schema import CRN_CONTRACT, OUTPUT_CONTRACT
from harness import TaskSpec

# The model's fixed reply to a session's opening turn, so a session call reads
# opening -> acknowledgement -> request. Byte-identical every call, like the opening.
SESSION_ACK = "I have read the Section Tally. Send the registration request."


RENDERINGS = ("json", "csv", "nl")


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


def to_task(case: dict, rendering: str = "json", session=None) -> TaskSpec:
    """What the model sees: the whole input object, never the answer key.
    `prompt` becomes the intent; the rest is the known state. In a session the sections are
    not in the case's own text: they are the session's opening turn (session: a
    session.TallySession, required for a whole-tally case)."""
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
        shared_context_ack=SESSION_ACK if is_whole(case) else "",
    )
