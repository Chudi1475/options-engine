"""bt_exp_joint_exits.py — first JOINT sweep of TP_HALF x GIVEBACK at STOP=-90,
walk-forward.

The three exit knobs (half trigger, give-back, hard stop) were only ever swept
ONE at a time. bt_exp_stop_walkforward.py just moved the stop to -90 after -90
beat -70 on BOTH win rate and expectancy in BOTH walk-forward halves (overall
76.8% / +21.8%/tr vs 73.2% / +19.6%/tr). But the +25 half and 40-pt give-back
were tuned when the stop was -70; a deeper stop changes the loss tail, so the
other two knobs deserve a joint re-check around the new operating point.

Grid: TP_HALF in {15, 20, 25, 30} x GIVEBACK in {30, 40, 50}, all at STOP=-90.
Incumbent = (25, 40, -90), i.e. the config the live scanner runs today.

ADOPTION RULE (pre-committed before seeing any numbers): a challenger beats the
incumbent ONLY if it improves BOTH win rate AND expectancy in BOTH calendar
half-windows. Win rate is the owner's first priority: if several challengers
pass, pick the highest overall win rate, tie-break on overall expectancy.
Mixed results = keep 25/40. Never cherry-pick.

Reuses the working engine end to end: backtest.load_data (yfinance 60d 5m +
1y 1d), backtest_param_sweep.collect (same allow-list entries as the live
scanner: SPX:call, SPY:call, QCOM:call, TSLA:put) and backtest_param_sweep.sim
(give-back exit with explicit params), backtest BtTrade/metrics (2-contract
standard sizing, slippage + fees, $ drawdown). Split into two calendar halves
exactly like bt_exp_stop_walkforward.py.

Writes bt_exp_joint_exits.json next to this file (NEVER reports/).
Read-only against every existing file. Usage: python bt_exp_joint_exits.py
"""

import json
import sys
import time as time_mod
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
from backtest import BtTrade, load_data, metrics
from backtest_param_sweep import collect, sim
from strategy import StrategyConfig

HERE = Path(__file__).parent
OUT = HERE / "bt_exp_joint_exits.json"

STOP = -90                      # fixed: the newly adopted hard stop
HALF_GRID = (15, 20, 25, 30)    # sell-half trigger, % gain
GIVE_GRID = (30, 40, 50)        # runner give-back, points from peak
INCUMBENT = (25, 40)            # live config at STOP=-90
RISK = config.RISK_PER_TRADE_PCT  # live risk-based sizing: 1% of account per full stop-out


def load_with_retry(cfg):
    """load_data with ONE retry after 60s if yfinance throttles/errors/comes
    back empty (per the task spec)."""
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


def trades_for(entries, half, give):
    """Identical entries; exits half +{half} / give-back {give} / stop -90."""
    trades = []
    for e in entries:
        legs = sim(e, half, give, STOP)
        if legs:
            trades.append(BtTrade(e["ticker"], e["right"], e["strike"],
                                  e["now"], e["entry_prem"], legs, e["expiry"]))
    return trades


def enrich(m):
    """metrics() dict + sizing-fair account return per trade. Live sizing is
    risk-based (alloc = RISK / (|stop|/100)); the stop is FIXED at -90 here so
    this is a constant rescale that cannot change the ranking — kept anyway so
    the numbers line up with bt_exp_stop_walkforward.json."""
    if m is None:
        return None
    m = dict(m)
    m["acct_ret_pct_per_trade_sizing_fair"] = (
        m["expectancy_pct"] * RISK / abs(STOP))
    return m


def main():
    cfg = StrategyConfig()  # direction="both", same as the committed runs
    print("Loading fresh data (yfinance 60d 5m + 1y 1d)...")
    intraday, daily = load_with_retry(cfg)

    print("Collecting allow-list entries (same as live scanner)...")
    entries = collect(cfg, intraday, daily)
    print(f"{len(entries)} allow-list entries.")
    if not entries:
        raise RuntimeError("no allow-list entries in the fresh window")

    # --- split the window by CALENDAR SESSIONS (not trade count) ------------
    sessions = sorted({e["entry_ts"].date() for e in entries})
    mid = sessions[len(sessions) // 2]  # first half = session < mid
    first_sessions = [d for d in sessions if d < mid]
    second_sessions = [d for d in sessions if d >= mid]
    print(f"\nWindow: {sessions[0]} .. {sessions[-1]} "
          f"({len(sessions)} sessions with entries)")
    print(f"Split at {mid}: first half {len(first_sessions)} sessions, "
          f"second half {len(second_sessions)} sessions")

    # --- run every (half, give) cell over identical entries ------------------
    grid = [(h, g) for h in HALF_GRID for g in GIVE_GRID]
    results = {}
    for idx, (half, give) in enumerate(grid, 1):
        key = f"{half}/{give}"
        tag = " (INCUMBENT)" if (half, give) == INCUMBENT else ""
        print(f"\n[{idx}/{len(grid)}] Simulating half +{half} / give-back "
              f"{give} / stop {STOP}{tag} ...")
        trades = trades_for(entries, half, give)
        first = [t for t in trades if t.entry_time.date() < mid]
        second = [t for t in trades if t.entry_time.date() >= mid]
        res = {
            "overall": enrich(metrics(trades)),
            "first_half": enrich(metrics(first)),
            "second_half": enrich(metrics(second)),
        }
        results[key] = res
        for label in ("first_half", "second_half", "overall"):
            m = res[label]
            if m is None:
                print(f"  {label:<12} no trades")
                continue
            print(f"  {label:<12} {m['trades']:>3} trades  "
                  f"win {m['win_rate']:5.1f}%  exp {m['expectancy_pct']:+6.1f}%/tr  "
                  f"pnl ${m['total_pnl']:>8,.0f}  maxDD ${m['max_drawdown']:>8,.0f}")

    # --- walk-forward verdict -------------------------------------------------
    inc_key = f"{INCUMBENT[0]}/{INCUMBENT[1]}"
    base = results[inc_key]
    challengers = [(h, g) for (h, g) in grid if (h, g) != INCUMBENT]

    def beats_incumbent_both_halves(cand):
        """Strictly better win rate AND expectancy in BOTH halves."""
        for half_label in ("first_half", "second_half"):
            c, b = cand[half_label], base[half_label]
            if c is None or b is None:
                return False
            if not (c["win_rate"] > b["win_rate"]
                    and c["expectancy_pct"] > b["expectancy_pct"]):
                return False
        return True

    passers = [(h, g) for (h, g) in challengers
               if beats_incumbent_both_halves(results[f"{h}/{g}"])]
    if not passers:
        verdict = f"keep {INCUMBENT[0]}/{INCUMBENT[1]}"
        why = (f"no TP_HALF/GIVEBACK cell beat the incumbent "
               f"{INCUMBENT[0]}/{INCUMBENT[1]} on BOTH win rate and expectancy "
               f"in BOTH half-windows at stop {STOP}; mixed/partial wins do "
               f"not clear the walk-forward bar, so the live exits stand")
    else:
        # win rate is the owner's top priority: among passers pick the higher
        # overall win rate, expectancy as tie-break
        best = max(passers,
                   key=lambda hg: (results[f"{hg[0]}/{hg[1]}"]["overall"]["win_rate"],
                                   results[f"{hg[0]}/{hg[1]}"]["overall"]["expectancy_pct"]))
        verdict = f"adopt {best[0]}/{best[1]}"
        bm = results[f"{best[0]}/{best[1]}"]["overall"]
        lm = base["overall"]
        why = (f"half +{best[0]} / give-back {best[1]} beat the incumbent "
               f"{INCUMBENT[0]}/{INCUMBENT[1]} on both win rate and expectancy "
               f"in BOTH halves at stop {STOP} (overall {bm['win_rate']:.1f}% / "
               f"{bm['expectancy_pct']:+.1f}%/tr vs {lm['win_rate']:.1f}% / "
               f"{lm['expectancy_pct']:+.1f}%/tr)")

    caveats = [
        "~60 trading days of 5m data total, so each half is only ~6 weeks; "
        "small per-half samples make single trades move win rate several points",
        "12-cell joint grid on one small window raises the overfit risk above "
        "the 1-D sweeps; the both-halves bar helps but does not eliminate it",
        "same data window that selected stop -90 — the stop choice and this "
        "sweep are not independent tests",
        "approximated Black-Scholes 0DTE pricing (optimistic) — trust relative "
        "ranking between cells, not the dollar levels",
        "total_pnl/max_drawdown use the backtester's standard fixed 2 contracts; "
        "LIVE sizing is risk-based, but the stop is fixed at -90 for every cell "
        "so acct_ret_pct_per_trade_sizing_fair is a constant rescale of "
        "expectancy and cannot change the ranking here",
    ]

    out = {
        "experiment": "joint TP_HALF x GIVEBACK walk-forward at fixed stop -90 "
                      "(half in {15,20,25,30} x give-back in {30,40,50})",
        "generated": datetime.now().isoformat(timespec="seconds"),
        "verdict": verdict,
        "why": why,
        "rule": "a challenger beats the incumbent 25/40 only if it improves "
                "BOTH win rate AND expectancy in BOTH halves (same bar the "
                "stop change had to clear); win rate is the first priority — "
                "if several pass, highest win rate wins, expectancy breaks "
                "ties; anything mixed = keep 25/40",
        "stop_fixed": STOP,
        "incumbent": {"half": INCUMBENT[0], "giveback": INCUMBENT[1]},
        "grid": {"half": list(HALF_GRID), "giveback": list(GIVE_GRID)},
        "allow_list": ["SPX:call", "SPY:call", "QCOM:call", "TSLA:put"],
        "window": {"first_session": str(sessions[0]),
                   "last_session": str(sessions[-1]),
                   "split_at": str(mid),
                   "sessions_total": len(sessions),
                   "sessions_first_half": len(first_sessions),
                   "sessions_second_half": len(second_sessions)},
        "pricing": "approximated (Black-Scholes, realized vol) — optimistic; "
                   "trust relative moves",
        "sizing_note": "total_pnl/max_drawdown = fixed 2 contracts (backtester "
                       "standard); acct_ret_pct_per_trade_sizing_fair = "
                       "expectancy% x RISK_PER_TRADE_PCT / |stop| — constant "
                       "rescale here because the stop is fixed at -90",
        "results_by_config": results,
        "both_halves_pass": {f"{h}/{g}": beats_incumbent_both_halves(results[f"{h}/{g}"])
                             for (h, g) in challengers},
        "caveats": caveats,
    }
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT}")

    # --- console summary ------------------------------------------------------
    print("\n=== JOINT WALK-FORWARD SUMMARY (identical entries; stop -90; "
          "win% / exp%/tr) ===")
    hdr = (f"{'half/give':>9}  {'H1 win':>7} {'H1 exp':>8} {'H1 n':>5}  "
           f"{'H2 win':>7} {'H2 exp':>8} {'H2 n':>5}  "
           f"{'all win':>8} {'all exp':>8}  {'both-halves?':>12}")
    print(hdr)
    for (h, g) in grid:
        r = results[f"{h}/{g}"]
        h1, h2, al = r["first_half"], r["second_half"], r["overall"]
        flag = ("INCUMBENT" if (h, g) == INCUMBENT
                else ("PASS" if beats_incumbent_both_halves(r) else "fail"))
        print(f"{h:>4}/{g:<4}  {h1['win_rate']:>6.1f}% {h1['expectancy_pct']:>+7.1f}% "
              f"{h1['trades']:>5}  {h2['win_rate']:>6.1f}% "
              f"{h2['expectancy_pct']:>+7.1f}% {h2['trades']:>5}  "
              f"{al['win_rate']:>7.1f}% {al['expectancy_pct']:>+7.1f}%  {flag:>12}")

    print(f"\nVERDICT: {verdict}")
    print(f"WHY: {why}")


if __name__ == "__main__":
    main()
