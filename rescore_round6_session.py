"""MEASUREMENT, not a selection: replay the round-6 procedural winner with the
live session window INSIDE the signal filter.

Why this exists
---------------
The live sniper fired SPY and TSLA tickets at 07:03 and 07:27 ET on 8/21 from
pre-market bars, then graded a 0.26-point SPY target "hit" at 08:28 ET on a
15-minute-delayed pre-market print. The round-6 backtest it quotes never saw a
single stock or index trade before 10:00 ET: its frozen stock data was regular
session only (09:30-16:00 ET) and SKIP_FIRST_BARS=4 pushed the first signal bar
to 09:50. So "entries only 7:00 ET or later" was a forex-only fact dressed as a
universal rule, and every pre-market stock ticket was NOT the verified pattern.

The owner's instruction (8/22): no alerts at 6 or 7 AM CT. The live window is
therefore 09:50 ET (8:50 AM CT) to the 16:00 ET close for every sniper symbol:
the first bar the backtest could have signalled on for stocks, applied to forex
too so there is one window everyone can remember.

Why a replay and not a filter
-----------------------------
A first version of this script just dropped the pre-09:50 rows from the
published trade list. That is wrong: the replay takes ONE trade per symbol per
day, so a pre-09:50 forex signal that was taken used up that day's slot and
suppressed every later signal on that symbol. Deleting the row afterwards
cannot bring the suppressed later trade back. The only honest number is a
fresh replay with the window applied at the signal, which is what this does:
same frozen stream, same simulator, same procedural-winner config, same
IS/OOS split, one extra condition in passes().

What it is not
--------------
Not a selection. The window was fixed by the instruction above and by the
backtest's own stock data before any number below was computed; nothing here
is ranked or chosen. Looks ledger: one more fixed-config look at the round-6
OOS days (26 through round 6 -> 27).

    python rescore_round6_session.py        # writes the report, prints the diff
"""

import json
import sys
import time as time_mod
from datetime import datetime, time
from pathlib import Path

REPO = Path(__file__).parent
SOURCE = REPO / "reports" / "chart_backtest_round6.json"
OUT = REPO / "reports" / "chart_backtest_round6_session.json"

# the live window, ET. Mirrors fvg._SNIPER_OPEN_ET / the 16:00 close; the
# test suite pins them equal.
SESSION_OPEN_ET = time(9, 50)
SESSION_CLOSE_ET = time(16, 0)
STOCK_SYMBOLS = {"SPX", "SPY", "TSLA"}


def _tally(rows):
    n = len(rows)
    wins = sum(1 for r in rows if r.get("exit") == "tp")
    stops = sum(1 for r in rows if r.get("exit") == "stop")
    flats = n - wins - stops
    total_r = round(sum(float(r.get("r") or 0.0) for r in rows), 3)
    return {"trades": n, "wins": wins, "stops": stops, "flats": flats,
            "win_rate_pct": round(100.0 * wins / n, 1) if n else None,
            "avg_r": round(total_r / n, 3) if n else None,
            "total_r": total_r}


def _entry_time(row):
    # "2026-04-28 11:05:00-04:00" -> 11:05 ET wall clock
    return datetime.fromisoformat(row["time"]).time()


def in_window(row) -> bool:
    t = _entry_time(row)
    return SESSION_OPEN_ET <= t < SESSION_CLOSE_ET


def _trim(rows):
    """The published row shape (drop the replay's internal sig_k)."""
    return [{k: v for k, v in r.items() if k != "sig_k"} for r in rows]


def replay_session(source: Path = SOURCE) -> dict:
    """Re-run the round-6 winner with the window inside passes(). Returns the
    report dict. Slow (loads the frozen stream and simulates every signal)."""
    import backtest_chart_v4 as v4
    import backtest_chart_v6 as v6

    rep = json.loads(source.read_text(encoding="utf-8"))
    published = rep["best"]["trades"]
    pub_cfg = rep["best"]["config"]
    # the procedural winner in v6's own config shape, from the report
    cfg = dict(v6.INC,
               gapf=pub_cfg["min_gap_atr"], gapc=pub_cfg["max_gap_atr"],
               runc=pub_cfg["max_run_from_open_atr"],
               effc=pub_cfg["max_day_efficiency"],
               efff=pub_cfg["min_day_efficiency"],
               chasec=pub_cfg["max_chase_atr_beyond_ce"],
               hours=pub_cfg["hours"], hcap=pub_cfg["max_entry_hour"],
               pdok=pub_cfg["pd_ok_required"], swept=pub_cfg["sweep_required"],
               unmitr=pub_cfg["unmitigated_required"],
               agec=pub_cfg["max_fvg_age_bars"], riskc=pub_cfg["max_est_risk_atr"],
               blmin=pub_cfg["min_bars_left"], buf=pub_cfg["stop_buffer_atr"],
               tp=pub_cfg["tp"], drop=frozenset(pub_cfg["drop_symbols"]))

    signals, bars, all_days, _cov = v6.load_stream()
    mid = len(all_days) // 2
    is_days = set(all_days[:mid])
    split = all_days[mid]
    assert split == rep["data"]["split_date"], (split, rep["data"]["split_date"])
    feats = v6.build_feats(signals, bars)
    m = v6.MGMT_IDX[(cfg["tp"], cfg["buf"])]
    outc_m = [[None] * len(signals) for _ in v6.MGMTS]
    outs = []
    for s in signals:
        H, L, C, O, _sub = bars[s["gid"]]
        outs.append(v4.simulate_mkt(s, H, L, C, O, cfg["tp"], cfg["buf"]))
    outc_m[m] = outs

    # 1) reproduce the published selection EXACTLY before changing anything
    base = v6.replay(cfg, signals, feats, outc_m)
    base_key = [(r["symbol"], r["time"], r["direction"], r["exit"]) for r in base]
    pub_key = [(r["symbol"], r["time"], r["direction"], r["exit"]) for r in published]
    reproduced = base_key == pub_key

    # 2) the window INSIDE the signal filter
    orig_passes = v6.passes

    def passes_in_window(c, s, f):
        return orig_passes(c, s, f) and in_window(s)

    v6.passes = passes_in_window
    try:
        live = v6.replay(cfg, signals, feats, outc_m)
    finally:
        v6.passes = orig_passes

    live_keys = {(r["symbol"], r["time"]) for r in live}
    base_keys = {(r["symbol"], r["time"]) for r in base}
    dropped = [r for r in base if (r["symbol"], r["time"]) not in live_keys]
    added = [r for r in live if (r["symbol"], r["time"]) not in base_keys]
    oos = [r for r in live if r["day"] >= split]
    ins = [r for r in live if r["day"] < split]
    per_symbol = {s: _tally([r for r in live if r["symbol"] == s])
                  for s in sorted({r["symbol"] for r in live})}
    stock_pre = [r for r in base if r["symbol"] in STOCK_SYMBOLS
                 and _entry_time(r) < time(10, 0)]
    post_filter = [r for r in base if in_window(r)]

    return {
        "kind": "measurement",
        "label": ("round-6 procedural winner REPLAYED with the live session "
                  "window inside the signal filter; window fixed by owner "
                  "instruction (no pre-market alerts) and by the backtest's own "
                  "stock data (regular session only) BEFORE these numbers were "
                  "computed; nothing selected on them"),
        "method": ("backtest_chart_v6.replay on the frozen round-4 stream with "
                   "backtest_chart_v4.simulate_mkt (market entry at the next 5m "
                   "open, stop first, tie = loss, no overnight), identical to "
                   "round 6 except passes() also requires "
                   f"{SESSION_OPEN_ET:%H:%M} <= signal time < "
                   f"{SESSION_CLOSE_ET:%H:%M} ET"),
        "reproduced_published_selection_first": reproduced,
        "source_report": str(source.relative_to(REPO)).replace("\\", "/"),
        "source_round": rep.get("round"),
        "source_run_date": rep.get("run_date"),
        "run_date": datetime.now().strftime("%Y-%m-%d"),
        "session_open_et": SESSION_OPEN_ET.strftime("%H:%M"),
        "session_close_et": SESSION_CLOSE_ET.strftime("%H:%M"),
        "config": dict(pub_cfg, hours=f"session {SESSION_OPEN_ET:%H:%M}-"
                                      f"{SESSION_CLOSE_ET:%H:%M} ET"),
        "rules": rep["best"]["rules"].replace(
            "entries only 7:00 ET or later",
            f"entries only {SESSION_OPEN_ET:%H:%M} to {SESSION_CLOSE_ET:%H:%M} "
            "ET (US session)"),
        "split_date": split,
        "stock_trades_before_10_et_in_source": len(stock_pre),
        "all": _tally(live),
        "is": _tally(ins),
        "oos": _tally(oos),
        "per_symbol": per_symbol,
        "before": {"all": _tally(base),
                   "oos": _tally([r for r in base if r["day"] >= split])},
        "post_filter_subset_for_reference": {
            "note": ("what you get by merely deleting out-of-window rows from "
                     "the published list; NOT a replay, shown only so the "
                     "difference is visible"),
            "all": _tally(post_filter),
            "oos": _tally([r for r in post_filter if r["day"] >= split])},
        "dropped": [{"symbol": r["symbol"], "time": r["time"],
                     "direction": r["direction"], "exit": r["exit"], "r": r["r"],
                     "oos": r["day"] >= split} for r in dropped],
        "added": [{"symbol": r["symbol"], "time": r["time"],
                   "direction": r["direction"], "exit": r["exit"], "r": r["r"],
                   "oos": r["day"] >= split} for r in added],
        "dropped_tally": _tally(dropped),
        "added_tally": _tally(added),
        "looks_ledger": {"prior_total_looks": rep.get("selection", {})
                         .get("total_looks"), "this_run": 1},
        "honest_note": (
            "OOS rows are the only untouched ones; quote the OOS pair (rate "
            "with its own count). The IS+OOS pooled rate is an upper bound. "
            "Zero transaction cost in the simulator; the Wilson 95% lower "
            "bound on the OOS rate sits near the 0.4R breakeven of 71.4%, so "
            "this is a measured record, not a demonstrated edge."),
        "trades": _trim(live),
    }


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    t0 = time_mod.time()
    out = replay_session()
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    b, a, pf = out["before"], out, out["post_filter_subset_for_reference"]
    print(f"reproduced the published 133-trade selection first: "
          f"{out['reproduced_published_selection_first']}")
    print(f"window {out['session_open_et']}-{out['session_close_et']} ET. stock "
          f"trades before 10:00 ET in the source: "
          f"{out['stock_trades_before_10_et_in_source']}")
    for name, t in (("BEFORE (published)", b), ("post-filter (NOT a replay)", pf),
                    ("AFTER (true replay)", a)):
        print(f"{name:28s} all {t['all']['wins']}/{t['all']['trades']} = "
              f"{t['all']['win_rate_pct']}%  avgR {t['all']['avg_r']:+.3f} | "
              f"OOS {t['oos']['wins']}/{t['oos']['trades']} = "
              f"{t['oos']['win_rate_pct']}%  avgR {t['oos']['avg_r']:+.3f}")
    for s, t in a["per_symbol"].items():
        print(f"  {s:8s} {t['wins']}/{t['trades']} = {t['win_rate_pct']}%  "
              f"avgR {t['avg_r']:+.3f}")
    print(f"DROPPED {a['dropped_tally']['trades']} ({a['dropped_tally']['wins']} tp "
          f"/ {a['dropped_tally']['stops']} stop), outside the window:")
    for r in a["dropped"]:
        print(f"  {r['symbol']:8s} {r['time'][:16]} {r['direction']:4s} "
              f"{r['exit']:4s} {r['r']:+.1f} {'OOS' if r['oos'] else 'IS'}")
    print(f"ADDED {a['added_tally']['trades']} ({a['added_tally']['wins']} tp "
          f"/ {a['added_tally']['stops']} stop), later signals the old "
          "pre-window trade used to block:")
    for r in a["added"]:
        print(f"  {r['symbol']:8s} {r['time'][:16]} {r['direction']:4s} "
              f"{r['exit']:4s} {r['r']:+.1f} {'OOS' if r['oos'] else 'IS'}")
    print(f"wrote {OUT.relative_to(REPO)} in {time_mod.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
