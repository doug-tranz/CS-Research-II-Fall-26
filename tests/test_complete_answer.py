"""Checks of the category O pair families (seconds, no model, no cost). Run from the repo root:

    python -m unittest discover tests

O is "answer completeness" (the task card has no description; see docs/test-cases.md). Each pair asks for
TWO named courses under one or more rules. A complete answer is one section of each course with every rule
holding at once. The variants flip one rule.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cases           # noqa: E402
import generate_pairs   # noqa: E402
import pairs            # noqa: E402
import scoring          # noqa: E402

CASES = json.loads((ROOT / "paired_cases.json").read_text(encoding="utf-8"))
BY_ID = {c["case_id"]: c for c in CASES}
FAMILIES = ("o1_shared_rule", "o2_stacked_rules", "o3_busy_both")
O = {pid: v for pid, v in pairs.pairs_of(CASES).items() if pid.split("/")[0] in FAMILIES}


def of_family(name):
    return {pid: v for pid, v in O.items() if pid.startswith(name + "/")}


class CompleteAnswerFamilies(unittest.TestCase):
    def test_each_family_is_in_the_file(self):
        for name in FAMILIES:
            self.assertGreaterEqual(len(of_family(name)), 1, name)

    def test_every_case_needs_both_courses(self):
        for pid, variants in O.items():
            for case_id in variants.values():
                case = BY_ID[case_id]
                rules = case["ground_truth"]["rules"]
                self.assertEqual(rules["count"], {"min": 2, "max": 2}, case_id)
                self.assertEqual(len(rules["must_include_courses"]), 2, case_id)
                for course in rules["must_include_courses"]:
                    self.assertIn(course, case["input"]["prompt"], case_id)

    def test_o1_flips_only_the_day_rule(self):
        for pid, variants in of_family("o1_shared_rule").items():
            ra, rb = (BY_ID[variants[v]]["ground_truth"]["rules"] for v in "ab")
            self.assertNotEqual(ra, rb, pid)
            differing = {k for k in set(ra) | set(rb) if ra.get(k) != rb.get(k)}
            self.assertEqual(differing, {"no_days"} if "no_days" in differing else {"modality_in"}, pid)

    def test_o2_keeps_the_time_rule_and_flips_the_day_rule(self):
        for pid, variants in of_family("o2_stacked_rules").items():
            ra, rb = (BY_ID[variants[v]]["ground_truth"]["rules"] for v in "ab")
            time_keys = [k for k in ("ends_by", "starts_before") if k in ra]
            self.assertEqual(len(time_keys), 1, pid)
            self.assertEqual(ra[time_keys[0]], rb[time_keys[0]], pid)
            self.assertNotEqual(ra["no_days"], rb["no_days"], pid)

    def test_o3_commitments_differ_and_apply_to_both_courses(self):
        for pid, variants in of_family("o3_busy_both").items():
            a, b = (BY_ID[variants[v]] for v in "ab")
            self.assertNotEqual(a["ground_truth"]["rules"]["busy"], b["ground_truth"]["rules"]["busy"], pid)
            self.assertIn("neither class", a["input"]["prompt"], pid)

    def test_no_cart_is_valid_in_both_variants(self):
        """Over every pair of sections (not only the saved examples): no cart is correct in both variants,
        so a model cannot pass a pair without reading the detail that changes."""
        for pid, variants in O.items():
            a, b = (BY_ID[variants[v]] for v in "ab")
            c = generate_pairs.Case(a)
            x, y = a["ground_truth"]["rules"]["must_include_courses"]
            cands = (generate_pairs.by_course()[x], generate_pairs.by_course()[y])
            right = [{tuple(cart) for cart, _, _ in generate_pairs.judge(c, cands, v["ground_truth"]["rules"], 2)[0]}
                     for v in (a, b)]
            self.assertTrue(right[0] and right[1], pid)
            self.assertFalse(right[0] & right[1], pid)

    def test_pairs_are_sound(self):
        """The team's own pair and answer-key checks, on the category O cases only (named courses: fast)."""
        ids = [c for v in O.values() for c in v.values()]
        loaded = cases.load_test_cases(str(ROOT / "paired_cases.json"), ids)
        self.assertEqual(pairs.check_pairs(loaded), [])
        for case in loaded:
            self.assertEqual(scoring.self_check(case), [], case["case_id"])


if __name__ == "__main__":
    unittest.main()
