"""Exit-policy bake-off: does letting the runner run beat the current
momentum-flip trail?

Same entries as the live scanner (StrategyConfig, direction="both"); ONLY the
exit on the second half changes. Scored on the population the bot ACTUALLY
trades (the allow-list: SPX:call, SPY:call, QCOM:call, TSLA:put) so the
comparison reflects real alerts, not setups the gate would never send.

Policies compared (all keep: sell HALF at +25%, hard stop -70%, time stop):
- CURRENT       — trail the runner until the 15-min momentum flips (live rule)
- GIVEBACK-G    — trail the runner by giving back G points off its peak
- FULL-GB-G     — no half; trail the WHOLE position by give-back G (max upside)

Same honesty rules as the other backtests: approximated Black-Scholes pricing
(optimistic for 0DTE), 1.5% slippage each way + fees, no lookahead. Absolute
dollars are optimistic; what matters here is the RELATIVE ranking of exits on
identical entries.

Usage:
    python backtest_runner_trail.py
"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path

import pandas as pd

import config
from backtest import (CONTRACTS, SLIPPAGE, BtTrade, bs_price, expiry_for,
                      load_data, metrics, realized_vol, years_to_expiry)
from backtest_new_rules import momentum_at, simulate_new_exits
from strategy import StrategyConfig, detect_setup

REPORTS_DIR = Path(__file__).parent / "reports"

# what the live scanner actually alerts on (scanner.ALLOWED_SETUPS)
ALLOWED = {("SPX", "C"), ("SPY", "C"), ("QCOM", "C"), ("TSLA", "P")}
ARM_PCT = config.TP_HALF_PCT   # the give-back trail only arms once the trade is
                               # clearly winning (peak >= +25%); below that the
                               # -70% hard stop governs, so we don't tighten the
                               # stop, we only protect a real gain.


def simulate_giveback(bars_all, entry_ts, entry_prem, strike, right,
                      sigma, expiry, cfg, giveback, take_half=True):
    """Half at +25% (optional), then trail the remaining position by giving back
    `giveback` points off its peak return. Keeps the -70% hard stop + time stop.
    Walks bar by bar with no lookahead."""
    future = bars_all[(bars_all.index > entry_ts)
                      & (bars_all.index <= pd.Timestamp(expiry))]
    legs, remaining, half_taken, last, peak = [], CONTRACTS, False, None, None
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
        if take_half and not half_taken and ret >= config.TP_HALF_PCT:
            half = remaining // 2
            legs.append((ts, prem_net, half, f"half +{config.TP_HALF_PCT:g}%"))
            remaining -= half
            half_taken = True
            continue  # start trailing the runner on the NEXT bar
        armed = (half_taken or not take_half) and peak >= ARM_PCT
        if armed and ret <= peak - giveback:
            legs.append((ts, prem_net, remaining,
                         f"give-back {giveback:g} (peak {peak:.0f}%)"))
            return legs
    if last is not None and remaining > 0:
        legs.append((last[0], last[1], remaining, "time stop"))
    return legs


def collect_entries(cfg, intraday, daily):
    """Every entry the live scanner would take, with the data needed to replay
    any exit policy. Generated ONCE so every policy sees identical entries."""
    entries = []
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
                entries.append({
                    "ticker": ticker, "right": right, "strike": setup.strike,
                    "now": now, "entry_ts": upto.index[-1], "entry_prem": entry_prem,
                    "sigma": sigma, "expiry": expiry, "bars_all": bars_all,
                })
    return entries


def trades_for(entries, exit_fn, cfg, allowed_only=True):
    out = []
    for e in entries:
        if allowed_only and (e["ticker"], e["right"]) not in ALLOWED:
            continue
        legs = exit_fn(e)
        if not legs:
            continue
        out.append(BtTrade(e["ticker"], e["right"], e["strike"], e["now"],
                           e["entry_prem"], legs, e["expiry"]))
    return out


def main():
    cfg = StrategyConfig()  # direction="both" — same entries as the live scanner
    print("Loading data (60d 5m + 1y daily per ticker)...")
    intraday, daily = load_data(cfg)
    print("Collecting entries (identical across all policies)...")
    entries = collect_entries(cfg, intraday, daily)
    allowed_n = sum(1 for e in entries if (e["ticker"], e["right"]) in ALLOWED)
    print(f"{len(entries)} raw entries, {allowed_n} on the live allow-list.\n")

    def cur(e):
        return simulate_new_exits(e["bars_all"], e["entry_ts"], e["entry_prem"],
                                  e["strike"], e["right"], e["sigma"], e["expiry"], cfg)

    def gb(g, take_half=True):
        return lambda e: simulate_giveback(
            e["bars_all"], e["entry_ts"], e["entry_prem"], e["strike"],
            e["right"], e["sigma"], e["expiry"], cfg, g, take_half=take_half)

    policies = [("CURRENT (momentum-flip trail)", cur)]
    for g in (20, 30, 40, 50, 60):
        policies.append((f"GIVEBACK-{g} (half +25, then trail {g} off peak)", gb(g)))
    for g in (30, 50):
        policies.append((f"FULL-GB-{g} (no half, trail {g} off peak)",
                         gb(g, take_half=False)))

    results = []
    for name, fn in policies:
        m = metrics(trades_for(entries, fn, cfg))
        results.append((name, m))

    def row(name, m):
        if not m:
            return f"{name:<46} no trades"
        return (f"{name:<46} {m['trades']:>3}  {m['win_rate']:>5.1f}%  "
                f"{m['avg_win_pct']:>+7.1f}/{m['avg_loss_pct']:>+6.1f}  "
                f"{m['expectancy_pct']:>+6.1f}%  ${m['total_pnl']:>9,.0f}  "
                f"${m['max_drawdown']:>9,.0f}")

    header = (f"{'POLICY (allow-list population)':<46} {'trd':>3}  {'win':>6}  "
              f"{'avgW/avgL':>14}  {'exp/tr':>7}  {'totalP&L':>10}  {'maxDD':>10}")
    lines = ["EXIT-POLICY BAKE-OFF — same entries, allow-list setups only",
             "(approx pricing: dollars optimistic; trust the RELATIVE ranking)",
             "", header, "-" * len(header)]
    for name, m in results:
        lines.append(row(name, m))

    base = results[0][1]
    lines += ["", "vs CURRENT (Δ total P&L / Δ expectancy / Δ win-rate):"]
    for name, m in results[1:]:
        if m and base:
            lines.append(
                f"  {name:<44} ${m['total_pnl'] - base['total_pnl']:>+9,.0f}  "
                f"{m['expectancy_pct'] - base['expectancy_pct']:>+5.1f}%  "
                f"{m['win_rate'] - base['win_rate']:>+5.1f}pp")

    # per-setup, current vs the best give-back, so we see WHERE any edge comes from
    out = "\n".join(lines)
    print(out)
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / "runner_trail_compare.txt").write_text(out, encoding="utf-8")
    print(f"\nWrote {REPORTS_DIR / 'runner_trail_compare.txt'}")


if __name__ == "__main__":
    main()
