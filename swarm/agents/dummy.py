import random
from swarm.base import Agent
from swarm.schema import Signal

class DummyAgent(Agent):
    def __init__(self, bus, underlyings, name, interval_s=5):
        super().__init__(bus, underlyings, interval_s)
        self.name = name                      # so the two instances are distinguishable

    async def analyze(self, underlying) -> Signal | None:
        # No real logic — just emit a random signal to prove the bus works.
        return Signal(
            agent=self.name,
            signal_type="dummy",
            underlying=underlying,
            strength=round(random.random(), 2),
            direction=random.choice(["bullish", "bearish", "neutral"]),
            data={"note": "placeholder"},
        )