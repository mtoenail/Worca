# swarm/agents/vol_surfer.py
import asyncio, statistics, time
from swarm.base import Agent
from swarm.schema import Signal
from swarm.optiontools import parse_occ, t_years
from swarm.greeks import implied_vol
from swarm.history import RollingHistory

R = 0.05

class VolSurfer(Agent):
    name = "vol_surfer"

    def __init__(self, bus, data, underlyings, interval_s=60,
                 z_threshold=1.5, hist_csv="volspread_history.csv", sample_every_s=900,
                 front_dte=14, mid_dte=35, back_dte=70, min_dte=6):
        super().__init__(bus, underlyings, interval_s)
        self.data = data
        self.z_threshold = z_threshold
        self.front_dte, self.mid_dte, self.back_dte = front_dte, mid_dte, back_dte
        self.min_dte = min_dte
        self.hist = RollingHistory(hist_csv, sample_every_s=sample_every_s, window=30)

    @staticmethod
    def _pick(exps, today, target, min_dte):
        """Expiration nearest `target` DTE, never inside the tradeable DTE floor.

        Selecting by position (exps[0] / exps[-1]) picks whatever the chain happens to
        list first and last - on SPY that is a 1-DTE weekly against a 2+ year LEAP,
        which is neither a mean-reverting relationship nor a tradeable calendar spread.
        """
        cands = [e for e in exps if (e - today).days > min_dte]
        return min(cands, key=lambda e: abs((e - today).days - target)) if cands else None

    def _atm_iv(self, chain, S, target_exp, today):
        """Average of call and put IV at the strike nearest spot.

        Taking whichever leg iteration reaches first makes the result depend on dict
        order, and call/put IV differ by skew - so the spread would carry an arbitrary
        wobble. Averaging both legs is the standard ATM proxy and is order-independent.
        """
        # Group IVs by strike in one pass, then take the strike nearest spot.
        by_strike = {}
        for sym, c in chain.items():
            _, exp, cp, K = parse_occ(sym)
            if exp != target_exp:
                continue
            iv = getattr(c, "implied_volatility", None)
            if iv is None:
                q = c.latest_quote
                mid = (q.bid_price + q.ask_price) / 2
                iv = implied_vol(mid, S, K, t_years(exp, today), R, call=(cp == "C"))
            if iv:
                by_strike.setdefault(K, {})[cp] = iv
        # Only strikes quoting BOTH legs are usable; a one-sided strike is skipped
        # rather than guessed, so the result never depends on iteration order.
        usable = [K for K, legs in by_strike.items() if "C" in legs and "P" in legs]
        if not usable:
            return None, None
        atm = min(usable, key=lambda K: abs(K - S))
        legs = by_strike[atm]
        return (legs["C"] + legs["P"]) / 2, atm

    async def analyze(self, underlying):
        snap = await asyncio.to_thread(self.data.get, underlying)
        today = await asyncio.to_thread(self.data.today)
        S, chain = snap["spot"], snap["chain"]
        data_age = round(time.time() - snap["ts"], 1)

        exps = sorted({parse_occ(s)[1] for s in chain if parse_occ(s)[1] > today})
        front = self._pick(exps, today, self.front_dte, self.min_dte)
        back = self._pick(exps, today, self.back_dte, self.min_dte)
        if not front or not back or front == back:
            return None

        f_iv, f_k = self._atm_iv(chain, S, front, today)
        b_iv, b_k = self._atm_iv(chain, S, back, today)
        if not f_iv or not b_iv:
            return None
        spread = f_iv - b_iv
        window = self.hist.observe(underlying, spread)
        if len(window) < 5:                          # not enough history yet -> no signal
            return None
        mean, std = statistics.mean(window), statistics.pstdev(window)
        z = (spread - mean) / std if std else 0.0
        if abs(z) < self.z_threshold:
            return None

        return Signal(
            agent=self.name, signal_type="calendar_anomaly", underlying=underlying,
            strength=round(min(abs(z) / 3, 1.0), 2), direction="neutral",
            strength_basis="iv_spread_zscore",
            # front/back expirations travel WITH the signal so the executor builds the
            # calendar spread on exactly the legs the dislocation was measured on.
            data={"front_iv": round(f_iv, 4), "back_iv": round(b_iv, 4),
                  "front_exp": front.isoformat(), "back_exp": back.isoformat(),
                  "front_dte": (front - today).days, "back_dte": (back - today).days,
                  "spread": round(spread, 4), "zscore": round(z, 2),
                  "atm_strike": f_k, "back_strike": b_k, "spot": round(S, 2),
                  "zscore_window_n": len(window),
                  "data_age_s": data_age})
