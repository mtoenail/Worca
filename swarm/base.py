import asyncio, abc
from swarm.bus import SignalBus
from swarm.schema import Signal

class Agent(abc.ABC):
    name: str = "base"
    def __init__(self, bus: SignalBus, underlyings: list[str], interval_s: int = 60):
        self.bus, self.underlyings, self.interval_s = bus, underlyings, interval_s

    @abc.abstractmethod
    async def analyze(self, underlying: str) -> Signal | None:
        ...

    def _is_stale(self, sig: Signal) -> bool:
        """Refuse to publish a signal computed on data older than 3x our poll interval."""
        age = sig.data.get("data_age_s")
        return age is not None and age > 3 * self.interval_s

    async def run(self):
        while True:
            for u in self.underlyings:
                try:
                    sig = await self.analyze(u)
                    if sig and self._is_stale(sig):
                        print(f"[{self.name}] STALE DATA on {u} "
                              f"(age {sig.data['data_age_s']}s) - withholding signal")
                    elif sig:
                        await self.bus.publish(sig)
                except Exception as e:
                    print(f"[{self.name}] error on {u}: {e}")
            await asyncio.sleep(self.interval_s)
