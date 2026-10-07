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
"""

import registration as reg
from harness import parse_didcomm
from semantic_check import check_semantic


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
