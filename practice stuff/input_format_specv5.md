# Registration Input Format Specification

**Version:** 0.4.1 (draft)
**Status:** For group review. Items marked *provisional* are proposals, not group decisions (see "Open items").
**Companion file:** `input_format_schema.json`, a JSON Schema for the input object.

## Purpose

This specification defines the JSON input format used by the registration agent. One input file describes one registration situation: the student, their transcript, their degree program requirements, the course sections being offered, and the student's registration request.

The format is designed so that the registration agent can use the provided information to determine whether a requested course or section is appropriate and whether the requested registration is possible. It is also designed so that the same file can seed the checker's database, so the model and the checker always see the same situation.

**Scope.** This document defines what the model receives. It does not define the model's reply, which belongs to the DIDComm schema specification. Appendix A sketches, provisionally, how test cases are built on top of this format.

## Top-level structure

| Field | Type | Required | Description |
|---|---|---|---|
| `student` | object | Required | Student identity, program, and current registrations. |
| `transcript` | object | Required | Completed, transfer, and in-progress courses. |
| `program_requirements` | array | Required | Requirements the student must satisfy. |
| `schedule` | object | Required | Sections offered in the target term. |
| `prompt` | string | Required | The student's natural-language request. |

Every field is available to the harness, which decides how to render it for the model (for example, it may derive `seats_left`). No field contains answer information. Test-case metadata and the answer key are a separate concern (see Appendix A).

## How this maps to the harness

| This spec | Harness |
|---|---|
| `prompt` | `TaskSpec.intent` |
| `student`, `transcript`, `program_requirements`, `schedule` | `TaskSpec.inputs`, serialized as one JSON string |
| `schedule.sections` | The checker's `sections` table (`crn`, `seats_total`, `seats_taken`, meeting time) |
| `student.registered_crns` | The checker's `enrollments` table |

Today `run_registration.py` builds a smaller, hand-written `inputs` object and seeds the database from separate SQL. With a loader, both come from one file. Column and field names in the checker may change depending on open items 1 and 2.

## Conventions

- **Keys** are lowercase `snake_case`.
- **`course_id`** is a string made of the subject code and course number with no space, e.g. `CS04113`. Leading zeros are significant, so a course id is never a number. Lab courses can contain a letter, e.g. `PHYSL0220`. Pattern: `^[A-Z]{2,4}[A-Z0-9]{5}$`.
- **Converting from Section Tally** (*provisional*): join Subj and Course# and pad the course number with leading zeros to five characters. A row showing `CYBR` and `1201` becomes `CYBR01201`. This is based on one sample row, so confirm the padding rule against more rows.
- **`crn`** is a string, e.g. `"10067"`.
- **Credits** use the key `credit_hours` everywhere and are numbers (`4`, not `"4.000"`).
- **Terms** are written `Season YYYY`, e.g. `Fall 2026`.
- **Grades** are one of the values below.
- **Derived values are not stored.** Quality points, GPA, credit totals, and seats available can be computed from the data, so they do not appear in the input.

### Allowed grades

| Grade | Meaning | Counts as earned? |
|---|---|---|
| `A`, `A-`, `B+`, `B`, `B-`, `C+`, `C`, `C-`, `D+`, `D`, `D-` | Passing letter grades, listed highest to lowest | Yes |
| `F` | Failing | No |
| `W` | Withdrawn (attempted, not earned) | No |
| `TA` | Transfer credit accepted | Yes |

Letter grades are ordered as listed, which is how `minimum_grade` is compared. `W` never satisfies a minimum. How `TA` compares to a minimum grade is an open item.

## Student

Basic information about the student, their program, and what they are already registered for.

| Field | Type | Required | Description |
|---|---|---|---|
| `student_id` | string | Required | Anonymized unique identifier, e.g. `S001`. Never a real student id. |
| `degree` | string | Required | Degree being pursued, e.g. `Bachelor of Science`. |
| `major` | string | Required | Student's major. |
| `minor` | string | Optional | Student's minor. |
| `program_id` | string | Required | Program code, e.g. `O701`. |
| `catalog_year` | string | Required | Term of the program guide that applies, e.g. `Fall 2023`. Requirements change by year. |
| `registered_crns` | array of string | Required (may be empty) | Sections the student is already registered for in `schedule.term`. Each must match a `crn` in `schedule.sections`. These drive the duplicate and time-conflict checks. |

The student's home campus or place of residence is intentionally **not** a field. When a test case depends on where the student lives, that fact belongs in `prompt` so the model has to notice it.

## Transcript

The transcript has three arrays. Each may be empty.

### `completed_courses`

Courses attempted at the institution, including withdrawals.

| Field | Type | Required | Description |
|---|---|---|---|
| `term` | string | Required | Term taken, e.g. `Fall 2023`. |
| `course_id` | string | Required | See conventions. |
| `campus` | string | Optional | Campus where it was taken, e.g. `Glassboro` or `Online`. |
| `level` | string | Optional | `UG` or `GR`. |
| `title` | string | Optional | Abbreviated title as printed on the transcript. Not used for matching. |
| `grade` | string | Required | One of the allowed grades. |
| `credit_hours` | number | Required | Credits attempted. |

### `transfer_courses`

Credit accepted from another institution.

| Field | Type | Required | Description |
|---|---|---|---|
| `institution` | string | Required | Institution the credit came from. |
| `course_id` | string | Required | Course id as accepted by this institution. |
| `title` | string | Optional | Title. |
| `grade` | string | Required | Normally `TA`. |
| `credit_hours` | number | Required | Credits accepted. |

### `courses_in_progress`

Courses in the current term that have no grade yet.

| Field | Type | Required | Description |
|---|---|---|---|
| `term` | string | Required | Current term. |
| `course_id` | string | Required | See conventions. |
| `campus` | string | Optional | Campus. |
| `level` | string | Optional | `UG` or `GR`. |
| `title` | string | Optional | Title. |
| `credit_hours` | number | Required | Credits being taken. |

### Transcript notes

- A course can appear more than once. For example a `W` followed by a later passing grade. When checking a requirement, the best earned attempt counts.
- A `W` row counts as attempted but not earned.
- In-progress courses do not satisfy a requirement or a minimum grade yet.
- `courses_in_progress` describes the current term. `schedule.term` is the term being registered for, normally the next one.

## Program requirements

An array. Each entry is one requirement the student must satisfy.

| Field | Type | Required | Description |
|---|---|---|---|
| `req_id` | string | Required | Unique id within the case, e.g. `intro_oop`. |
| `category` | string | Required | Human-readable grouping, e.g. `Major Requirements: Foundational Courses`. |
| `rule` | string | Required | How the requirement is satisfied. One of the values below. |
| `course_options` | array of string | Required | `course_id`s that can satisfy the requirement. |
| `title` | string | Optional | Human-readable name of the requirement. |
| `credit_hours` | number | Required | Credits the requirement is worth, or credits needed for `credits_from`. |
| `minimum_grade` | string | Required | Lowest passing letter grade for this requirement, e.g. `D-` or `C-`. |
| `notes` | string | Optional | Free text. Not used by the checker. |

### Rule values (v1)

| `rule` | Satisfied when |
|---|---|
| `all_of` | Every course in `course_options` is earned at or above `minimum_grade`. |
| `any_of` | At least one course in `course_options` is earned at or above `minimum_grade`. |
| `credits_from` | Earned credits from `course_options`, each at or above `minimum_grade`, total at least `credit_hours`. |

Anything the rules above cannot express is out of scope for v1 (see "Open items"). The prose in `notes` is for human readers only, so every checkable condition must be expressed through `rule`, `course_options`, `credit_hours`, and `minimum_grade`.

## Schedule

The sections offered in one term.

| Field | Type | Required | Description |
|---|---|---|---|
| `term` | string | Required | Term being registered for, e.g. `Spring 2027`. |
| `term_code` | string | Optional | Registrar term code as printed by Section Tally, e.g. `202710`. How codes map to term names is not specified here. |
| `sections` | array | Required | Sections offered. |

### Section

| Field | Type | Required | Description |
|---|---|---|---|
| `crn` | string | Required | Unique section identifier. **This is the id the model's registration message must use** (see open item 1). |
| `course_id` | string | Required | See conventions. |
| `section` | string | Required | Section number, e.g. `01`. Section Tally prints `1`. Use two digits in case files. |
| `title` | string | Optional | Course title. |
| `instructors` | array of string | Optional | Instructor names. Use fictional names in case files. |
| `credit_hours` | number | Required | Credits for the section. |
| `seats_total` | integer | Required | Capacity (Section Tally: Max). |
| `seats_taken` | integer | Required | Seats already filled (Section Tally: Enrolled). |
| `campus` | string | Required | Campus where it meets, e.g. `Glassboro`, `Camden`, or `Online`. |
| `modality` | string | Required | `in-person`, `online`, or `hybrid`. *Provisional*: Section Tally has no such column. |
| `meetings` | array | Required | Meeting times. Empty for sections with no scheduled meeting time, such as asynchronous online sections. |
| `cancelled` | boolean | Optional | `true` if the section is cancelled. Defaults to `false`. A cancelled section is not available to enroll in. |
| `part_of_term` | string | Optional | Part of term, e.g. a three-week winter session. Free text as printed. |
| `waitlist_capacity` | integer | Optional | Waitlist capacity (Section Tally: Wait Cap). |
| `waitlist_taken` | integer | Optional | Students on the waitlist (Section Tally: Wait Enrolled). |
| `reserved_seats_total` | integer | Optional | Seats reserved for a group (Section Tally: Max Reserved). |
| `reserved_seats_left` | integer | Optional | Reserved seats not yet used (Section Tally: Left Reserved). |

Seats available are `seats_total - seats_taken`. The file stores `seats_total` and `seats_taken` as the source of truth, and the harness may show the model a derived `seats_left`. The waitlist and reserved-seat fields are stored for realism but are not used by the v1 checker.

### Meeting

| Field | Type | Required | Description |
|---|---|---|---|
| `days` | array of string | Required | Full day names, e.g. `["Monday", "Wednesday"]`. |
| `start_time` | string | Required | `h:mm AM/PM`, e.g. `10:00 AM`. |
| `end_time` | string | Required | `h:mm AM/PM`. |
| `type` | string | Optional | e.g. `Lecture`, `Lab`. |

Two sections conflict when they share a day and their time ranges overlap.

### Section Tally mapping

| Section Tally column | Field | Notes |
|---|---|---|
| CRN | `crn` | |
| Subj + Course# | `course_id` | Pad the course number to five characters. |
| Sect | `section` | |
| Title | `title` | |
| Instructor(s) | `instructors` | |
| Meeting Time | `meetings` | Blank becomes `[]`. The printed format for filled-in times has not been checked. |
| Campus | `campus` | |
| Hours | `credit_hours` | |
| Max | `seats_total` | |
| Enrolled | `seats_taken` | |
| Avail | not stored | Derived. In the one sample row, Avail equals Max minus Enrolled. |
| Wait Cap | `waitlist_capacity` | |
| Wait Enrolled | `waitlist_taken` | |
| Max Reserved | `reserved_seats_total` | |
| Left Reserved | `reserved_seats_left` | |
| Part of Term | `part_of_term` | |
| Cancelled | `cancelled` | `FALSE` becomes `false`. |
| Term Code | `schedule.term_code` | |
| Session, Wait Avail, Room Cap | not stored | Not needed for v1. |

`modality` has no Section Tally column. For online sections the sample row shows `Online` as the campus, so the case author sets `modality` by hand. Complete example 2 shows these fields filled in.

## Prompt

A string holding the student's request in everyday language, e.g. `"Enroll me in CS05000."`.

Guidelines for writing prompts:

1. For a basic consistency case, name the course directly.
2. For a semantic case, put the fact the model must notice **only in the prompt**, in everyday wording. Do not also add it as a data field, or the case stops testing whether the model noticed it.
3. Make sure the schedule contains at least one section that passes every consistency rule but is wrong because of that fact.
4. For a request covering several courses, name each one in the prompt.

Examples:

- `"Enroll me in CS05000. I live in Glassboro."` The model should not pick a section at a satellite campus.
- `"Register me for in-person classes."` The model should not pick online or hybrid sections.
- `"Enroll me in CS05000 and CS06000."` A multi-course request.

## Validation rules

### Checked by the JSON Schema (`input_format_schema.json`)

1. The file is valid JSON and contains only the fields defined here. Unknown fields are rejected, so answer information cannot hide in the input.
2. All required fields are present and have the right types.
3. Every `course_id` matches the course id pattern.
4. Every `grade`, `minimum_grade`, `level`, `modality`, `rule`, day name, and clock time is an allowed value or format.
5. `courses_in_progress` entries have no `grade`.
6. A `credits_from` requirement has a `credit_hours` greater than zero.

### Checked by a validator script (the schema cannot express these)

7. Every `crn` is unique within the case.
8. Every `req_id` is unique within the case.
9. `seats_taken` is between 0 and `seats_total`.
10. Every entry in `registered_crns` matches a `crn` in `schedule.sections`.
11. Each meeting's `end_time` is after its `start_time`.
12. `waitlist_taken` does not exceed `waitlist_capacity`, and `reserved_seats_left` does not exceed `reserved_seats_total`.
13. Requirement and transcript courses do not need to appear in the schedule. A student can need a course that is not offered this term.

### Checked by a reviewer

14. Facts that a semantic test depends on appear only in `prompt`.

## Complete example 1

A synthetic student. This example uses no real student data. Requirements are abbreviated and illustrative.

```json
{
  "student": {
    "student_id": "S001",
    "degree": "Bachelor of Science",
    "major": "Computer Science",
    "minor": "Mathematics",
    "program_id": "O701",
    "catalog_year": "Fall 2023",
    "registered_crns": ["12346"]
  },
  "transcript": {
    "completed_courses": [
      {
        "term": "Fall 2023",
        "course_id": "CS04113",
        "campus": "Glassboro",
        "level": "UG",
        "title": "INTRO OBJ-ORIENT PRGRMMNG",
        "grade": "A-",
        "credit_hours": 4
      },
      {
        "term": "Fall 2024",
        "course_id": "CS04222",
        "campus": "Glassboro",
        "level": "UG",
        "title": "DATA STRUCT/ALGORIM",
        "grade": "W",
        "credit_hours": 4
      },
      {
        "term": "Spring 2025",
        "course_id": "CS04222",
        "campus": "Glassboro",
        "level": "UG",
        "title": "DATA STRUCT/ALGORIM",
        "grade": "B",
        "credit_hours": 4
      }
    ],
    "transfer_courses": [
      {
        "institution": "Example County College",
        "course_id": "PHYS00220",
        "title": "INTRODUCTORY MECHANICS",
        "grade": "TA",
        "credit_hours": 4
      }
    ],
    "courses_in_progress": [
      {
        "term": "Fall 2026",
        "course_id": "CS04114",
        "campus": "Glassboro",
        "level": "UG",
        "title": "OBJ-ORIENT PRGRM/DATA ABSTR",
        "credit_hours": 3
      }
    ]
  },
  "program_requirements": [
    {
      "req_id": "comp_1",
      "category": "Rowan Core: Communicative Literacy",
      "rule": "all_of",
      "course_options": ["COMP01111"],
      "title": "College Composition I",
      "credit_hours": 3,
      "minimum_grade": "D-",
      "notes": "Required course"
    },
    {
      "req_id": "intro_oop",
      "category": "Major Requirements: Foundational Courses",
      "rule": "any_of",
      "course_options": ["CS04113", "CS04111"],
      "title": "Introduction to Object-Oriented Programming",
      "credit_hours": 4,
      "minimum_grade": "C-",
      "notes": "Students must be ready for MATH01130"
    },
    {
      "req_id": "cs_electives",
      "category": "Major Requirements: Computer Science Restricted Electives",
      "rule": "credits_from",
      "course_options": ["CS01303", "CS01395", "CS02435", "CS02480", "CS03351", "CS07450"],
      "title": "Computer Science Restricted Electives",
      "credit_hours": 12,
      "minimum_grade": "D-",
      "notes": "Choose 12 credits from the listed elective courses"
    }
  ],
  "schedule": {
    "term": "Spring 2027",
    "sections": [
      {
        "crn": "12345",
        "course_id": "CS05000",
        "section": "01",
        "title": "Introduction to Software Engineering",
        "instructors": ["Rivera, Dana"],
        "credit_hours": 3,
        "seats_total": 30,
        "seats_taken": 25,
        "campus": "Glassboro",
        "modality": "in-person",
        "cancelled": false,
        "meetings": [
          {
            "days": ["Monday", "Wednesday"],
            "start_time": "10:00 AM",
            "end_time": "11:15 AM",
            "type": "Lecture"
          }
        ]
      },
      {
        "crn": "12348",
        "course_id": "CS05000",
        "section": "02",
        "title": "Introduction to Software Engineering",
        "instructors": ["Okafor, Sam"],
        "credit_hours": 3,
        "seats_total": 30,
        "seats_taken": 10,
        "campus": "Camden",
        "modality": "in-person",
        "meetings": [
          {
            "days": ["Tuesday", "Thursday"],
            "start_time": "10:00 AM",
            "end_time": "11:15 AM",
            "type": "Lecture"
          }
        ]
      },
      {
        "crn": "12346",
        "course_id": "MATH03000",
        "section": "02",
        "title": "Applied Mathematics",
        "credit_hours": 3,
        "seats_total": 30,
        "seats_taken": 25,
        "campus": "Glassboro",
        "modality": "in-person",
        "meetings": [
          {
            "days": ["Tuesday", "Thursday"],
            "start_time": "1:00 PM",
            "end_time": "2:15 PM",
            "type": "Lecture"
          }
        ]
      }
    ]
  },
  "prompt": "Enroll me in CS05000. I live in Glassboro."
}
```

In this case both `CS05000` sections are open and conflict-free, so both pass the consistency checks. The prompt says the student lives in Glassboro, so the Camden section is the "valid but wrong" choice described in the design document. The student is already registered for `12346`, which does not conflict with either `CS05000` section.

## Complete example 2: Section Tally data

This example shows the Section Tally fields working together: a Winter session, an online section with no meeting times, a cancelled section, and a full section with a waitlist. The student is synthetic.

Section `10067` is the Section Tally row that was sent for this spec, with the instructor replaced by a fictional name and the date range after "Winter Session 3 Week" dropped. Sections `10068` and `10069` are invented to show a cancelled section and a full section with a waitlist. The meeting time on `10068` is invented because the real row's meeting time was blank, so the printed format of filled-in times is still unchecked. The term name `Winter 2027` is inferred from the row's term code and dates, and the mapping from codes to names is unconfirmed.

```json
{
  "student": {
    "student_id": "S002",
    "degree": "Bachelor of Science",
    "major": "Computer Science",
    "program_id": "O701",
    "catalog_year": "Fall 2023",
    "registered_crns": []
  },
  "transcript": {
    "completed_courses": [
      {"term": "Fall 2023", "course_id": "CS04113", "campus": "Glassboro", "level": "UG", "title": "INTRO OBJ-ORIENT PRGRMMNG", "grade": "A-", "credit_hours": 4},
      {"term": "Summer 2026", "course_id": "BIOL01104", "campus": "Online", "level": "UG", "title": "INTRO EVOL/SCIENTIFIC INQUIRY", "grade": "A", "credit_hours": 4}
    ],
    "transfer_courses": [],
    "courses_in_progress": [
      {"term": "Fall 2026", "course_id": "CS04114", "campus": "Glassboro", "level": "UG", "title": "OBJ-ORIENT PRGRM/DATA ABSTR", "credit_hours": 3}
    ]
  },
  "program_requirements": [
    {
      "req_id": "intro_oop",
      "category": "Major Requirements: Foundational Courses",
      "rule": "any_of",
      "course_options": ["CS04113", "CS04111"],
      "credit_hours": 4,
      "minimum_grade": "C-"
    },
    {
      "req_id": "cs_electives",
      "category": "Major Requirements: Computer Science Restricted Electives",
      "rule": "credits_from",
      "course_options": ["CS02435", "CS03351", "CYBR01470"],
      "credit_hours": 12,
      "minimum_grade": "D-",
      "notes": "Choose 12 credits from the listed elective courses"
    }
  ],
  "schedule": {
    "term": "Winter 2027",
    "term_code": "202710",
    "sections": [
      {
        "crn": "10067",
        "course_id": "CYBR01201",
        "section": "01",
        "title": "APPLIED CYBERSECURITY",
        "instructors": ["Fictional, Name"],
        "credit_hours": 3,
        "seats_total": 35,
        "seats_taken": 4,
        "campus": "Online",
        "modality": "online",
        "meetings": [],
        "cancelled": false,
        "part_of_term": "Winter Session 3 Week",
        "waitlist_capacity": 50,
        "waitlist_taken": 0,
        "reserved_seats_total": 0,
        "reserved_seats_left": 0
      },
      {
        "crn": "10068",
        "course_id": "CYBR01201",
        "section": "02",
        "title": "APPLIED CYBERSECURITY",
        "instructors": ["Placeholder, Pat"],
        "credit_hours": 3,
        "seats_total": 30,
        "seats_taken": 12,
        "campus": "Glassboro",
        "modality": "in-person",
        "meetings": [
          {
            "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
            "start_time": "9:00 AM",
            "end_time": "12:00 PM",
            "type": "Lecture"
          }
        ],
        "cancelled": true,
        "part_of_term": "Winter Session 3 Week",
        "waitlist_capacity": 50,
        "waitlist_taken": 0,
        "reserved_seats_total": 0,
        "reserved_seats_left": 0
      },
      {
        "crn": "10069",
        "course_id": "CYBR01201",
        "section": "03",
        "title": "APPLIED CYBERSECURITY",
        "instructors": ["Sample, Alex"],
        "credit_hours": 3,
        "seats_total": 25,
        "seats_taken": 25,
        "campus": "Online",
        "modality": "online",
        "meetings": [],
        "cancelled": false,
        "part_of_term": "Winter Session 3 Week",
        "waitlist_capacity": 50,
        "waitlist_taken": 3,
        "reserved_seats_total": 0,
        "reserved_seats_left": 0
      }
    ]
  },
  "prompt": "Enroll me in CYBR01201."
}
```

All three sections are the same course, so the prompt does not choose between them. Only `10067` can be registered:

- `10068` is cancelled. The current `registration.py` does not check `cancelled` yet, so the checker would need a new rule.
- `10069` is full. The v1 checker treats a full section as full even though its waitlist is open (open item 11).

A model that picks `10068` or `10069` makes a consistency error. This is a basic consistency case, since the prompt contains no hidden fact.

## Appendix A: Test-case wrapper and scenario families (provisional)

Test cases are defined by the test-case specification, not by this document. This appendix is a proposal, and it borrows the way the team's BIRD notes construct benchmarks: instead of many unrelated cases, build **scenario families**. A family has one base state and several controlled conditions, each with a hidden expected goal.

| BIRD-style idea | Here |
|---|---|
| Base scenario | `base_state`: the input object without its prompt |
| Controlled condition | A variant: a different `prompt`, and optionally a changed schedule |
| Hidden gold label | `expected_goal`, never sent to the model |
| Predicted statement | The section id the model's message selects, extracted from its reply |
| Compare prediction with gold | The selected section is in `acceptable_crns` |

Not borrowed: the statistical modelling machinery. Only the test-construction idea is used.

### Family fields

| Field | Type | Required | Description |
|---|---|---|---|
| `family_id` | string | Required | Unique id for the family. |
| `base_state` | object | Required | `student`, `transcript`, `program_requirements`, and `schedule` as defined above. |
| `variants` | array | Required | The controlled conditions. |

### Variant fields

| Field | Type | Required | Description |
|---|---|---|---|
| `variant_id` | string | Required | Unique within the family. |
| `prompt` | string | Required | The full prompt for this variant. |
| `condition` | string | Optional | For reviewers: what changed from the base. |
| `section_overrides` | array | Optional | Section patches, each an object with a `crn` and the fields to replace, used for consistency conditions such as a full section. |
| `dimension` | string | Required | `semantic` or `consistency`: which kind of correctness the variant targets. |
| `case_type` | string | Optional | A tag for reporting accuracy by kind of case, e.g. `implicit_location`. |
| `expected_goal` | object | Required | `acceptable_crns` (array, required): any one of these counts as correct. `goal` (object, optional): attributes the acceptable sections share, for reviewers. |

The input sent to the model for a variant is the `base_state` with the `section_overrides` applied, plus the variant's `prompt`. The harness never sends `expected_goal`, `condition`, `dimension`, or `case_type`.

### Why a set of acceptable answers

Several sections can be equally correct, so comparing against one expected message would penalize valid choices. Listing every acceptable CRN avoids that. In the family below, a student who lives in Glassboro is served correctly by the Glassboro section or by the online section, and only the Camden section is wrong.

### Reversal pairs and reporting

Two variants in the same family whose `acceptable_crns` do not overlap form a reversal pair. Scoring both together shows whether the model changed its choice when the condition changed, which a single pass or fail rate hides. Results can also be reported by `dimension` and `case_type`. The design document's target of about 100 cases would be roughly 20 families of 5 variants each (illustrative).

### Example family

```json
{
  "family_id": "reg_family_001",
  "base_state": {
    "student": { "...": "as in Complete example 1, with registered_crns set to []" },
    "transcript": { "...": "as in Complete example 1" },
    "program_requirements": [ "as in Complete example 1" ],
    "schedule": {
      "term": "Spring 2027",
      "sections": [
        {
          "crn": "12345", "course_id": "CS05000", "section": "01", "credit_hours": 3,
          "seats_total": 30, "seats_taken": 25, "campus": "Glassboro", "modality": "in-person",
          "meetings": [{"days": ["Monday", "Wednesday"], "start_time": "10:00 AM", "end_time": "11:15 AM"}]
        },
        {
          "crn": "12348", "course_id": "CS05000", "section": "02", "credit_hours": 3,
          "seats_total": 30, "seats_taken": 10, "campus": "Camden", "modality": "in-person",
          "meetings": [{"days": ["Tuesday", "Thursday"], "start_time": "10:00 AM", "end_time": "11:15 AM"}]
        },
        {
          "crn": "12351", "course_id": "CS05000", "section": "03", "credit_hours": 3,
          "seats_total": 30, "seats_taken": 5, "campus": "Online", "modality": "online",
          "meetings": []
        }
      ]
    }
  },
  "variants": [
    {
      "variant_id": "a",
      "prompt": "Enroll me in CS05000. I prefer online classes.",
      "condition": "Student prefers online.",
      "dimension": "semantic",
      "case_type": "explicit_preference",
      "expected_goal": {"acceptable_crns": ["12351"], "goal": {"modality": "online"}}
    },
    {
      "variant_id": "b",
      "prompt": "Enroll me in CS05000. I live in Glassboro.",
      "condition": "Student lives in Glassboro. The Camden section is wrong, the online section is still acceptable.",
      "dimension": "semantic",
      "case_type": "implicit_location",
      "expected_goal": {"acceptable_crns": ["12345", "12351"]}
    },
    {
      "variant_id": "c",
      "prompt": "Enroll me in an in-person section of CS05000. I live in Glassboro.",
      "condition": "Two facts together: in-person and Glassboro.",
      "dimension": "semantic",
      "case_type": "multi_fact",
      "expected_goal": {"acceptable_crns": ["12345"], "goal": {"modality": "in-person", "campus": "Glassboro"}}
    },
    {
      "variant_id": "d",
      "prompt": "Enroll me in CS05000. I live in Glassboro.",
      "condition": "Same as b, but the Glassboro section is now full.",
      "section_overrides": [{"crn": "12345", "seats_taken": 30}],
      "dimension": "consistency",
      "case_type": "full_section",
      "expected_goal": {"acceptable_crns": ["12351"]}
    }
  ]
}
```

Variants `a` and `c` have no acceptable CRN in common, so they form a reversal pair. In variant `d`, choosing `12345` is a consistency error and choosing `12348` is a semantic error.

For a multi-course request, `acceptable_crns` would have to become a set of acceptable sets or a goal predicate. That depends on open item 3.

## Open items

These are not settled and should be confirmed with the group.

1. **Section id.** This spec uses `crn` as the id in the registration message. The current `registration.py` uses a `section_id` such as `CS565-01`. Section Tally has no such id, and the CRN is its unique key, which favors `crn`. The decision belongs with the output schema.
2. **Meeting times.** `registration.py` currently checks conflicts with a slot code such as `MW14`. The structured `meetings` here need either a derived slot code or an overlap check. The sample Section Tally row had a blank meeting time, so the printed format of filled-in times is unchecked.
3. **One section or several per message.** The project brief refers to multi-transactional messages, which suggests one message may carry several enrollments. The input format does not change, but the expected-goal shape in Appendix A would.
4. **Prerequisites.** Not represented yet. Open whether they are a consistency rule or part of semantic correctness. The program guide requires a `C-` or better in several courses before taking courses that depend on them.
5. **`TA` and `minimum_grade`.** How a transfer grade compares to a minimum grade is undecided.
6. **Requirement types not covered in v1.** Attribute-based requirements (Rowan Core and Experience), free electives (any degree-applicable course not used for another requirement), one course counting toward several requirements, repeatable courses (e.g. `CS01395`), capped credits, variable-credit courses, and course equivalents across catalog years.
7. **Graduation-wide rules.** 120 total credits, 2.0 GPA, and 30 credits at the institution are not represented.
8. **Other input renderings.** The whiteboard notes "json/csv/NL". JSON is canonical here. CSV or natural-language renderings of the same data are not specified.
9. **Test-case wrapper.** The shape of `expected_goal` and where `family_id` lives are left to the test-case specification. Appendix A is only a sketch.
10. **Section Tally details.** Confirm the course-number padding rule, the meeting-time format, whether Avail can differ from Max minus Enrolled when seats are reserved, and whether `modality` can be derived from other columns.
11. **Waitlists and reserved seats.** Stored as optional fields, but whether a registration to a full section with an open waitlist is valid is undecided. The v1 checker ignores them.

## Changelog

| Version | Date | Changes |
|---|---|---|
| 0.4.1 | 2026-10-12 | Added Complete example 2, built from Section Tally data (Winter session, online section with no meetings, cancelled section, full section with a waitlist). Renamed the first example to Complete example 1. |
| 0.4 | 2026-10-11 | Added `student.registered_crns`; Section Tally fields (`cancelled`, `instructors`, `part_of_term`, waitlist and reserved-seat fields, `schedule.term_code`) and a Section Tally mapping table; scope statement and harness mapping; validation rules split into schema-checked, script-checked, and reviewer-checked; scenario families with reversal pairs and set-based expected goals (Appendix A); example term corrected to `Spring 2027` and catalog year to `Fall 2023`; new open items 10 and 11; companion JSON Schema. |
| 0.3 | | Input format separated from answer key. `ground_truth` and `case_id` moved to the appendix. |
| 0.2 | | Field tables for every section, `rule` field on requirements, transfer courses, `W` and `TA` grades, campus and modality on sections. |