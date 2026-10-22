# The input format

**Status:** implements Anand's *Registration Input Format Specification*, v0.3 (draft, Task 1).
That document is the reference for the base fields and is not in this repo yet. Everything
marked **[provisional]** below fills one of its open items and is not a team decision.

One input object describes one registration situation, and all of it is sent to the model.

| Field | Required | Content |
|---|---|---|
| `student` | yes | `student_id`, `degree`, `major`, optional `minor`, `program_id`, `catalog_year`, optional `entry` (first-year or transfer; see "Fields for Rowan program guides"). |
| `transcript` | yes | `completed_courses` (with grades, including `W`), `transfer_courses`, `courses_in_progress` (no grade). |
| `program_requirements` | yes | Each has `req_id`, `category`, `rule`, `course_options`, `credit_hours`, `minimum_grade`. |
| `schedule` | yes | `term`, `term_code` (six digits, e.g. `202640`; the message must copy it), and `sections`. A section has `crn`, `course_id`, `section`, `credit_hours`, `seats_total`, `seats_taken`, `campus`, `modality`, `meetings` (days, optional `start_time`/`end_time`, `location`, `type`), plus optional fields from the Section Tally (see below). |
| `prompt` | yes | The student's request in everyday language. |
| `prerequisites` | no | Added for open item 4. See below. |
| `course_equivalents` | no | Added for open item 6. |
| `graduation_rules` | no | Added for open item 7. |
| `program_guide` | no | The Rowan Academic Program Guide the requirements come from. See "Fields for Rowan program guides". |
| `course_attributes` | no | Rowan Core and Rowan Experience designations by course id. Same section. |

`registration_input.schema.json` holds the field rules (types, the course id pattern, allowed
grades, `Season YYYY` terms, `h:mm AM/PM` times, no unknown fields).
`input_format.validate_input()` adds the rules a schema cannot express: every `crn` and
`req_id` is unique, `seats_taken` is at most `seats_total`, and a meeting ends after it
starts. Because unknown fields are rejected, an answer key placed inside the input is caught.

How one input object is used:

- **Prompt.** `render.to_task()` sends everything except `prompt` as the known
  state, and `prompt` as the intent. The answer key is never included.
- **Database.** `checker.seed_db()` builds the checker's database from the same object,
  so the model and the checker see the same situation.
- **Scoring.** `scoring.evaluate_case()` runs all four tiers.

A test case wraps one input object with an id and an answer key. Where the sections come
from is set by the case:

| Case gives | The model sees | Who picks the sections |
|---|---|---|
| `"schedule_from_tally": {"whole": true}` (what the default run, the session, gives every tally-based case) | Every section in the tally, in the session's opening turn; the case's own turn carries only the term | The model, from all 5,129 |
| `"schedule_from_tally": {"named_courses": true}` (what every case file says; used with `--named-courses`), optionally with `"courses": [...]` | Every open section of every course the input names, listed in the prompt, in one input | The pipeline (which courses); the model (which sections) |
| `input.schedule` written out | That schedule | The case author |

```json
{
  "case_id": "pair-busy_block-journalism-ba-a",
  "input": { "program": "journalism-ba", "student": {}, "transcript": {"fabricate": {}}, "prompt": "Register me for News Media Literacy (JRN02101). I work Mondays and Wednesdays ..." },
  "schedule_from_tally": { "named_courses": true, "courses": ["JRN02101"] },
  "ground_truth": { "rules": { "count": {"min": 1, "max": 1}, "must_include_courses": ["JRN02101"], "busy": [{"days": ["Monday", "Wednesday"], "start_time": "10:00 AM", "end_time": "11:15 AM"}] } }
}
```

`cases.load_test_cases()` fills in `input.schedule` from
`section_tally_fall2026.csv` when it loads the file (`resolve_case()`).

With `named_courses`, `cases.named_courses()` collects every course the input
names: each requirement's `course_options`, each course with a `prerequisites` entry, and each
course id written in the prompt. `section_tally.course_schedule()` then takes every section of
those courses, in export order, leaving out cancelled ones and skipping courses with no section
this term. `"courses": [...]` adds courses the input does not name by id; the pair generator
lists each case's candidate courses there, so `--named-courses` shows the model all of them. The
prompt and the checker's database hold the same sections, so a crn from outside them is a
`consistency_error` ("does not exist").

In whole mode the schedule is the whole tally (5,129 sections, cancelled ones flagged): the
checker's database holds every section, and the prompt's own part carries only the term,
because the sections are the session's opening turn (see "The session"). A case gives
`schedule` or `schedule_from_tally`, never both.

Cases are picked (`--task-ids`) before the export is read, so a case that does not use the
tally runs even if the export is missing; a case that uses it stops with a clear error.

## Sections from the Section Tally

The schedule can come from Rowan's real Section Tally (Fall 2026, 5,129 sections, 138
subjects) instead of invented sections. The data is one file, `section_tally_fall2026.csv`:
the export as downloaded, renamed, read whole on each run (about 20 ms). A case picks its
sections with `schedule_from_tally` (above), which calls `section_tally.course_schedule()` or
`section_tally.full_schedule()`; `section_tally.load_schedule("CS")` gives a whole subject. All 5,129 sections convert and
validate (`python section_tally.py check`, which fails if the export is missing or empty).
To use a newer export, save it under the same name and rerun `python run_semantic_eval.py
--check`: answer keys depend on seat counts and times, so a refresh can change them.

| Tally column | Input field | Conversion |
|---|---|---|
| `CRN` | `crn` | as is |
| `Subj` + `Course#` | `course_id` | `CS` + `00100` → `CS00100` |
| `Sect` | `section` | as is (`"1"`, not `"01"`) |
| `Title` | `title`, `title_note` | a closing parenthetical is split off: `PRINCIPLES OF ACCTG I(RCB students only.)` → note `RCB students only.` (1,203 sections; the note can be a restriction, a topic such as `Jazz`, or a former name) |
| `Hours` | `credit_hours` | `3.00` → `3`; `1.00 to 9.00` → `{"min": 1, "max": 9}` |
| `Max`, `Enrolled` | `seats_total`, `seats_taken` | as is, except that `Enrolled` above `Max` (666 over-enrolled sections) is capped at `Max`, so the section stays full. `Avail` is left out because it is always `Max - Enrolled` |
| `Campus` | `campus` | as is (Glassboro, Online, Off-campus, Remote, Camden, …) |
| `Session` | `session`, `modality` | `session` as is; `modality` is `online` for Online, `hybrid` for Hybrid, else `in-person` |
| `Meeting Time` | `meetings`, `meeting_note` | each `MW 09:30 AM - 10:45 AM Business Hall 101 (Class)` group becomes a meeting with full day names, times, `location` and `type`; blank or `N/A` → no meetings |
| `Instructor(s)` | `instructor` | verbatim. `STAFF` = not yet assigned; several names are run together with no separator |
| `Part of Term` | `part_of_term` | verbatim |
| `Wait Cap`, `Wait Enrolled` | `waitlist` | only when the cap is above 0 |
| `Max Reserved`, `Left Reserved` | `reserved_seats` | only when above 0 |
| `Room Cap` | `room_capacity`, `room_capacity_marked` | the number; `NA` → left out; a trailing `*` → `room_capacity_marked: true` |
| `Cancelled` | `cancelled` | cancelled sections are left out of a schedule unless asked for; then they carry `cancelled: true` |
| `Term Code` | `schedule.term_code`, `schedule.term` | `202640` → `Fall 2026` |

What changed in the input format so the tally fits:

- **A meeting may have no times.** 37 tally meetings list days and a room but no time (to be
  arranged). They are shown to the model but cannot conflict.
- **Times may have a leading zero** (`09:30 AM`), as the tally writes them.
- **Cancelled is a Tier 3 rule.** Registering for a section with `cancelled: true` is a
  `consistency_error`.
- **New optional section fields:** `session`, `title_note`, `instructor`, `part_of_term`,
  `waitlist`, `reserved_seats`, `room_capacity`, `room_capacity_marked`, `cancelled`,
  `meeting_note`, and meeting `location`. Existing cases are unaffected.

Open points:

- **A whole subject is too big for one prompt.** All 168 open CS sections come to about
  86,000 characters as JSON (about 37,000 as CSV). A case carries the sections of the courses
  its program guide names instead: 350–500 sections, about 86k–132k tokens (see "Cases built from degree requirements").
- **The `*` after `Room Cap`** (296 sections) is not documented in the export.
- **Only the Fall term code is known.** `TERM_SEASONS` in `section_tally.py` maps the last two
  digits (`40` → Fall); others are added when a tally for that term shows its dates.
- **Reserved seats are shown but not checked.** Waitlists are checked: the message can ask
  to join one (see the message body).

## How the spec's open items were filled in [provisional]

| Spec open item | What the code does |
|---|---|
| 1. Section id | Each section object in the message names its section by `crn`. The prompt tells the model so. |
| 2. Meeting times | Two sections conflict when they share a day and their time ranges overlap (`meetings` table in `checker.py`). |
| 3. One section or several per message | Several: the spec assumed one enroll message per section, but the 10/1 meeting settled on a cart, so one message registers every section. |
| 4. Prerequisites | New optional `prerequisites` list. Treated as a **consistency** rule (Tier 3): registering for a course whose prerequisite is unmet is a `consistency_error`. A course still in progress does not satisfy a prerequisite. |
| 5. `TA` and `minimum_grade` | Accepted transfer credit (`TA`) meets every minimum grade. `F` and `W` meet none. |
| 6. Other requirement types | New optional fields, all read by `audit.audit()`. See the next table. |
| 7. Graduation-wide rules | New optional `graduation_rules`: `total_credits`, `minimum_gpa`, `residency_credits`. The audit derives earned credits, institutional credits and GPA from the transcript; none is stored. A repeated course counts once, by its best attempt. |
| 8. Other input renderings | `--rendering csv` shows the same state as CSV tables, and `--rendering nl` as plain-text sentences. JSON stays the stored form. |
| 9. Test-case wrapper | A `rules` answer key (see the Correctness model): what any correct cart must satisfy, with `examples` of carts that must pass and fail. A cart is a list of crns; the key never repeats a section's details: `expected_message()` copies them in from the schedule. (Listed exact-cart keys, `acceptable_crns` / `acceptable_carts`, were removed on 10/18; no generated case used them.) |

Fields added for open items 4, 6 and 7:

| Field | Where | Meaning |
|---|---|---|
| `prerequisites` | top level | Entries of `{"course_id", "requires": [...], "minimum_grade"}`: every course in `requires` must be earned at or above the grade first. One entry per course. |
| `course_equivalents` | top level | Groups of course ids that are the same course under different catalog years. An equivalent satisfies a requirement or a prerequisite. |
| `graduation_rules` | top level | `total_credits`, `minimum_gpa`, `residency_credits`; each optional. |
| `attributes` | transcript courses and sections | Catalog attributes a course carries, e.g. a general-education area. |
| rule `credits_with_attribute`, with `attribute` | requirement | Satisfied by `credit_hours` credits of courses carrying that attribute. `course_options` is then empty. |
| `repeatable` | requirement | Courses whose credits count every time they are earned. Otherwise only the best attempt counts. |
| `credit_caps` | requirement | Entries of `{"course_options", "max_credits"}`: at most that many credits from those courses count. |
| `exclusive` | requirement | By default one course may count toward several requirements. Two requirements both marked `exclusive` cannot share a course; the earlier one in the list takes it. |
| `credit_hours` as `{"min", "max"}` | section | A variable-credit section. The message must say how many credits the student takes (`credit_hours` in its section object), and Tier 3 checks it is in the range. |

#### Fields for Rowan program guides

A Rowan Academic Program Guide is laid out in fixed sections: university graduation rules,
Rowan Core (six literacies), Rowan Experience (three attributes), Non-Program courses, Major
requirements and Free Electives. These fields let an input carry one:

| Field | Where | Meaning |
|---|---|---|
| `program_guide` | top level | `program_name`, `department`, `contact_email`, `effective_term`, `source_url`, `total_credits`, `sections` (each `{section, title, credit_hours, notes}`; `credit_hours` may be `{min, max}`, and the sections must add up to `total_credits`), `rowan_core.literacies`, `rowan_experience.attributes`, `program_rules` (as the guide words them). Context for the model; the audit reads `program_requirements`. |
| `course_attributes` | top level | `{course_id: [attributes]}`: Rowan Core codes (`COML ARTL GLBL HUML QNTL SCIL`) and Rowan Experience codes (`LIT WI RS`). The Section Tally export has no attributes column, so a case lists the designations the guide states; they count for `credits_with_attribute` like attributes on a transcript row. |
| `entry` | student | `{type: first_year \| transfer, transfer_credits_at_entry, nj_associate_degree}`. Decides the guide's waivers. |
| `guide_section` | requirement | `rowan_core`, `rowan_experience`, `non_program`, `major` or `free_electives`. |
| rule `one_of_groups`, with `course_groups` | requirement | Met when every course of any one group is earned: a choice of sequences, or "COMP01111 + COMP01112 + (CMS04205 or CMS04206)" written as two groups. |
| rule `credits_any` | requirement | Free electives: any credits no exclusive requirement took. Must be the last requirement. |
| `course_ranges` | requirement (`any_of`, `credits_from`) | Course families, `{subject, discipline?, min_number?, max_number?, exclude?}`, e.g. "any 300+ level PHYS course". The number is the id's last three digits: PHYS00340 is subject PHYS, discipline 00, number 340. |
| `recommended_courses` | requirement | The guide's "Recommendation from major". Informational. |
| `waived_if` | requirement | `{nj_associate_degree: true}` and/or `{transfer_credits_at_entry: 24}`: met with no course when the student's entry matches either. |
| `major_gpa`, `minimum_grade` | `graduation_rules` | GPA over courses that satisfy `major` requirements (2.5 for the business majors), and the university's D- floor. |

`audit.audit()` applies waivers, reports `missing` for the closest group of a
`one_of_groups` rule and `cap_remaining` for credit caps, and computes `major_gpa`.
`counts_toward()` skips a course whose credit cap is used up. Known limitation:
`counts_toward()` does not simulate `exclusive`, so a course that an earlier exclusive
requirement would take is still reported for a later one; the cases are written so this does
not change an answer. `named_courses()` includes `course_groups` and `recommended_courses`
but does not expand `course_ranges`; a case adds a range's offered courses with
`schedule_from_tally.courses`.

What the checker does and does not read:

- **Tier 3 reads** the schedule and the prerequisites (against the transcript).
- **The audit is not part of scoring.** `audit.audit()` and `counts_toward()` report
  which requirements are met and which courses would move one forward. They are for case
  authors and for checking an answer key. Tier 4 still compares the cart to the answer key,
  so whether a course "counts toward the degree" is decided by the author, with the audit
  as a cross-check.

## Degree programs and fabricated transcripts

**The requirements file.** `degree_requirements.json` holds 50 programs, keyed like
`marketing-bs` or `english-ba`. Six are the programs the earlier hand-built cases used (CS,
Psychological Science, Management, Physics, Accounting, Environmental Science). The other 44 were
parsed from the registrar's archived Academic Program Guides (the newest Rowan Core guide of each
program; the live catalog at catalog.rowan.edu blocks scripted access, so it was not used). Each
program's `program_guide.source_url` links its PDF, and choices made where a guide is ambiguous
or inconsistent (typos in course ids, subtotals that do not add up, unlisted electives
approximated by `course_ranges`) are written in the requirement's `notes` or in
`program_guide.program_rules`. ECE requires 128 sh; Biomedical Engineering counts all six Rowan
Core literacies (28 sh) and has no free electives.

**A case names its program.** Its input no longer writes out requirements or a transcript:

```json
"input": {
  "program": "marketing-bs",
  "student": {"student_id": "S1001", "catalog_year": "Fall 2023", "entry": {"type": "first_year"}},
  "transcript": {"fabricate": {"seed": 7, "complete": ["CORE-*", "NONPROG-*", "BUS-FOUNDATION"],
                               "rows": [{"term": "Spring 2026", "course_id": "MKT09379", "grade": "D+"}],
                               "avoid": ["MKT09384"], "keep_unmet": ["MKT-REQUIRED"]}},
  "prompt": "..."
}
```

`fabricate.build_input()` takes the program's requirements, guide, attributes, graduation rules
and prerequisites (plus any `extra_requirements`, `prerequisites` or `course_equivalents` the case
adds) and fabricates the transcript: real courses that satisfy each requirement in `complete`,
grades at or above every minimum they must meet, prerequisites in earlier terms, credits spread
from the catalog year to `through` (default Spring 2026), and the case's own `rows` (its traps)
kept as written. Rowan Core / Experience attribute requirements are met with courses some guide
designates. It then runs the degree audit and stops with an error unless every `complete`
requirement is met and every `keep_unmet` one is not. The same seed always gives the same
transcript. `cases.resolve_case()` calls it, so validation, the checker and the
prompt all see the full input object.

Caveats: requirements and attribute designations are only what the guides state, so a course a
model knows to be, say, Global Literacy counts only if the input designates it; real
prerequisites are checked only where a guide states them; and some guides are several years old
(effective Fall 2018 to Fall 2026).
