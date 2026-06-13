"""Every Telegram message the bot can send, in one place.
Written so a 6th grader can read every card. Cosmetics live here —
no trading logic."""

from datetime import date

import config

TIERS = [
    (85.0, "🟢🌟", "GREAT ODDS"),
    (80.0, "🟢", "GOOD ODDS"),
    (75.0, "🟠", "DECENT ODDS"),
    (70.0, "🔴", "RISKY"),
    (0.0, "⚪", "BELOW THE BAR"),
]

MODE_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def tier_for(win_rate: float):
    # rounded first — 69.77% displays as 70 everywhere, so it tiers as 70
    for floor, emoji, label in TIERS:
        if round(win_rate) >= floor:
            return emoji, label
    return "⚪", "?"


def fmt_day(d: date) -> str:
    return f"{d.month}/{d.day}"


def expiry_str(expiry: date, today: date) -> str:
    if expiry == today:
        return f"TODAY {fmt_day(expiry)}"
    return f"{expiry.strftime('%a')} {fmt_day(expiry)}"


def disp_ticker(ticker: str) -> str:
    return "SPXW" if ticker == "SPX" else ticker


def contract_str(pos) -> str:
    return f"{disp_ticker(pos.ticker)} {pos.strike:g} {pos.direction.upper()}"


def size_lines(risk_pct: float, mid: float, correlated: bool):
    """Risk-based sizing: a full stop-out costs exactly risk_pct of the
    account. Returns (lines, suggested_dollars_or_None)."""
    alloc = config.suggested_alloc_pct(risk_pct)
    acct = config.account_value()
    dollars = None
    line = f"Size: ~{alloc:.1f}% of account"
    if acct:
        dollars = acct * alloc / 100
        line += f" (≈ ${dollars:,.0f}"
        if mid and mid > 0:
            n = int(dollars // (mid * 100))
            if n >= 1:
                line += f" ≈ {n} contract{'s' if n != 1 else ''}"
            else:
                line += " — under 1 contract at this price"
        line += ")"
    line += f" — risking {risk_pct:g}% if stopped"
    lines = [line]
    if not acct:
        lines.append("(text /setaccount 25000 to the bot to see dollar amounts)")
    if correlated:
        lines.append("⚠️ CORRELATED — half size: a same-direction bet is already open.")
    return lines, dollars


def quote_lines(quote, est_mid: float):
    if quote is not None and quote.bid > 0:
        return [f"Quote: Bid ${quote.bid:.2f} / Ask ${quote.ask:.2f} — "
                f"set a LIMIT order near ${quote.mid:.2f}. Never market-order.",
                f"({quote.source})"]
    if quote is not None and quote.mid > 0:
        return [f"Quote: last trade ${quote.mid:.2f} (no live bid/ask) — "
                "set a LIMIT order near it. Never market-order."]
    return [f"No live option quote available — tracking from an ESTIMATE of ${est_mid:.2f}.",
            "Check the real price in your broker. Use a LIMIT order. Never market-order."]


def why_text(setup) -> str:
    if setup.direction == "call":
        return (f"WHY: {setup.ticker} just turned UP in the last 15 minutes "
                f"({setup.mom_pct:+.2f}%) — the exact pattern behind Kelechi's "
                "best trades.")
    return (f"WHY: {setup.ticker} is below its open and falling "
            f"({setup.mom_pct:+.2f}% in 15 min) — the mirror of the call "
            "setup. Less proven; extra care.")


def _expected_lines(stats: dict, dollars):
    ev = stats["ev_pct"]
    line = f"💰 EXPECTED: {ev:+.1f}% per trade {stats['costs_note']}"
    if dollars:
        line += f" (≈ {dollars * ev / 100:+,.0f} dollars on this trade)"
    return [line, f"({stats['label']})"]


def _winrate_footer(stats: dict) -> str:
    if stats["source"] == "live":
        emoji, label = tier_for(stats["win_rate"])
        return (f"{emoji} Live win rate: {stats['win_rate']:.0f} of 100 — {label} "
                f"({stats['trades']} real signals)")
    if stats["source"] == "backtest_new" and stats.get("old_win_rate") is not None:
        # the 70/75/80/85 tier bar belongs to the OLD-rules gate that
        # qualified this setup; the new exits trade win count for win size
        emoji, label = tier_for(stats["old_win_rate"])
        return (f"{emoji} Setup tier: {label} — old rules won "
                f"{stats['old_win_rate']:.0f} of 100. New exits win "
                f"{stats['win_rate']:.0f} of 100 but make more per trade "
                f"({stats['trades']} trades {stats['start']}-{stats['end']}, "
                "approx pricing)")
    emoji, label = tier_for(stats["win_rate"])
    return (f"{emoji} Win rate in testing: {stats['win_rate']:.0f} of 100 — {label} "
            f"({stats['trades']} trades {stats['start']}-{stats['end']}, "
            "old +15/-60 rules, approx pricing)")


def entry_card(setup, pos, quote, stats: dict, risk_mode: str,
               mode_reason: str, expiry: date, today: date,
               news_lines=None) -> str:
    lines = []
    if pos.paper:
        lines.append("[PAPER] practice mode — track it, don't trade it")
    if risk_mode == "red":
        lines.append("🚨 HIGH-RISK DAY — consider sitting out. Size below is HALVED.")
    elif risk_mode == "yellow":
        lines.append(f"⚠️ CAUTION DAY: {mode_reason}")
    size, dollars = size_lines(pos.risk_pct, pos.entry_mid, pos.correlated)
    lines += _expected_lines(stats, dollars)
    lines.append("")
    arrow = "📈" if setup.direction == "call" else "📉"
    lines.append(f"{arrow} BUY {setup.direction.upper()} — "
                 f"{disp_ticker(setup.ticker)} {setup.strike:g}, "
                 f"expires {expiry_str(expiry, today)}")
    lines += quote_lines(quote, pos.entry_mid)
    lines += size
    lines.append("")
    lines.append(why_text(setup))
    for n in (news_lines or []):
        lines.append(n)
    lines.append("")
    lines.append("EXIT PLAN — I'll text you each step:")
    lines.append(f"1️⃣ SELL HALF at +{config.TP_HALF_PCT:g}%")
    lines.append("2️⃣ momentum flips → I text you → sell the rest")
    lines.append(f"3️⃣ STOP: {config.STOP_PCT:g}% → sell everything")
    if expiry == today:
        lines.append(f"4️⃣ expires today → I warn you "
                     f"{config.EXPIRY_WARN_MINUTES} min before close")
    lines.append("")
    lines.append(_winrate_footer(stats))
    lines.append("Your call.")
    return "\n".join(lines)


def _paper(pos) -> str:
    return "[PAPER] " if pos.paper else ""


def half_card(pos, ev: dict) -> str:
    return "\n".join([
        f"{_paper(pos)}💰 SELL HALF — +{config.TP_HALF_PCT:g}% target hit",
        f"{contract_str(pos)} is up {ev['pct']:+.0f}% from your "
        f"${pos.entry_mid:.2f} entry ({ev['source']}).",
        "Sell HALF now. Let the rest ride.",
        "(Only got 1 contract? Just sell it — banking the win is the play.)",
        "Next: I'll text MOMENTUM FLIPPED when the move dies — sell the rest then.",
        "Your call.",
    ])


def flip_card(pos, ev: dict) -> str:
    half_pct = pos.half_exit["pct"] if pos.half_exit else 0.0
    return "\n".join([
        f"{_paper(pos)}🔄 MOMENTUM FLIPPED — SELL REMAINING",
        f"{contract_str(pos)}: the 15-min momentum just turned against the trade.",
        f"Remaining half is at {ev['pct']:+.0f}% ({ev['source']}).",
        f"Whole trade: about {ev['total_pct']:+.0f}% "
        f"(half banked at {half_pct:+.0f}%, half here).",
        "Sell the rest now.",
        "Your call.",
    ])


def stop_card(pos, ev: dict) -> str:
    return "\n".join([
        f"{_paper(pos)}🛑 STOP — SELL EVERYTHING",
        f"{contract_str(pos)} is down {ev['pct']:+.0f}% from your "
        f"${pos.entry_mid:.2f} entry ({ev['source']}).",
        "Sell it all now. The stop is the stop — one ignored stop "
        "erases a week of wins.",
        "Your call.",
    ])


def expiry_card(pos, ev: dict) -> str:
    return "\n".join([
        f"{_paper(pos)}⏰ CLOSE BEFORE EXPIRY",
        f"{contract_str(pos)} expires TODAY at 4 PM ET and is still open "
        f"(now {ev['pct']:+.0f}%, {ev['source']}).",
        f"Close it in the next {config.EXPIRY_WARN_MINUTES} minutes. "
        "0DTE options can go to $0 at the bell.",
        "Your call.",
    ])


def morning_card(mode: str, reason: str, today: date) -> str:
    effects = {
        "green": "Standard rules. Entry window 9:45-10:30 ET; "
                 "I'll watch every position until the close.",
        "yellow": "Setups still fire, with a warning banner — consider smaller size.",
        "red": "HIGH-RISK DAY — consider sitting out. Any alert today is HALF size.",
    }
    return "\n".join([
        f"{MODE_EMOJI[mode]} RISK MODE: {mode.upper()} — "
        f"{today.strftime('%A')} {fmt_day(today)}",
        reason,
        effects[mode],
    ])


def help_card() -> str:
    return "\n".join([
        "Commands I understand:",
        "/setaccount 25000 — set your account size (sizes cards in dollars)",
        "/risk green|yellow|red [reason] — override today's risk mode",
        "/status — risk mode, account, open positions right now",
        "/score — your personal win/loss record (I keep it for you)",
        "/adduser — let another person in (owner only)",
        "/users — see who has access (owner only)",
        "/test — fire a fake signal through every alert type",
        "/help — this list",
        "",
        "You can also just TALK to me — ask anything, or send a chart "
        "screenshot / PDF / CSV and I'll read it and answer like a human.",
    ])
