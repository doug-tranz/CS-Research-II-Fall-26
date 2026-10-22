"""
didcomm_schema.py
------------------
Tiers 1-2 for the registration CART message (docs/message-format.md).
Kept in its OWN file per team convention (new work = new file);
checker.evaluate() calls it.

The rules live in registration_message.schema.json (JSON Schema 2020-12),
not in this file. This module only loads that schema and maps a failure
to the right tier:

  Tier 1 (syntactic): not JSON, or not a DIDComm v2 plaintext message
                      -> schema's $defs/envelope
  Tier 2 (schema):    a DIDComm message, but not OUR register message
                      -> schema's $defs/register (type URI + body shape)

USAGE:
    from didcomm_schema import validate_message, OUTPUT_CONTRACT
    verdict = validate_message(raw_output)
    if verdict["outcome"] == "schema_ok":
        body = verdict["body"]      # {"student_id": ..., "sections": [...]}

Requires: pip install jsonschema  (listed in requirements.txt)
"""

import json
from pathlib import Path
from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).with_name("registration_message.schema.json")
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
MESSAGE_TYPE = SCHEMA["$defs"]["register"]["properties"]["type"]["const"]

Draft202012Validator.check_schema(SCHEMA)


def _validator(def_name: str) -> Draft202012Validator:
    """A validator for one entry of the schema's $defs (envelope or register)."""
    return Draft202012Validator({"$ref": f"#/$defs/{def_name}", "$defs": SCHEMA["$defs"]})


_ENVELOPE = _validator("envelope")
_REGISTER = _validator("register")

# Goes in TaskSpec.output_contract. The example uses a student and sections
# that appear in no test case, so a correct answer requires ADAPTING the
# shape -- not copying the example.
SECTION_FIELDS = ("crn", "course_id", "section", "credit_hours", "seats_total", "seats_taken",
                  "campus", "modality", "meetings")
_EXAMPLE = {
    "id": "3f2b8c1e-7a4d-4e9b-9c55-0d1e2f3a4b5c", "type": MESSAGE_TYPE,
    "body": {"student_id": "S042", "term_code": "201920", "sections": [
        {"crn": "90512", "course_id": "HIST01150", "section": "2", "credit_hours": 3,
         "seats_total": 35, "seats_taken": 20, "campus": "Glassboro", "modality": "in-person",
         "meetings": [{"days": ["Tuesday", "Thursday"], "start_time": "9:30 AM",
                       "end_time": "10:45 AM", "type": "Class", "location": "Bunce Hall 101"}]},
        {"crn": "90877", "course_id": "ART09900", "section": "1", "credit_hours": 2,
         "seats_total": 5, "seats_taken": 1, "campus": "Glassboro", "modality": "online",
         "meetings": []},
        {"crn": "91004", "course_id": "BIOL01120", "section": "4", "credit_hours": 4,
         "seats_total": 24, "seats_taken": 24, "campus": "Camden", "modality": "hybrid",
         "meetings": [{"days": ["Monday"], "start_time": "1:00 PM", "end_time": "3:50 PM"}],
         "waitlist": True}]}}

OUTPUT_CONTRACT = (
    "Respond with one DIDComm v2 plaintext message that registers the student "
    "for every section in a single request. "
    f'"type" MUST be exactly "{MESSAGE_TYPE}". '
    '"id" MUST be a unique string such as a UUID. '
    'The body MUST have exactly these fields: {"student_id": <string>, "term_code": <string>, '
    '"sections": [<section entry>, ...]}, with at least one section and no crn twice. '
    'Each section entry MUST have exactly these fields, copied from the offered section: '
    '"crn", "course_id", "section", "seats_total", "seats_taken", "campus", "modality", '
    '"meetings" (each meeting\'s "days", and its "start_time" and "end_time" when it has them; '
    '"type" and "location" may be copied too), plus "credit_hours": <number>, the credits the '
    "student takes (the section's credits, or a number in the range of a variable-credit "
    'section). An entry MAY also have "waitlist": true (ONLY to join the waitlist of a full '
    "section). Example for an unrelated request: " + json.dumps(_EXAMPLE)
)


# Added to OUTPUT_CONTRACT for input-format cases, where a section is named by its crn.
CRN_CONTRACT = (
    ' Each "crn" MUST be the crn of an offered section, and every other copied field MUST '
    'equal that section\'s value. "term_code" MUST be the schedule\'s term_code, and '
    '"student_id" MUST be the student\'s student_id.'
)


def _errors(validator: Draft202012Validator, message) -> list:
    """Every violation as '<path>: <reason>', in a stable order."""
    found = sorted(validator.iter_errors(message), key=lambda e: list(map(str, e.absolute_path)))
    return [f"{'/'.join(map(str, e.absolute_path)) or '<message>'}: {e.message}" for e in found]


def _check(requires: str, errors: list, found_ok: str) -> dict:
    """One Tier 1-2 check for a verdict's tier1_checks / tier2_checks."""
    return {"requires": requires, "found": "; ".join(errors) if errors else found_ok, "passed": not errors}


# Tier 2's schema errors, sorted into checks by where in the message they are.
_REGISTER_PARTS = (
    ("type", f'"type" is exactly "{MESSAGE_TYPE}"',
     lambda path: path == "type"),
    ("body", 'the body has exactly "student_id" (string), "term_code" (string) and "sections" (a non-empty list)',
     lambda path: path.startswith("body") and not path.startswith("body/sections/")),
    ("sections", "every section entry has exactly the copied fields (crn, course_id, section, credit_hours, "
                 "seats, campus, modality, meetings), each well-formed (full day names, 12-hour times)",
     lambda path: path.startswith("body/sections/")),
)


def validate_message(raw: str) -> dict:
    """
    raw: the model's output, verbatim.

    Returns {"outcome": "syntactic_error" | "schema_error" | "schema_ok",
             "detail": str, "errors": [str], "body": dict | None,
             "tier1_checks": [...], "tier2_checks": [...]}
    body is set only on schema_ok; pass it on to the consistency check. Each check is
    {"check", "requires", "found", "passed"}; a tier that was not reached has no checks.
    """
    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        return {"outcome": "syntactic_error", "detail": f"invalid JSON ({e})",
                "errors": ["<message>: not valid JSON"], "body": None,
                "tier1_checks": [{"check": "json", **_check("the whole answer is one JSON value, with no prose or "
                                                            "code fence around it", [f"invalid JSON ({e})"], "")}],
                "tier2_checks": []}
    tier1 = [{"check": "json", **_check("the whole answer is one JSON value, with no prose or code fence around it",
                                        [], "valid JSON")}]

    errors = _errors(_ENVELOPE, message)
    tier1.append({"check": "envelope", **_check(
        'a DIDComm v2 plaintext message: an object with an "id" string, a "type" message-type URI and a '
        '"body" object; any optional headers (typ, from, to, created_time) well-formed',
        errors, "valid DIDComm envelope")})
    if errors:
        return {"outcome": "syntactic_error",
                "detail": f"not a DIDComm plaintext message: {errors}",
                "errors": errors, "body": None, "tier1_checks": tier1, "tier2_checks": []}

    errors = _errors(_REGISTER, message)
    paths = [(e.split(":", 1)[0], e) for e in errors]
    tier2 = [{"check": name, **_check(requires, [e for path, e in paths if belongs(path)], "as required")}
             for name, requires, belongs in _REGISTER_PARTS]
    rest = [e for path, e in paths if not any(belongs(path) for _, _, belongs in _REGISTER_PARTS)]
    if rest:
        tier2.append({"check": "other", **_check("nothing else in the message breaks the register schema",
                                                 rest, "as required")})
    if not errors:                       # JSON Schema cannot say "no crn twice" for objects
        crns = [item["crn"] for item in message["body"]["sections"]]
        errors = [f"body/sections: crn {crn} is listed more than once"
                  for crn in sorted({c for c in crns if crns.count(c) > 1})]
        tier2.append({"check": "unique_crns", **_check("no crn appears twice in the cart", errors,
                                                       f"{len(crns)} distinct crn{'' if len(crns) == 1 else 's'}")})
    if errors:
        return {"outcome": "schema_error",
                "detail": f"not a valid register message: {errors}",
                "errors": errors, "body": None, "tier1_checks": tier1, "tier2_checks": tier2}

    return {"outcome": "schema_ok", "detail": "valid register message",
            "errors": [], "body": message["body"], "tier1_checks": tier1, "tier2_checks": tier2}


if __name__ == "__main__":
    # Smoke test: one message per way of failing, plus a few that pass.
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
        return {**good, **changes}

    def body(**changes):
        return variant(body={**good["body"], **changes})

    cases = [
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

    failed = 0
    for name, raw, expected in cases:
        verdict = validate_message(raw)
        mark = "ok  " if verdict["outcome"] == expected else "FAIL"
        failed += verdict["outcome"] != expected
        print(f"{mark} {name:30s} {verdict['outcome']:16s} | {'; '.join(verdict['errors'])}")
    print(f"\n{len(cases) - failed}/{len(cases)} as expected.")
    raise SystemExit(1 if failed else 0)
