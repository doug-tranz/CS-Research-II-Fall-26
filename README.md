# CSR2: Evaluating LLM-Generated Transactions

How well do deployed LLMs generate correct transactions? A model gets a student's transcript,
degree requirements, the term's sections and the student's request, and must write a
DIDComm registration message. The harness scores it on four tiers: is it valid DIDComm, is it
our message, is it consistent with the schedule and transcript, and does it do what the
student asked.

## Quick start

```bash
python run_semantic_eval.py --check          # every answer key, no model, no cost (~3 min)
python run_semantic_eval.py --model anthropic/claude-sonnet-5.5 --task-ids pair-transfer-philosophy-ba-a pair-transfer-philosophy-ba-b
python run_semantic_eval.py --pair-report    # paired accuracy from the results file
python -m unittest discover tests            # fast tests (seconds)
```

**A paid run asks first.** Before any OpenRouter call it prints the number of calls and an
estimated cost (rough, leans high) and asks `Continue? [y/N]`. `--yes` skips the question for
unattended runs. Free runs (`--check`, `--rescore`, `--backend ollama`) never ask.

**Local runs are free** and for testing the pipeline only (never reported): see
[Running](docs/running.md#local-dev-runs-ollama-free).

## Setup

OpenRouter lets the harness call many LLMs (Claude, GPT, Llama, …) with one API key.

### 1. Get the code

```bash
git clone https://github.com/doug-tranz/CS-Research-II-Fall-26.git
cd CS-Research-II-Fall-26
```

Already cloned? Just `git checkout main` and `git pull`. (Older instructions said `ben-test-branch`; that branch is far behind `main` and does not have the current pipeline.)

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

## How the code is laid out

One job per file. The model never sees a case's `ground_truth`: only `render.py` builds what
is sent, and only `scoring.py` reads the answer key.

| File | Job |
|---|---|
| `run_semantic_eval.py` | The command line: `--check`, runs, `--rescore`, `--pair-report`, `--compare` |
| `cases.py` | Load a case file; fill in each case's transcript and sections; the answer key's rules |
| `render.py` | What the model sees: the case as JSON, CSV or plain text; the Section Tally opening |
| `session.py` | The default run: the whole Section Tally sent (and cached) with every case |
| `harness.py` | Model backends (OpenRouter, local Ollama), `generate()`; knows nothing about registration |
| `scoring.py` | All four tiers for one answer: `evaluate_case()`, Tier 4 `check_rules()`, error types |
| `checker.py` | Tiers 1-3 against an in-memory SQLite copy of the situation (`seed_db()`, `evaluate()`) |
| `didcomm_schema.py` | Tiers 1-2: the message JSON Schema (`registration_message.schema.json`) |
| `input_format.py` | Validate an input object (`registration_input.schema.json`) |
| `audit.py` | The degree audit: requirements met, `counts_toward()`, missing prerequisites |
| `pairs.py` | Counterfactual pairs: soundness checks and paired accuracy |
| `results.py` | The results file: rows, summaries, safe writes, one run per file at a time |
| `stats.py` | Wilson intervals, McNemar's test |
| `costs.py` | The cost estimate and confirmation before a paid run |
| `fabricate.py` | Fabricates a case's transcript from its degree program |
| `generate_pairs.py` | Writes `paired_cases.json` (200 pairs, 400 cases) |
| `section_tally.py` | Reads Rowan's Section Tally export (`section_tally_fall2026.csv`) |
| `registration.py` | Compatibility only: re-exports the old names for older scripts |

Data: `paired_cases.json` (the test cases), `degree_requirements.json` (50 programs),
`section_tally_fall2026.csv` (Fall 2026 sections). Past runs: `results/`. Unused files kept
for reference: `archive/`.

## Docs

| | |
|---|---|
| [Running the pipeline](docs/running.md) | Sessions and tally versions, named courses, local runs, choosing models |
| [The results file](docs/results-format.md) | Every field of a results file and its summaries |
| [The cart message](docs/message-format.md) | The DIDComm message a model must write, and Tiers 1-2 |
| [The input format](docs/input-format.md) | What a case gives the model; the Section Tally; degree programs |
| [Test cases and scoring](docs/test-cases.md) | Counterfactual pairs, the correctness model, Tiers 3-4 |
| [Results so far](docs/findings.md) | Findings, open questions, what is provisional |
| [Design](docs/design.md) | Research questions, experimental design, changelog |

## Troubleshooting

| You see | Fix |
|---|---|
| `OpenRouter needs an API key` | Key not set, or you didn't restart VS Code/terminals after setting it (Setup step 3) |
| `OpenRouter 401` | Key is wrong or was deleted. Ask Khalil for a new one |
| `OpenRouter 402` | Out of credits, or your key hit its spending limit. Tell Khalil |
| `OpenRouter 400 ... not a valid model ID` | Typo in the model ID. Copy it from openrouter.ai/models |
| `ModuleNotFoundError: No module named 'requests'` or `'jsonschema'` | venv isn't active, or `pip install -r requirements.txt` hasn't been run since `jsonschema` was added (Setup step 2) |
| Blank `raw:` with `syntactic_error` | The model refused the request (seen with Fable 5.1). Not a formatting problem; we don't log refusals separately yet |
| `... is in use by another run` | Another run is writing that results file. Use a different `--output`, or, if a run was closed mid-way, delete the `.lock` file the message names |
| `No terminal to confirm at` | A paid run needs a yes. Add `--yes` for unattended runs |
| `prompt filled the whole context` (local runs) | The case is bigger than `--num-ctx`. Raise it or pick a smaller case |
