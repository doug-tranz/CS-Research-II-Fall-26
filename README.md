# CSR2 — Evaluating LLM-Generated Transactions

Measuring how well currently-deployed LLMs generate correct transactions, expressed as
DIDComm-format JSON messages. A model is given a student's transcript, their program
requirements, a portion of the school's class schedule, and the student's request, and must
produce a registration message. The harness scores that message on four tiers, from "is it
valid DIDComm" to "does it do what the student asked".

This README covers running the code, the message schema, the input format, the test cases
and results so far, and the full project design.

Contents: [What's here](#whats-here) · [Setup](#setup) · [Run](#run) · [Local runs](#local-runs-lm-studio) ·
[Read the results](#read-the-results) · [Troubleshooting](#troubleshooting) ·
[The cart message](#the-cart-message-didcomm-schema) · [The input format](#the-input-format) ·
[Correctness model](#correctness-model) · [Results so far](#results-so-far) ·
[Open questions](#open-questions-for-the-group) · [Provisional](#provisional-will-change-isolated-to-one-spot-each) ·
[Design](#design)

## What's here

```
run_semantic_eval.py     the evaluation (python run_semantic_eval.py --help)
generate_pairs.py        writes the test cases (python generate_pairs.py --help)
pipeline/                all the code, one job per file (below)
data/                    paired_cases.json, degree_requirements.json, section_tally_fall2026.csv
schemas/                 registration_input.schema.json, registration_message.schema.json
results/                 new runs (results/semantic_eval_results.json by default)
results/past_runs/       earlier results: Claude Sonnet 5.5 on 3 pairs, one file per tally version
tests/                   python -m unittest discover tests (no model, no network, ~7 s)
```

**`pipeline/`, the run** (`cli.py` is the command line; it calls the rest):

| File | Role |
|---|---|
| `cli.py` | The command line behind `run_semantic_eval.py`: options, backend setup, the run loop (generate, score, log). See [Run](#run). |
| `cases.py` | Loads a case file and builds each case's full input object; the answer-key format (`RULES`); `expected_message()` (a cart as a correct answer would write it); `validate_case()`. |
| `render.py` | Everything the model sees: the session's opening (the Section Tally in five versions, `TALLY_FIELDS`, `--subjects`) and each case's own turn (JSON, CSV or plain text); `to_task()`. |
| `session.py` | `TallySession`: one cached opening per run, one call per case, the run's totals. See [The session](#the-session). |
| `dashboard.py`, `dashboard.html` | A live dashboard of a run in the browser: progress and time left, accuracy, electricity, projected OpenRouter cost, outcomes by family, seconds and watts per call, the latest calls. See [Watch a run](#watch-a-run-the-dashboard). |
| `batch.py` | `--session-per-pair`: the cases split into one session per pair (`pair_groups()`), `--pairs N` (`spread_pairs()`), and the batch's grand totals (`--batch-report`). |
| `harness.py` | Core, domain-agnostic: `TaskSpec`, the chat layout for prompt caching (`build_messages()`), `generate()`, the log row (`GenerationResult`), DIDComm envelope parsing. |
| `backends.py` | The model backends: `OpenRouterBackend` (cloud, paid) and `LMStudioBackend` (a local model, metered in watts). See [Local runs](#local-runs-lm-studio). |
| `pricing.py` | Cost from OpenRouter's public prices (no key): the estimate printed before a run, the go/no-go for a paid run, and the projected OpenRouter cost of a local run. |
| `energy.py` | `PowerMeter`: samples the GPU's power draw with `nvidia-smi` during a local call; energy in Wh and USD. |
| `paths.py` | Where `data/`, `schemas/` and `results/` are, anchored on the repository, so commands work from any directory. |

**`pipeline/`, grading:**

| File | Role |
|---|---|
| `scoring.py` | All four tiers for one answer (`evaluate_case()`): Tiers 1–3 from `checker.py`, then Tier 4 (`check_rules()`, one function per rule), `error_type`, and the verdict's layout. |
| `checker.py` | Tiers 1–3: the in-memory SQLite registrar (`seed_db()`), the consistency invariants (`consistency_checks()`) and `evaluate()`. |
| `didcomm_schema.py` | `validate_message()`: Tiers 1–2 against `schemas/registration_message.schema.json`. `OUTPUT_CONTRACT`: the prompt text that tells a model the format. |
| `degree_audit.py` | The degree audit: `audit()`, `counts_toward()`, `missing_prerequisites()`, grades and course equivalents. |
| `input_format.py` | `validate_input()`: the input-format rules a JSON Schema cannot say; `minutes()`. |
| `pairs.py` | Counterfactual pairs: `check_pairs()`, paired accuracy (`pair_stats()`), each row's `pair_result`. |
| `results.py` | The results file: rows, session summaries (cloud and local costs), atomic saves. |
| `reports.py` | The no-model commands: `--pair-report`, `--compare`, `--rescore`. |
| `selfcheck.py` | `--check`: every answer key and pair, graded by the scorer, no model. |
| `stats.py` | Wilson intervals and McNemar's test. |

**`pipeline/`, building the cases:**

| File | Role |
|---|---|
| `section_tally.py` | Reads Rowan's Section Tally export and turns its rows into input-format sections (`full_schedule()`, every section, converted once per run). `python -m pipeline.section_tally check` validates every section. See [Sections from the Section Tally](#sections-from-the-section-tally). |
| `fabricate.py` | Builds a case's input from its program and fabricates its transcript from a short spec (which requirements are complete, a seed, trap rows); checks the result with the degree audit. |
| `generate_pairs.py` | Writes the test cases: counterfactual pairs (200 pairs, 400 cases), two cases that differ in one detail of the prompt or transcript, which flips the answer. Builds each case, fabricates its transcript and scores every candidate answer to write the answer key. See [Counterfactual pairs](#counterfactual-pairs-paired_casesjson). |

**`data/` and `schemas/`:**

| File | Role |
|---|---|
| `data/paired_cases.json` (400) | The test cases (the default `--test-file`): each names its program, fabricates its transcript, and carries an answer key and its pair. Its sections are the whole Section Tally, sent once per session in the cached opening. |
| `data/degree_requirements.json` (50) | Degree requirements for 50 Rowan programs, parsed from their Academic Program Guides: requirements, program guide, course attributes, graduation rules, and guide-stated prerequisites. A case names its program instead of writing these out. See [Degree programs and fabricated transcripts](#degree-programs-and-fabricated-transcripts). |
| `data/section_tally_fall2026.csv` | The Fall 2026 Section Tally export, as downloaded (renamed). The pipeline reads this one file. |
| `schemas/registration_input.schema.json` | The input format rules, as JSON Schema: student, transcript, program requirements, schedule, prompt, and the optional Rowan program-guide fields. See [The input format](#the-input-format). |
| `schemas/registration_message.schema.json` | The message rules, as JSON Schema (draft 2020-12). Source of truth for the cart message. |

Earlier work is in git history, not in the working tree. The 250 single (unpaired) cases and
their generator (`input_format_cases.json`, `generate_cases.py`) were removed on 10/20; the last
commit with them is `0afe332`. Older still: the single-section message
(`old_schema/`) and the 30 cases whose input was a bare list of sections (`test_cases.jsonl`,
`semantic_test_cases.jsonl`, `cart_test_cases.jsonl`). The last commit with all of them is
`e02b9f1`; for example `git show e02b9f1:cart_test_cases.jsonl`.

## Setup

OpenRouter lets the harness call many LLMs (Claude, GPT, Llama, …) with one API key.

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
python run_semantic_eval.py --model anthropic/claude-sonnet-5.5 --max-cost 5
python run_semantic_eval.py --model anthropic/claude-sonnet-5.5 --task-ids pair-transfer-philosophy-ba-a pair-transfer-philosophy-ba-b -n 3
python run_semantic_eval.py --model anthropic/claude-sonnet-5.5 --estimate-only    # price it, spend nothing
python run_semantic_eval.py --check                                              # no model, no cost
```

A paid run prints its estimated cost first, then starts only when `--max-cost` covers the
estimate, when you type `y` at `Continue? [y/N]`, or with `--yes`. A script that can't be
asked and gives neither `--max-cost` nor `--yes` does not start, so nothing is spent by mistake.

| Option | Meaning | Default |
|---|---|---|
| `--model` | One model ID (required unless `--check`) | |
| `--test-file` | Which case file to run | `data/paired_cases.json` |
| `--task-ids` | Run only these cases (give both halves of a pair) | all cases in the file |
| `-n` | Samples per case (3 or more to compare sessions) | `1` |
| `--tally` | Which version of the Section Tally opens the session: `raw`, `trimmed`, `compact`, `raw-open`, `trimmed-open` (see below) | `trimmed` |
| `--effort` | Reasoning effort sent to the model: `minimal`, `low`, `medium`, `high`, `xhigh` | the model's default |
| `--max-tokens` | Cap on output tokens per call, thinking included; far above the ~500-1,000 a call uses, so it only stops a runaway answer | `16000` |
| `--max-cost` | USD. The run prints its estimated cost first and does not start above this; it also stops once the calls made have cost this much | no limit |
| `--yes` | Start a paid run without asking (otherwise it asks, unless `--max-cost` is set) | off |
| `--estimate-only` | Print the run's estimated OpenRouter cost and exit. No model call | off |
| `--backend` | `openrouter` (cloud, paid) or `lmstudio` (a local model on this machine, see [Local runs](#local-runs-lm-studio)) | `openrouter` |
| `--dashboard` | Open a live dashboard of the run in the browser (see [Watch a run](#watch-a-run-the-dashboard)) | off |
| `--resume` | With `--session-per-pair`: continue the last batch in `--output`, skipping pairs already done | off |
| `--pairs N` | Run N pairs (2N cases), the families taking turns, instead of `--task-ids` | |
| `--session-per-pair` | Each pair in its own session, its opening only that pair's subjects; ends with the batch's totals. See [Local runs](#local-runs-lm-studio) | off |
| `--batch-report` | Print the grand totals of every `--session-per-pair` batch in `--output`, and exit. No model call | off |
| `--subjects` | **Dev runs only.** Keep only these subjects' sections in the opening (`CS MATH`), or `auto` for the subjects the chosen cases need. The opening then fits a local model and costs a fraction; results are not comparable with whole-tally runs, and the summary records `tally_subjects` | the whole tally |
| `--cache-ttl` | How long the session's cached opening lives between calls, `5m` or `1h` (use `1h` when calls may be more than 5 minutes apart) | `5m` |
| `--rendering` | How a case's own state is shown to the model: `json`, `csv` or `nl` (plain text) | `json` |
| `--output` | Results file (runs are appended; name a new file for a fresh run) | `results/semantic_eval_results.json` |
| `--check` | Score each case's own answer key and every pair, and exit. No model call | off |
| `--pair-report` | Print paired accuracy per session from `--output`, and exit. No model call | off |
| `--rescore` | Re-grade the saved answers in `--output` with the current scorer and rewrite their verdicts. No model call | off |
| `--compare A B` | McNemar's test between two results files, per case and per pair. No model call | |
| `--lmstudio-url`, `--price-as`, `--kwh-price`, `--watts`, `--extra-watts` | Local runs only; see [Local runs](#local-runs-lm-studio) | |

Every sample is appended to the results file, and the run prints paired accuracy at the end
(see [Counterfactual pairs](#counterfactual-pairs-paired_casesjson)).

### The session

Every run is one session:

1. **Start the session** (`TallySession` in `session.py`): read the whole Section
   Tally once and build the opening turn in the chosen `--tally` version: a short key to the
   columns, then every section, 5,129 rows, as CSV.
2. **Estimate the cost.** From OpenRouter's current prices for the model, the run prints an
   estimate (one cache write of the opening, a cache read of it per later call, each case's own
   turn and an answer), leaning high. With `--max-cost`, it does not start above that.
3. **Prompt each case.** Every case is one call with four turns (`harness.build_messages()`):
   - the system prompt;
   - the opening: the Section Tally **and the output instructions**, byte-identical every
     time and marked for prompt caching;
   - the model's fixed reply, "I have read the Section Tally. Send the registration request."
     (`harness.SESSION_ACK`);
   - the case: its student, transcript, requirements, program guide, request and the term.
   The model never sees another case or its answer, so the order of cases does not matter.
   With `--max-cost`, the run stops once the calls made have cost that much.
4. **Close the session.** It prints the model calls, input tokens, how many were written to
   and read from the cache, and the cost, and writes the session summary into the results file.

The API keeps no state between calls, so every call resends the opening; the provider's prompt
cache is what makes that cheap. The first call writes the opening to the cache (1.25x the
input price) and later calls read it from there (Sonnet 5.5: $0.10 per million tokens against
$2.00 uncached). The cache expires 5 minutes after its last use, or 1 hour with
`--cache-ttl 1h`; a run with gaps longer than that pays to write it again. The output
instructions sit inside the cached opening because they are the same for every case: they
are cached with the tally instead of being resent at full price in every case's own turn.
The summary records this as `prompt_layout`.

**Five versions of the opening** (`--tally`). The checker holds all 5,129 sections in every
version, so the answer keys are the same; a correct cart never needs a full or cancelled section.

| `--tally` | The opening | Rows | Size |
|---|---|---|---|
| `raw` | The export as downloaded: every row and column (instructor, part of term, room capacity, reserved seats...) | 5,129 | 1.27M characters, ~674k tokens |
| `trimmed` (default) | Converted rows; columns no check uses dropped (`TALLY_FIELDS`) | 5,129 | 753k characters, ~462k tokens |
| `compact` | `trimmed` without each meeting's room and type and without the waitlist column (`COMPACT_FIELDS`); full and cancelled sections stay | 5,129 | 595k characters (21% smaller), ~370k tokens (estimated) |
| `raw-open` | Every column, but full and cancelled sections dropped | 2,860 | 720k characters, ~381k tokens |
| `trimmed-open` | Converted rows, trimmed columns (also no waitlist / cancelled), full and cancelled dropped | 2,860 | 410k characters, ~254k tokens |

Token counts are the cache writes measured on Claude Sonnet 5.5. `compact` drops only what no
check and no answer key uses: the time checks read a meeting's days and times, Tier 3 lets a
message leave out a meeting's room and type, and no request asks for a waitlist. It keeps full
and cancelled sections, so the full-section fallback pairs and the cancelled-section trap still
work; that is why it is the cost-saving choice that leaves the semantic checks as they are,
where the `-open` versions drop the full sections those pairs depend on. Add new versions rather
than change `trimmed`, so results stay comparable across runs.

With a raw opening the instructions say how to build a message entry from the CSV columns
(course_id = Subj + Course#, seats_taken = Enrolled, modality from Session, meetings from
Meeting Time with full day names); with an open-only opening they add that full and cancelled
sections are not listed.

**Cost per call** (Claude Sonnet 5.5, `trimmed`, measured): about $0.068, of which $0.046 is
reading the cached tally, $0.013 the case's own turn and $0.009 the answer, after a one-time
cache write of about $1.15. All 200 pairs (400 calls) come to about $28 on Sonnet and $57 on
Opus 5.5; `compact` is about 15% less. Only providers that cache honor the marker; caching
through OpenRouter is confirmed on Claude. The first call can take minutes, so the OpenRouter
request timeout is 10 minutes.

**Batch pricing.** OpenRouter's `:batch` models (e.g. `anthropic/claude-sonnet-5.5:batch`) are
half price, cache reads and writes included, and need no code change: pass the batch model as
`--model`. Two cautions before a large batch run. Cache hits inside a batch are best-effort, and
with this opening a miss costs about as much as 13 hits save, so batch pays only if about 93% of
calls hit the cache: try a few calls first and read the summary's `tokens.cache_read_share`.
And batch latency is queue time, so `latency` and `first_token_ms` say nothing about the model.

What is measured: the model must find the right few sections among 5,129, so a wrong answer
can come from reading a very long input as well as from reasoning. The opening does not fit
models with a context window below about 400k tokens (e.g. Claude Haiku 4.5, 200k).

### The validators alone

```bash
python run_semantic_eval.py --check    # every case and its answer key, no model, no cost
python -m pipeline.section_tally check # every tally section converts and fits the input schema
python -m unittest discover tests      # 34 example messages (Tiers 1-2), the local backend, energy, cost projection
```

### Local runs (LM Studio)

Try a model on this machine before spending money on it. A local run costs nothing on
OpenRouter. It records what it did cost, the electricity the GPU drew, and what the same calls
**would** have cost on OpenRouter, priced from OpenRouter's public list (no key is used, so no
funds are touched). This is phase 1 of the design (§4.2): develop and debug locally, then pay
for cloud runs.

**Setup (once).**
1. Install [LM Studio](https://lmstudio.ai) and download a model (in the app, or
   `lms get qwen/qwen3-4b`). With 8 GB of VRAM, a 4B–8B model at 4-bit quantization fits.
2. Load it with a long enough context and start the server:
   ```bash
   lms load qwen/qwen3-4b --context-length 32768
   lms server start                      # or: LM Studio > Developer > Start Server
   ```
3. An NVIDIA GPU is measured with `nvidia-smi` (it ships with the driver). Without one, pass
   `--watts` with the machine's draw under load.

**Run one pair.**
```bash
python run_semantic_eval.py --backend lmstudio --model qwen/qwen3-4b --subjects auto \
    --price-as anthropic/claude-sonnet-5.5 --task-ids pair-modality-marketing-bs-a pair-modality-marketing-bs-b
```

The whole Section Tally (~370k–460k tokens) does not fit a local context window, so a local run
needs `--subjects`. `auto` keeps the subjects the chosen cases can be answered from (their
candidate courses and every answer-key section). Before the run, the pipeline checks that the
server is up and has the model. If LM Studio reports the loaded context length, a call that
fills it is failed rather than scored, because a local server may silently cut a prompt that
does not fit.

**Run many cases: one session per pair.** The subjects of 200 cases (48 of them) still make a
~250k-token opening, so `--session-per-pair` runs each pair as its own session whose opening
holds only that pair's subjects: a median of ~22k tokens per call, 55k at most. `--pairs N`
picks N pairs with the ten families taking turns (`--pairs 100` is 10 pairs of each family,
200 cases).
```bash
python run_semantic_eval.py --backend lmstudio --model qwen/qwen3-4b --price-as anthropic/claude-sonnet-5.5 \
    --tally compact --pairs 100 --session-per-pair --kwh-price 0.20 --output results/local_qwen3-4b.json
python run_semantic_eval.py --batch-report --output results/local_qwen3-4b.json     # the totals again, later
```
It prints the batch's grand totals at the end, like this (from a run against a stand-in server):
```
=== batch totals
batch-20261023T035838Z (stub-model via lmstudio): 3 sessions, 6 calls
  semantic_ok: 0/6
  paired accuracy: 0/3 pairs (0%, 95% CI 0.00-0.56)
  billed by OpenRouter: $0.0000
  electricity: 0.000 Wh = 0.0000 kWh, $0.0000 at $0.2/kWh
  projected on OpenRouter as anthropic/claude-sonnet-5.5: $0.2513 as sent (each pair with its own small opening, written to the cache once)
  the same cases in one whole-tally session on OpenRouter: about $1.41 (estimate; the real experiment)
```
Every session of a batch carries the same `batch` block in its summary (`id`, `sessions`,
`price_as`, `whole_tally_estimate_usd`), and its session id ends in its number (`-001`, `-002`, ...).
"As sent" prices what the local run really sent, 100 small openings each written to the cache
once. "One whole-tally session" is what the experiment itself would cost on OpenRouter, the
tally written once and read from the cache after that. With a 32k context, about a quarter of
the pairs do not fit; load the model with more (`--context-length 65536`) if your VRAM allows.
`--session-per-pair` works on OpenRouter too, with one go/no-go for the whole batch's estimate.

| Option | Meaning | Default |
|---|---|---|
| `--lmstudio-url` | LM Studio's server | `http://localhost:1234` (or `$LMSTUDIO_URL`) |
| `--price-as` | The OpenRouter model whose prices the projected cost uses | `--model` |
| `--kwh-price` | Electricity, USD per kWh. Use the rate on your bill | `0.20` |
| `--watts` | A fixed power draw instead of measuring the GPU | measured |
| `--extra-watts` | Added to the measured GPU draw for the rest of the machine (CPU, memory, fans; often 50–150 W under load) | `0` |

**What it prints.** Before the run: the projected OpenRouter cost of the run as made and, with
`--subjects`, of the same cases with the whole tally (the real experiment). Per call, for example:
`[local: 21,000 in, 300 out, 0.8512 Wh at 182 W, $0.000170; OpenRouter ~$0.0546]`. At the end:
the session's electricity and projected total.

**What it records.** Each row: `energy_wh`, `avg_watts`, `energy_usd` and
`projected_openrouter_usd` (`usd_cost` stays 0). The session summary: `backend`,
`tally_subjects`, `power` (`source`, `idle_watts`, `extra_watts`), `price_as`,
`kwh_price_usd`, `context_length`, `projected_estimate_usd`,
`projected_whole_tally_estimate_usd`, and `local_cost`: energy in Wh and USD, average watts,
energy per call and per correct answer, and `projected_openrouter` (total, per call, per
correct answer).

**How far to trust the projection.** It uses the local model's token counts. It is close when
`--price-as` is the same model on OpenRouter, e.g. a local Llama 3.1 8B priced as
`meta-llama/llama-3.1-8b-instruct`. Pricing a different model, e.g. a local Qwen as Claude,
uses the wrong tokenizer and the local model's answer lengths: treat it as an order of
magnitude. The local server reports one prompt-token count, so the cached part (the opening)
and the uncached part (the case) are split in proportion to their characters. The first call
writes the cache and later ones read it, as on OpenRouter, and a model with no cache price pays
full input price every call. Local results are for development; they do not replace the cloud
runs in reported findings.

### Watch a run (the dashboard)

```bash
python run_semantic_eval.py ... --dashboard                                      # with a run
python -m pipeline.dashboard --output C:/Users/Khalil/csr2_runs/llama_200.json    # any results file, any time
```

Opens `http://localhost:8765`, a page served from this computer only, which rereads the results
file every 5 seconds. A menu picks the run: a batch, or a single session. It shows:
- progress with the time left;
- correct answers and pairs passed (with a 95% interval);
- electricity in Wh and USD, the projected OpenRouter cost as sent, and the whole-tally estimate;
- outcomes by pair family, as a stacked bar per family (or as a table);
- seconds per call (a rising line can mean the GPU is running hot) and watts per call;
- the latest 20 calls.

It follows the system's light or dark theme and makes no model call. LM Studio's own
**Developer** tab shows the model's side live (each request and the answer streaming in), and
Task Manager > Performance > GPU shows load, memory and temperature.

### Change the model

Model IDs are the `provider/model` names on <https://openrouter.ai/models>. Click a model
and copy the ID under its name. Spelling must be exact. Run one model per command:

```bash
python run_semantic_eval.py --model anthropic/claude-sonnet-5
python run_semantic_eval.py --model openai/gpt-5.5
```

Estimated cost per call on `--tally trimmed` (the cached tally read, the case and an answer;
the first call of a session adds the cache write):

| Model ID | ~Cost per call | Cache write, once per session |
|---|---|---|
| `anthropic/claude-haiku-5.5` | $0.03 | $0.29 |
| `anthropic/claude-sonnet-5.5` | $0.068 (measured) | $1.15 (measured) |
| `anthropic/claude-opus-5.5` | $0.14 | $2.31 |
| `anthropic/claude-fable-5.1` | $0.23 | $5.77 |

**Watch the cost.** Cost ≈ cache write + price per call × number of calls, per model and tally
version. Use `--max-cost`, and try new models on a couple of `--task-ids` first. Avoid very
expensive models unless the group agrees. `--estimate-only` prints the estimate without
calling anything. To debug prompts or try an open model, run it locally first
([Local runs](#local-runs-lm-studio)): free, and it projects the OpenRouter cost. A model with
no cache price on OpenRouter pays the full input price for the opening on every call, and the
estimate assumes so.

## Read the results

`run_semantic_eval.py` prints one line per case (one per sample when `-n` is above 1), then
a total:

```
pair-transfer-philosophy-ba-b: semantic_ok          | satisfies every rule

5/6 semantic_ok on anthropic/claude-sonnet-5.5. Cost $1.4941. Full results in results/past_runs/pairs_sonnet_trimmed.json (session trimmed-20261019T225009Z)
Paired accuracy: 2/3 pairs with both variants semantic_ok (67%)
```

| Outcome | Tier | Meaning |
|---|---|---|
| `syntactic_error` | 1 | Not a DIDComm message: not valid JSON (e.g. the model wrapped it in a ```` ```json ```` code block, or returned nothing), or no `id`, or a malformed `type` |
| `schema_error` | 2 | A DIDComm message, but not our register message: wrong `type`, or the body isn't exactly `student_id`, `term_code` and a `sections` list of section objects |
| `consistency_error` | 3 | Breaks a rule, e.g. full section, time conflict, unmet prerequisite |
| `semantic_error` | 4 | Passes every rule, but the cart is not one the intent asked for |
| `semantic_ok` | 4 | Correct |
| `GENERATION FAILED` / `generation_failed` | | The call itself failed; the error message says why |

Where the full results go: `run_semantic_eval.py` → `results/semantic_eval_results.json` (or
`--output`), one indented JSON object, `{"sessions": [...], "results": [...]}`. Each run of the
script is one session. What every call of a session shares is written once, in its summary;
each result row keeps only what is its own and names its `session_id`. Nothing is stored twice,
except `deviation_score` (per row, and averaged in the summary).

**`sessions[]`, one summary per run.**
- **Settings (to repeat the run):** `session_id`, `model`, `test_file` and `test_file_sha256`,
  `tally_variant`, `rendering`, `samples_per_case`, `cache_ttl`,
  `generation_settings` (exactly what every call sent: `temperature`, `stream`, `usage`, and
  `max_tokens`, and `reasoning.effort` when `--effort` is given), `prompt_layout` (how a call is
  laid out), `shared_context` (the cached opening's sizes and hash), `output_contract` (the
  instructions in the cached opening), and `cost_estimate_usd` / `max_cost_usd`;
  `stopped_at_max_cost: true` when `--max-cost` ended the run early.
- **Correctness:** `cases`, `calls`, `generation_failures`, `outcomes`; `semantic_ok` as
  `{count, rate, ci95}` (a 95% Wilson interval, over calls that did not fail);
  `semantic_ok_by_case_type`; `mean_chance_baseline` (the average chance of a correct answer by
  picking a plausible answer at random, from each case's `difficulty`; compare it to the rate);
  `mean_deviation_score`; `error_types` (failures counted by kind, see below); and
  `paired_accuracy` when the session ran pairs (`passed`, `scored`, `rate`, `ci95`,
  `strong_pairs_only` with its own interval, `weak_pairs_scored`, `incomplete`, `by_family`).
  With fewer than 3 samples per case, a `sampling_note` warns that differences may be chance.
- **The calls:** `finish_reasons` (stop reasons counted, e.g. `stop (end_turn)`: 6),
  `served_by` (model and provider OpenRouter actually used, counted), `tokens` (prompt, cached,
  cache-write, completion, `reasoning_tokens`, `cache_read_share`), and `latency`
  (`mean_total_ms`, `mean_first_token_ms`).
- **Cost:** `cost.total_usd`; `avg_per_call_usd`; `setup_usd_est` (the one-time cache write of
  the shared opening) and `per_case_marginal_usd_est` (one more case once the cache is warm).
  These two are estimates: OpenRouter reports one cost per call, so the marginal cost is the
  median cost of cache-reading calls, and setup is what the writing call cost above that.
  Also `per_correct_answer_usd` and `per_passed_pair_usd`. A local session also has
  `local_cost` (electricity and the projected OpenRouter cost). `--rescore` makes no model call
  and costs nothing.

**`results[]`, one row per call:**
- **Identity:** `task_id`, `session_id`, `run_index`, `ts`, `case_hashes` (`input`: a hash of
  what the model saw; `rules`: a hash of the answer key's rules), so a result can be tied to the
  exact case version it ran on.
- **What was sent:** `prompt_readable`, the case's own turn as `known_state` and `intent`; the
  output instructions are in the cached opening (the summary's `output_contract`).
- **What came back:** `parsed_output`, the model's message. `raw_output` (the verbatim text) is
  kept only when it is not JSON, e.g. wrapped in a code fence, so the parsed copy would be lossy.
- **The call:** `error` (null when the call succeeded); `finish_reason` and
  `native_finish_reason` (why generation stopped: `stop`/`end_turn`, `length`/`max_tokens` =
  cut off, `refusal`); `served_model`, `provider` and `generation_id` (OpenRouter's id, for its
  dashboard); `prompt_tokens`, `cached_tokens`, `cache_write_tokens`, `completion_tokens`,
  `reasoning_tokens` (thinking, part of completion), `first_token_ms` (time to the first streamed
  token, thinking or answer), `latency_ms`, `usd_cost`. A local run adds `energy_wh`,
  `avg_watts`, `energy_usd` and `projected_openrouter_usd`
  ([Local runs](#local-runs-lm-studio)); cloud rows never carry them.
- **Grading:** `verdict`, with `error_type` on a failure. For a pair case, also `pair_result`:
  `pair_id`, `twin_id`, `same_answer_as_twin`, `pair_passed`, and an `explanation` naming the
  twin's outcome.

**`error_type`** names what went wrong, so failures can be counted.
- **A failed pair case** is named for the reasoning its family tests: `fallback_not_applied`,
  `in_progress_course_ignored`, `transfer_credit_ignored`, `time_overlap_missed`,
  `and_or_requirement_misread`, `credit_arithmetic`, `modality_ignored`, `meeting_days_ignored`,
  `minimum_grade_misread`, `withdrawal_counted_as_credit`.
- **Any other semantic error** is named for its first broken rule: `wrong_number_of_sections`,
  `requirement_mismatch`, `named_course_missing`, `time_limit_ignored`, `wrong_course`, and so on.
- **A consistency error** is named for its first failed Tier 3 check: `full_section`,
  `time_conflict_in_cart`, `prerequisite_missing`, `section_details_miscopied`, and so on.
- **A format error** is named for its first failed Tier 1–2 check: `not_json`, `not_didcomm`,
  `malformed_section_entry`, and so on.
- **From the call itself:** `truncated_at_max_tokens` (a format error from an answer cut off at
  `max_tokens`), `refused`, and `call_failed`.

**Comparing sessions:** `python run_semantic_eval.py --test-file <cases> --compare A.json B.json`
runs McNemar's exact test on the case runs both files have, per case and per pair. It prints
how often only A or only B was right, and p. With one sample per case and a handful of cases,
expect p near 1: run `-n 3` or more on more cases before claiming a difference.

**The four `results/past_runs/pairs_sonnet_*.json` sessions** ran before the instructions moved into the cached
opening: their calls sent the instructions at the end of each case's own turn (same words). Their
summaries say so in `prompt_layout`.

Dropped as duplicates: `ok` (= `error` is null), `prompt_sent`, and the per-row copies of the
session's fields.

## Troubleshooting

| You see | Fix |
|---|---|
| `OpenRouter needs an API key` | Key not set, or you didn't restart VS Code/terminals after setting it (Setup step 3) |
| `OpenRouter 401` | Key is wrong or was deleted. Ask Khalil for a new one |
| `OpenRouter 402` | Out of credits, or your key hit its spending limit. Tell Khalil |
| `OpenRouter 400 ... not a valid model ID` | Typo in the model ID. Copy it from openrouter.ai/models |
| `ModuleNotFoundError: No module named 'requests'` or `'jsonschema'` | venv isn't active, or `pip install -r requirements.txt` hasn't been run since `jsonschema` was added (Setup step 2) |
| Blank `raw:` with `syntactic_error` | The model refused the request (seen with Fable 5.1). Not a formatting problem; we don't log refusals separately yet |
| `LM Studio is not reachable` | Start the server: LM Studio > Developer > Start Server, or `lms server start` |
| `LM Studio has no model ...` | The id must match `lms ls` exactly; download it in LM Studio or with `lms get <model>` |
| `cannot read GPU power` | No NVIDIA GPU or driver: pass `--watts <the machine's draw under load>` |
| `the call filled the model's context window` | Reload the model with a longer context (`lms load <model> --context-length 32768`), or narrow `--subjects` |
| `not started: a paid run needs --max-cost <USD> or --yes` | The run could not ask you to confirm: add a budget (`--max-cost 5`) or `--yes` |

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
| `"sections": ["41836"]` (bare crns) or `[{"crn": "41836"}]` (crn only), the earlier shapes | `schema_error` |
| No `term_code`, or `"term_code": 202640` as a number | `schema_error` |
| `"sections": []`, or the same crn twice | `schema_error` |
| A section object missing a required field (`crn`, `meetings`, `credit_hours`, …), or with an unknown field | `schema_error` |
| `"credit_hours": "3"`, `"seats_taken": "20"` or `"waitlist": "yes"` | `schema_error` |
| `"course_id": "CS 04114"`, `"modality": "remote"`, `"days": ["MW"]` or a 24-hour time | `schema_error` |
| A meeting with `start_time` but no `end_time`, or an unknown meeting field | `schema_error` |
| Extra body field | `schema_error` |

`tests/test_message_schema.py` runs these and more; all 34 give the expected outcome.

## The input format

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

`schemas/registration_input.schema.json` holds the field rules (types, the course id pattern, allowed
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

A test case wraps one input object with an id, its candidate courses and an answer key:

```json
{
  "case_id": "pair-busy_block-journalism-ba-a",
  "input": { "program": "journalism-ba", "student": {}, "transcript": {"fabricate": {}}, "prompt": "Register me for News Media Literacy (JRN02101). I work Mondays and Wednesdays ..." },
  "candidate_courses": ["JRN02101"],
  "ground_truth": { "rules": { "count": {"min": 1, "max": 1}, "must_include_courses": ["JRN02101"], "busy": [{"days": ["Monday", "Wednesday"], "start_time": "10:00 AM", "end_time": "11:15 AM"}] } }
}
```

`cases.load_test_cases()` builds the full input object when it loads a case
(`resolve_case()`): the program from `data/degree_requirements.json`, the fabricated transcript, and
the whole Section Tally as `schedule` (5,129 sections, cancelled ones flagged). The checker's
database holds every section; the case's own turn carries only the term, because the sections
are the session's opening (see "The session"). A case never writes out a schedule. Its
`candidate_courses` (the courses the case is about) are not shown to the model: the pair
generator and the chance baseline use them.

### Sections from the Section Tally

The schedule can come from Rowan's real Section Tally (Fall 2026, 5,129 sections, 138
subjects) instead of invented sections. The data is one file, `data/section_tally_fall2026.csv`:
the export as downloaded, renamed, read whole on each run (about 20 ms). Every case gets
all of them (`section_tally.full_schedule()`). All 5,129 sections convert and
validate (`python -m pipeline.section_tally check`, which fails if the export is missing or empty).
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
| `Cancelled` | `cancelled` | cancelled sections stay in the schedule with `cancelled: true` |
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

- **The `*` after `Room Cap`** (296 sections) is not documented in the export.
- **Only the Fall term code is known.** `TERM_SEASONS` in `section_tally.py` maps the last two
  digits (`40` → Fall); others are added when a tally for that term shows its dates.
- **Reserved seats are shown but not checked.** Waitlists are checked: the message can ask
  to join one (see the message body).

### How the spec's open items were filled in [provisional]

| Spec open item | What the code does |
|---|---|
| 1. Section id | Each section object in the message names its section by `crn`. The prompt tells the model so. |
| 2. Meeting times | Two sections conflict when they share a day and their time ranges overlap (`meetings` table in `checker.py`). |
| 3. One section or several per message | Several: the spec assumed one enroll message per section, but the 10/1 meeting settled on a cart, so one message registers every section. |
| 4. Prerequisites | New optional `prerequisites` list. Treated as a **consistency** rule (Tier 3): registering for a course whose prerequisite is unmet is a `consistency_error`. A course still in progress does not satisfy a prerequisite. |
| 5. `TA` and `minimum_grade` | Accepted transfer credit (`TA`) meets every minimum grade. `F` and `W` meet none. |
| 6. Other requirement types | New optional fields, all read by `degree_audit.audit()`. See the next table. |
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

`degree_audit.audit()` applies waivers, reports `missing` for the closest group of a
`one_of_groups` rule and `cap_remaining` for credit caps, and computes `major_gpa`.
`counts_toward()` skips a course whose credit cap is used up. Known limitation:
`counts_toward()` does not simulate `exclusive`, so a course that an earlier exclusive
requirement would take is still reported for a later one; the cases are written so this does
not change an answer.

What the checker does and does not read:

- **Tier 3 reads** the schedule and the prerequisites (against the transcript).
- **The audit is not part of scoring.** `degree_audit.audit()` and `counts_toward()` report
  which requirements are met and which courses would move one forward. They are for case
  authors and for checking an answer key. Tier 4 still compares the cart to the answer key,
  so whether a course "counts toward the degree" is decided by the author, with the audit
  as a cross-check.

### Degree programs and fabricated transcripts

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

### Counterfactual pairs (`paired_cases.json`)

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
python generate_pairs.py                                    # adds pairs until data/paired_cases.json holds 200
python generate_pairs.py --total 250                        # grow it; pairs already there are kept
python generate_pairs.py --annotate                         # only rewrite each pair's `difference` field
python run_semantic_eval.py --check
python run_semantic_eval.py --model anthropic/claude-opus-5.5 --output results/pair_results.json
python run_semantic_eval.py --output results/pair_results.json --pair-report
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

**200 pairs (400 cases).** Families take turns adding one pair each, so they stay even: 21 each
for `full_fallback`, `in_progress`, `transfer`, `busy_block`, `credit_limit`, `modality` and
`days`, 20 each for `grade_threshold` and `withdrawal`, and 13 for `nested_groups`, which runs
out first. The generator goes through every program once, then goes round again with a new seed
(up to 4 rounds), so a program can give one family a second pair, built from other courses. A
later-round pair's id ends in `#<round>` (`transfer/philosophy-ba#3`, case
`pair-transfer-philosophy-ba-3-a`). A pair with the same rules and change as one already in that
program is skipped. Most families use 20 or more programs; `transfer` uses 8 and `nested_groups` 8.

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
- `candidate_courses`: how many courses the case is about (its `candidate_courses` list).
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

### Removed case files

The 21 hand-built cases (`case_001`–`case_015`, `aneesh_test` and five `rowan_` cases) were removed
on 10/18, and the 250 generated single cases (`input_format_cases.json`, `<program>-1` to `-5`)
and `generate_cases.py` on 10/20: the pipeline now runs on the pairs only. Both are in git history
(the hand-built cases at `bc6c9ba`, the single cases at `0afe332`). The six programs the hand-built
cases used remain in `degree_requirements.json`.

## Correctness model

Four tiers, stopping at the first failure:
`syntactic_error` → `schema_error` → `consistency_error` → `semantic_error` / `semantic_ok`.

- **Tiers 1–2, format.** See [The cart message](#the-cart-message-didcomm-schema).
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

## Results so far

**Current runs** (`results/past_runs/`): `pairs_sonnet_raw.json`, `pairs_sonnet_trimmed.json`,
`pairs_sonnet_raw-open.json`, `pairs_sonnet_trimmed-open.json` and `pairs_sonnet_compact.json` hold Claude Sonnet 5.5 at medium effort on the same 3 random
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
6. **How strictly the copied section fields are checked.** Every entry must now repeat the
   section's details (`course_id`, `section`, `credit_hours`, seats, `campus`, `modality`,
   `meetings`). Tier 3 ignores case, day order and a leading zero, and lets a meeting's
   `type` and `location` be left out. Should a wrong copy be a `consistency_error` (as now)
   or a separate measured deviation, given that it can fail an otherwise right cart?
7. **Waitlisting and time conflicts.** A waitlisted section is still checked for time
   conflicts with the rest of the cart. Is that right, given the student isn't enrolled yet?

The design's own open questions are in [§13](#13-open-questions--for-the-group).

## Provisional (will change; isolated to one spot each)

- DIDComm envelope shape → `harness.parse_didcomm()`
- Time-conflict logic (interval overlap) → `checker.consistency_checks()`
- Input-format open items: prerequisites as a consistency rule, `TA` meeting any minimum, best-attempt GPA → `degree_audit.py` (`missing_prerequisites()`, `meets_minimum()`, `audit()`)
- Tier 4 by rules → `scoring.check_rules()` (`RULE_CHECKS`; the vocabulary is `cases.RULES`)
- The columns of the tally in the cached opening → `render.TALLY_FIELDS`, `COMPACT_FIELDS`, `OPEN_FIELDS`
- In-memory SQLite → `checker.make_db()`
- Cart message type URI (`example.org` placeholder) → `schemas/registration_message.schema.json`

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
| 3.6 | 2026-10-22 | The pipeline split into single-purpose modules (`cases`, `render`, `session`, `backends`, `pricing`, `energy`, `scoring`, `checker`, `degree_audit`, `input_format`, `pairs`, `results`, `reports`, `selfcheck`, `stats`); `run_semantic_eval.py` is the command line. Tier 4 is one function per rule (`scoring.RULE_CHECKS`); `generate_pairs.py` passes its cached `counts_toward` to `check_rules()` instead of swapping `registration.counts_toward`. Prompts, verdicts, `--check`, rescored results and generated pairs are unchanged (compared before and after). New: `--backend lmstudio` for local runs, with GPU energy from `nvidia-smi` (`energy.py`) and the projected OpenRouter cost of every call (`--price-as`, `--kwh-price`, `--watts`, `--extra-watts`); `--subjects` (dev runs, a smaller opening; Aneesh's subject-limited tally); `--estimate-only`; a paid run starts only with `--max-cost`, a typed `y` or `--yes` (Ben's confirmation); the results file is saved atomically (Ben's fix). Faster: the Section Tally is converted once per run and validated once, and the checker looks up prerequisites only for courses that have them (`--check` 10 min → 1 min). Estimates no longer assume caching for a model with no cache price. `tests/`. Then the folders: the code in `pipeline/` (a package; `run_semantic_eval.py` and `generate_pairs.py` at the root are its two commands), `data/`, `schemas/`, `results/` (past runs in `results/past_runs/`), all paths in `pipeline/paths.py`; `registration.py` (old-name re-exports) removed; `--session-per-pair`, `--pairs N` and `--batch-report` (`pipeline/batch.py`): 200 cases on a local model in one command, with grand totals; `didcomm_schema.py`'s self-test moved to `tests/test_message_schema.py`; `section_tally.py check` is `python -m pipeline.section_tally check`. |
| 3.5 | 2026-10-21 | `code_version` removed from session summaries (and from the five Sonnet results files), with the git lookup that built it. `--max-cost` reads each provider's prices, including long-prompt rates (Claude Haiku 5.5 costs five times as much from 100k prompt tokens). `pairs_sonnet_compact.json`: Sonnet 5.5, medium effort, on the same 3 pairs with the `compact` tally. |
| 3.4 | 2026-10-21 | Cost cuts that leave the semantic checks as they are: a `compact` tally version (`trimmed` without meeting rooms and types and the waitlist column, 21% smaller); the output instructions move into the cached opening (`prompt_layout` in the summary); `--max-tokens` defaults to 16000; `--max-cost` prints an estimate, refuses to start above it and stops a run that reaches it; batch pricing documented. `--named-courses` removed with `named_courses()` and `section_tally.course_schedule()`: every run is a session over the whole tally, and cases carry `candidate_courses` instead of `schedule_from_tally`. Removed: conversion of old-format results files and the verdict fields only older files had. |
| 3.3 | 2026-10-20 | The pipeline runs on the pairs only: `input_format_cases.json` (250 single cases) and `generate_cases.py` removed; the helpers the pair generator needs (building and scoring a case, transcripts, course and requirement names, difficulty) moved into `generate_pairs.py` unchanged (regenerated pairs match the stored ones). `paired_cases.json` is the default `--test-file`; the single-case error types (`retake`, `passed`, `fallback`) removed. |
| 3.2 | 2026-10-19 | Removed `earlier_attempts` and `cost.total_spent_usd` (and `earlier_attempts_usd`) from session summaries and from the four Sonnet results files; `cost.total_usd` is the session's cost. |
| 3.1 | 2026-10-19 | Obsolete code removed: `registration.check_consistency()` (replaced by `consistency_checks()`); verdicts no longer build `tier3_detail` and `violated` only to drop them; `pair_summary()` formats `pair_stats()` instead of repeating its counting; leftover keys from intermediate file layouts. No file was obsolete. |
| 3.0 | 2026-10-19 | Research fields. The harness streams, and records per call `finish_reason` / `native_finish_reason`, `served_model`, `provider`, `generation_id`, `reasoning_tokens` and `first_token_ms`. New `--max-tokens` and `--effort`; every session records its `generation_settings`, `code_version` (commit, branch, uncommitted changes) and `test_file_sha256`; every row its `case_hashes`. Summaries gain 95% Wilson intervals, strong-pair accuracy, accuracy by case type, mean chance baseline, `error_types`, finish reasons, providers, first-token latency, a cost split (setup vs per case, per correct answer, per passed pair, earlier attempts, total spent) and a sampling note under 3 samples. Verdicts gain `error_type`. Case files gain `ground_truth.difficulty` (all cases) and `pair.has_shared_answer` (18 weak pairs). New `--compare` (McNemar's test). The four Sonnet sessions were backfilled with what is known about them. |
| 2.9 | 2026-10-19 | A failed Tier 4 check explains itself in `why` (what was chosen, why it breaks the rule, what would have satisfied it), and a `semantic_error` verdict gets `why_wrong`: the failed checks plus the case's intended reasoning. |
| 2.8 | 2026-10-19 | Results files become `{"sessions", "results"}`: one summary per session (settings, output contract, cached-opening info, token/cost/latency totals, outcome counts, mean deviation score, paired accuracy), and slim rows that name their session. Removed duplicates: per-row session fields, `ok`, `prompt_sent`, the per-row output contract, `raw_output` when `parsed_output` holds the same message, `tier3_detail`, `violated`, `tier4_summary`, and in `pair_result` the copies of this row's and the twin's outcome and answer. `TallySession` no longer writes into rows. The four Sonnet results files were converted with `--rescore`. |
| 2.7 | 2026-10-19 | Verdicts explain Tiers 1-3 too: a `tiers` overview and `tier1_checks`, `tier2_checks` (from `didcomm_schema.validate_message()`) and `tier3_checks` (from `registration.consistency_checks()`; `check_consistency()` reports its first failure, so outcomes are unchanged). Pair rows get `pair_result`, relating each result to its twin's (`relate_pairs()`, after a run and on `--rescore`). 18 pairs found where one answer is valid in both variants (documented as weak pairs). |
| 2.6 | 2026-10-19 | Tier 4 verdicts gain `tier4_summary` and `tier4_checks` (each check's rule, requirement in words, what the cart had, pass/fail). New `--rescore` re-grades a saved results file without calling a model. Paired accuracy counts a pair with a failed call as incomplete, not as a miss. |
| 2.5 | 2026-10-18 | 200 pairs (400 cases): `generate_pairs.py` adds pairs to the file (`--total`), keeping those already there, with families taking turns and later rounds reusing programs with other courses. New `ground_truth.pair.difference` on every pair (`--annotate`). Transcript-flip twins are their base transcript plus one edited row (the fabricator's `edits`); `check_pairs()` now fails a pair whose cases differ in more than one place. 7 pairs that differed in 10 to 20 transcript rows were rebuilt, one of them (`withdrawal/human-services-ba`) from the original 40. |
| 2.4 | 2026-10-18 | Counterfactual pairs: `generate_pairs.py` writes `paired_cases.json` (10 families, 40 pairs, 80 cases), modeled on the Tyler branch's BIRD-style pairs. New rules `courses_in` and `busy`; `--check` validates pairs (`check_pairs()`); runs print paired accuracy, and `--pair-report` computes it from a results file. The fabricator takes `transfer_rows` and `in_progress`. Fixed `generate_cases.Case.counts_toward()` recursing when its cache missed during `score()`. |
| 2.3 | 2026-10-18 | Fifth case template, credit_limit (one per program, 50 cases; 250 in all), and the `credits` rule back in Tier 4. |
| 2.2 | 2026-10-18 | Code that no current case or the OpenRouter session pipeline uses was removed: listed answer keys (`acceptable_crns` / `acceptable_carts`, `check_semantic()`, `evaluate_full()`), the rules `credits`, `campus_in`, `must_include`, `must_exclude_courses` and `waitlist: true`, the Ollama backend and `--backend` (§4.1 above describes the original dual-backend plan), `TaskSpec.domain`, the never-filled `enrollments` checks, `section_tally.py show`, and the fabricator's unused spec options. `ROWAN_PROGRAM_GUIDE_TASK.md` (a finished handoff) and `semantic_eval_results_random3.json` (results of removed cases) were deleted. |
| 2.1 | 2026-10-18 | Four versions of the session's opening (`--tally raw / trimmed / raw-open / trimmed-open`); result rows drop backend, Ollama timers and prompt_sent, and gain session_id, tally_variant, session_cost_usd, session_avg_cost_per_prompt_usd. |
| 2.0 | 2026-10-18 | The 21 hand-built cases removed; the case file holds the 200 generated cases (four per program). |
| 1.9 | 2026-10-17 | Cases built from degree requirements: `degree_requirements.json` (50 programs from Rowan's Academic Program Guides), `fabricate.py` (a case names its program; its transcript is fabricated from a spec and checked with the audit), `generate_cases.py` (four verified cases per program, 200). The 21 hand-built cases now name their program and fabricate their transcripts around their original rows. 221 cases. See "Cases built from degree requirements". |
| 1.8 | 2026-10-17 | The session is the default pipeline: the whole Section Tally is the opening turn, cached, followed by a fixed acknowledgement and then each case as its own request. `--whole-tally` is gone; `--named-courses` runs the earlier per-case portion. OpenRouter timeout raised to 10 minutes for the first (cache-writing) call. See "The session (the default)". |
| 1.7 | 2026-10-15 | Rowan program guides: `program_guide`, `course_attributes`, `student.entry`, requirement fields `guide_section`, `course_groups` (rule `one_of_groups`), `course_ranges`, `recommended_courses`, `waived_if`, rule `credits_any`, and `graduation_rules.major_gpa`/`minimum_grade`; five `rowan_` cases (21 in all). The sixteen original cases rebuilt on the B.S. Computer Science guide (Fall 2026): full requirements, `program_guide`, `course_attributes`, `student.entry`; case_006 and case_009 prompts changed, case_011's old-id trap removed. See "Fields for Rowan program guides" and "The twenty-one cases". |
| 1.6 | 2026-10-14 | Merged into README.md. The old §15 (implementation status as of 9/17) removed: it described code that has since changed, and the rest of this README now covers the current state. Its one still-true finding (Ollama timings) moved to §9; the changelog is renumbered §15. The 1.5 text is in git history. |
| 1.5 | 2026-09-17 | Post-pivot pipeline implemented and verified: `harness.py` refactored to dual-backend (Ollama real, OpenRouter stub) + DIDComm output; `registration.py` domain testbed with a real consistency checker; `run_registration.py` end-to-end runner; README + .gitignore added. §15 rewritten to reflect built-and-verified state (3 of 4 outcome buckets fire automatically; semantic half stubbed pending §13.1). |
| 1.4 | 2026-09-17 | Added §4.2 (phased execution): local-first/cloud-second sequencing with capability vs. confidence crossover triggers; documented the backend switch mechanism (DIDComm contract above the backend split; `Backend` protocol; OpenRouter stub until Phase 2). Decided harness stays Python (inference-latency-bound, churny research code, analysis ecosystem) rather than Rust. |
| 1.3 | 2026-09-17 | Reconciled to 9/17 group decisions: backend Ollama→**Ollama + OpenRouter dual-backend** (§4.1, robustness design); output SQL→**DIDComm JSON**; banking dropped→registration + second domain (tax/travel); ACID narrowed to **C only**; added **two-dimension correctness** (consistency + semantic) and error taxonomy (§3, §8); ground truth = AI-generated + human-cross-verified test sets (§6); RQ2 gains dollar cost (§9); §13 reset around semantic verification as the deep open problem; §15 updated for the pivot. |
| 1.2 | 2026-09-17 | Added §15 (implementation status): walking skeleton runs end-to-end; `generate`/`make_db`/`execute`/`snapshot`/`log_result` real, `evaluate` stubbed. Validated nanosecond timing, cold/warm gap, success + loud-failure buckets live. Environment recorded (Windows + native Ollama + CUDA); added `seed_sql` to `GroundTruth`. |
| 1.1 | 2026-09-17 | Added §5 (two-layer statelessness/grounding), §9 (RQ2 prefill/decode timing), §11 (MVP infra + hardware availability); §7 interface contract; §3 testbed-fidelity principle; new group questions. Removed division-of-labor section. |
| 1.0 | 2026-09-17 | Initial draft: motivation, RQs, ACID scoping, experimental design, pipeline, three-bucket evaluation, non-goals. |
