"""Long-horizon backtests — the honest maximum at each resolution.

Free data caps what's testable:
- 5-minute bars: ~60 days  -> backtest.py (full-fidelity strategy)
- 1-hour bars:   ~2 years  -> this file: first-hour momentum version
- daily bars:    5+ years  -> this file: previous-day momentum PROXY

The hourly version is a coarser cousin of the live strategy (momentum read
over the first hour instead of 15 minutes, one entry at 10:30 ET).
The daily version is a DIFFERENT strategy that only shares the spirit
(momentum continuation): prev day up -> call at open, exit by close/expiry.
Daily exits are approximated with day High/Low, stop checked FIRST
(pessimistic). All pricing is Black-Scholes on 20-day realized vol —
approximated options pricing, same caveats as backtest.py.

SPX trades before 05/16/2022 are excluded: daily SPX expirations did not
exist, so the instrument could not have been traded.

Usage:
    python backtest_long.py
"""

import math
from datetime import timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from backtest import (FEE_PER_CONTRACT, MULTIPLIER, SLIPPAGE, bs_price,
                      metrics, realized_vol)
from strategy import StrategyConfig

REPORTS_DIR = Path(__file__).parent / "reports"
ET = "America/New_York"
SPX_DAILY_EXPIRY_START = pd.Timestamp("2022-05-16").date()
CONTRACTS = 1  # single exit brackets here, no half leg


class Trade:
    def __init__(self, ticker, right, entry_time, entry_prem, exit_prem):
        self.ticker, self.right = ticker, right
        self.entry_time = entry_time
        self.entry_premium = entry_prem
        cost = entry_prem * CONTRACTS * MULTIPLIER + FEE_PER_CONTRACT * CONTRACTS
        gross = (exit_prem - entry_prem) * CONTRACTS * MULTIPLIER
        self.pnl = gross - FEE_PER_CONTRACT * CONTRACTS * 2
        self.ret_pct = self.pnl / cost * 100


def fetch(symbol, period, interval):
    df = yf.download(symbol, period=period, interval=interval,
                     progress=False, auto_adjust=False)
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    if interval != "1d":
        df.index = df.index.tz_convert(ET)
    return df


def strike_for(spot, ticker, right):
    inc = 5.0 if ticker == "SPX" else 2.5
    return (-(-spot // inc) * inc) if right == "C" else (spot // inc * inc)


def years(td: timedelta) -> float:
    return max(td.total_seconds(), 0) / (365.0 * 24 * 3600)


def hourly_backtest(target_pct, stop_pct):
    """First-hour momentum, enter 10:30 ET, exits on hourly closes."""
    cfg = StrategyConfig()
    trades = []
    for ticker, yfs in cfg.watchlist.items():
        bars = fetch(yfs, "730d", "1h")
        daily = fetch(yfs, "3y", "1d")
        for day in sorted(set(bars.index.date)):
            db = bars[bars.index.date == day]
            if len(db) < 3:
                continue
            if ticker == "SPX" and day < SPX_DAILY_EXPIRY_START:
                continue
            sigma = realized_vol(daily["Close"], day)
            if sigma <= 0:
                continue
            first = db.iloc[0]
            mom = first["Close"] / first["Open"] - 1
            if mom > 0:
                right = "C"
            elif mom < 0:
                right = "P"
            else:
                continue
            spot = float(first["Close"])
            strike = strike_for(spot, ticker, right)
            entry_t = db.index[1].to_pydatetime()  # 10:30 bar open time
            if ticker == "SPX":
                expiry = entry_t.replace(hour=16, minute=0)
            else:
                fri = entry_t + timedelta(days=(4 - entry_t.weekday()) % 7)
                expiry = fri.replace(hour=16, minute=0)
            entry_prem = bs_price(spot, strike, years(expiry - entry_t),
                                  sigma, right) * (1 + SLIPPAGE)
            if entry_prem < 0.10:
                continue
            walk = bars[(bars.index >= db.index[1]) & (bars.index <= pd.Timestamp(expiry))]
            exit_prem = None
            for ts, row in walk.iterrows():
                prem = bs_price(float(row["Close"]), strike,
                                years(expiry - ts.to_pydatetime()), sigma, right)
                prem_net = prem * (1 - SLIPPAGE)
                ret = (prem_net / entry_prem - 1) * 100
                if ret <= stop_pct or ret >= target_pct:
                    exit_prem = prem_net
                    break
                exit_prem = prem_net  # time stop fallback
            if exit_prem is not None:
                trades.append(Trade(ticker, right, entry_t, entry_prem, exit_prem))
    return trades


def daily_backtest(target_pct, stop_pct, period="5y"):
    """PROXY: prev day up -> call at open (down -> put), exit by close/expiry.
    Stop checked before target within each day (pessimistic)."""
    cfg = StrategyConfig()
    trades = []
    for ticker, yfs in cfg.watchlist.items():
        daily = fetch(yfs, period, "1d")
        closes = daily["Close"]
        for i in range(21, len(daily)):
            day = daily.index[i].date()
            if ticker == "SPX" and day < SPX_DAILY_EXPIRY_START:
                continue
            prev_ret = closes.iloc[i - 1] / closes.iloc[i - 2] - 1
            if prev_ret == 0:
                continue
            right = "C" if prev_ret > 0 else "P"
            sigma = realized_vol(closes, day)
            if sigma <= 0:
                continue
            spot = float(daily["Open"].iloc[i])
            strike = strike_for(spot, ticker, right)
            entry_t = pd.Timestamp(day).replace(hour=9, minute=30)
            if ticker == "SPX":
                exp_i = i  # 0DTE
            else:
                exp_day = day + timedelta(days=(4 - entry_t.weekday()) % 7)
                later = [j for j in range(i, len(daily)) if daily.index[j].date() <= exp_day]
                exp_i = later[-1] if later else i
            expiry = pd.Timestamp(daily.index[exp_i].date()).replace(hour=16, minute=0)
            entry_prem = bs_price(spot, strike, years(expiry - entry_t),
                                  sigma, right) * (1 + SLIPPAGE)
            if entry_prem < 0.10:
                continue
            exit_prem = None
            for j in range(i, exp_i + 1):
                bar_day = daily.index[j].date()
                eod = pd.Timestamp(bar_day).replace(hour=16, minute=0)
                T_mid = years(expiry - pd.Timestamp(bar_day).replace(hour=12, minute=45))
                lo_spot = float(daily["Low"].iloc[j])
                hi_spot = float(daily["High"].iloc[j])
                worst = min(bs_price(lo_spot, strike, T_mid, sigma, right),
                            bs_price(hi_spot, strike, T_mid, sigma, right))
                best = max(bs_price(lo_spot, strike, T_mid, sigma, right),
                           bs_price(hi_spot, strike, T_mid, sigma, right))
                worst_ret = (worst * (1 - SLIPPAGE) / entry_prem - 1) * 100
                best_ret = (best * (1 - SLIPPAGE) / entry_prem - 1) * 100
                if worst_ret <= stop_pct:           # pessimistic: stop first
                    exit_prem = entry_prem * (1 + stop_pct / 100)
                    break
                if best_ret >= target_pct:
                    exit_prem = entry_prem * (1 + target_pct / 100)
                    break
                exit_prem = bs_price(float(closes.iloc[j]), strike,
                                     years(expiry - eod), sigma, right) * (1 - SLIPPAGE)
            if exit_prem is not None:
                trades.append(Trade(ticker, right, entry_t, entry_prem, exit_prem))
    return trades


def section(title, trades, note=""):
    lines = [f"## {title}", ""]
    if note:
        lines += [note, ""]
    for label, sub in [("All", trades),
                       ("Calls", [t for t in trades if t.right == "C"]),
                       ("Puts", [t for t in trades if t.right == "P"])]:
        m = metrics(sub)
        if not m:
            lines.append(f"- {label}: no trades")
            continue
        lines.append(
            f"- {label}: **{m['win_rate']:.1f}% win rate**, "
            f"expectancy {m['expectancy_pct']:+.1f}%/trade, "
            f"P&L ${m['total_pnl']:,.0f}, max DD ${m['max_drawdown']:,.0f}, "
            f"{m['trades']} trades ({m['start']}–{m['end']})")
    lines.append("")
    return lines


def main():
    REPORTS_DIR.mkdir(exist_ok=True)
    # keep in sync with the scanner bracket chosen by backtest.py
    TARGET, STOP = 15.0, -60.0
    lines = [
        "# Long-horizon backtests (approximated options pricing)",
        "",
        f"Exit bracket for every test below: **+{TARGET:g}% target / {STOP:g}% stop** "
        "(the highest-win-rate bracket from the 60-day grid in backtest.md). "
        "Black-Scholes on realized vol, 1.5% slippage each way, "
        f"${FEE_PER_CONTRACT}/contract/side. SPX excluded before 05/16/2022 "
        "(daily expirations didn't exist).",
        "",
    ]
    print("Running hourly (~2y)...")
    hourly = hourly_backtest(TARGET, STOP)
    lines += section(
        "Hourly — ~2 years",
        hourly,
        "Coarser cousin of the live strategy: momentum read over the first hour, "
        "single entry at 10:30 ET, exits on hourly closes.")
    print("Running daily proxy (5y)...")
    daily = daily_backtest(TARGET, STOP, "5y")
    lines += section(
        "Daily proxy — 5 years",
        daily,
        "**This is NOT the live strategy** — daily bars cannot see 15-minute "
        "momentum. Proxy: previous day up -> call at next open (down -> put), "
        "stop checked before target inside each day (pessimistic). It tests "
        "whether short-term momentum continuation paid at all over 5 years.")
    out = REPORTS_DIR / "backtest_long.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
