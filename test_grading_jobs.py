"""W03: deterministic grading jobs.

Astra's work package W03, plus finding A21 which REVERSES something an earlier
round shipped. The required regression list is Astra's own, verbatim:

    "Missing data then recovery; per-symbol bad frame; write failure; retired
     historical row beside a valid row; six exhausted retries; restart mid-pass"

and the acceptance bar is:

    "Every due ID ends in a counted status; no successful grade before durable
     write; no cohort silently evicted"

The four requirements section 4 adds, each with its own block below:

  J1/J2  A21. "Extend the forward horizon only if the declared strategy horizon
         does. Do not extend the forward horizon to the evening while the live
         book closes at 16:00 and call the two comparable. Specify the
         instrument and strategy horizon before grading."

         A previous round made the outcome walk run to 23:59 of the row's own
         day so a EUR/USD move at 20:00 ET would count. The live sniper book
         settles every open position at the session close (sniper_book.SETTLE_ET
         and _settle_at), so that made the forward ledger measure a different
         trade from the one the bot actually runs. The horizon is now declared
         per instrument, before grading, and it is the session close.

         astra/grader_reconciliation.json also showed the live book and the
         backtest counting session-end exits on DIFFERENT denominators, which is
         why the recorded output now names its convention instead of leaving a
         reader to guess.

  J3     "distinguish job_complete from measurement_complete. A permanently
         unavailable row can be durably classified so it does not block every
         future job, while its outcome remains missing for research."

  J4     "Partition due IDs exactly once, persist the attempt identity and next
         eligible retry time."

  J5/J6  "keep the grading scheduler independent of AI billing, LEARN_ENABLED,
         and the intraday entry loop" and "Test a failed 16:12 pass whose retry
         occurs after the session loop ends, plus a restart between attempts.
         Retrying six times is ineffective if no caller remains scheduled to
         perform those retries."

         The real defect: maybe_grade_forward returned immediately on
         `now.time() < WEEKLY_AT`, so between midnight and 16:05 there was no
         caller at all, and forward_grade_session then rolled the key at the
         next close. A budget of six spaced retries was abandoned after one
         attempt, and the day was neither graded nor parked. A half day made it
         worse: the session closed at 13:00 and the key still named the
         PREVIOUS session until 16:05.

Measurement only. No strategy surface is touched: no sniper constant, no exit
threshold, no allow-list, no entry window, no symbol roster, and _walk_outcome's
tie rule is untouched. What changes is which bars the walk is allowed to see,
and that change makes the ledger agree with the live book instead of disagreeing
with it.

No network, no Telegram, no model API, no production storage. yfinance is
stubbed in sys.modules and the ledger lives in a temp dir.

Run:  python test_grading_jobs.py     (exit code 0 = all good)
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.

import json
import sys
import tempfile
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# an isolated data dir BEFORE config is imported, so nothing here can read or
# write the real runtime state
_TMP = tempfile.mkdtemp(prefix="kelbot_w03_")
_bot_test_os.environ["DATA_DIR"] = _TMP

import config          # noqa: E402
import forward_ledger  # noqa: E402
import market_calendar  # noqa: E402

ET = ZoneInfo("America/New_York")
REPO = Path(__file__).parent

DAY = "2026-09-08"            # a full session: closes 16:00 ET
DAY_D = date(2026, 9, 8)
HALF = "2026-11-27"           # the day after Thanksgiving: closes 13:00 ET
HALF_D = date(2026, 11, 27)

# The grading clock. The session AFTER the signal day, which is when a real
# deferred row is picked up.
NOW = datetime(2026, 9, 9, 17, 0, tzinfo=ET)
# 16:12 ET on the signal day: the exact tick the session loop hands back on.
AT_1612 = datetime(2026, 9, 8, 16, 12, tzinfo=ET)

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def guard(name, fn, detail=""):
    """Run a check that can RAISE on the un-repaired code (a missing function
    has no attributes), and record the raise as the failure it is instead of
    aborting the whole suite."""
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
check("the fixture days are real sessions, one full and one half",
      market_calendar.is_trading_day(DAY_D)
      and market_calendar.session_close(DAY_D) == market_calendar.REGULAR_CLOSE
      and market_calendar.is_trading_day(HALF_D)
      and market_calendar.session_close(HALF_D) == market_calendar.EARLY_CLOSE,
      f"{DAY} {HALF}")


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
    return forward_ledger.fill_outcomes(now_et=now or NOW)


# --------------------------------------------------------------------------
# yfinance stub: no network, ever
# --------------------------------------------------------------------------
import pandas as pd  # noqa: E402


def flat_bars(day=DAY, start_hh=10, n=12, hi=1.2000, lo=1.0000):
    """Wide bars: everything resolves on the first one."""
    idx = pd.date_range(f"{day} {start_hh:02d}:00", periods=n, freq="5min",
                        tz="America/New_York")
    return pd.DataFrame({"Open": [1.1] * n, "High": [hi] * n,
                         "Low": [lo] * n, "Close": [1.1] * n}, index=idx)


def fx_evening(day=DAY, wake_hour=20):
    """A 24h FX day for entry 1.1020 / stop 1.1008, so 0.4R is 1.10248.

    Quiet between the stop and the first target all the way through the equity
    close, then the whole ladder taken out at `wake_hour`. This is exactly the
    row A21 is about: nothing the LIVE BOOK could ever have traded happens
    after the close, because the live book settled at 16:00."""
    idx = pd.date_range(f"{day} 10:00", f"{day} 21:00", freq="5min",
                        tz="America/New_York")
    hi = [1.1050 if t.hour >= wake_hour else 1.1022 for t in idx]
    return pd.DataFrame({"Open": [1.1020] * len(idx), "High": hi,
                         "Low": [1.1015] * len(idx),
                         "Close": [1.1020] * len(idx)}, index=idx)


def half_day_afternoon(day=HALF):
    """A half day whose tape stops at 13:00 and whose FX quotes keep going.
    The target is only taken out at 14:00, an hour after the market shut."""
    idx = pd.date_range(f"{day} 10:00", f"{day} 16:00", freq="5min",
                        tz="America/New_York")
    hi = [1.1050 if t.hour >= 14 else 1.1022 for t in idx]
    return pd.DataFrame({"Open": [1.1020] * len(idx), "High": hi,
                         "Low": [1.1015] * len(idx),
                         "Close": [1.1020] * len(idx)}, index=idx)


class FakeYF(types.ModuleType):
    """Stands in for the yfinance module inside fill_outcomes. `plan` maps a
    symbol to a frame, to an Exception instance to raise, or to None for the
    empty-response case. `default` covers anything not named. `as_of` is what
    the provider can SEE yet."""

    def __init__(self, plan=None, default=None, as_of=None):
        super().__init__("yfinance")
        self.plan = plan or {}
        self.default = default if default is not None else flat_bars()
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


def identity_holds(res):
    """Astra's acceptance bar: every due ID lands in exactly one bucket."""
    return res["eligible"] == (res["graded"] + res["pending"]
                               + res["missing_data"] + res["failed_writes"]
                               + res["permanent_failures"])


# ==========================================================================
print("--- J1. A21: the instrument and its strategy horizon are DECLARED, "
      "before grading ---")
# ==========================================================================


def _j1_declares_instruments():
    decl = getattr(forward_ledger, "INSTRUMENTS", None)
    if not isinstance(decl, dict):
        return False, "forward_ledger declares no instrument table"
    import fvg
    missing = [s for s in fvg.SNIPER_SYMBOLS if s not in decl]
    return not missing, f"undeclared sniper instruments: {missing}"


guard("J1a every sniper instrument is declared before it is graded",
      _j1_declares_instruments)


def _j1_horizon_is_the_close():
    h = forward_ledger.declared_horizon("EURUSD=X", DAY_D)
    at = h["at"]
    return (at == datetime(2026, 9, 8, 16, 0, tzinfo=ET)
            and bool(h.get("basis")) and bool(h.get("policy"))
            and h.get("instrument") == "fx_spot"), str(h)


guard("J1b the declared FX horizon is the session close, not the evening",
      _j1_horizon_is_the_close)


def _j1_half_day():
    h = forward_ledger.declared_horizon("EURUSD=X", HALF_D)
    return h["at"] == datetime(2026, 11, 27, 13, 0, tzinfo=ET), str(h)


guard("J1c a half day's declared horizon is 13:00, not 16:00", _j1_half_day)


def _j1_matches_the_live_book():
    """The whole point of A21. If these two clocks differ, the forward ledger
    and the live book are not measuring the same trade."""
    import sniper_book
    out = []
    for d in (DAY_D, HALF_D):
        live = sniper_book._settle_at(datetime(d.year, d.month, d.day, 12,
                                               tzinfo=ET))
        research = forward_ledger.declared_horizon("EURUSD=X", d)["at"].time()
        out.append((d, live, research))
    bad = [o for o in out if o[1] != o[2]]
    return not bad, f"live settle vs research horizon differ: {bad}"


guard("J1d the declared research horizon IS the live book's settle clock",
      _j1_matches_the_live_book)


# --------------------------------------------------------------------------
# The behaviour A21 reverses: an FX target taken out at 20:00 ET is not a win,
# because the live book was flat three and a half hours earlier.
# --------------------------------------------------------------------------
reset()
cand(9, 50, 1.1020, 1.1008)
install_yf(FakeYF(default=fx_evening()))


def _j1_evening_move_is_not_a_win():
    res = grade()
    if not identity_holds(res):
        return False, f"identity broken: {res}"
    oc = (rows()[0].get("outcome") or {})
    hit = oc.get("hit") or {}
    if hit.get("t04") is True:
        return False, ("a move three and a half hours after the live book "
                       "settled was still counted as a target")
    return (res["graded"] == 1 and res["complete"] is True
            and hit.get("t04") is None), f"{res} hit={hit}"


guard("J1e a target only reached after the close is NOT counted as a win",
      _j1_evening_move_is_not_a_win)


def _j1_outcome_names_its_horizon():
    oc = rows()[0].get("outcome") or {}
    return (oc.get("horizon_et") == "2026-09-08 16:00"
            and oc.get("horizon_at") == "2026-09-08T16:00:00-04:00"
            and oc.get("instrument") == "fx_spot"
            and oc.get("terminal") == "horizon"
            and bool(oc.get("horizon_basis"))
            and bool(oc.get("horizon_policy"))), str(oc)


guard("J1f the graded row records the horizon it was graded to, and how it "
      "ended", _j1_outcome_names_its_horizon)


def _j1_mfe_stops_at_the_horizon():
    """MFE measured past the horizon is an excursion nobody could have taken.
    0.4R here is 1.10248 and the pre-close high is 1.1022, so the honest MFE
    is well under 0.2R."""
    oc = rows()[0].get("outcome") or {}
    return (oc.get("mfe_r") is not None and oc["mfe_r"] < 0.3), str(oc)


guard("J1g MFE is measured to the declared horizon, not to midnight",
      _j1_mfe_stops_at_the_horizon)


# a stop that really happened inside the session is still a stop
reset()
cand(9, 50, 1.1020, 1.1008)
install_yf(FakeYF(default=flat_bars()))


def _j1_intraday_still_grades():
    res = grade()
    oc = rows()[0].get("outcome") or {}
    return (res["graded"] == 1 and oc.get("terminal") == "stop"
            and oc.get("stopped") is True), f"{res} {oc}"


guard("J1h a level touched inside the session still grades exactly as before",
      _j1_intraday_still_grades)


# the half day: the tape stopped at 13:00, so a 14:00 FX move is not evidence
reset()
cand(10, 0, 1.1020, 1.1008, day=HALF)
install_yf(FakeYF(default=half_day_afternoon()))


def _j1_half_day_walk():
    res = grade(now=datetime(2026, 11, 30, 17, 0, tzinfo=ET))
    oc = rows()[0].get("outcome") or {}
    hit = oc.get("hit") or {}
    return (res["graded"] == 1 and hit.get("t04") is None
            and oc.get("horizon_et") == "2026-11-27 13:00"), f"{res} {oc}"


guard("J1i on a half day the walk stops at 13:00, so a 14:00 move is not "
      "evidence", _j1_half_day_walk)


# ==========================================================================
print("\n--- J2. the recorded output NAMES its denominator convention ---")
# ==========================================================================
# astra/grader_reconciliation.json: the live book reported targets over
# (targets + stops), the backtest reported targets over every row, and the two
# were compared as though they were the same number. Both are now published,
# each labelled, so no reader has to guess which one a figure came from.
reset()
_win = cand(9, 50, 1.1020, 1.1008, symbol="EURUSD=X", ss=1)
_horizon = cand(9, 55, 155.00, 154.00, symbol="JPY=X", ss=2)
forward_ledger.mark_selected(_win, fired_at_et=f"{DAY} 09:50:07",
                             position_id="pos-win")
forward_ledger.mark_selected(_horizon, fired_at_et=f"{DAY} 09:55:07",
                             position_id="pos-horizon")
install_yf(FakeYF(plan={
    "EURUSD=X": flat_bars(hi=1.2000, lo=1.1015),      # target, never stopped
    "JPY=X": flat_bars(hi=155.05, lo=154.50),         # neither level touched
}))
_j2_res = grade()


def _j2_two_conventions():
    sb = forward_ledger.scoreboard()
    t = sb["tiers"]["t04"]
    conv = t.get("conventions")
    if not isinstance(conv, dict):
        return False, f"the tier publishes no named convention: {t}"
    ro, ar = conv.get("resolved_only"), conv.get("all_rows")
    if not (isinstance(ro, dict) and isinstance(ar, dict)):
        return False, f"both conventions must be published: {conv}"
    if not (ro.get("formula") and ar.get("formula")):
        return False, f"each convention must state its formula: {conv}"
    return (ro["n"] == 1 and ro["wins"] == 1 and ro["pct"] == 100.0
            and ar["n"] == 2 and ar["wins"] == 1 and ar["pct"] == 50.0), \
        f"resolved_only={ro} all_rows={ar}"


guard("J2a both denominators are published, each naming its own formula",
      _j2_two_conventions)


def _j2_reconciles():
    t = forward_ledger.scoreboard()["tiers"]["t04"]
    conv = t["conventions"]
    return (conv["all_rows"]["n"]
            == conv["resolved_only"]["n"] + t["unresolved_at_horizon"]), str(t)


guard("J2b the two denominators reconcile through the horizon exits",
      _j2_reconciles)


def _j2_headline_says_which():
    sb = forward_ledger.scoreboard()
    t = sb["tiers"]["t04"]
    return (sb.get("convention") == "resolved_only"
            and bool(sb.get("convention_note"))
            and t["n"] == t["conventions"]["resolved_only"]["n"]
            and t["win_pct"] == t["conventions"]["resolved_only"]["pct"]), \
        f"convention={sb.get('convention')!r} t04={t}"


guard("J2c the headline number says which convention it is on", _j2_headline_says_which)


def _j2_horizon_declared_in_the_scoreboard():
    sb = forward_ledger.scoreboard()
    h = sb.get("horizon")
    return (isinstance(h, dict) and bool(h.get("policy"))
            and bool(h.get("basis"))), str(sb.get("horizon"))


guard("J2d the scoreboard declares the horizon its rows were graded to",
      _j2_horizon_declared_in_the_scoreboard)


def _j2_summary_states_it():
    txt = forward_ledger.nightly_summary()
    low = txt.lower()
    return ("session end" in low or "horizon" in low), repr(txt[:200])


guard("J2e the nightly line says how session-end rows were counted",
      _j2_summary_states_it)


# ==========================================================================
print("\n--- J3. job_complete is not measurement_complete ---")
# ==========================================================================
reset()
_stale = cand(9, 50, 1.1020, 1.1008, day="2026-08-09")   # far past the window
_fresh = cand(9, 50, 1.1020, 1.1008)
install_yf(FakeYF(default=flat_bars()))


def _j3_split():
    res = grade()
    if not identity_holds(res):
        return False, f"identity broken: {res}"
    if "job_complete" not in res or "measurement_complete" not in res:
        return False, f"the two completions are still one field: {res}"
    return (res["job_complete"] is True
            and res["measurement_complete"] is False
            and res["retired"] == 1 and res["graded"] == 1
            and res["complete"] is res["job_complete"]), str(res)


guard("J3a a retired row finishes the JOB and leaves the MEASUREMENT missing",
      _j3_split)


def _j3_missing_is_counted():
    res = grade()          # nothing left to do at all
    return (res["job_complete"] is True
            and res["measurement_complete"] is True
            and res["measurement_missing"] == 0), str(res)


guard("J3b a pass with nothing outstanding is complete on both meanings",
      _j3_missing_is_counted)


def _j3_pending_is_not_measured_yet():
    """A row whose horizon has not passed has not failed, but it has not been
    measured either. job_complete true, measurement_complete false."""
    reset()
    cand(9, 50, 1.1020, 1.1008)
    install_yf(FakeYF(default=fx_evening(), as_of=datetime(2026, 9, 8, 15, 0,
                                                           tzinfo=ET)))
    res = grade(now=datetime(2026, 9, 8, 15, 0, tzinfo=ET))
    return (res["pending"] == 1 and res["job_complete"] is True
            and res["measurement_complete"] is False), str(res)


guard("J3c a deferred row is job complete and measurement incomplete",
      _j3_pending_is_not_measured_yet)


# ==========================================================================
print("\n--- J4. every due ID is partitioned exactly once ---")
# ==========================================================================
# Astra's whole regression row in one fixture: missing data for one symbol, an
# unusable frame for a second, a retired historical row, and a valid row beside
# all of them.
reset()
_ids = {
    "ok": cand(9, 50, 1.1020, 1.1008, symbol="EURUSD=X", ss=1),
    "down": cand(9, 51, 155.0, 154.0, symbol="JPY=X", ss=2),
    "badframe": cand(9, 52, 300.0, 299.0, symbol="TSLA", ss=3),
    "retired": cand(9, 53, 500.0, 499.0, symbol="SPY", day="2026-08-09"),
}
# the cohort is the rows that were actually BROADCAST, so a fixture that wants
# to see a published number has to say the card went out
for _slot, _cid in _ids.items():
    forward_ledger.mark_selected(_cid, fired_at_et=f"{DAY} 09:50:07",
                                 position_id=f"pos-{_slot}")


class NaiveIndexFrame:
    """A frame whose index cannot be compared with an aware timestamp. This is
    the per-symbol bad frame: it raises inside the slice, one row's problem,
    and it must not cost the other symbols their outcomes."""

    def __init__(self):
        self.columns = ["Open", "High", "Low", "Close"]
        self.empty = False
        self.index = pd.date_range("2026-09-08 10:00", periods=3, freq="5min")

    def __getitem__(self, _mask):
        raise TypeError("cannot compare a naive index to an aware timestamp")


install_yf(FakeYF(plan={
    "EURUSD=X": flat_bars(),
    "JPY=X": RuntimeError("yfinance 429"),
    "TSLA": NaiveIndexFrame(),
    "SPY": flat_bars(),
}))
_j4 = grade()


def _j4_partition_exists():
    part = _j4.get("statuses")
    if not isinstance(part, dict):
        return False, f"the pass publishes no per-id partition: {sorted(_j4)}"
    return len(part) == _j4["eligible"], \
        f"{len(part)} statuses for {_j4['eligible']} due ids"


guard("J4a the pass reports one status per due id", _j4_partition_exists)


def _j4_each_id_once():
    part = _j4["statuses"]
    want = {_ids["ok"]: "graded", _ids["down"]: "missing_data",
            _ids["badframe"]: "missing_data", _ids["retired"]: "retired"}
    wrong = {k: (part.get(k), v) for k, v in want.items() if part.get(k) != v}
    return not wrong, f"id -> (got, wanted): {wrong}"


guard("J4b every due id lands in exactly the bucket it belongs to",
      _j4_each_id_once)


def _j4_counts_match_the_partition():
    part = _j4["statuses"]
    tally = {}
    for st in part.values():
        tally[st] = tally.get(st, 0) + 1
    return (identity_holds(_j4)
            and tally.get("graded", 0) == _j4["graded"]
            and tally.get("missing_data", 0) == _j4["missing_data"]
            and tally.get("retired", 0) == _j4["retired"]
            and _j4.get("partition_ok") is True), f"{tally} vs {_j4}"


guard("J4c the counts are the partition, not a second tally that can drift",
      _j4_counts_match_the_partition)


def _j4_bad_frame_did_not_evict_the_good_symbol():
    """Astra: no cohort silently evicted. One unusable frame used to unwind the
    whole pass; one download outage must not cost another symbol its row."""
    graded = [r for r in rows() if (r.get("outcome") or {}).get("hit")]
    return (len(graded) == 1
            and graded[0].get("symbol") == "EURUSD=X"), \
        f"graded symbols={[r.get('symbol') for r in graded]}"


guard("J4d one bad frame costs one row, never the whole pass",
      _j4_bad_frame_did_not_evict_the_good_symbol)


def _j4_retired_beside_valid():
    stale = [r for r in rows() if r.get("date") == "2026-08-09"][0]
    oc = stale.get("outcome") or {}
    sb = forward_ledger.scoreboard()
    return (bool(oc.get("retired")) and oc.get("hit") == {}
            and sb["n"] == 1), f"{oc} scoreboard n={sb['n']}"


guard("J4e a retired historical row is classified durably and never published",
      _j4_retired_beside_valid)


# write failure: no successful grade before a durable write
reset()
_w1 = cand(9, 50, 1.1020, 1.1008, symbol="EURUSD=X", ss=1)
_w2 = cand(9, 51, 155.0, 154.0, symbol="JPY=X", ss=2)
install_yf(FakeYF(default=flat_bars()))
_real_write = forward_ledger._write_all
forward_ledger._write_all = lambda records: False
_j4w = grade()
forward_ledger._write_all = _real_write


def _j4_write_failure():
    part = _j4w.get("statuses") or {}
    return (_j4w["graded"] == 0 and _j4w["failed_writes"] == 2
            and _j4w["complete"] is False
            and part.get(_w1) == "failed_write"
            and part.get(_w2) == "failed_write"
            and len(ungraded()) == 2), f"{_j4w} ungraded={len(ungraded())}"


guard("J4f a lost write leaves every id in failed_write, none in graded",
      _j4_write_failure)


def _j4_write_recovers():
    res = grade()
    part = res.get("statuses") or {}
    return (res["graded"] == 2 and res["complete"] is True
            and part.get(_w1) == "graded" and len(ungraded()) == 0), str(res)


guard("J4g the next pass persists what the lost write dropped",
      _j4_write_recovers)


# restart mid-pass: the process dies after the download and before the publish
reset()
_r1 = cand(9, 50, 1.1020, 1.1008)
install_yf(FakeYF(default=flat_bars()))


class _Boom(Exception):
    pass


def _die(records):
    raise _Boom("power cut between the walk and the publish")


forward_ledger._write_all = _die
try:
    grade()
except _Boom:
    pass
except Exception:
    pass
finally:
    forward_ledger._write_all = _real_write


def _j4_restart_mid_pass():
    if len(ungraded()) != 1:
        return False, "a crash before the publish still marked the row graded"
    res = grade()
    return (res["graded"] == 1 and res["complete"] is True
            and len(ungraded()) == 0), str(res)


guard("J4h a restart between the walk and the publish loses no row",
      _j4_restart_mid_pass)


# ==========================================================================
print("\n--- J5. the scheduler: attempt identity and next eligible retry "
      "time, persisted ---")
# ==========================================================================
import scanner  # noqa: E402

_src_scanner = (REPO / "scanner.py").read_text(encoding="utf-8-sig")
MAX_TRIES = scanner.Service.GRADE_MAX_ATTEMPTS
RETRY_S = scanner.Service.GRADE_RETRY_S
_SPACING = max(int(RETRY_S / 60) + 1, 1)


def fresh_service():
    svc = scanner.Service.__new__(scanner.Service)
    svc.dry = False
    for key in getattr(scanner.Service, "GRADE_STATE_KEYS",
                       ("forward_graded", "forward_grade_attention",
                        "forward_grade_last", "forward_grade_tries")):
        config.state_set(key, None)
    return svc


def tick(svc, when):
    scanner.Service.maybe_grade_forward(svc, when)


def job_record():
    rec = config.state_get("forward_grade_last")
    return rec if isinstance(rec, dict) else {}


reset()
cand(9, 50, 1.1020, 1.1008)
_dead = install_yf(FakeYF(plan={"EURUSD=X": RuntimeError("yfinance 429")}))
_svc = fresh_service()
tick(_svc, AT_1612)


def _j5_identity_persisted():
    rec = job_record()
    if not rec:
        return False, "the failed pass persisted no job record at all"
    for f in ("key", "attempt", "attempt_id", "next_eligible_at", "status"):
        if f not in rec:
            return False, f"the job record has no {f}: {rec}"
    return (rec["key"] == DAY and rec["attempt"] == 1
            and rec["status"] == "open"
            and str(rec["attempt_id"]).endswith("#1")), str(rec)


guard("J5a a failed pass persists its attempt identity and its status",
      _j5_identity_persisted)


def _j5_next_eligible_is_stored():
    rec = job_record()
    nxt = datetime.fromisoformat(rec["next_eligible_at"])
    return (nxt - AT_1612).total_seconds() == RETRY_S, \
        f"next_eligible_at={rec.get('next_eligible_at')} from {AT_1612}"


guard("J5b the NEXT ELIGIBLE RETRY TIME is stored, not recomputed from a "
      "wall clock", _j5_next_eligible_is_stored)


def _j5_spacing_respected():
    before = len(_dead.calls)
    tick(_svc, AT_1612 + timedelta(seconds=RETRY_S - 60))
    return len(_dead.calls) == before, "a retry ran before it was eligible"


guard("J5c a retry before its eligible time does not run", _j5_spacing_respected)


def _j5_restart_between_attempts():
    """A restart is a NEW Service reading the same durable state. The budget
    must neither reset (six retries forever) nor be double spent."""
    restarted = scanner.Service.__new__(scanner.Service)
    restarted.dry = False
    tick(restarted, AT_1612 + timedelta(seconds=RETRY_S))
    rec = job_record()
    return (rec.get("attempt") == 2 and rec.get("status") == "open"
            and str(rec.get("attempt_id")).endswith("#2")
            and len(_dead.calls) == 2), f"{rec} downloads={len(_dead.calls)}"


guard("J5d a restart between attempts resumes the same job, at the next "
      "attempt", _j5_restart_between_attempts)


# ==========================================================================
print("\n--- J6. a caller REMAINS SCHEDULED to perform the retries ---")
# ==========================================================================
# Astra: "Retrying six times is ineffective if no caller remains scheduled to
# perform those retries." The old gate returned on `now.time() < WEEKLY_AT`, so
# no tick between midnight and 16:05 could retry anything, and the session key
# then rolled at the next close and abandoned the budget mid-flight.
reset()
cand(9, 50, 1.1020, 1.1008)
_late = install_yf(FakeYF(plan={"EURUSD=X": RuntimeError("yfinance 429")}))
_svc = fresh_service()
_LATE_FIRST = datetime(2026, 9, 8, 23, 50, tzinfo=ET)   # a post-outage restart
tick(_svc, _LATE_FIRST)


def _j6_first_pass_ran():
    return len(_late.calls) == 1 and job_record().get("attempt") == 1, \
        f"downloads={len(_late.calls)} rec={job_record()}"


guard("J6a the late first pass runs and opens the job", _j6_first_pass_ran)


def _j6_retry_after_midnight():
    """The retry falls at 00:05 the next morning. Under the old wall-clock gate
    nothing between 00:00 and 16:05 could call the grader at all."""
    tick(_svc, _LATE_FIRST + timedelta(seconds=RETRY_S))
    return len(_late.calls) == 2, \
        f"downloads={len(_late.calls)}: the retry after midnight never ran"


guard("J6b a retry owed after midnight is actually performed",
      _j6_retry_after_midnight)


def _j6_budget_is_not_abandoned():
    """Keep ticking through the next day, including past the next session's
    close, when the session key rolls. The job must end in a COUNTED status,
    never simply vanish."""
    when = _LATE_FIRST + timedelta(seconds=RETRY_S)
    for _ in range(MAX_TRIES + 3):
        when = when + timedelta(seconds=RETRY_S)
        tick(_svc, when)
    rec = job_record()
    park = config.state_get("forward_grade_attention")
    if not isinstance(park, dict) or park.get("key") != DAY:
        return False, (f"the {DAY} job was abandoned rather than parked: "
                       f"record={rec} park={park}")
    return (rec.get("status") == "attention"
            and rec.get("attempt") == MAX_TRIES), f"{rec} park={park}"


guard("J6c an open job always ends in a counted status, never abandoned",
      _j6_budget_is_not_abandoned)


def _j6_parked_stops_spinning():
    """Still inside the parked key's own window (every one of these ticks is
    before the next session's close, so forward_grade_session still names the
    parked day)."""
    at = len(_late.calls)
    for i in range(6):
        tick(_svc, _LATE_FIRST + timedelta(hours=3 + i))
    return len(_late.calls) == at, \
        f"a parked day kept downloading: {len(_late.calls) - at} more"


guard("J6d a parked day stops re-attempting instead of spinning every tick",
      _j6_parked_stops_spinning)


def _j6_next_session_still_opens():
    """A parked day must not park the SCHEDULER. The next session that closes
    gets its own job, and its pass walks every ungraded row of every past date,
    so the parked day's rows are not evicted either."""
    at = len(_late.calls)
    tick(_svc, datetime(2026, 9, 9, 16, 12, tzinfo=ET))
    rec = job_record()
    return (len(_late.calls) > at and rec.get("key") == "2026-09-09"), \
        f"downloads={len(_late.calls) - at} rec={rec}"


guard("J6i a parked day does not park every later session too",
      _j6_next_session_still_opens)


# the 16:12 pass that fails and recovers on the retry AFTER the session loop
# has ended: Astra's named scenario, end to end through the real scheduler.
reset()
cand(9, 50, 1.1020, 1.1008)
_recover = install_yf(FakeYF(plan={"EURUSD=X": RuntimeError("yfinance 429")}))
_svc = fresh_service()
tick(_svc, AT_1612)
check("J6e the failed 16:12 pass does not claim the day",
      config.state_get("forward_graded") is None,
      f"forward_graded={config.state_get('forward_graded')!r}")
_recover.plan = {}
tick(_svc, AT_1612 + timedelta(seconds=RETRY_S))
check("J6f the retry after the session loop ends grades the row",
      len(ungraded()) == 0, f"ungraded={len(ungraded())}")
check("J6g and only then is the day recorded as graded",
      config.state_get("forward_graded") == DAY,
      f"forward_graded={config.state_get('forward_graded')!r}")


def _j6_done_status():
    return job_record().get("status") == "done", str(job_record())


guard("J6h a completed job is marked done, not left open", _j6_done_status)


def _j6_legacy_record_on_upgrade():
    """The record shape the RUNNING container has right now is {key, at}, with
    no status. Adopting one for a day state.json already records as graded
    would make every upgraded container run one extra pass, and an incomplete
    one would then park a day that was finished."""
    reset()
    cand(9, 50, 1.1020, 1.1008)
    yf = install_yf(FakeYF(default=flat_bars()))
    svc = fresh_service()
    config.state_set("forward_graded", DAY)
    config.state_set("forward_grade_last",
                     {"key": DAY, "at": AT_1612.isoformat()})
    tick(svc, AT_1612 + timedelta(hours=2))
    return len(yf.calls) == 0, \
        f"a settled day was re-opened from its legacy record: {yf.calls}"


guard("J6j a legacy job record for a day already graded is not re-opened",
      _j6_legacy_record_on_upgrade)


def _j6_legacy_record_still_owed():
    """The same shape for a day that is NOT settled keeps its budget and its
    spacing: an upgrade is not a reason to re-spend six attempts, nor to lose
    the ones already spent."""
    reset()
    cand(9, 50, 1.1020, 1.1008)
    yf = install_yf(FakeYF(plan={"EURUSD=X": RuntimeError("yfinance 429")}))
    svc = fresh_service()
    config.state_set("forward_grade_last",
                     {"key": DAY, "at": AT_1612.isoformat()})
    config.state_set("forward_grade_tries", {DAY: 5})
    tick(svc, AT_1612 + timedelta(seconds=60))       # inside the old spacing
    if yf.calls:
        return False, "the adopted job ignored the spacing it inherited"
    tick(svc, AT_1612 + timedelta(seconds=RETRY_S))  # now it is owed
    rec = job_record()
    return (len(yf.calls) == 1 and rec.get("attempt") == 6
            and rec.get("status") == "attention"), \
        f"downloads={len(yf.calls)} rec={rec}"


guard("J6k a legacy record for an unsettled day keeps its budget and spacing",
      _j6_legacy_record_still_owed)


# ==========================================================================
print("\n--- J7. independent of AI billing, LEARN_ENABLED and the entry "
      "loop ---")
# ==========================================================================
_saved = {k: _bot_test_os.environ.get(k) for k in ("LEARN_ENABLED", "API_MODE")}
try:
    _bot_test_os.environ["LEARN_ENABLED"] = "false"
    _bot_test_os.environ["API_MODE"] = "off"
    check("J7a the fixture really has the paid review and all AI spend off",
          not config.learn_enabled() and not config.api_allows("scheduled")[0])
    reset()
    cand(9, 50, 1.1020, 1.1008)
    install_yf(FakeYF(default=flat_bars()))
    _svc = fresh_service()
    tick(_svc, AT_1612)
    check("J7b grading still runs with every paid switch off",
          len(ungraded()) == 0 and config.state_get("forward_graded") == DAY,
          f"ungraded={len(ungraded())} "
          f"graded={config.state_get('forward_graded')!r}")
finally:
    for k, v in _saved.items():
        if v is None:
            _bot_test_os.environ.pop(k, None)
        else:
            _bot_test_os.environ[k] = v


def _j7_source_is_independent():
    src = _src_scanner
    start = src.index("def maybe_grade_forward")
    end = src.index("def maybe_holiday_notice", start)
    body = src[start:end]
    banned = [w for w in ("learn_enabled", "LEARN_ENABLED", "api_allows",
                          "assistant") if w in body]
    return not banned, f"the grading job still references {banned}"


guard("J7c the grading job's own code names no AI switch", _j7_source_is_independent)


def _j7_half_day_key():
    """The entry loop shuts at 13:12 on a half day. The grading key must name
    the session that just closed, not the previous one, or the day that closed
    early cannot even be identified until 16:05."""
    at = datetime(2026, 11, 27, 13, 12, tzinfo=ET)
    got = scanner.forward_grade_session(at)
    return got == HALF_D, f"key at 13:12 on a half day is {got}, wanted {HALF_D}"


guard("J7d a half day's grading key opens at ITS close, not at 16:05",
      _j7_half_day_key)


def _j7_full_day_key_unchanged():
    return (scanner.forward_grade_session(AT_1612) == DAY_D
            and scanner.forward_grade_session(NOW) == NOW.date()), \
        f"{scanner.forward_grade_session(AT_1612)}"


guard("J7e a full session's key is unchanged", _j7_full_day_key_unchanged)


def _j7_session_loop_hands_off():
    """A one-shot `python scanner.py` runs run_session and exits. If the
    session-over branch never grades, that process collects evidence all day
    and grades none of it."""
    src = _src_scanner
    start = src.index("    def run_session(self):")
    end = src.index("    def daemon(self):", start)
    return "self.maybe_grade_forward(now)" in src[start:end], \
        "run_session never hands the closed session to the grader"


guard("J7f the session loop hands the closed session to the grading job",
      _j7_session_loop_hands_off)


def _j7_daemon_still_calls_it():
    return "self.maybe_grade_forward(now)" in _src_scanner, "no daemon caller"


guard("J7g the daemon still calls the grading job", _j7_daemon_still_calls_it)


# ==========================================================================
print("\n--- J8. missing data, then recovery, through the real scheduler ---")
# ==========================================================================
reset()
_j8a = cand(9, 50, 1.1020, 1.1008, symbol="EURUSD=X", ss=1)
_j8b = cand(9, 51, 155.0, 154.0, symbol="JPY=X", ss=2)
for _i, _cid in enumerate((_j8a, _j8b)):
    forward_ledger.mark_selected(_cid, fired_at_et=f"{DAY} 09:50:07",
                                 position_id=f"pos-j8-{_i}")
_flaky = install_yf(FakeYF(plan={"EURUSD=X": RuntimeError("yfinance 503"),
                                 "JPY=X": RuntimeError("yfinance 503")},
                           default=flat_bars()))
_svc = fresh_service()
tick(_svc, AT_1612)
check("J8a a total outage grades nothing and claims nothing",
      len(ungraded()) == 2 and config.state_get("forward_graded") is None,
      f"ungraded={len(ungraded())}")

_flaky.plan = {"JPY=X": RuntimeError("yfinance 503")}   # half of it recovers
tick(_svc, AT_1612 + timedelta(seconds=RETRY_S))
check("J8b a partial recovery grades what it can and keeps the job open",
      len(ungraded()) == 1 and config.state_get("forward_graded") is None,
      f"ungraded={len(ungraded())}")

_flaky.plan = {}
tick(_svc, AT_1612 + timedelta(seconds=RETRY_S * 2))
check("J8c full recovery grades the rest and closes the job",
      len(ungraded()) == 0 and config.state_get("forward_graded") == DAY,
      f"ungraded={len(ungraded())} "
      f"graded={config.state_get('forward_graded')!r}")


def _j8_no_cohort_evicted():
    sb = forward_ledger.scoreboard()
    return sb["n"] == 2, f"scoreboard n={sb['n']}, expected both rows"


guard("J8d neither row was evicted by the outage", _j8_no_cohort_evicted)


print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all W03 grading-job checks passed")
