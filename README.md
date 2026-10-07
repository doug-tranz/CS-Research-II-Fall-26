# CSR2 — Evaluating LLM-Generated Transactions

Measuring how well currently-deployed LLMs generate correct transactions,
expressed as DIDComm-format JSON messages. See `transaction_llm_eval_design.md`
for the full design. This README covers how to run the code, the message
schema the models must produce, and the test cases and results so far.

Contents: [What's here](#whats-here) · [Setup](#setup) · [Run](#run) ·
[Read the results](#read-the-results) · [Troubleshooting](#troubleshooting) ·
[The cart message](#the-cart-message-didcomm-schema) · [Correctness model](#correctness-model) ·
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
| `registration.py` | Registration domain testbed: SQLite schema, `evaluate()` (Tiers 1–2 from `didcomm_schema`, then Tier 3 consistency over the whole cart). |
| `semantic_check.py` | `check_semantic()` (Tier 4): compares the model's body to the case's expected message. Section order is ignored. |
| `evaluate_with_semantics.py` | `evaluate_full()`: `registration.evaluate()` (Tiers 1–3), then Tier 4. |
| `run_registration.py` | End-to-end runner for one built-in task: generates, scores Tiers 1–3, logs to `runs.jsonl`. |
| `run_semantic_eval.py` | Runner: sends each case in a test file to a model and scores all four tiers. |
| `test_cases.jsonl` (5), `semantic_test_cases.jsonl` (15), `cart_test_cases.jsonl` (10) | Test cases: `task` (sent to the model) and `ground_truth` (never sent). See [Test cases](#test-cases). |
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
```

| Option | Meaning | Default |
|---|---|---|
| `--backend` | `openrouter` or `ollama` | `openrouter` |
| `--model` | One model ID (required) | |
| `--test-file` | Which case file to run | `test_cases.jsonl` |
| `--task-ids` | Run only these cases | all cases in the file |
| `--output` | Results file | `semantic_eval_results.json` |

Runs each case once and appends to the results file.

### The validator alone

```bash
python didcomm_schema.py        # 17 example messages, no model, no cost
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

`run_semantic_eval.py` prints one line per case, then a total:

```
cart-002: semantic_ok          | matches ground truth exactly
```

| Outcome | Tier | Meaning |
|---|---|---|
| `syntactic_error` | 1 | Not a DIDComm message: not valid JSON (e.g. the model wrapped it in a ```` ```json ```` code block, or returned nothing), or no `id`, or a malformed `type` |
| `schema_error` | 2 | A DIDComm message, but not our register message: wrong `type`, or the body isn't exactly `student_id` plus a `sections` list |
| `consistency_error` | 3 | Breaks a rule, e.g. full section, already enrolled, time conflict |
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
| `run_semantic_eval.py` | Sends `didcomm_schema.OUTPUT_CONTRACT` in place of the hard-coded single-section contract. Adds `--task-ids`, and writes an indented `.json` results file in place of `.jsonl`. |
| `run_registration.py` | Same task and seed data; asks for the cart message. |
| `test_cases.jsonl`, `semantic_test_cases.jsonl` | `ground_truth.expected` rewritten as a one-section cart with the new `type`. `task`, `note`, `category` and `pair_id` are copied as they were. |

`harness.py` needed no change; its system prompt describes the envelope loosely.

## Correctness model

Four tiers, stopping at the first failure:
`syntactic_error` → `schema_error` → `consistency_error` → `semantic_error` / `semantic_ok`.

- **Tiers 1–2, format.** See [The cart message](#the-cart-message-didcomm-schema).
- **Tier 3, consistency** — invariants hold. Each section exists, has a seat, is not already
  held by the student, and does not clash with their enrollments; inside the cart, no two
  sections share a time slot or a course.
- **Tier 4, semantic correctness** — the transaction achieves the user's intent. Scored as a
  match against one expected message (`semantic_check.py`), ignoring section order. That is
  fair only while every case has exactly one correct cart, which is true of all 30 today.
  How to score it when several carts are correct is still open (design §13.1).

## Test cases

One JSON object per line. Only `task` (`task_id`, `domain`, `inputs`, `intent`) is sent to
the model; `ground_truth.expected` is the known-correct message. Inputs are a bare list of
sections, because the Task 1 input format is not settled.

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
  tiers, with no new model calls. `run_semantic_eval.py` and `run_registration.py` have not
  themselves been run against a model since they were moved to the cart, and the 20
  one-section cases have no cart-schema results yet.

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
- Time-conflict logic (exact-slot match, not interval overlap) → `registration.check_consistency()`
- In-memory SQLite → `registration.make_db()`
- Cart message type URI (`example.org` placeholder) → `registration_message.schema.json`
- Tier 4 by comparison to one expected cart → `semantic_check.check_semantic()`
