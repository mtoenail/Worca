# swarm/agents/gamma_scout.py
import asyncio, time
from datetime import timedelta
import numpy as np
from swarm.base import Agent
from swarm.schema import Signal
from swarm.optiontools import parse_occ, t_years
from swarm.greeks import bs_gamma, bs_gamma_vec, implied_vol
from swarm.history import RollingHistory, percentile

R = 0.05   # ~risk-free / T-bill rate for the BS fallback

class GammaScout(Agent):
    name = "gamma_scout"

    def __init__(self, bus, data, underlyings, interval_s=60,
                 strike_pct=0.10, flip_pct=0.25, dte_min=7, dte_max=45,
                 proximity=0.01, hist_csv="gex_history.csv", sample_every_s=900):
        super().__init__(bus, underlyings, interval_s)
        self.data = data
        self.strike_pct, self.flip_pct = strike_pct, flip_pct
        self.dte_min, self.dte_max = dte_min, dte_max
        self.proximity = proximity
        self.hist = RollingHistory(hist_csv, sample_every_s=sample_every_s, window=30)

    def _iv_of(self, contract, S, K, T, cp):
        iv = getattr(contract, "implied_volatility", None)
        if iv is not None:
            return iv
        q = contract.latest_quote                       # BS fallback path
        mid = (q.bid_price + q.ask_price) / 2
        return implied_vol(mid, S, K, T, R, call=(cp == "C"))

    def _gamma_of(self, contract, S, K, T, cp, iv):
        g = getattr(getattr(contract, "greeks", None), "gamma", None)
        if g is not None:
            return g
        return bs_gamma(S, K, T, R, iv) if iv else None

    async def analyze(self, underlying):
        # Blocking HTTP - keep it off the event loop.
        snap = await asyncio.to_thread(self.data.get, underlying)
        today = await asyncio.to_thread(self.data.today)
        S, chain = snap["spot"], snap["chain"]
        data_age = round(time.time() - snap["ts"], 1)

        # OI is fetched over the WIDE band: the wall only needs +/-strike_pct, but the
        # flip scan re-prices the book across +/-15% of spot and needs the contracts
        # that dominate gamma at those hypothetical levels, not just the ones near spot.
        f_lo, f_hi = S * (1 - self.flip_pct), S * (1 + self.flip_pct)
        w_lo, w_hi = S * (1 - self.strike_pct), S * (1 + self.strike_pct)
        oi_map = await asyncio.to_thread(
            self.data.open_interest, underlying, f_lo, f_hi,
            today + timedelta(days=self.dte_max))

        gex_all = {}                                     # signed GEX per strike, wide band
        legs = []                                        # (K, T, iv, signed_oi) for the flip scan
        for sym, c in chain.items():
            _, exp, cp, K = parse_occ(sym)
            if not (f_lo <= K <= f_hi):
                continue
            if not (self.dte_min <= (exp - today).days <= self.dte_max):
                continue
            T = t_years(exp, today)
            iv = self._iv_of(c, S, K, T, cp)
            g = self._gamma_of(c, S, K, T, cp, iv)
            if g is None:
                continue
            sign = 1 if cp == "C" else -1                # calls +, puts -
            oi = oi_map.get(sym, 0)
            gex_all[K] = gex_all.get(K, 0.0) + g * oi * sign
            if iv and oi:
                legs.append((K, T, iv, oi * sign))
        if not gex_all:
            return None
        scale = 100 * S * S * 0.01                       # scale to $-ish GEX
        gex_all = {k: v * scale for k, v in gex_all.items()}

        gex_wall = {k: v for k, v in gex_all.items() if w_lo <= k <= w_hi}
        if not gex_wall:
            return None
        wall = max(gex_wall, key=lambda k: abs(gex_wall[k]))
        flip = self._flip_scan(legs, S)
        # float() throughout: feed greeks come back as numpy scalars, and json.dumps
        # refuses np.float64 - which would surface as an Oracle crash, not a data bug.
        net_gex = float(sum(gex_all.values()))
        wall_gex = float(abs(gex_wall[wall]))
        window = self.hist.observe(underlying, wall_gex)
        pctile = percentile(window, wall_gex)
        dist = abs(S - wall) / S

        if dist > self.proximity:                        # only fire when price hugs the wall
            return None
        if flip is not None:
            direction = "bullish" if S > flip else "bearish"
        else:
            direction = "neutral"                        # no zero-crossing -> no directional basis
        # Net gamma sign is a regime read even when the flip point is undefined:
        # net-positive dealer gamma pins price, net-negative accelerates moves.
        regime_hint = "pinning" if net_gex > 0 else "accelerant"

        return Signal(
            agent=self.name, signal_type="gamma_wall_proximity", underlying=underlying,
            strength=round(pctile, 2), direction=direction,
            strength_basis="gex_percentile",
            data={"wall_strike": float(wall), "wall_gex_pctile": round(pctile, 2),
                  "wall_gex": round(wall_gex, 2),
                  "flip_point": flip, "distance_pct": round(float(dist), 4),
                  "net_gex": round(net_gex, 2), "regime_hint": regime_hint,
                  "pctile_window_n": len(window),
                  "dealer_assumption": "long_calls_short_puts",
                  "data_age_s": data_age})

    @staticmethod
    def _flip_scan(legs, S, span=0.15, steps=61):
        """Spot level at which NET dealer gamma changes sign.

        This is the actual definition of the gamma flip point. Cumulating signed GEX
        across strikes - the obvious approach - only finds a crossing when the profile
        happens to cross on the way up; on a put-dominated chain the cumulative sum
        starts negative and never returns, so it reports None on exactly the days the
        flip point matters most. Re-pricing the book across a grid of hypothetical spot
        levels finds it whenever it exists inside the scanned range.
        """
        if not legs:
            return None
        K = np.fromiter((l[0] for l in legs), float, len(legs))
        T = np.fromiter((l[1] for l in legs), float, len(legs))
        iv = np.fromiter((l[2] for l in legs), float, len(legs))
        w = np.fromiter((l[3] for l in legs), float, len(legs))
        grid = np.linspace(S * (1 - span), S * (1 + span), steps)
        net = [float((bs_gamma_vec(s, K, T, R, iv) * w).sum() * 100 * s * s * 0.01)
               for s in grid]
        for i in range(1, len(grid)):
            y0, y1 = net[i - 1], net[i]
            if (y0 <= 0 < y1) or (y0 >= 0 > y1):
                x0, x1 = grid[i - 1], grid[i]
                return round(float(x0 - y0 * (x1 - x0) / (y1 - y0)), 2)   # interpolate
        return None
