# smoke_test.py
import os
from dotenv import load_dotenv
# pyrefly: ignore [missing-import]
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
# pyrefly: ignore [missing-import]
from alpaca.data.requests import OptionChainRequest, StockLatestTradeRequest

load_dotenv()
KEY, SEC = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
opt = OptionHistoricalDataClient(KEY, SEC)
stk = StockHistoricalDataClient(KEY, SEC)

spot = stk.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols="SPY"))["SPY"].price
print("SPY spot:", spot)

chain = opt.get_option_chain(OptionChainRequest(underlying_symbol="SPY"))
print("contracts returned:", len(chain))

def strike_of(sym):          # parse strike from the OCC symbol: last 8 digits = strike*1000
    return int(sym[-8:]) / 1000

with_greeks = [s for s, c in chain.items() if getattr(c, "greeks", None)]
print(f"contracts WITH greeks: {len(with_greeks)} / {len(chain)}")

if with_greeks:
    # sampling was the problem — pick the contract closest to ATM and show it
    atm = min(with_greeks, key=lambda s: abs(strike_of(s) - spot))
    c = chain[atm]
    print("ATM contract:", atm, "strike:", strike_of(atm))
    print("greeks:", c.greeks, "IV:", c.implied_volatility)
    print(">>> VERDICT: feed populates greeks. Filter to near-ATM; you're good.")
else:
    print(">>> VERDICT: feed returns NO greeks on any contract. "
          "Use the Black-Scholes fallback below — this is expected on the free feed.")