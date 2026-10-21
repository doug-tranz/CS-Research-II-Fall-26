import json
from pathlib import Path

CASES_FILE = Path("registration_format_cases.jsonl")
RESULTS_FILE = Path("claude_results.jsonl")


def load_jsonl(filename):
    """Read a JSONL file and return its records."""
    with filename.open("r", encoding="utf-8-sig") as file:
        return [
            json.loads(line)
            for line in file
            if line.strip()
        ]


def evaluate_case(case, result):
    """Compare one Claude response with its expected outcome."""
    case_id = case["case_id"]
    response = result.get("response", {})
    response_type = response.get("type")
    body = response.get("body", {})

    # Semantic registration cases
    if case["case_type"] == "semantic":
        acceptable_crns = case["expected_goal"]["acceptable_crns"]

        if not acceptable_crns:
            if response_type == "registration/error":
                return "PASS", "Correctly rejected; no acceptable CRNs"
            return "FAIL", (
                f"Expected rejection, got {response_type!r}"
            )

        if response_type != "registration/enroll":
            return "FAIL", (
                f"Expected enrollment, got {response_type!r}"
            )

        section_id = str(body.get("section_id", ""))

        if section_id in acceptable_crns:
            return "PASS", f"Selected acceptable section {section_id}"

        return "FAIL", (
            f"Selected {section_id!r}; acceptable CRNs: "
            f"{acceptable_crns}"
        )

    # Input validation cases
    if case["case_type"] == "validation":
        expected = case.get("expected_validation")

        if expected == "invalid":
            if response_type == "registration/error":
                return "PASS", (
                    f"Rejected invalid input; error code: "
                    f"{body.get('code', 'not supplied')}"
                )

            return "FAIL", (
                f"Expected rejection, got {response_type!r}"
            )

        return "REVIEW", f"Unknown expected validation: {expected!r}"

    return "REVIEW", f"Unknown case type: {case['case_type']!r}"


def main():
    cases = load_jsonl(CASES_FILE)
    results = load_jsonl(RESULTS_FILE)

    # Match results to cases by case_id.
    results_by_id = {}

    for result in results:
        case_id = result.get("case_id")

        if case_id in results_by_id:
            raise ValueError(f"Duplicate Claude result: {case_id}")

        results_by_id[case_id] = result

    case_ids = {case["case_id"] for case in cases}

    passed = 0
    failed = 0
    review = 0
    missing = 0

    print("\nCLAUDE REGISTRATION TEST RESULTS")
    print("=" * 65)

    for case in cases:
        case_id = case["case_id"]
        result = results_by_id.get(case_id)

        if result is None:
            status = "MISSING"
            detail = "No Claude result found"
            missing += 1
        else:
            status, detail = evaluate_case(case, result)

            if status == "PASS":
                passed += 1
            elif status == "FAIL":
                failed += 1
            else:
                review += 1

        print(f"{case_id:10} {status:7} {detail}")

    unexpected = set(results_by_id) - case_ids

    print("\n" + "=" * 65)
    print(f"Cases in original suite: {len(cases)}")
    print(f"Claude results received: {len(results)}")
    print(f"PASS: {passed}")
    print(f"FAIL: {failed}")
    print(f"REVIEW: {review}")
    print(f"MISSING: {missing}")

    if unexpected:
        print(f"Unexpected result IDs: {sorted(unexpected)}")

    scored = passed + failed
    if scored:
        print(f"Score: {passed}/{scored} ({passed / scored * 100:.1f}%)")
    else:
        print("Score: not available; no cases were scored")

    print("\nNote: This is a first-pass outcome check, not a full")
    print("validation of every requirement in the specification.")


if __name__ == "__main__":
    main()