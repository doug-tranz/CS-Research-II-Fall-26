"""
costs.py
---------
The confirmation before a paid run: how many model calls it makes and roughly what it will
cost, from OpenRouter's published prices, then "Continue? [y/N]". Free runs never ask.

The estimate leans high on purpose. Tokens are counted as characters / CHARS_PER_TOKEN
(the Section Tally CSV measured 1.6-1.9 characters per token on Claude); output is the
--max-tokens cap per call (ASSUMED_OUTPUT when no cap is set); and where OpenRouter lists a
higher long-context price (an override), that price is used. The real cost of every call is
in the results file afterwards.
"""
import sys

import requests

MODELS_URL = "https://openrouter.ai/api/v1/models"
CHARS_PER_TOKEN = 1.5
ASSUMED_OUTPUT = 2000          # tokens per call when --max-tokens is not set


def tokens(text: str) -> int:
    """Rough token count of some text (leans high)."""
    return int(len(text) / CHARS_PER_TOKEN) + 1


def fetch_pricing(model: str, url: str = MODELS_URL, timeout: float = 15):
    """The model's pricing object from OpenRouter's public model list, or None when it cannot
    be had (offline, unknown model). No API key is needed for the list."""
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return next((m.get("pricing") for m in r.json().get("data", []) if m.get("id") == model), None)
    except (requests.RequestException, ValueError):
        return None


def price(pricing: dict, key: str, prompt_tokens: int, fallback: str | None = None):
    """USD per token for `key`, the highest of the base price and every override that could
    apply to a call this size (a long-context rate, a time window); None when not listed.
    `fallback` names the key to use when `key` itself is not listed (e.g. cache read -> prompt)."""
    def get(entry, k):
        value = entry.get(k)
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None
    found = [get(pricing, key)]
    for o in pricing.get("overrides") or []:
        if isinstance(o, dict) and (o.get("min_prompt_tokens") or 0) <= prompt_tokens:
            found.append(get(o, key))
    found = [p for p in found if p is not None]
    if found:
        discount = pricing.get("discount") or 0
        return max(found) * (1 - discount if isinstance(discount, (int, float)) else 1)
    return price(pricing, fallback, prompt_tokens) if fallback else None


def estimate(calls: list, pricing: dict | None, output_tokens: int, cache_ttl: str = "5m") -> dict:
    """calls: one (shared opening tokens, own prompt tokens) per model call, in order. The first
    call with an opening writes it to the cache; the rest read it. Returns the call count, the
    token totals, and `usd` (None when the price is unknown)."""
    out = {"calls": len(calls), "input_tokens": sum(s + o for s, o in calls),
           "output_tokens": output_tokens * len(calls), "usd": None}
    if not pricing:
        return out
    usd, written = 0.0, False
    write_key = "input_cache_write_1h" if cache_ttl == "1h" else "input_cache_write"
    for shared, own in calls:
        size = shared + own
        prompt = price(pricing, "prompt", size)
        completion = price(pricing, "completion", size)
        if prompt is None or completion is None:
            return out
        if shared and not written:
            usd += shared * price(pricing, write_key, size, fallback="prompt")
            written = True
        elif shared:
            usd += shared * price(pricing, "input_cache_read", size, fallback="prompt")
        usd += own * prompt + output_tokens * completion
    out["usd"] = usd
    return out


def confirm(model: str, summary: dict, assume_yes: bool, output_note: str) -> bool:
    """Prints what the run will do and asks. True to go ahead. With assume_yes (--yes) it
    only prints. Without a terminal to answer at, it stops and says to pass --yes."""
    cost = f"about ${summary['usd']:.2f}" if summary["usd"] is not None else "unknown (could not get the price)"
    print(f"\nThis run is PAID (OpenRouter, {model}):\n"
          f"  {summary['calls']} model call(s), ~{summary['input_tokens']:,} input tokens, "
          f"up to ~{summary['output_tokens']:,} output tokens ({output_note})\n"
          f"  estimated cost: {cost} (rough, leans high; the real cost is in the results file)")
    if assume_yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        print("No terminal to confirm at: rerun with --yes to run it anyway.")
        return False
    return input("Continue? [y/N] ").strip().lower() in ("y", "yes")
