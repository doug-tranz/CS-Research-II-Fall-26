"""
finalize.py
-----------
Builds data/FINALIZED_TEST_CASES.json, the case file every run uses: every semantic test case found
on the repository's branches, renamed by checklist code (T1..O3), each listed once with the codes it
covers and where it came from.

  - the team's cases (ben-test-branch, Aneesh-Anand, Tyler / main, Daquan-1), read from the
    origin/ branches with git;
  - this repository's generated pairs (data/paired_cases.json) and ben-o-completeness's;
  - K7 (impossible request), which no branch had, created here; Daquan's C1-C4 prompts, which had
    no answer key, given one computed from each case's own schedule;
  - every team case that carried its own made-up schedule (the pipeline runs every case against
    the whole Fall 2026 Section Tally) replaced by a case of a pair rebuilt for the same check on
    the real tally (data/rebuild_manifest.json lists those pairs).

Nothing in a case's `input` may tell the model what is being tested: trap labels are removed
(scrub()). Everything outside `input` is for people and the scorer; the model never sees it.

HOW TO RUN (from the repo root, after `git fetch --all`):
    python finalize_cases.py
"""
import copy
import json
import re
import subprocess
from datetime import date
from pathlib import Path

from .paths import CASES, DATA, PAIRS, ROOT

REPO = ROOT

CHECKS = [  # code, name, group
    ("T1", "Time-of-day words (morning / afternoon)", "Temporal reasoning"),
    ("T2", "Relative time (earlier / later / latest)", "Temporal reasoning"),
    ("T3", "Hard time cutoff (\"no 8 a.m.\", \"ends by 5 PM\")", "Temporal reasoning"),
    ("T4", "Partial overlap with a different start time", "Temporal reasoning"),
    ("T5", "Chained conflicts (one choice forces the next)", "Temporal reasoning"),
    ("T6", "Boundary values (ends exactly at the cutoff)", "Temporal reasoning"),
    ("T7", "Back-to-back sections (no real conflict)", "Temporal reasoning"),
    ("T8", "Sections with several meetings (lecture + lab)", "Temporal reasoning"),
    ("T9", "AM/PM and noon traps", "Temporal reasoning"),
    ("D1", "Day pattern (MW vs TR)", "Days"),
    ("D2", "Shared day, different pattern (MW vs MWF)", "Days"),
    ("D3", "Day exclusion (\"no Fridays\")", "Days"),
    ("C1", "Avoid full section", "Capacity"),
    ("C2", "Last seat (29/30 is still open)", "Capacity"),
    ("C3", "Compare open seats (total - taken)", "Capacity"),
    ("C4", "Right answer is not the first one listed", "Capacity"),
    ("I1", "Look-alike IDs (CS101 vs CS1010)", "Course and section identity"),
    ("I2", "Course vs section vs CRN mix-up", "Course and section identity"),
    ("I3", "Same title, different course ID", "Course and section identity"),
    ("I4", "Pick course A, not B", "Course and section identity"),
    ("I5", "Negation (\"not section X\")", "Course and section identity"),
    ("K1", "Modality (online / in-person / hybrid)", "Request constraints"),
    ("K2", "Instructor", "Request constraints"),
    ("K3", "Campus from residence (implicit)", "Request constraints"),
    ("K4", "Several constraints at once", "Request constraints"),
    ("K5", "Hard vs soft (\"must\" vs \"preferred\")", "Request constraints"),
    ("K6", "Quantity (\"one class\", \"two\", \"any\")", "Request constraints"),
    ("K7", "Impossible request -> no valid answer", "Request constraints"),
    ("K8", "Ambiguous request -> ask or guess", "Request constraints"),
    ("R1", "Do not retake a passed course", "Student record"),
    ("R2", "W vs passed vs failed (retake allowed)", "Student record"),
    ("R3", "Repeatable courses", "Student record"),
    ("R4", "Prerequisite met by a completed course", "Student record"),
    ("R5", "In-progress course does not meet a prereq", "Student record"),
    ("R6", "Transfer credit meets a prereq", "Student record"),
    ("R7", "Minimum grade on a prereq", "Student record"),
    ("R8", "Course must be in the requirement's options", "Student record"),
    ("R9", "Requirement rule types (credits_from / attribute / any_of / all_of)", "Student record"),
    ("R10", "Credit-load limit", "Student record"),
    ("R11", "Requirement already satisfied", "Student record"),
    ("R12", "Department filter (\"computer science classes\")", "Student record"),
    ("O1", "Includes every requested course", "Completeness of the answer"),
    ("O2", "Adds nothing unrequested", "Completeness of the answer"),
    ("O3", "Accepts any of several correct answers", "Completeness of the answer"),
]
ORDER = [c for c, _, _ in CHECKS]
CHECK_NAMES = {c: name for c, name, _ in CHECKS}

# The checklist's own mapping of the early cases (ben-test-branch), plus codes for the ones it leaves out.
LEGACY = {
    "reg-sem-001": ["T1"], "reg-sem-002": ["T1"], "reg-001": ["T1"], "reg-004": ["T1"],
    "reg-sem-005": ["T2"], "reg-sem-006": ["T2"], "reg-005": ["T2"], "cart-010": ["T2", "O1"],
    "cart-006": ["T3", "K4", "O1"], "sem_004": ["T3", "K4", "K6", "R1", "R3", "R4", "R5", "R6", "R8", "R9", "R11", "R12", "O3"],
    "con_002": ["T4", "D2"], "cart-005": ["T4", "O1"], "cart-009": ["T5", "O1"],
    "reg-sem-003": ["D1"], "reg-sem-004": ["D1"], "cart-002": ["D1", "O1"],
    "con_001": ["C1", "C4"], "cart-004": ["C1", "C4", "O1"], "cart-008": ["C3", "O1"], "cart-001": ["C4", "O1", "O2"],
    "cart-007": ["I1", "O1"], "reg-sem-007": ["I4"], "reg-sem-008": ["I4"], "reg-003": ["I4"], "reg-sem-013": ["I5"],
    "sem_002": ["K1"], "reg-sem-009": ["K1"], "reg-sem-010": ["K1"], "reg-sem-011": ["K2"], "reg-sem-012": ["K2"],
    "sem_001": ["K3"], "reg-sem-014": ["K4"], "reg-sem-015": ["K5"], "sem_003": ["K6", "R1", "R8", "R9", "O3"],
    "reg-002": ["K6", "C1"], "cart-003": ["O1", "O2"],
}
DIMENSIONS = {  # Tyler / dynamic semantic_dimension -> codes
    "campus_preference": ["K3"], "modality_preference": ["K1"], "modality": ["K1"], "meeting_days": ["D1"],
    "time_preference": ["T1"], "negation": ["I5"], "transcript_grade_requirement": ["R2"], "grade_threshold": ["R2"],
    "degree_progress": ["R8"], "requirement_priority": ["K5"], "compound_intent": ["K4"],
    "transcript_credit_status": ["R1"], "conditional_capacity": ["C1"], "cross_source_credit_arithmetic": ["R9"],
    "in_progress_transcript_logic": ["R1"], "interval_overlap_reasoning": ["T4"], "boolean_and_or": ["K4"],
    "course_load_arithmetic": ["R10"],
}
FAMILIES = {  # pipeline pair family (ground_truth.case_type) -> codes
    "time_of_day": ["T1"], "relative_time": ["T2"], "time_cutoff": ["T3"], "busy_block": ["T4"],
    "chained_conflict": ["T5", "O1"], "boundary_time": ["T6"], "back_to_back": ["T7"], "multi_meeting": ["T8"],
    "noon": ["T9"], "late_meeting": ["T8", "T3"], "days": ["D1"], "full_fallback": ["C1"], "modality": ["K1"],
    "in_progress": ["R1"], "withdrawal": ["R2"], "grade_threshold": ["R2"], "transfer": ["R9"],
    "nested_groups": ["R9"], "credit_limit": ["R10", "O1"], "o1_shared_rule": ["O1"], "o2_stacked_rules": ["O1", "K4"],
    "o3_busy_both": ["O1", "T4"],
    "shared_day_pattern": ["D2"], "day_exclusion": ["D3"], "last_seat": ["C2"], "open_seats": ["C3"],
    "not_first_listed": ["C4", "C1"], "lookalike_id": ["I1"], "section_number": ["I2"], "same_title": ["I3"],
    "course_a_not_b": ["I4"], "negation": ["I5"], "campus_residence": ["K3"], "several_constraints": ["K4"],
    "hard_soft": ["K5"], "quantity": ["K6", "O1"], "ambiguous": ["K8", "O3"], "passed_retake": ["R1"],
    "repeatable": ["R3"], "prereq_completed": ["R4"], "prereq_in_progress": ["R5"], "prereq_transfer": ["R6"],
    "prereq_min_grade": ["R7"], "requirement_options": ["R8"], "requirement_satisfied": ["R11"], "department": ["R12"],
    "complete_cart": ["O2", "O1"], "any_correct": ["O3"], "impossible_days": ["K7", "D3"], "impossible_full": ["K7", "C1"],
}
PER_CHECK = 8   # cases kept per check (4 pairs): T5 (chained_conflict) runs out of real sections at 4 pairs
NOT_REBUILT = {
    "K2": "the compact Section Tally every case shares leaves out instructors (adding them to every call, about "
          "+16% tokens, was declined), so a request by instructor cannot be answered on the tally",
}

# Prompt sentences that tell the model where the trap is (Daquan's capacity prompts).
HINTS = [r"\s*Do not recommend a full section\.", r"\s*Calculate seats_total minus seats_taken, not total capacity\.",
         r"\s*The first listed section may not be correct\."]
TRAP_WORDS = re.compile(r"trap|decoy|distractor|\blure\b", re.I)

# Who wrote each case file (from the git history; Tyler committed Daquan's and Aneesh's SEM cases).
OWNERS = {
    ("ben-test-branch", "semantic_test_cases.jsonl"): "Tyler Henry",
    ("ben-test-branch", "test_cases.jsonl"): "Ben P",
    ("ben-test-branch", "cart_test_cases.jsonl"): "Khalil",
    ("ben-test-branch", "input_format_cases.json"): "Khalil",
    ("Aneesh-Anand", "new_input_thing_paired_all_tests.jsonl"): "Aneesh Anand",
    ("Tyler", "Tyler_branch_ready_semantic_tests/semantic_tests/semantic_cases_daquan_aneesh.jsonl"): "Daquan and Aneesh Anand",
    ("Tyler", "Tyler_branch_ready_semantic_tests/semantic_tests/CSR2_Develop_Test_Cases_Prototype/csr2_reasoning_cases_12.jsonl"): "Tyler Henry",
    ("Tyler", "Tyler_branch_ready_semantic_tests/semantic_tests/d_category_cases_12.jsonl"): "Tyler Henry",
    ("main", "tyler_supplemental_tests/tyler_review_cases.json"): "Tyler Henry",
    ("ben-o-completeness", "paired_cases.json"): "Ben P",
    ("Khalil-context-path", "data/paired_cases.json"): "Khalil",
    ("Khalil-context-path", "data/FINALIZED_TEST_CASES.json"): "Khalil",
}


def owner(src):
    """Who wrote the case at `src` (a source block). Tyler's reviewed copies of Daquan's and Aneesh's
    cases credit them too."""
    if src["branch"] == "Daquan-1":
        return "Daquan"
    who = OWNERS[(src["branch"], src["file"])]
    if "semantic_cases_daquan_aneesh" in src.get("derived_from", ""):
        who = "Daquan and Aneesh Anand (reviewed by Tyler Henry)"
    return who


def credit_of(rec):
    """ground_truth.credit: who wrote the case and where it is from; a rebuilt case credits the team
    member whose case it rebuilds, with the generated pair it was rebuilt as."""
    src, orig = rec["source"], rec.get("rebuilt_from")
    first = orig or src
    return {"owner": owner(first), "branch": first["branch"], "file": first["file"],
            "original_case_id": first["original_id"],
            "rebuilt_by": "Khalil" if orig else None, "rebuilt_as": src["original_id"] if orig else None}


def courses_of(truth):
    """candidate_courses for a case written without them (ben-o-completeness): the courses its rules name."""
    rules = truth["rules"]
    return sorted(set(rules.get("must_include_courses", [])) | set(rules.get("courses_in", [])))


def scrub(rec):
    """`rec` without anything telling the model it is a trap: a 'SYNTH-TRAP' student id is
    renamed throughout the case (so its answer key still matches the input), and prompt
    sentences that point at the trap are removed."""
    rec = json.loads(json.dumps(rec, ensure_ascii=False).replace("SYNTH-TRAP-", "S7"))
    inp = rec.get("input")
    if inp and isinstance(inp.get("prompt"), str):
        for hint in HINTS:
            inp["prompt"] = re.sub(hint, "", inp["prompt"]).strip()
    return rec


def main():
    """Builds the file (see the module docstring)."""
    MANIFEST = json.loads((DATA / "rebuild_manifest.json").read_text(encoding="utf-8"))
    REBUILT = {p for ids in MANIFEST["added"].values() for p in ids}


    def show(branch, path):
        out = subprocess.run(["git", "-C", str(REPO), "show", f"{branch}:{path}"], capture_output=True)
        if out.returncode:
            raise SystemExit(f"missing {branch}:{path}")
        return out.stdout.decode("utf-8")


    def rows(branch, path):
        text = show(branch, path)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = [json.loads(line) for line in text.splitlines() if line.strip()]
        return data if isinstance(data, list) else [data]


    found = []      # (checks, record)


    def add(checks, record):
        """checks[0] is the case's main code (it is named after it); the rest are in checklist order."""
        found.append(([checks[0]] + sorted(set(checks[1:]) - {checks[0]}, key=ORDER.index), record))


    # 1. ben-test-branch: the early hand-built cases the checklist names
    for f, fmt in (("semantic_test_cases.jsonl", "legacy_task"), ("test_cases.jsonl", "legacy_task"),
                   ("cart_test_cases.jsonl", "legacy_task"), ("input_format_cases.json", "input_format")):
        for c in rows("origin/ben-test-branch", f):
            oid = c["case_id"] if fmt == "input_format" else c["task"]["task_id"]
            body = {k: v for k, v in c.items() if k != "case_id"}
            add(LEGACY[oid], {"format": fmt, "source": {"branch": "ben-test-branch", "file": f, "original_id": oid}, **body})

    # 2. Aneesh-Anand: control/attack trap pairs, one per checklist code
    for c in rows("origin/Aneesh-Anand", "new_input_thing_paired_all_tests.jsonl"):
        code = c["semantic_dimension"].split("_")[0]
        body = {k: v for k, v in c.items() if k != "case_id"}
        add([code], {"format": "input_format", "source": {"branch": "Aneesh-Anand", "file": "new_input_thing_paired_all_tests.jsonl",
                                                          "original_id": c["case_id"]}, **body})

    # 3. Tyler: reviewed copies on main first, then the raw cases with no reviewed copy
    TYLER = "Tyler_branch_ready_semantic_tests/semantic_tests/"
    reviewed = rows("origin/main", "tyler_supplemental_tests/tyler_review_cases.json")
    raw = {}
    for p in ("semantic_cases_daquan_aneesh.jsonl", "CSR2_Develop_Test_Cases_Prototype/csr2_reasoning_cases_12.jsonl",
              "d_category_cases_12.jsonl", "dynamic_demo_6.jsonl", "generated_seed2026.jsonl"):
        for c in rows("origin/Tyler", TYLER + p):
            raw[(TYLER + p, c["case_id"])] = c
    done = set()
    for c in reviewed:
        src = raw.get((c["source_file"], c["source_case_id"]))
        if src is None:     # a D-category case reviewed from a file not on the Tyler branch
            src = next((v for (f, i), v in raw.items() if i == c["source_case_id"]), {})
        dim = src.get("semantic_dimension", "")
        codes = [dim.split("_")[0]] if dim[:1] in "D" and dim[1:2].isdigit() else DIMENSIONS[dim]
        done.add((c["source_file"], c["source_case_id"]))
        body = {k: v for k, v in c.items() if k not in ("case_id", "source_case_id", "source_branch", "source_file")}
        add(codes, {"format": "rules_key", "source": {"branch": "main", "file": "tyler_supplemental_tests/tyler_review_cases.json",
                                                      "original_id": c["case_id"], "derived_from": f"Tyler:{c['source_file']}:{c['source_case_id']}"},
                    "semantic_dimension": dim, **body})
    for (p, cid), c in raw.items():
        if (p, cid) in done or "generated_seed2026" in p or "dynamic_demo" in p:
            continue        # the generated/dynamic drafts are only kept in their reviewed form
        dim = c["semantic_dimension"]
        codes = [dim.split("_")[0]] if dim[:1] == "D" and dim[1:2].isdigit() else DIMENSIONS[dim]
        body = {k: v for k, v in c.items() if k != "case_id"}
        add(codes, {"format": "input_format", "source": {"branch": "Tyler", "file": p, "original_id": cid}, **body})

    # 4. Daquan-1 (same files on main as TestCasesC): capacity prompts, answer key derived from the schedule
    def open_seats(s):
        return s["seats_total"] - s["seats_taken"]


    for code, n in [(f"C{k}", i) for k in range(1, 5) for i in range(1, 4)]:
        f = f"DaquanTestCases/{code}_{n:02d}.json"
        data = json.loads(show("origin/Daquan-1", f))
        secs = [s for s in data["schedule"]["sections"] if not s.get("cancelled")]
        words = data["prompt"].split()
        course = next((w.strip(".,?") for w in words if any(ch.isdigit() for ch in w) and w.strip(".,?")[:2].isalpha()), None)
        mine = [s for s in secs if s["course_id"] == course] or secs
        if code == "C2":
            ok = [s for s in mine if open_seats(s) == 1]
            why = "the section with exactly one open seat (seats_total - seats_taken = 1); it is still open"
        elif code == "C3":
            best = max(open_seats(s) for s in mine)
            ok = [s for s in mine if open_seats(s) == best]
            why = "the section with the most open seats (seats_total - seats_taken), not the largest capacity"
        else:
            ok = [s for s in mine if open_seats(s) > 0]
            why = ("a section of the exact course id with an open seat, wherever it is listed" if code == "C4"
                   else "a section with an open seat; full sections are wrong")
        assert ok, f
        add([code], {"format": "input_format", "source": {"branch": "Daquan-1", "file": f, "original_id": f"{code}_{n:02d}",
                                                          "also_on": "main:TestCasesC/"},
                     "input": data, "ground_truth": {"acceptable_crns": [s["crn"] for s in ok],
                                                     "wrong_but_consistent_crns": [s["crn"] for s in mine if s not in ok],
                                                     "reason": f"Correct: {why}.",
                                                     "derived": "answer key computed from the case's own schedule when "
                                                                "FINALIZED_TEST_CASES.json was built (the source has none)"}})

    # 5. Pipeline pairs on the real Fall 2026 tally: this branch's file, plus ben-o-completeness's added families
    pairs = json.loads(PAIRS.read_text(encoding="utf-8"))
    have = {c["case_id"] for c in pairs}
    ben = [c for c in rows("origin/ben-o-completeness", "paired_cases.json") if c["case_id"] not in have]
    def tally_record(c, branch="Khalil-context-path", f="data/paired_cases.json"):
        """A pipeline pair case as a FINALIZED record."""
        return {"format": "rules_key_tally", "source": {"branch": branch, "file": f, "original_id": c["case_id"]},
                **{k: v for k, v in c.items() if k != "case_id"}}


    for c, branch, f in [(c, "Khalil-context-path", "data/paired_cases.json") for c in pairs
                         if c["ground_truth"]["pair"]["id"] not in REBUILT] + \
                        [(c, "ben-o-completeness", "paired_cases.json") for c in ben]:
        add(FAMILIES[c["ground_truth"]["case_type"]], tally_record(c, branch, f))

    # 6. K7, created: Aneesh's K8 case as the template, two counterfactual pairs
    k8 = next(c for c in rows("origin/Aneesh-Anand", "new_input_thing_paired_all_tests.jsonl") if c["case_id"] == "TRAP-K8-CONTROL")


    def k7(variant, prompt, sections, acceptable, wrong, reason, changed, pair):
        inp = copy.deepcopy(k8["input"])
        inp["prompt"], inp["schedule"]["sections"] = prompt, sections
        return {"format": "input_format", "source": {"branch": "Khalil-context-path", "file": "data/FINALIZED_TEST_CASES.json",
                                                     "original_id": None, "created": "K7 had no case on any branch; built "
                                                     "on the input of Aneesh-Anand TRAP-K8-CONTROL"},
                "scenario_id": f"k7_{pair}", "variation": variant, "semantic_dimension": "K7_impossible_request",
                "input": inp,
                "ground_truth": {"acceptable_crns": acceptable, "wrong_but_consistent_crns": wrong,
                                 "expected_behavior": "register" if acceptable else "no_registration",
                                 "reason": reason},
                "review_status": "candidate_needs_human_review",
                "pair_metadata": {"pair_id": f"K7-{pair}", "condition": variant, "only_input_field_changed_between_pair": changed}}


    def sec(crn, days, start, end, taken=12, total=35):
        return {"crn": crn, "course_id": "CS79010", "section": crn[-1:].zfill(2), "title": "Operating Systems", "credit_hours": 3,
                "seats_total": total, "seats_taken": taken, "campus": "Glassboro", "modality": "in-person",
                "meetings": [{"days": days, "start_time": start, "end_time": end, "type": "Lecture"}]}


    fri = "Register me for CS79010. I can only come to campus on Fridays."
    add(["K7", "D3"], k7("control", fri, [sec("79011", ["Monday", "Wednesday"], "9:00 AM", "10:15 AM"),
                                          sec("79012", ["Tuesday", "Thursday"], "11:00 AM", "12:15 PM"),
                                          sec("79013", ["Friday"], "9:00 AM", "11:45 AM")],
                         ["79013"], ["79011", "79012"], "Section 79013 meets only on Friday, so it is the answer.",
                         "schedule.sections[crn=79013].meetings[0].days (Friday -> Monday)", "fridays"))
    add(["K7", "D3"], k7("attack", fri, [sec("79011", ["Monday", "Wednesday"], "9:00 AM", "10:15 AM"),
                                         sec("79012", ["Tuesday", "Thursday"], "11:00 AM", "12:15 PM"),
                                         sec("79013", ["Monday"], "9:00 AM", "11:45 AM")],
                        [], ["79011", "79012", "79013"],
                        "No section of CS79010 meets on Friday, so no registration satisfies the request. The right answer "
                        "registers nothing and says why; picking the closest section is wrong.",
                        "schedule.sections[crn=79013].meetings[0].days (Friday -> Monday)", "fridays"))
    seat = "Register me for CS79010. Don't put me on a waitlist."
    add(["K7", "C1"], k7("control", seat, [sec("79021", ["Monday", "Wednesday"], "9:00 AM", "10:15 AM", taken=35),
                                           sec("79022", ["Tuesday", "Thursday"], "11:00 AM", "12:15 PM", taken=34)],
                         ["79022"], ["79021"], "Section 79022 has one open seat (34/35); 79021 is full.",
                         "schedule.sections[crn=79022].seats_taken (34 -> 35)", "full"))
    add(["K7", "C1"], k7("attack", seat, [sec("79021", ["Monday", "Wednesday"], "9:00 AM", "10:15 AM", taken=35),
                                          sec("79022", ["Tuesday", "Thursday"], "11:00 AM", "12:15 PM", taken=35)],
                        [], ["79021", "79022"],
                        "Every section of CS79010 is full and the student refuses a waitlist, so no registration satisfies "
                        "the request. The right answer registers nothing and says why.",
                        "schedule.sections[crn=79022].seats_taken (34 -> 35)", "full"))

    # 7. Rebuild: a case the pipeline cannot run (it carries its own toy schedule) is replaced, in its place, by a
    #    case of a pair rebuilt on the real tally for the same check (rebuild_pairs.py). A rebuilt pair's two cases
    #    go to consecutive originals (a control/attack original pair lands on one rebuilt pair); rebuilt cases left
    #    over follow as extra twins. K2 and K7 cannot be rebuilt, and an original whose family ran out of real
    #    sections is kept; both say why.
    supply = {}
    by_pair = {}
    for c in pairs:
        if c["ground_truth"]["pair"]["id"] in REBUILT:
            by_pair.setdefault(c["ground_truth"]["pair"]["id"], []).append(c)
    for key, ids in MANIFEST["added"].items():
        for pid in ids:
            supply.setdefault(key.split("/")[0], []).extend(sorted(by_pair[pid], key=lambda c: c["ground_truth"]["pair"]["variant"]))
    rebuilt, dropped = [], []
    for checks, rec in found:
        code = checks[0]
        if rec["format"] == "rules_key_tally":
            rebuilt.append((checks, rec))
        elif not supply.get(code):      # nothing stands in for it on the tally: left out, and credited in the manifest
            dropped.append({"original_case_id": rec["source"]["original_id"], "owner": owner(rec["source"]),
                            "branch": rec["source"]["branch"], "file": rec["source"]["file"], "checks": checks,
                            "why": NOT_REBUILT.get(code, "its family found no more pairs in the real Fall 2026 sections")})
        else:
            new = supply[code].pop(0)
            rebuilt.append(([code] + sorted(set(FAMILIES[new["ground_truth"]["case_type"]]) - {code}, key=ORDER.index),
                            {**tally_record(new), "rebuilt_from": rec["source"]}))
    for code, rest in supply.items():
        for new in rest:                # the twin of a rebuilt case whose original had none
            rebuilt.append(([code] + sorted(set(FAMILIES[new["ground_truth"]["case_type"]]) - {code}, key=ORDER.index),
                            tally_record(new)))
    found = [(checks, scrub(rec)) for checks, rec in rebuilt]
    leaks = [rec["source"] for _, rec in found if TRAP_WORDS.search(json.dumps(rec["input"]))]
    assert not leaks, f"an input still names a trap: {leaks[:3]}"

    # The same number of cases for every check: PER_CHECK // 2 whole pairs per main code. Team cases
    # (found on a branch or rebuilt from one) first, then the rest, taking turns across owners and families.
    groups = {}
    for checks, rec in found:
        groups.setdefault(checks[0], {}).setdefault(rec["ground_truth"]["pair"]["id"], []).append((checks, rec))
    kept, turn = [], {}
    for code, by_pair in groups.items():
        def rank(item):
            checks, rec = item[1][0]
            team = bool(rec.get("rebuilt_from")) or rec["source"]["branch"] != "Khalil-context-path"
            who = (credit_of(rec)["owner"], rec["ground_truth"]["case_type"])
            turn[who] = turn.get(who, -1) + 1
            return (not team, turn[who])
        ranked = sorted(by_pair.items(), key=rank)
        assert len(ranked) * 2 >= PER_CHECK or code in NOT_REBUILT, f"{code} has only {len(ranked)} pairs"
        for _, twins in ranked[:PER_CHECK // 2]:
            assert len(twins) == 2, twins
            kept += twins
    found = kept

    # Every case in data/paired_cases.json's format, named by its first checklist code (in checklist order);
    # its code and who wrote it go in ground_truth, which the model never sees.
    found.sort(key=lambda x: ORDER.index(x[0][0]))
    count, cases = {}, []
    for checks, rec in found:
        count[checks[0]] = count.get(checks[0], 0) + 1
        truth = {k: v for k, v in rec["ground_truth"].items() if k not in ("checks", "credit")}
        truth["pair"] = {**truth["pair"], "shared_answer_example": truth["pair"].get("shared_answer_example")}
        cases.append({"case_id": f"{checks[0]}-{count[checks[0]]:03d}",
                      "semantic_check": f"{checks[0]}: {CHECK_NAMES[checks[0]]}",      # for people; not sent to the model
                      "input": rec["input"],
                      "candidate_courses": rec.get("candidate_courses") or courses_of(truth),
                      "ground_truth": {**truth, "checks": checks[:1], "credit": credit_of(rec)}})     # its own code only
    shapes = {(tuple(c), tuple(c["input"]), tuple(c["ground_truth"]), tuple(c["ground_truth"]["pair"]),
               tuple(c["ground_truth"]["credit"])) for c in cases}
    assert len(shapes) == 1, f"cases differ in shape: {shapes}"
    missing = [code for code in ORDER if not any(code in c["ground_truth"]["checks"] for c in cases)]

    checklist = []
    for code, name, group in CHECKS:
        ids = [c["case_id"] for c in cases if code in c["ground_truth"]["checks"]]
        gone = [d for d in dropped if d["checks"][0] == code]
        checklist.append({"code": code, "name": name, "group": group, "cases": len(ids),
                          "status": "created" if code == "K7" else "found" if ids else "dropped",
                          "case_ids": ids, **({"dropped": gone} if gone else {})})
    about = {
        "what": "The semantic test cases found on the CS-Research-II-Fall-26 branches, plus generated pairs, renamed by checklist code "
                "(<code>-<nnn>, numbered by the case's first code) and listed once, in data/paired_cases.json's format "
                "(case_id, input, candidate_courses, ground_truth), all run on the whole Fall 2026 Section Tally. "
                "`semantic_check` (after case_id) names the check a case tests; ground_truth.checks holds that one code "
                "and ground_truth.credit who wrote it. The model reads only `input`.",
        "built": date.today().isoformat(),
        "per_check": f"{PER_CHECK} cases ({PER_CHECK // 2} counterfactual pairs) for every check named by its code, K2 "
                     "excepted (none); team cases kept first, the rest left out. T5 sets the number: the real "
                     "tally has only 4 chained-conflict pairs.",
        "sources": ["ben-test-branch: semantic_test_cases.jsonl (Tyler Henry), test_cases.jsonl (Ben P), "
                    "cart_test_cases.jsonl and input_format_cases.json (Khalil)",
                    "Aneesh-Anand: new_input_thing_paired_all_tests.jsonl (Aneesh Anand)",
                    "Tyler: semantic_cases_daquan_aneesh.jsonl (Daquan and Aneesh Anand), csr2_reasoning_cases_12.jsonl and "
                    "d_category_cases_12.jsonl (Tyler Henry); main: tyler_supplemental_tests/tyler_review_cases.json "
                    "(Tyler Henry's reviewed copies)",
                    "Daquan-1: DaquanTestCases/C1_01..C4_03 (Daquan; the same files are on main as TestCasesC/)",
                    "ben-o-completeness: paired_cases.json, its late_meeting and O-completeness families (Ben P)",
                    "Khalil-context-path: data/paired_cases.json, the generated counterfactual pairs (Khalil)"],
        "left_out": ["superseded drafts: Aneesh-Anand semantic_trap_pairs*.jsonl, semantic_trap_t8_reverse.jsonl and "
                     "Semantic_trap_pairs_v3 (replaced by new_input_thing_paired_all_tests.jsonl)",
                     "Aneesh-Anand practice stuff/ (practice and generated variants)",
                     "Tyler generated/dynamic drafts (generated_seed2026, dynamic_demo_6) except the 7 reviewed on main",
                     "duplicate copies: main, ollama-dev-backend and ben-late-meeting paired_cases.json (same cases as "
                     "data/paired_cases.json and ben-o-completeness), khalil and ben-test-branch old_schema/ copies",
                     "model-run outputs (ben-p_test/, *_llama31_8b.jsonl, results files): answers, not test cases",
                     "K2 (instructor): the compact tally leaves instructors out (adding them, about +16% tokens per call, "
                     "was declined); its 4 cases are credited under checklist K2 `dropped`"],
        "created": "K7 (impossible request) had no case on any branch: created as counterfactual pairs whose right "
                   "answer registers nothing (an empty sections list): impossible_days and impossible_full. Daquan's "
                   "C1-C4 prompts had no answer key; their checks were rebuilt with keys.",
        "rebuilt": "Every team case that came with its own made-up schedule (so could not run on the shared Fall 2026 "
                   "Section Tally) was replaced by a case of a counterfactual pair rebuilt for the same check on real "
                   "sections, programs and fabricated transcripts. It keeps its owner's credit, with rebuilt_by and "
                   "rebuilt_as; it tests the same check, not the original's exact scenario.",
        "traps": "No input says a case is a trap: SYNTH-TRAP student ids are renamed and prompt sentences pointing at "
                 "the trap are removed; the build refuses to write the file if an input still mentions one.",
        "rebuild": "git fetch --all; python finalize_cases.py (after python generate_pairs.py adds pairs)",
    }

    text = json.dumps({"about": about, "checklist": checklist, "cases": cases}, indent=2, ensure_ascii=False) + "\n"
    CASES.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))        # CRLF, like data/paired_cases.json
    MANIFEST["dropped"] = dropped
    (DATA / "rebuild_manifest.json").write_text(json.dumps(MANIFEST, indent=1) + "\n", encoding="utf-8")
    print(f"{len(cases)} cases -> {CASES}")
    for code, name, _ in CHECKS:
        print(f"  {code:4} {sum(code in c['ground_truth']['checks'] for c in cases):4}  {name}")
    print("checks with no case:", missing or "none")
    print("left out (credited in data/rebuild_manifest.json):", [(d["original_case_id"], d["owner"]) for d in dropped])

if __name__ == "__main__":
    main()
