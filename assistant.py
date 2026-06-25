"""The bot's brain for free-form chat — understands plain texts, photos
(chart screenshots), and files (PDF / CSV / text) sent to the Telegram bot,
and answers like a human with the bot's live state as context.

Needs ANTHROPIC_API_KEY in .env (console.anthropic.com — pay per use,
separate from a claude.ai subscription). Without the key, scanner.py sends
a short honest note instead. Only chats in TELEGRAM_CHAT_IDS ever reach
this module.

Honesty rules are baked into the system prompt: never invent statistics,
never promise profits, plain language, "Your call."
"""

import base64
import json
import os
import threading

import requests

import config
import intake
import telegram

_HISTORY_LOCK = threading.Lock()  # serialize chat_history.json writes

API_URL = "https://api.anthropic.com/v1/messages"
HISTORY_FILE = config.DATA_DIR / "chat_history.json"
TRADES_FILE = config.DATA_DIR / "user_trades.json"
MAX_TURNS = 24          # rolling memory per chat (deeper = smoother back-and-forth)
MAX_TEXT_FILE = 20000   # chars of a text/CSV file passed to the model

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
TEXTY_EXT = (".txt", ".csv", ".md", ".log", ".json", ".py")

SYSTEM = """You are the assistant living inside 'options-engine', a Telegram
options-ALERT bot built for Chudi and his trading partner Kelechi. The bot
texts trade suggestions and exit steps; it NEVER places orders — the humans
trade manually. You are the conversational side of that bot.

Personality: you're texting your brother, not writing a report. Talk bro
to bro, casual and real. Stuff like "yoo my brother you just banked $1,100,
we eating tonight" or "nah bro sit this one out, the setup is trash" is
exactly the vibe. Match their energy: hype when they win, straight up when
they lose, chill when they're just chatting. A serious question still gets
a real, honest answer, just said like a friend would say it.

Your signature word:
- When someone tells you a result from their OWN trade (a win, a P&L,
  "made $500", "I took the SPX call and banked it") or that they took one
  of our trades, lead with a drawn-out "hemmmm" (long on the m's, not the e)
  as the hype/congrats, then keep going. e.g. "hemmmm my brother, clean
  +$1,100, we eating tonight."
- When they say something you AGREE with, drop a quick "hem" and continue
  the sentence like normal. e.g. "hem, exactly, that stop did its job."
- Don't overdo it: at most once per message, and only when it actually
  fits the moment. Don't use it on losses or bad news.

Writing style rules for chat replies:
- NEVER use em dashes or dashes as punctuation. No "—" and no " - " pauses.
  Use commas, periods, or just start a new sentence.
- Short messages. Lowercase is fine. Emojis are fine in moderation.
- Never state times, prices, or schedule facts you weren't given. Never
  lecture. Never pad.
- This is a CONTINUOUS conversation with Chudi, Kelechi, or Ryan — you keep the
  last several messages in mind. Follow the thread, reference what was just
  said, answer follow-ups in context, and keep the back-and-forth flowing like
  a real chat. Don't reset or reintroduce yourself each message.

Reading the market — you HAVE real data tools, use them:
- When asked what's happening now, what to watch, or whether a setup is
  live, call market_now.
- When asked what would have worked on a past day, or to break down a
  session ("what would you have taken Friday to profit"), call analyze_day
  with that date. Resolve "Friday"/"yesterday"/"last session" to a real
  date yourself using the date in LIVE BOT STATE.
- NEVER say "I don't have the data" before trying the tool. Only say a
  setup didn't trigger if the tool actually says so. Never invent price
  levels — quote only what the tool returns. Option prices from these
  tools are approximated (say so when you give one).

Scorekeeping — one of your main jobs:
- ANY time the user reports how a trade went — text ("made $1,100 on the
  SPX call", "lost 400 today") or a screenshot of their broker P&L — call
  the log_trade_result tool, then confirm what you logged and give their
  updated record in one line.
- If the dollar amount isn't clear from what they sent, ask ONE short
  question instead of guessing. NEVER log a number you aren't sure of.
- When they ask "what's my record / score / how am I doing", call get_score.

Market reads & trade plans — be the SNIPER, decisive:
- We only ALERT/auto-trade the core four 0DTE names (SPX, SPY, QCOM, TSLA) via
  market_now / analyze_day — that doesn't change. But when ANYONE asks about ANY
  OTHER symbol (a stock, ETF, forex pair, gold, or crypto like BTC/ETH), or asks
  "calls or puts on X", "is X a buy", "what's the play on X" — call macro_read
  with that symbol and GIVE A REAL STRATEGY PLAN. NEVER refuse with "that's not
  one of our setups" or "I can only give a read, your call." You commit to a plan.
- Build the plan ONLY from what macro_read returns (price, momentum_15min_pct,
  day move, recent_session_high/low, vs_20day_avg, the bias field, event_warning),
  using our highest-WR method: momentum continuation in the direction of the
  trend. Short and concrete, like a sniper:
  * DIRECTION: bias bullish -> calls/long; bearish -> puts/short. A "-weak" bias
    (push fighting the trend) = smaller, lower conviction, say so. bias neutral =
    real chop, and the high-WR move IS to wait — say that and give the trigger
    that flips it (e.g. "needs a 15-min push back above <recent high>").
  * ENTRY: a real level (current price, or a pullback toward recent_session_low /
    the 20-day for longs, recent_session_high for shorts).
  * TARGET: the next real level (recent_session_high for longs, low for shorts).
  * STOP: just past the level that proves you wrong (below recent_session_low for
    longs, above the high for shorts).
  * MANAGE IT our way: take half into the first target, trail the rest, cut at the
    stop. Stock/ETF -> frame as calls or puts (near the money). Forex/crypto ->
    long or short the spot.
- HONESTY still holds: quote ONLY the numbers macro_read returned, never invent a
  level, and never claim a win rate for THAT name (our backtested WR is on the
  core four; for others it's the same METHOD, not a measured number — say that in
  one line if it comes up). If event_warning is set, LEAD with "wait for it."
  Don't promise profit. A short "Your call." at the end is fine, but the PLAN must
  be concrete and decisive — direction, entry, target, stop — not a hedge.
- If macro_read returns an "error", the symbol couldn't be found: say so and ask
  them to double-check the ticker.

Request intake — the upgrade backlog (one of your main jobs):
- Chudi, Kelechi, and Ryan are the TRUSTED requesters. LIVE BOT STATE tells
  you if THIS chat is one of them. When a trusted requester asks the bot to
  do, add, change, or fix something, OR asks for something the bot can't do
  yet, log it with log_request so the bot keeps getting better. Buckets:
  - can_do_now: you already handled it (a setting, a lookup, scorekeeping) or
    it's clearly within the bot's powers. Do it, then log it as can_do_now.
  - needs_boss: a real feature/change/new market a human must build or approve
    (e.g. add forex or gold, a new alert type, a behavior change). Tell them
    "that's buildable, flagging it for the boss," then log it. Chudi is pinged.
  - cannot: out of scope or against the rules (placing orders, guaranteeing
    profit, anything dishonest). Tell them straight you can't, then log it so
    we still see what people keep wanting.
- ALWAYS tell the requester the verdict in plain language, and answer their
  question to the best of your ability in the same breath. Pass asked_by with
  their name (Chudi/Kelechi/Ryan) when you know it.
- Do NOT log: trade results (use log_trade_result), plain chat, hype, or a
  question you fully answered with no gap. Only real, actionable signal.
- If the asker is NOT a trusted requester, chat normally and do NOT log.

Hard rules:
- NEVER invent statistics or prices. Only quote numbers from the LIVE BOT
  STATE block, the tools, or what the user sent. Missing a number? Say so.
- Never promise profits. When a question is really a trading decision,
  give your honest read and end with: Your call.
- Plain language a 6th grader could read. Short, Telegram-sized answers —
  a few sentences unless they ask for detail.
- Chart screenshots: describe what you actually see (trend, levels,
  candles) and connect it to the bot's strategy: 15-minute momentum turns,
  morning entry window 9:45-10:30 ET, sell half +25%, then let the runner run
  and sell it when it gives back ~40 points from its peak, -70% stop.
- Member commands: /setaccount /risk /status /score /calls /test /help.
  /calls [ticker] shows the live call/put setup per stock (BUY type, strike,
  expiry, win rate). Owner-only request controls: /requests /approve /reject
  /done /reqfrom /backlog. Point to them when relevant."""

TOOLS = [
    {
        "name": "log_trade_result",
        "description": ("Record a trade result the user reports — their real "
                        "fill, win or loss, in dollars. Use whenever they say "
                        "or show how a trade went."),
        "input_schema": {
            "type": "object",
            "properties": {
                "profit_dollars": {"type": "number",
                                   "description": "profit (positive) or loss (negative), dollars"},
                "ticker": {"type": "string",
                           "description": "ticker if known, e.g. SPX or QCOM"},
                "note": {"type": "string",
                         "description": "short note, e.g. 'call, sold half at +25'"},
                "date": {"type": "string",
                         "description": "YYYY-MM-DD if they said when; omit for today"},
            },
            "required": ["profit_dollars"],
        },
    },
    {
        "name": "get_score",
        "description": ("The user's running personal record: wins, losses, "
                        "total P&L from everything they've logged."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "market_now",
        "description": ("Live read on a ticker RIGHT NOW: price, today's move, "
                        "15-min momentum, and whether a setup is triggering. "
                        "Use for 'what's happening', 'any setups now'."),
        "input_schema": {"type": "object", "properties": {
            "ticker": {"type": "string", "description": "SPX, SPY, QCOM or TSLA"}}},
    },
    {
        "name": "analyze_day",
        "description": ("Replay one past trading day for one ticker through "
                        "the real strategy: did a setup trigger, would the bot "
                        "have alerted it, how would the trade have gone. Use for "
                        "'what would have worked Friday', 'break down yesterday'."),
        "input_schema": {"type": "object", "properties": {
            "ticker": {"type": "string", "description": "SPX, SPY, QCOM or TSLA"},
            "date": {"type": "string",
                     "description": "the day as YYYY-MM-DD; omit for the last session"}}},
    },
    {
        "name": "request_new_ticker",
        "description": ("Flag a stock/ETF the bot is NOT equipped for (anything "
                        "other than SPX, SPY, QCOM, TSLA) that the user wants added. "
                        "Pings the owner to approve. Call ONLY after the user "
                        "says they'd like it added."),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "the symbol, e.g. NVDA, SPY"},
                "asked_by": {"type": "string",
                             "description": "the requester's name if you know it, else empty"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "macro_read",
        "description": ("Live market data for ANY symbol — a STOCK or ETF (AAPL, "
                        "NVDA, QQQ, KO...), a FOREX pair (EUR/USD, USD/JPY...), "
                        "GOLD, or CRYPTO (BTC, ETH, SOL...): price, day move, "
                        "15-min momentum, recent high/low, 20-day trend, a "
                        "momentum 'bias', and any high-impact news coming. Call "
                        "this WHENEVER someone asks about a symbol or wants a "
                        "trade idea / plan on one (e.g. 'calls or puts on AAPL', "
                        "'is BTC a buy'). You build the strategy plan from what it "
                        "returns. Returns an error only if the symbol isn't found."),
        "input_schema": {"type": "object", "properties": {
            "symbol": {"type": "string",
                       "description": "e.g. AAPL, NVDA, KO, EUR/USD, USD/JPY, gold, BTC, ETH"}},
            "required": ["symbol"]},
    },
    {
        "name": "log_request",
        "description": ("Record an actionable ask from a TRUSTED requester "
                        "(Chudi, Kelechi, or Ryan) into the upgrade backlog so "
                        "the bot keeps getting better. Pick the bucket: "
                        "'can_do_now' if you already handled it or it's within "
                        "the bot's powers; 'needs_boss' if it's a real "
                        "feature/change/new market a human must build or "
                        "approve; 'cannot' if it's out of scope or against the "
                        "rules. Do NOT call for trade logs, plain chat, or a "
                        "question you already fully answered."),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string",
                            "description": "one line: what they want, in their words"},
                "bucket": {"type": "string",
                           "enum": ["can_do_now", "needs_boss", "cannot"],
                           "description": "can_do_now | needs_boss | cannot"},
                "category": {"type": "string",
                             "description": "feature | bug | feedback | data | ticker | other"},
                "asked_by": {"type": "string",
                             "description": "the requester's name if known: Chudi, Kelechi, or Ryan"},
            },
            "required": ["summary", "bucket"],
        },
    },
]


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def model() -> str:
    return os.environ.get("BOT_BRAIN_MODEL", "claude-sonnet-4-6").strip()


def _load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_turn(chat_id: str, user_text: str, reply: str):
    # the news-watcher thread calls respond() with chat_id 'newsdesk' for one-off
    # headline reads — not a real conversation, so don't persist it (this also
    # keeps that thread off chat_history.json entirely). The lock + atomic
    # temp-replace stop a concurrent member chat from tearing/clobbering the file.
    if chat_id == "newsdesk":
        return
    with _HISTORY_LOCK:
        hist = _load_history()
        turns = hist.get(chat_id, [])
        turns += [{"role": "user", "content": user_text},
                  {"role": "assistant", "content": reply}]
        hist[chat_id] = turns[-MAX_TURNS * 2:]
        tmp = HISTORY_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(hist, indent=1), encoding="utf-8")
        tmp.replace(HISTORY_FILE)


def _load_trades() -> dict:
    if TRADES_FILE.exists():
        try:
            return json.loads(TRADES_FILE.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def log_trade(chat_id: str, profit_dollars: float, ticker: str = "",
              note: str = "", date_str: str = None) -> dict:
    """Append one user-reported trade result to their personal ledger."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _zi
    entry = {  # ET date, so a 9 PM log doesn't land on tomorrow in the cloud
        "date": date_str or str(_dt.now(_zi("America/New_York")).date()),
        "profit_dollars": round(float(profit_dollars), 2),
        "ticker": (ticker or "").upper(),
        "note": note or "",
    }
    trades = _load_trades()
    trades.setdefault(chat_id, []).append(entry)
    TRADES_FILE.write_text(json.dumps(trades, indent=1), encoding="utf-8")
    return entry


def score(chat_id: str) -> dict:
    """Running W:L record + total P&L from everything this user logged."""
    entries = _load_trades().get(chat_id, [])
    wins = [e for e in entries if e["profit_dollars"] > 0]
    losses = [e for e in entries if e["profit_dollars"] <= 0]
    return {
        "entries": len(entries),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(entries) * 100) if entries else 0,
        "total_dollars": round(sum(e["profit_dollars"] for e in entries), 2),
        "last": entries[-1] if entries else None,
    }


def score_line(chat_id: str) -> str:
    s = score(chat_id)
    if not s["entries"]:
        return ("No trades logged yet — just tell me how one went "
                "(\"made $500 on SPX\") or send a P&L screenshot and "
                "I'll keep score.")
    return (f"📊 YOUR RECORD: {s['wins']}W - {s['losses']}L "
            f"({s['win_rate_pct']}%) — total {s['total_dollars']:+,.0f} "
            f"dollars across {s['entries']} logged trades.")


SUPPORTED_TICKERS = ("SPX", "SPY", "QCOM", "TSLA")


def _request_ticker(ticker: str, chat_id: str, asked_by: str = "") -> str:
    """Flag an unsupported ticker the user wants added; ping the owner ONLY."""
    ticker = (ticker or "").upper().strip().lstrip("$")
    if not ticker:
        return json.dumps({"error": "no ticker given"})
    who = (asked_by or "").strip() or f"chat {chat_id}"
    pending = config.state_get("pending_tickers", {})
    already = ticker in pending
    pending[ticker] = {"asked_by": who, "chat_id": str(chat_id)}
    config.state_set("pending_tickers", pending)
    owner = telegram.primary_owner_id()
    pinged = False
    if owner:
        err = telegram.send_to(
            owner,
            f"🆕 TICKER REQUEST\n{who} asked about {ticker}, which the bot "
            f"isn't set up for yet.\nWant to add {ticker} to the watchlist? "
            "Reply if you'd like to proceed.")
        pinged = err is None
    # mirror trusted requesters' ticker asks onto the upgrade backlog too, so
    # the daily digest shows them. Only the FIRST time per ticker ('already'
    # dedups repeats, same as the owner ping), and use the real requester name
    # (who_label) not the 'chat <id>' fallback built for the owner DM. ping=False
    # because request_new_ticker already pinged the owner above.
    if intake.is_requester(chat_id) and not already:
        try:
            intake.add_request(chat_id, intake.who_label(chat_id),
                               f"add {ticker} to the watchlist",
                               "needs_boss", "ticker", ping=False)
        except Exception:
            pass
    return json.dumps({"flagged": ticker, "owner_pinged": pinged,
                       "already_pending": already})


def _log_request(summary: str, chat_id: str, bucket: str = "needs_boss",
                 category: str = "other", asked_by: str = "") -> str:
    """Record a trusted requester's ask in the upgrade backlog (intake.py).
    Only Chudi, Kelechi, and Ryan are logged for now; everyone else is
    answered normally but never queued."""
    summary = (summary or "").strip()
    if not summary:
        return json.dumps({"error": "nothing to log"})
    if not intake.is_requester(chat_id):
        return json.dumps({"skipped": "not a trusted requester; not logged"})
    who = (asked_by or "").strip() or intake.who_label(chat_id)
    entry = intake.add_request(chat_id, who, summary, bucket, category)
    return json.dumps({"logged": {"id": entry["id"], "bucket": entry["bucket"],
                                  "status": entry["status"]}})


def _run_tool(name: str, args: dict, chat_id: str) -> str:
    try:
        if name == "log_trade_result":
            entry = log_trade(chat_id, args["profit_dollars"],
                              args.get("ticker", ""), args.get("note", ""),
                              args.get("date"))
            return json.dumps({"logged": entry, "score": score(chat_id)})
        if name == "get_score":
            return json.dumps(score(chat_id))
        if name == "request_new_ticker":
            return _request_ticker(args.get("ticker", ""), chat_id,
                                   args.get("asked_by", ""))
        if name == "log_request":
            return _log_request(args.get("summary", ""), chat_id,
                                args.get("bucket", "needs_boss"),
                                args.get("category", "other"),
                                args.get("asked_by", ""))
        if name in ("market_now", "analyze_day"):
            import market_tools
            if name == "market_now":
                return json.dumps(market_tools.market_now(args.get("ticker", "SPX")))
            return json.dumps(market_tools.analyze_day(
                args.get("ticker", "SPX"), args.get("date")))
        if name == "macro_read":
            import market_tools
            return json.dumps(market_tools.read_any(args.get("symbol", "gold")))
    except Exception as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"error": f"unknown tool {name}"})


def _file_blocks(item: dict):
    """Turn a photo/document into model content blocks.
    Returns (blocks, error_message)."""
    data = telegram.download_file(item["file_id"])
    if data is None:
        return None, ("I couldn't download that file — it may be over 10MB "
                      "or Telegram hiccuped. Try again or send a smaller one.")
    mime = item.get("mime", "")
    name = item.get("file_name", "photo")
    if item["kind"] == "photo" or mime in IMAGE_TYPES:
        return [{"type": "image",
                 "source": {"type": "base64",
                            "media_type": mime if mime in IMAGE_TYPES else "image/jpeg",
                            "data": base64.b64encode(data).decode()}}], None
    if mime == "application/pdf" or name.lower().endswith(".pdf"):
        return [{"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf",
                            "data": base64.b64encode(data).decode()}}], None
    if mime.startswith("text/") or name.lower().endswith(TEXTY_EXT):
        try:
            body = data.decode("utf-8", errors="replace")[:MAX_TEXT_FILE]
        except Exception:
            return None, "That file doesn't look readable as text."
        return [{"type": "text",
                 "text": f"[Contents of the file '{name}' the user sent:]\n{body}"}], None
    return None, (f"I can't read '{name}' ({mime or 'unknown type'}) yet — "
                  "send text, a photo, a PDF, or a CSV/TXT file.")


def respond(item: dict, context_text: str, tools_enabled: bool = True) -> str:
    """Answer one message (text/photo/document) from an authorized chat."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _zi
    now_et = _dt.now(_zi("America/New_York"))
    chat_id = item["chat_id"]
    who_line = ("This chat IS the owner (Chudi) himself."
                if telegram.is_owner(chat_id)
                else "This chat is a MEMBER, not the owner.")
    if intake.is_requester(chat_id):
        req_line = (f"This chat is a TRUSTED REQUESTER ({intake.who_label(chat_id)})"
                    " — log their actionable asks with log_request.")
    else:
        req_line = ("This chat is NOT a trusted requester — chat normally, do "
                    "NOT log requests.")
    context_text = (f"Right now it is {now_et:%A %Y-%m-%d %I:%M %p} ET.\n"
                    f"{who_line}\n{req_line}\n"
                    + context_text)
    blocks = []
    if item["kind"] in ("photo", "document"):
        file_blocks, err = _file_blocks(item)
        if err:
            return err
        blocks += file_blocks
    user_text = item.get("text", "").strip()
    if not user_text:
        user_text = ("What do you see here, and how does it relate to our "
                     "trading?" if blocks else "Hello")
    blocks.append({"type": "text", "text": user_text})

    history = _load_history().get(chat_id, [])
    messages = history + [{"role": "user", "content": blocks}]
    reply = ""
    try:
        for _ in range(5):  # room for tool calls (data lookups, scorekeeping)
            payload = {"model": model(), "max_tokens": 800,
                       "system": SYSTEM + "\n\nLIVE BOT STATE:\n" + context_text,
                       "messages": messages}
            if tools_enabled:
                payload["tools"] = TOOLS
            r = requests.post(
                API_URL,
                headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                         "anthropic-version": "2023-06-01"},
                json=payload, timeout=120)
            body = r.json()
            if r.status_code != 200:
                err = body.get("error", {}).get("message", r.text[:200])
                return f"My brain hit an error: {err}"
            content = body.get("content", [])
            if body.get("stop_reason") == "tool_use":
                messages.append({"role": "assistant", "content": content})
                results = [{"type": "tool_result", "tool_use_id": b["id"],
                            "content": _run_tool(b["name"], b.get("input", {}),
                                                 chat_id)}
                           for b in content if b.get("type") == "tool_use"]
                messages.append({"role": "user", "content": results})
                continue
            reply = "".join(b.get("text", "") for b in content
                            if b.get("type") == "text").strip()
            break
    except requests.RequestException as e:
        return f"My brain couldn't connect: {e}"
    if not reply:
        return "I read it but came back empty — try rephrasing?"
    # history stores text only (never base64 blobs)
    label = user_text
    if item["kind"] == "photo":
        label = f"[sent a photo] {user_text}"
    elif item["kind"] == "document":
        label = f"[sent file {item.get('file_name', '?')}] {user_text}"
    try:
        _save_turn(chat_id, label, reply)
    except OSError:
        pass
    return reply
