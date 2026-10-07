"""
evaluate_with_semantics.py
---------------------------
Wraps registration.evaluate() (Tiers 1-3, untouched) and adds Tier 4
(semantic_check.py) on top, ONLY when Tiers 1-3 already passed. This file
does not modify registration.py or harness.py — it just imports and calls
them, per the team's "new work = new file" rule.

This is the CART version (README.md). The single-section
version is old_schema/evaluate_with_semantics.py.

USAGE:
    from evaluate_with_semantics import evaluate_full
    verdict = evaluate_full(conn, raw_output, expected_raw)

`expected_raw` is the GroundTruth.expected DIDComm JSON string for this
exact task_id — it comes from the team's hidden test-case files
(test_cases.jsonl, semantic_test_cases.jsonl, cart_test_cases.jsonl).
Without it, Tier 4 correctly reports it has nothing to check against.

For a case in the full input format (README.md, "The input format"), use
    verdict = evaluate_case(case, raw_output)
which seeds the database from the case's own input and accepts any of the
carts its answer key lists.
"""

import json
import registration as reg
from harness import parse_didcomm
from semantic_check import check_semantic
from didcomm_schema import MESSAGE_TYPE


def evaluate_full(conn, raw_output: str, expected_raw: str = "") -> dict:
    base = reg.evaluate(conn, raw_output)

    # If it already failed at Tier 1-3, there's nothing more to check —
    # keep the original verdict as-is.
    if base["outcome"] != "consistent__semantic_TBD":
        return base

    body, _ = parse_didcomm(raw_output)
    semantic_result = check_semantic(body, expected_raw)

    return {
        "outcome": semantic_result["outcome"],
        "detail": semantic_result["detail"],
        "deviation_score": semantic_result["deviation_score"],
        "tier3_detail": base["detail"],  # keep the consistency reasoning too
    }


# ── Input-format cases: {"case_id", "input": {...}, "ground_truth": {...}} ──
# The answer key is a SET of valid outcomes. `acceptable_crns` lists crns that are each
# a correct one-section cart; `acceptable_carts` lists whole carts (lists of crns).

def acceptable_carts(case: dict) -> list:
    """Every correct cart for a case, each as a list of crns."""
    truth = case["ground_truth"]
    carts = [[crn] for crn in truth.get("acceptable_crns", [])]
    return carts + [list(cart) for cart in truth.get("acceptable_carts", [])]


def expected_message(case: dict, cart: list) -> str:
    return json.dumps({"id": "n/a", "type": MESSAGE_TYPE, "body": {
        "student_id": case["input"]["student"]["student_id"], "sections": cart}})


def validate_case(case) -> list:
    """Returns a list of problems with an input-format case; empty means it is sound."""
    if not isinstance(case, dict):
        return ["case is not a JSON object"]
    problems = [f"{field} is missing" for field in ("case_id", "input", "ground_truth")
                if field not in case]
    if problems:
        return problems
    problems = [f"input: {p}" for p in reg.validate_input(case["input"])]
    if problems:
        return problems

    carts = acceptable_carts(case)
    if not carts:
        problems.append("ground_truth lists no acceptable_crns or acceptable_carts")
    offered = {s["crn"] for s in case["input"]["schedule"]["sections"]}
    for cart in carts:
        if not cart:
            problems.append("ground_truth has an empty acceptable cart")
        for crn in cart:
            if crn not in offered:
                problems.append(f"ground_truth names crn {crn}, which is not in the schedule")
    return problems


def evaluate_case(case: dict, raw_output: str) -> dict:
    """Tiers 1-4 for an input-format case. Tier 4 passes when the cart equals ANY acceptable cart."""
    verdicts = [evaluate_full(reg.seed_db(case["input"]), raw_output, expected_message(case, cart))
                for cart in acceptable_carts(case)]
    for verdict in verdicts:
        if verdict["outcome"] != "semantic_error":
            return verdict           # semantic_ok, or a Tier 1-3 failure (same for every cart)
    return min(verdicts, key=lambda v: v["deviation_score"])   # the closest acceptable cart


def self_check(case: dict) -> list:
    """Scores a case's own answer key, with no model. Returns problems; empty means it behaves."""
    problems = validate_case(case)
    if problems:
        return problems
    carts = acceptable_carts(case)
    for cart in carts:
        verdict = evaluate_case(case, expected_message(case, cart))
        if verdict["outcome"] != "semantic_ok":
            problems.append(f"acceptable cart {cart} scores {verdict['outcome']}: {verdict['detail']}")
    # When every answer is a single section, no other single section may pass.
    if all(len(cart) == 1 for cart in carts):
        accepted = {cart[0] for cart in carts}
        for s in case["input"]["schedule"]["sections"]:
            if s["crn"] not in accepted:
                verdict = evaluate_case(case, expected_message(case, [s["crn"]]))
                if verdict["outcome"] == "semantic_ok":
                    problems.append(f"crn {s['crn']} is not in the answer key but scores semantic_ok")
    return problems


if __name__ == "__main__":
    # Smoke test using registration.py's own make_db(), no real DB file needed.
    SEED = """
        INSERT INTO sections VALUES ('CS401-A', 'CS401', 30, 5, 'MW09');
        INSERT INTO sections VALUES ('CS401-B', 'CS401', 30, 5, 'TR13');
        INSERT INTO sections VALUES ('MATH152-A', 'MATH152', 30, 5, 'MW09');
        INSERT INTO sections VALUES ('MATH152-B', 'MATH152', 30, 5, 'MW11');
    """
    TYPE = "https://example.org/course-registration/1.0/register"

    def message(sections):
        return ('{"id": "abc", "type": "%s", "body": {"student_id": "alice", "sections": %s}}'
                % (TYPE, str(sections).replace("'", '"')))

    expected = message(["CS401-A", "MATH152-B"])

    # Case 1: model registers the RIGHT cart -> should be semantic_ok
    print("Case 1 (correct cart):")
    print(evaluate_full(reg.make_db(SEED), message(["MATH152-B", "CS401-A"]), expected))
    print()

    # Case 2: a DIFFERENT, still-valid section -> the exact bug Tier 3 misses
    print("Case 2 (wrong but valid section -- the real bug):")
    print(evaluate_full(reg.make_db(SEED), message(["CS401-B", "MATH152-B"]), expected))
    print()

    # Case 3: two sections of the cart meet at the same time -> Tier 3 catches it
    print("Case 3 (time conflict inside the cart):")
    print(evaluate_full(reg.make_db(SEED), message(["CS401-A", "MATH152-A"]), expected))
    print()

    # Case 4: the old single-section body -> Tier 2 catches it
    print("Case 4 (old single-section body):")
    old = '{"id": "abc", "type": "%s", "body": {"student_id": "alice", "section_id": "CS401-A"}}' % TYPE
    print(evaluate_full(reg.make_db(SEED), old, expected))
