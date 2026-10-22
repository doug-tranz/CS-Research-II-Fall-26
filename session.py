"""
session.py
-----------
TallySession: one run of the default pipeline. The whole Section Tally is the opening turn,
sent (and cached by the provider) with every case; the session keeps the run's token and
cost totals and prints them when it closes.
"""
from datetime import datetime, timezone

import section_tally
from render import (OPEN_NOTE, RAW_CONTRACT, TALLY_VARIANTS, WHOLE_CONTRACT, _is_open,
                    render_raw, render_tally)


class TallySession:
    """The pipeline's session (the default run):
        start  read the Section Tally once and build the opening turn in the chosen version
               (TALLY_VARIANTS)
        run    each case is its own call: the opening turn (cached), the model's fixed
               acknowledgement (render.SESSION_ACK), then that case's request
        close  report what the session cost and how much of the input came from the cache (the
               results file's session summary keeps the same totals; run_cases() writes it)
    The API keeps no state, so every call resends the opening and the provider's cache keeps
    it cheap. Calls share nothing but the opening, so a case never sees another case or its
    answer."""

    def __init__(self, ttl: str = "5m", variant: str = "trimmed"):
        """ttl: cache lifetime; variant: a TALLY_VARIANTS key."""
        self.ttl, self.variant, self.text = ttl, variant, ""
        self.raw, self.open_only = TALLY_VARIANTS[variant]
        self.contract = (RAW_CONTRACT if self.raw else WHOLE_CONTRACT) + (OPEN_NOTE if self.open_only else "")
        self.id = f"{variant}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        self.calls = self.prompt = self.cached = self.written = self.rows = 0
        self.cost = 0.0

    def prepare(self) -> "TallySession":
        """Reads the Section Tally once and builds the opening turn (once; later calls reuse it).
        Separate from starting the session so a run can be sized and priced first."""
        if not self.text:
            schedule = section_tally.full_schedule()
            self.text = render_raw(self.open_only) if self.raw else render_tally(schedule, self.open_only)
            self.rows = (sum(1 for s in schedule["sections"] if _is_open(s)) if self.open_only
                         else len(schedule["sections"]))
        return self

    def __enter__(self):
        """Builds the opening turn (if not prepared yet) and starts the session."""
        self.prepare()
        print(f"session {self.id} started: Section Tally ({self.variant}), {self.rows:,} sections, "
              f"{len(self.text):,} characters, cache ttl {self.ttl}")
        return self

    def record(self, result) -> None:
        """Adds one call's tokens and cost to the session's totals."""
        self.calls += 1
        self.prompt += result.prompt_tokens
        self.cached += result.cached_tokens
        self.written += result.cache_write_tokens
        self.cost += result.usd_cost

    def __exit__(self, *exc):
        """Prints the session's totals."""
        share = f"{100 * self.cached / self.prompt:.0f}%" if self.prompt else "-"
        average = self.cost / self.calls if self.calls else 0.0
        print(f"session closed: {self.calls} model calls, {self.prompt:,} input tokens "
              f"({self.written:,} written to the cache, {self.cached:,} read from it: {share}), "
              f"${self.cost:.4f} in all, ${average:.4f} per prompt")
        return False
