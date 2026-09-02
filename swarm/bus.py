from swarm.schema import Signal

class SignalBus:
    def __init__(self):
        # keyed by (agent, underlying) -> each agent's latest signal PER underlying is kept.
        # Keying by agent alone would let a second underlying silently overwrite the first
        # the moment one agent covers more than one name - this is what prevents that.
        self.latest: dict[tuple[str, str], Signal] = {}

    async def publish(self, sig: Signal):
        self.latest[(sig.agent, sig.underlying)] = sig

    def snapshot(self) -> dict[tuple[str, str], Signal]:
        return dict(self.latest)                       # {(agent, underlying): Signal}

    def latest_for(self, underlying: str) -> dict[str, Signal]:
        """Convenience for the Oracle: one underlying, latest per agent - {agent: Signal}."""
        return {a: s for (a, u), s in self.latest.items() if u == underlying}
