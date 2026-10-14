# CSR2 — Evaluating LLM-Generated Transactions

Measuring how well currently-deployed LLMs generate correct transactions, expressed as
DIDComm-format JSON messages. A model is given a student's transcript, their program
requirements, a portion of the school's class schedule, and the student's request, and must
produce a registration message. The harness scores that message on four tiers, from "is it
valid DIDComm" to "does it do what the student asked".

This README covers running the code, the message schema, the input format, the test cases
and results so far, and the full project design.

Contents: [What's here](#whats-here) · [Setup](#setup) · [Run](#run) ·
[Read the results](#read-the-results) · [Troubleshooting](#troubleshooting) ·
[The cart message](#the-cart-message-didcomm-schema) · [The input format](#the-input-format) ·
[Correctness model](#correctness-model) · [Results so far](#results-so-far) ·
[Open questions](#open-questions-for-the-group) · [Provisional](#provisional-will-change-isolated-to-one-spot-each) ·
[Design](#design)

## What's here

| File | Role |
|---|---|
| `harness.py` | Core, domain-agnostic: backends (Ollama local, OpenRouter cloud), `generate()`, `generate_with_tools()` (lets the model call tools before it answers), DIDComm envelope parsing, logging. |
| `registration_input.schema.json` | The input format rules, as JSON Schema: student, transcript, program requirements, schedule, prompt. See [The input format](#the-input-format). |
| `registration_message.schema.json` | The message rules, as JSON Schema (draft 2020-12). Source of truth for the cart message. |
| `didcomm_schema.py` | `validate_message()`: Tiers 1–2 against the message schema. `OUTPUT_CONTRACT`: the prompt text that tells a model the format. |
| `registration.py` | Registration domain testbed: SQLite schema, `evaluate()` (Tiers 1–2 from `didcomm_schema`, then Tier 3 consistency over the whole cart). Also the input format: `validate_input()`, `audit()` (degree audit), `missing_prerequisites()`, and `seed_db()`. |
| `run_semantic_eval.py` | The pipeline. Builds each case's prompt (as JSON, CSV or plain text), sends it to a model, and scores the answer on all four tiers; `check_semantic()` (Tier 4) and `evaluate_case()` live here. |
| `input_format_cases.json` (15) | The test cases: each an input object plus an answer key. In every case the model searches the whole Section Tally itself (`"schedule_from_tally": {"search": true}`). |
| `section_tally.py` | Reads Rowan's Section Tally export, turns rows into input-format sections, and provides the `search_sections` tool the model uses to look sections up. See [Sections from the Section Tally](#sections-from-the-section-tally). |
| `section_tally_fall2026.csv` | The Fall 2026 Section Tally export, as downloaded (renamed). The pipeline reads this one file. |
| `section_tally/` (138 files, optional) | The same data split by subject (`CS.csv`, `MATH.csv`, …), for browsing only; `python section_tally.py split` regenerates it. The pipeline does not read it, so it can be deleted. |
| `semantic_eval_results.json` | Generated results of `run_semantic_eval.py`, as an indented JSON array. |
| `run_registration.py` | A runner for one built-in task: generates, scores Tiers 1–3, logs to `runs.jsonl`. |
| `probe.py` | Historical raw-Ollama diagnostic (kept for reference). |

Earlier work is in git history, not in the working tree: the single-section message
(`old_schema/`) and the 30 cases whose input was a bare list of sections (`test_cases.jsonl`,
`semantic_test_cases.jsonl`, `cart_test_cases.jsonl`). The last commit with all of them is
`e02b9f1`; for example `git show e02b9f1:cart_test_cases.jsonl`.

## Setup

OpenRouter lets the harness call many LLMs (Claude, GPT, Llama, …) with one API key.
You do **not** need Ollama installed to run on OpenRouter.

### 1. Get the code

```bash
git clone https://github.com/doug-tranz/CS-Research-II-Fall-26.git
cd CS-Research-II-Fall-26
git checkout ben-test-branch
```

Already cloned? Just `git checkout ben-test-branch` and `git pull`.

### 2. Set up Python (once)

**Windows (PowerShell):**
```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

**Mac / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

You'll see `(venv)` at the start of your prompt when it's active. Activate it again
(the second line) every time you open a new terminal. `requirements.txt` is `requests`
plus `jsonschema` (needed by both schemas).

For local runs only, also install Ollama and run `ollama pull llama3.1:8b`.

### 3. Install your OpenRouter key (once)

Get your key from Khalil. It starts with `sk-or-v1-`.

Copy the command below **exactly as it is** and run it. It asks you to paste your key,
so you don't edit the command itself.

**Windows (PowerShell):**
```powershell
[Environment]::SetEnvironmentVariable("OPENROUTER_API_KEY", (Read-Host "Paste your OpenRouter key"), "User")
```

**Mac / Linux (Terminal):**
```bash
read -rp "Paste your OpenRouter key: " k && echo "export OPENROUTER_API_KEY=\"$k\"" >> ~/.zshrc && unset k
```
(If your terminal uses bash instead of zsh, change `~/.zshrc` to `~/.bashrc`.)

When it asks, paste your key and press **Enter**. It prints nothing if it worked.

**Then close and reopen VS Code and all terminals.** Already-open windows won't see the key.

Check it (this doesn't print the key):
```powershell
# Windows
if ($env:OPENROUTER_API_KEY) { "key is set" } else { "not set" }
```
```bash
# Mac / Linux
[ -n "$OPENROUTER_API_KEY" ] && echo "key is set" || echo "not set"
```

**Keep the key secret.** The key spends real money.
- Never put it in code, commit it, or paste it in a group chat.
- Crop or blur it out of screenshots.
- If it leaks, tell Khalil right away so it can be deleted.

## Run

All commands are run from the repo root with `(venv)` active.

### The test cases, all four tiers

```bash
python run_semantic_eval.py --model anthropic/claude-opus-5.5
python run_semantic_eval.py --model anthropic/claude-opus-5.5 --task-ids tal_001 -n 5
python run_semantic_eval.py --model anthropic/claude-opus-5.5 --rendering nl
```

| Option | Meaning | Default |
|---|---|---|
| `--backend` | `openrouter` (cloud) or `ollama` (local) | `openrouter` |
| `--model` | One model ID (required unless `--check`) | |
| `--test-file` | Which case file to run | `input_format_cases.json` |
| `--task-ids` | Run only these cases | all cases in the file |
| `-n` | Samples per case | `1` |
| `--rendering` | How a case's state is shown to the model: `json`, `csv` or `nl` (plain text) | `json` |
| `--check` | Score each case's own answer key and exit. No model call | off |
| `--output` | Results file | `semantic_eval_results.json` |

Appends every sample to the results file. A search-mode case makes several model calls
(the searches, then the answer): tal_004 took 2 calls, 6 searches and about $0.04 on Opus 5.5.
A case with the schedule written into the prompt costs about $0.01–0.02 per call.

### The validators alone

```bash
python didcomm_schema.py               # 23 example messages, no model, no cost
python run_semantic_eval.py --check    # every case and its answer key, no model, no cost
python section_tally.py check          # every tally section converts and fits the input schema
```

### The Section Tally

```bash
python section_tally.py show CS --crns 41836 41837   # those sections in the input format
python section_tally.py split                        # optional: section_tally/<SUBJ>.csv, for browsing
```

### One built-in task, several samples

```bash
python run_registration.py --backend openrouter --models anthropic/claude-haiku-4.5 -n 5
```

| Option | Meaning | Default |
|---|---|---|
| `--backend` | `openrouter` (cloud) or `ollama` (local) | `ollama` |
| `--models` | One or more model IDs, separated by spaces | `llama3.1:8b` |
| `-n` | How many times to run each model (samples) | `5` |

Running with no options uses local Ollama, which only works if you have Ollama installed.
Scores each sample on Tiers 1–3 and appends rows to `runs.jsonl`.

### Change the model

Model IDs are the `provider/model` names on <https://openrouter.ai/models>. Click a model
and copy the ID under its name. Spelling must be exact. `run_registration.py` takes several
at once and runs them one after another:

```bash
python run_registration.py --backend openrouter --models anthropic/claude-sonnet-5 openai/gpt-4o-mini meta-llama/llama-3.1-8b-instruct -n 5
```

Include `meta-llama/llama-3.1-8b-instruct` to compare against local `llama3.1:8b` (design §4.1).

Models we've already tried (price is per call on the early single-section task; the current
cases are longer, so expect more):

| Model ID | Tier | ~Cost per call |
|---|---|---|
| `anthropic/claude-haiku-4.5` | low | $0.0006 |
| `anthropic/claude-sonnet-5` | mid | $0.0013 |
| `anthropic/claude-opus-5.5` | high | $0.004 |
| `anthropic/claude-fable-5.1` | top | $0.007 (often blocked, see Troubleshooting) |
| `meta-llama/llama-3.1-8b-instruct` | same model as our local Ollama run | very cheap |

**Watch the cost.** Cost ≈ price per call × number of models × number of calls. Try new
models with `-n 1` or a couple of `--task-ids` first. Avoid very expensive models unless the
group agrees.

## Read the results

`run_semantic_eval.py` prints one line per case (one per sample when `-n` is above 1), then
a total:

```
tal_001 run 0: semantic_ok          | matches ground truth exactly

5/5 semantic_ok on anthropic/claude-opus-5.5. Cost $0.1005. Full results in semantic_eval_results.json
```

`run_registration.py` prints one line per sample:

```
run 0: consistent__semantic_TBD   | invariants hold; ... | $0.001334
   raw: {"id": "...", "type": "https://example.org/course-registration/1.0/register", "body": ...
```

| Outcome | Tier | Meaning |
|---|---|---|
| `syntactic_error` | 1 | Not a DIDComm message: not valid JSON (e.g. the model wrapped it in a ```` ```json ```` code block, or returned nothing), or no `id`, or a malformed `type` |
| `schema_error` | 2 | A DIDComm message, but not our register message: wrong `type`, or the body isn't exactly `student_id`, `term_code` and a `sections` list of section objects |
| `consistency_error` | 3 | Breaks a rule, e.g. full section, already enrolled, time conflict, unmet prerequisite |
| `semantic_error` | 4 | Passes every rule, but the cart is not one the intent asked for |
| `semantic_ok` | 4 | Correct |
| `consistent__semantic_TBD` | 3 | `run_registration.py` only: passed Tiers 1–3; that runner has no expected answer to check intent against |
| `GENERATION FAILED` / `generation_failed` | | The call itself failed; the error message says why |

Where the full rows go, each with the prompt, the model's raw answer, tokens, latency, cost
and the verdict:

- `run_semantic_eval.py` → `semantic_eval_results.json`, an indented JSON array. Next to each
  verbatim string is a readable version: `prompt_sent` (the exact text sent) is followed by
  `prompt_readable`, split into `known_state` (the input, including the schedule, as an
  indented object; as lines for the CSV and plain-text renderings), `intent` and
  `output_contract`; `raw_output` (the exact text returned) is followed by `parsed_output`.
- `run_registration.py` → `runs.jsonl`, one line per sample.

## Troubleshooting

| You see | Fix |
|---|---|
| `OpenRouter needs an API key` | Key not set, or you didn't restart VS Code/terminals after setting it (Setup step 3) |
| `OpenRouter 401` | Key is wrong or was deleted. Ask Khalil for a new one |
| `OpenRouter 402` | Out of credits, or your key hit its spending limit. Tell Khalil |
| `OpenRouter 400 ... not a valid model ID` | Typo in the model ID. Copy it from openrouter.ai/models |
| `ModuleNotFoundError: No module named 'requests'` or `'jsonschema'` | venv isn't active, or `pip install -r requirements.txt` hasn't been run since `jsonschema` was added (Setup step 2) |
| Blank `raw:` with `syntactic_error` | The model refused the request (seen with Fable 5.1). Not a formatting problem; we don't log refusals separately yet |

## The cart message (DIDComm schema)

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
      {"crn": "60001"},
      {"crn": "46497", "credit_hours": 3},
      {"crn": "44033", "waitlist": true}
    ]
  }
}
```

A single enrollment is a cart with one section. There is no separate one-section message.

Earlier versions, whose results are not comparable with this one:
- **Until 10/7:** a single-section message, `{"student_id", "section_id"}` with type
  `registration/enroll`. The cart's Tiers 1–2 are stricter.
- **10/7 to 10/14:** the cart with `sections` as a bare list of crns and no `term_code`. The
  results in `semantic_eval_results.json` up to now are in that shape.

### Envelope (DIDComm v2 headers)

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

### Body

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
| `credit_hours` | for a variable-credit section | A number above 0: the credits the student takes. Required when the section's credits are a range (182 tally sections, e.g. 1–12 for independent study). For a fixed-credit section it may be left out, or must equal the section's credits. |
| `waitlist` | no | `true` to join the waitlist of a full section. Absent means `false`. |
| anything else | | Not allowed. |

The schema checks shape only. Tier 3 checks the values against the input:

| Rule | Fails as |
|---|---|
| `term_code` is the schedule's term | `consistency_error` |
| A variable-credit section has `credit_hours`, inside its range; a fixed-credit section's `credit_hours`, if given, equals its credits | `consistency_error` |
| `waitlist: true` only on a section that is full and has a waitlist with room | `consistency_error` |
| A full section without `waitlist: true` | `consistency_error` (as before) |

Tier 4 compares each section's `waitlist` with the answer key (waitlisting when the student
didn't ask is wrong, and so is not waitlisting when they did), and `credit_hours` whenever
the answer key gives it.

### Message type URI [proposed]

`https://example.org/course-registration/1.0/register`

DIDComm requires `type` to be a URI in this form. `example.org` is a placeholder;
`https://didcomm.org/` is reserved for community protocols, so it should not be used. The
old `"registration/enroll"` is not a valid message type URI. The URI is written once, in
the schema file.

### Which tier a format failure belongs to

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
| `"sections": ["41836"]` (bare crns, the earlier shape) | `schema_error` |
| No `term_code`, or `"term_code": 202640` as a number | `schema_error` |
| `"sections": []`, or the same crn twice | `schema_error` |
| A section object without `crn`, or with an unknown field | `schema_error` |
| `"credit_hours": "3"` or `"waitlist": "yes"` | `schema_error` |
| Extra body field | `schema_error` |

`python didcomm_schema.py` runs these and more; all 23 give the expected outcome.

## The input format

**Status:** implements Anand's *Registration Input Format Specification*, v0.3 (draft, Task 1).
That document is the reference for the base fields and is not in this repo yet. Everything
marked **[provisional]** below fills one of its open items and is not a team decision.

One input object describes one registration situation, and all of it is sent to the model.

| Field | Required | Content |
|---|---|---|
| `student` | yes | `student_id`, `degree`, `major`, optional `minor`, `program_id`, `catalog_year`. |
| `transcript` | yes | `completed_courses` (with grades, including `W`), `transfer_courses`, `courses_in_progress` (no grade). |
| `program_requirements` | yes | Each has `req_id`, `category`, `rule`, `course_options`, `credit_hours`, `minimum_grade`. |
| `schedule` | yes | `term`, `term_code` (six digits, e.g. `202640`; the message must copy it), and `sections`. A section has `crn`, `course_id`, `section`, `credit_hours`, `seats_total`, `seats_taken`, `campus`, `modality`, `meetings` (days, optional `start_time`/`end_time`, `location`, `type`), plus optional fields from the Section Tally (see below). |
| `prompt` | yes | The student's request in everyday language. |
| `prerequisites` | no | Added for open item 4. See below. |
| `course_equivalents` | no | Added for open item 6. |
| `graduation_rules` | no | Added for open item 7. |

`registration_input.schema.json` holds the field rules (types, the course id pattern, allowed
grades, `Season YYYY` terms, `h:mm AM/PM` times, no unknown fields).
`registration.validate_input()` adds the rules a schema cannot express: every `crn` and
`req_id` is unique, `seats_taken` is at most `seats_total`, and a meeting ends after it
starts. Because unknown fields are rejected, an answer key placed inside the input is caught.

How one input object is used:

- **Prompt.** `run_semantic_eval.to_task()` sends everything except `prompt` as the known
  state, and `prompt` as the intent. The answer key is never included.
- **Database.** `registration.seed_db()` builds the checker's database from the same object,
  so the model and the checker see the same situation.
- **Scoring.** `run_semantic_eval.evaluate_case()` runs all four tiers.

A test case wraps one input object with an id and an answer key. Where the sections come
from is set by the case:

| Case gives | The model sees | Who picks the sections |
|---|---|---|
| `"schedule_from_tally": {"search": true}` (every case in this repo) | Only the term. It must find sections with the `search_sections` tool, over the whole tally | The model |
| `"schedule_from_tally": {"crns": [...], "courses": [...]}` | Those sections, listed in the prompt | The case author |
| `input.schedule` written out | That schedule | The case author |

```json
{
  "case_id": "tal_004",
  "input": { "student": {}, "transcript": {}, "program_requirements": [], "prompt": "Register me for two classes that count toward my ..." },
  "schedule_from_tally": { "search": true },
  "ground_truth": { "rules": { "count": {"min": 2, "max": 2}, "courses_count_toward": ["cs_electives"], "no_days": ["Friday"] } }
}
```

`run_semantic_eval.load_test_cases()` fills in `input.schedule` from
`section_tally_fall2026.csv` when it loads the file (`resolve_case()`). In search mode that is
the whole tally (5,129 sections, cancelled ones flagged): the checker's database holds every
section, so whatever the model picks is checked against real data, but the prompt carries only
the term. A case gives `schedule` or `schedule_from_tally`, never both. With hand-picked
sections, `crns` come first in the order given, then every section of each of `courses`;
`include_cancelled` offers cancelled ones too.

### The search tool

In search mode the model gets one tool, `search_sections` (`section_tally.SEARCH_TOOL`), and
the prompt says the sections are not listed and must be looked up. The harness runs the loop
(`harness.generate_with_tools()`): each time the model calls the tool, the search runs and
the result goes back, until the model replies with its message (at most 12 model calls).

| Argument | Meaning |
|---|---|
| `course_id` | Exact course id, e.g. `CS04114` (a space is ignored) |
| `subject` | Subject code, e.g. `CS` |
| `title_contains` | Words that must all be in the title. Tally titles are abbreviated (`DES-ANLYS ALGORITHMS`), so this misses more than the others |
| `open_only` | Only sections with a free seat |
| `page` | Results come 40 at a time; a note says when there are more |

Each result is a list of input-format sections (crn, seats, credits, meetings, waitlist, …).
Cancelled sections are never returned. A bad call (no filter, an unknown argument) comes back
to the model as an error it can correct.

The log records the search: each result row has `model_calls` and `steps`, one entry per tool
call with its arguments, the number of matches and the crns returned. Tokens, cost and latency
are summed over all the calls.

What this changes in what is measured: the model now has to decide which courses to look up
from the transcript and requirements, so a wrong answer can come from searching badly as well
as from choosing badly. The steps show which.

Cases are picked (`--task-ids`) before the export is read, so a case that does not use the
tally runs even if the export is missing; a case that uses it stops with a clear error.

### Sections from the Section Tally

The schedule can come from Rowan's real Section Tally (Fall 2026, 5,129 sections, 138
subjects) instead of invented sections. The data is one file, `section_tally_fall2026.csv`:
the export as downloaded, renamed, read whole on each run (about 20 ms). A case picks its
sections with `schedule_from_tally` (above), which calls `section_tally.select_schedule()`;
`section_tally.load_schedule("CS")` gives a whole subject. All 5,129 sections convert and
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
  86,000 characters as JSON (about 37,000 as CSV), against 5,000–10,000 for today's cases. A
  case should carry a chosen portion: today's tally cases use 4–6 sections each, about 3,500
  characters.
- **The `*` after `Room Cap`** (296 sections) is not documented in the export.
- **Only the Fall term code is known.** `TERM_SEASONS` in `section_tally.py` maps the last two
  digits (`40` → Fall); others are added when a tally for that term shows its dates.
- **Reserved seats are shown but not checked.** Waitlists are checked: the message can ask
  to join one (see the message body).

### How the spec's open items were filled in [provisional]

| Spec open item | What the code does |
|---|---|
| 1. Section id | Each section object in the message names its section by `crn`. The prompt tells the model so. |
| 2. Meeting times | Two sections conflict when they share a day and their time ranges overlap (`meetings` table in `registration.py`). |
| 3. One section or several per message | Several: the spec assumed one enroll message per section, but the 10/1 meeting settled on a cart, so one message registers every section. |
| 4. Prerequisites | New optional `prerequisites` list. Treated as a **consistency** rule (Tier 3): registering for a course whose prerequisite is unmet is a `consistency_error`. A course still in progress does not satisfy a prerequisite. |
| 5. `TA` and `minimum_grade` | Accepted transfer credit (`TA`) meets every minimum grade. `F` and `W` meet none. |
| 6. Other requirement types | New optional fields, all read by `registration.audit()`. See the next table. |
| 7. Graduation-wide rules | New optional `graduation_rules`: `total_credits`, `minimum_gpa`, `residency_credits`. The audit derives earned credits, institutional credits and GPA from the transcript; none is stored. A repeated course counts once, by its best attempt. |
| 8. Other input renderings | `--rendering csv` shows the same state as CSV tables, and `--rendering nl` as plain-text sentences. JSON stays the stored form. |
| 9. Test-case wrapper | `acceptable_crns` lists sections that are each a correct one-section cart. `acceptable_carts` lists whole carts, for answers with several sections. A section is written as its crn (`"41836"`), or as an object when the answer needs more: `{"crn": "46497", "credit_hours": 3}`, `{"crn": "44033", "waitlist": true}`. Tier 4 passes when the cart equals any acceptable one, so a case can have more than one right answer. |

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

What the checker does and does not read:

- **Tier 3 reads** the schedule and the prerequisites (against the transcript).
- **The audit is not part of scoring.** `registration.audit()` and `counts_toward()` report
  which requirements are met and which courses would move one forward. They are for case
  authors and for checking an answer key. Tier 4 still compares the cart to the answer key,
  so whether a course "counts toward the degree" is decided by the author, with the audit
  as a cross-check.

### The fifteen cases

`input_format_cases.json`. In each, the model searches the whole Fall 2026 Section Tally; the
student, transcript, requirements and request are made up. "Listed" answer keys name every
correct cart; "rules" keys say what any correct cart must satisfy.

| Case | Prompt | What it tests | Answer key |
|---|---|---|---|
| sem_001 | "Enroll me in Advanced Cell Biology (BIOL01430). I live in Glassboro, so I want the section on my campus." | Campus: an open section at Rowan College at Burlington is valid but wrong; the other Glassboro section is full. | listed: `46195` |
| con_001 | "Enroll me in CS04225." | Two of three sections are full. | listed: `42070` |
| con_002 | "Enroll me in CS01303 and Calculus II (MATH01131)." | Partial time overlap: one Calculus II section overlaps CS01303 by ten minutes. | rules: 4 correct carts |
| sem_002 | "Register me for Interpersonal Communication (CMS04220) and CS07450. I only want classes that meet in person." | An open online section is valid but wrong. | rules: 2 correct carts |
| sem_003 | "Enroll me in one class that counts toward my Computer Science restricted electives. I don't want to retake anything I've already passed." | Two electives are already passed; two others are full. | rules: 4 correct sections |
| sem_004 | "Register me for two computer science classes that count toward my degree. I work evenings, so nothing that ends after 5 PM." | A prerequisite still in progress, one met by transfer credit, a repeatable course, evening sections. | rules: 2 correct carts |
| sem_005 | "I can only take 6 credits this fall. Register me for CS07450, and add Calculus I (MATH01130) too only if both fit within my limit." | Credit arithmetic: 3 + 4 is over the limit. | listed: `44477` |
| con_003 | "Register me for CS07450 if I meet its prerequisite. If I don't, register me for CS04222 instead so I can retake it." | A prerequisite earned with a D against a C- minimum, so the fallback applies. | rules: 3 correct sections |
| sem_006 | "I need one class for my Health and Society requirement. I'll be away from campus all fall, so it has to be fully online." | Which courses count, then online only; in-person and full online sections are traps. | rules: 5 correct sections |
| sem_007 | "Register me for Advanced Cell Biology (BIOL01430) and CS04222. I can't be on campus on Fridays." | Lecture-and-lab sections: one lab is on Friday, and one CS04222 section overlaps the remaining lab. | rules: 2 correct carts |
| sem_008 | "Register me for every class I still need to finish my Foundational Courses requirement." | One course met under an old id, one earned below the minimum grade, one withdrawn. | rules: 6 correct carts |
| tal_001 | "Register me for Operating Systems. If every section is full, put me on the waitlist for the Tuesday/Thursday section." | Every section is full, so the answer is a waitlist request, for one particular section. | listed: `42090` with `waitlist: true` |
| tal_002 | "Enroll me in CS04114 and CS06205. I don't want any classes on Fridays." | One of each course is full, one meets on Friday and also overlaps. | listed: 2 carts |
| tal_003 | "Sign me up for the Clinical Lab Science Seminar (CHEM05502) for 2 credits, and for a morning section of CS04114." | A variable-credit section (1–3 credits) and a time-of-day choice. | listed: `44459` at 2 credits + `41990` |
| tal_004 | "Register me for two classes that count toward my Computer Science restricted electives. No Friday classes, nothing that starts at 5 PM or later, and don't put me on any waitlists." | Which courses count, then day, time and seats. | rules: 2 correct carts |

All fifteen were written by Claude and have not been checked by a teammate. `python
run_semantic_eval.py --check` confirms each answer key behaves: listed carts score
`semantic_ok`; in a rules case the valid examples pass and the invalid ones fail. For every
rules case I also checked every combination of the real sections a student could reach for
(sections of the named courses, or of courses that count toward the named requirements): the
rules accept exactly the carts in the table.

The sem_ and con_ cases are rebuilt versions of eleven earlier cases that used invented
sections (removed on 10/14; the first six are in git history at `e02b9f1`). Each keeps what
its original tested, on real Fall 2026 sections, with two changes forced by the data:

- **sem_006** tested a requirement satisfied by a course *attribute*; the tally has no
  attributes, so its requirement is now a list of courses (an invented "Health and Society"
  requirement over real courses).
- **sem_001** and **sem_007** both use BIOL01430, the only course in the tally with both a
  second campus and a Friday lab; sem_007 adds a second course so the two cases differ.

Requirements and prerequisites are invented throughout (the tally has neither), so a course's
real prerequisites are not checked unless a case lists them.

## Correctness model

Four tiers, stopping at the first failure:
`syntactic_error` → `schema_error` → `consistency_error` → `semantic_error` / `semantic_ok`.

- **Tiers 1–2, format.** See [The cart message](#the-cart-message-didcomm-schema).
- **Tier 3, consistency** — invariants hold. The term is the schedule's. Each section exists,
  is not cancelled, has a seat (or is full with waitlist room when the message asks to
  waitlist), has valid credits, is not already held by the student, does not clash with
  their enrollments, and has its prerequisites met; inside the cart, no two sections overlap
  in time or belong to the same course.
- **Tier 4, semantic correctness** — the transaction achieves the user's intent. A case's
  answer key is one of two kinds:
    - **Listed carts** (`acceptable_crns`, `acceptable_carts`): the cart must equal one of them
      (`check_semantic()`), ignoring section order.
    - **Rules** (`rules`), for requests with too many correct carts to list: the cart must
      satisfy every rule (`check_rules()`). `examples.valid` and `examples.invalid` are carts
      `--check` uses to confirm the rules accept and reject what they should.

  | Rule | The cart must… |
  |---|---|
  | `count`, `credits` | have this many sections / total credits (`{"min", "max"}`) |
  | `courses_count_toward` | have only courses that move one of these unmet requirements forward (from the degree audit, so passed courses don't count unless repeatable) |
  | `no_days` | have no meeting on these days |
  | `starts_before`, `ends_by` | have every meeting start before / end by this time |
  | `modality_in`, `campus_in` | use only these modalities / campuses |
  | `waitlist` | waitlist nothing, unless this is `true` |
  | `must_include`, `must_include_courses`, `must_exclude_courses` | contain / not contain these sections or courses |

  The student id and term code are always checked.

## Results so far

Rows are in `semantic_eval_results.json`. OpenRouter, JSON rendering, current message body.

| Date | Model | Case | Samples | Result | Cost |
|---|---|---|---|---|---|
| 2026-10-14 | `anthropic/claude-opus-5.5` | tal_001, sections listed in the prompt | 1 | 1/1 `semantic_ok` (used `waitlist` as asked) | $0.012 |
| 2026-10-14 | `anthropic/claude-opus-5.5` | tal_004, **search mode** | 1 | 1/1 `semantic_ok` | $0.040 |

- In tal_004, Opus looked up each of the six courses in the electives requirement by course id
  (6 searches, sent together in one turn), then answered `44477` + `41892`: 2 model calls,
  6,948 input tokens, 10.9 s.
- tal_001 was run before it switched to search mode; tal_002 and tal_003 have not been run,
  nor have the `csv` and `nl` renderings.

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
  (18 runs), so the cases have not yet shown a difference between models.

The results committed before 10/14 are in git history (`semantic_eval_results.json` at
`e02b9f1`); results for the single-section message are at the same commit in
`old_schema/runs.jsonl`.

## Open questions for the group

1. **Message type URI.** What base URI do we use in place of `example.org`?
2. **Code fences.** A fenced answer is a syntactic error. Do we keep that, or strip fences
   before parsing and count the model as correct? This already decided one of ten results.
3. **Sender identity.** Should the student be the DIDComm `from` header (which needs DIDs in
   the input) in place of `body.student_id`?
4. **Unknown headers.** They are ignored, per DIDComm. Do we want to count them as a
   measured deviation anyway?
5. **Drops and swaps.** Is the scope register-only, or do we need message types for dropping
   or swapping a section?
6. **Credits on fixed-credit sections.** The message may leave `credit_hours` out or repeat
   the section's credits. Should repeating them be required, or forbidden, for consistency?
7. **Waitlisting and time conflicts.** A waitlisted section is still checked for time
   conflicts with the rest of the cart. Is that right, given the student isn't enrolled yet?
8. **What the search tool offers.** It searches by course id, subject and title words. A real
   registration site also filters by day, time, campus and attributes. Each filter we add
   makes the task easier, so the tool's design is part of the experiment.

The design's own open questions are in [§13](#13-open-questions--for-the-group).

## Provisional (will change; isolated to one spot each)

- DIDComm envelope shape → `harness.parse_didcomm()`
- Time-conflict logic (interval overlap) → `registration.check_consistency()`
- Input-format open items: prerequisites as a consistency rule, `TA` meeting any minimum, best-attempt GPA → `registration.py` (`missing_prerequisites()`, `meets_minimum()`, `audit()`)
- Answer-key shape (`acceptable_crns`, `acceptable_carts`) → `run_semantic_eval.acceptable_carts()`
- Tier 4 by comparison to the answer key → `run_semantic_eval.check_semantic()`; by rules → `run_semantic_eval.check_rules()` (`RULES`)
- The search tool: its arguments, 40 results per page, cancelled sections hidden → `section_tally.SEARCH_TOOL`, `search_sections()`
- At most 12 model calls per answer → `harness.generate_with_tools(max_calls=12)`
- In-memory SQLite → `registration.make_db()`
- Cart message type URI (`example.org` placeholder) → `registration_message.schema.json`

## Design

**Project:** How well do currently-deployed LLMs generate correct transactions?
**Course:** CS Research II (Dr. Ivanov)
**Authors:** Ben Dobkin, Claude
**Version:** 1.5
**Status:** Draft for group review — reflects the 9/17 group decisions. (Formerly `transaction_llm_eval_design.md`; merged here unchanged except where the changelog says.) Contains decisions made and open questions to resolve as a group.


### 1. Motivation

The observable phenomenon: LLMs are already being used to generate transactional messages, and this will only increase. What nobody has rigorously measured is *how good they are at it*. That gap is the opening — if we're first into this space, we start from the unanswered question rather than an incremental improvement on prior work. (Related-work task: confirm no prior paper uses LLMs to generate transactional messages; dig into blockchain / crypto-wallet literature specifically.)

The stakes are practical. A transaction that fails loudly is safe — you know it failed. A transaction that is *accepted but wrong* is the one that hurts someone. Measuring where LLMs land on that spectrum is the contribution.

### 2. Research questions

- **RQ1 — Accuracy.** How accurately do LLMs generate correct transactions? (Correctness has two dimensions — §3.)
- **RQ2 — Efficiency and cost.** What does it cost to generate them — compute/latency (local) and dollars per request (cloud)?
- **RQ3 — Tradeoff.** How do different models trade accuracy against cost?

### 3. Scope and definitions

**The model's job.** Given a description of what we know (as JSON) plus an intent, the model produces a **DIDComm-format JSON message** describing the transaction it wants the system to perform. The message explains *what to do*; a transactional system then digests it (and may convert it to SQL internally for storage/durability). The model outputs the DIDComm message — not SQL.

**ACID: only C is in scope.** Generating a transaction has nothing to do with Atomicity, Isolation, or Durability — those are properties of the *system that executes* the message, not of the message the model writes. So A/I/D are explicitly out of scope. (SQL still appears, but only as the system's internal storage/durability layer — never as the model's output.)

**Correctness has two dimensions.** This is the core measurement idea:

1. **Consistency** — the transaction respects the system's invariants. (Registration: the seat is available, no schedule conflict. Taxes: the numbers reconcile.) Algorithmically checkable.
2. **Semantic correctness** — the transaction actually expresses the user's *intent*. The sharp example: registering for classes that satisfy every invariant but don't move you toward graduation. Consistent, valid — and *wrong*. (The "books a motel with roaches" case: doesn't violate consistency, violates preference/intent.)

A transaction can be consistent yet semantically wrong. Measuring **both** is what makes the paper comprehensive. This maps onto a clean error taxonomy:

| Outcome | Meaning |
|---|---|
| Syntactic error | Malformed — doesn't fit the DIDComm/message grammar. |
| Consistency error | Well-formed but violates an invariant (e.g. registers for a full section). |
| **Semantic error** | Consistent but doesn't achieve the intent — the dangerous, subtle case. |
| Success | Consistent *and* semantically correct. |

### 4. Experimental design

**Independent variables (what we vary):**
- **Model** — several, across open and closed families (via the two backends below).
- **Backend** — **Ollama (local)** and **OpenRouter (cloud)**. See §4.1.
- **Domain** — class registration (primary) + a second system (candidates: simplified tax filing from a W-2 → 1040; travel/scheduling). Two+ domains so a finding is a *pattern*, not a one-system artifact — this removes "type of transactional system" as a variable (§14).

**Controlled:** the task specification, prompt wording, output-format instruction, sampling temperature, samples per task (`n`), and — for the backend comparison — the shared model.

**Measured:** consistency + semantic correctness → RQ1; latency/compute + dollar cost → RQ2.

**Open — does an NL-vs-structured input axis survive?** Prior design treated natural-language vs. structured input as an axis. The group's model now takes **JSON inputs** representing known state (§6), so input is structured by default. Whether we also test a natural-language arm is an open question (§13).

#### 4.1 Dual backend — and what it proves

Running the experiment through **both** Ollama and OpenRouter is a deliberate robustness design, not redundancy.

- **The clean, isolating claim:** run *the same model* on both backends. If findings match, the result is a property of the *model*, not the serving stack (quantization, provider defaults, sampling all wash out). Requires at least one model shared across both — e.g. Llama-3.1-8B, available on each.
- **What NOT to claim:** comparing *different* models across backends and calling agreement "backend-invariance" — that conflates backend with model. Closed models reachable only via OpenRouter add *range* to RQ3; they are not the backend control.
- **Bonus for RQ2:** Ollama yields *compute* cost (local timing); OpenRouter yields *dollar* cost (tokens × price). Two cost models from one pipeline.

This is cheap because the backend sits behind the `generate()` seam (§7) — swapping is one adapter, not a rewrite.

#### 4.2 Phased execution (local-first, cloud-second)

OpenRouter costs money per call; development makes thousands of throwaway calls (bugs, malformed-input tests, parser checks). Paying cloud rates to debug plumbing is waste. So execution is phased:

- **Phase 1 — Ollama, local, free.** All harness development, test-suite construction/verification, and the full local experiment happen here. Free and unlimited, so iteration is unconstrained.
- **Phase 2 — OpenRouter, cloud, paid.** Entered only once the harness is stable and the test suite is verified, so every dollar buys a *measurement*, not a debug cycle. Two distinct triggers to cross over:
  - **Capability** ("hardware outmatched") — a wanted model exceeds local VRAM, so cloud is the only route to run it.
  - **Confidence** ("favorable results") — the local pipeline produced a clean result and we spend to *strengthen* it (closed-model range + the shared-model backend-invariance check, §4.1).

**Switch mechanism.** The DIDComm output contract lives in the shared prompt layer, *above* the backend split, so changing endpoints cannot change the requested format. Backends (`OllamaBackend`, `OpenRouterBackend`) implement one `Backend` protocol, each normalizing its provider's response into a common shape; `generate()` is backend-agnostic (§7). The OpenRouter backend can remain a stub until Phase 2 — no API key needed to build and run everything locally.

### 5. Grounding and state input

A real user doesn't hand-write the system's structure; something supplies it. How much we inject is a modeling decision, and it must be **disclosed up front** — a model looks near-perfect when handed everything and mediocre without, so the grounding condition is load-bearing for interpreting every accuracy number.

New wrinkle from the group's model: **one of the JSON inputs can be a status-read from the transactional system itself** (e.g. "how many seats remain in CS-565"). So grounding isn't just static schema — it can include a live read that becomes part of the transaction's input. Whether grounding/state provision is fixed or itself a variable is open (§13).

### 6. Pipeline

```
JSON input(s)  →  generate(backend, model, task)  →  DIDComm message
   →  system digests message (→ SQL/state)  →  evaluate (consistency + semantic)  →  log
```

Everything is **logged unconditionally**; evaluation reads from the log and never gates what gets written, so results can be re-scored if a definition is refined without re-running generations.

**Test cases (ground truth).** Each case = one or several JSON inputs (what we know, possibly including a system status-read) → a known-correct JSON output. Target ~100 known-correct cases. To avoid hand-authoring everything, **AI generates candidate test sets, then several people verify manually** (cross-verify for hallucinations) before a case is trusted as ground truth.

**State isolation** still applies to whatever executes the message: each case runs from a known pre-state so results are comparable.

### 7. Model-interfacing layer

Backend is pluggable behind one uniform contract; `generate()` doesn't know or care which backend served the request.

```python
@dataclass(frozen=True)
class TaskSpec:            # everything that MAY go to the model
    task_id: str
    domain: str               # "registration" | "tax" | ...
    inputs: str               # the JSON input(s): known state, optional status-read
    intent: str               # what the user wants achieved

@dataclass(frozen=True)
class GroundTruth:         # NEVER touches generate()
    task_id: str
    expected: str = ""        # known-correct DIDComm output (human-verified)
    # consistency + semantic checks — see §8; assertion shape is OPEN (§13)

@dataclass(frozen=True)
class GenerationResult:    # one log row
    task_id: str
    backend: str              # "ollama" | "openrouter"
    model: str                # carries any quant tag
    prompt_sent: str
    raw_output: str           # the DIDComm JSON, verbatim, pre-parse
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    # local-only, when available (Ollama): prefill/decode split, load time (§9)
    gen_ms: float
    load_ms: float
    total_ms: float
    usd_cost: float           # OpenRouter: tokens × price; 0.0 for local
    run_index: int
    ok: bool                  # generation SUCCEEDED — not: was it correct
    error: str | None
    ts: str

class Backend(Protocol):
    def call(self, model: str, messages: list, options: dict) -> dict: ...
    # OllamaBackend  → /api/chat        (returns ns durations, token counts)
    # OpenRouterBackend → /v1/chat/completions (returns usage tokens; cost from pricing)

def generate(backend: Backend, model: str, task: TaskSpec) -> GenerationResult: ...
```

Decisions carried forward: **`GroundTruth` is separate from `TaskSpec`** (physically prevents leaking the answer into the prompt); **`ok` ≠ correct** (generation succeeding says nothing about correctness — that's §8, downstream); **chat/messages format** so each backend applies its own template server-side.

Backend note: Ollama returns the rich prefill/decode/load timings (§9); OpenRouter returns usage tokens + you compute dollar cost and measure wall-clock latency. So the timing *decomposition* is local-only; the *token + cost + latency* fields are common to both.

### 8. Evaluation — two dimensions

The evaluator reads the log and scores each generation on **both** axes:

- **Consistency** — algorithmically checkable against the system's invariants (seat availability, schedule conflict, numeric reconciliation). This is the tractable half.
- **Semantic correctness** — does the transaction achieve the stated intent? **This is the hard, open problem.** Options under discussion: compare against the human-verified ground-truth output ("apples-to-apples"), or an algorithmic equivalence/goal-satisfaction check (since many distinct JSON messages can be equally correct, exact-match is too strict). The notes leave this open — it is the deepest unresolved question (§13).

The two-axis result is the point: reporting *consistency rate* and *semantic-correctness rate* separately (and their overlap) is what distinguishes "fails loudly" from "quietly does the wrong thing" per model.

### 9. RQ2 measurement (efficiency + cost)

- **Local (Ollama):** prefill vs. decode is a real physical split. `prompt_eval_*` = ingesting the input, `eval_*` = generating output, `load_*` = model load (recorded **separately**, kept warm, so a one-time load never contaminates per-request latency). Durations arrive in **nanoseconds** — convert once, centrally.
- **Cloud (OpenRouter):** no prefill/decode split; measure **wall-clock latency**, **usage tokens**, and **dollar cost** (tokens × current price — pricing lookup is an assigned task). Report cost per successful transaction.
- Store **primitives, not rates**; compute tokens/sec and $/transaction in analysis.
- **Measured 9/17 (still true):** Ollama durations arrive in nanoseconds (one conversion); a cold-vs-warm run showed a ~100× prefill speedup, which confirms keeping the model warm and timing the load separately.
- **Drift:** `run_index` + `ts` catch cost/latency drift over a run. Locally, watch thermal throttling; on cloud, watch provider-side variance.

### 10. Confounds to control

- **Stochasticity.** Take `n` samples per task at fixed temperature; report a *rate*, not a single pass/fail.
- **Backend fairness (§4.1).** Isolate backend only via a *shared* model; don't read cross-model agreement as backend-invariance.
- **Ground-truth quality.** AI-generated test sets can hallucinate; multi-person manual cross-verification before a case is trusted (§6).
- **Grounding disclosure (§5).** State the grounding/state-provision condition up front.
- **Malformed vs. wrong.** Keep syntactic, consistency, and semantic errors as distinct buckets (§3) so formatting failures don't masquerade as reasoning failures.

### 11. Infrastructure

Two backends, one harness:

- **Ollama (local):** native app, GPU via CUDA; models pulled locally. Fixed box = RQ2 measurement consistency for the *compute* numbers (VRAM sets the local roster). Building/testing needs no dedicated server.
- **OpenRouter (cloud):** one API key, broad model access (open + closed), single output format. Reachable from any OS. Pricing/token-cost lookup is an assigned task and feeds RQ2's dollar figures.
- **Harness:** Python + `requests`; SQLite (stdlib) for the transactional system's storage/durability layer; JSONL logs.

Whichever machine runs the *measured local* runs is the fixed hardware — one person should own those runs to avoid cross-machine contamination. OS is not a hard requirement (native Windows + CUDA is fine); "one consistent box" is.

### 12. Experimental matrix (scale check)

```
backends × models × domains × tasks × samples
   2      ×  ~N    ×   2+    ×  ~100 ×  ~n
```

*(counts illustrative)* Generation is the cheap part; the **two-dimension evaluation harness** — especially the semantic check — is the bottleneck. Every generation must be scored automatically or the scale is unmanageable.

### 13. Open questions — for the group

1. **Semantic verification — THE hard one.** How do we check that a consistent transaction achieves the *intent*? Compare to human-verified ground-truth output, or an algorithmic goal-satisfaction check? Exact JSON match is too strict (many correct encodings). Blocks the semantic half of `evaluate()`.
2. **Ground-truth assertion shape.** Is the stored check a JSON output to diff against, a Python callable, or an invariant+goal spec? Ties to §13.1.
3. **Second domain.** Simplified taxes (W-2 → 1040) or travel/scheduling? (Registration is settled as primary.)
4. **DIDComm envelope.** What exact message schema are we standardizing on for inputs/outputs?
5. **Grounding/state provision.** Fixed (always provided, incl. status-reads) or a variable?
6. **NL input arm.** Do we test natural-language input alongside JSON, or is input JSON-only?
7. **Model set** across both backends — and which model(s) are shared on both for the backend control (§4.1).
8. **`n`** — samples per task.
9. **OpenRouter pricing** — token/request cost figures (assigned task) to finalize RQ2's cost model.

### 14. Non-goals and risks

- **Non-goal: A, I, D.** Only Consistency (plus semantics) is in scope; atomicity/isolation/durability belong to the executing system (§3).
- **Non-goal: banking.** Dropped — too simple to exercise consistency *and* semantics. Registration/taxes/travel are "satisfiability" problems (must satisfy invariants *and* express intent); banking isn't.
- **Non-goal: training our own model.** We measure what people *actually deploy*; a bespoke model answers a weaker question and risks the semester. Cost control is handled by local Ollama; range by OpenRouter.
- **Risk: scope creep.** Two domains and two backends is already a lot. Resist adding a third of either until the two-domain / two-backend pattern is solid. The semester is the binding constraint.

### 15. Changelog

| Version | Date | Changes |
|---|---|---|
| 1.6 | 2026-10-14 | Merged into README.md. The old §15 (implementation status as of 9/17) removed: it described code that has since changed, and the rest of this README now covers the current state. Its one still-true finding (Ollama timings) moved to §9; the changelog is renumbered §15. The 1.5 text is in git history. |
| 1.5 | 2026-09-17 | Post-pivot pipeline implemented and verified: `harness.py` refactored to dual-backend (Ollama real, OpenRouter stub) + DIDComm output; `registration.py` domain testbed with a real consistency checker; `run_registration.py` end-to-end runner; README + .gitignore added. §15 rewritten to reflect built-and-verified state (3 of 4 outcome buckets fire automatically; semantic half stubbed pending §13.1). |
| 1.4 | 2026-09-17 | Added §4.2 (phased execution): local-first/cloud-second sequencing with capability vs. confidence crossover triggers; documented the backend switch mechanism (DIDComm contract above the backend split; `Backend` protocol; OpenRouter stub until Phase 2). Decided harness stays Python (inference-latency-bound, churny research code, analysis ecosystem) rather than Rust. |
| 1.3 | 2026-09-17 | Reconciled to 9/17 group decisions: backend Ollama→**Ollama + OpenRouter dual-backend** (§4.1, robustness design); output SQL→**DIDComm JSON**; banking dropped→registration + second domain (tax/travel); ACID narrowed to **C only**; added **two-dimension correctness** (consistency + semantic) and error taxonomy (§3, §8); ground truth = AI-generated + human-cross-verified test sets (§6); RQ2 gains dollar cost (§9); §13 reset around semantic verification as the deep open problem; §15 updated for the pivot. |
| 1.2 | 2026-09-17 | Added §15 (implementation status): walking skeleton runs end-to-end; `generate`/`make_db`/`execute`/`snapshot`/`log_result` real, `evaluate` stubbed. Validated nanosecond timing, cold/warm gap, success + loud-failure buckets live. Environment recorded (Windows + native Ollama + CUDA); added `seed_sql` to `GroundTruth`. |
| 1.1 | 2026-09-17 | Added §5 (two-layer statelessness/grounding), §9 (RQ2 prefill/decode timing), §11 (MVP infra + hardware availability); §7 interface contract; §3 testbed-fidelity principle; new group questions. Removed division-of-labor section. |
| 1.0 | 2026-09-17 | Initial draft: motivation, RQs, ACID scoping, experimental design, pipeline, three-bucket evaluation, non-goals. |
