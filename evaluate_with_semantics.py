"""
evaluate_with_semantics.py
---------------------------
Wraps registration.evaluate() (Tiers 1-3, untouched) and adds Tier 4
(semantic_check.py) on top, ONLY when Tiers 1-3 already passed. This file
does not modify registration.py or harness.py — it just imports and calls
them, per the team's "new work = new file" rule.

USAGE:
    from evaluate_with_semantics import evaluate_full
    verdict = evaluate_full(conn, raw_output, expected_raw)

`expected_raw` is the GroundTruth.expected DIDComm JSON string for this
exact task_id — this needs to come from the team's hidden test-case file
(the input/expected-output pairs someone is building this week). Without
it, Tier 4 correctly reports it has nothing to check against.
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
    conn = reg.make_db("""
        INSERT INTO sections VALUES ('CS401-A', 'CS401', 30, 5, 'MW09');
        INSERT INTO sections VALUES ('CS401-B', 'CS401', 30, 5, 'TR13');
    """)

    # Case 1: model enrolls the RIGHT section -> should be semantic_ok
    raw_correct = '{"type": "registration/enroll", "id": "abc", "body": {"student_id": "alice", "section_id": "CS401-A"}}'
    expected = '{"type": "registration/enroll", "id": "n/a", "body": {"student_id": "alice", "section_id": "CS401-A"}}'
    print("Case 1 (correct section):")
    print(evaluate_full(conn, raw_correct, expected))
    print()

    # Case 2: model enrolls a DIFFERENT, still-valid section -> the exact bug Tier 3 misses
    conn2 = reg.make_db("""
        INSERT INTO sections VALUES ('CS401-A', 'CS401', 30, 5, 'MW09');
        INSERT INTO sections VALUES ('CS401-B', 'CS401', 30, 5, 'TR13');
    """)
    raw_wrong_section = '{"type": "registration/enroll", "id": "abc", "body": {"student_id": "alice", "section_id": "CS401-B"}}'
    print("Case 2 (wrong but valid section -- the real bug):")
    print(evaluate_full(conn2, raw_wrong_section, expected))
