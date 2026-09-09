"""Regressions for R03: the grading scheduler wrote completion for work that
never happened.

The forward ledger is the bot's only walk-forward evidence. Grading it is free
and deterministic, so the one thing that must never happen is the scheduler
recording a day as graded when nothing was graded. It did exactly that.

R03a  fill_outcomes returned a bare int. Five different failure paths returned
      0, and a fully graded day also returned 0, so the caller could not tell
      "nothing left to do" from "the data provider was down". The scheduler
      wrote forward_graded on every one of them, and its own early-return
      guard then made the retry it was designed to allow unreachable forever.

R03b  A first download that fails and a second that succeeds must both be
      visible to the scheduler. Before the repair the first tick claimed the
      day and the second tick never ran.

R03c  A first write that fails and a second that persists, same story. The
      graded count was taken from the in-memory mutation, so a lost write was
      reported as a graded day while every row on disk stayed ungraded.

R03d  Exhausted retries were recorded with the SAME state a fully graded day
      writes. A day that graded zero rows was byte-identical in state.json to
      a complete one. Exhaustion is now parked as needs-attention instead, and
      the job stops re-attempting once parked so removing the success write
      cannot turn it into a 45 second spin all evening.

R03e  The daily counts have to reconcile, and reconcile the same way whatever
      the AI switches say. A second, unaudited grading call inside learn.run
      made the day's counts move with LEARN_ENABLED even though the grading
      itself no longer did.

Then the four lens review of R03 found three more, and they are the sections
at the bottom of this file:

G1    One old row parked grading forever. A row whose day has aged past the
      window the download can reach can never produce bars, that was counted
      as missing_data, missing_data is RETRYABLE, so complete was False on
      every future pass and every later session parked as needs-attention.
      "The source can no longer reach this day" is permanent, and the age
      boundary is derived from the SAME number the download period is built
      from so the two can never drift apart.

G2    Grading moved to its own key at the close, which is right, but 16:12 is
      not final for a row that is still trading. Two symbols in the roster are
      24h FX and the download asks for prepost, and fill_outcomes only ever
      walks rows whose outcome is still None, so whatever the close-time pass
      wrote was frozen. A winner that resolves in the evening was dropped
      while a loser that stopped at lunch was kept, which biases the recorded
      win rate down. Finality is now decided per row, and a row that is not
      final yet is left for the next pass. Late beats wrong.

G3    The scoreboard decided a day's era from the mere PRESENCE of a selected
      key on any row of that date. On the deploy day every date holds both
      eras, so the first new row (selected False) flipped the whole date into
      selected-only mode and evicted that day's real, graded, broadcast trade.
      The era is a property of a ROW, not of a date.

Bookkeeping only. No strategy surface is touched: no sniper constant, no exit
threshold, no allow-list, no entry window, no symbol roster. _walk_outcome is
not modified, only the accounting around it.

No network, no Telegram, no model API, no production storage. yfinance is
stubbed in sys.modules and the ledger lives in a temp dir.

Run:  python test_grading_integrity.py     (exit code 0 = all good)
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.

import json
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# an isolated data dir BEFORE config is imported, so nothing here can read or
# write the real runtime state
_TMP = tempfile.mkdtemp(prefix="kelbot_grade_")
_bot_test_os.environ["DATA_DIR"] = _TMP

import config          # noqa: E402
import forward_ledger  # noqa: E402

ET = ZoneInfo("America/New_York")
REPO = Path(__file__).parent
DAY = "2026-09-08"                 # the signal day every fixture row is on
# The grading clock, handed in on every pass. It has to be a parameter: a row
# is only gradable once its own day has closed (see G2), so a fixture that
# borrowed the wall clock would grade one way at 17:00 and another way at
# 00:05, and would rot the moment the calendar moved past DAY. NOW is the
# session AFTER the signal day, which is when a real deferred row is picked
# up, so every pre-existing expectation below still reads as written.
NOW = datetime(2026, 9, 9, 17, 0, tzinfo=ET)
CLOSE = datetime(2026, 9, 8, 17, 0, tzinfo=ET)   # the signal day, still open

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def guard(name, fn, detail=""):
    """Run a check that can RAISE on the un-repaired code (a bare int has no
    .get), and record the raise as the failure it is instead of aborting."""
    try:
        ok, extra = fn()
    except Exception as e:
        check(name, False, f"{type(e).__name__}: {e}")
        return
    check(name, ok, extra or detail)


_LEDGER = Path(_TMP) / "sniper_forward.jsonl"
forward_ledger.LEDGER = _LEDGER

check("isolated data dir, not production",
      str(config.DATA_DIR) == _TMP, f"{config.DATA_DIR} vs {_TMP}")


def reset():
    for p in Path(_TMP).glob("sniper_*"):
        try:
            if p.is_dir():
                for kid in p.glob("*"):
                    kid.unlink()
                p.rmdir()
            else:
                p.unlink()
        except OSError:
            pass


def rows():
    if not _LEDGER.exists():
        return []
    return [json.loads(ln) for ln in
            _LEDGER.read_text(encoding="utf-8").splitlines() if ln.strip()]


def ungraded():
    return [r for r in rows() if r.get("outcome") is None]


def cand(hh, mm, entry, stop, symbol="EURUSD=X", direction="BUY", ss=0,
         day=DAY):
    y, m, d = (int(x) for x in day.split("-"))
    return forward_ledger.record_candidate(
        symbol=symbol, direction=direction, price=entry, atr=0.0012,
        ticket={"entry": entry, "stop": stop},
        conf={"grade": "A", "score": 9},
        passes=True, reasons=[], gap_atr=1.6, hour_et=hh,
        now_et=datetime(y, m, d, hh, mm, ss, tzinfo=ET))


def grade(now=None):
    """One grading pass on a controlled clock.

    The clock is a parameter because "can this outcome still change" is a
    question about the time. The un-repaired grader has no notion of it: 16:12
    and midnight look identical to it, which is exactly the G2 defect, so on
    the un-repaired code this call raises."""
    return forward_ledger.fill_outcomes(now_et=now or NOW)


# --------------------------------------------------------------------------
# yfinance stub: no network, ever
# --------------------------------------------------------------------------
import pandas as pd  # noqa: E402


def bars(day=DAY, start_hh=10, n=12, hi=1.2000, lo=1.0000):
    idx = pd.date_range(f"{day} {start_hh:02d}:00", periods=n, freq="5min",
                        tz="America/New_York")
    return pd.DataFrame({"Open": [1.1] * n, "High": [hi] * n,
                         "Low": [lo] * n, "Close": [1.1] * n}, index=idx)


def fx_day(day=DAY):
    """A 24 hour FX day for entry 1.1020 / stop 1.1008 (0.4R = 1.10248).

    Quiet between the stop and the first target all the way through the equity
    close, then the whole ladder taken out at 20:00 ET. Nothing about this row
    is decided at 16:12, and everything about it is decided by 20:05."""
    idx = pd.date_range(f"{day} 10:00", f"{day} 21:00", freq="5min",
                        tz="America/New_York")
    hi = [1.1050 if t.hour >= 20 else 1.1022 for t in idx]
    return pd.DataFrame({"Open": [1.1020] * len(idx), "High": hi,
                         "Low": [1.1015] * len(idx),
                         "Close": [1.1020] * len(idx)}, index=idx)


class FakeYF(types.ModuleType):
    """Stands in for the yfinance module inside fill_outcomes. `plan` maps a
    symbol to a frame, to an Exception instance to raise, or to None for the
    empty-response case. `default` covers anything not named.

    `as_of` is what the provider can SEE yet: bars stamped after it are cut,
    the way a real 16:12 download cannot contain the 20:00 bar. Without that
    the stub would hand a mid-afternoon pass the whole evening and hide the
    freeze entirely."""

    def __init__(self, plan=None, default=None, as_of=None):
        super().__init__("yfinance")
        self.plan = plan or {}
        self.default = default if default is not None else bars()
        self.as_of = as_of
        self.calls = []
        self.kwargs = []

    def download(self, symbol, **kw):
        self.calls.append(symbol)
        self.kwargs.append(kw)
        out = self.plan.get(symbol, self.default)
        if isinstance(out, BaseException):
            raise out
        if self.as_of is not None and out is not None:
            out = out[out.index <= self.as_of]
        return out


def install_yf(fake):
    sys.modules["yfinance"] = fake
    return fake


# --------------------------------------------------------------------------
# 1. R03a: the result must be structured, not a bare int
# --------------------------------------------------------------------------
reset()
cand(9, 50, 1.1020, 1.1008)
install_yf(FakeYF())

FIELDS = ("eligible", "graded", "unresolved", "pending", "missing_data",
          "failed_writes", "retryable_failures", "permanent_failures",
          "retired", "read_ok", "complete")

_res1 = grade()
check("R03a: fill_outcomes returns a structured result, not a bare count",
      isinstance(_res1, dict), f"returned {type(_res1).__name__}: {_res1!r}")
check("R03a: the result carries every count the scheduler has to reconcile",
      isinstance(_res1, dict) and all(f in _res1 for f in FIELDS),
      f"missing={[f for f in FIELDS if not (isinstance(_res1, dict) and f in _res1)]}")


def _r03a_clean_day():
    return (_res1["eligible"] == 1 and _res1["graded"] == 1
            and _res1["complete"] is True and _res1["read_ok"] is True
            and _res1["retryable_failures"] == 0), str(_res1)


guard("R03a: a clean day grades its one row and reports complete",
      _r03a_clean_day)
check("R03a: the clean day really persisted its outcome",
      len(ungraded()) == 0, f"still ungraded={len(ungraded())}")


def _r03a_nothing_due():
    res = grade()
    return (res["eligible"] == 0 and res["graded"] == 0
            and res["complete"] is True), str(res)


guard("R03a: a day with nothing left to do is complete, not a failure",
      _r03a_nothing_due)


# --------------------------------------------------------------------------
# 2. R03a: an unreadable ledger must not read as an empty one
# --------------------------------------------------------------------------
reset()
_LEDGER.mkdir()          # exists() is True, read_text raises OSError


def _r03a_unreadable():
    res = grade()
    return (res["read_ok"] is False and res["complete"] is False
            and res["retryable_failures"] >= 1), str(res)


guard("R03a: an unreadable ledger reports read_ok False, never a finished day",
      _r03a_unreadable)
reset()

check("R03a: the tolerant readers keep their tolerant behaviour",
      forward_ledger._read_all() == [], "a missing ledger must still read as []")


# --------------------------------------------------------------------------
# 3. R03a: a missing dependency is not a graded day either
# --------------------------------------------------------------------------
reset()
cand(9, 50, 1.1020, 1.1008)
_saved_yf = sys.modules.get("yfinance")
sys.modules["yfinance"] = None    # import yfinance -> ImportError


def _r03a_no_dependency():
    res = grade()
    return (res["eligible"] == 1 and res["graded"] == 0
            and res["complete"] is False
            and res["retryable_failures"] >= 1), str(res)


guard("R03a: a missing dependency reports incomplete, not a finished day",
      _r03a_no_dependency)
check("R03a: the missing dependency left the row ungraded on disk",
      len(ungraded()) == 1, f"ungraded={len(ungraded())}")


# --------------------------------------------------------------------------
# 4. R03b: first download fails, second succeeds. Neither the failure nor the
#    scheduler may suppress the recovery.
# --------------------------------------------------------------------------
reset()
cand(9, 50, 1.1020, 1.1008)
_flaky = install_yf(FakeYF(plan={"EURUSD=X": RuntimeError("yfinance 429")}))


def _r03b_download_fails():
    res = grade()
    return (res["eligible"] == 1 and res["graded"] == 0
            and res["missing_data"] == 1 and res["complete"] is False), str(res)


guard("R03b: a failed download is counted as missing data, not as graded",
      _r03b_download_fails)
check("R03b: the failed download left the row ungraded on disk",
      len(ungraded()) == 1, f"ungraded={len(ungraded())}")

_flaky.plan = {}          # the provider comes back


def _r03b_download_recovers():
    res = grade()
    return (res["eligible"] == 1 and res["graded"] == 1
            and res["missing_data"] == 0 and res["complete"] is True), str(res)


guard("R03b: the next pass grades the row the outage cost",
      _r03b_download_recovers)
check("R03b: the recovered pass persisted the outcome",
      len(ungraded()) == 0, f"ungraded={len(ungraded())}")


# --------------------------------------------------------------------------
# 5. R03c: first write fails, second persists. A count taken from the
#    in-memory mutation is not evidence that anything reached the disk.
# --------------------------------------------------------------------------
reset()
cand(9, 50, 1.1020, 1.1008)
install_yf(FakeYF())

_real_write = forward_ledger._write_all
_write_calls = []


def _write_that_fails(records):
    _write_calls.append(len(records))
    print("forward_ledger: could NOT persist (test stub)")
    return False


forward_ledger._write_all = _write_that_fails


def _r03c_write_fails():
    res = grade()
    return (res["eligible"] == 1 and res["graded"] == 0
            and res["failed_writes"] == 1
            and res["complete"] is False), str(res)


guard("R03c: a lost write is counted as a failed write, never as graded",
      _r03c_write_fails)
check("R03c: the write stub was actually reached",
      len(_write_calls) == 1, f"write calls={_write_calls}")
forward_ledger._write_all = _real_write
check("R03c: the lost write left the row ungraded on disk",
      len(ungraded()) == 1, f"ungraded={len(ungraded())}")


def _r03c_write_recovers():
    res = grade()
    return (res["graded"] == 1 and res["failed_writes"] == 0
            and res["complete"] is True), str(res)


guard("R03c: the next pass persists what the lost write dropped",
      _r03c_write_recovers)
check("R03c: the recovered pass persisted the outcome",
      len(ungraded()) == 0, f"ungraded={len(ungraded())}")


# --------------------------------------------------------------------------
# 6. R03a: unresolved partitions GRADED, not eligible. A finished row with a
#    tier that never resolved is a finished row.
# --------------------------------------------------------------------------
reset()
cand(9, 50, 1.1020, 1.1008)
# price drifts above the 0.4R tier and never reaches 1R, never hits the stop
install_yf(FakeYF(default=bars(hi=1.1030, lo=1.1015)))


def _r03a_unresolved():
    res = grade()
    row = rows()[0]
    hits = (row.get("outcome") or {}).get("hit") or {}
    return (res["graded"] == 1 and res["unresolved"] == 1
            and res["complete"] is True
            and any(v is None for v in hits.values())), f"{res} hit={hits}"


guard("R03a: an undecided tier counts as graded and unresolved, not a failure",
      _r03a_unresolved)


# --------------------------------------------------------------------------
# 7. R03a: the reconciliation identity over a mixed day
#      eligible == graded + pending + missing_data + failed_writes
#                  + permanent_failures
# --------------------------------------------------------------------------
def build_mixed_day():
    """One gradable row, one row whose provider is down, one row that can
    never parse. Returns nothing; writes straight into the ledger."""
    reset()
    cand(9, 50, 1.1020, 1.1008, symbol="EURUSD=X")
    cand(9, 55, 155.0, 154.0, symbol="JPY=X")
    with _LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "event_id": "malformed-row-1", "date": "not-a-date",
            "time_et": "09:50:00", "symbol": "EURUSD=X", "direction": "BUY",
            "entry": 1.1020, "stop": 1.1008, "risk": 0.0012, "atr": 0.0012,
            "targets": {"t04": 1.10248, "t1": 1.1032, "t2": 1.1044,
                        "liq": None},
            "passes": True, "outcome": None, "selected": False,
        }) + "\n")


def identity_holds(res):
    """Every eligible row lands in exactly one bucket. `pending` joined the
    sum with G2: a row whose own day has not closed yet has neither been
    graded nor failed, it is simply not answerable yet."""
    return (res["eligible"]
            == res["graded"] + res["pending"] + res["missing_data"]
            + res["failed_writes"] + res["permanent_failures"])


build_mixed_day()
install_yf(FakeYF(plan={"JPY=X": RuntimeError("yfinance 429")},
                  default=bars()))


def _r03_identity():
    res = grade()
    if not identity_holds(res):
        return False, f"identity broken: {res}"
    ok = (res["eligible"] == 3 and res["graded"] == 1
          and res["missing_data"] == 1 and res["permanent_failures"] == 1
          and res["complete"] is False)
    return ok, str(res)


guard("R03a: the mixed day reconciles and reports itself incomplete",
      _r03_identity)


def _r03_permanent_never_blocks():
    """A row that can never parse must not hold the day open forever."""
    install_yf(FakeYF(plan={}, default=bars(hi=200.0, lo=50.0)))
    res = grade()
    if not identity_holds(res):
        return False, f"identity broken: {res}"
    return (res["graded"] == 1 and res["permanent_failures"] == 1
            and res["retryable_failures"] == 0
            and res["complete"] is True), str(res)


guard("R03a: a permanently unparseable row cannot block completion forever",
      _r03_permanent_never_blocks)


# --------------------------------------------------------------------------
# 8. R03a: every run leaves an audit line, so no count is ever hand-typed
# --------------------------------------------------------------------------
def _r03_audit():
    path = forward_ledger._audit_path()
    if not path.exists():
        return False, f"no audit file at {path}"
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    last = json.loads(lines[-1])
    return (len(lines) >= 2 and all(f in last for f in FIELDS)), \
        f"{len(lines)} lines, last={last}"


guard("R03a: each run appends one audit line carrying its counts", _r03_audit)


# --------------------------------------------------------------------------
# 9. R03e: the counts must not move with an AI setting
# --------------------------------------------------------------------------
_saved_learn = _bot_test_os.environ.get("LEARN_ENABLED")
_saved_mode = _bot_test_os.environ.get("API_MODE")


def _counts_under(learn_flag, api_mode):
    _bot_test_os.environ["LEARN_ENABLED"] = learn_flag
    _bot_test_os.environ["API_MODE"] = api_mode
    build_mixed_day()
    install_yf(FakeYF(plan={"JPY=X": RuntimeError("yfinance 429")},
                      default=bars()))
    res = grade()
    return {k: res[k] for k in FIELDS}


try:
    _off = _counts_under("false", "off")
    _on = _counts_under("true", "ask_only")
    check("R03e: the fixture really flips the paid review off and on",
          True, "")
    check("R03e: the day reconciles identically whatever the AI switches say",
          _off == _on, f"off={_off} on={_on}")
finally:
    for k, v in (("LEARN_ENABLED", _saved_learn), ("API_MODE", _saved_mode)):
        if v is None:
            _bot_test_os.environ.pop(k, None)
        else:
            _bot_test_os.environ[k] = v

_learn_src = (REPO / "learn.py").read_text(encoding="utf-8-sig")
check("R03e: grading has exactly one scheduler, so the counts cannot fork",
      "fill_outcomes" not in _learn_src,
      "learn.run still grades on its own, unaudited, behind LEARN_ENABLED")
check("R03e: the nightly digest still reports the ledger",
      "nightly_summary" in _learn_src)


# --------------------------------------------------------------------------
# 10. The scheduler. Completion is recorded only when the work is durably
#     accounted for, and exhaustion parks as needs-attention.
# --------------------------------------------------------------------------
import market_calendar  # noqa: E402
import scanner          # noqa: E402

_src_scanner = (REPO / "scanner.py").read_text(encoding="utf-8-sig")

# The session the scheduler is grading IN, one day after the session the
# fixture rows were signalled in. The scheduler hands its own clock to the
# grader now (G2), so a scheduler fixture that ticked on the signal day would
# be asking it to grade a day that has not closed yet, which is the thing G2
# forbids. Both dates are real trading days.
_GRADE_DAY = NOW.date()
_SIGNAL_DAY = datetime(2026, 9, 8).date()
check("the scheduler fixture uses a real trading day",
      market_calendar.is_trading_day(_GRADE_DAY)
      and market_calendar.is_trading_day(_SIGNAL_DAY),
      f"{_GRADE_DAY} / {_SIGNAL_DAY}")

MAX_TRIES = getattr(scanner.Service, "GRADE_MAX_ATTEMPTS",
                    scanner.Service.MAX_JOB_ATTEMPTS)
RETRY_S = getattr(scanner.Service, "GRADE_RETRY_S", 0)


def fresh_service():
    svc = scanner.Service.__new__(scanner.Service)
    svc.dry = False
    for key in ("forward_graded", "forward_grade_attention",
                "forward_grade_last"):
        config.state_set(key, None)
    config.state_set("forward_grade_tries", {})
    return svc


def tick(svc, minute_offset, base=None):
    """One daemon tick, well after the 16:05 gate and spaced far enough apart
    that a retry budget measured in minutes is not burned in seconds."""
    from datetime import timedelta
    base = base or NOW
    scanner.Service.maybe_grade_forward(svc, base + timedelta(minutes=minute_offset))


_SPACING = max(int(RETRY_S / 60) + 1, 1)


# 10a. first download fails, second succeeds, through the real scheduler
reset()
cand(9, 50, 1.1020, 1.1008)
_sched_yf = install_yf(FakeYF(plan={"EURUSD=X": RuntimeError("yfinance 429")}))
_svc = fresh_service()
tick(_svc, 0)

check("R03b: an incomplete grading pass does NOT claim the day",
      config.state_get("forward_graded") is None,
      f"forward_graded={config.state_get('forward_graded')!r}")
check("R03b: the incomplete pass left the row ungraded",
      len(ungraded()) == 1, f"ungraded={len(ungraded())}")

_sched_yf.plan = {}
tick(_svc, _SPACING)
check("R03b: the scheduler runs again after an incomplete pass",
      len(_sched_yf.calls) >= 2, f"downloads={_sched_yf.calls}")
check("R03b: the recovered pass grades the row the outage cost",
      len(ungraded()) == 0, f"ungraded={len(ungraded())}")
check("R03b: only the durably accounted day is recorded as graded",
      config.state_get("forward_graded") == str(_GRADE_DAY),
      f"forward_graded={config.state_get('forward_graded')!r}")

_before = len(_sched_yf.calls)
tick(_svc, _SPACING * 2)
check("R03b: a graded day is not graded twice",
      len(_sched_yf.calls) == _before, f"downloads={_sched_yf.calls}")


# 10b. first write fails, second persists, through the real scheduler
reset()
cand(9, 50, 1.1020, 1.1008)
install_yf(FakeYF())
_svc = fresh_service()
forward_ledger._write_all = _write_that_fails
_write_calls.clear()
tick(_svc, 0)
forward_ledger._write_all = _real_write

check("R03c: a lost write does NOT claim the day",
      config.state_get("forward_graded") is None,
      f"forward_graded={config.state_get('forward_graded')!r}")
check("R03c: the lost write left the row ungraded",
      len(ungraded()) == 1, f"ungraded={len(ungraded())}")

tick(_svc, _SPACING)
check("R03c: the next scheduled pass persists what the lost write dropped",
      len(ungraded()) == 0, f"ungraded={len(ungraded())}")
check("R03c: the day is recorded as graded once the write really landed",
      config.state_get("forward_graded") == str(_GRADE_DAY),
      f"forward_graded={config.state_get('forward_graded')!r}")


# 10c. exhausted retries are needs-attention, not success, and do not spin
reset()
cand(9, 50, 1.1020, 1.1008)
_dead_yf = install_yf(FakeYF(plan={"EURUSD=X": RuntimeError("yfinance 429")}))
_svc = fresh_service()
for _i in range(MAX_TRIES + 2):
    tick(_svc, _i * _SPACING)

check("R03d: an exhausted grading budget never writes the graded state",
      config.state_get("forward_graded") != str(_GRADE_DAY),
      f"forward_graded={config.state_get('forward_graded')!r}")


def _r03d_parked():
    park = config.state_get("forward_grade_attention")
    if not isinstance(park, dict):
        return False, f"no needs-attention record: {park!r}"
    return (park.get("key") == str(_GRADE_DAY) and bool(park.get("reason"))), \
        str(park)


guard("R03d: exhaustion is parked as needs-attention, naming the key and why",
      _r03d_parked)
check("R03d: zero rows were graded, and the state says so",
      len(ungraded()) == 1, f"ungraded={len(ungraded())}")

_parked_at = len(_dead_yf.calls)
for _i in range(6):
    tick(_svc, (MAX_TRIES + 3 + _i) * _SPACING)
check("R03d: a parked day stops re-attempting instead of spinning every tick",
      len(_dead_yf.calls) == _parked_at,
      f"downloads after parking: {len(_dead_yf.calls) - _parked_at}")


# 10d. the grading key belongs to the grading job, not to the paid review
def _r03_own_key():
    """A free deterministic measurement must not wait on the paid nightly
    review's random 21:00 to 23:45 target to open its key. Which rows that
    key can actually settle is a separate question, decided per row: see G2."""
    early = NOW
    if not hasattr(scanner, "forward_grade_session"):
        return False, "no dedicated grading key: still on learn_session_due"
    got = scanner.forward_grade_session(early)
    return got == _GRADE_DAY, f"key at 17:00 ET is {got}, wanted {_GRADE_DAY}"


guard("R03d: the grading key opens at the close, not on the paid review's clock",
      _r03_own_key)


def _r03_key_off_session():
    """A weekend or holiday tick points back at the last real session."""
    sat = datetime(2026, 9, 12, 18, 0, tzinfo=ET)
    got = scanner.forward_grade_session(sat)
    return got == market_calendar.prev_trading_day(sat.date()), str(got)


guard("R03d: an off-session tick still points at the last session's work",
      _r03_key_off_session)


# --------------------------------------------------------------------------
# 11. G1: a row whose day has aged out of the download window must not park
#     grading forever.
#
#     The trigger is ordinary: the worker stopped for more than five trading
#     days. A paused deploy, a billing lapse, a Railway outage. When it comes
#     back the old row can never produce bars again, that was counted as
#     missing_data, and missing_data is RETRYABLE, so complete was False on
#     every pass from then on and every later session parked as
#     needs-attention with nothing wrong with it.
# --------------------------------------------------------------------------
reset()
_STALE_DAY = "2026-08-09"        # a month behind the frame the provider serves
_stale = cand(9, 50, 1.1020, 1.1008, day=_STALE_DAY)
_fresh = cand(9, 50, 1.1020, 1.1008)
forward_ledger.mark_selected(_stale, fired_at_et=f"{_STALE_DAY} 09:50:07",
                             position_id="pos-stale")
forward_ledger.mark_selected(_fresh, fired_at_et=f"{DAY} 09:50:07",
                             position_id="pos-fresh")
_g1_yf = install_yf(FakeYF())    # the frame only ever covers the signal day


def _g1_retires():
    res = grade()
    if not identity_holds(res):
        return False, f"identity broken: {res}"
    return (res["eligible"] == 2 and res["graded"] == 1
            and res["retired"] == 1 and res["permanent_failures"] == 1
            and res["missing_data"] == 0
            and res["retryable_failures"] == 0
            and res["complete"] is True), str(res)


guard("G1: a day the download can no longer reach is retired, not retried "
      "forever", _g1_retires)


def _g1_settles():
    """The passes after it are where the bug actually showed: on the
    un-repaired grader they graded nothing and stayed incomplete, for every
    day, forever."""
    r2 = grade()
    r3 = grade()
    return (r2["eligible"] == 0 and r2["complete"] is True
            and r3["eligible"] == 0 and r3["complete"] is True), f"{r2} {r3}"


guard("G1: the passes after it have nothing left to do and stay complete",
      _g1_settles)

check("G1: the retired row is still on the volume, not dropped",
      len(rows()) == 2, f"rows={len(rows())}")


def _g1_auditable():
    row = [r for r in rows() if r.get("date") == _STALE_DAY][0]
    oc = row.get("outcome") or {}
    return (bool(oc.get("retired")) and bool(oc.get("reason"))
            and oc.get("hit") == {}), str(oc)


guard("G1: the retirement is written on the row, with its reason, so it stays "
      "auditable", _g1_auditable)


def _g1_not_published():
    """A retired row is evidence of nothing. It must reach neither the
    numerator nor the denominator of a published number."""
    sb = forward_ledger.scoreboard()
    return (sb["n"] == 1 and sb["tiers"]["t04"]["n"] == 1), \
        f"n={sb['n']} t04={sb['tiers']['t04']}"


guard("G1: a retired row never enters the scoreboard", _g1_not_published)


def _g1_one_definition():
    """The age boundary and the download period have to come from the SAME
    number. A second copy of "5" that drifts brings the bug straight back."""
    days = getattr(forward_ledger, "GRADE_LOOKBACK_DAYS", None)
    period = getattr(forward_ledger, "GRADE_PERIOD", None)
    if not days or not period:
        return False, "no single definition of the download window"
    if period != f"{days}d":
        return False, f"period {period!r} is not built from {days}"
    asked = [kw.get("period") for kw in _g1_yf.kwargs]
    return (bool(asked) and all(p == period for p in asked)), \
        f"the downloads asked for {asked}"


guard("G1: the window the download asks for is the window the retirement uses",
      _g1_one_definition)


def _g1_short_frame_is_not_the_end_of_the_window():
    """A provider that returns a short frame on one pass is a hiccup, not the
    end of its window. Retiring on that alone would throw away rows that are
    still perfectly gradable tomorrow."""
    reset()
    cand(9, 50, 1.1020, 1.1008)                          # the signal day
    install_yf(FakeYF(default=bars(day="2026-09-09")))   # nothing from it
    res = grade()
    if res["retired"]:
        return False, f"a short frame retired a row inside the window: {res}"
    return (res["missing_data"] == 1 and res["complete"] is False), str(res)


guard("G1: a short frame alone does not retire a row that is still in reach",
      _g1_short_frame_is_not_the_end_of_the_window)


# --------------------------------------------------------------------------
# 12. G2: the equity close is not the end of the day for a row that is still
#     trading. EURUSD=X and JPY=X are in the sniper roster and run 24h, and
#     the download asks for prepost, so bars keep printing after 16:00.
#     fill_outcomes only ever walks rows whose outcome is still None, so
#     whatever the 16:12 pass wrote was frozen for good.
# --------------------------------------------------------------------------
reset()
cand(9, 50, 1.1020, 1.1008)                  # EURUSD=X, a 24h FX symbol
_fx_yf = install_yf(FakeYF(default=fx_day(), as_of=CLOSE))


def _g2_not_frozen_at_the_close():
    res = grade(CLOSE)
    if not identity_holds(res):
        return False, f"identity broken: {res}"
    return (res["eligible"] == 1 and res["graded"] == 0
            and res["pending"] == 1 and res["retryable_failures"] == 0
            and res["complete"] is True), str(res)


guard("G2: a row whose own day is still open is deferred, not graded early",
      _g2_not_frozen_at_the_close)
check("G2: the deferred row is left ungraded on disk, so a later pass sees it",
      len(ungraded()) == 1, f"ungraded={len(ungraded())}")

_fx_yf.as_of = None              # the evening has printed


def _g2_graded_when_knowable():
    res = grade()
    hit = (rows()[0].get("outcome") or {}).get("hit") or {}
    return (res["graded"] == 1 and res["pending"] == 0
            and res["complete"] is True and hit.get("t04") is True), \
        f"{res} hit={hit}"


guard("G2: the next pass grades it once the day has closed, and the late win "
      "counts as a win", _g2_graded_when_knowable)

reset()
cand(9, 50, 1.1020, 1.1008)
install_yf(FakeYF(as_of=CLOSE))  # the default bars stop it out on bar one


def _g2_terminal_still_grades_today():
    """Deferring must not become deferring everything. A walk that stopped, or
    that resolved every tier, can never be moved by a later bar: the walk
    stops reading there. Recording that is not early, it is finished."""
    res = grade(CLOSE)
    return (res["graded"] == 1 and res["pending"] == 0
            and res["complete"] is True), str(res)


guard("G2: a walk that already terminated is graded on the day it happened",
      _g2_terminal_still_grades_today)


# 12b. the same row through the REAL scheduler, at the clock the freeze
#      actually happened on
reset()
cand(9, 50, 1.1020, 1.1008)
_fx_sched = install_yf(FakeYF(default=fx_day(), as_of=CLOSE))
_svc = fresh_service()
tick(_svc, 12, base=datetime(2026, 9, 8, 16, 0, tzinfo=ET))   # 16:12 ET

check("G2: the 16:12 pass does not freeze a row whose day is still open",
      len(ungraded()) == 1, f"ungraded={len(ungraded())}")
check("G2: a deferred row is not a failure, so the day is not parked",
      config.state_get("forward_grade_attention") is None,
      f"park={config.state_get('forward_grade_attention')!r}")
check("G2: the pass still accounts for the session it ran in",
      config.state_get("forward_graded") == str(_SIGNAL_DAY),
      f"forward_graded={config.state_get('forward_graded')!r}")

_fx_sched.as_of = None
tick(_svc, 0)                    # the next session's pass, whole day in frame
check("G2: the next session's pass picks the deferred row up",
      len(ungraded()) == 0, f"ungraded={len(ungraded())}")


def _g2_sched_win():
    hit = (rows()[0].get("outcome") or {}).get("hit") or {}
    return hit.get("t04") is True, f"hit={hit}"


guard("G2: the target taken out after the close is recorded as the win it was",
      _g2_sched_win)

check("G2: the grading key no longer claims the bars are final at the close",
      "The bars are final at" not in _src_scanner,
      "forward_grade_session still says the close settles every row")


# --------------------------------------------------------------------------
# 13. G3: one new-format row must not evict a day's real trade.
#
#     The era was read off the mere PRESENCE of a selected key on ANY row of
#     the date, and rows are grouped by date alone. Every row on the live
#     volume predates the key, so the first row written after this ships
#     (selected False, because nothing has been broadcast yet) flipped its
#     whole date into selected-only mode and dropped that day's real, graded,
#     already-broadcast trade out of the scoreboard.
# --------------------------------------------------------------------------
def _era_row(day, sym, direction, t, passes, hit04, eid, selected=None):
    """One graded row. selected=None means the row predates the flag
    entirely, which is the shape of every row on the live volume."""
    row = {
        "event_id": eid, "date": day, "time_et": t, "symbol": sym,
        "direction": direction, "entry": 100.0, "stop": 99.0, "risk": 1.0,
        "atr": 1.0,
        "targets": {"t04": 100.4, "t1": 101.0, "t2": 102.0, "liq": 103.0},
        "grade": "A", "score": 9, "passes": passes, "reasons": [],
        "gap_atr": 1.6, "hour_et": int(t[:2]), "sibling": bool(passes),
        "outcome": {"stopped": not hit04, "mfe_r": 1.0,
                    "hit": {"t04": hit04, "t1": hit04, "t2": hit04,
                            "liq": hit04},
                    "graded_at": f"{day} 17:00"},
    }
    if selected is not None:
        row.update({"selected": selected, "selected_at": None,
                    "position_id": "pos-x" if selected else None})
    return row


def _write_rows(rs):
    reset()
    _LEDGER.write_text("\n".join(json.dumps(r) for r in rs) + "\n",
                       encoding="utf-8")


# the deploy day: rows written before the build and rows written after it, on
# the same date. EVERY date on the volume looks like this the moment a build
# that carries the flag starts recording mid-session.
_write_rows([
    _era_row(DAY, "EURUSD=X", "BUY", "09:50:00", True, True, "old-a"),
    _era_row(DAY, "EURUSD=X", "BUY", "14:00:00", True, True, "old-b"),
    _era_row(DAY, "TSLA", "BUY", "13:05:00", False, None, "old-c"),
    _era_row(DAY, "JPY=X", "SELL", "10:30:00", True, True, "new-a",
             selected=False),
    _era_row(DAY, "JPY=X", "SELL", "11:20:00", True, False, "new-b",
             selected=True),
])


def _g3_no_eviction():
    """old-a is the day's real broadcast trade and a win. new-b is what the
    repaired recorder says went out after the deploy, and a loss. old-b is a
    second passing read of a symbol and direction the old cap already counted,
    new-a was never broadcast, old-c never passed."""
    sb = forward_ledger.scoreboard()
    if sb["n"] != 2:
        return False, f"cohort n={sb['n']}, expected 2"
    t = sb["tiers"]["t04"]
    return (t["n"] == 2 and t["wins"] == 1), str(t)


guard("G3: a new-format row does not evict the day's real graded trade",
      _g3_no_eviction)

_write_rows([
    _era_row("2026-09-01", "EURUSD=X", "BUY", "09:35:00", False, None, "l-a"),
    _era_row("2026-09-01", "EURUSD=X", "BUY", "09:50:00", True, True, "l-b"),
    _era_row("2026-09-01", "EURUSD=X", "BUY", "10:50:00", True, True, "l-c"),
    _era_row("2026-09-01", "JPY=X", "SELL", "11:20:00", True, False, "l-d"),
])


def _g3_legacy_unmoved():
    """A date with no flag anywhere keeps exactly the cohort it published: the
    first passing observation per symbol and direction."""
    sb = forward_ledger.scoreboard()
    return (sb["n"] == 2 and sb["tiers"]["t04"]["wins"] == 1), \
        f"n={sb['n']} t04={sb['tiers']['t04']}"


guard("G3: a date that predates the flag publishes exactly what it did before",
      _g3_legacy_unmoved)

_write_rows([
    _era_row("2026-09-02", "EURUSD=X", "BUY", "09:50:00", True, True, "n-a",
             selected=False),
    _era_row("2026-09-02", "EURUSD=X", "BUY", "09:55:00", True, False, "n-b",
             selected=True),
])


def _g3_new_only_unmoved():
    """A date recorded entirely after the flag counts what actually went out,
    and only that."""
    sb = forward_ledger.scoreboard()
    return (sb["n"] == 1 and sb["tiers"]["t04"]["wins"] == 0), \
        f"n={sb['n']} t04={sb['tiers']['t04']}"


guard("G3: a date recorded entirely after the flag counts only the broadcast "
      "one", _g3_new_only_unmoved)


if _saved_yf is not None:
    sys.modules["yfinance"] = _saved_yf
else:
    sys.modules.pop("yfinance", None)

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all grading integrity checks passed")
