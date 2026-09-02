from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

@dataclass
class Signal:
    agent: str                                   # "gamma_scout" | "vol_surfer" | ...
    signal_type: str                             # e.g. "gamma_wall_break", "calendar_anomaly"
    underlying: str                              # "SPY"
    strength: float                              # 0.0-1.0 conviction ON THIS AGENT'S OWN SCALE
    direction: Literal["bullish", "bearish", "neutral"]
    # What `strength` actually measures. Strengths are NOT comparable across agents:
    # a gex_percentile of 0.8 and an iv_spread_zscore of 0.8 mean different things.
    # The Oracle is told this explicitly so it allocates on regime, not on "bigger number".
    strength_basis: str = "unspecified"
    data: dict = field(default_factory=dict)     # agent-specific payload (strikes, GEX, IV, etc.)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
