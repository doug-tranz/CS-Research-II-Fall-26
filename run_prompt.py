"""
run_prompt.py
------------------------------
Puts the ENTIRE Rowan Fall 2026 Section Tally into one context window, once, and then sends
the registration cases to the model as multiple prompts in that same conversation.

How it differs from run_multi_prompt.py:
  run_multi_prompt.py            each case prompt lists only the sections of the courses it names
                                 (~25-30k tokens per case), so the cases must be split into windows
  run_multi_prompt_full_tally.py the whole tally is placed ONCE in the system message (compact
                                 table, cancelled sections left out); each case prompt then holds
                                 only the student's state and request (a few thousand tokens), so
                                 all the cases fit in one window, IF the model's window can hold
                                 the tally. The model must find the right sections itself.

Reused from run_semantic_eval.py (imported as rse): case loading, the output contract, the four-tier
scoring (each reply is scored against ITS OWN case) and the readable result rows.
The checker's database is built from the whole tally too (search-mode style), so a section is valid
if it exists anywhere in the tally.

FIRST measure the size, no model call, no cost:
    python run_multi_prompt_full_tally.py --dry-run
THEN check the answer keys still behave against the whole tally (no model call):
    python run_multi_prompt_full_tally.py --check
THEN run:
    python run_multi_prompt_full_tally.py --model <a model whose context window is larger than the estimate>
    python run_multi_prompt_full_tally.py --model <model> --task-ids case_001 case_002
    python run_multi_prompt_full_tally.py --model <model> --subjects CS MATH   # smaller tally (not "entire")
"""

import argparse
import json
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from harness import (TaskSpec, OllamaBackend, OpenRouterBackend, GenerationResult,
                     SYSTEM_PROMPT, build_messages)
import run_semantic_eval as rse

CHARS_PER_TOKEN = 3   # conservative estimate; the log records the real prompt_tokens

FULL_TALLY_CONTRACT = (
    " The sections offered this term are NOT listed here. They are the complete Section Tally "
    "given in the system message; only sections listed there exist. Copy each chosen section's "
    "fields (crn, course_id, section, credits, seats, campus, modality, meetings) from its row."
)


# ── The tally as one compact table ──

def render_tally(schedule: dict, subjects=None) -> tuple:
    """The whole tally as a pipe-separated table, one section per row.
    Cancelled sections are left out. Returns (text, number of sections)."""
    header = "crn|course_id|section|title|credit_hours|seats_taken/seats_total|campus|modality|waitlist enrolled/capacity|meetings"
    rows = []
    for s in schedule["sections"]:
        if s.get("cancelled"):
            continue
        if subjects and re.match(r"[A-Z]+", s["course_id"]).group(0) not in subjects:
            continue
        title = s.get("title", "") + (f" ({s['title_note']})" if s.get("title_note") else "")
        waitlist = f"{s['waitlist']['enrolled']}/{s['waitlist']['capacity']}" if s.get("waitlist") else ""
        meetings = "; ".join(rse._meeting_text(m) for m in s["meetings"]) or "none"
        rows.append("|".join([s["crn"], s["course_id"], s["section"], title,
                              rse._credits_text(s["credit_hours"]),
                              f"{s['seats_taken']}/{s['seats_total']}", s["campus"], s["modality"],
                              waitlist, meetings]))
    text = (f"COMPLETE SECTION TALLY, {schedule['term']} (term_code {schedule['term_code']}). "
            f"One section per row; columns: {header}\n" + "\n".join(rows))
    return text, len(rows)


# ── Cases ──

def load_cases(path, task_ids):
    """Cases with the WHOLE tally as the checker's schedule (search-mode resolution)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if task_ids:
        raw = [c for c in raw if c.get("case_id") in task_ids]
    return [rse.resolve_case({**c, "schedule_from_tally": {"search": True}}) for c in raw]


def user_message(case: dict) -> dict:
    """One case's user turn: known state without sections, the request, the contract.
    The answer key is never included."""
    data = case["input"]
    state = {k: v for k, v in data.items() if k != "prompt"}
    state["schedule"] = {"term": data["schedule"]["term"], "term_code": data["schedule"]["term_code"]}
    task = TaskSpec(task_id=case["case_id"], domain="registration",
                    inputs=json.dumps(state), intent=data["prompt"],
                    output_contract=rse.OUTPUT_CONTRACT + rse.CRN_CONTRACT + FULL_TALLY_CONTRACT)
    return build_messages(task)[-1]


def partition(prompts, base_tokens, batch_size, max_tokens):
    """Consecutive groups of case indexes; each group is one conversation holding the tally."""
    if base_tokens >= max_tokens:
        raise SystemExit(f"The tally alone (~{base_tokens:,} tokens est.) is not under "
                         f"--max-context-tokens {max_tokens:,}. Use a model with a larger window, "
                         f"or shrink it with --subjects.")
    groups, current, size = [], [], base_tokens
    for i, user in enumerate(prompts):
        need = len(user["content"]) // CHARS_PER_TOKEN
        if current and (len(current) >= batch_size or size + need > max_tokens):
            groups.append(current)
            current, size = [], base_tokens
        current.append(i)
        size += need
    if current:
        groups.append(current)
    return groups


# ── One conversation ──

def run_context(backend, model, cases, prompts, indexes, system_text, n_sections,
                context_id, run_index, output):
    messages = [{"role": "system", "content": system_text}]    # the tally lives here, once
    order = [cases[i]["case_id"] for i in indexes]
    passed, cost = 0, 0.0

    for turn, i in enumerate(indexes, start=1):
        case, user = cases[i], prompts[i]
        messages.append(user)
        ts = datetime.now(timezone.utc).isoformat()
        context = {"mode": "shared_full_tally", "context_id": context_id, "turn": turn,
                   "cases_in_context": order, "tally_sections_in_context": n_sections}
        try:
            rc = backend.call(model, messages)
        except Exception as e:
            messages.pop()
            result = GenerationResult(case["case_id"], backend.name, model, user["content"],
                                      "", 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0,
                                      run_index, False, str(e), ts)
            rse.log_readable(result, {"outcome": "generation_failed", "detail": str(e),
                                      "context": context}, output)
            print(f"ctx {context_id} turn {turn} {case['case_id']}: generation_failed | {e}")
            continue

        messages.append({"role": "assistant", "content": rc.content})
        result = GenerationResult(
            task_id=case["case_id"], backend=backend.name, model=model,
            prompt_sent=user["content"], raw_output=rc.content,
            prompt_tokens=rc.prompt_tokens, completion_tokens=rc.completion_tokens,
            latency_ms=rc.latency_ms, gen_ms=rc.gen_ms, load_ms=rc.load_ms,
            total_ms=rc.total_ms, usd_cost=rc.usd_cost,
            run_index=run_index, ok=True, error=None, ts=ts)
        verdict = rse.evaluate_case(case, rc.content)           # this case's own answer key
        verdict["context"] = {**context, "messages_in_context": len(messages)}
        rse.log_readable(result, verdict, output)

        passed += verdict["outcome"] == "semantic_ok"
        cost += rc.usd_cost
        print(f"ctx {context_id} turn {turn} {case['case_id']:<12} {verdict['outcome']:<20} "
              f"ctx_tokens={rc.prompt_tokens} | {verdict.get('detail', '')[:90]}")
    return passed, len(indexes), cost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["ollama", "openrouter"], default="openrouter")
    ap.add_argument("--model", help="required unless --check or --dry-run")
    ap.add_argument("--test-file", default="input_format_cases.json")
    ap.add_argument("--output", default="full_tally_results.json")
    ap.add_argument("--task-ids", nargs="+")
    ap.add_argument("--subjects", nargs="+",
                    help="only these subject codes in the tally the model sees, e.g. CS MATH "
                         "(NOT the entire tally; use when the window is too small)")
    ap.add_argument("--batch-size", type=int, default=1000, help="max cases per conversation")
    ap.add_argument("--max-context-tokens", type=int, default=1000000,
                    help="estimated size at which a new conversation starts; set to the model's window")
    ap.add_argument("--shuffle", type=int, default=None, help="seed; omit to keep file order")
    ap.add_argument("--run-index", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true", help="print the tally size and exit; no model call")
    ap.add_argument("--check", action="store_true",
                        help="score each case's answer key against the whole tally; no model call")
    args = ap.parse_args()

    cases = load_cases(args.test_file, args.task_ids)
    if args.check:
        failed = 0
        for case in cases:
            problems = rse.self_check(case)
            failed += bool(problems)
            print(f"{case['case_id']}: {'FAIL' if problems else 'ok'}")
            for p in problems:
                print(f"    - {p}")
        print(f"\n{len(cases) - failed}/{len(cases)} cases sound against the whole tally.")
        sys.exit(1 if failed else 0)

    tally_text, n_sections = render_tally(cases[0]["input"]["schedule"], args.subjects)
    system_text = SYSTEM_PROMPT + "\n\n" + tally_text
    base_tokens = len(system_text) // CHARS_PER_TOKEN
    prompts = [user_message(c) for c in cases]
    case_tokens = sum(len(p["content"]) for p in prompts) // CHARS_PER_TOKEN

    print(f"Tally in the prompt: {n_sections:,} sections, {len(tally_text):,} characters, "
          f"~{base_tokens:,} tokens (estimate).")
    print(f"{len(cases)} case prompts: ~{case_tokens:,} tokens in total (without replies).")
    print(f"A single conversation needs a window of roughly {base_tokens + case_tokens:,}+ tokens "
          f"plus the replies.")
    if args.dry_run:
        return
    if not args.model:
        ap.error("--model is required unless --check or --dry-run is given")

    for case in cases:
        problems = rse.validate_case(case)
        if problems:
            raise ValueError(f"{case.get('case_id')} is not a valid test case: {problems}")
    if args.shuffle is not None:
        random.Random(args.shuffle).shuffle(cases)
        prompts = [user_message(c) for c in cases]

    groups = partition(prompts, base_tokens, args.batch_size, args.max_context_tokens)
    print(f"{len(cases)} cases -> {len(groups)} conversation(s): {[len(g) for g in groups]} cases each")

    backend = OpenRouterBackend() if args.backend == "openrouter" else OllamaBackend()
    passed = total = 0
    cost = 0.0
    for context_id, indexes in enumerate(groups, start=1):
        p, t, c = run_context(backend, args.model, cases, prompts, indexes, system_text,
                              n_sections, context_id, args.run_index, args.output)
        passed, total, cost = passed + p, total + t, cost + c
    print(f"\n{passed}/{total} semantic_ok on {args.model}. Cost ${cost:.4f}. "
          f"Full results in {args.output}")


if __name__ == "__main__":
    main()