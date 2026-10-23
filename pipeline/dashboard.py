"""
dashboard.py
------------
A live view of a run, in the browser. It serves one page (dashboard.html, next to this file)
and the numbers behind it, recomputed from the results file on every request, so it follows
a run while it is going (the file is rewritten after every call) and shows a finished one.

    python -m pipeline.dashboard --output C:/Users/Khalil/csr2_runs/llama_200.json
    (opens http://localhost:8765), or add --dashboard to a run_semantic_eval.py command

Nothing is sent anywhere: the server listens on this machine only (127.0.0.1) and reads the
results file and the case file. It makes no model call and spends nothing.

What it shows, for one batch (a --session-per-pair run) or one plain session, picked from a
menu: progress and the time left, accuracy and paired accuracy, the electricity used, the
projected OpenRouter cost, outcomes by pair family, seconds and watts per call, and the
latest calls.
"""
import argparse
import json
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .batch import batch_totals
from .cases import read_cases
from .pairs import pair_stats, pairs_of
from .paths import CASES, DEFAULT_OUTPUT, shown

PAGE = Path(__file__).with_name("dashboard.html")

# Outcomes in the order they are stacked and colored (the page assigns the colors in this order).
OUTCOMES = ("semantic_ok", "semantic_error", "consistency_error", "format_error", "generation_failed")
OUTCOME_LABELS = {"semantic_ok": "Correct", "semantic_error": "Wrong cart (Tier 4)",
                  "consistency_error": "Breaks a rule (Tier 3)", "format_error": "Bad format (Tiers 1-2)",
                  "generation_failed": "Call failed"}


def _outcome(row: dict) -> str:
    """A row's outcome, with the two format tiers counted together."""
    outcome = row["verdict"]["outcome"]
    return "format_error" if outcome in ("syntactic_error", "schema_error") else outcome


def _load(path: str) -> tuple:
    """(sessions, results) from a results file that may be mid-write: retried briefly."""
    for _ in range(5):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return data.get("sessions", []), data.get("results", [])
        except FileNotFoundError:
            return [], []
        except (json.JSONDecodeError, PermissionError, OSError):
            time.sleep(0.2)            # the run is replacing the file right now
    return [], []


def runs_in(sessions: list) -> list:
    """The runs a results file holds, newest first: each batch once, and each session that is
    not part of a batch on its own. Each is {"key", "label", "kind"}."""
    runs, seen = [], set()
    for s in sessions:
        batch = s.get("batch")
        key = batch["id"] if batch else s["session_id"]
        if key in seen:
            continue
        seen.add(key)
        count = batch["sessions"] if batch else 0
        label = (f"{key} - {s.get('model')} ({count} session{'' if count == 1 else 's'})" if batch
                 else f"{key} - {s.get('model')}")
        runs.append({"key": key, "label": label, "kind": "batch" if batch else "session"})
    return runs[::-1]


def snapshot(results_file: str, case_file: str, key: str | None, case_types: dict) -> dict:
    """Everything the page shows for one run (the newest when `key` is None)."""
    sessions, results = _load(results_file)
    runs = runs_in(sessions)
    if not runs:
        return {"file": shown(results_file), "runs": [], "empty": True}
    run = next((r for r in runs if r["key"] == key), runs[0])
    mine = [s for s in sessions if ((s.get("batch") or {}).get("id") or s["session_id"]) == run["key"]]
    ids = {s["session_id"] for s in mine}
    rows = sorted((r for r in results if r.get("session_id") in ids), key=lambda r: r["ts"])
    first = mine[0]
    n = first.get("samples_per_case", 1)
    planned = (first["batch"]["sessions"] * 2 * n) if run["kind"] == "batch" else len(first.get("cases", [])) * n

    if run["kind"] == "batch":
        totals = batch_totals(case_file, sessions, results, run["key"])
    else:
        outcomes = {(r["task_id"], r["run_index"]): r["verdict"]["outcome"] for r in rows}
        sum_of = lambda k: sum(r[k] for r in rows if r.get(k) is not None)
        local = any("energy_wh" in r for r in rows)
        totals = {"calls": len(rows), "failed_calls": sum(r.get("error") is not None for r in rows),
                  "semantic_ok": sum(r["verdict"]["outcome"] == "semantic_ok" for r in rows),
                  "paired_accuracy": pair_stats(pairs_of(read_cases(case_file)), outcomes),
                  "billed_usd": sum_of("usd_cost"),
                  "energy_wh": sum_of("energy_wh") if local else None,
                  "energy_usd": sum_of("energy_usd") if local else None,
                  "projected_usd": sum_of("projected_openrouter_usd") if local else None,
                  "price_as": first.get("price_as"),
                  "whole_tally_estimate_usd": first.get("projected_whole_tally_estimate_usd"),
                  "kwh_price_usd": first.get("kwh_price_usd")}

    calls = [{"i": k + 1, "task_id": r["task_id"], "family": case_types.get(r["task_id"], "other"),
              "outcome": _outcome(r), "detail": (r["verdict"].get("detail") or "")[:160],
              "seconds": round(r.get("latency_ms", 0) / 1000, 1),
              "watts": r.get("avg_watts"), "wh": r.get("energy_wh"),
              "prompt_tokens": r.get("prompt_tokens"), "completion_tokens": r.get("completion_tokens"),
              "projected_usd": r.get("projected_openrouter_usd"), "ts": r["ts"]} for k, r in enumerate(rows)]
    families = {}
    for c in calls:
        counts = families.setdefault(c["family"], {o: 0 for o in OUTCOMES})
        counts[c["outcome"]] = counts.get(c["outcome"], 0) + 1

    # time left: the median of the last 10 calls' durations times the calls still to make, less
    # what the call in progress has already run (a row's ts is when its call started)
    recent = sorted(c["seconds"] for c in calls[-10:])
    per_call = recent[len(recent) // 2] if recent else None
    left = max(0, planned - len(calls))
    last_ts = datetime.fromisoformat(rows[-1]["ts"]) if rows else None
    idle_s = (datetime.now(last_ts.tzinfo) - last_ts).total_seconds() if last_ts else None
    if idle_s is not None and calls:
        idle_s = max(0.0, idle_s - calls[-1]["seconds"])         # since the last call FINISHED
    eta = max(0, round(left * per_call - (idle_s or 0))) if per_call and left else 0
    return {
        "file": shown(results_file), "runs": runs, "run": run, "model": first.get("model"),
        "backend": first.get("backend"), "tally": first.get("tally_variant"),
        "planned_calls": planned, "calls_done": len(calls), "calls_left": left,
        "seconds_per_call": per_call, "eta_seconds": eta,
        "seconds_since_last_call": round(idle_s) if idle_s is not None else None,
        "started": rows[0]["ts"] if rows else None,
        "totals": totals, "outcomes": list(OUTCOMES), "outcome_labels": OUTCOME_LABELS,
        "families": families, "calls": calls,
    }


class Handler(BaseHTTPRequestHandler):
    """GET / -> the page; GET /data?run=<key> -> snapshot() as JSON."""
    results_file = case_file = None
    case_types: dict = {}

    def log_message(self, *args):           # quiet: the page polls every few seconds
        pass

    def _send(self, body: bytes, kind: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
        if url.path == "/data":
            key = (parse_qs(url.query).get("run") or [None])[0]
            data = snapshot(self.results_file, self.case_file, key, self.case_types)
            return self._send(json.dumps(data).encode("utf-8"), "application/json")
        self.send_error(404)


def serve(results_file: str, case_file: str, port: int = 8765, browser: bool = True, background: bool = False):
    """Serves the dashboard for one results file at http://localhost:<port>. background: in a
    daemon thread (run_semantic_eval.py --dashboard), so it lives exactly as long as the run."""
    Handler.results_file, Handler.case_file = results_file, case_file
    Handler.case_types = {c["case_id"]: (c.get("ground_truth") or {}).get("case_type", "other")
                          for c in read_cases(case_file)}
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"dashboard for {shown(results_file)} at {url}" + ("" if background else " (Ctrl+C to stop)"))
    if browser:
        webbrowser.open(url)
    if background:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    """Command line: serve the dashboard for one results file."""
    ap = argparse.ArgumentParser(description="A live dashboard for a results file (no model call).")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT), help="the results file to show")
    ap.add_argument("--test-file", default=str(CASES), help="the case file the run used")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="do not open the page in a browser")
    args = ap.parse_args()
    serve(args.output, args.test_file, args.port, browser=not args.no_browser)


if __name__ == "__main__":
    main()
