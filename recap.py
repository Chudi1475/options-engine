"""Daily 3 PM Central recap — grades every alert the bot sent today, figures
out why each one worked or failed, and texts a plain-English summary.

Runs after the close (scheduled 3:05 PM CT weekdays). If the bot sent no
alerts, it still recaps the market and says why staying out was the call.

Grading sources, most honest first:
1. The position book (positions.json) — real tracked entries and exits with
   the prices that were actually alerted.
2. Legacy alerts_sent.jsonl records (pre-upgrade days) — replayed with the
   same approximated pricing the old backtest used.

Usage:
    python recap.py            # build + send today's recap
    python recap.py --dry-run  # print instead of texting
"""

import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import config
import live_params
import telegram
from backtest import SLIPPAGE, bs_price, realized_vol
from positions import PositionBook
from scoreboard import load_report
from strategy import StrategyConfig


def yfs_for(cfg, ticker: str) -> str:
    """Ticker -> Yahoo symbol via the live watchlist, falling back to the
    built-in map (same semantics as scanner.yfs_for): a legacy alert on a
    ticker since removed from live_params.json must still grade against its
    real feed symbol, not a bare-ticker guess that fetches nothing."""
    return (cfg.watchlist.get(ticker)
            or StrategyConfig().watchlist.get(ticker, ticker))

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")  # display timezone ONLY — logic stays ET
ALERTS_FILE = config.ALERTS_JSONL

# = scanner.SESSION_END. Until then the daytime monitoring loop owns settles
# (each cycle monitors BEFORE it recaps, and no monitor runs after the loop
# exits), so a same-day in-memory settle before this moment could text a
# grade the loop then contradicts by persisting a fresher mark minutes later.
SETTLE_AFTER = time(16, 12)


def et_now():
    return datetime.now(ET)


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


def _t(hms: str) -> str:
    """ET wall clock '10:42:15' -> '9:42 AM CT' (display only)"""
    return (datetime.strptime(hms, "%H:%M:%S") - timedelta(hours=1)).strftime("%I:%M %p CT").lstrip("0")


def _minutes_between(a: str, b: str) -> int:
    fmt = "%H:%M:%S"
    return int((datetime.strptime(b, fmt) - datetime.strptime(a, fmt))
               .total_seconds() / 60)


def kelechi_tag(reason: str = "", banked_half: bool = False) -> str:
    """A nod to the momentum style the bot trades — ONLY ever added to wins,
    so a green day reads as 'we ran the playbook' instead of boilerplate. The
    strategy IS the Kelechi momentum read (spot the 15-min turn, ride the
    continuation, bank half, trail the flip), so a clean win earns the name."""
    if reason == "momentum flip" or banked_half:
        return (" Classic Kelechi-style trade: caught the momentum turn, "
                "banked half into strength, and trailed the rest.")
    return " Kelechi style: spotted the push early and rode the continuation."


def position_story(p):
    """Grade a real tracked position. Returns (verdict, story)."""
    est_note = ""
    if p.entry_source == "estimate" or "estimat" in (p.last_mark_source or ""):
        est_note = " (some prices were estimates from the stock move; your broker shows real fills)"

    if p.state == "closed" and p.final_pnl_pct is None:
        # settled with no price ever seen (bot down at the close, or the
        # feed was): an honest blank, never an invented result, and never
        # the STILL-OPEN watching line
        return ("NOT GRADED",
                "It expired without the bot ever seeing a price for it, so "
                "it settled without a grade instead of an invented result. "
                "Your broker shows the real outcome." + est_note)
    if p.state != "closed":
        cur = p.last_mark_pct if p.last_mark_pct is not None else 0.0
        return (f"STILL OPEN ({cur:+.0f}% so far)",
                "This one doesn't expire today. I'm still watching it and "
                "will text the exits as they come." + est_note)

    total = p.final_pnl_pct
    verdict = "RIGHT ✅" if total > 0 else "WRONG ❌"
    reason = (p.final_exit or {}).get("reason", "")
    exit_t = (p.final_exit or {}).get("time", p.time_et)

    if reason == "stop":
        mins = _minutes_between(p.time_et, exit_t)
        if mins <= 30:
            why = ("the move flipped against us almost right away. Sometimes "
                   "the first push is a fake-out")
        else:
            why = ("the move ran out of gas and the option bled. An option "
                   "that goes nowhere loses value every minute (time decay)")
        peaked = (f" It even peaked at {p.mfe_pct:+.0f}% first, a reminder "
                  "the half-target matters."
                  if p.mfe_pct is not None and p.mfe_pct >= 10 else "")
        story = (f"It hit the {config.STOP_PCT:g}% stop at {_t(exit_t)}. "
                 f"Why it failed: {why}. The stop did its job: it kept a bad "
                 f"trade small.{peaked}")
    elif reason in ("momentum flip", "runner give-back"):
        h = p.half_exit or {}
        trigger = (f"once its gain dropped {config.RUNNER_GIVEBACK_PCT:g} "
                   "points from its peak"
                   if reason == "runner give-back" else
                   "once the momentum flipped")
        story = (f"We banked HALF at {h.get('pct', 0):+.0f}% at "
                 f"{_t(h.get('time', exit_t))}, let the rest RUN, and sold the "
                 f"remainder at {(p.final_exit or {}).get('pct', 0):+.0f}% at "
                 f"{_t(exit_t)} {trigger}. "
                 f"Whole trade: {total:+.0f}%.")
    elif "expir" in reason:
        h_note = (f" (half was banked at {p.half_exit['pct']:+.0f}% earlier)"
                  if p.half_exit else "")
        if "offline" in reason:
            # the settle reason says 'bot offline at close', but the same
            # shape happens when the bot was up and the FEED died at the
            # bell, so the words only claim what is always true: the close
            # was never seen and the grade uses the last price that was
            story = (f"The bot never saw the close for this one, so this "
                     f"grade uses the last price it did see: "
                     f"{total:+.0f}%{h_note}. Your broker shows the exact "
                     "closing number.")
        else:
            story = (f"It ran into the closing bell and was closed at "
                     f"{total:+.0f}%{h_note}. That's why the bot warns "
                     f"{config.EXPIRY_WARN_MINUTES} minutes before expiry: "
                     "same-day (0DTE) options don't get a tomorrow.")
    else:
        story = f"Closed at {total:+.0f}% ({reason})."
    tag = kelechi_tag(reason, banked_half=bool(p.half_exit)) if total > 0 else ""
    return verdict, story + tag + est_note


# ---------- legacy grading (pre-upgrade alert records) ----------

def grade_alert(rec, bars, daily_closes, bracket):
    """Replay an old-format alert with approximated pricing (old rules)."""
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
    best_ret = 0.0
    outcome, outcome_time, final_ret = "time stop", after.index[-1], None
    for ts, row in after.iterrows():
        T = max((expiry - ts.to_pydatetime()).total_seconds(), 0) / (365 * 24 * 3600)
        prem = bs_price(float(row["Close"]), rec["strike"], T, sigma, right) * (1 - SLIPPAGE)
        ret = pct(prem, entry_prem)
        best_ret = max(best_ret, ret)
        final_ret = ret
        if ret <= stop:
            outcome, outcome_time, final_ret = "stop", ts, stop
            break
        if ret >= target:
            outcome, outcome_time, final_ret = "target", ts, target
            break

    t_str = outcome_time.tz_convert(CT).strftime("%I:%M %p CT").lstrip("0")
    if outcome == "target":
        verdict = "RIGHT ✅"
        story = (f"It hit the +{target:g}% profit target at {t_str}. "
                 "Why it worked: the move kept going after we spotted it. "
                 "That's exactly what this setup bets on." + kelechi_tag())
    elif outcome == "stop":
        verdict = "WRONG ❌"
        minutes = int((outcome_time - alert_ts).total_seconds() / 60)
        why = ("the move flipped against us almost right away. Sometimes the "
               "first push up is a fake-out.") if minutes <= 30 else \
              ("the move ran out of gas, and an option that goes nowhere "
               "loses value every minute (that's called time decay).")
        story = (f"It dropped to the {stop:g}% stop at {t_str}. Why it failed: {why} "
                 "The stop did its job: it kept a bad trade small.")
    else:
        if final_ret is not None and final_ret > 0:
            verdict = "RIGHT ✅ (small win)"
            story = (f"Never hit the target, but ended the day up {final_ret:+.0f}%. "
                     "The move was real, just slower than usual. Kelechi style, "
                     "we still got paid.")
        else:
            verdict = "WRONG ❌ (slow loss)"
            story = (f"Never hit the stop, but bled to {final_ret:+.0f}% by the close. "
                     "The market went sideways and time decay ate the option. "
                     + (f"(It actually peaked at +{best_ret:.0f}% during the day, "
                        "a reminder of why taking profits fast matters.)"
                        if best_ret >= 5 else ""))
    return verdict, story


def market_story(spx_day):
    o = float(spx_day["Open"].iloc[0])
    c = float(spx_day["Close"].iloc[-1])
    hi_t = spx_day["High"].idxmax().tz_convert(CT).strftime("%I:%M %p").lstrip("0")
    lo_t = spx_day["Low"].idxmin().tz_convert(CT).strftime("%I:%M %p").lstrip("0")
    move = pct(c, o)
    if move > 0.3:
        mood = "an UP day"
    elif move < -0.3:
        mood = "a DOWN day"
    else:
        mood = "a sideways, choppy day"
    return (f"The S&P opened at {o:,.0f} and closed at {c:,.0f} ({move:+.1f}%), {mood}. "
            f"High of the day came at {hi_t} CT, low at {lo_t} CT.")


def main(require_date=None):
    dry = "--dry-run" in sys.argv
    # the EFFECTIVE settings (live_params-aware), so an alert on a ticker the
    # owner added via live_params.json grades against its real Yahoo symbol
    cfg = live_params.effective()[0]
    backtest = load_report("backtest_results.json")
    bracket = (backtest or {}).get("bracket", {"target_pct": 15, "stop_pct": -60})

    spx = fetch_5m("^GSPC")
    session = max(set(spx.index.date))
    # don't grade/suppress the wrong day: if the caller wants today's recap but
    # yfinance hasn't published today's session yet, bail so the caller retries
    # later instead of texting a stale-day recap and marking today done.
    if require_date is not None and str(session) != str(require_date) and not dry:
        print(f"recap: latest session data is {session}, not {require_date} — "
              "yfinance not caught up; skipping so we don't grade the wrong day.")
        return "STALE"
    spx_day = spx[spx.index.date == session]
    day_name = pd.Timestamp(session).strftime("%A %#m/%#d") if sys.platform.startswith("win") \
        else pd.Timestamp(session).strftime("%A %-m/%-d")

    lines = [f"📋 DAILY RECAP: {day_name}", ""]
    lines.append("THE MARKET TODAY: " + market_story(spx_day))
    lines.append("")

    book = PositionBook()
    # Settle overdue stragglers IN MEMORY first (save=False): the catch-up
    # recap after a late reboot must not tell the owner an expired 0DTE is
    # still being watched. positions.json keeps its single daytime writer;
    # the next session's needs_monitoring persists this exact settle, and a
    # --dry-run stays write-free. Same-day settles wait for SETTLE_AFTER so
    # the 16:05 recap during a bell-time feed outage can never text a grade
    # the still-running monitoring loop contradicts minutes later.
    now = et_now()
    if session < now.date() or now.time() >= SETTLE_AFTER:
        book.settle_overdue(session, now, save=False)
    pos_today = book.for_date(session)
    graded = set()
    if pos_today:
        lines.append("OUR ALERTS TODAY:")
        for p in pos_today:
            verdict, story = position_story(p)
            t = _t(p.time_et)
            lines.append("")
            tag = "[PAPER] " if p.paper else ""
            head = (f"{tag}{p.ticker} {p.strike:g} {p.direction.upper()} "
                    f"(texted {t}): ")
            head += verdict if verdict.startswith(("STILL", "NOT GRADED")) \
                else f"WE WERE {verdict}"
            lines.append(head)
            lines.append(story)
            graded.add((p.ticker, p.direction))

    legacy = [r for r in todays_alerts(session)
              if (r["ticker"], r["direction"]) not in graded]
    if legacy and not pos_today:
        lines.append("OUR ALERTS TODAY:")
    if legacy:
        daily_cache = {}
        for rec in legacy:
            yfs = yfs_for(cfg, rec["ticker"])
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
            t = (pd.Timestamp(f"{rec['date']} {rec['time']}")
                 - pd.Timedelta(hours=1)).strftime("%I:%M %p").lstrip("0")
            lines.append("")
            lines.append(f"{rec['ticker']} {rec['strike']:g} "
                         f"{rec['direction'].upper()} (texted {t} CT): "
                         f"WE WERE {verdict}")
            lines.append(story)

    if not pos_today and not legacy:
        first_hr = spx_day[spx_day.index.time <= pd.Timestamp("10:30").time()]
        drift = pct(float(first_hr["Close"].iloc[-1]), float(first_hr["Open"].iloc[0])) \
            if len(first_hr) else 0.0
        if drift < 0:
            why_quiet = ("the morning was moving DOWN, and the only setups that "
                         "pass our filter right now are call (up) setups")
        else:
            why_quiet = ("no setup cleared our quality bar (wins at least 70 "
                         "of 100 in testing) during the morning window")
        lines.append(f"OUR TRADES TODAY: none. The bot stayed quiet because {why_quiet}. "
                     "No text = no trade. Sitting out is a position too.")

    lines.append("")
    lines.append(f"Tomorrow: same plan. Wait for the text, sell half at "
                 f"+{config.TP_HALF_PCT:g}%, let the rest run and sell when I say "
                 f"its gain dropped {config.RUNNER_GIVEBACK_PCT:g} points from "
                 f"its peak, and honor the {config.STOP_PCT:g}% stop.")
    msg = "\n".join(lines)
    if dry:
        print(msg)
        return []
    errors = telegram.send(msg)
    print("Recap sent." if not errors else f"Errors: {errors}")
    with config.ALERTS_LOG.open("a", encoding="utf-8") as f:
        f.write(f"--- recap sent for {session}\n{msg}\n\n")
    return errors


if __name__ == "__main__":
    main()
