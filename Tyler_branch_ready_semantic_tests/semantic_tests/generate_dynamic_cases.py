"""Generate repeatable, schema-valid semantic registration test cases.

Compatible with the team's registration_input.schema.json (v0.3) and
run_semantic_cases_aneesh.py.  No API keys or LLM calls are required.

Examples (from your existing semantic_tests directory):
    python generate_dynamic_cases.py --seed 42 --pairs-per-family 1 --schema registration_input.schema.json
    python generate_dynamic_cases.py --seed 43 --families campus,grade_threshold,compound_intent --output dynamic_demo_6.jsonl --schema registration_input.schema.json
    python run_semantic_cases_aneesh.py --dry-run --test-file dynamic_demo_6.jsonl --schema registration_input.schema.json

Generated ground_truth is kept separate from the input given to the LLM.
This is synthetic benchmark data requiring human review before publication.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import random

FAMILIES = (
    "campus", "modality", "meeting_days", "time_preference", "negation",
    "grade_threshold", "requirement_priority", "compound_intent",
    "earned_credit_arithmetic", "in_progress",
)
TERM = "Fall 2026"
GRADED_TERM = "Spring 2026"
TOPICS = ("Algorithms", "Software Engineering", "Database Systems", "Computer Networks",
          "Data Science", "Security Engineering", "Mobile Computing", "Information Systems",
          "AI Applications", "Advanced Programming", "Cloud Computing", "Software Testing")


class Builder:
    def __init__(self, seed: int):
        self.seed = seed
        self.rng = random.Random(seed)
        self.ids = set()
        self.crns = set()
        self.cases: list[dict] = []
        self.scenario_number = 0

    def course(self) -> str:
        while True:
            cid = f"CS{self.rng.randrange(10000, 100000):05d}"
            if cid not in self.ids:
                self.ids.add(cid)
                return cid

    def crn(self) -> str:
        while True:
            value = str(self.rng.randrange(60000, 100000))
            if value not in self.crns:
                self.crns.add(value)
                return value

    def sections(self, specs: list[dict]) -> list[dict]:
        result = []
        for n, cfg in enumerate(specs, 1):
            days = cfg.get("days", ["Monday", "Wednesday"])
            meeting = ([{"days": days, "start_time": cfg.get("start", "10:00 AM"),
                         "end_time": cfg.get("end", "11:15 AM"), "type": "Lecture"}]
                       if days else [])
            seats_total = self.rng.choice([24, 30, 35])
            result.append({
                "crn": self.crn(),
                "course_id": cfg["course"],
                "section": f"{n:02d}",
                "title": cfg.get("title", self.rng.choice(TOPICS)),
                "credit_hours": cfg.get("credits", 3),
                "seats_total": seats_total,
                "seats_taken": self.rng.randrange(0, seats_total),  # both candidates DB-consistent
                "campus": cfg.get("campus", "Glassboro"),
                "modality": cfg.get("modality", "in-person"),
                "meetings": meeting,
            })
        self.rng.shuffle(result)  # don't train the answer to be the first section
        return result

    def base(self, sections: list[dict]) -> dict:
        return {
            "student": {"student_id": f"S-DYN-{self.seed}-{self.scenario_number:03d}",
                        "degree": "Bachelor of Science", "major": "Computer Science",
                        "program_id": "O701", "catalog_year": TERM},
            "transcript": {"completed_courses": [], "transfer_courses": [],
                           "courses_in_progress": []},
            "program_requirements": [],
            "schedule": {"term": TERM, "sections": deepcopy(sections)},
            "prompt": "",
        }

    def pair(self, family: str, base: dict, variants: list[dict]):
        assert len(variants) == 2
        scenario_id = f"{family}-{self.scenario_number:03d}"
        sections = {s["crn"]: s for s in base["schedule"]["sections"]}
        for j, v in enumerate(variants, 1):
            inp = deepcopy(base)
            inp["prompt"] = v["prompt"]
            for k, value in v.get("input_changes", {}).items():
                inp[k] = deepcopy(value)
            good = list(v["acceptable"])
            bad = list(v["wrong"])
            assert good and bad and not (set(good) & set(bad))
            assert all(c in sections for c in good + bad)
            assert all(sections[c]["seats_taken"] < sections[c]["seats_total"] for c in good + bad)
            self.cases.append({
                "case_id": f"DYN-{len(self.cases)+1:04d}",
                "scenario_id": scenario_id,
                "variation": v["label"],
                "semantic_dimension": family,
                "input": inp,
                "ground_truth": {"acceptable_crns": good,
                                 "wrong_but_consistent_crns": bad,
                                 "reason": v["reason"]},
                "review_status": "candidate_needs_human_review",
            })

    def build_one_pair(self, family: str):
        self.scenario_number += 1
        fn = getattr(self, f"make_{family}")
        fn()

    def make_campus(self):
        cid = self.course()
        ss = self.sections([{"course": cid, "campus": "Glassboro"},
                            {"course": cid, "campus": "Camden", "days": ["Tuesday", "Thursday"]}])
        by = {s["campus"]: s["crn"] for s in ss}
        self.pair("campus", self.base(ss), [
            {"label": "Glassboro-only", "prompt": f"Register me for {cid}. I can attend ONLY at Glassboro.",
             "acceptable": [by["Glassboro"]], "wrong": [by["Camden"]],
             "reason": "Camden contradicts the explicit Glassboro-only constraint."},
            {"label": "Camden-only", "prompt": f"Register me for {cid}. I can attend ONLY at Camden.",
             "acceptable": [by["Camden"]], "wrong": [by["Glassboro"]],
             "reason": "Glassboro contradicts the explicit Camden-only constraint."},
        ])

    def make_modality(self):
        cid = self.course()
        ss = self.sections([{"course": cid, "modality": "online", "days": []},
                            {"course": cid, "modality": "in-person"}])
        by = {s["modality"]: s["crn"] for s in ss}
        self.pair("modality", self.base(ss), [
            {"label": "online required", "prompt": f"Enroll me in {cid}. Online delivery is required.",
             "acceptable": [by["online"]], "wrong": [by["in-person"]],
             "reason": "In-person violates the required online modality."},
            {"label": "in-person required", "prompt": f"Enroll me in {cid}. In-person delivery is required.",
             "acceptable": [by["in-person"]], "wrong": [by["online"]],
             "reason": "Online violates the required in-person modality."},
        ])

    def make_meeting_days(self):
        cid = self.course()
        mw = ["Monday", "Wednesday"]
        tt = ["Tuesday", "Thursday"]
        ss = self.sections([{"course": cid, "days": mw}, {"course": cid, "days": tt}])
        by = {tuple(s["meetings"][0]["days"]): s["crn"] for s in ss}
        self.pair("meeting_days", self.base(ss), [
            {"label": "Mon/Wed", "prompt": f"Enroll in {cid} on Mondays and Wednesdays, not Tuesdays/Thursdays.",
             "acceptable": [by[tuple(mw)]], "wrong": [by[tuple(tt)]],
             "reason": "Tuesday/Thursday is not the requested meeting-day set."},
            {"label": "Tue/Thu", "prompt": f"Enroll in {cid} on Tuesdays and Thursdays, not Mondays/Wednesdays.",
             "acceptable": [by[tuple(tt)]], "wrong": [by[tuple(mw)]],
             "reason": "Monday/Wednesday is not the requested meeting-day set."},
        ])

    def make_time_preference(self):
        cid = self.course()
        ss = self.sections([{"course": cid, "start": "9:00 AM", "end": "10:15 AM"},
                            {"course": cid, "start": "2:00 PM", "end": "3:15 PM",
                             "days": ["Tuesday", "Thursday"]}])
        by = {s["meetings"][0]["start_time"]: s["crn"] for s in ss}
        self.pair("time_preference", self.base(ss), [
            {"label": "morning only", "prompt": f"Register for {cid} in the morning only (before noon).",
             "acceptable": [by["9:00 AM"]], "wrong": [by["2:00 PM"]],
             "reason": "2:00 PM starts in the afternoon."},
            {"label": "afternoon only", "prompt": f"Register for {cid} in the afternoon only (after noon).",
             "acceptable": [by["2:00 PM"]], "wrong": [by["9:00 AM"]],
             "reason": "9:00 AM starts in the morning."},
        ])

    def make_negation(self):
        cid = self.course()
        ss = self.sections([{"course": cid}, {"course": cid, "days": ["Tuesday", "Thursday"]}])
        a, b = [s["crn"] for s in ss]
        self.pair("negation", self.base(ss), [
            {"label": f"exclude {a}", "prompt": f"Enroll me in {cid}, but do NOT use CRN {a}.",
             "acceptable": [b], "wrong": [a], "reason": f"CRN {a} is explicitly excluded."},
            {"label": f"exclude {b}", "prompt": f"Enroll me in {cid}, but do NOT use CRN {b}.",
             "acceptable": [a], "wrong": [b], "reason": f"CRN {b} is explicitly excluded."},
        ])

    def make_grade_threshold(self):
        foundation, fallback = self.course(), self.course()
        ss = self.sections([{"course": foundation, "credits": 4, "title": "Programming Foundation"},
                            {"course": fallback, "days": ["Tuesday", "Thursday"],
                             "title": "Software Elective"}])
        by = {s["course_id"]: s["crn"] for s in ss}
        base = self.base(ss)
        base["program_requirements"] = [{"req_id": "programming_foundation", "category": "Major",
                                          "rule": "all_of", "course_options": [foundation],
                                          "credit_hours": 4, "minimum_grade": "C-"}]
        prompt = (f"I need C- or better for {foundation}. If my grade is below C-, register me to "
                  f"repeat {foundation}; otherwise register me in {fallback}.")
        def transcript(grade):
            return {"completed_courses": [{"term": GRADED_TERM, "course_id": foundation,
                                           "grade": grade, "credit_hours": 4}],
                    "transfer_courses": [], "courses_in_progress": []}
        self.pair("grade_threshold", base, [
            {"label": "D below C-", "prompt": prompt,
             "input_changes": {"transcript": transcript("D")},
             "acceptable": [by[foundation]], "wrong": [by[fallback]],
             "reason": "D is below the specified C- minimum."},
            {"label": "B meets C-", "prompt": prompt,
             "input_changes": {"transcript": transcript("B")},
             "acceptable": [by[fallback]], "wrong": [by[foundation]],
             "reason": "B meets the specified C- minimum; take the fallback."},
        ])

    def make_requirement_priority(self):
        cid = self.course()
        ss = self.sections([{"course": cid, "modality": "online", "days": []},
                            {"course": cid, "modality": "in-person",
                             "start": "9:00 AM", "end": "10:15 AM"}])
        by = {s["modality"]: s["crn"] for s in ss}
        self.pair("requirement_priority", self.base(ss), [
            {"label": "online mandatory", "prompt": (f"Enroll in {cid}: online is MANDATORY, "
              "and morning is only a preference if possible."),
             "acceptable": [by["online"]], "wrong": [by["in-person"]],
             "reason": "In-person violates the mandatory online constraint."},
            {"label": "morning mandatory", "prompt": (f"Enroll in {cid}: a scheduled morning "
              "meeting is MANDATORY, and online is only a preference if possible."),
             "acceptable": [by["in-person"]], "wrong": [by["online"]],
             "reason": "The asynchronous online offering has no scheduled morning meeting."},
        ])

    def make_compound_intent(self):
        cid = self.course()
        specs = []
        for modality, start, end in [("online", "9:00 AM", "10:15 AM"),
                                     ("online", "2:00 PM", "3:15 PM"),
                                     ("in-person", "2:00 PM", "3:15 PM"),
                                     ("in-person", "9:00 AM", "10:15 AM")]:
            specs.append({"course": cid, "modality": modality, "start": start, "end": end,
                          "days": (["Monday", "Wednesday"] if start == "9:00 AM" else
                                   ["Tuesday", "Thursday"])})
        ss = self.sections(specs)
        by = {(s["modality"], s["meetings"][0]["start_time"]): s["crn"] for s in ss}
        good1, good2 = by[("online", "2:00 PM")], by[("in-person", "9:00 AM")]
        all_crns = set(s["crn"] for s in ss)
        self.pair("compound_intent", self.base(ss), [
            {"label": "online AND afternoon", "prompt": f"Enroll in {cid} online AND in the afternoon. BOTH conditions are mandatory.",
             "acceptable": [good1], "wrong": sorted(all_crns - {good1}),
             "reason": "All other sections violate at least one of the two mandatory constraints."},
            {"label": "in-person AND morning", "prompt": f"Enroll in {cid} in-person AND in the morning. BOTH conditions are mandatory.",
             "acceptable": [good2], "wrong": sorted(all_crns - {good2}),
             "reason": "All other sections violate at least one of the two mandatory constraints."},
        ])

    def make_earned_credit_arithmetic(self):
        passed_course, transfer_course, extra_course, new_elective, alternative = [self.course() for _ in range(5)]
        ss = self.sections([{"course": new_elective, "title": "Restricted Elective"},
                            {"course": alternative, "title": "Nonrestricted Elective",
                             "days": ["Tuesday", "Thursday"]}])
        by = {s["course_id"]: s["crn"] for s in ss}
        base = self.base(ss)
        base["program_requirements"] = [
            {"req_id": "restricted_electives", "category": "Restricted CS Electives",
             "rule": "credits_from", "course_options": [passed_course, transfer_course,
                                                       extra_course, new_elective],
             "credit_hours": 9, "minimum_grade": "C-"}
        ]
        prompt = (f"Count earned credits from the restricted elective course_options only, including "
                  f"transfer credit. If fewer than 9 credits are earned, enroll in {new_elective}; "
                  f"if at least 9 have been earned, enroll in {alternative} instead.")
        completed = [{"term": GRADED_TERM, "course_id": passed_course,
                      "grade": "B", "credit_hours": 3}]
        transfer = [{"institution": "Partner College", "course_id": transfer_course,
                     "grade": "B", "credit_hours": 3}]
        def transcript(extra):
            return {"completed_courses": deepcopy(completed) + (
                    [{"term": GRADED_TERM, "course_id": extra_course,
                      "grade": "A", "credit_hours": 3}] if extra else []),
                    "transfer_courses": deepcopy(transfer), "courses_in_progress": []}
        self.pair("earned_credit_arithmetic", base, [
            {"label": "6 earned credits", "prompt": prompt,
             "input_changes": {"transcript": transcript(False)},
             "acceptable": [by[new_elective]], "wrong": [by[alternative]],
             "reason": "3 completed + 3 transfer = 6, below the 9-credit requirement."},
            {"label": "9 earned credits", "prompt": prompt,
             "input_changes": {"transcript": transcript(True)},
             "acceptable": [by[alternative]], "wrong": [by[new_elective]],
             "reason": "6 completed + 3 transfer = 9, which meets the requirement."},
        ])

    def make_in_progress(self):
        selected, alternative = self.course(), self.course()
        ss = self.sections([{"course": selected, "title": "Planned Course"},
                            {"course": alternative, "title": "Alternative Course",
                             "days": ["Tuesday", "Thursday"]}])
        by = {s["course_id"]: s["crn"] for s in ss}
        base = self.base(ss)
        prompt = (f"If I am already taking {selected} this term, enroll me in {alternative} instead. "
                  f"If I am NOT currently taking {selected}, enroll me in {selected}.")
        active = {"completed_courses": [], "transfer_courses": [],
                  "courses_in_progress": [{"term": TERM, "course_id": selected,
                                           "credit_hours": 3}]}
        empty = {"completed_courses": [], "transfer_courses": [], "courses_in_progress": []}
        self.pair("in_progress", base, [
            {"label": "not currently taking", "prompt": prompt,
             "input_changes": {"transcript": empty},
             "acceptable": [by[selected]], "wrong": [by[alternative]],
             "reason": "No in-progress record exists for the planned course."},
            {"label": "currently taking", "prompt": prompt,
             "input_changes": {"transcript": active},
             "acceptable": [by[alternative]], "wrong": [by[selected]],
             "reason": "The planned course is already listed as in progress."},
        ])


def select_families(spec: str) -> list[str]:
    names = [name.strip() for name in spec.split(",") if name.strip()]
    if not names or len(names) != len(set(names)):
        raise ValueError("Choose at least one family; do not repeat family names")
    unknown = sorted(set(names) - set(FAMILIES))
    if unknown:
        raise ValueError(f"Unknown families: {unknown}; available: {', '.join(FAMILIES)}")
    return names


def save_cases(cases: list[dict], args, families: list[str]):
    output = Path(args.output)
    index = Path(args.index_output) if args.index_output else output.with_name(output.stem + "_index.csv")
    manifest = (Path(args.manifest_output) if args.manifest_output else
                output.with_name(output.stem + "_manifest.json"))
    paths = (output, index, manifest)
    if len(set(p.resolve() for p in paths)) != 3:
        raise ValueError("Output JSONL, index, and manifest paths must be different")
    for path in paths:
        if path.exists() and not args.overwrite:
            raise FileExistsError(f"{path} exists; choose another --output or pass --overwrite")
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "\n" for case in cases)
    output.write_text(body, encoding="utf-8")
    with index.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["case_id", "scenario_id", "family", "variation", "expected_crns", "wrong_crns", "review_status"])
        for case in cases:
            writer.writerow([case["case_id"], case["scenario_id"], case["semantic_dimension"],
                             case["variation"], ";".join(case["ground_truth"]["acceptable_crns"]),
                             ";".join(case["ground_truth"]["wrong_but_consistent_crns"]),
                             case["review_status"]])
    summary = {"seed": args.seed, "pairs_per_family": args.pairs_per_family,
               "families": families, "total_cases": len(cases),
               "cases_by_family": dict(Counter(c["semantic_dimension"] for c in cases)),
               "jsonl_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
               "ground_truth_review": "candidate_needs_human_review",
               "note": "All generated records are synthetic; case.input alone is sent to the model."}
    manifest.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=42, help="Same seed and arguments produce identical cases")
    parser.add_argument("--pairs-per-family", type=int, default=1, help="Number of controlled-reversal pairs per family")
    parser.add_argument("--families", default=",".join(FAMILIES), help="Comma-separated subset of supported families")
    parser.add_argument("--output", default="dynamic_semantic_cases.jsonl", help="Output JSONL path")
    parser.add_argument("--index-output", help="Optional CSV path; defaults beside output")
    parser.add_argument("--manifest-output", help="Optional manifest JSON path")
    parser.add_argument("--schema", help="Team's registration_input.schema.json (optional but recommended)")
    parser.add_argument("--smoke-check", action="store_true", help="Call existing runner's fixture checker; requires harness.py and registration.py beside it")
    parser.add_argument("--overwrite", action="store_true", help="Replace previously generated files")
    args = parser.parse_args()
    if args.pairs_per_family < 1:
        parser.error("--pairs-per-family must be at least 1")
    try:
        families = select_families(args.families)
    except ValueError as e:
        parser.error(str(e))
    builder = Builder(args.seed)
    for _ in range(args.pairs_per_family):
        for family in families:
            builder.build_one_pair(family)
    schema = None
    if args.schema:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            parser.error("Install JSON Schema dependency: python -m pip install jsonschema")
        schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        for case in builder.cases:
            validator.validate(case["input"])
        print(f"PASS: {len(builder.cases)} generated inputs validate against the supplied schema")
    else:
        print("WARNING: input schema not checked. Pass --schema registration_input.schema.json")
    if args.smoke_check:
        try:
            from run_semantic_cases_aneesh import smoke_check
        except ImportError as e:
            parser.error(f"Runner or its dependencies unavailable: {e}")
        smoke_check(builder.cases, schema)
    try:
        paths = save_cases(builder.cases, args, families)
    except (FileExistsError, ValueError) as e:
        parser.error(str(e))
    print(f"Generated {len(builder.cases)} cases ({len(builder.cases)//2} paired scenarios), seed={args.seed}")
    for path in paths:
        print(f"Saved: {path}")


if __name__ == "__main__":
    main()
