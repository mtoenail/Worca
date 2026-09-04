import time
from datetime import datetime, timezone

from swarm.schema import Signal

# Derived from MEASURED republish cadence, not from the configured poll interval.
# `interval_s` is 60s, but that is the sleep BETWEEN passes: a pass fetches chains and
# paginates open interest for every underlying, and on 2026-09-04 the observed gaps
# between successive publishes were 120-270s, with one of 870s. A 300s TTL was expiring
# signals the agents were still asserting, and the Oracle saw an empty bus on 8 of 11
# cycles. 600s covers the observed cadence with margin while still expiring a genuinely
# dead signal well inside a session.
DEFAULT_TTL_S = 600


class SignalBus:
    def __init__(self, ttl_s: int = DEFAULT_TTL_S):
        # keyed by (agent, underlying) -> each agent's latest signal PER underlying is kept.
        # Keying by agent alone would let a second underlying silently overwrite the first
        # the moment one agent covers more than one name - this is what prevents that.
        self.latest: dict[tuple[str, str], Signal] = {}
        self.ttl_s = ttl_s

    async def publish(self, sig: Signal):
        self.latest[(sig.agent, sig.underlying)] = sig

    # ---------- expiry ----------
    def age_s(self, sig: Signal) -> float:
        return (datetime.now(timezone.utc) - sig.ts).total_seconds()

    def _live(self, sig: Signal) -> bool:
        """A signal is live only while its agent is still asserting it.

        `analyze()` returns None when an agent's precondition lapses - gamma_scout fires
        only while spot is within 1% of the wall - and the agent then simply publishes
        nothing. Without expiry the last signal would sit on the bus indefinitely: on
        2026-09-03 the Oracle was allocating to a gamma_scout:SPY signal 77,610s (21.5h)
        old, and the executor would have built a live order from those overnight strikes.
        Silence from an agent means "no signal", not "the previous signal".

        This is distinct from the B4 staleness check in `Agent.run`, which refuses to
        PUBLISH a signal computed on stale market data. This one expires a signal that was
        fresh when published and has not been renewed since.
        """
        return self.age_s(sig) <= self.ttl_s

    def get(self, agent: str, underlying: str) -> Signal | None:
        """The live signal for one agent/underlying, or None if it has lapsed."""
        sig = self.latest.get((agent, underlying))
        return sig if sig is not None and self._live(sig) else None

    def snapshot(self) -> dict[tuple[str, str], Signal]:
        """Only signals the agents are currently asserting. {(agent, underlying): Signal}"""
        return {k: s for k, s in self.latest.items() if self._live(s)}

    def expired(self) -> dict[tuple[str, str], float]:
        """Lapsed signals and their ages, for the dashboard's staleness panel."""
        return {k: round(self.age_s(s), 1)
                for k, s in self.latest.items() if not self._live(s)}

    def latest_for(self, underlying: str) -> dict[str, Signal]:
        """Convenience for the Oracle: one underlying, latest per agent - {agent: Signal}."""
        return {a: s for (a, u), s in self.snapshot().items() if u == underlying}
