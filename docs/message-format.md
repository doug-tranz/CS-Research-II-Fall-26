# The cart message (DIDComm schema)

**Status:** draft for group review (Task 2, Khalil, drafted with Claude, 2026-10-07).
Items marked **[proposed]** are not team decisions yet.

The one message a model must produce: a request to register a student for a semester's set
of sections (a "cart"), written as a DIDComm v2 plaintext message. This answers open
question 4 in the design (§13). It does not define the inputs (Task 1) or what makes a
cart correct for an intent (Task 3).

```json
{
  "id": "3f2b8c1e-7a4d-4e9b-9c55-0d1e2f3a4b5c",
  "type": "https://example.org/course-registration/1.0/register",
  "body": {
    "student_id": "S001",
    "term_code": "202640",
    "sections": [
      {
        "crn": "41990", "course_id": "CS04114", "section": "4", "credit_hours": 3,
        "seats_total": 28, "seats_taken": 27, "campus": "Glassboro", "modality": "in-person",
        "meetings": [{"days": ["Monday", "Wednesday"], "start_time": "11:00 AM",
                      "end_time": "12:15 PM", "type": "Class", "location": "James Hall 2113"}]
      },
      {
        "crn": "44459", "course_id": "CHEM05502", "section": "1", "credit_hours": 2,
        "seats_total": 10, "seats_taken": 2, "campus": "Glassboro", "modality": "in-person",
        "meetings": [{"days": ["Monday"], "start_time": "02:00 PM", "end_time": "03:15 PM",
                      "type": "Class", "location": "James Hall 2095"}]
      }
    ]
  }
}
```

A single enrollment is a cart with one section. There is no separate one-section message.

Earlier versions, whose results are not comparable with this one:
- **Until 10/7:** a single-section message, `{"student_id", "section_id"}` with type
  `registration/enroll`. The cart's Tiers 1–2 are stricter.
- **10/7 to 10/14:** the cart with `sections` as a bare list of crns and no `term_code`.
- **10/14:** section objects holding only `crn`, plus optional `credit_hours` and
  `waitlist`. Results logged then (in git history) are in this shape or the one before; the
  current schema rejects both as `schema_error`.

## Envelope (DIDComm v2 headers)

| Field | Required | Rule |
|---|---|---|
| `id` | yes | Non-empty string. Should be unique per message; the validator cannot check uniqueness from one message. |
| `type` | yes | A DIDComm message type URI, `<base>/<protocol>/<version>/<message>`. For this message it must be exactly the URI below. |
| `body` | yes | JSON object. |
| `typ` | no | If present, exactly `application/didcomm-plain+json`. |
| `from` | no | If present, a DID (`did:<method>:<id>`). |
| `to` | no | If present, an array of DIDs. |
| `created_time`, `expires_time` | no | If present, integers (UTC epoch seconds). An ISO date string is an error. |
| `thid`, `pthid` | no | If present, non-empty strings. |
| `attachments` | no | If present, an array of objects, each with a `data` object holding `base64`, `json`, or `links`. |
| any other header | no | Allowed and ignored, as DIDComm requires of receivers. |

The optional headers are accepted because they are legal DIDComm. The prompt does not ask
for them, and the registration system does not read them.

## Body

| Field | Required | Rule |
|---|---|---|
| `student_id` | yes | String with no whitespace. |
| `term_code` | yes | Six digits, as a string: the term being registered for, copied from the input's `schedule.term_code` (e.g. `202640`, Fall 2026). |
| `sections` | yes | Array of section objects: at least one, no crn twice. |
| anything else | | Not allowed. An invented field such as `total_credits` is a schema error. |

Each section object:

| Field | Required | Rule |
|---|---|---|
| `crn` | yes | The section's `crn` from the input. |
| `course_id` | yes | Copied from the section, e.g. `CS04114` (same pattern as the input). |
| `section` | yes | Copied: the section number, a string, e.g. `"4"`. |
| `credit_hours` | yes | A number, 0 or more: the credits the student takes. For a fixed-credit section, its credits (271 tally sections have 0, e.g. labs); for a variable-credit section (182 tally sections, e.g. 1–12 for independent study), a number in its range. |
| `seats_total`, `seats_taken` | yes | Copied: integers. |
| `campus` | yes | Copied. |
| `modality` | yes | Copied: `in-person`, `online` or `hybrid`. |
| `meetings` | yes | Copied: an array, empty if the section has none. Each meeting has `days` (full day names), and `start_time`/`end_time` (`h:mm AM/PM` or `hh:mm AM/PM`) when it has times; `type` and `location` are optional. |
| `waitlist` | no | `true` to join the waitlist of a full section. Absent means `false`. |
| anything else | | Not allowed. |

`didcomm_schema.SECTION_FIELDS` lists the required fields; `OUTPUT_CONTRACT` tells the model
to copy them from the offered section. The copied fields make the model restate what it
registers for, so a crn that does not belong to the section it means is caught.

The schema checks shape only. Tier 3 checks the values against the input:

| Rule | Fails as |
|---|---|
| `term_code` is the schedule's term | `consistency_error` |
| Every copied field equals the offered section's: `course_id`, `section`, `campus` and `modality` ignoring case and surrounding spaces; `seats_total` and `seats_taken` exactly; `meetings` with the same days and times, in any order and with or without a leading zero, and `type` and `location`, where given, matching (they may be left out) | `consistency_error` |
| `credit_hours` equals a fixed-credit section's credits, or is inside a variable-credit section's range | `consistency_error` |
| `waitlist: true` only on a section that is full and has a waitlist with room | `consistency_error` |
| A full section without `waitlist: true` | `consistency_error` (as before) |

Tier 4 compares each section's `waitlist` with the answer key (waitlisting when the student
didn't ask is wrong, and so is not waitlisting when they did), and `credit_hours` whenever
the answer key gives it.

## Message type URI [proposed]

`https://example.org/course-registration/1.0/register`

DIDComm requires `type` to be a URI in this form. `example.org` is a placeholder;
`https://didcomm.org/` is reserved for community protocols, so it should not be used. The
old `"registration/enroll"` is not a valid message type URI. The URI is written once, in
the schema file.

## Which tier a format failure belongs to

| Tier | Outcome | Fails when | Schema part |
|---|---|---|---|
| 1 | `syntactic_error` | The output is not JSON, or it breaks an envelope rule. | `$defs/envelope` |
| 2 | `schema_error` | It is a DIDComm message, but `type` is not ours or the body breaks a body rule. | `$defs/register` |

The rule: Tier 1 asks "is this a DIDComm message at all?" and Tier 2 asks "is it our
register message?". This keeps formatting failures separate from wrong-shape failures, as
the design asks (§10).

`validate_message()` returns `outcome`, `detail`, `errors` (every violation, as
`<path>: <reason>`), and `body` (set only on `schema_ok`).

| Output | Outcome |
|---|---|
| Prose or a code fence around the JSON | `syntactic_error` |
| A JSON array, or an object with no `id` | `syntactic_error` |
| `"type": "registration/enroll"` | `syntactic_error` |
| `"from": "alice"` | `syntactic_error` |
| `"type"` of another protocol | `schema_error` |
| Old body `{"student_id", "section_id"}` | `schema_error` |
| `"sections": ["41836"]` (bare crns) or `[{"crn": "41836"}]` (crn only), the earlier shapes | `schema_error` |
| No `term_code`, or `"term_code": 202640` as a number | `schema_error` |
| `"sections": []`, or the same crn twice | `schema_error` |
| A section object missing a required field (`crn`, `meetings`, `credit_hours`, …), or with an unknown field | `schema_error` |
| `"credit_hours": "3"`, `"seats_taken": "20"` or `"waitlist": "yes"` | `schema_error` |
| `"course_id": "CS 04114"`, `"modality": "remote"`, `"days": ["MW"]` or a 24-hour time | `schema_error` |
| A meeting with `start_time` but no `end_time`, or an unknown meeting field | `schema_error` |
| Extra body field | `schema_error` |

`python didcomm_schema.py` runs these and more; all 34 give the expected outcome.
