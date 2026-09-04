# Quantamental Options Swarm — Write-up

## The idea

Most "AI trading" systems put a language model where it is worst: picking instruments and
timing entries, on numeric data it reasons about poorly, with no audit trail.

This system puts the LLM where it is actually good — **weighing incommensurable evidence
and explaining the trade-off** — and keeps it away from everything else.

Two deterministic quantitative agents do the measuring:

- **gamma_scout** computes dealer gamma exposure per strike from the live chain, locates
  the dominant strike (the "wall"), and finds the gamma flip point by re-pricing the whole
  book across a grid of hypothetical spot levels. It fires only while spot is within 1% of
  the wall, and reports a regime: *pinning* (positive net dealer gamma, price damped toward
  the wall) or *accelerant* (negative, moves amplified away from it).
- **vol_surfer** measures the ATM implied-vol spread between a ~14 DTE and a ~70 DTE
  expiration and flags it when it dislocates from its own recent range. Direction-neutral
  by construction; it trades a calendar spread.

The **Oracle** — Kimi-K2 via Featherless, `temperature=0` — sees both signals and decides
only *how much capital each one gets*. It never picks a strike, never picks an expiration,
never sends an order. Deterministic code does all of that, behind risk gates the LLM cannot
reach or influence.

## Why an LLM at all

The honest case rests on one problem the code cannot solve on its own.

The two agents' conviction scores are **not commensurable**. `gamma_scout.strength` is a
rarity percentile over its own short history; `vol_surfer.strength` is `|z|/3`. A 0.8 from
each means entirely different things, and no principled scalar weighting exists between
them — the choice of weights *is* the judgement.

So the system stops pretending. `Signal` carries a `strength_basis` field, and the Oracle's
prompt states in full that the numbers are not comparable and must not be traded off as if
they were, then instructs it to allocate on **regime and corroboration** instead. Its
reasoning is logged in natural language on every cycle:

> *"NVDA gamma_scout shows pinning regime with bullish direction — its best setup — so I
> size it meaningfully. Both vol_surfer signals show extreme z-scores indicating rare
> calendar dislocations; these are direction-neutral and diversify across underlyings. SPY
> gamma_scout is in accelerant regime, making the wall a launchpad not a magnet, so I skip
> it despite proximity."*

That is a real trade-off, correctly reasoned, and it is the answer to "how are these
weighted?" A hard-coded weight would have to invent a number and could not explain itself.

## Architecture in one line each

```
agents ──▶ bus ──▶ Oracle ──▶ risk gates ──▶ executor ──▶ Alpaca
                      │
                      └────▶ shadow book (same signals, no Oracle)
```

**Risk was written before the executor**, and owns the order dataclasses. There is no code
path that constructs an order without importing the module that checks it.

**`check()` evaluates every gate rather than stopping at the first failure**, so a
rejection carries its complete reason. That is what the dashboard displays.

**The allocation is a ceiling, not a target.** `size()` takes `min(alloc, max_per_trade)` —
risk narrows the Oracle's number and can never widen it. A 0.7 allocation on a 10%
per-trade cap sizes to 10%.

**The LLM is off the critical path.** Every blocking HTTP call is wrapped in
`asyncio.to_thread`. When the Oracle fails, the swarm holds its last good allocation and
keeps trading. This is not theoretical: of 111 logged decisions, **3 were fallbacks** — one
provider 500 and two genuine network failures — and none interrupted the swarm.

**One exit implementation.** `exits.py` is imported by both the live position manager and
the shadow book. Two copies would have drifted, and the swarm-vs-solo curve would then be
measuring that drift instead of the Oracle.

## Measuring the Oracle's contribution

The claim "allocation adds value" is easy to assert and easy to fake. The shadow book is
the control: it opens a position for **every** signal either agent emits, regardless of
allocation, at a fixed 10% size — the no-Oracle baseline. It uses the executor's own intent
builder, so both books hold *identical contracts*, and the same exit rules. The only
difference between the two equity curves is the Oracle's sizing.

### Live session, 2026-09-04

Account **PA3YDAINTVU4**, opened fresh that morning, traded from the 09:30 ET open.

| | |
|---|---|
| Orders sent | 3 |
| Filled | 3 — **one single-leg, two multi-leg calendars** |
| Rejected or errored at the broker | 0 |
| Equity | $100,000 -> **$100,413.15** (+$413.15) |
| Unrealized on open legs | +$436.00 |

```
13:34:16Z  SIMPLE  NVDA260911C00237500      10 @ $2.09
13:36:31Z  MLEG    SPY calendar (770)        6 @ $14.66   sell 260918 / buy 261120
13:51:24Z  MLEG    SPY calendar (775)        6 @ $15.00   sell 260918 / buy 261120
```

The full chain ran unattended: `gamma_scout` detected NVDA pinning at the dealer wall ->
the Oracle allocated 0.5 -> the risk gates sized it to the 10-contract cap -> the executor
selected the ~0.35-delta strike -> filled at $2.09 against a $2.10 limit.

**The solo baseline is not reported as a P&L comparison, and should not be.** It recorded
roughly +$6,000 against the live account's +$413 on the same signals. A 10x divergence is
not an allocation effect — it is a marking artefact: the shadow book marks to mid on a
chain whose opening spreads were wide and erratic, and it opened at the bell where the
live book filled minutes later at executable prices. The apparatus works and is the right
control, but one session of opening-auction marks cannot support a swarm-vs-solo claim,
and presenting that number as a result would be dishonest.

The defensible claim from this session is narrower and it is about the machinery, not the
edge: signal to allocation to gated order to fill worked end to end on a fresh account,
including the multi-leg path, with no broker rejections.

## Two failures worth reporting

Both were found by logging inputs rather than only outputs.

**1. The Oracle's allocations oscillated on unchanged inputs.** Three consecutive cycles
with materially identical market data produced allocations of 0.35, 0.00, 0.35 — despite
`temperature=0`. A hosted mixture-of-experts is not bit-deterministic, and two contributing
causes were separable: `data_age_s` was entering the prompt at 0.1s resolution, making
every prompt textually unique even in an unchanged market; and the model's own sampling
varied. The fix addresses both — quantize the jitter out of the prompt, then apply a 0.10
deadband and require a drop to be confirmed on two consecutive cycles, because going flat
is the most expensive thing a noisy cycle can do. **The raw model output is retained in
`model_allocations`**, so the damping is auditable rather than hidden, and the dashboard
plots the two against each other.

**2. The bus never expired signals.** `analyze()` returns `None` when an agent's
precondition lapses, and the agent then publishes nothing — but the bus kept the last
signal indefinitely, so silence was read as *"the previous signal"* rather than *"no
signal"*. The Oracle was found allocating capital to a `gamma_scout:SPY` signal **77,610
seconds (21.5 hours) old**, and at the next open the executor would have built a live order
from those overnight strikes. Signals now expire after 300s — five poll intervals. This is
distinct from the B4 staleness check, which refuses to *publish* a signal computed on stale
market data; this one expires a signal that was fresh when published and was never renewed.

## Known limitations

All evidence-backed, from live runs.

- **Greeks come from the feed for ~84% of cleaned contracts** (9,562/11,378 on SPY); a
  Black-Scholes fallback covers the rest. Both paths run in production.
- **Open interest is missing on ~20% of in-window contracts** (430/2,102 on SPY) and is
  treated as zero, biasing GEX toward liquid strikes. Directionally fine for locating a
  wall; stated rather than hidden.
- **GEX percentile is ranked against days of history, not months.** It is a relative
  indicator over the observation window, not a distributional claim. All figures here come
  from 15-minute sampling.
- **The gamma flip point is not always defined.** On some chains the scan finds no sign
  change in range; the system reports `neutral` and **declines to trade directionally**
  rather than inventing a direction. The risk gate refuses a neutral single-leg order
  outright.
- **Dealer positioning is assumed, not known.** GEX uses the standard
  long-calls/short-puts convention. It is a convention, not a measurement.
- **The expiry-week blackout is currently unreachable code.** gamma_scout time-stops at 5
  DTE and vol_surfer at 7 DTE on the front leg, so no position survives to the 1 DTE
  blackout. The guarantee B2 wanted holds — more conservatively — but the branch is dead
  today and is kept only as a backstop. Documented in `tests/test_exits.py`.
- **One trading session of live results.** The equity comparison is a demonstration that
  the measurement apparatus works, not evidence of edge. Two agents over one day on two
  underlyings cannot support a claim about profitability.
- **Paper fills are optimistic.** Alpaca paper does not model queue position or the market
  impact of crossing a spread, and the calendar spread crosses two.
- **Agent cadence is 2-4x slower than the poll interval.** `interval_s` is 60s, but that is
  the sleep between passes; a pass fetches chains and paginates open interest per
  underlying. Measured publish gaps on 2026-09-04 were 120-270s, with one of 870s. The bus
  TTL is now derived from that measurement rather than from the configured interval.
- **A restart is expensive during market hours.** The open-interest cache is in-memory, so
  a restart re-paginates every contract, and `MarketData`'s per-underlying lock serialises
  the agents and the shadow book behind that first fetch. Cold start costs several minutes
  of signal silence — a fix applied mid-session can cost more than the problem it solves.

## What I would do next

Widen to more underlyings so the Oracle is choosing among genuinely competing
opportunities rather than sizing three signals it can fund simultaneously — the allocation
problem only becomes interesting under scarcity. Then measure the Oracle against a
fixed-weight allocator, not just against no allocator, which is the comparison that would
actually isolate its contribution.
