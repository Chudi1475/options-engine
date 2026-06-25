"""Live alert service — entry signals, all-day position monitoring, exit
alerts, Telegram commands, risk gate, weekly scoreboard.
NEVER places orders. Every alert is a suggestion ending "Your call."

What runs when (all times ET):
    9:45-10:30   entry window — detect_setup() fires the entry cards
    9:45-16:00   every open position is checked each cycle:
                 SELL HALF at +25% -> let the runner run (give-back trail) -> stop
    15:45        "close before expiry" warning for anything expiring today
    16:00        expiring positions are settled for the scoreboard
    Fri 16:05    weekly scoreboard report
Telegram commands (/setaccount /risk /status /test /help) are answered
every cycle while running, and around the clock in --daemon mode.

Usage:
    python scanner.py            # one trading session (Task Scheduler mode)
    python scanner.py --daemon   # run forever (cloud mode)
    python scanner.py --dry-run  # print cards instead of texting (separate book)
    python scanner.py --setup    # print chat IDs of people who messaged the bot
    python scanner.py --test     # fire a fake signal through all 5 alert types
    python scanner.py --weekly   # send the weekly scoreboard now
"""

import argparse
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # emoji on Windows console

import threading
import time as time_mod
from datetime import datetime, time
from zoneinfo import ZoneInfo

import yfinance as yf

import cards
import config
import news
import positions as poslib
import quotes
import risk_gate
import scoreboard
import telegram
from backtest import expiry_for, realized_vol
from data_feed import DataFeed
from positions import Position, PositionBook
from strategy import Setup, StrategyConfig, detect_setup

ET = ZoneInfo("America/New_York")
SESSION_END = time(16, 12)      # loop exits after settle + weekly are done
MONITOR_START = time(9, 45)
WEEKLY_AT = time(16, 5)


def et_now() -> datetime:
    return datetime.now(ET)


def keep_awake(on: bool):
    """Stop the PC from sleeping mid-session (Windows only; no-op anywhere
    else, so this stays cloud-safe). Released when the session ends."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def log_alert(text: str, sent_errors):
    stamp = et_now().strftime("%Y-%m-%d %H:%M:%S %Z")
    status = "SENT" if not sent_errors else f"ERRORS: {sent_errors}"
    with config.ALERTS_LOG.open("a", encoding="utf-8") as f:
        f.write(f"--- {stamp} [{status}]\n{text}\n\n")


def record_alert(setup, now: datetime, stats):
    """Structured line for the daily recap (legacy format, kept compatible)."""
    rec = {
        "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"),
        "ticker": setup.ticker, "direction": setup.direction,
        "strike": setup.strike, "spot": setup.spot, "mom_pct": setup.mom_pct,
        "win_rate": stats.get("win_rate") if stats else None,
    }
    with config.ALERTS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


class Service:
    def __init__(self, dry_run: bool = False):
        self.dry = dry_run
        self.cfg = StrategyConfig()
        self.feed = DataFeed()
        book_path = (config.DATA_DIR / "positions_dryrun.json") if dry_run else None
        self.book = PositionBook(book_path)
        self.backtest_old = scoreboard.load_report("backtest_results.json")
        self.backtest_new = scoreboard.load_report("backtest_new_rules.json")
        self.old_bracket = (self.backtest_old or {}).get(
            "bracket", {"target_pct": 15, "stop_pct": -60})
        self.day = None
        self.skipped_today = set()
        self.daily_closes = {}
        self._sigma_retry = {}
        self._bars_cache = {}  # one bars download per ticker per cycle
        self.mode, self.mode_reason = "green", ""
        self.morning_sent_for = None
        self.premarket_sent_for = None
        self.heartbeat = 0
        self._last_feed_ok = None       # last time a bars fetch returned data
        self._health_last_stamp = 0.0   # monotonic; throttles the alive-stamp
        self._feed_warned = False       # in-memory: feed-stale DM already sent
        self._feed_none_warned = False  # in-memory: no-data-yet DM already sent

    # ---------- plumbing ----------

    def notify(self, text: str) -> list:
        if self.dry:
            print(f"\n{text}\n")
            log_alert(text, ["dry-run, not sent"])
            return []
        try:
            errors = telegram.send(text)
        except RuntimeError as e:  # e.g. no chat IDs configured — fail LOUD and
            print(f"send failed ({e}) — queueing for retry")  # queue, don't drop
            errors = [str(e)]
        log_alert(text, errors)
        if errors:
            # a network blip must not eat a STOP text — queue it for retry.
            # (Retries go to all chats, so a partial failure can duplicate a
            # message for whoever already got it. Duplicate beats missing.)
            # Atomic append: the news thread and main loop both enqueue here, so
            # a plain get-then-set would let one thread's queued alert clobber
            # the other's during an outage.
            config.state_update(
                "pending_sends",
                lambda cur: ((cur or []) + [{"text": text, "tries": 0}])[-50:],
                default=[])
            print(f"send failed, queued for retry: {errors}")
        return errors

    MAX_SEND_RETRIES = 6

    def flush_pending(self):
        """Retry alerts that failed to send on an earlier cycle. A permanently
        failing recipient (e.g. someone blocked the bot -> HTTP 403) must NOT
        turn one alert into an endless duplicate storm to everyone else, so each
        queued message is dropped after MAX_SEND_RETRIES attempts."""
        if self.dry:
            return
        pending = config.state_get("pending_sends", [])
        if not pending:
            return
        n = len(pending)
        keep = []  # originals still failing and under the retry cap
        for item in pending:
            if isinstance(item, str):  # migrate legacy string-only entries
                item = {"text": item, "tries": 0}
            if telegram.send("(retry) " + item["text"]):
                item["tries"] = item.get("tries", 0) + 1
                if item["tries"] < self.MAX_SEND_RETRIES:
                    keep.append(item)
                else:
                    print(f"dropping undeliverable alert after "
                          f"{self.MAX_SEND_RETRIES} tries: {item['text'][:60]}")
        # Atomically replace ONLY the items we just processed (the first n).
        # Anything the news thread enqueued meanwhile is at cur[n:] and is kept,
        # so a concurrent enqueue is never overwritten/lost.
        def _merge(cur):
            cur = cur or []
            extra = cur[n:] if len(cur) >= n else []
            return (keep + extra)[-50:]
        config.state_update("pending_sends", _merge, default=[])

    def reset_day(self, now: datetime):
        if self.day != now.date():
            self.day = now.date()
            self.skipped_today = set()
            self.daily_closes = {}

    def sigma(self, ticker: str) -> float:
        """Realized vol for estimates. Never raises — a throttled download
        returns 0.0 (estimate falls back to intrinsic) and is retried after
        5 minutes instead of hammering Yahoo every cycle."""
        if self.daily_closes.get(ticker) is None:
            if time_mod.time() < self._sigma_retry.get(ticker, 0):
                return 0.0
            try:
                yfs = self.cfg.watchlist.get(ticker, ticker)
                d1 = yf.download(yfs, period="1y", interval="1d",
                                 progress=False, auto_adjust=False)
                if hasattr(d1.columns, "levels"):
                    d1.columns = d1.columns.get_level_values(0)
                closes = d1["Close"]
                if closes.empty:
                    raise ValueError("no daily data")
                self.daily_closes[ticker] = closes
            except Exception as e:
                print(f"{ticker}: daily download failed ({e}) — retry in 5 min")
                self.daily_closes[ticker] = None
                self._sigma_retry[ticker] = time_mod.time() + 300
                return 0.0
        return realized_vol(self.daily_closes[ticker], self.day)

    def get_bars(self, yfs: str, now: datetime):
        """Today's completed 5m bars, downloaded at most once per cycle even
        when entry scan and position monitoring both need the same ticker."""
        hit = self._bars_cache.get(yfs)
        if hit and (now - hit[0]).total_seconds() < config.POLL_SECONDS - 2:
            bars = hit[1]
        else:
            bars = self.feed.today_bars(yfs, now)
            self._bars_cache[yfs] = (now, bars)
        if bars is not None and not bars.empty:
            self._last_feed_ok = now  # feeds the self-heartbeat's feed-dead check
        return bars

    def current_mode(self):
        """Manual /risk override (today only) beats the automatic morning mode."""
        override = config.state_get("risk_override")
        if override and override.get("date") == str(et_now().date()):
            return override["mode"], (override.get("reason") or "manual override")
        return self.mode, self.mode_reason

    # ---------- self-heartbeat (owner-only health alerts) ----------
    # The bot watches itself and DMs the OWNER (never members) the moment it
    # goes silent, so a quiet failure is never mistaken for a quiet market.
    # Pure monitoring — it never touches entries, exits, sizing, or alerts.

    FEED_STALE_MIN = 10   # warn if no data fetch succeeds for this long in-session
    DOWNTIME_MIN = 10     # warn on restart if silently down this long in-session

    def _hb_owner(self, text: str):
        """DM the OWNER only (never members) with an ops/health note."""
        owner = telegram.primary_owner_id()
        if not owner or self.dry:
            return
        try:
            telegram.send_to(owner, text)
        except Exception as e:
            print(f"heartbeat DM failed: {e}")

    def _hb_warned_once(self, key: str, day: str) -> bool:
        """True if this warning already fired today; else mark it and return
        False. Persisted, so a restart can't re-spam the same warning."""
        if key in (config.state_get("hb_warned", {}) or {}).get(day, []):
            return True

        def upd(w):
            w = w if isinstance(w, dict) else {}
            fired = list(w.get(day, []))
            if key not in fired:
                fired.append(key)
            return {day: fired}  # keep only today's flags
        config.state_update("hb_warned", upd, default={})
        return False

    def health_stamp(self, now: datetime):
        """Throttled 'I'm alive' stamp to state.json (~once a minute)."""
        t = time_mod.monotonic()
        if t - self._health_last_stamp < 55:
            return
        self._health_last_stamp = t
        config.state_set("heartbeat", {"ts": now.isoformat(), "day": str(now.date())})

    def check_downtime_on_start(self, now: datetime):
        """On session start, if the last alive-stamp was earlier TODAY and the
        gap spans market hours, the bot was silently down — tell the owner once.
        A normal redeploy (a couple minutes) is under the threshold, so routine
        deploys stay quiet."""
        if self.dry or now.weekday() >= 5 or now.time() < MONITOR_START:
            return
        hb = config.state_get("heartbeat")
        if not hb or hb.get("day") != str(now.date()):
            return
        try:
            last = datetime.fromisoformat(hb["ts"])
        except (ValueError, KeyError, TypeError):
            return
        gap = (now - last).total_seconds() / 60
        if gap >= self.DOWNTIME_MIN and last.time() <= time(16, 0):
            self._hb_owner(
                f"⚠️ Heartbeat: I was down ~{gap:.0f} min "
                f"({last:%I:%M}–{now:%I:%M %p} ET) during market hours. "
                "Back up now — check for any missed alerts.")

    def health_check(self, now: datetime):
        """Once-a-cycle, in-session health checks. Owner-only DMs."""
        if self.dry:
            return
        self.health_stamp(now)
        if now.weekday() >= 5 or not (MONITOR_START <= now.time() <= time(16, 0)):
            return
        last_ok = self._last_feed_ok
        if last_ok is None:  # no data has loaded at all this session
            if now.time() >= time(10, 0) and not self._feed_none_warned:
                self._feed_none_warned = True
                self._hb_owner(
                    "⚠️ Heartbeat: no market data has loaded yet this session — "
                    "the feed may be down. No setups can fire until it recovers.")
            return
        self._feed_none_warned = False
        stale = (now - last_ok).total_seconds() / 60
        if stale >= self.FEED_STALE_MIN and not self._feed_warned:
            self._feed_warned = True
            self._hb_owner(
                f"⚠️ Heartbeat: the data feed has returned nothing for ~{stale:.0f} "
                "min during market hours. Setups/exits may be stalled — check it.")
        elif stale < self.FEED_STALE_MIN and self._feed_warned:
            self._feed_warned = False
            self._hb_owner("✅ Heartbeat: data feed recovered.")

    def health_eod(self, now: datetime):
        """One owner-only end-of-session 'all clear', so silence is meaningful:
        no close ping = something is wrong."""
        if self.dry or now.weekday() >= 5 or now.time() < WEEKLY_AT:
            return
        today = str(now.date())
        if self._hb_warned_once("eod", today):
            return
        n = len(self.book.for_date(now.date()))
        feed = (f"OK (last {self._last_feed_ok:%I:%M %p})"
                if self._last_feed_ok else "NO DATA seen")
        warns = [k for k in (config.state_get("hb_warned", {}) or {}).get(today, [])
                 if k != "eod"]
        tail = ("  Heads-up today: " + ", ".join(warns)) if warns else ""
        self._hb_owner(
            f"✅ Heartbeat: session done. {n} alert{'s' if n != 1 else ''} sent, "
            f"feed {feed}.{tail}  (Once-a-day check — no close ping from me means "
            "something's wrong.)")

    def health_text(self) -> str:
        """/health — on-demand snapshot for the owner."""
        now = et_now()
        hb = config.state_get("heartbeat") or {}
        last_ok = self._last_feed_ok
        today = str(now.date())
        open_n = sum(1 for p in self.book.positions if p.state != "closed")
        warns = [w for w in (config.state_get("hb_warned", {}) or {}).get(today, [])
                 if w != "eod"]
        lines = [
            "🩺 BOT HEALTH",
            f"Now: {now:%a %I:%M %p ET}",
            f"Last heartbeat: {hb.get('ts', 'n/a')}",
            (f"Feed last OK: {last_ok:%I:%M %p ET}" if last_ok
             else "Feed last OK: not yet this run"),
            f"Morning card today: "
            f"{'sent' if config.state_get('morning_sent') == today else 'NOT sent'}",
            f"Alerts today: {len(self.book.for_date(now.date()))}",
            f"Open positions: {open_n}",
        ]
        if warns:
            lines.append("Warnings today: " + ", ".join(warns))
        return "\n".join(lines)

    # ---------- morning ----------

    def morning_report(self, now: datetime, include_gap: bool = True,
                       premarket: bool = False):
        today = now.date()
        cached = config.state_get("risk_auto")
        # survives restarts: don't re-text the morning card after a reboot
        if (self.morning_sent_for != today
                and config.state_get("morning_sent") == str(today)):
            self.morning_sent_for = today
            # also restore the already-ANNOUNCED mode, so `prev` below isn't the
            # default 'green' — otherwise a restart on a red/yellow day fires a
            # bogus "UPDATE — RED" escalation for a mode that was already sent
            if cached and cached.get("date") == str(today):
                self.mode, self.mode_reason = cached["mode"], cached["reason"]
        if cached and cached.get("date") == str(today) \
                and bool(cached.get("gap", False)) >= include_gap:
            mode, reason = cached["mode"], cached["reason"]
        else:
            mode, reason = risk_gate.risk_mode(include_gap=include_gap)
            config.state_set("risk_auto", {"date": str(today), "mode": mode,
                                           "reason": reason, "gap": include_gap})
        prev = self.mode if self.morning_sent_for == today else None
        self.mode, self.mode_reason = mode, reason
        if premarket:
            if self.premarket_sent_for == today:
                return
            self.premarket_sent_for = today
        if self.morning_sent_for == today:
            # already reported; only speak again if the day got riskier
            if prev is not None and risk_gate.SEVERITY[mode] > risk_gate.SEVERITY[prev]:
                self.notify("UPDATE — " + cards.morning_card(mode, reason, today))
            return
        self.morning_sent_for = today
        card = cards.morning_card(mode, reason, today)
        try:  # earnings radar + hot headlines (news must never block the report)
            extra = news.morning_lines(self.cfg.watchlist)
            if extra:
                card += "\n" + "\n".join(extra)
        except Exception as e:
            print(f"news scan failed: {e}")
        if config.paper_mode():
            card += "\n[PAPER MODE is ON — cards are practice, not trades.]"
        self.notify(card)
        if not self.dry:  # dry-run must not set the live morning dedup key
            config.state_set("morning_sent", str(today))
        # if the card went out AFTER the entry window opened, we came up late
        # (likely a slow restart) — tell the owner so a missed open isn't silent.
        if (not self.dry and now.weekday() < 5 and now.time() > self.cfg.entry_start
                and not self._hb_warned_once("late_open", str(today))):
            self._hb_owner(
                f"⚠️ Heartbeat: morning card went out at {now:%I:%M %p} ET, after "
                f"the {self.cfg.entry_start:%H:%M} entry window opened — I may have "
                "missed early setups today.")

    # ---------- telegram commands ----------

    def handle_commands(self, timeout: int = 0):
        if self.dry:
            return
        try:
            items, max_id = telegram.get_messages(timeout=timeout)
        except RuntimeError:
            return
        for item in items:
            try:
                reply = self.handle_item(item)
            except Exception as e:
                reply = f"That message hit an error: {e}"
            if reply:
                telegram.send_to(item["chat_id"], reply)
        telegram.ack_offset(max_id)  # after processing: crash = safe replay

    def handle_item(self, item: dict):
        """Commands run as commands; everything else (plain text, photos,
        files) goes to the bot's brain — or an honest hint if no AI key."""
        if item["kind"] == "unknown":
            return self.offer_add_user(item)
        if item["kind"] == "command":
            if item["cmd"] == "/score":
                import assistant
                return assistant.score_line(item["chat_id"])
            return self.run_command(item["cmd"], item["args"], item["chat_id"])
        if item["kind"] == "unsupported":
            return ("I can read text, photos, PDFs and CSV/TXT files — "
                    "not voice or video yet.")
        import assistant
        if not assistant.enabled():
            return ("I see your message, but my brain isn't plugged in yet. "
                    "Add ANTHROPIC_API_KEY to the bot's .env "
                    "(get one at console.anthropic.com) and I'll answer "
                    "questions, read chart screenshots, and read files like "
                    "a human. Commands still work any time: /help")
        telegram.send_chat_action(item["chat_id"])  # 'typing…' so it feels live
        return assistant.respond(item, self.status_text())

    def offer_add_user(self, item: dict):
        """A stranger messaged the bot. Tell the owner(s) once, with a
        one-tap way to add them. Never reply to or act on the stranger."""
        cid, name = item["chat_id"], item.get("name", "someone")
        pending = config.state_get("pending_chats", {})
        if cid in pending or cid in telegram.chat_ids():
            return None  # already flagged or already a member
        pending[cid] = name
        config.state_set("pending_chats", pending)
        for owner in telegram.owner_ids():
            telegram.send_to(owner,
                f"👤 {name} (id {cid}) just messaged the bot.\n"
                f"Want them to get every alert and be able to ask questions?\n"
                f"Reply  /adduser {cid}  to let them in, or ignore to keep "
                "them out.\n"
                f"If it's Kelechi or Ryan, use  /reqfrom add {cid} <name>  so "
                "their asks hit the upgrade backlog too.")
        return None  # stranger gets nothing back

    ADMIN_CMDS = {"/adduser", "/removeuser", "/users", "/risk", "/setaccount",
                  "/test", "/health", "/requests", "/approve", "/reject",
                  "/done", "/reqfrom", "/backlog"}

    def run_command(self, cmd: str, args: str, chat_id: str = ""):
        if cmd in self.ADMIN_CMDS and not telegram.is_owner(chat_id):
            return ("That's an owner-only command. You can use /status, "
                    "/score, /help, or just talk to me.")
        if cmd == "/adduser":
            return self.cmd_adduser(args)
        if cmd == "/removeuser":
            return self.cmd_removeuser(args)
        if cmd == "/users":
            members = telegram.chat_ids()
            owners = set(telegram.owner_ids())
            lines = ["Who can use the bot:"]
            for c in members:
                lines.append(f"  {c}" + ("  (owner)" if c in owners else "  (added)"))
            return "\n".join(lines)
        if cmd == "/setaccount":
            raw = args.replace(",", "").replace("$", "").strip()
            try:
                val = float(raw)
                if val <= 0:
                    raise ValueError
            except ValueError:
                return "Usage: /setaccount 25000  (your account size in dollars)"
            config.state_set("account_value", val)
            alloc = config.suggested_alloc_pct(config.RISK_PER_TRADE_PCT)
            return (f"Account set: ${val:,.0f}.\n"
                    f"Full-size suggestion ≈ ${val * alloc / 100:,.0f} per trade "
                    f"(~{alloc:.1f}%), risking "
                    f"${val * config.RISK_PER_TRADE_PCT / 100:,.0f} "
                    f"({config.RISK_PER_TRADE_PCT:g}%) if stopped.")
        if cmd == "/risk":
            parts = args.split(None, 1)
            mode = parts[0].lower() if parts else ""
            if mode not in ("green", "yellow", "red"):
                return "Usage: /risk green|yellow|red [reason]"
            reason = parts[1] if len(parts) > 1 else "manual override"
            config.state_set("risk_override", {
                "date": str(et_now().date()), "mode": mode, "reason": reason})
            effect = {"green": "standard rules",
                      "yellow": "warning banner on every card",
                      "red": "sizes HALVED + high-risk warning"}[mode]
            return (f"{cards.MODE_EMOJI[mode]} Risk mode set to {mode.upper()} "
                    f"for today ({reason}). Effect: {effect}.")
        if cmd == "/status":
            return self.status_text()
        if cmd == "/health":
            return self.health_text()
        if cmd == "/test":
            self.test_sequence(chat_id)
            return None
        if cmd in ("/calls", "/opt", "/option", "/puts"):
            return self.calls_text(args)
        if cmd == "/requests":
            import intake
            return intake.list_text()
        if cmd == "/backlog":
            import intake
            return intake.backlog_md()
        if cmd in ("/approve", "/reject", "/done"):
            return self.cmd_request_status(cmd, args)
        if cmd == "/reqfrom":
            import intake
            return intake.reqfrom_command(args)
        if cmd in ("/help", "/start"):
            return cards.help_card()
        return None  # silently ignore unknown commands

    def calls_text(self, arg: str = ""):
        """/calls [ticker] — a compact, scannable read of the live call/put
        setup for each watched ticker, in STOCK -> BUY CALL/PUT -> strike ->
        expiry -> win-rate order. Real-time; only the 4 supported tickers."""
        import market_tools
        from backtest import expiry_for
        now = et_now()
        wl = list(self.cfg.watchlist)
        if arg.strip():
            t = arg.strip().upper().lstrip("$")
            if t not in self.cfg.watchlist:
                return (f"{t} isn't on the watchlist. I track {', '.join(wl)} "
                        "only — ask me to add it and I'll flag it for the boss.")
            tickers = [t]
        else:
            tickers = wl
        lines = ["📊 LIVE SETUPS · calls & puts (real-time read):"]
        for t in tickers:
            try:
                mn = market_tools.market_now(t)
                try:
                    exp = expiry_for(t, now)
                except Exception:
                    exp = None
                lines.append(cards.option_line(t, mn, exp))
            except Exception as e:
                lines.append(f"{t}: couldn't read right now ({e})")
        lines.append("")
        lines.append("BUY = the entry. Sells come as live exit texts. "
                     "/status shows open trades.")
        return "\n".join(lines)

    def cmd_request_status(self, cmd: str, args: str):
        """/approve|/reject|/done <id> [note] — move a request and tell the
        person who asked."""
        import intake
        parts = args.split(None, 1)
        if not parts or not parts[0].lstrip("#").isdigit():
            return f"Usage: {cmd} <id> [note]   (see open ones with /requests)"
        rid = int(parts[0].lstrip("#"))
        note = parts[1].strip() if len(parts) > 1 else ""
        status = {"/approve": "approved", "/reject": "rejected",
                  "/done": "done"}[cmd]
        entry, ok = intake.set_status(rid, status, note)
        if not ok:
            return f"No request #{rid}. Use /requests to see open ones."
        notified = intake.notify_asker(entry, status, note)
        return intake.confirm_line(entry, status, notified)

    def cmd_adduser(self, args: str):
        pending = config.state_get("pending_chats", {})
        target = args.strip() or (next(iter(pending), "") if pending else "")
        if not target:
            return ("Nobody's waiting to be added. Have them message the bot "
                    "first, then I'll text you their ID — or use "
                    "/adduser <their chat id>.")
        extra = [str(x) for x in config.state_get("extra_chat_ids", [])]
        if target in extra or target in telegram.owner_ids():
            return f"{target} already has access."
        extra.append(target)
        config.state_set("extra_chat_ids", extra)
        name = pending.pop(target, "your partner")
        config.state_set("pending_chats", pending)
        telegram.send_to(target,
            "You're in! 🎯 This bot texts options setups in the morning and "
            "walks you through the exits all day. Just talk to me like a "
            "person — ask anything, send a chart screenshot, or tell me how "
            "a trade went and I'll keep your record. Type /help to see more. "
            "Nothing here is auto-traded — every alert ends 'Your call.'")
        return f"✅ Added {name} (id {target}). They'll get every alert now."

    def cmd_removeuser(self, args: str):
        target = args.strip()
        if not target:
            return "Usage: /removeuser <chat id>. See IDs with /users."
        if target in telegram.owner_ids():
            return "Can't remove an owner."
        extra = [str(x) for x in config.state_get("extra_chat_ids", [])]
        if target not in extra:
            return f"{target} isn't on the added list."
        extra.remove(target)
        config.state_set("extra_chat_ids", extra)
        return f"Removed {target}. They won't get alerts anymore."

    def status_text(self) -> str:
        mode, reason = self.current_mode()
        acct = config.account_value()
        lines = [f"{cards.MODE_EMOJI[mode]} Risk mode: {mode.upper()} — {reason}",
                 f"Account: {'$' + format(acct, ',.0f') if acct else 'not set (/setaccount)'}",
                 f"Paper mode: {'ON' if config.paper_mode() else 'off'}",
                 f"Data: {self.feed.backend_for('QCOM')} for stocks, "
                 f"{self.feed.backend_for('^GSPC')} for SPX"]
        open_pos = [p for p in self.book.positions if p.state != "closed"]
        if open_pos:
            lines.append("Open positions:")
            for p in open_pos:
                pct = p.last_mark_pct if p.last_mark_pct is not None else 0.0
                lines.append(f"  {cards.contract_str(p)}: {pct:+.1f}% "
                             f"({p.state}, in since {p.time_et[:5]} ET)")
        else:
            lines.append("Open positions: none")
        return "\n".join(lines)

    # ---------- entries (signal logic UNCHANGED) ----------

    # explicit allow-list: ONLY these setups ever alert, so a backtest re-run
    # shifting the chosen bracket can never silently switch on a money-losing
    # put. Validated calls (incl. SPY, mirroring SPX) + the one probationary
    # put; every other put tested as a net loser.
    ALLOWED_SETUPS = {"SPX:call", "SPY:call", "QCOM:call", "TSLA:put"}

    def gate_stats(self, setup):
        """The eligibility filter: backtested win rate of 70+ (rounded the
        same way every card displays it — 69.77% IS the '70%' the user sees)
        AND positive expectancy under the EXITS WE ACTUALLY TRADE (the
        new-rules backtest when it exists, old-rules otherwise)."""
        if self.backtest_old is None:
            print("No backtest results — refusing to alert without real stats. "
                  "Run backtest.py.")
            return None
        key = f"{setup.ticker}:{setup.direction}"
        now = et_now()
        if key not in self.ALLOWED_SETUPS:
            print(f"{now:%H:%M:%S} {key}: not on the alert allow-list "
                  f"{sorted(self.ALLOWED_SETUPS)} — skipped.")
            return None
        stats = self.backtest_old.get("per_setup", {}).get(key)
        if stats is None:
            print(f"{now:%H:%M:%S} {setup.ticker} {setup.direction}: setup formed "
                  "but no backtest stats for it — skipped.")
            return None
        if round(stats["win_rate"]) < config.MIN_WINRATE:
            print(f"{now:%H:%M:%S} {setup.ticker} {setup.direction}: win rate "
                  f"{stats['win_rate']:.0f}% is below {config.MIN_WINRATE:.0f}% "
                  "— skipped, not forcing it.")
            return None
        new_stats = (self.backtest_new or {}).get("per_setup", {}).get(key)
        exp = (new_stats or stats)["expectancy_pct"]
        rules = "our exits" if new_stats else "the old exits"
        if exp <= 0:
            print(f"{now:%H:%M:%S} {setup.ticker} {setup.direction}: wins "
                  f"{stats['win_rate']:.0f}% of the time but LOSES money with "
                  f"{rules} in testing — skipped, not forcing it.")
            return None
        return stats

    def scan_entries(self, now: datetime):
        opened = self.book.opened_today(now.date())
        for ticker, yfs in self.cfg.watchlist.items():
            if ticker in self.skipped_today or ticker in opened:
                continue
            try:
                bars = self.get_bars(yfs, now)
                if bars is None or bars.empty:
                    continue
                setup = detect_setup(ticker, bars, now, self.cfg)
            except Exception as e:
                print(f"{now:%H:%M:%S} {ticker}: data error: {e}")
                continue
            if setup is None:
                continue
            # A setup whose DIRECTION isn't the one we trade for this ticker
            # (e.g. an early SPX:put on a weak open, before the tape turns up to
            # the SPX:call we actually trade) is NOT a decision for the day —
            # momentum routinely flips to the allowed side later in the window.
            # Just wait and re-check next cycle; do NOT burn the ticker.
            if f"{setup.ticker}:{setup.direction}" not in self.ALLOWED_SETUPS:
                continue
            if self.gate_stats(setup) is None:
                # allowed direction, but it failed the win-rate/expectancy bar:
                # THAT is final for the day.
                self.skipped_today.add(ticker)
                continue
            try:
                did_open = self.open_position(setup, now)
            except Exception as e:
                print(f"{now:%H:%M:%S} {ticker}: failed to open position: {e} "
                      "— will retry next cycle")
                continue  # transient failure must not burn the day's alert
            if did_open:
                self.skipped_today.add(ticker)

    def open_position(self, setup, now: datetime):
        right = "C" if setup.direction == "call" else "P"
        expiry_dt = expiry_for(setup.ticker, now)
        # holiday weeks move weeklies (Friday holiday -> Thursday expiry)
        expiry_date = quotes.nearest_listed_expiry(setup.ticker, expiry_dt.date())
        if expiry_date != expiry_dt.date():
            expiry_dt = expiry_dt.replace(year=expiry_date.year,
                                          month=expiry_date.month,
                                          day=expiry_date.day)

        # earnings inside the option's life = a coin flip on the report,
        # not the momentum pattern we backtested. Skip, say why in the log.
        try:
            blocked, e_date = news.earnings_inside(setup.ticker, expiry_date)
        except Exception:
            blocked, e_date = False, None
        if blocked:
            print(f"{now:%H:%M:%S} {setup.ticker} {setup.direction}: earnings "
                  f"{e_date} lands inside this option's life — skipped, "
                  "not gambling on a report.")
            return True  # final decision for the day
        quote = quotes.get_option_quote(setup.ticker, right, setup.strike, expiry_date)
        sigma = self.sigma(setup.ticker)
        est = quotes.estimate_premium(setup.spot, setup.strike, right,
                                      expiry_dt, now, sigma)
        # price preference: real bid/ask > fresh estimate > stale last trade
        if quote is not None and quote.bid > 0:
            entry_mid, entry_source = quote.mid, "quote"
            entry_bid, entry_ask = quote.bid, quote.ask
        elif est > 0:
            entry_mid, entry_source = est, "estimate"
            entry_bid = entry_ask = 0.0
        elif quote is not None and quote.mid > 0:
            entry_mid, entry_source = quote.mid, "quote"
            entry_bid = entry_ask = 0.0
        else:
            print(f"{now:%H:%M:%S} {setup.ticker}: no usable option price yet "
                  "— will retry next cycle.")
            return False

        risk = config.RISK_PER_TRADE_PCT
        correlated = self.book.open_same_direction(setup.direction)
        if correlated:
            risk = config.CORRELATED_RISK_PCT
        mode, mode_reason = self.current_mode()
        if mode == "red":
            risk = risk / 2

        display = scoreboard.stats_for_card(setup.ticker, setup.direction,
                                            self.book, self.backtest_old,
                                            self.backtest_new)
        pos = Position(
            id=f"{now:%Y%m%d-%H%M%S}-{setup.ticker}-{right}{setup.strike:g}",
            date=str(now.date()), time_et=now.strftime("%H:%M:%S"),
            ticker=setup.ticker, direction=setup.direction, right=right,
            strike=setup.strike, expiry=str(expiry_date),
            entry_mid=entry_mid, entry_source=entry_source,
            entry_bid=entry_bid, entry_ask=entry_ask,
            est_entry=est if est > 0 else 0.0,
            spot_at_signal=setup.spot, mom_pct=setup.mom_pct,
            risk_pct=risk, correlated=correlated, paper=config.paper_mode(),
            risk_mode=mode, stats_note=display["label"] if display else "",
            win_rate_quoted=display["win_rate"] if display else 0.0,
            ev_quoted=display["ev_pct"] if display else 0.0,
        )
        news_lines = []
        if setup.ticker != "SPX":
            try:
                news_lines = [f"⚠️ News today — {outlet}: {title}"
                              for outlet, title in news.hot_headlines(setup.ticker)[:2]]
            except Exception:
                pass
        card = cards.entry_card(setup, pos, quote, display, mode, mode_reason,
                                expiry_date, now.date(), news_lines=news_lines)
        # persist FIRST, then alert. A crash in between then leaves a tracked
        # (still-monitored) position that merely missed its entry card —
        # recoverable — instead of an alerted-but-untracked one that would
        # re-fire a duplicate BUY next cycle and never get exit alerts.
        self.book.add(pos)
        if not self.dry:  # dry-run must not poison the shared legacy-alert log
            record_alert(setup, now, display)
        errors = self.notify(card)  # sends the moment it's recorded
        print(f"{now:%H:%M:%S} alert sent: {setup.ticker} {setup.strike:g} "
              f"{setup.direction} entry ${entry_mid:.2f} ({entry_source})"
              + (f" errors: {errors}" if errors else ""))
        return True

    # ---------- monitoring ----------

    def monitor_positions(self, now: datetime):
        watch = self.book.needs_monitoring(now.date())
        for pos in watch:
            try:
                self.monitor_one(pos, now)
            except Exception as e:
                print(f"{now:%H:%M:%S} {pos.ticker}: monitor error: {e}")
        self.heartbeat += 1
        if watch and self.heartbeat % 40 == 0:  # ~every 10 minutes
            states = ", ".join(
                f"{p.ticker} {(p.last_mark_pct if p.last_mark_pct is not None else 0):+.1f}%"
                for p in watch)
            print(f"{now:%H:%M:%S} watching: {states}")

    def monitor_one(self, pos: Position, now: datetime):
        yfs = self.cfg.watchlist.get(pos.ticker, pos.ticker)
        bars = self.get_bars(yfs, now)
        last_close = (float(bars["Close"].iloc[-1])
                      if bars is not None and not bars.empty else None)
        spot = self.feed.latest_price(yfs)
        if spot is None:
            spot = last_close
        elif last_close and abs(spot / last_close - 1) > 0.10:
            spot = last_close  # garbage-tick guard: a 'live' price 10% away
                               # from the last completed bar is not believable
        if spot is None:
            return
        sigma = self.sigma(pos.ticker)
        expiry_dt = datetime.combine(pos.expires_on(), time(16, 0), tzinfo=ET)
        # sigma==0 means the vol download was throttled; a BS price with no vol
        # collapses to pure intrinsic (all time value stripped) and reads as a
        # huge phantom loss vs the entry estimate -> a FALSE stop. Treat it as
        # "no estimate this cycle" so est can't become the mark or trip the stop.
        est = (quotes.estimate_premium(spot, pos.strike, pos.right,
                                       expiry_dt, now, sigma) if sigma > 0 else 0.0)
        # the estimate-based stop floor compares model-to-model: the BS
        # estimate now vs the BS estimate AT ENTRY. (Estimate vs a real quote
        # mid would read -30% on day one just from the vol-model gap.)
        est_pct = ((est / pos.est_entry - 1) * 100
                   if est > 0 and pos.est_entry > 0 else None)
        quote = quotes.get_option_quote(pos.ticker, pos.right, pos.strike,
                                        pos.expires_on())
        # mark preference: real bid/ask > fresh estimate > stale last trade
        if quote is not None and quote.bid > 0:
            mark, source, usable = quote.mid, quote.source, True
        elif est > 0:
            mark, source, usable = est, "estimated from the stock move", True
        elif quote is not None and quote.mid > 0:
            mark, source, usable = quote.mid, quote.source, True
        else:
            # no fresh option price this cycle (0DTE chains routinely go bid-less
            # near the bell). Fall back to the last known price so the time-based
            # 'close before expiry' warning + 16:00 settle can still fire.
            mark = pos.last_mark if pos.last_mark is not None else pos.entry_mid
            source, usable = "last known price (no live quote)", False

        # Is this mark priced the same WAY as entry (quote-vs-quote / est-vs-est)?
        # A BS estimate over a real-quote entry (or vice versa) reads as a fake
        # ±30% and would fire a phantom exit. When the source TYPE differs (or we
        # only have a stale fallback), step() leans on the model-to-model est_pct
        # for the stop and refuses to trip the +25% half off the bad number.
        mark_is_est = source.startswith("estimat")
        entry_is_est = (pos.entry_source != "quote")
        comparable = usable and (mark_is_est == entry_is_est)

        near_expiry = (pos.expires_on() == now.date()
                       and now.time() >= poslib.WARN_T)
        # nothing trustworthy to act on (no comparable mark AND no model signal)
        # and not at the bell -> skip the cycle rather than act on a biased number
        if not comparable and est_pct is None and not near_expiry:
            return

        # runner exit is now a give-back trail (positions.step), not a momentum
        # flip, so we no longer need the 15-min momentum read here.
        events = poslib.step(pos, now, mark, source, est_pct, False,
                             self.old_bracket, comparable=comparable)
        builders = {"sell_half": cards.half_card, "runner_trail": cards.trail_card,
                    "stop": cards.stop_card, "expiry_warn": cards.expiry_card}
        # send/queue the exit alert BEFORE persisting the closed state: a crash
        # in between then re-evaluates next cycle (at worst a duplicate) instead
        # of recording 'closed' with the STOP/SELL text never sent.
        for ev in events:
            self.notify(builders[ev["type"]](pos, ev))
            print(f"{now:%H:%M:%S} {pos.ticker}: {ev['type']} at {ev['pct']:+.1f}%")
        self.book.save()

    # ---------- breaking news (own thread, instant-send) ----------

    def start_news_watch(self):
        """Spin up the breaking-news watcher in its OWN thread so it fires the
        instant a fresh headline lands instead of waiting on the ~15s trading
        loop, and so a slow AI 'read' never delays the alert or the next trade
        cycle. Safe to call repeatedly."""
        t = getattr(self, "_news_thread", None)
        if t and t.is_alive():
            return
        self._news_stop = threading.Event()
        self._news_thread = threading.Thread(
            target=self._news_worker, name="news-watcher", daemon=True)
        self._news_thread.start()

    def stop_news_watch(self):
        ev = getattr(self, "_news_stop", None)
        if ev:
            ev.set()

    def _news_worker(self):
        # tight loop: scan -> fire instantly -> sleep NEWS_POLL_SECONDS.
        # only runs while a session is live (run_session owns the lifecycle).
        while not self._news_stop.is_set():
            try:
                self._scan_news_once()
            except Exception as e:
                print(f"{et_now():%H:%M:%S} news watcher error (continuing): {e}")
            self._news_stop.wait(max(3, config.NEWS_POLL_SECONDS))

    def _load_news_seen(self):
        try:
            d = json.loads(config.NEWS_SEEN_FILE.read_text(encoding="utf-8-sig"))
            return d.get("seen", []), d.get("date")
        except (OSError, json.JSONDecodeError):
            return [], None

    def _save_news_seen(self, seen, day):
        try:  # only the news thread writes this file -> no cross-thread race
            config.NEWS_SEEN_FILE.write_text(
                json.dumps({"seen": seen, "date": day}), encoding="utf-8")
        except OSError:
            pass

    def _scan_news_once(self):
        """One pass: text the moment a NEW hot headline hits the wires.
        Headlines already on the wires at the day's first pass are seeded
        silently (the morning report covered those)."""
        now = et_now()
        ttl = max(5, config.NEWS_POLL_SECONDS - 2)  # refetch nearly every pass
        hot, all_ok = news.all_hot_healthy(self.cfg.watchlist, ttl=ttl)
        seen, seen_date = self._load_news_seen()
        first_seed = seen_date != str(now.date())
        fresh = [(o, t) for o, t in hot if t not in seen]
        if first_seed:  # day's first pass: seed ALL current headlines silently
            if not all_ok:
                # only seed once EVERY feed has truly fetched. A partial fetch
                # (some feeds down) must NOT mark the day seeded, or a recovering
                # feed's real pre-startup headlines later fire as false BREAKING.
                return
            self._save_news_seen((seen + [t for _, t in fresh])[-300:],
                                 str(now.date()))
            return  # never replay headlines that predate startup
        if not fresh:
            return
        # cap the SEND rate at 3/pass, but mark only what we actually send as
        # seen — any extra fresh headlines stay un-seen and fire next pass (a
        # multi-headline burst is exactly when we must not silently drop them)
        to_send = fresh[:3]
        self._save_news_seen((seen + [t for _, t in to_send])[-300:],
                             str(now.date()))
        for outlet, title in to_send:
            # 1) RAW alert goes out INSTANTLY — nothing slow runs before it
            self.notify(f"🚨 BREAKING — {outlet}: {title}")
            print(f"{now:%H:%M:%S} breaking news alert: {title[:60]}")
            # 2) AI 'read' is a best-effort FOLLOW-UP — never blocks the alert
            take = self._news_take(title)
            if take:
                self.notify(f"🧠 Quick read: {take}")

    def _news_take(self, title: str):
        try:
            import assistant
            if not assistant.enabled():
                return None
            take = assistant.respond(
                {"chat_id": "newsdesk", "kind": "text",
                 "text": ("One sentence only, no invented numbers: what could "
                          "this headline mean for SPX/QCOM/TSLA trades today? "
                          f"Headline: {title}")},
                self.status_text(), tools_enabled=False)
            # only forward a genuine model answer. assistant.respond returns
            # several failure strings that DON'T contain 'error' ("My brain
            # couldn't connect…", "came back empty…") — those must never reach
            # members as a "Quick read".
            if take:
                low = take.lower()
                if ("error" not in low and not low.startswith("my brain")
                        and "came back empty" not in low):
                    return take
        except Exception:
            pass
        return None

    # ---------- daily recap + weekly ----------

    def maybe_recap(self, now: datetime):
        """Send the daily 3:05 PM CT (16:05 ET) recap from inside the bot,
        so the cloud needs no separate scheduled task."""
        if self.dry or now.weekday() >= 5 or now.time() < WEEKLY_AT:
            return
        today = str(now.date())
        if config.state_get("recap_sent") == today:
            return
        try:
            import recap
            errs = recap.main(require_date=today)
            if errs == "STALE":
                # yfinance hasn't published today's session yet — don't send a
                # wrong-day recap and don't mark sent; just retry next pass.
                print(f"{now:%H:%M:%S} recap data not caught up to {today} yet; will retry")
                return
            if not errs:
                config.state_set("recap_sent", today)
                return
            # delivery errors: retry a BOUNDED number of times, then mark sent so
            # one permanently-unreachable member can't re-broadcast the full
            # recap to everyone else every cycle for the next 7 minutes.
            tries = config.state_get("recap_tries", {})
            n = (tries.get(today, 0) if isinstance(tries, dict) else 0) + 1
            if n >= 2:
                config.state_set("recap_sent", today)
                print(f"{now:%H:%M:%S} recap had delivery errors after {n} tries; "
                      f"marking done to avoid duplicates: {errs}")
            else:
                config.state_set("recap_tries", {today: n})
                print(f"{now:%H:%M:%S} recap send had errors, will retry once: {errs}")
        except Exception as e:
            print(f"{now:%H:%M:%S} recap failed (will retry): {e}")

    def maybe_request_digest(self, now: datetime):
        """After the close, send Chudi one rollup of every request that came in
        today (plus anything still open). Self-guards to once per day, same
        16:05 ET window as the recap."""
        if self.dry or now.weekday() >= 5 or now.time() < WEEKLY_AT:
            return
        today = str(now.date())
        if config.state_get("request_digest_sent") == today:
            return
        try:
            import intake
            msg = intake.digest()
            owner = telegram.primary_owner_id()
            if msg and owner:
                err = telegram.send_to(owner, msg)
                if err:
                    print(f"{now:%H:%M:%S} request digest send error: {err}")
                    return  # don't mark sent — retry next pass
            config.state_set("request_digest_sent", today)
        except Exception as e:
            print(f"{now:%H:%M:%S} request digest failed (will retry): {e}")

    def maybe_weekly(self, now: datetime):
        # Friday after the close — with weekend catch-up if the bot was
        # offline at 16:05 (daemon mode picks it up later)
        due = (now.weekday() == 4 and now.time() >= WEEKLY_AT) or now.weekday() >= 5
        if not due:
            return
        key = now.strftime("%G-W%V")
        if config.state_get("weekly_sent") == key:
            return
        errors = self.notify(scoreboard.weekly_report(
            self.book, self.backtest_old, self.backtest_new, now.date()))
        if not errors and not self.dry:  # only mark sent when it really went out
            config.state_set("weekly_sent", key)            # (and never in dry)

    # ---------- main loops ----------

    def run_session(self):
        now = et_now()
        self.reset_day(now)
        self.check_downtime_on_start(now)  # were we silently down mid-session?
        mode_src = "live alerts" if not self.dry else "dry-run"
        print(f"Scanner running ({mode_src}). Entry window "
              f"{self.cfg.entry_start}-{self.cfg.entry_end} ET, polling every "
              f"{config.POLL_SECONDS}s. Watchlist: {', '.join(self.cfg.watchlist)}. "
              f"Min win rate {config.MIN_WINRATE:.0f}%. Exits: half at "
              f"+{config.TP_HALF_PCT:g}%, give-back {config.RUNNER_GIVEBACK_PCT:g} "
              f"off peak, stop {config.STOP_PCT:g}%. Being picky — no forced trades.")
        if self.backtest_old is None:
            print("WARNING: reports/backtest_results.json missing — the bot "
                  "will not send entry alerts without real backtest stats.")
        if now.weekday() < 5:
            self.morning_report(now)
        keep_awake(True)
        self.start_news_watch()  # instant breaking-news alerts, own thread
        try:
            while True:
                now = et_now()
                self.reset_day(now)
                if now.time() >= SESSION_END or now.weekday() >= 5:
                    self.maybe_recap(now)
                    self.maybe_request_digest(now)
                    self.maybe_weekly(now)
                    self.health_eod(now)  # owner-only 'all clear' for the day
                    print("Session over for today.")
                    return
                t0 = time_mod.monotonic()
                try:  # one bad cycle must never end the trading day
                    self.flush_pending()
                    # money-critical FIRST: entries + exits must never wait on a
                    # slow chat command (a photo/assistant reply can block secs)
                    if self.cfg.entry_start <= now.time() <= self.cfg.entry_end:
                        self.scan_entries(now)
                    if now.time() >= MONITOR_START:
                        self.monitor_positions(now)
                    self.handle_commands()
                    self.maybe_recap(now)
                    self.maybe_request_digest(now)
                    self.maybe_weekly(now)
                    self.health_check(now)
                except Exception as e:
                    print(f"{now:%H:%M:%S} cycle error (continuing): {e}")
                # adaptive sleep: data-fetch time counts toward the cadence,
                # so a slow cycle doesn't push the next look further out
                elapsed = time_mod.monotonic() - t0
                time_mod.sleep(max(2.0, config.POLL_SECONDS - elapsed))
        finally:
            self.stop_news_watch()
            keep_awake(False)

    def daemon(self):
        print("Daemon mode: running around the clock. Commands answered "
              "any time; sessions run on trading days 9:31-16:12 ET.")
        while True:
            now = et_now()
            try:
                if now.weekday() < 5:
                    if time(9, 0) <= now.time() < time(9, 30) \
                            and self.premarket_sent_for != now.date():
                        self.reset_day(now)
                        self.morning_report(now, include_gap=False, premarket=True)
                    if time(9, 31) <= now.time() < SESSION_END:
                        self.run_session()
                        continue
                self.flush_pending()
                self.maybe_recap(now)   # catch-up: a recap missed/STALE during
                                        # the 16:05-16:12 window still goes out
                self.maybe_request_digest(now)  # catch-up the request rollup too
                self.maybe_weekly(now)  # weekend catch-up
                self.health_eod(now)    # close ping even if a restart ended the
                                        # session early (self-guards once/day)
                self.handle_commands(timeout=45)  # long-poll, responsive + cheap
            except Exception as e:
                print(f"daemon error (continuing): {e}")
                time_mod.sleep(30)

    # ---------- /test ----------

    def test_sequence(self, chat_id=None):
        """Fire a fake signal through the entire pipeline: entry card,
        sell-half, momentum-flip, stop, and expiry alerts. Nothing is
        persisted; every message is clearly labeled TEST."""
        now = et_now()
        today = now.date()
        setup = Setup(ticker="SPX", direction="call", strike=7300.0,
                      spot=7297.2, mom_pct=0.21, reason="test")
        display = scoreboard.stats_for_card("SPX", "call", self.book,
                                            self.backtest_old, self.backtest_new)
        if display is None:  # no backtest on disk — use clearly-fake numbers
            display = {"win_rate": 72.0, "avg_win_pct": 30.0, "avg_loss_pct": -25.0,
                       "expectancy_pct": 9.0, "ev_pct": 9.0, "trades": 60,
                       "start": "01/01/2026", "end": "06/01/2026",
                       "label": "EXAMPLE NUMBERS — no backtest on disk",
                       "costs_note": "after est. costs", "source": "backtest_old"}
        mode, mode_reason = self.current_mode()
        pos = Position(
            id="test", date=str(today), time_et=now.strftime("%H:%M:%S"),
            ticker="SPX", direction="call", right="C", strike=7300.0,
            expiry=str(today), entry_mid=4.40, entry_source="quote",
            entry_bid=4.20, entry_ask=4.60, spot_at_signal=7297.2,
            mom_pct=0.21, risk_pct=config.RISK_PER_TRADE_PCT,
            correlated=False, paper=config.paper_mode(), risk_mode=mode,
            win_rate_quoted=display["win_rate"], ev_quoted=display["ev_pct"])
        fake_quote = quotes.Quote(4.20, 4.60, 4.40,
                                  "live quote (example numbers)", False)
        src = "live quote (example numbers)"
        msgs = [
            cards.entry_card(setup, pos, fake_quote, display, mode,
                             mode_reason, today, today),
            cards.half_card(pos, {"pct": 27.3, "source": src}),
        ]
        pos.half_exit = {"time": "10:05:00", "pct": 27.3, "mark": 5.60}
        msgs.append(cards.trail_card(pos, {"pct": 18.0, "total_pct": 22.7,
                                           "source": src}))
        msgs.append(cards.stop_card(pos, {"pct": -31.2, "source": src}))
        msgs.append(cards.expiry_card(pos, {"pct": -8.0, "source": src}))
        for i, msg in enumerate(msgs, 1):
            tagged = (f"🧪 TEST {i}/5 — EXAMPLE ONLY, NOT A REAL ALERT 🧪\n\n{msg}")
            if chat_id and not self.dry:   # /test from a chat: reply ONLY to them,
                err = telegram.send_to(chat_id, tagged)   # never the whole group
                errors = [err] if err else []
            else:                          # CLI --test / dry-run: broadcast path
                errors = self.notify(tagged)
            print(f"test {i}/5 {'sent' if not errors else errors}")
            if not self.dry:
                time_mod.sleep(1.5)  # keep Telegram happy, preserve order


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="print cards, don't text")
    p.add_argument("--setup", action="store_true", help="print Telegram chat IDs")
    p.add_argument("--test", action="store_true",
                   help="fire a fake signal through all 5 alert types")
    p.add_argument("--daemon", action="store_true", help="run forever (cloud mode)")
    p.add_argument("--weekly", action="store_true", help="send the weekly report now")
    args = p.parse_args()
    if args.setup:
        telegram.print_chat_ids()
        return
    svc = Service(dry_run=args.dry_run)
    if args.test:
        svc.test_sequence()
    elif args.weekly:
        svc.notify(scoreboard.weekly_report(svc.book, svc.backtest_old,
                                            svc.backtest_new, et_now().date()))
    elif args.daemon:
        svc.daemon()
    else:
        svc.run_session()


if __name__ == "__main__":
    main()
