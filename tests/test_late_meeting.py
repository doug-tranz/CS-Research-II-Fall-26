"""Checks of the late_meeting pair family (seconds, no model, no cost). Run from the repo root:

    python -m unittest discover tests

Each late_meeting pair has a section whose FIRST meeting ends by a cutoff hour but whose LATER meeting
runs past it. Version (a) says nothing may end after the cutoff; version (b) says "No <day> classes".
A model that reads only a section's first meeting picks the hidden-late section in (a), and in (b)
that section is a right answer.
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
from input_format import minutes   # noqa: E402

CASES = json.loads((ROOT / "paired_cases.json").read_text(encoding="utf-8"))
BY_ID = {c["case_id"]: c for c in CASES}
LATE = {pid: v for pid, v in pairs.pairs_of(CASES).items() if pid.startswith("late_meeting/")}


def ends(section):
    return [minutes(m["end_time"]) for m in section["meetings"] if "end_time" in m]


def crn_sections():
    return {s["crn"]: s for sections in generate_pairs.by_course().values() for s in sections}


class LateMeetingFamily(unittest.TestCase):
    def test_the_family_is_in_the_file(self):
        self.assertGreaterEqual(len(LATE), 1)

    def test_prompts_are_the_two_constraints(self):
        for pid, variants in LATE.items():
            a, b = BY_ID[variants["a"]], BY_ID[variants["b"]]
            self.assertIn("nothing that ends after", a["input"]["prompt"], pid)
            self.assertIn("ends_by", a["ground_truth"]["rules"], pid)
            self.assertRegex(b["input"]["prompt"], r"No \w+ classes\.$", pid)
            self.assertIn("no_days", b["ground_truth"]["rules"], pid)

    def test_pairs_are_strong(self):
        """No single section is a valid answer in both variants, so luck cannot pass a pair."""
        for pid, variants in LATE.items():
            valid_a = {tuple(x) for x in BY_ID[variants["a"]]["ground_truth"]["examples"]["valid"]}
            valid_b = {tuple(x) for x in BY_ID[variants["b"]]["ground_truth"]["examples"]["valid"]}
            self.assertFalse(valid_a & valid_b, pid)

    def test_a_hidden_late_section_is_what_the_twin_accepts(self):
        """In (a) a section the twin accepts is rejected because a LATER meeting runs past the cutoff,
        while its first meeting is fine."""
        sections = crn_sections()
        for pid, variants in LATE.items():
            a = BY_ID[variants["a"]]
            cutoff = minutes(a["ground_truth"]["rules"]["ends_by"])
            hidden = [sections[cart[0]] for cart in BY_ID[variants["b"]]["ground_truth"]["examples"]["valid"]
                      if len(ends(sections[cart[0]])) >= 2
                      and ends(sections[cart[0]])[0] <= cutoff < max(ends(sections[cart[0]])[1:])]
            self.assertTrue(hidden, f"{pid}: no section hides its late meeting")

    def test_pairs_are_sound(self):
        """The team's own pair and answer-key checks, on the late_meeting cases only (named courses: fast)."""
        ids = [c for v in LATE.values() for c in v.values()]
        loaded = cases.load_test_cases(str(ROOT / "paired_cases.json"), ids)
        self.assertEqual(pairs.check_pairs(loaded), [])
        for case in loaded:
            self.assertEqual(scoring.self_check(case), [], case["case_id"])


if __name__ == "__main__":
    unittest.main()
