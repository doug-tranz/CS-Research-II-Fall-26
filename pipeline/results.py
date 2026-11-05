"""
results.py
----------
The results file: {"sessions": [summary, ...], "results": [row, ...]}.

A session summary holds what every call of one run shares (model, tally version, cached
opening, output contract) and its totals; a result row holds only what is its own and names
its session. Nothing is stored twice, except deviation_score (also averaged in the summary).

Rows are appended one at a time as the run goes (log_row()), so a crash loses at most the call
in flight; the file is written to a temporary file and swapped in (save_results()), so a crash
DURING a write cannot destroy the rows already paid for.
"""
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from .cases import read_cases
from .pairs import pair_stats, pairs_of
from .stats import rate

# Fields of a generation that are the same for every row of a session: kept once, in its summary.
SESSION_FIELDS = ("model", "shared_context")
# Duplicates dropped from a row: prompt_sent (prompt_readable holds it, parsed), ok (= error is null).
ROW_DROPPED = ("prompt_sent", "ok")
# Local-run fields (LM Studio): dropped from a row when empty, so OpenRouter rows are unchanged.
LOCAL_FIELDS = ("energy_wh", "avg_watts", "energy_usd", "projected_openrouter_usd")
ROW_ORDER = ("task_id", "session_id", "run_index", "ts", "case_hashes", "prompt_readable", "parsed_output",
             "raw_output", "reasoning", "error", "finish_reason", "native_finish_reason", "served_model", "provider",
             "generation_id", "prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens",
             "reasoning_tokens", "first_token_ms", "latency_ms", "usd_cost", *LOCAL_FIELDS, "generation", "verdict",
             "pair_result")


# ── One row ──

def readable_prompt(prompt_sent: str) -> dict:
    """The case's own turn split into its parts, for reading: the known state (an object when
    it was sent as JSON, otherwise its lines) and the intent. The layout is
    harness.build_messages(): 'Known state (JSON):\\n<state>\\n\\nIntent: <intent>'; the output
    instructions are in the session's cached opening (its summary's output_contract)."""
    head = "Known state (JSON):\n"
    if not prompt_sent.startswith(head) or "\n\nIntent: " not in prompt_sent:
        return {"text": prompt_sent.splitlines()}
    state, _, intent = prompt_sent[len(head):].partition("\n\nIntent: ")
    try:
        state = json.loads(state)
    except json.JSONDecodeError:
        state = state.splitlines()           # the csv and nl renderings
    return {"known_state": state, "intent": intent}


def readable_row(row: dict, verdict: dict) -> dict:
    """Adds the prompt and the model's message as parsed objects, right after the verbatim
    strings they come from; prompt_sent and raw_output stay exactly as sent and received."""
    try:
        parsed = json.loads(row["raw_output"])
    except json.JSONDecodeError:
        parsed = None
    out = {}
    for key, value in row.items():
        if key in ("prompt_readable", "parsed_output", "verdict"):
            continue
        out[key] = value
        if key == "prompt_sent":
            out["prompt_readable"] = readable_prompt(value)
        if key == "raw_output":
            out["parsed_output"] = parsed
    out["verdict"] = verdict
    return out


def answer_text(row: dict) -> str:
    """The model's answer as the scorer reads it: raw_output when the row keeps it (it did not
    parse as JSON), else the parsed message, serialized."""
    return row["raw_output"] if "raw_output" in row else json.dumps(row["parsed_output"])


def slim_row(row: dict) -> dict:
    """A result row without its session's shared fields or duplicates: the raw answer is kept
    only when it is not JSON (otherwise parsed_output holds it). Idempotent."""
    out = {k: v for k, v in row.items() if k not in SESSION_FIELDS + ROW_DROPPED
           and not (k in LOCAL_FIELDS and v is None)}
    if out.get("parsed_output") is not None and "raw_output" in out:
        try:
            if json.loads(out["raw_output"]) == out["parsed_output"]:
                del out["raw_output"]
        except (json.JSONDecodeError, TypeError):
            pass
    if out.get("parsed_output") is None:
        out.pop("parsed_output", None)
    if not out.get("reasoning"):
        out.pop("reasoning", None)            # the provider hid it (OpenAI) or the row predates the field
    if out.get("raw_output") == "" and out.get("error") is not None:
        del out["raw_output"]                 # a failed call returned nothing; error says why
    ordered = {k: out[k] for k in ROW_ORDER if k in out}
    return {**ordered, **{k: v for k, v in out.items() if k not in ordered}}


# ── The file ──

def load_results(path: str) -> tuple:
    """(sessions, results) from a results file, [] and [] if there is none."""
    file = Path(path)
    if not file.exists():
        return [], []
    data = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a results file of this pipeline ({{\"sessions\", \"results\"}})")
    return data.get("sessions", []), data.get("results", [])


def save_results(path: str, sessions: list, results: list) -> None:
    """Writes the results file, every row slimmed. Written to <path>.tmp first and then swapped
    in (os.replace is atomic), so the old file stays whole until the new one is complete."""
    rows = [slim_row(row) for row in results]
    tmp = Path(f"{path}.tmp")
    tmp.write_text(json.dumps({"sessions": sessions, "results": rows}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def log_row(result, verdict: dict, path: str, meta: dict, extra: dict | None = None) -> None:
    """Appends one result row to the results file, adding its session's summary (meta: the
    settings the run shares) the first time; totals are filled in when the run ends. extra:
    more row fields (the case's hashes, a local call's projected OpenRouter cost)."""
    row = readable_row(asdict(result), verdict)
    row.update(session_id=meta["session_id"], **(extra or {}))
    append_row(path, row, meta)


def append_row(path: str, row: dict, meta: dict) -> None:
    """Appends a finished row (a new call's, or one --reuse-from copies) to the results file,
    adding its session's summary (meta) the first time."""
    sessions, results = load_results(path)
    if not any(s["session_id"] == meta["session_id"] for s in sessions):
        sessions.append(dict(meta))
    results.append(row)
    save_results(path, sessions, results)



def add_generations(rows: list, api_key: str) -> int:
    """Adds OpenRouter's record of each call (backends.generation_record()) as `generation` to the
    rows that have a generation_id and no record yet, in place; batch calls have none to fetch.
    Returns how many rows got one."""
    from .backends import generation_record
    added = 0
    for r in rows:
        gid = r.get("generation_id")
        if not gid or "generation" in r or str(gid).startswith("gen-batch"):
            continue
        record = generation_record(api_key, gid)
        if record:
            r["generation"] = record
            added += 1
    return added


def file_hash(path: str) -> str:
    """The first 16 hex digits of a file's sha256."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


# ── Session summaries ──

def _cost_breakdown(rows: list, correct: int, passed_pairs: int | None) -> dict:
    """What the session cost, split into the one-time cache write of the shared opening and the
    marginal cost of one case, plus cost per correct answer and per passed pair. The split is an
    estimate: OpenRouter reports one cost per call. The setup calls are the session's first call
    (it pays for the opening, whether or not the provider reports a cache write: DeepSeek and
    Gemini bill it as plain input) and any later call that wrote most of its prompt to the cache
    again (the cache expired). The marginal cost is the median of the other calls, which may
    write a little (OpenAI caches the part of a case that repeats); setup is what the setup
    calls cost above it. A session that started on a warm cache gets a setup near 0."""
    ok = [r for r in rows if r.get("error") is None]
    cost = sum(r.get("usd_cost", 0) or 0 for r in rows)
    rewrote = lambda r: (r.get("cache_write_tokens") or 0) * 2 >= (r.get("prompt_tokens") or 0) > 0
    setups = ok[:1] + [r for r in ok[1:] if rewrote(r)]
    others = sorted(r["usd_cost"] for r in ok[1:] if not rewrote(r))
    marginal = others[len(others) // 2] if others else None
    setup = (round(sum(max(0.0, r["usd_cost"] - marginal) for r in setups), 6)
             if setups and marginal is not None else None)
    out = {"total_usd": round(cost, 6), "avg_per_call_usd": round(cost / len(rows), 6) if rows else None,
           "setup_usd_est": setup, "per_case_marginal_usd_est": round(marginal, 6) if marginal is not None else None,
           "per_correct_answer_usd": round(cost / correct, 6) if correct else None}
    if passed_pairs is not None:
        out["per_passed_pair_usd"] = round(cost / passed_pairs, 6) if passed_pairs else None
    out["note"] = ("All costs are what OpenRouter billed for the original model calls; re-grading a results "
                   "file (--rescore) makes no model call and costs nothing.")
    return out


def _local_breakdown(rows: list, correct: int, meta: dict) -> dict | None:
    """A local (LM Studio) session's costs: the electricity the GPU drew, and what the same calls
    would have cost on OpenRouter as meta's price_as model. None for a cloud session."""
    metered = [r for r in rows if r.get("energy_wh") is not None]
    if not metered:
        return None
    wh = sum(r["energy_wh"] for r in metered)
    usd = sum(r["energy_usd"] for r in metered)
    seconds = sum(r["energy_wh"] / r["avg_watts"] * 3600 for r in metered if r.get("avg_watts"))   # metered time
    out = {"energy_wh": round(wh, 4), "energy_usd": round(usd, 6),
           "avg_watts": round(wh * 3600 / seconds, 1) if seconds else None,
           "energy_usd_per_call": round(usd / len(metered), 8),
           "energy_usd_per_correct_answer": round(usd / correct, 8) if correct else None}
    projected = [r["projected_openrouter_usd"] for r in rows if r.get("projected_openrouter_usd") is not None]
    if projected:
        total = sum(projected)
        out["projected_openrouter"] = {
            "model": meta.get("price_as"), "total_usd": round(total, 6),
            "per_call_usd": round(total / len(projected), 6),
            "per_correct_answer_usd": round(total / correct, 6) if correct else None,
            "note": ("What these calls would have cost on OpenRouter at its current prices, from the local "
                     "model's token counts (split into cached and uncached by characters). Pricing another "
                     "model than the one run locally uses the local tokenizer and answer lengths.")}
    return out


# ── Analysis: where models go wrong, from a session's rows and the case file ──

GROUPS = {"T": "Temporal reasoning", "D": "Days", "C": "Capacity", "I": "Course and section identity",
          "K": "Request constraints", "R": "Student record", "O": "Completeness of the answer"}
SIZES = ((10, "1-10 courses"), (20, "11-20 courses"), (30, "21-30 courses"), (10 ** 6, "over 30 courses"))


def _code_order(code: str) -> tuple:
    """'T10' after 'T9': checklist codes in checklist order."""
    return list(GROUPS).index(code[0]) if code[0] in GROUPS else 99, int(code[1:]) if code[1:].isdigit() else 0


def _by(rows: list, keys_of, chance_of) -> dict:
    """semantic_ok per key (a row can count under several), each with a 95% interval, the mean
    chance baseline of its cases and the accuracy above it (lift_over_chance)."""
    tally = {}
    for r in rows:
        for key in keys_of(r):
            t = tally.setdefault(key, [0, 0, 0.0, 0])
            t[0] += r["verdict"]["outcome"] == "semantic_ok"
            t[1] += 1
            if chance_of(r) is not None:
                t[2] += chance_of(r)
                t[3] += 1
    out = {}
    for key, (ok, n, chance, counted) in tally.items():
        base = chance / counted if counted else None
        out[key] = {**rate(ok, n), "mean_chance": round(base, 3) if base is not None else None,
                    "lift_over_chance": round(ok / n - base, 3) if base is not None else None}
    return out


def _answer(row: dict):
    """A row's answer, comparable across samples: its sorted crns, else its raw text."""
    body = (row.get("parsed_output") or {}).get("body") if isinstance(row.get("parsed_output"), dict) else None
    if isinstance(body, dict) and isinstance(body.get("sections"), list):
        return tuple(sorted(str(s.get("crn")) for s in body["sections"] if isinstance(s, dict)))
    return answer_text(row)


def analysis(rows: list, pairs: dict, cases: dict) -> dict:
    """The session's breakdowns for analysis (all from its rows and the case file, no model call):
    semantic_ok by checklist code, group, case owner and transcript size, each against chance;
    paired accuracy by what the pair changes (prompt or transcript); per check, the kinds of
    mistake, how often both twins got the same answer though their right answers differ (the
    change was ignored) and cart size against the request; empty carts when nothing (or
    something) was right; thinking tokens on right and wrong answers; and, with -n 2 or more,
    how stable the model's answers are across samples of one case."""
    truth = lambda r: cases.get(r["task_id"], {}).get("ground_truth") or {}
    chance = lambda r: (truth(r).get("difficulty") or {}).get("chance")
    ok = [r for r in rows if r.get("error") is None]
    size = lambda r: [next(label for top, label in SIZES if r["transcript_courses"] <= top)] \
        if r.get("transcript_courses") is not None else []
    out = {
        "semantic_ok_by_check": dict(sorted(_by(ok, lambda r: truth(r).get("checks", []), chance).items(),
                                            key=lambda x: _code_order(x[0]))),
        "semantic_ok_by_group": {g: v for g, v in sorted(
            _by(ok, lambda r: sorted({GROUPS[c[0]] for c in truth(r).get("checks", []) if c[0] in GROUPS}), chance).items(),
            key=lambda x: list(GROUPS.values()).index(x[0]))},
        "semantic_ok_by_owner": _by(ok, lambda r: [(truth(r).get("credit") or {}).get("owner")] if truth(r).get("credit") else [],
                                    chance),
        "semantic_ok_by_transcript_size": {k: v for k, v in sorted(_by(ok, size, chance).items(),
                                                                   key=lambda x: [l for _, l in SIZES].index(x[0]))},
    }
    # paired accuracy by where the twins differ (pair.difference.where: prompt or transcript)
    outcomes = {(r["task_id"], r["run_index"]): r["verdict"]["outcome"] for r in rows}
    where = {}
    for pid, variants in pairs.items():
        first = cases.get(next(iter(variants.values())), {})
        w = (((first.get("ground_truth") or {}).get("pair") or {}).get("difference") or {}).get("where")
        if w:
            where.setdefault(w, {})[pid] = variants
    out["paired_by_change"] = {w: {k: s[k] for k in ("passed", "scored", "rate", "ci95")}
                               for w, group in sorted(where.items()) if (s := pair_stats(group, outcomes))}
    by_code = lambda r: sorted(truth(r).get("checks", []), key=_code_order)
    # what kind of mistake, per check: error_type (else the outcome) of every wrong answer
    errors = {}
    for r in ok:
        if r["verdict"]["outcome"] != "semantic_ok":
            for code in by_code(r):
                e = errors.setdefault(code, {})
                kind = r["verdict"].get("error_type") or r["verdict"]["outcome"]
                e[kind] = e.get(kind, 0) + 1
    out["errors_by_check"] = {c: dict(sorted(e.items(), key=lambda x: -x[1]))
                              for c, e in sorted(errors.items(), key=lambda x: _code_order(x[0]))}
    # ignored the change: the same answer to both twins of a pair whose right answers differ
    answered = {(r["task_id"], r["run_index"]): r for r in ok}
    ignored, runs = {}, sorted({run for _, run in answered})
    for variants in pairs.values():
        ids = list(variants.values())
        gts = [cases.get(cid, {}).get("ground_truth") or {} for cid in ids]
        if len(ids) != 2 or any((g.get("pair") or {}).get("has_shared_answer") for g in gts):
            continue                            # a weak pair: one answer is right in both
        for run in runs:
            a, b = answered.get((ids[0], run)), answered.get((ids[1], run))
            if a and b:
                for code in sorted(gts[0].get("checks", []), key=_code_order):
                    t = ignored.setdefault(code, [0, 0])
                    t[0] += _answer(a) == _answer(b)
                    t[1] += 1
    out["ignored_change_by_check"] = {c: rate(*t) for c, t in sorted(ignored.items(), key=lambda x: _code_order(x[0]))}
    # empty carts and cart size against the request's count rule (answers that parsed into sections only)
    counted = [(r, len(_answer(r))) for r in ok if isinstance(_answer(r), tuple)]
    count = lambda r: (truth(r).get("rules") or {}).get("count") or {}
    nothing = [n == 0 for r, n in counted if count(r).get("max") == 0]
    something = [n == 0 for r, n in counted if count(r).get("max") != 0]
    out["empty_cart"] = {"registered_nothing_when_right": rate(sum(nothing), len(nothing)),
                         "registered_nothing_when_wrong": rate(sum(something), len(something))}
    sizes = {}
    for r, n in counted:
        c = count(r)
        if c:
            side = "fewer" if n < c.get("min", 0) else "more" if "max" in c and n > c["max"] else "as_requested"
            for code in by_code(r):
                t = sizes.setdefault(code, {"fewer": 0, "as_requested": 0, "more": 0})
                t[side] += 1
    out["sections_vs_requested_by_check"] = dict(sorted(sizes.items(), key=lambda x: _code_order(x[0])))
    # thinking tokens on right and on wrong answers
    think = {}
    for r in ok:
        if isinstance(r.get("reasoning_tokens"), int):
            think.setdefault("correct" if r["verdict"]["outcome"] == "semantic_ok" else "wrong", []).append(r["reasoning_tokens"])
    out["reasoning_tokens_by_outcome"] = {k: {"calls": len(v), "mean": round(sum(v) / len(v)), "median": sorted(v)[len(v) // 2]}
                                          for k, v in sorted(think.items())}
    # stability across samples of one case
    samples = {}
    for r in ok:
        samples.setdefault(r["task_id"], []).append(r)
    many = {cid: rs for cid, rs in samples.items() if len(rs) >= 2}
    if many:
        same_answer = sum(len({_answer(r) for r in rs}) == 1 for rs in many.values())
        same_outcome = {cid: len({r["verdict"]["outcome"] for r in rs}) == 1 for cid, rs in many.items()}
        flips = {}
        for cid, steady in same_outcome.items():
            for code in (cases.get(cid, {}).get("ground_truth") or {}).get("checks", []):
                f = flips.setdefault(code, [0, 0])
                f[0] += not steady
                f[1] += 1
        out["stability"] = {"cases_sampled": len(many), "samples_per_case": round(sum(map(len, many.values())) / len(many), 2),
                            "same_answer_every_sample": rate(same_answer, len(many)),
                            "same_outcome_every_sample": rate(sum(same_outcome.values()), len(many)),
                            "flip_rate_by_check": {c: rate(*f) for c, f in sorted(flips.items(), key=lambda x: _code_order(x[0]))}}
    return out


def summarize_session(meta: dict, rows: list, pairs: dict, cases: dict | None = None) -> dict:
    """A session's summary: its shared settings (meta) and what its rows add up to. cases: the
    case file's raw cases by id, for pair flags, case types and chance baselines."""
    cases = cases or {}
    calls = len(rows)
    outcomes, errors, finish, served = {}, {}, {}, {}
    for r in rows:
        v = r["verdict"]
        outcomes[v["outcome"]] = outcomes.get(v["outcome"], 0) + 1
        if v.get("error_type"):
            errors[v["error_type"]] = errors.get(v["error_type"], 0) + 1
        if r.get("finish_reason") or r.get("native_finish_reason"):
            key = f"{r.get('finish_reason')} ({r.get('native_finish_reason')})"
            finish[key] = finish.get(key, 0) + 1
        if r.get("provider") or r.get("served_model"):
            key = f"{r.get('served_model')} via {r.get('provider')}"
            served[key] = served.get(key, 0) + 1
    scored = [r["verdict"]["deviation_score"] for r in rows if "deviation_score" in r["verdict"]]
    made = [r for r in rows if not r.get("reused_from")]      # tokens and cost: this session's own calls
    tokens = {k: sum(r.get(k, 0) or 0 for r in made)
              for k in ("prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens")}
    tokens["reasoning_tokens"] = (sum(r.get("reasoning_tokens") or 0 for r in made)
                                  if any("reasoning_tokens" in r for r in made) else "not recorded")
    ok = [r for r in rows if r.get("error") is None]
    correct = outcomes.get("semantic_ok", 0)
    truth = lambda r: cases.get(r["task_id"], {}).get("ground_truth") or {}
    by_type = {}
    for r in ok:
        kind = truth(r).get("case_type")
        if kind:
            t = by_type.setdefault(kind, [0, 0])
            t[0] += r["verdict"]["outcome"] == "semantic_ok"
            t[1] += 1
    chance = [truth(r)["difficulty"]["chance"] for r in ok if truth(r).get("difficulty")]
    first = [r["first_token_ms"] for r in ok if r.get("first_token_ms") is not None]
    timed = [r["latency_ms"] for r in ok if r.get("latency_ms") is not None]     # batch rows have none
    # weak pairs: one answer is correct in both variants, so passing them shows less
    weak = {pid for pid in pairs if any((cases.get(cid, {}).get("ground_truth") or {}).get("pair", {}).get("has_shared_answer")
                                        for cid in pairs[pid].values())}
    stats = pair_stats(pairs, {(r["task_id"], r["run_index"]): r["verdict"]["outcome"] for r in rows}, weak)
    sampling = meta.get("samples_per_case")
    summary = {
        **{k: v for k, v in meta.items() if v is not None},
        "started": min((r["ts"] for r in rows), default=None), "ended": max((r["ts"] for r in rows), default=None),
        "cases": sorted({r["task_id"] for r in rows}), "calls": calls,
        "generation_failures": calls - len(ok),
        "outcomes": dict(sorted(outcomes.items())),
        "semantic_ok": rate(correct, len(ok)),
        "semantic_ok_by_case_type": {k: f"{a}/{n}" for k, (a, n) in sorted(by_type.items())},
        "mean_chance_baseline": round(sum(chance) / len(chance), 4) if chance else None,
        "mean_deviation_score": round(sum(scored) / len(scored), 4) if scored else None,
        "error_types": dict(sorted(errors.items(), key=lambda x: -x[1])),
        "finish_reasons": finish or "not recorded (calls made before 2026-10-19 streaming)",
        "served_by": served or "not recorded (calls made before 2026-10-19 streaming)",
        "tokens": {**tokens, "cache_read_share": f"{100 * tokens['cached_tokens'] / tokens['prompt_tokens']:.0f}%"
                   if tokens["prompt_tokens"] else None},
        "cost": _cost_breakdown(made, correct, stats.get("passed") if stats else None),
        "latency": {"mean_total_ms": round(sum(timed) / len(timed)) if timed else None,
                    "mean_first_token_ms": round(sum(first) / len(first)) if first else None},
    }
    if len(made) < calls:
        summary["reused_answers"] = (f"{calls - len(made)} of the {calls} answers were reused (--reuse-from) from "
                                     f"calls made and paid for in another session; tokens and cost count only "
                                     f"the {len(made)} made here")
    local = _local_breakdown(made, correct, meta)
    if local:
        summary["local_cost"] = local
    if stats:
        summary["paired_accuracy"] = stats
    summary["analysis"] = analysis(rows, pairs, cases)
    if sampling is not None and sampling < 3:
        summary["sampling_note"] = (f"{sampling} sample(s) per case: intervals are wide, and a difference between "
                                    f"sessions can be chance. Use -n 3 or more, and --compare for a paired test.")
    return summary


# The summary keys computed from rows (the rest of a summary is the run's settings).
SUMMARY_TOTALS = ("started", "ended", "cases", "calls", "generation_failures", "outcomes", "semantic_ok",
                  "semantic_ok_by_case_type", "mean_chance_baseline", "mean_deviation_score", "error_types",
                  "finish_reasons", "served_by", "tokens", "cost", "latency", "local_cost", "paired_accuracy",
                  "sampling_note", "reused_answers", "analysis")


def rebuild_summaries(case_file: str, sessions: list, results: list, raw: list | None = None) -> list:
    """Every session's summary recomputed from its rows (settings kept, totals refreshed). raw:
    the cases to use instead of reading case_file."""
    if raw is None:
        raw = read_cases(case_file) if Path(case_file).exists() else []
    pairs, cases = pairs_of(raw), {c["case_id"]: c for c in raw}
    out = []
    for s in sessions:
        meta = {k: v for k, v in s.items() if k not in SUMMARY_TOTALS}
        out.append(summarize_session(meta, [r for r in results if r.get("session_id") == s["session_id"]],
                                     pairs, cases))
    return out
