"""
didcomm_schema.py
------------------
Tiers 1-2 for the registration CART message (README.md).
Kept in its OWN file per team convention (new work = new file);
registration.evaluate() calls it.

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
    return Draft202012Validator({"$ref": f"#/$defs/{def_name}", "$defs": SCHEMA["$defs"]})


_ENVELOPE = _validator("envelope")
_REGISTER = _validator("register")

# Goes in TaskSpec.output_contract. The example uses a student and sections
# that appear in no test case, so a correct answer requires ADAPTING the
# shape -- not copying the example (same rule as the original run_registration.py).
OUTPUT_CONTRACT = (
    "Respond with one DIDComm v2 plaintext message that registers the student "
    "for every section in a single request. "
    f'"type" MUST be exactly "{MESSAGE_TYPE}". '
    '"id" MUST be a unique string such as a UUID. '
    'The body MUST have exactly these fields: {"student_id": <string>, "term_code": <string>, '
    '"sections": [{"crn": <string>}, ...]}, with at least one section and no crn twice. '
    'A section entry MAY also have "credit_hours": <number> (REQUIRED when the section is '
    'variable-credit: the credits the student takes) and "waitlist": true (ONLY to join the '
    'waitlist of a full section). '
    "Example for an unrelated request: "
    f'{{"id": "3f2b8c1e-7a4d-4e9b-9c55-0d1e2f3a4b5c", "type": "{MESSAGE_TYPE}", '
    '"body": {"student_id": "S042", "term_code": "201920", "sections": [{"crn": "90512"}, '
    '{"crn": "90877", "credit_hours": 2}, {"crn": "91004", "waitlist": true}]}}'
)


# Added to OUTPUT_CONTRACT for input-format cases, where a section is named by its crn.
CRN_CONTRACT = (
    ' Each "crn" MUST be the crn of an offered section, "term_code" MUST be the '
    'schedule\'s term_code, and "student_id" MUST be the student\'s student_id.'
)


def _errors(validator: Draft202012Validator, message) -> list:
    """Every violation as '<path>: <reason>', in a stable order."""
    found = sorted(validator.iter_errors(message), key=lambda e: list(map(str, e.absolute_path)))
    return [f"{'/'.join(map(str, e.absolute_path)) or '<message>'}: {e.message}" for e in found]


def validate_message(raw: str) -> dict:
    """
    raw: the model's output, verbatim.

    Returns {"outcome": "syntactic_error" | "schema_error" | "schema_ok",
             "detail": str, "errors": [str], "body": dict | None}
    body is set only on schema_ok; pass it on to the consistency check.
    """
    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        return {"outcome": "syntactic_error", "detail": f"invalid JSON ({e})",
                "errors": ["<message>: not valid JSON"], "body": None}

    errors = _errors(_ENVELOPE, message)
    if errors:
        return {"outcome": "syntactic_error",
                "detail": f"not a DIDComm plaintext message: {errors}",
                "errors": errors, "body": None}

    errors = _errors(_REGISTER, message)
    if not errors:                       # JSON Schema cannot say "no crn twice" for objects
        crns = [item["crn"] for item in message["body"]["sections"]]
        errors = [f"body/sections: crn {crn} is listed more than once"
                  for crn in sorted({c for c in crns if crns.count(c) > 1})]
    if errors:
        return {"outcome": "schema_error",
                "detail": f"not a valid register message: {errors}",
                "errors": errors, "body": None}

    return {"outcome": "schema_ok", "detail": "valid register message",
            "errors": [], "body": message["body"]}


if __name__ == "__main__":
    # Smoke test: one message per way of failing, plus two that pass.
    good = {"id": "a1", "type": MESSAGE_TYPE,
            "body": {"student_id": "S001", "term_code": "202640",
                     "sections": [{"crn": "41836"}, {"crn": "41909"}]}}

    def variant(**changes):
        return {**good, **changes}

    def body(**changes):
        return variant(body={**good["body"], **changes})

    cases = [
        ("minimal valid cart", json.dumps(good), "schema_ok"),
        ("credit_hours and waitlist", json.dumps(body(sections=[
            {"crn": "45510", "credit_hours": 3}, {"crn": "41909", "waitlist": True}])), "schema_ok"),
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
        ("no term_code", json.dumps(variant(body={"student_id": "S001", "sections": [{"crn": "41836"}]})), "schema_error"),
        ("term_code as a number", json.dumps(body(term_code=202640)), "schema_error"),
        ("empty cart", json.dumps(body(sections=[])), "schema_error"),
        ("same crn twice", json.dumps(body(sections=[{"crn": "41836"}, {"crn": "41836", "waitlist": True}])), "schema_error"),
        ("section entry without crn", json.dumps(body(sections=[{"section_id": "41836"}])), "schema_error"),
        ("unknown field in a section", json.dumps(body(sections=[{"crn": "41836", "grade_mode": "P/F"}])), "schema_error"),
        ("credit_hours as a string", json.dumps(body(sections=[{"crn": "45510", "credit_hours": "3"}])), "schema_error"),
        ("waitlist as a string", json.dumps(body(sections=[{"crn": "41909", "waitlist": "yes"}])), "schema_error"),
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
