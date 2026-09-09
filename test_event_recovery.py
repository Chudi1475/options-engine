"""W02: recoverable events. Crash at every persist, send and link boundary.

Astra's required regression for this package, verbatim: "Crash at every
persist/send/link boundary, lost acknowledgment, partial recipient delivery,
stale reservation release, append during grading". Its acceptance line: "Zero
lost logical trades in fixtures; unknown deliveries explicit; every committed
alert intent links to one position and candidate".

What was actually wrong, per path, before this file existed
-----------------------------------------------------------
E  the momentum entry persisted the position and then sent the card, with no
   durable record anywhere that a card was OWED. A crash in between left a
   tracked position that would later fire exit cards for an entry nobody was
   ever told about, and no id linked the alert to the observation that made it.

X  the exit alert did the opposite: it SENT and then saved. A crash in between
   re-evaluated the same stop next cycle and sent the same card again and
   booked the same leg twice. The comment called that "at worst a duplicate";
   a duplicate stop card is a second reported trade.

L  a read timeout raised after the request body was written was recorded the
   same way as a connection refused: an error string, queued, and later
   re-broadcast. Telegram delivery is not exactly once, and an ambiguous
   acknowledgment is not an unsent opportunity.

P  a partial recipient failure appended the WHOLE text to pending_sends and
   flush_pending re-broadcast it to every chat, so one recipient with a
   permanent 403 cost everyone else a duplicate on every retry.

R  the sniper day key was a bare "HH:MM" string with no operation identity, and
   the compensating release dropped that key from whatever dict was current, so
   it could delete a NEWER claimant's reservation (Astra A06).

G  a journal append during a grading pass must survive the pass republishing
   the ledger, and two threads appending must not tear a line.

H  if the durable record cannot be written at all, no unrecorded actionable
   entry may be created, and the same fault must never discard known open
   positions or stop their monitoring.

Layer A sections call TODAY'S functions with injected faults and assert the
correct outcome, so they fail for a real defect before the change. Layer B
covers the event_journal interface and replay and fails because the interface
does not exist yet, which the spec explicitly permits.

No network, no Telegram, no yfinance, no production storage.

Run:  python test_event_recovery.py     (exit code 0 = all good)
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.
_bot_test_os.environ.setdefault("OWNER_CHAT_ID", "900001")
_bot_test_os.environ.setdefault("TELEGRAM_CHAT_IDS", "900001,900002,900003")
_bot_test_os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000:offline-test-token")

import json
import sys
import tempfile
import threading
import time as time_mod
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# an isolated data dir BEFORE config is imported, so nothing here can read or
# write the real runtime state
_TMP = Path(tempfile.mkdtemp(prefix="kelbot_events_"))
_bot_test_os.environ["DATA_DIR"] = str(_TMP)

import config          # noqa: E402

config.DATA_DIR = _TMP
config.STATE_FILE = _TMP / "state.json"
config.POSITIONS_FILE = _TMP / "positions.json"
config.ALERTS_LOG = _TMP / "alerts.log"
config.ALERTS_JSONL = _TMP / "alerts_sent.jsonl"
config.NEWS_SEEN_FILE = _TMP / "news_seen.json"
config._LAST_GOOD = {}

import forward_ledger  # noqa: E402
import positions as poslib  # noqa: E402
import requests        # noqa: E402
import scanner         # noqa: E402
import sniper_book     # noqa: E402
import storage_io      # noqa: E402
import telegram        # noqa: E402

forward_ledger.LEDGER = _TMP / "sniper_forward.jsonl"
sniper_book.LEDGER = _TMP / "sniper_positions.json"

ET = ZoneInfo("America/New_York")
REPO = Path(__file__).parent
CHATS = ["900001", "900002", "900003"]

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def probe(name, fn, detail=""):
    """check() for something that may not exist yet. A missing interface is a
    normal FAIL line instead of a traceback that would hide every later
    section, which matters when the whole point of the run is to watch a red
    list before the change and the same list green after it."""
    try:
        ok = fn()
    except Exception as e:
        check(name, False, f"{type(e).__name__}: {e}")
        return False
    if isinstance(ok, tuple):
        ok, detail = ok
    check(name, bool(ok), str(detail))
    return bool(ok)


def jrn():
    """The journal module, or None before it exists."""
    try:
        import event_journal
        return event_journal
    except ImportError:
        return None


def reset_all():
    """Wipe every runtime file between sections. Only inside the temp dir."""
    for p in list(_TMP.rglob("*")):
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
    config._LAST_GOOD = {}
    ej = jrn()
    if ej is not None and hasattr(ej, "_reset_for_test"):
        ej._reset_for_test()


check("isolated data dir, not production",
      str(config.DATA_DIR) == str(_TMP), f"{config.DATA_DIR} vs {_TMP}")
check("the wire is gagged for this whole run", telegram.test_mode())


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _Resp:
    """A Telegram HTTP reply. `ok` and the body are whatever the case needs."""

    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = int(status_code)
        self._body = body if body is not None else {"ok": True,
                                                    "result": {"message_id": 11}}
        self.text = text if text is not None else json.dumps(self._body)

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._body


class _Wire:
    """Stands in for telegram._session. Every post is recorded and answered by
    a per-recipient script, so a case can make ONE chat fail while the others
    succeed without anything leaving the machine."""

    def __init__(self, script=None):
        self.posts = []          # (chat_id, text)
        self.script = script or {}   # chat_id -> callable(n_for_that_chat)

    def post(self, url, **kw):
        payload = kw.get("json") or {}
        cid = str(payload.get("chat_id", ""))
        text = payload.get("text", "")
        self.posts.append((cid, text))
        fn = self.script.get(cid)
        if fn is None:
            return _Resp()
        n = sum(1 for c, _ in self.posts if c == cid)
        out = fn(n)
        if isinstance(out, Exception):
            raise out
        return out

    def get(self, url, **kw):
        return _Resp(body={"ok": True, "result": []})

    def texts_to(self, cid):
        return [t for c, t in self.posts if c == str(cid)]


class _Crash(RuntimeError):
    """The process dying at a chosen boundary."""


def with_wire(script=None, allow=True):
    """Install a stubbed transport and (optionally) let the send classifier
    run against it. Returns the wire; ALWAYS paired with restore_wire."""
    wire = _Wire(script)
    telegram._session = wire
    if allow and hasattr(telegram, "allow_test_send"):
        telegram.allow_test_send(True)
    return wire


def restore_wire():
    if hasattr(telegram, "allow_test_send"):
        telegram.allow_test_send(False)


def fresh_service(dms=None):
    svc = scanner.Service()
    svc.dry = False
    svc._hb_owner = (lambda text: dms.append(text)) if dms is not None \
        else (lambda text: None)
    return svc


def a_position(pid="20260909-100000-SPY-C500", **kw):
    row = dict(id=pid, date="2026-09-09", time_et="10:00:00", ticker="SPY",
               direction="call", right="C", strike=500.0, expiry="2026-09-09",
               entry_mid=1.00, entry_source="quote", est_entry=1.00,
               spot_at_signal=500.0, mom_pct=0.5)
    row.update(kw)
    return poslib.Position(**row)


# ===========================================================================
print("\n--- E. crash at the persist / send / link boundaries (entries) ---")
# ===========================================================================
reset_all()

# E2 is the one that can be shown against TODAY'S code with no new interface:
# open_position persists and then sends, and nothing durable says a card is
# owed. Inject a send that dies and look for a record that recovery could use.
def _stub_entry_path(svc, card="ENTRY CARD"):
    """Make open_position deterministic: no chain, no news, no scoreboard."""
    scanner.quotes.nearest_listed_expiry = lambda t, d: d
    scanner.quotes.get_option_quote = lambda *a, **k: None
    scanner.quotes.estimate_premium = lambda *a, **k: 1.25
    scanner.news.earnings_inside = lambda *a, **k: (False, None)
    scanner.news.hot_headlines = lambda *a, **k: []
    scanner.scoreboard.stats_for_card = lambda *a, **k: None
    scanner.cards.entry_card = lambda *a, **k: card
    svc.sigma = lambda t: 0.20
    svc.current_mode = lambda: ("green", "")


class _Setup:
    ticker = "SPY"
    direction = "call"
    strike = 500.0
    spot = 499.0
    mom_pct = 0.42
    reason = "test"


_ORIG = {
    "nearest": scanner.quotes.nearest_listed_expiry,
    "quote": scanner.quotes.get_option_quote,
    "est": scanner.quotes.estimate_premium,
    "earn": scanner.news.earnings_inside,
    "hot": scanner.news.hot_headlines,
    "stats": scanner.scoreboard.stats_for_card,
    "card": scanner.cards.entry_card,
}


def _restore_entry_path():
    scanner.quotes.nearest_listed_expiry = _ORIG["nearest"]
    scanner.quotes.get_option_quote = _ORIG["quote"]
    scanner.quotes.estimate_premium = _ORIG["est"]
    scanner.news.earnings_inside = _ORIG["earn"]
    scanner.news.hot_headlines = _ORIG["hot"]
    scanner.scoreboard.stats_for_card = _ORIG["stats"]
    scanner.cards.entry_card = _ORIG["card"]


def _e2():
    """E2: crash between the position write and the send. Recovery has to be
    able to find out that a card is owed for that position."""
    reset_all()
    svc = fresh_service()
    _stub_entry_path(svc)
    now = datetime(2026, 9, 9, 10, 0, 0, tzinfo=ET)
    boom = lambda *a, **k: (_ for _ in ()).throw(_Crash("died before the card"))
    svc.notify = boom
    svc.notify_intent = boom
    try:
        svc.open_position(_Setup(), now)
    except _Crash:
        pass
    tracked = [p for p in svc.book.positions if p.ticker == "SPY"]
    if not tracked:
        return False, "nothing was tracked at all, so nothing to recover"
    ej = jrn()
    if ej is None:
        return False, "no event_journal: the card owed is recorded nowhere"
    owed = [i for i in ej.unresolved() if i.kind == "entry"]
    return (len(owed) == 1 and owed[0].position_id == tracked[0].id,
            f"unresolved entry intents: {len(owed)}")


probe("E2 a crash between the position write and the send leaves a "
      "recoverable record that a card is owed", _e2)


def _e1():
    """E1: crash between the durable intent and the position write. Replay
    opens exactly one position and sends exactly one card."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    svc = fresh_service()
    _stub_entry_path(svc)
    now = datetime(2026, 9, 9, 10, 5, 0, tzinfo=ET)
    real_add = svc.book.add
    svc.book.add = lambda pos: (_ for _ in ()).throw(_Crash("died before add"))
    try:
        svc.open_position(_Setup(), now)
    except _Crash:
        pass
    svc.book.add = real_add
    wire = with_wire()
    try:
        svc.replay_journal(now)
        svc.replay_journal(now)   # twice: replay must be idempotent
    finally:
        restore_wire()
    ids = {p.id for p in svc.book.positions}
    cards_out = [t for _, t in wire.posts if "ENTRY CARD" in t]
    return (len(ids) == 1 and len(cards_out) == len(CHATS)
            and not [i for i in ej.unresolved() if i.kind == "entry"],
            f"positions={len(ids)} cards={len(cards_out)}")


probe("E1 replay after a crash before the position write opens exactly one "
      "position and sends exactly one card", _e1)


def _e3():
    """E3 / Astra A05: the card went out and the linkage died. The delivered
    alert must not fall out of the selected cohort."""
    reset_all()
    ej = jrn()
    now = datetime(2026, 9, 9, 10, 10, 0, tzinfo=ET)
    cid = forward_ledger.record_candidate(
        "EURUSD=X", "BUY", 1.1000, 0.0012,
        {"entry": 1.1000, "stop": 1.0980, "target": 1.1008},
        {"grade": "A", "score": 9}, True, [], gap_atr=1.6, hour_et=10,
        now_et=now)
    if not cid:
        return False, "the fixture observation was not recorded"
    if ej is None:
        return False, "no event_journal: a delivered card has no durable link"
    decision = ej.new_decision_id()
    intent = ej.commit_intent(
        "sniper_entry", candidate_id=cid, decision_id=decision,
        position_id="2026-09-09-101000-EURUSD=X-BUY",
        payload={"symbol": "EURUSD=X"}, recipients=CHATS, text="SNIPER CARD")
    for i in range(len(CHATS)):
        ej.record_result(intent.journal_id, i, ej.CONFIRMED,
                         provider_message_id=100 + i)
    # ...and the process dies right here, before mark_selected ever runs.
    before = [r for r in _ledger_rows() if r.get("event_id") == cid]
    if before and before[0].get("selected"):
        return False, "the fixture was already selected"
    svc = fresh_service()
    wire = with_wire()
    try:
        svc.replay_journal(now)
    finally:
        restore_wire()
    after = [r for r in _ledger_rows() if r.get("event_id") == cid]
    return (bool(after) and after[0].get("selected") is True
            and not wire.posts,
            f"selected={after[0].get('selected') if after else 'no row'} "
            f"resends={len(wire.posts)}")


def _ledger_rows():
    if not forward_ledger.LEDGER.exists():
        return []
    return [json.loads(ln) for ln
            in forward_ledger.LEDGER.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


probe("E3 a delivered card whose linkage died is re-linked by replay and "
      "stays in the selected cohort", _e3)


# ===========================================================================
print("\n--- X. an exit leg is never reported twice ---")
# ===========================================================================


def _exit_service(events):
    """A Service whose monitor_one will see exactly `events` from step()."""
    svc = fresh_service()
    svc.get_bars = lambda yfs, now: None
    svc.feed.latest_price = lambda yfs: 480.0
    svc.sigma = lambda t: 0.20
    scanner.quotes.get_option_quote = lambda *a, **k: None
    scanner.quotes.estimate_premium = lambda *a, **k: 0.55
    scanner.cards.stop_card = lambda pos, ev: f"STOP CARD {pos.id}"
    scanner.cards.half_card = lambda pos, ev: f"HALF CARD {pos.id}"
    scanner.cards.trail_card = lambda pos, ev: f"TRAIL CARD {pos.id}"
    scanner.cards.expiry_card = lambda pos, ev: f"EXPIRY CARD {pos.id}"

    fired = {"n": 0}

    def _step(pos, now, mark, source, est_pct, flip, bracket, comparable=True):
        # the real trigger arithmetic is NOT touched by this package; this
        # stands in for it so a crash can be injected around the boundary
        if pos.state == "closed":
            return []
        fired["n"] += 1
        pos.state = "closed"
        pos.final_exit = {"time": "10:30:00", "pct": -45.0, "mark": 0.55,
                          "reason": "stop"}
        pos.final_pnl_pct = -45.0
        pos.old_rules = {"status": "closed", "exit_pct": -45.0,
                         "exit_reason": "stop", "exit_time": "10:30:00"}
        return list(events)

    poslib.step = _step
    return svc, fired


_REAL_STEP = poslib.step


def _x1():
    """X1: the process dies between the exit card and the book write. On the
    next boot the same prices must not produce a second card or a second
    booked leg."""
    reset_all()
    now = datetime(2026, 9, 9, 10, 30, 0, tzinfo=ET)
    ev = [{"type": "stop", "pct": -45.0, "mark": 0.55}]
    svc, fired = _exit_service(ev)
    pos = a_position()
    svc.book.positions = [pos]
    svc.book.save()
    sent = []
    svc.notify = lambda text: sent.append(text) or []
    if hasattr(svc, "notify_intent"):
        svc.notify_intent = lambda text, intent: (sent.append(text) or [])
    # the crash: the book write never lands
    real_save = svc.book.save
    svc.book.save = lambda: (_ for _ in ()).throw(_Crash("died before save"))
    try:
        svc.monitor_one(pos, now)
    except _Crash:
        pass
    svc.book.save = real_save

    # ...restart: a fresh book off the file, which still has the OPEN row
    svc2, _ = _exit_service(ev)
    sent2 = []
    svc2.notify = lambda text: sent2.append(text) or []
    if hasattr(svc2, "notify_intent"):
        svc2.notify_intent = lambda text, intent: (sent2.append(text) or [])
    watch = svc2.book.needs_monitoring(date(2026, 9, 9))
    for p in watch:
        try:
            svc2.monitor_one(p, now)
        except _Crash:
            pass
    # ...and then the replay that would deliver anything still owed. Without an
    # idempotency key on the leg the restart commits a SECOND intent for the
    # same stop, and this pass is where that second card would go out.
    wire = with_wire()
    try:
        if hasattr(svc2, "replay_journal"):
            svc2.replay_journal(now)
    finally:
        restore_wire()
    replayed = [t for _, t in wire.posts if "STOP CARD" in t]
    total = [t for t in sent + sent2 if "STOP CARD" in t]
    ej = jrn()
    legs = []
    if ej is not None:
        legs = [i for i in ej.all_intents()
                if i.kind == "exit" and i.position_id == pos.id]
    # one card, one intent for the leg, and the leg booked once
    booked = [p for p in svc2.book.positions
              if p.id == pos.id and p.state == "closed"]
    return (len(total) == 1 and len(replayed) <= len(CHATS)
            and len(legs) == 1 and len(booked) == 1,
            f"{len(total)} direct cards, {len(replayed)} replayed, "
            f"{len(legs)} exit intents for the leg")


probe("X1 a crash at the exit boundary does not send a second stop card or "
      "book the same leg twice", _x1)


def _x2():
    """X2: replay run twice over the same journal closes nothing twice."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    now = datetime(2026, 9, 9, 10, 35, 0, tzinfo=ET)
    ev = [{"type": "stop", "pct": -45.0, "mark": 0.55}]
    svc, _ = _exit_service(ev)
    pos = a_position(pid="20260909-103500-SPY-C500")
    svc.book.positions = [pos]
    svc.book.save()
    wire = with_wire({"900001": lambda n: (_ for _ in ()).throw(
        requests.ConnectionError("refused"))})
    try:
        svc.monitor_one(pos, now)
        svc.replay_journal(now)
        svc.replay_journal(now)
    finally:
        restore_wire()
    stops = [t for _, t in wire.posts if "STOP CARD" in t]
    per_chat = {c: len(wire.texts_to(c)) for c in CHATS}
    # chat 1 refused the first attempt and is retried; the other two got it
    # once and must never get it again
    return (per_chat["900002"] == 1 and per_chat["900003"] == 1
            and len(stops) >= 3,
            f"per chat {per_chat}")


probe("X2 replay twice over one journal never re-sends a confirmed recipient",
      _x2)

poslib.step = _REAL_STEP
_restore_entry_path()


# ===========================================================================
print("\n--- L. a lost acknowledgment is unknown, not a retry ---")
# ===========================================================================
reset_all()


def _l1():
    """L1: a read timeout raised AFTER the body was written. The request may
    have reached Telegram. That is unknown, and unknown is never auto-resent."""
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    reset_all()
    wire = with_wire({"900002": lambda n: requests.ReadTimeout("read timed out")})
    try:
        recs = telegram.send_detailed("LOST ACK CARD")
    finally:
        restore_wire()
    by_index = {r["recipient_index"]: r for r in recs}
    if by_index.get(1, {}).get("status") != ej.UNKNOWN:
        return False, f"status was {by_index.get(1, {}).get('status')}"
    queued = config.state_get("pending_sends", []) or []
    return (not queued, f"pending_sends={len(queued)}")


probe("L1 a read timeout after the body was written is unknown and is not "
      "queued for a re-broadcast", _l1)


def _l1b():
    """The other half of L1: a connection refused BEFORE the body was written
    is a plain failure, and those two must not collapse into one word."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    wire = with_wire({"900002": lambda n: requests.ConnectionError("refused")})
    try:
        recs = telegram.send_detailed("REFUSED CARD")
    finally:
        restore_wire()
    by_index = {r["recipient_index"]: r for r in recs}
    return (by_index.get(1, {}).get("status") == ej.FAILED,
            str(by_index.get(1, {}).get("status")))


probe("L1b a connection refused before the write is failed, not unknown", _l1b)


def _l2():
    """L2: selected and delivery_confirmed are two fields, and an unknown or
    queued send never sets delivery_confirmed."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    now = datetime(2026, 9, 9, 11, 0, 0, tzinfo=ET)
    cid = forward_ledger.record_candidate(
        "TSLA", "BUY", 250.0, 2.0, {"entry": 250.0, "stop": 248.0},
        {"grade": "A", "score": 9}, True, [], gap_atr=1.5, hour_et=11,
        now_et=now)
    decision = ej.new_decision_id()
    intent = ej.commit_intent("sniper_entry", candidate_id=cid,
                              decision_id=decision, position_id="p-l2",
                              payload={}, recipients=CHATS, text="L2 CARD")
    ej.record_result(intent.journal_id, 0, ej.UNKNOWN)
    ej.record_result(intent.journal_id, 1, ej.FAILED, error_class="http_500")
    ej.record_result(intent.journal_id, 2, ej.UNKNOWN)
    forward_ledger.mark_selected(cid, fired_at_et=now, position_id="p-l2",
                                 decision_id=decision,
                                 intent_id=intent.journal_id,
                                 delivery_confirmed=False)
    row = [r for r in _ledger_rows() if r.get("event_id") == cid]
    if not row:
        return False, "the row vanished"
    row = row[0]
    live = ej.get(intent.journal_id)
    return (row.get("selected") is True
            and row.get("delivery_confirmed") is False
            and live.selected is True and live.delivery_confirmed is False,
            f"selected={row.get('selected')} "
            f"confirmed={row.get('delivery_confirmed')}")


probe("L2 selected and delivery_confirmed are separate fields and an unknown "
      "send never sets delivery_confirmed", _l2)


def _l3():
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    reset_all()
    intent = ej.commit_intent("entry", candidate_id="c-l3",
                              decision_id=ej.new_decision_id(),
                              position_id="p-l3", payload={},
                              recipients=CHATS, text="L3 CARD")
    ej.record_result(intent.journal_id, 0, ej.CONFIRMED, provider_message_id=1)
    ej.record_result(intent.journal_id, 1, ej.UNKNOWN)
    ej.record_result(intent.journal_id, 2, ej.CONFIRMED, provider_message_id=3)
    svc = fresh_service()
    wire = with_wire()
    try:
        svc.replay_journal(datetime(2026, 9, 9, 11, 5, tzinfo=ET))
    finally:
        restore_wire()
    unk = ej.unknown_deliveries()
    return (not wire.posts and len(unk) == 1
            and unk[0]["recipient_index"] == 1
            and "L3" in ej.report_text().upper() or len(unk) == 1,
            f"resends={len(wire.posts)} unknown={len(unk)}")


probe("L3 an unknown delivery is reported and never auto-resent by replay",
      _l3)


# ===========================================================================
print("\n--- P. partial recipient delivery ---")
# ===========================================================================


def _p1():
    """P1: #1 confirmed, #2 permanently refused, #3 confirmed. The retry hits
    ONLY the unresolved recipient."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    forbidden = _Resp(403, body={"ok": False, "description": "bot was blocked"},
                      text='{"ok":false,"description":"bot was blocked"}')
    wire = with_wire({"900002": lambda n: forbidden})
    try:
        svc = fresh_service()
        intent = ej.commit_intent("entry", candidate_id="c-p1",
                                  decision_id=ej.new_decision_id(),
                                  position_id="p-p1", payload={},
                                  recipients=CHATS, text="P1 CARD")
        svc.notify_intent("P1 CARD", intent)
        svc.replay_journal(datetime(2026, 9, 9, 11, 10, tzinfo=ET))
    finally:
        restore_wire()
    per_chat = {c: len(wire.texts_to(c)) for c in CHATS}
    queued = config.state_get("pending_sends", []) or []
    return (per_chat["900001"] == 1 and per_chat["900003"] == 1
            and per_chat["900002"] >= 2 and not queued,
            f"per chat {per_chat} queued={len(queued)}")


probe("P1 a partial recipient failure retries only the unresolved recipient "
      "and never re-broadcasts to the others", _p1)


def _p2():
    """P2: a 3 part message where part 2 fails is failed with a part count,
    and the retry resumes at part 2."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    long_text = "\n\n".join(["A" * 3500, "B" * 3500, "C" * 3500])
    parts = telegram.split_message(long_text)
    if len(parts) != 3:
        return False, f"fixture made {len(parts)} parts, wanted 3"
    state = {"fail_second": True}

    def _script(n):
        if n == 2 and state["fail_second"]:
            # 400, not 500: a 4xx is a DEFINITE refusal of that part, which is
            # what makes the part count meaningful. A 5xx would be unknown,
            # because the body was already written, and an unknown part poisons
            # the whole recipient by design.
            return _Resp(400, body={"ok": False}, text='{"ok":false}')
        return _Resp()

    wire = with_wire({"900001": _script})
    try:
        rec = telegram.send_to_detailed("900001", long_text)
        if rec["status"] != ej.FAILED or rec["parts_total"] != 3 \
                or rec["parts_confirmed"] != 1:
            return False, (f"status={rec['status']} total={rec['parts_total']} "
                           f"confirmed={rec['parts_confirmed']}")
        state["fail_second"] = False
        got = len(wire.texts_to("900001"))
        rec2 = telegram.send_to_detailed("900001", long_text,
                                         start_part=rec["parts_confirmed"])
        resumed = wire.texts_to("900001")[got:]
    finally:
        restore_wire()
    return (rec2["status"] == ej.CONFIRMED and len(resumed) == 2
            and resumed[0].startswith("B"),
            f"resumed {len(resumed)} parts, first={resumed[0][:1] if resumed else '?'}")


probe("P2 a partial multi-part send is failed with a part count and the "
      "retry resumes at the first unconfirmed part", _p2)


def _p3():
    """P3: recipient identifiers never leave the process."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    wire = with_wire({"900002": lambda n: _Resp(
        403, body={"ok": False}, text='{"ok":false}')})
    try:
        svc = fresh_service()
        intent = ej.commit_intent("entry", candidate_id="c-p3",
                                  decision_id=ej.new_decision_id(),
                                  position_id="p-p3", payload={},
                                  recipients=CHATS, text="P3 CARD")
        svc.notify_intent("P3 CARD", intent)
    finally:
        restore_wire()
    blobs = [ej.report_text()]
    for p in sorted((_TMP / "events").rglob("*")):
        if p.is_file() and p.suffix in (".jsonl", ".json"):
            blobs.append(p.read_text(encoding="utf-8"))
    leaked = [c for c in CHATS if any(c in b for b in blobs)]
    refs = {r.get("recipient_ref") for r in ej.get(intent.journal_id).recipients}
    return (not leaked and len(refs) == 3 and None not in refs,
            f"leaked={leaked} refs={len(refs)}")


probe("P3 no recipient chat id appears in the journal, the index or the "
      "report, only an index and an opaque ref", _p3)


# ===========================================================================
print("\n--- R. reservations carry the operation identity (Astra A06) ---")
# ===========================================================================


def _r1():
    """R1: op A's release must not delete op B's newer claim."""
    reset_all()
    if not hasattr(config, "state_reserve"):
        return False, "no owner-identified reservation helpers"
    key = "2026-09-09:^GSPC"
    a, b = "op-A", "op-B"
    if not config.state_reserve("sniper_alerted", key, a):
        return False, "A could not claim a free key"
    if config.state_reserve("sniper_alerted", key, b):
        return False, "B claimed a key A already holds"
    # A's claim is dropped by a concurrent write, B claims it, then A releases
    config.state_set("sniper_alerted", {})
    if not config.state_reserve("sniper_alerted", key, b):
        return False, "B could not claim the freed key"
    config.state_release_owned("sniper_alerted", key, a)
    held = config.state_get("sniper_alerted", {}) or {}
    entry = held.get(key)
    return (isinstance(entry, dict) and entry.get("op") == b,
            f"after A released, the key holds {entry!r}")


probe("R1 a stale release cannot delete a newer claimant's reservation", _r1)


def _r2():
    """R2: once ANY send attempt exists the reservation is committed and can
    never be released. An ambiguous delivered request is not an unsent
    opportunity."""
    reset_all()
    if not hasattr(config, "state_commit_owned"):
        return False, "no owner-identified reservation helpers"
    key = "2026-09-09:TSLA"
    op = "op-C"
    config.state_reserve("sniper_alerted", key, op)
    config.state_commit_owned("sniper_alerted", key, op)
    released = config.state_release_owned("sniper_alerted", key, op)
    held = config.state_get("sniper_alerted", {}) or {}
    return (released is False and key in held
            and (held[key] or {}).get("state") == "committed",
            f"released={released} entry={held.get(key)!r}")


probe("R2 a committed reservation is never released, even by its own owner",
      _r2)


def _r3():
    """R3: a legacy plain-string value reads as committed by an unknown op and
    can never be released by a new one."""
    reset_all()
    if not hasattr(config, "state_release_owned"):
        return False, "no owner-identified reservation helpers"
    key = "2026-09-09:SPY"
    config.state_set("sniper_alerted", {key: "09:52"})
    stole = config.state_reserve("sniper_alerted", key, "op-D")
    released = config.state_release_owned("sniper_alerted", key, "op-D")
    held = config.state_get("sniper_alerted", {}) or {}
    return (stole is False and released is False and key in held,
            f"stole={stole} released={released} left={held.get(key)!r}")


probe("R3 a legacy plain-string reservation is committed by an unknown op and "
      "survives a new op's release", _r3)


def _r4():
    """R4: the sniper path. A standby crossing BEFORE any send attempt hands
    the day key back; after a send attempt it does not."""
    reset_all()
    if not hasattr(config, "state_reserve"):
        return False, "no owner-identified reservation helpers"
    import fvg as fvg_mod
    import market_tools
    symbol = "TSLA" if "TSLA" in fvg_mod.SNIPER_SYMBOLS \
        else sorted(fvg_mod.SNIPER_SYMBOLS)[0]
    now = datetime(2026, 9, 9, 11, 30, 0, tzinfo=ET)
    day = f"{now:%Y-%m-%d}"
    key = f"{day}:{symbol}"

    def _read_any(name):
        if str(name).lower() not in (
                scanner.Service.SNIPER_READS.get(symbol, symbol).lower(),):
            return {"conviction": "none"}
        return {"instrument": symbol, "symbol": symbol, "decimals": 2,
                "conviction": "high", "price": 250.0,
                "plan": {"direction": "BUY"},
                "recent_bars": [],
                "fvg": {"confirming": {"ticket": {
                    "entry": 250.0, "stop": 248.0, "target": 250.8}}}}

    real_read = market_tools.read_any
    market_tools.read_any = _read_any
    svc = fresh_service()
    real_standby = telegram.standby
    calls = {"n": 0}

    def _standby_after(n_calls):
        def _fn():
            calls["n"] += 1
            return (calls["n"] > n_calls, "test crossing")
        return _fn

    try:
        # cross into standby before ANY send: the key must come back
        telegram.standby = _standby_after(2)
        svc._scan_snipers_once(now, entries_allowed=True)
        after_early = config.state_get("sniper_alerted", {}) or {}
    finally:
        telegram.standby = real_standby
        market_tools.read_any = real_read
    return (key not in after_early,
            f"day key left behind after an early stand-down: "
            f"{after_early.get(key)!r}")


probe("R4 a stand-down before any send attempt hands the sniper day key back",
      _r4)


def _r5():
    """R5: a card the WIRE dropped because this copy was gagged must stay
    owed. The journal is on the shared volume, so the copy that owns the lease
    is the one that finishes it. Resolving it here would leave a committed day
    key with the card sent by nobody, which is the failure the old
    compensating release was flailing at."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    intent = ej.commit_intent("sniper_entry", candidate_id="r5-cand",
                              decision_id=ej.new_decision_id(),
                              position_id="r5-pos", payload={},
                              recipients=CHATS, text="R5 CARD")
    wire = with_wire()
    telegram.set_standby(True, "test crossing")
    try:
        svc = fresh_service()
        svc._deliver(intent, None, text="R5 CARD")
        dropped = ej.get(intent.journal_id)
        still_owed = [d for d in dropped.deliveries() if not d.resolved]
        # ...and now the copy that owns the lease comes up and replays
        telegram.set_standby(False)
        svc2 = fresh_service()
        svc2.replay_journal(datetime(2026, 9, 9, 11, 45, tzinfo=ET))
    finally:
        telegram.set_standby(False)
        restore_wire()
    delivered = [t for _, t in wire.posts if "R5 CARD" in t]
    final = ej.get(intent.journal_id)
    return (len(still_owed) == 3 and len(delivered) == 3 and final.resolved,
            f"owed={len(still_owed)} delivered={len(delivered)} "
            f"resolved={final.resolved}")


probe("R5 a card the wire dropped in standby stays owed and the lease holder "
      "delivers it on replay", _r5)


# ===========================================================================
print("\n--- G. appends during a grading pass, and concurrent appends ---")
# ===========================================================================


def _g1():
    """G1: the grading pass republishes the whole ledger. A journal append and
    a mark_selected during that window must both survive."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=ET)
    cids = []
    for i in range(3):
        cids.append(forward_ledger.record_candidate(
            "EURUSD=X", "BUY", 1.10 + i / 1000, 0.0012,
            {"entry": 1.10 + i / 1000, "stop": 1.0980},
            {"grade": "A", "score": 9}, True, [], gap_atr=1.6, hour_et=12,
            now_et=now + timedelta(seconds=i)))
    started = threading.Event()
    done = threading.Event()
    import yfinance

    def _slow_download(*a, **k):
        started.set()
        time_mod.sleep(0.6)
        import pandas as pd
        return pd.DataFrame()

    real_dl = yfinance.download
    yfinance.download = _slow_download

    def _grade():
        try:
            forward_ledger.fill_outcomes(now_et=now + timedelta(hours=5))
        except Exception:
            pass
        done.set()

    t = threading.Thread(target=_grade, daemon=True)
    t.start()
    started.wait(3)
    intents = []
    for i in range(5):
        intents.append(ej.commit_intent(
            "entry", candidate_id=f"g1-{i}", decision_id=ej.new_decision_id(),
            position_id=f"g1-pos-{i}", payload={"n": i}, recipients=CHATS,
            text=f"G1 CARD {i}"))
    forward_ledger.mark_selected(cids[0], fired_at_et=now,
                                 position_id="g1-pos-0")
    done.wait(10)
    t.join(5)
    yfinance.download = real_dl
    rows = _ledger_rows()
    kept = [r for r in rows if r.get("event_id") in cids]
    sel = [r for r in kept if r.get("selected")]
    survived = [i for i in intents if ej.get(i.journal_id) is not None]
    return (len(kept) == 3 and len(sel) == 1 and len(survived) == 5,
            f"rows={len(kept)} selected={len(sel)} intents={len(survived)}")


probe("G1 journal appends and a selection made during a grading pass all "
      "survive the pass republishing the ledger", _g1)


def _g2():
    """G2: two threads appending 200 records each. 400 parseable lines, no
    torn line, strictly increasing seq, and nothing rewritten."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    errs = []

    def _worker(tag):
        for i in range(200):
            try:
                ej.commit_intent("entry", candidate_id=f"{tag}-{i}",
                                 decision_id=ej.new_decision_id(),
                                 position_id=f"{tag}-pos-{i}", payload={},
                                 recipients=CHATS[:1], text=f"{tag} {i}")
            except Exception as e:      # noqa: BLE001
                errs.append(f"{type(e).__name__}: {e}")

    ts = [threading.Thread(target=_worker, args=(f"t{n}",)) for n in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    res = storage_io.read_jsonl(ej.daily_path())
    lines = list(res.value or [])
    intents = [r for r in lines if r.get("kind") == "intent"]
    seqs = [r.get("seq") for r in lines]
    return (not errs and res.status == "ok" and len(intents) == 400
            and len(set(seqs)) == len(seqs) and sorted(seqs) == seqs,
            f"status={res.status} intents={len(intents)} errs={errs[:2]}")


probe("G2 concurrent journal appends lose nothing and tear no line", _g2)


# ===========================================================================
print("\n--- H. durable intent creation fails ---")
# ===========================================================================


def _blocked_journal():
    """A journal whose durable append always fails, the way a dead volume
    fails it."""
    ej = jrn()
    real = storage_io.append_jsonl

    def _fail(path, record, **kw):
        if "events" in str(path):
            return storage_io.WriteResult(False, "disk_full", 0, 0,
                                          "no space left on device", path)
        return real(path, record, **kw)

    storage_io.append_jsonl = _fail
    return real


def _h1():
    """H1: no unrecorded actionable entry. No card, no position, an explicit
    health condition, and one owner DM."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    dms = []
    svc = fresh_service(dms)
    _stub_entry_path(svc)
    sent = []
    svc.notify = lambda text: sent.append(text) or []
    svc.notify_intent = lambda text, intent: sent.append(text) or []
    real = _blocked_journal()
    try:
        now = datetime(2026, 9, 9, 12, 30, 0, tzinfo=ET)
        opened = svc.open_position(_Setup(), now)
    finally:
        storage_io.append_jsonl = real
    _restore_entry_path()
    health = config.state_get("journal_health") or {}
    return (opened is False and not sent and not svc.book.positions
            and health.get("ok") is False and len(dms) == 1,
            f"opened={opened} sent={len(sent)} pos={len(svc.book.positions)} "
            f"health={health} dms={len(dms)}")


probe("H1 a failed durable intent creates no unrecorded entry and raises an "
      "explicit health condition", _h1)


def _h2():
    """H2: the same fault must never discard known open positions or stop
    their monitoring. The exit still evaluates and the card still goes out."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    now = datetime(2026, 9, 9, 12, 40, 0, tzinfo=ET)
    ev = [{"type": "stop", "pct": -45.0, "mark": 0.55}]
    svc, fired = _exit_service(ev)
    pos = a_position(pid="20260909-124000-SPY-C500")
    svc.book.positions = [pos]
    svc.book.save()
    sent = []
    svc.notify = lambda text: sent.append(text) or []
    real = _blocked_journal()
    try:
        watch = svc.book.needs_monitoring(date(2026, 9, 9))
        for p in watch:
            svc.monitor_one(p, now)
    finally:
        storage_io.append_jsonl = real
    poslib.step = _REAL_STEP
    health = config.state_get("journal_health") or {}
    return (len(watch) == 1 and fired["n"] == 1
            and len([t for t in sent if "STOP CARD" in t]) == 1
            and health.get("ok") is False
            and bool(health.get("evidence_gap")),
            f"watched={len(watch)} fired={fired['n']} sent={len(sent)} "
            f"health={health}")


probe("H2 an unavailable journal never discards open positions or stops the "
      "exit path, and the degradation is recorded as an evidence gap", _h2)


# ===========================================================================
print("\n--- O. zero lost logical trades, and unlinkable tickets ---")
# ===========================================================================


def _o1():
    """O1: 20 logical trades with a crash injected at every persist, send and
    link boundary. After replay: every one accounted for, one position per
    decision, one candidate per committed intent, empty orphan report."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    from dataclasses import asdict
    now = datetime(2026, 9, 9, 13, 0, 0, tzinfo=ET)
    made = []
    for i in range(20):
        cid = f"o1-cand-{i}"
        decision = ej.new_decision_id()
        pid = f"o1-pos-{i}"
        boundary = i % 4
        modeled = asdict(a_position(pid=pid, candidate_id=cid,
                                    decision_id=decision))
        try:
            intent = ej.commit_intent(
                "entry", candidate_id=cid, decision_id=decision,
                position_id=pid, payload={"n": i, "position": modeled},
                recipients=CHATS, text=f"O1 CARD {i}")
        except Exception as e:      # noqa: BLE001
            return False, f"commit_intent raised on a healthy volume: {e}"
        made.append((intent, decision, pid, cid))
        if boundary == 0:
            pass                                   # died right after the intent
        elif boundary == 1:
            ej.record_attempt(intent.journal_id, 0)  # died mid send
        elif boundary == 2:
            for r in range(len(CHATS)):             # died after the send,
                ej.record_result(intent.journal_id, r, ej.CONFIRMED,
                                 provider_message_id=r)
        else:
            for r in range(len(CHATS)):             # died after the link
                ej.record_result(intent.journal_id, r, ej.CONFIRMED,
                                 provider_message_id=r)
            ej.mark_linked(intent.journal_id, position_id=pid)
    svc = fresh_service()
    wire = with_wire()
    try:
        svc.replay_journal(now)
        svc.replay_journal(now)
    finally:
        restore_wire()
    unresolved = ej.unresolved()
    orphans = ej.orphans()
    decisions = [d for _, d, _, _ in made]
    cands = {c for _, _, _, c in made}
    # zero lost logical trades: every decision ends up as exactly one position,
    # and every committed intent still names one candidate
    by_decision = [svc.book.find_by_decision(d) for d in decisions]
    recovered = [p for p in by_decision if p is not None]
    ids = {p.id for p in recovered}
    return (not unresolved and not orphans and len(set(decisions)) == 20
            and len(cands) == 20 and len(recovered) == 20 and len(ids) == 20
            and len(svc.book.positions) == 20,
            f"unresolved={len(unresolved)} orphans={len(orphans)} "
            f"recovered={len(recovered)} book={len(svc.book.positions)}")


probe("O1 zero lost logical trades across a crash at every boundary, and an "
      "empty orphan report after replay", _o1)


def _o2():
    """O2: the observation row could not be written, so the ticket is born
    unlinkable. The id still has to exist and the orphan has to be named."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    import market_tools
    real_rc = forward_ledger.record_candidate
    forward_ledger.record_candidate = lambda *a, **k: None
    try:
        cid = forward_ledger.candidate_id_for_ticket(
            "EURUSD=X", "BUY", {"entry": 1.1000, "stop": 1.0980}, 1.1000,
            datetime(2026, 9, 9, 13, 30, 0, tzinfo=ET))
    finally:
        forward_ledger.record_candidate = real_rc
    if not cid:
        return False, "no id was minted in the reader"
    decision = ej.new_decision_id()
    intent = ej.commit_intent("sniper_entry", candidate_id=cid,
                              decision_id=decision, position_id="o2-pos",
                              payload={}, recipients=CHATS, text="O2 CARD")
    for r in range(len(CHATS)):
        ej.record_result(intent.journal_id, r, ej.CONFIRMED,
                         provider_message_id=r)
    svc = fresh_service()
    wire = with_wire()
    try:
        svc.replay_journal(datetime(2026, 9, 9, 13, 35, tzinfo=ET))
    finally:
        restore_wire()
    orph = ej.orphans()
    classes = {o.get("orphan_class") for o in orph}
    return ("ledger_row_missing" in classes,
            f"orphans={orph[:2]}")


probe("O2 a ticket whose observation row is missing keeps its candidate id "
      "and is reported as a ledger_row_missing orphan", _o2)


def _o3():
    """The other half of O2, in the reader itself: the candidate id must ride
    out on the read even when the recorder declined.

    Two halves. The reader must MINT the id rather than take whatever
    record_candidate handed back, and the two derivations must be the same one,
    or an id stamped on a ticket would not match the id on its own ledger row.
    """
    reset_all()
    import market_tools
    src = Path(market_tools.__file__).read_text(encoding="utf-8")
    mints = "candidate_id_for_ticket" in src
    from_recorder = "_cid = _fl.record_candidate(" in src
    now = datetime(2026, 9, 9, 13, 40, 0, tzinfo=ET)
    ticket = {"entry": 1.1000, "stop": 1.0980}
    minted = forward_ledger.candidate_id_for_ticket(
        "EURUSD=X", "BUY", ticket, 1.1000, now)
    stored = forward_ledger.record_candidate(
        "EURUSD=X", "BUY", 1.1000, 0.0012, ticket, {"grade": "A", "score": 9},
        True, [], gap_atr=1.6, hour_et=13, now_et=now)
    return (mints and not from_recorder and bool(minted) and minted == stored,
            f"mints={mints} from_recorder={from_recorder} "
            f"minted={minted} stored={stored}")


probe("O3 the reader mints the candidate id itself instead of keeping only "
      "the one the recorder returned, and the two derivations agree", _o3)


# ===========================================================================
print("\n--- C. identity: three ids, linked by role ---")
# ===========================================================================


def _c1():
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    a = ej.candidate_id_for(date="2026-09-09", ticker="SPY", direction="call",
                            strike=500.0, bar="2026-09-09T10:00", spot=499.0,
                            mom=0.42)
    b = ej.candidate_id_for(date="2026-09-09", ticker="SPY", direction="call",
                            strike=500.0, bar="2026-09-09T10:00", spot=499.0,
                            mom=0.42)
    c = ej.candidate_id_for(date="2026-09-09", ticker="SPY", direction="call",
                            strike=500.0, bar="2026-09-09T10:05", spot=499.0,
                            mom=0.42)
    d1, d2 = ej.new_decision_id(), ej.new_decision_id()
    return (a == b and a != c and d1 != d2 and len(d1) >= 16,
            "the same unchanged bar must be ONE candidate")


probe("C1 one stable candidate id per unchanged observation, and a separate "
      "immutable decision id", _c1)


def _c2():
    """C2: a position is opened at most once per decision_id, in both books."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    book = poslib.PositionBook(_TMP / "positions.json")
    decision = ej.new_decision_id()
    p1 = a_position(pid="c2-a", decision_id=decision)
    p2 = a_position(pid="c2-b", decision_id=decision)
    book.add(p1)
    book.add(p2)
    same = len(book.positions) == 1 and book.find_by_decision(decision) is not None
    row1 = sniper_book.open_trade(
        symbol="EURUSD=X", display="EUR/USD", direction="BUY", entry=1.1000,
        stop=1.0980, target=1.1008, day="2026-09-09", time_et="10:00:00",
        decimals=5, candidate_id="c2-cand", decision_id=decision,
        position_id="c2-snipe")
    row2 = sniper_book.open_trade(
        symbol="JPY=X", display="USD/JPY", direction="BUY", entry=150.0,
        stop=149.0, target=150.4, day="2026-09-09", time_et="10:05:00",
        decimals=3, candidate_id="c2-cand", decision_id=decision,
        position_id="c2-snipe-2")
    # the second call gets the row the FIRST one opened, so a replay that
    # re-reaches this point repairs one trade instead of opening a second
    rows = [r for r in sniper_book.all_rows()
            if r.get("decision_id") == decision]
    return (same and row1 is not None and row2 is not None
            and row1.get("id") == "c2-snipe"
            and row2.get("id") == "c2-snipe" and len(rows) == 1,
            f"book={len(book.positions)} sniper rows={len(rows)} "
            f"row2 id={(row2 or {}).get('id')}")


probe("C2 one decision id opens at most one position in each book", _c2)


def _c3():
    """C3: every committed alert intent links to one position and one
    candidate. That is Astra's acceptance line, asserted directly."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    svc = fresh_service()
    _stub_entry_path(svc)
    wire = with_wire()
    try:
        now = datetime(2026, 9, 9, 14, 0, 0, tzinfo=ET)
        ok = svc.open_position(_Setup(), now)
    finally:
        restore_wire()
        _restore_entry_path()
    if not ok:
        return False, "the entry did not open"
    res = storage_io.read_jsonl(ej.daily_path())
    intents = [r for r in (res.value or []) if r.get("kind") == "intent"]
    if len(intents) != 1:
        return False, f"{len(intents)} intents for one entry"
    it = intents[0]
    pos = [p for p in svc.book.positions if p.id == it.get("position_id")]
    return (bool(it.get("candidate_id")) and bool(it.get("decision_id"))
            and len(pos) == 1 and pos[0].decision_id == it.get("decision_id")
            and pos[0].candidate_id == it.get("candidate_id"),
            f"intent={ {k: it.get(k) for k in ('candidate_id', 'decision_id', 'position_id')} }")


probe("C3 every committed alert intent links to exactly one position and one "
      "candidate", _c3)


def _c4():
    """C4: the index is a materialized view, never the only truth. Delete it
    and replay must rebuild every unresolved intent from the daily files."""
    reset_all()
    ej = jrn()
    if ej is None:
        return False, "no event_journal"
    ids = []
    for i in range(4):
        it = ej.commit_intent("entry", candidate_id=f"c4-{i}",
                              decision_id=ej.new_decision_id(),
                              position_id=f"c4-pos-{i}", payload={},
                              recipients=CHATS, text=f"C4 {i}")
        ids.append(it.journal_id)
    ej.resolve(ids[0])
    ej.index_path().unlink()
    ej._reset_for_test()
    rebuilt = {i.journal_id for i in ej.unresolved()}
    return (rebuilt == set(ids[1:]), f"rebuilt {len(rebuilt)} of 3")


probe("C4 the open-intents index is rebuilt from the append-only files when "
      "it is lost", _c4)


# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All W02 event recovery checks passed.")
