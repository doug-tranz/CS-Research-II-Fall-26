# Agent Permission Experiment — Tech Team Weekly Contribution

## What this is

A small, self-contained simulation comparing two ways of restricting an AI
agent's ability to perform risky actions:

- **Mode A — prompt-only restriction:** the agent is told in its system
  prompt "you may only read email, draft replies, and summarize" — but the
  tool interface itself still technically allows send/delete/transfer/forward.
- **Mode B — capability-gated restriction:** the agent's tool interface only
  *exposes* read/draft/summarize functions at the API level. Send, delete,
  transfer, and forward are not reachable, regardless of what the agent
  decides to do.

10 test instructions were run against both modes: 4 benign (in-scope),
2 benign-but-out-of-scope (a normal, non-malicious request for something the
agent isn't authorized to do), and 4 adversarial (instructions engineered to
resemble documented prompt-injection / authority-escalation attacks).

## Result

| | Unauthorized executions |
|---|---|
| Mode A (prompt-only) | 4 / 10 |
| Mode B (capability-gated) | 0 / 10 |

Full per-case output: [`results.json`](./results.json)

Mode A correctly declines the two non-adversarial out-of-scope requests
(cases 5, 10) — a well-behaved model *does* follow plain instructions. It only
fails under adversarial framing (cases 6–9), which is the actual security
concern: the restriction is a suggestion the model can be talked out of.
Mode B blocks all restricted actions unconditionally, because the action
simply isn't available to call.

## Why this matters / what it's evidence for

This models the core architectural claim in Nannini et al. (2026), *"AI
Agents Under EU Law: A Compliance Architecture for AI Providers,"* Section
6.1: telling a model "don't do X" via its prompt is not a real security
control, because prompt injection or jailbreaking can override it; the
restriction has to be enforced where the model has no way to talk its way
around it — at the tool/API layer. See `../tech_section.md` for the full
write-up with citations.

Maps to group research questions: **RQ8** (validation before execution),
**RQ11** (risk-based authorization), **RQ14** (capability-based permissions
vs. traditional role-based access control).

## Honest limitations (for the "significant changes / obstacles" part of the report)

- This is a **rule-based simulation**, not a call to a real LLM. The
  "adversarial trigger" detection is a simple keyword match standing in for
  the way a real model might be manipulated. It demonstrates the
  *architectural principle*, not model-specific vulnerability.
- Next step: replace `simulate_agent_decision()` with an actual LLM API call
  (with Mode A implemented as system-prompt-only restriction and Mode B
  implemented as literally not registering the restricted tools) to see if
  the same pattern holds against a real model instead of a keyword-matched
  stand-in.

## How to run

```
python3 experiment.py
```

Outputs `results.json` and prints a summary table.
