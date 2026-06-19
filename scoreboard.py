"""Live scoreboard built from tracked positions, plus the Friday weekly report.

Honesty design:
- Cards quote backtest numbers (clearly labeled) only until the scoreboard
  holds LIVE_STATS_MIN_TOTAL closed signals AND LIVE_STATS_MIN_SETUP for the
  specific setup — then real results take over automatically.
- Live EV subtracts SPREAD_COST_PCT because live tracking is mid-to-mid and
  nobody actually fills at the mid. Backtest EV does NOT subtract it again —
  the backtest already charges 1.5% slippage each way plus per-contract fees.
- Every signal stores both the new-rules result and an old-rules (+15/-60)
  shadow result on the same prices, so the weekly comparison is apples to
  apples.

Usage:
    python scoreboard.py            # print the current scoreboard
    python scoreboard.py --weekly   # print this week's report
    python scoreboard.py --send     # send the weekly report to Telegram
"""

import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import date, timedelta
from pathlib import Path

import config
from positions import PositionBook

REPORTS_DIR = Path(__file__).parent / "reports"


def load_report(name: str):
    path = REPORTS_DIR / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None


def _stats(pcts: list):
    if not pcts:
        return None
    wins = [p for p in pcts if p > 0]
    losses = [p for p in pcts if p <= 0]
    return {
        "trades": len(pcts),
        "win_rate": len(wins) / len(pcts) * 100,
        "avg_win_pct": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_pct": sum(losses) / len(losses) if losses else 0.0,
        "expectancy_pct": sum(pcts) / len(pcts),
    }


def live_stats(book: PositionBook):
    """(per-setup stats dict, total closed count) from real tracked signals."""
    per = {}
    for p in book.closed():
        per.setdefault(p.setup_key(), []).append(p.final_pnl_pct)
    return {k: _stats(v) for k, v in per.items()}, sum(len(v) for v in per.values())


def stats_for_card(ticker: str, direction: str, book: PositionBook,
                   backtest_old, backtest_new):
    """What the card quotes, with its honest label. Live beats backtest once
    there's enough real data; new-rules backtest beats old-rules backtest
    because it matches the exits the card actually recommends."""
    key = f"{ticker}:{direction}"
    live, total = live_stats(book)
    if (total >= config.LIVE_STATS_MIN_TOTAL and key in live
            and live[key]["trades"] >= config.LIVE_STATS_MIN_SETUP):
        s = dict(live[key])
        s["ev_pct"] = s["expectancy_pct"] - config.SPREAD_COST_PCT
        s["label"] = f"LIVE RESULTS — {s['trades']} real tracked signals"
        s["costs_note"] = "after est. spread"
        s["source"] = "live"
        return s
    old = (backtest_old or {}).get("per_setup", {}).get(key)
    new = (backtest_new or {}).get("per_setup", {}).get(key)
    if new:
        s = dict(new)
        s["ev_pct"] = s["expectancy_pct"]  # slippage+fees already inside
        s["label"] = "NEW-RULES BACKTEST, approx pricing — live stats take over after 30 signals"
        s["costs_note"] = "after est. costs"
        s["source"] = "backtest_new"
        s["old_win_rate"] = old["win_rate"] if old else None
        return s
    if old:
        s = dict(old)
        s["ev_pct"] = s["expectancy_pct"]
        s["label"] = "OLD-RULES BACKTEST — live stats take over after 30 signals"
        s["costs_note"] = "after est. costs"
        s["source"] = "backtest_old"
        return s
    return None


def _week_bounds(today: date):
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=4)


def _fmt_d(d: date) -> str:
    return f"{d.month}/{d.day}"


def weekly_report(book: PositionBook, backtest_old, backtest_new,
                  today: date) -> str:
    monday, friday = _week_bounds(today)
    closed = book.closed()
    week = [p for p in closed
            if monday <= date.fromisoformat(p.date) <= friday]
    still_open = [p for p in book.positions if p.state != "closed"]

    lines = [f"📊 WEEKLY SCOREBOARD — week of {_fmt_d(monday)}-{_fmt_d(friday)}", ""]
    add = lines.append

    if not week:
        add("Signals this week: 0. Nothing passed the filter — sitting out "
            "was the trade. No text = no trade, and that's working as designed.")
    else:
        s = _stats([p.final_pnl_pct for p in week])
        add(f"Signals this week: {s['trades']} "
            f"({sum(1 for p in week if p.final_pnl_pct > 0)} wins, "
            f"{sum(1 for p in week if p.final_pnl_pct <= 0)} losses)")
        add(f"Average win: {s['avg_win_pct']:+.0f}% | "
            f"Average loss: {s['avg_loss_pct']:+.0f}%")
        peaks = [p.mfe_pct for p in week if p.mfe_pct is not None]
        if peaks:
            avg_peak = sum(peaks) / len(peaks)
            add(f"Average peak before exit: {avg_peak:+.0f}% "
                "(how high trades got before we left)")
        add("")
        new_total = sum(p.final_pnl_pct for p in week)
        add(f"NEW exit rules (half at +{config.TP_HALF_PCT:g} → ride till "
            f"momentum flips → stop {config.STOP_PCT:g}):")
        add(f"  this week: {new_total:+.0f}% (adding up each trade's %)")
        both = [p for p in week if p.old_rules.get("exit_pct") is not None]
        if both:
            old_total = sum(p.old_rules["exit_pct"] for p in both)
            add(f"OLD exit rules (+15/-60) on the exact same entries: {old_total:+.0f}%")
            diff = new_total - old_total
            winner = "NEW" if diff >= 0 else "OLD"
            add(f"This week's winner: {winner} rules by {abs(diff):.0f} points")
        if any(p.paper for p in week):
            add("[PAPER] — some or all of this week's signals were practice mode.")
    if still_open:
        add("")
        add("Still open: " + ", ".join(
            f"{p.ticker} {p.strike:g} {p.direction}"
            f" ({(p.last_mark_pct if p.last_mark_pct is not None else 0):+.0f}%)"
            for p in still_open))
    add("")

    if closed:
        s_all = _stats([p.final_pnl_pct for p in closed])
        first = min(p.date for p in closed)
        ev_live = s_all["expectancy_pct"] - config.SPREAD_COST_PCT
        add(f"ALL-TIME: {s_all['trades']} signals since {first}. "
            f"Win rate {s_all['win_rate']:.0f}%. "
            f"Expected value per trade: {ev_live:+.1f}% after est. spread.")
        claims = []
        if backtest_old:
            claims.append(f"old-rules backtest claimed "
                          f"{backtest_old['overall']['win_rate']:.0f}% wins")
        if backtest_new and backtest_new.get("overall"):
            claims.append(f"new-rules backtest claimed "
                          f"{backtest_new['overall']['win_rate']:.0f}% wins")
        if claims:
            add("For comparison: " + "; ".join(claims) + ".")
        if s_all["trades"] < config.LIVE_STATS_MIN_TOTAL:
            add(f"Still early — live stats take over the cards at "
                f"{config.LIVE_STATS_MIN_TOTAL} signals "
                f"({s_all['trades']} so far).")
    add("")
    add("Same plan Monday: wait for the text, honor the exits. No trade is forced.")
    return "\n".join(lines)


def main():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    book = PositionBook()
    today = datetime.now(ZoneInfo("America/New_York")).date()
    bt_old = load_report("backtest_results.json")
    bt_new = load_report("backtest_new_rules.json")
    text = weekly_report(book, bt_old, bt_new, today)
    if "--send" in sys.argv:
        import telegram
        errors = telegram.send(text)
        print("Weekly report sent." if not errors else f"Errors: {errors}")
    else:
        print(text)


if __name__ == "__main__":
    main()
