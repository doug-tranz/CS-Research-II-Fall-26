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
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

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
def generation_settings(max_tokens: int | None = None, effort: str | None = None,
                        provider: str | None = None) -> dict:
    """The request fields sent with every OpenRouter call: temperature, streaming, usage
    reporting, and max_tokens / reasoning effort when given (else the provider's default).
    provider: one OpenRouter endpoint tag (e.g. "anthropic", "deepinfra/fp8", "openai"); every
    call goes to that endpoint only, with no fallback, so a run never mixes providers, their
    quantizations or their prices, and the cache stays with one provider."""
    settings = {"temperature": 0, "stream": True, "usage": {"include": True}}
    if max_tokens is not None:
        settings["max_tokens"] = max_tokens
    if effort is not None:
        settings["reasoning"] = {"effort": effort}
    if provider is not None:
        settings["provider"] = {"only": [provider], "allow_fallbacks": False}
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


class _Refused(RuntimeError):
    """A stream that ended in an error before its first token; `code` is the error's status."""

    def __init__(self, message: str, code):
        super().__init__(message)
        self.code = code


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
                message = f"{self.server} error mid-stream: {chunk['error']}"
                if not "".join(self.text) and self.first is None:     # refused before any token
                    raise _Refused(message, (chunk["error"] or {}).get("code"))
                raise RuntimeError(message)
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
        fields = dict(
            content="".join(self.text), latency_ms=self.latency_ms, **usage_fields(self.usage),
            first_token_ms=self.first, finish_reason=self.info.get("finish_reason"),
            native_finish_reason=self.info.get("native_finish_reason"),
            served_model=self.info.get("model"), provider=self.info.get("provider"),
            generation_id=self.info.get("id"))
        return RawCall(**{**fields, **extra})


class PendingBatches:
    """--batch-api's record of paid-for work not yet in the results file: the first (live) call's
    answer and each batch's id, saved as they happen, in a file beside --output. It belongs to
    one session's exact calls (model, settings, every message, in order), so running the same
    command again finds it and collects what was paid for; run_cases() deletes it once every row
    is in the results file. A file left by different calls stops the run rather than overwrite
    it, since that would lose batches already paid for. path None: nothing is recorded."""

    def __init__(self, path: str | None, model: str, settings: dict, calls: list):
        self.path = Path(path) if path else None
        digest = hashlib.sha256(json.dumps([model, settings], sort_keys=True).encode("utf-8"))
        for messages in calls:
            digest.update(json.dumps(messages, sort_keys=True).encode("utf-8"))
        self.key = digest.hexdigest()
        self.state = {"key": self.key, "model": model, "first": None, "batches": []}
        if self.path and self.path.exists():
            found = json.loads(self.path.read_text(encoding="utf-8"))
            if found.get("key") != self.key:
                raise RuntimeError(
                    f"{self.path} holds batches of a different run ({found.get('model')}, batches "
                    f"{', '.join(found.get('batches') or []) or 'none yet'}), already paid for. Run that "
                    f"command again to collect them, or delete the file to give them up.")
            self.state = found
            print(f"{self.path.name}: collecting what an earlier run of this command paid for")

    def first(self):
        """The first call's answer from an earlier run, or None."""
        return RawCall(**self.state["first"]) if self.state.get("first") else None

    def batch_id(self, k: int):
        """The id of batch k from an earlier run, or None."""
        return self.state["batches"][k] if k < len(self.state["batches"]) else None

    def save_first(self, call: RawCall) -> None:
        self.state["first"] = asdict(call)
        self._save()

    def save_batch(self, k: int, batch_id: str) -> None:
        self.state["batches"][k:k + 1] = [batch_id]
        self._save()

    def _save(self) -> None:
        if self.path:
            self.path.write_text(json.dumps(self.state, indent=1), encoding="utf-8")


def batch_chunks(calls: list, max_bytes: int) -> list:
    """`calls` (chat messages) split in order into runs whose JSON stays under max_bytes: a
    session's 400 calls with the compact tally are ~246 MB, over OpenRouter's 200 MB per batch."""
    chunks, size = [], max_bytes
    for messages in calls:
        n = len(json.dumps(messages).encode("utf-8"))
        if size + n > max_bytes:
            chunks.append([])
            size = 0
        chunks[-1].append(messages)
        size += n
    return chunks


def usage_fields(usage: dict) -> dict:
    """The RawCall fields an OpenAI-style usage block gives: tokens, cache use, thinking, cost."""
    cache = usage.get("prompt_tokens_details") or {}
    return dict(prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                usd_cost=usage.get("cost", 0.0) or 0.0,
                cached_tokens=cache.get("cached_tokens", 0) or 0,
                cache_write_tokens=cache.get("cache_write_tokens", 0) or 0,
                reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0)


# ── OpenRouter: cloud, paid ──

class OpenRouterBackend:        # One key, many models (§4.2).
    """Cloud models through OpenRouter's OpenAI-compatible API, streamed: reports the exact cost,
    how many input tokens were read from or written to the prompt cache, thinking tokens, time
    to the first token, why generation stopped, and which provider served the call."""
    URL = "https://openrouter.ai/api/v1/chat/completions"
    BATCH_URL = "https://openrouter.ai/api/v1/batches"
    BATCH_POLL_SECONDS = 60             # OpenRouter: polling once a minute is enough
    BATCH_FINAL = ("completed", "failed", "expired", "cancelled")
    BATCH_MAX_BYTES = 150_000_000       # OpenRouter takes up to 200 MB per batch; each call resends the tally
    # A live call refused before its first token is tried again after each wait: 429 (rate
    # limit), 502/503 (no provider available right now). A refusal can come as the HTTP status or
    # as an error in the stream: openai/flex answers 200, then "Flex processing is temporarily
    # unavailable" (502) mid-stream when it has no capacity.
    RETRY_STATUS = (429, 502, 503)
    RETRY_SECONDS = (15, 30, 60, 120, 240)
    name, paid = "openrouter", True

    def __init__(self, api_key: str | None = None, max_tokens: int | None = None, effort: str | None = None,
                 provider: str | None = None, batch_api: bool = False):
        """provider: pin every call to this endpoint tag (generation_settings()). batch_api: run
        a session through OpenRouter's Batch API at about half price (prefetch())."""
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("OpenRouter needs an API key: set OPENROUTER_API_KEY")
        self.settings = generation_settings(max_tokens, effort, provider)
        self.batch_api = batch_api
        self.batch_info = []            # after prefetch(): each batch's id, provider, status, counts
        self._ready = []                # prefetched (messages, RawCall or error), in call order

    def prefetch(self, model, calls: list, pending: str | None = None) -> None:
        """--batch-api: makes a session's calls ahead of time, in order; call() then hands them
        out one by one, so the run loop is the same as for live calls. The first call is made
        live, which writes the opening to the provider's cache (OpenRouter: requests inside one
        batch may run at once, so a batch cannot warm its own cache); the rest go in batches of at
        most BATCH_MAX_BYTES, one after another, each waited for. Batch rows have no latency:
        their time is queue time, not the model's.
        calls: the chat messages of every call, in the order call() will be asked for them.
        pending: a file that records the paid-for work as it happens (the first call's answer, each
        batch's id), so a run stopped while waiting (a crash, Ctrl+C, a closed laptop) collects it
        when the same command is run again instead of paying for it twice; see PendingBatches."""
        if not self.batch_api or not calls:
            return
        state = PendingBatches(pending, model, self.settings, calls)
        first = state.first()
        if first is None:
            try:
                first = self._stream(model, calls[0])
                state.save_first(first)
            except Exception as e:      # handed out by call(), so generate() logs it as a failed call
                first = e
        self._ready = [(calls[0], first)]
        for k, chunk in enumerate(batch_chunks(calls[1:], self.BATCH_MAX_BYTES)):
            self._ready += list(zip(chunk, self._batch(model, chunk, state.batch_id(k),
                                                       lambda batch_id, k=k: state.save_batch(k, batch_id))))

    def _batch(self, model, calls: list, batch_id: str | None = None, submitted=None) -> list:
        """Submits `calls` as one batch, waits for it, and returns one RawCall (or the error, as
        an Exception) per call, in order. Adds the batch to batch_info. batch_id: a batch of these
        calls already submitted (and paid for), to wait for instead of submitting again.
        submitted: called with the new batch's id once OpenRouter has accepted it."""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if batch_id is not None:
            print(f"batch {batch_id}: submitted by an earlier run of this command; collecting it")
            return self._wait(model, calls, {"id": batch_id, "status": "in_progress"}, headers)
        body = {k: v for k, v in self.settings.items() if k not in ("stream", "provider")}
        submit = {"endpoint": "/v1/chat/completions", "model": model,
                  "requests": [{"custom_id": f"call-{i:04d}", "body": {"messages": m, **body}}
                               for i, m in enumerate(calls)]}
        if "provider" in self.settings:
            submit["provider"] = self.settings["provider"]
        r = requests.post(self.BATCH_URL, headers=headers, json=submit, timeout=600)
        if r.status_code not in (200, 202):
            d = r.json() if r.content else {}
            raise RuntimeError(f"OpenRouter batch not accepted ({r.status_code}): {d.get('error', r.text)}")
        batch = r.json()
        if submitted:
            submitted(batch["id"])
        print(f"batch {batch['id']} submitted: {len(calls)} calls (results stay on OpenRouter for 30 days; "
              f"if this run stops, run the same command again to collect them)")
        return self._wait(model, calls, batch, headers)

    def _wait(self, model, calls: list, batch: dict, headers: dict) -> list:
        """Polls a submitted batch until it is final; returns _batch()'s list."""
        while batch.get("status") not in self.BATCH_FINAL or (batch["status"] == "completed"
                                                                and batch.get("results") is None):
            time.sleep(self.BATCH_POLL_SECONDS)
            try:
                r = requests.get(f"{self.BATCH_URL}/{batch['id']}", headers=headers, timeout=120)
            except requests.RequestException as e:          # money is spent: keep waiting
                print(f"batch {batch['id']}: could not poll ({type(e).__name__}); trying again")
                continue
            if r.status_code in (404, 410):
                raise RuntimeError(f"batch {batch['id']}: {r.status_code} {r.text}")
            if r.status_code != 200:                        # 429 and 5xx clear by waiting
                continue
            batch = r.json()
            counts = batch.get("request_counts") or {}
            print(f"batch {batch['id']}: {batch.get('status')}, "
                  f"{counts.get('completed', 0) + counts.get('failed', 0)}/{len(calls)} done")
        self.batch_info.append({"id": batch["id"], "provider": batch.get("provider"), "status": batch["status"],
                                "request_counts": batch.get("request_counts")})
        if batch["status"] != "completed":
            why = (batch.get("error") or {}).get("message", "")
            return [RuntimeError(f"batch {batch['id']} {batch['status']}: {why}")] * len(calls)
        found = {e.get("custom_id"): e for e in batch.get("results") or []}
        return [self._batch_result(found.get(f"call-{i:04d}"), batch) for i in range(len(calls))]

    @staticmethod
    def _batch_result(entry: dict | None, batch: dict):
        """One batch result as a RawCall, or an Exception when that request failed."""
        if entry is None:
            return RuntimeError(f"batch {batch['id']} returned no result for this call")
        if entry.get("error"):
            return RuntimeError(f"batch request failed: {entry['error'].get('message', entry['error'])}")
        body = (entry.get("response") or {}).get("body") or {}
        choice = (body.get("choices") or [{}])[0]
        return RawCall(content=(choice.get("message") or {}).get("content") or "", latency_ms=None,
                       **usage_fields(body.get("usage") or {}),
                       finish_reason=choice.get("finish_reason"),
                       native_finish_reason=choice.get("native_finish_reason"),
                       served_model=body.get("model"), provider=body.get("provider") or batch.get("provider"),
                       generation_id=body.get("id"))

    def call(self, model, messages) -> RawCall:
        if not self.batch_api:
            return self._stream(model, messages)
        if not self._ready:
            raise RuntimeError("--batch-api: no prefetched result left for this call")
        sent, result = self._ready.pop(0)
        if sent != messages:
            raise RuntimeError("--batch-api: calls asked for out of the order they were prefetched in")
        if isinstance(result, Exception):
            raise result
        return result

    def _stream(self, model, messages) -> RawCall:
        # Streamed, so the first token's arrival can be timed; usage (cost, cache, thinking
        # tokens) comes in the last chunk because usage.include is set. The latency is the
        # attempt that answered, not the waits before it.
        for wait in (*self.RETRY_SECONDS, None):
            t0 = time.perf_counter()
            with requests.post(self.URL, headers={"Authorization": f"Bearer {self.api_key}"},
                               json={"model": model, "messages": messages, **self.settings},
                               timeout=600, stream=True) as r:   # a session's first call writes a ~400k-token opening
                status = r.status_code
                if status == 200:
                    stream = _StreamReader(t0, "OpenRouter")
                    try:
                        stream.read(r)
                        return stream.raw_call()
                    except _Refused as e:      # e.g. "Flex processing is temporarily unavailable" (502)
                        status, error = e.code, str(e)
                else:
                    d = r.json() if r.content else {}
                    error = f"OpenRouter {status}: {d.get('error', r.text)}"
            if status not in self.RETRY_STATUS or wait is None:
                raise RuntimeError(error)
            print(f"  {error}; trying again in {wait}s")
            time.sleep(wait)


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
