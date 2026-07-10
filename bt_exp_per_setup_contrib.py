"""bt_exp: PER-SETUP CONTRIBUTION over the freshest 60d of 5m data.

Recomputes every allow-list setup with the live give-back-40 exit engine
(backtest_new_rules.run — TP_HALF +25 / give-back 40 / stop -70), then
compares portfolio totals:

  A. full allow-list  {SPX:call, SPY:call, QCOM:call, TSLA:put}
  B. without TSLA:put
  C. TSLA:put at HALF size (0.5x dollar weight; win rate unchanged)

Also splits TSLA:put into first-half vs second-half of the window and the
fresh tail (entries after 2026-06-18, i.e. bars the last saved report never
saw) to see whether it drifted down or recovered.

Experiment file only. Writes bt_exp_per_setup_contrib.json in the repo root
(NEVER reports/). Approximate Black-Scholes pricing — trust RELATIVE ranks.
"""

import json
import sys
import time as _time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

import config
from backtest import load_data, metrics
from backtest_new_rules import run
from strategy import StrategyConfig

OUT = Path(__file__).parent / "bt_exp_per_setup_contrib.json"
ALLOWED = ("SPX:call", "SPY:call", "QCOM:call", "TSLA:put")
STOP_ABS = abs(config.STOP_PCT)  # 70 — same stop everywhere, sizing-fair divisor


def load_with_retry(cfg, tries=4):
    last = None
    for k in range(tries):
        try:
            return load_data(cfg)
        except Exception as e:  # yfinance throttle / transient net error
            last = e
            wait = 5 * (k + 1)
            print(f"load_data failed ({e}); retrying in {wait}s...")
            _time.sleep(wait)
    raise last


def skey(t):
    return f"{t.ticker}:{'call' if t.right == 'C' else 'put'}"


def portfolio(trades, weights=None):
    """Portfolio stats with optional per-trade dollar weights (sizing).
    Win rate is count-based (a half-size win is still a win)."""
    if not trades:
        return None
    if weights is None:
        weights = {id(t): 1.0 for t in trades}
    seq = sorted(trades, key=lambda t: t.entry_time)
    wins = sum(1 for t in seq if t.pnl > 0)
    cum = peak = max_dd = 0.0
    total = 0.0
    acct_units = 0.0  # sizing-fair: ret% / |stop%| * weight, summed
    for t in seq:
        w = weights[id(t)]
        total += t.pnl * w
        acct_units += (t.ret_pct / STOP_ABS) * w
        cum += t.pnl * w
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    n = len(seq)
    return {
        "trades": n,
        "win_rate": wins / n * 100,
        "expectancy_pct": sum(t.ret_pct * weights[id(t)] for t in seq) / n,
        "total_pnl": total,
        "max_drawdown": -max_dd,
        "acct_return_units": acct_units,  # sum of R multiples vs the -70 stop
        "start": seq[0].entry_time.strftime("%m/%d/%Y"),
        "end": seq[-1].entry_time.strftime("%m/%d/%Y"),
    }


def main():
    cfg = StrategyConfig()  # direction="both" — the live scanner's entries
    print("Loading freshest 60d of 5m data (yfinance)...")
    intraday, daily = load_with_retry(cfg)
    for tk, df in intraday.items():
        print(f"  {tk}: {len(df)} 5m bars, {df.index.min()} .. {df.index.max()}")

    print("Simulating give-back-40 exits on all setups...")
    trades = run(cfg, intraday, daily)

    # per-setup fresh stats (all 8 for context, allow-list is what matters)
    per_setup = {}
    for tk in {t.ticker for t in trades}:
        for right, d in (("C", "call"), ("P", "put")):
            mm = metrics([t for t in trades if t.ticker == tk and t.right == right])
            if mm:
                per_setup[f"{tk}:{d}"] = mm

    allowed_trades = [t for t in trades if skey(t) in ALLOWED]
    tsla_puts = [t for t in allowed_trades if skey(t) == "TSLA:put"]
    no_tsla = [t for t in allowed_trades if skey(t) != "TSLA:put"]

    # scenario weights
    w_full = {id(t): 1.0 for t in allowed_trades}
    w_half = {id(t): (0.5 if skey(t) == "TSLA:put" else 1.0) for t in allowed_trades}

    scen = {
        "A_full_allowlist": portfolio(allowed_trades, w_full),
        "B_without_TSLA_put": portfolio(no_tsla),
        "C_half_size_TSLA_put": portfolio(allowed_trades, w_half),
    }

    # TSLA:put drift: first half vs second half of the fresh window, plus the
    # tail the saved report never saw (entries after 2026-06-18).
    drift = {}
    if tsla_puts:
        seq = sorted(tsla_puts, key=lambda t: t.entry_time)
        t0, t1 = seq[0].entry_time, seq[-1].entry_time
        mid = t0 + (t1 - t0) / 2
        drift["first_half"] = metrics([t for t in seq if t.entry_time <= mid])
        drift["second_half"] = metrics([t for t in seq if t.entry_time > mid])
        cutoff = pd.Timestamp("2026-06-18 23:59", tz="America/New_York")
        drift["fresh_tail_after_06_18"] = metrics(
            [t for t in seq if pd.Timestamp(t.entry_time) > cutoff])

    out = {
        "pricing": "approximated (Black-Scholes, realized vol) — relative only",
        "exit_rules": {"tp_half": config.TP_HALF_PCT, "stop": config.STOP_PCT,
                       "giveback": config.RUNNER_GIVEBACK_PCT},
        "window": {tk: [str(df.index.min()), str(df.index.max())]
                   for tk, df in intraday.items()},
        "per_setup_fresh": per_setup,
        "portfolio_scenarios": scen,
        "tsla_put_drift": drift,
    }
    OUT.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {OUT}\n")

    print("== per-setup fresh (allow-list first) ==")
    order = list(ALLOWED) + sorted(set(per_setup) - set(ALLOWED))
    for k in order:
        m = per_setup.get(k)
        star = "*" if k in ALLOWED else " "
        if m:
            print(f" {star}{k:10s} {m['win_rate']:5.1f}% win  "
                  f"{m['expectancy_pct']:+6.1f}%/trade  {m['trades']:3d} trades  "
                  f"${m['total_pnl']:+9,.0f}  ({m['start']}..{m['end']})")
    print("\n== portfolio scenarios (allow-list entries, identical everywhere) ==")
    for name, m in scen.items():
        if m:
            print(f" {name:22s} {m['trades']:3d} trades  {m['win_rate']:5.1f}% win  "
                  f"{m['expectancy_pct']:+6.1f}%/tr  ${m['total_pnl']:+9,.0f}  "
                  f"maxDD ${m['max_drawdown']:+9,.0f}  "
                  f"acctR {m['acct_return_units']:+7.2f}")
    print("\n== TSLA:put drift ==")
    for name, m in drift.items():
        if m:
            print(f" {name:24s} {m['win_rate']:5.1f}% win  "
                  f"{m['expectancy_pct']:+6.1f}%/tr  {m['trades']:3d} trades  "
                  f"${m['total_pnl']:+8,.0f}  ({m['start']}..{m['end']})")
        else:
            print(f" {name:24s} no trades")


if __name__ == "__main__":
    main()
