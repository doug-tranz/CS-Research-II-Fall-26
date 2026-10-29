"""
Tests for --provider (one OpenRouter endpoint) and --batch-api (OpenRouter's Batch API). No
network and no money: OpenRouter's price list, the live call and the batch endpoints are stubbed.

    python -m unittest discover tests
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import backends, cli, pricing                                     # noqa: E402
from pipeline.backends import OpenRouterBackend, RawCall                        # noqa: E402

PAIR = ("pair-modality-marketing-bs-a", "pair-modality-marketing-bs-b")
ENDPOINTS = {"data": {"endpoints": [
    {"tag": "deepinfra/fp8", "pricing": {"prompt": "0.00000014", "completion": "0.00000042",
                                         "input_cache_read": "0.0000000042"}},
    {"tag": "openinference/fp4", "pricing": {"prompt": "0.000000198", "completion": "0.00000001",
                                             "input_cache_read": "0.000000149"}}]}}
SONNET = {"prompt": "0.000002", "completion": "0.00001", "input_cache_read": "0.0000001",
          "input_cache_write": "0.0000025", "input_cache_write_1h": "0.000004"}


def reply(payload, status=200):
    return SimpleNamespace(status_code=status, content=b"x", text=json.dumps(payload), json=lambda: payload)


def batch_entry(i, text, cost=0.01):
    return {"custom_id": f"call-{i:04d}", "response": {"body": {
        "id": f"gen-{i}", "model": "m", "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "cost": cost,
                  "prompt_tokens_details": {"cached_tokens": 90}}}}}


class ProviderTests(unittest.TestCase):
    def test_settings_pin_one_endpoint_without_fallback(self):
        s = backends.generation_settings(provider="deepinfra/fp8")
        self.assertEqual(s["provider"], {"only": ["deepinfra/fp8"], "allow_fallbacks": False})
        self.assertNotIn("provider", backends.generation_settings())

    def test_price_is_the_pinned_endpoints_not_the_most_expensive(self):
        with mock.patch("pipeline.pricing.requests.get", return_value=reply(ENDPOINTS)):
            worst = pricing.model_price("deepseek/x", 400000)
            pinned = pricing.model_price("deepseek/x", 400000, "deepinfra/fp8")
        self.assertAlmostEqual(worst["input_cache_read"], 0.000000149)
        self.assertAlmostEqual(pinned["input_cache_read"], 0.0000000042)

    def test_an_unknown_tag_names_the_models_tags(self):
        with mock.patch("pipeline.pricing.requests.get", return_value=reply(ENDPOINTS)):
            price, why = pricing.lookup_price("deepseek/x", 400000, "deepinfra")
        self.assertIsNone(price)
        self.assertIn("deepinfra/fp8, openinference/fp4", why)

    def test_one_hour_cache_uses_its_own_write_price(self):
        self.assertAlmostEqual(pricing.rates(SONNET, "1h")["cache_write"], 0.000004)
        self.assertAlmostEqual(pricing.rates(SONNET)["cache_write"], 0.0000025)


class RetryTests(unittest.TestCase):
    def post(self, *statuses):
        """A stubbed requests.post answering each status in turn (200: a one-chunk stream)."""
        def answer(status):
            r = mock.MagicMock(status_code=status, content=b"x", text="busy")
            r.json.return_value = {"error": {"code": status, "message": "busy"}}
            r.iter_lines.return_value = iter(['data: {"choices": [{"delta": {"content": "ok"}, '
                                              '"finish_reason": "stop"}]}', "data: [DONE]"])
            r.__enter__.return_value = r
            return r
        return mock.patch("pipeline.backends.requests.post", side_effect=[answer(s) for s in statuses])

    def test_a_busy_endpoint_is_tried_again(self):
        b = OpenRouterBackend(api_key="k", provider="openai/flex")
        with self.post(429, 503, 200) as post, mock.patch("pipeline.backends.time.sleep") as sleep, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(b._stream("m", []).content, "ok")
        self.assertEqual(post.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], list(b.RETRY_SECONDS[:2]))

    def test_a_refusal_inside_the_stream_is_tried_again(self):
        busy = mock.MagicMock(status_code=200)
        busy.iter_lines.return_value = iter(['data: {"error": {"code": 502, "message": "Flex processing is '
                                             'temporarily unavailable"}}'])
        busy.__enter__.return_value = busy
        b = OpenRouterBackend(api_key="k", provider="openai/flex")
        with self.post(200) as post, mock.patch("pipeline.backends.time.sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            post.side_effect = [busy, *post.side_effect]
            self.assertEqual(b._stream("m", []).content, "ok")
        self.assertEqual(post.call_count, 2)

    def test_other_errors_and_a_last_refusal_are_not_retried(self):
        b = OpenRouterBackend(api_key="k")
        with self.post(400) as post, mock.patch("pipeline.backends.time.sleep"), \
                self.assertRaisesRegex(RuntimeError, "OpenRouter 400"):
            b._stream("m", [])
        self.assertEqual(post.call_count, 1)
        b.RETRY_SECONDS = (1,)
        with self.post(429, 429), mock.patch("pipeline.backends.time.sleep"), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "OpenRouter 429"):
            b._stream("m", [])


class BatchEstimateTests(unittest.TestCase):
    def estimate(self, batch_api):
        session = SimpleNamespace(raw=False, ttl="5m")
        tasks = [SimpleNamespace(shared_context="x" * 620000, output_contract="", inputs="y" * 2000, intent="")] * 10
        with mock.patch("pipeline.pricing.lookup_price", return_value=(SONNET, None)):
            return pricing.estimate_cost("m", session, tasks, 1, None, answer_tokens=1000, batch_api=batch_api)["usd"]

    def test_calls_after_the_first_cost_half(self):
        live, batch = self.estimate(False), self.estimate(True)
        first = 384400 * 0.0000025 + 1000 * 0.000002 + 1000 * 0.00001     # cache write, own turn, answer
        self.assertAlmostEqual(live - first, 2 * (batch - first), places=4)

    def test_reused_answers_are_not_priced(self):
        session = SimpleNamespace(raw=False, ttl="5m")
        tasks = [SimpleNamespace(shared_context="x" * 620000, output_contract="", inputs="y" * 2000, intent="")] * 10
        with mock.patch("pipeline.pricing.lookup_price", return_value=(SONNET, None)):
            est = lambda **kw: pricing.estimate_cost("m", session, tasks, 3, None, answer_tokens=1000, **kw)
            all_calls, fewer, none = est(), est(reused=10), est(reused=30)
        per_read = (all_calls["usd"] - fewer["usd"]) / 10
        self.assertAlmostEqual(per_read, 384400 * 0.0000001 + 1000 * 0.000002 + 1000 * 0.00001, places=4)
        self.assertIn("10 more answer(s) reused", fewer["text"])
        self.assertEqual(none["usd"], 0.0)


class BatchBackendTests(unittest.TestCase):
    def backend(self, **kw):
        b = OpenRouterBackend(api_key="k", batch_api=True, **kw)
        b.BATCH_POLL_SECONDS = 0
        return b

    def test_first_call_live_rest_batched_and_handed_out_in_order(self):
        calls = [[{"role": "user", "content": str(i)}] for i in range(3)]
        b = self.backend(provider="anthropic")
        done = {"id": "b1", "status": "completed", "provider": "Anthropic",
                "results": [batch_entry(1, "one"), batch_entry(0, "zero")]}
        with mock.patch.object(b, "_stream", return_value=RawCall(content="live", latency_ms=5.0)) as live, \
                mock.patch("pipeline.backends.requests.post", return_value=reply({"id": "b1", "status": "validating"},
                                                                                202)) as post, \
                mock.patch("pipeline.backends.requests.get", return_value=reply(done)), \
                contextlib.redirect_stdout(io.StringIO()):
            b.prefetch("m", calls)
        live.assert_called_once_with("m", calls[0])
        submit = post.call_args.kwargs["json"]
        self.assertEqual(submit["provider"], {"only": ["anthropic"], "allow_fallbacks": False})
        self.assertNotIn("stream", submit["requests"][0]["body"])
        self.assertEqual([r["body"]["messages"] for r in submit["requests"]], calls[1:])
        self.assertEqual([b.call("m", c).content for c in calls], ["live", "zero", "one"])
        self.assertEqual([i["id"] for i in b.batch_info], ["b1"])

    def test_calls_too_big_for_one_batch_are_split_in_order(self):
        calls = [[{"role": "user", "content": "x" * 100 + str(i)}] for i in range(7)]
        chunks = backends.batch_chunks(calls, 350)          # ~120 bytes per call: 2 per batch
        self.assertEqual([len(c) for c in chunks], [2, 2, 2, 1])
        self.assertEqual([m for c in chunks for m in c], calls)
        self.assertEqual(backends.batch_chunks([], 350), [])

    def test_each_chunk_is_its_own_batch(self):
        calls = [[{"role": "user", "content": "x" * 100 + str(i)}] for i in range(5)]
        b = self.backend()
        b.BATCH_MAX_BYTES = 350
        ids = iter(["b1", "b2"])

        def submit(url, headers, json, timeout):
            return reply({"id": next(ids), "status": "validating", "n": len(json["requests"])}, 202)

        def poll(url, headers, timeout):
            return reply({"id": url.rsplit("/", 1)[1], "status": "completed",
                          "results": [batch_entry(i, url[-2:] + str(i)) for i in range(2)]})
        with mock.patch.object(b, "_stream", return_value=RawCall(content="live")), \
                mock.patch("pipeline.backends.requests.post", side_effect=submit) as post, \
                mock.patch("pipeline.backends.requests.get", side_effect=poll), \
                contextlib.redirect_stdout(io.StringIO()):
            b.prefetch("m", calls)
        self.assertEqual([len(c.kwargs["json"]["requests"]) for c in post.call_args_list], [2, 2])
        self.assertEqual([b.call("m", c).content for c in calls], ["live", "b10", "b11", "b20", "b21"])
        self.assertEqual([i["id"] for i in b.batch_info], ["b1", "b2"])

    def test_a_failed_request_or_batch_becomes_a_failed_call(self):
        calls = [[{"role": "user", "content": str(i)}] for i in range(3)]
        b = self.backend()
        done = {"id": "b2", "status": "completed",
                "results": [batch_entry(0, "ok"), {"custom_id": "call-0001", "error": {"message": "overloaded"}}]}
        with mock.patch.object(b, "_stream", side_effect=RuntimeError("OpenRouter 503")), \
                mock.patch("pipeline.backends.requests.post", return_value=reply({"id": "b2", "status": "in_progress"})), \
                mock.patch("pipeline.backends.requests.get", return_value=reply(done)), \
                contextlib.redirect_stdout(io.StringIO()):
            b.prefetch("m", calls)
        with self.assertRaisesRegex(RuntimeError, "503"):
            b.call("m", calls[0])
        self.assertEqual(b.call("m", calls[1]).content, "ok")
        with self.assertRaisesRegex(RuntimeError, "overloaded"):
            b.call("m", calls[2])

    def test_a_stopped_run_collects_what_it_paid_for_when_run_again(self):
        calls = [[{"role": "user", "content": str(i)}] for i in range(3)]
        done = {"id": "b7", "status": "completed", "results": [batch_entry(0, "zero"), batch_entry(1, "one")]}
        with tempfile.TemporaryDirectory() as tmp:
            pending = str(Path(tmp) / "r.json.pending-batch.json")
            b = self.backend()
            with mock.patch.object(b, "_stream", return_value=RawCall(content="live", usd_cost=1.5)), \
                    mock.patch("pipeline.backends.requests.post", return_value=reply({"id": "b7", "status": "validating"},
                                                                                    202)), \
                    mock.patch("pipeline.backends.requests.get", side_effect=KeyboardInterrupt), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
                b.prefetch("m", calls, pending)                 # stopped while waiting for the batch
            again = self.backend()
            with mock.patch.object(again, "_stream") as live, \
                    mock.patch("pipeline.backends.requests.post") as post, \
                    mock.patch("pipeline.backends.requests.get", return_value=reply(done)) as poll, \
                    contextlib.redirect_stdout(io.StringIO()):
                again.prefetch("m", calls, pending)
            live.assert_not_called()                            # the first call is not paid for twice
            post.assert_not_called()                            # nor is the batch
            self.assertTrue(poll.call_args.args[0].endswith("/b7"))
            first = again.call("m", calls[0])
            self.assertEqual((first.content, first.usd_cost), ("live", 1.5))
            self.assertEqual([again.call("m", c).content for c in calls[1:]], ["zero", "one"])
            other = self.backend(effort="high")                 # different calls: refuse, keep the file
            with self.assertRaisesRegex(RuntimeError, "different run"):
                other.prefetch("m", calls, pending)
            self.assertTrue(Path(pending).exists())

    def test_calls_asked_for_out_of_order_fail(self):
        b = self.backend()
        b._ready = [([{"role": "user", "content": "a"}], RawCall(content="a"))]
        with self.assertRaisesRegex(RuntimeError, "order"):
            b.call("m", [{"role": "user", "content": "b"}])


class BatchRunTests(unittest.TestCase):
    def run_cli(self, out, *extra):
        """run_semantic_eval.py on PAIR through a stubbed OpenRouter (live call and batches);
        returns the stubbed requests.post, whose calls are the batches submitted."""
        argv = ["run_semantic_eval.py", "--model", "m", "--tally", "compact", "--task-ids", *PAIR,
                "--provider", "anthropic", "--batch-api", "--yes", "--output", str(out), *extra]
        done = {"id": "b3", "status": "completed", "provider": "Anthropic", "results": [batch_entry(0, "{}")]}
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}), \
                mock.patch("pipeline.pricing.lookup_price", return_value=(SONNET, None)), \
                mock.patch.object(OpenRouterBackend, "_stream", return_value=RawCall(content="{}", latency_ms=9.0)), \
                mock.patch.object(OpenRouterBackend, "BATCH_POLL_SECONDS", 0), \
                mock.patch("pipeline.backends.requests.post",
                           return_value=reply({"id": "b3", "status": "validating"}, 202)) as post, \
                mock.patch("pipeline.backends.requests.get", return_value=reply(done)), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main()
        return post

    def test_several_efforts_run_back_to_back_and_resume_skips_finished_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            self.run_cli(out, "--effort", "low", "high")
            post = self.run_cli(out, "--effort", "low", "high", "medium", "--resume")
            sessions = json.loads(out.read_text(encoding="utf-8"))["sessions"]
        efforts = [s["generation_settings"]["reasoning"]["effort"] for s in sessions]
        self.assertEqual(efforts, ["low", "high", "medium"])
        self.assertEqual(post.call_count, 1)                    # only medium ran the second time
        self.assertEqual(post.call_args.kwargs["json"]["requests"][0]["body"]["reasoning"], {"effort": "medium"})
        self.assertTrue(all(s["session_id"].endswith(e) for s, e in zip(sessions, efforts)))

    def test_a_batch_run_logs_every_call_and_its_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            self.run_cli(out)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertFalse(cli.pending_file(str(out)).exists())   # removed once every row is saved
        session, rows = data["sessions"][0], data["results"]
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["latency_ms"] for r in rows], [9.0, None])
        self.assertEqual(session["cache_ttl"], "1h")
        self.assertEqual([b["id"] for b in session["batch_api"]], ["b3"])
        self.assertEqual(session["generation_settings"]["provider"]["only"], ["anthropic"])
        self.assertEqual(session["latency"]["mean_total_ms"], 9)


class SetupCostTests(unittest.TestCase):
    def setup(self, *calls):
        """setup_usd_est for calls given as (usd_cost, prompt_tokens, cached_tokens, cache_write_tokens)."""
        from pipeline.results import _cost_breakdown
        rows = [{"usd_cost": c, "prompt_tokens": p, "cached_tokens": r, "cache_write_tokens": w} for c, p, r, w in calls]
        return _cost_breakdown(rows, 0, None)["setup_usd_est"]

    def test_the_first_call_is_setup_whether_or_not_a_write_is_reported(self):
        self.assertAlmostEqual(self.setup((0.30, 300000, 0, 0), (0.03, 300000, 295000, 0)), 0.27)       # Gemini
        self.assertAlmostEqual(self.setup((0.15, 234000, 0, 234000), (0.01, 234000, 230700, 3300)), 0.14)  # OpenAI

    def test_a_warm_start_costs_nothing_and_a_rewrite_counts(self):
        self.assertEqual(self.setup((0.02, 378000, 378000, 0), (0.02, 378000, 378000, 0)), 0.0)
        self.assertAlmostEqual(self.setup((0.24, 378000, 0, 378000), (0.02, 378000, 378000, 0),
                                          (0.02, 378000, 378000, 0), (0.24, 378000, 0, 378000)), 0.44)
        self.assertIsNone(self.setup((0.30, 300000, 0, 0)))                    # one call: nothing to compare


class MeasuredEstimateTests(unittest.TestCase):
    def test_a_flex_test_run_prices_a_standard_batch_run(self):
        prices = {"openai/flex": {"prompt": "0.000001"}, "openai": {"prompt": "0.000002"}}
        tested = {"sessions": [{"model": "m", "generation_settings": {"reasoning": {"effort": "high"},
                                                                       "provider": {"only": ["openai/flex"]}},
                                "cost": {"setup_usd_est": 0.30, "per_case_marginal_usd_est": 0.02}}], "results": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.json"
            path.write_text(json.dumps(tested), encoding="utf-8")
            args = SimpleNamespace(measured_from=str(path), effort="high", provider="openai", batch_api=True)
            with mock.patch("pipeline.cli.lookup_price", side_effect=lambda m, t, p: (prices[p], None)):
                got = cli.measured_estimate(args, "m", 101)
                args.effort = "low"
                other = cli.measured_estimate(args, "m", 101)
                args.measured_from = None
        # standard is 2x flex; the first call is setup + one call, the other 100 are halved by the batch
        self.assertAlmostEqual(got["usd"], 2 * (0.30 + 0.02 + 100 * 0.02 * 0.5))
        self.assertEqual(other["usd"], got["usd"])              # no low-effort session: the high one's cost
        self.assertIn("at effort high", other["text"])


class ReuseTests(unittest.TestCase):
    def run_cli(self, out, *extra):
        """run_semantic_eval.py on PAIR, live, through a stubbed OpenRouter; returns the stubbed
        _stream, whose calls are the calls made (each answer numbered by its call)."""
        argv = ["run_semantic_eval.py", "--model", "m", "--tally", "compact", "--task-ids", *PAIR,
                "--provider", "openai/flex", "--yes", "--output", str(out), *extra]
        answers = (RawCall(content="{}", usd_cost=0.5) for _ in range(100))
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}), \
                mock.patch("pipeline.pricing.lookup_price", return_value=(SONNET, None)), \
                mock.patch.object(OpenRouterBackend, "_stream", side_effect=lambda m, msgs: next(answers)) as live, \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            cli.main()
        self.printed = printed.getvalue()
        return live

    def test_the_check_reuses_the_main_runs_answers_and_calls_only_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            main, check = Path(tmp) / "main.json", Path(tmp) / "check.json"
            self.run_cli(main, "--effort", "medium")
            live = self.run_cli(check, "--effort", "medium", "-n", "3", "--reuse-from", str(main))
            data = json.loads(check.read_text(encoding="utf-8"))
        self.assertEqual(live.call_count, 4)                    # 2 cases x 3 samples, 2 of them reused
        self.assertIn("for 4 call(s) (2 more answer(s) reused", self.printed)
        rows, session = data["results"], data["sessions"][0]
        self.assertEqual(len(rows), 6)
        self.assertEqual([r["run_index"] for r in rows], [0, 1, 2, 0, 1, 2])
        self.assertEqual([bool(r.get("reused_from")) for r in rows], [True, False, False] * 2)
        self.assertTrue(all(r["session_id"] == session["session_id"] for r in rows))
        self.assertEqual(session["calls"], 6)
        self.assertEqual(session["cost"]["total_usd"], 2.0)     # only the 4 calls made here
        self.assertIn("2 of the 6 answers were reused", session["reused_answers"])

    def test_answers_from_other_settings_are_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            main, check = Path(tmp) / "main.json", Path(tmp) / "check.json"
            self.run_cli(main, "--effort", "low")
            live = self.run_cli(check, "--effort", "medium", "-n", "3", "--reuse-from", str(main))
        self.assertEqual(live.call_count, 6)


if __name__ == "__main__":
    unittest.main()
