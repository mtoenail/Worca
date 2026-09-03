# swarm/oracle.py - the LLM allocator. Reads the bus, decides capital allocation.
import asyncio, json, os, time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from openai import OpenAI

FEATHERLESS_BASE = "https://api.featherless.ai/v1"
DEFAULT_MODEL = os.getenv("ORACLE_MODEL", "moonshotai/Kimi-K2-Instruct-0905")

SYSTEM_PROMPT = """You are the Oracle of a two-agent options trading swarm. You allocate \
capital between agents; you do not pick strikes and you do not place orders.

The agents:
- gamma_scout: locates the dominant dealer gamma strike (the "wall") and the gamma flip
  point. Fires only when spot is within 1% of the wall. Its `regime_hint` is "pinning"
  when net dealer gamma is positive (price is damped toward the wall) and "accelerant"
  when negative (moves are amplified away from it).
- vol_surfer: measures the ATM implied-vol spread between a ~14 DTE and a ~70 DTE
  expiration and flags it when it dislocates from its own recent range. Trades a
  calendar spread; it is direction-neutral by construction.

CRITICAL - `strength` is NOT comparable across agents. Each agent reports conviction on
its own scale, named in `strength_basis`:
- "gex_percentile": where today's wall gamma ranks against that agent's own recent
  observations. A short history (days, not months). It is a rarity rank, not a probability.
- "iv_spread_zscore": |z| / 3, capped at 1.0. A statistical dislocation measure.
A gex_percentile of 0.8 and an iv_spread_zscore of 0.8 do not mean the same thing and must
not be traded off against each other as if they did. Allocate on the REGIME and on whether
the signals CORROBORATE or CONTRADICT each other - never on whose number is larger.

Guidance:
- Pinning regime + price at the wall is the gamma_scout's best setup. Accelerant regime
  means the wall is a launchpad, not a magnet - size it down.
- A vol_surfer signal is independent of direction, so it can be funded alongside a
  gamma_scout signal without doubling directional risk.
- direction "neutral" from gamma_scout means the flip point was undefined and there is no
  directional basis. Allocate 0 to it; the executor will refuse the trade anyway.
- Holding cash is a legitimate decision. Do not feel obliged to deploy.

Reply with JSON ONLY, no prose outside it, in exactly this shape:
{"regime": "<one short phrase describing the market regime you infer>",
 "allocations": {"<signal_id>": <fraction of portfolio, 0.0 to 0.7>, ...},
 "reasoning": "<2-3 sentences: why these sizes, citing the regime and corroboration>"}
Use the exact signal_id strings given to you. Omit a signal, or give it 0.0, to skip it.
The allocations must sum to at most 1.0."""


@dataclass
class Decision:
    ts: str
    allocations: dict                    # {signal_id: fraction}
    regime: str = ""
    reasoning: str = ""
    fallback: bool = False               # True if this reuses the last good decision
    fallback_reason: str = ""
    latency_ms: int = 0
    deployed_fraction: float = 0.0       # B5: make the cash held visible, not silent
    model: str = ""
    raw: str = ""                        # unparsed model output, for the audit trail
    inputs: dict = field(default_factory=dict)


class Oracle:
    """Turns the swarm's signals into capital allocations, with a logged audit trail.

    Every decision - including every fallback - is appended to `oracle_log.jsonl`. That
    file is both the explainability artefact and the dashboard's data source, so it
    records the inputs, the raw model output, the sanitized output, and the latency.
    """

    def __init__(self, bus, model=DEFAULT_MODEL, log_path="oracle_log.jsonl",
                 interval_s=300, clamp=(0.1, 0.7), max_total=1.0, api_key=None,
                 base_url=FEATHERLESS_BASE, timeout_s=30):
        self.bus, self.model, self.log_path = bus, model, log_path
        self.interval_s = interval_s
        self.clamp, self.max_total = clamp, max_total
        self.client = OpenAI(api_key=api_key or os.getenv("FEATHERLESS_API_KEY"),
                             base_url=base_url, timeout=timeout_s)
        self.last_good: Decision | None = None

    # ---------- input ----------
    @staticmethod
    def sig_id(agent, underlying):
        return f"{agent}:{underlying}"

    def _serialize(self, snap):
        """Bus snapshot -> the JSON the model sees. Ids are the executor's handles too."""
        out = {}
        for (agent, underlying), s in snap.items():
            out[self.sig_id(agent, underlying)] = {
                "agent": agent, "underlying": underlying,
                "signal_type": s.signal_type, "direction": s.direction,
                "strength": s.strength, "strength_basis": s.strength_basis,
                "age_s": round((datetime.now(timezone.utc) - s.ts).total_seconds(), 1),
                "data": s.data,
            }
        return out

    # ---------- output ----------
    def _sanitize(self, raw_allocs, valid_ids):
        """Clamp, drop unknown ids, and normalize only if the total overshoots.

        A model-returned 0.0 (or an omission) means "do not trade this" and is kept at
        zero - clamping it up to the floor would turn a decision to stand aside into a
        forced position. The floor applies only to allocations the model actually made.
        """
        lo, hi = self.clamp
        clean = {}
        for k, v in (raw_allocs or {}).items():
            if k not in valid_ids:
                continue                            # hallucinated id - drop it
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v <= 0:
                continue                            # explicit stand-aside
            clean[k] = min(max(v, lo), hi)
        total = sum(clean.values())
        if total > self.max_total:
            clean = {k: round(v * self.max_total / total, 4) for k, v in clean.items()}
        return clean

    # ---------- the call ----------
    def _call_model(self, payload):
        r = self.client.chat.completions.create(
            model=self.model, temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": json.dumps(payload, default=str)}],
        )
        return r.choices[0].message.content

    async def decide(self) -> "Decision | None":
        snap = self.bus.snapshot()
        if not snap:
            return None
        inputs = self._serialize(snap)
        t0 = time.time()
        raw, parsed, fb_reason = "", None, ""
        try:
            # Blocking HTTP (1-3s). Off the event loop, or it stalls both agents.
            raw = await asyncio.to_thread(self._call_model, inputs)
            parsed = json.loads(raw)
        except Exception as e:
            fb_reason = f"{type(e).__name__}: {e}"
        latency = int((time.time() - t0) * 1000)

        if parsed is None:
            d = self._fallback(inputs, raw, fb_reason, latency)
        else:
            allocs = self._sanitize(parsed.get("allocations"), set(inputs))
            d = Decision(
                ts=datetime.now(timezone.utc).isoformat(), allocations=allocs,
                regime=str(parsed.get("regime", ""))[:200],
                reasoning=str(parsed.get("reasoning", ""))[:1000],
                latency_ms=latency, deployed_fraction=round(sum(allocs.values()), 4),
                model=self.model, raw=raw, inputs=inputs)
            self.last_good = d
        self._log(d)
        return d

    def _fallback(self, inputs, raw, reason, latency):
        """Hold the last good allocation rather than crashing or flattening to zero.

        Only ids still present on the bus survive: reusing an allocation for a signal
        that has since disappeared would keep funding a thesis nothing is asserting.
        """
        prev = self.last_good.allocations if self.last_good else {}
        allocs = {k: v for k, v in prev.items() if k in inputs}
        return Decision(
            ts=datetime.now(timezone.utc).isoformat(), allocations=allocs,
            regime=self.last_good.regime if self.last_good else "",
            reasoning="FALLBACK: reusing last good allocation." if allocs
                      else "FALLBACK: no prior allocation, holding cash.",
            fallback=True, fallback_reason=reason, latency_ms=latency,
            deployed_fraction=round(sum(allocs.values()), 4),
            model=self.model, raw=raw, inputs=inputs)

    def _log(self, d: Decision):
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(d), default=str) + "\n")

    async def run(self, on_decision=None):
        while True:
            try:
                d = await self.decide()
                if d:
                    tag = "FALLBACK" if d.fallback else "ok"
                    print(f"[oracle] {tag} regime={d.regime!r} "
                          f"deployed={d.deployed_fraction:.0%} "
                          f"allocs={d.allocations} ({d.latency_ms}ms)")
                    if on_decision:
                        await on_decision(d)
                else:
                    print("[oracle] bus empty, waiting...")
            except Exception as e:
                print(f"[oracle] loop error: {type(e).__name__}: {e}")
            await asyncio.sleep(self.interval_s)
