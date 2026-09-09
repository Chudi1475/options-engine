"""Split the live sniper book two ways and write one measurement file.

WHY THIS EXISTS
---------------
Two different "before and after the 09:50 window" splits are floating around the
handoff, and they were being quoted as if they were the same split. They are not:

  Split A, by ENTRY HOUR. The `strategy_version` column in SNIPER_RECORDS.csv
  tags a row "pre-0950-window" when its entry hour is 07, 08 or 09 ET, i.e. a
  ticket the CURRENT config could never fire, and "post-0950-window" otherwise.
  This is a config-consistency cut. It says nothing about when the code shipped:
  both tags contain rows dated 2026-08-10.

  Split B, by SHIP DATE. The window shipped 2026-08-22. Rows dated on or after
  that day ran under the new code.

ASK_GPT.md mixed them: it took "18 rows predate the window" from Split A and
"post-change only: 23 rows, -4.925R" from Split B. 18 + 23 is 41, not 53, so the
pair cannot both describe the same partition of the book. This file computes both
cuts side by side so the next reader cannot repeat that.

This is a MEASUREMENT of an already-fixed configuration, not a selection. Nothing
here was tuned, swept, or chosen on these numbers, and the frozen backtest dataset
was not touched.

    python sniper_window_split.py
"""

import collections
import csv
import json
import pathlib

import _source

HERE = pathlib.Path(__file__).parent
SRC = _source.data_dir() / "SNIPER_RECORDS.csv"
OUT = HERE / "sniper_window_split.json"

SHIP_DATE = "2026-08-22"  # the 09:50 session window went live this day


def _num(row, key):
    """A float, or None when the ledger genuinely has no value."""
    v = (row.get(key) or "").strip()
    if v in ("", "None", "null"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def summarize(rows, label):
    """Counts and R for one subset. Unresolved rows are counted, never invented.

    win_rate_resolved deliberately excludes 'session end' flats: a flat is not a
    loss and calling it one flatters the stop and punishes the target. The flats
    are reported separately so the denominator is always visible.
    """
    graded = [r for r in rows if r["exit_reason"] in ("target", "stop")]
    wins = sum(1 for r in graded if r["exit_reason"] == "target")
    rs = [_num(r, "r") for r in rows]
    rs = [x for x in rs if x is not None]
    return {
        "label": label,
        "n": len(rows),
        "exits": dict(collections.Counter(r["exit_reason"] for r in rows)),
        "resolved": len(graded),
        "wins": wins,
        "win_rate_resolved_pct": round(100.0 * wins / len(graded), 1) if graded else None,
        "rows_with_r": len(rs),
        "net_r": round(sum(rs), 3),
        "avg_r_per_ticket": round(sum(rs) / len(rs), 4) if rs else None,
        "by_symbol": {
            d: {
                "n": sum(1 for r in rows if r["display"] == d),
                "target": sum(1 for r in rows if r["display"] == d and r["exit_reason"] == "target"),
                "stop": sum(1 for r in rows if r["display"] == d and r["exit_reason"] == "stop"),
                "net_r": round(sum(x for x in (_num(r, "r") for r in rows if r["display"] == d)
                                   if x is not None), 3),
            }
            for d in sorted({r["display"] for r in rows})
        },
    }


def main():
    rows = list(csv.DictReader(SRC.open(encoding="utf-8-sig")))
    hour_pre = [r for r in rows if r["strategy_version"].startswith("pre")]
    hour_post = [r for r in rows if r["strategy_version"].startswith("post")]
    date_pre = [r for r in rows if r["date"] < SHIP_DATE]
    date_post = [r for r in rows if r["date"] >= SHIP_DATE]
    forex = [r for r in rows if r["instrument_class"] == "forex"]
    equity = [r for r in rows if r["instrument_class"] != "forex"]

    out = {
        "kind": "measurement",
        "label": "live sniper book split two ways: by entry hour (config consistency) "
                 "and by the 2026-08-22 ship date of the 09:50 window. Nothing selected "
                 "on these numbers.",
        "source": "HANDOFF_BUILD/data/SNIPER_RECORDS.csv",
        "source_rows": len(rows),
        "ship_date": SHIP_DATE,
        "note": "These are bot marks on the UNDERLYING, in R. No option premium, no "
                "spread, no broker fill. A 'session end' row is flat, not a loss.",
        "whole_book": summarize(rows, "all 53 tickets"),
        "split_a_by_entry_hour": {
            "basis": "the strategy_version column: entry hour 07/08/09 ET is a ticket "
                     "the current 09:50 window forbids",
            "forbidden_by_current_window": summarize(hour_pre, "entry before 09:50 ET"),
            "allowed_by_current_window": summarize(hour_post, "entry at or after 10:00 ET"),
        },
        "split_b_by_ship_date": {
            "basis": f"row date against {SHIP_DATE}, the day the window shipped",
            "before_ship": summarize(date_pre, f"dated before {SHIP_DATE}"),
            "on_or_after_ship": summarize(date_post, f"dated on or after {SHIP_DATE}"),
        },
        "split_c_by_instrument": {
            "forex": summarize(forex, "EUR/USD and USD/JPY"),
            "equity_or_index": summarize(equity, "SPX, SPY, TSLA"),
        },
        "reconciliation": {
            "why_18_plus_23_is_not_53":
                "18 is the Split A count of tickets whose entry hour the current window "
                "forbids. 23 is the Split B count of tickets dated on or after the ship "
                "date. They partition the book along different axes, so they do not add. "
                "Split A sums 18 + 35 = 53. Split B sums 30 + 23 = 53.",
        },
    }
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")
    for k in ("whole_book",):
        s = out[k]
        print(f"  {s['label']}: n={s['n']} netR={s['net_r']:+} "
              f"wr={s['win_rate_resolved_pct']}% of {s['resolved']} resolved")
    for group in ("split_a_by_entry_hour", "split_b_by_ship_date", "split_c_by_instrument"):
        for key, s in out[group].items():
            if key == "basis":
                continue
            print(f"  {s['label']}: n={s['n']} netR={s['net_r']:+} "
                  f"wr={s['win_rate_resolved_pct']}% of {s['resolved']} resolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
