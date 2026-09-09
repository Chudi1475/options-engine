"""Option quotes: real bid/ask from the Yahoo option chain when available,
clearly-labeled Black-Scholes estimate from the stock's move when not.

Honesty rules baked in:
- Yahoo option quotes can be ~15 minutes delayed — every message that uses
  one says so.
- When no usable quote exists, the estimate is marked "estimate" everywhere
  it appears. Estimates use the same approximated pricing as the backtest.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone

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
    # W06 provenance. Additive, with a default, so every existing construction
    # (positional, five arguments) builds the same object and no price anywhere
    # changes. The recorder has to tell three clocks apart and the old shape
    # could only tell it one. This is the LAST TRADE time, not a quote time,
    # and it is named that way so it can never be read as one: the chain rows
    # carry no per quote timestamp at all.
    last_trade_at_utc: str = None


def nearest_listed_expiry(ticker: str, target: date):
    """Snap a computed expiry to one that actually trades. Holiday weeks move
    weeklies (e.g. Friday-holiday -> Thursday expiry). Returns the listed
    expiration closest to `target` within 3 days, else `target` unchanged."""
    if ticker in ("SPX", "SPY"):
        return target  # 0DTE: NEVER snap to another date — a 1-3 DTE contract
                       # would break the strategy/stats/"expires today" card. If
                       # today isn't listed, get_option_quote returns None and
                       # the bot falls back to the clearly-labeled estimate.
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
        traded = _ts_utc(row["lastTradeDate"].iloc[0])
    except Exception:
        return None
    if bid > 0 and ask >= bid:
        return Quote(bid, ask, round((bid + ask) / 2, 2),
                     f"live quote ({QUOTE_NOTE})", False, traded)
    if last > 0:
        return Quote(0.0, 0.0, last, "last trade (no live bid/ask)", False,
                     traded)
    return None


def _ts_utc(ts):
    """One chain timestamp as a UTC iso string, or None.

    None on anything unparseable, deliberately. W06's rule is that an unknown
    stays null: a made up timestamp on an option that has not traded for an
    hour is worse than an admitted blank, because it makes a stale row look
    fresh."""
    try:
        if ts is None:
            return None
        dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def chain_snapshot(underlying: str, expiry_date, now=None) -> dict:
    """ONE read of one option chain, shaped for the recorder.

    This is the batching Astra asks for: a chain read returns every strike and
    both rights for one expiry, so N contracts under observation on one expiry
    cost ONE provider call rather than N. It is called from the recorder's own
    thread and NEVER from the monitoring loop, because a synchronous chain read
    in front of a live stop is the one thing the recorder may not do.

    The three clocks are reported honestly. requested_at and received_at are
    real. provider_at is None with a reason, because a Yahoo chain row carries
    no per quote timestamp and inventing one is how a roughly fifteen minute
    delayed quote gets read as live."""
    sym = CHAIN_SYMBOL.get(underlying, underlying)
    exp = expiry_date if isinstance(expiry_date, str) else expiry_date.isoformat()
    requested = datetime.now(timezone.utc).isoformat()
    out = {"provider": "yfinance", "feed": "yahoo option chain",
           "provider_at_utc": None, "requested_at_utc": requested,
           "received_at_utc": None, "underlying_price": None,
           "underlying_at_utc": None, "strikes": {"C": [], "P": []},
           "rows": {},
           "missing_reason": "a yahoo option chain row carries no per quote "
                             "timestamp; the feed is documented as roughly 15 "
                             "minutes delayed"}
    chain = yf.Ticker(sym).option_chain(exp)   # may raise; the caller counts it
    out["received_at_utc"] = datetime.now(timezone.utc).isoformat()
    for right, df in (("C", chain.calls), ("P", chain.puts)):
        if df is None:
            continue
        for _, r in df.iterrows():
            try:
                strike = float(r["strike"])
                bid = float(r["bid"] or 0)
                ask = float(r["ask"] or 0)
            except (TypeError, ValueError, KeyError):
                continue
            out["strikes"][right].append(strike)
            out["rows"][(right, strike)] = {
                "bid": bid if bid > 0 else None,
                "ask": ask if ask > 0 else None,
                # the chain carries no quote sizes at all, on any tier. Null
                # with a reason beats a zero that reads as "no size offered".
                "bid_size": None, "ask_size": None,
                "last_trade_at_utc": _ts_utc(r.get("lastTradeDate")),
            }
    return out


def estimate_premium(spot: float, strike: float, right: str,
                     expiry_dt: datetime, now: datetime, sigma: float) -> float:
    """Approximated option price from the underlying — same model as the
    backtest. Only used (and labeled) when no real quote is available."""
    T = max((expiry_dt - now).total_seconds(), 0) / (365.0 * 24 * 3600)
    return round(bs_price(spot, strike, T, sigma, right), 2)
