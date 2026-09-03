# main.py - Day 4: agents -> Oracle -> risk -> execution, on live data.
import os, argparse, asyncio
from dotenv import load_dotenv
# pyrefly: ignore [missing-import]
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
# pyrefly: ignore [missing-import]
from alpaca.trading.client import TradingClient
from swarm.bus import SignalBus
from swarm.data import MarketData
from swarm.agents.gamma_scout import GammaScout
from swarm.agents.vol_surfer import VolSurfer
from swarm.oracle import Oracle
from swarm.risk import RiskManager
from swarm.execution import Executor, PositionManager
from swarm.shadow import ShadowBook

UNDERLYINGS = ["SPY", "NVDA"]     # SPY + one volatile single name


def build(env_file=".env", *, live=False, sample_every_s=900, oracle_interval_s=300):
    load_dotenv(env_file, override=True)
    key, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    opt = OptionHistoricalDataClient(key, sec)
    stk = StockHistoricalDataClient(key, sec)
    trade = TradingClient(key, sec, paper=True)
    data = MarketData(opt, stk, trade, ttl_s=30)
    bus = SignalBus()

    agents = [GammaScout(bus, data, UNDERLYINGS, interval_s=60, sample_every_s=sample_every_s),
              VolSurfer(bus, data, UNDERLYINGS, interval_s=60, sample_every_s=sample_every_s)]
    oracle = Oracle(bus, interval_s=oracle_interval_s)
    risk = RiskManager(trade)
    execu = Executor(bus, data, trade, risk, dry_run=not live)
    posman = PositionManager(bus, data, trade, execu)
    execu.position_manager = posman
    shadow = ShadowBook(data, bus, executor=execu)
    return dict(data=data, bus=bus, agents=agents, oracle=oracle, risk=risk,
                execu=execu, posman=posman, shadow=shadow, trade=trade)


async def main(live=False, sample_every_s=900, oracle_interval_s=300):
    s = build(live=live, sample_every_s=sample_every_s, oracle_interval_s=oracle_interval_s)

    async def on_decision(d):
        # Shadow first: it must record what each agent WOULD have done on its own,
        # independently of whether the Oracle funded it. That comparison is the point.
        await s["shadow"].on_decision(d)
        await s["execu"].on_decision(d)

    tasks = [asyncio.create_task(a.run()) for a in s["agents"]]
    tasks.append(asyncio.create_task(s["oracle"].run(on_decision=on_decision)))
    tasks.append(asyncio.create_task(s["shadow"].run()))
    tasks.append(asyncio.create_task(s["posman"].run()))
    mode = "LIVE (paper account, real orders)" if live else "DRY RUN (gates run, nothing sent)"
    print(f"swarm running - {mode} - Ctrl+C to stop")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="actually submit orders (still the paper account)")
    ap.add_argument("--sample-every-s", type=int, default=900,
                    help="history sampling cadence; 120 for a faster demo warm-up")
    ap.add_argument("--oracle-interval-s", type=int, default=300)
    a = ap.parse_args()
    try:
        asyncio.run(main(a.live, a.sample_every_s, a.oracle_interval_s))
    except KeyboardInterrupt:
        print("\nstopped.")
