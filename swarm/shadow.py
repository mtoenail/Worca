# swarm/shadow.py - the solo baseline: what each agent would have done ALONE.
#
# The swarm-vs-solo equity curve is the central claim of this project, so the only
# difference between the two books must be the Oracle. Shadow positions therefore use
# the same intent builder (identical contracts), the same exit rules, and the same
# fixed per-trade size - and are opened for EVERY signal the agent emits, funded or not.
# The swarm book is the real Alpaca account, where the Oracle decides what gets funded.
import asyncio, csv, json, os, time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone

from swarm.exits import exit_reason
from swarm.optiontools import parse_occ

SOLO_ALLOC = 0.10          # a solo agent sizes every signal at the per-trade cap
SOLO_EQUITY = 100_000.0    # notional starting capital for the baseline book


@dataclass
class ShadowPosition:
    signal_id: str
    agent: str
    underlying: str
    strategy: str
    direction: str
    qty: int
    legs: list                       # [{symbol, side, ratio, entry_mid, dte, exp}]
    entry_debit: float               # dollars per unit, positive = paid
    entry_ts: float
    entry_spot: float
    meta: dict = field(default_factory=dict)
    mark: float = 0.0                # current value per unit, dollars
    pnl: float = 0.0                 # total, all contracts
    open: bool = True
    exit_ts: float = 0.0
    exit_reason: str = ""


class ShadowBook:
    """A marked-to-mid paper book with the B2 exit rules, persisted to disk.

    Exits (from PLAN_REVISED B2):
      gamma_scout: +50% / -50% of premium, spot >2% from the wall, or 5 DTE.
      vol_surfer:  |z| back inside 0.5, -100% of the net debit, or 7 DTE on the front leg.
      both:        force-close inside the close blackout during the front leg's expiry week.
    The live executor applies the identical rules to real positions, so the two books
    differ only in which trades got funded.
    """

    def __init__(self, data, bus, *, executor=None, state_path="shadow_book.json",
                 equity_path="shadow_equity.csv", interval_s=60,
                 solo_alloc=SOLO_ALLOC, equity=SOLO_EQUITY):
        self.data, self.bus, self.executor = data, bus, executor
        self.state_path, self.equity_path = state_path, equity_path
        self.interval_s = interval_s
        self.solo_alloc, self.start_equity = solo_alloc, equity
        self.positions: list[ShadowPosition] = []
        self.realized = 0.0
        self._load()

    # ---------- persistence ----------
    def _load(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                blob = json.load(f)
            self.realized = blob.get("realized", 0.0)
            self.positions = [ShadowPosition(**p) for p in blob.get("positions", [])]
        except Exception as e:
            print(f"[shadow] could not load {self.state_path}: {e}")

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"realized": self.realized,
                       "positions": [asdict(p) for p in self.positions]}, f, default=str)
        os.replace(tmp, self.state_path)      # atomic: never leave a half-written book

    # ---------- opening ----------
    def _is_open(self, signal_id):
        return any(p.open and p.signal_id == signal_id for p in self.positions)

    async def on_decision(self, _decision=None):
        """Open a shadow position for every live signal, regardless of allocation.

        Deliberately ignores the Decision: the baseline is 'each agent trades its own
        signals with no Oracle'. It is driven off the Oracle's cadence only so both
        books are evaluated at the same instants.
        """
        if self.executor is None:
            return
        today = await asyncio.to_thread(self.data.today)
        for (agent, underlying), sig in self.bus.snapshot().items():
            sid = f"{agent}:{underlying}"
            if self._is_open(sid):
                continue
            try:
                snap = await asyncio.to_thread(self.data.get, underlying)
                intent, _why = self.executor.build_intent(sig, self.solo_alloc, snap, today)
                if intent is None:
                    continue
                per_unit = intent.max_loss
                if per_unit <= 0:
                    continue
                qty = max(1, int(self.solo_alloc * self.start_equity // per_unit))
                self.positions.append(ShadowPosition(
                    signal_id=sid, agent=agent, underlying=underlying,
                    strategy=intent.strategy, direction=intent.direction, qty=qty,
                    legs=[{"symbol": l.symbol, "side": l.side, "ratio": l.ratio,
                           "entry_mid": l.mid, "dte": l.dte,
                           "exp": parse_occ(l.symbol)[1].isoformat()} for l in intent.legs],
                    entry_debit=intent.net_debit, entry_ts=time.time(),
                    entry_spot=snap["spot"], mark=intent.net_debit, meta=dict(intent.meta)))
                print(f"[shadow] OPEN {sid} {intent.strategy} qty={qty} "
                      f"debit=${intent.net_debit:.2f}")
            except Exception as e:
                print(f"[shadow] open error {sid}: {type(e).__name__}: {e}")
        self._save()

    # ---------- marking ----------
    @staticmethod
    def _mid(chain, symbol):
        c = chain.get(symbol)
        q = getattr(c, "latest_quote", None) if c else None
        if not q or not q.bid_price or not q.ask_price:
            return None                       # dropped by _clean or unquoted right now
        return (float(q.bid_price) + float(q.ask_price)) / 2

    def _mark(self, pos, chain):
        """Current value per unit. Returns None if any leg is unquotable this cycle."""
        total = 0.0
        for leg in pos.legs:
            m = self._mid(chain, leg["symbol"])
            if m is None:
                return None
            total += m * leg["ratio"] * (1 if leg["side"] == "buy" else -1)
        return total * 100

    # ---------- exits (B2) ----------
    def _exit_reason(self, pos, spot, today):
        """Delegates to swarm.exits so the live book cannot diverge from this one."""
        front_dte = min((date.fromisoformat(l["exp"]) - today).days for l in pos.legs)
        live = self.bus.get(pos.agent, pos.underlying)
        return exit_reason(
            pos.agent, entry_debit=pos.entry_debit, mark=pos.mark, front_dte=front_dte,
            spot=spot, wall_strike=pos.meta.get("wall_strike"),
            live_zscore=(live.data.get("zscore") if live else None))

    def _close(self, pos, reason):
        pos.open, pos.exit_ts, pos.exit_reason = False, time.time(), reason
        self.realized += pos.pnl
        print(f"[shadow] CLOSE {pos.signal_id} pnl=${pos.pnl:+.2f} :: {reason}")

    # ---------- the loop ----------
    async def mark_all(self):
        today = await asyncio.to_thread(self.data.today)
        unreal = 0.0
        for pos in self.positions:
            if not pos.open:
                continue
            try:
                snap = await asyncio.to_thread(self.data.get, pos.underlying)
                m = self._mark(pos, snap["chain"])
                if m is not None:                 # stale leg -> hold the previous mark
                    pos.mark = m
                pos.pnl = (pos.mark - pos.entry_debit) * pos.qty
                unreal += pos.pnl
                reason = self._exit_reason(pos, snap["spot"], today)
                if reason:
                    self._close(pos, reason)
                    unreal -= pos.pnl             # now realized, don't double-count
            except Exception as e:
                print(f"[shadow] mark error {pos.signal_id}: {type(e).__name__}: {e}")
        equity = self.start_equity + self.realized + unreal
        self._save()
        self._append_equity(equity, unreal)
        return equity

    def _append_equity(self, equity, unrealized):
        new = not os.path.exists(self.equity_path)
        with open(self.equity_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "iso", "equity", "realized", "unrealized", "open_positions"])
            w.writerow([time.time(), datetime.now(timezone.utc).isoformat(),
                        round(equity, 2), round(self.realized, 2), round(unrealized, 2),
                        sum(1 for p in self.positions if p.open)])

    async def run(self):
        while True:
            try:
                eq = await self.mark_all()
                n = sum(1 for p in self.positions if p.open)
                print(f"[shadow] solo equity ${eq:,.2f} ({n} open, "
                      f"realized ${self.realized:+,.2f})")
            except Exception as e:
                print(f"[shadow] loop error: {type(e).__name__}: {e}")
            await asyncio.sleep(self.interval_s)
