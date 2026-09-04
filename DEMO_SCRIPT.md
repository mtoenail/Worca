# Demo Script — Worca

**Target: 5 minutes.** Re-read the live equity number immediately before recording; it moves.

Layout before you hit record:
- Browser at `http://localhost:8501`, **Signals** tab, sidebar collapsed (the `»` control)
- A second window with `swarm/oracle.py` and `swarm/risk.py` open, ready
- Terminal with `swarm.log` tailing

---

## 0:00 — The problem (no screen change, stay on Signals)

> "Almost every AI trading project puts a language model in the worst possible seat: picking
> the instrument, picking the strike, timing the entry. Models are bad at that. They're bad
> at arithmetic on live numbers, they hallucinate tickers, and when they lose money you
> can't tell why.
>
> So we asked a narrower question. Is there any job in a trading system that a language
> model is genuinely *better* at than code?
>
> There is exactly one, and it's this. Suppose you have two strategies. One says 'this is
> the rarest gamma reading I've seen in a month.' The other says 'implied vol between these
> two expirations is two standard deviations from normal.' Both want capital. **How much
> does each get?**
>
> You cannot answer that with a formula, because the two numbers aren't on the same scale.
> A percentile and a z-score aren't comparable — any weighting you hard-code is a number you
> invented. That judgement call *is* the problem. And it's the one thing an LLM can do that
> code can't: weigh incommensurable evidence and explain the trade-off in writing.
>
> So Worca is a swarm where deterministic code does all the measuring and all the executing,
> and the LLM does exactly one job — capital allocation — and has to justify it every time."

---

## 0:50 — The agents → **Signals tab**

Point at the two signal cards.

> "Two specialist agents, both pure quant, no LLM.
>
> **Gamma Scout** reconstructs dealer gamma exposure across the whole option chain. It finds
> the 'wall' — the strike where dealer hedging is most concentrated — and the gamma flip
> point, which it computes by re-pricing the entire book across a grid of hypothetical spot
> levels. It only fires when price is within 1% of that wall, and it reports a regime:
> *pinning*, where dealer hedging pulls price toward the wall, or *accelerant*, where it
> pushes price away.
>
> **Vol Surfer** measures implied vol between a 14-day and a 77-day expiration and flags it
> when that spread dislocates from its own recent range. It's direction-neutral — it trades
> a calendar spread."

Point at the two `strength` bars and the `strength_basis` labels.

> "Here's the crux. Both report a strength between 0 and 1 — but look at the labels.
> One is `gex_percentile`. The other is `iv_spread_zscore`. **A 0.8 from each means
> completely different things.** Most systems would quietly average them. We don't."

Point at the `live - Ns old` badge.

> "And every signal carries its age. If an agent stops asserting something, it expires off
> the bus — silence means 'no signal,' never 'the last signal.' I'll come back to that."

---

## 1:50 — The Oracle → **Oracle tab**

Read the live reasoning box aloud, then:

> "This is the LLM's entire job. It sees both signals and outputs allocations plus a written
> justification. It never picks a strike, never picks an expiration, never sends an order.
>
> And it's explicitly *told* the strengths aren't comparable. Look at what it wrote:"

**Read this quote directly** (it's in the decision log, 13:23:49):

> *"The 0.5/0.3 split respects that gex_percentile and iv_spread_zscore are incomparable
> scales, while keeping 20% cash as the vol signal is moderate strength and both agents
> trade the same underlying."*

> "That's the system reasoning about its own epistemics. That's the answer to 'how are these
> weighted' — and a hard-coded weight could never give it."

Scroll to the **allocation chart**.

> "Two lines per signal: what the model *said*, and what we *acted on*. They diverge, and
> that's deliberate. On day one we caught the Oracle outputting 0.35, then 0.00, then 0.35
> on identical inputs — at temperature zero. A hosted mixture-of-experts isn't
> bit-deterministic. So we added a deadband and require a drop to be confirmed twice before
> going flat. **We kept the raw model output in the log** so the damping is auditable rather
> than hidden."

Scroll to **Deployed vs cash**.

> "And it's allowed to hold cash. Right now it's 35% deployed, 65% cash — because only one
> agent is firing. Here's what it wrote:"

**Read** (13:53:29):

> *"gamma_scout is silent so no directional overlay exists... holding 65% cash preserves dry
> powder for when gamma_scout corroborates with a pinning regime."*

> "It's declining to trade. That's a feature."

---

## 3:00 — Risk and execution → **Orders & risk tab**

> "The Oracle's allocation is a **ceiling, not a target**. It's a request that then has to
> survive every risk gate."

Point at the live positions and the order log.

> "Three orders, three fills, zero broker rejections — one single-leg and two multi-leg
> calendar spreads, submitted through Alpaca's MLEG order class on a fresh paper account."

**Switch to `swarm/risk.py`** (~20 seconds, don't linger):

> "Two deliberate choices here. First, the risk module owns the *order dataclasses* — so
> there is no code path that builds an order without importing the thing that checks it.
> Second, `check()` evaluates **every** gate rather than stopping at the first failure. So a
> rejected order carries the complete reason it was rejected."

Back to the dashboard, point at the rejection chart.

> "Which is why this chart exists. Market closed, spread too wide, DTE out of band, already
> holding — every refusal is logged and visible."

---

## 3:50 — The control → **Swarm vs solo tab**

> "The claim 'allocation adds value' is easy to assert and easy to fake. So we built the
> control: a shadow book that opens a position for *every* signal, ignoring the Oracle
> entirely. Same contracts, same exit rules, same code path — the only difference is the
> allocation.
>
> **And I'm going to tell you not to trust this number.** The shadow book shows a large
> gain against the live account's roughly flat result. That gap isn't the Oracle — it's a
> marking artifact. The shadow marks to mid on opening-auction spreads that were wide and
> erratic, and it enters at the bell where the live book fills minutes later at executable
> prices.
>
> The apparatus is right. One session of opening marks can't support the claim. So we're
> reporting the machinery, not a P&L edge."

---

## 4:20 — Results, honestly

> "On P&L: we're at roughly break-even — [**state the live number**] on $100,000, over about
> two hours, from three trades.
>
> I'm not going to dress that up. Three trades is not a track record. Any number I quoted
> here — good or bad — would be noise, and annualising it would be dishonest.
>
> What we *can* defend is that the machinery works end to end, unattended, on a fresh
> account: signal, to an explained allocation, to a gated order, to a fill, including the
> multi-leg path — with no broker rejections and no unhandled exceptions across 13 logged
> decisions."

---

## 4:45 — Close: what going live actually taught us

> "Three real bugs only surfaced with live money on the line.
>
> The bus never expired signals — we caught the Oracle funding a gamma signal **21 hours
> stale**, which at the open would have built a live order from overnight strikes.
>
> The exit checker crashed on every cycle because the trade record didn't carry the field it
> read — meaning live positions would have been held to expiry. Found minutes after our
> first fill.
>
> And our signal timeout was tuned to the *configured* poll interval instead of the
> *measured* one, so we were expiring signals the agents were still asserting.
>
> All three are fixed, committed with the evidence, and written into the limitations section.
> That's the actual output of a day like this — not the P&L."

---

## Do you show code?

**Yes, but only ~30 seconds, and only twice.** "Technology Implementation" is a judging
criterion, so *some* code earns its place — but a code tour kills a 5-minute demo.

Show exactly these:
1. **`swarm/risk.py`** — the order dataclasses living beside the gates (3:00 above)
2. **The Oracle system prompt in `swarm/oracle.py`** — the block stating strengths are not
   comparable. Optional; use it only if you're under time.

Do **not** walk through `execution.py`, the agents, or the greeks. If a judge wants depth,
the README has the architecture diagram and `results/` has the full audit trail.

## Questions you should expect

**"Why not just hard-code the weights?"**
> Because any weight between a percentile and a z-score is invented, and it can't explain
> itself or adapt when one agent goes silent. Watch what it does when Gamma Scout stops
> firing — it drops to 35% and says why.

**"Isn't the LLM a single point of failure?"**
> No. Every LLM call is off the event loop, and on failure it holds the last good allocation
> and keeps trading. Across yesterday's 111 decisions we had 3 fallbacks — a provider error
> and two network failures — and none interrupted the swarm.

**"Your P&L is flat."**
> Correct, over three trades and two hours. That's noise either way. The defensible claim is
> the machinery, and I'd rather show you a working risk gate than a lucky number.

**"Could the LLM place a bad order?"**
> It can't place an order at all. It emits a fraction. Code picks the contract, and the risk
> gates can only shrink that fraction, never grow it.
