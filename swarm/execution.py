# swarm/execution.py - turns an Oracle allocation into a risk-checked options order.
#
# The only path from a signal to a broker order. Every intent goes through
# RiskManager.check first; a rejected intent is logged with every gate it failed and
# nothing is sent. There is deliberately no "force" argument.
import asyncio, json, os, time, uuid
from datetime import datetime, timezone

# pyrefly: ignore [missing-import]
from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce
# pyrefly: ignore [missing-import]
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

from swarm.exits import exit_reason
from swarm.greeks import bs_delta, implied_vol
from swarm.optiontools import parse_occ, t_years
from swarm.risk import Leg, OrderIntent

R = 0.05
TARGET_DELTA = 0.35          # B3: gamma_scout buys the ~0.35-delta option


def _quote(contract):
    q = getattr(contract, "latest_quote", None)
    if not q:
        return 0.0, 0.0
    return float(q.bid_price or 0), float(q.ask_price or 0)


class Executor:
    """Builds, gates, and submits orders. Defaults to dry_run - arm it explicitly."""

    def __init__(self, bus, data, trade_client, risk, *,
                 log_path="trade_log.jsonl", dry_run=True, limit_slippage=0.25):
        self.bus, self.data, self.trade, self.risk = bus, data, trade_client, risk
        self.log_path, self.dry_run = log_path, dry_run
        # Fraction of the bid-ask spread we are willing to give up to get filled.
        # 0.0 = mid (may never fill), 1.0 = pay the ask (always fills, worst price).
        self.limit_slippage = limit_slippage
        self._sent: dict[str, float] = {}    # signal_id -> ts, so one cycle sends once
        self.position_manager = None         # set by main once both exist

    # ---------- strike selection ----------
    def _delta_of(self, c, S, K, T, cp):
        d = getattr(getattr(c, "greeks", None), "delta", None)
        if d is not None:
            return float(d)
        iv = getattr(c, "implied_volatility", None)
        if iv is None:
            bid, ask = _quote(c)
            iv = implied_vol((bid + ask) / 2, S, K, T, R, call=(cp == "C"))
        return bs_delta(S, K, T, R, iv, call=(cp == "C")) if iv else None

    def _pick_delta_strike(self, chain, S, today, cp, dte_min, dte_max):
        """The ~0.35-delta contract on the nearest expiration inside the DTE band.

        Expiration is chosen first and independently of delta: picking the globally
        closest delta would drift the trade onto whatever expiration happens to quote
        a 0.35 strike, which is not the horizon the signal was measured over.
        """
        cands = []
        for sym, c in chain.items():
            _, exp, ocp, K = parse_occ(sym)
            if ocp != cp:
                continue
            dte = (exp - today).days
            if not (dte_min <= dte <= dte_max):
                continue
            cands.append((dte, exp, sym, c, K))
        if not cands:
            return None
        near_exp = min(c[1] for c in cands)
        best, best_gap = None, 9e9
        for dte, exp, sym, c, K in cands:
            if exp != near_exp:
                continue
            d = self._delta_of(c, S, K, t_years(exp, today), cp)
            if d is None:
                continue
            gap = abs(abs(d) - TARGET_DELTA)
            if gap < best_gap:
                best, best_gap = (sym, c, K, dte, d), gap
        return best

    # ---------- intent builders ----------
    def _build_single_leg(self, sig, alloc, snap, today):
        """gamma_scout -> one long option in the signal's direction."""
        if sig.direction == "neutral":
            return None, "neutral direction - no directional basis"
        cp = "C" if sig.direction == "bullish" else "P"
        pick = self._pick_delta_strike(snap["chain"], snap["spot"], today, cp,
                                       self.risk.dte_min, self.risk.dte_max)
        if not pick:
            return None, f"no {cp} contract with a usable delta in the DTE band"
        sym, c, K, dte, delta = pick
        bid, ask = _quote(c)
        leg = Leg(symbol=sym, side="buy", ratio=1, bid=bid, ask=ask, dte=dte)
        return OrderIntent(
            signal_id=f"{sig.agent}:{sig.underlying}", agent=sig.agent,
            underlying=sig.underlying, strategy="single_leg", direction=sig.direction,
            legs=[leg], alloc=alloc,
            meta={"strike": K, "delta": round(delta, 3), "dte": dte,
                  "wall_strike": sig.data.get("wall_strike"),
                  "flip_point": sig.data.get("flip_point"),
                  "regime_hint": sig.data.get("regime_hint"),
                  "spot_at_entry": snap["spot"]}), None

    def _build_calendar(self, sig, alloc, snap, today):
        """vol_surfer -> sell the front ATM, buy the back ATM, on the SIGNAL's expirations.

        The expirations travel in the signal payload rather than being re-derived here:
        re-picking them would let the order be placed on a different calendar than the
        one the z-score was measured on.
        """
        from datetime import date as _date
        try:
            front = _date.fromisoformat(sig.data["front_exp"])
            back = _date.fromisoformat(sig.data["back_exp"])
        except (KeyError, ValueError):
            return None, "signal carries no front_exp/back_exp"
        if front == back:
            return None, "front and back resolve to the same expiration"

        S = snap["spot"]
        # The strike must be quoted on BOTH expirations - a calendar on two different
        # strikes is a diagonal, which is not the position this signal justifies.
        by_exp = {front: {}, back: {}}
        for sym, c in snap["chain"].items():
            _, exp, cp, K = parse_occ(sym)
            if cp != "C" or exp not in by_exp:
                continue
            by_exp[exp][K] = (sym, c)
        common = set(by_exp[front]) & set(by_exp[back])
        if not common:
            return None, "no strike quoted on both expirations"
        K = min(common, key=lambda k: abs(k - S))

        f_sym, f_c = by_exp[front][K]
        b_sym, b_c = by_exp[back][K]
        f_bid, f_ask = _quote(f_c)
        b_bid, b_ask = _quote(b_c)
        legs = [Leg(f_sym, "sell", 1, f_bid, f_ask, (front - today).days),
                Leg(b_sym, "buy", 1, b_bid, b_ask, (back - today).days)]
        return OrderIntent(
            signal_id=f"{sig.agent}:{sig.underlying}", agent=sig.agent,
            underlying=sig.underlying, strategy="calendar", direction="neutral",
            legs=legs, alloc=alloc,
            meta={"strike": K, "front_exp": front.isoformat(), "back_exp": back.isoformat(),
                  "front_iv": sig.data.get("front_iv"), "back_iv": sig.data.get("back_iv"),
                  "zscore": sig.data.get("zscore"), "spot_at_entry": S}), None

    def build_intent(self, sig, alloc, snap, today):
        """Signal -> OrderIntent, or (None, why). The single construction path.

        Public because the shadow book calls it too: the solo baseline must hold the
        identical contracts the swarm would have traded, or the swarm-vs-solo curve is
        comparing strike selection rather than allocation.
        """
        if sig.agent == "gamma_scout":
            return self._build_single_leg(sig, alloc, snap, today)
        if sig.agent == "vol_surfer":
            return self._build_calendar(sig, alloc, snap, today)
        return None, f"no execution path for agent {sig.agent}"

    # ---------- submission ----------
    def _limit_price(self, intent):
        """Cross a fraction of the spread, on the correct side of the net position."""
        net_mid = intent.net_debit / 100
        half = sum(((leg.ask - leg.bid) / 2) * leg.ratio for leg in intent.legs)
        px = net_mid + self.limit_slippage * half   # debit: pay up; credit: net_mid is <0
        return round(max(px, 0.01), 2)

    def _request(self, intent, qty, limit_px, coid):
        if intent.strategy == "single_leg":
            leg = intent.legs[0]
            return LimitOrderRequest(
                symbol=leg.symbol, qty=qty,
                side=OrderSide.BUY if leg.side == "buy" else OrderSide.SELL,
                type=OrderType.LIMIT, time_in_force=TimeInForce.DAY,
                limit_price=limit_px, client_order_id=coid)
        return LimitOrderRequest(
            qty=qty, order_class=OrderClass.MLEG, type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY, limit_price=limit_px, client_order_id=coid,
            legs=[OptionLegRequest(
                symbol=leg.symbol, ratio_qty=leg.ratio,
                side=OrderSide.BUY if leg.side == "buy" else OrderSide.SELL)
                for leg in intent.legs])

    def _log(self, rec):
        rec["ts"] = datetime.now(timezone.utc).isoformat()
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        # The position manager learns about new trades here rather than by polling
        # Alpaca, so a submitted spread is under exit management immediately.
        if self.position_manager is not None:
            self.position_manager.record(rec)

    async def execute(self, intent):
        """Gate, then send. The gate runs even in dry_run, so rejections are real."""
        clock = None
        try:
            clock = await asyncio.to_thread(self.trade.get_clock)
        except Exception:
            pass                                    # gate 3 simply won't apply
        verdict = await asyncio.to_thread(lambda: self.risk.check(intent, clock=clock))
        base = {"signal_id": intent.signal_id, "strategy": intent.strategy,
                "direction": intent.direction, "alloc": intent.alloc,
                "legs": [{"symbol": l.symbol, "side": l.side, "bid": l.bid, "ask": l.ask,
                          "dte": l.dte, "rel_spread": round(l.rel_spread, 4)}
                         for l in intent.legs],
                "net_debit": round(intent.net_debit, 2), "meta": intent.meta,
                "risk_notes": verdict.notes}
        if not verdict.ok:
            self._log({**base, "event": "rejected", "reasons": verdict.reasons})
            print(f"[exec] REJECT {intent.signal_id} :: {'; '.join(verdict.reasons)}")
            return None

        qty, limit_px = verdict.qty, self._limit_price(intent)
        coid = f"{intent.agent[:4]}-{intent.underlying}-{uuid.uuid4().hex[:8]}"
        if self.dry_run:
            self._log({**base, "event": "dry_run", "qty": qty, "limit_price": limit_px,
                       "client_order_id": coid})
            print(f"[exec] DRY {intent.signal_id} {intent.strategy} qty={qty} @ {limit_px}")
            return None
        try:
            order = await asyncio.to_thread(
                self.trade.submit_order, self._request(intent, qty, limit_px, coid))
        except Exception as e:
            self._log({**base, "event": "submit_error", "qty": qty,
                       "limit_price": limit_px, "error": f"{type(e).__name__}: {e}"})
            print(f"[exec] SUBMIT ERROR {intent.signal_id}: {type(e).__name__}: {e}")
            return None
        self._log({**base, "event": "submitted", "qty": qty, "limit_price": limit_px,
                   "client_order_id": coid, "order_id": str(order.id),
                   "status": str(order.status)})
        print(f"[exec] SENT {intent.signal_id} {intent.strategy} qty={qty} @ {limit_px} "
              f"id={order.id}")
        return order

    # ---------- the Oracle hands us a Decision ----------
    async def on_decision(self, decision, cooldown_s=900):
        """Build and submit one order per allocated signal.

        `cooldown_s` stops the Oracle's every-5-minute re-allocation from re-sending the
        same thesis; the risk manager's already_holding gate is the second line of
        defence, but it only engages once a position actually exists.
        """
        if not decision.allocations:
            return
        today = await asyncio.to_thread(self.data.today)
        for sig_id, alloc in decision.allocations.items():
            agent, _, underlying = sig_id.partition(":")
            sig = self.bus.get(agent, underlying)
            if sig is None:
                continue
            last = self._sent.get(sig_id)
            if last and time.time() - last < cooldown_s:
                continue
            try:
                snap = await asyncio.to_thread(self.data.get, underlying)
                intent, why = self.build_intent(sig, alloc, snap, today)
                if intent is None:
                    self._log({"event": "no_intent", "signal_id": sig_id,
                               "alloc": alloc, "reason": why})
                    print(f"[exec] SKIP {sig_id}: {why}")
                    continue
                self._sent[sig_id] = time.time()
                await self.execute(intent)
            except Exception as e:
                self._log({"event": "build_error", "signal_id": sig_id,
                           "error": f"{type(e).__name__}: {e}"})
                print(f"[exec] BUILD ERROR {sig_id}: {type(e).__name__}: {e}")


class PositionManager:
    """Applies the B2 exit rules to LIVE positions, using the same swarm.exits module
    the shadow book uses.

    Live trades are tracked from `trade_log.jsonl` rather than from Alpaca's position
    list: a calendar shows up there as two unrelated option positions, and reconstructing
    which two were one spread - and which thesis they came from - is only possible from
    the record we wrote when we sent the order.
    """

    def __init__(self, bus, data, trade_client, executor, *, interval_s=60):
        self.bus, self.data, self.trade = bus, data, trade_client
        self.execu, self.interval_s = executor, interval_s
        self.open_trades: dict[str, dict] = {}      # client_order_id -> record
        self._rehydrate()

    def _rehydrate(self):
        """Rebuild open trades from the log so a restart does not orphan a position."""
        path = self.execu.log_path
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                coid = r.get("client_order_id")
                if not coid:
                    continue
                if r.get("event") == "submitted":
                    self.open_trades[coid] = r
                elif r.get("event") in ("closed", "close_submitted"):
                    self.open_trades.pop(coid, None)

    def record(self, log_record):
        if log_record.get("event") == "submitted":
            self.open_trades[log_record["client_order_id"]] = log_record

    def _mark(self, rec, chain):
        total = 0.0
        for leg in rec["legs"]:
            c = chain.get(leg["symbol"])
            q = getattr(c, "latest_quote", None) if c else None
            if not q or not q.bid_price or not q.ask_price:
                return None
            mid = (float(q.bid_price) + float(q.ask_price)) / 2
            total += mid * (1 if leg["side"] == "buy" else -1)
        return total * 100

    async def _close(self, coid, rec, reason):
        """Reverse the original order: same legs, opposite sides, marketable limit."""
        legs = rec["legs"]
        try:
            if len(legs) == 1:
                req = LimitOrderRequest(
                    symbol=legs[0]["symbol"], qty=rec["qty"],
                    side=OrderSide.SELL if legs[0]["side"] == "buy" else OrderSide.BUY,
                    type=OrderType.LIMIT, time_in_force=TimeInForce.DAY,
                    limit_price=rec.get("exit_limit", 0.01),
                    client_order_id=f"x-{coid[:24]}")
            else:
                req = LimitOrderRequest(
                    qty=rec["qty"], order_class=OrderClass.MLEG, type=OrderType.LIMIT,
                    time_in_force=TimeInForce.DAY, limit_price=rec.get("exit_limit", 0.01),
                    client_order_id=f"x-{coid[:24]}",
                    legs=[OptionLegRequest(
                        symbol=l["symbol"], ratio_qty=1,
                        side=OrderSide.SELL if l["side"] == "buy" else OrderSide.BUY)
                        for l in legs])
            order = await asyncio.to_thread(self.trade.submit_order, req)
            self.execu._log({"event": "close_submitted", "client_order_id": coid,
                             "signal_id": rec.get("signal_id"), "reason": reason,
                             "order_id": str(order.id)})
            print(f"[exit] CLOSE {rec.get('signal_id')} :: {reason}")
        except Exception as e:
            self.execu._log({"event": "close_error", "client_order_id": coid,
                             "reason": reason, "error": f"{type(e).__name__}: {e}"})
            print(f"[exit] CLOSE ERROR {coid}: {type(e).__name__}: {e}")
            return
        self.open_trades.pop(coid, None)

    async def check_exits(self):
        if not self.open_trades:
            return
        today = await asyncio.to_thread(self.data.today)
        for coid, rec in list(self.open_trades.items()):
            try:
                snap = await asyncio.to_thread(self.data.get, rec["underlying"])
                mark = self._mark(rec, snap["chain"])
                if mark is None:
                    continue                       # unquotable this cycle - hold
                front_dte = min((parse_occ(l["symbol"])[1] - today).days
                                for l in rec["legs"])
                live = self.bus.get(rec["agent"], rec["underlying"])
                reason = exit_reason(
                    rec["agent"], entry_debit=rec["net_debit"], mark=mark,
                    front_dte=front_dte, spot=snap["spot"],
                    wall_strike=(rec.get("meta") or {}).get("wall_strike"),
                    live_zscore=(live.data.get("zscore") if live else None))
                if reason:
                    # Exit at a marketable limit: give up the same fraction of the
                    # spread on the way out that we paid on the way in.
                    rec["exit_limit"] = round(max(mark / 100 * 0.9, 0.01), 2)
                    await self._close(coid, rec, reason)
            except Exception as e:
                print(f"[exit] check error {coid}: {type(e).__name__}: {e}")

    async def run(self):
        while True:
            try:
                await self.check_exits()
            except Exception as e:
                print(f"[exit] loop error: {type(e).__name__}: {e}")
            await asyncio.sleep(self.interval_s)
