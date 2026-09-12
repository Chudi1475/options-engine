"""Market data with two backends: yfinance and Alpaca."""

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
    # Fix 1: Pass group_by and explicitly flatten columns to prevent Multi-Index errors
    df = yf.download(yf_symbol, period="1d", interval="5m",
                     progress=False, auto_adjust=False, group_by="ticker")
    if df is None or df.empty:
        return None
    
    # Clean up multi-index columns if they exist
    if isinstance(df.columns, pd.MultiIndex):
        if yf_symbol in df.columns.levels[0]:
            df = df[yf_symbol] # Isolate the target ticker slice
        else:
            df.columns = df.columns.get_level_values(0)
            
    df.index = df.index.tz_convert(ET)
    df = _completed(df, now)
    return df if df is not None and not df.empty else None


def yf_latest_price(yf_symbol: str):
    try:
        df = yf.download(yf_symbol, period="1d", interval="1m",
                         progress=False, auto_adjust=False, group_by="ticker")
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex) and yf_symbol in df.columns.levels[0]:
            df = df[yf_symbol]
        return float(df["Close"].iloc[-1])
    except Exception:
        return None


class AlpacaREST:
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

    def latest_trade(self, symbol: str):
        r = requests.get(f"{ALPACA_DATA}/v2/stocks/{symbol}/trades/latest",
                         params={"feed": "iex"}, headers=self.headers, timeout=10)
        r.raise_for_status()
        tr = r.json()["trade"]
        try:
            ts = pd.Timestamp(tr["t"]).tz_convert(ET)
        except (KeyError, TypeError, ValueError):
            ts = None
        return float(tr["p"]), ts

    def latest_price(self, symbol: str):
        return self.latest_trade(symbol)[0]


class DataFeed:
    def __init__(self):
        key = os.environ.get("ALPACA_API_KEY", "").strip()
        secret = os.environ.get("ALPACA_API_SECRET", "").strip()
        
        # Fix 3: Ensure placeholder variables aren't blocking initialization
        if key and secret and "YOUR_" not in key.upper():
            self.alpaca = AlpacaREST(key, secret)
        else:
            self.alpaca = None

    def backend_for(self, yf_symbol: str) -> str:
        if self.alpaca and not yf_symbol.startswith("^"):
            return "alpaca (real-time)"
        return "yfinance (~1 min delay)"

    def today_bars(self, yf_symbol: str, now: datetime):
        """Today's completed 5m bars, ET index. None if no data yet."""
        # Fix 2: Skip Alpaca on weekends (Saturday=5, Sunday=6) to conserve API load
        is_weekend = now.weekday() >= 5
        
        if self.alpaca and not yf_symbol.startswith("^") and not is_weekend:
            try:
                bars = self.alpaca.today_bars_5m(yf_symbol, now)
                if bars is not None:
                    return bars
            except Exception as e:
                # Log the error tracking explicitly instead of hiding it silently
                print(f"⚠️ Alpaca failed, falling back to yfinance: {e}")
                pass
        return yf_today_bars(yf_symbol, now)

    def latest_price(self, yf_symbol: str):
        if self.alpaca and not yf_symbol.startswith("^") and not now.weekday() >= 5:
            try:
                return self.alpaca.latest_price(yf_symbol)
            except Exception:
                pass
        return yf_latest_price(yf_symbol)
