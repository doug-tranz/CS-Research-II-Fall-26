# harness.py — core harness: generation through OpenRouter (or a local Ollama for dev testing),
# prompt caching. Domain-agnostic.
# Domain modules (render.py builds tasks, scoring.py grades answers) plug in around it.
import os, time, json, hashlib, requests
from dataclasses import dataclass
from datetime import datetime, timezone

# ── Output contract: the DIDComm request lives HERE, above the backend,
#    so switching models cannot change the requested format. ──
SYSTEM_PROMPT = (
    "You produce transactions as DIDComm-format JSON messages. "
    "Respond with ONLY the JSON message — no prose, no markdown, no code fences. "
    'The message must have the shape {"type": <string>, "id": <string>, "body": { ... }}.'
)

# ── Task ── (the answer key never comes here: it stays in the case file, see cases.py)
@dataclass(frozen=True)
class TaskSpec:                 # everything that MAY go to the model
    """Everything that may go to the model for one task; the answer key never does."""
    task_id: str
    inputs: str                 # JSON: known state (+ optional status-read)
    intent: str                 # what the user wants achieved
    output_contract: str = ""   # domain-specific required body shape + example (grounding)
    shared_context: str = ""    # the session's opening turn, identical for many tasks (e.g. the
                                # whole Section Tally); sent first and marked for prompt caching
    shared_context_ttl: str = "5m"   # cache lifetime: "5m" or "1h"
    shared_context_ack: str = ""     # the model's fixed reply to the opening (the domain's text)

def build_messages(task: TaskSpec) -> list:
    """The chat messages for one task: the system prompt, then either one user turn, or a
    session's opening turn, its fixed acknowledgement and the task's own turn."""
    user = f"Known state (JSON):\n{task.inputs}\n\nIntent: {task.intent}"
    if task.output_contract:
        user += f"\n\n{task.output_contract}"
    if not task.shared_context:
        return [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user}]
    # A session: the shared opening is its own first turn, byte-identical for every task, with
    # a cache breakpoint after it; the acknowledgement and the request follow. The API keeps no
    # state, so every call resends the opening; the provider caches it on the first call and
    # later calls read it from the cache (Anthropic: write 1.25x, read ~0.05-0.1x).
    cache = {"type": "ephemeral", **({"ttl": "1h"} if task.shared_context_ttl == "1h" else {})}
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": task.shared_context, "cache_control": cache}]},
            {"role": "assistant", "content": task.shared_context_ack},
            {"role": "user", "content": user}]

def _prompt_text(message: dict) -> str:
    """The task-specific prompt text of a message (the shared opening is logged separately)."""
    content = message["content"]
    return content if isinstance(content, str) else content[-1]["text"]

# ── What one call returns ──
@dataclass
class RawCall:
    """What one OpenRouter call returns."""
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
    served_model: str | None = None       # the model OpenRouter reports actually answering
    provider: str | None = None           # which provider OpenRouter routed the call to
    generation_id: str | None = None      # OpenRouter's id for the call (its dashboard / generation API)

# The generation settings every call sends (beyond model and messages). Recorded in each results
# file's session summary, so a run can be repeated exactly. temperature 0 is sent, but models that
# reject sampling parameters (e.g. Claude Sonnet 5.5) have it dropped by OpenRouter, so the
# summary says what was sent, not what each provider applied.
def generation_settings(max_tokens: int | None = None, effort: str | None = None) -> dict:
    """The request fields sent with every call: temperature, streaming, usage reporting, and
    max_tokens / reasoning effort when given (else the provider's default applies)."""
    settings = {"temperature": 0, "stream": True, "usage": {"include": True}}
    if max_tokens is not None:
        settings["max_tokens"] = max_tokens
    if effort is not None:
        settings["reasoning"] = {"effort": effort}
    return settings

class OpenRouterBackend:        # One key, many models (§4.2).
    """Cloud models through OpenRouter's OpenAI-compatible API, streamed: reports the exact cost,
    how many input tokens were read from or written to the prompt cache, thinking tokens, time
    to the first token, why generation stopped, and which provider served the call."""
    URL = "https://openrouter.ai/api/v1/chat/completions"
    def __init__(self, api_key: str | None = None, max_tokens: int | None = None, effort: str | None = None):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("OpenRouter needs an API key: set OPENROUTER_API_KEY")
        self.settings = generation_settings(max_tokens, effort)
    def call(self, model, messages) -> RawCall:
        # Streamed, so the first token's arrival can be timed; usage (cost, cache, thinking
        # tokens) comes in the last chunk because usage.include is set.
        t0 = time.perf_counter()
        r = requests.post(self.URL, headers={"Authorization": f"Bearer {self.api_key}"},
                          json={"model": model, "messages": messages, **self.settings},
                          timeout=600, stream=True)   # a session's first call writes a ~400k-token opening
        if r.status_code != 200:
            d = r.json() if r.content else {}
            raise RuntimeError(f"OpenRouter {r.status_code}: {d.get('error', r.text)}")
        text, first, usage, info = [], None, {}, {}
        for line in r.iter_lines(decode_unicode=True):
            if not line or line.startswith(":") or not line.startswith("data: "):
                continue                  # keep-alive comments (": OPENROUTER PROCESSING")
            data = line[len("data: "):]
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if "error" in chunk:
                raise RuntimeError(f"OpenRouter error mid-stream: {chunk['error']}")
            for key in ("id", "model", "provider"):
                info.setdefault(key, chunk.get(key))
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if first is None and (delta.get("content") or delta.get("reasoning")):
                    first = (time.perf_counter() - t0) * 1000
                text.append(delta.get("content") or "")
                if choice.get("finish_reason"):
                    info["finish_reason"] = choice["finish_reason"]
                    info["native_finish_reason"] = choice.get("native_finish_reason")
            usage = chunk.get("usage") or usage
        latency_ms = (time.perf_counter() - t0) * 1000
        cache = usage.get("prompt_tokens_details") or {}
        return RawCall(
            content="".join(text),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency_ms,
            usd_cost=usage.get("cost", 0.0),
            cached_tokens=cache.get("cached_tokens", 0) or 0,
            cache_write_tokens=cache.get("cache_write_tokens", 0) or 0,
            reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0,
            first_token_ms=first, finish_reason=info.get("finish_reason"),
            native_finish_reason=info.get("native_finish_reason"),
            served_model=info.get("model"), provider=info.get("provider"), generation_id=info.get("id"),
        )

class OllamaBackend:            # Local and free: for testing the pipeline, not for reported results.
    """A model served by a local Ollama (native /api/chat). Dev use only: a quantized local model
    is not the same model as the cloud one, so its scores are not previews of cloud scores.

    Ollama cuts a prompt longer than num_ctx without an error, so num_ctx is always sent, and a
    call whose prompt filled the whole window fails instead of being scored on a cut-off prompt.
    The whole-tally session (~400k tokens) cannot fit any local window: use --named-courses."""
    URL = "http://localhost:11434/api/chat"

    def __init__(self, num_ctx: int = 65536, max_tokens: int | None = None, timeout: int = 1800,
                 url: str | None = None, verbose: bool = False):
        self.url = url or os.environ.get("OLLAMA_URL", self.URL)
        self.num_ctx, self.timeout, self.verbose = num_ctx, timeout, verbose
        options = {"temperature": 0, "num_ctx": num_ctx}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        self.settings = {"backend": "ollama", "stream": True, "options": options}

    def call(self, model, messages) -> RawCall:
        # Ollama wants plain-text content; a session turn is a list of parts (with cache_control).
        flat = [{"role": m["role"], "content": m["content"] if isinstance(m["content"], str)
                 else "".join(part.get("text", "") for part in m["content"])} for m in messages]
        # Streamed: one JSON object per line, each with a piece of the answer; the last one
        # ("done": true) carries the token counts and why generation stopped. Streaming also
        # lets Ctrl+C stop a run between pieces, and with verbose the answer prints as it comes.
        t0 = time.perf_counter()
        r = requests.post(self.url, json={"model": model, "messages": flat, "stream": True,
                                          "options": self.settings["options"]},
                          timeout=self.timeout, stream=True)   # timeout = longest wait between pieces
        if r.status_code != 200:
            raise RuntimeError(f"Ollama {r.status_code}: {r.text[:300]}")
        text, first, d = [], None, {}
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            chunk = json.loads(line)
            if "error" in chunk:
                raise RuntimeError(f"Ollama error mid-stream: {chunk['error']}")
            piece = (chunk.get("message") or {}).get("content") or ""
            if piece:
                if first is None:
                    first = (time.perf_counter() - t0) * 1000
                text.append(piece)
                if self.verbose:
                    print(piece, end="", flush=True)
            if chunk.get("done"):
                d = chunk
                break
        if self.verbose:
            print()                                   # end the streamed answer's line
        if not d:
            raise RuntimeError("Ollama's stream ended before the answer was finished")
        latency_ms = (time.perf_counter() - t0) * 1000
        prompt_tokens = d.get("prompt_eval_count", 0) or 0
        if prompt_tokens >= self.num_ctx:
            raise RuntimeError(f"prompt filled the whole context ({prompt_tokens} >= num_ctx {self.num_ctx}); "
                               "Ollama cut it, so the answer is not scored. Raise --num-ctx or use a smaller case")
        return RawCall(
            content="".join(text),
            prompt_tokens=prompt_tokens,
            completion_tokens=d.get("eval_count", 0) or 0,
            latency_ms=latency_ms,
            first_token_ms=first,
            finish_reason=d.get("done_reason"),        # "stop" or "length", as OpenRouter names them
            native_finish_reason=d.get("done_reason"),
            served_model=d.get("model"), provider="ollama (local)",
        )

# ── Generation result (one log row) ──
@dataclass(frozen=True)
class GenerationResult:
    """One generation as logged: what was sent, what came back, tokens, timing, cost, cache use."""
    task_id: str
    model: str
    prompt_sent: str
    raw_output: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    usd_cost: float
    run_index: int
    ok: bool                    # generation SUCCEEDED — not: was it correct
    error: str | None
    ts: str
    cached_tokens: int = 0      # of prompt_tokens, read from the prompt cache
    cache_write_tokens: int = 0 # of prompt_tokens, written to the prompt cache
    shared_context: str = ""    # what the cached opening was (not the text itself), e.g. size + hash
    reasoning_tokens: int = 0   # of completion_tokens, spent thinking
    first_token_ms: float | None = None
    finish_reason: str | None = None
    native_finish_reason: str | None = None
    served_model: str | None = None
    provider: str | None = None
    generation_id: str | None = None

def context_label(task: TaskSpec) -> str:
    """Describes a task's shared opening for the log (size, hash, cache lifetime), never its text."""
    if not task.shared_context:
        return ""
    digest = hashlib.sha256(task.shared_context.encode("utf-8")).hexdigest()[:12]
    return f"{len(task.shared_context):,} characters, sha256 {digest}, cache ttl {task.shared_context_ttl}"

def generate(backend: OpenRouterBackend, model: str, task: TaskSpec, run_index: int = 0) -> GenerationResult:
    """Sends one task to a model. A failed call comes back with ok=False and the error; it never raises."""
    messages = build_messages(task)
    prompt_sent = _prompt_text(messages[-1])
    ts = datetime.now(timezone.utc).isoformat()
    try:
        rc = backend.call(model, messages)
    except Exception as e:
        return GenerationResult(task.task_id, model, prompt_sent, "", 0, 0, 0.0, 0.0,
                                run_index, False, str(e), ts, shared_context=context_label(task))
    return GenerationResult(
        task_id=task.task_id, model=model,
        prompt_sent=prompt_sent, raw_output=rc.content,
        prompt_tokens=rc.prompt_tokens, completion_tokens=rc.completion_tokens,
        latency_ms=rc.latency_ms, usd_cost=rc.usd_cost,
        run_index=run_index, ok=True, error=None, ts=ts,
        cached_tokens=rc.cached_tokens, cache_write_tokens=rc.cache_write_tokens,
        shared_context=context_label(task),
        reasoning_tokens=rc.reasoning_tokens, first_token_ms=rc.first_token_ms,
        finish_reason=rc.finish_reason, native_finish_reason=rc.native_finish_reason,
        served_model=rc.served_model, provider=rc.provider, generation_id=rc.generation_id)

# ── Shared DIDComm envelope parsing (the body's schema is domain-specific) ──
def parse_didcomm(raw: str):
    """Envelope {type, id, body:{...}} → (body_dict, error). error != '' = syntactic."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON ({e})"
    if not isinstance(msg, dict) or not isinstance(msg.get("body"), dict):
        return None, "missing or invalid 'body'"
    return msg["body"], ""

_context_label = context_label          # the old private name, for older imports
