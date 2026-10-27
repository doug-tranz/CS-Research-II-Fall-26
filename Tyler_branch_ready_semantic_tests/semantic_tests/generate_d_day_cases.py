"""Build D1/D2/D3 day-reasoning registration test cases for CS Research II.

Pairs isolate a change in desired meeting days (D1, D2) or the Friday
meeting in one section (D3). Ground truth is not sent to the model.

Requires generate_dynamic_cases.py from Tyler's existing GitHub generator.
Run from the semantic_tests directory:

  python generate_d_day_cases.py --seed 2026 --pairs-per-category 2 \\
      --schema registration_input.schema.json --smoke-check \\
      --output d_category_cases_12.jsonl
  python run_semantic_cases_aneesh.py --dry-run \\
      --test-file d_category_cases_12.jsonl --schema registration_input.schema.json
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import csv
import json
from pathlib import Path

from generate_dynamic_cases import Builder

CATEGORIES = ("D1", "D2", "D3")
DESCRIPTIONS = {
    "D1": "Day pattern: Monday/Wednesday versus Tuesday/Thursday",
    "D2": "Shared days, different pattern: MW versus MWF (or a parallel pattern)",
    "D3": "Day exclusion: avoid Friday sessions",
}


class DayBuilder(Builder):
    """Reuse deterministic student/course/CRN creation and paired-label logic."""

    def _pair_d1(self, pair_index: int) -> None:
        self.scenario_number += 1
        cid = self.course()
        ss = self.sections([
            {"course": cid, "days": ["Monday", "Wednesday"], "title": "Day-Pattern Seminar", "start": "9:00 AM" if pair_index % 2 else "1:00 PM", "end": "10:15 AM" if pair_index % 2 else "2:15 PM"},
            {"course": cid, "days": ["Tuesday", "Thursday"], "title": "Day-Pattern Seminar", "start": "9:00 AM" if pair_index % 2 else "1:00 PM", "end": "10:15 AM" if pair_index % 2 else "2:15 PM"},
        ])
        by = {tuple(s["meetings"][0]["days"]): s["crn"] for s in ss}
        mw, tr = by[("Monday", "Wednesday")], by[("Tuesday", "Thursday")]
        self.pair("D1_day_pattern", self.base(ss), [
            {"label": "MW requested", "prompt": f"Enroll me in {cid}. My available class days are Monday and Wednesday only; I cannot attend Tuesday/Thursday meetings.",
             "acceptable": [mw], "wrong": [tr], "reason": "The requested weekday pattern is Monday/Wednesday."},
            {"label": "TR requested", "prompt": f"Enroll me in {cid}. My available class days are Tuesday and Thursday only; I cannot attend Monday/Wednesday meetings.",
             "acceptable": [tr], "wrong": [mw], "reason": "The requested weekday pattern is Tuesday/Thursday."},
        ])

    def _pair_d2(self, pair_index: int) -> None:
        self.scenario_number += 1
        cid = self.course()
        common = ["Monday", "Wednesday"] if pair_index % 2 else ["Tuesday", "Thursday"]
        extra = "Friday" if pair_index % 2 else "Saturday"
        ss = self.sections([
            {"course": cid, "days": common, "title": "Shared-Day Workshop", "start": "11:00 AM", "end": "12:15 PM"},
            {"course": cid, "days": common + [extra], "title": "Shared-Day Workshop", "start": "11:00 AM", "end": "12:15 PM"},
        ])
        by = {tuple(s["meetings"][0]["days"]): s["crn"] for s in ss}
        short, long = by[tuple(common)], by[tuple(common + [extra])]
        days_words = " and ".join(common)
        all_words = ", ".join(common + [extra])
        self.pair("D2_shared_day_pattern", self.base(ss), [
            {"label": "exactly two weekdays", "prompt": f"Enroll me in {cid}. I need the section scheduled on exactly {days_words}. Do not pick one with an additional meeting day.",
             "acceptable": [short], "wrong": [long], "reason": f"Both meet on {days_words}, but only one has exactly two meeting days."},
            {"label": "three weekdays required", "prompt": f"Enroll me in {cid}. I specifically need a section meeting on all three of these days: {all_words}.",
             "acceptable": [long], "wrong": [short], "reason": f"Only the longer meeting pattern includes {extra}."},
        ])

    def _pair_d3(self, pair_index: int) -> None:
        self.scenario_number += 1
        cid = self.course()
        if pair_index % 2:
            fri_days = ["Monday", "Wednesday", "Friday"]
            safe_days = ["Monday", "Wednesday"]
        else:
            fri_days = ["Tuesday", "Friday"]
            safe_days = ["Tuesday", "Thursday"]
        ss = self.sections([
            {"course": cid, "days": fri_days, "title": "Friday-Exclusion Topics", "start": "2:00 PM", "end": "3:15 PM"},
            {"course": cid, "days": safe_days, "title": "Friday-Exclusion Topics", "start": "2:00 PM", "end": "3:15 PM"},
        ])
        by_days = {tuple(s["meetings"][0]["days"]): s["crn"] for s in ss}
        friday_crn, safe_crn = by_days[tuple(fri_days)], by_days[tuple(safe_days)]
        prompt = f"Register me for {cid}. I cannot attend anything on Fridays. Choose an open section without a Friday meeting."
        # Control: one section has Friday, the other does not. Variation: the
        # same two CRNs have their weekday patterns exchanged. ONLY days change.
        new_schedule = deepcopy(self.base(ss)["schedule"])
        for s in new_schedule["sections"]:
            s["meetings"][0]["days"] = (deepcopy(safe_days) if s["crn"] == friday_crn
                                               else deepcopy(fri_days))
        self.pair("D3_day_exclusion", self.base(ss), [
            {"label": "Friday on distractor", "prompt": prompt,
             "acceptable": [safe_crn], "wrong": [friday_crn],
             "reason": "The Friday meeting disqualifies the distractor despite open seats."},
            {"label": "Friday switched to other section", "prompt": prompt,
             "input_changes": {"schedule": new_schedule},
             "acceptable": [friday_crn], "wrong": [safe_crn],
             "reason": "Only the weekday schedule changed; the correct CRN must flip."},
        ])


def validate_cases(cases: list[dict], schema_path: Path | None) -> None:
    if schema_path is not None:
        from jsonschema import Draft202012Validator
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        for c in cases:
            validator.validate(c["input"])
        print(f"PASS: {len(cases)} generated inputs match JSON Schema {schema_path.name}")
    for c in cases:
        ss = c["input"]["schedule"]["sections"]
        by = {s["crn"]: s for s in ss}
        assert len(by) == len(ss), c["case_id"]
        good = set(c["ground_truth"]["acceptable_crns"])
        wrong = set(c["ground_truth"]["wrong_but_consistent_crns"])
        assert good and wrong and not good.intersection(wrong), c["case_id"]
        assert all(crn in by and by[crn]["seats_taken"] < by[crn]["seats_total"] for crn in good | wrong), c["case_id"]
        if c["semantic_dimension"] == "D3_day_exclusion":
            for crn in good:
                assert all("Friday" not in m["days"] for m in by[crn]["meetings"]), c["case_id"]
            for crn in wrong:
                assert any("Friday" in m["days"] for m in by[crn]["meetings"]), c["case_id"]
        else:
            for crn in good | wrong:
                assert by[crn]["meetings"], c["case_id"]
    print(f"PASS: {len(cases)} fixtures have available, distinct correct/wrong CRNs")


def generate(seed: int, pairs_per_category: int) -> list[dict]:
    b = DayBuilder(seed)
    for n in range(1, pairs_per_category + 1):
        b._pair_d1(n)
        b._pair_d2(n)
        b._pair_d3(n)
    for idx, c in enumerate(b.cases, 1):
        cat = c["semantic_dimension"].split("_")[0]
        c["case_id"] = f"DAY-{cat}-{idx:03d}"
        c["review_status"] = "candidate_needs_human_review"
    return b.cases


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--pairs-per-category", type=int, default=2)
    p.add_argument("--schema", type=Path, default=None)
    p.add_argument("--smoke-check", action="store_true")
    p.add_argument("--output", type=Path, default=Path("d_category_cases_12.jsonl"))
    a = p.parse_args()
    if a.pairs_per_category < 1:
        p.error("--pairs-per-category must be at least 1")
    cases = generate(a.seed, a.pairs_per_category)
    validate_cases(cases, a.schema)
    if a.smoke_check:
        from run_semantic_cases_aneesh import smoke_check
        schema = json.loads(a.schema.read_text(encoding="utf-8")) if a.schema else None
        smoke_check(cases, schema)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False, separators=(",", ":")) + "\n")
    stem = a.output.with_suffix("")
    index = stem.with_name(stem.name + "_index.csv")
    manifest = stem.with_name(stem.name + "_manifest.json")
    with index.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(("case_id", "category", "scenario_id", "variation", "correct_crn", "wrong_crn", "review_status"))
        for c in cases:
            writer.writerow((c["case_id"], c["semantic_dimension"], c["scenario_id"], c["variation"],
                             ";".join(c["ground_truth"]["acceptable_crns"]),
                             ";".join(c["ground_truth"]["wrong_but_consistent_crns"]), c["review_status"]))
    manifest.write_text(json.dumps({"seed": a.seed, "pairs_per_category": a.pairs_per_category,
                                    "case_count": len(cases), "categories": dict(Counter(c["semantic_dimension"] for c in cases)),
                                    "source_schema": str(a.schema) if a.schema else None,
                                    "label_status": "candidate_needs_human_review"}, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {len(cases)} D-category cases ({len(cases)//2} pairs), seed={a.seed}")
    print(f"Saved: {a.output}, {index}, {manifest}")


if __name__ == "__main__":
    main()
