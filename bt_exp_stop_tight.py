"""bt_exp_stop_tight.py — MEASUREMENT of tight hard-stop levels.

This is a MEASUREMENT, not a selection. It exists because the earlier
walk-forward study (bt_exp_stop_walkforward.json) only ever compared
{-70, -80, -90} and adopted -90 on win rate + per-contract expectancy. It
never looked at the range real option buyers actually use, and on its OWN
sizing-fair metric the ranking was already inverted:

    stop   win%   exp%/tr   sizing-fair account return per trade
    -70    73.2   +19.6     0.2796   <- best account return
    -80    74.5   +21.0     0.2626
    -90    76.8   +21.8     0.2420   <- adopted, worst account return

Why those disagree: live sizing is RISK-BASED. Position = risk% / |stop|, so a
wider stop buys a SMALLER position. Per-contract expectancy flatters wide
stops; account return is what the owner actually earns.

This run reports the WHOLE curve from tight to wide on every metric, plus the
one thing a stop change can silently break: which setups still clear the live
alert gate (MIN_WINRATE and positive expectancy). A stop that lifts account
return but drops every setup under the win-rate floor turns the bot silent.

No decision rule is applied here and nothing is adopted. Output is a table for
a human to choose from.

    python bt_exp_stop_tight.py

Writes bt_exp_stop_tight.json next to this file (NEVER reports/).
Read-only against every existing file.
"""

import json
import sys
import time as time_mod
from collections import defaultdict
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
from backtest import BtTrade, load_data, metrics
from backtest_param_sweep import collect, sim
from strategy import StrategyConfig

HERE = Path(__file__).parent
OUT = HERE / "bt_exp_stop_tight.json"

HALF_TRIG = 25    # fixed, unchanged from live
GIVEBACK = 40     # fixed, unchanged from live
LIVE_STOP = int(config.STOP_PCT)
RISK = config.RISK_PER_TRADE_PCT
FLOOR = config.MIN_WINRATE

# the range a long-premium day trader would actually consider, plus the three
# the old study covered so the curve joins up with the published numbers
STOPS = (-20, -25, -30, -35, -40, -50, -60, -70, -80, -90)


def load_with_retry(cfg):
    for attempt in (1, 2):
        try:
            intraday, daily = load_data(cfg)
            empty = [tk for tk in cfg.watchlist
                     if intraday[tk].empty or daily[tk].empty]
            if not empty:
                return intraday, daily
            print(f"  attempt {attempt}: empty data for {empty}")
        except Exception as e:  # noqa: BLE001
            print(f"  attempt {attempt}: load_data failed: {e}")
        if attempt == 1:
            print("  yfinance likely rate-limited — retrying once in 60s...")
            time_mod.sleep(60)
    raise RuntimeError("could not load yfinance data after one 60s retry")


def trades_for_stop(entries, stop):
    trades, stopped = [], 0
    for e in entries:
        legs = sim(e, HALF_TRIG, GIVEBACK, stop)
        if not legs:
            continue
        if any(lab == "stop" for _, _, _, lab in legs):
            stopped += 1
        trades.append(BtTrade(e["ticker"], e["right"], e["strike"],
                              e["now"], e["entry_prem"], legs, e["expiry"]))
    return trades, stopped


def enrich(m, stop):
    """metrics() + the sizing-fair account return the owner actually earns."""
    if m is None:
        return None
    m = dict(m)
    m["acct_ret_pct_per_trade_sizing_fair"] = (
        m["expectancy_pct"] * RISK / abs(stop))
    m["position_pct_of_account"] = RISK / (abs(stop) / 100.0)
    return m


def per_setup(trades, stop):
    """Win rate + expectancy per TICKER:right, and whether it clears the live
    alert gate. This is the eligibility diff a stop change must not hide."""
    groups = defaultdict(list)
    for t in trades:
        groups[f"{t.ticker}:{t.right}"].append(t)
    out = {}
    for key, ts in sorted(groups.items()):
        m = enrich(metrics(ts), stop)
        if m is None:
            continue
        m["would_alert"] = bool(round(m["win_rate"]) >= FLOOR
                                and m["expectancy_pct"] > 0)
        out[key] = m
    return out


def main():
    cfg = StrategyConfig()
    print("Loading fresh data (yfinance 60d 5m + 1y 1d)...")
    intraday, daily = load_with_retry(cfg)

    print("Collecting allow-list entries (same as live scanner)...")
    entries = collect(cfg, intraday, daily)
    print(f"{len(entries)} allow-list entries.")
    if not entries:
        raise RuntimeError("no allow-list entries in the fresh window")

    sessions = sorted({e["entry_ts"].date() for e in entries})
    mid = sessions[len(sessions) // 2]
    print(f"\nWindow: {sessions[0]} .. {sessions[-1]} ({len(sessions)} sessions)")
    print(f"Walk-forward split at {mid}")

    results = {}
    for stop in STOPS:
        trades, stopped = trades_for_stop(entries, stop)
        first = [t for t in trades if t.entry_time.date() < mid]
        second = [t for t in trades if t.entry_time.date() >= mid]
        results[str(stop)] = {
            "overall": enrich(metrics(trades), stop),
            "first_half": enrich(metrics(first), stop),
            "second_half": enrich(metrics(second), stop),
            "per_setup": per_setup(trades, stop),
            "stopped_out": stopped,
            "stop_out_rate_pct": round(100.0 * stopped / len(trades), 1) if trades else None,
        }

    live_key = str(LIVE_STOP) if str(LIVE_STOP) in results else "-90"

    out = {
        "experiment": "tight-stop MEASUREMENT (half +25 / give-back 40 fixed; "
                      f"stop swept over {list(STOPS)})",
        "kind": "measurement — no selection rule applied, nothing adopted",
        "generated": datetime.now().isoformat(timespec="seconds"),
        "live_stop": LIVE_STOP,
        "exit_params_fixed": {"half": HALF_TRIG, "giveback": GIVEBACK},
        "alert_gate": {"min_winrate": FLOOR, "requires": "positive expectancy"},
        "allow_list": ["SPX:call", "SPY:call", "QCOM:call", "TSLA:put"],
        "window": {"first_session": str(sessions[0]),
                   "last_session": str(sessions[-1]),
                   "split_at": str(mid),
                   "sessions_total": len(sessions)},
        "results_by_stop": results,
        "caveats": [
            "SIMULATION FLATTERS TIGHT STOPS: backtest_param_sweep.sim checks the "
            "stop only at 5-MINUTE BAR CLOSES. A real -25/-30% stop would be hit "
            "intrabar far more often than this shows, so tight-stop win rates "
            "here are an UPPER BOUND and the true numbers are worse.",
            "approximated Black-Scholes 0DTE pricing (optimistic) — trust the "
            "relative shape of the curve, not the dollar levels",
            "~60 trading days total, each half ~6 weeks; a few trades move win "
            "rate several points at these sample sizes",
            "this is a 10-point sweep on one window: the best cell is an upper "
            "bound on true edge, and the curve's SHAPE is more trustworthy than "
            "any single winner",
            "a tighter stop means a BIGGER position (risk-based sizing), so the "
            "same % expectancy earns more account return but full-risk losses "
            "arrive more often — check stop_out_rate_pct, not just win rate",
        ],
    }
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")

    # ---------------------------- console table ------------------------------
    print("\n=== FULL CURVE (identical entries; half +25 / give-back 40) ===")
    print(f"{'stop':>5} {'n':>4} {'win%':>6} {'exp%/tr':>8} {'stopout%':>9} "
          f"{'pos%acct':>9} {'ACCT RET/tr':>12} {'H1 win':>7} {'H2 win':>7}")
    for s in STOPS:
        r = results[str(s)]
        o, h1, h2 = r["overall"], r["first_half"], r["second_half"]
        mark = "  <- LIVE" if s == LIVE_STOP else ""
        print(f"{s:>5} {o['trades']:>4} {o['win_rate']:>6.1f} "
              f"{o['expectancy_pct']:>+8.1f} {r['stop_out_rate_pct']:>9.1f} "
              f"{o['position_pct_of_account']:>8.2f}% "
              f"{o['acct_ret_pct_per_trade_sizing_fair']:>12.4f} "
              f"{h1['win_rate']:>7.1f} {h2['win_rate']:>7.1f}{mark}")

    print(f"\n=== ALERT-GATE ELIGIBILITY (needs win rate >= {FLOOR:g} AND +EV) ===")
    setups = sorted({k for r in results.values() for k in r["per_setup"]})
    print(f"{'stop':>5}  " + "  ".join(f"{k:>10}" for k in setups) + "   ALERTS")
    for s in STOPS:
        ps = results[str(s)]["per_setup"]
        cells, live = [], []
        for k in setups:
            m = ps.get(k)
            if m is None:
                cells.append(f"{'--':>10}")
                continue
            cells.append(f"{m['win_rate']:>9.1f}{'*' if m['would_alert'] else ' '}")
            if m["would_alert"]:
                live.append(k)
        mark = "  <- LIVE" if s == LIVE_STOP else ""
        print(f"{s:>5}  " + "  ".join(cells)
              + f"   {len(live)}/{len(setups)}{mark}")
    print("\n* = would still clear the alert gate at that stop. A row with 0 "
          "alerting setups means the bot goes SILENT at that stop.")
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
