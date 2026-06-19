"""Entry-filter bake-off: do regime / trend / chop filters improve the live
strategy?

Same entries as the live scanner, same LIVE exit (half +25 -> give-back-40 ->
stop -70 -> time stop). For every entry we compute features KNOWN AT DECISION
TIME ONLY (no look-ahead), simulate the exit ONCE, then score each candidate
filter as the subset of trades that pass it. Scored on the population the bot
actually trades (allow-list: SPX:call, SPY:call, QCOM:call, TSLA:put).

Honesty: ~60 days, partly in-sample, approximated (optimistic) 0DTE pricing.
A filter that removes most trades has a tiny, untrustworthy sample — trade
count is shown so you can judge. Trust the RELATIVE ranking, not the dollars.

Usage:
    python backtest_entry_filters.py
"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import time as dtime
from pathlib import Path

import pandas as pd

import config
from backtest import (SLIPPAGE, BtTrade, bs_price, expiry_for, load_data,
                      metrics, realized_vol, years_to_expiry)
from backtest_new_rules import simulate_new_exits
from strategy import StrategyConfig, detect_setup

REPORTS_DIR = Path(__file__).parent / "reports"
ALLOWED = {("SPX", "C"), ("SPY", "C"), ("QCOM", "C"), ("TSLA", "P")}
OR_END = dtime(9, 45)   # opening range = 9:30-9:45


def features(session_bars, entry_ts):
    """Everything we know at decision time from this session's bars up to entry."""
    sofar = session_bars[session_bars.index <= entry_ts]
    px = float(sofar["Close"].iloc[-1])
    day_open = float(sofar["Open"].iloc[0])
    # VWAP (volume-weighted) where volume is real, else a simple mean-close proxy
    # (the ^GSPC index has no real volume, so it falls back to the mean)
    vol = sofar["Volume"].fillna(0).astype(float) if "Volume" in sofar else None
    if vol is not None and float(vol.sum()) > 0:
        vwap = float((sofar["Close"] * vol).sum() / vol.sum())
    else:
        vwap = float(sofar["Close"].mean())
    rets = sofar["Close"].pct_change().dropna()
    signs = [1 if r > 0 else (-1 if r < 0 else 0) for r in rets]
    nz = [s for s in signs if s != 0]
    flips = sum(1 for a, b in zip(nz, nz[1:]) if a != b)
    orbars = sofar[sofar.index.time < OR_END]
    orh = float(orbars["High"].max()) if len(orbars) else None
    orl = float(orbars["Low"].min()) if len(orbars) else None
    return {"px": px, "day_open": day_open, "vwap": vwap, "flips": flips,
            "orh": orh, "orl": orl}


def collect(cfg, intraday, daily):
    """Every live entry, each with decision-time features and its give-back trade."""
    rows = []
    for ticker in cfg.watchlist:
        bars_all = intraday[ticker]
        for day in sorted(set(bars_all.index.date)):
            day_bars = bars_all[bars_all.index.date == day]
            if day_bars.empty:
                continue
            sigma = realized_vol(daily[ticker]["Close"], day)
            if sigma <= 0:
                continue
            entered = set()
            for i in range(len(day_bars)):
                upto = day_bars.iloc[: i + 1]
                now = upto.index[-1].to_pydatetime()
                if not (cfg.entry_start <= now.time() <= cfg.entry_end):
                    continue
                setup = detect_setup(ticker, upto, now, cfg)
                if setup is None or setup.direction in entered:
                    continue
                entered.add(setup.direction)
                right = "C" if setup.direction == "call" else "P"
                expiry = expiry_for(ticker, now)
                entry_prem = bs_price(setup.spot, setup.strike,
                                      years_to_expiry(now, expiry), sigma, right) * (1 + SLIPPAGE)
                if entry_prem < 0.10:
                    continue
                legs = simulate_new_exits(bars_all, upto.index[-1], entry_prem,
                                          setup.strike, right, sigma, expiry, cfg)
                if not legs:
                    continue
                feat = features(day_bars, upto.index[-1])
                feat.update({"right": right, "mom": setup.mom_pct,
                             "t": now.time(), "allowed": (ticker, right) in ALLOWED})
                rows.append((feat, BtTrade(ticker, right, setup.strike, now,
                                           entry_prem, legs, expiry)))
    return rows


def aligned_open(f):
    return f["px"] > f["day_open"] if f["right"] == "C" else f["px"] < f["day_open"]


def aligned_vwap(f):
    return f["px"] >= f["vwap"] if f["right"] == "C" else f["px"] <= f["vwap"]


def orb(f):
    if f["right"] == "C":
        return f["orh"] is not None and f["px"] > f["orh"]
    return f["orl"] is not None and f["px"] < f["orl"]


FILTERS = [
    ("BASELINE (live, no extra filter)", lambda f: True),
    ("OPEN-ALIGN (price on signal side of open)", aligned_open),
    ("VWAP-ALIGN (price on signal side of VWAP)", aligned_vwap),
    ("ORB (break the 9:30-9:45 opening range)", orb),
    ("CHOP<=3 (<=3 morning sign-flips)", lambda f: f["flips"] <= 3),
    ("CHOP<=4", lambda f: f["flips"] <= 4),
    ("CHOP<=5", lambda f: f["flips"] <= 5),
    ("MOM>=0.10%", lambda f: abs(f["mom"]) >= 0.10),
    ("MOM>=0.15%", lambda f: abs(f["mom"]) >= 0.15),
    ("MOM>=0.20%", lambda f: abs(f["mom"]) >= 0.20),
    ("EARLY (entry by 10:15)", lambda f: f["t"] <= dtime(10, 15)),
    ("OPEN-ALIGN + CHOP<=5", lambda f: aligned_open(f) and f["flips"] <= 5),
    ("OPEN-ALIGN + MOM>=0.15%", lambda f: aligned_open(f) and abs(f["mom"]) >= 0.15),
    ("OPEN-ALIGN + EARLY", lambda f: aligned_open(f) and f["t"] <= dtime(10, 15)),
    ("OPEN-ALIGN + VWAP-ALIGN", lambda f: aligned_open(f) and aligned_vwap(f)),
]


def main():
    cfg = StrategyConfig()
    print("Loading data...")
    intraday, daily = load_data(cfg)
    print("Collecting entries + simulating the live give-back exit once each...")
    rows = collect(cfg, intraday, daily)
    allowed = [(f, t) for f, t in rows if f["allowed"]]
    print(f"{len(rows)} raw entries, {len(allowed)} on the live allow-list.\n")

    header = (f"{'ENTRY FILTER (allow-list population)':<44} {'trd':>3}  {'win':>6}  "
              f"{'avgW/avgL':>14}  {'exp/tr':>7}  {'totalP&L':>10}  {'maxDD':>9}")
    lines = ["ENTRY-FILTER BAKE-OFF — live give-back exit, identical entries",
             "(approx pricing: dollars optimistic; trust RELATIVE ranking; watch trade count)",
             "", header, "-" * len(header)]
    results = []
    for name, pred in FILTERS:
        trades = [t for f, t in allowed if pred(f)]
        m = metrics(trades)
        results.append((name, m))
        if not m:
            lines.append(f"{name:<44} no trades")
            continue
        lines.append(f"{name:<44} {m['trades']:>3}  {m['win_rate']:>5.1f}%  "
                     f"{m['avg_win_pct']:>+7.1f}/{m['avg_loss_pct']:>+6.1f}  "
                     f"{m['expectancy_pct']:>+6.1f}%  ${m['total_pnl']:>9,.0f}  "
                     f"${m['max_drawdown']:>8,.0f}")
    base = results[0][1]
    lines += ["", "vs BASELINE (Δ total P&L / Δ expectancy / Δ win-rate / kept trades):"]
    for name, m in results[1:]:
        if m and base:
            lines.append(f"  {name:<42} ${m['total_pnl'] - base['total_pnl']:>+9,.0f}  "
                         f"{m['expectancy_pct'] - base['expectancy_pct']:>+5.1f}%  "
                         f"{m['win_rate'] - base['win_rate']:>+5.1f}pp  "
                         f"{m['trades']}/{base['trades']}")
    out = "\n".join(lines)
    print(out)
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / "entry_filters_compare.txt").write_text(out, encoding="utf-8")
    print(f"\nWrote {REPORTS_DIR / 'entry_filters_compare.txt'}")


if __name__ == "__main__":
    main()
