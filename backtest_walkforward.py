"""Walk-forward check of the shipped exit change (give-back-40 vs the old
momentum-flip trail).

The biggest honesty caveat on all these backtests is that the ~60-day window is
partly IN-SAMPLE (the strategy was derived inside it). So split the entries by
date into halves (and thirds) and check that give-back-40 STILL beats the
momentum-flip trail in the LATER (more out-of-sample) period, not just overall.
Both exits share the same -70 stop and +25 half, so this is sizing-neutral —
a fair head-to-head.

Usage:
    python backtest_walkforward.py
"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

import config
from backtest import (CONTRACTS, SLIPPAGE, BtTrade, bs_price, load_data,
                      metrics, years_to_expiry)
from backtest_new_rules import momentum_at
from backtest_runner_trail import ALLOWED, collect_entries, simulate_giveback
from strategy import StrategyConfig


def sim_flip(e, cfg):
    """The OLD exit: half +25, then trail the runner until the 15-min momentum
    flips; hard stop; time stop."""
    bars_all, entry_ts, expiry = e["bars_all"], e["entry_ts"], e["expiry"]
    future = bars_all[(bars_all.index > entry_ts) & (bars_all.index <= pd.Timestamp(expiry))]
    legs, remaining, half_taken, last = [], CONTRACTS, False, None
    for ts, row in future.iterrows():
        prem_net = bs_price(float(row["Close"]), e["strike"],
                            years_to_expiry(ts.to_pydatetime(), expiry), e["sigma"], e["right"]) * (1 - SLIPPAGE)
        ret = (prem_net / e["entry_prem"] - 1) * 100
        last = (ts, prem_net)
        if ret <= config.STOP_PCT:
            legs.append((ts, prem_net, remaining, "stop")); return legs
        if not half_taken and ret >= config.TP_HALF_PCT:
            h = remaining // 2
            legs.append((ts, prem_net, h, "half")); remaining -= h; half_taken = True; continue
        if half_taken:
            day_bars = bars_all[bars_all.index.date == ts.date()]
            mom = momentum_at(day_bars, ts, cfg.mom_bars)
            if mom is not None and ((e["right"] == "C" and mom < 0) or (e["right"] == "P" and mom > 0)):
                legs.append((ts, prem_net, remaining, "flip")); return legs
    if last is not None and remaining > 0:
        legs.append((last[0], last[1], remaining, "time"))
    return legs


def trades(entries, exit_fn, cfg):
    out = []
    for e in entries:
        if (e["ticker"], e["right"]) not in ALLOWED:
            continue
        legs = exit_fn(e) if exit_fn is not sim_flip else exit_fn(e, cfg)
        if legs:
            out.append(BtTrade(e["ticker"], e["right"], e["strike"], e["now"],
                               e["entry_prem"], legs, e["expiry"]))
    return out


def report(name, entries, cfg):
    al = [e for e in entries if (e["ticker"], e["right"]) in ALLOWED]
    flip = metrics([t for t in trades(al, sim_flip, cfg)])
    give = metrics([t for t in trades(al, lambda e: simulate_giveback(
        e["bars_all"], e["entry_ts"], e["entry_prem"], e["strike"], e["right"],
        e["sigma"], e["expiry"], cfg, config.RUNNER_GIVEBACK_PCT), cfg)])
    if not flip or not give:
        return [f"{name}: too few trades"]
    d = give["total_pnl"] - flip["total_pnl"]
    winner = "GIVE-BACK" if d >= 0 else "FLIP"
    return [
        f"{name}  ({al and al[0]['now'].date()} -> {al and al[-1]['now'].date()}, {len(al)} trades)",
        f"  momentum-flip : {flip['win_rate']:>5.1f}% win  {flip['expectancy_pct']:>+6.1f}% exp  ${flip['total_pnl']:>9,.0f}",
        f"  give-back-{config.RUNNER_GIVEBACK_PCT:g}  : {give['win_rate']:>5.1f}% win  {give['expectancy_pct']:>+6.1f}% exp  ${give['total_pnl']:>9,.0f}",
        f"  -> winner: {winner} by ${abs(d):,.0f}  ({give['expectancy_pct']-flip['expectancy_pct']:+.1f}pp exp)",
        "",
    ]


def main():
    cfg = StrategyConfig()
    print("Loading data...")
    intraday, daily = load_data(cfg)
    entries = sorted(collect_entries(cfg, intraday, daily), key=lambda e: e["now"])
    al = [e for e in entries if (e["ticker"], e["right"]) in ALLOWED]
    n = len(al)
    print(f"{n} allow-list entries.\n")

    lines = ["WALK-FORWARD: give-back-40 vs momentum-flip (same -70 stop / +25 half)",
             "(approx pricing; the question is whether give-back wins OUT-OF-SAMPLE,",
             " i.e. in the LATER period, not just overall)", ""]
    lines += report("FULL window", al, cfg)
    half = n // 2
    lines += report("FIRST half (older)", al[:half], cfg)
    lines += report("SECOND half (newer / more out-of-sample)", al[half:], cfg)
    third = n // 3
    lines += report("LAST third (most out-of-sample)", al[2 * third:], cfg)

    out = "\n".join(lines)
    print(out)
    from pathlib import Path
    rp = Path(__file__).parent / "reports"
    rp.mkdir(exist_ok=True)
    (rp / "walkforward.txt").write_text(out, encoding="utf-8")
    print(f"Wrote {rp / 'walkforward.txt'}")


if __name__ == "__main__":
    main()
