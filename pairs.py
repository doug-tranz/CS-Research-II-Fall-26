"""
pairs.py
---------
Counterfactual pairs: two cases that are the same but for one detail, chosen so the detail
flips the answer. A pair passes a run only when both variants are semantic_ok in that run.
check_pairs() proves each pair in a case file is sound; pair_stats() / relate_pairs()
report paired accuracy on results.
"""
import json
from pathlib import Path

from cases import PAIR_VARIANTS, expected_message
from checker import seed_db
from scoring import evaluate_case
from stats import rate, wilson


def pairs_of(cases: list) -> dict:
    """pair id -> {variant: case_id}, for every case with ground_truth.pair."""
    pairs = {}
    for case in cases:
        pair = case.get("ground_truth", {}).get("pair")
        if pair:
            pairs.setdefault(pair["id"], {})[pair["variant"]] = case["case_id"]
    return pairs


def _rows_changed(a: dict, b: dict) -> int:
    """How many transcript rows differ between two transcripts (a row edited counts once). A
    row is its list, course and grade: the term a fabricator files it under does not count."""
    def rows(t):
        return {(name, r["course_id"], r.get("grade")) for name in ("completed_courses", "transfer_courses",
                                                                    "courses_in_progress") for r in t[name]}
    ra, rb = rows(a), rows(b)
    return max(len(ra - rb), len(rb - ra))


def check_pairs(cases: list) -> list:
    """Problems with the file's pairs: each has both variants, they differ in exactly one place
    (the prompt, or one transcript row), and the detail matters, i.e. each variant rejects at
    least one cart its twin accepts."""
    by_id, problems = {case["case_id"]: case for case in cases}, []
    for pair_id, variants in pairs_of(cases).items():
        if set(variants) != set(PAIR_VARIANTS):
            problems.append(f"pair {pair_id} has variants {sorted(variants)}, wants {list(PAIR_VARIANTS)}")
            continue
        a, b = by_id[variants["a"]]["input"], by_id[variants["b"]]["input"]
        changed = (a["prompt"] != b["prompt"]) + _rows_changed(a["transcript"], b["transcript"])
        if changed != 1:
            problems.append(f"pair {pair_id}: its cases differ in {changed} places (prompt and transcript rows), "
                            f"wants exactly 1")
        for here, twin in (("a", "b"), ("b", "a")):
            case, other = by_id[variants[here]], by_id[variants[twin]]
            conn = seed_db(case["input"])
            carts = other["ground_truth"].get("examples", {}).get("valid", [])
            if all(evaluate_case(case, expected_message(case, cart), conn)["outcome"] == "semantic_ok"
                   for cart in carts):
                problems.append(f"pair {pair_id}: {case['case_id']} accepts every valid cart of "
                                f"{other['case_id']}, so the changed detail does not change the answer")
    return problems


def pair_stats(pairs: dict, outcomes: dict, weak: set = frozenset()) -> dict:
    """Paired accuracy as data, for a session summary: passed and scored pairs with a 95%
    interval, the same for strong pairs only (those no single answer can pass, i.e. not in
    `weak`), incomplete pairs, and passed/scored by family; {} when no pair was run."""
    passed, total, incomplete, families, strong = 0, 0, 0, {}, [0, 0]
    runs = sorted({run for _, run in outcomes})
    for pair_id, variants in sorted(pairs.items()):
        for run in runs:
            got = [outcomes.get((variants.get(v), run)) for v in PAIR_VARIANTS]
            if got == [None, None]:
                continue
            if None in got or "generation_failed" in got:
                incomplete += 1
                continue
            ok = all(outcome == "semantic_ok" for outcome in got)
            family = families.setdefault(pair_id.split("/")[0], [0, 0])
            family[0] += ok
            family[1] += 1
            passed += ok
            total += 1
            if pair_id not in weak:
                strong[0] += ok
                strong[1] += 1
    if not total and not incomplete:
        return {}
    return {"passed": passed, "scored": total, "rate": round(passed / total, 3) if total else None,
            "ci95": wilson(passed, total), "strong_pairs_only": rate(*strong),
            "weak_pairs_scored": total - strong[1], "incomplete": incomplete,
            "by_family": {name: f"{ok}/{n}" for name, (ok, n) in sorted(families.items())}}


def pair_summary(pairs: dict, outcomes: dict) -> list:
    """Lines reporting paired accuracy (pair_stats(), printed). outcomes: (case_id, run_index) ->
    outcome."""
    stats = pair_stats(pairs, outcomes)
    if not stats:
        return []
    note = (f"; {stats['incomplete']} incomplete (a call failed or a variant was not run), not scored"
            if stats["incomplete"] else "")
    if not stats["scored"]:
        return [f"Paired accuracy: no complete pairs{note}"]
    lines = [f"Paired accuracy: {stats['passed']}/{stats['scored']} pairs with both variants semantic_ok "
             f"({100 * stats['rate']:.0f}%){note}"]
    return lines + [f"    {name:18s} {count}" for name, count in stats["by_family"].items()]


def _answer(row: dict) -> list:
    """The sections a result row's message registered for, as 'crn (course)'; [] if none."""
    body = (row.get("parsed_output") or {}).get("body") if isinstance(row.get("parsed_output"), dict) else None
    sections = body.get("sections") if isinstance(body, dict) else None
    if not isinstance(sections, list):
        return []
    return [f"{s.get('crn')} ({s.get('course_id')})" if isinstance(s, dict) else str(s) for s in sections]


def _relation(here: dict, twin: dict | None, pair: dict, twin_id: str) -> dict:
    """pair_result for one row: how its result relates to its twin's in the same run. Only
    what the row and its twin do not already say: the twin's id, whether the two answers are
    the same, the pair's verdict, and an explanation (which names the twin's outcome)."""
    out = {"pair_id": pair["id"], "twin_id": twin_id}
    changed, where = pair.get("changed"), (pair.get("difference") or {}).get("where") or "one detail"
    mine = here["verdict"]["outcome"]
    if twin is None or "generation_failed" in (mine, twin["verdict"]["outcome"]):
        why = "its twin was not run in this session" if twin is None else "a call failed"
        return {**out, "pair_passed": None, "explanation": f"Incomplete: {why}, so the pair is not scored."}
    theirs = twin["verdict"]["outcome"]
    a, b = _answer(here), _answer(twin)
    same = bool(a) and sorted(a) == sorted(b)
    ok_here, ok_twin = mine == "semantic_ok", theirs == "semantic_ok"
    if ok_here and ok_twin and same:
        text = (f"Both cases are correct with the same answer, which is valid under both versions of the {where} "
                f"({changed}). Pair passed, but weakly: an answer that ignores the change would pass too, so "
                f"this pair does not show the model used the changed detail.")
    elif ok_here and ok_twin:
        text = (f"Both cases are correct and the answer changed with the {where} ({changed}): "
                f"the model used the changed detail. Pair passed.")
    elif ok_here or ok_twin:
        right, wrong = ("this case", "its twin") if ok_here else ("its twin", "this case")
        text = (f"Only {right} is correct; {wrong} is a {theirs if ok_here else mine}. Pair failed. "
                + (f"The model gave the same answer to both, so it ignored the change in the {where} and was "
                   f"right only where that answer happened to fit." if same else
                   f"The model changed its answer between the two, but not to a correct one in {wrong}."))
    else:
        text = (f"Neither case is correct (this: {mine}, twin: {theirs}). Pair failed"
                + (", with the same answer to both: the change in the " + where + " was ignored." if same else "."))
    return {**out, "same_answer_as_twin": same, "pair_passed": ok_here and ok_twin, "explanation": text}


def relate_pairs(case_file: str, results: list) -> int:
    """Sets pair_result on every row of a pair case (in place): related to its twin's row in the
    same session and run. Returns how many rows it set."""
    meta = {c["case_id"]: c["ground_truth"]["pair"] for c in json.loads(Path(case_file).read_text(encoding="utf-8"))
            if c.get("ground_truth", {}).get("pair")}
    members = {}
    for cid, p in meta.items():
        members.setdefault(p["id"], []).append(cid)
    key = lambda r, task: (r.get("session_id"), r["run_index"], task)
    index = {key(r, r["task_id"]): r for r in results}
    set_ = 0
    for row in results:
        pair = meta.get(row["task_id"])
        if not pair:
            continue
        twin_id = next(cid for cid in members[pair["id"]] if cid != row["task_id"])
        row["pair_result"] = _relation(row, index.get(key(row, twin_id)), pair, twin_id)
        set_ += 1
    return set_
