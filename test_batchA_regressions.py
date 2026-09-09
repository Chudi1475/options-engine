"""Batch A adversarial findings: the defects a green 19/19 suite was hiding.

W05 (storage_io), W01 (five-state ownership) and W02 (event_journal) landed
together, the gate was green, and a four-lens pass then reproduced nineteen
defects inside that same batch. This file is the regression for the thirteen of
them that live in scanner.py, telegram.py and storage_io.py. Every section
FAILS against the code as it stood before the fix and passes after it.

  A  news_seen.json (#6, CRITICAL). It is a hard gate on reaching ACTIVE and
     the only mandatory reconcile store still written with a truncate-then-
     write whose OSError is swallowed. One torn or zero byte file stranded the
     sole instance in BLOCKED forever, on a healthy disk, with open positions
     never monitored and no human free way back.

  B  the dry run wire (#1, #11). The sniper entry path calls _deliver directly
     and _deliver had no dry guard, so `python scanner.py --dry-run` put real
     sniper tickets on three real phones. The guard belongs at the wire, where
     the test mode guard already sits, not at each call site.

  C  a delivered card with no tracked position (#7, #12, #17). open_position
     threw away PositionBook.add()'s new boolean and resolved the intent on
     delivery alone, and the sniper replay never classified a missing row at
     all. Astra A05 both ways: never infer delivery from position existence,
     and never infer a position from delivery.

  D  no scheduled retry (#10). replay_journal ran once, on the way into ACTIVE,
     and the ACTIVE fast path returned before reaching it, so MAX_DELIVERY_
     RETRIES had no caller and a journaled card whose send failed was never
     tried again while the process lived.

  E  no ACTIVE to BLOCKED edge (#4). state_health was asked once, on the way
     in. A state.json torn AFTER promotion left the copy sending and saving
     off a file it could no longer read.

  F  the durable delivery store was invisible to promotion (#2). The "pending
     deliveries" slot validated the LEGACY state.json queue and never looked
     at the journal the same batch introduced.

  G  the outage that silences its own dedup (#8). _journal_down's once a day
     flag is written into state.json, the same volume whose failure triggered
     it, so on a full disk the owner was DMed on every cycle.

  H  the wedged winner DM fired at itself (#9). standby_wait's branch ran in
     the two new states where this copy holds the lock, so during an outage
     the sole instance told the owner to restart a copy that is itself.

  I  multi part resume was dead code (#13). send_to_detailed can resume at the
     first unconfirmed part and nothing on the production retry path ever
     asked it to.

  J  sweep_orphans deleted foreign files (#14). It globs *.tmp and unlinks any
     match, contradicting its own docstring, in a directory that defaults to
     the repo root.

No network, no Telegram, no yfinance, no production storage.

Run:  python test_batchA_regressions.py     (exit code 0 = all good)
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
import time as time_mod
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# an isolated data dir BEFORE config is imported, so nothing here can read or
# write the real runtime state
_TMP = Path(tempfile.mkdtemp(prefix="kelbot_batchA_"))
_bot_test_os.environ["DATA_DIR"] = str(_TMP)

import config          # noqa: E402

config.DATA_DIR = _TMP
config.STATE_FILE = _TMP / "state.json"
config.POSITIONS_FILE = _TMP / "positions.json"
config.ALERTS_LOG = _TMP / "alerts.log"
config.ALERTS_JSONL = _TMP / "alerts_sent.jsonl"
config.NEWS_SEEN_FILE = _TMP / "news_seen.json"
config._LAST_GOOD = {}

import event_journal   # noqa: E402
import forward_ledger  # noqa: E402
import instance_lock   # noqa: E402
import positions as poslib  # noqa: E402
import scanner         # noqa: E402
import sniper_book     # noqa: E402
import storage_io      # noqa: E402
import telegram        # noqa: E402

forward_ledger.LEDGER = _TMP / "sniper_forward.jsonl"
sniper_book.LEDGER = _TMP / "sniper_positions.json"

ET = ZoneInfo("America/New_York")
CHATS = ["900001", "900002", "900003"]
NOW = datetime(2026, 9, 9, 11, 0, 0, tzinfo=ET)

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def probe(name, fn):
    """check() for something that may raise. A missing attribute is a normal
    FAIL line instead of a traceback that would hide every later section, which
    matters when the whole point of the run is to watch a red list before the
    change and the same list green after it."""
    try:
        out = fn()
    except Exception as e:                                     # noqa: BLE001
        check(name, False, f"{type(e).__name__}: {e}")
        return False
    detail = ""
    if isinstance(out, tuple):
        out, detail = out
    check(name, bool(out), str(detail))
    return bool(out)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _Resp:
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
        self.posts = []              # (chat_id, text)
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


def with_wire(script=None):
    wire = _Wire(script)
    telegram._session = wire
    telegram.allow_test_send(True)
    return wire


def restore_wire():
    telegram.allow_test_send(False)


def reset_all():
    """Wipe every runtime file between sections. Only inside the temp dir."""
    for p in sorted(_TMP.rglob("*"), key=lambda q: -len(str(q))):
        try:
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        except OSError:
            pass
    config._LAST_GOOD = {}
    event_journal._reset_for_test()
    storage_io._SWEPT.clear()
    instance_lock.release()
    instance_lock.set_state(instance_lock.STARTING, "test reset")
    telegram.set_ownership_state(instance_lock.STARTING, "test reset")


def seed_good_state():
    """A healthy volume: parseable state.json and a real open position."""
    config.STATE_FILE.write_text(json.dumps({"tg_offset": 5}), encoding="utf-8")
    config.POSITIONS_FILE.write_text(json.dumps([]), encoding="utf-8")


def fresh_service(dms=None, dry=False):
    svc = scanner.Service(dry_run=dry)
    svc.dry = dry
    svc._hb_owner = (lambda text: dms.append(text)) if dms is not None \
        else (lambda text: None)
    return svc


def an_intent(kind="exit", text="STOP CARD", event_key="p1:stop", **kw):
    return event_journal.commit_intent(
        kind, candidate_id=kw.pop("candidate_id", ""),
        decision_id=kw.pop("decision_id", "d-1"),
        position_id=kw.pop("position_id", "p1"),
        strategy_id="momentum", event_key=event_key,
        payload=kw.pop("payload", {}), recipients=CHATS, text=text)


# the momentum entry path, made deterministic: no chain, no news, no scoreboard
_ENTRY_ORIG = {
    "nearest": scanner.quotes.nearest_listed_expiry,
    "quote": scanner.quotes.get_option_quote,
    "est": scanner.quotes.estimate_premium,
    "earn": scanner.news.earnings_inside,
    "hot": scanner.news.hot_headlines,
    "stats": scanner.scoreboard.stats_for_card,
    "card": scanner.cards.entry_card,
}


class _Setup:
    ticker = "SPY"
    direction = "call"
    strike = 500.0
    spot = 499.0
    mom_pct = 0.42
    reason = "test"


def _stub_entry_path(svc, card="ENTRY CARD"):
    scanner.quotes.nearest_listed_expiry = lambda t, d: d
    scanner.quotes.get_option_quote = lambda *a, **k: None
    scanner.quotes.estimate_premium = lambda *a, **k: 1.25
    scanner.news.earnings_inside = lambda *a, **k: (False, None)
    scanner.news.hot_headlines = lambda *a, **k: []
    scanner.scoreboard.stats_for_card = lambda *a, **k: None
    scanner.cards.entry_card = lambda *a, **k: card
    svc.sigma = lambda t: 0.20
    svc.current_mode = lambda: ("green", "")


def _restore_entry_path():
    scanner.quotes.nearest_listed_expiry = _ENTRY_ORIG["nearest"]
    scanner.quotes.get_option_quote = _ENTRY_ORIG["quote"]
    scanner.quotes.estimate_premium = _ENTRY_ORIG["est"]
    scanner.news.earnings_inside = _ENTRY_ORIG["earn"]
    scanner.news.hot_headlines = _ENTRY_ORIG["hot"]
    scanner.scoreboard.stats_for_card = _ENTRY_ORIG["stats"]
    scanner.cards.entry_card = _ENTRY_ORIG["card"]


check("isolated data dir, not production",
      str(config.DATA_DIR) == str(_TMP), f"{config.DATA_DIR} vs {_TMP}")
check("the wire is gagged for this whole run", telegram.test_mode())


# ===========================================================================
print("\n--- A. news_seen.json must not be able to brick the only instance (#6) ---")
# ===========================================================================
def _a1():
    """A torn news_seen.json is a rebuildable cache, not the position book.
    The instance must reach ACTIVE anyway, and say what it did."""
    reset_all()
    seed_good_state()
    # exactly what a SIGKILL inside the 12s news rewrite, or an ENOSPC out of
    # the middle of Path.write_text, leaves behind
    config.NEWS_SEEN_FILE.write_bytes(b'{"seen": ["a", "b"')
    svc = fresh_service()
    got = [svc.ensure_active(NOW) for _ in range(5)]
    state = instance_lock.state()
    instance_lock.release()
    return (any(got) and state == instance_lock.ACTIVE,
            f"ensure_active={got} state={state} "
            f"report={svc._reconcile_report}")


probe("A1 a torn news_seen.json does not strand the sole instance in BLOCKED",
      _a1)


def _a2():
    """The write goes through storage_io, so a refused write leaves the file
    that is already there intact instead of truncating it to nothing."""
    reset_all()
    seed_good_state()
    good = {"seen": ["already texted this one"], "date": "2026-09-09"}
    config.NEWS_SEEN_FILE.write_text(json.dumps(good), encoding="utf-8")
    svc = fresh_service()
    real_free = storage_io._disk_free
    storage_io._disk_free = lambda d: 0        # a full 500 MB volume
    try:
        ok = svc._save_news_seen(["a new headline"], "2026-09-09")
    finally:
        storage_io._disk_free = real_free
    after = storage_io.read_json(config.NEWS_SEEN_FILE)
    return (ok is False and after.status == "ok" and after.value == good,
            f"returned {ok!r}, file now {after.status} {after.value!r}")


probe("A2 a refused news_seen write says so and leaves the old file readable",
      _a2)


# ===========================================================================
print("\n--- B. a dry run must put nothing on the wire (#1, #11) ---")
# ===========================================================================
def _b1():
    """--dry-run reaches _deliver through the sniper path, and _deliver had no
    dry guard. The guard has to sit at the wire so no call site can route
    around it."""
    reset_all()
    seed_good_state()
    svc = fresh_service(dry=True)
    active = svc.ensure_active(NOW)
    intent = an_intent(kind="sniper_entry", text="SNIPER ticket",
                       event_key="dry:ticket")
    wire = with_wire()
    try:
        svc._deliver(intent, None, text="SNIPER ticket")
        telegram.send_to("900001", "ops DM from a dry run", ops=True)
        telegram.send_photo_all(b"not-a-real-image", caption="dry chart")
    finally:
        restore_wire()
    return (not wire.posts,
            f"dry run active={active} posted {len(wire.posts)} messages: "
            f"{[c for c, _ in wire.posts]}")


probe("B1 a dry run sends nothing through _deliver, send_to or send_photo_all",
      _b1)


def _b2():
    """...and a LIVE service still sends, so the guard is a dry-run guard and
    not a new way to mute the bot."""
    reset_all()
    seed_good_state()
    svc = fresh_service()
    svc.ensure_active(NOW)
    intent = an_intent(kind="exit", text="LIVE CARD", event_key="live:stop")
    wire = with_wire()
    try:
        svc._deliver(intent, None, text="LIVE CARD")
    finally:
        restore_wire()
        instance_lock.release()
    return len(wire.posts) == len(CHATS), f"posted {len(wire.posts)}"


probe("B2 a live service still delivers to every recipient", _b2)


def _b3():
    """Gagging the wire alone is not enough. The journal is on the SHARED
    volume, so a dry copy that still committed a durable intent and then
    (rightly) sent nothing left a committed-but-undelivered card behind for the
    LIVE daemon to replay and broadcast. Texting nobody today and making the
    real bot text three people tomorrow is the same defect one restart later."""
    reset_all()
    seed_good_state()
    svc = fresh_service(dry=True)
    svc.ensure_active(NOW)
    _stub_entry_path(svc)
    wire = with_wire()
    try:
        done = svc.open_position(_Setup(), NOW)
    finally:
        restore_wire()
        _restore_entry_path()
    shared = storage_io.read_json(config.POSITIONS_FILE)
    return (done and not event_journal.all_intents() and not wire.posts
            and shared.value == [],
            f"returned={done} intents={len(event_journal.all_intents())} "
            f"posts={len(wire.posts)} shared book={shared.value!r}")


probe("B3 a dry run entry writes no shared durable intent and no shared book",
      _b3)


def _b4():
    """The sniper pass is the one that burns a day/symbol reservation in the
    shared state.json. A dry run that burns it makes the LIVE daemon skip that
    symbol's real alert for the rest of the day."""
    reset_all()
    seed_good_state()
    import fvg as fvg_mod
    import market_tools
    symbol = sorted(fvg_mod.SNIPER_SYMBOLS)[0]
    day = f"{NOW:%Y-%m-%d}"
    key = f"{day}:{symbol}"

    def _read_any(name):
        if str(name).lower() != str(
                scanner.Service.SNIPER_READS.get(symbol, symbol)).lower():
            return {"conviction": "none"}
        return {"instrument": symbol, "symbol": symbol, "decimals": 2,
                "conviction": "high", "price": 250.0,
                "plan": {"direction": "BUY"}, "recent_bars": [],
                "fvg": {"confirming": {"ticket": {
                    "entry": 250.0, "stop": 248.0, "target": 250.8}}}}

    real_read = market_tools.read_any
    market_tools.read_any = _read_any
    svc = fresh_service(dry=True)
    svc.ensure_active(NOW)
    wire = with_wire()
    try:
        svc._scan_snipers_once(NOW, entries_allowed=True)
    finally:
        market_tools.read_any = real_read
        restore_wire()
    held = config.state_get("sniper_alerted", {}) or {}
    return (key not in held and not event_journal.all_intents()
            and not wire.posts,
            f"day key={held.get(key)!r} "
            f"intents={len(event_journal.all_intents())} "
            f"posts={len(wire.posts)}")


probe("B4 a dry run never burns the live sniper day key", _b4)


# ===========================================================================
print("\n--- C. a delivered card with no tracked position (#7, #12, #17) ---")
# ===========================================================================
def _c1():
    """The entry card goes out and positions.json refuses the publish. The
    intent may NOT be resolved and the trade may not vanish from every report:
    a card on three phones for a trade in no store is a health condition."""
    reset_all()
    seed_good_state()
    svc = fresh_service()
    svc.ensure_active(NOW)
    _stub_entry_path(svc)
    real_write = storage_io.write_json

    def denied(path, obj, **kw):
        if Path(path).name == config.POSITIONS_FILE.name:
            return storage_io.WriteResult(False, "replace_denied", 3, 0,
                                          "[WinError 5] Access is denied",
                                          Path(path))
        return real_write(path, obj, **kw)

    storage_io.write_json = denied
    wire = with_wire()
    try:
        svc.open_position(_Setup(), NOW)
    finally:
        storage_io.write_json = real_write
        restore_wire()
        _restore_entry_path()
        instance_lock.release()
    entries = [i for i in event_journal.all_intents() if i.kind == "entry"]
    orphs = [o for o in event_journal.orphans()
             if o["orphan_class"] == "position_missing"]
    sent = [t for _, t in wire.posts if "ENTRY CARD" in t]
    return (len(entries) == 1 and not entries[0].resolved and len(orphs) == 1
            and sent,
            f"cards={len(sent)} intents={len(entries)} "
            f"resolved={[i.resolved for i in entries]} orphans={orphs}")


probe("C1 a refused positions.json publish leaves the entry intent open and "
      "reported as an orphan", _c1)


def _c2():
    """...and the same publish landing must still resolve normally, so the fix
    is not 'never resolve anything'."""
    reset_all()
    seed_good_state()
    svc = fresh_service()
    svc.ensure_active(NOW)
    _stub_entry_path(svc)
    wire = with_wire()
    try:
        svc.open_position(_Setup(), NOW)
    finally:
        restore_wire()
        _restore_entry_path()
        instance_lock.release()
    entries = [i for i in event_journal.all_intents() if i.kind == "entry"]
    return (len(entries) == 1 and entries[0].resolved
            and not event_journal.orphans()
            and len(svc.book.positions) == 1,
            f"intents={len(entries)} orphans={event_journal.orphans()}")


probe("C2 a healthy entry still resolves its intent and reports no orphan",
      _c2)


def _c3():
    """A sniper ticket that was broadcast and never tracked. _replay_link's
    sniper branch never set position_missing, so orphans() could not classify
    it and replay closed it for good."""
    reset_all()
    seed_good_state()
    svc = fresh_service()
    svc.ensure_active(NOW)
    intent = an_intent(kind="sniper_entry", text="SNIPER SPY BUY",
                       event_key="sniper:spy", decision_id="d-sniper",
                       position_id="2026-09-09-110000-SPY-BUY",
                       payload={"symbol": "SPY", "direction": "BUY",
                                "entry": 500.0, "stop": 499.0,
                                "target": 502.0,
                                "fired_at": "2026-09-09 11:00:00"})
    # the row is not in the sniper book: open_trade returns None on a busy
    # ledger lock, an unreadable book or a refused write, and all three print
    # and carry on
    sniper_book.LEDGER.write_text("[]", encoding="utf-8")
    real_open = sniper_book.open_trade
    sniper_book.open_trade = lambda **kw: None
    try:
        found = svc._replay_link(intent)
    finally:
        sniper_book.open_trade = real_open
        instance_lock.release()
    event_journal.mark_linked(intent.journal_id, **found)
    orphs = [o for o in event_journal.orphans()
             if o["orphan_class"] == "position_missing"]
    return (bool(found.get("position_missing")) and len(orphs) == 1,
            f"found={found} orphans={event_journal.orphans()}")


probe("C3 a sniper ticket with no tracked row is classified position_missing",
      _c3)


# ===========================================================================
print("\n--- D. a journaled send that failed is retried while the process "
      "lives (#10) ---")
# ===========================================================================
def _d1():
    reset_all()
    seed_good_state()
    dms = []
    svc = fresh_service(dms)
    svc.ensure_active(NOW)          # promotes, reconciles, replays once
    intent = an_intent(kind="exit", text="STOP CARD", event_key="p1:stop")
    import requests as _rq
    boom = {c: (lambda n: _rq.exceptions.ConnectionError("network blip"))
            for c in CHATS}
    wire = with_wire(boom)
    try:
        svc._deliver(intent, None, text="STOP CARD")
        first = len(wire.posts)
    finally:
        restore_wire()
    live = event_journal.get(intent.journal_id)
    failed_now = [d.status for d in live.deliveries()]
    # the process stays up and stays ACTIVE. nothing else happens: no restart,
    # no promotion, just the next poll cycle.
    svc._last_replay = 0.0
    ok_wire = with_wire()
    try:
        again = svc.ensure_active(NOW)
    finally:
        restore_wire()
        instance_lock.release()
    return (again and len(ok_wire.posts) == len(CHATS),
            f"first pass={first} statuses={failed_now} "
            f"retry pass={len(ok_wire.posts)} active={again}")


probe("D1 an ACTIVE process re-drives replay, so a failed card is re-sent",
      _d1)


def _d2():
    """The retry is bounded. A recipient the journal can never resolve must not
    become a card every pass for the life of the process."""
    reset_all()
    seed_good_state()
    svc = fresh_service()
    svc.ensure_active(NOW)
    intent = an_intent(kind="exit", text="STOP CARD", event_key="p1:stop")
    for _ in range(event_journal.MAX_DELIVERY_RETRIES):
        event_journal.record_attempt(intent.journal_id, 0)
    live = event_journal.get(intent.journal_id)
    wire = with_wire()
    try:
        out = svc._replay_send(live, [0])
    finally:
        restore_wire()
        instance_lock.release()
    return (not wire.posts and out == [],
            f"attempts={[d.attempts for d in live.deliveries()]} "
            f"posts={len(wire.posts)}")


probe("D2 replay stops at MAX_DELIVERY_RETRIES instead of resending forever",
      _d2)


# ===========================================================================
print("\n--- E. ACTIVE is re-checked, not entered once and trusted (#4) ---")
# ===========================================================================
def _e1():
    reset_all()
    seed_good_state()
    config.STATE_FILE.write_text(
        json.dumps({"tg_offset": 5, "morning_sent": "2026-09-09"}),
        encoding="utf-8")
    svc = fresh_service()
    up = svc.ensure_active(NOW)
    # a torn write / partial replace mid session: exactly what storage_io
    # exists for, arriving AFTER the promotion rather than before it
    config.STATE_FILE.write_text('{"tg_offset": 5, "morning_', encoding="utf-8")
    health = config.state_health()
    got = [svc.ensure_active(NOW) for _ in range(4)]
    state = instance_lock.state()
    gagged = telegram.standby()[0]
    quarantined = (config.STATE_FILE.with_suffix(".corrupt")).exists()
    instance_lock.release()
    return (up and health != "ok" and not any(got)
            and state in (instance_lock.RECOVERING, instance_lock.BLOCKED)
            and gagged and not quarantined,
            f"promoted={up} health={health} after={got} state={state} "
            f"gagged={gagged} quarantined={quarantined}")


probe("E1 a state.json torn AFTER promotion drops the copy out of ACTIVE and "
      "gags the wire without quarantining the file", _e1)


# ===========================================================================
print("\n--- F. promotion validates the DURABLE delivery store (#2) ---")
# ===========================================================================
def _f1():
    reset_all()
    seed_good_state()
    svc = fresh_service()
    svc.ensure_active(NOW)
    an_intent(kind="exit", text="STOP CARD", event_key="p1:stop")
    instance_lock.release()
    # the day's events file is there and the OS will not hand it over. This is
    # what an EIO/ESTALE mount fault, an AV hold or a mandatory range lock
    # does, and read_jsonl reports it as "unreadable" with value None.
    dp = event_journal.daily_path()
    dp.unlink()
    dp.mkdir()
    event_journal._reset_for_test()
    try:
        (event_journal.index_path()).unlink()
    except OSError:
        pass
    svc2 = fresh_service()
    got = [svc2.ensure_active(NOW) for _ in range(2)]
    report = dict(svc2._reconcile_report)
    state = instance_lock.state()
    instance_lock.release()
    try:
        dp.rmdir()
    except OSError:
        pass
    return (not any(got) and state != instance_lock.ACTIVE
            and "ok" != report.get("pending_deliveries"),
            f"ensure_active={got} state={state} report={report}")


probe("F1 an unreadable durable event file blocks promotion instead of "
      "reading as an empty queue", _f1)


def _f2():
    """A volume with no events at all is a clean empty store, not a fault: a
    fresh Railway volume has to be able to boot."""
    reset_all()
    seed_good_state()
    svc = fresh_service()
    got = svc.ensure_active(NOW)
    report = dict(svc._reconcile_report)
    instance_lock.release()
    return (got and report.get("pending_deliveries") in ("ok", "absent"),
            f"ensure_active={got} report={report}")


probe("F2 a fresh volume with no journal still promotes", _f2)


# ===========================================================================
print("\n--- G. an outage must not make its own warning a per-cycle DM (#8) ---")
# ===========================================================================
def _g1():
    reset_all()
    seed_good_state()
    dms = []
    svc = fresh_service(dms)
    svc.ensure_active(NOW)
    real_free = storage_io._disk_free
    storage_io._disk_free = lambda d: 0    # nothing can be persisted any more
    try:
        for _ in range(6):                 # six poll cycles of one outage
            svc._journal_down("index publish refused: disk_full")
    finally:
        storage_io._disk_free = real_free
        instance_lock.release()
    return len(dms) == 1, f"{len(dms)} owner DMs for one outage"


probe("G1 _journal_down DMs the owner once even when state.json cannot be "
      "written", _g1)


# ===========================================================================
print("\n--- H. the wedged-winner DM is about a DIFFERENT copy (#9) ---")
# ===========================================================================
def _h1():
    reset_all()
    seed_good_state()
    dms = []
    svc = fresh_service(dms)
    svc.ensure_active(NOW)                 # this copy now HOLDS the lock
    holding = instance_lock.holding()
    real_stale, real_poll = instance_lock.STALE_S, instance_lock.STANDBY_POLL_S
    instance_lock.STALE_S = 0              # reach the branch in one call
    instance_lock.STANDBY_POLL_S = 0
    try:
        svc._reconciled = False            # RECOVERING / BLOCKED with the lock
        svc.standby_wait(NOW)
        svc.standby_wait(NOW)
    finally:
        instance_lock.STALE_S = real_stale
        instance_lock.STANDBY_POLL_S = real_poll
        instance_lock.release()
    wedge = [d for d in dms if "wedged" in d or "has not renewed" in d]
    return (holding and not wedge,
            f"holding={holding} wedge DMs={wedge}")


probe("H1 a copy that holds the lock never DMs that the winner is wedged",
      _h1)


def _h2():
    """...and a real standby copy watching a real frozen holder still warns."""
    reset_all()
    seed_good_state()
    dms = []
    svc = fresh_service(dms)
    config.state_set(instance_lock.LEASE_KEY,
                     {"instance_id": "someone-else", "seq": 7,
                      "pid": 4242, "host": "other"})
    real_stale, real_poll = instance_lock.STALE_S, instance_lock.STANDBY_POLL_S
    instance_lock.STALE_S = 0
    instance_lock.STANDBY_POLL_S = 0
    try:
        svc.standby_wait(NOW)
    finally:
        instance_lock.STALE_S = real_stale
        instance_lock.STANDBY_POLL_S = real_poll
    wedge = [d for d in dms if "has not renewed" in d]
    return (not instance_lock.holding() and len(wedge) == 1,
            f"holding={instance_lock.holding()} DMs={dms}")


probe("H2 a standby copy still warns about a frozen holder", _h2)


# ===========================================================================
print("\n--- I. a partly delivered multi part card resumes (#13) ---")
# ===========================================================================
def _i1():
    reset_all()
    seed_good_state()
    svc = fresh_service()
    svc.ensure_active(NOW)
    part = "X" * (telegram.TG_MAX_CHARS - 10)
    body = "AAA" + part + "\nBBB" + part + "\nCCC" + part
    total = len(telegram.split_message(body))
    intent = an_intent(kind="exit", text=body, event_key="p2:stop")
    # exactly the state a 400 on part 2 leaves behind
    event_journal.record_attempt(intent.journal_id, 0)
    event_journal.record_result(intent.journal_id, 0, event_journal.FAILED,
                                error_class="http_400", parts_total=total,
                                parts_confirmed=1)
    live = event_journal.get(intent.journal_id)
    wire = with_wire()
    try:
        svc._deliver(live, [0])
    finally:
        restore_wire()
        instance_lock.release()
    first = wire.posts[0][1] if wire.posts else ""
    return (total == 3 and wire.posts and not first.startswith("AAA"),
            f"parts={total} resent {len(wire.posts)} starting "
            f"{first[:6]!r}")


probe("I1 a replay of a partly delivered card does not re-send a confirmed "
      "part", _i1)


def _i2():
    """A recipient with nothing confirmed still starts at part one."""
    reset_all()
    seed_good_state()
    svc = fresh_service()
    svc.ensure_active(NOW)
    part = "X" * (telegram.TG_MAX_CHARS - 10)
    body = "AAA" + part + "\nBBB" + part
    intent = an_intent(kind="exit", text=body, event_key="p3:stop")
    wire = with_wire()
    try:
        svc._deliver(intent, [1])
    finally:
        restore_wire()
        instance_lock.release()
    first = wire.posts[0][1] if wire.posts else ""
    return (len(wire.posts) == 2 and first.startswith("AAA"),
            f"posts={len(wire.posts)} first={first[:6]!r}")


probe("I2 a fresh recipient still gets every part", _i2)


# ===========================================================================
print("\n--- J. sweep_orphans touches only this module's staging names (#14) ---")
# ===========================================================================
def _j1():
    reset_all()
    d = _TMP / "sweep"
    d.mkdir(parents=True, exist_ok=True)
    mine = storage_io._tmp_for(d / "positions.json")
    mine.write_text("staged", encoding="utf-8")
    theirs = d / "my_important_export.tmp"
    theirs.write_text("someone else's file", encoding="utf-8")
    old = time_mod.time() - 7200
    for p in (mine, theirs):
        _bot_test_os.utime(p, (old, old))
    swept = storage_io.sweep_orphans(d, older_than_s=3600)
    return (not mine.exists() and theirs.exists() and swept == 1,
            f"swept={swept} mine={mine.exists()} theirs={theirs.exists()}")


probe("J1 a foreign *.tmp in the publish directory survives the sweep", _j1)


# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All batch A regressions passed.")
