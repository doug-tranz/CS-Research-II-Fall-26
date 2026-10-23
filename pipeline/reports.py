"""
reports.py
----------
The commands that read a results file and make no model call (so they cost nothing):

    --pair-report   pair_report(): paired accuracy for every session in a file
    --compare A B   compare():     McNemar's test between two results files
    --rescore       rescore():     re-grade saved answers with the current scorer
"""
from .cases import PAIR_VARIANTS, load_test_cases, read_cases
from .pairs import pair_summary, pairs_of, relate_pairs
from .results import answer_text, load_results, rebuild_summaries, save_results
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
