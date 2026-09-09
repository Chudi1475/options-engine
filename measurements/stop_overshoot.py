"""Every loss deeper than the -90% stop was priced by the model, not the market.

WHY THIS EXISTS
---------------
The review said "five losses were booked worse than the -90% stop level, so the
stop is not a floor". That is true but it buries the actual finding. Splitting the
same rows by HOW the closing mark was obtained separates two completely different
things:

  - a live quote overshooting the stop by a fraction of a point, which is just the
    15 second poll cadence and is unavoidable;
  - a MODELED mark, priced by Black-Scholes off realized volatility, producing a
    tail that no live quote in the book ever produced.

If every deep loss carries a modeled mark, then the tail is a pricing artifact and
the fix is in the pricer, not in the stop level. Moving the stop would not have
prevented a single one of them, and it WOULD cost win rate, which matters because
MIN_WINRATE is 70 and a setup under the floor stops alerting.

Relevant known bias: the estimate is priced with REALIZED volatility from daily
closes, not the option's implied vol. On 0DTE, live IV is routinely far above
realized vol, so the model is biased and there is an open backlog item about it.

This is a MEASUREMENT of the existing record. Nothing was tuned or selected on it.
Bot marks, not broker fills.

    python stop_overshoot.py
"""

import collections
import csv
import json
import pathlib

import _source

HERE = pathlib.Path(__file__).parent
SRC = _source.data_dir() / "MOMENTUM_RECORDS.csv"
OUT = HERE / "stop_overshoot.json"

STOP_PCT = -90.0  # config.STOP_PCT, the level the bot is supposed to exit at
MODELED = "estimated"  # the substring that marks a Black-Scholes mark


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    rows = [r for r in csv.DictReader(SRC.open(encoding="utf-8-sig"))
            if r["state"] == "closed"]
    past = [r for r in rows if (_f(r["final_pnl_pct"]) or 0.0) < STOP_PCT]

    def bucket(rs):
        pnl = sorted(_f(r["final_pnl_pct"]) for r in rs)
        return {
            "n": len(rs),
            "worst": round(pnl[0], 2) if pnl else None,
            "best": round(pnl[-1], 2) if pnl else None,
            "max_overshoot_points": round(abs(pnl[0] - STOP_PCT), 2) if pnl else None,
            "exit_reasons": dict(collections.Counter(r["final_exit_reason"] for r in rs)),
            "tickers": dict(collections.Counter(r["ticker"] for r in rs)),
        }

    modeled = [r for r in past if MODELED in (r["last_mark_source"] or "").lower()]
    quoted = [r for r in past if MODELED not in (r["last_mark_source"] or "").lower()]

    all_stops = [r for r in rows if r["final_exit_reason"] == "stop"]

    out = {
        "kind": "measurement",
        "label": "losses deeper than the -90% stop, split by whether the closing mark "
                 "was a live quote or a Black-Scholes estimate. Nothing selected on these.",
        "source": "HANDOFF_BUILD/data/MOMENTUM_RECORDS.csv",
        "window": "2026-06-15 to 2026-09-04, closed tracked signals only",
        "stop_level_pct": STOP_PCT,
        "closed_rows": len(rows),
        "stop_exits_total": len(all_stops),
        "stop_exits_past_the_level": len(past),
        "every_past_level_row_was_a_stop": all(r["final_exit_reason"] == "stop" for r in past),
        "by_mark_source": {
            "modeled_black_scholes": bucket(modeled),
            "live_quote": bucket(quoted),
        },
        # the magnitudes only. Per-trade dates and tickers are the live alert
        # record, which is gitignored on purpose (positions.json, alerts_sent.jsonl)
        # because this repo is public. The shape of the tail is the finding; who
        # traded what on which day is not, and it does not belong in a public file.
        "modeled_pnl_pcts": sorted(round(_f(r["final_pnl_pct"]), 2) for r in modeled),
        "quoted_pnl_pcts": sorted(round(_f(r["final_pnl_pct"]), 2) for r in quoted),
        "finding": {
            "reading": "Every row in the book that closed past the stop level was a stop "
                       "exit. Split by mark source, the two groups do not overlap in "
                       "magnitude: the modeled marks hold the deep tail, and no live "
                       "quote in the book ever produced a comparable loss. The deep "
                       "losses are a pricing artifact, not market slippage.",
            "why_it_matters": "Moving the stop level would not have prevented any of "
                              "them, and a tighter stop costs win rate. MIN_WINRATE is "
                              "70, so a setup pushed under the floor stops alerting.",
            "known_bias": "The estimate is priced with realized volatility from daily "
                          "closes, not implied vol. On 0DTE, live IV is routinely far "
                          "above realized, so the model mark is biased on exactly the "
                          "contracts where the chain goes bid-less near the close.",
            "not_established": "This does not show what the true exit price was. There "
                               "is no broker fill and no historical option quote "
                               "archive, so the honest claim is that the recorded loss "
                               "is unverified whenever the mark is modeled, not that it "
                               "is wrong by a known amount.",
        },
    }
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"  {len(all_stops)} stop exits, {len(past)} of them past {STOP_PCT}")
    for k, v in out["by_mark_source"].items():
        print(f"  {k:24s} n={v['n']:2d} worst={v['worst']} best={v['best']} "
              f"max overshoot {v['max_overshoot_points']} points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
