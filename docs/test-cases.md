# Test cases and scoring

## Counterfactual pairs (`paired_cases.json`)

A pair is two cases that are the same except for one detail, chosen so the detail flips the
answer. A model that ignores the detail can get at most one of the two right, so **a pair
passes a run only when both variants are `semantic_ok` in that run**. This separates reasoning
from luck better than scoring cases one at a time. The idea comes from the BIRD-style pairs on
the `Tyler` branch (`Tyler_branch_ready_semantic_tests/`: `SEM-001`–`020` and `RC-001`–`012`).
These pairs are rebuilt on this pipeline: real Fall 2026 sections, real degree programs, rules
answer keys, and every candidate cart scored the way a model's answer is scored. Seats are real,
so a pair cannot make a section full. It flips what the student asked for (the prompt) or what
they have taken (the fabricated transcript).

```bash
python generate_pairs.py                                    # adds pairs until paired_cases.json holds 200
python generate_pairs.py --total 250                        # grow it; pairs already there are kept
python generate_pairs.py --annotate                         # only rewrite each pair's `difference` field
python run_semantic_eval.py --test-file paired_cases.json --check
python run_semantic_eval.py --model anthropic/claude-opus-5.5 --test-file paired_cases.json --output pair_results.json
python run_semantic_eval.py --test-file paired_cases.json --output pair_results.json --pair-report
```

| Family | What changes between a and b | Reasoning it needs | Tyler-branch analogue |
|---|---|---|---|
| `full_fallback` | Prompt: the first-choice course has every section full (a) or open seats (b); the fallback is the same | Notice that the first choice is full and apply the fallback | RC-001/002 |
| `in_progress` | Transcript: the named course is in `courses_in_progress` (a), or another course is (b) | Don't register for a course already being taken | RC-005/006 |
| `transfer` | Transcript: the transfer course is one of the requirement's options (a) or a general-education course (b) | Add up credits across completed and transfer courses | RC-003/004 |
| `busy_block` | Prompt: a weekly commitment that starts 30 minutes into one section's meeting vs another's | Real interval overlap, not equal times (`busy` rule) | RC-007/008 |
| `nested_groups` | Transcript: which courses of a `one_of_groups` requirement are done | Evaluate (A and B and C) or (A and B and D) | RC-009/010 |
| `credit_limit` | Prompt: credit limit A + B (a) or A + B − 1 (b) | Credit arithmetic | RC-011/012 |
| `modality` | Prompt: "fully online" vs "in person" | Modality constraint | SEM modality |
| `days` | Prompt: Mondays/Wednesdays vs Tuesdays/Thursdays, in person | Meeting-day constraint | SEM weekday |
| `grade_threshold` | Transcript: the grade is one step below the requirement's minimum (a) or at it (b) | Minimum passing grade | SEM grade_threshold |
| `withdrawal` | Transcript: `W` (a) or `B` (b) | A W earns no credit | SEM withdrawal_credit |
| `late_meeting` | Prompt: "nothing that ends after `<hour>`" (a) or "No `<day>` classes" (b), on a course where one section's first meeting ends in time but a later meeting runs past the hour | Read every meeting of a section, not only the first | none (new) |

**200 pairs (400 cases).** Families take turns adding one pair each, so they stay even: 21 each
for `full_fallback`, `in_progress`, `transfer`, `busy_block`, `credit_limit`, `modality` and
`days`, 20 each for `grade_threshold` and `withdrawal`, and 13 for `nested_groups`, which runs
out first. The generator goes through every program once, then goes round again with a new seed
(up to 4 rounds), so a program can give one family a second pair, built from other courses. A
later-round pair's id ends in `#<round>` (`transfer/philosophy-ba#3`, case
`pair-transfer-philosophy-ba-3-a`). A pair with the same rules and change as one already in that
program is skipped. Most families use 20 or more programs; `transfer` uses 8 and `nested_groups` 8.

**`late_meeting` (27 pairs, 54 cases).** Added after the 200 pairs above: `python generate_pairs.py --families late_meeting --total 227`. Each pair is strong (no single section is right in both variants). The cutoff is a whole hour, noon to 8 PM, that one section's first meeting meets and a later meeting does not; it exists because `busy_block` only builds its overlap from a section's first meeting, so nothing else tests later meetings. Of the Fall 2026 sections with set times, 1,242 have two or more meetings.

There is no campus family: all but 30 of the 5,129 sections are Glassboro, online or
off-campus. `nested_groups` has little variety: the only `one_of_groups` requirement with open
sections in a usable shape is Rowan Core Communicative Literacy, so all its pairs are that
requirement in eight business programs. Its transcripts never hold a later course without the
earlier one (no Composition II without I).

**One difference only.** A transcript-flip twin is not fabricated again: it is its base
transcript with one row changed, via the fabricator's `edits` (e.g. `{"in": "completed_courses",
"course_id": "SOC08120", "set": {"grade": "B"}}`, applied after the checks). Fabricating each
variant separately let the one changed row steer the fabricator into a different transcript (7
of 101 transcript pairs differed in 10 to 20 rows), so the twin could be answered from details
that were never meant to change.

Case ids are `pair-<family>-<program>-a` / `-b`, and `ground_truth.pair` is
`{"id": "<family>/<program>", "variant": "a" | "b", "changed": "<the detail, short>",
"difference": {...}}`. Each variant's `examples.invalid` begins with its twin's valid carts that
it rejects. `--check` also runs `check_pairs()`, which fails a pair unless both variants are
present, they differ in exactly one place (the prompt, or one transcript row), and each one
rejects at least one cart its twin accepts, i.e. unless the changed detail really changes the answer.

**`ground_truth.pair.difference`** spells out how this case differs from its twin. It is read
from the inputs the model is given, not from the generator's notes, so an unintended difference
would show up in it too:

| Field | Meaning | Example (`pair-withdrawal-human-services-ba-a`) |
|---|---|---|
| `where` | What differs: `prompt` or `transcript` | `transcript` |
| `this_case` | What this case has that the twin does not | `completed: SOC08120 Intro To Sociology (W)` |
| `twin_case` | What the twin has instead | `completed: SOC08120 Intro To Sociology (B)` |
| `twin_id` | The twin's case id | `pair-withdrawal-human-services-ba-b` |
| `answer_here` | A correct answer for this case, in words | `a section of Introduction to Sociology (SOC08120)` |
| `answer_in_twin` | A correct answer for the twin | `a section of Sociology of Social Welfare (SOC08223)` |
| `summary` | All of the above in one sentence | "Only the transcript differs. Here: … So the answer here is …" |

Like everything in `ground_truth`, it is never sent to the model. `python generate_pairs.py
--annotate` rewrites it for every pair in the file without building anything.

Paired accuracy is reported per run index: run *i* of variant a is paired with run *i* of
variant b. It is broken down by family (the first part of the pair id). A pair whose call
failed is counted as incomplete, not as a miss.

**`pair_result` in a results file.** After a run (and on `--rescore`), every row of a pair case
gets a `pair_result`, relating it to its twin's row in the same session and run: `pair_id`,
`twin_id`, `same_answer_as_twin`, `pair_passed`, and an `explanation` that names the twin's
outcome. The twin's own row has its outcome and answer, and the session summary has the pair
totals. For example: *"Only its twin is correct; this case is a semantic_error. Pair failed. The
model gave the same answer to both, so it ignored the change in the transcript …"*

**Weak pairs.** In 18 of the 200 pairs, one answer is correct in both variants: 13 of 21
`busy_block` pairs (a section that overlaps neither commitment) and 5 of 21 `transfer` pairs (a
course that counts toward both requirements). A model can pass those without using the changed
detail. When it does, by giving the same correct answer to both, `pair_result` says the pair
passed only weakly. The other eight families have no such answer. Each pair case records this
as `ground_truth.pair.has_shared_answer` (with a `shared_answer_example` crn when true), and a
session summary's `paired_accuracy.strong_pairs_only` scores the other 182 pairs on their own.

**Difficulty and chance baseline (every case, in both case files).** `ground_truth.difficulty`
holds:
- `candidate_courses`: the courses the case is about (its `schedule_from_tally.courses`).
- `candidate_answers`: every non-cancelled section of those courses, or every pair of sections
  when the answer is two named courses.
- `correct_answers`: how many of those the rules accept.
- `chance`: correct ÷ candidate, the probability of being right by picking one at random.
- `rules` and `prompt_words`.

Chance ranges from 0.006 to 1.0 (mean 0.31), so raw
accuracy is not comparable across families. A summary's `mean_chance_baseline` is the average
over the cases it ran. A model at that rate is doing no better than picking a plausible
section at random.

Two caveats. Chance counts full sections among the candidates, since a careless model might
pick one. And it measures picking a section, not structural decisions: in `credit_limit`, once
the right courses are chosen, every section combination may be correct (chance 1.0), because
the real test is whether to add the second course.

`python generate_pairs.py --annotate` recomputes the flag and difficulty for every pair. New
pairs get them when generated. Neither changes what the model sees, the rules or the
examples.

## Removed case files

The 21 hand-built cases (`case_001`–`case_015`, `aneesh_test` and five `rowan_` cases) were removed
on 10/18, and the 250 generated single cases (`input_format_cases.json`, `<program>-1` to `-5`)
and `generate_cases.py` on 10/20: the pipeline now runs on the pairs only. Both are in git history
(the hand-built cases at `bc6c9ba`, the single cases at `0afe332`). The six programs the hand-built
cases used remain in `degree_requirements.json`.

# Correctness model

Four tiers, stopping at the first failure:
`syntactic_error` → `schema_error` → `consistency_error` → `semantic_error` / `semantic_ok`.

- **Tiers 1–2, format.** See [The cart message](message-format.md#the-cart-message-didcomm-schema).
- **Tier 3, consistency** — invariants hold. The term is the schedule's. Each section exists,
  is not cancelled, has a seat (or is full with waitlist room when the message asks to
  waitlist), has valid credits and has its prerequisites met; inside the cart, no two
  sections overlap in time or belong to the same course.
- **Tier 4, semantic correctness** — the transaction achieves the user's intent. A case's
  answer key is `rules`: the cart must satisfy every rule (`check_rules()`). `examples.valid`
  and `examples.invalid` are carts `--check` uses to confirm the rules accept and reject what
  they should.

  | Rule | The cart must… |
  |---|---|
  | `count`, `credits` | have this many sections / this many total credits (`{"min", "max"}`) |
  | `courses_count_toward` | have only courses that move one of these unmet requirements forward (from the degree audit, so passed courses don't count unless repeatable) |
  | `no_days` | have no meeting on these days |
  | `starts_before`, `ends_by` | have every meeting start before / end by this time |
  | `modality_in` | use only these modalities |
  | `must_include_courses` | contain these courses |
  | `courses_in` | have only courses from this list |
  | `busy` | have no meeting that overlaps one of the student's fixed commitments (`[{days, start_time, end_time}]`; touching end to start is not an overlap) |

  The student id and term code are always checked, and so is that no section is waitlisted
  (no request asks for a waitlist).

  Every verdict in a results file explains itself, tier by tier. `tiers` gives one line per
  tier: what it checks, and `passed`, `failed` or `not reached` with a check count, e.g.
  `"tier3": "consistency (invariants against the schedule and transcript): passed (9/9 checks passed)"`.
  Then each tier's checks are listed in `tier1_checks` … `tier4_checks`, each with what it
  `requires` in words, what was `found`, and whether it `passed`:

  | List | Checks |
  |---|---|
  | `tier1_checks` | `json` (one JSON value, no prose or code fence); `envelope` (DIDComm v2: `id`, a `type` URI, a `body` object, well-formed optional headers) |
  | `tier2_checks` | `type` (exactly our register type); `body` (exactly `student_id`, `term_code`, a non-empty `sections`); `sections` (every entry's fields present and well-formed); `unique_crns` |
  | `tier3_checks` | `term`, then for each section (`section` = its crn): `exists`, `copied_fields`, `not_cancelled`, `seat` (or `waitlist`), `credits`, `prerequisites`, `one_section_per_course`, `no_time_conflict` |
  | `tier4_checks` | the case's rules plus `student_id`, `term_code` and `waitlist` (each also names its `rule`) |

  A tier that failed lists every check it ran, so a cart can show several Tier 3 problems; the
  `outcome` and `detail` are still the first failure, as before. Tiers after the failing one
  are `not reached` and have no checks. A failed transfer case's Tier 4 check, for example:

  ```json
  {"rule": "courses_count_toward",
   "requires": "every course moves one of these unfinished requirements forward: PHIL-LOGIC",
   "found": "40961 (PHIL09120) counts toward PHIL-ELECTIVES, FREE",
   "passed": false}
  ```

  A failed Tier 4 check also has a `why`, in words: what the model chose, why that breaks the
  rule, and what would have satisfied it. For example: how many minutes a section overlaps the
  student's commitment; that a course would have counted toward a requirement the transcript
  already completes; how far over the credit limit the cart is; which offered courses would
  have counted. A `semantic_error` verdict also has `why_wrong`, placed right after `detail`.
  It says which checks failed and what the request needed (the case's intended reasoning, from
  its answer key's `implicit_relationship`):

  ```json
  "why_wrong": "1 of 5 Tier 4 checks failed (courses_count_toward). What the request needed: PHIL-FOUNDATIONAL needs 6 sh: 3 completed plus transfer PHIL09150 (3 sh, one of its options) meets it, so the request moves to PHIL-LOGIC."
  ```

  `outcome`, `detail` and `deviation_score` are as before (the rules a cart broke are the
  `tier4_checks` with `passed: false`). To add these fields to an older results file, re-grade it: `python run_semantic_eval.py --test-file <cases> --output
  <results> --rescore`. That re-scores the saved answers with the current scorer (no model call)
  and prints any outcome that changed.
