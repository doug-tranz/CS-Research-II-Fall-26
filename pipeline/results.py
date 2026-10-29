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
             "raw_output", "error", "finish_reason", "native_finish_reason", "served_model", "provider",
             "generation_id", "prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens",
             "reasoning_tokens", "first_token_ms", "latency_ms", "usd_cost", *LOCAL_FIELDS, "verdict", "pair_result")


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
    if sampling is not None and sampling < 3:
        summary["sampling_note"] = (f"{sampling} sample(s) per case: intervals are wide, and a difference between "
                                    f"sessions can be chance. Use -n 3 or more, and --compare for a paired test.")
    return summary


# The summary keys computed from rows (the rest of a summary is the run's settings).
SUMMARY_TOTALS = ("started", "ended", "cases", "calls", "generation_failures", "outcomes", "semantic_ok",
                  "semantic_ok_by_case_type", "mean_chance_baseline", "mean_deviation_score", "error_types",
                  "finish_reasons", "served_by", "tokens", "cost", "latency", "local_cost", "paired_accuracy",
                  "sampling_note", "reused_answers")


def rebuild_summaries(case_file: str, sessions: list, results: list) -> list:
    """Every session's summary recomputed from its rows (settings kept, totals refreshed)."""
    raw = read_cases(case_file) if Path(case_file).exists() else []
    pairs, cases = pairs_of(raw), {c["case_id"]: c for c in raw}
    out = []
    for s in sessions:
        meta = {k: v for k, v in s.items() if k not in SUMMARY_TOTALS}
        out.append(summarize_session(meta, [r for r in results if r.get("session_id") == s["session_id"]],
                                     pairs, cases))
    return out
