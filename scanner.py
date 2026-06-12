"""Live alert service — entry signals, all-day position monitoring, exit
alerts, Telegram commands, risk gate, weekly scoreboard.
NEVER places orders. Every alert is a suggestion ending "Your call."

What runs when (all times ET):
    9:45-10:30   entry window — detect_setup() fires the entry cards
    9:45-16:00   every open position is checked each cycle:
                 SELL HALF at +25% -> trail until momentum flips -> stop -30%
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
from strategy import Setup, StrategyConfig, detect_setup, momentum_pct

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

    # ---------- plumbing ----------

    def notify(self, text: str) -> list:
        if self.dry:
            print(f"\n{text}\n")
            log_alert(text, ["dry-run, not sent"])
            return []
        errors = telegram.send(text)
        log_alert(text, errors)
        if errors:
            # a network blip must not eat a STOP text — queue it for retry.
            # (Retries go to all chats, so a partial failure can duplicate a
            # message for whoever already got it. Duplicate beats missing.)
            pending = config.state_get("pending_sends", [])
            pending.append(text)
            config.state_set("pending_sends", pending[-20:])
            print(f"send failed, queued for retry: {errors}")
        return errors

    def flush_pending(self):
        """Retry alerts that failed to send on an earlier cycle."""
        if self.dry:
            return
        pending = config.state_get("pending_sends", [])
        if not pending:
            return
        remaining = []
        for text in pending:
            if telegram.send("(retry) " + text):
                remaining.append(text)
        config.state_set("pending_sends", remaining)

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
            return hit[1]
        bars = self.feed.today_bars(yfs, now)
        self._bars_cache[yfs] = (now, bars)
        return bars

    def current_mode(self):
        """Manual /risk override (today only) beats the automatic morning mode."""
        override = config.state_get("risk_override")
        if override and override.get("date") == str(et_now().date()):
            return override["mode"], (override.get("reason") or "manual override")
        return self.mode, self.mode_reason

    # ---------- morning ----------

    def morning_report(self, now: datetime, include_gap: bool = True,
                       premarket: bool = False):
        today = now.date()
        # survives restarts: don't re-text the morning card after a reboot
        if (self.morning_sent_for != today
                and config.state_get("morning_sent") == str(today)):
            self.morning_sent_for = today
        cached = config.state_get("risk_auto")
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
        config.state_set("morning_sent", str(today))

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
        if item["kind"] == "command":
            if item["cmd"] == "/score":
                import assistant
                return assistant.score_line(item["chat_id"])
            return self.run_command(item["cmd"], item["args"])
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
        return assistant.respond(item, self.status_text())

    def run_command(self, cmd: str, args: str):
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
        if cmd == "/test":
            self.test_sequence()
            return None
        if cmd in ("/help", "/start"):
            return cards.help_card()
        return None  # silently ignore unknown commands

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
        stats = self.backtest_old.get("per_setup", {}).get(key)
        now = et_now()
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
            if self.gate_stats(setup) is None:
                self.skipped_today.add(ticker)  # final decision for the day
                continue
            try:
                opened = self.open_position(setup, now)
            except Exception as e:
                print(f"{now:%H:%M:%S} {ticker}: failed to open position: {e} "
                      "— will retry next cycle")
                continue  # transient failure must not burn the day's alert
            if opened:
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
        errors = self.notify(card)  # sends the moment it's seen
        record_alert(setup, now, display)
        self.book.add(pos)
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
        est = quotes.estimate_premium(spot, pos.strike, pos.right,
                                      expiry_dt, now, sigma)
        # the estimate-based stop floor compares model-to-model: the BS
        # estimate now vs the BS estimate AT ENTRY. (Estimate vs a real quote
        # mid would read -30% on day one just from the vol-model gap.)
        est_pct = ((est / pos.est_entry - 1) * 100
                   if est > 0 and pos.est_entry > 0 else None)
        quote = quotes.get_option_quote(pos.ticker, pos.right, pos.strike,
                                        pos.expires_on())
        # mark preference: real bid/ask > fresh estimate > stale last trade
        if quote is not None and quote.bid > 0:
            mark, source = quote.mid, quote.source
        elif est > 0:
            mark, source = est, "estimated from the stock move"
        elif quote is not None and quote.mid > 0:
            mark, source = quote.mid, quote.source
        else:
            return

        flipped = False
        if pos.state == "half_sold" and bars is not None and not bars.empty:
            mom = momentum_pct(bars, self.cfg)
            if mom is not None:
                flipped = mom < 0 if pos.direction == "call" else mom > 0

        events = poslib.step(pos, now, mark, source, est_pct, flipped,
                             self.old_bracket)
        self.book.save()
        builders = {"sell_half": cards.half_card, "momentum_flip": cards.flip_card,
                    "stop": cards.stop_card, "expiry_warn": cards.expiry_card}
        for ev in events:
            self.notify(builders[ev["type"]](pos, ev))
            print(f"{now:%H:%M:%S} {pos.ticker}: {ev['type']} at {ev['pct']:+.1f}%")

    # ---------- weekly ----------

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
        if not errors:  # only mark sent when it actually went out
            config.state_set("weekly_sent", key)

    # ---------- main loops ----------

    def run_session(self):
        now = et_now()
        self.reset_day(now)
        mode_src = "live alerts" if not self.dry else "dry-run"
        print(f"Scanner running ({mode_src}). Entry window "
              f"{self.cfg.entry_start}-{self.cfg.entry_end} ET, polling every "
              f"{config.POLL_SECONDS}s. Watchlist: {', '.join(self.cfg.watchlist)}. "
              f"Min win rate {config.MIN_WINRATE:.0f}%. Exits: half at "
              f"+{config.TP_HALF_PCT:g}%, momentum-flip trail, stop "
              f"{config.STOP_PCT:g}%. Being picky — no forced trades.")
        if self.backtest_old is None:
            print("WARNING: reports/backtest_results.json missing — the bot "
                  "will not send entry alerts without real backtest stats.")
        if now.weekday() < 5:
            self.morning_report(now)
        keep_awake(True)
        try:
            while True:
                now = et_now()
                self.reset_day(now)
                if now.time() >= SESSION_END or now.weekday() >= 5:
                    self.maybe_weekly(now)
                    print("Session over for today.")
                    return
                t0 = time_mod.monotonic()
                try:  # one bad cycle must never end the trading day
                    self.flush_pending()
                    self.handle_commands()
                    if self.cfg.entry_start <= now.time() <= self.cfg.entry_end:
                        self.scan_entries(now)
                    if now.time() >= MONITOR_START:
                        self.monitor_positions(now)
                    self.maybe_weekly(now)
                except Exception as e:
                    print(f"{now:%H:%M:%S} cycle error (continuing): {e}")
                # adaptive sleep: data-fetch time counts toward the cadence,
                # so a slow cycle doesn't push the next look further out
                elapsed = time_mod.monotonic() - t0
                time_mod.sleep(max(2.0, config.POLL_SECONDS - elapsed))
        finally:
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
                self.maybe_weekly(now)  # weekend catch-up
                self.handle_commands(timeout=45)  # long-poll, responsive + cheap
            except Exception as e:
                print(f"daemon error (continuing): {e}")
                time_mod.sleep(30)

    # ---------- /test ----------

    def test_sequence(self):
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
        msgs.append(cards.flip_card(pos, {"pct": 18.0, "total_pct": 22.7,
                                          "source": src}))
        msgs.append(cards.stop_card(pos, {"pct": -31.2, "source": src}))
        msgs.append(cards.expiry_card(pos, {"pct": -8.0, "source": src}))
        for i, msg in enumerate(msgs, 1):
            tagged = (f"🧪 TEST {i}/5 — EXAMPLE ONLY, NOT A REAL ALERT 🧪\n\n{msg}")
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
