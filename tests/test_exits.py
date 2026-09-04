"""The B2 exit rules. One implementation, shared by the live and shadow books.

If these diverged, the swarm-vs-solo equity curve would be measuring the difference
between two exit implementations rather than the Oracle's allocation.
"""
import pytest

from swarm.exits import exit_reason

GAMMA = dict(agent="gamma_scout", entry_debit=300.0, front_dte=30,
             spot=100.0, wall_strike=100.0)
VOL = dict(agent="vol_surfer", entry_debit=1000.0, front_dte=30, live_zscore=3.0)


def reason(base, **over):
    kw = {**base, **over}
    return exit_reason(kw.pop("agent"), **kw)


# ---------------------------------------------------------------- gamma_scout
def test_gamma_holds_inside_the_bands():
    assert reason(GAMMA, mark=330.0) is None


def test_gamma_takes_profit_at_plus_50pct():
    assert "target" in reason(GAMMA, mark=450.0)


def test_gamma_stops_at_minus_50pct():
    assert "stop" in reason(GAMMA, mark=150.0)


def test_gamma_exits_when_spot_leaves_the_wall():
    r = reason(GAMMA, mark=300.0, spot=103.0)
    assert "thesis invalidated" in r


def test_gamma_tolerates_drift_inside_2pct():
    assert reason(GAMMA, mark=300.0, spot=101.5) is None


def test_gamma_time_stops_at_5_dte():
    assert "time stop" in reason(GAMMA, mark=300.0, front_dte=5)


def test_gamma_without_a_wall_does_not_crash():
    assert reason(GAMMA, mark=300.0, wall_strike=None, spot=None) is None


# ---------------------------------------------------------------- vol_surfer
def test_vol_holds_while_dislocated():
    assert reason(VOL, mark=1000.0) is None


def test_vol_exits_when_z_reverts():
    r = reason(VOL, mark=1000.0, live_zscore=0.2)
    assert "thesis realised" in r


def test_reversion_is_checked_before_the_stop():
    """Reversion is the thesis being RIGHT, so it must win over a losing mark."""
    r = reason(VOL, mark=0.0, live_zscore=0.1)
    assert "thesis realised" in r


def test_vol_stops_at_minus_100pct_of_debit():
    assert reason(VOL, mark=0.0) == "stop -100% of net debit"


def test_vol_time_stops_at_7_dte_on_the_front_leg():
    assert "time stop" in reason(VOL, mark=1000.0, front_dte=7)


def test_vol_without_a_live_zscore_still_applies_the_stop():
    assert reason(VOL, mark=0.0, live_zscore=None) == "stop -100% of net debit"


# ---------------------------------------------------------------- both
def test_nothing_is_ever_held_into_expiry_week():
    """B2's actual guarantee: no position survives to 1 DTE."""
    assert reason(GAMMA, mark=300.0, front_dte=1, spot=100.0) is not None
    assert reason(VOL, mark=1000.0, front_dte=1) is not None


@pytest.mark.parametrize("agent,base", [("gamma_scout", GAMMA), ("vol_surfer", VOL)])
def test_the_time_stops_subsume_the_blackout(agent, base):
    """The BLACKOUT_DTE branch is unreachable, and deliberately left in place.

    gamma_scout time-stops at 5 DTE and vol_surfer at 7 DTE on the front leg, so neither
    can still be open at the 1 DTE blackout. B2 asked for a force-close inside the expiry
    week; the time stops deliver that guarantee earlier and more conservatively, and the
    blackout remains as a backstop if either time stop is ever loosened.
    """
    mark = base["entry_debit"]
    assert not any("blackout" in (reason(base, mark=mark, front_dte=d) or "")
                   for d in range(0, 10))


def test_zero_entry_debit_does_not_divide_by_zero():
    assert reason(GAMMA, mark=100.0, entry_debit=0.0) is None
