#!/usr/bin/env python3
"""Build a conservative main-compatible review set from Tyler's original cases.

Usage from the repository root:
  python prepare_tyler_review_cases.py
  python run_semantic_eval.py --test-file tyler_supplemental_tests/tyler_review_cases.json --check

No LLM calls, API keys, or repository modifications occur beyond creating the output folder.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

REPOSITORY = "doug-tranz/CS-Research-II-Fall-26"
SOURCE_COMMIT = "33c66f7ea4baf402cb669d211d0df6e051bef30e"
TARGET_MAIN_COMMIT_AT_REVIEW = "7ae8fac790fc2333f9c23c72300eac9b99912a0d"
SOURCE_PREFIX = "Tyler_branch_ready_semantic_tests/semantic_tests"
SOURCE_FILES = (
    "semantic_cases_daquan_aneesh.jsonl",
    "CSR2_Develop_Test_Cases_Prototype/csr2_reasoning_cases_12.jsonl",
    "d_category_cases_12.jsonl",
    "generated_seed2026.jsonl",
)
SELECTED_IDS = (
    "SEM-003", "SEM-004", "SEM-005", "SEM-006", "SEM-007", "SEM-014",
    "SEM-015", "SEM-018",
    "RC-007", "RC-008",
    "DAY-D1-001", "DAY-D1-002", "DAY-D1-007", "DAY-D1-008",
    "DAY-D2-003", "DAY-D2-009",
    "DAY-D3-005", "DAY-D3-006", "DAY-D3-011", "DAY-D3-012",
    "DYN-0003", "DYN-0004", "DYN-0005", "DYN-0006",
    "DYN-0007", "DYN-0013", "DYN-0016",
)
PAIR_GROUPS = (
    ("SEM-003", "SEM-004", "modality"),
    ("SEM-005", "SEM-006", "weekday"),
    ("RC-007", "RC-008", "busy"),
    ("DAY-D1-001", "DAY-D1-002", "weekday"),
    ("DAY-D1-007", "DAY-D1-008", "weekday"),
    ("DYN-0003", "DYN-0004", "modality"),
    ("DYN-0005", "DYN-0006", "weekday"),
)
MODALITY = {"SEM-003", "SEM-004", "SEM-015", "DYN-0003", "DYN-0004", "DYN-0013"}
WEEKDAYS = {"SEM-005", "SEM-006", "DAY-D1-001", "DAY-D1-002",
            "DAY-D1-007", "DAY-D1-008", "DAY-D2-003", "DAY-D2-009",
            "DYN-0005", "DYN-0006"}
FRIDAY_EXCLUSION = {"DAY-D3-005", "DAY-D3-006", "DAY-D3-011", "DAY-D3-012"}
MORNING = {"SEM-007", "DYN-0007"}
COMPOUND = {"SEM-018", "DYN-0016"}
BUSY = {"RC-007", "RC-008"}
EXPLICIT_COURSE = {"SEM-014"}
WEEK = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ALLOWED_RULES = {"count", "credits", "courses_count_toward", "no_days",
                 "starts_before", "ends_by", "modality_in", "must_include_courses",
                 "courses_in", "busy"}


def get_source(path: str, repo_root: Path, source_ref: str, offline: bool) -> str:
    # Prefer the locally fetched branch/commit, so no Internet is needed.
    if (repo_root / ".git").exists():
        for ref in (SOURCE_COMMIT, source_ref):
            proc = subprocess.run(
                ["git", "-C", str(repo_root), "show", f"{ref}:{path}"],
                capture_output=True, text=True, encoding="utf-8",
            )
            if proc.returncode == 0:
                return proc.stdout
    if offline:
        raise RuntimeError(f"Cannot read {path}; fetch the Tyler branch first or remove --offline")
    # Public source; the SHA pins the inputs so the generated dataset is reproducible.
    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{SOURCE_COMMIT}/{path}"
    with urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8")


def minutes(time: str) -> int:
    match = re.fullmatch(r"(\d{1,2}):(\d{2}) (AM|PM)", time)
    if match is None:
        raise ValueError(f"Invalid time: {time!r}")
    hour, minute, meridian = match.groups()
    return (int(hour) % 12 + (12 if meridian == "PM" else 0)) * 60 + int(minute)


def rule_accepts_section(section: dict, rules: dict) -> bool:
    """Subset of current Tier 4 used to audit the conversion; not the official runner."""
    if "modality_in" in rules and section["modality"] not in rules["modality_in"]:
        return False
    if "courses_in" in rules and section["course_id"] not in rules["courses_in"]:
        return False
    if "no_days" in rules and any(
        day in rules["no_days"]
        for meeting in section["meetings"] for day in meeting["days"]
    ):
        return False
    if "starts_before" in rules and any(
        minutes(meeting["start_time"]) >= minutes(rules["starts_before"])
        for meeting in section["meetings"] if "start_time" in meeting
    ):
        return False
    if "busy" in rules and any(
        set(meeting["days"]) & set(block["days"])
        and "start_time" in meeting
        and minutes(meeting["start_time"]) < minutes(block["end_time"])
        and minutes(block["start_time"]) < minutes(meeting["end_time"])
        for meeting in section["meetings"] for block in rules["busy"]
    ):
        return False
    return True


def derive_rules(case_id: str, good_sections: list[dict]) -> dict:
    rules: dict = {"count": {"min": 1, "max": 1}}
    if case_id in MODALITY:
        rules["modality_in"] = [good_sections[0]["modality"]]
    elif case_id in WEEKDAYS:
        chosen_days = {d for s in good_sections for m in s["meetings"] for d in m["days"]}
        rules["no_days"] = [d for d in WEEK if d not in chosen_days]
    elif case_id in FRIDAY_EXCLUSION:
        rules["no_days"] = ["Friday"]
    elif case_id in MORNING:
        rules["starts_before"] = "12:00 PM"
    elif case_id in COMPOUND:
        rules["modality_in"] = ["in-person"]
        rules["starts_before"] = "12:00 PM"
    elif case_id in BUSY:
        start, end = (("10:30 AM", "11:30 AM") if case_id == "RC-007"
                      else ("12:30 PM", "1:30 PM"))
        rules["busy"] = [{"days": ["Wednesday"], "start_time": start, "end_time": end}]
    elif case_id in EXPLICIT_COURSE:
        rules["courses_in"] = [good_sections[0]["course_id"]]
    else:
        raise ValueError(f"Case lacks a faithful rule conversion: {case_id}")
    assert set(rules) <= ALLOWED_RULES
    return rules


def convert(source: list[dict]) -> tuple[list[dict], dict]:
    cases_by_id: dict[str, dict] = {}
    for case in source:
        if case["case_id"] in cases_by_id:
            raise ValueError(f"Duplicate source case_id {case['case_id']}")
        cases_by_id[case["case_id"]] = case

    pair_meta: dict[str, dict] = {}
    for a, b, family in PAIR_GROUPS:
        pair_id = f"{family}/tyler-{a.lower()}-{b.lower()}"
        for case_id, variant in ((a, "a"), (b, "b")):
            pair_meta[case_id] = {
                "id": pair_id,
                "variant": variant,
                "changed": "single explicit constraint in request",
            }

    result: list[dict] = []
    for case_id in SELECTED_IDS:
        original = cases_by_id[case_id]
        data = copy.deepcopy(original["input"])
        if data["schedule"]["term"] != "Fall 2026":
            raise ValueError(f"Unexpected term for {case_id}")
        data["schedule"]["term_code"] = "202640"
        by_crn = {s["crn"]: s for s in data["schedule"]["sections"]}
        if len(by_crn) != len(data["schedule"]["sections"]):
            raise ValueError(f"Repeated CRN in {case_id}")

        good = original["ground_truth"]["acceptable_crns"]
        stated_bad = original["ground_truth"]["wrong_but_consistent_crns"]
        if len(good) != 1:
            raise ValueError(f"Expected one correct section in {case_id}")
        if not all(crn in by_crn for crn in good + stated_bad):
            raise ValueError(f"Answer key refers to absent CRN in {case_id}")

        rules = derive_rules(case_id, [by_crn[c] for c in good])
        for s in data["schedule"]["sections"]:
            accepted = rule_accepts_section(s, rules)
            expected = s["crn"] in good
            if accepted != expected:
                raise ValueError(
                    f"Unfaithful conversion {case_id}: CRN {s['crn']} "
                    f"accepted={accepted} expected={expected}"
                )

        truth = {
            "rules": rules,
            "examples": {
                "valid": [[crn] for crn in good],
                "invalid": [[s["crn"]] for s in data["schedule"]["sections"]
                            if s["crn"] not in good],
            },
            "implicit_relationship": original["ground_truth"].get(
                "reason", f"Adapted from {case_id}"
            ),
        }
        if case_id in pair_meta:
            truth["pair"] = pair_meta[case_id]

        result.append({
            "case_id": f"tyler-{case_id.lower()}",
            "input": data,
            "ground_truth": truth,
            "review_status": "candidate_needs_human_review",
            "source_case_id": case_id,
            "source_branch": "Tyler",
            "source_file": f"{SOURCE_PREFIX}/{original['_source_file']}",
            "synthetic": True,
        })

    by_original = {case["source_case_id"]: case for case in result}
    for a, b, _ in PAIR_GROUPS:
        left, right = by_original[a], by_original[b]
        a_input, b_input = copy.deepcopy(left["input"]), copy.deepcopy(right["input"])
        a_prompt, b_prompt = a_input.pop("prompt"), b_input.pop("prompt")
        if a_input != b_input or a_prompt == b_prompt:
            raise ValueError(f"Source pair is not a single prompt reversal: {a}/{b}")
        a_good = left["ground_truth"]["examples"]["valid"][0][0]
        b_good = right["ground_truth"]["examples"]["valid"][0][0]
        a_by = {s["crn"]: s for s in left["input"]["schedule"]["sections"]}
        b_by = {s["crn"]: s for s in right["input"]["schedule"]["sections"]}
        if rule_accepts_section(a_by[b_good], left["ground_truth"]["rules"]):
            raise ValueError(f"Left member accepts twin's correct answer: {a}/{b}")
        if rule_accepts_section(b_by[a_good], right["ground_truth"]["rules"]):
            raise ValueError(f"Right member accepts twin's correct answer: {a}/{b}")

    selected_set = set(SELECTED_IDS)
    manifest = {
        "status": "candidate_needs_human_review",
        "description": "Supplementary, synthetic cases using current run_semantic_eval format",
        "source_repo": REPOSITORY,
        "source_commit": SOURCE_COMMIT,
        "main_commit_reviewed": TARGET_MAIN_COMMIT_AT_REVIEW,
        "source_cases_count": len(source),
        "converted_cases_count": len(result),
        "deferred_cases_count": len(source) - len(result),
        "counterfactual_pairs_count": len(PAIR_GROUPS),
        "selected_original_ids": list(SELECTED_IDS),
        "deferred_original_ids": [
            c["case_id"] for c in source if c["case_id"] not in selected_set
        ],
        "limitations": [
            "Pending human confirmation of label semantics.",
            "Synthetic CRNs, courses, and schedules—not official Section Tally data.",
            "Rule discrimination checked locally; run the main Python --check next.",
            "Conceptual overlap with main's pre-existing paired families remains possible.",
            "Excluded cases require additional semantic rules or further review.",
        ],
    }
    return result, manifest


def write_package(cases: list[dict], manifest: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "tyler_review_cases.json").write_text(
        json.dumps(cases, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (output_dir / "review_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    readme = f"""# Tyler supplementary semantic tests — human review candidate

**NOT FINALIZED. NOT A PUBLICATION-READY BENCHMARK OR LLM SCORE.**

This folder contains {len(cases)} synthetic candidate cases and {len(PAIR_GROUPS)}
prompt-reversal pairs converted from Tyler's {manifest['source_cases_count']} original
cases to the format expected by current main's `run_semantic_eval.py`.
The other {manifest['deferred_cases_count']} cases are listed in `review_manifest.json`.

The converted cases are a separate JSON array, not the older JSONL format.
Each Fall 2026 input includes `schedule.term_code = "202640"` and each
answer key contains `ground_truth.rules` and valid/invalid example carts.
The source cases were not overwritten.

## REQUIRED — no API calls or LLM cost
From the repository root, on a branch created from the latest main:

    python run_semantic_eval.py --test-file tyler_supplemental_tests/tyler_review_cases.json --check

Accept this package only if the checker reports
`{len(cases)}/{len(cases)} cases sound` and `{len(PAIR_GROUPS)}/{len(PAIR_GROUPS)} pairs sound`.

## Integration cautions
- The input sections are **synthetic**, not taken from the official Fall 2026 Section Tally.
  Keep the file separate from the main real-tally `paired_cases.json`.
- The grader's existing supported rules can distinguish each offered candidate
  in these converted cases. Cases whose intent cannot be represented faithfully
  (campus choice, CRN exclusion, some afternoon/Boolean or conditional rules)
  are deferred, rather than silently mis-scored.
- Some reasoning families are already tested by `main`. Team members should
  review overlap before using these as additional reported benchmark cases.
- Ground truth remains **candidate_needs_human_review**. Review labels, then
  run the actual official pipeline before presenting any model metrics.
- The repository has **not** been modified by this package.

## Files
- `tyler_review_cases.json` — selected converted candidate cases
- `review_manifest.json` — provenance, coverage, exclusions, limitations
"""
    (output_dir / "README.md").write_text(readme, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(),
                        help="Path to clone of CS-Research-II-Fall-26 (default: current directory)")
    parser.add_argument("--source-ref", default="origin/Tyler",
                        help="Git ref to read the source from before trying pinned commit")
    parser.add_argument("--output-dir", type=Path, default=Path("tyler_supplemental_tests"))
    parser.add_argument("--offline", action="store_true",
                        help="Require source in local git; never download")
    parser.add_argument("--skip-python-check", action="store_true",
                        help="Do not run the repository's no-cost --check automatically")
    args = parser.parse_args()

    root = args.repo_root.resolve()
    output_dir = args.output_dir if args.output_dir.is_absolute() else root / args.output_dir
    all_cases = []
    for rel in SOURCE_FILES:
        content = get_source(f"{SOURCE_PREFIX}/{rel}", root, args.source_ref, args.offline)
        for line in content.splitlines():
            if line.strip():
                case = json.loads(line)
                case["_source_file"] = rel
                all_cases.append(case)
    cases, manifest = convert(all_cases)
    if len(all_cases) != 84 or len(cases) != 27:
        raise RuntimeError("Unexpected source case count; inspect source revision")
    write_package(cases, manifest, output_dir)
    print(f"Prepared {len(cases)} candidate cases and {len(PAIR_GROUPS)} pairs in {output_dir}")
    print(f"Deferred {len(all_cases) - len(cases)} source cases; see review_manifest.json")

    runner = root / "run_semantic_eval.py"
    if runner.is_file() and not args.skip_python_check:
        print("Running official no-cost answer-key check...")
        proc = subprocess.run(
            [sys.executable, str(runner), "--test-file",
             str(output_dir / "tyler_review_cases.json"), "--check"],
            cwd=root,
        )
        if proc.returncode:
            print("Official runner check FAILED; do not submit until resolved.", file=sys.stderr)
        return proc.returncode
    print("Run official check after placing package in the repository:")
    print("python run_semantic_eval.py --test-file "
          "tyler_supplemental_tests/tyler_review_cases.json --check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
