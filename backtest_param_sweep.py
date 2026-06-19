"""Re-optimize the exit parameters for the LIVE give-back exit.

The -70% stop and +25% half were tuned under the OLD momentum-flip trail. Now
that the runner uses a give-back trail, re-check them. Same entries, allow-list
only, only the exit PARAMS change (half trigger, give-back, hard stop).

Honesty: ~60 days, partly in-sample, optimistic 0DTE pricing. A grid search on
this little data WILL overfit a single lucky cell — so we look for a ROBUST
region and only move a param if a broad, sensible neighborhood beats current.
Trust RELATIVE ranking, not dollars.

Usage:
    python backtest_param_sweep.py
"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path

import pandas as pd

import config
from backtest import (CONTRACTS, SLIPPAGE, BtTrade, bs_price, expiry_for,
                      load_data, metrics, realized_vol, years_to_expiry)
from strategy import StrategyConfig, detect_setup

REPORTS_DIR = Path(__file__).parent / "reports"
ALLOWED = {("SPX", "C"), ("SPY", "C"), ("QCOM", "C"), ("TSLA", "P")}


def sim(entry, half_trig, giveback, stop):
    """Give-back exit with explicit params (no globals)."""
    bars_all, entry_ts = entry["bars_all"], entry["entry_ts"]
    strike, right, sigma, expiry = entry["strike"], entry["right"], entry["sigma"], entry["expiry"]
    entry_prem = entry["entry_prem"]
    future = bars_all[(bars_all.index > entry_ts) & (bars_all.index <= pd.Timestamp(expiry))]
    legs, remaining, half_taken, last, peak = [], CONTRACTS, False, None, None
    for ts, row in future.iterrows():
        prem_net = bs_price(float(row["Close"]), strike,
                            years_to_expiry(ts.to_pydatetime(), expiry), sigma, right) * (1 - SLIPPAGE)
        ret = (prem_net / entry_prem - 1) * 100
        last = (ts, prem_net)
        peak = ret if peak is None else max(peak, ret)
        if ret <= stop:
            legs.append((ts, prem_net, remaining, "stop"))
            return legs
        if not half_taken and ret >= half_trig:
            h = remaining // 2
            legs.append((ts, prem_net, h, "half"))
            remaining -= h
            half_taken = True
            continue
        if half_taken and ret <= peak - giveback:
            legs.append((ts, prem_net, remaining, "give-back"))
            return legs
    if last is not None and remaining > 0:
        legs.append((last[0], last[1], remaining, "time"))
    return legs


def collect(cfg, intraday, daily):
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
                if (ticker, right) not in ALLOWED:
                    continue
                expiry = expiry_for(ticker, now)
                entry_prem = bs_price(setup.spot, setup.strike,
                                      years_to_expiry(now, expiry), sigma, right) * (1 + SLIPPAGE)
                if entry_prem < 0.10:
                    continue
                rows.append({"bars_all": bars_all, "entry_ts": upto.index[-1],
                             "entry_prem": entry_prem, "strike": setup.strike,
                             "right": right, "sigma": sigma, "expiry": expiry,
                             "ticker": ticker, "now": now})
    return rows


def run(entries, half, give, stop):
    trades = []
    for e in entries:
        legs = sim(e, half, give, stop)
        if legs:
            trades.append(BtTrade(e["ticker"], e["right"], e["strike"], e["now"],
                                  e["entry_prem"], legs, e["expiry"]))
    return metrics(trades)


def main():
    cfg = StrategyConfig()
    print("Loading data...")
    intraday, daily = load_data(cfg)
    print("Collecting allow-list entries...")
    entries = collect(cfg, intraday, daily)
    print(f"{len(entries)} allow-list entries.\n")

    CUR = (25, 40, -70)
    combos = []
    for half in (20, 25, 30, 40):
        for give in (30, 40, 50):
            for stop in (-50, -60, -70, -80, -90):
                m = run(entries, half, give, stop)
                if m:
                    combos.append((half, give, stop, m))

    cur_m = run(entries, *CUR)
    print(f"CURRENT half+{CUR[0]} / give-back {CUR[1]} / stop {CUR[2]}: "
          f"{cur_m['win_rate']:.1f}% win, {cur_m['expectancy_pct']:+.1f}% exp, "
          f"${cur_m['total_pnl']:,.0f}, DD ${cur_m['max_drawdown']:,.0f}\n")

    # CRITICAL: total P&L here assumes a fixed 2 contracts, but LIVE sizing is
    # risk-based — alloc = RISK / (|stop|/100), so a WIDER stop forces a SMALLER
    # position. The sizing-fair metric is per-trade ACCOUNT return = expectancy%
    # x RISK / |stop| (max loss is pinned at RISK% of account for every stop). So
    # we rank by that, not by the misleading fixed-contract dollars.
    def acct_exp(m, stop):
        return m["expectancy_pct"] * config.RISK_PER_TRADE_PCT / abs(stop)

    viable = [c for c in combos if c[3]["win_rate"] >= 70.0]
    viable.sort(key=lambda c: -acct_exp(c[3], c[2]))
    lines = ["EXIT PARAM SWEEP — live give-back exit, allow-list, win-rate>=70%",
             "(approx pricing: optimistic; ranked by SIZING-FAIR account return,",
             " NOT fixed-contract dollars; look for a ROBUST region, not one cell)",
             "", f"{'half':>4} {'give':>4} {'stop':>4}  {'win':>6}  {'exp/tr':>7}  "
             f"{'acct%/tr':>8}  {'fixed$P&L':>10}  {'maxDD':>9}", "-" * 70]
    for half, give, stop, m in viable[:15]:
        star = "  <- CURRENT" if (half, give, stop) == CUR else ""
        lines.append(f"{half:>4} {give:>4} {stop:>4}  {m['win_rate']:>5.1f}%  "
                     f"{m['expectancy_pct']:>+6.1f}%  {acct_exp(m, stop):>+7.3f}%  "
                     f"${m['total_pnl']:>9,.0f}  ${m['max_drawdown']:>8,.0f}{star}")
    out = "\n".join(lines)
    print(out)
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / "param_sweep.txt").write_text(out, encoding="utf-8")
    print(f"\nWrote {REPORTS_DIR / 'param_sweep.txt'}")


if __name__ == "__main__":
    main()
