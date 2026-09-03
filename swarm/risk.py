# swarm/risk.py - the gates. Written BEFORE the executor, on purpose.
#
# The order types live here rather than in execution.py so that constructing an order
# and checking an order cannot drift apart: the executor cannot build an OrderIntent
# without importing this module, and `RiskManager.check` is the only thing that returns
# permission to send one.
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timezone

# US options close 16:00 ET. We refuse new entries inside the blackout before that.
MARKET_CLOSE_ET = dtime(16, 0)


@dataclass
class Leg:
    symbol: str                       # OCC symbol
    side: str                         # "buy" | "sell"
    ratio: int = 1                    # contracts per unit of qty
    bid: float = 0.0
    ask: float = 0.0
    dte: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid and self.ask else 0.0

    @property
    def rel_spread(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / m if m > 0 else 1.0


@dataclass
class OrderIntent:
    signal_id: str                    # "gamma_scout:SPY" - ties the order to its thesis
    agent: str
    underlying: str
    strategy: str                     # "single_leg" | "calendar"
    direction: str                    # "bullish" | "bearish" | "neutral"
    legs: list[Leg]
    alloc: float                      # the Oracle's fraction of portfolio for this signal
    qty: int = 0                      # contracts, filled in by RiskManager.size()
    meta: dict = field(default_factory=dict)

    @property
    def net_debit(self) -> float:
        """Dollars per unit of qty. Positive = debit paid, negative = credit received."""
        s = sum((leg.mid * leg.ratio * (1 if leg.side == "buy" else -1)) for leg in self.legs)
        return s * 100

    @property
    def max_loss(self) -> float:
        """Worst case per unit of qty, for sizing.

        A long single leg risks the premium. A calendar is a net debit whose defined
        risk is that debit - the short leg is covered by the longer-dated long leg,
        which is why it is the only spread this system is allowed to sell into.
        """
        return abs(self.net_debit)


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)   # every failed gate, not just the first
    qty: int = 0
    notes: dict = field(default_factory=dict)

    def __bool__(self):
        return self.ok


class RiskManager:
    """Every gate the plan specifies, in one place, evaluated before any order is sent.

    `check` returns ALL failed gates rather than short-circuiting, so a rejected order
    logs the complete reason it was rejected - which is what the dashboard shows and
    what a judge asks about.
    """

    def __init__(self, trade_client, *,
                 max_per_trade=0.10,        # one position may risk 10% of equity
                 max_total_deployed=0.50,   # ... and everything open, 50%
                 max_positions=6,
                 max_contracts=10,          # hard cap: paper account, thin books
                 min_contracts=1,
                 dte_min=7, dte_max=45,     # B3: gamma_scout's tradeable band
                 calendar_front_min_dte=6,  # A2: front leg must clear the DTE floor
                 max_rel_spread=0.10,       # B3: tighter than _clean's 20% - we cross it
                 daily_loss_limit=0.03,     # kill switch: 3% of the day's opening equity
                 close_blackout_min=15,
                 allow_neutral_directional=False):
        self.trade = trade_client
        self.max_per_trade, self.max_total_deployed = max_per_trade, max_total_deployed
        self.max_positions, self.max_contracts = max_positions, max_contracts
        self.min_contracts = min_contracts
        self.dte_min, self.dte_max = dte_min, dte_max
        self.calendar_front_min_dte = calendar_front_min_dte
        self.max_rel_spread = max_rel_spread
        self.daily_loss_limit = daily_loss_limit
        self.close_blackout_min = close_blackout_min
        self.allow_neutral_directional = allow_neutral_directional
        self._day: date | None = None
        self._day_open_equity: float | None = None
        self.halted = False
        self.halt_reason = ""

    # ---------- account state ----------
    def account(self):
        a = self.trade.get_account()
        return {"equity": float(a.equity),
                "buying_power": float(a.options_buying_power
                                      if getattr(a, "options_buying_power", None)
                                      else a.buying_power),
                "blocked": bool(getattr(a, "trading_blocked", False)
                                or getattr(a, "account_blocked", False))}

    def _roll_day(self, equity, today):
        """Anchor the daily loss limit to the equity at the first check of each session."""
        if self._day != today:
            self._day, self._day_open_equity = today, equity
            self.halted, self.halt_reason = False, ""

    # ---------- sizing ----------
    def size(self, intent: OrderIntent, equity: float) -> int:
        """Contracts such that worst-case loss <= min(alloc, max_per_trade) x equity.

        The Oracle's allocation is a CEILING, not a target: risk narrows it, never widens
        it. A signal allocated 0.7 on a position whose per-trade cap is 0.10 gets 0.10.
        """
        budget = min(intent.alloc, self.max_per_trade) * equity
        per_unit = intent.max_loss
        if per_unit <= 0:
            return 0
        return max(0, min(self.max_contracts, int(math.floor(budget / per_unit))))

    # ---------- gates ----------
    def check(self, intent: OrderIntent, *, now=None, clock=None) -> Verdict:
        reasons, notes = [], {}
        now = now or datetime.now(timezone.utc)

        # --- 0. global halt (set by the daily loss kill switch) ---
        if self.halted:
            reasons.append(f"halted: {self.halt_reason}")

        # --- 1. account ---
        try:
            acct = self.account()
        except Exception as e:
            return Verdict(False, [f"account_unavailable: {type(e).__name__}: {e}"])
        equity, bp = acct["equity"], acct["buying_power"]
        notes["equity"], notes["buying_power"] = equity, bp
        if acct["blocked"]:
            reasons.append("account_blocked")

        # --- 2. daily loss limit ---
        self._roll_day(equity, now.date())
        if self._day_open_equity:
            dd = (self._day_open_equity - equity) / self._day_open_equity
            notes["daily_drawdown"] = round(dd, 4)
            if dd >= self.daily_loss_limit:
                self.halted = True
                self.halt_reason = f"daily loss {dd:.2%} >= {self.daily_loss_limit:.2%}"
                reasons.append(f"daily_loss_limit: {self.halt_reason}")

        # --- 3. market open, and not inside the close blackout ---
        if clock is not None:
            if not getattr(clock, "is_open", False):
                reasons.append("market_closed")
            else:
                nc = getattr(clock, "next_close", None)
                if nc is not None:
                    mins = (nc - clock.timestamp).total_seconds() / 60
                    notes["minutes_to_close"] = round(mins, 1)
                    if mins < self.close_blackout_min:
                        reasons.append(f"close_blackout: {mins:.0f}m to close")

        # --- 4. direction (B3): never let a neutral signal become a directional order ---
        if intent.strategy == "single_leg" and intent.direction == "neutral" \
                and not self.allow_neutral_directional:
            reasons.append("neutral_direction: no directional basis, declining to trade")

        # --- 5. legs: DTE band and the spread we have to cross ---
        if not intent.legs:
            reasons.append("no_legs")
        for i, leg in enumerate(intent.legs):
            tag = f"leg{i}({leg.symbol})"
            if leg.bid <= 0 or leg.ask <= 0:
                reasons.append(f"{tag}_unquoted")
                continue
            if leg.rel_spread > self.max_rel_spread:
                reasons.append(f"{tag}_spread {leg.rel_spread:.1%} > {self.max_rel_spread:.0%}")
            if intent.strategy == "single_leg":
                if not (self.dte_min <= leg.dte <= self.dte_max):
                    reasons.append(f"{tag}_dte {leg.dte} outside "
                                   f"[{self.dte_min},{self.dte_max}]")
            elif leg.dte < self.calendar_front_min_dte:
                reasons.append(f"{tag}_dte {leg.dte} < {self.calendar_front_min_dte}")

        # --- 6. a calendar must be a net debit on distinct expirations ---
        if intent.strategy == "calendar":
            dtes = [leg.dte for leg in intent.legs]
            if len(set(dtes)) < 2:
                reasons.append("calendar_same_expiry")
            if intent.net_debit <= 0:
                reasons.append(f"calendar_not_a_debit: {intent.net_debit:.2f}")

        # --- 7. concurrency and aggregate exposure ---
        try:
            positions = self.trade.get_all_positions()
        except Exception as e:
            return Verdict(False, reasons + [f"positions_unavailable: {type(e).__name__}: {e}"])
        opts = [p for p in positions if getattr(p, "asset_class", "") == "us_option"
                or len(getattr(p, "symbol", "")) > 6]
        notes["open_positions"] = len(opts)
        if len(opts) >= self.max_positions:
            reasons.append(f"max_positions {len(opts)} >= {self.max_positions}")
        # Don't stack a second position on a symbol we already hold - the Oracle
        # re-allocates every cycle and would otherwise pyramid the same thesis.
        held = {getattr(p, "symbol", "") for p in opts}
        for leg in intent.legs:
            if leg.symbol in held:
                reasons.append(f"already_holding {leg.symbol}")
        deployed = sum(abs(float(getattr(p, "market_value", 0) or 0)) for p in opts)
        notes["deployed_fraction"] = round(deployed / equity, 4) if equity else 0.0

        # --- 8. size it, then re-check the aggregate cap with this order included ---
        qty = self.size(intent, equity)
        notes["sized_qty"] = qty
        if qty < self.min_contracts:
            reasons.append(f"size_too_small: {qty} contract(s) at "
                           f"${intent.max_loss:.0f}/unit vs budget "
                           f"${min(intent.alloc, self.max_per_trade) * equity:.0f}")
        cost = qty * intent.max_loss
        notes["order_cost"] = round(cost, 2)
        if equity and (deployed + cost) / equity > self.max_total_deployed:
            reasons.append(f"max_total_deployed: "
                           f"{(deployed + cost) / equity:.1%} > {self.max_total_deployed:.0%}")
        if cost > bp:
            reasons.append(f"insufficient_buying_power: ${cost:.0f} > ${bp:.0f}")

        return Verdict(not reasons, reasons, qty, notes)
