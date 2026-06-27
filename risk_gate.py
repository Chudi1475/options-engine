"""Pre-market risk gate: GREEN / YELLOW / RED, set fresh every trading day.

Sources, in order of authority:
1. Manual override — /risk green|yellow|red texted to the bot (wins all day).
2. Scheduled releases — the free ForexFactory weekly calendar feed
   (nfs.faireconomy.media). USD high-impact FOMC/CPI/PPI/NFP -> RED;
   any other USD high-impact release -> YELLOW. The feed allows only
   2 fetches per 5 minutes, so the parsed result is cached to disk and a
   failed fetch falls back to the last good pull (labeled).
3. Market stress — VIX >= 35 -> RED, VIX >= 25 -> YELLOW, overnight SPX gap
   >= 1% -> YELLOW (thresholds from the 5-year study in reports/news_study.md).
4. Optional news check — if ANTHROPIC_API_KEY is set, a web-search model is
   asked about major geopolitical escalation. Skipped silently if not set.
5. data/event_days.txt — manual extra event dates, kept for compatibility.

Effects (applied by the scanner): YELLOW adds a warning banner to cards;
RED halves all suggested sizes and prepends "HIGH-RISK DAY".

Usage:
    python risk_gate.py            # print today's mode + reason
    python risk_gate.py --study    # rebuild reports/news_study.md
"""

import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

import config

ET = ZoneInfo("America/New_York")
REPORTS_DIR = Path(__file__).parent / "reports"
EVENT_FILE = Path(__file__).parent / "data" / "event_days.txt"
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_CACHE = config.DATA_DIR / "calendar_cache.json"

VIX_RED = 35.0       # panic
VIX_YELLOW = 25.0    # elevated fear
GAP_YELLOW = 1.0     # overnight SPX gap, percent

# releases that historically whip the market hardest -> RED
RED_EVENTS = re.compile(
    r"FOMC|Federal Funds|CPI|Consumer Price|PPI|Producer Price|Non-?Farm|NFP",
    re.IGNORECASE)

SEVERITY = {"green": 0, "yellow": 1, "red": 2}


def _worse(a: str, b: str) -> str:
    return a if SEVERITY[a] >= SEVERITY[b] else b


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


def fetch_calendar() -> list:
    """This week's scheduled releases. Cached to disk; the feed allows only
    2 requests per 5 minutes, so never hammer it."""
    cache = {}
    if CALENDAR_CACHE.exists():
        try:
            cache = json.loads(CALENDAR_CACHE.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            cache = {}
    today = str(datetime.now(ET).date())  # ET, not the container's UTC date, so
    # the once-a-day cache rolls at ET midnight like every consumer (else it
    # re-fetches the rate-limited feed all evening)
    if cache.get("fetched_on") == today:
        return cache.get("events", [])
    try:
        r = requests.get(CALENDAR_URL, timeout=20)
        r.raise_for_status()
        events = r.json()
        CALENDAR_CACHE.write_text(
            json.dumps({"fetched_on": today, "events": events}), encoding="utf-8")
        return events
    except (requests.RequestException, ValueError):
        return cache.get("events", [])  # stale is better than blind


def calendar_check(today: date):
    """(mode, reasons) from this week's scheduled USD releases."""
    mode, reasons = "green", []
    for ev in fetch_calendar():
        try:
            ev_date = datetime.fromisoformat(ev["date"]).astimezone(ET).date()
        except (KeyError, ValueError, TypeError):
            continue
        if ev_date != today or ev.get("country") != "USD":
            continue
        impact = (ev.get("impact") or "").lower()
        title = ev.get("title", "scheduled release")
        when = datetime.fromisoformat(ev["date"]).astimezone(ET).strftime("%I:%M %p").lstrip("0")
        if impact == "high" and RED_EVENTS.search(title):
            mode = _worse(mode, "red")
            reasons.append(f"{title} at {when} ET (major release)")
        elif impact == "high":
            mode = _worse(mode, "yellow")
            reasons.append(f"{title} at {when} ET")
    return mode, reasons


# which currencies move each instrument the bot gives reads on — so a gold or
# forex read can warn about the scheduled news that actually whips that market
MACRO_CCY = {
    "GC=F": ["USD"],                 # gold: Fed / inflation / the dollar
    "EURUSD=X": ["USD", "EUR"],
    "GBPUSD=X": ["USD", "GBP"],
    "JPY=X": ["USD", "JPY"],
    "AUDUSD=X": ["USD", "AUD"],
    "CAD=X": ["USD", "CAD"],
    "CHF=X": ["USD", "CHF"],
}


def upcoming_events(currencies, within_hours: int = 24, high_only: bool = True):
    """Scheduled releases for the given currencies between now and
    now+within_hours, soonest first. Reuses the cached ForexFactory feed.
    Returns [{when, title, currency, impact, mins_away}]."""
    now = datetime.now(ET)
    horizon = now + timedelta(hours=within_hours)
    want = {c.upper() for c in currencies}
    out = []
    for ev in fetch_calendar():
        try:
            when = datetime.fromisoformat(ev["date"]).astimezone(ET)
        except (KeyError, ValueError, TypeError):
            continue
        if when < now or when > horizon:
            continue
        if (ev.get("country") or "").upper() not in want:
            continue
        impact = (ev.get("impact") or "").lower()
        if high_only and impact != "high":
            continue
        out.append({
            "when": when.strftime("%a %I:%M %p ET").replace(" 0", " "),
            "title": ev.get("title", "scheduled release"),
            "currency": ev.get("country"),
            "impact": impact,
            "mins_away": int((when - now).total_seconds() / 60),
        })
    out.sort(key=lambda e: e["mins_away"])
    return out


def market_stress_check(include_gap: bool):
    """(mode, reasons) from VIX level and the overnight gap."""
    mode, reasons = "green", []
    vix_now = None
    try:
        vix = fetch_daily("^VIX", "10d")
        vix_now = float(vix["Close"].iloc[-1])
    except Exception:
        reasons.append("couldn't read VIX")
    if vix_now is not None:
        if vix_now >= VIX_RED:
            mode = "red"
            reasons.append(f"VIX is {vix_now:.0f} (panic level)")
        elif vix_now >= VIX_YELLOW:
            mode = "yellow"
            reasons.append(f"VIX is {vix_now:.0f} (elevated fear)")
    gap = None
    if include_gap:
        try:
            spx = fetch_daily("^GSPC", "10d")
            gap = (float(spx["Open"].iloc[-1]) / float(spx["Close"].iloc[-2]) - 1) * 100
            if abs(gap) >= GAP_YELLOW:
                mode = _worse(mode, "yellow")
                reasons.append(f"big overnight gap ({gap:+.1f}%)")
        except Exception:
            pass
    return mode, reasons, vix_now, gap


def anthropic_news_check(today: date):
    """Optional: ask a web-search model about unscheduled escalation.
    Returns (mode, reason) or None. Silently skipped without an API key."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={
                "model": os.environ.get("RISK_CHECK_MODEL", "claude-haiku-4-5-20251001"),
                "max_tokens": 300,
                "tools": [{"type": "web_search_20250305", "name": "web_search",
                           "max_uses": 2}],
                "messages": [{"role": "user", "content":
                    f"Today is {today}. In the last 24 hours, has there been a "
                    "MAJOR geopolitical escalation or unscheduled market-moving "
                    "event that makes US index options unusually risky today? "
                    "Reply with exactly one line: GREEN, YELLOW or RED, then a "
                    "colon, then a reason of at most 12 words."}],
            },
            timeout=90)
        text = "".join(b.get("text", "") for b in r.json().get("content", [])
                       if b.get("type") == "text")
        m = re.search(r"\b(GREEN|YELLOW|RED)\b\s*[:\-]?\s*(.*)", text,
                      re.IGNORECASE | re.DOTALL)
        if m:
            reason = (m.group(2).strip().splitlines() or ["news check"])[0][:140]
            return m.group(1).lower(), reason
    except Exception:
        pass
    return None


def risk_mode(include_gap: bool = True):
    """Today's (mode, reason). Manual /risk override wins outright."""
    today = datetime.now(ET).date()

    override = config.state_get("risk_override")
    if override and override.get("date") == str(today):
        why = override.get("reason") or "manual override"
        return override["mode"], f"{why} (set by hand with /risk)"

    mode, reasons = calendar_check(today)
    s_mode, s_reasons, vix_now, gap = market_stress_check(include_gap)
    mode = _worse(mode, s_mode)
    reasons += s_reasons

    manual_event = load_event_days().get(today)
    if manual_event:
        mode = _worse(mode, "yellow")
        reasons.append(f"event list: {manual_event}")

    news = anthropic_news_check(today)
    if news:
        n_mode, n_reason = news
        if n_mode != "green":
            mode = _worse(mode, n_mode)
            reasons.append(f"news: {n_reason}")

    if not reasons or mode == "green":
        calm = []
        if vix_now is not None:
            calm.append(f"VIX {vix_now:.0f}")
        if gap is not None:
            calm.append(f"overnight gap {gap:+.1f}%")
        detail = ", ".join(calm) if calm else "no data problems"
        return "green", f"No major releases scheduled. {detail}. All clear."
    return mode, "; ".join(reasons)


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
    big = df[df["gap_pct"].abs() >= GAP_YELLOW]
    small = df[df["gap_pct"].abs() < GAP_YELLOW]
    add(f"- Gap >= ±{GAP_YELLOW:g}%: {len(big)} days, follow-through "
        f"{big['follow'].mean() * 100:.0f}%, avg abs move {big['day_ret'].abs().mean():.2f}%")
    add(f"- Gap < ±{GAP_YELLOW:g}%: {len(small)} days, follow-through "
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
            calm = r[r["vix"] < VIX_YELLOW]
            hot = r[r["vix"] >= VIX_YELLOW]
            add("")
            add(f"- Days with VIX under {VIX_YELLOW:g}: {len(calm)}, "
                f"total P&L **${calm['pnl'].sum():,.0f}**")
            add(f"- Days with VIX {VIX_YELLOW:g}+: {len(hot)}, "
                f"total P&L **${hot['pnl'].sum():,.0f}**")
        add("")
    add("Live thresholds in risk_gate.py: "
        f"YELLOW at VIX {VIX_YELLOW:g}, RED at VIX {VIX_RED:g}, "
        f"gap YELLOW at ±{GAP_YELLOW:g}%. Scheduled releases come from the "
        "free ForexFactory weekly feed (FOMC/CPI/PPI/NFP = RED); extra dates "
        "can still go in data/event_days.txt by hand.")
    add("")
    out = REPORTS_DIR / "news_study.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    if "--study" in sys.argv:
        study()
    else:
        mode, reason = risk_mode()
        print(f"{mode.upper()}: {reason}")
