"""Regressions for the defects found in the 2026-09-08 independent review.

Each check below reproduced a real defect before its fix. The reproductions are
kept as tests so the same mistake cannot come back.

P01  Stretch targets took their sign from a direction variable that had already
     been superseded by the sniper plan, so a BUY ticket printed 1R and 2R
     BELOW entry: levels that are losses at the moment the card is sent.

P02  The forward ledger deduplicated on (symbol, direction, date), so the first
     candidate of the day won forever. A 9:35 near miss therefore erased the
     9:50 alert that actually fired, which is why live alerts had no matching
     forward observation at their real entry. The rest of that repair, the
     candidate id being returned and carried onto the alert and the position,
     and the ledger surviving a concurrent append, lives in
     test_ledger_integrity.py.

P06  /brain ran its forced billing probe inline on the Telegram command
     dispatcher, the same thread that walks open positions for stops between
     cycles, so typing /brain during a billing hold delayed the next
     monitoring pass by however long the API took to refuse. It now answers
     at once and a worker texts the verdict back to the same chat.

No network, no Telegram, no model API, no production storage.

Run:  python test_review_regressions.py     (exit code 0 = all good)
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.

import json
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# an isolated data dir BEFORE config is imported, so nothing here can read or
# write the real runtime state
_TMP = tempfile.mkdtemp(prefix="kelbot_test_")
_bot_test_os.environ["DATA_DIR"] = _TMP

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# --------------------------------------------------------------------------
# P01: stretch targets must lie on the correct side of entry
# --------------------------------------------------------------------------
import market_tools

st = market_tools.stretch_targets

# the exact reproduction from the review: a BUY ticket while the earlier,
# superseded read said SELL
buy = st(100.0, 99.0, "BUY")
check("P01: a BUY ticket puts 1R and 2R ABOVE entry",
      buy == {"target_1r": 101.0, "target_2r": 102.0}, str(buy))

sell = st(100.0, 101.0, "SELL")
check("P01: a SELL ticket puts 1R and 2R BELOW entry",
      sell == {"target_1r": 99.0, "target_2r": 98.0}, str(sell))

check("P01: targets never sit on the losing side of entry",
      buy["target_1r"] > 100.0 and buy["target_2r"] > buy["target_1r"]
      and sell["target_1r"] < 100.0 and sell["target_2r"] < sell["target_1r"])

# the ticket and the direction disagreeing is exactly the confusion that
# produced the defect, so refuse rather than print something plausible
check("P01: a BUY whose stop sits above entry is refused",
      st(100.0, 101.0, "BUY") is None)
check("P01: a SELL whose stop sits below entry is refused",
      st(100.0, 99.0, "SELL") is None)
check("P01: a zero-distance stop is refused",
      st(100.0, 100.0, "BUY") is None)
for bad in (None, "", "LONG", "buy ", 5):
    check(f"P01: direction {bad!r} is refused unless it is BUY or SELL",
          st(100.0, 99.0, bad) is not None if str(bad).strip().upper() == "BUY"
          else st(100.0, 99.0, bad) is None)
check("P01: unusable numbers return None instead of raising",
      st(None, 99.0, "BUY") is None and st("x", 99.0, "BUY") is None
      and st(100.0, None, "BUY") is None)

# lowercase is accepted, because the ticket side is written in several places
check("P01: direction matching is case insensitive",
      st(100.0, 99.0, "buy") == {"target_1r": 101.0, "target_2r": 102.0})

# and the call site must hand it the SNIPER direction, not the superseded one
src = Path(__file__).with_name("market_tools.py").read_text(encoding="utf-8-sig")
call = src[src.index('ticket["direction"] = sdir'):][:400]
check("P01: the call site passes the sniper direction, not the outer one",
      "stretch_targets(ticket.get(\"entry\")" in call and "sdir)" in call, call[:200])
check("P01: the ticket now carries its own direction for downstream readers",
      'ticket["direction"] = sdir' in src)
check("P01: the old sign-from-outer-direction expression is gone",
      '_sign = 1 if direction == "BUY" else -1' not in src)

# --------------------------------------------------------------------------
# P02: an earlier rejected candidate must not erase a later real alert
# --------------------------------------------------------------------------
import config
import forward_ledger

check("P02: the test is using an isolated data dir, not production",
      str(config.DATA_DIR) == _TMP, f"{config.DATA_DIR} vs {_TMP}")

_LEDGER = Path(_TMP) / "sniper_forward.jsonl"


def _rows():
    if not _LEDGER.exists():
        return []
    return [json.loads(l) for l in
            _LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]


forward_ledger.LEDGER = _LEDGER
if _LEDGER.exists():
    _LEDGER.unlink()

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _cand(hh, mm, passes, entry, stop, reasons=(), gap=1.6):
    return forward_ledger.record_candidate(
        symbol="EURUSD=X", direction="BUY", price=entry, atr=0.0012,
        ticket={"entry": entry, "stop": stop},
        conf={"grade": "A" if passes else "B"},
        passes=passes, reasons=list(reasons), gap_atr=gap, hour_et=hh,
        now_et=datetime(2026, 9, 8, hh, mm, tzinfo=ET))


# 09:35 near miss, then the 09:50 alert that actually fired
_cand(9, 35, False, 1.1000, 1.0990, ["gap 0.8 ATR, under the 1.0 floor"], gap=0.8)
_cand(9, 50, True, 1.1020, 1.1008)

rows = _rows()
passing = [r for r in rows if r.get("passes")]
rejects = [r for r in rows if not r.get("passes")]

check("P02: the 09:50 passing candidate survives the earlier 09:35 reject",
      len(passing) == 1 and passing[0].get("time_et") == "09:50:00",
      f"rows={[(r.get('time_et'), r.get('passes')) for r in rows]}")
check("P02: the deliberate 09:35 rejection is still retained",
      len(rejects) == 1 and rejects[0].get("time_et") == "09:35:00",
      f"rows={[(r.get('time_et'), r.get('passes')) for r in rows]}")
check("P02: both events are recorded, not collapsed into one",
      len(rows) == 2, f"got {len(rows)} rows")

# a retry of the SAME event must not become a second trade
before = len(_rows())
_cand(9, 50, True, 1.1020, 1.1008)
check("P02: re-recording the same event does not create a duplicate",
      len(_rows()) == before, f"{before} -> {len(_rows())}")

# A DIFFERENT passing look later the same day is a real observation and is
# RECORDED. The old rule dropped it on the floor: not demoted, not kept as a
# reject, no trace at all, so the ledger could not say what the bot had seen.
# One trade per symbol per day is an ENTRY rule and it still holds, but it
# lives in the scanner and in sniper_book, not in the recorder. Here it shows
# up as exactly one observation carrying the selected flag.
_second = _cand(11, 15, True, 1.1050, 1.1038)
check("P02: a second passing observation the same day is still recorded",
      len([r for r in _rows() if r.get("passes")]) == 2,
      f"passing={[r.get('time_et') for r in _rows() if r.get('passes')]}")
check("P02: recording alone selects nothing for broadcast",
      not any(r.get("selected") for r in _rows()),
      f"selected={[r.get('time_et') for r in _rows() if r.get('selected')]}")
forward_ledger.mark_selected(_second, position_id="pos-test")
check("P02: exactly one observation is marked as the delivered entry",
      len([r for r in _rows() if r.get("selected")]) == 1,
      f"selected={[r.get('time_et') for r in _rows() if r.get('selected')]}")

# but a later REJECT is still recorded, because rejects are the denominator
_cand(13, 5, False, 1.1070, 1.1058, ["day efficiency 0.9, one-way tape"])
check("P02: later rejects keep accumulating as opportunity evidence",
      len([r for r in _rows() if not r.get("passes")]) == 2,
      f"rejects={[r.get('time_et') for r in _rows() if not r.get('passes')]}")

# every recorded event needs a stable identity to link alert -> observation
ids = [r.get("event_id") for r in _rows()]
check("P02: every row carries a unique event id",
      all(ids) and len(set(ids)) == len(ids), str(ids))

# --------------------------------------------------------------------------
# P03: deterministic grading must not depend on paid-AI permission
# --------------------------------------------------------------------------
# Capping spend must never cost measurement. The grader used to live inside
# learn.run, so LEARN_ENABLED=false silently stopped the free evidence
# collection that is supposed to settle the 0.4R question.
import scanner

_saved_learn = _bot_test_os.environ.get("LEARN_ENABLED")
_saved_mode = _bot_test_os.environ.get("API_MODE")
_saved_fill = forward_ledger.fill_outcomes
try:
    _bot_test_os.environ["LEARN_ENABLED"] = "false"
    _bot_test_os.environ["API_MODE"] = "off"
    check("P03: the fixture really has the paid review switched off",
          not config.learn_enabled() and not config.api_allows("scheduled")[0])

    calls = []
    # the grader returns a COUNTS dict now, not a bare int, and the scheduler
    # claims the day off res["complete"]. A stub that still returned 3 would
    # blow up on .get inside the job, get swallowed, and show up here as the
    # unrelated-looking double-grade check failing.
    _COMPLETE = {"eligible": 1, "graded": 1, "unresolved": 0,
                 "missing_data": 0, "failed_writes": 0,
                 "retryable_failures": 0, "permanent_failures": 0,
                 "read_ok": True, "complete": True}
    forward_ledger.fill_outcomes = lambda *a, **k: (calls.append(1),
                                                    dict(_COMPLETE))[1]

    svc = scanner.Service.__new__(scanner.Service)
    svc.dry = False
    svc.MAX_JOB_ATTEMPTS = 2

    now = datetime(2026, 9, 8, 22, 30, tzinfo=ET)
    # clear the whole grading state, not just the done key: a leftover park or
    # retry stamp from another section would suppress the call and this would
    # read as "grading is off when the paid review is off"
    for _k in ("forward_graded", "forward_grade_attention",
               "forward_grade_last"):
        config.state_set(_k, None)
    scanner.Service.maybe_grade_forward(svc, now)
    check("P03: grading runs with the paid review and all AI spend disabled",
          len(calls) == 1, f"calls={len(calls)}")

    # a restart on the same session must not grade twice
    scanner.Service.maybe_grade_forward(svc, now)
    check("P03: a second pass on the same session does not double-grade",
          len(calls) == 1, f"calls={len(calls)}")

    # and it must be reachable independently of maybe_learn
    calls.clear()
    scanner.Service.maybe_learn(svc, now)
    check("P03: maybe_learn still declines to spend when the switch is off",
          len(calls) == 0)

    check("P03: the config docstring no longer claims the ledger is unaffected",
          "used to be affected" in (config.learn_enabled.__doc__ or ""),
          (config.learn_enabled.__doc__ or "")[:120])
    check("P03: the daemon calls the grading job",
          "self.maybe_grade_forward(now)" in
          Path(__file__).with_name("scanner.py").read_text(encoding="utf-8-sig"))
finally:
    forward_ledger.fill_outcomes = _saved_fill
    for k, v in (("LEARN_ENABLED", _saved_learn), ("API_MODE", _saved_mode)):
        if v is None:
            _bot_test_os.environ.pop(k, None)
        else:
            _bot_test_os.environ[k] = v

# --------------------------------------------------------------------------
# P05: an imported review must describe a trade that really happened
# --------------------------------------------------------------------------
import learn
import positions as poslib

_tmp5 = Path(tempfile.mkdtemp(prefix="kelbot_p05_"))
learn.REVIEWS_FILE = _tmp5 / "trade_reviews.jsonl"
learn.LESSONS_LOG = _tmp5 / "lessons.jsonl"
learn.LESSONS_DIGEST = _tmp5 / "lessons_digest.md"

_pos = poslib.Position(id="REAL-1", date="2026-09-04", time_et="09:50:01",
                       ticker="SPX", direction="call", right="C", strike=7745.0,
                       expiry="2026-09-04", entry_mid=7.85, entry_source="quote")
_pos.state, _pos.final_pnl_pct, _pos.paper = "closed", -90.83, False
_book = poslib.PositionBook()
_book.positions = [_pos]
learn.PositionBook = lambda *a, **k: _book

_BASE = dict(id="REAL-1", date="2026-09-04", ticker="SPX", direction="call",
             strike=7745.0, paper=False, final_pnl_pct=-90.83, verdict="WRONG",
             why="stopped out", cause="setup", cause_detail="", lesson="a lesson")


def _imp(row):
    f = _tmp5 / "in.json"
    f.write_text(json.dumps({"reviews": [row]}), encoding="utf-8")
    return learn.import_reviews(str(f))


check("P05: a fabricated trade id is refused",
      _imp(dict(_BASE, id="MADE-UP")) == 0)
check("P05: a cause the bot does not use is refused",
      _imp(dict(_BASE, cause="vibes")) == 0)
check("P05: the string 'false' is not accepted as a boolean",
      _imp(dict(_BASE, paper="maybe")) == 0)
check("P05: a paper flag contradicting the tracked position is refused",
      _imp(dict(_BASE, paper=True)) == 0)
check("P05: nothing was written by any refused import",
      not learn.REVIEWS_FILE.exists()
      or not learn.REVIEWS_FILE.read_text(encoding="utf-8").strip())

# a batch is all-or-nothing: one bad row must not let the good ones land
_f = _tmp5 / "batch.json"
_f.write_text(json.dumps({"reviews": [dict(_BASE), dict(_BASE, id="NOPE")]}),
              encoding="utf-8")
check("P05: one bad row refuses the whole batch",
      learn.import_reviews(str(_f)) == 0
      and (not learn.REVIEWS_FILE.exists()
           or not learn.REVIEWS_FILE.read_text(encoding="utf-8").strip()))

# the valid row commits, and the immutable facts come from the POSITION
check("P05: a valid row is accepted", _imp(dict(_BASE, final_pnl_pct=999.0)) == 1)
_rows5 = [json.loads(l) for l in
          learn.REVIEWS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
check("P05: the committed P&L comes from the position, not the file",
      len(_rows5) == 1 and _rows5[0]["final_pnl_pct"] == -90.83,
      str(_rows5))
check("P05: the row carries import provenance",
      _rows5[0].get("imported_from") and _rows5[0].get("imported_at"))
check("P05: an already-reviewed id is never overwritten", _imp(dict(_BASE)) == 0)
check("P05: the digest is rebuilt by the import", learn.LESSONS_DIGEST.exists())

_before5 = len(learn.LESSONS_LOG.read_text(encoding="utf-8").splitlines())
learn._derive_lessons_for(learn.REVIEWS_FILE)
_after5 = len(learn.LESSONS_LOG.read_text(encoding="utf-8").splitlines())
check("P05: the lesson repair path is idempotent",
      _before5 == _after5, f"{_before5} -> {_after5}")

# the recovery case: a review committed but its lesson lost
learn.LESSONS_LOG.write_text("", encoding="utf-8")
_made = learn._derive_lessons_for(learn.REVIEWS_FILE)
check("P05: an interrupted import repairs its missing lesson on a re-run",
      _made == 1, f"derived {_made}")

# --------------------------------------------------------------------------
# P06: a billing probe must never hold up the loop that watches stops
# --------------------------------------------------------------------------
import threading
import time as _time

import assistant

_saved_probe = assistant.probe_billing
_saved_hold = assistant.billing_hold
_saved_en = assistant.enabled
try:
    assistant.enabled = lambda: True
    assistant.billing_hold = lambda: {"since": 1, "last_probe": 1,
                                      "notified": True}
    started = threading.Event()

    def _slow_probe(force=False):
        started.set()
        _time.sleep(2.0)          # stands in for a stalled API
        return False

    assistant.probe_billing = _slow_probe

    t0 = _time.monotonic()
    assistant.probe_billing_async()
    elapsed = _time.monotonic() - t0
    check("P06: starting a probe returns immediately, it does not block",
          elapsed < 0.5, f"took {elapsed:.2f}s")
    check("P06: the probe really did start", started.wait(timeout=2.0))

    # concurrent callers must create at most one probe
    check("P06: a second caller does not queue another probe",
          assistant.probe_billing_async() is False)
    check("P06: the daemon no longer calls the blocking probe inline",
          "assistant.probe_billing()" not in
          Path(__file__).with_name("scanner.py").read_text(encoding="utf-8-sig"))
finally:
    assistant.probe_billing = _saved_probe
    assistant.billing_hold = _saved_hold
    assistant.enabled = _saved_en

# --------------------------------------------------------------------------
# P06 (command path): /brain must answer without blocking the dispatcher
# --------------------------------------------------------------------------
# run_command is called from handle_commands, on the same loop thread that
# walks open positions for +25%/give-back/STOP between cycles. The probe
# /brain forces is up to three attempts at a 15 second timeout plus backoff,
# and that is one scenario, not a ceiling: requests' timeout bounds each
# socket operation, not the wall clock. So the dispatcher has to come back at
# once AND the fresh verdict still has to land in the chat that asked.
import telegram as _tg

_saved_probe = assistant.probe_billing
_saved_hold = assistant.billing_hold
_saved_en = assistant.enabled
_saved_send = _tg.send_to
_saved_isowner = _tg.is_owner
_saved_primary = _tg.primary_owner_id
try:
    # the block above left a stubbed 2 second probe holding the in-flight
    # lock. wait it out so this block only ever times its own work.
    for _ in range(500):
        if assistant._PROBE_INFLIGHT.acquire(blocking=False):
            assistant._PROBE_INFLIGHT.release()
            break
        _time.sleep(0.02)

    from scanner import Service

    svc = Service.__new__(Service)   # no network-y __init__, same as test_adduser

    assistant.enabled = lambda: True
    assistant.billing_hold = lambda: {"since": 1, "last_probe": 1,
                                      "notified": True}
    _tg.is_owner = lambda cid: True
    _tg.primary_owner_id = lambda: "111"

    _sent = []
    _got_one = threading.Event()

    def _capture(cid, text):
        _sent.append((str(cid), text))
        _got_one.set()
        return None

    _tg.send_to = _capture

    _calls = []
    _release = threading.Event()

    def _stalled_probe(force=False):
        _calls.append(bool(force))
        _release.wait(timeout=5.0)   # stands in for a stalled API
        return False

    assistant.probe_billing = _stalled_probe

    t0 = _time.monotonic()
    first = svc.run_command("/brain", "", chat_id="111")
    elapsed = _time.monotonic() - t0
    check("P06: /brain returns to the dispatcher immediately",
          elapsed < 0.5, f"took {elapsed:.2f}s")
    check("P06: /brain says a check is running",
          "Checking the API right now" in (first or ""), repr(first))

    for _ in range(500):             # let the worker reach the probe
        if _calls:
            break
        _time.sleep(0.02)
    check("P06: /brain still forces a real check", _calls == [True], str(_calls))

    second = svc.run_command("/brain", "", chat_id="222")
    check("P06: a second /brain starts no second probe",
          _calls == [True], str(_calls))
    check("P06: the second /brain is told the answer is coming",
          "Already checking" in (second or ""), repr(second))

    _release.set()                   # the API finally answers
    check("P06: the verdict is texted after the command already returned",
          _got_one.wait(timeout=10))
    for _ in range(500):             # both waiters, one verdict
        if len(_sent) >= 2:
            break
        _time.sleep(0.02)
    check("P06: both /brain callers get the same fresh verdict",
          {c for c, _ in _sent} == {"111", "222"}, str(_sent))
    check("P06: the verdict reads as a check made just now",
          bool(_sent) and all("Checked just now" in t for _, t in _sent),
          str(_sent))
    check("P06: the dispatcher no longer runs the forced probe inline",
          "probe_billing(force=True)" not in
          Path(__file__).with_name("scanner.py").read_text(encoding="utf-8-sig"))
finally:
    assistant.probe_billing = _saved_probe
    assistant.billing_hold = _saved_hold
    assistant.enabled = _saved_en
    _tg.send_to = _saved_send
    _tg.is_owner = _saved_isowner
    _tg.primary_owner_id = _saved_primary

# --------------------------------------------------------------------------
# P06 (start failure): a worker that never started must not kill /brain
# --------------------------------------------------------------------------
# _FORCED_RUNNING is latched BEFORE the worker exists and only the worker's
# finally clears it. On a container already running the sniper-watch,
# news-watch, flush-pending, brain-reply and billing-probe workers,
# Thread.start() can raise "can't start new thread". If that escapes, the flag
# stays set for the life of the process, the caller's callback stays parked,
# and every later /brain sees a probe in flight and answers nothing. The
# automatic path claims the same way with the in-flight lock instead of a flag.
_saved_probe = assistant.probe_billing
_saved_hold = assistant.billing_hold
_saved_en = assistant.enabled
_saved_threading = assistant.threading
_saved_send = _tg.send_to
_saved_primary = _tg.primary_owner_id


class _NoNewThreads:
    """threading with Thread() refusing, the way a maxed-out container does."""

    def Thread(self, *a, **kw):
        raise RuntimeError("can't start new thread")

    def __getattr__(self, name):
        return getattr(threading, name)


try:
    assistant.enabled = lambda: True
    assistant.billing_hold = lambda: {"since": 1, "last_probe": 1,
                                      "notified": True}
    assistant.probe_billing = lambda force=False: False
    _notes = []
    _tg.send_to = lambda cid, text: _notes.append((str(cid), text))
    _tg.primary_owner_id = lambda: "111"

    _verdicts = []
    assistant.threading = _NoNewThreads()
    _started = assistant.probe_billing_forced(on_done=_verdicts.append)

    check("P06: a /brain whose worker cannot start does not report a probe",
          _started is False, repr(_started))
    check("P06: a worker that never started does not latch /brain forever",
          assistant._FORCED_RUNNING is False)
    check("P06: no caller is left parked on a worker that never started",
          assistant._FORCED_WAITERS == [] and len(_verdicts) == 1,
          f"{assistant._FORCED_WAITERS} {_verdicts}")
    check("P06: the chat is told the check could not start",
          any("could not start" in t.lower() for _, t in _notes), str(_notes))

    check("P06: an automatic probe whose worker cannot start returns False",
          assistant.probe_billing_async() is False)
    _free = assistant._PROBE_INFLIGHT.acquire(blocking=False)
    if _free:
        assistant._PROBE_INFLIGHT.release()
    check("P06: a probe that never started does not hold the in-flight lock",
          _free)

    # threads are available again: the next /brain has to be a real check
    assistant.threading = _saved_threading
    _verdicts2 = []
    _ran = threading.Event()

    def _quick_probe(force=False):
        _ran.set()
        return True

    assistant.probe_billing = _quick_probe
    check("P06: the next /brain after a failed start still probes",
          assistant.probe_billing_forced(on_done=_verdicts2.append) is True)
    check("P06: that probe reached the API", _ran.wait(timeout=5))
    for _ in range(500):
        if _verdicts2:
            break
        _time.sleep(0.02)
    check("P06: and its verdict reaches the caller", _verdicts2 == [True],
          str(_verdicts2))
finally:
    assistant.probe_billing = _saved_probe
    assistant.billing_hold = _saved_hold
    assistant.enabled = _saved_en
    assistant.threading = _saved_threading
    _tg.send_to = _saved_send
    _tg.primary_owner_id = _saved_primary

# ---------------------------------------------------------------------------
# W-conflict: a test may not text a real person, and may not take their mail.
#
# The wire guard covered every SEND path but not the RECEIVE path. get_messages
# checked only the lease, so an offline suite run polled the LIVE token: the
# cloud bot took a 409 and warned the owner about a stray instance that was the
# test suite, and because a poll acknowledges updates through the offset, a
# command typed during a local run could be swallowed and never answered by the
# copy on duty. Observed in production on 2026-09-08.
print()
print("--- getUpdates is inside the test-mode wire guard ---")

_polled = []
_saved_session_get = _tg._session.get


def _tripwire(url, *a, **k):
    _polled.append(url)
    raise AssertionError("a test reached the real getUpdates endpoint")


try:
    _tg._session.get = _tripwire
    _tg.set_standby(False)   # not standing by, so the lease is NOT what is
                             # protecting us here: test_mode must be
    check("wire: the lease is not what is holding this back",
          _tg.standby()[0] is False)
    import os as _os
    check("wire: BOT_TEST_MODE is on for this suite",
          bool(_os.environ.get("BOT_TEST_MODE")))
    _items, _off = _tg.get_messages(timeout=0)
    check("wire: get_messages makes no network call in test mode", not _polled,
          str(_polled))
    check("wire: it returns no messages", _items == [])
    check("wire: and it does not move the offset",
          _off == int(config.state_get("tg_offset", 0)))
    _tg.print_chat_ids()
    check("wire: print_chat_ids does not poll either in test mode", not _polled,
          str(_polled))
finally:
    _tg._session.get = _saved_session_get


print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all good")
