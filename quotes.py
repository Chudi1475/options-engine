"""Option quotes: real bid/ask from the Yahoo option chain when available,
clearly-labeled Black-Scholes estimate from the stock's move when not.

Honesty rules baked in:
- Yahoo option quotes can be ~15 minutes delayed — every message that uses
  one says so.
- When no usable quote exists, the estimate is marked "estimate" everywhere
  it appears. Estimates use the same approximated pricing as the backtest.
"""

from dataclasses import dataclass
from datetime import date, datetime

import yfinance as yf

from backtest import bs_price

# what symbol the OPTION chain lives under (can differ from the bars symbol)
CHAIN_SYMBOL = {"SPX": "^SPX"}

QUOTE_NOTE = "may be ~15 min delayed"

# cache of expiration lists per symbol, refreshed per day: {(sym, date): tuple}
# only NON-EMPTY results are cached — one throttled Yahoo response must not
# disable real quotes for the rest of the day
_exp_cache = {}


def _expirations(sym: str) -> tuple:
    key = (sym, date.today())
    if key not in _exp_cache:
        exps = tuple(yf.Ticker(sym).options or ())
        if not exps:
            return ()  # don't cache failure — retry next cycle
        _exp_cache[key] = exps
    return _exp_cache[key]


@dataclass
class Quote:
    bid: float
    ask: float
    mid: float
    source: str          # human label for where the price came from
    is_estimate: bool


def nearest_listed_expiry(ticker: str, target: date):
    """Snap a computed expiry to one that actually trades. Holiday weeks move
    weeklies (e.g. Friday-holiday -> Thursday expiry). Returns the listed
    expiration closest to `target` within 3 days, else `target` unchanged."""
    sym = CHAIN_SYMBOL.get(ticker, ticker)
    try:
        listed = [date.fromisoformat(e) for e in _expirations(sym)]
    except Exception:
        return target
    if not listed or target in listed:
        return target
    near = min(listed, key=lambda d: abs((d - target).days))
    return near if abs((near - target).days) <= 3 else target


def get_option_quote(ticker: str, right: str, strike: float, expiry: date):
    """Real quote for one contract, or None if Yahoo has nothing usable."""
    sym = CHAIN_SYMBOL.get(ticker, ticker)
    try:
        exp = expiry.isoformat()
        if exp not in _expirations(sym):
            return None
        chain = yf.Ticker(sym).option_chain(exp)
        df = chain.calls if right == "C" else chain.puts
        row = df[df["strike"] == strike]
        if row.empty:
            return None
        bid = float(row["bid"].iloc[0] or 0)
        ask = float(row["ask"].iloc[0] or 0)
        last = float(row["lastPrice"].iloc[0] or 0)
    except Exception:
        return None
    if bid > 0 and ask >= bid:
        return Quote(bid, ask, round((bid + ask) / 2, 2),
                     f"live quote ({QUOTE_NOTE})", False)
    if last > 0:
        return Quote(0.0, 0.0, last, "last trade (no live bid/ask)", False)
    return None


def estimate_premium(spot: float, strike: float, right: str,
                     expiry_dt: datetime, now: datetime, sigma: float) -> float:
    """Approximated option price from the underlying — same model as the
    backtest. Only used (and labeled) when no real quote is available."""
    T = max((expiry_dt - now).total_seconds(), 0) / (365.0 * 24 * 3600)
    return round(bs_price(spot, strike, T, sigma, right), 2)
