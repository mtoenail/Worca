"""Sanitizing and stabilizing the model's allocations.

The Oracle talks to a hosted mixture-of-experts. At temperature=0 it is still not
bit-deterministic: on 2026-09-03 three consecutive cycles with materially identical
inputs produced 0.35 / 0.00 / 0.35. These tests pin the two defences.
"""
import pytest

from swarm.oracle import Decision, Oracle

IDS = {"gamma_scout:SPY", "vol_surfer:SPY", "vol_surfer:NVDA"}


@pytest.fixture
def oracle():
    return Oracle(bus=None, api_key="test-key-not-used", deadband=0.10, drop_confirms=2)


def seed(oracle, allocations):
    oracle.last_good = Decision(ts="t", allocations=dict(allocations))


# ---------------------------------------------------------------- _sanitize
def test_hallucinated_ids_are_dropped(oracle):
    out = oracle._sanitize({"gamma_scout:SPY": 0.3, "gamma_scout:TSLA": 0.4}, IDS)
    assert set(out) == {"gamma_scout:SPY"}


def test_zero_is_an_explicit_stand_aside_not_a_floor(oracle):
    """Clamping 0 up to the 0.1 floor would turn a decision to stand aside into a trade."""
    assert oracle._sanitize({"gamma_scout:SPY": 0.0}, IDS) == {}


def test_negative_allocations_are_dropped(oracle):
    assert oracle._sanitize({"gamma_scout:SPY": -0.5}, IDS) == {}


def test_allocations_are_clamped_to_the_band(oracle):
    out = oracle._sanitize({"gamma_scout:SPY": 0.02, "vol_surfer:SPY": 0.99}, IDS)
    assert out == {"gamma_scout:SPY": 0.1, "vol_surfer:SPY": 0.7}


def test_non_numeric_values_are_dropped(oracle):
    assert oracle._sanitize({"gamma_scout:SPY": "lots"}, IDS) == {}


def test_normalisation_only_kicks_in_above_one(oracle):
    under = oracle._sanitize({"gamma_scout:SPY": 0.2, "vol_surfer:SPY": 0.2}, IDS)
    assert under == {"gamma_scout:SPY": 0.2, "vol_surfer:SPY": 0.2}, "cash may be held"

    over = oracle._sanitize(
        {"gamma_scout:SPY": 0.7, "vol_surfer:SPY": 0.7, "vol_surfer:NVDA": 0.7}, IDS)
    assert sum(over.values()) <= 1.0
    assert sum(over.values()) == pytest.approx(1.0, abs=1e-3)   # 4dp rounding


def test_missing_allocations_are_handled(oracle):
    assert oracle._sanitize(None, IDS) == {}


# ---------------------------------------------------------------- _stabilize
def test_the_observed_oscillation_is_damped(oracle):
    """The exact 0.35 -> 0.00 -> 0.35 sequence seen live on 2026-09-03."""
    seed(oracle, {"gamma_scout:SPY": 0.35})
    held = oracle._stabilize({})                       # model dropped it, unconfirmed
    assert held == {"gamma_scout:SPY": 0.35}


def test_a_drop_survives_once_confirmed(oracle):
    seed(oracle, {"gamma_scout:SPY": 0.35})
    assert oracle._stabilize({}) == {"gamma_scout:SPY": 0.35}   # cycle 1: held
    assert oracle._stabilize({}) == {}                          # cycle 2: confirmed


def test_a_reappearing_signal_resets_the_drop_counter(oracle):
    seed(oracle, {"gamma_scout:SPY": 0.35})
    oracle._stabilize({})                                        # one unconfirmed drop
    oracle._stabilize({"gamma_scout:SPY": 0.35})                 # back again
    assert oracle._stabilize({}) == {"gamma_scout:SPY": 0.35}, "counter should restart"


def test_small_moves_keep_the_previous_size(oracle):
    seed(oracle, {"gamma_scout:SPY": 0.35})
    assert oracle._stabilize({"gamma_scout:SPY": 0.40}) == {"gamma_scout:SPY": 0.35}


def test_large_moves_pass_straight_through(oracle):
    """This damps noise; it must not veto the model."""
    seed(oracle, {"gamma_scout:SPY": 0.35})
    assert oracle._stabilize({"gamma_scout:SPY": 0.60}) == {"gamma_scout:SPY": 0.60}


def test_a_brand_new_signal_is_never_damped(oracle):
    seed(oracle, {"gamma_scout:SPY": 0.35})
    out = oracle._stabilize({"gamma_scout:SPY": 0.35, "vol_surfer:NVDA": 0.25})
    assert out["vol_surfer:NVDA"] == 0.25


def test_hysteresis_cannot_push_the_total_over_the_cap(oracle):
    seed(oracle, {"gamma_scout:SPY": 0.7, "vol_surfer:SPY": 0.7})
    out = oracle._stabilize({"vol_surfer:NVDA": 0.7})
    assert sum(out.values()) <= 1.0
    assert sum(out.values()) == pytest.approx(1.0, abs=1e-3)   # 4dp rounding


def test_no_prior_decision_passes_through_unchanged(oracle):
    assert oracle._stabilize({"gamma_scout:SPY": 0.3}) == {"gamma_scout:SPY": 0.3}


# ---------------------------------------------------------------- inputs
def test_data_age_is_bucketed_so_an_unchanged_market_gives_an_unchanged_prompt(oracle):
    """Sub-second jitter in data_age_s was enough to flip the model's output."""
    a = oracle._quantize({"data_age_s": 0.4, "wall_strike": 760.0})
    b = oracle._quantize({"data_age_s": 12.7, "wall_strike": 760.0})
    assert a == b


def test_quantize_preserves_real_freshness_differences(oracle):
    assert oracle._quantize({"data_age_s": 5})["data_age_s"] != \
           oracle._quantize({"data_age_s": 120})["data_age_s"]


def test_quantize_tolerates_a_missing_age(oracle):
    assert oracle._quantize({"wall_strike": 760.0}) == {"wall_strike": 760.0}


# ---------------------------------------------------------------- fallback
def test_fallback_reuses_only_signals_still_on_the_bus(oracle):
    """Funding a thesis nothing is asserting any more is worse than holding cash."""
    seed(oracle, {"gamma_scout:SPY": 0.35, "vol_surfer:NVDA": 0.25})
    d = oracle._fallback({"gamma_scout:SPY": {}}, "", "boom", 10)
    assert d.fallback and d.allocations == {"gamma_scout:SPY": 0.35}


def test_fallback_with_no_history_holds_cash(oracle):
    d = oracle._fallback({"gamma_scout:SPY": {}}, "", "boom", 10)
    assert d.allocations == {} and d.deployed_fraction == 0
