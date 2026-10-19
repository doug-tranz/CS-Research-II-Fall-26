"""Offline checks for the dynamic semantic-case generator; does NOT call an LLM."""
import json
from pathlib import Path
import re
import unittest

from generate_dynamic_cases import Builder, FAMILIES

HERE = Path(__file__).resolve().parent


def make_cases(seed=42, repetitions=2):
    builder = Builder(seed)
    for _ in range(repetitions):
        for family in FAMILIES:
            builder.build_one_pair(family)
    return builder.cases


def expected_from_input(case):
    """Independent rule check for the candidate section(s), not the saved answer key."""
    inp = case["input"]
    family = case["semantic_dimension"]
    sections = inp["schedule"]["sections"]
    prompt = inp["prompt"]
    if family == "campus":
        required = "Glassboro" if "ONLY at Glassboro" in prompt else "Camden"
        return [s["crn"] for s in sections if s["campus"] == required]
    if family == "modality":
        modality = "online" if "Online delivery" in prompt else "in-person"
        return [s["crn"] for s in sections if s["modality"] == modality]
    if family == "meeting_days":
        days = ["Monday", "Wednesday"] if "Mondays and Wednesdays" in prompt else ["Tuesday", "Thursday"]
        return [s["crn"] for s in sections if s["meetings"][0]["days"] == days]
    if family == "time_preference":
        am = "in the morning" in prompt
        return [s["crn"] for s in sections if s["meetings"][0]["start_time"].endswith("AM") == am]
    if family == "negation":
        forbidden = re.search(r"do NOT use CRN (\d+)", prompt).group(1)
        return [s["crn"] for s in sections if s["crn"] != forbidden]
    if family == "grade_threshold":
        foundation = inp["program_requirements"][0]["course_options"][0]
        grade = inp["transcript"]["completed_courses"][0]["grade"]
        repeat = grade == "D"
        return [s["crn"] for s in sections if (s["course_id"] == foundation) == repeat]
    if family == "requirement_priority":
        online = "online is MANDATORY" in prompt
        return [s["crn"] for s in sections if (s["modality"] == "online") == online]
    if family == "compound_intent":
        online = "online AND in the afternoon" in prompt
        required_modality = "online" if online else "in-person"
        required_am = not online
        return [s["crn"] for s in sections if s["modality"] == required_modality and
                s["meetings"][0]["start_time"].endswith("AM") == required_am]
    if family == "earned_credit_arithmetic":
        options = set(inp["program_requirements"][0]["course_options"])
        completed = inp["transcript"]["completed_courses"]
        transfer = inp["transcript"]["transfer_courses"]
        earned = sum(c["credit_hours"] for c in completed + transfer if c["course_id"] in options)
        needs_more = earned < 9
        return [s["crn"] for s in sections if (s["course_id"] in options) == needs_more]
    if family == "in_progress":
        planned = re.search(r"already taking (CS\d{5})", prompt).group(1)
        taking = planned in {c["course_id"] for c in inp["transcript"]["courses_in_progress"]}
        return [s["crn"] for s in sections if (s["course_id"] == planned) != taking]
    raise AssertionError("Unrecognized family")


class DynamicGeneratorTests(unittest.TestCase):
    def test_generated_pairs_are_logically_consistent(self):
        for seed in (0, 1, 42, 99):
            cases = make_cases(seed)
            self.assertEqual(len(cases), 40)
            self.assertEqual(len({c["case_id"] for c in cases}), len(cases))
            for a, b in zip(cases[::2], cases[1::2]):
                self.assertEqual(a["scenario_id"], b["scenario_id"])
                self.assertEqual(a["input"]["schedule"], b["input"]["schedule"])
                self.assertNotEqual(a["ground_truth"]["acceptable_crns"], b["ground_truth"]["acceptable_crns"])
            for case in cases:
                good = case["ground_truth"]["acceptable_crns"]
                wrong = case["ground_truth"]["wrong_but_consistent_crns"]
                self.assertEqual(sorted(good), sorted(expected_from_input(case)))
                self.assertFalse(set(good) & set(wrong))
                lookup = {s["crn"]: s for s in case["input"]["schedule"]["sections"]}
                for crn in good + wrong:
                    self.assertLess(lookup[crn]["seats_taken"], lookup[crn]["seats_total"])

    def test_seed_is_reproducible(self):
        self.assertEqual(make_cases(42), make_cases(42))
        self.assertNotEqual(make_cases(42), make_cases(43))

    def test_matches_official_schema_if_present(self):
        schema_file = HERE / "registration_input.schema.json"
        if not schema_file.exists():
            self.skipTest("Copy your existing registration_input.schema.json next to this script")
        from jsonschema import Draft202012Validator
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        for case in make_cases(42):
            validator.validate(case["input"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
