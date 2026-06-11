"""Daily 3 PM Central recap — grades every alert the bot sent today, figures
out why each one worked or failed, and texts a plain-English summary.

Runs after the close (scheduled 3:05 PM CT weekdays). If the bot sent no
alerts, it still recaps the market and says why staying out was the call.

Usage:
    python recap.py            # build + send today's recap
    python recap.py --dry-run  # print instead of texting
"""

import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from backtest import SLIPPAGE, bs_price, realized_vol
from scanner import load_backtest, load_env, telegram_send
from strategy import StrategyConfig

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).parent
ALERTS_FILE = HERE / "alerts_sent.jsonl"


def fetch_5m(yf_symbol):
    df = yf.download(yf_symbol, period="5d", interval="5m",
                     progress=False, auto_adjust=False)
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df.index = df.index.tz_convert(ET)
    return df


def todays_alerts(session_date):
    if not ALERTS_FILE.exists():
        return []
    out = []
    for line in ALERTS_FILE.read_text(encoding="utf-8-sig").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("date") == str(session_date):
            out.append(rec)
    return out


def pct(a, b):
    return (a / b - 1) * 100


def grade_alert(rec, bars, daily_closes, bracket):
    """Replay the option from alert time to close. Returns (verdict, story)."""
    cfg = StrategyConfig()
    session = pd.Timestamp(rec["date"]).date()
    day = bars[bars.index.date == session]
    alert_ts = pd.Timestamp(f"{rec['date']} {rec['time']}").tz_localize(ET)
    after = day[day.index > alert_ts]
    if after.empty:
        return None, None
    sigma = realized_vol(daily_closes, session)
    right = "C" if rec["direction"] == "call" else "P"
    expiry = alert_ts.replace(hour=16, minute=0).to_pydatetime()
    T0 = max((expiry - alert_ts.to_pydatetime()).total_seconds(), 0) / (365 * 24 * 3600)
    entry_prem = bs_price(rec["spot"], rec["strike"], T0, sigma, right) * (1 + SLIPPAGE)
    if entry_prem <= 0:
        return None, None

    target, stop = bracket["target_pct"], bracket["stop_pct"]
    best_ret, worst_ret = 0.0, 0.0
    outcome, outcome_time, final_ret = "time stop", after.index[-1], None
    for ts, row in after.iterrows():
        T = max((expiry - ts.to_pydatetime()).total_seconds(), 0) / (365 * 24 * 3600)
        prem = bs_price(float(row["Close"]), rec["strike"], T, sigma, right) * (1 - SLIPPAGE)
        ret = pct(prem, entry_prem)
        best_ret, worst_ret = max(best_ret, ret), min(worst_ret, ret)
        final_ret = ret
        if ret <= stop:
            outcome, outcome_time, final_ret = "stop", ts, stop
            break
        if ret >= target:
            outcome, outcome_time, final_ret = "target", ts, target
            break

    und_after = pct(float(after["Close"].iloc[-1]), rec["spot"])
    went_our_way = und_after > 0 if right == "C" else und_after < 0
    t_str = outcome_time.strftime("%I:%M %p ET").lstrip("0")

    if outcome == "target":
        verdict = "RIGHT ✅"
        story = (f"It hit the +{target:g}% profit target at {t_str}. "
                 "Why it worked: the move kept going after we spotted it — "
                 "that's exactly what this setup bets on.")
    elif outcome == "stop":
        verdict = "WRONG ❌"
        minutes = int((outcome_time - alert_ts).total_seconds() / 60)
        if minutes <= 30:
            why = ("the move flipped against us almost right away. "
                   "Sometimes the first push up is a fake-out.")
        else:
            why = ("the move ran out of gas, and an option that goes nowhere "
                   "loses value every minute (that's called time decay).")
        story = (f"It dropped to the {stop:g}% stop at {t_str}. Why it failed: {why} "
                 "The stop did its job — it kept a bad trade small.")
    else:
        if final_ret is not None and final_ret > 0:
            verdict = "RIGHT ✅ (small win)"
            story = (f"Never hit the target, but ended the day up {final_ret:+.0f}%. "
                     "The move was real, just slower than usual.")
        else:
            verdict = "WRONG ❌ (slow loss)"
            story = (f"Never hit the stop, but bled to {final_ret:+.0f}% by the close. "
                     "The market went sideways and time decay ate the option. "
                     + ("(It actually peaked at +"
                        f"{best_ret:.0f}% during the day — a reminder of why "
                        "taking the target fast matters.)" if best_ret >= 5 else ""))
    if not went_our_way and outcome == "stop":
        story += (f" Bigger picture: {rec['ticker']} finished the day moving against "
                  "our direction, so the entry signal was simply early/wrong today.")
    return verdict, story


def market_story(spx_day):
    o = float(spx_day["Open"].iloc[0])
    c = float(spx_day["Close"].iloc[-1])
    hi_t = spx_day["High"].idxmax().strftime("%I:%M %p").lstrip("0")
    lo_t = spx_day["Low"].idxmin().strftime("%I:%M %p").lstrip("0")
    move = pct(c, o)
    if move > 0.3:
        mood = "an UP day"
    elif move < -0.3:
        mood = "a DOWN day"
    else:
        mood = "a sideways, choppy day"
    return (f"The S&P opened at {o:,.0f} and closed at {c:,.0f} ({move:+.1f}%) — {mood}. "
            f"High of the day came at {hi_t} ET, low at {lo_t} ET.")


def main():
    dry = "--dry-run" in sys.argv
    load_env()
    cfg = StrategyConfig()
    backtest = load_backtest()
    bracket = (backtest or {}).get("bracket", {"target_pct": 15, "stop_pct": -60})

    spx = fetch_5m("^GSPC")
    session = max(set(spx.index.date))
    spx_day = spx[spx.index.date == session]
    day_name = pd.Timestamp(session).strftime("%A %-m/%-d") if not sys.platform.startswith("win") \
        else pd.Timestamp(session).strftime("%A %#m/%#d")

    lines = [f"📋 DAILY RECAP — {day_name}", ""]
    lines.append("THE MARKET TODAY: " + market_story(spx_day))
    lines.append("")

    alerts = todays_alerts(session)
    if not alerts:
        first_hr = spx_day[spx_day.index.time <= pd.Timestamp("10:30").time()]
        drift = pct(float(first_hr["Close"].iloc[-1]), float(first_hr["Open"].iloc[0])) \
            if len(first_hr) else 0.0
        if drift < 0:
            why_quiet = ("the morning was moving DOWN, and the only setups that "
                         "pass our filter right now are call (up) setups")
        else:
            why_quiet = ("no setup cleared the 70% win-rate bar during the "
                         "morning window")
        lines.append(f"OUR TRADES TODAY: none. The bot stayed quiet because {why_quiet}. "
                     "No text = no trade. Sitting out is a position too.")
    else:
        lines.append("OUR ALERTS TODAY:")
        daily_cache = {}
        for rec in alerts:
            yfs = cfg.watchlist.get(rec["ticker"], rec["ticker"])
            bars = spx if yfs == "^GSPC" else fetch_5m(yfs)
            if yfs not in daily_cache:
                d1 = yf.download(yfs, period="1y", interval="1d",
                                 progress=False, auto_adjust=False)
                if hasattr(d1.columns, "levels"):
                    d1.columns = d1.columns.get_level_values(0)
                daily_cache[yfs] = d1["Close"]
            verdict, story = grade_alert(rec, bars, daily_cache[yfs], bracket)
            if verdict is None:
                continue
            disp = "SPXW" if rec["ticker"] == "SPX" else rec["ticker"]
            t = pd.Timestamp(f"{rec['date']} {rec['time']}").strftime("%I:%M %p").lstrip("0")
            lines.append("")
            lines.append(f"{disp} {rec['strike']:g} {rec['direction'].upper()} "
                         f"(texted {t} ET): WE WERE {verdict}")
            lines.append(story)
    lines.append("")
    lines.append("Tomorrow: same plan. Wait for the text, take only what passes "
                 "the filter, honor the exits.")
    msg = "\n".join(lines)
    if dry:
        print(msg)
    else:
        errors = telegram_send(msg)
        print("Recap sent." if not errors else f"Errors: {errors}")
        (HERE / "alerts.log").open("a", encoding="utf-8").write(
            f"--- recap sent for {session}\n{msg}\n\n")


if __name__ == "__main__":
    main()
