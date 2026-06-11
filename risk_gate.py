"""Phase 5 — pre-market risk gate ("is today a good day to trade?").

There is no free archive of news headlines going back 5 years, so this gate
uses what news actually moves: the VIX (the market's fear gauge — every
headline, Fed speech, and war scare is priced into it within seconds), the
overnight gap, and a manual list of scheduled event days (CPI/FOMC/etc.)
in data/event_days.txt, one YYYY-MM-DD per line with an optional label.

--study mode measures, over 5 years and over his profitable May 2026 month,
how morning follow-through behaved by VIX level and gap size, and writes
reports/news_study.md. The live thresholds below come from that study.

Usage:
    python risk_gate.py            # print today's verdict
    python risk_gate.py --study    # rebuild reports/news_study.md
"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

REPORTS_DIR = Path(__file__).parent / "reports"
EVENT_FILE = Path(__file__).parent / "data" / "event_days.txt"

# live thresholds (see reports/news_study.md for where these come from)
VIX_CAUTION = 25.0   # above this: trade smaller, expect whips
VIX_BLOCK = 35.0     # above this: scanner stands down for the day
GAP_CAUTION = 1.0    # overnight SPX gap bigger than ±1%: caution


def load_event_days() -> dict:
    days = {}
    if EVENT_FILE.exists():
        for line in EVENT_FILE.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split(None, 1)
            if not parts or parts[0].startswith("#"):
                continue
            try:
                d = date.fromisoformat(parts[0])
            except ValueError:
                continue
            days[d] = parts[1] if len(parts) > 1 else "scheduled event"
    return days


def fetch_daily(symbol, period="5y"):
    df = yf.download(symbol, period=period, interval="1d",
                     progress=False, auto_adjust=False)
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    return df


def morning_check():
    """Returns (approved, tier_emoji, message) for today."""
    spx = fetch_daily("^GSPC", "10d")
    vix = fetch_daily("^VIX", "10d")
    vix_now = float(vix["Close"].iloc[-1])
    gap = (float(spx["Open"].iloc[-1]) / float(spx["Close"].iloc[-2]) - 1) * 100
    today = date.today()
    event = load_event_days().get(today)

    problems = []
    if vix_now >= VIX_BLOCK:
        return False, "🔴", (f"🔴 NO-TRADE DAY: VIX is {vix_now:.0f} (panic level). "
                             "The bot is standing down today.")
    if vix_now >= VIX_CAUTION:
        problems.append(f"VIX is {vix_now:.0f} (elevated fear — moves get violent)")
    if abs(gap) >= GAP_CAUTION:
        problems.append(f"big overnight gap ({gap:+.1f}%) — chasing gaps lost money in testing")
    if event:
        problems.append(f"scheduled event today: {event} — expect fakeouts around the release")
    if problems:
        return True, "🟠", ("🟠 CAUTION DAY: " + "; ".join(problems) +
                            ". Setups still fire but consider half size.")
    return True, "🟢", (f"🟢 NORMAL DAY: VIX {vix_now:.0f}, overnight gap {gap:+.1f}%, "
                        "no scheduled events listed. Standard rules apply.")


def study():
    """Where the thresholds come from: 5y of follow-through by regime, plus
    his actual May 2026 daily P&L vs VIX and gap."""
    spx = fetch_daily("^GSPC", "5y")
    vix = fetch_daily("^VIX", "5y")
    df = pd.DataFrame({
        "open": spx["Open"], "close": spx["Close"],
        "prev_close": spx["Close"].shift(1),
        "vix": vix["Close"].reindex(spx.index).ffill(),
    }).dropna()
    df["gap_pct"] = (df["open"] / df["prev_close"] - 1) * 100
    df["day_ret"] = (df["close"] / df["open"] - 1) * 100
    # follow-through: did the day continue in the gap's direction?
    df["follow"] = (df["gap_pct"] * df["day_ret"]) > 0

    lines = ["# News / risk-regime study", "",
             "No free 5-year headline archive exists, so 'news' here is measured "
             "the way the market scores it: VIX level (all fear, all headlines, "
             "priced live) and overnight gap size. 5 years of S&P data.", ""]
    add = lines.append

    add("## 5 years: does the morning follow through, by VIX level?")
    add("")
    add("| VIX at close before | Days | Gap-direction follow-through | Avg abs day move |")
    add("|---|---|---|---|")
    for lo, hi, label in [(0, 15, "under 15 (calm)"), (15, 20, "15-20 (normal)"),
                          (20, 25, "20-25 (nervous)"), (25, 35, "25-35 (fear)"),
                          (35, 99, "35+ (panic)")]:
        b = df[(df["vix"].shift(1) >= lo) & (df["vix"].shift(1) < hi)]
        if len(b) == 0:
            continue
        add(f"| {label} | {len(b)} | {b['follow'].mean() * 100:.0f}% "
            f"| {b['day_ret'].abs().mean():.2f}% |")
    add("")
    add("## 5 years: big overnight gaps")
    add("")
    big = df[df["gap_pct"].abs() >= GAP_CAUTION]
    small = df[df["gap_pct"].abs() < GAP_CAUTION]
    add(f"- Gap ≥ ±{GAP_CAUTION:g}%: {len(big)} days, follow-through "
        f"{big['follow'].mean() * 100:.0f}%, avg abs move {big['day_ret'].abs().mean():.2f}%")
    add(f"- Gap < ±{GAP_CAUTION:g}%: {len(small)} days, follow-through "
        f"{small['follow'].mean() * 100:.0f}%, avg abs move {small['day_ret'].abs().mean():.2f}%")
    add("")

    trips_csv = REPORTS_DIR / "round_trips.csv"
    if trips_csv.exists():
        trips = pd.read_csv(trips_csv, parse_dates=["entry_time", "exit_time"])
        daily_pnl = trips.groupby(trips["exit_time"].dt.date)["pnl"].sum()
        rows = []
        for d, pnl in daily_pnl.items():
            ts = pd.Timestamp(d)
            if ts in df.index:
                rows.append((d, pnl, float(df.loc[ts, "vix"]),
                             float(df.loc[ts, "gap_pct"])))
        add("## His May 2026: daily P&L vs the risk regime")
        add("")
        add("| Day | P&L | VIX | Overnight gap |")
        add("|---|---|---|---|")
        for d, pnl, v, g in rows:
            add(f"| {d} | ${pnl:,.0f} | {v:.0f} | {g:+.1f}% |")
        if rows:
            r = pd.DataFrame(rows, columns=["d", "pnl", "vix", "gap"])
            calm = r[r["vix"] < VIX_CAUTION]
            hot = r[r["vix"] >= VIX_CAUTION]
            add("")
            add(f"- Days with VIX under {VIX_CAUTION:g}: {len(calm)}, "
                f"total P&L **${calm['pnl'].sum():,.0f}**")
            add(f"- Days with VIX {VIX_CAUTION:g}+: {len(hot)}, "
                f"total P&L **${hot['pnl'].sum():,.0f}**")
        add("")
    add("Live thresholds in risk_gate.py: "
        f"caution at VIX {VIX_CAUTION:g}, stand-down at VIX {VIX_BLOCK:g}, "
        f"gap caution at ±{GAP_CAUTION:g}%. Scheduled events (CPI/FOMC/wars/"
        "tariff announcements) go in data/event_days.txt by hand — one date "
        "per line — because no free feed lists them historically.")
    add("")
    out = REPORTS_DIR / "news_study.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    if "--study" in sys.argv:
        study()
    else:
        ok, tier, msg = morning_check()
        print(msg)
        sys.exit(0 if ok else 1)
