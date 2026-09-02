# main.py - Day 2: real agents on live data, no execution yet
import os, asyncio
from dotenv import load_dotenv
# pyrefly: ignore [missing-import]
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
# pyrefly: ignore [missing-import]
from alpaca.trading.client import TradingClient
from swarm.bus import SignalBus
from swarm.data import MarketData
from swarm.agents.gamma_scout import GammaScout
from swarm.agents.vol_surfer import VolSurfer
from swarm.stub_oracle import stub_oracle_loop

load_dotenv()
KEY, SEC = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
UNDERLYINGS = ["SPY", "NVDA"]     # SPY + one volatile single name

async def main():
    opt   = OptionHistoricalDataClient(KEY, SEC)
    stk   = StockHistoricalDataClient(KEY, SEC)
    trade = TradingClient(KEY, SEC, paper=True)
    data  = MarketData(opt, stk, trade, ttl_s=30)
    bus   = SignalBus()

    agents = [GammaScout(bus, data, UNDERLYINGS, interval_s=60),
              VolSurfer(bus, data, UNDERLYINGS, interval_s=60)]
    tasks  = [asyncio.create_task(a.run()) for a in agents]
    tasks.append(asyncio.create_task(stub_oracle_loop(bus, interval_s=30)))
    print("Day 2 swarm running (real signals, no execution) - Ctrl+C to stop")
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")