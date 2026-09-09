"""Grader reconciliation: does the session report's average R add up, and is
the live book scored the same way?

WHY THIS EXISTS
---------------
Astra could not reconcile the session report and said so (M08 in the missing
evidence list): "43 full 0.4R wins and ten full 1R losses alone imply
+0.13585R, so session exits or other payoff differences must explain the
difference."

They do, exactly, and the answer is not the one I expected. I had guessed stop
overshoot, that a gap through the stop makes a loss worse than -1R. That is
wrong: every stop in the out of sample leg is exactly -1.000R. The difference is
entirely in the two END OF DAY exits, which are NOT flat. They average -0.516R.

Astra's arithmetic assumed ten full 1R losses. The truth is eight full stops
plus two partial losses of about half a risk unit each, which is why the figure
came out low.

THE PART THAT MATTERS MORE
--------------------------
Reconciling it exposed an error in my own packet. The backtest and the live book
count session-end exits DIFFERENTLY:

  backtest   win rate 43/53 = 81.1%. The two end of day rows are IN the
             denominator and count as non-wins.
  live book  win rate 32/49 = 65.3%. I EXCLUDED the four session-end rows from
             the denominator, on the argument that a flat is not a loss.

But the backtest's own end of day rows are not flat, they are losses of about
half a unit. So the two numbers I have been comparing were never on the same
convention, and the live-versus-backtest gap I reported was understated.

    python grader_reconciliation.py
"""

import collections
import json
import pathlib
import statistics as st

HERE = pathlib.Path(__file__).parent
REPO = HERE.parent
REPORT = REPO / "reports" / "chart_backtest_round6_session.json"
LIVE = HERE.parent.parent / "Desktop" / "Desktop#2" / "kelbot-review-packet" \
    / "HANDOFF_BUILD" / "data" / "SNIPER_RECORDS.csv"
OUT = HERE / "grader_reconciliation.json"

TP_R = 0.4


def _live_rows():
    """The 53 ticket live book, or None when the export is not on this machine.

    Never fabricated. A missing export makes the live half of this
    reconciliation unavailable and says so."""
    if not LIVE.exists():
        return None
    import csv
    return list(csv.DictReader(LIVE.open(encoding="utf-8-sig")))


def main():
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    split = rep["split_date"]
    oos = [t for t in rep["trades"] if t["day"] >= split]

    by_exit = {}
    for ex in sorted({t["exit"] for t in oos}):
        rs = [t["r"] for t in oos if t["exit"] == ex]
        by_exit[ex] = {
            "n": len(rs),
            "sum_r": round(sum(rs), 4),
            "mean_r": round(st.mean(rs), 4),
            "min_r": round(min(rs), 4),
            "max_r": round(max(rs), 4),
            "all_identical": min(rs) == max(rs),
        }

    total = sum(t["r"] for t in oos)
    reported = rep["oos"]

    # what Astra assumed, reproduced so the difference is visible
    astra_assumed = (43 * TP_R - 10 * 1.0) / 53

    recon = {
        "reproduces": {
            "n": len(oos) == reported["trades"],
            "total_r": abs(total - reported["total_r"]) < 1e-6,
            "avg_r": abs(total / len(oos) - reported["avg_r"]) < 5e-4,
        },
        "computed_total_r": round(total, 4),
        "computed_avg_r": round(total / len(oos), 4),
        "reported_total_r": reported["total_r"],
        "reported_avg_r": reported["avg_r"],
        "astra_assumed_avg_r": round(astra_assumed, 5),
        "why_astra_could_not_reconcile":
            "the assumption was ten full 1R losses. There are EIGHT full stops "
            "at exactly -1.000R and TWO end of day exits averaging -0.516R, so "
            "the loss side totals -9.032R rather than -10R.",
        "stop_overshoot_hypothesis": {
            "guess": "a gap through the stop makes a loss worse than -1R",
            "verdict": "REFUTED for this report. Every out of sample stop is "
                       "exactly -1.000R, so the simulator books the stop at the "
                       "threshold. That is an idealisation, not a conservatism: "
                       "Astra A18 says stops filled at the threshold are "
                       "idealised, and a real gap can fill worse.",
        },
    }

    # ---- the convention mismatch
    live = _live_rows()
    live_block = {"available": live is not None}
    if live:
        ex = collections.Counter(r["exit_reason"] for r in live)
        targets = ex.get("target", 0)
        stops = ex.get("stop", 0)
        sess = ex.get("session end", 0)
        n = len(live)
        live_block.update({
            "n": n,
            "targets": targets, "stops": stops, "session_end": sess,
            "as_i_reported_it": {
                "formula": "targets / (targets + stops), session end EXCLUDED",
                "pct": round(100.0 * targets / (targets + stops), 3),
            },
            "on_the_backtest_convention": {
                "formula": "targets / all rows, session end IN the denominator",
                "pct": round(100.0 * targets / n, 3),
            },
        })

    backtest_pct = reported["win_rate_pct"]
    if live:
        mine = live_block["as_i_reported_it"]["pct"]
        same = live_block["on_the_backtest_convention"]["pct"]
        live_block["gap_to_backtest"] = {
            "as_i_reported_it_points": round(backtest_pct - mine, 3),
            "like_for_like_points": round(backtest_pct - same, 3),
            "reading": "the gap I published used two different conventions. On "
                       "the backtest's own convention the live shortfall is "
                       "larger, not smaller.",
        }

    out = {
        "kind": "measurement",
        "label": "reconciliation of reports/chart_backtest_round6_session.json "
                 "out of sample average R, and of the win-rate convention used "
                 "for the live book. Closes Astra M08.",
        "source_report": str(REPORT.relative_to(REPO)),
        "split_date": split,
        "out_of_sample": {"n": len(oos), "by_exit": by_exit},
        "reconciliation": recon,
        "live_book_convention": live_block,
        "conclusions": [
            "The report reconciles exactly. M08 closes.",
            "Session-end exits are NOT flat in the backtest: they average "
            "-0.516R. Treating them as 0R is wrong in both directions.",
            "The live book's win rate and the backtest's win rate were computed "
            "on different denominators. Every future table states its "
            "convention.",
            "Stops book at exactly the threshold, which is an idealisation. A "
            "real gap through the stop can fill worse and this report cannot "
            "show it.",
        ],
    }
    OUT.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT.name}")
    print(f"  OOS n={len(oos)}  total {recon['computed_total_r']:+} "
          f"avg {recon['computed_avg_r']:+}  reproduces={recon['reproduces']}")
    for ex, v in by_exit.items():
        print(f"    {ex:6s} n={v['n']:3d} mean={v['mean_r']:+.4f} "
              f"identical={v['all_identical']}")
    print(f"  Astra assumed avg {recon['astra_assumed_avg_r']:+}")
    if live:
        a = live_block["as_i_reported_it"]["pct"]
        b = live_block["on_the_backtest_convention"]["pct"]
        print(f"  live win rate: {a}% as I reported it, {b}% on the backtest's "
              f"convention (backtest {backtest_pct}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
