"""
Section restrictions. A section's title_note in the Section Tally often says who may register
("Non-majors only.", "Psychology majors only", "RCBC 3+1 students only."). The model sees it, but
no check or answer key reads it, so a key can accept a section the student is barred from.

barred() reads the common patterns against a case's student. flag_cases() writes
ground_truth.restriction on every case where it matters, so the analysis can filter on it:
    status "no_allowed_answer"   every correct answer needs a section the student is barred from
           "unclear"             no correct answer is clearly allowed, and some notes could not be read
           "some_restricted"     an allowed correct answer exists, but some correct answers are barred
    barred / unclear             the CRNs behind it

    python -m pipeline.restrictions            # flag data/FINALIZED_TEST_CASES.json in place

shortcut: a pattern reader, not the registrar's rules (minors, class standing and "except"
clauses other than RCB's are read as unclear); upgrade to a Tier 3 check once the team agrees
on the patterns.
"""
import json
import re
import sys
from pathlib import Path

RESTRICTED = re.compile(r"\bonly\b|\breserved\b|\brestricted\b", re.I)
OUTSIDE = re.compile(r"3\+1|rcbc|rowan online|partnership|co-?op\b|cooperative|\bllc\b", re.I)   # programs no case student is in
FIRST_TIME = re.compile(r"first[- ]time", re.I)
BUSINESS = re.compile(r"accounting|finance|marketing|management|entrepreneur|business|information systems", re.I)
GROUP = re.compile(r"^(.*?)\b(?:majors?|students?)\b", re.I)
NOT_NAMES = {"majo", "mino", "stud", "only", "firs", "time", "unde", "grad", "open"}


def _stems(text: str) -> set:
    return {w[:4] for w in re.findall(r"[a-z]{4,}", text.lower())} - NOT_NAMES


def barred(note: str, course_id: str, data: dict, title: str = ""):
    """True when the section bars this case's student, False when it does not (or restricts
    nothing), None when its note cannot be read. data: a resolved case input. An HONORS section
    is for Honors College students, and no case's student is one."""
    if re.match(r"HONORS\b", title or "", re.I):
        return True
    if not note or not RESTRICTED.search(note):
        return False
    student, transcript = data["student"], data["transcript"]
    major = student.get("major", "")
    if OUTSIDE.search(note):
        return True
    if FIRST_TIME.search(note):     # a student with any record is not a first-time undergraduate
        if transcript.get("completed_courses") or transcript.get("transfer_courses") \
                or transcript.get("courses_in_progress"):
            return True
        return None
    text = note.strip()
    negated = bool(re.match(r"non[- ]", text, re.I))
    text = re.sub(r"^non[- ]", "", text, flags=re.I)
    if re.search(r"\bminors?\b", text, re.I) or re.search(r"\b(jr|sr|junior|senior)", text, re.I):
        return None
    if re.match(r"(pre-business\s+(and|&)\s+)?rcb\b", text, re.I):     # Rowan College of Business
        inside = bool(BUSINESS.search(major))
        if re.search(r"except accounting\s*&\s*finance", text, re.I) and re.search(r"accounting|finance", major, re.I):
            inside = False
    else:
        group = GROUP.match(text)
        if not group:
            return None
        named = _stems(group.group(1))
        if named:
            inside = bool(named & _stems(major))
        elif course_id.startswith("CHEM"):     # a bare "Majors only": the department's majors
            inside = "chemistry" in data.get("program_guide", {}).get("department", "").lower()
        else:
            return None
    return inside if negated else not inside


def restriction(case: dict):
    """ground_truth.restriction for a case (see the module doc), or None when no correct answer
    needs a restricted section."""
    from .generate_pairs import Case, by_course, correct_answers
    if case["ground_truth"]["rules"].get("count", {}).get("max", 1) == 0:
        return None
    c = Case(case)
    notes = {s["crn"]: (s.get("title_note") or "", s["course_id"], s.get("title") or "")
             for cid in case.get("candidate_courses", []) for s in by_course().get(cid, [])}
    _, accepted = correct_answers(case, c)
    if not accepted:
        return None
    seen, allowed, some_barred = {}, False, False
    for cart, *_ in accepted:
        for crn in cart:
            if crn not in seen:
                note, cid, title = notes.get(crn, ("", "", ""))
                seen[crn] = barred(note, cid, c.data, title)
        verdicts = [seen[crn] for crn in cart]
        allowed |= not any(v is None or v for v in verdicts)
        some_barred |= any(v is True for v in verdicts)
    status = ("some_restricted" if some_barred else None) if allowed else \
        ("no_allowed_answer" if all(any(seen[crn] is True for crn in cart) for cart, *_ in accepted)
         else "unclear")
    if not status:
        return None
    return {"status": status, "barred": sorted(k for k, v in seen.items() if v is True),
            "unclear": sorted(k for k, v in seen.items() if v is None)}


def self_check() -> None:
    data = {"student": {"major": "Biochemistry"}, "program_guide": {"department": "Department of Chemistry and Biochemistry"},
            "transcript": {"completed_courses": [{"course_id": "CHEM06100"}]}}
    assert barred("Non-majors only.", "CHEM05100", data) is True
    assert barred("Majors only. Must be taken with CHEM L6100 Section 1.", "CHEM06100", data) is False
    assert barred("Psychology majors only.", "PSY01107", data) is True
    assert barred("RCBC 3+1 students only.", "BIOL01100", data) is True
    assert barred("First-time undergraduate students only.", "ENGR01101", data) is True
    assert barred("Biochemistry majors only", "CHEM07200", data) is False
    assert barred("Art majors and minors only.", "ART01000", data) is None
    assert barred("Additional Materials Required.", "ART01000", data) is False
    assert barred("", "CS04113", data, "HONORS INTR OBJ-ORNT PRGRMMNG") is True
    assert barred("", "CS04113", data, "INTRO OBJECT ORIENTED PROG") is False
    biz = {**data, "student": {"major": "Accounting"}}
    assert barred("RCB Majors Only Except Accounting & Finance", "MGT06300", biz) is True
    assert barred("Non-RCB majors only.", "MGT06300", biz) is True


def flag_cases(path: str = "data/FINALIZED_TEST_CASES.json") -> None:
    file = Path(path)
    raw = file.read_text(encoding="utf-8")
    data = json.loads(raw)
    counts = {}
    for case in data["cases"]:
        flag = restriction(case)
        case["ground_truth"].pop("restriction", None)
        if flag:
            case["ground_truth"]["restriction"] = flag
            counts[flag["status"]] = counts.get(flag["status"], 0) + 1
    file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + ("\n" if raw.endswith("\n") else ""), encoding="utf-8")
    print(f"{path}: {counts or 'no restricted keys'}")


if __name__ == "__main__":
    self_check()
    flag_cases(*sys.argv[1:])
