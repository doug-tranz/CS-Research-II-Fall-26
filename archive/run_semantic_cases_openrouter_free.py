"""Run Tyler's existing 20 semantic cases through OpenRouter instead of Ollama.

Place beside:
  harness.py, registration.py, run_semantic_cases_aneesh.py,
  semantic_cases_daquan_aneesh.jsonl, registration_input.schema.json

Git Bash / Windows:
  export OPENROUTER_API_KEY='your-private-key'
  python run_semantic_cases_openrouter.py --dry-run --schema registration_input.schema.json
  python run_semantic_cases_openrouter_free.py --model google/gemma-4-26b-a4b-it:free \
      --samples 1 --schema registration_input.schema.json \
      --output gemma4_free_results.jsonl

Uses the original runner for all input formatting, ground-truth evaluation,
and logging. Only the inference backend is replaced. This file never logs
or prints the API key. This script rejects paid model identifiers; only verified free-model suffix :free is accepted.
"""

import os
import sys
import time

import requests
from harness import RawCall
import run_semantic_cases_aneesh as original_runner


class OpenRouterBackend:
    name = "openrouter"
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self):
        self.key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not self.key:
            raise SystemExit(
                "Missing OPENROUTER_API_KEY. Set it in Git Bash with "
                "export OPENROUTER_API_KEY='your-key' (do not commit the key)."
            )

    def call(self, model: str, messages: list) -> RawCall:
        if not model.endswith(":free"):
            raise ValueError("Paid model blocked: model ID must end with :free")
        t0 = time.perf_counter()
        response = requests.post(
            self.URL,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0,        # Match Aneesh's Ollama configuration
                "stream": False,
                "usage": {"include": True},
            },
            timeout=120,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"].get("content")
        if not isinstance(content, str):
            raise ValueError("OpenRouter returned no text content")
        usage = data.get("usage") or {}
        if usage.get("cost") is None:
            print("WARNING: API response did not include usage.cost; dollar cost will be logged as 0 (unknown).", file=sys.stderr)
        return RawCall(
            content=content,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=elapsed_ms,
            usd_cost=float(usage.get("cost") or 0.0),
        )


def _has_arg(flag: str) -> bool:
    return any(s == flag or s.startswith(flag + "=") for s in sys.argv[1:])


if __name__ == "__main__":
    # Keep the current run isolated from the original Ollama JSONL output.
    if not _has_arg("--model"):
        sys.argv += ["--model", "google/gemma-4-26b-a4b-it:free"]

    # Fail closed: never make a paid-model API call, even if the wrong
    # --model is passed by accident. For scientific comparisons, pin a
    # named free model, not the random-model free router.
    candidates = [sys.argv[i + 1] for i, arg in enumerate(sys.argv[:-1]) if arg == "--model"]
    candidates += [arg.split("=", 1)[1] for arg in sys.argv[1:] if arg.startswith("--model=")]
    if len(candidates) != 1 or not candidates[0].endswith(":free"):
        raise SystemExit("Refusing to call a non-free or ambiguous model. Use --model PROVIDER/MODEL:free")
    if not _has_arg("--output"):
        sys.argv += ["--output", "gemma4_free_results.jsonl"]

    # The original runner is the authoritative source of case parsing,
    # prompts, consistency checking, semantic checking, and output records.
    original_runner.OllamaBackend = OpenRouterBackend
    original_runner.main()
