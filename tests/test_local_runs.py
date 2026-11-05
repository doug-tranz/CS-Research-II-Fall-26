"""
Tests for local runs (LM Studio) and their costs. No model, no network, no GPU needed: a small
stand-in server speaks LM Studio's OpenAI-style streaming API, power is a fixed --watts, and
OpenRouter's prices are given rather than fetched.

    python -m unittest discover tests
"""
import contextlib
import csv
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import backends, cli, paths, pricing, results                     # noqa: E402
from pipeline.cases import expected_message, load_test_cases, read_cases        # noqa: E402
from pipeline.energy import PowerMeter                                          # noqa: E402
from pipeline.harness import TaskSpec, build_messages, cached_chars             # noqa: E402

SONNET = {"prompt": "0.000002", "completion": "0.00001", "input_cache_read": "0.0000002",
          "input_cache_write": "0.0000025"}
NO_CACHE = {"prompt": "0.000001", "completion": "0.000002"}
# the first modality (K1) pair in the finalized case file, variant a then b
PAIR = tuple(c["case_id"] for c in sorted([c for c in read_cases() if c["case_id"].startswith("K1-")][:2],
                                          key=lambda c: c["ground_truth"]["pair"]["variant"]))


class StubLMStudio:
    """A stand-in for LM Studio's server: lists one model, reports its context length, and
    streams `answer` back for every chat completion with the given token counts."""

    def __init__(self, answer="{}", prompt_tokens=1000, completion_tokens=50, context=32768):
        self.answer, self.prompt_tokens, self.completion_tokens = answer, prompt_tokens, completion_tokens
        self.context, self.requests = context, []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/v1/models":
                    return self._json({"data": [{"id": "stub-model"}]})
                if self.path == "/api/v0/models/stub-model":
                    return self._json({"id": "stub-model", "loaded_context_length": stub.context})
                self.send_error(404)

            def do_POST(self):
                stub.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                half = len(stub.answer) // 2
                chunks = [{"id": "c1", "model": "stub-model", "choices": [{"delta": {"content": stub.answer[:half]}}]},
                          {"id": "c1", "choices": [{"delta": {"content": stub.answer[half:]}}]},
                          {"id": "c1", "choices": [{"delta": {}, "finish_reason": "stop"}]},
                          {"id": "c1", "choices": [], "usage": {"prompt_tokens": stub.prompt_tokens,
                                                                "completion_tokens": stub.completion_tokens}}]
                for c in chunks:
                    self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def task(shared="T" * 900, own="state"):
    return TaskSpec("t1", own, "register me", "CONTRACT" * 10, shared)


class PricingTests(unittest.TestCase):
    def test_projection_writes_the_cache_once_then_reads_it(self):
        p = pricing.Projection("anthropic/claude-sonnet-5.5", SONNET)
        t = task()
        cached, own = cached_chars(t)
        first, second = p.call(t, 1000, 100), p.call(t, 1000, 100)
        share = cached / (cached + own)
        self.assertAlmostEqual(first, 1000 * share * 2.5e-6 + 1000 * (1 - share) * 2e-6 + 100 * 1e-5, places=7)
        self.assertAlmostEqual(second, 1000 * share * 2e-7 + 1000 * (1 - share) * 2e-6 + 100 * 1e-5, places=7)

    def test_a_model_without_cache_prices_pays_full_input_every_call(self):
        r = pricing.rates(NO_CACHE)
        self.assertEqual(r["cache_read"], r["input"])
        self.assertEqual(r["cache_write"], r["input"])

    def test_cache_read_without_a_write_price_assumes_anthropic_rate(self):
        r = pricing.rates({"prompt": "0.000001", "completion": "0.000002", "input_cache_read": "0.0000001"})
        self.assertAlmostEqual(r["cache_write"], 1.25e-6)

    def test_paid_run_needs_a_budget_or_yes_when_it_cannot_ask(self):
        with mock.patch("sys.stdin", io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(pricing.confirm_spend({"usd": 1.0}, None, False))
            self.assertTrue(pricing.confirm_spend({"usd": 1.0}, None, True))
            self.assertTrue(pricing.confirm_spend({"usd": 1.0}, 2.0, False))
            self.assertFalse(pricing.confirm_spend({"usd": 3.0}, 2.0, False))


class EnergyTests(unittest.TestCase):
    def test_fixed_watts_times_duration(self):
        meter = PowerMeter(fixed_watts=120, extra_watts=30)
        with mock.patch("pipeline.energy.time.perf_counter", side_effect=[0.0, 36.0]):
            meter.start()
            reading = meter.stop()
        self.assertEqual(reading.avg_watts, 150)
        self.assertAlmostEqual(reading.wh, 1.5)               # 150 W for 36 s
        self.assertAlmostEqual(reading.usd(0.20), 0.0003)


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubLMStudio(answer='{"id": "x"}', prompt_tokens=1200, completion_tokens=40)

    def tearDown(self):
        self.stub.close()

    def backend(self):
        b = backends.LMStudioBackend(PowerMeter(fixed_watts=100), 0.2, url=self.stub.url, max_tokens=500)
        b.check_model("stub-model")
        return b

    def test_streams_the_answer_with_tokens_and_energy(self):
        call = self.backend().call("stub-model", build_messages(task()))
        self.assertEqual(call.content, '{"id": "x"}')
        self.assertEqual((call.prompt_tokens, call.completion_tokens), (1200, 40))
        self.assertEqual(call.finish_reason, "stop")
        self.assertEqual(call.usd_cost, 0.0)
        self.assertEqual(call.avg_watts, 100)
        self.assertGreater(call.energy_wh, 0)

    def test_sends_plain_string_messages_without_cache_markers(self):
        self.backend().call("stub-model", build_messages(task()))
        sent = self.stub.requests[0]
        self.assertTrue(all(isinstance(m["content"], str) for m in sent["messages"]))
        self.assertNotIn("cache_control", json.dumps(sent))
        self.assertEqual(sent["stream_options"], {"include_usage": True})
        self.assertEqual(sent["max_tokens"], 500)

    def test_unknown_model_fails_before_the_run(self):
        b = backends.LMStudioBackend(PowerMeter(fixed_watts=100), 0.2, url=self.stub.url)
        with self.assertRaisesRegex(RuntimeError, "no model 'other'"):
            b.check_model("other")

    def test_a_call_that_fills_the_context_window_fails(self):
        self.stub.context = 1200
        with self.assertRaisesRegex(RuntimeError, "context window"):
            self.backend().call("stub-model", build_messages(task()))

    def test_server_down_says_how_to_start_it(self):
        b = backends.LMStudioBackend(PowerMeter(fixed_watts=100), 0.2, url="http://127.0.0.1:9")
        with self.assertRaisesRegex(RuntimeError, "Start Server"):
            b.models()


class EndToEndTests(unittest.TestCase):
    """The command line, run locally against the stand-in server, on one real pair."""

    def test_local_run_records_energy_and_projected_cost(self):
        case = load_test_cases(task_ids={PAIR[0]})[0]
        right = expected_message(case, case["ground_truth"]["examples"]["valid"][0])
        stub = StubLMStudio(answer=right, prompt_tokens=20000, completion_tokens=300, context=200000)
        out = Path(tempfile.mkdtemp()) / "local.json"
        argv = ["run_semantic_eval.py", "--backend", "lmstudio", "--model", "stub-model", "--lmstudio-url", stub.url,
                "--watts", "150", "--price-as", "anthropic/claude-sonnet-5.5", "--subjects", "auto",
                "--task-ids", PAIR[0], "--output", str(out)]
        try:
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch("pipeline.pricing.lookup_price", return_value=(SONNET, None)), \
                    mock.patch("pipeline.cli.lookup_price", return_value=(SONNET, None)), \
                    contextlib.redirect_stdout(io.StringIO()) as printed:
                cli.main()
        finally:
            stub.close()
        data = json.loads(out.read_text(encoding="utf-8"))
        row, summary = data["results"][0], data["sessions"][0]
        self.assertEqual(row["verdict"]["outcome"], "semantic_ok", printed.getvalue())
        for field in ("energy_wh", "avg_watts", "energy_usd", "projected_openrouter_usd"):
            self.assertIn(field, row)
        self.assertEqual(summary["backend"], "lmstudio")
        self.assertTrue(summary["tally_subjects"])
        self.assertEqual(summary["local_cost"]["projected_openrouter"]["model"], "anthropic/claude-sonnet-5.5")
        self.assertIn("projected_whole_tally_estimate_usd", summary)
        # the opening held only the chosen subjects
        opening = stub.requests[0]["messages"][1]["content"]
        self.assertLess(len(opening), 200_000)

    def test_session_per_pair_runs_each_pair_alone_and_totals_the_batch(self):
        stub = StubLMStudio(answer="{}", prompt_tokens=15000, completion_tokens=200, context=200000)
        out = Path(tempfile.mkdtemp()) / "batch.json"
        argv = ["run_semantic_eval.py", "--backend", "lmstudio", "--model", "stub-model", "--lmstudio-url", stub.url,
                "--watts", "100", "--price-as", "anthropic/claude-sonnet-5.5", "--pairs", "2",
                "--session-per-pair", "--tally", "compact", "--output", str(out)]
        prices = (SONNET, None)
        try:
            with mock.patch.object(sys, "argv", argv), mock.patch("pipeline.pricing.lookup_price", return_value=prices), \
                    mock.patch("pipeline.cli.lookup_price", return_value=prices), \
                    contextlib.redirect_stdout(io.StringIO()) as printed:
                cli.main()
            with mock.patch.object(sys, "argv", ["run_semantic_eval.py", "--batch-report", "--output", str(out)]), \
                    contextlib.redirect_stdout(io.StringIO()) as report:
                cli.main()
        finally:
            stub.close()
        data = json.loads(out.read_text(encoding="utf-8"))
        sessions = data["sessions"]
        self.assertEqual(len(sessions), 2)                                  # one session per pair
        self.assertEqual(len({s["batch"]["id"] for s in sessions}), 1)      # one batch
        self.assertEqual(len({s["session_id"] for s in sessions}), 2)
        self.assertEqual(len(data["results"]), 4)
        for s in sessions:                                                  # each opening: only its pair's subjects
            self.assertTrue(s["tally_subjects"])
            self.assertEqual(len(s["cases"]), 2)
        self.assertIsNotNone(sessions[0]["batch"]["whole_tally_estimate_usd"])
        for text in (printed.getvalue(), report.getvalue()):
            self.assertIn("2 sessions, 4 calls", text)
            self.assertIn("electricity:", text)
            self.assertIn("projected on OpenRouter as anthropic/claude-sonnet-5.5", text)
            self.assertIn("one whole-tally session", text)

    def test_resume_continues_the_batch_and_skips_finished_pairs(self):
        stub = StubLMStudio(answer="{}", prompt_tokens=15000, completion_tokens=200, context=200000)
        out = Path(tempfile.mkdtemp()) / "resume.json"
        argv = ["run_semantic_eval.py", "--backend", "lmstudio", "--model", "stub-model", "--lmstudio-url", stub.url,
                "--watts", "100", "--price-as", "anthropic/claude-sonnet-5.5", "--pairs", "2",
                "--session-per-pair", "--tally", "compact", "--output", str(out)]
        prices = (SONNET, None)

        def run(extra=()):
            with mock.patch.object(sys, "argv", argv + list(extra)), \
                    mock.patch("pipeline.pricing.lookup_price", return_value=prices), \
                    mock.patch("pipeline.cli.lookup_price", return_value=prices), \
                    contextlib.redirect_stdout(io.StringIO()) as printed:
                cli.main()
            return printed.getvalue()
        try:
            run()
            data = json.loads(out.read_text(encoding="utf-8"))
            second = data["sessions"][1]["session_id"]          # as if the run died during session 2
            data["sessions"] = data["sessions"][:1]
            data["results"] = [r for r in data["results"] if r["session_id"] != second]
            out.write_text(json.dumps(data), encoding="utf-8")
            printed = run(["--resume"])
            self.assertIn("1 of 2 sessions already done", printed)
            self.assertEqual(len(stub.requests), 2 + 2 + 2)      # first run 4 calls, resume only 2
        finally:
            stub.close()
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len({s["batch"]["id"] for s in data["sessions"]}), 1)   # still one batch
        self.assertEqual(len(data["results"]), 4)
        self.assertTrue(data["sessions"][1]["session_id"].endswith("-002"))

    def test_a_course_with_an_unchecked_condition_is_found_and_left_out(self):
        from pipeline.finalize import restricted_courses
        self.assertEqual(restricted_courses({"notes": "PR06310 needs 60 sh; JRN02317 needs 45 sh. "
                                                      "Four courses (12 sh): CMS04210"}), {"PR06310", "JRN02317"})
        self.assertEqual(restricted_courses([{"notes": "they need COMP01112 and 90 sh overall"},
                                             {"notes": "CS99300 and CS99310 need the instructor's permission"}]),
                         {"CS99300", "CS99310"})
        for c in read_cases():                                          # none of them is a case's course
            self.assertNotIn("PR06310", c["candidate_courses"] if c["input"].get("program") == "advertising-ba" else [])

    def test_pairs_spread_over_the_checks(self):
        from collections import Counter
        from pipeline.batch import spread_pairs
        code = {c["case_id"]: c["ground_truth"]["checks"][0] for c in read_cases()}
        ten = spread_pairs(read_cases(), 10)
        self.assertEqual(len({code[cid] for cid in ten}), 10)                # ten checks, one pair each
        self.assertEqual(len({code[cid][0] for cid in ten}), 7)              # reaching all seven groups
        hundred = spread_pairs(read_cases(), 100)
        self.assertEqual(len(hundred), 200)
        self.assertEqual(set(Counter(code[cid] for cid in hundred).values()), {4, 6})   # 100 pairs over 43 checks
        self.assertEqual(hundred[:20], ten)                                  # a smaller n is a subset

    def test_a_row_keeps_its_reasoning_and_drops_an_empty_one(self):
        kept = results.slim_row({"task_id": "t", "reasoning": "because", "verdict": {}})
        self.assertEqual(kept["reasoning"], "because")
        self.assertNotIn("reasoning", results.slim_row({"task_id": "t", "reasoning": "", "verdict": {}}))

    def test_cloud_rows_have_no_local_fields(self):
        row = results.slim_row({"task_id": "t", "energy_wh": None, "avg_watts": None, "energy_usd": None,
                                "usd_cost": 0.1, "verdict": {}})
        self.assertNotIn("energy_wh", row)


class ResultsFileTests(unittest.TestCase):
    def test_analysis_breaks_down_by_check_change_and_samples(self):
        truth = lambda variant: {"checks": ["T1", "O1"], "difficulty": {"chance": 0.5}, "credit": {"owner": "Tyler Henry"},
                                 "pair": {"id": "p/1", "variant": variant, "difference": {"where": "prompt"}}}
        cases = {"A-a": {"ground_truth": truth("a")}, "A-b": {"ground_truth": truth("b")}}
        row = lambda cid, run, ok, crn: {"task_id": cid, "run_index": run, "error": None, "transcript_courses": 12,
                                         "verdict": {"outcome": "semantic_ok" if ok else "semantic_error"},
                                         "parsed_output": {"body": {"sections": [{"crn": crn}]}}}
        rows = [row("A-a", 0, True, "1"), row("A-a", 1, False, "2"), row("A-b", 0, True, "3"), row("A-b", 1, True, "3")]
        got = results.analysis(rows, {"p/1": {"a": "A-a", "b": "A-b"}}, cases)
        t1 = got["semantic_ok_by_check"]["T1"]
        self.assertEqual((t1["count"], t1["mean_chance"], t1["lift_over_chance"]), ("3/4", 0.5, 0.25))
        self.assertEqual(got["semantic_ok_by_group"]["Temporal reasoning"]["count"], "3/4")
        self.assertEqual(got["semantic_ok_by_owner"]["Tyler Henry"]["count"], "3/4")
        self.assertEqual(list(got["semantic_ok_by_transcript_size"]), ["11-20 courses"])
        self.assertEqual(got["paired_by_change"]["prompt"]["passed"], 1)       # run 0 both right, run 1 not
        stable = got["stability"]
        self.assertEqual(stable["same_answer_every_sample"]["count"], "1/2")   # A-b answered 3 twice; A-a changed
        self.assertEqual(stable["flip_rate_by_check"]["T1"]["count"], "1/2")
        self.assertEqual(got["errors_by_check"]["T1"], {"semantic_error": 1})
        self.assertEqual(got["ignored_change_by_check"]["T1"]["count"], "0/2")       # the twins never answered alike
        self.assertEqual(got["empty_cart"]["registered_nothing_when_wrong"]["count"], "0/4")

    def test_analysis_flags_same_answer_empty_carts_and_cart_size(self):
        gt = lambda v, mx: {"checks": ["K7"], "rules": {"count": {"min": 0 if mx == 0 else 1, "max": mx}},
                            "pair": {"id": "p", "variant": v}}
        cases = {"a": {"ground_truth": gt("a", 1)}, "b": {"ground_truth": gt("b", 0)}}
        row = lambda cid, crns, ok, think: {"task_id": cid, "run_index": 0, "error": None, "reasoning_tokens": think,
                                            "verdict": {"outcome": "semantic_ok" if ok else "semantic_error",
                                                        "error_type": None if ok else "registered_anyway"},
                                            "parsed_output": {"body": {"sections": [{"crn": c} for c in crns]}}}
        got = results.analysis([row("a", ["1"], True, 100), row("b", ["1"], False, 300)], {"p": {"a": "a", "b": "b"}}, cases)
        self.assertEqual(got["ignored_change_by_check"]["K7"]["count"], "1/1")       # same cart for both twins
        self.assertEqual(got["errors_by_check"]["K7"], {"registered_anyway": 1})
        self.assertEqual(got["empty_cart"]["registered_nothing_when_right"]["count"], "0/1")
        self.assertEqual(got["sections_vs_requested_by_check"]["K7"], {"fewer": 0, "as_requested": 1, "more": 1})
        self.assertEqual(got["reasoning_tokens_by_outcome"]["wrong"]["mean"], 300)

    def test_export_csv_writes_one_flat_row_per_call(self):
        out = Path(tempfile.mkdtemp()) / "all.csv"
        res = Path(out.parent) / "r.json"
        cid = PAIR[0]
        results.save_results(str(res), [{"session_id": "s", "model": "m",
                                         "generation_settings": {"reasoning": {"effort": "low"}}}],
                             [{"task_id": cid, "session_id": "s", "run_index": 0, "ts": "2026-11-05T00:00:00Z", "error": None, "usd_cost": 0.01,
                               "verdict": {"outcome": "semantic_ok"}, "parsed_output": {"body": {"sections": []}}}])
        from pipeline.reports import CSV_COLUMNS, export_csv
        self.assertEqual(export_csv(str(paths.CASES), str(out), [str(res)]), (1, 1))
        with open(out, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(tuple(rows[0]), CSV_COLUMNS)
        self.assertEqual((rows[0]["check"], rows[0]["effort"], rows[0]["semantic_ok"], rows[0]["registered_nothing"]),
                         ("K1", "low", "1", "1"))
        with open(out.with_name("all_sessions.csv"), encoding="utf-8") as f:
            session = next(csv.DictReader(f))
        self.assertEqual((session["model"], session["effort"], session["semantic_ok_rate"], session["ok_K1"]),
                         ("m", "low", "1.0", "1.0"))

    def test_save_is_atomic_and_leaves_no_temp_file(self):
        path = Path(tempfile.mkdtemp()) / "r.json"
        results.save_results(str(path), [{"session_id": "s"}], [])
        self.assertEqual(json.loads(path.read_text())["sessions"], [{"session_id": "s"}])
        self.assertFalse(Path(f"{path}.tmp").exists())


if __name__ == "__main__":
    unittest.main()
