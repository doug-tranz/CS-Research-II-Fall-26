# CSR2 — Evaluating LLM-Generated Transactions

Measuring how well currently-deployed LLMs generate correct transactions,
expressed as DIDComm-format JSON messages. See `transaction_llm_eval_design.md`
for the full design. This README covers how to run the code, the message
schema the models must produce, and the test cases and results so far.

Contents: [What's here](#whats-here) · [Setup](#setup) · [Run](#run) ·
[Read the results](#read-the-results) · [Troubleshooting](#troubleshooting) ·
[The cart message](#the-cart-message-didcomm-schema) · [The input format](#the-input-format) ·
[Correctness model](#correctness-model) ·
[Test cases](#test-cases) · [Results so far](#results-so-far) ·
[Open questions](#open-questions-for-the-group) · [Phase](#phase) · [Provisional](#provisional-will-change-isolated-to-one-spot-each)

## What's here

| File | Role |
|---|---|
| `harness.py` | Core, domain-agnostic: backends (Ollama local, OpenRouter cloud), `generate()`, DIDComm envelope parsing, logging. |
| `probe.py` | Historical raw-Ollama diagnostic (kept for reference). |

**Repo root — the cart message** (`{"student_id", "sections": [...]}`, draft schema).

| File | Role |
|---|---|
| `registration_message.schema.json` | The message rules, as JSON Schema (draft 2020-12). Source of truth for the cart message. |
| `didcomm_schema.py` | `validate_message()`: Tiers 1–2 against the schema. `OUTPUT_CONTRACT`: the prompt text that tells a model the format. |
| `registration.py` | Registration domain testbed: SQLite schema, `evaluate()` (Tiers 1–2 from `didcomm_schema`, then Tier 3 consistency over the whole cart). Also the input format: `validate_input()`, `audit()` (degree audit), `missing_prerequisites()`, and `seed_db()`. |
| `semantic_check.py` | `check_semantic()` (Tier 4): compares the model's body to the case's expected message. Section order is ignored. |
| `evaluate_with_semantics.py` | `evaluate_full()`: `registration.evaluate()` (Tiers 1–3), then Tier 4. `evaluate_case()`: the same for an input-format case, accepting any cart in its answer key. |
| `run_registration.py` | End-to-end runner for one built-in task: generates, scores Tiers 1–3, logs to `runs.jsonl`. |
| `run_semantic_eval.py` | Runner: sends each case in a test file to a model and scores all four tiers. Builds the prompt for input-format cases, as JSON, CSV or plain text. |
| `registration_input.schema.json` | The input format rules, as JSON Schema. See [The input format](#the-input-format). |
| `input_format_cases.json` (6) | Test cases in the full input format: student, transcript, program requirements, schedule, prompt. |
| `test_cases.jsonl` (5), `semantic_test_cases.jsonl` (15), `cart_test_cases.jsonl` (10) | Earlier test cases, whose input is a bare list of sections: `task` (sent to the model) and `ground_truth` (never sent). See [Test cases](#test-cases). |
| `semantic_eval_results.json` | Generated results of `run_semantic_eval.py`, as an indented JSON array. |

**`old_schema/` — the single-section message** (`{"student_id", "section_id"}`). The
originals of the files above, kept as they were. Run them from the repo root as
`python old_schema/<script>.py`; they read and write their own data files in that folder.

| File | Role |
|---|---|
| `old_schema/registration.py`, `run_registration.py`, `semantic_check.py`, `evaluate_with_semantics.py`, `run_semantic_eval.py` | Same roles as above, for the old message. |
| `old_schema/test_cases.jsonl`, `old_schema/semantic_test_cases.jsonl` | The cases with the old expected message. |
| `old_schema/runs.jsonl`, `old_schema/semantic_eval_results.jsonl` | Results of every run made before the cart schema. |

`*.jsonl` is in `.gitignore`. A `.jsonl` file you want teammates to get needs `git add -f`.

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
plus `jsonschema` (needed by the cart schema).

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

All commands are run from the repo root with `(venv)` active. They use the cart message;
put `old_schema/` in front of a script name to run the single-section original instead.

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

### The test cases, all four tiers

```bash
python run_semantic_eval.py --backend openrouter --model anthropic/claude-sonnet-5
python run_semantic_eval.py --model anthropic/claude-sonnet-5 --test-file semantic_test_cases.jsonl
python run_semantic_eval.py --model anthropic/claude-sonnet-5 --test-file cart_test_cases.jsonl --task-ids cart-006 cart-007
python run_semantic_eval.py --model anthropic/claude-opus-5.5 --test-file input_format_cases.json --task-ids sem_004 -n 5
python run_semantic_eval.py --model anthropic/claude-opus-5.5 --test-file input_format_cases.json --rendering nl
```

| Option | Meaning | Default |
|---|---|---|
| `--backend` | `openrouter` or `ollama` | `openrouter` |
| `--model` | One model ID (required unless `--check`) | |
| `--test-file` | Which case file to run. A `.jsonl` file holds bare-section-list cases; a `.json` file holds full input-format cases | `test_cases.jsonl` |
| `--task-ids` | Run only these cases | all cases in the file |
| `-n` | Samples per case | `1` |
| `--rendering` | How an input-format case's state is shown to the model: `json`, `csv` or `nl` (plain text) | `json` |
| `--check` | Score each input-format case's own answer key and exit. No model call | off |
| `--output` | Results file | `semantic_eval_results.json` |

Appends every sample to the results file. An input-format case costs more per call than the
earlier cases, because the whole transcript and program are in the prompt (about $0.02 on
Opus 5.5).

### The validators alone

```bash
python didcomm_schema.py        # 17 example messages, no model, no cost
python run_semantic_eval.py --test-file input_format_cases.json --check   # every input-format case and its answer key
```

### Change the model

Model IDs are the `provider/model` names on <https://openrouter.ai/models>. Click a model
and copy the ID under its name. Spelling must be exact. `run_registration.py` takes several
at once and runs them one after another:

```bash
python run_registration.py --backend openrouter --models anthropic/claude-sonnet-5 openai/gpt-4o-mini meta-llama/llama-3.1-8b-instruct -n 5
```

Include `meta-llama/llama-3.1-8b-instruct` to compare against local `llama3.1:8b` (design §4.1).

Models we've already tried (price is per call on the single-section task; the cart prompt is
longer, so expect somewhat more):

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

`run_registration.py` prints one line per sample:

```
run 0: consistent__semantic_TBD   | invariants hold; ... | $0.001334
   raw: {"id": "...", "type": "https://example.org/course-registration/1.0/register", "body": ...
```

`run_semantic_eval.py` prints one line per case (one per sample when `-n` is above 1), then
a total:

```
cart-002: semantic_ok          | matches ground truth exactly
sem_004 run 0: semantic_ok          | matches ground truth exactly

5/5 semantic_ok on anthropic/claude-opus-5.5. Cost $0.1005. Full results in semantic_eval_results.json
```

| Outcome | Tier | Meaning |
|---|---|---|
| `syntactic_error` | 1 | Not a DIDComm message: not valid JSON (e.g. the model wrapped it in a ```` ```json ```` code block, or returned nothing), or no `id`, or a malformed `type` |
| `schema_error` | 2 | A DIDComm message, but not our register message: wrong `type`, or the body isn't exactly `student_id` plus a `sections` list |
| `consistency_error` | 3 | Breaks a rule, e.g. full section, already enrolled, time conflict, unmet prerequisite |
| `semantic_error` | 4 | Passes every rule, but the cart is not the one the intent asked for |
| `semantic_ok` | 4 | Correct |
| `consistent__semantic_TBD` | 3 | `run_registration.py` only: passed Tiers 1–3; that runner has no expected answer to check intent against |
| `GENERATION FAILED` / `generation_failed` | | The call itself failed; the error message says why |

Where the full rows go, each with the prompt, the model's raw answer, tokens, latency, cost
and the verdict:

- `run_registration.py` → `runs.jsonl`, one line per sample.
- `run_semantic_eval.py` → `semantic_eval_results.json`, an indented JSON array. Each row
  also has `parsed_output`, the model's message as an indented object; `raw_output` is the
  exact text the model returned.

## Troubleshooting

| You see | Fix |
|---|---|
| `OpenRouter needs an API key` | Key not set, or you didn't restart VS Code/terminals after setting it (Setup step 3) |
| `OpenRouter 401` | Key is wrong or was deleted. Ask Khalil for a new one |
| `OpenRouter 402` | Out of credits, or your key hit its spending limit. Tell Khalil |
| `OpenRouter 400 ... not a valid model ID` | Typo in the model ID. Copy it from openrouter.ai/models |
| `ModuleNotFoundError: No module named 'requests'` or `'jsonschema'` | venv isn't active, or `pip install -r requirements.txt` hasn't been run since `jsonschema` was added (Setup step 2) |
| `No such file or directory: 'cart_test_cases.jsonl'` | `.jsonl` files are git-ignored; ask whoever has it to `git add -f` it |
| Blank `raw:` with `syntactic_error` | The model refused the request (seen with Fable 5.1). Not a formatting problem; we don't log refusals separately yet |

## The cart message (DIDComm schema)

**Status:** draft for group review (Task 2, Khalil, drafted with Claude, 2026-10-07).
Items marked **[proposed]** are not team decisions yet.

The one message a model must produce: a request to register a student for a semester's set
of sections (a "cart"), written as a DIDComm v2 plaintext message. This answers open
question 4 in the design doc (§13). It does not define the inputs (Task 1) or what makes a
cart correct for an intent (Task 3).

```json
{
  "id": "3f2b8c1e-7a4d-4e9b-9c55-0d1e2f3a4b5c",
  "type": "https://example.org/course-registration/1.0/register",
  "body": {
    "student_id": "alice",
    "sections": ["CS301-A", "MATH152-A"]
  }
}
```

A single enrollment is a cart with one section. There is no separate one-section message.

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
| `sections` | yes | Array of section IDs: at least one, no repeats, each a string with no whitespace. |
| anything else | | Not allowed. An invented field such as `credits` or `term` is a schema error. |

The schema checks shape only. Whether a section exists, has a seat, or conflicts with
another is Tier 3.

### Message type URI [proposed]

`https://example.org/course-registration/1.0/register`

DIDComm requires `type` to be a URI in this form. `example.org` is a placeholder;
`https://didcomm.org/` is reserved for community protocols, so it should not be used. The
old code's `"registration/enroll"` is not a valid message type URI. The URI is written once,
in the schema file.

### Which tier a format failure belongs to

| Tier | Outcome | Fails when | Schema part |
|---|---|---|---|
| 1 | `syntactic_error` | The output is not JSON, or it breaks an envelope rule. | `$defs/envelope` |
| 2 | `schema_error` | It is a DIDComm message, but `type` is not ours or the body breaks a body rule. | `$defs/register` |

The rule: Tier 1 asks "is this a DIDComm message at all?" and Tier 2 asks "is it our
register message?". This keeps formatting failures separate from wrong-shape failures, as
the design doc asks (§10).

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
| `"sections": []`, or a repeated section | `schema_error` |
| `"sections": [{"section_id": "CS301-A"}]` | `schema_error` |
| Extra body field | `schema_error` |

`python didcomm_schema.py` runs these and eight more; all 17 give the expected outcome.

### What changed from the single-section message

| | Single-section (`old_schema/`) | Cart (repo root) |
|---|---|---|
| Body | `{"student_id", "section_id"}`, one section | `{"student_id", "sections": [...]}`, a cart |
| `type` | `registration/enroll`, never checked | Fixed URI, checked |
| `id` | Never checked | Required |
| Extra body fields | Ignored | Schema error |
| Tier 1 | Valid JSON with an object `body` | Also requires `id`, a valid `type`, and well-formed optional headers |
| Tier 2 | The two body fields are present | `type` is ours and the body matches exactly (`didcomm_schema.validate_message()`) |
| Tier 3 | One section against the database | Every section, plus the cart against itself |
| Tier 4 | Exact field match | Field match, section order ignored |

The meeting on 10/1 settled on the cart. Stricter Tiers 1–2 move some outputs that passed
before into an error bucket, so the earlier results in `old_schema/` were scored under
different rules and are not comparable.

### How the pipeline was moved to the cart

The teammates' files were copied to the repo root and changed to use the cart message. The
originals stay in `old_schema/`.

| File (repo root) | Change from the original |
|---|---|
| `registration.py` | `evaluate()` calls `validate_message()` for Tiers 1–2. `check_consistency()` checks every section in the cart, and adds two checks that only exist for a cart: two cart sections in the same time slot, and two sections of the same course. |
| `semantic_check.py` | Compares `sections` as a set, so the order of the cart does not matter. Otherwise the same field-by-field match against the expected message. |
| `evaluate_with_semantics.py` | Logic unchanged. Its smoke test now uses carts. |
| `run_semantic_eval.py` | Sends `didcomm_schema.OUTPUT_CONTRACT` in place of the hard-coded single-section contract. Adds `--task-ids`, `-n`, `--rendering` and `--check`, and writes an indented `.json` results file in place of `.jsonl`. |
| `run_registration.py` | Same task and seed data; asks for the cart message. |
| `test_cases.jsonl`, `semantic_test_cases.jsonl` | `ground_truth.expected` rewritten as a one-section cart with the new `type`. `task`, `note`, `category` and `pair_id` are copied as they were. |

`harness.py` needed no change; its system prompt describes the envelope loosely.

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
| `schedule` | yes | `term` and `sections`. A section has `crn`, `course_id`, `section`, `credit_hours`, `seats_total`, `seats_taken`, `campus`, `modality`, `meetings` (days, `start_time`, `end_time`). |
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
- **Scoring.** `evaluate_with_semantics.evaluate_case()` runs all four tiers.

A test case wraps one input object with an id and an answer key:

```json
{
  "case_id": "sem_001",
  "input": { "...": "the input object" },
  "ground_truth": {
    "acceptable_crns": ["12345"],
    "implicit_relationship": "The student lives in Glassboro, so the Camden section (12348) is valid but wrong."
  }
}
```

### How the spec's open items were filled in [provisional]

| Spec open item | What the code does |
|---|---|
| 1. Section id | The message's `sections` list holds `crn` values. The prompt tells the model so. |
| 2. Meeting times | Two sections conflict when they share a day and their time ranges overlap (`meetings` table in `registration.py`). The slot codes (`MW14`) of the earlier cases still work. |
| 3. One section or several per message | Several: the spec assumed one enroll message per section, but the 10/1 meeting settled on a cart, so one message registers every section. |
| 4. Prerequisites | New optional `prerequisites` list. Treated as a **consistency** rule (Tier 3): registering for a course whose prerequisite is unmet is a `consistency_error`. A course still in progress does not satisfy a prerequisite. |
| 5. `TA` and `minimum_grade` | Accepted transfer credit (`TA`) meets every minimum grade. `F` and `W` meet none. |
| 6. Other requirement types | New optional fields, all read by `registration.audit()`. See the next table. |
| 7. Graduation-wide rules | New optional `graduation_rules`: `total_credits`, `minimum_gpa`, `residency_credits`. The audit derives earned credits, institutional credits and GPA from the transcript; none is stored. A repeated course counts once, by its best attempt. |
| 8. Other input renderings | `--rendering csv` shows the same state as CSV tables, and `--rendering nl` as plain-text sentences. JSON stays the stored form. |
| 9. Test-case wrapper | `acceptable_crns` lists crns that are each a correct one-section cart. `acceptable_carts` lists whole carts, for answers with several sections. Tier 4 passes when the cart equals any acceptable one, so a case can have more than one right answer. |

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
| `credit_hours` as `{"min", "max"}` | section | A variable-credit section. The message has no field for the credits chosen, so the range is shown to the model but not checked. |

What the checker does and does not read:

- **Tier 3 reads** the schedule and the prerequisites (against the transcript).
- **The audit is not part of scoring.** `registration.audit()` and `counts_toward()` report
  which requirements are met and which courses would move one forward. They are for case
  authors and for checking an answer key. Tier 4 still compares the cart to the answer key,
  so whether a course "counts toward the degree" is decided by the author, with the audit
  as a cross-check.

### The six cases

`input_format_cases.json`:

| Case | Prompt | What it tests | Correct |
|---|---|---|---|
| sem_001 | "Enroll me in CS05000. I live in Glassboro." | The spec's own example: an open Camden section is valid but wrong. | `12345` |
| con_001 | "Enroll me in MATH03000." | The first section listed is full. | `20002` |
| con_002 | "Enroll me in CS05000 and MATH03000." | Two-section cart; one MATH section overlaps CS (10:30–11:20 against 10:00–11:15) without starting at the same time. | `30001` + `30003` |
| sem_002 | "Register me for CS05000 and MATH03000. I only want classes that meet in person." | Online and hybrid sections are open but wrong. | `40002` + `40004` |
| sem_003 | "Enroll me in one class that counts toward my Computer Science restricted electives. I don't want to retake anything I've already passed." | Uses the transcript and requirements. An already-passed elective and a non-elective are wrong. | `50002` or `50003` |
| sem_004 | "Register me for two computer science classes that count toward my degree. I work evenings, so nothing that ends after 5 PM." | Uses the open-item fields: a prerequisite still in progress, a prerequisite met by transfer credit, a repeatable course, an attribute requirement, graduation rules. | any two of `60001`, `60004`, `60007` |

In sem_004 the seven sections are: three correct electives; one elective whose prerequisite
is still in progress (`consistency_error`); one that ends at 6:15 PM; one course already
passed; and an art course that counts toward the degree but is not computer science.

sem_001 is the spec's example. The other five were written by Claude and have not been
checked by a teammate. `python run_semantic_eval.py --test-file input_format_cases.json --check`
confirms each answer key scores `semantic_ok` and, for one-section cases, that no other
section does.

The 30 earlier cases (`.jsonl`) do not use this format: their input is a bare list of
sections with short ids such as `CS401-A`, which the course id pattern here does not allow.

## Correctness model

Four tiers, stopping at the first failure:
`syntactic_error` → `schema_error` → `consistency_error` → `semantic_error` / `semantic_ok`.

- **Tiers 1–2, format.** See [The cart message](#the-cart-message-didcomm-schema).
- **Tier 3, consistency** — invariants hold. Each section exists, has a seat, is not already
  held by the student, does not clash with their enrollments, and has its prerequisites met
  (input-format cases); inside the cart, no two sections overlap in time or belong to the
  same course.
- **Tier 4, semantic correctness** — the transaction achieves the user's intent. Scored as a
  match against an expected message (`semantic_check.py`), ignoring section order. The 30
  `.jsonl` cases each have exactly one correct cart. Input-format cases can list several
  acceptable carts, and matching any of them passes. Listing every correct cart by hand
  stops being practical for open-ended intents; a rule-based check is still open (design §13.1).

## Test cases

These are the earlier cases; the ones in the full input format are listed under
[The input format](#the-input-format).

One JSON object per line. Only `task` (`task_id`, `domain`, `inputs`, `intent`) is sent to
the model; `ground_truth.expected` is the known-correct message. Inputs are a bare list of
sections, because these cases were written before the input format.

| File | Cases | Content |
|---|---|---|
| `test_cases.jsonl` | 5 | Ben's cases (reg-001 to 005), as one-section carts. |
| `semantic_test_cases.jsonl` | 15 | reg-sem-001 to 015, as one-section carts, with `category` and `pair_id`. |
| `cart_test_cases.jsonl` | 10 | Carts of one to three sections, below. |

| Case | What it tests | Expected cart |
|---|---|---|
| cart-001 | Two-course baseline, with an unrequested course listed first | `CS301-A`, `MATH152-A` |
| cart-002 | Choosing the Tuesday/Thursday section of one course | `CS401-B`, `STAT200-A` |
| cart-003 | One-course cart, which must still use the `sections` list | `HIST110-A` |
| cart-004 | Avoiding a full section that is listed first | `BIO101-B`, `CHEM101-A` |
| cart-005 | Avoiding a time conflict between two sections in the cart | `PHYS201-A`, `CS250-B` |
| cart-006 | Three courses, with "no class at 8 a.m." | `CS330-B`, `MATH210-A`, `ENG105-A` |
| cart-007 | Look-alike course IDs (CS101 and CS1010, MATH151 and MATH115) | `CS101-A`, `MATH151-A` |
| cart-008 | Most open seats per course (total minus taken) | `PHYS201-B`, `CHEM110-A` |
| cart-009 | Chained time conflicts, where one forced choice forces the next | `CS310-A`, `CS320-B`, `CS340-B` |
| cart-010 | Latest-starting section that is not full | `HIST110-C`, `ECON101-A` |

The cart cases were written by Claude and have not been independently checked by a teammate
(design doc §6).

Checked offline, with no model:

- All 30 cases score `semantic_ok` when their own expected message is scored.
- In the 20 one-section cases, swapping in any other offered section never scores
  `semantic_ok`: 19 swaps are `semantic_error` and 1 is `consistency_error` (a full section).
- Wrong carts land in the right tier: a full section and a time conflict inside the cart are
  `consistency_error`; a valid but unrequested section is `semantic_error`.

## Results so far

Cart message, OpenRouter, 2026-10-07, one sample per case. Rows are in
`semantic_eval_results.json`.

| Model | Cases | Result | Cost |
|---|---|---|---|
| `anthropic/claude-opus-5.5` | cart-001 to 005 | 5/5 `semantic_ok` | $0.020 |
| `anthropic/claude-sonnet-5` | cart-006 to 010 | 4/5 `semantic_ok`, 1 `syntactic_error` | $0.014 |

- Every answer that parsed was a valid register message: correct `type`, an `id`, and a
  `sections` list. No `schema_error`, `consistency_error` or `semantic_error` occurred.
- The one failure (Sonnet 5, cart-006) was a code fence around the JSON. The cart inside the
  fence was the correct one.
- The two models ran different cases, so the scores do not compare the models. One sample
  per case shows the format works end to end; it is not a pass rate.
- These ten answers were generated by an earlier cart runner that skipped Tier 3 (since
  removed). The verdicts in the file are the same raw answers re-scored through all four
  tiers, with no new model calls. The 20 one-section cases have no cart-schema results yet,
  and `run_registration.py` has not been run against a model since it was moved to the cart.

Input format, OpenRouter, 2026-10-07, JSON rendering, `run_semantic_eval.py -n 5`:

| Model | Case | Result | Cost |
|---|---|---|---|
| `anthropic/claude-opus-5.5` | sem_004, 5 samples | 5/5 `semantic_ok` | $0.10 |

- All five answers were the same cart: `60001` + `60007` (CS07450 and the repeatable
  CS01395). The harness runs at temperature 0, so five samples show stability, not spread.
- The model never chose `60004` (CS01303), the acceptable section whose prerequisite is met
  only by transfer credit. The runs do not show why; it is the option that depends on the
  provisional `TA` rule.
- Only sem_004 has been run. The other five input-format cases, and the `csv` and `nl`
  renderings, have not been run against a model.

Results for the single-section message are in `old_schema/runs.jsonl` and
`old_schema/semantic_eval_results.jsonl`.

## Open questions for the group

1. **Message type URI.** What base URI do we use in place of `example.org`?
2. **Code fences.** A fenced answer is a syntactic error, as it was before. Do we keep that,
   or strip fences before parsing and count the model as correct? This already decided one
   of ten results.
3. **`term`.** The cart does not name the semester. Does the registration system need it in
   the body, or is it implied by the offerings in the input?
4. **Sender identity.** Should the student be the DIDComm `from` header (which needs DIDs in
   the input) in place of `body.student_id`?
5. **Unknown headers.** They are ignored, per DIDComm. Do we want to count them as a
   measured deviation anyway?
6. **Drops and swaps.** Is the scope register-only, or do we need message types for dropping
   or swapping a section?

## Phase

- **Phase 1 (now):** Ollama, local, free — all development + the local experiment.
- **Phase 2 (later):** OpenRouter, cloud, paid — closed models + backend-invariance
  check. Backend is a one-class swap; `OpenRouterBackend` is implemented (`--backend openrouter`).

## Provisional (will change; isolated to one spot each)

- DIDComm envelope shape → `harness.parse_didcomm()`
- Time-conflict logic (interval overlap for input-format cases, exact-slot match for the earlier ones) → `registration.check_consistency()`
- Input-format open items: prerequisites as a consistency rule, `TA` meeting any minimum, best-attempt GPA → `registration.py` (`missing_prerequisites()`, `meets_minimum()`, `audit()`)
- Input-format answer-key shape (`acceptable_crns`, `acceptable_carts`) → `evaluate_with_semantics.py`
- In-memory SQLite → `registration.make_db()`
- Cart message type URI (`example.org` placeholder) → `registration_message.schema.json`
- Tier 4 by comparison to one expected cart → `semantic_check.check_semantic()`
