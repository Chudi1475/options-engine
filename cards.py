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


def option_line(ticker: str, mn: dict, expiry=None) -> str:
    """One compact, scannable line for a ticker's live read, in the order:
    STOCK -> action+type (BUY CALL / BUY PUT) -> strike -> expiry -> win rate.
    `mn` is a market_tools.market_now() result; expiry is a date or None."""
    disp = disp_ticker(ticker)
    if mn.get("error"):
        return f"{disp}: not on the watchlist"
    if mn.get("note"):  # market closed / no data yet
        return f"{disp}: {mn['note'].rstrip('.').lower()}"
    setup = mn.get("live_setup")
    price = mn.get("price")
    pstr = f"${price:g}" if price is not None else "?"
    if not setup:
        if not mn.get("in_entry_window"):
            return f"{disp}  no setup yet (entry window is 9:45-10:30 ET, {pstr})"
        mom = mn.get("momentum_15min_pct")
        mtxt = f"{mom:+.2f}%" if mom is not None else "n/a"
        return f"{disp}  no live setup ({pstr}, 15m momentum {mtxt})"
    arrow = "📈" if setup["direction"] == "call" else "📉"
    typ = setup["direction"].upper()
    wr = setup.get("win_rate")
    wtxt = f"{wr:g}%" if wr is not None else "n/a"
    flag = "✅ alert-worthy" if setup.get("would_alert") else "⚪ below the bar"
    exp = f"  exp {expiry_str(expiry, date.today())}" if expiry else ""
    return f"{disp}  {arrow} BUY {typ}  {setup['strike']:g}{exp}  · {wtxt} {flag}"


ICONS = {"gold": "🪙", "forex": "💱", "stock": "📊", "crypto": "🟠"}


def fmt_price(price, kind: str, dec: int = 2) -> str:
    """One place for instrument price formatting: forex prints bare (1.0832),
    everything else gets a $, and decimals come from the read (so sub-dollar
    crypto like SHIB keeps real digits instead of rounding to $0.00)."""
    if price is None:
        return "?"
    s = f"{price:,.{dec}f}"
    return s if kind == "forex" else f"${s}"


def fmt_lvl(x, dec: int = 2) -> str:
    """A bare price level (SL/TP/risk) with thousands separators, no currency."""
    return "?" if x is None else f"{x:,.{dec}f}"


def macro_line(r: dict) -> str:
    """The compact answer for ANY non-core symbol (gold, forex, crypto, a
    looked-up stock). When the read carries a clean directional PLAN it renders
    as a trade ticket (BUY/SELL, SL, TP). When it's chop or the market's closed
    it falls back to the plain read + a 'wait' note."""
    if r.get("error"):
        return r["error"]
    if r.get("note"):
        return f"{r.get('instrument', 'that')}: {r['note']}"
    if r.get("plan"):
        return signal_card(r)
    return read_card(r)


def signal_card(r: dict) -> str:
    """The trade ticket: clean and scannable, like a real signal.
        🪙 SELL XAUUSD  @ 4,077.56
        SL: 4,081.4   (risk 3.8)
        TP: 4,069.9   (reward 7.6, 2R)
    Built only from the read's real numbers — never a fabricated level."""
    kind = r.get("kind", "stock")
    dec = r.get("decimals", 2)
    icon = ICONS.get(kind, "📊")
    sym = r.get("ticker") or r.get("instrument", "?")
    p = r["plan"]
    dot = "🟢" if p["direction"] == "BUY" else "🔴"
    verb = p["direction"]
    if kind == "stock":  # a stock is traded as options, not spot
        verb = "BUY CALLS" if p["direction"] == "BUY" else "BUY PUTS"
    lines = [f"{icon} {dot} {verb} {sym}  @ {fmt_price(p['entry'], kind, dec)}"]
    lines.append(f"SL: {fmt_lvl(p['stop'], dec)}   (risk {fmt_lvl(p['risk'], dec)})")
    if p.get("target1") is not None:
        lines.append(f"TP1: {fmt_lvl(p['target1'], dec)}   (1R, bank half, stop to entry)")
    lines.append(f"TP2: {fmt_lvl(p['target'], dec)}   (2R, let it run)")
    if kind == "stock":
        lines.append("(levels are on the stock; trade it as the calls/puts)")
    if r.get("asof"):
        lines.append(f"price as of {r['asof']}, ~15m delayed, confirm live before you click.")
    why = _why_line(r)
    if why:
        lines.append(why)
    size = _size_line(r)
    if size:
        lines.append(size)
    if p.get("weak"):
        lines.append("lower conviction: this push fights the 20-day trend, go lighter.")
    if r.get("event_warning"):
        lines.append(r["event_warning"])
    lines.append("read only, not auto-traded. Your call.")
    return "\n".join(lines)


def _why_line(r: dict) -> str:
    """One honest sentence on WHY, straight from the read's numbers."""
    bits = []
    mom = r.get("momentum_15min_pct")
    if mom is not None:
        bits.append(f"15m momentum {mom:+.2f}%")
    vs = r.get("vs_20day_avg")
    if vs:
        bits.append(f"{vs} the 20-day")
    return ("why: " + ", ".join(bits)) if bits else ""


def _size_line(r: dict):
    """Optional sizing. For gold the contract is well-defined (1 lot = 100 oz,
    a $1 move = $100/lot), so when the account size is known we can suggest a
    lot count that risks exactly RISK_PER_TRADE_PCT at the stop. Forex/crypto
    only get the percent framing (pip value varies by broker). A stock plan is
    traded as options, so its dollar risk is the premium, not the stop distance."""
    p = r.get("plan") or {}
    risk_pts = p.get("risk")
    if not risk_pts or risk_pts <= 0:
        return ""
    kind = r.get("kind")
    rp = config.RISK_PER_TRADE_PCT
    if kind == "stock":
        # option P&L isn't linear with the stock, so an underlying-distance stop
        # is NOT the option's dollar risk. Don't imply it is.
        return (f"your real risk is the premium you pay, size that to ~{rp:g}% of "
                "the account. the SL is where you cut the calls/puts.")
    acct = config.account_value()
    if not acct:
        return (f"size so the {fmt_lvl(risk_pts, r.get('decimals', 2))} stop costs "
                f"~{rp:g}% of your account (/setaccount to size it).")
    risk_dollars = acct * rp / 100
    if kind == "gold":
        lots = risk_dollars / (risk_pts * 100)
        if lots < 0.01:
            return (f"under a micro-lot at ~{rp:g}% of ${acct:,.0f}; gold's too big "
                    "for this account on this stop, size down or skip.")
        return (f"size: ~{lots:.2f} lots, risks ~${risk_dollars:,.0f} "
                f"({rp:g}% of ${acct:,.0f}) if the stop hits.")
    return (f"size it so the stop costs ~${risk_dollars:,.0f} ({rp:g}% of ${acct:,.0f}).")


def read_card(r: dict) -> str:
    """Plain read (no clean plan): price, day move, momentum, trend, and the
    trigger that would turn it into a trade. Used on chop or a closed market."""
    disp = r.get("instrument", "?")
    kind = r.get("kind") or ("gold" if disp == "Gold" else "forex")
    dec = r.get("decimals", 2)
    icon = ICONS.get(kind, "📊")
    head = ""
    if r.get("stale"):
        asof = r.get("asof")
        head = (f"🕒 market's closed. last session{(' ' + asof) if asof else ''}, "
                "these are reference levels, expect a gap on the open.\n")
    parts = [f"{icon} {disp}  {fmt_price(r.get('price'), kind, dec)}"]
    if r.get("day_move_pct") is not None:
        parts.append(f"{r['day_move_pct']:+.2f}% today")
    if r.get("momentum_15min_pct") is not None:
        parts.append(f"15m mom {r['momentum_15min_pct']:+.2f}%")
    if r.get("vs_20day_avg"):
        parts.append(f"{r['vs_20day_avg']} 20d avg")
    line = "  ·  ".join(parts) + "\n" + _lean(kind, r.get("bias"), r)
    if r.get("event_warning"):
        line += f"\n{r['event_warning']}"
    return head + line + "\nYour call."


def _lean(kind: str, bias: str, r: dict = None) -> str:
    """One-line directional lean from the read's momentum/trend bias."""
    if not bias or bias == "neutral":
        trig = ""
        if r:
            hi, lo = r.get("recent_session_high"), r.get("recent_session_low")
            dec = r.get("decimals", 2)
            if hi is not None and lo is not None:
                trig = (f" (flips to a trade on a 15-min push over {fmt_lvl(hi, dec)} "
                        f"or under {fmt_lvl(lo, dec)})")
        return f"⚪ chop, no clean lean. Wait for a 15-min push to pick a side{trig}"
    bull = bias.startswith("bull")
    weak = bias.endswith("weak")
    side = ("CALLS" if bull else "PUTS") if kind == "stock" else ("LONG" if bull else "SHORT")
    qual = " (counter the 20-day, lighter)" if weak else " (with the 20-day trend)"
    return f"{'📈' if bull else '📉'} lean: {side}{qual}"


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
                line += ", under 1 contract at this price"
        line += ")"
    line += f", risking {risk_pct:g}% if stopped"
    lines = [line]
    if not acct:
        lines.append("(text /setaccount 25000 to the bot to see dollar amounts)")
    if correlated:
        lines.append("⚠️ CORRELATED, half size: a same-direction bet is already open.")
    return lines, dollars


def quote_lines(quote, est_mid: float):
    if quote is not None and quote.bid > 0:
        return [f"Quote: Bid ${quote.bid:.2f} / Ask ${quote.ask:.2f}. "
                f"Set a LIMIT order near ${quote.mid:.2f}. Never market-order.",
                f"({quote.source})"]
    if quote is not None and quote.mid > 0:
        return [f"Quote: last trade ${quote.mid:.2f} (no live bid/ask). "
                "Set a LIMIT order near it. Never market-order."]
    return [f"No live option quote available, tracking from an ESTIMATE of ${est_mid:.2f}.",
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
    lines.append("2️⃣ let the rest RUN — I text you to sell when it gives back "
                 "from its peak")
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


def trail_card(pos, ev: dict) -> str:
    half_pct = pos.half_exit["pct"] if pos.half_exit else 0.0
    return "\n".join([
        f"{_paper(pos)}🔄 LOCK IN THE RUNNER — SELL REMAINING",
        f"{contract_str(pos)}: the runner gave back enough off its peak — bank it.",
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


def public_commands_card() -> str:
    """The commands EVERYONE with access can use — no owner-only entries.
    Single source of truth: /commands returns this and broadcast_commands.py
    sends this, so the two never drift."""
    return "\n".join([
        "📋 Commands everyone can use:",
        "",
        "/status — risk mode, account, open positions right now",
        "/calls [ticker] — live call/put setup per stock (BUY type, strike, expiry)",
        "/signal <symbol> — clean trade ticket: BUY/SELL, entry, SL, TP, 2R "
        "(e.g. /signal xauusd, /signal eurusd, /signal btc)",
        "/chart <symbol> — same ticket PLUS a chart image with the levels drawn on",
        "/gold — read on gold (price, momentum, news)",
        "/fx [pair] — read on a forex pair (EUR/USD, GBP/USD, USD/JPY, AUD/USD, "
        "USD/CAD, USD/CHF)",
        "/<symbol> — read + plan on ANY stock, ETF, fx, gold, or crypto "
        "(e.g. /aapl /nvda /btc /eth)",
        "/score — your personal win/loss record",
        "/help — the full list",
        "",
        "💬 You can also just talk to me, or send a chart screenshot / PDF / CSV "
        "and I'll read it and answer like a human.",
    ])


def help_card() -> str:
    return "\n".join([
        "Commands I understand:",
        "/setaccount 25000 — set your account size (sizes cards in dollars)",
        "/risk green|yellow|red [reason] — override today's risk mode",
        "/status — risk mode, account, open positions right now",
        "/calls [ticker] — live call/put setup per stock (BUY type, strike, expiry)",
        "/signal <symbol> — clean trade ticket: BUY/SELL, entry, SL, TP, 2R "
        "(e.g. /signal xauusd, /signal eurusd, /signal btc)",
        "/chart <symbol> — same ticket PLUS a chart image with the levels drawn on",
        "/gold · /fx [pair] — read on gold or a forex pair (price, momentum, news)",
        "/<symbol> — read + plan on ANY stock, ETF, fx, gold, or crypto "
        "(e.g. /aapl /nvda /btc /eth) — or just ask me for a plan on it",
        "/health — bot self-check: feed, last heartbeat, today's alerts (owner)",
        "/score — your personal win/loss record (I keep it for you)",
        "/adduser — let another person in (owner only)",
        "/users — see who has access (owner only)",
        "/test — fire a fake signal through every alert type",
        "/commands — just the everyone-can-use list (great to share)",
        "/help — this list",
        "",
        "Owner request controls:",
        "/requests — see open asks from Chudi/Kelechi/Ryan",
        "/approve <id> [note] · /reject <id> [note] · /done <id> — close one out "
        "(I text the person back)",
        "/backlog — open build items, ready to paste into Claude Code",
        "/reqfrom add <id> <name> — bring Kelechi/Ryan online (asks + alerts)",
        "",
        "You can also just TALK to me — ask anything, or send a chart "
        "screenshot / PDF / CSV and I'll read it and answer like a human.",
    ])
