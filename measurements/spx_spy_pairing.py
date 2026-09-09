"""Why the SPX and SPY legs of the SAME signal do not pay the same.

WHY THIS EXISTS
---------------
The handoff treats SPX and SPY as one bet counted twice: they co-fire in the same
minute, the underlyings are highly correlated, and the working plan was to drop
the expensive leg (SPX, median contract $996) and keep the affordable one (SPY,
median $98). But in the live book SPY:call is the only EV-negative setup while
SPX:call is positive, on the SAME signal. Something other than the signal is
different, and this file measures what.

FINDING: strike-grid granularity. SPY's strike grid is $1 on a ~$762 underlying,
0.1312% of spot. SPX's grid is $5 on a ~$7562 underlying, 0.0661% of spot. So the
nearest strike to the money on SPY is structurally about twice as far out, in
percentage terms, as it is on SPX. The realized medians match that ratio almost
exactly. SPY is not "the cheap SPX". It is a materially further out of the money
bet on the same move, and dropping SPX for SPY silently changes the trade.

A hypothesis this measurement KILLED: that SPY's losses come from a wide bid/ask
against the 40 point give-back trail. At entry SPY has the TIGHTEST spread of the
four names, median 1.1% of mid, and across 116 entries with a recorded quote NOT
ONE had a spread at or above 40% of mid. The spread story cannot be tested at exit
because the bot records no exit-time quote. That gap is the finding, not the spread.

This is a MEASUREMENT of the existing record. Nothing was tuned or selected on it,
and the frozen backtest dataset was not touched. Every figure is a bot mark on a
tracked signal, not a broker fill.

    python spx_spy_pairing.py
"""

import collections
import csv
import json
import pathlib
import statistics as st

import _source

HERE = pathlib.Path(__file__).parent
DATA = _source.data_dir()
OUT = HERE / "spx_spy_pairing.json"

# the two live exit thresholds the spread hypothesis was measured against
TP_HALF_PCT = 25.0
RUNNER_GIVEBACK_PCT = 40.0


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _otm_pct(row):
    """How far out of the money the chosen strike sat, as a percent of spot.

    Signed so that positive always means further OTM, for a call or a put.
    """
    k, s = _f(row["strike"]), _f(row["spot_at_signal"])
    if k is None or not s:
        return None
    return (k - s) / s * 100.0 if row["right"] == "C" else (s - k) / s * 100.0


def _med(xs):
    return round(st.median(xs), 4) if xs else None


def main():
    mom = [r for r in csv.DictReader((DATA / "MOMENTUM_RECORDS.csv").open(encoding="utf-8-sig"))
           if r["state"] == "closed"]
    obs = {r["position_id"]: r
           for r in csv.DictReader((DATA / "OPTION_OBSERVATIONS.csv").open(encoding="utf-8-sig"))}

    # ---- per ticker: moneyness, premium, and the strike grid it is forced onto
    per = {}
    for t in sorted({r["ticker"] for r in mom}):
        rows = [r for r in mom if r["ticker"] == t]
        strikes = sorted({_f(r["strike"]) for r in rows} - {None})
        steps = sorted({round(b - a, 2) for a, b in zip(strikes, strikes[1:]) if 0 < b - a < 50})
        spots = [_f(r["spot_at_signal"]) for r in rows if _f(r["spot_at_signal"])]
        med_spot = st.median(spots) if spots else None
        step = steps[0] if steps else None
        spreads = [_f(obs[r["id"]]["spread_pct_of_mid"]) for r in rows
                   if r["id"] in obs and _f(obs[r["id"]]["spread_pct_of_mid"]) is not None]
        per[t] = {
            "n": len(rows),
            "median_otm_pct_of_spot": _med([x for x in map(_otm_pct, rows) if x is not None]),
            "median_premium_pct_of_spot": _med([_f(r["entry_mid"]) / _f(r["spot_at_signal"]) * 100.0
                                                for r in rows
                                                if _f(r["entry_mid"]) and _f(r["spot_at_signal"])]),
            "median_contract_cost_usd": _med([_f(r["contract_cost_usd_1x"]) for r in rows
                                              if _f(r["contract_cost_usd_1x"]) is not None]),
            "strike_step_usd": step,
            "median_spot": round(med_spot, 2) if med_spot else None,
            "strike_grid_pct_of_spot": round(100.0 * step / med_spot, 4) if step and med_spot else None,
            "quotes_seen": len(spreads),
            "median_entry_spread_pct_of_mid": _med(spreads),
            "entries_with_spread_ge_giveback": sum(1 for x in spreads if x >= RUNNER_GIVEBACK_PCT),
            "entries_with_spread_ge_half_trigger": sum(1 for x in spreads if x >= TP_HALF_PCT),
        }

    # ---- the paired view: same date, same minute, both legs present
    idx = collections.defaultdict(dict)
    for r in mom:
        if r["ticker"] in ("SPX", "SPY"):
            idx[(r["date"], r["time_et"][:5])][r["ticker"]] = r
    pairs = [(v["SPX"], v["SPY"]) for v in idx.values() if "SPX" in v and "SPY" in v]

    px = [(_f(a["final_pnl_pct"]), _f(b["final_pnl_pct"])) for a, b in pairs]
    px = [(x, y) for x, y in px if x is not None and y is not None]
    otm = [(_otm_pct(a), _otm_pct(b)) for a, b in pairs]
    otm = [(x, y) for x, y in otm if x is not None and y is not None]

    grid_ratio = (per["SPY"]["strike_grid_pct_of_spot"] / per["SPX"]["strike_grid_pct_of_spot"]
                  if per.get("SPX", {}).get("strike_grid_pct_of_spot") else None)
    otm_ratio = (per["SPY"]["median_otm_pct_of_spot"] / per["SPX"]["median_otm_pct_of_spot"]
                 if per.get("SPX", {}).get("median_otm_pct_of_spot") else None)

    out = {
        "kind": "measurement",
        "label": "SPX vs SPY on the same signal: strike-grid granularity, moneyness, "
                 "paired outcomes, and entry spread. Nothing selected on these numbers.",
        "sources": ["HANDOFF_BUILD/data/MOMENTUM_RECORDS.csv",
                    "HANDOFF_BUILD/data/OPTION_OBSERVATIONS.csv"],
        "window": "2026-06-15 to 2026-09-04, closed tracked signals only",
        "note": "Bot marks, not broker fills. A wider window than ledger_stats.json, "
                "which starts 2026-07-01, so per-ticker counts differ from that file "
                "on purpose. Both are correct for their own window.",
        "per_ticker": per,
        "paired_spx_spy": {
            "pairs": len(pairs),
            "graded_pairs": len(px),
            "spx_mean_final_pct": round(st.mean([x for x, _ in px]), 2) if px else None,
            "spy_mean_final_pct": round(st.mean([y for _, y in px]), 2) if px else None,
            "same_outcome_sign": sum(1 for x, y in px if (x > 0) == (y > 0)),
            "median_otm_pct_spx": _med([x for x, _ in otm]),
            "median_otm_pct_spy": _med([y for _, y in otm]),
        },
        "finding": {
            "strike_grid_ratio_spy_over_spx": round(grid_ratio, 3) if grid_ratio else None,
            "realized_otm_ratio_spy_over_spx": round(otm_ratio, 3) if otm_ratio else None,
            "reading": "The two ratios agree. SPY sits about twice as far out of the "
                       "money as SPX on the same signal, and the cause is mechanical: "
                       "its strike grid is twice as coarse as a percentage of spot. "
                       "This is a strike-selection artifact, not a market view, and it "
                       "is a candidate explanation for SPY's worse average loss and "
                       "smaller average win on an identically timed trade.",
            "spread_hypothesis": "REFUTED at entry. Across every entry with a recorded "
                                 "quote, none had a bid/ask spread at or above the 40 "
                                 "point give-back trail. SPY's entry spread is the "
                                 "tightest of the four names. The bot records no "
                                 "exit-time quote, so the exit-side version of this "
                                 "hypothesis is untestable with what exists today.",
            "not_established": "This measurement does NOT show that moving SPY's strike "
                               "closer to the money would make it profitable. It shows "
                               "the two legs are not the same trade. Testing the fix "
                               "needs option prices at alternative strikes for past "
                               "timestamps, which no free source provides.",
        },
    }
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")
    for t, v in per.items():
        print(f"  {t:5s} n={v['n']:3d} otm={v['median_otm_pct_of_spot']}% "
              f"grid={v['strike_grid_pct_of_spot']}% cost=${v['median_contract_cost_usd']:.0f} "
              f"spread={v['median_entry_spread_pct_of_mid']}%")
    p = out["paired_spx_spy"]
    print(f"  paired {p['pairs']}: SPX {p['spx_mean_final_pct']:+}% vs SPY "
          f"{p['spy_mean_final_pct']:+}%, same sign {p['same_outcome_sign']}/{p['graded_pairs']}")
    print(f"  grid ratio {out['finding']['strike_grid_ratio_spy_over_spx']}, "
          f"realized OTM ratio {out['finding']['realized_otm_ratio_spy_over_spx']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
