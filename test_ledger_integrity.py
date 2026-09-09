"""Integrity of the forward ledger: candidate identity, and durable writes.

The 2026-09-08 independent review left P02 only partly repaired. Four failing
cases are pinned here.

R01a  A passing read at 09:50 silently erased a DIFFERENT passing observation
      at 09:55. The later one was not demoted, not flagged, not written as a
      reject: it left no trace at all. That is a strategy entry rule (one trade
      per symbol per day) implemented inside the recorder, and it made the
      ledger an unfaithful record of what the bot actually saw.

R01b  record_candidate allocated an id, stored it in the row, and returned
      None. Nothing downstream could hold it, so an alert and its forward
      observation could only ever be matched by guesswork on the day.

R01c  The alert card and the tracked position were built from a LATER clock
      than the read that produced the observation, and carried no id, so the
      two clocks disagreed and the join was approximate in both directions.

R02   fill_outcomes read the whole file, spent seconds inside a download, then
      republished its stale snapshot. Any observation appended during the
      download was destroyed. Atomic replace protects the bytes of one write.
      It does nothing about a concurrent update.

Also pinned: the recording cap must not be the place the one-entry-per-day
rule lives, and the published scoreboard cohort must not move as a side
effect of loosening the recorder.

No network, no Telegram, no yfinance, no production storage.

Run:  python test_ledger_integrity.py     (exit code 0 = all good)
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.

import json
import sys
import tempfile
import threading
import time
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# an isolated data dir BEFORE config is imported, so nothing here can read or
# write the real runtime state
_TMP = tempfile.mkdtemp(prefix="kelbot_ledger_")
_bot_test_os.environ["DATA_DIR"] = _TMP

import config          # noqa: E402
import forward_ledger  # noqa: E402
import sniper_book     # noqa: E402

ET = ZoneInfo("America/New_York")
REPO = Path(__file__).parent

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def guard(name, fn, detail=""):
    """Run a check that can raise on the un-repaired code, and record the
    raise as the failure it is instead of aborting the whole file."""
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
    for p in Path(_TMP).glob("sniper_forward*"):
        try:
            p.unlink()
        except OSError:
            pass


def rows():
    if not _LEDGER.exists():
        return []
    return [json.loads(ln) for ln in
            _LEDGER.read_text(encoding="utf-8").splitlines() if ln.strip()]


def cand(hh, mm, passes, entry, stop, symbol="EURUSD=X", direction="BUY",
         ss=0, gap=1.6, reasons=()):
    return forward_ledger.record_candidate(
        symbol=symbol, direction=direction, price=entry, atr=0.0012,
        ticket={"entry": entry, "stop": stop},
        conf={"grade": "A" if passes else "B", "score": 9},
        passes=passes, reasons=list(reasons), gap_atr=gap, hour_et=hh,
        now_et=datetime(2026, 9, 8, hh, mm, ss, tzinfo=ET))


# --------------------------------------------------------------------------
# 1. R01a: an earlier passing read must not erase a later, different one
# --------------------------------------------------------------------------
reset()
cand(9, 50, True, 1.1020, 1.1008)
cand(9, 55, True, 1.1035, 1.1022)
_r = rows()
_passing = [r for r in _r if r.get("passes")]
check("R01a: two different passing reads are BOTH recorded",
      len(_passing) == 2,
      f"kept={[(r.get('time_et'), r.get('entry')) for r in _passing]}")
check("R01a: the 09:55 observation is present, not silently discarded",
      any(r.get("time_et") == "09:55:00" for r in _passing),
      f"times={[r.get('time_et') for r in _r]}")
check("R01a: every recorded observation carries a unique id",
      len(_r) > 0 and all(r.get("event_id") for r in _r)
      and len({r["event_id"] for r in _r}) == len(_r),
      str([r.get("event_id") for r in _r]))


# --------------------------------------------------------------------------
# 2. R01b: the id must be RETURNED so something downstream can hold it
# --------------------------------------------------------------------------
reset()
_cid = cand(9, 50, True, 1.1020, 1.1008)
check("R01b: record_candidate returns the candidate id",
      isinstance(_cid, str) and len(_cid) > 0, repr(_cid))
_again = cand(9, 50, True, 1.1020, 1.1008)
check("R01b: a byte-identical retry returns the SAME id",
      _again == _cid and isinstance(_again, str), f"{_cid!r} vs {_again!r}")
check("R01b: that retry adds no second row", len(rows()) == 1,
      f"{len(rows())} rows")
check("R01b: the returned id is the row's persisted event_id",
      bool(rows()) and rows()[0].get("event_id") == _cid,
      f"{rows()[0].get('event_id') if rows() else None} vs {_cid}")

# a genuinely unrecordable observation still returns nothing to hold
check("R01b: an observation with no ticket still returns nothing",
      forward_ledger.record_candidate(
          symbol="SPY", direction="BUY", price=1.0, atr=1.0, ticket=None,
          conf={}, passes=False, reasons=[]) is None)


# --------------------------------------------------------------------------
# 3. R01c: the delivered alert and the tracked position must carry the id,
#    and must join the observation even though their clocks differ
# --------------------------------------------------------------------------
reset()
_src_mt = (REPO / "market_tools.py").read_text(encoding="utf-8")
check("R01c: market_tools keeps the id record_candidate returns",
      "_cid = _fl.record_candidate(" in _src_mt, "call site still discards it")
check("R01c: market_tools hands the id to the read's fvg payload",
      'fvg_info["candidate_id"] = _cid' in _src_mt,
      "no carrier for the id on the read result")

_src_sc = (REPO / "scanner.py").read_text(encoding="utf-8")
check("R01c: the scanner reads the id off the read it is about to alert on",
      'candidate_id' in _src_sc and 'mark_selected' in _src_sc,
      "scanner never links the delivered alert back to the observation")


def _r01c_join():
    cid = cand(9, 55, True, 1.1035, 1.1022)
    # the alert clock is LATER than the read clock: that is the real gap
    fired = datetime(2026, 9, 8, 9, 55, 7, tzinfo=ET)
    sniper_book.LEDGER = Path(_TMP) / "sniper_positions.json"
    if sniper_book.LEDGER.exists():
        sniper_book.LEDGER.unlink()
    row = sniper_book.open_trade(
        symbol="EURUSD=X", display="EUR/USD", direction="BUY",
        entry=1.1035, stop=1.1022, target=1.10402, day="2026-09-08",
        time_et=f"{fired:%H:%M:%S}", decimals=5, entry_ts=fired,
        candidate_id=cid)
    if not row:
        return False, "open_trade rejected the ticket"
    if row.get("candidate_id") != cid:
        return False, f"position candidate_id={row.get('candidate_id')!r}"
    if row.get("time_et") == "09:55:00":
        return False, "the two clocks were not actually different"
    joined = [r for r in rows() if r.get("event_id") == row.get("candidate_id")]
    return len(joined) == 1, f"joined {len(joined)} observations"


guard("R01c: the position joins exactly one observation by id, "
      "across a different clock", _r01c_join)


def _r01c_selected():
    cid = rows()[0]["event_id"]
    ok = forward_ledger.mark_selected(
        cid, fired_at_et=datetime(2026, 9, 8, 9, 55, 7, tzinfo=ET),
        position_id="2026-09-08-095507-EURUSD=X-BUY")
    sel = [r for r in rows() if r.get("selected")]
    return (bool(ok) and len(sel) == 1
            and sel[0].get("position_id") == "2026-09-08-095507-EURUSD=X-BUY"), \
        f"ok={ok} selected={[(r.get('time_et'), r.get('position_id')) for r in sel]}"


guard("R01c: the delivered alert is recorded as an explicit selection",
      _r01c_selected)


# --------------------------------------------------------------------------
# 4. R01d: the recorder must hold NO opinion about how many entries a day
#    permits. That rule lives in the scanner and in sniper_book, once.
# --------------------------------------------------------------------------
_src_fl = (REPO / "forward_ledger.py").read_text(encoding="utf-8")
check("R01d: the per-day passing suppression is gone from the recorder",
      "At most one ACCEPTED entry per symbol" not in _src_fl,
      "the recorder still enforces an entry-count rule")

reset()
_b = cand(9, 50, True, 1.1020, 1.1008, direction="BUY")
_s = cand(10, 10, True, 1.1005, 1.1018, direction="SELL")
check("R01d: a passing BUY and a passing SELL on one symbol are both recorded",
      len(rows()) == 2,
      f"rows={[(r.get('time_et'), r.get('direction')) for r in rows()]}")
check("R01d: recording alone selects nothing for broadcast",
      not any(r.get("selected") for r in rows()),
      f"selected={[r.get('time_et') for r in rows() if r.get('selected')]}")


def _r01d_one_selected():
    forward_ledger.mark_selected(_b, position_id="pos-1")
    sel = [r for r in rows() if r.get("selected")]
    return len(sel) == 1 and sel[0].get("direction") == "BUY", \
        f"selected={[(r.get('direction'), r.get('time_et')) for r in sel]}"


guard("R01d: exactly one observation becomes the selected entry",
      _r01d_one_selected)


# --------------------------------------------------------------------------
# yfinance stub: no network, ever
# --------------------------------------------------------------------------
import pandas as pd  # noqa: E402


def _bars_for(day="2026-09-08", start_hh=10, n=12, hi=1.2000, lo=1.0000):
    idx = pd.date_range(f"{day} {start_hh:02d}:00", periods=n, freq="5min",
                        tz="America/New_York")
    return pd.DataFrame({"Open": [1.1] * n, "High": [hi] * n,
                         "Low": [lo] * n, "Close": [1.1] * n}, index=idx)


class _FakeYF(types.ModuleType):
    """Stands in for the yfinance module inside fill_outcomes."""

    def __init__(self, on_call=None, frame=None):
        super().__init__("yfinance")
        self.on_call = on_call
        self.frame = frame if frame is not None else _bars_for()
        self.calls = 0

    def download(self, symbol, **kw):
        self.calls += 1
        if self.on_call:
            self.on_call(symbol, self.calls)
        return self.frame


def install_yf(fake):
    sys.modules["yfinance"] = fake
    return fake


# --------------------------------------------------------------------------
# 5. R02 sequential: an append that lands DURING grading must survive the
#    grader's publish, and the grader must not be holding the ledger while
#    it downloads
# --------------------------------------------------------------------------
reset()
cand(9, 50, True, 1.1020, 1.1008)

_appended = {}


def _append_mid_download(symbol, n):
    if n == 1:
        t0 = time.monotonic()
        _appended["cid"] = cand(10, 5, False, 155.0, 154.0, symbol="JPY=X",
                                reasons=["gap under the floor"])
        _appended["secs"] = time.monotonic() - t0


install_yf(_FakeYF(on_call=_append_mid_download))


def _r02_sequential():
    graded = forward_ledger.fill_outcomes()
    after = rows()
    jpy = [r for r in after if r.get("symbol") == "JPY=X"]
    eur = [r for r in after if r.get("symbol") == "EURUSD=X"]
    if not jpy:
        return False, ("the observation appended during grading was destroyed: "
                       f"rows={[r.get('symbol') for r in after]}")
    if not (eur and eur[0].get("outcome")):
        return False, f"the grader did not persist its outcome (graded={graded})"
    return True, f"graded={graded}"


guard("R02: an append landing during grading survives the publish",
      _r02_sequential)
check("R02: the appender was not blocked behind the grader's download",
      _appended.get("secs") is not None and _appended["secs"] < 1.0,
      f"append took {_appended.get('secs')}")
check("R02: the append during grading returned a usable id",
      isinstance(_appended.get("cid"), str) and _appended.get("cid"),
      repr(_appended.get("cid")))


# --------------------------------------------------------------------------
# 6. R02 threaded: the real interleave, an appender on another thread
# --------------------------------------------------------------------------
reset()
for i, sym in enumerate(("EURUSD=X", "JPY=X", "^GSPC", "TSLA", "SPY")):
    cand(9, 50, True, 100.0 + i, 99.0 + i, symbol=sym, ss=i)


def _slow(symbol, n):
    time.sleep(0.25)


install_yf(_FakeYF(on_call=_slow, frame=_bars_for(hi=200.0, lo=50.0)))

_res = {}


def _grade():
    try:
        _res["graded"] = forward_ledger.fill_outcomes()
    except Exception as e:
        _res["error"] = f"{type(e).__name__}: {e}"


_t = threading.Thread(target=_grade, daemon=True)
_t.start()
time.sleep(0.05)
_t0 = time.monotonic()
_late = cand(10, 30, False, 500.0, 499.0, symbol="QCOM",
             reasons=["one-way tape"])
_late_secs = time.monotonic() - _t0
_t.join(timeout=30)


def _r02_threaded():
    after = rows()
    qcom = [r for r in after if r.get("symbol") == "QCOM"]
    graded = [r for r in after if r.get("outcome")]
    if _res.get("error"):
        return False, _res["error"]
    if not qcom:
        return False, ("the concurrently appended reject was clobbered: "
                       f"rows={[r.get('symbol') for r in after]}")
    if not graded:
        return False, "the grader's outcomes did not persist"
    return True, f"rows={len(after)} graded={len(graded)}"


guard("R02: a concurrent append on another thread survives grading",
      _r02_threaded)
check("R02: grading does not hold the ledger across its download",
      _late_secs < 0.2, f"the concurrent append waited {_late_secs:.2f}s")


# --------------------------------------------------------------------------
# 7. R02 volume: many writers, no torn line, no lost row, no duplicate id
# --------------------------------------------------------------------------
reset()
_PER = 25
_SYMS = ("EURUSD=X", "JPY=X", "TSLA", "SPY")


def _spam(sym, base):
    for i in range(_PER):
        cand(10, i % 60, i % 2 == 0, base + i * 0.01, base + i * 0.01 - 1.0,
             symbol=sym, ss=i)


_threads = [threading.Thread(target=_spam, args=(s, 100.0 + j * 50))
            for j, s in enumerate(_SYMS)]
for t in _threads:
    t.start()
for t in _threads:
    t.join(timeout=60)

_raw = [ln for ln in _LEDGER.read_text(encoding="utf-8").splitlines() if ln.strip()]
_ok_json = []
_bad = 0
for ln in _raw:
    try:
        _ok_json.append(json.loads(ln))
    except json.JSONDecodeError:
        _bad += 1
check("R02 volume: every line written by four threads is well formed JSON",
      _bad == 0, f"{_bad} torn lines of {len(_raw)}")
check("R02 volume: no observation is lost",
      len(_ok_json) == _PER * len(_SYMS),
      f"{len(_ok_json)} of {_PER * len(_SYMS)}")
_ids = [r.get("event_id") for r in _ok_json]
check("R02 volume: no duplicate id",
      len(set(_ids)) == len(_ids) and all(_ids),
      f"{len(set(_ids))} unique of {len(_ids)}")


# --------------------------------------------------------------------------
# 8. the lock must FAIL OPEN. A ledger hiccup can never break a read or an
#    alert, so a lock fault degrades to the old behaviour, never to a drop
#    and never to a stall.
# --------------------------------------------------------------------------
reset()


def _r02_fail_open():
    orig = getattr(forward_ledger, "_os_lock", None)

    def _boom(*a, **k):
        raise OSError("simulated lock failure")

    forward_ledger._os_lock = _boom
    try:
        t0 = time.monotonic()
        cid = cand(9, 50, True, 1.1020, 1.1008)
        secs = time.monotonic() - t0
    finally:
        if orig is not None:
            forward_ledger._os_lock = orig
    if not isinstance(cid, str) or not cid:
        return False, f"a lock fault dropped the observation (returned {cid!r})"
    if len(rows()) != 1:
        return False, f"{len(rows())} rows written"
    if secs > 2.0:
        return False, f"a lock fault stalled the recorder for {secs:.2f}s"
    return True, ""


guard("lock fail-open: a lock fault still records and still returns the id",
      _r02_fail_open)


# --------------------------------------------------------------------------
# 9. the published cohort must not move. Loosening the recorder is a
#    RECORDING change; the scoreboard denominator is a measurement and it
#    stays exactly where it was on rows that predate the selected flag.
# --------------------------------------------------------------------------
def _legacy_row(day, sym, direction, t, passes, hit04, sibling=True):
    entry, stop = 100.0, 99.0
    return {
        "event_id": None, "date": day, "time_et": t, "symbol": sym,
        "direction": direction, "entry": entry, "stop": stop, "risk": 1.0,
        "atr": 1.0,
        "targets": {"t04": 100.4, "t1": 101.0, "t2": 102.0, "liq": 103.0},
        "grade": "A", "score": 9, "passes": passes, "reasons": [],
        "gap_atr": 1.6, "hour_et": int(t[:2]), "sibling": sibling and passes,
        "outcome": {"stopped": not hit04, "mfe_r": 1.0,
                    "hit": {"t04": hit04, "t1": hit04, "t2": hit04,
                            "liq": hit04},
                    "graded_at": "2026-09-08 17:00"},
    }


def _legacy_fixture():
    """The shape of the rows already on the production volume: no selected
    key anywhere, at most one passing row per symbol, direction and day."""
    out = []
    for i, day in enumerate(("2026-09-01", "2026-09-02", "2026-09-03")):
        out.append(_legacy_row(day, "EURUSD=X", "BUY", "09:35:00", False, None))
        out.append(_legacy_row(day, "EURUSD=X", "BUY", "09:50:00", True,
                               i % 2 == 0))
        out.append(_legacy_row(day, "JPY=X", "SELL", "11:20:00", True,
                               i != 1))
        out.append(_legacy_row(day, "TSLA", "BUY", "13:05:00", False, None))
    return out


def _reference_scoreboard(records):
    """The cohort the code produced BEFORE this repair: every graded passing
    row. Under the old recorder that was exactly one row per symbol,
    direction and day, because the recorder refused any more."""
    return [r for r in records if r.get("outcome") and r.get("passes")]


reset()
_fix = _legacy_fixture()
_LEDGER.write_text("\n".join(json.dumps(r) for r in _fix) + "\n",
                   encoding="utf-8")
_ref = _reference_scoreboard(_fix)
_sb = forward_ledger.scoreboard()
check("cohort: legacy rows keep the published denominator exactly",
      _sb["n"] == len(_ref), f"{_sb['n']} vs reference {len(_ref)}")
_ref_wins = sum(1 for r in _ref if r["outcome"]["hit"]["t04"])
check("cohort: legacy rows keep the published tier counts exactly",
      _sb["tiers"]["t04"]["n"] == len(_ref)
      and _sb["tiers"]["t04"]["wins"] == _ref_wins,
      f"{_sb['tiers']['t04']} vs n={len(_ref)} wins={_ref_wins}")
check("cohort: legacy rows keep the sibling promotion denominator exactly",
      _sb["sibling"]["n"] == len([r for r in _ref if r.get("sibling")]),
      f"{_sb['sibling']['n']} vs {len([r for r in _ref if r.get('sibling')])}")


def _cohort_new_style():
    """A day recorded AFTER the repair: two passing observations, one of them
    actually broadcast. Only the broadcast one enters the cohort."""
    day = "2026-09-04"
    a = _legacy_row(day, "EURUSD=X", "BUY", "09:50:00", True, False)
    b = _legacy_row(day, "EURUSD=X", "BUY", "09:55:00", True, True)
    a.update({"event_id": "aaaa", "selected": False, "selected_at": None,
              "position_id": None})
    b.update({"event_id": "bbbb", "selected": True,
              "selected_at": "2026-09-04 09:55:07", "position_id": "pos-b"})
    newfix = _legacy_fixture() + [a, b]
    _LEDGER.write_text("\n".join(json.dumps(r) for r in newfix) + "\n",
                       encoding="utf-8")
    sb = forward_ledger.scoreboard()
    # the legacy days are untouched, and the new day adds exactly the one
    # observation that was actually broadcast
    if sb["n"] != len(_ref) + 1:
        return False, f"n={sb['n']}, expected {len(_ref) + 1}"
    if sb["tiers"]["t04"]["wins"] != _ref_wins + 1:
        return False, (f"wins={sb['tiers']['t04']['wins']}, expected "
                       f"{_ref_wins + 1}: the unselected observation leaked in")
    return True, ""


guard("cohort: on a repaired day only the SELECTED observation is counted",
      _cohort_new_style)


# --------------------------------------------------------------------------
# 10. one shared temp name is a second way two writers destroy each other
# --------------------------------------------------------------------------
reset()
_fl_src = (REPO / "forward_ledger.py").read_text(encoding="utf-8")
check("write: the publish temp is qualified per process and thread",
      "getpid()" in _fl_src and "get_ident()" in _fl_src,
      "every writer still shares one temp file name")
forward_ledger._write_all([{"event_id": "x", "date": "2026-09-08"}])
_orphans = [p.name for p in Path(_TMP).glob("sniper_forward*.tmp")]
check("write: no orphan temp file is left behind", not _orphans, str(_orphans))


# --------------------------------------------------------------------------
# 11. the docstring must describe what the id actually guarantees
# --------------------------------------------------------------------------
check("docs: the id docstring no longer promises a cross-restart collapse",
      "retry after a crash or a" not in _fl_src,
      "event_id still claims a retry a second later collapses")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all ledger integrity checks passed")
