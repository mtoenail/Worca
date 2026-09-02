# Quantamental Swarm — Revised Plan (audited 2026-09-02)

Supersedes Days 2–5 of the original implementation plan. Days 1 and §1 prerequisites
stand as written. Everything below is either a correction to shipped Day-2 code or a
change to the remaining schedule, and each item names the evidence that motivated it.

---

## A. Day-2 corrections (apply before starting Day 3)

These are ordered by how much they change the numbers the Oracle will see.

### A1. Decouple the rolling window from the poll interval

**Evidence.** Both agents append one history row per `analyze()` call at `interval_s=60`,
so `window=30` covers 30 minutes. Replaying `_rolling_pctile` over the recorded
`gex_history.csv` (57 obs/underlying): SPY GEX varied 8.6% across the session, and the
resulting percentile sat at ≤0.1 on 21% of observations and ≥0.9 on 23% — 44% pinned at
the rails. The percentile is measuring intraday drift, not concentration rarity, and it
is the value that becomes `Signal.strength`.

**Fix.** Separate *observation* from *sampling*. Write at most one history row per
`sample_every_s` (default 900s), and keep the in-memory series for the live comparison:

```python
def _rolling_pctile(self, underlying, value, window=30, sample_every_s=900):
    rows = self._read_hist(underlying)          # cached in memory, re-read only on write
    now = time.time()
    if not rows or now - rows[-1][0] >= sample_every_s:
        self._append_hist(underlying, now, value)
    vals = [v for _, v in rows][-window:]
    return sum(1 for v in vals if v <= value) / len(vals) if vals else 0.5
```

Add a timestamp column to both CSVs (`underlying,ts,value`) so the window is defined in
time, not in rows, and so the dashboard can plot a real time series. Migrate the existing
files or start fresh — 57 rows of 60s-spaced data is not worth preserving.

Apply the identical change to `VolSurfer._rolling_stats`.

**Consequence for the write-up:** state that GEX percentile is ranked against a
15-minute-sampled window, and that with under a week of history the rank is indicative,
not a distributional claim.

> **Cost of this fix — start the swarm early.** At `sample_every_s=900`, Vol Surfer's
> 5-observation minimum takes **75 minutes** to reach after a restart (it was ~5 minutes
> at the old 60s cadence), and a full 30-observation window takes 7.5 hours. Gamma Scout's
> percentile is similarly uninformative until the window fills. Leave the swarm running
> from now through submission; do not restart it casually. If you need signals sooner for
> a demo rehearsal, construct the agents with `sample_every_s=120` — but note in the
> write-up which cadence produced the numbers you show.

### A2. Bound Vol Surfer's expiration selection

**Evidence.** Live SPY chain, 2026-09-02: 31 expirations, `exps[0]` = 1 DTE,
`exps[-1]` = 835 DTE (2028-12-15 LEAP). The shipped code measures the IV spread between a
1-day option and a 2.3-year LEAP. That is not a mean-reverting calendar relationship, and
the front leg violates the Day-3 `>5 DTE` gate, so the signal cannot be traded as measured.

**Fix.** Pick expirations by target DTE, and pick the three the plan text always described:

```python
FRONT_DTE, MID_DTE, BACK_DTE = 14, 35, 70

def _pick(self, exps, today, target):
    cands = [e for e in exps if (e - today).days > 5]
    return min(cands, key=lambda e: abs((e - today).days - target)) if cands else None
```

Emit `front_dte` and `back_dte` in the signal payload so the executor can build the
calendar spread from the exact expirations the signal was computed on. Skip the cycle if
front and back resolve to the same expiration.

### A3. Make the C/P choice in `_atm_iv` deterministic

**Evidence.** `if exp != target_exp or abs(K - S) >= best_dist: continue` keeps whichever
of the call/put pair at the nearest strike dict iteration reaches first. Call and put IV
differ by skew, so the spread carries an order-dependent wobble.

**Fix.** Collect both legs at the nearest strike and average them (a standard ATM IV
proxy, and closer to put-call parity than either leg alone). Return `None` if only one
side is present rather than silently using a single leg.

### A4. Handle `flip_point is None` explicitly

**Evidence.** Live SPY run: `wall=760.0, flip=None → direction='neutral'`. Cumulative
signed GEX does not cross zero inside the ±10% / 7–45 DTE window on SPY, so the
*directional* agent has no direction on the primary underlying.

**Fix — the algorithm was wrong, not just the window.** Widening the strike band was tried
first and did not resolve it: SPY's cumulative signed GEX runs negative from the lowest
strike upward and never returns, so it still reported `None`. Cumulating across strikes
only finds a crossing when the profile happens to cross on the way up — it fails on exactly
the put-heavy days when the flip point matters most.

Replaced with a **spot scan**, which is the actual definition of the flip point: re-price
the whole book across a grid of hypothetical spot levels (±15%, 61 steps) and find where
net dealer gamma changes sign, interpolating the crossing. Vectorised via a new
`bs_gamma_vec` in `greeks.py`, so the ~120k gamma evaluations per cycle stay cheap.

Live result on SPY: `flip_point = 772.96` against spot 762.15 → `direction="bearish"`,
consistent with net GEX of −$4.1B (accelerant regime). Previously `None`/`neutral`.

Retained from the original fix:
- OI is fetched over a wider `flip_pct` band (±25%) than the wall band (±10%), because the
  scan needs contracts that dominate gamma away from spot.
- `regime_hint` (`"pinning"` / `"accelerant"`, from the sign of net GEX) is emitted
  regardless, so the Oracle has a regime read even when the scan finds no crossing in range.
- **Never let a `neutral` signal reach the executor as a directional order** — see B3.

### A5. Stop blocking the event loop

**Evidence.** `MarketData.get()` and `open_interest()` are synchronous HTTP (OI paginates
~3430 contracts) called directly from `async def analyze`. The loop stalls for the whole
fetch. On Day 3 the Featherless call (1–3s, also synchronous) lands in the same loop,
which makes Principle #3 — "keep the LLM off the critical path" — false as built.

**Fix.** Wrap every blocking call: `snap = await asyncio.to_thread(self.data.get, underlying)`,
same for `open_interest`, and for the Oracle's `client.chat.completions.create`. Guard
`MarketData` with a `threading.Lock` per underlying so two agents entering the cache
simultaneously don't double-fetch.

### A6. Smaller corrections

| Item | Fix |
|---|---|
| `bus.queue` has no consumer — unbounded growth | Drop the queue; `latest` + `snapshot()` is all anything uses. Or bound it with `maxsize` and drain. |
| OI cache keyed on underlying only, ignores strike window | Key on `(underlying, round(lo), round(hi), exp_lte)`. Currently 430/2102 in-window SPY contracts resolve to OI 0. |
| Debug `print` at `gamma_scout.py:57` | Remove — the original plan said to put it back. |
| Unused `defaultdict` import in `bus.py` | Remove. |
| `t_years(exp)` in `_atm_iv` omits `today` | Pass `today` for consistency with Gamma Scout. |
| `date.today()` is local time | Use the exchange calendar date (Alpaca `get_clock`) so expiry comparisons don't flip around local midnight. |

### A7. Install the remaining dependencies now, not on Day 3

`openai`, `streamlit`, `yfinance` are all missing. They resolve cleanly on this
Python 3.14 venv (dry-run verified: openai 3.7.0, streamlit 1.63.0, yfinance 1.7.0).
Install today so a resolver surprise doesn't land on the day everything else breaks.

> **openai 3.x note.** The original plan's snippet was written against the 1.x SDK.
> `OpenAI(base_url=...)` and `client.chat.completions.create` still exist in 3.x, but
> verify with one live call before building the Oracle around it, and pin the version in
> `requirements.txt` once it works.

---

## B. Plan-level fixes (things the original plan left underspecified)

### B1. `strength` is not commensurate across agents — define it

Gamma Scout's `strength` is a rarity percentile; Vol Surfer's is `|z|/3`. The schema calls
both "0.0–1.0 normalized conviction," and the Oracle will compare them as if they mean the
same thing. They don't.

**Fix.** Add a `strength_basis` field to `Signal` (`"gex_percentile"` / `"iv_spread_zscore"`)
and state in the Oracle system prompt that strengths are **not** cross-comparable — each is
that agent's own conviction on its own scale. Then have the Oracle allocate on the
*combination and regime*, which is the thesis anyway, rather than on "whose number is bigger."

This is also the honest answer to a judge asking "how are these weighted?"

### B2. Define the exit rule — Shadow P&L depends on it

§3.5 says shadow positions use "the same entry/exit logic." There is no exit logic anywhere
in the plan. The swarm-vs-solo equity curve is the centrepiece chart and it is currently
undefined.

**Fix — specify it before writing the executor:**
- Gamma Scout: exit at +50% / −50% of premium, or when spot moves beyond 2% from the wall
  (thesis invalidated), or at 5 DTE, whichever first.
- Vol Surfer: exit when the z-score reverts inside ±0.5 (thesis realised), at −100% of the
  net debit, or at 7 DTE on the front leg.
- Both: force-close at the close blackout on the front leg's expiry week.

Shadow positions use the identical rule, marked to the mid of the chain you already fetched.

### B3. Close the signal → order gap

The plan says "Gamma Scout → single-leg near the wall" but never specifies strike or expiry,
and Vol Surfer's measured expirations were untradeable (A2). Pin it down:

- **Gamma Scout:** buy the ~0.35-delta option in the direction of the signal, using the
  nearest expiration in the 7–45 DTE band. `direction == "neutral"` → **no trade**, log
  the reason. Allocation is permission, not obligation.
- **Vol Surfer:** sell the ATM option at `front_dte`, buy the ATM option at `back_dte` —
  the exact expirations carried in the signal payload. Reject if either leg's relative
  spread exceeds 10% (tighter than `_clean`'s 20%, because you are crossing it).

### B4. Produce the STALE DATA flag

§4.3 promises a freshness indicator with nothing generating it. `MarketData` already stores
`ts` in every snapshot — surface it: add `data_age_s` to every `Signal.data`, and have the
base class refuse to publish when age exceeds `3 × interval_s`. That gives the dashboard a
real flag and gives you the demo line the plan wants.

### B5. Verify `_sanitize`'s dry-powder behaviour is intended

Clamping to `[0.1, 0.7]` and normalizing only when the total exceeds 1.0 means two agents at
0.1 each deploy 20% and hold 80% cash, silently. That may be what you want. Decide, and log
the deployed fraction so the dashboard shows it rather than it being invisible.

---

## C. Revised schedule

The original Day 3 carried Oracle + risk gates + execution + shadow P&L in one day, on the
day things break, with three uninstalled dependencies. Rebalanced:

### Day 3 (Sep 2, remainder) — corrections + Oracle only
1. ~~A7 (install deps) and A6 (small fixes)~~ — **done.**
2. ~~A1, A2, A3, A4, A5 plus B1 and B4~~ — **done**, all modules compile and `main.py`
   runs clean end-to-end publishing a directional SPY signal.
3. Restart the swarm and let the corrected history accumulate at 15-min sampling
   (see the warm-up note under A1 — start it now, leave it running).
4. `swarm/oracle.py` per §3.1, with `_serialize`, `temperature=0`, `try/except → last_good`,
   `_sanitize`, and the B1 prompt change. Wrap the call in `asyncio.to_thread`.
5. Log every decision to `oracle_log.jsonl` (input snapshot, raw output, sanitized output,
   latency, fallback-or-not). This is the explainability artefact and the dashboard's source.
6. **Do not write the executor today.** Run the Oracle in log-only mode overnight.

**Gate before Day 4:** the Oracle has produced ≥10 logged decisions with zero unhandled
exceptions, and at least one deliberate fallback (kill your network for a cycle and confirm
it holds the last good allocation rather than crashing).

### Day 4 (Sep 3) — risk gates + execution + submission account
1. `swarm/risk.py` first, per §3.2, plus the B3 rules. Write it **before** the executor so
   no order path can exist that bypasses a gate.
2. `swarm/execution.py`. Single-leg Gamma Scout path first; get one real paper fill on the
   **dev** account before touching multi-leg.
3. Multi-leg calendar spread (this is a hard requirement — do not defer it past today).
4. Register the fresh $100k submission account (§4.1), keys in `.env.submission`, Level 3
   options verified. Point the swarm at it only after the dev account has produced fills.
5. Shadow P&L (§3.5) with the B2 exit rules.

**Note:** the current account `PA369WIVYOJ8` (created 2026-08-30, options level 3, $100k,
$400k buying power) is your **dev** account. It has development history on it. The submission
account must be a new one — this is the single most common disqualification.

### Day 5 (Sep 4) — dashboard, write-up, demo, submit
1. Streamlit dashboard (§4.3) reading `oracle_log.jsonl` + the history CSVs + `get_all_positions()`.
2. `README.md` with architecture diagram and run instructions; 1-page write-up (§4.4).
3. Record the demo (§5.2). Do not demo live.
4. Submit with buffer.

**Cut without hesitation:** the VPIN third agent, the React dashboard, vertical-spread
variants. The 2-agent core trading reliably beats a broken 3-agent one.

---

## D. Repo hygiene (done 2026-09-02)

- `git init` run inside `quantamental-swarm/` — the repo is scoped to the project, no
  longer rooted at `C:\Users\jeffr`. Verified: `.env`, `.venv/`, `*.csv` and `*.legacy`
  all resolve as ignored.
- `.gitignore`, `.env.example` (empty keys, safe to commit), `requirements.txt` (85 pins).
- Old 60s-cadence history archived to `gex_history.csv.legacy` / `volspread_history.csv.legacy`
  (2-column format, superseded by the 3-column timestamped files).

**Still to do:** nothing has been committed yet. Make the first commit before Day 3 so you
have a rollback point ahead of the executor work.

---

## E. Updated Known Limitations (fold into the write-up)

Additions to the original list, all now evidence-backed:

- **Greeks come from the feed for ~84% of cleaned contracts** (9562/11378 on SPY);
  the Black-Scholes fallback covers the remainder. Both paths are exercised in production.
- **Open interest is missing on ~20% of in-window contracts** (430/2102 on SPY) and is
  treated as zero, which biases GEX toward the liquid strikes. Directionally fine for wall
  detection; stated rather than hidden.
- **GEX percentile is ranked against a short history** — days, not months. It is a relative
  indicator over the observation window, not a distributional claim.
- **The gamma flip point is not always defined.** On call-dominated chains, cumulative signed
  GEX may not cross zero in-window; the system reports `neutral` and declines to trade
  directionally rather than inventing a direction.
