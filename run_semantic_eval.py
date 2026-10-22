"""
run_semantic_eval.py
---------------------
The command line. Runs test cases through a model and scores each answer on all four tiers;
the work itself lives in small modules:

  cases.py       load and fill in test cases          render.py   what the model sees
  session.py     the whole-tally session              scoring.py  Tiers 1-4 for one answer
  pairs.py       counterfactual pairs                 results.py  the results file
  stats.py       intervals and tests                  harness.py  model backends

HOW TO RUN:
    python run_semantic_eval.py --check      # scores every answer key; no model, no cost
    python run_semantic_eval.py --model anthropic/claude-opus-5.5 --task-ids pair-transfer-philosophy-ba-a
    python run_semantic_eval.py --backend ollama --named-courses --model llama3.1:8b --task-ids ... --max-tokens 2000

A paid run (OpenRouter) shows its size and a cost estimate and asks before it starts; --yes skips
the question. Appends results to semantic_eval_results.json (or --output).
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from cases import PAIR_VARIANTS, is_whole, load_test_cases, validate_case
import costs
from harness import OllamaBackend, OpenRouterBackend, build_messages, generate
from pairs import check_pairs, pair_summary, pairs_of, relate_pairs
from render import RENDERINGS, TALLY_VARIANTS, to_task
from results import (answer_text, case_hashes, code_version, file_hash, load_results, log_readable,
                     output_lock, rebuild_summaries, save_results)
from scoring import _with_tiers, call_error_type, evaluate_case, self_check
from session import TallySession
from stats import mcnemar
from harness import context_label as shared_context_info

# Kept importable from here for older scripts: `import run_semantic_eval as rse; rse.load_test_cases(...)`.
from cases import expected_message, resolve_case          # noqa: F401,E402
from scoring import check_rules                           # noqa: F401,E402


def check_cases(cases: list) -> int:
    """Scores every case's own answer key. No model call. Returns the failure count."""
    failed = 0
    for case in cases:
        problems = self_check(case)
        failed += bool(problems)
        print(f"{case.get('case_id', '<no id>')}: {'FAIL' if problems else 'ok'}")
        for p in problems:
            print(f"    - {p}")
    print(f"\n{len(cases) - failed}/{len(cases)} cases sound.")
    pairs = pairs_of(cases)
    if pairs:
        problems = check_pairs(cases)
        for p in problems:
            print(f"    - {p}")
        print(f"{len(pairs) - len({p.split(':')[0] for p in problems})}/{len(pairs)} pairs sound.")
        failed += len(problems)
    return failed


def main():
    """Command line. --check scores every answer key and exits; otherwise every case is validated
    first (nothing is spent on a broken case), then run in a session or, with --named-courses,
    without one."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="required unless --check")
    parser.add_argument("--test-file", default="paired_cases.json")
    parser.add_argument("--output", default="semantic_eval_results.json")
    parser.add_argument("--task-ids", nargs="+", help="run only these cases, e.g. case_012 case_013")
    parser.add_argument("-n", type=int, default=1, help="samples per case")
    parser.add_argument("--rendering", choices=RENDERINGS, default="json",
                        help="how a case's state is shown to the model")
    parser.add_argument("--check", action="store_true",
                        help="score each case's own answer key and exit; no model call")
    parser.add_argument("--named-courses", action="store_true",
                        help="no session: each case's prompt lists only the sections of the courses "
                             "it names (the earlier pipeline)")
    parser.add_argument("--tally", choices=list(TALLY_VARIANTS), default="trimmed",
                        help="the session's opening: 1 raw (the CSV as downloaded), 2 trimmed (converted, "
                             "unused columns dropped), 3 raw-open (raw, full and cancelled sections "
                             "dropped), 4 trimmed-open (both)")
    parser.add_argument("--cache-ttl", choices=["5m", "1h"], default="5m",
                        help="how long the session's cached opening lives between calls")
    parser.add_argument("--pair-report", action="store_true",
                        help="print paired accuracy per model from --output against --test-file's pairs and exit")
    parser.add_argument("--rescore", action="store_true",
                        help="re-grade the saved answers in --output with the current scorer and rewrite their "
                             "verdicts; no model call")
    parser.add_argument("--max-tokens", type=int, help="cap on output tokens per call (default: the provider's)")
    parser.add_argument("--effort", choices=["minimal", "low", "medium", "high", "xhigh"],
                        help="reasoning effort sent to the model (default: the model's own; recorded either way)")
    parser.add_argument("--backend", choices=["openrouter", "ollama"], default="openrouter",
                        help="openrouter (default; for results) or ollama (a local model, free; for testing the "
                             "pipeline only, needs --named-courses)")
    parser.add_argument("--num-ctx", type=int, default=65536,
                        help="ollama only: context window in tokens (a longer prompt fails instead of being cut)")
    parser.add_argument("--verbose", action="store_true",
                        help="ollama only: print the model's answer as it is generated")
    parser.add_argument("--yes", action="store_true",
                        help="skip the cost confirmation before a paid run (for unattended runs)")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"),
                        help="compare two results files on the cases and runs both have (McNemar's test); "
                             "no model call")
    args = parser.parse_args()

    if args.pair_report:
        pair_report(args.test_file, args.output)
        return
    if args.rescore:
        with output_lock(args.output):
            rescore(args.test_file, args.output)
        return
    if args.compare:
        compare(args.test_file, *args.compare)
        return
    cases = load_test_cases(args.test_file, args.task_ids, whole=not args.named_courses)
    if args.check:
        sys.exit(1 if check_cases(cases) else 0)
    if not args.model:
        parser.error("--model is required unless --check is given")

    if args.backend == "ollama":
        if not args.named_courses:
            parser.error("--backend ollama needs --named-courses: the whole-tally session (~400k tokens) "
                         "does not fit a local model's context")
        if args.effort:
            parser.error("--effort is an OpenRouter setting; leave it out with --backend ollama")
        backend = OllamaBackend(num_ctx=args.num_ctx, max_tokens=args.max_tokens, verbose=args.verbose)
    else:
        backend = OpenRouterBackend(max_tokens=args.max_tokens, effort=args.effort)
    for case in cases:                       # every case is sound before anything is spent
        problems = validate_case(case)
        if problems:
            raise ValueError(f"{case.get('case_id')} is not a valid test case: {problems}")
    session = TallySession(args.cache_ttl, args.tally).prepare() if any(is_whole(c) for c in cases) else None
    if args.backend == "openrouter" and not confirm_cost(cases, args, session):
        print("Nothing was run.")
        return
    with output_lock(args.output):
        if session:
            with session:
                run_cases(cases, backend, args, session)
        else:
            run_cases(cases, backend, args)


def confirm_cost(cases: list, args, session) -> bool:
    """Sizes a paid run (every call's prompt, as it will be sent), prices it from OpenRouter's
    list, and asks before spending (costs.confirm). True to go ahead."""
    calls = []
    for case in cases:
        task = to_task(case, args.rendering, session)
        shared = costs.tokens(task.shared_context) if task.shared_context else 0
        own = sum(costs.tokens(m["content"] if isinstance(m["content"], str) else "".join(
            part.get("text", "") for part in m["content"] if part.get("text") is not task.shared_context))
            for m in build_messages(task))
        calls += [(shared, own)] * args.n
    output = args.max_tokens or costs.ASSUMED_OUTPUT
    note = f"--max-tokens {args.max_tokens} per call" if args.max_tokens else \
        f"no --max-tokens set, assumed {costs.ASSUMED_OUTPUT:,} per call"
    summary = costs.estimate(calls, costs.fetch_pricing(args.model), output, args.cache_ttl)
    return costs.confirm(args.model, summary, args.yes, note)


def pair_report(case_file: str, results_file: str) -> None:
    """Paired accuracy for every session in a results file, from its rows' task ids and verdicts."""
    pairs = pairs_of(json.loads(Path(case_file).read_text(encoding="utf-8")))
    sessions, results = load_results(results_file)
    for s in sessions:
        outcomes = {(r["task_id"], r["run_index"]): r["verdict"]["outcome"]
                    for r in results if r.get("session_id") == s["session_id"]}
        lines = pair_summary(pairs, outcomes)
        print(f"{s['session_id']} ({s.get('model')}, tally {s.get('tally_variant') or 'none'}):\n  "
              + ("\n  ".join(lines) if lines else "no pairs in this session"))


def compare(case_file: str, file_a: str, file_b: str) -> None:
    """McNemar's test between two results files, on the (case, run) items both have with no
    failed call: per case, and per pair when the cases are pairs. Prints the counts and p."""
    def outcomes(path):
        sessions, results = load_results(path)
        label = ", ".join(f"{s.get('model')} / {s.get('tally_variant') or 'named'}" for s in sessions)
        return label, {(r["task_id"], r["run_index"]): r["verdict"]["outcome"] for r in results}
    (la, a), (lb, b) = outcomes(file_a), outcomes(file_b)
    keys = sorted(k for k in set(a) & set(b) if "generation_failed" not in (a[k], b[k]))
    ok = lambda o: o == "semantic_ok"
    both, only_a = sum(ok(a[k]) and ok(b[k]) for k in keys), sum(ok(a[k]) and not ok(b[k]) for k in keys)
    only_b, neither = sum(ok(b[k]) and not ok(a[k]) for k in keys), sum(not ok(a[k]) and not ok(b[k]) for k in keys)
    print(f"A = {file_a} ({la})\nB = {file_b} ({lb})\n{len(keys)} shared case runs")
    print(f"  both right {both}, only A {only_a}, only B {only_b}, neither {neither}")
    print(f"  per case: A {both + only_a}/{len(keys)}, B {both + only_b}/{len(keys)}, "
          f"McNemar p = {mcnemar(only_a, only_b):.3f}")
    pairs = pairs_of(json.loads(Path(case_file).read_text(encoding="utf-8")))
    runs = sorted({run for _, run in keys})
    pa, pb = [], []
    for variants in pairs.values():
        for run in runs:
            ks = [(variants.get(v), run) for v in PAIR_VARIANTS]
            if all(k in keys for k in ks):
                pa.append(all(ok(a[k]) for k in ks))
                pb.append(all(ok(b[k]) for k in ks))
    if pa:
        oa, ob = sum(x and not y for x, y in zip(pa, pb)), sum(y and not x for x, y in zip(pa, pb))
        print(f"  per pair: A {sum(pa)}/{len(pa)}, B {sum(pb)}/{len(pb)}, only A {oa}, only B {ob}, "
              f"McNemar p = {mcnemar(oa, ob):.3f}")
    print("  p < 0.05 suggests a real difference; with few disagreements no test can show one.")


def rescore(case_file: str, results_file: str) -> None:
    """Re-grades every saved answer in a results file with the current scorer (all four tiers),
    rewrites its verdict and pair_result, and rebuilds the session summaries. A row whose call
    failed keeps its verdict. Each case is graded against the schedule its session used: the
    whole tally when the session had one, else the named courses. Prints any outcome that
    changed. An older file (a bare list of rows) is rewritten in the current layout."""
    sessions, results = load_results(results_file)
    variant = {s["session_id"]: s.get("tally_variant") for s in sessions}
    ids = {row["task_id"] for row in results}
    whole = load_test_cases(case_file, ids, whole=True) if any(variant.get(r["session_id"]) for r in results) else []
    named = load_test_cases(case_file, ids) if any(not variant.get(r["session_id"]) for r in results) else []
    by_id = {"whole": {c["case_id"]: c for c in whole}, "named": {c["case_id"]: c for c in named}}
    changed = graded = 0
    for row in results:
        if row.get("error") is not None:
            row["verdict"].setdefault("error_type", "call_failed")
            continue
        case = by_id["whole" if variant.get(row["session_id"]) else "named"][row["task_id"]]
        verdict = evaluate_case(case, answer_text(row))
        tag = call_error_type(verdict, row.get("finish_reason"), row.get("native_finish_reason"))
        if tag:
            verdict = _with_tiers({**verdict, "error_type": tag})
        if verdict["outcome"] != row["verdict"]["outcome"]:
            changed += 1
            print(f"{row['task_id']} run {row['run_index']}: {row['verdict']['outcome']} -> {verdict['outcome']}")
        row["verdict"] = verdict
        graded += 1
    related = relate_pairs(case_file, results)
    save_results(results_file, rebuild_summaries(case_file, sessions, results), results)
    print(f"rescored {graded} rows in {results_file}; {changed} outcomes changed"
          + (f"; pair_result on {related} rows" if related else ""))


def run_cases(cases: list, backend, args, session: TallySession | None = None) -> None:
    """Runs every case n times: generate, score through all four tiers, print one line, and
    append the row to the results file. At the end, sets each pair row's pair_result and writes
    the session's summary (settings and totals) into the file, then prints the totals and
    paired accuracy."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    first = to_task(cases[0], args.rendering, session) if cases else None
    raw_cases = {c["case_id"]: c for c in json.loads(Path(args.test_file).read_text(encoding="utf-8"))}
    meta = {"session_id": session.id if session else f"named-{stamp}", "model": args.model,
            "test_file": args.test_file, "test_file_sha256": file_hash(args.test_file),
            "code_version": code_version(), "tally_variant": session.variant if session else None,
            "rendering": args.rendering, "samples_per_case": args.n,
            "cache_ttl": session.ttl if session else None,
            "generation_settings": backend.settings,
            "shared_context": shared_context_info(first) if first and first.shared_context else None,
            "output_contract": first.output_contract if first else None}
    if args.n < 3:
        print(f"note: {args.n} sample(s) per case; use -n 3 or more to compare sessions reliably")
    passed, runs, cost, outcomes = 0, 0, 0.0, {}
    for case in cases:
        task = to_task(case, args.rendering, session)
        hashes = case_hashes(raw_cases.get(case["case_id"], case))

        for run_index in range(args.n):
            if getattr(backend, "verbose", False):
                print(f"--- {task.task_id} run {run_index}: the model's answer as it streams ---")
            result = generate(backend, args.model, task, run_index=run_index)
            if result.ok:
                verdict = evaluate_case(case, result.raw_output)
                tag = call_error_type(verdict, result.finish_reason, result.native_finish_reason)
                if tag:
                    verdict = _with_tiers({**verdict, "error_type": tag})
            else:
                verdict = {"outcome": "generation_failed", "detail": result.error, "error_type": "call_failed"}

            if session:
                session.record(result)
            passed += verdict["outcome"] == "semantic_ok"
            outcomes[(task.task_id, run_index)] = verdict["outcome"]
            runs += 1
            cost += result.usd_cost
            label = task.task_id if args.n == 1 else f"{task.task_id} run {run_index}"
            cache = (f" [cache: {result.cached_tokens:,} read, {result.cache_write_tokens:,} "
                     f"written, ${result.usd_cost:.4f}]") if is_whole(case) else ""
            print(f"{label}: {verdict['outcome']:20s} | {verdict.get('detail', '')}{cache}")
            log_readable(result, verdict, args.output, meta, {"case_hashes": hashes})

    sessions, results = load_results(args.output)
    if pairs_of(cases):
        relate_pairs(args.test_file, results)     # each pair row's pair_result
    save_results(args.output, rebuild_summaries(args.test_file, sessions, results), results)
    print(f"\n{passed}/{runs} semantic_ok on {args.model}. Cost ${cost:.4f}. "
          f"Full results in {args.output} (session {meta['session_id']})")
    for line in pair_summary(pairs_of(cases), outcomes):
        print(line)


if __name__ == "__main__":
    main()
