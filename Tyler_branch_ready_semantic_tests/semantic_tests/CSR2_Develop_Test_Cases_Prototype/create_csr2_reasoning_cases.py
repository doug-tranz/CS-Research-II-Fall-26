"""Generate 12 BIRD-inspired, Daquan-v0.3-compatible registration reasoning cases.

Outputs are compatible with run_semantic_cases_aneesh.py on the Tyler branch.
Only each record's `input` is model-visible; ground_truth and review_status
are reserved for evaluation. The scenarios are synthetic research fixtures.
"""
from pathlib import Path
from copy import deepcopy
import csv
import json

OUT = Path(__file__).resolve().parent
CASES = []


def meeting(days, start, end):
    return {"days": days, "start_time": start, "end_time": end, "type": "Lecture"}


def sec(crn, course, title, *, number="01", credits=3, total=30, taken=5,
        campus="Glassboro", modality="in-person", when=None):
    return {"crn": str(crn), "course_id": course, "section": number,
            "title": title, "credit_hours": credits, "seats_total": total,
            "seats_taken": taken, "campus": campus, "modality": modality,
            "meetings": when or []}


def course(course_id, grade, credits=3, term="Spring 2026"):
    return {"term": term, "course_id": course_id, "grade": grade,
            "credit_hours": credits}


def transfer(course_id, grade, credits=3):
    return {"institution": "Example Community College", "course_id": course_id,
            "grade": grade, "credit_hours": credits}


def in_progress(course_id, credits=3, term="Fall 2026"):
    return {"term": term, "course_id": course_id, "credit_hours": credits}


def req(req_id, options, credits, *, rule="credits_from", min_grade="C-"):
    return {"req_id": req_id, "category": "Degree Requirements", "rule": rule,
            "course_options": options, "credit_hours": credits,
            "minimum_grade": min_grade}


def state(sections, *, completed=None, transferred=None, progress=None, requirements=None):
    return {"student": {"student_id": "S-TYLER-DEMO", "degree": "Bachelor of Science",
                        "major": "Computer Science", "program_id": "O701",
                        "catalog_year": "Fall 2026"},
            "transcript": {"completed_courses": completed or [],
                           "transfer_courses": transferred or [],
                           "courses_in_progress": progress or []},
            "program_requirements": requirements or [],
            "schedule": {"term": "Fall 2026", "sections": deepcopy(sections)},
            "prompt": ""}


def add(case_id, family, variation, dimension, base_state, prompt, acceptable,
        wrong, reason, *, extra=None):
    inp=deepcopy(base_state)
    inp["prompt"]=prompt
    case={"case_id": case_id, "scenario_id": family,
          "variation": variation, "semantic_dimension": dimension,
          "input": inp,
          "ground_truth": {"acceptable_crns": acceptable,
                           "wrong_but_consistent_crns": wrong,
                           "reason": reason},
          "review_status": "candidate_needs_human_review"}
    if extra:
        case["test_design"] = extra
    CASES.append(case)

# F11. Conditional fallback requires combining campus priority, live capacity,
# and a prohibition of otherwise-available online sections.
s=[sec("41001","CS05101","Software Design",number="01",total=20,taken=20,
       campus="Glassboro",when=[meeting(["Monday","Wednesday"],"9:00 AM","10:15 AM")]),
   sec("41002","CS05101","Software Design",number="02",campus="Camden",
       when=[meeting(["Tuesday","Thursday"],"11:00 AM","12:15 PM")]),
   sec("41003","CS05101","Software Design",number="03",modality="online")]
p="Enroll me in CS05101 in person. My first choice is Glassboro if it has a seat; if Glassboro is full, use Camden. Online is not acceptable."
add("RC-001","capacity_fallback","Glassboro full","conditional_capacity",state(s),p,["41002"],["41003"],
    "Glassboro is at capacity; Camden is the designated in-person fallback. Online remains open but prohibited.",
    extra={"consistency_negative_control_crns":["41001"]})
s2=deepcopy(s);s2[0]["seats_taken"]=19
add("RC-002","capacity_fallback","Glassboro has one seat","conditional_capacity",state(s2),p,["41001"],["41002","41003"],
    "The first-choice in-person Glassboro section is available; fallback choices violate the priority or online prohibition.")

# F12. Credit calculation spans transcript lists and membership of a credits_from requirement.
ss=[sec("42001","CS02300","Restricted Elective C",when=[meeting(["Monday","Wednesday"],"11:00 AM","12:15 PM")]),
    sec("42002","CS02500","Senior Topics",when=[meeting(["Tuesday","Thursday"],"11:00 AM","12:15 PM")])]
r=[req("elective_9credits",["CS02100","CS02200","CS02300","CS02400"],9)]
cp="First check the 9-credit restricted elective requirement: count only completed or transfer courses listed under its course_options with grades at least C-. If fewer than 9 qualifying credits are earned, enroll me in CS02300; if I already have at least 9, enroll me in CS02500 instead."
b=state(ss,completed=[course("CS02100","B")],transferred=[transfer("CS02200","A")],requirements=r)
add("RC-003","transfer_credit_sum","6 qualifying earned credits","cross_source_credit_arithmetic",b,cp,["42001"],["42002"],
    "3 completed plus 3 transfer = 6, below the 9-credit threshold; choose eligible CS02300.")
b2=state(ss,completed=[course("CS02100","B"),course("CS02400","C")],transferred=[transfer("CS02200","A")],requirements=r)
add("RC-004","transfer_credit_sum","9 qualifying earned credits","cross_source_credit_arithmetic",b2,cp,["42002"],["42001"],
    "3+3 completed plus 3 transfer = 9 qualifying credits; choose the alternative CS02500.")

# F13. Presence/absence of a current-term in-progress course changes a request.
ss=[sec("43001","CS06200","Algorithms",when=[meeting(["Monday","Wednesday"],"2:00 PM","3:15 PM")]),
    sec("43002","CS07200","Operating Systems",when=[meeting(["Tuesday","Thursday"],"2:00 PM","3:15 PM")])]
p="I need Algorithms (CS06200) unless my transcript already lists it as a course in progress this Fall. If I am already taking CS06200, enroll me in CS07200 instead so I do not request a duplicate."
add("RC-005","in_progress_dedup","Algorithms currently in progress","in_progress_transcript_logic",
    state(ss,progress=[in_progress("CS06200")]),p,["43002"],["43001"],
    "CS06200 is listed in courses_in_progress for Fall 2026, so select the alternate.")
add("RC-006","in_progress_dedup","Algorithms not in progress","in_progress_transcript_logic",
    state(ss,progress=[]),p,["43001"],["43002"],
    "CS06200 is absent from courses_in_progress, so select Algorithms.")

# F14. True interval overlap, rather than matching identical slot-code strings.
ss=[sec("44001","CS08300","Applied Security",number="01",when=[meeting(["Wednesday"],"10:00 AM","11:15 AM")]),
    sec("44002","CS08300","Applied Security",number="02",when=[meeting(["Wednesday"],"12:00 PM","1:15 PM")])]
p1="I have a non-negotiable lab on Wednesday from 10:30 AM to 11:30 AM. Enroll me in CS08300 only in a section whose meeting does not overlap any part of that lab."
p2="I have a non-negotiable lab on Wednesday from 12:30 PM to 1:30 PM. Enroll me in CS08300 only in a section whose meeting does not overlap any part of that lab."
add("RC-007","time_interval_overlap","lab overlaps early section","interval_overlap_reasoning",state(ss),p1,["44002"],["44001"],
    "10:30-11:30 overlaps the 10:00-11:15 section; noon section does not.")
add("RC-008","time_interval_overlap","lab overlaps noon section","interval_overlap_reasoning",state(ss),p2,["44001"],["44002"],
    "12:30-1:30 overlaps the 12:00-1:15 section; morning section does not.")

# F15. DNF Boolean rules; multiple legitimate gold outputs test set-valued scoring.
ss=[sec("45001","CS08400","Systems Seminar",number="01",modality="online",when=[meeting(["Monday"],"9:00 AM","10:15 AM")]),
    sec("45002","CS08400","Systems Seminar",number="02",modality="online",when=[meeting(["Monday"],"2:00 PM","3:15 PM")]),
    sec("45003","CS08400","Systems Seminar",number="03",modality="in-person",when=[meeting(["Tuesday"],"9:00 AM","10:15 AM")]),
    sec("45004","CS08400","Systems Seminar",number="04",modality="in-person",when=[meeting(["Tuesday"],"2:00 PM","3:15 PM")]),
    sec("45005","CS08400","Systems Seminar",number="05",modality="hybrid",when=[meeting(["Friday"],"6:00 PM","7:15 PM")])]
p1="Enroll me in CS08400, but the section must satisfy at least one COMPLETE option: (online AND afternoon) OR (in-person AND Tuesday morning). Choose either valid option; combinations outside these two are unacceptable."
p2="Enroll me in CS08400, but the section must satisfy at least one COMPLETE option: (online AND morning) OR (in-person AND Tuesday afternoon). Choose either valid option; combinations outside these two are unacceptable."
add("RC-009","boolean_alternatives","online afternoon OR Tuesday in-person morning","boolean_and_or",state(ss),p1,["45002","45003"],["45001","45004","45005"],
    "Both 45002 and 45003 satisfy an entire AND-clause; 45001, 45004, and 45005 satisfy neither.")
add("RC-010","boolean_alternatives","online morning OR Tuesday in-person afternoon","boolean_and_or",state(ss),p2,["45001","45004"],["45002","45003","45005"],
    "Both 45001 and 45004 satisfy an entire AND-clause; 45002, 45003, and 45005 satisfy neither.")

# F16. Total credits in progress plus single offered section; numerical equality.
ss=[sec("46001","CS09300","Web Applications",number="01",credits=3,when=[meeting(["Monday"],"3:00 PM","4:15 PM")]),
    sec("46002","CS09400","Advanced Project",number="01",credits=4,when=[meeting(["Tuesday"],"3:00 PM","4:15 PM")])]
current=[in_progress("CS01111",3),in_progress("CS02222",3),in_progress("CS03333",3)]
b=state(ss,progress=current)
p1="The courses_in_progress list is my complete current Fall 2026 registration. Add exactly one of the listed available CS sections so my total term credits equal exactly 12 (not 13)."
p2="The courses_in_progress list is my complete current Fall 2026 registration. Add exactly one of the listed available CS sections so my total term credits equal exactly 13 (not 12)."
add("RC-011","semester_credit_target","target 12 credits","course_load_arithmetic",b,p1,["46001"],["46002"],
    "Current load is 3+3+3=9; 9+3=12 exactly, not 9+4=13.")
add("RC-012","semester_credit_target","target 13 credits","course_load_arithmetic",b,p2,["46002"],["46001"],
    "Current load is 3+3+3=9; 9+4=13 exactly, not 9+3=12.")


def write_jsonl(path, cases):
    with path.open("w",encoding="utf-8") as f:
        for case in cases:
            f.write(json.dumps(case, ensure_ascii=False, separators=(",", ":"))+"\n")

write_jsonl(OUT/"csr2_reasoning_cases_12.jsonl", CASES)
demoids={"RC-001","RC-002","RC-003","RC-004","RC-007","RC-008"}
write_jsonl(OUT/"csr2_reasoning_demo_6.jsonl",[c for c in CASES if c["case_id"] in demoids])
with (OUT/"csr2_reasoning_case_index.csv").open("w",newline="",encoding="utf-8") as f:
    w=csv.writer(f)
    w.writerow(["case_id","family","variation","reasoning_dimension","acceptable_crns","wrong_but_consistent_crns","review_status"])
    for c in CASES:
        w.writerow([c["case_id"],c["scenario_id"],c["variation"],c["semantic_dimension"],
                    ";".join(c["ground_truth"]["acceptable_crns"]),";".join(c["ground_truth"]["wrong_but_consistent_crns"]),c["review_status"]])
print(f"Created {len(CASES)} scenarios and a {len(demoids)}-case demo.")
