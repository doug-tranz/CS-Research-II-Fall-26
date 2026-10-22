# Results so far and open questions

# Results so far

**Current runs** (in `results/`): `pairs_sonnet_raw.json`, `pairs_sonnet_trimmed.json`, `pairs_sonnet_raw-open.json`
and `pairs_sonnet_trimmed-open.json` hold Claude Sonnet 5.5 at medium effort on the same 3 random
pairs under each tally version (one sample per case); each file's session summary has the totals.

**Earlier runs** (below) are of hand-built cases that no longer exist, before the 10/15 rebuild on
the CS guide; their rows are in git history (`semantic_eval_results_finalized.json` at `bc6c9ba`).

| Model | Case | Sections in prompt | Input tokens | Result | Cost |
|---|---|---|---|---|---|
| `anthropic/claude-opus-5.5` | case_011 | 21 | 7,735 | `semantic_ok` | $0.046 |
| `anthropic/claude-opus-5.5` | case_006 | 97 | 25,649 | `semantic_ok` | $0.125 |
| `anthropic/claude-opus-5.5` | case_009 | 115 | 29,245 | `semantic_ok` | $0.125 |
| `anthropic/claude-opus-5.5` | aneesh_test | 220 | 54,310 | `semantic_ok` | $0.233 |

The section list was 90–97% of every prompt, so cost followed the number of sections.

Findings from removed runs (earlier message body or removed cases), kept because they bear on
open questions:

- **Code fences.** Sonnet 5's one failure on 10/7 was a correct cart wrapped in a code fence
  (open question 2).
- **Temperature 0.** Six Opus runs of one case gave the same cart every time: at temperature 0,
  repeated samples show stability, not spread (design §10).
- **Transfer credit.** In those runs Opus never chose the one acceptable section whose
  prerequisite was met only by transfer credit; the runs don't show why. That option depends
  on the provisional `TA` rule.
- **No separation yet.** Opus 5.5 answered every input-format case it was given correctly
  (22 runs, including the 4 above), so the cases have not yet shown a difference between models.
- **Searching.** In an earlier search mode (since removed; in git history at `45be488`), Opus
  looked up the courses it needed by course id, all in one turn, and answered on the next
  call: 2 model calls and about $0.03–0.04 per case.

The results committed before 10/14 are in git history (`semantic_eval_results.json` at
`e02b9f1`); results for the single-section message are at the same commit in
`old_schema/runs.jsonl`.

# Open questions for the group

1. **Message type URI.** What base URI do we use in place of `example.org`?
2. **Code fences.** A fenced answer is a syntactic error. Do we keep that, or strip fences
   before parsing and count the model as correct? This already decided one of ten results.
3. **Sender identity.** Should the student be the DIDComm `from` header (which needs DIDs in
   the input) in place of `body.student_id`?
4. **Unknown headers.** They are ignored, per DIDComm. Do we want to count them as a
   measured deviation anyway?
5. **Drops and swaps.** Is the scope register-only, or do we need message types for dropping
   or swapping a section?
6. **How strictly the copied section fields are checked.** Every entry must now repeat the
   section's details (`course_id`, `section`, `credit_hours`, seats, `campus`, `modality`,
   `meetings`). Tier 3 ignores case, day order and a leading zero, and lets a meeting's
   `type` and `location` be left out. Should a wrong copy be a `consistency_error` (as now)
   or a separate measured deviation, given that it can fail an otherwise right cart?
7. **Waitlisting and time conflicts.** A waitlisted section is still checked for time
   conflicts with the rest of the cart. Is that right, given the student isn't enrolled yet?

The design's own open questions are in [§13](design.md#13-open-questions--for-the-group).

# Provisional (will change; isolated to one spot each)

- DIDComm envelope shape → `harness.parse_didcomm()`
- Time-conflict logic (interval overlap) → `checker.consistency_checks()`
- Input-format open items: prerequisites as a consistency rule, `TA` meeting any minimum, best-attempt GPA → `registration.py` (`missing_prerequisites()`, `meets_minimum()`, `audit()`)
- Tier 4 by rules → `scoring.check_rules()` (`RULES`)
- The columns of the whole tally in the cached opening → `run_semantic_eval.TALLY_FIELDS`
- In-memory SQLite → `checker.make_db()`
- Cart message type URI (`example.org` placeholder) → `registration_message.schema.json`
