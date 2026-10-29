"""
pricing.py
----------
What a run costs on OpenRouter, from OpenRouter's public price list (no API key needed, so no
funds are touched):

  estimate_cost()   BEFORE a run: the estimated cost of the calls it will make, printed first;
                    --max-cost refuses to start above it.
  Projection        DURING a local run (LM Studio): what each call WOULD have cost on
                    OpenRouter, from the tokens the local model actually used, so a model can be
                    tried locally before anyone spends money on it.
  confirm_spend()   the go / no-go for a paid run: --max-cost, a typed "y", or --yes.

How a call is priced (call_cost()): the cached opening is written to the provider's cache once
(input_cache_write, Anthropic 1.25x the input price) and read from it on every later call
(input_cache_read, ~0.05-0.1x); the case's own turn is input; the answer, thinking included,
is output. A model with no cache-read price is charged the full input price for the opening on
every call: that is what happens on OpenRouter when a provider does not cache.
"""
import sys

import requests

from .harness import cached_chars

# Tokens per character of the session's opening, measured on Claude in the 2026-10-19 runs:
# the registrar's CSV (raw) tokenizes denser than the converted tables. The case's own turn is
# estimated at 0.5 tokens per character and an answer at 1,500 tokens (thinking included), both
# on the high side of what the runs used, so the estimate leans high.
TOKENS_PER_CHAR = {True: 0.54, False: 0.62}
CASE_TOKENS_PER_CHAR, ANSWER_TOKENS = 0.5, 1500

MODELS_URL = "https://openrouter.ai/api/v1/models"
PRICE_KEYS = ("prompt", "completion", "input_cache_read", "input_cache_write", "input_cache_write_1h")


def model_price(model: str, prompt_tokens: float, provider: str | None = None) -> dict | None:
    """OpenRouter's per-token prices for `model` at this prompt size: the highest across its
    providers (OpenRouter may route to any of them), with each provider's long-prompt prices
    applied when the prompt reaches their threshold (e.g. Claude Haiku 5.5 costs five times as
    much from 100k prompt tokens). The general model list shows only the base prices, so the
    per-provider endpoints are read; the list is the fallback. None when the model is unknown.
    provider: an endpoint tag (--provider); only that endpoint's prices, and a ValueError naming
    the model's tags when it has no such endpoint."""
    url = f"{MODELS_URL}/{model.split(':')[0]}/endpoints"
    endpoints = (requests.get(url, timeout=30).json().get("data") or {}).get("endpoints") or []
    if provider is not None:
        tags = [e.get("tag") for e in endpoints]
        endpoints = [e for e in endpoints if e.get("tag") == provider]
        if not endpoints:
            raise ValueError(f"{model} has no endpoint tagged {provider!r}; its tags: {', '.join(map(str, tags))}")
    prices = []
    for e in endpoints:
        p = dict(e.get("pricing") or {})
        for tier in sorted(p.pop("overrides", None) or [], key=lambda o: o.get("min_prompt_tokens", 0)):
            if prompt_tokens >= tier.get("min_prompt_tokens", float("inf")):
                p.update({k: v for k, v in tier.items() if k in PRICE_KEYS})
        prices.append(p)
    if not prices:
        listed = requests.get(MODELS_URL, timeout=30).json()["data"]
        return next((m["pricing"] for m in listed if m["id"] == model), None)
    worst = {k: max(float(p[k]) for p in prices if p.get(k)) for k in PRICE_KEYS if any(p.get(k) for p in prices)}
    if model.endswith(":batch"):        # endpoints list the standard prices; batch is half of each
        worst = {k: v / 2 for k, v in worst.items()}
    return worst


def lookup_price(model: str, prompt_tokens: float, provider: str | None = None) -> tuple:
    """(prices, None) or (None, why not): model_price() that never raises."""
    try:
        price = model_price(model, prompt_tokens, provider)
        return (price, None) if price else (None, f"{model} has no prices on OpenRouter")
    except ValueError as e:             # no endpoint with that --provider tag
        return None, str(e)
    except Exception as e:              # offline, rate-limited, unexpected reply
        return None, f"OpenRouter's prices could not be read ({type(e).__name__})"


def rates(price: dict, ttl: str = "5m") -> dict:
    """USD per token for each part of a call. Without a cache-read price the opening is charged
    as plain input every time (no caching); with one but no write price, the write is assumed
    to cost 1.25x the input (Anthropic's rate, the highest in use). ttl "1h": the one-hour cache
    write price where the provider lists one (Anthropic: 2x the input)."""
    p_in = float(price["prompt"])
    read = price.get("input_cache_read")
    if read in (None, "", 0, "0"):
        return {"input": p_in, "output": float(price["completion"]), "cache_read": p_in, "cache_write": p_in}
    write = price.get("input_cache_write_1h") if ttl == "1h" else None
    write = write if write not in (None, "") else price.get("input_cache_write")
    return {"input": p_in, "output": float(price["completion"]), "cache_read": float(read),
            "cache_write": float(write) if write not in (None, "") else 1.25 * p_in}


def call_cost(r: dict, cached: float, own: float, output: float, writes_cache: bool) -> float:
    """One call's cost from rates(): the cached opening (written or read), the own turn, the answer."""
    return cached * (r["cache_write"] if writes_cache else r["cache_read"]) + own * r["input"] + output * r["output"]


# ── Before a run ──

def opening_tokens(session, first_task) -> float:
    """The estimated tokens of the cached opening (tally + instructions)."""
    return (len(first_task.shared_context) + len(first_task.output_contract)) * TOKENS_PER_CHAR[session.raw]


def estimate_cost(model: str, session, tasks: list, n: int, max_tokens: int | None,
                  answer_tokens: float | None = None, provider: str | None = None,
                  batch_api: bool = False, reused: int = 0) -> dict:
    """The run's estimated cost from OpenRouter's current prices for `model`: one cache write of
    the opening, a cache read of it on every later call, each case's own turn at the input price,
    and each answer at the output price. tasks: one TaskSpec per case (render.to_task()).
    answer_tokens: tokens per answer, when measured (default ANSWER_TOKENS, leaning high).
    provider: price that endpoint only (--provider). batch_api: the first call is live (it writes
    the cache) and the rest are billed at half price, OpenRouter's usual batch discount.
    reused: answers --reuse-from supplies, which are not called.
    Returns {"usd": float or None, "text": one line to print}."""
    calls = len(tasks) * n - reused
    if calls <= 0:
        return {"usd": 0.0, "text": f"estimated cost: $0 (all {reused} answer(s) reused, no call to make)"}
    opening = opening_tokens(session, tasks[0])
    price, why = lookup_price(model, opening, provider)
    if price is None:
        return {"usd": None, "text": f"cost estimate unavailable: {why}; --max-cost still stops the run "
                                     f"once it is reached"}
    r = rates(price, session.ttl)
    case = sum(len(t.inputs) + len(t.intent) for t in tasks) * CASE_TOKENS_PER_CHAR / len(tasks)
    answer = answer_tokens if answer_tokens is not None else min(ANSWER_TOKENS, max_tokens or ANSWER_TOKENS)
    write = opening * r["cache_write"]
    own = case * r["input"] + answer * r["output"]         # a call's own turn and answer
    later = (calls - 1) * (opening * r["cache_read"] + own) * (0.5 if batch_api else 1)
    usd = write + own + later
    where = f" via {provider}" if provider else ""
    how = ", later calls at batch prices" if batch_api else ""
    kept = f" ({reused} more answer(s) reused, not called)" if reused else ""
    return {"usd": round(usd, 4),
            "text": f"estimated cost: ${usd:.2f} for {calls} call(s){kept} on {model}{where} (cache write ${write:.2f}"
                    f"{how}, then about ${(usd - write) / calls:.3f} per call; prices from OpenRouter, "
                    f"tokens estimated on the high side)"}


def confirm_spend(estimate: dict, max_cost: float | None, yes: bool) -> bool:
    """Whether a PAID run may start. --max-cost already set a budget (the run stops at it), so it
    starts when the estimate is within it; --yes starts it anyway; otherwise the person running
    it types y. A non-interactive run with neither flag does not start: nothing is spent by a
    script that forgot to set a budget."""
    if max_cost is not None:
        if estimate["usd"] is not None and estimate["usd"] > max_cost:
            print(f"not started: the estimate (${estimate['usd']:.2f}) is above --max-cost ${max_cost:g}; "
                  f"run fewer cases (--task-ids, -n), a smaller --tally, or raise --max-cost")
            return False
        return True
    if yes:
        return True
    refused = "not started: a paid run needs --max-cost <USD> or --yes when it cannot ask"
    if not sys.stdin.isatty():
        print(refused)
        return False
    try:
        return input("Continue? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:          # stdin is a device with nothing to read (Windows NUL counts as a terminal)
        print(f"\n{refused}")
        return False


# ── During a local run ──

class Projection:
    """What a local run's calls would have cost on OpenRouter as `model` (--price-as), call by
    call, from the tokens the local model really used. The first call writes the opening to the
    cache, later ones read it, as on OpenRouter.

    Approximate in two ways, both stated in the session summary: the local server reports one
    prompt-token count, so it is split into cached and uncached parts by characters
    (harness.cached_chars()); and the tokens are the local model's, so pricing a different model
    (e.g. a local Qwen priced as Claude) uses the wrong tokenizer and the local answer length."""

    def __init__(self, model: str, price: dict):
        self.model, self.rates, self.written = model, rates(price), False

    def call(self, task, prompt_tokens: int, completion_tokens: int) -> float:
        """The projected USD of one call to `task` that used these tokens."""
        cached, own = cached_chars(task)
        cached_tokens = prompt_tokens * cached / (cached + own)
        usd = call_cost(self.rates, cached_tokens, prompt_tokens - cached_tokens, completion_tokens,
                        writes_cache=not self.written)
        self.written = True
        return round(usd, 8)
