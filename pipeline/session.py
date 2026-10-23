"""
session.py
----------
The pipeline's session. Every run is one:

    start  read the Section Tally once and build the opening turn in the chosen version
           (render.TALLY_VARIANTS), optionally only some subjects (--subjects, dev runs)
    run    each case is its own call: the opening turn and the output instructions (cached),
           the model's fixed acknowledgement (harness.SESSION_ACK), then that case's request
    close  report what the session cost and how much of the input came from the cache (the
           results file's session summary keeps the same totals; run_semantic_eval writes it)

The API keeps no state, so every call resends the opening and the provider's cache keeps it
cheap. Calls share nothing but the opening, so a case never sees another case or its answer.
"""
from datetime import datetime, timezone

from .render import TALLY_VARIANTS, render_opening, session_contract


class TallySession:
    """One session: its opening (text, contract, ttl) and its running totals."""

    def __init__(self, ttl: str = "5m", variant: str = "trimmed", subjects=None, label: str | None = None):
        """ttl: cache lifetime; variant: a TALLY_VARIANTS key; subjects: keep only these
        subjects' sections in the opening (None = the whole tally); label: appended to the
        session id, so the sessions of a batch (which can start within one second) stay apart."""
        self.ttl, self.variant, self.text = ttl, variant, ""
        self.subjects = sorted(subjects) if subjects else None
        self.raw, self.open_only, self.compact = TALLY_VARIANTS[variant]
        self.contract = session_contract(variant)
        self.id = f"{variant}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}" + (f"-{label}" if label else "")
        self.rows = 0
        # running totals over the calls made
        self.calls = self.prompt = self.cached = self.written = 0
        self.cost = 0.0
        self.energy_wh = self.energy_usd = self.projected = None    # local runs only

    def __enter__(self):
        """Reads the Section Tally once and builds the opening turn."""
        self.text, self.rows = render_opening(self.variant, self.subjects)
        only = f" of {', '.join(self.subjects)}" if self.subjects else ""
        print(f"session {self.id} started: Section Tally ({self.variant}), {self.rows:,} sections{only}, "
              f"{len(self.text):,} characters, cache ttl {self.ttl}")
        return self

    def record(self, result, projected_usd: float | None = None) -> None:
        """Adds one call's tokens and cost to the session's totals (and, for a local call, its
        energy and its projected OpenRouter cost)."""
        self.calls += 1
        self.prompt += result.prompt_tokens
        self.cached += result.cached_tokens
        self.written += result.cache_write_tokens
        self.cost += result.usd_cost
        if result.energy_wh is not None:
            self.energy_wh = (self.energy_wh or 0.0) + result.energy_wh
            self.energy_usd = (self.energy_usd or 0.0) + result.energy_usd
        if projected_usd is not None:
            self.projected = (self.projected or 0.0) + projected_usd

    def __exit__(self, *exc):
        """Prints the session's totals."""
        share = f"{100 * self.cached / self.prompt:.0f}%" if self.prompt else "-"
        average = self.cost / self.calls if self.calls else 0.0
        print(f"session closed: {self.calls} model calls, {self.prompt:,} input tokens "
              f"({self.written:,} written to the cache, {self.cached:,} read from it: {share}), "
              f"${self.cost:.4f} in all, ${average:.4f} per prompt")
        if self.energy_wh is not None:
            print(f"  electricity: {self.energy_wh:.4f} Wh, ${self.energy_usd:.6f}"
                  + (f"; on OpenRouter these calls would have cost ${self.projected:.4f}"
                     if self.projected is not None else ""))
        return False
