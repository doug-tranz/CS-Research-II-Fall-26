"""The local Ollama backend (against a stand-in server) and the cost estimate. No network."""
import io
import json
import sys
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import costs                               # noqa: E402
from harness import OllamaBackend          # noqa: E402

MODE = {"value": "ok"}


class FakeOllama(BaseHTTPRequestHandler):
    """Streams an answer in pieces like Ollama's /api/chat; MODE: ok, full, error."""
    protocol_version = "HTTP/1.0"

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.end_headers()
        for piece in ('{"id": "1", ', '"body": {}}'):
            self.wfile.write((json.dumps({"message": {"content": piece}, "done": False}) + "\n").encode())
            if MODE["value"] == "error":
                self.wfile.write(b'{"error": "model crashed"}\n')
                return
        used = req["options"]["num_ctx"] if MODE["value"] == "full" else 100
        self.wfile.write((json.dumps({"done": True, "done_reason": "stop", "model": req["model"],
                                      "prompt_eval_count": used, "eval_count": 7}) + "\n").encode())

    def log_message(self, *args):
        pass


class Ollama(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/api/chat"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, mode, **kw):
        MODE["value"] = mode
        return OllamaBackend(url=self.url, num_ctx=4096, **kw).call("m", [{"role": "user", "content": "hi"}])

    def test_streamed_answer_is_joined(self):
        raw = self.call("ok")
        self.assertEqual(raw.content, '{"id": "1", "body": {}}')
        self.assertEqual((raw.prompt_tokens, raw.completion_tokens, raw.finish_reason), (100, 7, "stop"))

    def test_verbose_prints_the_answer(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.call("ok", verbose=True)
        self.assertIn('{"id": "1", "body": {}}', out.getvalue())

    def test_a_cut_prompt_fails_instead_of_being_scored(self):
        with self.assertRaisesRegex(RuntimeError, "filled the whole context"):
            self.call("full")

    def test_mid_stream_error_fails(self):
        with self.assertRaisesRegex(RuntimeError, "model crashed"):
            self.call("error")


class Costs(unittest.TestCase):
    PRICING = {"prompt": "0.000003", "completion": "0.000015", "input_cache_read": "0.0000003",
               "input_cache_write": "0.00000375",
               "overrides": [{"min_prompt_tokens": 200000, "prompt": "0.000006", "completion": "0.0000225"}]}

    def test_session_writes_the_opening_once_then_reads_it(self):
        e = costs.estimate([(1000, 100), (1000, 100)], {"prompt": "1", "completion": "1",
                                                       "input_cache_write": "2", "input_cache_read": "0.5"}, 10)
        self.assertEqual(e["usd"], (1000 * 2 + 100 + 10) + (1000 * 0.5 + 100 + 10))

    def test_long_context_price_applies_to_big_calls(self):
        self.assertEqual(costs.price(self.PRICING, "prompt", 1000), 0.000003)
        self.assertEqual(costs.price(self.PRICING, "prompt", 300000), 0.000006)

    def test_unknown_price_gives_no_number(self):
        self.assertIsNone(costs.estimate([(0, 100)], None, 10)["usd"])

    def test_confirm_needs_a_terminal_or_yes(self):
        summary = {"calls": 1, "input_tokens": 1, "output_tokens": 1, "usd": 0.01}
        with redirect_stdout(io.StringIO()):
            self.assertTrue(costs.confirm("m", summary, True, ""))
            with mock.patch.object(sys, "stdin", io.StringIO("y\n")):     # not a terminal
                self.assertFalse(costs.confirm("m", summary, False, ""))

    def test_offline_price_lookup_is_none(self):
        with mock.patch("costs.requests.get", side_effect=costs.requests.ConnectionError):
            self.assertIsNone(costs.fetch_pricing("anthropic/claude-sonnet-5.5"))


if __name__ == "__main__":
    unittest.main()
