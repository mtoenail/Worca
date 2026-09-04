"""The bus must expire signals an agent has stopped asserting.

`analyze()` returns None when an agent's precondition lapses, and the agent then simply
publishes nothing. Before expiry existed, the last signal sat on the bus indefinitely: on
2026-09-03 the Oracle was allocating to a gamma_scout:SPY signal 77,610s (21.5h) old, and
the executor would have built a live order from those overnight strikes.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from swarm.bus import SignalBus
from swarm.schema import Signal

STALE_21H = 77_610      # the age actually observed in oracle_log.jsonl


def sig(agent, underlying, age_s, **data):
    return Signal(agent=agent, signal_type="t", underlying=underlying, strength=0.5,
                  direction="bullish", data=data,
                  ts=datetime.now(timezone.utc) - timedelta(seconds=age_s))


@pytest.fixture
def bus():
    b = SignalBus(ttl_s=300)
    asyncio.run(b.publish(sig("gamma_scout", "SPY", 10)))
    asyncio.run(b.publish(sig("gamma_scout", "NVDA", STALE_21H)))
    asyncio.run(b.publish(sig("vol_surfer", "SPY", 299)))
    asyncio.run(b.publish(sig("vol_surfer", "NVDA", 301)))
    return b


def test_snapshot_hides_lapsed_signals(bus):
    assert set(bus.snapshot()) == {("gamma_scout", "SPY"), ("vol_surfer", "SPY")}


def test_the_21_hour_signal_is_not_tradeable(bus):
    assert bus.get("gamma_scout", "NVDA") is None


def test_ttl_boundary_is_inclusive(bus):
    assert bus.get("vol_surfer", "SPY") is not None      # 299s - inside
    assert bus.get("vol_surfer", "NVDA") is None         # 301s - outside


def test_unknown_signal_is_none(bus):
    assert bus.get("nobody", "SPY") is None


def test_raw_store_is_retained_for_the_dashboard(bus):
    assert len(bus.latest) == 4
    assert set(bus.expired()) == {("gamma_scout", "NVDA"), ("vol_surfer", "NVDA")}


def test_latest_for_filters_by_liveness(bus):
    assert set(bus.latest_for("SPY")) == {"gamma_scout", "vol_surfer"}
    assert bus.latest_for("NVDA") == {}


def test_republishing_revives_a_lapsed_signal(bus):
    asyncio.run(bus.publish(sig("gamma_scout", "NVDA", 0)))
    assert bus.get("gamma_scout", "NVDA") is not None
