"""
reports.py
----------
The commands that read a results file and make no model call (so they cost nothing):

    --pair-report   pair_report(): paired accuracy for every session in a file
    --compare A B   compare():     McNemar's test between two results files
    --rescore       rescore():     re-grade saved answers with the current scorer
    --effort-report effort_report(): one line per session (model x effort): accuracy, pairs, cost;
                                     then accuracy per check, one column per session
    --export-csv    export_csv():    one flat row per call, from any number of results files, for EDA,
                                     and <OUT>_sessions.csv: one row per session (model x effort)
"""
import csv
from pathlib import Path

from .cases import PAIR_VARIANTS, load_test_cases, read_cases
from .pairs import pair_summary, pairs_of, relate_pairs
from .results import GROUPS, _answer, _code_order, answer_text, load_results, rebuild_summaries, save_results
from .scoring import score_answer
from .stats import mcnemar


def pair_report(case_file: str, results_file: str) -> None:
    """Paired accuracy for every session in a results file, from its rows' task ids and verdicts."""
    pairs = pairs_of(read_cases(case_file))
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
        label = ", ".join(f"{s.get('model')} / {s.get('tally_variant')}" for s in sessions)
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
    pairs = pairs_of(read_cases(case_file))
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


def _print_table(lines: list) -> None:
    widths = [max(len(line[i]) for line in lines) for i in range(len(lines[0]))]
    for line in lines:
        print("  ".join(cell.ljust(w) for cell, w in zip(line, widths)))


def effort_report(case_file: str, results_file: str) -> None:
    """One line per session in a results file, for the model x effort comparison: accuracy, paired
    accuracy (all pairs and strong pairs only), cost in all and per correct answer, thinking tokens
    per call (and mean on right / wrong answers) and latency. Then accuracy per checklist code, one
    column per session: where more effort helps. From summaries recomputed from the rows; no model call."""
    sessions, results = load_results(results_file)
    pct = lambda r: f"{100 * r:.0f}%" if r is not None else "-"
    lines = [("model", "effort", "provider", "calls", "correct", "pairs", "strong", "cost", "$/correct",
              "think/call", "think ok/wrong", "latency")]
    summaries = rebuild_summaries(case_file, sessions, results)
    for s in summaries:
        g, p = s.get("generation_settings") or {}, s.get("paired_accuracy") or {}
        thinking = s["tokens"]["reasoning_tokens"]
        per_correct = s["cost"].get("per_correct_answer_usd")
        ms = s["latency"]["mean_total_ms"]
        lines.append((s["model"], (g.get("reasoning") or {}).get("effort", "default"),
                      ((g.get("provider") or {}).get("only") or ["any"])[0], str(s["calls"]),
                      pct(s["semantic_ok"]["rate"]), pct(p.get("rate")), pct((p.get("strong_pairs_only") or {}).get("rate")),
                      f"${s['cost']['total_usd']:.2f}", f"${per_correct:.4f}" if per_correct else "-",
                      f"{thinking / s['calls']:.0f}" if isinstance(thinking, int) and s["calls"] else "-",
                      "/".join(str(t[k]["mean"]) if k in t else "-" for k in ("correct", "wrong"))
                      if (t := s["analysis"]["reasoning_tokens_by_outcome"]) else "-",
                      f"{ms / 1000:.1f}s" if ms else "-"))
    _print_table(lines)
    by_check = [s["analysis"]["semantic_ok_by_check"] for s in summaries]
    codes = sorted({c for b in by_check for c in b}, key=_code_order)
    if codes:
        print("\nsemantic_ok per check (one column per session above, in order)")
        _print_table([("check", *[f"{line[0].split('/')[-1]} {line[1]}" for line in lines[1:]])]
                     + [(c, *[b[c]["count"] if c in b else "-" for b in by_check]) for c in codes])


# The columns of --export-csv, in order (the README's "EDA export" says what each means).
CSV_COLUMNS = ("session_id", "model", "effort", "provider", "tally_variant", "task_id", "run_index",
               "check", "check_name", "group", "case_type", "owner", "rebuilt", "pair_id", "variant", "pair_changes",
               "weak_pair", "chance", "candidate_answers", "correct_answers", "n_rules", "prompt_words",
               "transcript_courses", "expected_min", "expected_max", "outcome", "semantic_ok", "error_type",
               "tier1_ok", "tier2_ok", "tier3_ok", "tier4_ok", "sections_registered", "registered_nothing",
               "pair_passed", "same_answer_as_twin", "prompt_tokens", "cached_tokens", "cache_write_tokens",
               "completion_tokens", "reasoning_tokens", "usd_cost", "first_token_ms", "latency_ms",
               "finish_reason", "reused")


def _case_index(case_file: str) -> dict:
    """Every case by id: the finalized file's, also under the pair id it was rebuilt from (older
    results files), then data/paired_cases.json's pairs left out of it, given their checklist code."""
    cases = {}
    for c in read_cases(case_file):     # also by the pair id it had before finalizing (older results files)
        credit = (c.get("ground_truth") or {}).get("credit") or {}
        for old in (credit.get("rebuilt_as"), credit.get("original_case_id")):
            if old:
                cases.setdefault(old, c)
        cases[c["case_id"]] = c
    from .finalize import CHECK_NAMES, FAMILIES
    from .paths import PAIRS
    for c in read_cases(str(PAIRS)) if PAIRS.exists() else []:     # pairs left out of the finalized file
        code = FAMILIES.get(c["ground_truth"].get("case_type"), [""])[0]
        cases.setdefault(c["case_id"], {**c, "semantic_check": f"{code}: {CHECK_NAMES.get(code, '')}",
                                         "ground_truth": {**c["ground_truth"], "checks": [code]}})
    return cases


def _session_row(s: dict) -> dict:
    """One session summary as a flat row of SESSION_COLUMNS, then semantic_ok per checklist code."""
    from .finalize import ORDER
    g, a = s.get("generation_settings") or {}, s.get("analysis") or {}
    ok, pairs, cost = s.get("semantic_ok") or {}, s.get("paired_accuracy") or {}, s.get("cost") or {}
    ci, think = ok.get("ci95") or [None, None], a.get("reasoning_tokens_by_outcome") or {}
    ignored = [int(x) for v in (a.get("ignored_change_by_check") or {}).values() for x in v["count"].split("/")]
    empty = a.get("empty_cart") or {}
    tokens = s.get("tokens") or {}
    errors = s.get("error_types") or {}
    row = {"session_id": s.get("session_id"), "model": s.get("model"),
           "effort": (g.get("reasoning") or {}).get("effort", "default"),
           "provider": ((g.get("provider") or {}).get("only") or ["any"])[0],
           "tally_variant": s.get("tally_variant"), "samples_per_case": s.get("samples_per_case"),
           "batch_api": int(bool(s.get("batch_api"))), "started": s.get("started"), "cases": len(s.get("cases") or []),
           "calls": s.get("calls"), "generation_failures": s.get("generation_failures"),
           "semantic_ok_rate": ok.get("rate"), "semantic_ok_ci_low": ci[0], "semantic_ok_ci_high": ci[1],
           "mean_chance": s.get("mean_chance_baseline"),
           "pairs_scored": pairs.get("scored"), "paired_rate": pairs.get("rate"),
           "strong_pairs_rate": (pairs.get("strong_pairs_only") or {}).get("rate"),
           "ignored_change_rate": round(sum(ignored[::2]) / sum(ignored[1::2]), 3) if sum(ignored[1::2]) else None,
           "empty_when_right_rate": (empty.get("registered_nothing_when_right") or {}).get("rate"),
           "empty_when_wrong_rate": (empty.get("registered_nothing_when_wrong") or {}).get("rate"),
           "top_error_type": next(iter(errors), None), "top_error_count": next(iter(errors.values()), None),
           **{k: tokens.get(k) if isinstance(tokens.get(k), int) else None
              for k in ("prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens", "reasoning_tokens")},
           "think_mean_correct": (think.get("correct") or {}).get("mean"),
           "think_mean_wrong": (think.get("wrong") or {}).get("mean"),
           "cost_usd": cost.get("total_usd"), "setup_usd_est": cost.get("setup_usd_est"),
           "per_case_marginal_usd_est": cost.get("per_case_marginal_usd_est"),
           "per_correct_answer_usd": cost.get("per_correct_answer_usd"),
           "mean_latency_ms": (s.get("latency") or {}).get("mean_total_ms"),
           "mean_first_token_ms": (s.get("latency") or {}).get("mean_first_token_ms")}
    by_check = a.get("semantic_ok_by_check") or {}
    return {**row, **{f"ok_{c}": (by_check.get(c) or {}).get("rate") for c in ORDER}}


def export_csv(case_file: str, out_path: str, results_files: list) -> tuple:
    """Writes one flat row per call (CSV_COLUMNS) from every results file, joined with its session's
    settings and its case's ground truth: ready for pandas (pd.read_csv) or a spreadsheet. Flags are
    1/0, unknown is empty. An older file's case ids are found by the pair id a finalized case was
    rebuilt from, else in data/paired_cases.json; a case in neither leaves its case columns empty.
    Also writes <out>_sessions.csv: one row per session (model x effort) of every file, its summary
    recomputed on those cases (accuracy, pairs, the analysis rates, tokens, cost, latency, then
    semantic_ok per checklist code as ok_T1 ... ok_O3). Returns (call rows, session rows)."""
    cases = _case_index(case_file)
    bit = lambda x: "" if x is None else int(bool(x))
    sessions_out = []
    rows = []
    for path in results_files:
        sessions, results = load_results(path)
        relate_pairs(case_file, results)            # pair_result against this case file; not saved back
        mine = [{**cases[t], "case_id": t} for t in sorted({r["task_id"] for r in results}) if t in cases]
        sessions_out += [_session_row(s) for s in rebuild_summaries(case_file, sessions, results, mine)]
        meta = {s["session_id"]: s for s in sessions}
        for r in results:
            s, v = meta.get(r.get("session_id"), {}), r.get("verdict") or {}
            g = s.get("generation_settings") or {}
            c = cases.get(r["task_id"], {})
            gt = c.get("ground_truth") or {}
            pair, diff, credit = gt.get("pair") or {}, gt.get("difficulty") or {}, gt.get("credit") or {}
            code = (gt.get("checks") or [""])[0]
            count = (gt.get("rules") or {}).get("count") or {}
            answer = _answer(r) if r.get("error") is None else None
            tiers = {f"tier{i}_ok": bit(all(x["passed"] for x in v[f"tier{i}_checks"])) if v.get(f"tier{i}_checks") else ""
                     for i in range(1, 5)}
            pr = r.get("pair_result") or {}
            rows.append({
                "session_id": r.get("session_id"), "model": s.get("model"),
                "effort": (g.get("reasoning") or {}).get("effort", "default"),
                "provider": ((g.get("provider") or {}).get("only") or [r.get("provider") or ""])[0],
                "tally_variant": s.get("tally_variant"), "task_id": r["task_id"], "run_index": r.get("run_index"),
                "check": code, "check_name": (c.get("semantic_check") or "").partition(": ")[2],
                "group": GROUPS.get(code[:1], ""), "case_type": gt.get("case_type"), "owner": credit.get("owner"),
                "rebuilt": bit(credit.get("rebuilt_by")) if credit else "", "pair_id": pair.get("id"),
                "variant": pair.get("variant"), "pair_changes": (pair.get("difference") or {}).get("where"),
                "weak_pair": bit(pair.get("has_shared_answer")) if pair else "", "chance": diff.get("chance"),
                "candidate_answers": diff.get("candidate_answers"), "correct_answers": diff.get("correct_answers"),
                "n_rules": diff.get("rules"), "prompt_words": diff.get("prompt_words"),
                "transcript_courses": r.get("transcript_courses"),
                "expected_min": count.get("min"), "expected_max": count.get("max"),
                "outcome": v.get("outcome"), "semantic_ok": bit(v.get("outcome") == "semantic_ok"),
                "error_type": v.get("error_type"), **tiers,
                "sections_registered": len(answer) if isinstance(answer, tuple) else "",
                "registered_nothing": bit(answer == ()) if isinstance(answer, tuple) else "",
                "pair_passed": bit(pr.get("pair_passed")), "same_answer_as_twin": bit(pr.get("same_answer_as_twin")),
                **{k: r.get(k) for k in ("prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens",
                                         "reasoning_tokens", "usd_cost", "first_token_ms", "latency_ms", "finish_reason")},
                "reused": bit(r.get("reused_from"))})
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    if sessions_out:
        with open(_sessions_path(out_path), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(sessions_out[0]))
            w.writeheader()
            w.writerows(sessions_out)
    return len(rows), len(sessions_out)


def _sessions_path(out_path: str) -> str:
    """results/all.csv -> results/all_sessions.csv"""
    p = Path(out_path)
    return str(p.with_name(f"{p.stem}_sessions{p.suffix or '.csv'}"))


def rescore(case_file: str, results_file: str) -> None:
    """Re-grades every saved answer in a results file with the current scorer (all four tiers),
    rewrites its verdict and pair_result, and rebuilds the session summaries. A row whose call
    failed keeps its verdict. Prints any outcome that changed."""
    sessions, results = load_results(results_file)
    by_id = {c["case_id"]: c for c in load_test_cases(case_file, {row["task_id"] for row in results})}
    changed = graded = 0
    for row in results:
        if row.get("error") is not None:
            continue
        verdict = score_answer(by_id[row["task_id"]], answer_text(row),
                               row.get("finish_reason"), row.get("native_finish_reason"))
        if verdict["outcome"] != row["verdict"]["outcome"]:
            changed += 1
            print(f"{row['task_id']} run {row['run_index']}: {row['verdict']['outcome']} -> {verdict['outcome']}")
        row["verdict"] = verdict
        graded += 1
    related = relate_pairs(case_file, results)
    save_results(results_file, rebuild_summaries(case_file, sessions, results), results)
    print(f"rescored {graded} rows in {results_file}; {changed} outcomes changed"
          + (f"; pair_result on {related} rows" if related else ""))
