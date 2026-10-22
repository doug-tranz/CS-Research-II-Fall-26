"""Fast checks of the pipeline (seconds, no model, no cost). Run from the repo root:

    python -m unittest discover tests

The slow, complete check of every answer key is still `python run_semantic_eval.py --check`.
"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cases          # noqa: E402
import input_format   # noqa: E402
import pairs          # noqa: E402
import render         # noqa: E402
import results        # noqa: E402
import scoring        # noqa: E402

CASE_FILE = str(ROOT / "paired_cases.json")
PAIR = ["pair-transfer-philosophy-ba-a", "pair-transfer-philosophy-ba-b"]


class Schema(unittest.TestCase):
    def test_message_schema_examples(self):
        """didcomm_schema.py's 34 example messages get the expected tier."""
        out = subprocess.run([sys.executable, "didcomm_schema.py"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("34/34 as expected", out.stdout)


class Scoring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = cases.load_test_cases(CASE_FILE, PAIR)          # named courses: small and fast
        cls.case = cls.cases[0]

    def answer(self, cart):
        return cases.expected_message(self.case, cart)

    def test_answer_key_scores_itself(self):
        for case in self.cases:
            self.assertEqual(scoring.self_check(case), [], case["case_id"])

    def test_pair_is_sound(self):
        self.assertEqual(pairs.check_pairs(self.cases), [])

    def test_each_tier_fails_where_it_should(self):
        good = self.answer(self.case["ground_truth"]["examples"]["valid"][0])
        self.assertEqual(scoring.evaluate_case(self.case, good)["outcome"], "semantic_ok")
        msg = json.loads(good)
        broken = copy.deepcopy(msg)
        broken["body"]["sections"][0]["crn"] = "00000"
        wrong_student = copy.deepcopy(msg)
        wrong_student["body"]["student_id"] = "S999999"
        expected = {
            "Sure, here it is.": "syntactic_error",
            "```json\n" + good + "\n```": "syntactic_error",
            json.dumps({**msg, "body": {**msg["body"], "total_credits": 3}}): "schema_error",
            json.dumps(broken): "consistency_error",
            json.dumps(wrong_student): "semantic_error",
        }
        for raw, outcome in expected.items():
            self.assertEqual(scoring.evaluate_case(self.case, raw)["outcome"], outcome, raw[:60])

    def test_model_never_sees_the_answer_key(self):
        for rendering in render.RENDERINGS:
            task = render.to_task(self.case, rendering)
            text = task.inputs + task.intent + task.output_contract
            self.assertNotIn("ground_truth", text)
            self.assertNotIn(self.case["ground_truth"]["implicit_relationship"][:40], text)


class InputValidation(unittest.TestCase):
    def test_cached_schedule_check_sees_changes(self):
        """The schedule check is cached by content: a changed schedule is checked again."""
        data = cases.load_test_cases(CASE_FILE, PAIR[:1])[0]["input"]
        self.assertEqual(input_format.validate_input(data), [])
        bad = copy.deepcopy(data)
        bad["schedule"]["sections"][0]["seats_taken"] = bad["schedule"]["sections"][0]["seats_total"] + 1
        self.assertTrue(any("seats_taken" in p for p in input_format.validate_input(bad)))
        self.assertEqual(input_format.validate_input(data), [])


class ResultsFile(unittest.TestCase):
    def test_one_run_per_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "out.json")
            with results.output_lock(path):
                with self.assertRaises(SystemExit):
                    with results.output_lock(path):
                        pass
            self.assertFalse(Path(path + ".lock").exists())

    def test_save_replaces_the_file_whole(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "out.json")
            results.save_results(path, [{"session_id": "s"}], [])
            self.assertEqual(json.loads(Path(path).read_text())["sessions"][0]["session_id"], "s")
            self.assertFalse(Path(path + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
