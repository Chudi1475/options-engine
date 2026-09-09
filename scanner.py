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
import hashlib
import json
import os
import random
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # emoji on Windows console

import threading
import time as time_mod
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

import cards
import config
import event_journal
import fill_journal
import forward_ledger
import instance_lock
import live_params
import market_calendar
import news
import positions as poslib
import quotes
import risk_gate
import scoreboard
import sniper_book
import storage_io
import strategy_spec
import telegram
import trade_recorder
from backtest import expiry_for, realized_vol
from data_feed import DataFeed
from positions import Position, PositionBook
from strategy import Setup, StrategyConfig, detect_setup

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")  # display timezone ONLY — logic stays ET
# built-in ticker -> Yahoo symbol map, kept as the FALLBACK for open
# positions: if live_params.json drops a ticker from the watchlist while a
# position is still open, monitoring must keep resolving its feed symbol
# (SPX -> ^GSPC); the bare ticker fetches nothing and the position would
# silently lose its stop/half/trail alerts for the rest of the day.
DEFAULT_WATCHLIST = StrategyConfig().watchlist
SESSION_END = time(16, 12)      # loop exits after settle + weekly are done
MONITOR_START = time(9, 45)
WEEKLY_AT = time(16, 5)
LEARN_START = time(21, 0)       # nightly self-review fires at a RANDOM minute
LEARN_END = time(23, 45)        # inside this window (kept before midnight ET so
                                # the same-day date/dedup logic never rolls over)
HOLIDAY_NOTICE_AT = time(17, 0)  # the eve-of-holiday heads-up goes out after the
                                 # close of the last session before a closure,
                                 # early enough to still be that evening in CT


def is_session_day(d: date) -> bool:
    """Whether the US equity market actually trades that ET date.

    Every scheduled job used to ask weekday() instead, which is wrong on the
    nine or ten market holidays a year. On those days the bot ran a full
    session against a feed that never ticks, texted a morning card, and the
    nightly review then graded the empty day as a deliberate 'we stayed out,
    being picky' lesson that the chat brain reads back on every reply."""
    return market_calendar.is_trading_day(d)


def session_end_for(d: date) -> time:
    """When the session loop should shut down for the day. SESSION_END is the
    16:00 close plus the 12 minutes the settle and weekly jobs need; a 13:00
    half day gets the same 12 minutes after ITS close."""
    close = market_calendar.session_close(d)
    if close == market_calendar.EARLY_CLOSE:
        return time(13, 12)
    return SESSION_END


def et_now() -> datetime:
    return datetime.now(ET)


def ct_wall(t: time) -> time:
    """ET wall-clock time -> CT wall-clock time, DISPLAY ONLY (ET is always
    CT+1, same DST switch dates). All internal logic stays on ET."""
    return (datetime.combine(date(2000, 1, 3), t) - timedelta(hours=1)).time()


def ct_hm(hms: str) -> str:
    """Stored ET wall-clock 'HH:MM:SS' -> 'HH:MM' in CT, DISPLAY ONLY."""
    try:
        return (datetime.strptime(hms, "%H:%M:%S")
                - timedelta(hours=1)).strftime("%H:%M")
    except (TypeError, ValueError):
        return (hms or "?")[:5]


def learn_target(day) -> time:
    """The randomized late-night fire time for the nightly self-review, SEEDED
    off the date so every tick of the same day re-derives the SAME minute. That
    makes it restart-stable: a crash-and-restart at 22:00 won't re-roll to an
    earlier time and fire twice, and the once-per-day dedup key covers the rest."""
    lo = LEARN_START.hour * 60 + LEARN_START.minute
    hi = LEARN_END.hour * 60 + LEARN_END.minute
    m = random.Random(f"learn-{day}").randrange(lo, hi + 1)
    return time(m // 60, m % 60)


def learn_session_due(now: datetime) -> date:
    """The session whose nightly self-review is due at this tick: today once
    the random 21:00-23:45 window opens on a TRADING day, otherwise the most
    recent prior trading day. Weekend ticks, holiday ticks and post-outage
    restarts therefore point back at the session that may have been missed
    instead of forgetting it; catch-up is bounded to that single most recent
    session, so a long outage can never backfill a week of owner DMs.

    Holidays are skipped the same way weekends are. Reviewing Thanksgiving
    would find no positions and no morning card, and the review would either
    be suppressed as a suspected outage or invent a lesson about a day the
    market was shut."""
    d = now.date()
    if is_session_day(d) and now.time() >= learn_target(d):
        return d
    return market_calendar.prev_trading_day(d)


GRADE_SETTLE_MINUTES = 5   # after the close, so the last bar has printed


def forward_grade_open_at(d: date) -> time:
    """The clock at which every row signalled on session `d` is past its
    DECLARED horizon and can therefore be graded.

    It is that day's own close plus the minutes the last bar needs, so a half
    day opens at 13:05 rather than 16:05. That was a real defect and not a
    tidy-up: on the Friday after Thanksgiving the entry loop shuts at 13:12,
    the tape stopped at 13:00, every row is settled, and the scheduler was
    still locked out until 16:05. Worse, forward_grade_session returned the
    PREVIOUS session for the whole of that window, so the day that had just
    closed could not even be named as the work in front of it.

    Derived from market_calendar.session_close, the same function
    forward_ledger.declared_horizon and sniper_book._settle_at use, so the
    three cannot drift."""
    close = market_calendar.session_close(d)
    return (datetime.combine(date(2000, 1, 3), close)
            + timedelta(minutes=GRADE_SETTLE_MINUTES)).time()


def forward_grade_session(now: datetime) -> date:
    """The session whose sniper candidates are due to be graded at this tick.

    Grading gets its OWN key, opening at that session's close, instead of
    riding learn_session_due's randomized 21:00-23:45 target. Borrowing the
    paid review's clock made a free deterministic measurement wait on a paid
    one and tied the grading key to a setting that has nothing to do with
    grading.

    What this key does NOT claim is that every row of the session is settled
    the moment it opens. Whether one row can be called final is decided per row
    inside forward_ledger against that row's declared horizon, and a row that
    is not final yet is left for the next pass instead of being frozen wrong.
    See _outcome_is_final there.

    Off-session ticks still point back at the last real session, same as the
    review does, so a weekend tick or a post-outage restart grades the day
    that was missed rather than forgetting it."""
    d = now.date()
    if is_session_day(d) and now.time() >= forward_grade_open_at(d):
        return d
    return market_calendar.prev_trading_day(d)


def _grade_attention_reason(res, n: int) -> str:
    """A short sentence naming what stayed unresolved, for the parked record.

    Every number in it is read back off the counts the grader returned, so
    the needs-attention line cannot say anything the ledger did not."""
    if not isinstance(res, dict):
        return f"grading raised on every one of {n} passes"
    if not res.get("read_ok"):
        return f"the ledger could not be read on any of {n} passes"
    total = res.get("eligible", 0)
    bits = []
    if res.get("missing_data"):
        bits.append(f"{res['missing_data']} of {total} row(s) still had no "
                    "data")
    if res.get("failed_writes"):
        bits.append(f"{res['failed_writes']} of {total} row(s) could not be "
                    "persisted")
    if not bits:
        bits.append(f"the pass over {total} row(s) did not reconcile")
    return f"{', '.join(bits)} after {n} passes"


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
        # Declared at the WIRE, on both branches, because self.dry is a call
        # site check and the sniper entry path was rewritten to reach
        # telegram through _deliver, which has no call site check. One
        # forgotten branch there put real tickets on three real phones from
        # `scanner.py --dry-run`. Set on the False branch too, so a live
        # Service built after a dry one in the same process is live.
        telegram.set_dry_run(bool(dry_run))
        self.cfg = StrategyConfig()
        self.feed = DataFeed()
        book_path = (config.DATA_DIR / "positions_dryrun.json") if dry_run else None
        self.book = PositionBook(book_path)
        self.backtest_old = None  # loaded by reload_tunables()
        self.backtest_new = None
        self.reload_tunables()
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
        self._last_feed_try = None      # last time a bars fetch was attempted
        self._health_last_stamp = 0.0   # monotonic; throttles the alive-stamp
        self._feed_warned = False       # in-memory: feed-stale DM already sent
        self._feed_none_warned = False  # in-memory: no-data-yet DM already sent
        # ownership state machine (see ensure_active). all in-memory dampers,
        # so a crash-looping copy DMs at most once per boot on top of the
        # persisted once-a-day flag.
        self._standby_warned = False    # stand-down DM already sent this boot
        self._wedge_warned = False      # "winner stopped renewing" DM sent
        self._stood_down = False        # this copy has SEEN a live holder, so
                                        # a later acquisition is a PROMOTION
                                        # and runs the ceremony
        self._blind_warned = False      # "ownership unknown" DM sent
        self._recovery_warned = False   # "cannot read my own state" DM sent
        self._blocked_cause = None      # which cause the BLOCKED DM covered
        self._standby_print = 0.0       # monotonic; throttles the standby log
        self._held_lock = False         # this copy currently owns the OS lock
        self._reconciled = False        # ...AND has validated every mandatory
                                        # store. Holding the lock is necessary
                                        # and not sufficient: promotion is all
                                        # or nothing (see _reconcile_all)
        self._recovery_tries = 0        # consecutive failed reconciles
        self._reconcile_report = {}     # store name -> ok/absent/unreadable,
                                        # what the last reconcile actually
                                        # looked at, for /status and the tests
        self._seqwatch = instance_lock.SeqWatch()
        self._last_replay = 0.0         # monotonic; throttles the ACTIVE replay
        self._warned_memo = set()       # (day, key) warnings this PROCESS has
                                        # already emitted. The persisted dedup
                                        # flag lives in state.json, on the very
                                        # volume whose failure is usually what
                                        # raised the warning, so on a full or
                                        # read-only disk it can never land and
                                        # one condition became one DM per cycle

    # ---------- plumbing ----------

    def notify(self, text: str) -> list:
        if self.dry:
            print(f"\n{text}\n")
            log_alert(text, ["dry-run, not sent"])
            return []
        if telegram.standby()[0]:
            # THE most important line in the singleton change. Returning an
            # error list instead would append this text to pending_sends, and
            # pending_sends lives in the SHARED state.json, so the WINNER
            # would later flush and broadcast the loser's duplicates. Return
            # empty, queue nothing, write no alerts.log line.
            print(f"[standby, not broadcast] {text[:80]}")
            return []
        try:
            errors = telegram.send(text)
        except RuntimeError as e:  # e.g. no chat IDs configured — fail LOUD and
            print(f"send failed ({e}), queueing for retry")  # queue, don't drop
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

    # ---------- durable events (W02) ----------

    def notify_intent(self, text: str, intent) -> list:
        """Send a text that already has a durable intent behind it, and record
        what each recipient's wire actually did.

        The difference from notify(): a failure here is NOT appended to
        pending_sends. pending_sends re-broadcasts the whole text to every
        chat, so one recipient with a permanent 403 cost the other two a
        duplicate on every retry pass. A journaled send has per recipient
        records instead, and replay retries exactly the recipients that are
        still unresolved.

        Every attempt is written BEFORE the request goes out, so a process that
        dies inside the HTTP call still leaves the attempt on disk. That is the
        whole difference between an unknown delivery and an invisible one."""
        if self.dry:
            print(f"\n{text}\n")
            log_alert(text, ["dry-run, not sent"])
            return []
        if telegram.standby()[0]:
            # same rule as notify: a losing copy broadcasts nothing and queues
            # nothing. the intent stays unresolved and the WINNER replays it.
            print(f"[standby, not broadcast] {text[:80]}")
            return []
        return self._deliver(intent, None, text=text)

    def _deliver(self, intent, indices, text=None) -> list:
        """One delivery pass over some or all recipients of one intent.

        indices=None means every recipient; a list means exactly those and
        nobody else, which is what makes a retry stop bothering the people who
        already got the card."""
        body = text if text is not None else intent.text
        wanted = indices
        if wanted is None:
            wanted = [d.recipient_index for d in intent.deliveries()]
        # Resume a multi part card at the first part that was NOT confirmed.
        # send_to_detailed has always been able to do this and nothing on the
        # production path ever asked it to, so a replay of a three part card
        # whose second part failed re-sent part one to a chat that already had
        # it. Two conditions, both load bearing: the body has to be byte for
        # byte the text the intent recorded (a different card is a different
        # message, and resuming into it would SKIP real content), and the
        # recorded part count has to still match this split.
        start_parts = {}
        if body == (intent.text or ""):
            total_now = len(telegram.split_message(body))
            for d in intent.deliveries():
                done = int(d.parts_confirmed or 0)
                if (d.status == event_journal.FAILED
                        and int(d.parts_total or 1) == total_now
                        and 0 < done < total_now):
                    start_parts[d.recipient_index] = done
        for i in wanted:
            try:
                event_journal.record_attempt(intent.journal_id, i)
            except event_journal.JournalUnavailable as e:
                # the card matters more than the bookkeeping line about it, but
                # an unrecorded attempt is an evidence gap and is said out loud
                self._journal_down(f"attempt line refused: {e}")
        try:
            results = telegram.send_detailed(body, only_indices=wanted,
                                             start_parts=start_parts)
        except RuntimeError as e:      # e.g. no chat IDs configured
            print(f"journaled send failed ({e})")
            results = []
        errors = [r["error"] for r in results if r.get("error")]
        log_alert(body, errors)
        for r in results:
            try:
                event_journal.record_result(
                    intent.journal_id, r["recipient_index"], r["status"],
                    provider_message_id=r.get("message_id"),
                    error_class=r.get("error_class") or None,
                    parts_total=r.get("parts_total"),
                    parts_confirmed=r.get("parts_confirmed"))
            except event_journal.JournalUnavailable as e:
                self._journal_down(f"delivery line refused: {e}")
        return results

    def _journal_down(self, why: str):
        """Raise the explicit health condition Astra asks for when durable
        intent creation fails, and DM the owner once a day.

        It never touches the book. Astra is explicit that this failure "must
        never discard known open positions or silently stop their monitoring",
        so the exit path keeps running under the old ordering and records the
        degradation as an evidence gap rather than a silent one."""
        now = et_now()
        config.state_set("journal_health", {
            "ok": False, "why": str(why)[:300], "at": f"{now:%Y-%m-%d %H:%M:%S}",
            # named, not implied: with no durable record the exit path falls
            # back to send-then-save, so a crash in that window can still
            # duplicate an exit card. that is a known gap, not a repair.
            "evidence_gap": "durable event journal unavailable, exit alerts "
                            "fall back to the legacy send then save order",
        })
        print(f"{now:%H:%M:%S} durable event journal UNAVAILABLE: {why}. "
              "no new entry alerts until it is writable again. open positions "
              "are still monitored and their exits still send.")
        if self._hb_warned_once("journal_unavailable", str(now.date())):
            return
        self._hb_owner(
            "Heartbeat: I cannot write my durable event record, so I am NOT "
            "sending new entry alerts. An alert I cannot record is one I "
            "cannot recover if I restart, and an unrecorded alert is worse "
            f"than a missed one. Reason: {str(why)[:200]}. Everything already "
            "open is still being watched and its exits still send. Check the "
            "volume. The Railway logs carry this on every cycle.")

    def _journal_ok(self):
        """Clear the condition once a durable write lands again."""
        if (config.state_get("journal_health") or {}).get("ok") is False:
            config.state_set("journal_health", {
                "ok": True, "at": f"{et_now():%Y-%m-%d %H:%M:%S}"})

    def _commit_intent(self, kind, **kw):
        """Every durable intent this service commits goes through here.

        A dry run gets None instead of a record. The journal lives on the
        SHARED volume, so once the wire correctly refuses to send from a dry
        run, a dry copy that still committed an intent left a
        committed-but-undelivered card behind for the LIVE daemon to replay and
        broadcast. A dry run that texts nobody today but makes the real bot
        text three people tomorrow is the same defect one restart later.

        None is not a new condition for any caller: it is exactly the shape
        they already handle when the journal is unavailable, which falls back
        to the old send-then-save order, and in a dry run "send" is a print."""
        # getattr, not self.dry: several suites build a Service with __new__
        # and stub only the plumbing a case needs, so an attribute that is not
        # there means "not a dry run", exactly as it did before this existed.
        if getattr(self, "dry", False):
            return None
        return event_journal.commit_intent(kind, **kw)

    def _position_unpersisted(self, kind: str, what: str, why: str):
        """The card went out and the position reached no store. Astra A05 says
        never infer delivery from position existence; the converse holds too,
        and this is the converse. A delivered alert for a trade that is tracked
        nowhere is a health condition, not a success.

        The intent is left UNRESOLVED and flagged position_missing by the
        caller, so replay retries the publish and orphans() reports it either
        way. This is the part a person can see without reading /health."""
        now = et_now()
        config.state_set("position_health", {
            "ok": False, "kind": kind, "what": what, "why": str(why)[:200],
            "at": f"{now:%Y-%m-%d %H:%M:%S}",
            "evidence_gap": "an alert was delivered for a modeled position "
                            "that no store holds; it is retried on every "
                            "replay pass and reported as an orphan until it "
                            "lands or a person looks",
        })
        print(f"{now:%H:%M:%S} {kind} card sent for {what} but the position "
              f"could NOT be persisted ({why}). it is NOT tracked, its stop is "
              "NOT being watched, and the durable intent stays open so replay "
              "keeps retrying.")
        if self._hb_warned_once("position_unpersisted", str(now.date())):
            return
        self._hb_owner(
            "Heartbeat: I sent an alert and then could not save the position "
            f"behind it ({kind} {what}: {str(why)[:120]}). That trade is on "
            "your phone and in none of my books, so I am not watching its stop "
            "until the write lands. I retry it every replay pass and it shows "
            "in /health as an orphan. Check the volume.")

    def _position_ok(self):
        """Clear the condition once a retried publish lands."""
        if (config.state_get("position_health") or {}).get("ok") is False:
            config.state_set("position_health", {
                "ok": True, "at": f"{et_now():%Y-%m-%d %H:%M:%S}"})

    def replay_journal(self, now: datetime):
        """Finish every intent a crash left half done. Runs on the way into
        ACTIVE, once ownership is established and every store has reconciled.

        Deliberately NOT run in RECOVERING: the wire is gagged there, so a
        resend would be dropped and then recorded as a failure. This is the
        first thing the new owner does with a working wire, which is what
        Astra's "replay local durable intents under ownership" buys once the
        gag is accounted for."""
        try:
            stats = event_journal.replay(send=self._replay_send,
                                         relink=self._replay_link, now=now)
        except event_journal.JournalUnavailable as e:
            self._journal_down(f"replay could not read the journal: {e}")
            return {}
        except Exception as e:                                  # noqa: BLE001
            print(f"{now:%H:%M:%S} journal replay error (continuing): {e}")
            return {}
        if stats.get("intents"):
            print(f"{now:%H:%M:%S} journal replay: {stats['intents']} "
                  f"unresolved, {stats['relinked']} re-linked, "
                  f"{stats['resent']} recipient sends, {stats['unknown']} "
                  f"unknown left alone, {stats['orphans']} orphans")
        return stats

    def _replay_send(self, intent, indices):
        if telegram.standby()[0]:
            return []
        # The bound, enforced at the one place that actually puts a retry on
        # the wire. replay used to run once per promotion, so nothing was
        # scheduled and nothing could spin; it is now driven every
        # REPLAY_EVERY_S while this copy is ACTIVE, and a recipient the journal
        # cannot resolve (a result line it could not write leaves one pinned in
        # ATTEMPTED) would otherwise become a card every minute for the life of
        # the process. MAX_DELIVERY_RETRIES is the number that already means
        # "stop"; honor it here rather than inventing a second one.
        capped = {d.recipient_index for d in intent.deliveries()
                  if int(d.attempts or 0) >= event_journal.MAX_DELIVERY_RETRIES}
        todo = [i for i in indices if i not in capped]
        if not todo:
            return []
        return self._deliver(intent, todo)

    def _replay_link(self, intent):
        """Re-apply an intent's linkage, idempotently, and say what was found.

        Never opens anything a second time: the position is looked up by
        decision_id and the sniper row by the same, so a replay that runs twice
        repairs the same trade twice and creates nothing. Astra A05: never
        infer delivery from position existence, so nothing here touches a
        delivery status."""
        found = {}
        kind = intent.kind or ""
        retry = False
        if kind == "entry":
            pos = self.book.find_by_decision(intent.decision_id)
            # A position the BOOK holds is not the same as a position the
            # STORE holds: add() appends to memory and then publishes, so a
            # refused publish leaves a row this process can see and the next
            # boot cannot. The link is therefore judged on the publish, never
            # on the object existing, which is Astra A05 pointing the other
            # way: never infer a tracked position from a delivered card.
            landed = pos is not None
            if pos is None and intent.payload.get("position"):
                pos, landed = self._reopen_from_intent(intent)
            elif pos is not None and intent.linked.get("retry_link"):
                landed = self.book.save()
            found["position_id"] = pos.id if pos is not None else None
            found["position_missing"] = not landed
            retry = retry or (pos is not None and not landed)
        elif kind == "sniper_entry":
            row = None
            for r in sniper_book.all_rows():
                if r.get("decision_id") == intent.decision_id:
                    row = r
                    break
            # "the position is missing" is only a claim an intent that
            # DESCRIBES one can make. A real sniper_entry payload carries every
            # level of the ticket, which is both what makes the repair possible
            # and what makes the absence meaningful.
            p = intent.payload or {}
            describes = all(p.get(k) not in (None, "")
                            for k in ("symbol", "direction", "entry", "stop",
                                      "target"))
            if row is None and describes:
                # open_trade returns None on a busy ledger lock, an unreadable
                # book or a refused write, and the ticket still went out. This
                # branch had no equivalent of _reopen_from_intent and never set
                # position_missing, so orphans() could not classify a broadcast
                # sniper ticket that is tracked nowhere and replay closed it
                # for good: a live stop nobody is watching, reported as zero
                # orphans.
                row = self._reopen_sniper_from_intent(intent)
            if row is not None:
                found["position_id"] = row.get("id")
            if describes:
                found["position_missing"] = row is None
                retry = retry or row is None
            if intent.candidate_id:
                res = forward_ledger.mark_selected(
                    intent.candidate_id, fired_at_et=intent.payload.get("fired_at"),
                    position_id=(row or {}).get("id"),
                    decision_id=intent.decision_id,
                    intent_id=intent.journal_id,
                    delivery_confirmed=intent.delivery_confirmed)
                status = getattr(res, "status", "applied" if res else "row_missing")
                if status == forward_ledger.MarkResult.ROW_MISSING:
                    found["ledger_row_missing"] = True
                elif status == forward_ledger.MarkResult.WRITE_REFUSED:
                    found["ledger_write_refused"] = True
                    retry = True
                else:
                    found["ledger_linked"] = True
        if not found:
            return found
        # one answer for the whole intent: a repaired ledger row must not clear
        # a retry the missing position still needs, and vice versa
        found["retry_link"] = retry
        if intent.linked.get("position_missing") and found.get(
                "position_missing") is False:
            self._position_ok()      # a retried publish landed
        # Do not re-write a link line that says exactly what the intent already
        # says. replay is driven every REPLAY_EVERY_S now instead of once per
        # promotion, so an unchanged relink would append a journal line and
        # republish the index every minute for as long as one recipient is
        # still owed a card.
        cur = dict(intent.linked or {})
        if all(cur.get(k) == v for k, v in found.items()):
            return {}
        return found

    def _reopen_from_intent(self, intent):
        """Rebuild the modeled position an intent described, when the crash
        landed between the journal line and the book write.

        Returns (position or None, whether the book actually PUBLISHED). The
        second half used to be dropped: add()'s return was ignored and the
        caller then read the row straight back out of memory, so a replay that
        could not persist the reopened position reported it as repaired and
        resolved the intent, losing the trade for good on the next boot.

        The intent carries the whole row, so this is a replay of a decision
        already made, never a new one: no quote is fetched, no gate is
        re-evaluated and no threshold is consulted."""
        try:
            allowed = {f.name for f in poslib.fields(Position)}
            row = {k: v for k, v in (intent.payload.get("position") or {}).items()
                   if k in allowed}
            if not row.get("id"):
                return None, False
            pos = Position(**row)
        except (TypeError, ValueError, AttributeError) as e:
            print(f"replay could not rebuild the position for "
                  f"{intent.journal_id}: {e}")
            return None, False
        landed = self.book.add(pos)
        return self.book.find_by_decision(intent.decision_id), bool(landed)

    def _reopen_sniper_from_intent(self, intent):
        """Re-open the sniper row an intent already described, or None.

        Same contract as _reopen_from_intent and for the same reason: the
        ticket on three phones is a decision that was already made, and the
        payload carries every level it named, so nothing here re-reads a bar or
        re-evaluates the pattern. open_trade is idempotent on decision_id and
        refuses a second row on a symbol that already has one, so running this
        every replay pass repairs one trade and can never open a second."""
        p = intent.payload or {}
        need = ("symbol", "direction", "entry", "stop", "target")
        if any(p.get(k) in (None, "") for k in need):
            return None
        try:
            fired = str(p.get("fired_at") or "")
            day, _, hms = fired.partition(" ")
            return sniper_book.open_trade(
                symbol=str(p["symbol"]), display=str(p["symbol"]),
                direction=str(p["direction"]), entry=float(p["entry"]),
                stop=float(p["stop"]), target=float(p["target"]),
                day=day or str(intent.session_date or ""),
                time_et=hms or "00:00:00", entry_ts=fired or None,
                candidate_id=intent.candidate_id or None,
                decision_id=intent.decision_id or None,
                intent_id=intent.journal_id,
                position_id=intent.position_id or None)
        except (TypeError, ValueError, KeyError) as e:
            print(f"replay could not rebuild the sniper row for "
                  f"{intent.journal_id}: {e}")
            return None

    MAX_SEND_RETRIES = 6

    def flush_pending(self):
        """Retry alerts that failed to send on an earlier cycle. A permanently
        failing recipient (e.g. someone blocked the bot -> HTTP 403) must NOT
        turn one alert into an endless duplicate storm to everyone else, so each
        queued message is dropped after MAX_SEND_RETRIES attempts."""
        if self.dry:
            return
        if telegram.standby()[0]:
            # the queue is shared state on a shared volume. the copy holding
            # the lease owns it; a loser draining it would re-broadcast the
            # winner's backlog, or burn its retry budget on sends it drops.
            return
        pending = config.state_get("pending_sends", [])
        if not pending:
            return
        n = len(pending)
        keep = []  # originals still failing and under the retry cap
        for i, item in enumerate(pending):
            if telegram.standby()[0]:
                # this runs on its own thread (flush_pending_bg), so the check
                # on entry is one moment in time and a long backlog outlives
                # it. the sniper and news loops re-check every pass for exactly
                # this reason and this one was missed. hand the rest of the
                # queue back untouched: the winner owns it now, and dropping it
                # here would lose alerts nobody ever sent.
                keep.extend(pending[i:])
                break
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

    def flush_pending_bg(self):
        """Run flush_pending on its own thread (single flight) so a retry
        backlog never delays the entry/exit scans or command pickup."""
        t = getattr(self, "_flush_thread", None)
        if t and t.is_alive():
            return  # previous flush still working through the backlog
        if not config.state_get("pending_sends", []):
            return
        self._flush_thread = threading.Thread(
            target=self.flush_pending, daemon=True, name="flush-pending")
        self._flush_thread.start()

    # Once-per-day jobs (morning card, recap, request digest, nightly review,
    # weekly) send FIRST and write their dedup key AFTER, so a crash or
    # redeploy landing in between re-broadcasts the whole report on the next
    # start — and railway restarts with restartPolicy ALWAYS, so a crash loop
    # could re-text it indefinitely. Each job now records its attempt BEFORE
    # sending; a key that has burned MAX_JOB_ATTEMPTS attempts (sends OR hard
    # failures, not just delivery errors) is marked done without sending
    # again. Duplicate still beats missing: the first re-broadcast after a
    # crash is allowed, the next is not.
    MAX_JOB_ATTEMPTS = 2

    # Grading is not a send, so it gets its own budget and, more importantly,
    # its own SPACING. The night loop ticks about every 45 seconds
    # (handle_commands(timeout=45) in the daemon), so two back-to-back
    # attempts were both spent inside 90 seconds and any provider outage
    # longer than that cost the whole day's evidence. Six passes fifteen
    # minutes apart covers about an hour and a quarter of yfinance being
    # down. These are scheduler knobs: they change how often the grader
    # LOOKS, never what it grades or what alerts.
    GRADE_MAX_ATTEMPTS = 6
    GRADE_RETRY_S = 900
    # Every piece of durable state the grading job owns, named in one place so a
    # fixture that resets the scheduler resets ALL of it. A test that clears
    # three of four keys is testing a machine nobody runs.
    GRADE_STATE_KEYS = ("forward_graded", "forward_grade_attention",
                        "forward_grade_last", "forward_grade_tries")

    def _job_attempt(self, job: str, key: str) -> int:
        """Record one attempt for (job, key) and return the new total.
        Stores only the current key, so old days/weeks self-prune."""
        rec = config.state_get(f"{job}_tries", {})
        n = (rec.get(key, 0) if isinstance(rec, dict) else 0) + 1
        config.state_set(f"{job}_tries", {key: n})
        return n

    @staticmethod
    def load_extra_closures() -> dict:
        """Push the owner's unscheduled closures into the calendar module.

        Called at boot and on every trading-date flip, so a closure texted in
        at 6am is live for that morning without a redeploy. market_calendar
        itself stays pure and file-free; this is the only thing that injects."""
        try:
            stored = config.state_get("extra_closures", {}) or {}
            live = market_calendar.set_extra_closures(stored)
            if live:
                print("extra market closures in effect: "
                      + ", ".join(f"{d} ({r})" for d, r in sorted(live.items())))
            return live
        except Exception as e:
            print(f"could not load extra closures (ignoring): {e}")
            return {}

    def reload_tunables(self):
        """Pick up the overnight backtest without a redeploy. The daemon
        lives for weeks, but backtest.py rewrites the report jsons overnight,
        so re-read them (and rebuild cfg) whenever the trading date flips.
        A report that is missing or unreadable keeps the previous in-memory
        stats: stale-but-verified beats wiping the alert gate mid-flight."""
        self.load_extra_closures()
        for attr, name in (("backtest_old", "backtest_results.json"),
                           ("backtest_new", "backtest_new_rules.json")):
            fresh = scoreboard.load_report(name)
            prev = getattr(self, attr, None)
            if fresh is not None:
                setattr(self, attr, fresh)
                if prev is not None and fresh != prev:
                    print(f"reloaded {name}: stats changed overnight")
            elif prev is not None:
                print(f"{name} unreadable on reload, keeping previous stats")
        # the report's bracket is validated, not trusted: a hand-corrupted
        # "bracket": null (present key, so .get's default never applies) or
        # bool/NaN legs would crash entry pinning with dict(None) or feed the
        # shadow garbage comparisons — degrade to the built-in default instead
        loaded = (self.backtest_old or {}).get("bracket")
        self.old_bracket = (dict(loaded) if poslib.valid_bracket(loaded)
                            else dict(poslib.DEFAULT_OLD_BRACKET))
        self.cfg = StrategyConfig()
        # Owner-tunable live settings (live_params.json on DATA_DIR): the
        # no-redeploy path for the knobs that used to be frozen in code.
        # All-or-nothing: a missing OR invalid file means built-ins, so a
        # typo can never half-apply, and gate_stats still demands real
        # backtest stats + the win-rate/expectancy bar for any setup the
        # file allows. The class constant stays the built-in fallback.
        self.ALLOWED_SETUPS = Service.ALLOWED_SETUPS
        try:
            params, errs, exists = live_params.load()
        except Exception as e:  # a loader bug must never kill the daemon
            params, errs, exists = None, [f"loader error: {e}"], True
        if params is not None:
            self.ALLOWED_SETUPS = live_params.apply(
                self.cfg, params, Service.ALLOWED_SETUPS)
            self.live_params_note = ("live_params.json applied: "
                                     + live_params.summary(params))
        elif exists:
            self.live_params_note = ("live_params.json REJECTED, using "
                                     "built-in settings: " + "; ".join(errs))
        else:
            self.live_params_note = "no live_params.json; built-in settings"
        if getattr(self, "_live_params_last", None) != self.live_params_note:
            self._live_params_last = self.live_params_note
            print(f"live params: {self.live_params_note}")

    def reset_day(self, now: datetime):
        if self.day != now.date():
            self.day = now.date()
            self.skipped_today = set()
            self.daily_closes = {}
            # Also on the process's FIRST flip: a daemon started the evening
            # before must not trade its first morning on reports the overnight
            # backtest already rewrote. Reload is idempotent, so the extra
            # re-read right after __init__ costs nothing.
            self.reload_tunables()

    def yfs_for(self, ticker: str) -> str:
        """Ticker -> Yahoo symbol via the live watchlist, falling back to
        the built-in map so an open position keeps a working feed even when
        its ticker was removed from live_params.json mid-flight."""
        return (self.cfg.watchlist.get(ticker)
                or DEFAULT_WATCHLIST.get(ticker, ticker))

    def sigma(self, ticker: str) -> float:
        """Realized vol for estimates. Never raises — a throttled download
        returns 0.0 (estimate falls back to intrinsic) and is retried after
        5 minutes instead of hammering Yahoo every cycle."""
        if self.daily_closes.get(ticker) is None:
            if time_mod.time() < self._sigma_retry.get(ticker, 0):
                return 0.0
            try:
                yfs = self.yfs_for(ticker)
                d1 = yf.download(yfs, period="1y", interval="1d",
                                 progress=False, auto_adjust=False)
                if hasattr(d1.columns, "levels"):
                    d1.columns = d1.columns.get_level_values(0)
                closes = d1["Close"]
                if closes.empty:
                    raise ValueError("no daily data")
                self.daily_closes[ticker] = closes
            except Exception as e:
                print(f"{ticker}: daily download failed ({e}), retry in 5 min")
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
            self._last_feed_try = now  # a real fetch happened (feed-dead check)
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
        """DM the OWNER only (never members) with an ops/health note.

        ops=True, so this is the one channel that survives a stand-down. A
        copy that lost the singleton lease sends nothing else, and it still
        has to be able to tell the owner that it stood down and why."""
        owner = telegram.primary_owner_id()
        if not owner or self.dry:
            return
        try:
            telegram.send_to(owner, text, ops=True)
        except Exception as e:
            print(f"heartbeat DM failed: {e}")

    def _hb_warned_once(self, key: str, day: str) -> bool:
        """True if this warning already fired today; else mark it and return
        False. Persisted, so a restart can't re-spam the same warning.

        A standing-by copy reads but never WRITES. state.json is a whole-file
        read-modify-write and the only lock around it is a thread lock, which
        says nothing across two processes on one volume, so a mute copy
        recording its own dedup flag could publish a stale snapshot of the
        winner's keys (sniper_alerted, morning_sent, recap_sent) and cause a
        missed or duplicated alert on the copy that is actually working. That
        is a much worse trade than the thing this dedup buys. The stand-down
        DMs are already deduped for the life of the boot by _standby_warned,
        _blind_warned and _wedge_warned, and a mute copy restarting often
        enough to repeat itself is a signal worth seeing anyway.

        The flag lives in state.json, on the same volume whose failure raises
        most of these warnings. On a full or read-only disk the write never
        lands and the read never sees it, so "once a day" became once a POLL
        CYCLE: _journal_down alone is called once per candidate ticker and once
        per delivery line, so one outage was a phone full of the same message.
        A process-local memo is the fallback. It cannot survive a restart,
        which is exactly the property the persisted flag has and this one does
        not claim to.
        """
        # setdefault, not self._warned_memo: several suites build a Service
        # with __new__ and stub only the plumbing a case needs
        memo = self.__dict__.setdefault("_warned_memo", set())
        if (day, key) in memo:
            return True
        # asked read only, and asked FIRST: load_state QUARANTINES a corrupt
        # state.json the moment it reads one, which resets morning_sent,
        # recap_sent and every other once-a-day guard in the same motion. A
        # warning ABOUT a broken volume must not be the thing that wipes the
        # day's dedup keys.
        healthy = config.state_health() == "ok"
        if healthy and key in (config.state_get("hb_warned", {}) or {}).get(day, []):
            return True
        if telegram.standby()[0]:
            return False  # say it, but do not touch the shared file
        if not healthy:
            memo.add((day, key))
            return False

        def upd(w):
            w = w if isinstance(w, dict) else {}
            fired = list(w.get(day, []))
            if key not in fired:
                fired.append(key)
            return {day: fired}  # keep only today's flags
        config.state_update("hb_warned", upd, default={})
        if key not in (config.state_get("hb_warned", {}) or {}).get(day, []):
            # the write did not stick (a full volume, a lock this copy could
            # not take). Remember it here, so an outage does not repeat itself
            # onto the owner's phone every cycle until the disk is fixed.
            memo.add((day, key))
        return False

    def _warn_conflict(self, description: str):
        """Telegram answered getUpdates with 409 Conflict: another process is
        polling the same bot token, so commands are being split between the
        two instances and alerts can go out twice. This used to be swallowed
        as an empty poll; now the owner hears about it, once per day.

        The text names an INVENTORY, not a location. A 409 proves competing
        requests and nothing else: not where the other consumer runs, not that
        it shares this volume, not that it is even a copy of this program. The
        old wording said "an old deploy still up, or a local run alongside the
        cloud one", which asserted both. The token never appears in any of
        this."""
        if self._hb_warned_once("tg_conflict", str(et_now().date())):
            return
        print(f"telegram getUpdates conflict: {description}")
        self._hb_owner(
            "⚠️ Heartbeat: Telegram reports another consumer is polling with "
            "the same bot token (409 Conflict). Two consumers split commands "
            "between them and can send every alert twice. This proves there "
            "are competing requests; it does NOT say where from, and the "
            "instance lock cannot see most of the places it could be. Check "
            "the whole inventory: the Railway service, every deployment on it, "
            "the replica count, any desktop Task Scheduler entry, ad hoc "
            "scripts, getUpdates utilities and the webhook configuration. "
            "Telegram said: " + description)

    # ---------- ownership: the five-state contract ----------
    # Astra section 3, implemented literally:
    #   STARTING    booted, ownership not established. local checks only.
    #   STANDBY     another cooperating process holds the lock. retries only.
    #   RECOVERING  lock held, durable state not reconciled yet. no new entries
    #               and no fresh strategy notifications.
    #   ACTIVE      ownership held AND every mandatory store reconciled.
    #   BLOCKED     lock unsupported, storage unhealthy, recovery incomplete or
    #               ownership uncertain. local reporting and bounded
    #               acquisition retries, never activation on a timer.
    #
    # What changed and why. There used to be a third "ACTIVE (DEGRADED)" state
    # that alerted with no lock at all, on the argument that a silent bot is
    # worse than a duplicate card. That is the fail open Astra A03 overturned:
    # unknown ownership is not exclusive ownership, and the same code rejected
    # blind resume one branch further down for exactly the reason it accepted
    # this one. Both are gone; "I could not tell" now lands in BLOCKED whether
    # or not this copy has ever seen a holder.
    #
    # The other half is that taking the lock is no longer the end of the story.
    # A promotion is all or nothing across every durable store, and any
    # mandatory reload failure holds the process in RECOVERING or BLOCKED even
    # though a perfectly good in-memory book is sitting right there. Preserving
    # stale memory is not permission to resume.
    #
    # There is still no ACTIVE -> STANDBY edge: a lock cannot be lost while its
    # holder lives. The lock handle is retained for the whole active lifetime
    # and the lock file is never unlinked or replaced while an owner exists.
    #
    # There IS an ACTIVE -> RECOVERING -> BLOCKED edge now, and there was not.
    # "Storage unhealthy" is in BLOCKED's entry condition above, but the health
    # question was only ever asked on the way in, so a state.json torn after
    # promotion left the copy sending and saving off a file it could no longer
    # read. It is asked on every ACTIVE pass now, read only, before anything
    # touches the file.

    # how many consecutive failed reconciles before the state is reported as
    # BLOCKED rather than RECOVERING. It keeps retrying either way: stopping
    # would brick the bot, which is the one outcome worse than going quiet.
    RECOVERY_ATTEMPTS_BEFORE_BLOCKED = 3

    def _set_ownership(self, state: str, reason: str = ""):
        """One place sets the state, and it sets it in both modules. telegram
        drives the wire gag off it; instance_lock renders /status from it."""
        instance_lock.set_state(state, reason)
        telegram.set_ownership_state(state, reason)

    def _enter_starting(self):
        """Gag the wire for the window between process start and the first
        ownership answer.

        Nothing established ownership in that window before, so a boot could
        poll getUpdates and send from a copy that turned out to be the loser
        one tick later. Placed at the top of daemon() and run_session() rather
        than in __init__ on purpose: --setup, --test and --weekly build a
        Service too, and none of them runs this machine."""
        if self.dry:
            return
        self._set_ownership(instance_lock.STARTING,
                            "ownership not established yet")

    def ensure_active(self, now: datetime) -> bool:
        """True only when this process owns the token AND has reconciled every
        mandatory store. Anything else is False and gagged.

        Fails CLOSED, which is the reversal at the heart of this package. A
        copy that cannot establish ownership does not get to alert on the
        grounds that silence is worse: silence is a known, reported, once a day
        condition, while two copies on one token is a split brain that marks
        positions closed in a file the other one never alerts on."""
        if self.dry:
            # dry-run stays OUTSIDE the ownership machine: it prints its cards
            # and still answers commands, which is what it is for. Said out
            # loud here rather than left to the module default, because the
            # default is STARTING and that would have quietly stopped a dry run
            # answering /status, which nobody asked for.
            #
            # What this line does NOT buy any more, and used to. Declaring the
            # dry run ACTIVE leaves the standby flag False, and BOTH the wire
            # gag and the shared-write gate hung off that one flag, so this
            # line was the thing that made a dry run a real sender and an
            # authorized writer of positions.json, the sniper ledger, the
            # forward ledger, the sniper day keys and the durable event
            # journal. telegram now carries a separate dry-run flag that
            # answers both questions independently of ownership
            # (telegram.set_dry_run, called from __init__), so a dry run prints
            # its cards, keeps its book in memory for the life of the process,
            # and touches nothing the live copy reads.
            #
            # The residual hazard is real and unchanged by this release: a dry
            # run uses the SAME bot token, so one running beside a live daemon
            # is a second getUpdates consumer. That is one of the things a
            # local lock cannot see (instance_lock.CANNOT_DETECT), and putting
            # dry runs under the lock is a separate decision, not this one.
            telegram.set_ownership_state(instance_lock.ACTIVE,
                                         "dry run, sends nothing")
            return True
        res = instance_lock.try_acquire()
        if res == "unavailable":
            # "I could not tell" is not "I am alone", and it never was. This is
            # a missing lock primitive OR an os.open that raised, which is what
            # a read-only remount, an EIO/ESTALE mount fault, fd exhaustion or
            # a full volume does. Both readings end in the same place now.
            #
            # There used to be a "blind resume" path here as well: after the
            # holder's published seq stood still for a while, this copy resumed
            # anyway so a volume that permanently lost locking could not mute
            # the bot forever. That premise does not hold. The lease record
            # lives on the SAME volume whose failure sent us down this branch,
            # so the fault that stops us reading the lock also stops the live
            # holder's renew from landing, and a frozen seq is a symptom of our
            # own broken disk rather than proof the holder died. And a copy
            # that cannot read the shared volume cannot read positions.json
            # coherently either, so it has no business trading on it.
            self._held_lock = False
            self._enter_blocked(now, "lock")
            return False
        if res == "contended":
            self._held_lock = False
            self._enter_standby(now)
            return False
        # acquired or held: ownership is established. That is necessary and not
        # yet sufficient.
        if res == "held" and self._held_lock and self._reconciled:
            # already ACTIVE and STILL holding the same handle. "held" is doing
            # real work in that condition: "acquired" means this process did
            # NOT have the lock a moment ago, and whatever ran in the gap could
            # have written the book, so a re-acquisition has to reconcile again
            # rather than trust a snapshot from before the gap.
            # Re-asserted every cycle rather than only on the transition,
            # because run_session re-enters STARTING at its top and this is
            # what un-gags it again.
            #
            # ACTIVE is not a state you enter once and stop checking. Astra's
            # table puts "storage unhealthy" in BLOCKED's entry condition and
            # the comment above this block repeats it, but state_health() was
            # asked in exactly one place, inside _reconcile_all, and this fast
            # path returns before ever reaching it again. There was no
            # ACTIVE -> RECOVERING or ACTIVE -> BLOCKED edge of any kind, so a
            # state.json torn AFTER promotion left this copy alerting and
            # saving off a file it could no longer read. Asked read only, and
            # BEFORE renew(): renew goes through state_update, and load_state
            # quarantines a corrupt file on sight, so renewing first would move
            # the bad file aside and hide the very thing being checked.
            health = config.state_health()
            if health != "ok":
                self._reconciled = False   # coming back has to re-read the world
                self._recovery_tries += 1
                self._reconcile_report = {"scheduled_jobs": health}
                self._hold_recovering(now, ["scheduled_jobs"])
                return False
            self._set_ownership(instance_lock.ACTIVE, "holding the lock")
            instance_lock.renew()
            if self._replay_due():
                # the SCHEDULED half of the retry. See replay_journal: the
                # promotion below runs it once, and an already ACTIVE process
                # returned here and never reached it, so MAX_DELIVERY_RETRIES
                # had no caller at all and a journaled card whose send failed
                # was never tried again while the process lived.
                self.replay_journal(now)
            return True
        self._held_lock = True
        was_standing_by = self._stood_down
        self._set_ownership(instance_lock.RECOVERING,
                            "reconciling saved state before sending")
        ok, failed = self._reconcile_all(now)
        if not ok:
            self._recovery_tries += 1
            self._hold_recovering(now, failed)
            return False
        self._reconciled = True
        self._recovery_tries = 0
        self._blocked_cause = None
        self._set_ownership(instance_lock.ACTIVE, "holding the lock")
        # the lease record is published only NOW, never before the reconcile.
        # renew goes through config.state_update, which reads state.json, and
        # load_state QUARANTINES a corrupt one on sight: renewing first moved
        # the bad file aside and rebuilt it, so the health check one line later
        # saw a clean file and this copy walked into ACTIVE on a state.json it
        # had just silently reset. Read before you write.
        instance_lock.renew(force=(res == "acquired"))
        # ON THE WAY INTO ACTIVE, and then every REPLAY_EVERY_S for as long as
        # it stays there. Astra's contract puts "replay local durable intents
        # under ownership" in RECOVERING, but the wire is gagged in every state
        # that is not ACTIVE, so a resend attempted there would be dropped and
        # then recorded as a delivery failure. So the RECONCILE happens in
        # RECOVERING, above, and the first thing the new owner does with a
        # working wire is finish what a crash left half done. This used to be
        # the ONLY call, which meant a failed send was retried on the next
        # promotion, i.e. on a healthy box, never.
        self._replay_due()      # stamp it, so the periodic driver above does
                                # not immediately repeat this pass
        self.replay_journal(now)
        if was_standing_by:
            self._promote(now, "the other copy is gone")
        return True

    # How often an already ACTIVE process re-drives replay. This is a SCHEDULE,
    # and the thing that was missing: MAX_DELIVERY_RETRIES=6 bounds attempts,
    # it does not cause them, and notify_intent deliberately does not queue
    # into pending_sends (that queue re-broadcasts to everyone). Without a
    # caller, "retried up to six times" meant "retried on the next promotion",
    # which on a healthy box is never.
    REPLAY_EVERY_S = 60

    def _replay_due(self) -> bool:
        """True at most once every REPLAY_EVERY_S, and stamps as it answers."""
        t = time_mod.monotonic()
        if t - getattr(self, "_last_replay", 0.0) < self.REPLAY_EVERY_S:
            return False
        self._last_replay = t
        return True

    # Every durable store a promotion must have in hand before this process is
    # allowed to send anything. Astra names the set: positions, sniper
    # positions, day reservations, pending deliveries, the Telegram update
    # offset, news-seen state and scheduled-job state.
    def _reconcile_all(self, now: datetime):
        """Parse every mandatory store into temporary objects, validate them
        all, and only then replace memory. Returns (ok, failed_names).

        The rule that makes this worth writing: a store that is ABSENT is a
        clean empty store (a fresh railway volume has none of these files and
        must still boot), and a store that is PRESENT and unparseable is a
        mandatory reload failure. Nothing is installed until everything parses,
        so a good positions.json beside a truncated sniper ledger leaves the
        book in memory exactly as it was instead of half updating the world.

        This replaces _resync_after_gap, which wrapped the same re-read in
        try/except, printed, and walked on into ACTIVE ungagged on the boot era
        snapshot. Folding the two edges into one function is also why they can
        no longer drift apart: there is one way into ACTIVE now."""
        report, failed = {}, []

        # "absent" is a clean empty store (a fresh railway volume has none of
        # these files and must still boot). "reset" is a store that was torn,
        # was moved aside, and rebuilds itself from scratch with nothing lost:
        # only a cache may ever answer that, and only _news_seen_health does.
        CLEAN = ("ok", "absent", "reset")

        def note(name, status):
            report[name] = status
            if status not in CLEAN:
                failed.append(name)

        # 1. storage health FIRST, before any state.json read. config.load_state
        #    QUARANTINES a corrupt state.json the first time it is read, which
        #    resets morning_sent, recap_sent, weekly_sent, learn_sent and every
        #    other once a day guard in the same motion and can re-send a day of
        #    reports with nobody told. state_health asks without touching.
        health = config.state_health()
        note("scheduled_jobs", "ok" if health == "ok" else health)
        if health != "ok":
            # every check below reads state.json. Stop here so the asking does
            # not become the damage.
            self._reconcile_report = report
            return False, failed

        # 2. the money store. parsed into temporaries, installed at the end.
        try:
            pos_status, pos_rows = self.book.parse_snapshot()
        except Exception as e:
            pos_status, pos_rows = "unreadable", []
            print(f"positions parse failed during recovery: {e}")
        note("positions", pos_status)

        # 3. the sniper ledger. _read() answers [] for a truncated file, which
        #    reads as "nothing open" and silently stops live stops being
        #    watched, so recovery consults parse_snapshot instead.
        try:
            snip_status, _snip_rows = sniper_book.parse_snapshot()
        except Exception as e:
            snip_status = "unreadable"
            print(f"sniper ledger parse failed during recovery: {e}")
        note("sniper_positions", snip_status)

        # 4. the Telegram update offset. A value int() cannot read would crash
        #    the poll or replay a week of commands.
        raw_offset = config.state_get("tg_offset", 0)
        try:
            int(raw_offset)
            note("tg_offset", "ok")
        except (TypeError, ValueError):
            note("tg_offset", "unreadable")

        # 5. pending deliveries. BOTH stores, because there are two now. The
        #    legacy queue in state.json (flush_pending iterates pending_sends,
        #    offer_add_user indexes pending_chats, so the shapes matter) and
        #    the durable journal W02 introduced, which is where every entry,
        #    exit and sniper card that is still owed actually lives. Only the
        #    legacy one was checked, so a promotion could declare "pending
        #    deliveries: ok" over daily event files the OS would not hand over,
        #    and the journal's own rebuild folds an unreadable file in as empty.
        legacy = self._shape_ok([("pending_sends", list), ("pending_chats", dict)])
        note("pending_deliveries",
             legacy if legacy != "ok" else self._event_store_health())

        # 6. day reservations: the day/symbol keys a sniper burns before it
        #    fires. A wrong shape here re-fires or blocks every ticket.
        note("day_reservations", self._shape_ok(
            [("sniper_alerted", dict), ("hb_warned", dict)]))

        # 7. news-seen. Its own file, read by the news thread. Absent is clean,
        #    and so is a torn one now: see _news_seen_health.
        note("news_seen", self._news_seen_health())

        # scheduled-job day keys share state.json, already health checked
        # above. They are read through on every access, so nothing is cached to
        # go stale; what matters is that a dict or list did not land in a slot
        # the code compares to a date string, because that never matches and
        # re-sends the report every cycle.
        for key in ("morning_sent", "recap_sent", "weekly_sent", "learn_sent",
                    "request_digest_sent", "forward_graded"):
            val = config.state_get(key)
            if isinstance(val, (dict, list)):
                note(f"scheduled_jobs:{key}", "unreadable")

        self._reconcile_report = report
        if failed:
            return False, failed

        # ---- everything parsed. install, all at once. ----
        try:
            self.book.install(pos_rows)
        except Exception as e:  # an install that raises is a failed recovery
            print(f"installing the position book failed: {e}")
            return False, ["positions"]
        try:
            # the day goes with it: reset_day re-reads live_params.json and the
            # overnight backtest reports, which a copy that sat out the night
            # would otherwise trade its first morning on.
            self.day = None       # force the rebuild even on the same date
            self.reset_day(now)
        except Exception as e:
            print(f"day rebuild during recovery failed: {e}")
            return False, ["day_rebuild"]
        return True, []

    @staticmethod
    def _event_store_health() -> str:
        """The DURABLE delivery store: "ok", "absent" or "unreadable".

        The daily event files are where a committed-but-undelivered card
        actually lives, and a promotion never looked at them. That matters more
        than it sounds, because event_journal's rebuild folds an unreadable
        daily file in as EMPTY (read_jsonl answers value None for status
        "unreadable" and the fold does `res.value or []`), so a file the OS
        will not hand over contributes zero events, raises no error and shows
        up in /health as a clean bill. Asking here, before ACTIVE, is what
        turns that into a refused promotion.

        The same window the journal rebuilds from is checked, day for day. The
        INDEX is deliberately NOT a blocker: it is a materialized view and a
        missing or unreadable one is documented to rebuild from these files.

        Only "unreadable" blocks. A file with a torn LAST LINE reads as
        "corrupt" and read_jsonl still returns its parseable prefix, which is
        real evidence: the only line lost is the one a crash was in the middle
        of writing, which by definition never completed. Refusing on that would
        brick the bot on the most ordinary crash there is, which is the trap
        news_seen.json was already caught in."""
        day = et_now().date()
        seen_any = False
        for back in range(event_journal.INDEX_RETAIN_DAYS + 2):
            try:
                p = event_journal.daily_path(
                    (day - timedelta(days=back)).isoformat())
            except event_journal.JournalUnavailable as e:
                print(f"durable event directory unusable: {e}")
                return "unreadable"
            res = storage_io.read_jsonl(p)
            if res.status == "missing":
                continue
            seen_any = True
            if res.status == "unreadable":
                print(f"durable event file {p.name} is unreadable: "
                      f"{res.error}. a file the OS will not hand over is not "
                      "an empty one, and the journal's rebuild folds it in as "
                      "empty, so nothing downstream would ever notice.")
                return "unreadable"
        return "ok" if seen_any else "absent"

    @staticmethod
    def _shape_ok(pairs) -> str:
        """"ok" when every (key, type) in state.json holds that type or is
        absent, "unreadable" otherwise. Absent is clean on purpose."""
        for key, want in pairs:
            val = config.state_get(key)
            if val is not None and not isinstance(val, want):
                print(f"state key {key} has shape {type(val).__name__}, "
                      f"expected {want.__name__}")
                return "unreadable"
        return "ok"

    def _news_seen_health(self) -> str:
        """news_seen.json: "ok", "absent" or "reset". Never a promotion
        blocker, and that is the correction.

        It WAS one. news_seen.json is in Astra's mandatory promotion set, and
        it was also the only store in that set still written with a plain
        truncate-then-write whose OSError was swallowed, so one SIGKILL inside
        the 12 second news rewrite, or one ENOSPC, left a torn file that
        stranded the sole instance in BLOCKED forever on a healthy disk: open
        positions never monitored, no alerts, and no human-free way back. The
        write is atomic now (see _save_news_seen), which closes the vector, and
        this closes the trap it opened.

        A rebuildable cache is not the position book. Everything this file
        holds is "which headlines have I already texted TODAY", and the news
        thread's own first-pass-of-the-day branch seeds every current headline
        SILENTLY, so resetting it costs at most one duplicate BREAKING text for
        a headline that dropped out of the feed and came back. Losing the
        position book costs a live trade nobody is watching. They are not the
        same class of thing and they must not have the same failure mode, so a
        torn one is moved aside once and reported as reset, and the promotion
        continues.

        Astra's "promotion is all or nothing" is still honored: the store is
        reconciled to a known good value before ACTIVE, and what changed is
        that the known good value for a cache the bot can regenerate is empty
        rather than unreachable."""
        try:
            if not config.NEWS_SEEN_FILE.exists():
                return "absent"
        except OSError:
            return "reset"
        res = storage_io.read_json(config.NEWS_SEEN_FILE)
        if res.status == "missing":
            return "absent"
        if res.usable and isinstance(res.value, dict):
            return "ok"
        bad = config.NEWS_SEEN_FILE.with_suffix(".corrupt")
        try:
            if not bad.exists():
                config.NEWS_SEEN_FILE.replace(bad)
            else:
                config.NEWS_SEEN_FILE.unlink()
            print(f"news_seen.json unreadable ({res.status}), moved aside; "
                  "today's headlines will be re-seeded silently on the next "
                  "news pass. No alert is lost by this and none is replayed.")
        except OSError as e:
            # even the move failed. Still not a blocker: _load_news_seen reads
            # a bad file as an unseeded day, which seeds silently and then
            # republishes the file, so it heals itself on the first pass.
            print(f"news_seen.json unreadable and could not be moved aside "
                  f"({e}); the news thread will re-seed over it.")
        return "reset"

    def _hold_recovering(self, now: datetime, failed):
        """Hold the lock, stay gagged, keep retrying, and say why.

        This is the branch _promote used to not have. It wrapped the re-read in
        try/except, printed, and continued into ACTIVE on the boot era
        snapshot, so a truncated positions.json produced a copy that alerted
        and saved from a book it could not verify. An older in-memory book is
        NOT permission to resume."""
        names = ", ".join(sorted(set(failed))) or "unknown"
        overdue = self._recovery_tries > self.RECOVERY_ATTEMPTS_BEFORE_BLOCKED
        state = instance_lock.BLOCKED if overdue else instance_lock.RECOVERING
        self._set_ownership(state, f"could not reconcile {names}")
        print(f"instance {state.lower()}: holding the lock but NOT sending, "
              f"could not read {names} (attempt {self._recovery_tries}). "
              "an in-memory copy of the book is not permission to resume.")
        if self._recovery_warned or self._hb_warned_once(
                "instance_recovery", str(now.date())):
            return
        self._recovery_warned = True
        self._hb_owner(
            "Heartbeat: I hold the single-instance lock but I cannot read my "
            f"own saved state ({names}), so I am NOT sending alerts and NOT "
            "opening anything new. I have an older copy of the book in memory "
            "and that is not good enough to trade on: I would be acting on a "
            "picture of the world I could not verify. I keep retrying every "
            "cycle and start sending the moment those files read cleanly. "
            "Check the volume. The Railway logs show this on every cycle, "
            "which is the record to trust if this message does not arrive. "
            f"Instance {instance_lock.instance_id()}, pid {os.getpid()}.")

    def _enter_blocked(self, now: datetime, cause: str):
        """Ownership could not be established, so nothing is sent.

        cause is "lock" (no primitive here, or the lock file could not be
        opened at all). Same gag as standby, different reason, so the owner is
        told the real one and does not go hunting for a second container that
        is not there. This replaces the old ACTIVE-DEGRADED branch, which
        un-gagged the wire and DMed "alerts keep flowing normally"."""
        self._set_ownership(instance_lock.BLOCKED,
                            "the instance lock could not be read here")
        # any earlier reconciliation is void: it was made under an ownership
        # claim that no longer stands, so coming back has to re-read the world
        # rather than resume on what was true before the volume broke.
        self._reconciled = False
        t = time_mod.monotonic()
        if t - self._standby_print >= 60:
            self._standby_print = t
            # stdout FIRST and every cycle. A DM needs a working network and
            # can be duplicated by several blocked copies, so the platform log
            # is the record, not the text message.
            print("BLOCKED: the instance lock could not be read, so ownership "
                  "is unknown and I am not sending. a read that failed is not "
                  "proof I am alone. " + instance_lock.cannot_detect_line()
                  + " last holder: " + instance_lock.holder_line())
        if self._blocked_cause == cause and self._blind_warned:
            return
        self._blocked_cause = cause
        self._blind_warned = True
        if self._hb_warned_once("instance_blind", str(now.date())):
            return
        self._hb_owner(
            "Heartbeat: I could not establish that I am the only copy running "
            "(the lock is unsupported on this filesystem or the lock file "
            "cannot be opened), so I am NOT sending alerts and NOT monitoring "
            "positions. Unknown ownership is not the same as being alone, and "
            "I will not un-mute myself on a timer: the lease record lives on "
            "the same volume that is failing, so nothing I can read here tells "
            "me whether another copy is alive or my disk is broken. I retry "
            "every cycle and come back on my own the moment the lock works. "
            + instance_lock.cannot_detect_line()
            + " You get this at most once a day, and it needs a working "
            "network to arrive at all, so the Railway logs are the record: "
            "they carry this line every cycle. Instance "
            f"{instance_lock.instance_id()}, pid {os.getpid()}.")

    def _promote(self, now: datetime, reason: str = "the other copy is gone"):
        """The ceremony after a stand-down ends, run only once the reconcile
        has actually succeeded.

        The re-read that used to live here is now _reconcile_all, and it runs
        BEFORE this, because announcing a promotion that then turns out to be
        unreadable is how a copy talked itself into ACTIVE on a stale book.

        Deliberately NOT reloaded: every state.json key (morning_sent,
        recap_sent, sniper_alerted, hb_warned, pending_sends) is read off disk
        on each access, so there is nothing cached to go stale and the
        once-a-day guards still hold across a promotion. _bars_cache expires
        itself after POLL_SECONDS. _last_feed_ok / _last_feed_try stay None on
        purpose: this copy really has not fetched anything yet, and claiming
        the winner's fetches would blind the feed-dead check."""
        self._standby_warned = False
        self._wedge_warned = False
        self._blind_warned = False
        self._recovery_warned = False
        self._stood_down = False
        self._blocked_cause = None
        self._seqwatch = instance_lock.SeqWatch()
        instance_lock.renew(force=True)
        print(f"instance lease: promoted to active, {reason}.")
        self._hb_owner(
            f"Heartbeat: promoted to active, {reason}. I am now the one "
            "sending alerts and answering commands. Instance "
            f"{instance_lock.instance_id()}, pid {os.getpid()}.")
        try:
            self.check_downtime_on_start(now)
        except Exception as e:
            print(f"downtime check after promotion failed: {e}")

    def _enter_standby(self, now: datetime):
        """Go mute. The wire gate stops every broadcast, every chat reply and
        the getUpdates poll itself, which is what actually clears the 409."""
        self._set_ownership(instance_lock.STANDBY,
                            "another instance holds the lease")
        # remembered for the rest of this boot: a later promotion runs the
        # stand-down ceremony (the DM, the downtime check) rather than a silent
        # first acquisition.
        self._stood_down = True
        self._reconciled = False
        lease = instance_lock.read_lease()
        t = time_mod.monotonic()
        if t - self._standby_print >= 60:  # say why the railway log is quiet
            self._standby_print = t
            print("standby: another copy holds the instance lock. not "
                  "polling, not sending, not monitoring. holder: "
                  + instance_lock.holder_line(lease))
        if self._standby_warned:
            return
        self._standby_warned = True
        if self._hb_warned_once("instance_standby", str(now.date())):
            return
        self._hb_owner(
            "Heartbeat: a second copy of this bot is already running, so I "
            "stood down. I am NOT sending alerts, NOT answering commands and "
            "NOT monitoring positions while the other copy is up. It holds "
            "the lease: " + instance_lock.holder_line(lease) + ". I will take "
            "over on my own the moment it stops. Nothing to do unless both "
            "copies were meant to be up.")

    def standby_wait(self, now: datetime):
        """One wait between ownership attempts, for STANDBY, RECOVERING and
        BLOCKED alike. Never exits the process: a copy that quit here would not
        come back when the other one is torn down, and railway's restartPolicy
        would just start it into the same contention. It waits, and takes over
        within one poll of the lock freeing.

        The wait is a RETRY interval, not a countdown to activation. Nothing
        here promotes anything, and no elapsed time is ever compared against a
        threshold that would.

        Also watches for a WEDGED winner: the lock is still held (so the
        process is alive) but its seq has not advanced for STALE_S of this
        observer's own monotonic clock. That gets a DM and nothing else.
        Stealing from a live-but-hung process is the split brain the whole
        lease exists to prevent."""
        lease = instance_lock.read_lease()
        frozen = self._seqwatch.observe(lease)
        # ...and only about a DIFFERENT copy. This wait now also serves
        # RECOVERING and BLOCKED-from-a-failed-reconcile, which are the two new
        # states where THIS process is the lock holder. renew() is only called
        # on the way into ACTIVE, so during an outage the lease record stands
        # still because the sole instance is the one not renewing it, and the
        # frozen-seq branch fired at itself: the owner was DMed that "the
        # active copy still holds the lock but has not renewed" and told to
        # restart it, minutes after an accurate DM saying this copy cannot read
        # its own state. Restarting changes nothing and the two messages
        # contradict each other. holding() is pid-checked, so a child that
        # merely inherited the handle still answers False.
        if instance_lock.holding():
            time_mod.sleep(instance_lock.STANDBY_POLL_S)
            return
        if frozen >= instance_lock.STALE_S and not self._wedge_warned:
            self._wedge_warned = True
            if not self._hb_warned_once("instance_wedged", str(now.date())):
                self._hb_owner(
                    "Heartbeat: the active copy of the bot still holds the "
                    f"lock but has not renewed in about {frozen / 60:.0f} "
                    "min, so it may be wedged and open positions may not be "
                    "monitored. I am standing by and will NOT take over on my "
                    "own. Holder: " + instance_lock.holder_line(lease)
                    + ". Restarting the stuck copy hands me the lease.")
        time_mod.sleep(instance_lock.STANDBY_POLL_S)

    def health_stamp(self, now: datetime):
        """Throttled 'I'm alive' stamp to state.json (~once a minute)."""
        t = time_mod.monotonic()
        if t - self._health_last_stamp < 55:
            return
        self._health_last_stamp = t
        config.state_set("heartbeat", {"ts": now.isoformat(), "day": str(now.date())})

    RECORDER_HEALTH_S = 300     # W06 record 6 cadence, monotonic

    def recorder_health(self, now: datetime):
        """Write one coverage row every few minutes.

        Throttled on the MONOTONIC clock, like every other duration in this
        bot, so a container whose wall clock jumps cannot make the recorder
        either spam or go quiet. It runs AFTER monitor_positions in the cycle
        and it only enqueues, so it can never sit in front of a stop."""
        t = time_mod.monotonic()
        if t - getattr(self, "_rec_health_at", 0.0) < self.RECORDER_HEALTH_S:
            return
        self._rec_health_at = t
        try:
            trade_recorder.record_health(
                ownership_state=instance_lock.state(),
                instance_id=instance_lock.instance_id())
        except Exception as e:                                 # noqa: BLE001
            print(f"{now:%H:%M:%S} recorder: health row not written: {e}")

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
                f"({last.astimezone(CT):%I:%M}–{now.astimezone(CT):%I:%M %p} CT) "
                "during market hours. "
                "Back up now. Check for any missed alerts.")

    def health_check(self, now: datetime):
        """Once-a-cycle, in-session health checks. Owner-only DMs."""
        if self.dry:
            return
        self.health_stamp(now)
        self.recorder_health(now)
        if now.weekday() >= 5 or not (MONITOR_START <= now.time() <= time(16, 0)):
            return
        last_ok = self._last_feed_ok
        last_try = self._last_feed_try
        if last_ok is None:  # no data has loaded at all this session
            if (now.time() >= time(10, 0) and not self._feed_none_warned
                    and last_try is not None):
                self._feed_none_warned = True
                self._hb_owner(
                    "⚠️ Heartbeat: no market data has loaded yet this session. "
                    "The feed may be down. No setups can fire until it recovers.")
            return
        self._feed_none_warned = False
        stale = (now - last_ok).total_seconds() / 60
        # Only a fetch that was TRIED and came back empty means the feed is
        # dead. With no open position and the entry window shut the loop has
        # nothing to fetch, and that silence is the bot idling, not the feed
        # dying. (8/21: the last SPY runner sold at 10:31 CT and a "feed
        # returned nothing for ~10 min" text went out at 10:41 CT, ten minutes
        # of nothing-to-do later.)
        failing = last_try is not None and last_try > last_ok
        if stale >= self.FEED_STALE_MIN and failing and not self._feed_warned:
            self._feed_warned = True
            self._hb_owner(
                f"⚠️ Heartbeat: the data feed has returned nothing for ~{stale:.0f} "
                "min during market hours. Setups/exits may be stalled. Check it.")
        elif stale < self.FEED_STALE_MIN and self._feed_warned:
            self._feed_warned = False
            self._hb_owner("✅ Heartbeat: data feed recovered.")

    def health_eod(self, now: datetime):
        """One owner-only end-of-session 'all clear', so silence is meaningful:
        no close ping = something is wrong.

        A holiday gets no all-clear, because there was no session to clear.
        Silence still means something that day: the eve-of-holiday notice sent
        the night before is what says the quiet is expected."""
        if self.dry or not is_session_day(now.date()) or now.time() < WEEKLY_AT:
            return
        today = str(now.date())
        if self._hb_warned_once("eod", today):
            return
        n = len(self.book.for_date(now.date()))
        feed = (f"OK (last {self._last_feed_ok.astimezone(CT):%I:%M %p} CT)"
                if self._last_feed_ok else "NO DATA seen")
        warns = [k for k in (config.state_get("hb_warned", {}) or {}).get(today, [])
                 if k != "eod"]
        tail = ("  Heads-up today: " + ", ".join(warns)) if warns else ""
        self._hb_owner(
            f"✅ Heartbeat: session done. {n} alert{'s' if n != 1 else ''} sent, "
            f"feed {feed}.{tail}  (Once-a-day check: no close ping from me means "
            "something's wrong.)")

    @staticmethod
    def _brain_status() -> str:
        try:
            import assistant
            return assistant.brain_status_line()
        except Exception as e:
            return f"unknown ({e})"

    @staticmethod
    def _next_closure_line(today: date) -> str:
        """The next market holiday, for /health. Scans a year ahead so the
        answer is never 'none' just because the year rolled over."""
        d = today
        for _ in range(400):
            d += timedelta(days=1)
            name = market_calendar.holiday_name(d)
            if name:
                return f"{name} on {d:%a %b} {d.day}"
        return "none found"

    @staticmethod
    def _sniper_line() -> str:
        """The morning card's sniper-window sentence, from the live spec."""
        try:
            import strategy_spec
            return strategy_spec.get().sniper_watch_sentence()
        except Exception:
            return ""

    def health_text(self) -> str:
        """/health: on-demand snapshot for the owner."""
        now = et_now()
        hb = config.state_get("heartbeat") or {}
        last_ok = self._last_feed_ok
        today = str(now.date())
        open_n = sum(1 for p in self.book.positions if p.state != "closed")
        warns = [w for w in (config.state_get("hb_warned", {}) or {}).get(today, [])
                 if w != "eod"]
        try:  # heartbeat ts is stored as an ET iso string — show it in CT
            hb_disp = f"{datetime.fromisoformat(hb['ts']).astimezone(CT):%a %I:%M %p CT}"
        except (KeyError, TypeError, ValueError):
            hb_disp = "n/a"
        lines = [
            "🩺 BOT HEALTH",
            f"Now: {now.astimezone(CT):%a %I:%M %p CT}",
            f"Last heartbeat: {hb_disp}",
            (f"Feed last OK: {last_ok.astimezone(CT):%I:%M %p CT}" if last_ok
             else "Feed last OK: not yet this run"),
            f"Morning card today: "
            f"{'sent' if config.state_get('morning_sent') == today else 'NOT sent'}",
            f"Alerts today: {len(self.book.for_date(now.date()))}",
            f"Open positions: {open_n}",
            f"Sniper trades open: {len(sniper_book.open_rows())}",
            f"Brain: {self._brain_status()}",
            f"Today: {market_calendar.describe(now.date())}",
            f"Next close: {self._next_closure_line(now.date())}",
            config.api_usage_line(),
        ]
        # the durable event picture. unknown deliveries and orphans are the two
        # things nothing else in this text would ever show, and an unknown
        # delivery in particular is a card that may or may not have arrived, so
        # it has to be visible rather than resolved by guessing.
        try:
            lines.append(event_journal.report_text())
        except Exception as e:                                  # noqa: BLE001
            lines.append(f"Durable events: cannot be read ({e})")
        # W06 coverage, in the owner's own health text. A completeness word
        # nobody can see is a completeness word nobody checks.
        try:
            lines.append(trade_recorder.report_text())
        except Exception as e:                                  # noqa: BLE001
            lines.append(f"Recorder: cannot be read ({e})")
        # W07 coverage. The silent recipients are the number that matters here:
        # they are UNKNOWN, and a health text that showed only the reports
        # would read as full coverage of a lane almost nobody uses.
        try:
            lines.append(fill_journal.report_text())
        except Exception as e:                                  # noqa: BLE001
            lines.append(f"Fills: cannot be read ({e})")
        jh = config.state_get("journal_health") or {}
        if jh.get("ok") is False:
            lines.append("Durable event journal UNAVAILABLE since "
                         f"{jh.get('at', 'unknown')}: {jh.get('why', '')}. "
                         "No new entry alerts. Open positions are still "
                         "monitored and their exits still send.")
        ph = config.state_get("position_health") or {}
        if ph.get("ok") is False:
            # a card that went out with no tracked position behind it. Said in
            # its own line because the orphan count above is a number and this
            # is the sentence that says what the number means for a trade
            lines.append(
                f"A {ph.get('kind', 'position')} card went out at "
                f"{ph.get('at', 'unknown')} for {ph.get('what', 'a trade')} "
                f"that I could NOT save ({ph.get('why', '')}). It is not being "
                "watched. Replay retries the write on every pass and it stays "
                "in the orphan count until it lands.")
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
            # bogus "UPDATE: RED" escalation for a mode that was already sent
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
                self.notify("UPDATE: " + cards.morning_card(
                    mode, reason, today,
                    window_ct=cards.entry_window_ct(self.cfg)))
            return
        self.morning_sent_for = today
        # count the broadcast BEFORE it happens: a crash between the notify
        # below and the morning_sent write used to re-text the card on every
        # restart. Past the cap, adopt the mark without sending again.
        if not self.dry:
            n = self._job_attempt("morning", str(today))
            if n > self.MAX_JOB_ATTEMPTS:
                config.state_set("morning_sent", str(today))
                print(f"morning card already attempted {n - 1} times today "
                      "(restart between send and mark?); marking done")
                return
        card = cards.morning_card(mode, reason, today,
                                  window_ct=cards.entry_window_ct(self.cfg),
                                  sniper_line=self._sniper_line())
        try:  # earnings radar + hot headlines (news must never block the report)
            extra = news.morning_lines(self.cfg.watchlist)
            if extra:
                card += "\n" + "\n".join(extra)
        except Exception as e:
            print(f"news scan failed: {e}")
        if config.paper_mode():
            card += "\n[PAPER MODE is ON: cards are practice, not trades.]"
        self.notify(card)
        if not self.dry:  # dry-run must not set the live morning dedup key
            config.state_set("morning_sent", str(today))
        # if the card went out AFTER the entry window opened, we came up late
        # (likely a slow restart) — tell the owner so a missed open isn't silent.
        if (not self.dry and now.weekday() < 5 and now.time() > self.cfg.entry_start
                and not self._hb_warned_once("late_open", str(today))):
            self._hb_owner(
                f"⚠️ Heartbeat: morning card went out at "
                f"{now.astimezone(CT):%I:%M %p} CT, after the "
                f"{ct_wall(self.cfg.entry_start):%H:%M} entry window opened. "
                "I may have missed early setups today.")

    # ---------- telegram commands ----------

    def handle_commands(self, timeout: int = 0):
        if self.dry:
            return
        try:
            items, max_id = telegram.get_messages(timeout=timeout)
        except RuntimeError:
            return
        conflict = telegram.poll_conflict()
        if conflict:
            self._warn_conflict(conflict)
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
            if item["cmd"] == "/fill":
                # W07. Deliberately NOT in ADMIN_CMDS: three recipients get one
                # card, and each of their fills is a separate execution
                # experience of that one signal. An owner-only lane could only
                # ever record one of the three.
                #
                # Bare /fill is somebody asking how this works, not a report
                # missing its numbers, so it gets the interface rather than a
                # complaint about the numbers it never claimed to have.
                if not (item.get("args") or "").strip():
                    return cards.fill_help_card()
                return self.try_fill_report(item) or cards.fill_help_card()
            return self.run_command(item["cmd"], item["args"], item["chat_id"])
        if item["kind"] == "unsupported":
            return ("I can read text, photos, PDFs and CSV/TXT files, "
                    "not voice or video yet.")
        if item["kind"] == "text":
            # W07: a reply that REPORTS a real fill is logged rather than sent
            # to the brain. looks_like_report is narrow on purpose. A question
            # about the same alert card is still a question and still goes to
            # the brain, because hijacking the chat for a record nobody asked
            # for is a worse bug than a missing row.
            logged = self.try_fill_report(item)
            if logged:
                return logged
        import assistant
        if not assistant.enabled():
            return ("I see your message, but my brain isn't plugged in yet. "
                    "Add ANTHROPIC_API_KEY to the bot's .env "
                    "(get one at console.anthropic.com) and I'll answer "
                    "questions, read chart screenshots, and read files like "
                    "a human. Commands still work any time: /help")
        self._dispatch_brain(item)
        return None  # the worker thread sends the reply itself

    def _dispatch_brain(self, item: dict):
        """Answer a free-form chat message on a BACKGROUND thread so the main
        trading loop NEVER blocks on a slow brain call. A stalled Anthropic
        request (up to ~120s per turn, longer across tool calls) must never
        stall the next cycle's entry scans or, far worse, the +25%/give-back/
        STOP exit monitoring. This mirrors how breaking-news reads already run
        off-loop. A typing heartbeat keeps 'Sniper is typing…' alive until the
        reply is sent. The status snapshot is taken HERE, on the loop thread
        (serialized with scan/monitor), so the worker never races the book."""
        import threading

        import assistant
        chat_id = item["chat_id"]
        status = self.status_text()  # snapshot on the loop thread (race-free)

        def worker():
            stop = threading.Event()

            def beat():
                while not stop.is_set():
                    telegram.send_chat_action(chat_id)
                    stop.wait(3)

            threading.Thread(target=beat, daemon=True).start()
            attachments = []  # high-conviction reads the brain pulled, to chart
            try:
                reply = assistant.respond(item, status, attachments=attachments)
            except Exception as e:
                reply = f"that one hit an error on my end: {e}"
            finally:
                stop.set()
            if reply:
                telegram.send_to(chat_id, reply)
            # when a read held conviction (FVG confirmed the plan), send the
            # marked-up FVG chart right after the text — everything per usual,
            # then the fig with the arrows. One per symbol, never blocks the reply.
            self._send_fvg_charts(chat_id, attachments)

        threading.Thread(target=worker, daemon=True, name="brain-reply").start()

    def _send_fvg_charts(self, chat_id: str, reads: list):
        """Text a candlestick FVG chart for each high-conviction read (deduped by
        symbol). Best-effort: a render/send hiccup never disturbs the chat."""
        seen = set()
        for r in reads or []:
            tk = r.get("ticker") or r.get("symbol")
            if not tk or tk in seen:
                continue
            seen.add(tk)
            try:
                import charts
                img, _ = charts.render_fvg(r)
                if img:
                    d = (r.get("plan") or {}).get("direction", "")
                    fresh = ("real-time (IEX)"
                             if str(r.get("source") or "").startswith("alpaca")
                             else "~15m delayed")
                    cap = f"{tk} {d} · FVG confirmed. {fresh}, your call."
                    telegram.send_photo(chat_id, img, caption=cap[:1024])
            except Exception as e:
                print(f"fvg chart send skipped for {tk}: {e}")

    # ---------- W07: the optional fill lane ----------

    # How far back a reply can still be about. A correction that arrives the
    # next morning is one of the cases Astra names, so a same-day window would
    # refuse the exact repair the schema asks for. Bounded anyway, because an
    # unbounded walk of every intent ever written belongs nowhere near the
    # command path.
    FILL_REPLY_DAYS = 5

    def fill_candidates(self) -> list:
        """Every alert a reply could be about, shaped the way fill_journal
        resolves against.

        Built from the durable journal rather than from memory, so a restart
        does not lose the link between a card that went out and the trade
        behind it. Wrapped whole: this is an observer, and if the journal
        cannot be read the person simply gets told their reply could not be
        placed, which is honest and costs nothing else."""
        out = []
        try:
            cutoff = (et_now() - timedelta(days=self.FILL_REPLY_DAYS)).date()
            states = {p.id: p.state for p in self.book.positions}
            for intent in event_journal.all_intents():
                if intent.kind not in ("entry", "exit", "sniper_entry",
                                       "sniper_exit"):
                    continue
                try:
                    if date.fromisoformat(intent.session_date) < cutoff:
                        continue
                except (TypeError, ValueError):
                    pass          # an unparseable day is kept, never dropped
                payload = intent.payload or {}
                pos = payload.get("position") or {}
                mids = {}
                for d in intent.deliveries():
                    if d.provider_message_id is not None:
                        mids.setdefault(d.recipient_ref, []).append(
                            d.provider_message_id)
                out.append({
                    "position_id": intent.position_id,
                    "candidate_id": intent.candidate_id,
                    "contract_id": self._fill_contract_id(pos),
                    "symbol": (payload.get("ticker") or pos.get("ticker")
                               or payload.get("symbol") or ""),
                    "kind": ("entry" if intent.kind.endswith("entry")
                             else "exit"),
                    "text": intent.text or "",
                    "state": states.get(intent.position_id, ""),
                    "message_ids": mids,
                })
        except Exception as e:                                 # noqa: BLE001
            print(f"fill lane: candidates unavailable ({e})")
            return []
        return out

    @staticmethod
    def _fill_contract_id(pos: dict) -> str:
        """The OCC style name of the contract, or empty. Empty and not a guess:
        record 5 references contract metadata, and a made up reference is worse
        than a missing one."""
        try:
            return trade_recorder.contract_id_for(
                pos["ticker"], pos["right"], pos["strike"], pos["expiry"])
        except Exception:                                      # noqa: BLE001
            return ""

    def try_fill_report(self, item: dict):
        """One inbound message, offered to the fill journal. Returns the reply
        to send, or None to leave the message where it was going.

        OPTIONAL, and never a nag. Nothing in this path ever starts a
        conversation: it only answers one. A person who never replies costs
        nothing and their rows stay unknown.

        OBSERVER, per constraint 10. A fault here hands the message back to the
        brain and never reaches the exit loop, which does not call any of
        this."""
        try:
            # getattr, because several suites build a Service through __new__
            # with no __init__, so an attribute that is not there means "not a
            # dry run", exactly as it does for the other observers here.
            if getattr(self, "dry", False):
                return None       # a dry run must not write the shared journal
            text = item.get("text") or ""
            if item.get("kind") == "command":
                text = f"{item.get('cmd', '')} {item.get('args', '')}".strip()
            reply_ref = item.get("reply_to") or {}
            is_reply = reply_ref.get("message_id") is not None
            if not fill_journal.looks_like_report(text,
                                                  is_reply_to_alert=is_reply):
                return None
            # the opaque handle, never the chat id. The journal files must not
            # be able to name a person even if somebody reads them.
            user = event_journal.recipient_ref(item["chat_id"])
            d = fill_journal.handle_message(
                user_ref=user, text=text, candidates=self.fill_candidates(),
                message_id=item.get("message_id"),
                reply_message_id=reply_ref.get("message_id"),
                reply_text=reply_ref.get("text") or "",
                now_utc=item.get("sent_at_utc"))
            if d.get("status") == fill_journal.NOT_A_REPORT:
                return None
            return d.get("reply") or None
        except Exception as e:                                 # noqa: BLE001
            print(f"fill lane: reply not logged ({e})")
            return None

    def fills_text(self, chat_id: str = "") -> str:
        """/fills: the caller's OWN reported fills, and the denominator.

        One person's rows only. Two recipients reporting on one card are two
        execution experiences of ONE signal, and they are also not each other's
        business."""
        try:
            me = event_journal.recipient_ref(chat_id) if chat_id else ""
            cov = fill_journal.coverage()
            rows = []
            for pid in sorted({r.get("position_id")
                               for r in fill_journal.fills(user_ref=me)}):
                mine = fill_journal.reconcile(pid)["by_user"].get(me) or {}
                sym = ""
                for r in fill_journal.fills(pid, me):
                    sym = r.get("symbol") or sym
                rows.append({"position_id": pid, "symbol": sym, "me": mine})
            return cards.fills_card(cov, rows,
                                    len(fill_journal.unknown_signals()))
        except Exception as e:                                 # noqa: BLE001
            return f"I could not read the fill journal just now: {e}"

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
                  "/done", "/reqfrom", "/backlog", "/proposals", "/reload",
                  "/brain", "/calendar", "/closed", "/open"}

    def run_command(self, cmd: str, args: str, chat_id: str = ""):
        if cmd in self.ADMIN_CMDS and not telegram.is_owner(chat_id):
            return ("That's an owner-only command. You can use /status, "
                    "/score, /help, or just talk to me.")
        if cmd in ("/closed", "/open"):
            return self.cmd_closed(cmd, args)
        if cmd == "/brain":
            # the chat goes with it: the API check runs off this thread and
            # the worker texts the verdict back to whoever asked. the
            # ADMIN_CMDS gate above already turned away everyone but an owner.
            return self.cmd_brain(chat_id)
        if cmd == "/calendar":
            return self.cmd_calendar()
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
        if cmd == "/fills":
            # W07, and open to everyone for the same reason /fill is: each
            # recipient can only ever see and log their own execution of the
            # one signal.
            return self.fills_text(chat_id)
        if cmd == "/health":
            return self.health_text()
        if cmd == "/test":
            self.test_sequence(chat_id)
            return None
        if cmd in ("/calls", "/opt", "/option", "/puts"):
            return self.calls_text(args)
        if cmd == "/gold":
            return self.read_text("gold")
        if cmd in ("/fx", "/forex"):
            if args.strip():
                return self.read_text(args)
            return ("Which pair? I read EUR/USD, GBP/USD, USD/JPY, AUD/USD, "
                    "USD/CAD, USD/CHF.  e.g.  /fx eurusd   (or /gold for gold)")
        if cmd in ("/signal", "/plan", "/trade"):
            if args.strip():
                return self.read_text(args)
            return ("Which symbol? e.g.  /signal xauusd  ·  /signal eurusd  ·  "
                    "/signal btc  ·  /signal nvda")
        if cmd in ("/chart", "/pic", "/img"):
            return self.chart_reply(args, chat_id)
        if cmd in ("/ask", "/deep"):
            return self.ask_reply(args, chat_id)
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
        if cmd == "/proposals":
            import learn
            return learn.proposals_command(args)
        if cmd == "/reload":
            return self.cmd_reload()
        if cmd in ("/help", "/start"):
            return cards.help_card()
        # bare-symbol shortcut: /spx /qcom /gold /usdjpy /eurusd ... just work.
        # macro_read returns an error (no network) for anything it doesn't cover,
        # so an unknown command still falls through to None below.
        sym = cmd.lstrip("/")
        if sym.upper() in self.cfg.watchlist:
            return self.calls_text(sym)
        if sym:
            import market_tools
            if market_tools.resolve(sym):  # plausible symbol (no network yet)
                r = market_tools.read_any(sym)
                if not r.get("error") and not r.get("note"):
                    return cards.macro_line(r)  # stay silent on a failed fetch
        return None  # silently ignore unknown commands

    def cmd_reload(self):
        """Owner: re-read the backtest reports and live_params.json right
        now instead of waiting for the overnight date flip, and reply with
        exactly what is live. This is how an approved rule change gets
        applied without a redeploy: edit live_params.json, then /reload."""
        self.reload_tunables()
        stats = ("loaded" if self.backtest_old is not None
                 else "MISSING (no entry alerts until backtest.py runs)")
        # an allow-listed setup with no backtest stats structurally cannot
        # alert (gate_stats refuses without real numbers) — say so here
        # instead of letting the owner wait weeks for a silent no-op
        per = (self.backtest_old or {}).get("per_setup", {})
        allow = ", ".join(
            k if k in per else f"{k} (no backtest stats yet, cannot alert)"
            for k in sorted(self.ALLOWED_SETUPS))
        return "\n".join([
            "Reloaded. Live settings now:",
            f"Backtest stats: {stats}",
            f"Live params: {self.live_params_note}",
            f"Alert allow-list: {allow}",
            f"Watchlist: {', '.join(self.cfg.watchlist)}",
            (f"Entry window: {self.cfg.entry_start:%H:%M}-"
             f"{self.cfg.entry_end:%H:%M} ET"),
            f"Momentum window: {self.cfg.mom_bars} bars "
            f"({self.cfg.mom_bars * 5} min)",
            f"To change these: edit {live_params.path()} then /reload.",
        ])

    def calls_text(self, arg: str = ""):
        """/calls [ticker] — a compact, scannable read of the live call/put
        setup for each watched ticker, in STOCK -> BUY CALL/PUT -> strike ->
        expiry -> win-rate order. Real-time; only the watched alert tickers.
        The Service's own cfg/allow-list are threaded through so the read and
        the scanner can never disagree about what is watched or gated."""
        import market_tools
        from backtest import expiry_for
        now = et_now()
        wl = list(self.cfg.watchlist)
        if arg.strip():
            t = arg.strip().upper().lstrip("$")
            if t not in self.cfg.watchlist:
                # not an options ticker — gold/forex/lookup-stock get a read
                # instead of a flat rejection (so /calls gold, /calls aapl work)
                return self.read_text(arg)
            tickers = [t]
        else:
            tickers = wl
        lines = ["📊 LIVE SETUPS · calls & puts (real-time read):"]
        for t in tickers:
            try:
                mn = market_tools.market_now(t, cfg=self.cfg,
                                             allowed=self.ALLOWED_SETUPS)
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

    def read_text(self, arg: str):
        """A read on gold, a forex pair, or a popular stock/ETF (price, day move,
        momentum, trend, and any high-impact news coming). Used by /gold, /fx,
        bare-symbol commands like /aapl, and /calls."""
        import market_tools
        return cards.macro_line(market_tools.any_read(arg))

    def chart_reply(self, arg: str, chat_id: str = ""):
        """/chart <symbol> — text the trade ticket AND a chart image of it with
        the entry/SL/TP drawn on. The read (two downloads) + render (a third +
        matplotlib) run on a BACKGROUND thread so they never block exit
        monitoring; it falls back to text only if the chart can't render."""
        arg = (arg or "").strip()
        if not arg:
            return ("Which symbol? e.g.  /chart xauusd  ·  /chart eurusd  ·  "
                    "/chart btc  ·  /chart nvda")
        if self.dry:  # CLI/dry-run: synchronous text, no photo
            import market_tools
            return cards.macro_line(market_tools.read_any(arg))

        import threading
        stop = threading.Event()

        def beat():
            while not stop.is_set():
                telegram.send_chat_action(chat_id, "upload_photo")
                stop.wait(3)

        def worker():
            # Every step below is guarded on its own so one failure can never
            # swallow the reply: read -> text card -> fvg render -> line render
            # -> photo send -> text send. The user ALWAYS hears back.
            threading.Thread(target=beat, daemon=True).start()
            try:
                # 1) the read (two downloads) — a crash here becomes an error read
                try:
                    import market_tools
                    r = market_tools.read_any(arg)
                except Exception as e:
                    r = {"error": f"couldn't read {arg} right now ({e})"}
                if not isinstance(r, dict):
                    r = {"error": f"couldn't read {arg} right now (empty read)"}

                # 2) the text card — even if it crashes we still have the raw
                # note/error to send, and failing that one honest line
                try:
                    text = cards.macro_line(r)
                except Exception:
                    text = r.get("error") or r.get("note")
                if not text or not isinstance(text, str):
                    text = (f"couldn't build a read for {arg} right now. "
                            f"Try again in a minute, or /signal {arg} for the "
                            "text-only read.")

                # 3) the picture (a bonus, never a blocker): candlestick FVG
                # chart when we can build it; fall back to the plain price-line
                # chart if the candle render can't. A note/error read skips the
                # render and just texts the note.
                img = None
                if not (r.get("error") or r.get("note")):
                    try:
                        import charts
                        img, _ = charts.render_fvg(r)
                    except Exception:
                        img = None
                    if img is None:
                        try:
                            import charts
                            img, _ = charts.render_signal(r)
                        except Exception:
                            img = None

                # 4) deliver. If the photo carries the whole read in its caption
                # we're done; otherwise (long text, failed send, no image) the
                # text goes out on its own.
                sent = False
                if img:
                    try:
                        err = telegram.send_photo(chat_id, img,
                                                  caption=text[:1024])
                        sent = err is None and len(text) <= 1024
                    except Exception:
                        sent = False
                if not sent:
                    telegram.send_to(chat_id, text)
            except Exception as e:
                # last-resort: one honest line with what to try
                try:
                    telegram.send_to(
                        chat_id,
                        f"couldn't build that chart ({e}). Try again in a "
                        f"minute, or /signal {arg} for the text-only read.")
                except Exception:
                    pass
            finally:
                stop.set()

        threading.Thread(target=worker, daemon=True, name="chart-reply").start()
        return None  # the worker sends the photo/text itself

    def ask_reply(self, arg: str, chat_id: str = ""):
        """/ask <question> — deep reasoning mode. Escalates a hard question to
        Fable 5 at high effort and texts back the full answer. Runs on a
        background thread (it can take a minute) with a typing indicator, so it
        never blocks exit monitoring. The bot never says 'I'm not trained'."""
        arg = (arg or "").strip()
        if not arg:
            return ("Ask me anything, even outside trading. e.g.  /ask explain "
                    "the carry trade and what unwinds it")
        if self.dry:
            import assistant
            return assistant.deep_think(arg)

        import threading
        stop = threading.Event()

        def beat():
            while not stop.is_set():
                telegram.send_chat_action(chat_id)
                stop.wait(3)

        def worker():
            threading.Thread(target=beat, daemon=True).start()
            try:
                import assistant
                ans = assistant.deep_think(arg)
            except Exception as e:
                ans = f"that one hit an error on my end: {e}"
            finally:
                stop.set()
            telegram.send_to(chat_id, ans)

        threading.Thread(target=worker, daemon=True, name="ask-reply").start()
        return None  # the worker sends the answer itself

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

    def cmd_closed(self, cmd: str, args: str):
        """/closed YYYY-MM-DD [reason] marks a day the market is shut that no
        rule can predict: a hurricane, a funeral, an exchange outage. /open
        YYYY-MM-DD takes it back. /closed with no date lists what is set.

        This is the answer to the one honest gap in the calendar. The regular
        holidays are derived and never need touching; unscheduled closures
        happen every few years and always at short notice, so they have to be
        settable from a phone rather than by editing a file and redeploying."""
        stored = dict(config.state_get("extra_closures", {}) or {})
        parts = args.split(None, 1)
        raw = parts[0].strip() if parts else ""
        if not raw:
            if not stored:
                return ("No extra closures set. Every regular holiday is "
                        "already built in; this is only for the unscheduled "
                        "kind.\nUsage: /closed 2026-10-29 hurricane")
            lines = ["Extra market closures set:"]
            for d, r in sorted(stored.items()):
                lines.append(f"  {d}: {r}")
            lines.append("Remove one with /open <date>.")
            return "\n".join(lines)
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            return f"'{raw}' is not a date. Use YYYY-MM-DD, like 2026-10-29."
        if cmd == "/open":
            if raw not in stored:
                return f"{day} was not on the extra-closure list."
            stored.pop(raw)
            config.state_set("extra_closures", stored)
            self.load_extra_closures()
            return (f"{day} is back to a normal session."
                    if market_calendar.is_trading_day(day)
                    else f"{day} is off the list, but it still is not a "
                         "session (weekend or a real holiday).")
        if day.weekday() >= 5:
            return f"{day} is a {day:%A}. The market is already shut."
        built_in = market_calendar.holiday_name(day)
        if built_in and raw not in stored:
            return f"{day} is already a known holiday ({built_in})."
        stored[raw] = (parts[1].strip() if len(parts) > 1
                       else "unscheduled market closure")
        config.state_set("extra_closures", stored)
        self.load_extra_closures()
        return (f"Marked {day:%a %b} {day.day} closed: {stored[raw]}.\n"
                "No setups, no sniper tickets and no recap that day. "
                f"Undo with /open {raw}")

    PROBE_PENDING_TEXT = ("Checking the API right now. One token, and nothing "
                          "about a trade waits on it. I will text the verdict "
                          "here the moment it lands.")

    def cmd_brain(self, chat_id: str = ""):
        """/brain: check the paid API right now and say what came back.

        This exists because the bot spent days telling the owner his credits
        were empty when they were not. A billing hold used to sit there until
        some unrelated call happened to succeed, and the status line reported
        the stale verdict as if it were current. One token answers it.

        The check runs OFF this thread. run_command is called from
        handle_commands, which is the same loop thread that walks open
        positions for +25%/give-back/STOP between cycles, and a forced probe
        is up to three attempts at a 15 second timeout plus backoff. That
        arithmetic is one scenario, not a ceiling: requests' timeout bounds
        each socket operation, not the wall clock. So the command answers at
        once and the worker texts the verdict back to the same chat, the same
        shape every free-form chat reply already uses (_dispatch_brain)."""
        import assistant
        if not assistant.enabled():
            return "No ANTHROPIC_API_KEY is set, so there is no brain to check."
        if assistant.billing_hold() is None:
            # pure state read, no network: answer in one message as before
            return "\n".join(["Brain: " + assistant.brain_status_line(),
                              config.api_usage_line()])
        target = str(chat_id or telegram.primary_owner_id() or "")

        told = []

        def _answer(ok):
            if ok is None:
                # NO ANSWER: either the worker never started, or the spending
                # policy sent nothing, or nothing came back. "Checked just
                # now" for any of those is a fabricated result, and this bot
                # does not report a check it did not make.
                text = "\n".join(["No answer came back, so I have nothing "
                                  "fresh to report and nothing was billed.",
                                  "Last known: " + assistant.brain_status_line(),
                                  "Try /brain again in a moment.",
                                  config.api_usage_line()])
            elif ok:
                text = "\n".join(["Checked just now: the API answered, so the "
                                  "brain is back online.",
                                  config.api_usage_line()])
            else:
                text = "\n".join(["Checked just now and the API still refused.",
                                  assistant.brain_status_line(),
                                  config.api_usage_line()])
            if not target:
                print("/brain verdict had nowhere to go: " + text[:80])
                return
            err = telegram.send_to(target, text)
            if err:
                print(f"/brain verdict send failed to {target}: {err}")
                return
            told.append(True)

        status = assistant.probe_billing_dispatch(on_done=_answer)
        if status == assistant.PROBE_STARTED:
            return self.PROBE_PENDING_TEXT
        if status == assistant.PROBE_JOINED:
            # a probe really is in the air and this chat is parked on it, so
            # it gets the same fresh verdict rather than the last one
            return "Already checking. The verdict lands here in a moment."
        # NOT STARTED: no worker exists and none was made. The old code said
        # "already checking" here, one message before the worker's own text
        # said no check had been made. _answer has already run on THIS thread
        # with None, so speak again only if it had nowhere to send.
        return (None if told else
                "I could not start the API check, so nothing was asked.")

    def cmd_calendar(self):
        """/calendar: what the bot thinks the market is doing next."""
        now = et_now()
        lines = [f"📅 {market_calendar.describe(now.date())}"]
        d, found = now.date(), 0
        while found < 4:
            d += timedelta(days=1)
            name = market_calendar.holiday_name(d)
            reason = market_calendar.early_close_reason(d)
            if name:
                lines.append(f"  {d:%a %b} {d.day}: {name}, closed")
                found += 1
            elif reason:
                lines.append(f"  {d:%a %b} {d.day}: {reason}, closes 12 PM CT")
                found += 1
            if (d - now.date()).days > 400:
                break
        return "\n".join(lines)

    def cmd_adduser(self, args: str):
        pending = config.state_get("pending_chats", {})
        target = args.strip() or (next(iter(pending), "") if pending else "")
        if not target:
            return ("Nobody's waiting to be added. Have them message the bot "
                    "first, then I'll text you their ID, or use "
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
            "person, ask anything, send a chart screenshot, or tell me how "
            "a trade went and I'll keep your record. Type /help to see more. "
            "Nothing here is auto-traded, every alert ends 'Your call.'")
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
        lines = [f"{cards.MODE_EMOJI[mode]} Risk mode: {mode.upper()}: {reason}",
                 f"Account: {'$' + format(acct, ',.0f') if acct else 'not set (/setaccount)'}",
                 f"Paper mode: {'ON' if config.paper_mode() else 'off'}",
                 f"Data: {self.feed.backend_for('QCOM')} for stocks, "
                 f"{self.feed.backend_for('^GSPC')} for SPX",
                 # the LIVE settings, not the built-ins: this block doubles as
                 # the brain's LIVE BOT STATE, so a live_params.json override
                 # reaches every chat reply the moment it applies
                 f"Alert watchlist: {', '.join(self.cfg.watchlist)}",
                 f"Entry window: {cards.entry_window_ct(self.cfg)} "
                 f"({self.cfg.entry_start:%H:%M}-{self.cfg.entry_end:%H:%M} ET)"]
        try:
            import strategy_spec
            _sp = strategy_spec.get()
            lines.append(f"Sniper window: {_sp.sniper_window_txt()}, on "
                         f"{', '.join(sorted(_sp.sniper_symbol_names()))}")
        except Exception:
            pass
        lines.append(f"Brain: {self._brain_status()}")
        # rendered from the live lease record, never a typed-in value, so
        # "which copy is answering me" is always checkable from the chat
        lines.append(instance_lock.status_line())
        open_pos = [p for p in self.book.positions if p.state != "closed"]
        if open_pos:
            lines.append("Open positions:")
            for p in open_pos:
                pct = p.last_mark_pct if p.last_mark_pct is not None else 0.0
                lines.append(f"  {cards.contract_str(p)}: {pct:+.1f}% "
                             f"({p.state}, in since {ct_hm(p.time_et)} CT)")
        else:
            lines.append("Open positions: none")
        return "\n".join(lines)

    # ---------- entries (signal logic UNCHANGED) ----------

    # explicit allow-list: ONLY these setups ever alert, so a backtest re-run
    # shifting the chosen bracket can never silently switch on a money-losing
    # put. Validated calls (incl. SPY, mirroring SPX) + the one probationary
    # put; every other put tested as a net loser. The literal lives in
    # live_params.py so Service-less surfaces compute the same effective list.
    ALLOWED_SETUPS = live_params.DEFAULT_ALLOWED_SETUPS

    def gate_stats(self, setup, reject_codes=None, gate_values=None):
        """The eligibility filter: backtested win rate of 70+ (rounded the
        same way every card displays it — 69.77% IS the '70%' the user sees)
        AND positive expectancy under the EXITS WE ACTUALLY TRADE (the
        new-rules backtest when it exists, old-rules otherwise).

        reject_codes and gate_values are W06 OUT PARAMETERS and nothing else.
        They are filled in on the way past each existing branch so the recorder
        can say WHY a candidate was rejected without a second copy of this
        logic drifting away from it. No branch, no threshold and no return
        value moves; passing neither leaves the function exactly as it was."""
        codes = reject_codes if reject_codes is not None else []
        vals = gate_values if gate_values is not None else {}
        if self.backtest_old is None:
            print("No backtest results: refusing to alert without real stats. "
                  "Run backtest.py.")
            codes.append("no_backtest_report")
            return None
        key = f"{setup.ticker}:{setup.direction}"
        now = et_now()
        vals["allow_list"] = key in self.ALLOWED_SETUPS
        if key not in self.ALLOWED_SETUPS:
            print(f"{now:%H:%M:%S} {key}: not on the alert allow-list "
                  f"{sorted(self.ALLOWED_SETUPS)}, skipped.")
            codes.append("not_on_allow_list")
            # the later checks were never evaluated for this candidate, and
            # saying so is the difference between a measured reject and a
            # guess about one (Astra section 5)
            codes.append("win_rate_and_expectancy_not_evaluated")
            return None
        stats = self.backtest_old.get("per_setup", {}).get(key)
        if stats is None:
            print(f"{now:%H:%M:%S} {setup.ticker} {setup.direction}: setup formed "
                  "but no backtest stats for it, skipped.")
            codes.append("no_backtest_stats_for_setup")
            codes.append("win_rate_and_expectancy_not_evaluated")
            return None
        # both the raw and the DISPLAYED rate, because the gate rounds and a
        # raw 69.77 passes a rounded 70 bar. Astra section 7 asks for both to
        # be stored rather than one standing in for the other.
        vals["win_rate_raw"] = stats["win_rate"]
        vals["win_rate_rounded"] = round(stats["win_rate"])
        vals["min_winrate"] = config.MIN_WINRATE
        vals["rounding_rule"] = "python round() on the raw win rate"
        if round(stats["win_rate"]) < config.MIN_WINRATE:
            print(f"{now:%H:%M:%S} {setup.ticker} {setup.direction}: win rate "
                  f"{stats['win_rate']:.0f}% is below {config.MIN_WINRATE:.0f}%, "
                  "skipped, not forcing it.")
            codes.append("win_rate_below_floor")
            codes.append("expectancy_not_evaluated")
            return None
        new_stats = (self.backtest_new or {}).get("per_setup", {}).get(key)
        exp = (new_stats or stats)["expectancy_pct"]
        rules = "our exits" if new_stats else "the old exits"
        vals["expectancy_pct"] = exp
        vals["expectancy_report"] = "new rules" if new_stats else "old rules"
        if exp <= 0:
            print(f"{now:%H:%M:%S} {setup.ticker} {setup.direction}: wins "
                  f"{stats['win_rate']:.0f}% of the time but LOSES money with "
                  f"{rules} in testing, skipped, not forcing it.")
            codes.append("expectancy_not_positive")
            return None
        return stats

    # ---------- W06: what the recorder needs to know about this build ----------

    def recorder_context(self) -> dict:
        """The identity every recorded decision is replayable against.

        Read from the repository and the live config, never typed. policy_hash
        covers the thresholds, the allow list, the entry window and the roster,
        so a row recorded under one policy can never be silently pooled with a
        row recorded under another. Cached per process because none of it moves
        while the process runs."""
        ctx = getattr(self, "_rec_ctx", None)
        if ctx is not None:
            return ctx
        policy = json.dumps({
            "min_winrate": config.MIN_WINRATE,
            "tp_half_pct": config.TP_HALF_PCT,
            "stop_pct": config.STOP_PCT,
            "runner_giveback_pct": config.RUNNER_GIVEBACK_PCT,
            "risk_per_trade_pct": config.RISK_PER_TRADE_PCT,
            "correlated_risk_pct": config.CORRELATED_RISK_PCT,
            "gap_up_skip_pct": config.GAP_UP_SKIP_PCT,
            "allow_list": sorted(self.ALLOWED_SETUPS),
            "entry_window": [str(self.cfg.entry_start), str(self.cfg.entry_end)],
            "watchlist": sorted(self.cfg.watchlist),
            "old_bracket": self.old_bracket,
        }, sort_keys=True, default=str)
        phash = hashlib.sha256(policy.encode("utf-8")).hexdigest()[:16]
        commit, deployment = "", ""
        res = storage_io.read_json(config.REPO_DIR / "release_manifest.json")
        if res.usable and isinstance(res.value, dict):
            commit = (res.value.get("source") or {}).get("commit") or ""
            deployment = (res.value.get("deployed") or {}).get("deployment_id") or ""
        ctx = {
            "policy_hash": phash,
            # this bot has no declared strategy version string, so the version
            # IS the policy fingerprint. Naming it that way beats inventing a
            # number nobody increments.
            "strategy_version": f"momentum-{phash[:8]}",
            "source_commit": commit,
            "deployment_id": os.environ.get("RAILWAY_DEPLOYMENT_ID", "")
                             or deployment,
            "input_feed": self.feed.backend_for("SPY") + " 5m bars",
        }
        self._rec_ctx = ctx
        return ctx

    @staticmethod
    def _bar_end_utc(bars):
        """The END of the newest completed input bar, in UTC.

        The bar END and not the poll clock, because Astra section 5 makes the
        completed input bar the unit: a 15 second recheck of the same unchanged
        bar is one candidate, and only a bar boundary can say that."""
        try:
            start = bars.index[-1].to_pydatetime()
            return (start + timedelta(minutes=5)).astimezone(
                ZoneInfo("UTC")).isoformat()
        except Exception:                                      # noqa: BLE001
            return None

    def record_candidate(self, ticker, direction, bar_end, codes, values,
                         now, selected=False, position_id=None,
                         gate_passed=False, candidate_id=None):
        """One recorded opportunity. Wrapped whole, because a recorder fault
        may never end a trading cycle: this is an observer and it gets no vote
        on whether the bot scans.

        A dry run records NOTHING. It reads the same bars and reaches the same
        decisions as the live copy, so letting it write would put a second copy
        of every candidate into the one record the study counts rows in."""
        # getattr, because test_pipeline builds a Service through __new__ with
        # every I/O path stubbed and no __init__, so it has no .dry at all. An
        # observer must not be the thing that raises in that object.
        if getattr(self, "dry", False):
            return None
        try:
            ctx = self.recorder_context()
            return trade_recorder.record_candidate(
                strategy_id="momentum", symbol=ticker, direction=direction,
                session_date=str(now.date()),
                decision_at_utc=now.astimezone(ZoneInfo("UTC")).isoformat(),
                observed_at_utc=trade_recorder._utc_iso(),
                input_bar_end_utc=bar_end, input_values=values or {},
                gate_passed=bool(gate_passed), reject_codes=codes or [],
                selected=bool(selected), position_id=position_id,
                candidate_id=candidate_id,
                strategy_version=ctx["strategy_version"],
                policy_hash=ctx["policy_hash"],
                source_commit=ctx["source_commit"],
                deployment_id=ctx["deployment_id"],
                input_feed=ctx["input_feed"])
        except Exception as e:                                 # noqa: BLE001
            print(f"{now:%H:%M:%S} recorder: candidate not recorded: {e}")
            return None

    def gap_up_pct(self, now: datetime):
        """SPX open vs yesterday's close, computed once per day (cached).
        Returns the gap in percent, or None if it can't be read."""
        cache = getattr(self, "_gap_cache", None)
        if cache and cache[0] == now.date():
            return cache[1]
        gap = None
        try:
            spx = yf.download("^GSPC", period="2d", interval="1d",
                              progress=False, auto_adjust=False)
            if hasattr(spx.columns, "levels"):
                spx.columns = spx.columns.get_level_values(0)
            if spx is not None and len(spx) >= 2:
                gap = (float(spx["Open"].iloc[-1])
                       / float(spx["Close"].iloc[-2]) - 1) * 100
        except Exception as e:
            print(f"{now:%H:%M:%S} gap check failed (rule skipped): {e}")
        self._gap_cache = (now.date(), gap)
        return gap

    def scan_entries(self, now: datetime):
        # Verified regime rule: big gap-UP opens are historically toxic for
        # this call-heavy playbook (won 56.7% and lost money; skipping them
        # lifted the whole book to 76.7% win rate in walk-forward). Stand
        # aside for the day and say so once.
        if config.GAP_UP_SKIP_PCT > 0:
            gap = self.gap_up_pct(now)
            if gap is not None and gap >= config.GAP_UP_SKIP_PCT:
                if getattr(self, "_gap_skip_told", None) != now.date():
                    self._gap_skip_told = now.date()
                    try:  # on the record so the nightly coach can judge it
                        config.state_set("gap_up_skip_date", f"{now.date()}")
                    except Exception:
                        pass
                    self.notify(
                        f"⏭️ Standing aside today: SPX opened {gap:+.1f}% "
                        f"above yesterday's close. "
                        f"{strategy_spec.get().gap_skip_sentence()}. "
                        "No entry alerts today; open positions "
                        "still get managed to the close.")
                return
        opened = self.book.opened_today(now.date())
        # a weekly (TSLA/QCOM) opened earlier in the week is not dated today, so
        # also skip any ticker with a live position: no stacking a fresh entry
        # on top of an open one day after day
        live = {p.ticker for p in self.book.positions
                if getattr(p, "state", "") != "closed"}
        for ticker, yfs in self.cfg.watchlist.items():
            # W06: one scan evaluation per look, counted separately from the
            # candidate opportunities below. Astra section 5 wants both numbers
            # to stay available, because four rechecks of one bar are four
            # evaluations and ONE opportunity.
            trade_recorder.record_scan_evaluation("momentum", ticker)
            if ticker in self.skipped_today or ticker in opened or ticker in live:
                # recorded once per session per ticker, not once per poll: the
                # candidate key dedups on the bar and this branch has no bar
                self.record_candidate(
                    ticker, None, None,
                    ["already_decided_today", "no_bar_read_on_this_branch"],
                    {"skipped_today": ticker in self.skipped_today,
                     "opened_today": ticker in opened,
                     "position_open": ticker in live}, now)
                continue
            try:
                bars = self.get_bars(yfs, now)
                if bars is None or bars.empty:
                    self.record_candidate(ticker, None, None, ["no_bars"],
                                          {"feed": yfs}, now)
                    continue
                setup = detect_setup(ticker, bars, now, self.cfg)
            except Exception as e:
                print(f"{now:%H:%M:%S} {ticker}: data error: {e}")
                self.record_candidate(ticker, None, None, ["data_error"],
                                      {"feed": yfs, "error": str(e)[:120]}, now)
                continue
            bar_end = self._bar_end_utc(bars)
            if setup is None:
                # a completed bar that produced no setup is still an OBSERVED
                # opportunity, and it is most of the denominator. The direction
                # is null because there is no setup to have one, which is a
                # missing value, not a zero.
                self.record_candidate(
                    ticker, None, bar_end, ["no_setup"],
                    {"close": float(bars["Close"].iloc[-1])}, now)
                continue
            # A setup whose DIRECTION isn't the one we trade for this ticker
            # (e.g. an early SPX:put on a weak open, before the tape turns up to
            # the SPX:call we actually trade) is NOT a decision for the day —
            # momentum routinely flips to the allowed side later in the window.
            # Just wait and re-check next cycle; do NOT burn the ticker.
            if f"{setup.ticker}:{setup.direction}" not in self.ALLOWED_SETUPS:
                self.record_candidate(
                    ticker, setup.direction, bar_end,
                    ["direction_not_on_allow_list",
                     "gate_not_evaluated"],
                    {"mom_pct": setup.mom_pct, "spot": setup.spot,
                     "allow_list": sorted(self.ALLOWED_SETUPS)}, now)
                continue
            codes, gate_values = [], {}
            gate_values.update({"mom_pct": setup.mom_pct, "spot": setup.spot,
                                "strike": setup.strike,
                                "risk_mode": self.mode})
            if self.gate_stats(setup, codes, gate_values) is None:
                # allowed direction, but it failed the win-rate/expectancy bar:
                # THAT is final for the day.
                self.record_candidate(ticker, setup.direction, bar_end, codes,
                                      gate_values, now)
                self.skipped_today.add(ticker)
                continue
            try:
                # bar_end and gate_values are PASSED, not stashed on self.
                # Instance state would survive a raised open_position and the
                # next ticker would record its decision against the previous
                # ticker's bar.
                did_open = self.open_position(setup, now, bar_end=bar_end,
                                              gate_values=gate_values)
            except Exception as e:
                print(f"{now:%H:%M:%S} {ticker}: failed to open position: {e}, "
                      "will retry next cycle")
                continue  # transient failure must not burn the day's alert
            if did_open:
                self.skipped_today.add(ticker)

    def open_position(self, setup, now: datetime, bar_end=None,
                      gate_values=None):
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
        gate_values = dict(gate_values or {})
        if blocked:
            print(f"{now:%H:%M:%S} {setup.ticker} {setup.direction}: earnings "
                  f"{e_date} lands inside this option's life, skipped, "
                  "not gambling on a report.")
            self.record_candidate(
                setup.ticker, setup.direction, bar_end,
                ["earnings_inside_option_life"],
                dict(gate_values, earnings_date=str(e_date),
                     expiry=str(expiry_date)), now)
            return True  # final decision for the day
        # the three clocks around the ONE chain read this path already makes.
        # Captured here rather than inside quotes so no pricing code moves: the
        # recorder needs to tell "when we asked" from "when we were answered",
        # and a Yahoo chain row carries no quote timestamp of its own.
        q_requested = trade_recorder._utc_iso()
        quote = quotes.get_option_quote(setup.ticker, right, setup.strike, expiry_date)
        q_received = trade_recorder._utc_iso()
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
            print(f"{now:%H:%M:%S} {setup.ticker}: no usable option price yet, "
                  "will retry next cycle.")
            self.record_candidate(
                setup.ticker, setup.direction, bar_end, ["no_option_price"],
                dict(gate_values, sigma=sigma, expiry=str(expiry_date)), now)
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
        # ---- W02 identity, minted before anything is written or sent ----
        # candidate_id is the OBSERVATION. It is hashed over the decision's own
        # inputs including the 5 minute bar the read came from, so the same
        # unchanged bar re-scanned every POLL_SECONDS is ONE candidate and not
        # four (Astra section 5). decision_id is the strategy's commitment and
        # is deliberately NOT derived from it: multiple observations of a setup
        # are not permission to fire another trade.
        bar_floor = now.replace(minute=now.minute - now.minute % 5, second=0,
                                microsecond=0)
        candidate_id = event_journal.candidate_id_for(
            date=str(now.date()), ticker=setup.ticker,
            direction=setup.direction, strike=setup.strike,
            bar=bar_floor.isoformat(), spot=round(float(setup.spot), 4),
            mom=round(float(setup.mom_pct), 4))
        decision_id = event_journal.new_decision_id()
        position_id = f"{now:%Y%m%d-%H%M%S}-{setup.ticker}-{right}{setup.strike:g}"
        pos = Position(
            id=position_id,
            candidate_id=candidate_id, decision_id=decision_id,
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
            # pin the old-rules bracket at entry (a copy, so tonight's reload
            # can't mutate it) — the shadow is judged under the rules it opened on
            old_bracket=dict(self.old_bracket),
        )
        news_lines = []
        if setup.ticker != "SPX":
            try:
                news_lines = [f"⚠️ News today: {outlet}: {title}"
                              for outlet, title in news.hot_headlines(setup.ticker)[:2]]
            except Exception:
                pass
        card = cards.entry_card(setup, pos, quote, display, mode, mode_reason,
                                expiry_date, now.date(), news_lines=news_lines)
        # DURABLE INTENT FIRST, then the position, then the card.
        #
        # The old order was book.add then notify, on the reasoning that a
        # tracked position missing its card is recoverable. It is not: nothing
        # anywhere said a card was owed, so a crash in that window left a
        # position that would later fire exit cards for an entry nobody was
        # ever told about. The journal is the missing third place, and it is
        # written before either side effect so any crash leaves a record that
        # says what was owed. The whole modeled position rides along, so replay
        # rebuilds it without re-running a single strategy check.
        try:
            intent = self._commit_intent(
                "entry", candidate_id=candidate_id, decision_id=decision_id,
                position_id=position_id, strategy_id="momentum",
                payload={"position": asdict(pos), "ticker": setup.ticker,
                         "direction": setup.direction,
                         "entry_mid": entry_mid, "entry_source": entry_source},
                recipients=telegram.chat_ids(), text=card)
        except event_journal.JournalUnavailable as e:
            # Astra section 3: if durable intent creation fails, do NOT create
            # an unrecorded new actionable entry. Nothing is tracked and
            # nothing is sent, the ticker is retried next cycle rather than
            # burned, and the health condition says why. This lowers alert
            # availability on purpose.
            self._journal_down(f"entry intent for {setup.ticker}: {e}")
            return False
        if intent is None:      # dry run: print the card, write no shared store
            self.book.add(pos)
            self.notify(card)
            print(f"{now:%H:%M:%S} dry-run entry: {setup.ticker} "
                  f"{setup.strike:g} {setup.direction} ${entry_mid:.2f}")
            return True
        self._journal_ok()
        pos.intent_id = intent.journal_id
        # add() reports whether the bytes reached positions.json, and that
        # return used to be thrown away. A position only this process knows
        # about stops being monitored the moment the process does, so a
        # delivered entry card whose publish was REFUSED left a live modeled
        # trade in no store at all, with the intent resolved on delivery status
        # alone so replay never looked at it again and orphans() never saw it.
        landed = self.book.add(pos)
        if not self.dry:  # dry-run must not poison the shared legacy-alert log
            record_alert(setup, now, display)
        results = self.notify_intent(card, intent)
        # position_missing and retry_link are recorded as durable FACTS beside
        # the position_id, so the next replay pass republishes the book and
        # orphans() can classify this until it does.
        event_journal.mark_linked(intent.journal_id, position_id=pos.id,
                                  candidate_id=candidate_id,
                                  position_missing=not landed,
                                  retry_link=not landed)
        live = event_journal.get(intent.journal_id) or intent
        if landed and all(d.resolved for d in live.deliveries()):
            event_journal.resolve(intent.journal_id)
        if not landed:
            self._position_unpersisted(
                "entry", f"{setup.ticker} {setup.strike:g} {setup.direction}",
                "positions.json publish refused")
        errors = [r["error"] for r in results if r.get("error")]
        print(f"{now:%H:%M:%S} alert sent: {setup.ticker} {setup.strike:g} "
              f"{setup.direction} entry ${entry_mid:.2f} ({entry_source})"
              + (f" errors: {errors}" if errors else ""))
        self._start_observation(pos, setup, quote, est, now, expiry_date,
                                bar_end, gate_values, entry_source,
                                q_requested, q_received, intent)
        self._note_fill_signal(pos, intent, now, expiry_date)
        return True

    def _note_fill_signal(self, pos, intent, now, expiry_date):
        """W07: name this signal in the fill journal's denominator.

        Without this row a session with one report and eleven silences reads as
        one out of one. The row says who was alerted and that nobody has
        reported yet, which is what turns "no reply is unknown, not no trade"
        into a fact on disk instead of an absence.

        Nothing is ever sent from here. Wrapped whole, and never on the
        monitoring path: the fill journal is an observer and a fault in it may
        not cost an alert that has already gone out."""
        try:
            fill_journal.note_signal(
                position_id=pos.id, candidate_id=pos.candidate_id,
                contract_id=trade_recorder.contract_id_for(
                    pos.ticker, pos.right, pos.strike, expiry_date),
                symbol=pos.ticker,
                recipients=[d.recipient_ref for d in intent.deliveries()],
                alerted_at_utc=now.astimezone(ZoneInfo("UTC")).isoformat())
        except Exception as e:                                 # noqa: BLE001
            print(f"{now:%H:%M:%S} fill lane: signal not noted for "
                  f"{pos.id}: {e}")

    def _start_observation(self, pos, setup, quote, est, now, expiry_date,
                           bar_end, gate_values, entry_source,
                           q_requested, q_received, intent):
        """W06: open the record for this trade, and for the roads not taken.

        The whole A15 answer starts here. The chosen contract and its two
        predeclared controls are all observed to ONE common horizon, and the
        registry does not care that the position closes: it keeps sampling
        until that horizon, which is the only way "the exit was at 0.4R" can
        ever be compared against what a later target would have got.

        Wrapped whole. The recorder is an observer and a fault in it may not
        cost an alert that has already gone out."""
        try:
            ctx = self.recorder_context()
            cid = trade_recorder.register_contract(
                underlying=pos.ticker, option_right=pos.right,
                strike=pos.strike, expiry_date=expiry_date)
            horizon = trade_recorder.common_horizon_utc(now.date())
            rec_candidate = self.record_candidate(
                pos.ticker, setup.direction, bar_end, [],
                dict(gate_values, entry_source=entry_source,
                     entry_mid=pos.entry_mid, expiry=str(expiry_date),
                     risk_pct=pos.risk_pct, correlated=pos.correlated),
                now, selected=True, gate_passed=True, position_id=pos.id)
            trade_recorder.observe(
                contract_id=cid, contract_role=trade_recorder.CHOSEN,
                candidate_id=rec_candidate, position_id=pos.id,
                observation_end_utc=horizon, underlying=pos.ticker,
                right=pos.right, strike=pos.strike,
                expiry_date=str(expiry_date))
            # the entry quote itself, recorded from what this path already
            # fetched. No second chain read anywhere in here.
            trade_recorder.record_sample(
                candidate_id=rec_candidate, contract_id=cid,
                contract_role=trade_recorder.CHOSEN, provider="yfinance",
                feed="yahoo option chain",
                provider_at_utc=None, received_at_utc=q_received,
                requested_at_utc=q_requested,
                bid=(quote.bid if quote is not None and quote.bid > 0 else None),
                ask=(quote.ask if quote is not None and quote.ask > 0 else None),
                bid_size=None, ask_size=None, underlying_price=setup.spot,
                underlying_at_utc=now.astimezone(ZoneInfo("UTC")).isoformat(),
                price_basis=("quote_mid" if entry_source == "quote"
                             else "black_scholes_estimate"),
                is_model=(entry_source != "quote"),
                quality_flags=["entry"], observation_end_utc=horizon,
                mark=pos.entry_mid, position_id=pos.id,
                extra={"model_price": est,
                       "last_trade_at_utc": getattr(quote, "last_trade_at_utc",
                                                    None),
                       "intent_candidate_id": pos.candidate_id,
                       "decision_id": pos.decision_id})
            trade_recorder.request_controls(
                candidate_id=rec_candidate, position_id=pos.id,
                underlying=pos.ticker, right=pos.right, strike=pos.strike,
                spot=setup.spot, expiry_date=str(expiry_date),
                observation_end_utc=horizon,
                decision_at_utc=now.astimezone(ZoneInfo("UTC")).isoformat())
            trade_recorder.record_event(
                position_id=pos.id, event_type="entry",
                trigger_at_utc=now.astimezone(ZoneInfo("UTC")).isoformat(),
                trigger_sample_id=None, trigger_basis=entry_source,
                trigger_threshold=None, mark_used=pos.entry_mid,
                mark_source=entry_source, leg_quantity=1,
                deliveries=trade_recorder.deliveries_from_intent(
                    event_journal.get(intent.journal_id) or intent),
                candidate_id=rec_candidate, decision_id=pos.decision_id,
                extra={"policy_hash": ctx["policy_hash"]})
        except Exception as e:                                 # noqa: BLE001
            print(f"{now:%H:%M:%S} recorder: observation not opened for "
                  f"{pos.id}: {e}")

    # ---------- monitoring ----------

    def monitor_positions(self, now: datetime):
        watch = self.book.needs_monitoring(now.date())
        for pos in watch:
            try:
                self.monitor_one(pos, now)
            except Exception as e:
                print(f"{now:%H:%M:%S} {pos.ticker}: monitor error: {e}")
        self.heartbeat += 1
        # W06 record 6: the health row's monitor freshness. A pure in memory
        # assignment, so the observer still costs the exit loop nothing.
        trade_recorder.note_monitor_ok(
            now.astimezone(ZoneInfo("UTC")).isoformat())
        if watch and self.heartbeat % 40 == 0:  # ~every 10 minutes
            states = ", ".join(
                f"{p.ticker} {(p.last_mark_pct if p.last_mark_pct is not None else 0):+.1f}%"
                for p in watch)
            print(f"{now:%H:%M:%S} watching: {states}")

    def _record_monitor_sample(self, pos, now, quote, est, spot, mark, source,
                               usable, requested_at, received_at):
        """One monitoring observation, handed to the recorder for free.

        Everything here was already fetched by the cycle. Nothing in this
        method talks to a provider, waits on a lock or touches a disk, and the
        one queue operation it does perform cannot block. Wrapped whole anyway:
        an observer never gets to end a monitoring cycle.

        Returns the sample id so an exit fired on this cycle can point at the
        exact quote that fired it, which is what lets an exit be re derived
        later instead of re asserted.

        A dry run records nothing, for the same reason it records no candidate:
        it is not this book."""
        if getattr(self, "dry", False):
            return None
        try:
            obs = trade_recorder.chosen_observation(pos.id)
            flags = []
            if obs is None:
                # the registry did not survive, so the sample is filed under
                # the intent's candidate id and SAYS it fell back
                flags.append("candidate_from_position")
            cand = (obs or {}).get("candidate_id") or getattr(
                pos, "candidate_id", None)
            horizon = ((obs or {}).get("observation_end_utc")
                       or trade_recorder.common_horizon_utc(now.date()))
            cid = trade_recorder.contract_id_for(pos.ticker, pos.right,
                                                 pos.strike, pos.expires_on())
            live = quote is not None and quote.bid > 0
            if not usable:
                flags.append("stale_fallback_mark")
            # the sampler thread has no feed of its own and must never grow
            # one, so the underlying it stamps on a control sample is the one
            # THIS loop already paid for, carried across with its own timestamp
            if spot is not None:
                trade_recorder.note_underlying(
                    pos.ticker, spot,
                    now.astimezone(ZoneInfo("UTC")).isoformat())
            if pos.state == "closed":
                # THE A15 LINE. The position is finished and the path is not:
                # this keeps arriving until the common horizon so a later high
                # is on the record rather than censored by the earlier exit.
                flags.append("after_position_close")
            return trade_recorder.record_sample(
                candidate_id=cand, contract_id=cid,
                contract_role=trade_recorder.CHOSEN, provider="yfinance",
                feed="yahoo option chain", provider_at_utc=None,
                received_at_utc=received_at, requested_at_utc=requested_at,
                bid=(quote.bid if live else None),
                ask=(quote.ask if live else None),
                bid_size=None, ask_size=None, underlying_price=spot,
                underlying_at_utc=now.astimezone(ZoneInfo("UTC")).isoformat(),
                # a cycle with no price at all gets a NULL basis and a null
                # is_model, not "last known price": there was no price, and
                # calling the absence a basis is the invented value the whole
                # schema forbids
                price_basis=("quote_mid" if live else
                             "black_scholes_estimate"
                             if source.startswith("estimat")
                             else "last_known_price" if mark is not None
                             else None),
                is_model=(bool(source.startswith("estimat"))
                          if mark is not None else None),
                quality_flags=flags,
                missing_reason=(None if usable else
                                ("stale_mark_carried" if mark is not None
                                 else "no_underlying_price")),
                observation_end_utc=horizon, mark=mark, position_id=pos.id,
                extra={"model_price": est if est > 0 else None,
                       "mark_source": source,
                       "last_trade_at_utc": getattr(quote, "last_trade_at_utc",
                                                    None)})
        except Exception as e:                                 # noqa: BLE001
            print(f"{now:%H:%M:%S} recorder: sample not recorded for "
                  f"{pos.id}: {e}")
            return None

    def monitor_one(self, pos: Position, now: datetime):
        yfs = self.yfs_for(pos.ticker)
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
            # a cycle the bot could not price at all. Recorded rather than
            # returned from in silence: an unrecorded missed cycle is the exact
            # thing that later reads as "we observed everything", and this one
            # costs a queue put and no I/O.
            self._record_monitor_sample(
                pos, now, None, 0.0, None, None, "no underlying price", False,
                trade_recorder._utc_iso(), trade_recorder._utc_iso())
            return
        sigma = self.sigma(pos.ticker)
        # the contract dies when the MARKET shuts, not at a fixed 16:00. On a
        # 13:00 half day the old literal carried three hours of time value
        # that does not exist, and because this estimate becomes the mark
        # whenever the 0DTE chain goes bid-less, that inflated number was
        # what got written into final_pnl_pct and quoted as the win rate.
        expiry_dt = datetime.combine(pos.expires_on(),
                                     poslib.close_t(pos.expires_on()), tzinfo=ET)
        # sigma==0 means the vol download was throttled; a BS price with no vol
        # collapses to pure intrinsic (all time value stripped) and reads as a
        # huge phantom loss vs the entry estimate -> a FALSE stop. Treat it as
        # "no estimate this cycle" so est can't become the mark or trip the stop.
        est = (quotes.estimate_premium(spot, pos.strike, pos.right,
                                       expiry_dt, now, sigma) if sigma > 0 else 0.0)
        # A throttled vol download at ENTRY stored est_entry as 0.0, which
        # would leave the model stop-floor dead for the position's whole life:
        # the first stale/bid-less stretch then has NO stop signal at all and
        # the position can bleed to the bell unwatched. The first cycle vol is
        # back, rebuild the baseline the entry would have stored — the model
        # price at the recorded entry moment — so est_pct compares
        # model-to-model again. A malformed legacy record skips the repair
        # rather than aborting the monitoring cycle.
        if pos.est_entry <= 0 and sigma > 0 and pos.spot_at_signal > 0:
            try:
                entry_dt = datetime.combine(
                    date.fromisoformat(pos.date),
                    time.fromisoformat(pos.time_et), tzinfo=ET)
                baseline = quotes.estimate_premium(
                    pos.spot_at_signal, pos.strike, pos.right,
                    expiry_dt, entry_dt, sigma)
            except Exception:
                baseline = 0.0
            if baseline > 0:
                pos.est_entry = baseline
                print(f"{now:%H:%M:%S} {pos.ticker}: vol was throttled at "
                      f"entry, model baseline backfilled at ${baseline:.2f}, "
                      "estimate stop-floor active again")
        # the estimate-based stop floor compares model-to-model: the BS
        # estimate now vs the BS estimate AT ENTRY. (Estimate vs a real quote
        # mid would read -30% on day one just from the vol-model gap.)
        est_pct = ((est / pos.est_entry - 1) * 100
                   if est > 0 and pos.est_entry > 0 else None)
        q_requested = trade_recorder._utc_iso()
        quote = quotes.get_option_quote(pos.ticker, pos.right, pos.strike,
                                        pos.expires_on())
        q_received = trade_recorder._utc_iso()
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

        # W06: hand the observation this cycle ALREADY made to the recorder.
        # No chain read is added here and none ever may be: this is the loop
        # that walks a live stop, and the recorder is an observer. The call
        # builds a dict and does one put_nowait, and it is recorded BEFORE the
        # early return below, because a cycle the bot could not act on is
        # exactly the kind of gap that otherwise disappears from the record.
        sample_id = self._record_monitor_sample(
            pos, now, quote, est, spot, mark, source, usable, q_requested,
            q_received)

        near_expiry = (pos.expires_on() == now.date()
                       and now.time() >= poslib.warn_t(now.date()))
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
        # JOURNAL, then persist, then send. This inverts the old order, which
        # sent first and saved after on the reasoning that a crash in between
        # was "at worst a duplicate". It was not: step() has already closed the
        # row in memory, so a crash before save() made the next boot re-read
        # the pre exit row, trip the same stop and send the same card AND book
        # the same leg a second time.
        #
        # Safe to invert only because of the two things below it. event_key
        # makes commit_intent idempotent, so a re-derived leg finds its own
        # earlier intent instead of committing a second one; and an intent
        # committed but not yet delivered is replayed on the way into ACTIVE,
        # so the card still arrives. The exit TRIGGER arithmetic in
        # positions.step is not touched by any of this.
        journaled, degraded = [], []
        for ev in events:
            text = builders[ev["type"]](pos, ev)
            key = f"{pos.id}:{ev['type']}"
            intent = None
            try:
                intent = self._commit_intent(
                    "exit", candidate_id=getattr(pos, "candidate_id", ""),
                    decision_id=getattr(pos, "decision_id", ""),
                    position_id=pos.id, strategy_id="momentum", event_key=key,
                    payload={"leg": ev["type"], "pct": ev.get("pct"),
                             "mark": ev.get("mark"), "mark_source": source,
                             "trigger_at_et": f"{now:%Y-%m-%d %H:%M:%S}"},
                    recipients=telegram.chat_ids(), text=text)
                self._journal_ok()
            except event_journal.JournalUnavailable as e:
                # Astra: this must never discard known open positions or stop
                # their monitoring. The exit still evaluates and the card still
                # goes out, and the degradation is recorded as an evidence gap.
                self._journal_down(f"exit intent for {pos.id}: {e}")
            print(f"{now:%H:%M:%S} {pos.ticker}: {ev['type']} at {ev['pct']:+.1f}%")
            if intent is None:
                degraded.append(text)
            elif intent.duplicate and intent.any_attempt():
                # this leg's card already went on the wire once. replay owns
                # whatever is still unresolved about it; sending here would be
                # the second report the whole reorder exists to prevent.
                print(f"{now:%H:%M:%S} {pos.ticker}: {ev['type']} already "
                      "committed and sent, not reported twice")
            else:
                journaled.append((text, intent))
        # A leg with NO durable record keeps the old send-then-save order. That
        # order is right precisely when there is no journal: with nothing to
        # replay from, a crash between the save and the send loses the card for
        # good, and a duplicate beats a silent close.
        for text in degraded:
            self.notify(text)
        self.book.save()               # one save per cycle, exactly as before
        # ...and a leg WITH a durable record sends after it, because the intent
        # is what makes the card recoverable if this send never happens.
        for text, intent in journaled:
            self.notify_intent(text, intent)
            event_journal.mark_linked(intent.journal_id, position_id=pos.id)
            live = event_journal.get(intent.journal_id) or intent
            if all(d.resolved for d in live.deliveries()):
                event_journal.resolve(intent.journal_id)
        # W06 record 3, written LAST: by here the exit has been journaled,
        # the book has been saved and the card has had its send attempt, so
        # the delivery status recorded beside the trigger is the real one
        # rather than a hopeful one. Nothing above waits on this.
        self._record_exit_events(pos, now, events, source, sample_id, journaled)

    def _record_exit_events(self, pos, now, events, source, sample_id,
                            journaled):
        """The exit, the exact quote that fired it, and where the card went.

        selected and delivery_confirmed are different facts and they stay
        different here: the trigger comes from positions.step, the delivery
        comes from the journal, and neither is inferred from the other (A05).
        A leg with no durable intent is recorded with an empty delivery list
        and a stated reason, not with an invented confirmation."""
        if not events or getattr(self, "dry", False):
            return
        try:
            by_leg = {}
            for text, intent in journaled:
                live = event_journal.get(intent.journal_id) or intent
                by_leg[(live.payload or {}).get("leg")] = live
            thresholds = {"sell_half": config.TP_HALF_PCT,
                          "stop": config.STOP_PCT,
                          "runner_trail": config.RUNNER_GIVEBACK_PCT}
            for ev in events:
                intent = by_leg.get(ev["type"])
                trade_recorder.record_event(
                    position_id=pos.id, event_type=ev["type"],
                    trigger_at_utc=now.astimezone(ZoneInfo("UTC")).isoformat(),
                    trigger_sample_id=sample_id,
                    trigger_basis=("quote_mid" if not source.startswith("estimat")
                                   else "black_scholes_estimate"),
                    trigger_threshold=thresholds.get(ev["type"]),
                    mark_used=ev.get("mark"), mark_source=source,
                    leg_quantity=1,
                    deliveries=(trade_recorder.deliveries_from_intent(intent)
                                if intent is not None else []),
                    candidate_id=getattr(pos, "candidate_id", None),
                    decision_id=getattr(pos, "decision_id", None),
                    extra={"pct": ev.get("pct"),
                           "position_state_after": pos.state,
                           "journal_available": intent is not None})
        except Exception as e:                                 # noqa: BLE001
            print(f"{now:%H:%M:%S} recorder: exit not recorded for "
                  f"{pos.id}: {e}")

    # ---------- sniper watch (own thread, US session) ----------
    # The verified pattern used to fire ONLY when someone happened to text
    # the bot at the right minute. This thread watches the verified symbols
    # through the US session (fvg.sniper_window_open: weekdays from 09:50 ET,
    # 8:50 AM CT, to the close), texts EVERYONE the moment the pattern forms
    # on a COMPLETED bar, sends the marked-up chart, tracks the trade to its
    # exit, and records every candidate to the forward ledger so the bot
    # grades itself nightly. Before 8/22 it ran from 07:00 ET and fired
    # stock tickets off pre-market bars the backtest never contained.

    SNIPER_WATCH_SECONDS = 320  # ceiling on one wait; polls align to bar closes

    def sniper_poll_wait_s(self, now: datetime) -> float:
        """Seconds until ~20s after the next 5-minute bar closes. The gate
        acts on COMPLETED bars only, so polling faster repeats one read and
        polling unaligned (the old flat 240s) could fill up to four minutes
        after the bar the backtest filled on."""
        into = (now.minute % 5) * 60 + now.second
        return max(20.0, min(float(self.SNIPER_WATCH_SECONDS), 300 - into + 20))

    def start_sniper_watch(self):
        """Safe to call repeatedly; the thread gates its own hours."""
        if telegram.standby()[0]:
            return  # a standby copy sends no sniper cards and writes no book
        t = getattr(self, "_sniper_thread", None)
        if t and t.is_alive():
            return
        self._sniper_stop = threading.Event()
        self._sniper_thread = threading.Thread(
            target=self._sniper_worker, name="sniper-watch", daemon=True)
        self._sniper_thread.start()

    @staticmethod
    def sniper_watch_mode(now: datetime, has_open: bool) -> str:
        """'entries' inside the session window; 'step' after the close while
        a trade is still open (until 16:15 ET, so the 15:55 bar is graded and
        the settle fires); else 'off'. A ticket fired on the last in-window
        pass used to be left open all night and then graded against the
        next morning's bars."""
        import fvg as fvg_mod
        if fvg_mod.sniper_window_open(now):
            return "entries"
        # the settle window rides the day's real close: on a half day the
        # tape stops at 13:00, so waiting for 16:00 left an open sniper
        # unwatched for three hours and then graded it on forex bars that
        # printed after the equity session was already over.
        if has_open and market_calendar.is_trading_day(now.date()):
            close = market_calendar.session_close(now.date())
            end = (datetime.combine(date(2000, 1, 3), close)
                   + timedelta(minutes=15)).time()
            if close <= now.time() < end:
                return "step"
        return "off"

    def _settle_open_snipers(self, now: datetime):
        """After the close: anything still open settles flat at the last
        price it saw, even when the read that would normally do it failed."""
        for row in sniper_book.open_rows():
            self._step_sniper(row["symbol"], row.get("last_price"), now, None)

    def _sniper_worker(self):
        while not self._sniper_stop.is_set():
            now = et_now()
            try:
                if telegram.standby()[0]:
                    # defense in depth: this thread can outlive the transition
                    # into standby, and it writes sniper_book on a shared
                    # volume. skip the whole pass rather than write.
                    self._sniper_stop.wait(instance_lock.STANDBY_POLL_S)
                    continue
                mode = self.sniper_watch_mode(now, bool(sniper_book.open_rows()))
                if mode != "off":
                    self._scan_snipers_once(now, entries_allowed=(mode == "entries"))
                    if mode == "step":
                        self._settle_open_snipers(now)
            except Exception as e:
                print(f"{et_now():%H:%M:%S} sniper watch error (continuing): {e}")
            # the wait is measured from NOW, after the scan, so a slow read
            # cannot push the next poll past the bar it is meant to catch
            self._sniper_stop.wait(self.sniper_poll_wait_s(et_now()))

    # resolve() speaks human names, not raw Yahoo symbols; map the verified
    # sniper universe onto the names it understands
    SNIPER_READS = {"EURUSD=X": "eurusd", "JPY=X": "usdjpy",
                    "^GSPC": "spx", "TSLA": "tsla", "SPY": "spy"}

    @staticmethod
    def sniper_record_line(rec: dict) -> str:
        """'Live record: 20 wins, 8 stops, 0 flat over 28 trades. Net +0.00R.
        (A 0.4R target needs 71 of 100 to break even.)' The old line read
        '20 of 28 hit target, +0.00R total', which looks like a contradiction
        until you know a 0.4R win pays less than half of what a stop costs."""
        import strategy_spec
        spec = strategy_spec.get()
        be = spec.sniper_breakeven()
        def _n(count, word):
            return f"{count} {word}{'' if count == 1 else 's'}"
        flat = f", {rec['flats']} flat" if rec.get("flats") else ""
        txt = (f"Live record: {_n(rec['n'], 'trade')}, {_n(rec['wins'], 'win')}, "
               f"{_n(rec['losses'], 'stop')}{flat}. Net {rec['total_r']:+.2f}R.")
        if be:
            txt += (f" A {spec.sniper_tp_txt()} target needs {be:.0f} wins "
                    "in 100 just to break even.")
        return txt

    def _step_sniper(self, yfs: str, price, now: datetime, bars=None):
        """Grade an open sniper against the completed bars the read carried
        (then the live price) and text the exit if it just closed. Never
        raises: a tracking fault must not stop the scan."""
        if telegram.standby()[0]:
            # grading MUTATES the shared sniper book: it can close a row the
            # winner is still watching, on the winner's own volume. a mute
            # copy grades nothing.
            return
        try:
            row = sniper_book.step(yfs, price, now, bars=bars)
        except Exception as e:
            print(f"{now:%H:%M:%S} sniper tracking error ({yfs}): {e}")
            return
        if not row:
            return
        dec = int(row.get("decimals", 2))
        name = row.get("display") or yfs
        reason, r_mult = row["exit_reason"], row.get("r")
        if reason == "target":
            head = f"✅ SNIPER TARGET HIT · {name} {row['direction']}"
            body = (f"Out at {row['exit_price']:.{dec}f}. "
                    f"That is the whole trade, all out as planned.")
        elif reason == "stop":
            head = f"🛑 SNIPER STOPPED · {name} {row['direction']}"
            body = (f"Out at {row['exit_price']:.{dec}f}. "
                    f"One full R. It happens, that is the plan working.")
        else:
            head = f"🔔 SNIPER SESSION END · {name} {row['direction']}"
            body = (f"Closing flat at {row['exit_price']:.{dec}f}: neither the "
                    "stop nor the target was reached and this pattern does "
                    "not hold overnight.")
        via = row.get("exit_via")
        when = ct_hm(row.get("exit_time") or "")
        if via == "bar":
            body += f" Graded on the {when} CT five-minute bar."
        elif via == "poll" and reason != "session end":
            body += f" Graded on a live print at {when} CT."
        elif via == "stale":
            body = (f"Closing this one flat at {row['exit_price']:.{dec}f}, the "
                    f"last price I saw on {row.get('date', 'that day')}. It was "
                    "still open when that session ended, and I do not grade "
                    "a trade against a new day's bars.")
        lines = [head, body,
                 f"Entry was {row['entry']:.{dec}f} · stop {row['stop']:.{dec}f}"
                 f" · target {row['target']:.{dec}f}"]
        if isinstance(r_mult, (int, float)):
            lines.append(f"Result: {r_mult:+.2f}R (best {row.get('mfe_r', 0):+.2f}R"
                         f" · worst {row.get('mae_r', 0):+.2f}R)")
        rec = sniper_book.record()
        if rec["n"]:
            lines.append(self.sniper_record_line(rec))
        lines.append("Your call.")
        print(f"{now:%H:%M:%S} sniper {name} {row['direction']} {reason} at "
              f"{row['exit_price']} ({r_mult})")
        text = "\n".join(lines)
        # same treatment as the momentum exit: the durable record of this leg
        # goes down before the card, keyed on the row and the reason, so a
        # restart that re-grades the same closed row cannot report it twice.
        # sniper_book.step already published the closed row before we got here,
        # so the only thing left to make durable is the notification intent.
        try:
            intent = self._commit_intent(
                "sniper_exit", candidate_id=row.get("candidate_id") or "",
                decision_id=row.get("decision_id") or "",
                position_id=row.get("id") or "", strategy_id="sniper",
                event_key=f"{row.get('id')}:{reason}",
                payload={"symbol": yfs, "reason": reason, "r": r_mult,
                         "exit_price": row.get("exit_price"),
                         "exit_via": via},
                recipients=telegram.chat_ids(), text=text)
            self._journal_ok()
        except event_journal.JournalUnavailable as e:
            self._journal_down(f"sniper exit intent for {yfs}: {e}")
            self.notify(text)          # never stop reporting an exit
            return
        if intent is None:             # dry run: print it, record nothing
            self.notify(text)
            return
        if intent.duplicate and intent.any_attempt():
            print(f"{now:%H:%M:%S} sniper {name} {reason} already reported, "
                  "not sent twice")
            return
        self.notify_intent(text, intent)
        live = event_journal.get(intent.journal_id) or intent
        if all(d.resolved for d in live.deliveries()):
            event_journal.resolve(intent.journal_id)

    def _scan_snipers_once(self, now: datetime, entries_allowed: bool = True):
        import fvg as fvg_mod
        import market_tools
        if telegram.standby()[0]:
            # the wire gag alone is not enough, and the guard at the top of
            # _sniper_worker's loop does not cover the callers that reach here
            # another way. this pass burns the day/symbol key in the SHARED
            # state.json, opens a row in the shared sniper book and marks the
            # ledger row selected, so a mute copy that ran it once makes the
            # WINNER skip the entry alert entirely and then text an exit for a
            # trade nobody was ever told about. read nothing, write nothing.
            return
        alerted = config.state_get("sniper_alerted", {})
        day = f"{now:%Y-%m-%d}"
        for yfs in sorted(fvg_mod.SNIPER_SYMBOLS):
            key = f"{day}:{yfs}"
            # A fired sniper still needs watching, so the once-a-day alert
            # guard can no longer skip the read: it used to `continue` here and
            # that is exactly the state in which the trade is live and
            # unmonitored. Read when there is either a signal still to find OR
            # a position to mark; one read serves both.
            live = sniper_book.has_open(yfs)
            if not live and (key in alerted or not entries_allowed):
                continue
            try:
                r = market_tools.read_any(self.SNIPER_READS.get(yfs, yfs))
            except Exception:
                continue
            if not isinstance(r, dict):
                continue
            if live:
                self._step_sniper(yfs, r.get("price"), now, r.get("recent_bars"))
            if (not entries_allowed or key in alerted
                    or r.get("conviction") != "high"):
                continue
            ticket = ((r.get("fvg") or {}).get("confirming") or {}).get("ticket")
            if not ticket:
                continue
            # the forward ledger's id for the observation this ticket came
            # from, allocated at the read. It is carried onto the position and
            # handed back to the ledger below, so the delivered alert and its
            # observation are linked by an id instead of by a guess.
            cid = (r.get("fvg") or {}).get("candidate_id")
            # the fill is NOW, after the read, not the pre-scan clock: on a
            # slow scan those can sit in different bars
            fired_at = et_now()
            if telegram.standby()[0] or not telegram.may_write_shared_state():
                # last line before the FIRST write, re-checked because the read
                # above can take seconds and this thread can cross into standby
                # inside them. a pass stopped here leaves nothing half-written
                # on the shared volume.
                #
                # It asks "may I write the shared volume" rather than "am I on
                # standby" because a DRY RUN is the other answer to that
                # question. ensure_active declares a dry run ACTIVE so it can
                # still answer /status, which left standby False, so a dry copy
                # walked straight through here and burned the live day key,
                # committed a shared durable intent and marked a forward
                # observation selected for a ticket it then (correctly) did not
                # send. That makes the REAL daemon skip the symbol for the day.
                print("standby or dry run mid-pass: sniper commit abandoned "
                      "before any write")
                return
            # the strategy's one commitment for this ticket, minted before any
            # write. It is also the reservation's OWNER, which is what makes a
            # release safe: only the operation that claimed the day key can
            # ever hand it back.
            decision_id = event_journal.new_decision_id()
            if not config.state_reserve(
                    "sniper_alerted", key, decision_id,
                    extra={"hhmm": f"{fired_at:%H:%M}"},
                    keep=lambda k: k.startswith(day)):
                # somebody else holds this day/symbol, or the state file could
                # not be written. either way this pass has no claim and must
                # not send: the old code wrote the key unconditionally and then
                # tried to compensate.
                print(f"{fired_at:%H:%M:%S} sniper {yfs}: the day key is held "
                      "by another operation, no ticket sent")
                continue
            d = (r.get("plan") or {}).get("direction", "")
            dec = int(r.get("decimals", 2))
            try:  # the record is read from its report; never let a bad
                import strategy_spec  # report file hold up the ticket
                claim = strategy_spec.get().sniper_card_txt()
            except Exception:
                claim = ""
            lines = [
                f"🎯 SNIPER · {r.get('instrument', yfs)} {d}",
                (f"Verified pattern: {claim}." if claim
                 else "Verified pattern (record file missing, no number quoted)."),
                f"Enter now: {ticket['entry']:.{dec}f}",
                f"Stop: {ticket['stop']:.{dec}f}",
                f"Take profit: {ticket['target']:.{dec}f} (all out, no greed)",
            ]
            if ticket.get("target_1r"):
                lines.append(
                    f"Stretch map (no track record yet, being graded nightly): "
                    f"1R {ticket['target_1r']:.{dec}f} · "
                    f"2R {ticket.get('target_2r', 0):.{dec}f}"
                    + (f" · structure {ticket['target_structure']:.{dec}f}"
                       if ticket.get("target_structure") else ""))
            lines.append("One trade per symbol per day. Your call.")
            text = "\n".join(lines)
            if telegram.standby()[0]:
                # a report file read sits between the claim and the send, and
                # the send alone can take seconds, so this thread really can
                # cross into standby in that window. NOTHING has gone on the
                # wire yet, so the reservation is still merely claimed and
                # handing it back is honest. state_release_owned checks the
                # owner AND the lifecycle state, so unlike the old compensating
                # delete it cannot remove a newer claimant's key (Astra A06).
                config.state_release_owned("sniper_alerted", key, decision_id)
                print("standby mid-pass: sniper ticket abandoned and this "
                      f"operation's own day key for {key} released")
                return
            # DURABLE INTENT, then the position, then the ledger link, then the
            # card. The old order sent first and linked last inside a try that
            # only printed, so a fault there lost the link for good: the card
            # went out and the row still said selected=False, which quietly
            # dropped a delivered alert from the forward cohort (Astra A05).
            position_id = f"{day}-{fired_at:%H%M%S}-{yfs}-{d}"
            try:
                intent = self._commit_intent(
                    "sniper_entry", candidate_id=cid or "",
                    decision_id=decision_id, position_id=position_id,
                    strategy_id="sniper",
                    payload={"symbol": yfs, "direction": d,
                             "entry": ticket["entry"], "stop": ticket["stop"],
                             "target": ticket["target"],
                             "fired_at": f"{fired_at:%Y-%m-%d %H:%M:%S}"},
                    recipients=telegram.chat_ids(), text=text)
                self._journal_ok()
            except event_journal.JournalUnavailable as e:
                # no unrecorded actionable entry. the claim is released
                # (nothing was sent), so tomorrow's, or this cycle's, retry can
                # take it cleanly once the volume is writable again.
                config.state_release_owned("sniper_alerted", key, decision_id)
                self._journal_down(f"sniper entry intent for {yfs}: {e}")
                continue
            if intent is None:
                # unreachable while the shared-writer gate above stands, and
                # asserted here so a later edit that moves it fails loudly on
                # its own line instead of inside this thread's catch-all
                config.state_release_owned("sniper_alerted", key, decision_id)
                continue
            # Track it. Until this existed the bot texted a ticket and then
            # went silent forever: no exit alert, and no way to ever know
            # whether its own verified pattern actually won.
            pos = sniper_book.open_trade(
                symbol=yfs, display=str(r.get("instrument", yfs)),
                direction=d, entry=ticket["entry"], stop=ticket["stop"],
                target=ticket["target"], day=day,
                time_et=f"{fired_at:%H:%M:%S}", decimals=dec, entry_ts=fired_at,
                candidate_id=cid, decision_id=decision_id,
                intent_id=intent.journal_id, position_id=position_id)
            # BEFORE the send now, not after: the link is what puts a delivered
            # alert in its cohort, and it is recoverable from the intent either
            # way, so there is no reason left to leave it until last.
            # open_trade answers None on a busy ledger lock, an unreadable book
            # or a refused write, and it always did; what was missing is that
            # the code then substituted the DERIVED position_id everywhere as
            # if a row existed. That recorded a link to a position that is not
            # there, so orphans() could not classify it, replay resolved the
            # intent on delivery alone, and a live stop went unwatched with
            # /health reporting zero orphans.
            tracked_id = (pos or {}).get("id")
            if cid:
                try:
                    res = forward_ledger.mark_selected(
                        cid, fired_at_et=fired_at, position_id=tracked_id,
                        decision_id=decision_id, intent_id=intent.journal_id,
                        delivery_confirmed=False)
                    if not res:
                        print(f"{fired_at:%H:%M:%S} sniper selection not "
                              f"recorded for {yfs}: {res}; replay will retry")
                except Exception as e:
                    print(f"{fired_at:%H:%M:%S} sniper selection not recorded "
                          f"for {yfs}: {e}")
            results = self._deliver(intent, None, text=text)
            # ANY send attempt commits the reservation for good. An ambiguous
            # delivered request is not an unsent opportunity, so from here the
            # day key is terminal and nothing releases it.
            config.state_commit_owned("sniper_alerted", key, decision_id)
            event_journal.mark_linked(
                intent.journal_id, position_id=tracked_id,
                candidate_id=cid or None,
                ledger_linked=bool(cid),
                position_missing=pos is None,
                retry_link=pos is None)
            live = event_journal.get(intent.journal_id) or intent
            if live.delivery_confirmed and cid:
                forward_ledger.mark_selected(
                    cid, fired_at_et=fired_at, position_id=tracked_id,
                    decision_id=decision_id, intent_id=intent.journal_id,
                    delivery_confirmed=True)
            if pos is not None and all(dl.resolved for dl in live.deliveries()):
                event_journal.resolve(intent.journal_id)
            if pos is None:
                self._position_unpersisted(
                    "sniper", f"{r.get('instrument', yfs)} {d}",
                    "the sniper ledger refused the row")
            print(f"{fired_at:%H:%M:%S} sniper FIRED {r.get('instrument', yfs)} {d} "
                  f"entry {ticket['entry']} stop {ticket['stop']} "
                  f"target {ticket['target']}")
            try:  # chart to everyone: upload once, fan out by file_id
                import charts
                img, _ = charts.render_fvg(r)
                if img:
                    telegram.send_photo_all(
                        img, caption=f"{r.get('instrument', yfs)} {d} · "
                        "SNIPER setup, levels drawn.")
            except Exception as e:
                print(f"{now:%H:%M:%S} sniper chart skipped: {e}")

    def stop_sniper_watch(self):
        ev = getattr(self, "_sniper_stop", None)
        if ev:
            ev.set()

    # ---------- breaking news (own thread, instant-send) ----------

    def start_news_watch(self):
        """Spin up the breaking-news watcher in its OWN thread so it fires the
        instant a fresh headline lands instead of waiting on the ~15s trading
        loop, and so a slow AI 'read' never delays the alert or the next trade
        cycle. Safe to call repeatedly."""
        if telegram.standby()[0]:
            return  # a standby copy sends no BREAKING texts
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
                if telegram.standby()[0]:
                    # same reason as the sniper thread: it can outlive the
                    # transition, and _scan_news_once writes news_seen.json
                    # on the shared volume before it sends.
                    self._news_stop.wait(instance_lock.STANDBY_POLL_S)
                    continue
                self._scan_news_once()
            except Exception as e:
                print(f"{et_now():%H:%M:%S} news watcher error (continuing): {e}")
            self._news_stop.wait(max(3, config.NEWS_POLL_SECONDS))

    def _load_news_seen(self):
        """(headlines already texted today, the day they belong to).

        A file that will not read comes back as an UNSEEDED day rather than as
        a seeded empty one, which is what makes a reset safe: the caller's
        first-pass branch then seeds every current headline silently instead
        of texting them all again."""
        res = storage_io.read_json(config.NEWS_SEEN_FILE)
        if res.usable and isinstance(res.value, dict):
            seen = res.value.get("seen")
            return (seen if isinstance(seen, list) else []), res.value.get("date")
        return [], None

    def _save_news_seen(self, seen, day) -> bool:
        """Publish the dedup cache through the shared protocol. Returns whether
        the bytes landed.

        This was the last mandatory reconcile store still written with a plain
        Path.write_text, which TRUNCATES first: a SIGKILL, a redeploy or an OOM
        kill inside the 12 second rewrite left partial JSON on disk, and an
        ENOSPC out of the middle of it left zero bytes, with the OSError
        swallowed by a bare `except OSError: pass` so nothing anywhere said so.
        A torn file then failed the promotion check and stranded the only
        instance in BLOCKED. storage_io stages, fsyncs and replaces, so the
        file on disk is either the old content or the new one and never a
        prefix of either, and it reports a refusal instead of hiding it.

        The ownership question is asked HERE, at the one writer of this file,
        for the same reason PositionBook.save asks it at its own publish: a
        copy that does not own the shared volume (standing by, recovering, or a
        dry run, which ensure_active declares ACTIVE so it can still answer
        /status) that marks a headline seen makes the copy that IS working skip
        that BREAKING alert entirely. The call sites check too; this is the one
        that cannot be forgotten."""
        if not telegram.may_write_shared_state():
            return False
        res = storage_io.write_json(config.NEWS_SEEN_FILE,
                                    {"seen": seen, "date": day})
        if not res.ok:
            print(f"news_seen.json not persisted ({res.status} {res.error}); "
                  "the headlines already texted this pass may be texted again "
                  "after a restart. The file on disk is unchanged.")
        return bool(res.ok)

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
        # one story, one text: a second outlet retelling a headline already
        # seen today is marked seen and never sent (8/21 texted one
        # Canada-tariff story three times between 10:54 and 13:44 ET)
        if fresh and not first_seed:
            kept, known = [], list(seen)
            for o, t in fresh:
                if news.same_story(t, known):
                    seen = seen + [t]
                    continue
                kept.append((o, t))
                known.append(t)
            if len(kept) != len(fresh):
                self._save_news_seen(seen[-300:], str(now.date()))
            fresh = kept
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
        if telegram.standby()[0]:
            # the gate at the top of this pass is one moment in time and the
            # feed fetches above take seconds. news_seen.json is on the SHARED
            # volume, so a copy that crossed into standby in between would burn
            # these headlines as seen and then send nothing, and the winner,
            # reading that same file, would never text them at all. a BREAKING
            # alert lost outright is worse than one sent a pass late.
            return
        self._save_news_seen((seen + [t for _, t in to_send])[-300:],
                             str(now.date()))
        for outlet, title in to_send:
            # 1) RAW alert goes out INSTANTLY — nothing slow runs before it
            self.notify(f"🚨 BREAKING ({outlet}): {title}")
            print(f"{now:%H:%M:%S} breaking news alert: {title[:60]}")
            # 2) AI 'read' is a best-effort FOLLOW-UP on its OWN thread, so a
            # slow read on headline #1 never delays the RAW alert for #2
            threading.Thread(target=self._news_take_and_send, args=(title,),
                             daemon=True, name="news-take").start()

    def _news_take_and_send(self, title: str):
        take = self._news_take(title)
        if take:
            self.notify(f"🧠 Quick read: {take}")

    def _news_take(self, title: str):
        try:
            import assistant
            if not assistant.enabled():
                return None
            if assistant.cooldown_left_s() or assistant.billing_hold():
                return None  # a resting brain has no read to offer anyone
            take = assistant.respond(
                {"chat_id": "newsdesk", "kind": "text",
                 "text": ("One sentence only, no invented numbers: what could "
                          "this headline mean for "
                          f"{'/'.join(self.cfg.watchlist)} trades today? "
                          f"Headline: {title}")},
                self.status_text(), tools_enabled=False,
                purpose="scheduled")  # nobody asked for this read; it fires on
                                      # whatever the wires print, so it only
                                      # spends under API_MODE=full
            # only forward a genuine model answer. assistant.respond returns
            # several failure strings that DON'T contain 'error' ("My brain
            # couldn't connect…", "came back empty…") — those must never reach
            # members as a "Quick read".
            if take and not assistant.is_outage_text(take):
                return take
        except Exception:
            pass
        return None

    # ---------- daily recap + weekly ----------

    def maybe_recap(self, now: datetime):
        """Send the daily 3:05 PM CT (16:05 ET) recap from inside the bot,
        so the cloud needs no separate scheduled task.

        Nothing to recap on a day the market never opened, and recap.main
        would grade an empty holiday as a day the bot chose to sit out."""
        if self.dry or not is_session_day(now.date()) or now.time() < WEEKLY_AT:
            return
        today = str(now.date())
        if config.state_get("recap_sent") == today:
            return
        # recorded BEFORE the send: a crash between recap.main's broadcast and
        # the recap_sent write used to re-text the whole recap on every restart
        n = self._job_attempt("recap", today)
        if n > self.MAX_JOB_ATTEMPTS:
            config.state_set("recap_sent", today)
            print(f"{now:%H:%M:%S} recap already attempted {n - 1} times today "
                  "(restart between send and mark?); marking done")
            return
        try:
            import recap
            errs = recap.main(require_date=today)
            if errs == "STALE":
                # yfinance hasn't published today's session yet — nothing was
                # sent, so refund the attempt (data lag must not burn the cap)
                # and retry next pass rather than text a wrong-day recap.
                config.state_set("recap_tries", {today: n - 1})
                print(f"{now:%H:%M:%S} recap data not caught up to {today} yet; will retry")
                return
            if not errs:
                config.state_set("recap_sent", today)
                return
            # delivery errors: retry a BOUNDED number of times, then mark sent so
            # one permanently-unreachable member can't re-broadcast the full
            # recap to everyone else every cycle for the next 7 minutes.
            if n >= self.MAX_JOB_ATTEMPTS:
                config.state_set("recap_sent", today)
                print(f"{now:%H:%M:%S} recap had delivery errors after {n} tries; "
                      f"marking done to avoid duplicates: {errs}")
            else:
                print(f"{now:%H:%M:%S} recap send had errors, will retry once: {errs}")
        except Exception as e:
            print(f"{now:%H:%M:%S} recap failed (attempt {n} recorded, will retry): {e}")

    def maybe_request_digest(self, now: datetime):
        """After the close, send Chudi one rollup of every request that came in
        today (plus anything still open). Self-guards to once per day, same
        16:05 ET window as the recap."""
        if self.dry or now.weekday() >= 5 or now.time() < WEEKLY_AT:
            return
        today = str(now.date())
        if config.state_get("request_digest_sent") == today:
            return
        # recorded BEFORE the send — see MAX_JOB_ATTEMPTS. Owner-only, so the
        # blast radius is small, but the retry-forever loop was unbounded.
        n = self._job_attempt("request_digest", today)
        if n > self.MAX_JOB_ATTEMPTS:
            config.state_set("request_digest_sent", today)
            print(f"{now:%H:%M:%S} request digest already attempted {n - 1} "
                  "times today; marking done")
            return
        try:
            import intake
            msg = intake.digest()
            owner = telegram.primary_owner_id()
            if msg and owner:
                err = telegram.send_to(owner, msg)
                if err and n < self.MAX_JOB_ATTEMPTS:
                    print(f"{now:%H:%M:%S} request digest send error: {err}")
                    return  # don't mark sent — retry next pass
                if err:
                    print(f"{now:%H:%M:%S} request digest send error after "
                          f"{n} tries; marking done: {err}")
            config.state_set("request_digest_sent", today)
        except Exception as e:
            print(f"{now:%H:%M:%S} request digest failed (attempt {n} "
                  f"recorded, will retry): {e}")

    def maybe_learn(self, now: datetime):
        """Nightly self-review at a RANDOM late-evening time (21:00-23:45 ET),
        fired from the daemon outer loop (the only loop alive at night). It
        grades the day's own calls, writes lessons the brain then reads on every
        reply, and texts the OWNER a 'what I learned' digest. Self-guards to
        once per session; never in dry mode. A review missed while the bot was
        down across the whole window (redeploy, crash loop, a Friday outage
        rolling into the weekend) is caught up on a later tick, recap/weekly
        style, but only the single most recent session. Tonight's session and
        a caught-up one alike are graded only when there is evidence the bot
        actually ran that trading day: tracked positions, or that day's
        morning card (only ever sent from the daytime loops). The recap
        deliberately does NOT count as evidence: its own catch-up fires on
        the same night tick right before this one, so a bot that was dead
        all day and restarted at 21:00 would look alive by the recap it just
        sent, and its outage would be graded as a deliberate stay-out."""
        if self.dry:
            return
        key = str(learn_session_due(now))
        if not config.learn_enabled():
            # Owner switched the nightly review off (LEARN_ENABLED=false).
            # Mark the session done so flipping it back on later reviews
            # tonight rather than back-filling every skipped night at once,
            # and stay quiet: this is a deliberate setting, not an outage.
            if config.state_get("learn_sent") != key:
                config.state_set("learn_sent", key)
                print(f"{now:%H:%M:%S} learn: nightly review is OFF "
                      f"(LEARN_ENABLED=false); skipping {key}")
            return
        if config.state_get("learn_sent") == key:
            return
        if not self.book.for_date(key) \
                and config.state_get("morning_sent") != key:
            # no sign the bot was up during that session: grading it would
            # invent a "stayed out, being picky" story about an outage, and
            # that fabricated lesson would steer every future reply.
            config.state_set("learn_sent", key)
            print(f"{now:%H:%M:%S} learn: {key} left no positions and no "
                  "morning card, so the bot was likely down that session; "
                  "skipping the review rather than grading an outage as a choice")
            return
        # recorded BEFORE the review runs: learn.run writes lessons and texts
        # the owner, so a crash between that send and the learn_sent write
        # used to re-run the review (a fresh API call) and re-text the digest
        # on every restart.
        n = self._job_attempt("learn", key)
        if n > self.MAX_JOB_ATTEMPTS:
            config.state_set("learn_sent", key)
            print(f"{now:%H:%M:%S} learn: {key} already attempted {n - 1} "
                  "times (restart between send and mark?); marking done")
            return
        try:
            import learn
            # learn.run returns [] or a list of delivery errors, never "STALE":
            # it grades off positions.json, so there is no data-lag state to
            # wait out (a STALE branch copied from recap sat dead here for
            # months and implied a retry path that could not happen).
            errs = learn.run(require_date=key)
            if not errs:
                config.state_set("learn_sent", key)
                return
            # delivery errors: bounded retries, then mark done so a permanently
            # unreachable owner can't re-run the review every 45s all night.
            if n >= self.MAX_JOB_ATTEMPTS:
                config.state_set("learn_sent", key)
                print(f"{now:%H:%M:%S} learn had delivery errors after {n} tries; "
                      f"marking done: {errs}")
            else:
                print(f"{now:%H:%M:%S} learn send had errors, will retry once: {errs}")
        except Exception as e:
            print(f"{now:%H:%M:%S} learn failed (attempt {n} recorded, will retry): {e}")

    def _grade_job(self, now: datetime):
        """The grading job this tick should work on, or None.

        THE DEFECT THIS EXISTS TO KILL. Astra W03: "Retrying six times is
        ineffective if no caller remains scheduled to perform those retries."
        The old code opened with `if now.time() < WEEKLY_AT: return`, which is a
        wall-clock gate applied on EVERY tick, not just when deciding what is
        newly due. So between midnight and 16:05 no tick could retry anything,
        and forward_grade_session then rolled the key at the next close. A pass
        that failed at 23:50 spent one of its six attempts and the other five
        were never performed by anybody: the day was neither graded nor parked,
        it simply stopped existing as work.

        An OPEN job therefore outlives the clock. Whichever caller ticks next,
        at any hour, on any day, in or out of session, picks it up and works it
        until it is done or its budget is spent. A job only ever ends in a
        counted status.

        A new job is opened only when nothing is open, and only for a session
        whose declared horizon has passed (forward_grade_open_at)."""
        rec = config.state_get("forward_grade_last")
        # Settled is checked FIRST, always. A record from the build before the
        # job shipped is {key, at} with no status, and adopting one blindly
        # would re-open a day state.json already records as graded: every
        # upgraded container would run one extra pass, and an incomplete one
        # would then park a day that was finished.
        if isinstance(rec, dict) and rec.get("key") \
                and not self._grade_key_settled(rec["key"]):
            if rec.get("status") == "open":
                return dict(rec)
            if "status" not in rec:
                # a legacy record for a day that is NOT settled describes an
                # attempt that really was made, so adopt its budget and its
                # spacing rather than restarting either from zero.
                tries = config.state_get("forward_grade_tries", {}) or {}
                n = tries.get(rec["key"], 0) if isinstance(tries, dict) else 0
                nxt = None
                try:
                    nxt = (datetime.fromisoformat(rec["at"])
                           + timedelta(seconds=self.GRADE_RETRY_S)).isoformat()
                except (ValueError, TypeError, KeyError):
                    pass
                return {"key": rec["key"], "attempt": n,
                        "attempt_id": f"forward_grade:{rec['key']}#{n}",
                        "opened_at": rec.get("at"), "at": rec.get("at"),
                        "next_eligible_at": nxt, "status": "open"}
        due = str(forward_grade_session(now))
        if self._grade_key_settled(due):
            return None         # graded, or handed to a human. Either way done
        # Nothing else may refuse. A job record that says done or attention
        # while state.json records NEITHER is a half written transaction, and
        # for a free deterministic measurement the safe side of that is to run
        # again: the pass is idempotent, and completing it writes the missing
        # accounting. Refusing on the record alone would leave the day
        # permanently unaccounted for, which is the failure this package
        # exists to remove.
        return {"key": due, "attempt": 0,
                "attempt_id": f"forward_grade:{due}#0",
                "opened_at": now.isoformat(), "at": None,
                "next_eligible_at": None, "status": "open"}

    @staticmethod
    def _grade_key_settled(key: str) -> bool:
        """Has this session already been accounted for, either way?

        Graded is settled. Parked as needs-attention is settled too: a human
        has it, and re-attempting behind their back is the 45 second spin the
        park exists to stop."""
        if config.state_get("forward_graded") == key:
            return True
        park = config.state_get("forward_grade_attention")
        return isinstance(park, dict) and park.get("key") == key

    def maybe_grade_forward(self, now: datetime):
        """Grade the day's sniper candidates against the day's bars.

        This is DELIBERATELY not inside maybe_learn. Grading is deterministic:
        it walks bars and decides which level was touched first, and it costs
        nothing. It used to live inside learn.run, so switching the paid
        nightly review off silently switched off the evidence collection too,
        and the forward ledger stopped earning the record that is supposed to
        settle the 0.4R question. Capping spend must never cost measurement.
        Nothing in this method may consult an AI switch, and nothing does.

        The job's identity, its attempt number and its NEXT ELIGIBLE RETRY TIME
        are persisted before the work starts (Astra W03: "persist the attempt
        identity and next eligible retry time"), so a restart between attempts
        resumes the same job at the next attempt instead of re-spending the
        budget or losing it.

        The day is claimed on the ledger's own RECONCILIATION, never on the
        mere fact that the call returned. fill_outcomes used to hand back a
        bare int, so an unreadable ledger, a missing dependency, a download
        that raised and a lost write all looked exactly like a finished day,
        and this job wrote forward_graded on the line after the call, before
        it had even looked at the answer.

        It claims the day on JOB completion, not on measurement completion. A
        row the download can never reach again is durably retired and does not
        hold every future job open; its outcome stays missing for research and
        the counts say so. Astra section 4.

        Exhausting the budget is parked as needs-attention instead of being
        written with the byte-identical state a real success writes, and a
        parked day stops re-attempting.

        A row the grader DEFERRED (its declared horizon has not passed, so
        freezing an answer now would freeze a wrong one) is not a failure and
        does not hold the key open. The next pass walks every ungraded row of
        every past date and picks it up, well inside the download window."""
        if self.dry:
            return
        job = self._grade_job(now)
        if job is None:
            return
        key = job["key"]
        nxt = job.get("next_eligible_at")
        if nxt:
            try:
                if now < datetime.fromisoformat(nxt):
                    return      # the retry is owed, but not yet
            except (ValueError, TypeError):
                pass            # unreadable stamp: treat it as due now
        # _job_attempt is the shared counter every scheduled job uses, and it
        # is the source of truth for n so the record and the counter can never
        # disagree about how much budget is left.
        n = self._job_attempt("forward_grade", key)
        job.update({
            "attempt": n,
            "attempt_id": f"forward_grade:{key}#{n}",
            "at": now.isoformat(),
            "next_eligible_at": (now + timedelta(
                seconds=self.GRADE_RETRY_S)).isoformat(),
            "status": "open",
        })
        config.state_set("forward_grade_last", job)
        res = None
        try:
            import forward_ledger
            # this tick's clock, not a second reading of the wall clock. the
            # grader decides per row whether the outcome can be called final
            # yet, and that answer must come from the same moment the job
            # thinks it is running in.
            res = forward_ledger.fill_outcomes(now_et=now)
        except Exception as e:
            print(f"{now:%H:%M:%S} forward grading raised (attempt {n} "
                  f"recorded, will retry): {e}")
        # isinstance, not truthiness: a dict is ALWAYS truthy, so `if res:`
        # would read an all-zero failed pass as a success, and anything that
        # is not the counts contract must never be able to claim the day.
        # job_complete falls back to complete so an older result shape, or a
        # test stub written against it, still reads correctly.
        finished = isinstance(res, dict) and bool(
            res.get("job_complete", res.get("complete")))
        job["counts"] = {k: v for k, v in (res or {}).items()
                         if k != "statuses"}
        if finished:
            config.state_set("forward_graded", key)
            job["status"] = "done"
            job["next_eligible_at"] = None
            config.state_set("forward_grade_last", job)
            # .get on the newer counts: a stub or an older result shape must
            # not turn a finished pass into an exception here.
            print(f"{now:%H:%M:%S} forward grading complete for {key}: graded "
                  f"{res['graded']} of {res['eligible']} eligible "
                  f"(unresolved {res['unresolved']}, deferred "
                  f"{res.get('pending', 0)} until their horizon, retired "
                  f"{res.get('retired', 0)}, permanent "
                  f"{res['permanent_failures']}, measurement complete "
                  f"{res.get('measurement_complete')})")
            return
        if n >= self.GRADE_MAX_ATTEMPTS:
            reason = _grade_attention_reason(res, n)
            job["status"] = "attention"
            job["next_eligible_at"] = None
            config.state_set("forward_grade_last", job)
            config.state_set("forward_grade_attention",
                             {"key": key, "reason": reason,
                              "at": now.isoformat(),
                              "attempt_id": job["attempt_id"],
                              "counts": job["counts"]})
            print(f"{now:%H:%M:%S} forward grading NEEDS ATTENTION for {key}: "
                  f"{reason}")
        else:
            print(f"{now:%H:%M:%S} forward grading incomplete for {key} "
                  f"(attempt {n} of {self.GRADE_MAX_ATTEMPTS}), retry due "
                  f"{job['next_eligible_at']}: {job['counts']}")

    def maybe_holiday_notice(self, now: datetime):
        """Text everyone the evening before the market is shut, so the silence
        the next morning reads as expected rather than broken.

        It fires on the EVENING OF THE LAST SESSION before a closure, not
        literally the night before, and those differ whenever the holiday is a
        Monday: the Friday evening text says "Labor Day is Monday" because
        cards.holiday_card asks market_calendar.day_reference how a person
        would say that date out loud. Sunday night nobody is looking at their
        phone for a trading bot anyway.

        Same shape as the other scheduled jobs: once per closure, dedup key
        recorded BEFORE the send so a crash between the broadcast and the mark
        cannot re-text everyone, and bounded attempts so an unreachable member
        cannot make it retry all night."""
        if self.dry:
            return
        today = now.date()
        closures, half = [], None
        if not is_session_day(today) and today.weekday() < 5:
            # The closure is ALREADY HERE and was never announced: the bot was
            # down through the evening before, or the owner declared it that
            # morning with /closed. Say so today rather than stay silent on the
            # one day the silence is the whole question. day_reference renders
            # this as "today", so the card reads correctly either way.
            name = market_calendar.holiday_name(today)
            if not name:
                return
            closures = [(today, name)]
        elif is_session_day(today) and now.time() >= HOLIDAY_NOTICE_AT:
            closures = market_calendar.upcoming_closures(today)
            if not closures:
                nxt = market_calendar.next_trading_day(today)
                reason = market_calendar.early_close_reason(nxt)
                if reason:
                    half = (nxt, reason)
        else:
            return
        if not closures and not half:
            return
        # The key names the WHOLE announced set and the return day, not just
        # the first date. An extended closure (a two-day storm declared one day
        # at a time) then reads as new news instead of being swallowed as a
        # repeat of the announcement that only mentioned day one.
        key = (("closed:" + ",".join(str(d) for d, _ in closures)
                + f"|back:{market_calendar.next_trading_day(closures[-1][0])}")
               if closures else f"half:{half[0]}")
        if config.state_get("holiday_notice_sent") == key:
            return
        n = self._job_attempt("holiday_notice", key)
        if n > self.MAX_JOB_ATTEMPTS:
            config.state_set("holiday_notice_sent", key)
            print(f"{now:%H:%M:%S} holiday notice already attempted {n - 1} "
                  f"times for {key}; marking done")
            return
        try:
            if closures:
                back_on = market_calendar.next_trading_day(closures[-1][0])
                msg = cards.holiday_card(closures, today, back_on)
            else:
                msg = cards.half_day_card(half[0], half[1], today)
            errs = self.notify(msg)
            if errs and n < self.MAX_JOB_ATTEMPTS:
                print(f"{now:%H:%M:%S} holiday notice send errors, will "
                      f"retry: {errs}")
                return  # don't mark sent, notify() has already queued them
            config.state_set("holiday_notice_sent", key)
            print(f"{now:%H:%M:%S} holiday notice sent for {key}")
        except Exception as e:
            print(f"{now:%H:%M:%S} holiday notice failed (attempt {n} "
                  f"recorded, will retry): {e}")

    def maybe_weekly(self, now: datetime):
        # Friday after the close — with weekend catch-up if the bot was
        # offline at 16:05 (daemon mode picks it up later)
        due = (now.weekday() == 4 and now.time() >= WEEKLY_AT) or now.weekday() >= 5
        if not due:
            return
        key = now.strftime("%G-W%V")
        if config.state_get("weekly_sent") == key:
            return
        n = 0
        if not self.dry:  # recorded BEFORE the send — see MAX_JOB_ATTEMPTS.
            n = self._job_attempt("weekly", key)
            if n > self.MAX_JOB_ATTEMPTS:
                config.state_set("weekly_sent", key)
                print(f"weekly report already attempted {n - 1} times this "
                      "week (restart between send and mark?); marking done")
                return
        errors = self.notify(scoreboard.weekly_report(
            self.book, self.backtest_old, self.backtest_new, now.date()))
        if self.dry:  # dry never marks sent (and never counted an attempt)
            return
        if not errors:  # only mark sent when it really went out
            config.state_set("weekly_sent", key)
        elif n >= self.MAX_JOB_ATTEMPTS:
            # persistent delivery errors used to re-run AND re-send this every
            # daemon tick all weekend. notify() already queued the failed text
            # for bounded per-message retries, so stop re-running the job.
            config.state_set("weekly_sent", key)
            print(f"weekly report had delivery errors after {n} tries; "
                  f"marking done (text queued for retry): {errors}")

    # ---------- main loops ----------

    def run_session(self):
        self._enter_starting()  # gagged until ownership is established
        now = et_now()
        if not self.ensure_active(now):
            # the daemon loop owns the wait and the promotion. a plain one-shot
            # session run has no outer loop, so it just returns and the process
            # exits: whichever copy OWNS the token keeps alerting.
            print("not running this session: "
                  f"{instance_lock.status_line()}")
            return
        self.reset_day(now)
        self.check_downtime_on_start(now)  # were we silently down mid-session?
        mode_src = "live alerts" if not self.dry else "dry-run"
        print(f"Scanner running ({mode_src}). Entry window "
              f"{ct_wall(self.cfg.entry_start):%H:%M}-"
              f"{ct_wall(self.cfg.entry_end):%H:%M} CT, polling every "
              f"{config.POLL_SECONDS}s. Watchlist: {', '.join(self.cfg.watchlist)}. "
              f"Min win rate {config.MIN_WINRATE:.0f}%. Exits: half at "
              f"+{config.TP_HALF_PCT:g}%, give-back {config.RUNNER_GIVEBACK_PCT:g} "
              f"off peak, stop {config.STOP_PCT:g}%. Being picky, no forced trades.")
        if self.backtest_old is None:
            print("WARNING: reports/backtest_results.json missing: the bot "
                  "will not send entry alerts without real backtest stats.")
        if is_session_day(now.date()):
            self.morning_report(now)
        keep_awake(True)
        self.start_news_watch()  # instant breaking-news alerts, own thread
        self.start_sniper_watch()  # verified-pattern watch, own thread
        self.start_recorder()      # W06 observation recorder, own two threads
        try:
            while True:
                now = et_now()
                if not self.ensure_active(now):
                    # cannot normally happen: a lock is not lost while its
                    # holder lives. hand control back to the daemon loop,
                    # which owns standby, rather than sending from here.
                    return
                self.reset_day(now)
                if now.time() >= session_end_for(now.date()) \
                        or not is_session_day(now.date()):
                    self.maybe_recap(now)
                    self.maybe_request_digest(now)
                    self.maybe_weekly(now)
                    self.health_eod(now)  # owner-only 'all clear' for the day
                    # Hand the closed session to the grading job before this
                    # loop exits. Two reasons, both Astra W03. A one-shot
                    # `python scanner.py` has no daemon loop behind it, so
                    # without this it collects evidence all day and grades none
                    # of it. And an OPEN job left over from last night gets its
                    # owed retry here instead of waiting for the daemon.
                    # Deliberately on this branch only: monitoring has stopped
                    # for the day, so a download that takes a minute cannot
                    # delay a stop. The grader is an observer and never gets to
                    # sit in front of an exit.
                    self.maybe_grade_forward(now)
                    print("Session over for today.")
                    return
                t0 = time_mod.monotonic()
                try:  # one bad cycle must never end the trading day
                    # money-critical FIRST: entries + exits must never wait on
                    # a retry backlog (each queued send can block ~10s) or a
                    # slow chat command (a photo/assistant reply can block secs)
                    if self.cfg.entry_start <= now.time() <= self.cfg.entry_end:
                        self.scan_entries(now)
                    if now.time() >= MONITOR_START:
                        self.monitor_positions(now)
                    self.flush_pending_bg()
                    self.handle_commands()
                    try:  # announce "brain is back" when the 5h05m wait ends
                        import assistant
                        assistant.check_cooldown_recovery()
                        # OFF-THREAD: a probe is up to 3 attempts at a 15s
                        # timeout, and this loop walks live stops
                        assistant.probe_billing_async()
                    except Exception:
                        pass
                    self.maybe_recap(now)
                    self.maybe_request_digest(now)
                    self.maybe_weekly(now)
                    self.health_check(now)
                except Exception as e:
                    print(f"{now:%H:%M:%S} cycle error (continuing): {e}")
                # adaptive sleep: data-fetch time counts toward the cadence,
                # so a slow cycle doesn't push the next look further out.
                # Sleep in short slices and peek for commands between them so
                # a text gets picked up in ~3s instead of a full cycle.
                elapsed = time_mod.monotonic() - t0
                wake_at = time_mod.monotonic() + max(2.0, config.POLL_SECONDS - elapsed)
                while True:
                    left = wake_at - time_mod.monotonic()
                    if left <= 0:
                        break
                    time_mod.sleep(min(3.0, left))
                    if wake_at - time_mod.monotonic() > 0.5:
                        try:
                            self.handle_commands()
                        except Exception as e:
                            print(f"{now:%H:%M:%S} command peek error: {e}")
        finally:
            self.stop_news_watch()
            self.stop_recorder()
            keep_awake(False)

    def start_recorder(self):
        """W06: the writer and the batched path sampler, both on their own
        threads.

        Two threads and not zero, because the alternative is a chain read on
        the monitoring loop, and that loop walks live stops. The writer drains
        a bounded queue; the sampler is what pays for the control contracts and
        for the chosen contract AFTER its own trade closes, which is the whole
        A15 answer. A dry run records nothing: it is not this book."""
        if self.dry:
            return
        try:
            trade_recorder.start()
            trade_recorder.start_sampler(quotes.chain_snapshot)
        except Exception as e:                                 # noqa: BLE001
            print(f"recorder: could not start, continuing without it: {e}")

    def stop_recorder(self):
        try:
            trade_recorder.stop_sampler()
            trade_recorder.stop()
        except Exception as e:                                 # noqa: BLE001
            print(f"recorder: could not stop cleanly: {e}")

    def daemon(self):
        # gagged before the first ownership answer. the window between process
        # start and that answer used to be wide open, so a boot could poll
        # getUpdates and send from a copy that turned out to be the loser one
        # tick later.
        self._enter_starting()
        print("Daemon mode: running around the clock. Commands answered "
              "any time; sessions run on trading days 8:31-15:12 CT.")
        while True:
            now = et_now()
            try:
                # FIRST thing in the loop. a copy that does not own the token,
                # or owns it but has not reconciled its saved state, does
                # nothing at all: no poll, no scan, no monitoring, no
                # once-a-day job. it stays alive and retries, so it takes over
                # the moment the other container is torn down, and it starts
                # sending the moment a broken volume reads cleanly again.
                if not self.ensure_active(now):
                    self.standby_wait(now)
                    continue
                # started here, not before the loop, so it also starts on a
                # promotion. self-guards on is_alive, so repeats cost nothing.
                self.start_sniper_watch()  # gates itself to the US session window
                if is_session_day(now.date()):
                    if time(9, 0) <= now.time() < time(9, 30) \
                            and self.premarket_sent_for != now.date():
                        self.reset_day(now)
                        self.morning_report(now, include_gap=False, premarket=True)
                    if time(9, 31) <= now.time() < session_end_for(now.date()):
                        self.run_session()
                        continue
                self.flush_pending()
                try:  # announce "brain is back" when the 5h05m wait ends
                    import assistant
                    assistant.check_cooldown_recovery()
                    # and check whether a billing hold has been topped up. One
                    # token, off-thread, self-limited to one real call per probe
                    # interval, so an empty balance costs nothing to watch.
                    assistant.probe_billing_async()
                except Exception:
                    pass
                self.maybe_recap(now)   # catch-up: a recap missed/STALE during
                                        # the 16:05-16:12 window still goes out
                self.maybe_request_digest(now)  # catch-up the request rollup too
                self.maybe_weekly(now)  # weekend catch-up
                self.health_eod(now)    # close ping even if a restart ended the
                                        # session early (self-guards once/day)
                self.maybe_grade_forward(now)  # deterministic, free, and
                                        # deliberately NOT behind the paid
                                        # nightly switch
                self.maybe_learn(now)   # nightly self-review at a random late
                                        # evening time (self-guards once per
                                        # session; catches up the most recent
                                        # missed session after an outage)
                self.maybe_holiday_notice(now)  # "Thanksgiving is tomorrow,
                                        # no trades will be sent" on the
                                        # evening of the last session before
                                        # a market closure
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
                       "label": "EXAMPLE NUMBERS, no backtest on disk",
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
            tagged = (f"🧪 TEST {i}/5: EXAMPLE ONLY, NOT A REAL ALERT 🧪\n\n{msg}")
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
        # --setup is a REAL getUpdates poll, so it is a production token
        # consumer exactly like the daemon. Astra lists "getUpdates utilities"
        # among the second consumers a file lock cannot see, and a poll
        # ACKNOWLEDGES updates through the offset, so running this beside a
        # live daemon can swallow a command that copy should have answered.
        # It asks for ownership like everything else instead of being trusted
        # because a human typed it.
        res = instance_lock.try_acquire()
        if res not in ("acquired", "held"):
            print("not printing chat ids: another copy owns this bot token "
                  f"({res}). {instance_lock.holder_line()}. Stop it first.")
            return
        telegram.set_ownership_state(instance_lock.ACTIVE, "setup holds the lock")
        try:
            telegram.print_chat_ids()
        finally:
            telegram.set_ownership_state(instance_lock.STARTING, "setup done")
            instance_lock.release()
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
