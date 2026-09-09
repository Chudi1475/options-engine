"""W02 follow-up: the journal's index and its rebuild must not trust data they
have never checked.

The suite that shipped with event_journal.py (test_event_recovery.py) drives
the crash boundaries. It never drove the READS, and five defects lived in
exactly that gap. Each section below names the one it pins.

J1  _rebuild folded `for rec in (res.value or [])` and never looked at
    res.status. storage_io.read_jsonl returns value=None for a file the OS will
    not hand over, so an unreadable daily file contributed ZERO events, raised
    nothing, counted nothing, and then _save_index published that empty view
    over the good index. That is the missing-versus-unreadable collapse
    storage_io exists to prevent, committed inside the fallback whose own
    comment says "Never start from empty on a file we could not read".

J2  the same blind read in _next_seq. An append can still land in a file whose
    bytes the OS will not return, and a counter seeded from nothing restarts at
    1 and reuses the sequence numbers of lines that are really there.

J3  open_intents.json was rebuilt only when MISSING or UNREADABLE. A STALE
    index is perfectly readable. A crash between _append and _save_index leaves
    one, and so does a single denied index publish, and every read after it
    (unresolved, replay, health, orphans, and the event_key duplicate guard)
    answered from a book with a page torn out.

J4  _compact promises an unresolved intent is "never dropped, at any age" and
    the module docstring promises that losing the index loses no event, but the
    rebuild only walked back INDEX_RETAIN_DAYS + 1 days. Both promises were
    false for anything still owed after three days.

J5  record_attempt's docstring says the line written before the HTTP call "is
    the difference between an unknown delivery and an invisible one". Nothing
    ever read it back: a process that died inside the send left the recipient
    ATTEMPTED, retryable_recipients handed it straight to the next replay, and
    the card went out a second time.

J6  MAX_DELIVERY_RETRIES bounded only a FAILED recipient. An ATTEMPTED one was
    unresolvable at any attempt count, stayed retryable for ever and was
    re-broadcast on every restart.

No network, no Telegram, no scanner, no production storage. This suite talks to
event_journal and storage_io only, so it stays honest about which module the
defect is in.

Run:  python test_journal_integrity.py     (exit code 0 = all good)
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # set BEFORE any repo import
_bot_test_os.environ.setdefault("OWNER_CHAT_ID", "900001")
_bot_test_os.environ.setdefault("TELEGRAM_CHAT_IDS", "900001,900002,900003")
_bot_test_os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000:offline-test-token")

import sys
import tempfile
from datetime import timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# an isolated data dir BEFORE config is imported, so nothing here can read or
# write the real runtime state
_TMP = Path(tempfile.mkdtemp(prefix="kelbot_journal_"))
_bot_test_os.environ["DATA_DIR"] = str(_TMP)

import config          # noqa: E402

config.DATA_DIR = _TMP
config.STATE_FILE = _TMP / "state.json"
config.POSITIONS_FILE = _TMP / "positions.json"
config._LAST_GOOD = {}

import event_journal as ej   # noqa: E402
import storage_io            # noqa: E402

CHATS = ["900001", "900002", "900003"]
failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def probe(name, fn):
    """check() for something that may not exist yet: a missing attribute is a
    normal FAIL line instead of a traceback that would hide every later
    section, which matters when the whole point of the run is to watch a red
    list before the change and the same list green after it."""
    detail = ""
    try:
        ok = fn()
    except Exception as e:                                   # noqa: BLE001
        check(name, False, f"{type(e).__name__}: {e}")
        return False
    if isinstance(ok, tuple):
        ok, detail = ok
    check(name, bool(ok), str(detail))
    return bool(ok)


def reset():
    """Wipe every journal file between sections. Only inside the temp dir."""
    for p in sorted(_TMP.rglob("*"), reverse=True):
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
    config._LAST_GOOD = {}
    ej._reset_for_test()


check("isolated data dir, not production",
      str(config.DATA_DIR) == str(_TMP), f"{config.DATA_DIR} vs {_TMP}")


# ---------------------------------------------------------------------------
# fault injection: the two storage answers this module has to stop ignoring
# ---------------------------------------------------------------------------
_REAL_READ_JSONL = storage_io.read_jsonl
_REAL_WRITE_JSON = storage_io.write_json


def deny_read(names):
    """Make named files answer the way the OS answers when an indexer, an
    antivirus scanner or a mandatory range lock holds the handle: the file is
    THERE and its bytes are not available."""
    def fake(path):
        if Path(path).name in names:
            return storage_io.ReadResult(status="unreadable", value=None,
                                         error="[WinError 5] Access is denied",
                                         path=Path(path))
        return _REAL_READ_JSONL(path)
    storage_io.read_jsonl = fake


def deny_write(names):
    """The denied publish storage_io's own header says hits this machine about
    one run in ten, aimed at one file."""
    def fake(path, obj, **kw):
        if Path(path).name in names:
            return storage_io.WriteResult(False, "replace_denied", 5, 0,
                                          "[WinError 5] Access is denied",
                                          Path(path))
        return _REAL_WRITE_JSON(path, obj, **kw)
    storage_io.write_json = fake


def restore_io():
    storage_io.read_jsonl = _REAL_READ_JSONL
    storage_io.write_json = _REAL_WRITE_JSON


def commit(kind="entry", key=None, text="CARD", recipients=None, pos="p-1"):
    return ej.commit_intent(kind, candidate_id="cand-1",
                            decision_id=ej.new_decision_id(), position_id=pos,
                            payload={}, event_key=key,
                            recipients=recipients if recipients is not None
                            else CHATS, text=text)


# ===========================================================================
print("\n--- J1. an unreadable daily file is not an empty one ---")
# ===========================================================================


def _j1():
    """Two committed, undelivered alerts. The index is gone (a crash before the
    first publish, or a fresh volume) and the day's file cannot be read. The
    rebuild must not answer zero, and must not publish that zero."""
    reset()
    commit(text="J1 A", pos="j1-a")
    commit(text="J1 B", pos="j1-b")
    ej.index_path().unlink()
    ej._reset_for_test()
    deny_read({ej.daily_path().name})
    try:
        rebuilt = ej.rebuild_index()
        h = ej.health()
        txt = ej.report_text()
        published = ej.index_path().exists()
    finally:
        restore_io()
    return (not published and h.get("damaged_files") == 1
            and "could not be read" in txt.lower(),
            f"published={published} damaged={h.get('damaged_files')} "
            f"rebuilt={rebuilt} report={txt!r}")


probe("J1 a daily file the OS will not hand over is reported as damage and an "
      "empty view is never published over it", _j1)


def _j1b():
    """The other half: a torn last line, through the REAL reader. The
    parseable prefix is evidence and is folded; the damage is still counted."""
    reset()
    a = commit(text="J1B GOOD", pos="j1b-a")
    with ej.daily_path().open("a", encoding="utf-8") as f:
        f.write('{"kind": "intent", "journal_id": "j-torn"')   # died mid line
    ej.index_path().unlink()
    ej._reset_for_test()
    ids = {i.journal_id for i in ej.unresolved()}
    h = ej.health()
    return (ids == {a.journal_id} and h.get("damaged_files") == 1,
            f"ids={len(ids)} damaged={h.get('damaged_files')}")


probe("J1b a torn last line folds the parseable prefix and still counts the "
      "damage", _j1b)


def _j1c():
    """The way back. Refusing to publish is only safe if it ends by itself:
    the fault this is written for is an indexer or a scanner holding a handle
    for a moment, so the first journal write after it clears must republish,
    carrying the events the blind view could not see."""
    reset()
    a = commit(text="J1C A", pos="j1c-a")
    ej.index_path().unlink()
    ej._reset_for_test()
    deny_read({ej.daily_path().name})
    try:
        ej.unresolved()                     # loads a view it knows is blind
        blind = ej.health().get("damaged_files")
    finally:
        restore_io()
    b = commit(text="J1C B", pos="j1c-b")   # the first write after it clears
    on_index = (storage_io.read_json(ej.index_path()).value
                or {}).get("intents") or {}
    return (blind == 1 and ej.health().get("damaged_files") == 0
            and set(on_index) == {a.journal_id, b.journal_id},
            f"blind={blind} now={ej.health().get('damaged_files')} "
            f"index={sorted(on_index)}")


probe("J1c the view republishes by itself on the first write after the day "
      "becomes readable again", _j1c)


# ===========================================================================
print("\n--- J2. a sequence top that cannot be read is not zero ---")
# ===========================================================================


def _j2():
    """An append can still land in a file whose bytes the OS will not return.
    Seeding the counter from a read that gave nothing back reuses the numbers
    of the lines that are really in there, so the commit must fail closed."""
    reset()
    commit(text="J2 FIRST", pos="j2-a")
    ej._reset_for_test()                 # a restart, so the counter re-seeds
    deny_read({ej.daily_path().name})
    raised = ""
    try:
        try:
            out = commit(text="J2 SECOND", pos="j2-b")
            raised = f"returned {out.journal_id}"
        except ej.JournalUnavailable as e:
            raised = "raised"
        except Exception as e:                               # noqa: BLE001
            raised = f"{type(e).__name__}: {e}"
    finally:
        restore_io()
    return raised == "raised", f"commit_intent {raised}"


probe("J2 committing into a daily file whose sequence top cannot be read "
      "fails closed instead of restarting the counter", _j2)


# ===========================================================================
print("\n--- J3. a STALE index is not a trustworthy one ---")
# ===========================================================================


def _j3():
    """Commit one intent normally, then commit a second while the index
    publish is denied. The durable line lands, the view does not. That is what
    a crash between _append and _save_index leaves too."""
    reset()
    a = commit(text="J3 A", pos="j3-a")
    deny_write({ej.index_path().name})
    try:
        b = commit("exit", key="j3-row:stop", text="J3 B", pos="j3-b")
    finally:
        restore_io()
    lines = _REAL_READ_JSONL(ej.daily_path()).value or []
    on_file = len([r for r in lines if r.get("kind") == "intent"])
    on_index = len((storage_io.read_json(ej.index_path()).value
                    or {}).get("intents") or {})
    ej._reset_for_test()                 # a restart onto the stale view
    ids = {i.journal_id for i in ej.unresolved()}
    again = commit("exit", key="j3-row:stop", text="J3 B", pos="j3-b")
    return (on_file == 2 and on_index == 1
            and ids == {a.journal_id, b.journal_id}
            and again.duplicate and again.journal_id == b.journal_id,
            f"file={on_file} index={on_index} recovered={len(ids)} "
            f"duplicate={again.duplicate}")


probe("J3 a durable line the index never saw is folded forward on the next "
      "boot, and its event_key still stops a second commit", _j3)


def _j3b():
    """The health record must not report a clean queue while the view is
    missing an event the volume is holding."""
    reset()
    commit(text="J3B A", pos="j3b-a")
    deny_write({ej.index_path().name})
    try:
        commit(text="J3B B", pos="j3b-b")
    finally:
        restore_io()
    ej._reset_for_test()
    return (ej.health().get("queue_depth") == 2,
            f"queue_depth={ej.health().get('queue_depth')}")


probe("J3b health counts the intent the stale index dropped", _j3b)


# ===========================================================================
print("\n--- J4. an unresolved intent is never dropped, at any age ---")
# ===========================================================================


def _j4():
    """An exit leg still owed ten days later. _compact promises it is kept at
    any age and the docstring promises losing the index loses no event."""
    reset()
    old_day = (ej._et_now().date() - timedelta(days=10)).isoformat()
    real_today = ej._today
    ej._today = lambda: old_day
    try:
        old = commit("exit", key="j4-row:stop", text="J4 OLD", pos="j4-old")
        ej.record_result(old.journal_id, 0, ej.FAILED,
                         error_class="ConnectionError")
    finally:
        ej._today = real_today
    commit(text="J4 TODAY", pos="j4-today")          # a normal day since
    before = {i.journal_id for i in ej.unresolved()}
    ej.index_path().unlink()
    ej._reset_for_test()
    after = {i.journal_id for i in ej.unresolved()}
    again = commit("exit", key="j4-row:stop", text="J4 OLD", pos="j4-old")
    return (old.journal_id in before and after == before
            and again.duplicate and again.journal_id == old.journal_id,
            f"before={len(before)} after={len(after)} "
            f"old_kept={old.journal_id in after} duplicate={again.duplicate}")


probe("J4 losing the index does not drop an unresolved intent older than the "
      "retention window, and its event_key survives with it", _j4)


# ===========================================================================
print("\n--- J5. an attempt nobody heard back from is unknown, not a retry ---")
# ===========================================================================


def _j5():
    """The textbook uncertain acknowledgment: the attempt line is on disk and
    the process died inside the HTTP call. The request may have arrived."""
    reset()
    it = commit("sniper_entry", text="J5 CARD", pos="j5-pos",
                recipients=CHATS[:2])
    ej.record_attempt(it.journal_id, 0)
    ej.record_attempt(it.journal_id, 1)
    ej._reset_for_test()                 # the next boot, a new process
    asked = []

    def send(intent, indices):
        asked.append(list(indices))
        return []

    stats = ej.replay(send=send)
    unk = ej.unknown_deliveries()
    live = ej.get(it.journal_id)
    states = sorted(d.status for d in live.deliveries()) if live else []
    return (not asked and stats.get("unknown") == 2 and len(unk) == 2
            and states == [ej.UNKNOWN, ej.UNKNOWN],
            f"asked={asked} stats={stats} unknown={len(unk)} states={states}")


probe("J5 an attempt left behind by a dead process becomes an explicit "
      "unknown delivery and is never resent", _j5)


def _j5b():
    """The contrast, so the fix does not turn every local throw into an
    unknown: an attempt THIS process made, still under the cap, is retried."""
    reset()
    it = commit(text="J5B CARD", pos="j5b-pos", recipients=CHATS[:1])
    ej.record_attempt(it.journal_id, 0)
    asked = []

    def send(intent, indices):
        asked.append(list(indices))
        ej.record_result(intent.journal_id, 0, ej.CONFIRMED,
                         provider_message_id=7)
        return [{"recipient_index": 0}]

    ej.replay(send=send)
    live = ej.get(it.journal_id)
    return (asked == [[0]] and live.deliveries()[0].status == ej.CONFIRMED
            and live.resolved,
            f"asked={asked} status={live.deliveries()[0].status} "
            f"resolved={live.resolved}")


probe("J5b an unfinished attempt from THIS process is still retried once", _j5b)


# ===========================================================================
print("\n--- J6. the retry bound covers ATTEMPTED, not only FAILED ---")
# ===========================================================================


def _j6():
    """Twelve replay passes with a send that records the attempt and returns
    nothing, which is byte for byte what scanner._deliver returns when
    telegram.send_detailed raises. Under Railway's restart policy this runs on
    every promotion into ACTIVE."""
    reset()
    it = commit(text="J6 CARD", pos="j6-pos", recipients=CHATS[:1])

    def send(intent, indices):
        for i in indices:
            ej.record_attempt(intent.journal_id, i)
        return []

    for _ in range(12):
        ej.replay(send=send)
    live = ej.get(it.journal_id)
    d = live.deliveries()[0]
    reported = len(ej.unknown_deliveries()) + len(ej.orphans())
    return (d.attempts <= ej.MAX_DELIVERY_RETRIES and d.resolved
            and reported >= 1 and not live.retryable_recipients(),
            f"status={d.status} attempts={d.attempts} resolved={d.resolved} "
            f"reported={reported} retryable={live.retryable_recipients()}")


probe("J6 an ATTEMPTED recipient stops at MAX_DELIVERY_RETRIES and is "
      "reported instead of retried for ever", _j6)


def _j6b():
    """The FAILED contrast still behaves exactly as it did: bounded at the cap
    and named as an exhausted delivery."""
    reset()
    it = commit(text="J6B CARD", pos="j6b-pos", recipients=CHATS[:1])

    def send(intent, indices):
        for i in indices:
            ej.record_attempt(intent.journal_id, i)
            ej.record_result(intent.journal_id, i, ej.FAILED,
                             error_class="ConnectionError")
        return [{"recipient_index": 0}]

    for _ in range(12):
        ej.replay(send=send)
    live = ej.get(it.journal_id)
    d = live.deliveries()[0]
    classes = {o.get("orphan_class") for o in ej.orphans()}
    return (d.attempts == ej.MAX_DELIVERY_RETRIES and d.resolved
            and "delivery_exhausted" in classes,
            f"attempts={d.attempts} resolved={d.resolved} classes={classes}")


probe("J6b a FAILED recipient still stops at the cap and is reported as an "
      "exhausted delivery", _j6b)


# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All journal integrity checks passed.")
