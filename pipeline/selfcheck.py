"""
selfcheck.py
------------
--check: proves every case's answer key behaves before any money is spent on it. No model
call. Each case's valid example carts must score semantic_ok and its invalid ones must not,
graded by the same code that grades a model's answer; then every pair must be sound
(pairs.check_pairs()).
"""
from .cases import expected_message, validate_case
from .checker import seed_db
from .pairs import check_pairs, pairs_of
from .scoring import evaluate_case


def self_check(case: dict) -> list:
    """Scores a case's own answer key, with no model. Returns problems; empty means it behaves."""
    problems = validate_case(case)
    if problems:
        return problems
    truth = case["ground_truth"]
    conn = seed_db(case["input"])                  # one database for every example
    for cart in truth.get("examples", {}).get("valid", []):
        verdict = evaluate_case(case, expected_message(case, cart), conn)
        if verdict["outcome"] != "semantic_ok":
            problems.append(f"valid example {cart} scores {verdict['outcome']}: {verdict['detail']}")
    for cart in truth.get("examples", {}).get("invalid", []):
        if evaluate_case(case, expected_message(case, cart), conn)["outcome"] == "semantic_ok":
            problems.append(f"invalid example {cart} scores semantic_ok")
    return problems


def check_cases(cases: list) -> int:
    """Scores every case's own answer key and checks every pair. Prints a line per case.
    Returns the failure count."""
    failed = 0
    for case in cases:
        problems = self_check(case)
        failed += bool(problems)
        print(f"{case.get('case_id', '<no id>')}: {'FAIL' if problems else 'ok'}")
        for p in problems:
            print(f"    - {p}")
    print(f"\n{len(cases) - failed}/{len(cases)} cases sound.")
    pairs = pairs_of(cases)
    if pairs:
        problems = check_pairs(cases)
        for p in problems:
            print(f"    - {p}")
        print(f"{len(pairs) - len({p.split(':')[0] for p in problems})}/{len(pairs)} pairs sound.")
        failed += len(problems)
    return failed
