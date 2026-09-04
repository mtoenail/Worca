"""The gates. `check()` evaluates all of them rather than stopping at the first failure,
so a rejected order carries the complete reason it was rejected."""
from datetime import datetime, timedelta, timezone

import pytest

from swarm.risk import Leg, OrderIntent, RiskManager


class FakeAccount:
    def __init__(self, equity=100_000.0, bp=100_000.0, blocked=False):
        self.equity, self.options_buying_power, self.buying_power = equity, bp, bp
        self.trading_blocked = self.account_blocked = blocked


class FakePosition:
    def __init__(self, symbol, market_value=0.0):
        self.symbol, self.market_value, self.asset_class = symbol, market_value, "us_option"


class FakeTrade:
    def __init__(self, account=None, positions=()):
        self._a, self._p = account or FakeAccount(), list(positions)

    def get_account(self):
        return self._a

    def get_all_positions(self):
        return self._p


class FakeClock:
    def __init__(self, is_open=True, mins_to_close=180):
        self.is_open = is_open
        self.timestamp = datetime.now(timezone.utc)
        self.next_close = self.timestamp + timedelta(minutes=mins_to_close)


def leg(symbol="SPY260917C00770000", side="buy", bid=3.00, ask=3.10, dte=30):
    return Leg(symbol=symbol, side=side, bid=bid, ask=ask, dte=dte)


def intent(strategy="single_leg", direction="bullish", legs=None, alloc=0.10):
    return OrderIntent(signal_id="gamma_scout:SPY", agent="gamma_scout", underlying="SPY",
                       strategy=strategy, direction=direction,
                       legs=legs if legs is not None else [leg()], alloc=alloc)


@pytest.fixture
def rm():
    return RiskManager(FakeTrade())


def why(verdict):
    return " | ".join(verdict.reasons)


# ---------------------------------------------------------------- happy path
def test_a_clean_order_passes(rm):
    v = rm.check(intent(), clock=FakeClock())
    assert v, why(v)
    assert v.qty > 0


def test_verdict_is_falsy_when_rejected(rm):
    assert not rm.check(intent(), clock=FakeClock(is_open=False))


# ---------------------------------------------------------------- market state
def test_closed_market_is_refused(rm):
    assert "market_closed" in why(rm.check(intent(), clock=FakeClock(is_open=False)))


def test_close_blackout_is_refused(rm):
    v = rm.check(intent(), clock=FakeClock(mins_to_close=5))
    assert "close_blackout" in why(v)


def test_no_clock_skips_the_market_gate(rm):
    """The shadow book prices intents with no clock; it must not be gated on the session."""
    assert rm.check(intent()), "an intent with no clock should not fail on market state"


# ---------------------------------------------------------------- B3 direction
def test_neutral_never_becomes_a_directional_order(rm):
    v = rm.check(intent(direction="neutral"), clock=FakeClock())
    assert "neutral_direction" in why(v)


def test_neutral_is_fine_for_a_calendar(rm):
    legs = [leg(dte=14, side="sell", bid=8.85, ask=8.95),
            leg(symbol="SPY261120C00770000", dte=78, side="buy", bid=23.43, ask=24.25)]
    v = rm.check(intent("calendar", "neutral", legs), clock=FakeClock())
    assert "neutral_direction" not in why(v)


# ---------------------------------------------------------------- legs
def test_wide_spread_is_refused(rm):
    v = rm.check(intent(legs=[leg(bid=1.00, ask=1.30)]), clock=FakeClock())
    assert "_spread" in why(v)


def test_unquoted_leg_is_refused(rm):
    v = rm.check(intent(legs=[leg(bid=0, ask=0)]), clock=FakeClock())
    assert "_unquoted" in why(v)


@pytest.mark.parametrize("dte", [3, 60])
def test_dte_outside_the_band_is_refused(rm, dte):
    v = rm.check(intent(legs=[leg(dte=dte)]), clock=FakeClock())
    assert "_dte" in why(v)


def test_no_legs_is_refused(rm):
    assert "no_legs" in why(rm.check(intent(legs=[]), clock=FakeClock()))


# ---------------------------------------------------------------- calendars
def test_calendar_on_one_expiry_is_refused(rm):
    legs = [leg(dte=14, side="sell"), leg(symbol="SPY260917C00775000", dte=14, side="buy")]
    v = rm.check(intent("calendar", "neutral", legs), clock=FakeClock())
    assert "calendar_same_expiry" in why(v)


def test_calendar_must_be_a_net_debit(rm):
    """Sell the expensive leg, buy the cheap one -> a credit. Not the defined-risk trade."""
    legs = [leg(dte=14, side="sell", bid=23.43, ask=24.25),
            leg(symbol="SPY261120C00770000", dte=78, side="buy", bid=8.85, ask=8.95)]
    v = rm.check(intent("calendar", "neutral", legs), clock=FakeClock())
    assert "calendar_not_a_debit" in why(v)


# ---------------------------------------------------------------- exposure
def test_already_holding_blocks_pyramiding():
    """The Oracle re-allocates every 5 minutes and would otherwise stack the same thesis."""
    rm = RiskManager(FakeTrade(positions=[FakePosition("SPY260917C00770000", 5_000)]))
    v = rm.check(intent(), clock=FakeClock())
    assert "already_holding" in why(v)


def test_max_positions_is_enforced():
    pos = [FakePosition(f"SPY260917C0077{i:04d}", 1_000) for i in range(6)]
    rm = RiskManager(FakeTrade(positions=pos))
    assert "max_positions" in why(rm.check(intent(), clock=FakeClock()))


def test_aggregate_deployment_cap_is_enforced():
    pos = [FakePosition("NVDA260917C00230000", 48_000)]
    rm = RiskManager(FakeTrade(positions=pos))
    assert "max_total_deployed" in why(rm.check(intent(), clock=FakeClock()))


def test_blocked_account_is_refused():
    rm = RiskManager(FakeTrade(FakeAccount(blocked=True)))
    assert "account_blocked" in why(rm.check(intent(), clock=FakeClock()))


def test_unreachable_account_fails_closed():
    class Broken:
        def get_account(self):
            raise ConnectionError("down")

    v = RiskManager(Broken()).check(intent(), clock=FakeClock())
    assert not v and "account_unavailable" in why(v)


# ---------------------------------------------------------------- kill switch
def test_daily_loss_limit_halts_trading():
    trade = FakeTrade(FakeAccount(equity=100_000.0))
    rm = RiskManager(trade)
    rm.check(intent(), clock=FakeClock())          # anchors the day at 100k
    trade._a = FakeAccount(equity=96_000.0)        # -4%, past the 3% limit
    assert "daily_loss_limit" in why(rm.check(intent(), clock=FakeClock()))
    assert rm.halted


def test_the_halt_persists_after_a_recovery():
    trade = FakeTrade(FakeAccount(equity=100_000.0))
    rm = RiskManager(trade)
    rm.check(intent(), clock=FakeClock())
    trade._a = FakeAccount(equity=96_000.0)
    rm.check(intent(), clock=FakeClock())
    trade._a = FakeAccount(equity=100_000.0)
    assert "halted" in why(rm.check(intent(), clock=FakeClock()))


# ---------------------------------------------------------------- sizing
def test_allocation_is_a_ceiling_not_a_target(rm):
    """A 0.7 allocation on a 0.10 per-trade cap must size to 0.10."""
    big, capped = intent(alloc=0.70), intent(alloc=0.10)
    assert rm.size(big, 100_000.0) == rm.size(capped, 100_000.0)


def test_size_scales_with_the_allocation(rm):
    assert rm.size(intent(alloc=0.02), 100_000.0) < rm.size(intent(alloc=0.10), 100_000.0)


def test_contract_cap_is_absolute(rm):
    assert rm.size(intent(legs=[leg(bid=0.05, ask=0.05)]), 100_000.0) == rm.max_contracts


def test_unaffordable_order_is_refused(rm):
    v = rm.check(intent(legs=[leg(bid=200.0, ask=205.0)], alloc=0.10), clock=FakeClock())
    assert "size_too_small" in why(v)


def test_all_failing_gates_are_reported_not_just_the_first(rm):
    """This is what makes a rejection legible on the dashboard."""
    v = rm.check(intent(direction="neutral", legs=[leg(bid=1.0, ask=1.4, dte=2)]),
                 clock=FakeClock(is_open=False))
    assert len(v.reasons) >= 4, v.reasons
    joined = why(v)
    for expected in ("market_closed", "neutral_direction", "_spread", "_dte"):
        assert expected in joined
