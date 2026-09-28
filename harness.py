# harness.py — core harness: generation, backends, logging. Domain-agnostic.
# Domain testbeds (e.g. registration.py) plug in for evaluation.
import os, time, json, requests
from dataclasses import dataclass, asdict
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

class Backend(Protocol):
    name: str
    def call(self, model: str, messages: list) -> RawCall: ...

_NS = 1e6  # ns → ms, in one place

class OllamaBackend:            # Phase 1 — local, free
    name = "ollama"
    URL = "http://localhost:11434/api/chat"
    def call(self, model, messages) -> RawCall:
        t0 = time.perf_counter()
        r = requests.post(self.URL, json={
            "model": model, "messages": messages,
            "stream": False, "options": {"temperature": 0},
        }, timeout=120)
        r.raise_for_status()
        d = r.json()
        return RawCall(
            content=d["message"]["content"],
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
    def call(self, model, messages) -> RawCall:
        # OpenAI-compatible; no prefill/decode split, so latency = wall-clock.
        # usage.include asks OpenRouter to report the exact USD cost of the call.
        t0 = time.perf_counter()
        r = requests.post(self.URL, headers={
            "Authorization": f"Bearer {self.api_key}",
        }, json={
            "model": model, "messages": messages,
            "temperature": 0, "usage": {"include": True},
        }, timeout=120)
        latency_ms = (time.perf_counter() - t0) * 1000
        d = r.json() if r.content else {}
        if r.status_code != 200 or "error" in d:
            raise RuntimeError(f"OpenRouter {r.status_code}: {d.get('error', r.text)}")
        usage = d.get("usage") or {}
        return RawCall(
            content=d["choices"][0]["message"].get("content") or "",
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
