# Design

**Project:** How well do currently-deployed LLMs generate correct transactions?
**Course:** CS Research II (Dr. Ivanov)
**Authors:** Ben Dobkin, Claude
**Version:** 1.5
**Status:** Draft for group review — reflects the 9/17 group decisions. (Formerly `transaction_llm_eval_design.md`; merged here unchanged except where the changelog says.) Contains decisions made and open questions to resolve as a group.


## 1. Motivation

The observable phenomenon: LLMs are already being used to generate transactional messages, and this will only increase. What nobody has rigorously measured is *how good they are at it*. That gap is the opening — if we're first into this space, we start from the unanswered question rather than an incremental improvement on prior work. (Related-work task: confirm no prior paper uses LLMs to generate transactional messages; dig into blockchain / crypto-wallet literature specifically.)

The stakes are practical. A transaction that fails loudly is safe — you know it failed. A transaction that is *accepted but wrong* is the one that hurts someone. Measuring where LLMs land on that spectrum is the contribution.

## 2. Research questions

- **RQ1 — Accuracy.** How accurately do LLMs generate correct transactions? (Correctness has two dimensions — §3.)
- **RQ2 — Efficiency and cost.** What does it cost to generate them — compute/latency (local) and dollars per request (cloud)?
- **RQ3 — Tradeoff.** How do different models trade accuracy against cost?

## 3. Scope and definitions

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

## 4. Experimental design

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

## 5. Grounding and state input

A real user doesn't hand-write the system's structure; something supplies it. How much we inject is a modeling decision, and it must be **disclosed up front** — a model looks near-perfect when handed everything and mediocre without, so the grounding condition is load-bearing for interpreting every accuracy number.

New wrinkle from the group's model: **one of the JSON inputs can be a status-read from the transactional system itself** (e.g. "how many seats remain in CS-565"). So grounding isn't just static schema — it can include a live read that becomes part of the transaction's input. Whether grounding/state provision is fixed or itself a variable is open (§13).

## 6. Pipeline

```
JSON input(s)  →  generate(backend, model, task)  →  DIDComm message
   →  system digests message (→ SQL/state)  →  evaluate (consistency + semantic)  →  log
```

Everything is **logged unconditionally**; evaluation reads from the log and never gates what gets written, so results can be re-scored if a definition is refined without re-running generations.

**Test cases (ground truth).** Each case = one or several JSON inputs (what we know, possibly including a system status-read) → a known-correct JSON output. Target ~100 known-correct cases. To avoid hand-authoring everything, **AI generates candidate test sets, then several people verify manually** (cross-verify for hallucinations) before a case is trusted as ground truth.

**State isolation** still applies to whatever executes the message: each case runs from a known pre-state so results are comparable.

## 7. Model-interfacing layer

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

## 8. Evaluation — two dimensions

The evaluator reads the log and scores each generation on **both** axes:

- **Consistency** — algorithmically checkable against the system's invariants (seat availability, schedule conflict, numeric reconciliation). This is the tractable half.
- **Semantic correctness** — does the transaction achieve the stated intent? **This is the hard, open problem.** Options under discussion: compare against the human-verified ground-truth output ("apples-to-apples"), or an algorithmic equivalence/goal-satisfaction check (since many distinct JSON messages can be equally correct, exact-match is too strict). The notes leave this open — it is the deepest unresolved question (§13).

The two-axis result is the point: reporting *consistency rate* and *semantic-correctness rate* separately (and their overlap) is what distinguishes "fails loudly" from "quietly does the wrong thing" per model.

## 9. RQ2 measurement (efficiency + cost)

- **Local (Ollama):** prefill vs. decode is a real physical split. `prompt_eval_*` = ingesting the input, `eval_*` = generating output, `load_*` = model load (recorded **separately**, kept warm, so a one-time load never contaminates per-request latency). Durations arrive in **nanoseconds** — convert once, centrally.
- **Cloud (OpenRouter):** no prefill/decode split; measure **wall-clock latency**, **usage tokens**, and **dollar cost** (tokens × current price — pricing lookup is an assigned task). Report cost per successful transaction.
- Store **primitives, not rates**; compute tokens/sec and $/transaction in analysis.
- **Measured 9/17 (still true):** Ollama durations arrive in nanoseconds (one conversion); a cold-vs-warm run showed a ~100× prefill speedup, which confirms keeping the model warm and timing the load separately.
- **Drift:** `run_index` + `ts` catch cost/latency drift over a run. Locally, watch thermal throttling; on cloud, watch provider-side variance.

## 10. Confounds to control

- **Stochasticity.** Take `n` samples per task at fixed temperature; report a *rate*, not a single pass/fail.
- **Backend fairness (§4.1).** Isolate backend only via a *shared* model; don't read cross-model agreement as backend-invariance.
- **Ground-truth quality.** AI-generated test sets can hallucinate; multi-person manual cross-verification before a case is trusted (§6).
- **Grounding disclosure (§5).** State the grounding/state-provision condition up front.
- **Malformed vs. wrong.** Keep syntactic, consistency, and semantic errors as distinct buckets (§3) so formatting failures don't masquerade as reasoning failures.

## 11. Infrastructure

Two backends, one harness:

- **Ollama (local):** native app, GPU via CUDA; models pulled locally. Fixed box = RQ2 measurement consistency for the *compute* numbers (VRAM sets the local roster). Building/testing needs no dedicated server.
- **OpenRouter (cloud):** one API key, broad model access (open + closed), single output format. Reachable from any OS. Pricing/token-cost lookup is an assigned task and feeds RQ2's dollar figures.
- **Harness:** Python + `requests`; SQLite (stdlib) for the transactional system's storage/durability layer; JSONL logs.

Whichever machine runs the *measured local* runs is the fixed hardware — one person should own those runs to avoid cross-machine contamination. OS is not a hard requirement (native Windows + CUDA is fine); "one consistent box" is.

## 12. Experimental matrix (scale check)

```
backends × models × domains × tasks × samples
   2      ×  ~N    ×   2+    ×  ~100 ×  ~n
```

*(counts illustrative)* Generation is the cheap part; the **two-dimension evaluation harness** — especially the semantic check — is the bottleneck. Every generation must be scored automatically or the scale is unmanageable.

## 13. Open questions — for the group

1. **Semantic verification — THE hard one.** How do we check that a consistent transaction achieves the *intent*? Compare to human-verified ground-truth output, or an algorithmic goal-satisfaction check? Exact JSON match is too strict (many correct encodings). Blocks the semantic half of `evaluate()`.
2. **Ground-truth assertion shape.** Is the stored check a JSON output to diff against, a Python callable, or an invariant+goal spec? Ties to §13.1.
3. **Second domain.** Simplified taxes (W-2 → 1040) or travel/scheduling? (Registration is settled as primary.)
4. **DIDComm envelope.** What exact message schema are we standardizing on for inputs/outputs?
5. **Grounding/state provision.** Fixed (always provided, incl. status-reads) or a variable?
6. **NL input arm.** Do we test natural-language input alongside JSON, or is input JSON-only?
7. **Model set** across both backends — and which model(s) are shared on both for the backend control (§4.1).
8. **`n`** — samples per task.
9. **OpenRouter pricing** — token/request cost figures (assigned task) to finalize RQ2's cost model.

## 14. Non-goals and risks

- **Non-goal: A, I, D.** Only Consistency (plus semantics) is in scope; atomicity/isolation/durability belong to the executing system (§3).
- **Non-goal: banking.** Dropped — too simple to exercise consistency *and* semantics. Registration/taxes/travel are "satisfiability" problems (must satisfy invariants *and* express intent); banking isn't.
- **Non-goal: training our own model.** We measure what people *actually deploy*; a bespoke model answers a weaker question and risks the semester. Cost control is handled by local Ollama; range by OpenRouter.
- **Risk: scope creep.** Two domains and two backends is already a lot. Resist adding a third of either until the two-domain / two-backend pattern is solid. The semester is the binding constraint.

## 15. Changelog

| Version | Date | Changes |
|---|---|---|
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
