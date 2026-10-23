"""
harness.py
----------
The core harness, domain-agnostic: what one task sends to a model, how the chat is laid out
for prompt caching, and what one generation logs. The backends that actually call a model
live in backends.py (OpenRouter in the cloud, LM Studio on this machine); the registration
domain plugs in through render.py (what the model sees) and scoring.py (how it is graded).

The output contract (SYSTEM_PROMPT here, OUTPUT_CONTRACT in didcomm_schema.py) sits ABOVE the
backends, so switching models or backends cannot change the requested format.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

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
    inputs: str                 # the known state, rendered (render.render_state())
    intent: str                 # what the user wants achieved
    output_contract: str        # domain-specific required body shape + example (grounding)
    shared_context: str         # the session's opening turn, identical for every task (the
                                # whole Section Tally); sent first and marked for prompt caching
    shared_context_ttl: str = "5m"   # cache lifetime: "5m" or "1h"


# The model's fixed reply to the session's opening turn, so a call reads
# opening -> acknowledgement -> request. Byte-identical every call, like the opening.
SESSION_ACK = "I have read the Section Tally. Send the registration request."

# How build_messages() lays a call out; recorded in each results file's session summary.
PROMPT_LAYOUT = ("system prompt | user: Section Tally + output instructions (cached) | "
                 "assistant: fixed acknowledgement | user: known state + intent")


def build_messages(task: TaskSpec) -> list:
    """The chat messages for one task: the system prompt; the session's opening turn, which is
    the shared context and the output instructions; its fixed acknowledgement; and the task's own
    turn. The opening is byte-identical for every task, with a cache breakpoint after it, so the
    instructions are cached with the tally rather than resent at full price. The API keeps no
    state, so every call resends the opening; the provider caches it on the first call and later
    calls read it from the cache (Anthropic: write 1.25x, read ~0.05-0.1x)."""
    cache = {"type": "ephemeral", **({"ttl": "1h"} if task.shared_context_ttl == "1h" else {})}
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": task.shared_context},
                {"type": "text", "text": task.output_contract, "cache_control": cache}]},
            {"role": "assistant", "content": SESSION_ACK},
            {"role": "user", "content": f"Known state (JSON):\n{task.inputs}\n\nIntent: {task.intent}"}]


def cached_chars(task: TaskSpec) -> tuple:
    """(characters before the cache breakpoint, characters after it) of a task's messages: the
    system prompt, tally and instructions are cached; the acknowledgement and the task's own
    turn are not. Used to split a local model's prompt tokens when projecting what the same
    call would cost on OpenRouter (pricing.Projection)."""
    messages = build_messages(task)
    cached = len(SYSTEM_PROMPT) + len(task.shared_context) + len(task.output_contract)
    return cached, len(SESSION_ACK) + len(messages[-1]["content"])


def _prompt_text(message: dict) -> str:
    """The task-specific prompt text of a message (the shared opening is logged separately)."""
    content = message["content"]
    return content if isinstance(content, str) else content[-1]["text"]


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
    usd_cost: float             # what the provider billed (0 for a local model)
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
    # Local runs only (LM Studio); None on OpenRouter and dropped from its rows (results.py)
    energy_wh: float | None = None      # watt-hours the GPU (plus --extra-watts) drew during the call
    avg_watts: float | None = None      # mean power over the call
    energy_usd: float | None = None     # energy_wh at --kwh-price


def context_label(task: TaskSpec) -> str:
    """Describes a task's cached opening (the shared context and the output instructions) for the
    log: sizes, one hash over both, cache lifetime; never the text."""
    digest = hashlib.sha256((task.shared_context + task.output_contract).encode("utf-8")).hexdigest()[:12]
    return (f"{len(task.shared_context):,} + {len(task.output_contract):,} characters (tally + instructions), "
            f"sha256 {digest}, cache ttl {task.shared_context_ttl}")


def generate(backend, model: str, task: TaskSpec, run_index: int = 0) -> GenerationResult:
    """Sends one task to a model through any backend (backends.py). A failed call comes back
    with ok=False and the error; it never raises."""
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
        served_model=rc.served_model, provider=rc.provider, generation_id=rc.generation_id,
        energy_wh=rc.energy_wh, avg_watts=rc.avg_watts, energy_usd=rc.energy_usd)


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
