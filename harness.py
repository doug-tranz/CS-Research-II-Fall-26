# harness.py — core harness: generation, backends, logging. Domain-agnostic.
# Domain testbeds (e.g. registration.py) plug in for evaluation.
import os, time, json, requests
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Protocol

# ── Output contract: the DIDComm request lives HERE, above the backend split,
#    so switching endpoints cannot change the requested format. ──
SYSTEM_PROMPT = (
    "You produce transactions as DIDComm-format JSON messages. "
    "Respond with ONLY the JSON message — no prose, no markdown, no code fences. "
    'The message must have the shape {"type": <string>, "id": <string>, "body": { ... }}.'
)

# ── Task + ground truth ──
@dataclass(frozen=True)
class TaskSpec:                 # everything that MAY go to the model
    task_id: str
    domain: str                 # "registration" | "tax" | ...
    inputs: str                 # JSON: known state (+ optional status-read)
    intent: str                 # what the user wants achieved
    output_contract: str = ""   # domain-specific required body shape + example (grounding)

@dataclass(frozen=True)
class GroundTruth:              # NEVER passed to generate()
    task_id: str
    expected: str = ""          # known-correct DIDComm output (human-verified), optional
    # consistency + semantic checks live in the domain module (see registration.py)

def build_messages(task: TaskSpec) -> list:
    user = f"Known state (JSON):\n{task.inputs}\n\nIntent: {task.intent}"
    if task.output_contract:
        user += f"\n\n{task.output_contract}"
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user}]

# ── Normalized backend response — both backends map INTO this ──
@dataclass
class RawCall:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    load_ms: float = 0.0        # ollama only
    gen_ms: float = 0.0         # ollama only
    total_ms: float = 0.0       # ollama only
    usd_cost: float = 0.0       # openrouter only
    tool_calls: list = field(default_factory=list)  # [{"id", "name", "arguments": dict}]; empty = final answer
    message: dict = field(default_factory=dict)     # the assistant message as returned, to send back

class Backend(Protocol):
    name: str
    def call(self, model: str, messages: list, tools: list | None = None) -> RawCall: ...

def _arguments(raw) -> dict:
    """A tool call's arguments: OpenRouter sends a JSON string, Ollama a dict."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {"_unparsed": raw}
    except json.JSONDecodeError:
        return {"_unparsed": raw}

_NS = 1e6  # ns → ms, in one place

class OllamaBackend:            # Phase 1 — local, free
    name = "ollama"
    URL = "http://localhost:11434/api/chat"
    def call(self, model, messages, tools=None) -> RawCall:
        t0 = time.perf_counter()
        payload = {"model": model, "messages": messages,
                   "stream": False, "options": {"temperature": 0}}
        if tools:
            payload["tools"] = tools
        r = requests.post(self.URL, json=payload, timeout=120)
        r.raise_for_status()
        d = r.json()
        msg = d["message"]
        calls = [{"id": f"call_{i}", "name": c["function"]["name"],
                  "arguments": _arguments(c["function"].get("arguments"))}
                 for i, c in enumerate(msg.get("tool_calls") or [])]
        return RawCall(
            content=msg.get("content") or "",
            tool_calls=calls, message=msg,
            prompt_tokens=d.get("prompt_eval_count", 0),
            completion_tokens=d.get("eval_count", 0),
            latency_ms=(time.perf_counter() - t0) * 1000,
            load_ms=d.get("load_duration", 0) / _NS,
            gen_ms=d.get("eval_duration", 0) / _NS,
            total_ms=d.get("total_duration", 0) / _NS,
        )

class OpenRouterBackend:        # Phase 2 — cloud, paid (§4.2). One key, many models.
    name = "openrouter"
    URL = "https://openrouter.ai/api/v1/chat/completions"
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("OpenRouter needs an API key: set OPENROUTER_API_KEY")
    def call(self, model, messages, tools=None) -> RawCall:
        # OpenAI-compatible; no prefill/decode split, so latency = wall-clock.
        # usage.include asks OpenRouter to report the exact USD cost of the call.
        t0 = time.perf_counter()
        payload = {"model": model, "messages": messages,
                   "temperature": 0, "usage": {"include": True}}
        if tools:
            payload["tools"] = tools
        r = requests.post(self.URL, headers={
            "Authorization": f"Bearer {self.api_key}",
        }, json=payload, timeout=120)
        latency_ms = (time.perf_counter() - t0) * 1000
        d = r.json() if r.content else {}
        if r.status_code != 200 or "error" in d:
            raise RuntimeError(f"OpenRouter {r.status_code}: {d.get('error', r.text)}")
        usage = d.get("usage") or {}
        msg = d["choices"][0]["message"]
        calls = [{"id": c["id"], "name": c["function"]["name"],
                  "arguments": _arguments(c["function"].get("arguments"))}
                 for c in msg.get("tool_calls") or []]
        return RawCall(
            content=msg.get("content") or "",
            tool_calls=calls, message={**msg, "content": msg.get("content") or ""},
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency_ms,
            usd_cost=usage.get("cost", 0.0),
        )

# ── Generation result (one log row) ──
@dataclass(frozen=True)
class GenerationResult:
    task_id: str
    backend: str
    model: str
    prompt_sent: str
    raw_output: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    gen_ms: float               # ollama-only detail; 0.0 on cloud
    load_ms: float              # ollama-only detail; 0.0 on cloud
    total_ms: float             # ollama-only detail; 0.0 on cloud
    usd_cost: float             # openrouter-only; 0.0 on local
    run_index: int
    ok: bool                    # generation SUCCEEDED — not: was it correct
    error: str | None
    ts: str
    model_calls: int = 1        # >1 when the model used tools before answering
    steps: list = field(default_factory=list)   # each tool call: tool, arguments, what came back

def generate(backend: Backend, model: str, task: TaskSpec, run_index: int = 0) -> GenerationResult:
    messages = build_messages(task)
    prompt_sent = messages[-1]["content"]
    ts = datetime.now(timezone.utc).isoformat()
    try:
        rc = backend.call(model, messages)
    except Exception as e:
        return GenerationResult(task.task_id, backend.name, model, prompt_sent,
                                "", 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0,
                                run_index, False, str(e), ts)
    return GenerationResult(
        task_id=task.task_id, backend=backend.name, model=model,
        prompt_sent=prompt_sent, raw_output=rc.content,
        prompt_tokens=rc.prompt_tokens, completion_tokens=rc.completion_tokens,
        latency_ms=rc.latency_ms, gen_ms=rc.gen_ms, load_ms=rc.load_ms,
        total_ms=rc.total_ms, usd_cost=rc.usd_cost,
        run_index=run_index, ok=True, error=None, ts=ts)

def generate_with_tools(backend: Backend, model: str, task: TaskSpec, tools: list, run_tool,
                        run_index: int = 0, max_calls: int = 12) -> GenerationResult:
    """Like generate(), but the model may call tools before it answers. run_tool(name, arguments)
    returns (content sent back to the model, summary kept in the log). Tokens, cost and latency
    are summed over every model call; the final answer is the first reply with no tool call."""
    messages = build_messages(task)
    prompt_sent = messages[-1]["content"]
    ts = datetime.now(timezone.utc).isoformat()
    steps, calls = [], 0
    totals = dict(prompt=0, completion=0, latency=0.0, gen=0.0, load=0.0, total=0.0, cost=0.0)

    def result(raw_output, ok, error):
        return GenerationResult(task.task_id, backend.name, model, prompt_sent, raw_output,
                                totals["prompt"], totals["completion"], totals["latency"],
                                totals["gen"], totals["load"], totals["total"], totals["cost"],
                                run_index, ok, error, ts, calls, steps)
    try:
        while calls < max_calls:
            rc = backend.call(model, messages, tools=tools)
            calls += 1
            for key, value in (("prompt", rc.prompt_tokens), ("completion", rc.completion_tokens),
                               ("latency", rc.latency_ms), ("gen", rc.gen_ms), ("load", rc.load_ms),
                               ("total", rc.total_ms), ("cost", rc.usd_cost)):
                totals[key] += value
            if not rc.tool_calls:
                return result(rc.content, True, None)
            messages.append(rc.message)
            for call in rc.tool_calls:
                content, summary = run_tool(call["name"], call["arguments"])
                steps.append({"tool": call["name"], "arguments": call["arguments"], **summary})
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        return result("", False, f"no final answer after {max_calls} model calls")
    except Exception as e:
        return result("", False, str(e))

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

# ── Logging: generation row, optionally with the evaluation verdict alongside ──
def log_result(result: GenerationResult, verdict: dict | None = None,
               path: str = "runs.jsonl") -> None:
    row = asdict(result)
    if verdict is not None:
        row["verdict"] = verdict
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
