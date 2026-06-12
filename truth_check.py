"""Truth check — compare what the bot SAID option prices were (estimates /
delayed quotes) against the REAL traded prices from Databento's OPRA archive.

Uses the free signup credit; each pull is a fraction of a cent (the script
prints the exact cost before downloading). Needs DATABENTO_API_KEY in .env.

Usage:
    python truth_check.py            # check every closed position in the book
"""

import io
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import config  # noqa: F401  (loads .env)
from positions import PositionBook

ET = ZoneInfo("America/New_York")
HIST = "https://hist.databento.com/v0"
KEY = os.environ.get("DATABENTO_API_KEY", "").strip()

# index options trade under the weekly root, stocks under their own
OSI_ROOT = {"SPX": "SPXW"}


def auth():
    return (KEY, "")


def osi_symbol(ticker: str, expiry: str, right: str, strike: float) -> str:
    root = OSI_ROOT.get(ticker, ticker).ljust(6)
    ymd = expiry[2:4] + expiry[5:7] + expiry[8:10]
    return f"{root}{ymd}{right}{int(round(strike * 1000)):08d}"


def fetch_day_bars(symbol: str, day: str):
    """Real 1-min OHLCV trade bars for one contract, ET index. Prints cost."""
    start = f"{day}T13:30:00Z"
    end = f"{day}T20:05:00Z"
    params = {"dataset": "OPRA.PILLAR", "symbols": symbol, "schema": "ohlcv-1m",
              "stype_in": "raw_symbol", "start": start, "end": end}
    try:
        c = requests.get(f"{HIST}/metadata.get_cost", params=params,
                         auth=auth(), timeout=30)
        cost = float(c.text) if c.ok else None
    except requests.RequestException:
        cost = None
    print(f"  download cost: ${cost:.4f}" if cost is not None else
          "  (couldn't estimate cost, proceeding — these pulls are sub-cent)")
    r = requests.get(f"{HIST}/timeseries.get_range",
                     params={**params, "encoding": "csv",
                             "pretty_px": "true", "pretty_ts": "true"},
                     auth=auth(), timeout=120)
    if r.status_code == 422 and "available up to" in r.text:
        # intraday: the archive lags ~an hour behind live — clamp and retry
        import re as _re
        m = _re.search(r"available up to '([^']+)'", r.text)
        if m:
            avail = m.group(1).replace(" ", "T").replace("+00:00", "Z")
            print(f"  (archive currently ends {m.group(1)} — clamping)")
            r = requests.get(f"{HIST}/timeseries.get_range",
                             params={**params, "end": avail, "encoding": "csv",
                                     "pretty_px": "true", "pretty_ts": "true"},
                             auth=auth(), timeout=120)
    if not r.ok:
        print(f"  fetch failed: {r.status_code} {r.text[:200]}")
        return None
    df = pd.read_csv(io.StringIO(r.text))
    if df.empty:
        return None
    df["ts"] = pd.to_datetime(df["ts_event"], utc=True).dt.tz_convert(ET)
    return df.set_index("ts")


def real_price_at(bars, day: str, hms: str):
    """Last real traded price at/just before this ET time."""
    t = pd.Timestamp(f"{day} {hms}", tz=ET)
    upto = bars[bars.index <= t]
    if upto.empty:
        return None
    return float(upto["close"].iloc[-1])


def compare(label: str, ours, real):
    if ours is None or real is None:
        print(f"  {label:<28} ours: {ours}   real: {real}   (no comparison)")
        return
    diff = (ours / real - 1) * 100 if real else 0.0
    print(f"  {label:<28} ours: ${ours:<6.2f} real: ${real:<6.2f} "
          f"(we were {diff:+.0f}% off)")


def main():
    if not KEY:
        sys.exit("DATABENTO_API_KEY not set in .env")
    r = requests.get(f"{HIST}/metadata.list_datasets", auth=auth(), timeout=30)
    if not r.ok:
        sys.exit(f"key check failed: {r.status_code} {r.text[:200]}")
    if "OPRA.PILLAR" not in r.text:
        sys.exit("key works but OPRA dataset not visible — check the portal")
    print("Databento key OK, OPRA options archive reachable.\n")

    book = PositionBook()
    checked = 0
    for p in book.positions:
        if p.final_pnl_pct is None:
            continue
        sym = osi_symbol(p.ticker, p.expiry, p.right, p.strike)
        print(f"{p.date}  {p.ticker} {p.strike:g} {p.direction.upper()} "
              f"(exp {p.expiry})  [{sym.strip()}]")
        bars = fetch_day_bars(sym, p.date)
        if bars is None:
            print("  no real data returned (too recent or symbol gap) — "
                  "try again after the close.\n")
            continue
        compare(f"entry {p.time_et[:5]} ({p.entry_source})", p.entry_mid,
                real_price_at(bars, p.date, p.time_et))
        if p.half_exit:
            compare(f"sell-half {p.half_exit['time'][:5]}", p.half_exit["mark"],
                    real_price_at(bars, p.date, p.half_exit["time"]))
        if p.final_exit:
            compare(f"{p.final_exit['reason']} {p.final_exit['time'][:5]}",
                    p.final_exit["mark"],
                    real_price_at(bars, p.date, p.final_exit["time"]))
        # what the whole trade really did, real prices only
        re_entry = real_price_at(bars, p.date, p.time_et)
        re_exit = real_price_at(bars, p.date, (p.final_exit or {}).get("time", "16:00:00"))
        if re_entry and re_exit:
            print(f"  real-world move entry->final exit: "
                  f"{(re_exit / re_entry - 1) * 100:+.0f}% "
                  f"(bot's tracked result: {p.final_pnl_pct:+.0f}% weighted)")
        print()
        checked += 1
    # pre-book alerts worth checking (the position book started 6/12)
    LEGACY = [{"date": "2026-06-11", "ticker": "SPX", "right": "C",
               "strike": 7300.0, "expiry": "2026-06-11", "time": "09:50:03"}]
    for a in LEGACY:
        sym = osi_symbol(a["ticker"], a["expiry"], a["right"], a["strike"])
        print(f"{a['date']}  {a['ticker']} {a['strike']:g} CALL "
              f"(legacy alert)  [{sym.strip()}]")
        bars = fetch_day_bars(sym, a["date"])
        if bars is None:
            print("  no real data returned.\n")
            continue
        e = real_price_at(bars, a["date"], a["time"])
        for mins in (5, 10, 15):
            t = (datetime.strptime(a["time"], "%H:%M:%S")
                 + timedelta(minutes=mins)).strftime("%H:%M:%S")
            px = real_price_at(bars, a["date"], t)
            if e and px:
                print(f"  real price {a['time'][:5]} -> +{mins}min: "
                      f"${e:.2f} -> ${px:.2f} ({(px / e - 1) * 100:+.0f}%)")
        after = bars[bars.index >= pd.Timestamp(f"{a['date']} {a['time']}", tz=ET)]
        if e is not None and not after.empty:
            hi = float(after["high"].max())
            print(f"  real day high after alert: ${hi:.2f} "
                  f"({(hi / e - 1) * 100:+.0f}% from entry)")
        print()
        checked += 1
    if not checked:
        print("No closed positions in the book yet to check.")


if __name__ == "__main__":
    main()
