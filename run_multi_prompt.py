"""
run_multi_prompt.py
-------------------
Shared-context version of run_semantic_eval.py: many cases go to the model in ONE
conversation instead of one fresh context per case.

Everything else is reused from run_semantic_eval.py, so results are directly
comparable with semantic_eval_results.json:
  - load_test_cases()  (reads input_format_cases.json, fills in schedule_from_tally)
  - to_task()          (the exact prompt for a case; --rendering json|csv|nl)
  - evaluate_case()    (all four tiers, each reply scored against ITS OWN case)
  - log_readable()     (same row layout as semantic_eval_results.json)

The only change: one `messages` list is kept alive across cases, so case N is sent
with cases 1..N-1 and the model's earlier replies still in view.

A case prompt is ~25-30k tokens (every open section of the named courses), so a
context cannot hold all 16 cases. The cases are split into consecutive contexts:
a new context starts when --batch-size cases have been sent, or when the estimated
size would pass --max-context-tokens.

HOW TO RUN:
    python run_multi_prompt.py --model anthropic/claude-opus-5.5
    python run_multi_prompt.py --model anthropic/claude-opus-5.5 --batch-size 4 --shuffle 1
    python run_multi_prompt.py --model anthropic/claude-opus-5.5 --rendering csv
    python run_multi_prompt.py --model anthropic/claude-opus-5.5 --task-ids case_001 case_002 case_003
"""

import argparse
import random
from datetime import datetime, timezone

from harness import (OllamaBackend, OpenRouterBackend, GenerationResult,
                     SYSTEM_PROMPT, build_messages)
import run_semantic_eval as rse

CHARS_PER_TOKEN = 3   # deliberately conservative; the log records the real prompt_tokens


def partition(prompts, batch_size, max_tokens):
    """Consecutive groups of case indexes: each group is one shared context."""
    groups, current, size = [], [], len(SYSTEM_PROMPT) // CHARS_PER_TOKEN
    for i, user in enumerate(prompts):
        need = len(user["content"]) // CHARS_PER_TOKEN
        if current and (len(current) >= batch_size or size + need > max_tokens):
            groups.append(current)
            current, size = [], len(SYSTEM_PROMPT) // CHARS_PER_TOKEN
        current.append(i)
        size += need
    if current:
        groups.append(current)
    return groups


def run_context(backend, model, cases, prompts, indexes, context_id, run_index, output):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]   # the shared context
    order = [cases[i]["case_id"] for i in indexes]
    passed = 0

    for turn, i in enumerate(indexes, start=1):
        case, user = cases[i], prompts[i]
        messages.append(user)
        ts = datetime.now(timezone.utc).isoformat()
        context = {"mode": "shared", "context_id": context_id, "turn": turn,
                   "cases_in_context": order}

        try:
            rc = backend.call(model, messages)
        except Exception as e:
            messages.pop()                       # drop the unanswered user turn
            result = GenerationResult(case["case_id"], backend.name, model, user["content"],
                                      "", 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0,
                                      run_index, False, str(e), ts)
            verdict = {"outcome": "generation_failed", "detail": str(e), "context": context}
            rse.log_readable(result, verdict, output)
            print(f"ctx {context_id} turn {turn} {case['case_id']}: generation_failed | {e}")
            continue

        messages.append({"role": "assistant", "content": rc.content})   # kept, valid or not

        result = GenerationResult(
            task_id=case["case_id"], backend=backend.name, model=model,
            prompt_sent=user["content"], raw_output=rc.content,
            prompt_tokens=rc.prompt_tokens, completion_tokens=rc.completion_tokens,
            latency_ms=rc.latency_ms, gen_ms=rc.gen_ms, load_ms=rc.load_ms,
            total_ms=rc.total_ms, usd_cost=rc.usd_cost,
            run_index=run_index, ok=True, error=None, ts=ts)

        verdict = rse.evaluate_case(case, rc.content)     # this case's own answer key
        verdict["context"] = {**context, "messages_in_context": len(messages)}
        rse.log_readable(result, verdict, output)

        passed += verdict["outcome"] == "semantic_ok"
        print(f"ctx {context_id} turn {turn} {case['case_id']:<12} "
              f"{verdict['outcome']:<20} ctx_tokens={rc.prompt_tokens} | "
              f"{verdict.get('detail', '')[:90]}")
    return passed, len(indexes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["ollama", "openrouter"], default="openrouter")
    ap.add_argument("--model", required=True)
    ap.add_argument("--test-file", default="input_format_cases.json")
    ap.add_argument("--output", default="shared_context_results.json")
    ap.add_argument("--task-ids", nargs="+", help="run only these cases")
    ap.add_argument("--rendering", choices=rse.RENDERINGS, default="json")
    ap.add_argument("--batch-size", type=int, default=1000,
                    help="max cases per shared context (default: limited only by size)")
    ap.add_argument("--max-context-tokens", type=int, default=120000,
                    help="estimated size at which a new context starts")
    ap.add_argument("--shuffle", type=int, default=None, help="seed; omit to keep file order")
    ap.add_argument("--run-index", type=int, default=0)
    args = ap.parse_args()

    cases = rse.load_test_cases(args.test_file, args.task_ids)
    for case in cases:
        problems = rse.validate_case(case)
        if problems:
            raise ValueError(f"{case.get('case_id')} is not a valid test case: {problems}")
        if rse.is_search(case):
            raise ValueError(f"{case['case_id']} uses search mode, which needs tools; "
                             f"shared-context runs support listed-schedule cases only")
    if args.shuffle is not None:
        random.Random(args.shuffle).shuffle(cases)

    prompts = [build_messages(rse.to_task(case, args.rendering))[-1] for case in cases]
    groups = partition(prompts, args.batch_size, args.max_context_tokens)
    print(f"{len(cases)} cases -> {len(groups)} shared context(s): "
          f"{[len(g) for g in groups]} cases each")

    backend = OpenRouterBackend() if args.backend == "openrouter" else OllamaBackend()
    passed = total = 0
    for context_id, indexes in enumerate(groups, start=1):
        p, t = run_context(backend, args.model, cases, prompts, indexes,
                           context_id, args.run_index, args.output)
        passed, total = passed + p, total + t
    print(f"\n{passed}/{total} semantic_ok on {args.model}. Full results in {args.output}")


if __name__ == "__main__":
    main()