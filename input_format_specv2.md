# Registration Input Format Specification

**Version:** 0.3 (draft)
**Status:** For group review. Items marked *provisional* are proposals, not group decisions (see "Open items").

## Purpose

This specification defines the JSON input format used by the registration agent. One input file describes one registration situation: the student, their transcript, their degree program requirements, the course sections being offered, and the student's registration request.

The format is designed so that the registration agent can use the provided information to determine whether a requested course or section is appropriate and whether the requested registration is possible. It is also designed so that the same file can seed the checker's database, so the model and the checker always see the same situation.

## Top-level structure

| Field | Type | Required | Description |
|---|---|---|---|
| `student` | object | Required | Student identity and program. |
| `transcript` | object | Required | Completed, transfer, and in-progress courses. |
| `program_requirements` | array | Required | Requirements the student must satisfy. |
| `schedule` | object | Required | Sections offered in the target term. |
| `prompt` | string | Required | The student's natural-language request. |

Everything in this format is sent to the model. Test-case metadata and the answer key are a separate concern and are not part of this format (see the appendix).

## Conventions

- **Keys** are lowercase `snake_case`.
- **`course_id`** is a string made of the subject code and course number with no space, e.g. `CS04113`. Leading zeros are significant, so a course id is never a number. Lab courses can contain a letter, e.g. `PHYSL0220`. Pattern: `^[A-Z]{2,4}[A-Z0-9]{5}$`.
- **Credits** use the key `credit_hours` everywhere and are numbers (`4`, not `"4.000"`).
- **Terms** are written `Season YYYY`, e.g. `Fall 2026`.
- **Grades** are one of the values below.
- **Derived values are not stored.** Quality points, GPA, and credit totals can be computed from the data, so they do not appear in the input.

### Allowed grades

| Grade | Meaning | Counts as earned? |
|---|---|---|
| `A`, `A-`, `B+`, `B`, `B-`, `C+`, `C`, `C-`, `D+`, `D`, `D-` | Passing letter grades, listed highest to lowest | Yes |
| `F` | Failing | No |
| `W` | Withdrawn (attempted, not earned) | No |
| `TA` | Transfer credit accepted | Yes |

Letter grades are ordered as listed, which is how `minimum_grade` is compared. `W` never satisfies a minimum. How `TA` compares to a minimum grade is an open item.

## Student

Basic information about the student and the program they are in.

| Field | Type | Required | Description |
|---|---|---|---|
| `student_id` | string | Required | Anonymized unique identifier, e.g. `S001`. Never a real student id. |
| `degree` | string | Required | Degree being pursued, e.g. `Bachelor of Science`. |
| `major` | string | Required | Student's major. |
| `minor` | string | Optional | Student's minor. |
| `program_id` | string | Required | Program code, e.g. `O701`. |
| `catalog_year` | string | Required | Term of the program guide that applies, e.g. `Fall 2026`. Requirements change by year. |

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
| `term` | string | Required | Term being registered for, e.g. `Fall 2026`. |
| `sections` | array | Required | Sections offered. |

### Section

| Field | Type | Required | Description |
|---|---|---|---|
| `crn` | string | Required | Unique section identifier. **This is the id the model's registration message must use.** |
| `course_id` | string | Required | See conventions. |
| `section` | string | Required | Section number, e.g. `01`. |
| `title` | string | Optional | Course title. |
| `credit_hours` | number | Required | Credits for the section. |
| `seats_total` | integer | Required | Capacity. |
| `seats_taken` | integer | Required | Seats already filled. |
| `campus` | string | Required | Campus where it meets, e.g. `Glassboro`. |
| `modality` | string | Required | `in-person`, `online`, or `hybrid`. |
| `meetings` | array | Required | Meeting times (may be empty for fully asynchronous sections). |

The file stores `seats_total` and `seats_taken` as the source of truth. The harness may show the model a derived `seats_left` (`seats_total - seats_taken`).

### Meeting

| Field | Type | Required | Description |
|---|---|---|---|
| `days` | array of string | Required | Full day names, e.g. `["Monday", "Wednesday"]`. |
| `start_time` | string | Required | `h:mm AM/PM`, e.g. `10:00 AM`. |
| `end_time` | string | Required | `h:mm AM/PM`. |
| `type` | string | Optional | e.g. `Lecture`, `Lab`. |

Two sections conflict when they share a day and their time ranges overlap.

## Prompt

A string holding the student's request in everyday language, e.g. `"Enroll me in CS05000."`.

Guidelines for writing prompts:

1. For a basic consistency case, name the course directly.
2. For a semantic case, put the fact the model must notice **only in the prompt**, in everyday wording. Do not also add it as a data field, or the case stops testing whether the model noticed it.
3. Make sure the schedule contains at least one section that passes every consistency rule but is wrong because of that fact.

Examples:

- `"Enroll me in CS05000. I live in Glassboro."` The model should not pick a section at a satellite campus.
- `"Register me for in-person classes."` The model should not pick online or hybrid sections.

## Validation rules

1. The file is valid JSON.
2. Every `crn` is unique within the case.
3. `seats_taken` is between 0 and `seats_total`.
4. Every `course_id` matches the course id pattern.
5. Every `req_id` is unique within the case.
6. Every `rule` is one of the allowed values, and every `minimum_grade` is a letter grade from the ordered list.
7. `courses_in_progress` entries have no `grade`.
8. Requirement and transcript courses do not need to appear in the schedule. A student can need a course that is not offered this term.
9. The file contains no answer information. Expected answers live in the test-case wrapper, not in the input.
10. Facts that a semantic test depends on appear only in `prompt`.

## Complete example

A synthetic student. This example uses no real student data.

```json
{
  "student": {
    "student_id": "S001",
    "degree": "Bachelor of Science",
    "major": "Computer Science",
    "minor": "Mathematics",
    "program_id": "O701",
    "catalog_year": "Fall 2026"
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
    "term": "Fall 2026",
    "sections": [
      {
        "crn": "12345",
        "course_id": "CS05000",
        "section": "01",
        "title": "Introduction to Software Engineering",
        "credit_hours": 3,
        "seats_total": 30,
        "seats_taken": 25,
        "campus": "Glassboro",
        "modality": "in-person",
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

In this case both `CS05000` sections are open and conflict-free, so both pass the consistency checks. The prompt says the student lives in Glassboro, so the Camden section is the "valid but wrong" choice described in the design document.

## Appendix: test-case wrapper (not part of the input format)

For scoring, a test-case file wraps one input object with metadata and an answer key. This is defined by the test-case specification, not by this document. The sketch below is provisional.

```json
{
  "case_id": "sem_001",
  "input": { "...": "the input object defined above" },
  "ground_truth": {
    "acceptable_crns": ["12345"],
    "implicit_relationship": "The student lives in Glassboro, so the Camden section (12348) is valid but wrong."
  }
}
```

The harness sends only `input` to the model. `case_id` and `ground_truth` never reach it.

## Open items

These are not settled and should be confirmed with the group.

1. **Section id.** This spec uses `crn` as the id in the registration message. The current `registration.py` uses a `section_id` such as `CS565-01`.
2. **Meeting times.** `registration.py` currently checks conflicts with a slot code such as `MW14`. The structured `meetings` here need either a derived slot code or an overlap check.
3. **One section or several per message.** This spec assumes one enroll message per section.
4. **Prerequisites.** Not represented yet. Open whether they are a consistency rule or part of semantic correctness. The program guide requires a `C-` or better in several courses before taking courses that depend on them.
5. **`TA` and `minimum_grade`.** How a transfer grade compares to a minimum grade is undecided.
6. **Requirement types not covered in v1.** Attribute-based requirements (Rowan Core and Experience), one course counting toward several requirements, repeatable courses (e.g. `CS01395`), capped credits, variable-credit courses, and course equivalents across catalog years.
7. **Graduation-wide rules.** 120 total credits, 2.0 GPA, and 30 credits at the institution are not represented.
8. **Other input renderings.** The whiteboard notes "json/csv/NL". JSON is canonical here. CSV or natural-language renderings of the same data are not specified.
9. **Test-case wrapper.** The shape of `ground_truth` and where `case_id` lives are left to the test-case specification. The appendix is only a sketch.