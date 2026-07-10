"""bt_exp_stop_walkforward.py — WALK-FORWARD validation of the hard stop level.

The full-window sweep (reports/param_sweep.txt) said 25/40/-90 beats the live
25/40/-70 on BOTH win rate and expectancy, but a single-window grid win can be
one lucky regime. This experiment re-runs the exact same allow-list entries
(SPX:call, SPY:call, QCOM:call, TSLA:put) over the fresh 60d 5m window with
exits half +25 / give-back 40 fixed, and ONLY the stop varying over
{-70, -80, -90}, then splits the window into first-half / second-half calendar
sessions.

Verdict rule (same both-halves bar the gap-up rule had to clear): a candidate
stop BEATS the live -70 only if it improves BOTH win rate AND expectancy in
BOTH halves. Mixed results = keep -70. Never cherry-pick.

Reuses the working engine end to end: backtest.load_data (yfinance 60d 5m +
1y 1d), backtest_param_sweep.collect (same entries as the live scanner) and
backtest_param_sweep.sim (give-back exit with explicit params), backtest
BtTrade/metrics (2-contract standard sizing, slippage + fees, $ drawdown).

Writes bt_exp_stop_walkforward.json next to this file (NEVER reports/).
Read-only against every existing file. Usage: python bt_exp_stop_walkforward.py
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
OUT = HERE / "bt_exp_stop_walkforward.json"

HALF_TRIG = 25          # fixed: sell half at +25%
GIVEBACK = 40           # fixed: runner give-back 40 pts from peak
LIVE_STOP = -70
CANDIDATES = (-80, -90)
ALL_STOPS = (LIVE_STOP,) + CANDIDATES
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


def trades_for_stop(entries, stop):
    """Identical entries, exits half +25 / give-back 40 / the given stop."""
    trades = []
    for e in entries:
        legs = sim(e, HALF_TRIG, GIVEBACK, stop)
        if legs:
            trades.append(BtTrade(e["ticker"], e["right"], e["strike"],
                                  e["now"], e["entry_prem"], legs, e["expiry"]))
    return trades


def enrich(m, stop):
    """metrics() dict + sizing-fair account return per trade. Live sizing is
    risk-based (alloc = RISK / (|stop|/100)), so a wider stop trades SMALLER;
    fixed-2-contract dollars alone would flatter wide stops."""
    if m is None:
        return None
    m = dict(m)
    m["acct_ret_pct_per_trade_sizing_fair"] = (
        m["expectancy_pct"] * RISK / abs(stop))
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

    # --- run every stop over identical entries -------------------------------
    results = {}
    for stop in ALL_STOPS:
        print(f"\nSimulating half +{HALF_TRIG} / give-back {GIVEBACK} / "
              f"stop {stop} ...")
        trades = trades_for_stop(entries, stop)
        first = [t for t in trades if t.entry_time.date() < mid]
        second = [t for t in trades if t.entry_time.date() >= mid]
        res = {
            "overall": enrich(metrics(trades), stop),
            "first_half": enrich(metrics(first), stop),
            "second_half": enrich(metrics(second), stop),
        }
        results[str(stop)] = res
        for label in ("first_half", "second_half", "overall"):
            m = res[label]
            if m is None:
                print(f"  {label:<12} no trades")
                continue
            print(f"  {label:<12} {m['trades']:>3} trades  "
                  f"win {m['win_rate']:5.1f}%  exp {m['expectancy_pct']:+6.1f}%/tr  "
                  f"pnl ${m['total_pnl']:>8,.0f}  maxDD ${m['max_drawdown']:>8,.0f}")

    # --- walk-forward verdict -------------------------------------------------
    base = results[str(LIVE_STOP)]

    def beats_live_both_halves(cand):
        """Strictly better win rate AND expectancy in BOTH halves."""
        for half in ("first_half", "second_half"):
            c, b = cand[half], base[half]
            if c is None or b is None:
                return False
            if not (c["win_rate"] > b["win_rate"]
                    and c["expectancy_pct"] > b["expectancy_pct"]):
                return False
        return True

    passers = [s for s in CANDIDATES if beats_live_both_halves(results[str(s)])]
    if not passers:
        verdict = f"keep {LIVE_STOP}"
        why = (f"no candidate stop beat {LIVE_STOP} on BOTH win rate and "
               f"expectancy in BOTH half-windows; mixed/partial wins do not "
               f"clear the walk-forward bar, so the live stop stands")
    else:
        # win rate is the owner's top priority: among passers pick the higher
        # overall win rate, expectancy as tie-break
        best = max(passers, key=lambda s: (results[str(s)]["overall"]["win_rate"],
                                           results[str(s)]["overall"]["expectancy_pct"]))
        verdict = f"adopt {best}"
        bm, lm = results[str(best)]["overall"], base["overall"]
        why = (f"stop {best} beat {LIVE_STOP} on both win rate and expectancy "
               f"in BOTH halves (overall {bm['win_rate']:.1f}% / "
               f"{bm['expectancy_pct']:+.1f}%/tr vs {lm['win_rate']:.1f}% / "
               f"{lm['expectancy_pct']:+.1f}%/tr)")

    caveats = [
        "~60 trading days of 5m data total, so each half is only ~6 weeks; "
        "small per-half samples make single trades move win rate several points",
        "approximated Black-Scholes 0DTE pricing (optimistic) — trust relative "
        "ranking between stops, not the dollar levels",
        "total_pnl/max_drawdown use the backtester's standard fixed 2 contracts; "
        "LIVE sizing is risk-based, so a wider stop trades a smaller position — "
        "see acct_ret_pct_per_trade_sizing_fair for the sizing-fair comparison",
        "a deeper stop means deeper per-trade drawdown before exit even when "
        "the summary stats improve",
    ]

    out = {
        "experiment": "stop-level walk-forward (half +25 / give-back 40 fixed; "
                      "stop in {-70,-80,-90})",
        "generated": datetime.now().isoformat(timespec="seconds"),
        "verdict": verdict,
        "why": why,
        "rule": "a candidate beats the live -70 only if it improves BOTH win "
                "rate AND expectancy in BOTH halves (same bar as the gap-up "
                "rule); anything mixed = keep -70",
        "exit_params_fixed": {"half": HALF_TRIG, "giveback": GIVEBACK},
        "live_stop": LIVE_STOP,
        "candidates": list(CANDIDATES),
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
                       "expectancy% x RISK_PER_TRADE_PCT / |stop| (live "
                       "risk-based sizing)",
        "results_by_stop": results,
        "both_halves_pass": {str(s): beats_live_both_halves(results[str(s)])
                             for s in CANDIDATES},
        "caveats": caveats,
    }
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT}")

    # --- console summary ------------------------------------------------------
    print("\n=== WALK-FORWARD SUMMARY (identical entries; win% / exp%/tr) ===")
    hdr = (f"{'stop':>5}  {'H1 win':>7} {'H1 exp':>8} {'H1 n':>5}  "
           f"{'H2 win':>7} {'H2 exp':>8} {'H2 n':>5}  "
           f"{'all win':>8} {'all exp':>8}  {'both-halves?':>12}")
    print(hdr)
    for s in ALL_STOPS:
        r = results[str(s)]
        h1, h2, al = r["first_half"], r["second_half"], r["overall"]
        flag = ("BASELINE" if s == LIVE_STOP
                else ("PASS" if beats_live_both_halves(r) else "fail"))
        print(f"{s:>5}  {h1['win_rate']:>6.1f}% {h1['expectancy_pct']:>+7.1f}% "
              f"{h1['trades']:>5}  {h2['win_rate']:>6.1f}% "
              f"{h2['expectancy_pct']:>+7.1f}% {h2['trades']:>5}  "
              f"{al['win_rate']:>7.1f}% {al['expectancy_pct']:>+7.1f}%  {flag:>12}")

    print(f"\nVERDICT: {verdict}")
    print(f"WHY: {why}")


if __name__ == "__main__":
    main()
