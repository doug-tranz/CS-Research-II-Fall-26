"""
results.py
-----------
The results file: {"sessions": [summary, ...], "results": [row, ...]}. Writing rows as they
come in (log_readable), slimming them, reading files back (load_results), and computing each
session's summary (summarize_session).
"""
import hashlib
import json
import os
import socket
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path

from pairs import pair_stats, pairs_of
from stats import rate


# ── Logging ──

def readable_prompt(prompt_sent: str) -> dict:
    """prompt_sent split into its parts, for reading: the known state (an object when it was
    sent as JSON, otherwise its lines), the intent, and the output contract.
    The layout is harness.build_messages(): 'Known state (JSON):\\n<state>\\n\\nIntent: <intent>\\n\\n<contract>'."""
    head = "Known state (JSON):\n"
    if not prompt_sent.startswith(head) or "\n\nIntent: " not in prompt_sent:
        return {"text": prompt_sent.splitlines()}
    state, _, rest = prompt_sent[len(head):].partition("\n\nIntent: ")
    intent, _, contract = rest.partition("\n\n")
    try:
        state = json.loads(state)
    except json.JSONDecodeError:
        state = state.splitlines()           # the csv and nl renderings
    return {"known_state": state, "intent": intent, "output_contract": contract}


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


# ── The results file: {"sessions": [summary, ...], "results": [row, ...]} ──
# A session summary holds what every call of one run shares (model, tally version, cached
# opening, output contract) and its totals; a result row holds only what is its own and names
# its session. Nothing is stored twice, except deviation_score (also averaged in the summary).

# Fields that are the same for every row of a session: kept once, in its summary.
SESSION_FIELDS = ("model", "tally_variant", "shared_context", "session_cost_usd", "session_avg_cost_per_prompt_usd")
# Duplicates dropped from a row: prompt_sent (prompt_readable holds it, parsed), ok (= error is null).
ROW_DROPPED = ("prompt_sent", "ok")
# Verdict fields of older results files that repeat others (tiers.tier3, the failed tier4_checks);
# dropped when such a file is read and saved again.
VERDICT_DROPPED = ("tier3_detail", "violated")
ROW_ORDER = ("task_id", "session_id", "run_index", "ts", "case_hashes", "prompt_readable", "parsed_output",
             "raw_output", "error", "finish_reason", "native_finish_reason", "served_model", "provider",
             "generation_id", "prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens",
             "reasoning_tokens", "first_token_ms", "latency_ms", "usd_cost", "verdict", "pair_result")


def answer_text(row: dict) -> str:
    """The model's answer as the scorer reads it: raw_output when the row keeps it (it did not
    parse as JSON), else the parsed message, serialized."""
    return row["raw_output"] if "raw_output" in row else json.dumps(row["parsed_output"])


def slim_row(row: dict) -> dict:
    """A result row without its session's shared fields or duplicates: the raw answer is kept
    only when it is not JSON (otherwise parsed_output holds it), and the output contract only
    where it differs from the session's (the caller passes that check). Idempotent."""
    out = {k: v for k, v in row.items() if k not in SESSION_FIELDS + ROW_DROPPED}
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
    if isinstance(out.get("verdict"), dict):
        out["verdict"] = {k: v for k, v in out["verdict"].items() if k not in VERDICT_DROPPED}
    ordered = {k: out[k] for k in ROW_ORDER if k in out}
    return {**ordered, **{k: v for k, v in out.items() if k not in ordered}}


def load_results(path: str) -> tuple:
    """(sessions, results) from a results file, [] and [] if there is none. An older file (a
    bare list of rows, each carrying its session's fields) is converted: each session's shared
    fields become its summary's, and rows without a session are grouped by model."""
    file = Path(path)
    if not file.exists():
        return [], []
    data = json.loads(file.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data.get("sessions", []), data.get("results", [])
    sessions = {}
    for row in data:
        sid = row.get("session_id") or f"named-{row.get('model', 'unknown')}"
        row["session_id"] = sid
        s = sessions.setdefault(sid, {"session_id": sid})
        for field in ("model", "tally_variant", "shared_context"):
            s.setdefault(field, row.get(field))
        contract = (row.get("prompt_readable") or {}).get("output_contract")
        if contract is not None:
            s.setdefault("output_contract", contract)
    return list(sessions.values()), data


def save_results(path: str, sessions: list, results: list) -> None:
    """Writes the results file; every row is slimmed, and a row's output contract is dropped
    when it is its session's."""
    contract = {s["session_id"]: s.get("output_contract") for s in sessions}
    rows = []
    for row in results:
        row = slim_row(row)
        readable = row.get("prompt_readable")
        if isinstance(readable, dict) and readable.get("output_contract") == contract.get(row.get("session_id")):
            row["prompt_readable"] = {k: v for k, v in readable.items() if k != "output_contract"}
        rows.append(row)
    # Written to a temporary file first, then swapped in: a crash or Ctrl+C mid-write leaves the
    # previous file whole instead of a half-written one (which would lose every earlier row).
    tmp = Path(f"{path}.tmp")
    tmp.write_text(json.dumps({"sessions": sessions, "results": rows}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _cost_breakdown(rows: list, correct: int, passed_pairs: int | None) -> dict:
    """What the session cost, split into the one-time cache write of the shared opening and the
    marginal cost of one case, plus cost per correct answer and per passed pair. The split is an
    estimate: OpenRouter reports one cost per call, so the marginal cost is the median cost of
    the calls that only read the cache, and the setup cost is what the writing calls cost above it."""
    ok = [r for r in rows if r.get("error") is None]
    cost = sum(r.get("usd_cost", 0) or 0 for r in rows)
    reads = sorted(r["usd_cost"] for r in ok if not r.get("cache_write_tokens") and r.get("cached_tokens"))
    marginal = reads[len(reads) // 2] if reads else None
    writes = [r["usd_cost"] for r in ok if r.get("cache_write_tokens")]
    setup = round(max(0.0, sum(writes) - len(writes) * marginal), 6) if writes and marginal is not None else None
    out = {"total_usd": round(cost, 6), "avg_per_call_usd": round(cost / len(rows), 6) if rows else None,
           "setup_usd_est": setup, "per_case_marginal_usd_est": round(marginal, 6) if marginal is not None else None,
           "per_correct_answer_usd": round(cost / correct, 6) if correct else None}
    if passed_pairs is not None:
        out["per_passed_pair_usd"] = round(cost / passed_pairs, 6) if passed_pairs else None
    out["note"] = ("All costs are what OpenRouter billed for the original model calls; re-grading a results "
                   "file (--rescore) makes no model call and costs nothing.")
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
    tokens = {k: sum(r.get(k, 0) or 0 for r in rows)
              for k in ("prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens")}
    tokens["reasoning_tokens"] = (sum(r.get("reasoning_tokens") or 0 for r in rows)
                                  if any("reasoning_tokens" in r for r in rows) else "not recorded")
    ok = [r for r in rows if r.get("error") is None]
    correct = outcomes.get("semantic_ok", 0)
    by_type = {}
    for r in ok:
        kind = (cases.get(r["task_id"], {}).get("ground_truth") or {}).get("case_type")
        if kind:
            t = by_type.setdefault(kind, [0, 0])
            t[0] += r["verdict"]["outcome"] == "semantic_ok"
            t[1] += 1
    chance = [cases[r["task_id"]]["ground_truth"]["difficulty"]["chance"] for r in ok
              if (cases.get(r["task_id"], {}).get("ground_truth") or {}).get("difficulty")]
    first = [r["first_token_ms"] for r in ok if r.get("first_token_ms") is not None]
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
        "cost": _cost_breakdown(rows, correct, stats.get("passed") if stats else None),
        "latency": {"mean_total_ms": round(sum(r["latency_ms"] for r in ok) / len(ok)) if ok else None,
                    "mean_first_token_ms": round(sum(first) / len(first)) if first else None},
    }
    if stats:
        summary["paired_accuracy"] = stats
    if sampling is not None and sampling < 3:
        summary["sampling_note"] = (f"{sampling} sample(s) per case: intervals are wide, and a difference between "
                                    f"sessions can be chance. Use -n 3 or more, and --compare for a paired test.")
    return summary


SUMMARY_TOTALS = ("started", "ended", "cases", "calls", "generation_failures", "outcomes", "semantic_ok",
                  "semantic_ok_by_case_type", "mean_chance_baseline", "mean_deviation_score", "error_types",
                  "finish_reasons", "served_by", "tokens", "cost", "latency", "paired_accuracy", "sampling_note")


def rebuild_summaries(case_file: str, sessions: list, results: list) -> list:
    """Every session's summary recomputed from its rows (settings kept, totals refreshed)."""
    raw = json.loads(Path(case_file).read_text(encoding="utf-8")) if Path(case_file).exists() else []
    pairs, cases = pairs_of(raw), {c["case_id"]: c for c in raw}
    out = []
    for s in sessions:
        meta = {k: v for k, v in s.items() if k not in SUMMARY_TOTALS}
        out.append(summarize_session(meta, [r for r in results if r.get("session_id") == s["session_id"]],
                                     pairs, cases))
    return out


def log_readable(result, verdict: dict, path: str, meta: dict, extra: dict | None = None) -> None:
    """Appends one result row to the results file, adding its session's summary (meta: the
    settings the run shares) the first time; totals are filled in when the run ends. extra:
    more row fields (the case's hashes)."""
    sessions, results = load_results(path)
    if not any(s["session_id"] == meta["session_id"] for s in sessions):
        sessions.append(dict(meta))
    row = readable_row(asdict(result), verdict)
    row.update(session_id=meta["session_id"], **(extra or {}))
    results.append(row)
    save_results(path, sessions, results)


def file_hash(path: str) -> str:
    """The first 16 hex digits of a file's sha256."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def case_hashes(case: dict) -> dict:
    """Hashes of what the model saw (the case's input, as the case file writes it) and of its
    answer key's rules, so a result can be matched to the exact case version it was run on."""
    digest = lambda obj: hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return {"input": digest(case.get("input")), "rules": digest((case.get("ground_truth") or {}).get("rules"))}


def code_version() -> dict:
    """The git commit the code ran from, and whether the working tree had uncommitted changes
    (then the commit alone does not reproduce the run; the changed files are listed)."""
    def git(*args):
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True, timeout=20,
                                  cwd=Path(__file__).parent).stdout.rstrip("\n")
        except (OSError, subprocess.SubprocessError):
            return ""
    commit = git("rev-parse", "HEAD").strip()
    # porcelain lines are "XY path": two status letters (either may be a space), a space, the path
    changed = [line[3:] for line in git("status", "--porcelain").splitlines() if line.strip()]
    return {"commit": commit or None, "branch": git("rev-parse", "--abbrev-ref", "HEAD").strip() or None,
            "uncommitted_changes": bool(changed), "changed_files": changed}


@contextmanager
def output_lock(path: str):
    """Only one run writes a results file at a time. Two runs appending to the same file would
    each rewrite it from their own copy and silently drop the other's rows, so a second run
    stops with a message instead. The lock is <file>.lock, removed when the run ends (also on
    Ctrl+C); a run that was killed outright can leave it behind, and the message says so."""
    lock = Path(f"{path}.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        held = lock.read_text(encoding="utf-8", errors="replace").strip() if lock.exists() else "unknown"
        raise SystemExit(f"{path} is in use by another run ({held}).\n"
                         f"Use another --output, or if no run is using it (one was closed mid-run), delete {lock}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"pid {os.getpid()} on {socket.gethostname()}, started "
                f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)
