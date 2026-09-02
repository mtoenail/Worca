# swarm/data.py
import threading, time
from datetime import date
# pyrefly: ignore [missing-import]
from alpaca.data.requests import OptionChainRequest, StockLatestTradeRequest
# pyrefly: ignore [missing-import]
from alpaca.trading.requests import GetOptionContractsRequest

class MarketData:
    """One shared, cached view of the market. Fetch once per TTL; both agents read it.

    Every method here is BLOCKING HTTP. Agents must call these via asyncio.to_thread so
    a fetch does not stall the event loop (and, later, the Oracle's LLM call with it).
    Locks are per-underlying so two agents arriving together make one fetch, not two.
    """
    def __init__(self, opt_client, stk_client, trade_client, ttl_s=30):
        self.opt, self.stk, self.trade, self.ttl = opt_client, stk_client, trade_client, ttl_s
        self._cache = {}       # underlying -> (ts, snapshot)
        self._oi_cache = {}    # (underlying, lo, hi, exp_lte) -> (ts, {symbol: oi})
        self._clock = None     # (ts, date) - exchange date, not local date
        self._locks = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, key):
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def today(self, ttl_s=300) -> date:
        """The exchange's current date (Eastern), not the machine's local date.

        Local `date.today()` flips at local midnight, which can shift every DTE
        calculation by a day depending on where the machine is.
        """
        now = time.time()
        if self._clock and now - self._clock[0] < ttl_s:
            return self._clock[1]
        try:
            d = self.trade.get_clock().timestamp.date()
        except Exception:
            d = date.today()                       # degrade rather than halt
        self._clock = (now, d)
        return d

    def get(self, underlying):
        with self._lock_for(("chain", underlying)):
            now = time.time()
            hit = self._cache.get(underlying)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
            spot = self.stk.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=underlying))[underlying].price
            chain = self._clean(self.opt.get_option_chain(
                OptionChainRequest(underlying_symbol=underlying)))
            snap = {"spot": spot, "chain": chain, "ts": now}
            self._cache[underlying] = (now, snap)
            return snap

    def open_interest(self, underlying, strike_lo, strike_hi, exp_lte, ttl_s=3600):
        """OI per contract in a strike/expiration window. 1-day lagged (OCC EOD).

        Cached on the FULL window, not just the underlying: as spot drifts the window
        moves, and a cache keyed on the symbol alone would keep serving OI fetched for
        the old strike range, silently scoring newly in-window contracts as zero.
        """
        lo, hi = round(strike_lo, 2), round(strike_hi, 2)
        key = (underlying, lo, hi, exp_lte)
        with self._lock_for(("oi", key)):
            now = time.time()
            hit = self._oi_cache.get(key)
            if hit and now - hit[0] < ttl_s:
                return hit[1]
            oi, token = {}, None
            while True:
                req = GetOptionContractsRequest(
                    underlying_symbols=[underlying],
                    strike_price_gte=str(lo),
                    strike_price_lte=str(hi),
                    expiration_date_lte=exp_lte,          # a datetime.date
                    status="active", limit=10000, page_token=token)
                resp = self.trade.get_option_contracts(req)
                for c in resp.option_contracts:
                    oi[c.symbol] = int(c.open_interest) if getattr(c, "open_interest", None) else 0
                token = getattr(resp, "next_page_token", None)
                if not token:
                    break
            self._oi_cache[key] = (now, oi)
            return oi

    def _clean(self, chain):
        # Drop contracts that will corrupt GEX/IV: no quote, zero bid, or absurd spread.
        out = {}
        for k, c in chain.items():
            q = getattr(c, "latest_quote", None)
            if not q or not getattr(q, "bid_price", None) or q.bid_price <= 0:
                continue
            if not getattr(q, "ask_price", None) or q.ask_price <= 0:
                continue
            mid = (q.bid_price + q.ask_price) / 2
            if mid <= 0 or (q.ask_price - q.bid_price) / mid > 0.20:   # spread > 20% of mid
                continue
            out[k] = c
        return out
