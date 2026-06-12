"""Market data with two backends:

- yfinance (default): free, ~1 minute delayed, works for everything
  including the SPX index (^GSPC).
- Alpaca (optional): genuinely real-time IEX data for plain stocks.
  Activates automatically when ALPACA_API_KEY + ALPACA_API_SECRET are set
  (free paper account at alpaca.markets). Alpaca has NO index data on any
  tier, so ^GSPC/^VIX always stay on yfinance. Any Alpaca error falls back
  to yfinance for that call — same bars, just slower eyes.

Both backends return the same shape: today's COMPLETED 5-minute bars with
an America/New_York index and Open/High/Low/Close columns, so the strategy
code cannot tell them apart.
"""

import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

ET = ZoneInfo("America/New_York")
ALPACA_DATA = "https://data.alpaca.markets"


def _completed(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Keep only bars whose 5-minute window has fully closed, today only,
    regular session only."""
    if df is None or df.empty:
        return df
    cutoff = now - timedelta(minutes=5)
    df = df[df.index <= cutoff]
    df = df[df.index.date == now.date()]
    return df[(df.index.time >= time(9, 30)) & (df.index.time < time(16, 0))]


def yf_today_bars(yf_symbol: str, now: datetime):
    df = yf.download(yf_symbol, period="1d", interval="5m",
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df.index = df.index.tz_convert(ET)
    df = _completed(df, now)
    return df if df is not None and not df.empty else None


def yf_latest_price(yf_symbol: str):
    try:
        df = yf.download(yf_symbol, period="1d", interval="1m",
                         progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        return float(df["Close"].iloc[-1])
    except Exception:
        return None


class AlpacaREST:
    """Minimal Alpaca market-data client (free IEX feed). Real-time prices,
    200 REST calls/min on the free tier — far more than this bot uses."""

    def __init__(self, key: str, secret: str):
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def today_bars_5m(self, symbol: str, now: datetime):
        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        r = requests.get(
            f"{ALPACA_DATA}/v2/stocks/{symbol}/bars",
            params={"timeframe": "5Min", "start": start.isoformat(),
                    "feed": "iex", "adjustment": "raw", "limit": 1000},
            headers=self.headers, timeout=10)
        r.raise_for_status()
        bars = r.json().get("bars") or []
        if not bars:
            return None
        df = pd.DataFrame({
            "Open": [b["o"] for b in bars],
            "High": [b["h"] for b in bars],
            "Low": [b["l"] for b in bars],
            "Close": [b["c"] for b in bars],
            "Volume": [b["v"] for b in bars],
        }, index=pd.DatetimeIndex([pd.Timestamp(b["t"]) for b in bars]).tz_convert(ET))
        df = _completed(df, now)
        return df if not df.empty else None

    def latest_price(self, symbol: str):
        r = requests.get(f"{ALPACA_DATA}/v2/stocks/{symbol}/trades/latest",
                         params={"feed": "iex"}, headers=self.headers, timeout=10)
        r.raise_for_status()
        return float(r.json()["trade"]["p"])


class DataFeed:
    def __init__(self):
        key = os.environ.get("ALPACA_API_KEY", "").strip()
        secret = os.environ.get("ALPACA_API_SECRET", "").strip()
        self.alpaca = AlpacaREST(key, secret) if key and secret else None

    def backend_for(self, yf_symbol: str) -> str:
        if self.alpaca and not yf_symbol.startswith("^"):
            return "alpaca (real-time)"
        return "yfinance (~1 min delay)"

    def today_bars(self, yf_symbol: str, now: datetime):
        """Today's completed 5m bars, ET index. None if no data yet."""
        if self.alpaca and not yf_symbol.startswith("^"):
            try:
                bars = self.alpaca.today_bars_5m(yf_symbol, now)
                if bars is not None:
                    return bars
            except Exception:
                pass  # fall back to yfinance
        return yf_today_bars(yf_symbol, now)

    def latest_price(self, yf_symbol: str):
        """Freshest trade price available, for option-price estimates."""
        if self.alpaca and not yf_symbol.startswith("^"):
            try:
                return self.alpaca.latest_price(yf_symbol)
            except Exception:
                pass
        return yf_latest_price(yf_symbol)
