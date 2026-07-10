"""DATA DRIFT CHECK — re-run the current live rules (give-back-40 exits, the
exact logic behind reports/backtest_new_rules.json) on the FRESHEST 60d 5m
window ending today, then compare per-setup win rate / expectancy to the
committed report the cloud eligibility gate reads.

Same code paths as the live backtest: load_data() (yfinance 60d 5m + 1y 1d),
strategy.detect_setup() entries, backtest_new_rules.run() with the give-back-40
exit engine, backtest.metrics(). Only difference from the committed run is the
calendar window (rolls forward as data ages) — that is exactly the drift we are
measuring.

Writes bt_exp_fresh.json in the repo root (NEVER reports/). Read-only against
reports/. No scanner, no Telegram.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
from backtest import load_data, metrics
from backtest_new_rules import run
from strategy import StrategyConfig

REPO = Path(__file__).parent
COMMITTED = REPO / "reports" / "backtest_new_rules.json"
OUT = REPO / "bt_exp_fresh.json"

FLOOR = 70.0  # portfolio win-rate floor
# setups the live gate treats as eligible (positive-expectancy in committed run)
ELIGIBLE = {"SPX:call", "SPY:call", "QCOM:call", "TSLA:put"}


def per_setup_metrics(trades):
    out = {}
    for ticker in {t.ticker for t in trades}:
        for right, dirname in (("C", "call"), ("P", "put")):
            mm = metrics([t for t in trades
                          if t.ticker == ticker and t.right == right])
            if mm:
                out[f"{ticker}:{dirname}"] = mm
    return out


def main():
    cfg = StrategyConfig()  # direction="both", identical to the committed run
    print("Loading FRESH data (yfinance 60d 5m)...")
    intraday, daily = load_data(cfg)

    # report the actual freshest bar per ticker so the window is auditable
    windows = {}
    for tk in cfg.watchlist:
        idx = intraday[tk].index
        windows[tk] = {"first_bar": str(idx.min()), "last_bar": str(idx.max()),
                       "sessions": len(set(idx.date))}

    print("Simulating give-back-40 exits on fresh entries...")
    trades = run(cfg, intraday, daily)
    fresh_per = per_setup_metrics(trades)
    fresh_overall = metrics(trades)

    committed = json.loads(COMMITTED.read_text(encoding="utf-8"))
    old_per = committed["per_setup"]
    old_overall = committed["overall"]

    # drift table on the union of setups
    drift = {}
    for key in sorted(set(fresh_per) | set(old_per)):
        o = old_per.get(key)
        n = fresh_per.get(key)
        row = {
            "old_win": o["win_rate"] if o else None,
            "new_win": n["win_rate"] if n else None,
            "old_exp": o["expectancy_pct"] if o else None,
            "new_exp": n["expectancy_pct"] if n else None,
            "old_trades": o["trades"] if o else None,
            "new_trades": n["trades"] if n else None,
            "old_pnl": o["total_pnl"] if o else None,
            "new_pnl": n["total_pnl"] if n else None,
        }
        if o and n:
            row["win_drift"] = n["win_rate"] - o["win_rate"]
            row["exp_drift"] = n["expectancy_pct"] - o["expectancy_pct"]
        row["eligible_in_committed"] = key in ELIGIBLE
        # flags: only meaningful for setups the gate actually trades
        if n:
            row["below_floor_now"] = n["win_rate"] < FLOOR
            row["negative_exp_now"] = n["expectancy_pct"] < 0
        drift[key] = row

    flags = []
    for key in sorted(ELIGIBLE):
        n = fresh_per.get(key)
        o = old_per.get(key)
        if n is None:
            flags.append(f"{key}: NO TRADES in fresh window (was "
                         f"{o['trades']} trades / {o['win_rate']:.1f}% / "
                         f"{o['expectancy_pct']:+.1f}%)")
            continue
        problems = []
        if n["win_rate"] < FLOOR:
            problems.append(f"win {n['win_rate']:.1f}% < {FLOOR:.0f}% floor")
        if n["expectancy_pct"] < 0:
            problems.append(f"expectancy {n['expectancy_pct']:+.1f}% NEGATIVE")
        if problems:
            flags.append(f"{key}: " + "; ".join(problems)
                         + f" (was {o['win_rate']:.1f}% / {o['expectancy_pct']:+.1f}%)")

    result = {
        "experiment": "data_drift_check",
        "generated": datetime.now().isoformat(timespec="seconds"),
        "pricing": "approximated (Black-Scholes, realized vol) — optimistic; trust relative moves",
        "rules": {"tp_half_pct": config.TP_HALF_PCT, "stop_pct": config.STOP_PCT,
                  "runner_giveback_pct": config.RUNNER_GIVEBACK_PCT,
                  "direction": cfg.direction},
        "floor_pct": FLOOR,
        "eligible_setups_in_committed": sorted(ELIGIBLE),
        "committed_window": {"start": old_overall["start"], "end": old_overall["end"]},
        "fresh_window_bars": windows,
        "fresh_overall": fresh_overall,
        "committed_overall": old_overall,
        "fresh_per_setup": fresh_per,
        "committed_per_setup": old_per,
        "drift": drift,
        "eligible_flags": flags,
    }
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}")

    print("\n=== FRESH WINDOW (per ticker, freshest 5m bars) ===")
    for tk, w in windows.items():
        print(f"  {tk}: {w['first_bar']} -> {w['last_bar']} ({w['sessions']} sessions)")
    print(f"\nCommitted window: {old_overall['start']} -> {old_overall['end']}")

    print("\n=== PER-SETUP DRIFT (committed -> fresh) ===")
    hdr = f"{'setup':12} {'old win':>8} {'new win':>8} {'dwin':>7}  {'old exp':>8} {'new exp':>8} {'dexp':>8}  {'oN':>4} {'nN':>4}"
    print(hdr)
    for key in sorted(drift):
        r = drift[key]
        ow = f"{r['old_win']:.1f}" if r['old_win'] is not None else "-"
        nw = f"{r['new_win']:.1f}" if r['new_win'] is not None else "-"
        dw = f"{r['win_drift']:+.1f}" if r.get('win_drift') is not None else "-"
        oe = f"{r['old_exp']:+.1f}" if r['old_exp'] is not None else "-"
        ne = f"{r['new_exp']:+.1f}" if r['new_exp'] is not None else "-"
        de = f"{r['exp_drift']:+.1f}" if r.get('exp_drift') is not None else "-"
        oN = r['old_trades'] if r['old_trades'] is not None else "-"
        nN = r['new_trades'] if r['new_trades'] is not None else "-"
        star = " *ELIG" if r["eligible_in_committed"] else ""
        print(f"{key:12} {ow:>8} {nw:>8} {dw:>7}  {oe:>8} {ne:>8} {de:>8}  {str(oN):>4} {str(nN):>4}{star}")

    print("\n=== ELIGIBLE-SETUP FLAGS (70% floor / negative expectancy) ===")
    if flags:
        for f in flags:
            print("  FLAG " + f)
    else:
        print("  none — all eligible setups still clear the floor with positive expectancy")

    if fresh_overall:
        print(f"\nFresh overall: {fresh_overall['trades']} trades, "
              f"{fresh_overall['win_rate']:.1f}% win, "
              f"{fresh_overall['expectancy_pct']:+.1f}%/trade  "
              f"(committed {old_overall['trades']}/"
              f"{old_overall['win_rate']:.1f}%/"
              f"{old_overall['expectancy_pct']:+.1f}%)")


if __name__ == "__main__":
    main()
