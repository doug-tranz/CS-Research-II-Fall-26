# Running the tests on OpenRouter

OpenRouter lets the harness call many LLMs (Claude, GPT, Llama, …) with one API key.
You do **not** need Ollama installed to run on OpenRouter.

## 1. Get the code

```bash
git clone https://github.com/doug-tranz/CS-Research-II-Fall-26.git
cd CS-Research-II-Fall-26
git checkout ben-test-branch
```

Already cloned? Just `git checkout ben-test-branch` and `git pull`.

## 2. Set up Python (once)

**Windows (PowerShell):**
```powershell
python -m venv venv
venv\Scripts\activate
pip install requests
```

**Mac / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install requests
```

You'll see `(venv)` at the start of your prompt when it's active. Activate it again
(the second line) every time you open a new terminal.

## 3. Install your OpenRouter key (once)

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

### Keep the key secret
The key spends real money.
- Never put it in code, commit it, or paste it in a group chat.
- Crop or blur it out of screenshots.
- If it leaks, tell Khalil right away so it can be deleted.

## 4. Run the tests

With `(venv)` active, from the repo folder:

```bash
python run_registration.py --backend openrouter --models anthropic/claude-haiku-4.5 -n 5
```

| Option | Meaning | Default |
|---|---|---|
| `--backend` | `openrouter` (cloud) or `ollama` (local) | `ollama` |
| `--models` | One or more model IDs, separated by spaces | `llama3.1:8b` |
| `-n` | How many times to run each model (samples) | `5` |

Running with no options uses local Ollama, which only works if you have Ollama installed.

## 5. Change the model

Pass a different ID to `--models`. You can list several models and they run one after another:

```bash
python run_registration.py --backend openrouter --models anthropic/claude-sonnet-5 openai/gpt-4o-mini meta-llama/llama-3.1-8b-instruct -n 5
```

Model IDs are the `provider/model` names on <https://openrouter.ai/models>. Click a model
and copy the ID under its name. Spelling must be exact.

Models we've already tried (price is per call on this task):

| Model ID | Tier | ~Cost per call |
|---|---|---|
| `anthropic/claude-haiku-4.5` | low | $0.0006 |
| `anthropic/claude-sonnet-5` | mid | $0.0013 |
| `anthropic/claude-opus-5.5` | high | $0.004 |
| `anthropic/claude-fable-5.1` | top | $0.007 (often blocked, see below) |
| `meta-llama/llama-3.1-8b-instruct` | same model as our local Ollama run | very cheap |

**Watch the cost.** Cost ≈ price per call × number of models × `-n`. Try new models
with `-n 1` first. Avoid very expensive models unless the group agrees.

## 6. Read the results

Each run prints one line:

```
run 0: consistent__semantic_TBD   | invariants hold; ... | $0.001334
   raw: {"type": "registration/enroll", "id": "...", "body": {"student_id": ...
```

| Outcome | Meaning |
|---|---|
| `syntactic_error` | Not valid JSON, e.g. the model wrapped it in a ```` ```json ```` code block, or returned nothing |
| `schema_error` | Valid JSON, but the body doesn't have `student_id` and `section_id` |
| `consistency_error` | Breaks a rule, e.g. full section, already enrolled, time conflict |
| `consistent__semantic_TBD` | Passes every check we have so far (the semantic check isn't built yet) |
| `GENERATION FAILED` | The call itself failed; the error message says why |

Every run is also added as one line to `runs.jsonl`, with the full prompt, the model's raw
answer, tokens, latency, cost, and the outcome.

## Troubleshooting

| You see | Fix |
|---|---|
| `OpenRouter needs an API key` | Key not set, or you didn't restart VS Code/terminals after setting it (step 3) |
| `OpenRouter 401` | Key is wrong or was deleted. Ask Khalil for a new one |
| `OpenRouter 402` | Out of credits, or your key hit its spending limit. Tell Khalil |
| `OpenRouter 400 ... not a valid model ID` | Typo in `--models`. Copy the ID from openrouter.ai/models |
| `ModuleNotFoundError: No module named 'requests'` | venv isn't active. Run the activate line from step 2 |
| Blank `raw:` with `syntactic_error` | The model refused the request (seen with Fable 5.1). Not a formatting problem; we don't log refusals separately yet |
