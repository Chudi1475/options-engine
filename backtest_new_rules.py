"""Backtest of the NEW exit system on the exact same entries as backtest.py:

    sell HALF at +TP_HALF_PCT (+25%)
    -> let the runner RUN; sell it when it gives back RUNNER_GIVEBACK_PCT
       points from its peak
    -> hard stop at STOP_PCT any time
    -> close at session end / expiry

Same honesty rules as backtest.py: approximated Black-Scholes pricing
(labeled), 1.5% slippage each way, per-contract fees, no lookahead — the
momentum-flip check at each bar uses only bars at or before that bar, the
same data the live scanner would have had.

Writes reports/backtest_new_rules.{md,json}. The scanner quotes these
numbers on cards (labeled NEW-RULES BACKTEST) until 30 live signals exist.

Usage:
    python backtest_new_rules.py
"""

import json
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


def momentum_at(day_bars: pd.DataFrame, ts, mom_bars: int):
    """The entry momentum measure, evaluated at bar `ts` using only bars at
    or before `ts` — exactly what the live scanner sees."""
    closes = day_bars[day_bars.index <= ts]["Close"]
    if len(closes) < mom_bars + 1:
        return None
    return (float(closes.iloc[-1]) / float(closes.iloc[-(mom_bars + 1)]) - 1) * 100


def simulate_new_exits(bars_all, entry_ts, entry_prem, strike, right,
                       sigma, expiry, cfg):
    """Walk forward bar by bar applying the live exit framework: sell HALF at
    +25%, then let the runner RUN and sell it only when it gives back
    RUNNER_GIVEBACK_PCT points from its peak; hard stop; time stop."""
    future = bars_all[(bars_all.index > entry_ts)
                      & (bars_all.index <= pd.Timestamp(expiry))]
    legs, remaining, half_taken, last, peak = [], CONTRACTS, False, None, None
    give = config.RUNNER_GIVEBACK_PCT
    for ts, row in future.iterrows():
        S = float(row["Close"])
        prem_net = bs_price(S, strike, years_to_expiry(ts.to_pydatetime(), expiry),
                            sigma, right) * (1 - SLIPPAGE)
        ret = (prem_net / entry_prem - 1) * 100
        last = (ts, prem_net)
        peak = ret if peak is None else max(peak, ret)
        if ret <= config.STOP_PCT:
            legs.append((ts, prem_net, remaining, f"stop {config.STOP_PCT:g}%"))
            return legs
        if not half_taken and ret >= config.TP_HALF_PCT:
            half = remaining // 2
            legs.append((ts, prem_net, half, f"half +{config.TP_HALF_PCT:g}%"))
            remaining -= half
            half_taken = True
            continue  # start trailing the runner on the NEXT bar
        if half_taken and ret <= peak - give:
            legs.append((ts, prem_net, remaining, f"give-back {give:g} off peak"))
            return legs
    if last is not None and remaining > 0:
        legs.append((last[0], last[1], remaining, "time stop"))
    return legs


def run(cfg, intraday, daily):
    trades = []
    for ticker in cfg.watchlist:
        bars_all = intraday[ticker]
        for day in sorted(set(bars_all.index.date)):
            day_bars = bars_all[bars_all.index.date == day]
            if day_bars.empty:
                continue
            sigma = realized_vol(daily[ticker]["Close"], day)
            if sigma <= 0:
                continue
            entered_dirs = set()
            for i in range(len(day_bars)):
                upto = day_bars.iloc[: i + 1]
                now = upto.index[-1].to_pydatetime()
                if not (cfg.entry_start <= now.time() <= cfg.entry_end):
                    continue
                setup = detect_setup(ticker, upto, now, cfg)
                if setup is None or setup.direction in entered_dirs:
                    continue
                entered_dirs.add(setup.direction)
                right = "C" if setup.direction == "call" else "P"
                expiry = expiry_for(ticker, now)
                model_prem = bs_price(setup.spot, setup.strike,
                                      years_to_expiry(now, expiry), sigma, right)
                entry_prem = model_prem * (1 + SLIPPAGE)
                if entry_prem < 0.10:
                    continue
                legs = simulate_new_exits(bars_all, upto.index[-1], entry_prem,
                                          setup.strike, right, sigma, expiry, cfg)
                if not legs:
                    continue
                trades.append(BtTrade(ticker, right, setup.strike, now,
                                      entry_prem, legs, expiry))
    return trades


def fmt(m, title):
    if m is None:
        return [f"### {title}", "", "No trades.", ""]
    return [
        f"### {title}", "",
        f"- Trades: **{m['trades']}** ({m['start']} - {m['end']})",
        f"- Win rate: **{m['win_rate']:.1f}%**",
        f"- Avg win: **{m['avg_win_pct']:+.1f}%** | Avg loss: **{m['avg_loss_pct']:+.1f}%**",
        f"- Expectancy: **{m['expectancy_pct']:+.1f}% per trade**",
        f"- Total P&L (2 contracts/trade, net of slippage+fees): **${m['total_pnl']:,.0f}**",
        f"- Max drawdown: **${m['max_drawdown']:,.0f}**",
        "",
    ]


def main():
    REPORTS_DIR.mkdir(exist_ok=True)
    cfg = StrategyConfig()  # direction="both" — same entries as the live scanner
    print("Loading data...")
    intraday, daily = load_data(cfg)
    print("Simulating new exit rules...")
    trades = run(cfg, intraday, daily)

    per_setup = {}
    for ticker in {t.ticker for t in trades}:
        for right, dirname in (("C", "call"), ("P", "put")):
            mm = metrics([t for t in trades
                          if t.ticker == ticker and t.right == right])
            if mm:
                per_setup[f"{ticker}:{dirname}"] = mm
    overall = metrics(trades)

    lines = ["# Backtest — NEW exit rules", "",
             "**APPROXIMATED OPTIONS PRICING** — Black-Scholes on 20-day "
             "realized vol (no free historical option chains exist). Slippage "
             "1.5% each way + $1.30/contract/side included. Same entries as "
             "backtest.py; only the exits differ:", "",
             f"- sell half at **+{config.TP_HALF_PCT:g}%**",
             f"- let the runner RUN, sell it when it **gives back "
             f"{config.RUNNER_GIVEBACK_PCT:g} points** from its peak",
             f"- hard stop **{config.STOP_PCT:g}%**",
             "- close at session end / expiry", ""]
    lines += fmt(overall, "All trades")
    for key in sorted(per_setup, key=lambda k: -per_setup[k]["win_rate"]):
        lines += fmt(per_setup[key], key)

    old = None
    old_path = REPORTS_DIR / "backtest_results.json"
    if old_path.exists():
        old = json.loads(old_path.read_text(encoding="utf-8"))
        lines += ["## vs the old rules (+15/-60)", ""]
        lines.append("| setup | old win% | old expectancy | new win% | new expectancy |")
        lines.append("|---|---|---|---|---|")
        for key in sorted(set(per_setup) | set(old.get("per_setup", {}))):
            o = old.get("per_setup", {}).get(key)
            n = per_setup.get(key)
            lines.append(
                f"| {key} | {o['win_rate']:.1f}% | {o['expectancy_pct']:+.1f}% |"
                f" {n['win_rate']:.1f}% | {n['expectancy_pct']:+.1f}% |"
                if o and n else f"| {key} | - | - | - | - |")
        lines.append("")

    (REPORTS_DIR / "backtest_new_rules.md").write_text("\n".join(lines),
                                                       encoding="utf-8")
    (REPORTS_DIR / "backtest_new_rules.json").write_text(json.dumps({
        "pricing": "approximated (Black-Scholes, realized vol)",
        "rules": {"tp_half_pct": config.TP_HALF_PCT, "stop_pct": config.STOP_PCT,
                  "runner_giveback_pct": config.RUNNER_GIVEBACK_PCT,
                  "trail": f"give-back {config.RUNNER_GIVEBACK_PCT:g} off peak"},
        "overall": overall,
        "per_setup": per_setup,
    }, indent=2), encoding="utf-8")
    print(f"Wrote {REPORTS_DIR / 'backtest_new_rules.md'}")
    print(f"Wrote {REPORTS_DIR / 'backtest_new_rules.json'}")
    if overall:
        print(f"\nOverall: {overall['trades']} trades, win rate "
              f"{overall['win_rate']:.1f}%, expectancy "
              f"{overall['expectancy_pct']:+.1f}%/trade")
    for key, mm in sorted(per_setup.items(), key=lambda kv: -kv[1]["win_rate"]):
        print(f"  {key}: {mm['win_rate']:.1f}% win, "
              f"{mm['expectancy_pct']:+.1f}%/trade, {mm['trades']} trades")


if __name__ == "__main__":
    main()
