# Running the pipeline

# Run

All commands are run from the repo root with `(venv)` active.

## The test cases, all four tiers

```bash
python run_semantic_eval.py --model anthropic/claude-opus-5.5
python run_semantic_eval.py --model anthropic/claude-opus-5.5 --task-ids pair-transfer-philosophy-ba-a pair-transfer-philosophy-ba-b
python run_semantic_eval.py --model anthropic/claude-opus-5.5 --named-courses   # the earlier way
python run_semantic_eval.py --check                                              # no model, no cost
```

| Option | Meaning | Default |
|---|---|---|
| `--model` | One model ID (required unless `--check`) | |
| `--test-file` | Which case file to run | `paired_cases.json` |
| `--task-ids` | Run only these cases | all cases in the file |
| `-n` | Samples per case | `1` |
| `--rendering` | How a case's own state is shown to the model: `json`, `csv` or `nl` (plain text) | `json` |
| `--check` | Score each case's own answer key and exit. No model call | off |
| `--named-courses` | No session: each case's prompt lists only the sections of the courses it names (below) | off |
| `--tally` | Which version of the Section Tally opens the session: `raw`, `trimmed`, `raw-open`, `trimmed-open` (see below) | `trimmed` |
| `--cache-ttl` | How long the session's cached opening lives between calls, `5m` or `1h` | `5m` |
| `--output` | Results file | `semantic_eval_results.json` |
| `--pair-report` | Print paired accuracy per model from `--output`, using `--test-file`'s pairs, and exit. No model call | off |
| `--rescore` | Re-grade the saved answers in `--output` with the current scorer and rewrite their verdicts. No model call | off |
| `--max-tokens` | Cap on output tokens per call (sent and recorded) | the provider's default |
| `--effort` | Reasoning effort sent to the model: `minimal`, `low`, `medium`, `high`, `xhigh` | the model's default |
| `--compare A B` | McNemar's test between two results files, per case and per pair. No model call | |

Appends every sample to the results file. When the cases include pairs, the run also prints
paired accuracy (see [Counterfactual pairs](test-cases.md#counterfactual-pairs-paired_casesjson)).

## The session (the default)

A run is one session:

1. **Start the session** (`TallySession` in `session.py`): read the whole Section
   Tally once and build the opening turn: a short key to the columns, then every section,
   5,129 rows, as CSV (`render_tally()`, about 753,000 characters, roughly 400k tokens).
2. **Prompt each case.** Every case is one call with four turns: the system prompt; the
   Section Tally (byte-identical every time, marked for prompt caching); the model's fixed
   reply, "I have read the Section Tally. Send the registration request."
   (`render.SESSION_ACK`); then the case: its student, transcript, requirements, program
   guide, request and the term (8k-9k tokens). The model never sees another case or its
   answer, so the order of cases does not matter.
3. **Close the session.** It prints the model calls, input tokens, how many were written to
   and read from the cache, and the cost. The cache expires on its own (5 minutes after its
   last use, or 1 hour with `--cache-ttl 1h`); a run with gaps longer than that pays to write it again.

**Four versions of the opening** (`--tally`). The checker holds all 5,129 sections in every
version, so the answer keys are the same; a correct cart never needs a full or cancelled section.

| # | `--tally` | The opening | Rows | Size |
|---|---|---|---|---|
| 1 | `raw` | The export as downloaded: every row and column (instructor, part of term, room capacity, reserved seats...) | 5,129 | 1.27M characters, ~674k tokens |
| 2 | `trimmed` (default) | Converted rows; columns no check uses dropped (`TALLY_FIELDS`) | 5,129 | 753k characters, ~386k tokens |
| 3 | `raw-open` | Every column, but full and cancelled sections dropped | 2,860 | 720k characters, ~380k tokens |
| 4 | `trimmed-open` | Converted rows, trimmed columns (also no waitlist / cancelled), full and cancelled dropped | 2,860 | 410k characters, ~210k tokens |

With a raw opening the instructions say how to build a message entry from the CSV columns (course_id =
Subj + Course#, seats_taken = Enrolled, modality from Session, meetings from Meeting Time with full day
names); with an open-only opening they add that full and cancelled sections are not listed. Costs scale
with the opening: roughly ×1.75 for `raw`, ×1 for `raw-open`, ×0.55 for `trimmed-open` against
`trimmed`. Only `trimmed-open` fits a 256k context.

**Result rows.** Each row names its `session_id` and `tally_variant`; when the session closes,
every row of that session gets `session_cost_usd` (the whole session) and
`session_avg_cost_per_prompt_usd`. `prompt_sent` is not logged: `prompt_readable` holds the
same text, parsed.

The model API keeps no state between calls, so "kept in its context" means every call resends
the tally; the provider's prompt cache is what makes that cheap. The first call writes the
tally to the cache and later calls read it from there.

The opening's columns are what a model needs to choose a section and copy it into the
message: crn, course_id, section, title, title_note, credit_hours, seats_total, seats_taken,
waitlist, campus, modality, meetings, cancelled. Instructor, part_of_term, room capacity,
session and reserved seats are left out (`TALLY_FIELDS`). Full and cancelled sections are
included, so the full-section, waitlist and cancelled-section traps still work. The checker
holds the same 5,129 sections.

Cost on Opus 5.5 (prices: input $4 per million tokens; a cache write costs 1.25×, a cache read
$0.20 per million): about $2.05 for the first case (the write), then about $0.13 per case
(about $0.08 to read the tally, $0.04 for the case, plus the answer). All 200 pairs (400 calls)
come to an estimated $57 with `--tally trimmed` (about $39 with `trimmed-open`, $75 with `raw`;
about half that on Sonnet 5.5; use `--task-ids` to run fewer). Only providers that cache honor the marker: caching through
OpenRouter was confirmed on Claude (a second call read the whole opening from the cache at
about a sixth of the cost). The first call can take minutes, so the
OpenRouter request timeout is 10 minutes.

What is measured: the model must find the right few sections among 5,129, so a wrong answer
can come from reading a very long input as well as from reasoning. The tally does not fit
Claude Haiku 4.5 (200k tokens), Grok 4.7 (500k) or the local Llama (131k); run those with
`--named-courses`.

## Named courses (`--named-courses`, the earlier pipeline)

No session: each case is one call whose prompt holds the student, transcript, requirements,
request and every open section of the courses the case names (`schedule_from_tally` in the
case file). The CS cases put about 490 sections in the prompt, about 130k input tokens or
about $0.50 per call on Opus 5.5; the rowan_ cases about 350-510 sections, $0.35-0.50.

## The validators alone

```bash
python didcomm_schema.py               # 34 example messages, no model, no cost
python run_semantic_eval.py --check    # every case and its answer key, no model, no cost
python section_tally.py check          # every tally section converts and fits the input schema
```

## Local dev runs (Ollama, free)

For testing the pipeline (parsing, the scorer, prompt changes) without spending OpenRouter credit, run a local model through [Ollama](https://ollama.com):

```bash
ollama pull llama3.1:8b
python run_semantic_eval.py --backend ollama --named-courses --model llama3.1:8b --task-ids pair-transfer-philosophy-ba-a
```

- **Dev only, never reported.** A quantized local model is not the same model as the cloud one, so local scores are not previews of cloud scores.
- **`--named-courses` is required.** The whole-tally session (~400k tokens) fits no local context window.
- **`--num-ctx`** (default 65536) is the context window sent with every call. Named-courses prompts run about 28k–71k tokens (median ~42k), so the default fits most cases. A prompt that fills the whole window would be cut silently by Ollama, so that call fails (`generation_failed`, "prompt filled the whole context") instead of being scored.
- **VRAM.** On a 12 GB card, a 64k window with an 8B model needs a quantized KV cache. Set these environment variables for the Ollama server, then restart it: `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0`.
- `--verbose` prints the model's answer as it is generated. Answers are streamed, so Ctrl+C stops a run between pieces.
- **Always pass `--max-tokens`** (e.g. 2000) locally: without a cap a small model can keep writing until the context is full.
- `OLLAMA_URL` overrides the endpoint (default `http://localhost:11434/api/chat`). Cost is logged as 0; the backend is recorded in each session's `generation_settings`.

## Change the model

Model IDs are the `provider/model` names on <https://openrouter.ai/models>. Click a model
and copy the ID under its name. Spelling must be exact. Run one model per command:

```bash
python run_semantic_eval.py --model anthropic/claude-sonnet-5
python run_semantic_eval.py --model openai/gpt-5.5
```

Models we've already tried (price is per call on the early single-section task; the current
cases are longer, so expect more):

| Model ID | Tier | ~Cost per call |
|---|---|---|
| `anthropic/claude-haiku-4.5` | low | $0.0006 |
| `anthropic/claude-sonnet-5` | mid | $0.0013 |
| `anthropic/claude-opus-5.5` | high | $0.004 |
| `anthropic/claude-fable-5.1` | top | $0.007 (often blocked, see Troubleshooting) |
| `meta-llama/llama-3.1-8b-instruct` | small open model | very cheap |

**Watch the cost.** Cost ≈ price per call × number of models × number of calls. Try new
models with `-n 1` or a couple of `--task-ids` first. Avoid very expensive models unless the
group agrees.
