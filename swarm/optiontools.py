# swarm/optiontools.py
from datetime import datetime, date

def parse_occ(symbol: str):
    """'SPY260902P00762000' -> ('SPY', date(2026,9,2), 'P', 762.0)"""
    strike = int(symbol[-8:]) / 1000
    cp = symbol[-9]
    exp = datetime.strptime(symbol[-15:-9], "%y%m%d").date()
    root = symbol[:-15]
    return root, exp, cp, strike

def t_years(exp: date, today: date | None = None) -> float:
    return max((exp - (today or date.today())).days, 0) / 365.0