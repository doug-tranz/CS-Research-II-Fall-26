# CSR2 — Evaluating LLM-Generated Transactions

Measuring how well currently-deployed LLMs generate correct transactions,
expressed as DIDComm-format JSON messages. See `transaction_llm_eval_design.md`
for the full design; this README is just how to run the code.

## What's here

| File | Role |
|---|---|
| `harness.py` | Core, domain-agnostic: backends (Ollama local, OpenRouter cloud), `generate()`, DIDComm envelope parsing, logging. |
| `registration.py` | Registration domain testbed: SQLite schema, consistency check, two-dimension `evaluate()`. |
| `run_registration.py` | End-to-end runner: builds a task, generates, scores, logs. |
| `probe.py` | Historical raw-Ollama diagnostic (kept for reference). |
| `runs.jsonl` | Generated log — **not** source; regenerated on every run. |

## Setup (Windows + native Ollama)

```powershell
python -m venv venv
venv\Scripts\activate
pip install requests
ollama pull llama3.1:8b
```

## Run

```powershell
python run_registration.py
```

Generates N samples of one registration task, scores each on consistency,
and appends rows to `runs.jsonl`.

### OpenRouter (cloud, many models)

Get a key at <https://openrouter.ai/keys>, then:

```powershell
$env:OPENROUTER_API_KEY = "sk-or-..."
python run_registration.py --backend openrouter --models meta-llama/llama-3.1-8b-instruct openai/gpt-4o-mini -n 5
```

Model IDs are the `provider/model` slugs listed at <https://openrouter.ai/models>.
Each row logs `usd_cost` as reported by OpenRouter. Include
`meta-llama/llama-3.1-8b-instruct` to compare against local `llama3.1:8b` (design §4.1).

## Correctness model (two dimensions)

- **Consistency** — invariants hold (seat available, no time conflict). *Implemented.*
- **Semantic correctness** — the transaction achieves the user's intent. *Stubbed —
  blocked on the group's decision (design §13.1). Never faked.*

Outcome buckets: `syntactic_error` → `consistency_error` → `consistent__semantic_TBD`
(→ future: `semantic_error` / `success`).

## Phase

- **Phase 1 (now):** Ollama, local, free — all development + the local experiment.
- **Phase 2 (later):** OpenRouter, cloud, paid — closed models + backend-invariance
  check. Backend is a one-class swap; `OpenRouterBackend` is implemented (`--backend openrouter`).

## Provisional (will change; isolated to one spot each)

- DIDComm envelope shape → `harness.parse_didcomm()`
- Time-conflict logic (exact-slot match, not interval overlap) → `registration.check_consistency()`
- In-memory SQLite → `registration.make_db()`
