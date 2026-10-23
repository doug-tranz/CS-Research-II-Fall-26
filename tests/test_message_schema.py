"""
Tiers 1-2 (pipeline/didcomm_schema.py): one message per way of failing, plus a few that pass.
Each must land in the expected outcome: syntactic_error (Tier 1), schema_error (Tier 2) or
schema_ok.

    python -m unittest tests.test_message_schema -v
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.didcomm_schema import MESSAGE_TYPE, _EXAMPLE, validate_message   # noqa: E402


def entry(crn, **changes):
    """A complete section entry; a change of None removes that field."""
    item = {"crn": crn, "course_id": "CS04114", "section": "1", "credit_hours": 3,
            "seats_total": 30, "seats_taken": 20, "campus": "Glassboro", "modality": "in-person",
            "meetings": [{"days": ["Monday", "Wednesday"], "start_time": "09:30 AM",
                          "end_time": "10:45 AM", "type": "Class", "location": "Robinson Hall 207"}]}
    item.update(changes)
    return {k: v for k, v in item.items() if v is not None}

good = {"id": "a1", "type": MESSAGE_TYPE,
        "body": {"student_id": "S001", "term_code": "202640",
                 "sections": [entry("41836"), entry("41909")]}}

def variant(**changes):
    """The good message with top-level fields changed."""
    return {**good, **changes}

def body(**changes):
    """The good message with body fields changed."""
    return variant(body={**good["body"], **changes})

CASES = [
    ("valid cart", json.dumps(good), "schema_ok"),
    ("waitlist, no meetings, bare meeting", json.dumps(body(sections=[
        entry("45510", meetings=[]), entry("41909", waitlist=True, seats_taken=30),
        entry("41910", meetings=[{"days": ["Friday"]}])])), "schema_ok"),
    ("contract example", json.dumps(_EXAMPLE), "schema_ok"),
    ("valid, with optional headers", json.dumps(variant(
        typ="application/didcomm-plain+json", **{"from": "did:example:alice"},
        to=["did:example:registrar"], created_time=1791331200)), "schema_ok"),
    ("prose around the JSON", "Here is the message:\n" + json.dumps(good), "syntactic_error"),
    ("code fence", "```json\n" + json.dumps(good) + "\n```", "syntactic_error"),
    ("JSON array, not an object", json.dumps([good]), "syntactic_error"),
    ("no id", json.dumps({k: v for k, v in good.items() if k != "id"}), "syntactic_error"),
    ("body is a string", json.dumps(variant(body="S001 41836")), "syntactic_error"),
    ("type is not a message type URI", json.dumps(variant(type="registration/enroll")), "syntactic_error"),
    ("from is not a DID", json.dumps(variant(**{"from": "alice"})), "syntactic_error"),
    ("created_time is an ISO string", json.dumps(variant(created_time="2026-10-07T00:00:00Z")), "syntactic_error"),
    ("a different protocol's type", json.dumps(variant(type="https://didcomm.org/basicmessage/2.0/message")), "schema_error"),
    ("old single-section body", json.dumps(variant(body={"student_id": "S001", "section_id": "41836"})), "schema_error"),
    ("sections as bare strings", json.dumps(body(sections=["41836", "41909"])), "schema_error"),
    ("no term_code", json.dumps(variant(body={"student_id": "S001", "sections": [entry("41836")]})), "schema_error"),
    ("term_code as a number", json.dumps(body(term_code=202640)), "schema_error"),
    ("empty cart", json.dumps(body(sections=[])), "schema_error"),
    ("same crn twice", json.dumps(body(sections=[entry("41836"), entry("41836", waitlist=True)])), "schema_error"),
    ("crn only (the earlier entry)", json.dumps(body(sections=[{"crn": "41836"}])), "schema_error"),
    ("section entry without crn", json.dumps(body(sections=[entry(None, section_id="41836")])), "schema_error"),
    ("section entry without meetings", json.dumps(body(sections=[entry("41836", meetings=None)])), "schema_error"),
    ("section entry without credit_hours", json.dumps(body(sections=[entry("41836", credit_hours=None)])), "schema_error"),
    ("unknown field in a section", json.dumps(body(sections=[entry("41836", grade_mode="P/F")])), "schema_error"),
    ("credit_hours as a string", json.dumps(body(sections=[entry("45510", credit_hours="3")])), "schema_error"),
    ("seats_taken as a string", json.dumps(body(sections=[entry("45510", seats_taken="20")])), "schema_error"),
    ("course_id with a space", json.dumps(body(sections=[entry("45510", course_id="CS 04114")])), "schema_error"),
    ("unknown modality", json.dumps(body(sections=[entry("45510", modality="remote")])), "schema_error"),
    ("day abbreviated", json.dumps(body(sections=[entry("45510", meetings=[{"days": ["MW"]}])])), "schema_error"),
    ("24-hour time", json.dumps(body(sections=[entry("45510", meetings=[
        {"days": ["Monday"], "start_time": "13:00", "end_time": "14:15"}])])), "schema_error"),
    ("start_time without end_time", json.dumps(body(sections=[entry("45510", meetings=[
        {"days": ["Monday"], "start_time": "1:00 PM"}])])), "schema_error"),
    ("unknown meeting field", json.dumps(body(sections=[entry("45510", meetings=[
        {"days": ["Monday"], "room": "101"}])])), "schema_error"),
    ("waitlist as a string", json.dumps(body(sections=[entry("41909", waitlist="yes")])), "schema_error"),
    ("extra body field", json.dumps(body(total_credits=6)), "schema_error"),
]


class MessageSchemaTests(unittest.TestCase):
    def test_every_message_lands_in_its_tier(self):
        for name, raw, expected in CASES:
            with self.subTest(name):
                verdict = validate_message(raw)
                self.assertEqual(verdict["outcome"], expected, "; ".join(verdict["errors"]))


if __name__ == "__main__":
    unittest.main()
