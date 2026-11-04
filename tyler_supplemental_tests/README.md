# Tyler supplementary semantic tests — human review candidate

**NOT FINALIZED. NOT A PUBLICATION-READY BENCHMARK OR LLM SCORE.**

This folder contains 27 synthetic candidate cases and 7
prompt-reversal pairs converted from Tyler's 84 original
cases to the format expected by current main's `run_semantic_eval.py`.
The other 57 cases are listed in `review_manifest.json`.

The converted cases are a separate JSON array, not the older JSONL format.
Each Fall 2026 input includes `schedule.term_code = "202640"` and each
answer key contains `ground_truth.rules` and valid/invalid example carts.
The source cases were not overwritten.

## REQUIRED — no API calls or LLM cost
From the repository root, on a branch created from the latest main:

    python run_semantic_eval.py --test-file tyler_supplemental_tests/tyler_review_cases.json --check

Accept this package only if the checker reports
`27/27 cases sound` and `7/7 pairs sound`.

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
