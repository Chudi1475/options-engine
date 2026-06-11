"""Winners-vs-losers study.

For each FIFO-matched round trip (reports/round_trips.csv, produced by
analyze_history.py), reconstructs what the underlying was doing at the moment
of entry using 5-minute bars, then compares winners against losers to find
conditions that separate them. Only data available at entry time is used —
no lookahead.

Usage:
    python study_wins.py
"""

from collections import defaultdict
from pathlib import Path

import pandas as pd
import yfinance as yf

REPORTS_DIR = Path(__file__).parent / "reports"
TRIPS_CSV = REPORTS_DIR / "round_trips.csv"

YF_TICKER = {"SPXW": "^GSPC", "SPX": "^GSPC"}  # everything else maps to itself

ET = "America/New_York"


def load_bars(tickers, start, end):
    """5m intraday + daily bars per ticker."""
    intraday, daily = {}, {}
    for t in tickers:
        yft = YF_TICKER.get(t, t)
        if yft in intraday:
            continue
        i5 = yf.download(yft, start=start, end=end, interval="5m",
                         progress=False, auto_adjust=False)
        d1 = yf.download(yft, start=start - pd.Timedelta(days=10), end=end,
                         interval="1d", progress=False, auto_adjust=False)
        if isinstance(i5.columns, pd.MultiIndex):
            i5.columns = i5.columns.get_level_values(0)
        if isinstance(d1.columns, pd.MultiIndex):
            d1.columns = d1.columns.get_level_values(0)
        intraday[yft] = i5
        daily[yft] = d1
    return intraday, daily


def entry_features(row, intraday, daily):
    """Market state at entry time, using bars strictly at/before entry."""
    yft = YF_TICKER.get(row["underlying"], row["underlying"])
    bars = intraday.get(yft)
    if bars is None or bars.empty:
        return None
    entry = pd.Timestamp(row["entry_time"]).tz_localize(ET)
    day = bars[bars.index.date == entry.date()]
    upto = day[day.index <= entry]
    if len(upto) < 1:
        return None

    day_open = float(day["Open"].iloc[0])
    px = float(upto["Close"].iloc[-1])

    dd = daily[yft]
    prior = dd[dd.index.date < entry.date()]
    prev_close = float(prior["Close"].iloc[-1]) if len(prior) else None

    feats = {
        "ret_open_to_entry": (px / day_open - 1) * 100,
        "above_open": px > day_open,
        "gap_pct": (day_open / prev_close - 1) * 100 if prev_close else None,
        "gap_up": day_open > prev_close if prev_close else None,
        "at_day_high": px >= float(upto["High"].max()) * 0.999,
        "minutes_in": (entry - day.index[0]).total_seconds() / 60,
    }
    # momentum over the prior 15 minutes (3 bars), needs 4+ bars of history
    if len(upto) >= 4:
        feats["mom_15m"] = (px / float(upto["Close"].iloc[-4]) - 1) * 100
        feats["mom_up"] = feats["mom_15m"] > 0
    else:
        feats["mom_15m"] = None
        feats["mom_up"] = None
    return feats


def wr(trades):
    w = sum(1 for t in trades if t["pnl"] > 0)
    return w / len(trades) * 100 if trades else 0


def fmt_split(name, trades_true, trades_false, label_true, label_false):
    return (
        f"| {name} | {label_true}: **{wr(trades_true):.0f}%** ({len(trades_true)}) "
        f"| {label_false}: **{wr(trades_false):.0f}%** ({len(trades_false)}) |"
    )


def main():
    trips = pd.read_csv(TRIPS_CSV, parse_dates=["entry_time", "exit_time"])
    start = trips["entry_time"].min().normalize()
    end = trips["exit_time"].max().normalize() + pd.Timedelta(days=1)

    print(f"Fetching bars for {trips['underlying'].nunique()} underlyings...")
    intraday, daily = load_bars(trips["underlying"].unique(), start, end)

    rows = []
    skipped = 0
    for _, r in trips.iterrows():
        f = entry_features(r, intraday, daily)
        if f is None:
            skipped += 1
            continue
        rows.append({**r.to_dict(), **f})
    df = pd.DataFrame(rows)
    n = len(df)
    wins = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]

    lines = []
    add = lines.append
    add("# Winners vs Losers Study")
    add("")
    add(f"{n} of {len(trips)} round trips had usable 5-minute market data at entry "
        f"({skipped} skipped — delisted ticker or missing bars). "
        f"Baseline win rate in this sample: **{wr(df.to_dict('records')):.1f}%**.")
    add("")
    add("All features are computed from bars at or before the entry fill — no lookahead. "
        "This is one month of data (in-sample); treat differences with small counts as noise.")
    add("")

    add("## What winners and losers looked like at entry (averages)")
    add("")
    add("| Feature | Winners | Losers |")
    add("|---|---|---|")
    for col, label in [
        ("ret_open_to_entry", "Underlying % from day open"),
        ("mom_15m", "15-min momentum %"),
        ("gap_pct", "Overnight gap %"),
        ("minutes_in", "Minutes after 9:30 open"),
        ("dte", "DTE"),
        ("entry_price", "Premium paid ($)"),
        ("qty", "Contracts"),
    ]:
        add(f"| {label} | {wins[col].mean():+.2f} | {losses[col].mean():+.2f} |")
    add("")

    add("## Win rate by condition")
    add("")
    add("| Condition | True | False |")
    add("|---|---|---|")
    recs = df.to_dict("records")
    splits = [
        ("Underlying above day open", "above_open"),
        ("15-min momentum positive", "mom_up"),
        ("Gapped up overnight", "gap_up"),
        ("Entry at the day's high (breakout)", "at_day_high"),
    ]
    for name, col in splits:
        t = [r for r in recs if r.get(col) is True]
        f = [r for r in recs if r.get(col) is False]
        add(fmt_split(name, t, f, "yes", "no"))
    early = [r for r in recs if r["minutes_in"] <= 60]
    late = [r for r in recs if r["minutes_in"] > 60]
    add(fmt_split("Entered within first hour", early, late, "yes", "no"))
    spx = [r for r in recs if r["underlying"] in ("SPXW", "SPX")]
    other = [r for r in recs if r["underlying"] not in ("SPXW", "SPX")]
    add(fmt_split("SPX/SPXW vs everything else", spx, other, "SPX", "other"))
    zdte = [r for r in recs if r["dte"] <= 1]
    swing = [r for r in recs if r["dte"] > 1]
    add(fmt_split("0-1 DTE vs longer", zdte, swing, "0-1", ">1"))
    add("")

    add("## Win rate by entry half-hour")
    add("")
    add("| Bucket | Win rate | Trades | Total P&L |")
    add("|---|---|---|---|")
    buckets = defaultdict(list)
    for r in recs:
        ts = pd.Timestamp(r["entry_time"])
        buckets[ts.strftime("%H:") + ("00" if ts.minute < 30 else "30")].append(r)
    for b in sorted(buckets):
        bt = buckets[b]
        add(f"| {b} | {wr(bt):.0f}% | {len(bt)} | ${sum(t['pnl'] for t in bt):,.0f} |")
    add("")

    # candidate setup: the conditions that held up, combined
    add("## Candidate setup (derived, in-sample)")
    add("")
    combo = [
        r for r in recs
        if r.get("mom_up") is True and r["dte"] <= 1
    ]
    anti = [
        r for r in recs
        if r.get("mom_up") is False and r.get("above_open") is True
    ]
    first15 = [r for r in recs if r.get("mom_up") is None]
    add("**Setup:** 0-1 DTE call, entered after the first 15 minutes, when 15-min "
        "momentum on the underlying is positive.")
    add("")
    add(f"- Matching trades: **{len(combo)}** — win rate **{wr(combo):.1f}%**, "
        f"total P&L **${sum(t['pnl'] for t in combo):,.0f}**")
    add(f"- Anti-setup (momentum down while extended above the open — chasing): "
        f"**{len(anti)}** trades, win rate **{wr(anti):.1f}%**, "
        f"P&L **${sum(t['pnl'] for t in anti):,.0f}**")
    add(f"- Entries in the first 15 min (momentum not yet readable): "
        f"**{len(first15)}** trades, win rate **{wr(first15):.1f}%**, "
        f"P&L **${sum(t['pnl'] for t in first15):,.0f}**")
    add("")
    add("This is measured on the same month it was derived from (in-sample), so it is "
        "an upper bound, not a forecast. Sample sizes are small. The honest "
        "out-of-sample test is Phase 3.")
    add("")

    out = REPORTS_DIR / "win_study.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    df.to_csv(REPORTS_DIR / "trips_with_features.csv", index=False)
    print(f"Wrote {out}")
    print(f"Wrote {REPORTS_DIR / 'trips_with_features.csv'}")


if __name__ == "__main__":
    main()
