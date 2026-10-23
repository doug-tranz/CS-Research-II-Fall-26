"""
cli.py
------
The command line, run as `python run_semantic_eval.py` from the repository root. Reads a file
of test cases, sends each case to a model in one session, and scores the answer through all
four tiers:

  Tiers 1-2  syntactic + schema   didcomm_schema.validate_message()  } checker.evaluate()
  Tier 3     consistency          checker.consistency_checks()       }
  Tier 4     semantic             scoring.check_rules()

The model NEVER sees `ground_truth`: render.to_task() builds the prompt from the case's input
only, and the answer key is read only after generation, in scoring.py.

Where the rest lives (one job per file, all in pipeline/):
    cases.py      load a case file; the answer key format; expected messages
    render.py     what the model sees: the tally opening and each case's state
    session.py    the session: one cached opening, one call per case
    backends.py   OpenRouter (cloud, paid) and LM Studio (local, metered in watts)
    pricing.py    cost estimates, the paid-run go/no-go, projected OpenRouter cost of local runs
    energy.py     the GPU power meter for local runs
    scoring.py    all four tiers for one answer      pairs.py    counterfactual pairs
    results.py    the results file and its summaries  reports.py  --pair-report/--compare/--rescore
    selfcheck.py  --check                             stats.py    Wilson, McNemar
    paths.py      where data/, schemas/ and results/ are
    building the cases: fabricate.py (transcripts), generate_pairs.py (the pairs), section_tally.py

HOW TO RUN:
    python run_semantic_eval.py --model anthropic/claude-sonnet-5.5 --max-cost 5
    python run_semantic_eval.py --model anthropic/claude-sonnet-5.5 --task-ids pair-transfer-philosophy-ba-a pair-transfer-philosophy-ba-b -n 3
    python run_semantic_eval.py --model anthropic/claude-sonnet-5.5 --estimate-only     # price it, spend nothing
    python run_semantic_eval.py --check      # scores every answer key; no model, no cost

    # local, through LM Studio (free to run; electricity measured; OpenRouter cost projected)
    python run_semantic_eval.py --backend lmstudio --model qwen/qwen3-4b --subjects auto \\
        --task-ids pair-modality-marketing-bs-a pair-modality-marketing-bs-b

OpenRouter runs need OPENROUTER_API_KEY; LM Studio runs need its server running.
Appends full results to results/semantic_eval_results.json (or --output): {"sessions": [...],
"results": [...]}, one summary per run and one row per call.
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from .backends import LMStudioBackend, OpenRouterBackend
from .batch import batch_report, batch_totals, pair_groups, report_lines, spread_pairs
from .cases import case_subjects, case_hashes, load_test_cases, read_cases, validate_case
from .energy import PowerMeter
from .harness import PROMPT_LAYOUT, context_label, generate
from .pairs import pair_summary, pairs_of, relate_pairs
from .paths import CASES, DEFAULT_OUTPUT, shown
from .pricing import Projection, confirm_spend, estimate_cost, lookup_price, opening_tokens
from .render import RENDERINGS, TALLY_VARIANTS, render_opening, session_contract, to_task
from .reports import compare, pair_report, rescore
from .results import file_hash, load_results, log_row, rebuild_summaries, save_results
from .scoring import failed_call_verdict, score_answer
from .selfcheck import check_cases
from .session import TallySession

DEFAULT_KWH_PRICE = 0.20   # USD per kWh; set --kwh-price to the rate on your electricity bill


def build_parser() -> argparse.ArgumentParser:
    """Every command-line option, grouped by what it is for."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    run = parser.add_argument_group("what to run")
    run.add_argument("--model", help="required unless --check; an OpenRouter id, or an LM Studio model id")
    run.add_argument("--test-file", default=str(CASES), help="the case file (default data/paired_cases.json)")
    run.add_argument("--output", default=str(DEFAULT_OUTPUT),
                     help="the results file runs are appended to (default results/semantic_eval_results.json)")
    run.add_argument("--task-ids", nargs="+", help="run only these cases (give both halves of a pair)")
    run.add_argument("--pairs", type=int, metavar="N",
                     help="run N pairs (2N cases) spread evenly over the families, instead of --task-ids")
    run.add_argument("-n", type=int, default=1, help="samples per case")
    run.add_argument("--rendering", choices=RENDERINGS, default="json",
                     help="how a case's state is shown to the model")
    run.add_argument("--tally", choices=list(TALLY_VARIANTS), default="trimmed",
                     help="the session's opening: raw (the CSV as downloaded), trimmed (converted, unused "
                          "columns dropped), compact (trimmed without meeting rooms, types and waitlists), "
                          "raw-open / trimmed-open (full and cancelled sections dropped)")
    run.add_argument("--subjects", nargs="+", metavar="SUBJ",
                     help="DEV RUNS ONLY: keep just these subjects' sections in the opening (e.g. CS MATH), "
                          "or 'auto' for the subjects the chosen cases need. Makes the opening fit a local "
                          "model's context and cuts cost; results are not comparable with whole-tally runs")
    run.add_argument("--session-per-pair", action="store_true",
                     help="run each pair as its own session whose opening holds only that pair's subjects "
                          "(--subjects auto per pair): how a local model gets through many cases. Ends with "
                          "the batch's grand totals")
    run.add_argument("--dashboard", action="store_true",
                     help="open a live dashboard of the run in the browser (http://localhost:8765)")
    run.add_argument("--resume", action="store_true",
                     help="with --session-per-pair: continue the last batch in --output, skipping the pairs "
                          "already done (rerun the same command with --resume after a crash or a stop)")

    model = parser.add_argument_group("the model call")
    model.add_argument("--backend", choices=["openrouter", "lmstudio"], default="openrouter",
                       help="openrouter (cloud, paid) or lmstudio (a local model on this machine)")
    model.add_argument("--max-tokens", type=int, default=16000,
                       help="cap on output tokens per call, thinking included (well above the ~500-1,000 a "
                            "call uses; it only stops a runaway answer)")
    model.add_argument("--effort", choices=["minimal", "low", "medium", "high", "xhigh"],
                       help="reasoning effort sent to the model (default: the model's own; recorded either way)")
    model.add_argument("--cache-ttl", choices=["5m", "1h"], default="5m",
                       help="how long the session's cached opening lives between calls (1h for slow runs)")

    money = parser.add_argument_group("cost")
    money.add_argument("--max-cost", type=float,
                       help="USD: refuse to start when the estimated cost is higher, and stop the run once "
                            "the calls made have cost this much")
    money.add_argument("--yes", action="store_true",
                       help="start a paid run without asking (otherwise it asks unless --max-cost is set)")
    money.add_argument("--estimate-only", action="store_true",
                       help="print the run's estimated OpenRouter cost and exit; no model call")

    local = parser.add_argument_group("local runs (--backend lmstudio)")
    local.add_argument("--lmstudio-url", help=f"LM Studio's server (default {LMStudioBackend.DEFAULT_URL}, "
                                              f"or $LMSTUDIO_URL)")
    local.add_argument("--price-as", metavar="OPENROUTER_ID",
                       help="the OpenRouter model whose prices the projected cost uses (default: --model)")
    local.add_argument("--kwh-price", type=float, default=DEFAULT_KWH_PRICE,
                       help=f"electricity price in USD per kWh (default {DEFAULT_KWH_PRICE}; use your bill's rate)")
    local.add_argument("--watts", type=float,
                       help="a fixed power draw instead of measuring the GPU with nvidia-smi")
    local.add_argument("--extra-watts", type=float, default=0.0,
                       help="added to the measured GPU draw for the rest of the machine (CPU, memory, fans)")

    other = parser.add_argument_group("no model call")
    other.add_argument("--check", action="store_true",
                       help="score each case's own answer key and exit; no model call")
    other.add_argument("--pair-report", action="store_true",
                       help="print paired accuracy per session from --output against --test-file's pairs and exit")
    other.add_argument("--batch-report", action="store_true",
                       help="print the grand totals of every --session-per-pair batch in --output and exit")
    other.add_argument("--rescore", action="store_true",
                       help="re-grade the saved answers in --output with the current scorer and rewrite their "
                            "verdicts; no model call")
    other.add_argument("--compare", nargs=2, metavar=("A", "B"),
                       help="compare two results files on the cases and runs both have (McNemar's test); "
                            "no model call")
    return parser


# ── Setting up a run ──

def pick_subjects(args, cases: list, auto: bool = False) -> list | None:
    """--subjects as a sorted list: the named subjects, or with 'auto' (or `auto` set) those the
    given cases can answer from (cases.case_subjects()); None for the whole tally."""
    if not args.subjects and not auto:
        return None
    if not args.subjects or [s.lower() for s in args.subjects] == ["auto"]:
        raw = {c["case_id"]: c for c in read_cases(args.test_file)}
        return sorted(set().union(*(case_subjects(raw[c["case_id"]]) for c in cases)))
    return sorted(s.upper() for s in args.subjects)


def make_backend(args):
    """The backend for --backend. A local backend is checked before anything else happens: the
    server is up, it has the model, and the power meter can read the GPU."""
    if args.backend == "openrouter":
        return OpenRouterBackend(max_tokens=args.max_tokens, effort=args.effort)
    meter = PowerMeter(fixed_watts=args.watts, extra_watts=args.extra_watts)
    backend = LMStudioBackend(meter, args.kwh_price, url=args.lmstudio_url,
                              max_tokens=args.max_tokens, effort=args.effort)
    backend.check_model(args.model)
    return backend


def preview(args, subjects=None) -> SimpleNamespace:
    """A session's opening without starting the session (nothing printed), for estimates."""
    raw, _, _ = TALLY_VARIANTS[args.tally]
    text, _ = render_opening(args.tally, subjects)
    return SimpleNamespace(raw=raw, text=text, ttl=args.cache_ttl, contract=session_contract(args.tally),
                           variant=args.tally, subjects=subjects)


def estimate_for(args, model: str, cases: list, subjects=None) -> dict:
    """pricing.estimate_cost() for these cases in one session whose opening holds `subjects`."""
    opening = preview(args, subjects)
    return estimate_cost(model, opening, [to_task(c, args.rendering, opening) for c in cases], args.n, args.max_tokens)


def whole_tally_estimate(args, model: str, cases: list) -> dict:
    """What these cases would cost on OpenRouter as `model` in ONE session over the whole tally
    (the real experiment), whatever subjects this run's openings hold."""
    return estimate_for(args, model, cases)


def local_preamble(args, backend, session, cases: list, tasks: list, whole: bool = True) -> tuple:
    """For a local run: what it would cost on OpenRouter, as printed lines and summary fields,
    and the Projection that prices each call as it is made. Warns when the opening looks too
    long for the model's loaded context. whole: also estimate the whole-tally session (a batch
    does that once for all its sessions instead)."""
    price_as = args.price_as or args.model
    opening = opening_tokens(session, tasks[0])
    lines = [f"local run on {args.model} via LM Studio: electricity measured "
             f"({'nvidia-smi' if args.watts is None else f'fixed {args.watts:g} W'}"
             f"{f' + {args.extra_watts:g} W' if args.extra_watts else ''}) at ${args.kwh_price:g}/kWh"]
    if backend.context_length and opening > backend.context_length:
        lines.append(f"WARNING: the opening is about {opening:,.0f} tokens but {args.model} is loaded with a "
                     f"{backend.context_length:,}-token context; narrow --subjects or load it with a longer "
                     f"context (lms load {args.model} --context-length N). Calls that fill it are failed.")
    price, why = lookup_price(price_as, opening)
    meta = {"price_as": price_as, "kwh_price_usd": args.kwh_price, "lmstudio_url": backend.url,
            "context_length": backend.context_length,
            "power": {"source": backend.meter.source, "idle_watts": backend.meter.idle_watts,
                      "fixed_watts": args.watts, "extra_watts": args.extra_watts or None}}
    if price is None:
        lines.append(f"no projected OpenRouter cost: {why} (pass --price-as <an OpenRouter model id>)")
        return lines, meta, None
    here = estimate_cost(price_as, session, tasks, args.n, args.max_tokens)
    lines.append(f"on OpenRouter as {price_as}, this session would cost about ${here['usd']:.2f} (estimate; "
                 f"the real projection from the local token counts is printed per call)")
    meta["projected_estimate_usd"] = here["usd"]
    if whole and session.subjects:
        full = whole_tally_estimate(args, price_as, cases)
        if full["usd"] is not None:
            lines.append(f"  with the whole {session.variant} tally instead of {len(session.subjects)} subject(s): "
                         f"about ${full['usd']:.2f}")
            meta["projected_whole_tally_estimate_usd"] = full["usd"]
    return lines, meta, Projection(price_as, price)


# ── Running ──

def session_meta(args, backend, session, first_task, estimate_usd, extra: dict) -> dict:
    """The settings every call of the run shares, written once into the session summary."""
    return {"session_id": session.id, "model": args.model, "backend": backend.name,
            "test_file": shown(args.test_file), "test_file_sha256": file_hash(args.test_file),
            "tally_variant": session.variant, "tally_subjects": session.subjects,
            "rendering": args.rendering, "samples_per_case": args.n, "cache_ttl": session.ttl,
            "generation_settings": backend.settings, "prompt_layout": PROMPT_LAYOUT,
            "shared_context": context_label(first_task), "output_contract": first_task.output_contract,
            "cost_estimate_usd": estimate_usd, "max_cost_usd": args.max_cost, **extra}


def call_note(result, projected: float | None) -> str:
    """The bracketed part of a call's printed line: cache use and cost, or a local call's
    tokens, energy and projected OpenRouter cost."""
    if result.energy_wh is None:
        return (f" [cache: {result.cached_tokens:,} read, {result.cache_write_tokens:,} "
                f"written, ${result.usd_cost:.4f}]")
    projected_text = f"; OpenRouter ~${projected:.4f}" if projected is not None else ""
    return (f" [local: {result.prompt_tokens:,} in, {result.completion_tokens:,} out, "
            f"{result.energy_wh:.4f} Wh at {result.avg_watts:.0f} W, ${result.energy_usd:.6f}{projected_text}]")


def run_cases(cases: list, backend, args, session: TallySession, meta: dict, projection=None,
              spent: float = 0.0) -> dict:
    """Runs every case n times: generate, score through all four tiers, print one line, and
    append the row to the results file; stops early once the calls (with `spent`, what earlier
    sessions of a batch cost) reach --max-cost. At the end, sets each pair row's pair_result and
    writes the session's summary into the file, then prints the totals and paired accuracy.
    Returns {"cost", "stopped"}."""
    raw_cases = {c["case_id"]: c for c in read_cases(args.test_file)}
    passed, runs, cost, outcomes = 0, 0, 0.0, {}
    stopped = False
    for case in cases:
        task = to_task(case, args.rendering, session)
        hashes = case_hashes(raw_cases.get(case["case_id"], case))
        for run_index in range(args.n):
            if args.max_cost is not None and spent + cost >= args.max_cost:
                stopped = True
                break
            result = generate(backend, args.model, task, run_index=run_index)
            if result.ok:
                verdict = score_answer(case, result.raw_output, result.finish_reason, result.native_finish_reason)
            else:
                verdict = failed_call_verdict(result.error)
            projected = (projection.call(task, result.prompt_tokens, result.completion_tokens)
                         if projection and result.ok else None)

            session.record(result, projected)
            passed += verdict["outcome"] == "semantic_ok"
            outcomes[(task.task_id, run_index)] = verdict["outcome"]
            runs += 1
            cost += result.usd_cost
            label = task.task_id if args.n == 1 else f"{task.task_id} run {run_index}"
            print(f"{label}: {verdict['outcome']:20s} | {verdict.get('detail', '')}{call_note(result, projected)}")
            extra = {"case_hashes": hashes}
            if projected is not None:
                extra["projected_openrouter_usd"] = projected
            log_row(result, verdict, args.output, meta, extra)
        if stopped:
            break

    if stopped:
        print(f"stopped: the calls made have cost ${spent + cost:.4f}, reaching --max-cost ${args.max_cost:g}; "
              f"{len(cases) * args.n - runs} call(s) of this session not made")
    # The run's summary: mark an early stop, relate each pair row to its twin, recompute totals.
    sessions, results = load_results(args.output)
    for s in sessions:
        if s["session_id"] == meta["session_id"] and stopped:
            s["stopped_at_max_cost"] = True
    if pairs_of(cases):
        relate_pairs(args.test_file, results)     # each pair row's pair_result
    save_results(args.output, rebuild_summaries(args.test_file, sessions, results), results)
    print(f"\n{passed}/{runs} semantic_ok on {args.model}. Cost ${cost:.4f}. "
          f"Full results in {shown(args.output)} (session {meta['session_id']})")
    for line in pair_summary(pairs_of(cases), outcomes):
        print(line)
    return {"cost": cost, "stopped": stopped}


def run_session(args, backend, cases: list, subjects, label=None, batch=None, spent: float = 0.0,
                approved: bool = False) -> dict | None:
    """One session over `cases`: open it, estimate (a paid run is approved here unless a batch
    already approved it), run every case, write the summary. batch: the batch block every
    session of a --session-per-pair run carries. Returns run_cases()'s totals, or None when a
    paid run was not approved."""
    extra = {"batch": batch} if batch else {}
    with TallySession(args.cache_ttl, args.tally, subjects, label) as session:
        tasks = [to_task(c, args.rendering, session) for c in cases]
        if backend.paid:
            estimate = estimate_cost(args.model, session, tasks, args.n, args.max_tokens)
            print(estimate["text"])
            if not approved and not confirm_spend(estimate, args.max_cost, args.yes):
                return None
            meta = session_meta(args, backend, session, tasks[0], estimate["usd"], extra)
            return run_cases(cases, backend, args, session, meta, spent=spent)
        lines, local, projection = local_preamble(args, backend, session, cases, tasks, whole=batch is None)
        print("\n".join(lines))
        meta = session_meta(args, backend, session, tasks[0], None, {**local, **extra})
        return run_cases(cases, backend, args, session, meta, projection, spent=spent)


def run_batch(args, backend, cases: list) -> None:
    """--session-per-pair: each pair in its own session, its opening only the subjects that pair
    needs (--subjects auto unless --subjects names them). Estimates first: for a paid run the
    sessions' estimates add up to one go/no-go; either way the same cases in one whole-tally
    session are priced too. With --resume it continues the last batch in --output instead,
    skipping the pairs whose calls all succeeded. Ends with the batch's grand totals."""
    groups = list(enumerate(pair_groups(cases), 1))          # (session number, its cases)
    price_as = args.model if backend.paid else (args.price_as or args.model)
    resumed = last_batch(args.output) if args.resume else None
    if resumed:
        batch, done = resumed
        left = [(k, g) for k, g in groups if not all(c["case_id"] in done for c in g)]
        print(f"resuming {batch['id']}: {len(groups) - len(left)} of {len(groups)} sessions already done")
        groups = left
    else:
        if args.resume:
            print(f"--resume: no batch in {shown(args.output)} yet, so starting one")
        whole = whole_tally_estimate(args, price_as, cases)
        batch = {"id": f"batch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}", "sessions": len(groups),
                 "price_as": price_as, "whole_tally_estimate_usd": whole["usd"]}
    calls = sum(len(g) for _, g in groups) * args.n
    print(f"{batch['id']}: {len(groups)} sessions to run (one per pair), {calls} calls")
    if batch["whole_tally_estimate_usd"] is not None:
        print(f"  the same cases in one whole-{args.tally}-tally session on OpenRouter as {price_as}: "
              f"about ${batch['whole_tally_estimate_usd']:.2f}")
    if backend.paid and groups:
        parts = [estimate_for(args, args.model, g, pick_subjects(args, g, auto=True))["usd"] for _, g in groups]
        total = None if None in parts else round(sum(parts), 4)
        print("estimated cost of the batch: " + (f"${total:.2f}" if total is not None else "unknown"))
        if not confirm_spend({"usd": total}, args.max_cost, args.yes):
            return
    spent = 0.0
    for k, group in groups:
        print(f"\n--- session {k}/{batch['sessions']}: {', '.join(c['case_id'] for c in group)}")
        result = run_session(args, backend, group, pick_subjects(args, group, auto=True), label=f"{k:03d}",
                             batch=batch, spent=spent, approved=True)
        spent += result["cost"]
        if result["stopped"]:
            break
    print("\n=== batch totals")
    sessions, results = load_results(args.output)
    print("\n".join(report_lines(batch_totals(args.test_file, sessions, results, batch["id"]))))


def last_batch(output: str):
    """(the batch block, ids of the cases whose calls succeeded) for the last batch in a results
    file, or None when it has none. What --resume continues."""
    sessions, results = load_results(output)
    blocks = [s["batch"] for s in sessions if s.get("batch")]
    if not blocks:
        return None
    batch = blocks[-1]
    ids = {s["session_id"] for s in sessions if (s.get("batch") or {}).get("id") == batch["id"]}
    done = {r["task_id"] for r in results if r.get("session_id") in ids and r.get("error") is None}
    return batch, done


def main():
    """Command line. The no-model commands (--pair-report, --batch-report, --rescore, --compare,
    --check) run and exit. Otherwise every case is validated first (nothing is spent on a broken
    case), the run's cost is estimated (and, for a paid run, approved: --max-cost, --yes or a
    typed y), then the cases run in one session, or one per pair with --session-per-pair."""
    for stream in (sys.stdout, sys.stderr):    # never crash on a character the console cannot show
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = build_parser()
    args = parser.parse_args()
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    if args.pair_report:
        pair_report(args.test_file, args.output)
        return
    if args.batch_report:
        batch_report(args.test_file, args.output)
        return
    if args.rescore:
        rescore(args.test_file, args.output)
        return
    if args.compare:
        compare(args.test_file, *args.compare)
        return
    if args.pairs:
        if args.task_ids:
            parser.error("give --pairs or --task-ids, not both")
        args.task_ids = spread_pairs(read_cases(args.test_file), args.pairs)
    cases = load_test_cases(args.test_file, args.task_ids)
    if args.check:
        sys.exit(1 if check_cases(cases) else 0)
    if not args.model:
        parser.error("--model is required unless --check is given")
    if not cases:
        parser.error("no cases to run: check --task-ids against --test-file")

    for case in cases:                       # every case is sound before anything is spent
        problems = validate_case(case)
        if problems:
            raise ValueError(f"{case.get('case_id')} is not a valid test case: {problems}")
    if args.estimate_only:
        model = args.price_as or args.model
        if args.session_per_pair:
            print(f"{len(pair_groups(cases))} sessions (one per pair); the same cases in one whole-tally "
                  f"session: {whole_tally_estimate(args, model, cases)['text']}")
            return
        print(estimate_for(args, model, cases, pick_subjects(args, cases))["text"])
        return
    backend = make_backend(args)
    if args.dashboard:                         # lives in a background thread until the run ends
        from .dashboard import serve
        serve(args.output, args.test_file, background=True)
    if args.n < 3:
        print(f"note: {args.n} sample(s) per case; use -n 3 or more to compare sessions reliably")
    if args.session_per_pair:
        run_batch(args, backend, cases)
    else:
        run_session(args, backend, cases, pick_subjects(args, cases))


if __name__ == "__main__":
    main()
