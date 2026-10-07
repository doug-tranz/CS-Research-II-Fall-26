"""
semantic_check.py
------------------
Fills the Tier 4 stub in registration.py's evaluate() (marked §13.1,
"consistent__semantic_TBD"). Kept in its OWN file per team convention
(new work = new file) so it never touches or overwrites registration.py
or harness.py directly.

THE GAP THIS CLOSES:
Tier 3 (consistency, in registration.py) only checks database invariants:
does the section exist, is there a seat, no time conflict, not already
enrolled. It never checks whether the model chose the CORRECT student or
section for what the task's `intent` actually asked for. A response can
pass every consistency check while enrolling the right-shaped, DB-valid,
but simply WRONG section. That's what this tier catches.

HOW: compares the model's output body against GroundTruth.expected (a
human-verified, known-correct DIDComm message for that exact task_id).
Per harness.py's own design, GroundTruth is never shown to the model, so
this comparison is legitimate — it only happens here, after generation.
"""

import json
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))   # harness.py is in the repo root
from harness import parse_didcomm


def check_semantic(body: dict, expected_raw: str) -> dict:
    """
    body: the model's parsed DIDComm body (already passed schema + consistency)
    expected_raw: GroundTruth.expected — a raw DIDComm JSON string for this task_id

    Returns {"outcome": "semantic_ok" | "semantic_error", "detail": str,
             "deviation_score": float | None}
    deviation_score: fraction of expected fields that didn't match (0.0 = perfect).
    """
    if not expected_raw:
        return {
            "outcome": "semantic_error",
            "detail": "no ground truth available for this task_id — cannot score intent",
            "deviation_score": None,
        }

    expected_body, err = parse_didcomm(expected_raw)
    if expected_body is None:
        return {
            "outcome": "semantic_error",
            "detail": f"ground truth itself failed to parse: {err}",
            "deviation_score": None,
        }

    mismatches = {}
    for key, expected_value in expected_body.items():
        actual_value = body.get(key)
        if str(actual_value).strip().lower() != str(expected_value).strip().lower():
            mismatches[key] = {"expected": expected_value, "actual": actual_value}

    deviation_score = len(mismatches) / max(len(expected_body), 1)

    if mismatches:
        return {
            "outcome": "semantic_error",
            "detail": f"field mismatch vs. ground truth: {mismatches}",
            "deviation_score": deviation_score,
        }

    return {
        "outcome": "semantic_ok",
        "detail": "matches ground truth exactly",
        "deviation_score": 0.0,
    }


if __name__ == "__main__":
    # Smoke test: model picks a real, valid, but WRONG section.
    # This is exactly the failure mode Tier 3 alone cannot catch.
    expected = json.dumps({
        "type": "registration/enroll", "id": "irrelevant-for-semantics",
        "body": {"student_id": "alice", "section_id": "CS401-A"},
    })
    model_body = {"student_id": "alice", "section_id": "CS401-B"}  # valid section, wrong one

    result = check_semantic(model_body, expected)
    print(json.dumps(result, indent=2))
