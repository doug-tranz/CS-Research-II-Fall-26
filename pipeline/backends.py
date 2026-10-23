"""
backends.py
-----------
The model backends. Each one takes the chat messages harness.build_messages() made and
returns a RawCall; harness.generate() does not know which one served the call.

    OpenRouterBackend   cloud models through OpenRouter (paid; needs OPENROUTER_API_KEY)
    LMStudioBackend     local models through LM Studio's server on this machine (free to run;
                        records the electricity the GPU drew, see energy.py)

A backend has `name` (recorded in the session summary), `settings` (exactly what every call
sends besides the model and messages), `paid` (whether a run spends money), and
call(model, messages) -> RawCall.
"""
import json
import os
import time
from dataclasses import dataclass

import requests


# ── What one call returns ──
@dataclass
class RawCall:
    """What one call returns, whichever backend made it."""
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    usd_cost: float = 0.0
    cached_tokens: int = 0      # input tokens read from the prompt cache (part of prompt_tokens)
    cache_write_tokens: int = 0 # input tokens written to the prompt cache (part of prompt_tokens)
    reasoning_tokens: int = 0   # of completion_tokens, spent thinking (not in the answer)
    first_token_ms: float | None = None   # time to the first streamed token (thinking or answer)
    finish_reason: str | None = None      # stop / length (hit max_tokens) / content_filter / ...
    native_finish_reason: str | None = None   # the provider's own, e.g. end_turn, max_tokens, refusal
    served_model: str | None = None       # the model the server reports actually answering
    provider: str | None = None           # which provider served the call
    generation_id: str | None = None      # the server's id for the call (OpenRouter's dashboard)
    energy_wh: float | None = None        # local only: energy drawn during the call
    avg_watts: float | None = None        # local only: mean power during the call
    energy_usd: float | None = None       # local only: energy_wh at the electricity price


# The generation settings every call sends (beyond model and messages). Recorded in each results
# file's session summary, so a run can be repeated exactly. temperature 0 is sent, but models that
# reject sampling parameters (e.g. Claude Sonnet 5.5) have it dropped by OpenRouter, so the
# summary says what was sent, not what each provider applied.
def generation_settings(max_tokens: int | None = None, effort: str | None = None) -> dict:
    """The request fields sent with every OpenRouter call: temperature, streaming, usage
    reporting, and max_tokens / reasoning effort when given (else the provider's default)."""
    settings = {"temperature": 0, "stream": True, "usage": {"include": True}}
    if max_tokens is not None:
        settings["max_tokens"] = max_tokens
    if effort is not None:
        settings["reasoning"] = {"effort": effort}
    return settings


def _sse_chunks(response):
    """The JSON chunks of an OpenAI-style server-sent event stream, in order. Skips keep-alive
    comments (": OPENROUTER PROCESSING") and stops at [DONE]."""
    for line in response.iter_lines(decode_unicode=True):
        if not line or line.startswith(":") or not line.startswith("data: "):
            continue
        data = line[len("data: "):]
        if data == "[DONE]":
            return
        yield json.loads(data)


class _StreamReader:
    """Collects one streamed chat completion: the answer text, the time to the first token,
    the usage block and the stop reason. Shared by both backends (same OpenAI-style stream)."""

    def __init__(self, t0: float, server: str):
        self.t0, self.server = t0, server
        self.text, self.first, self.usage, self.info = [], None, {}, {}
        self.latency_ms = None

    def read(self, response) -> None:
        """Reads the stream to its end; the call's latency is the time to that end."""
        for chunk in _sse_chunks(response):
            if "error" in chunk:
                raise RuntimeError(f"{self.server} error mid-stream: {chunk['error']}")
            for key in ("id", "model", "provider"):
                self.info.setdefault(key, chunk.get(key))
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                thinking = delta.get("reasoning") or delta.get("reasoning_content")
                if self.first is None and (delta.get("content") or thinking):
                    self.first = (time.perf_counter() - self.t0) * 1000
                self.text.append(delta.get("content") or "")
                if choice.get("finish_reason"):
                    self.info["finish_reason"] = choice["finish_reason"]
                    self.info["native_finish_reason"] = choice.get("native_finish_reason")
            self.usage = chunk.get("usage") or self.usage
        self.latency_ms = (time.perf_counter() - self.t0) * 1000

    def raw_call(self, **extra) -> RawCall:
        """The collected stream as a RawCall; `extra` sets fields the stream does not carry."""
        usage = self.usage
        cache = usage.get("prompt_tokens_details") or {}
        fields = dict(
            content="".join(self.text),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=self.latency_ms,
            usd_cost=usage.get("cost", 0.0),
            cached_tokens=cache.get("cached_tokens", 0) or 0,
            cache_write_tokens=cache.get("cache_write_tokens", 0) or 0,
            reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0,
            first_token_ms=self.first, finish_reason=self.info.get("finish_reason"),
            native_finish_reason=self.info.get("native_finish_reason"),
            served_model=self.info.get("model"), provider=self.info.get("provider"),
            generation_id=self.info.get("id"))
        return RawCall(**{**fields, **extra})


# ── OpenRouter: cloud, paid ──

class OpenRouterBackend:        # One key, many models (§4.2).
    """Cloud models through OpenRouter's OpenAI-compatible API, streamed: reports the exact cost,
    how many input tokens were read from or written to the prompt cache, thinking tokens, time
    to the first token, why generation stopped, and which provider served the call."""
    URL = "https://openrouter.ai/api/v1/chat/completions"
    name, paid = "openrouter", True

    def __init__(self, api_key: str | None = None, max_tokens: int | None = None, effort: str | None = None):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("OpenRouter needs an API key: set OPENROUTER_API_KEY")
        self.settings = generation_settings(max_tokens, effort)

    def call(self, model, messages) -> RawCall:
        # Streamed, so the first token's arrival can be timed; usage (cost, cache, thinking
        # tokens) comes in the last chunk because usage.include is set.
        t0 = time.perf_counter()
        with requests.post(self.URL, headers={"Authorization": f"Bearer {self.api_key}"},
                           json={"model": model, "messages": messages, **self.settings},
                           timeout=600, stream=True) as r:   # a session's first call writes a ~400k-token opening
            if r.status_code != 200:
                d = r.json() if r.content else {}
                raise RuntimeError(f"OpenRouter {r.status_code}: {d.get('error', r.text)}")
            stream = _StreamReader(t0, "OpenRouter")
            stream.read(r)
        return stream.raw_call()


# ── LM Studio: local, free to run, measured in watts ──

def flatten(messages: list) -> list:
    """The messages with every content list joined into one string and the cache markers
    dropped: local servers neither need nor always accept the content-part form. The text the
    model reads is the same (the tally, then the instructions after a blank line)."""
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            content = "\n\n".join(part["text"] for part in content)
        out.append({"role": m["role"], "content": content})
    return out


class LMStudioBackend:
    """Local models through LM Studio's OpenAI-compatible server (Developer tab > Start Server,
    or `lms server start`; default http://localhost:1234). Streamed like OpenRouter. Each call
    is metered: a PowerMeter (energy.py) samples the GPU while the call runs, so every row
    records energy_wh, avg_watts and energy_usd. usd_cost stays 0: nothing is billed.

    The whole Section Tally (~370k tokens) does not fit a local context window; run with
    --subjects (render.render_opening()) so the opening holds only the cases' subjects."""
    name, paid = "lmstudio", False
    DEFAULT_URL = "http://localhost:1234"

    def __init__(self, meter, kwh_price: float, url: str | None = None,
                 max_tokens: int | None = None, effort: str | None = None):
        self.url = (url or os.environ.get("LMSTUDIO_URL") or self.DEFAULT_URL).rstrip("/")
        self.meter, self.kwh_price = meter, kwh_price
        # stream_options.include_usage asks for token counts in the last chunk (OpenAI style)
        self.settings = {"temperature": 0, "stream": True, "stream_options": {"include_usage": True}}
        if max_tokens is not None:
            self.settings["max_tokens"] = max_tokens
        if effort is not None:
            self.settings["reasoning_effort"] = effort      # honored by models that support it
        self.context_length = None          # set by check_model()

    def models(self) -> list:
        """The model ids the server offers (loaded, or loadable on demand)."""
        try:
            r = requests.get(f"{self.url}/v1/models", timeout=10)
            r.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"LM Studio is not reachable at {self.url} ({type(e).__name__}): open LM Studio "
                               f"> Developer > Start Server, or run `lms server start`") from e
        return [m["id"] for m in r.json().get("data", [])]

    def check_model(self, model: str) -> None:
        """Fails early, before the session starts, when the server is down or does not have the
        model; also reads the model's loaded context length when the server reports it."""
        offered = self.models()
        if model not in offered:
            raise RuntimeError(f"LM Studio has no model {model!r}. Available: {', '.join(offered) or 'none'} "
                               f"(download one in LM Studio, or `lms get <model>`)")
        try:      # LM Studio's own REST API (beta) reports context lengths; optional
            info = requests.get(f"{self.url}/api/v0/models/{model}", timeout=10).json()
            self.context_length = info.get("loaded_context_length") or info.get("max_context_length")
        except (requests.RequestException, ValueError):
            self.context_length = None

    def call(self, model, messages) -> RawCall:
        self.meter.start()               # before the clock starts, so latency excludes the meter
        t0 = time.perf_counter()
        try:
            with requests.post(f"{self.url}/v1/chat/completions",
                               json={"model": model, "messages": flatten(messages), **self.settings},
                               timeout=(10, 1800), stream=True) as r:   # reading a long prompt can take minutes
                if r.status_code != 200:
                    raise RuntimeError(f"LM Studio {r.status_code}: {r.text[:500]}")
                stream = _StreamReader(t0, "LM Studio")
                stream.read(r)
        finally:
            energy = self.meter.stop()
        call = stream.raw_call(provider="lmstudio", usd_cost=0.0, energy_wh=round(energy.wh, 6),
                               avg_watts=energy.avg_watts, energy_usd=round(energy.usd(self.kwh_price), 8))
        if self.context_length and call.prompt_tokens + call.completion_tokens >= self.context_length:
            # A local server may cut a prompt that does not fit instead of failing: do not score
            # an answer to a prompt the model never fully read.
            raise RuntimeError(f"the call filled the model's context window ({call.prompt_tokens:,} prompt + "
                               f"{call.completion_tokens:,} answer tokens >= {self.context_length:,}): the prompt "
                               f"may have been cut. Load the model with a longer context, or narrow --subjects")
        return call
