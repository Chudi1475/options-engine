"""Forward evidence ledger for the FVG sniper: the bot's self-teaching memory.

Every sniper candidate that forms live gets recorded here with the exact
ticket (entry/stop) and what EVERY target tier would pay: the verified 0.4R,
1R, 2R, and the structural liquidity target. At the end of each day the
outcomes are filled in from the day's bars. Over weeks this builds the
honest, walk-forward dataset that either EARNS bigger targets a win-rate
claim or proves they don't pay, and it tracks the round-6 "sibling" config
(gap >= 1.1 ATR and entries >= 08:00 ET, 81.1% on 53 OOS trades) toward its
pre-committed promotion bar: >= 60 forward trades AND a Wilson 95% lower
bound above the 71.4% breakeven for 0.4R all-out.

Nothing here changes live behavior. It watches, records, grades, and tells
the owner when the evidence clears the bar. Fully guarded: a ledger hiccup
must never break a read or an alert.
"""

import contextlib
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import storage_io

ET = ZoneInfo("America/New_York")
LEDGER = config.DATA_DIR / "sniper_forward.jsonl"

# ---------------------------------------------------------------------------
# serialising the read-modify-write
# ---------------------------------------------------------------------------
# One lock, from storage_io, covering the whole read-modify-write rather than
# just the publish at the end of it. It layers a per-file in-process RLock
# under an OS lock on a sidecar beside the ledger, so the threads in this
# process (the sniper watch, the brain reply workers, the main loop) and a
# SECOND process on the same volume (a coach run, an ad-hoc script) are both
# serialised, and it releases on process death because the OS holds it.
#
# What changed and why: the old version computed
# `outermost = got and depth == 0`, so when the in-process RLock timed out no
# OS lock was attempted AT ALL and the whole-file rewrite then ran completely
# unserialised, with one warning. Failing open on the LOCK is not the same
# thing as failing open on the WRITE. The contract that a ledger hiccup never
# breaks a read or an alert is now kept by handing the caller a lock it can
# see is unheld: readers carry on, and _write_all refuses to truncate.
_LOCK_BUDGET_S = 0.75                  # never a latency source on a live scan
_LOCK_WARNED = [False]


def _lock_path():
    """The sidecar, derived from the CURRENT LEDGER value. Tests repoint
    LEDGER at a temp dir, so this can never be captured at import.

    name + '.lock', not with_suffix: with_suffix turned sniper_forward.jsonl
    into sniper_forward.lock, a name every sibling with the same stem would
    have collided on."""
    return storage_io.lock_path(LEDGER)


def _warn_once(why: str):
    if not _LOCK_WARNED[0]:
        _LOCK_WARNED[0] = True
        print(f"forward_ledger: ledger lock UNAVAILABLE ({why}). Reads carry "
              "on; a whole-file rewrite will refuse until this is resolved.")


@contextlib.contextmanager
def _locked():
    """Serialise a ledger read-modify-write, and SAY whether it worked.

    Yields a storage_io.Lock. Check .held if what you are about to do
    truncates the file; a pure read may proceed either way, which is what
    keeps a lock hiccup from breaking a scan or dropping an observation."""
    with storage_io.file_lock(LEDGER, budget_s=_LOCK_BUDGET_S) as lk:
        if not lk.held:
            _warn_once(lk.why)
        yield lk


# how far back the grading download can actually reach. ONE definition on
# purpose: the period string handed to yfinance and the age a row has to pass
# before it can be retired are built from the same number, because two copies
# of "5" drifting apart is exactly how a row the provider can no longer serve
# stayed retryable forever.
GRADE_LOOKBACK_DAYS = 5
GRADE_PERIOD = f"{GRADE_LOOKBACK_DAYS}d"

# pre-committed promotion rule for the round-6 sibling (edge_lessons round 6):
SIBLING_MIN_TRADES = 60
BREAKEVEN_04R = 71.4          # percent needed for 0.4R all-out to break even
SIBLING_GAP_MIN_ATR = 1.1
SIBLING_MIN_HOUR_ET = 8


def wilson_lb(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson 95% lower bound on a win rate, in percent. 0 when n == 0."""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return 100.0 * (centre - margin) / denom


def _read_all_status():
    """The ledger, plus whether it could actually be READ. Returns
    (records, read_ok).

    Only the grader looks at the flag, and it needs it because an OSError
    swallowed to [] is indistinguishable from an empty or a fully graded
    ledger. That single ambiguity is what let a day whose file could not be
    opened get written off as finished.

    The read runs through storage_io now, which underneath tells UNREADABLE
    (the OS would not hand the file over) apart from CORRUPT (a line the
    parser could not read). The flag keeps its old meaning on purpose: a
    missing file and a file with a torn last line are both readable states
    that return the rows they do have, and only a file nobody could open
    reports False."""
    res = storage_io.read_jsonl(LEDGER)
    if res.status == "unreadable":
        return [], False
    return list(res.value or []), True


def _read_all() -> list:
    """Best-effort view of the ledger, [] when it cannot be read. Every reader
    but the grader wants exactly that tolerance, so the signature and the
    behaviour here are unchanged."""
    return _read_all_status()[0]


def _standing_by() -> bool:
    """True when this copy lost the single-instance lease.

    the ledger is on a shared volume, and the OS lock above only serialises
    writers, it does not say which of them is allowed to write. a mute copy
    marking a row selected claims an alert the winner never sent, and its
    grading pass rewrites rows the winner still owns. imported lazily on
    purpose: this module records evidence and must not grow an import-time
    dependency on the transport."""
    try:
        import telegram
        return bool(telegram.standby()[0])
    except Exception:  # no transport loaded at all: nothing is standing by
        return False


def _write_all(records: list):
    """Publish the whole ledger. Call this INSIDE _locked(): it truncates and
    replaces, so anything appended since `records` was read is gone.

    Returns whether the bytes landed, and REFUSES rather than publishing when
    the ledger lock cannot be taken. The old version had no retry at all, so
    the WinError 5 that hits this machine about one run in ten lost a whole
    grading pass; storage_io retries a bounded number of times and reports a
    full disk as a status. The caller already counts a False as a failed
    write, so nothing downstream needs to change to notice."""
    if _standing_by():
        return False  # not this copy's file to write; see _standing_by
    if not storage_io.held(LEDGER):
        # the publish below takes the lock for itself either way, but a caller
        # that did not hold it across its own READ has already lost anything
        # appended in between, and that is worth saying out loud
        print(f"forward_ledger: {LEDGER.name} is being rewritten without the "
              "ledger lock held across the read; concurrent appends may be lost")
    res = storage_io.write_jsonl_all(LEDGER, records)
    if not res.ok:
        print(f"forward_ledger: could NOT persist {LEDGER.name}: "
              f"{res.status} {res.error}")
    return bool(res.ok)


def _audit_path():
    """The grading audit log, beside the ledger. Derived from the CURRENT
    LEDGER value for exactly the reason _lock_path is: tests repoint LEDGER at
    a temp dir, and a path captured at import would write into the real data
    dir. The sniper_ prefix keeps it in the same family as the ledger so
    anything that cleans one cleans the other."""
    return LEDGER.with_name(LEDGER.stem + "_grading.jsonl")


def _audit(res: dict):
    """Append one line per grading pass, carrying that pass's whole count set.

    This is the thing that makes a grading number quotable: nothing anywhere
    hand-types how many rows were graded, it gets read back from here. Wrapped
    and silent-ish on failure because losing the log must never turn a good
    pass into a bad one, and it never touches the result it was handed."""
    try:
        line = {"ts": f"{datetime.now(ET):%Y-%m-%d %H:%M:%S}"}
        line.update(res)
        # under the audit file's OWN lock, not the ledger's: two files, two
        # locks, and this module never pretends the pair is one transaction
        out = storage_io.append_jsonl(_audit_path(), line)
        if not out.ok:
            print("forward_ledger: could not append the grading audit line: "
                  f"{out.status} {out.error}")
    except OSError as e:
        print(f"forward_ledger: could not append the grading audit line: {e}")


def event_id(day, symbol, direction, time_et, entry, stop) -> str:
    """A stable identity for ONE candidate observation, derived from that
    observation's own facts.

    What it actually guarantees, stated precisely because the old wording
    here promised more than the hash delivers: the stamp folded into the hash
    is the read clock to the second, so the same observation recorded twice
    within the same second collapses to one id and one row. A re-read a
    second later, or a read repeated after a restart, is a DIFFERENT
    observation and gets its own id and its own row. That is the honest
    reading of the record: the bot did look twice.

    The linkage this id exists for is the explicit one. The read hands the id
    to the alert path, the alert path stores it on the position and calls
    mark_selected, and the graded outcome is joined by the same id, so
    nothing downstream has to match an alert to an observation by guessing on
    the day. Do not change what is hashed: rows already on the volume carry
    ids built from exactly these fields."""
    import hashlib
    raw = f"{day}|{symbol}|{direction}|{time_et}|{entry}|{stop}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def candidate_id_for_ticket(symbol, direction, ticket, price, now_et) -> str:
    """The id record_candidate WOULD allocate for this observation.

    One definition, called from two places on purpose. The reader used to keep
    the id only when record_candidate handed one back, so a refused ledger
    write produced a ticket that could never be linked to anything: the card
    went out carrying no candidate id at all. Minting it here lets the reader
    stamp the ticket first and hand the SAME id to the recorder, so a failed
    write costs an observation row and not the identity of the alert.

    The values are handed to event_id RAW, exactly as record_candidate has
    always handed them over. Converting them to float here would change the
    hash and orphan every id already on the volume."""
    t = ticket or {}
    return event_id(f"{now_et:%Y-%m-%d}", symbol, direction,
                    f"{now_et:%H:%M:%S}", t.get("entry", price), t.get("stop"))


def _row_key(r: dict) -> str:
    """The identity of a stored row. Rows written before event_id shipped do
    not carry one, and they are the rows sitting on the live volume, so their
    identity is recomputed from the same fields the id is built from."""
    eid = r.get("event_id")
    if eid:
        return str(eid)
    try:
        return event_id(r.get("date"), r.get("symbol"), r.get("direction"),
                        r.get("time_et"), r.get("entry"), r.get("stop"))
    except (TypeError, ValueError):
        return ""


def record_candidate(symbol: str, direction: str, price: float, atr: float,
                     ticket: dict, conf: dict, passes: bool, reasons: list,
                     gap_atr=None, hour_et=None, now_et: datetime = None,
                     candidate_id: str = None):
    """Log one live sniper candidate and RETURN its id. Called from the read
    path; must be fast and can never raise.

    Returns the candidate id whenever a row for this observation is in the
    ledger, whether it was just written or was already there from a retry
    inside the same second. Returns None only when there is nothing to record
    at all. The caller keeps that id, carries it onto the alert and the
    position, and hands it back to mark_selected, which is the only thing that
    makes an alert and its observation joinable rather than guessed at.

    This recorder holds NO opinion about how many entries a day permits. It
    used to: any existing passing row for (date, symbol, direction) made it
    return early, so a passing read at 09:50 silently erased a different
    passing observation at 09:55, and the erased one was not even kept as a
    reject. That is an entry rule, and an entry rule does not belong in a
    recorder. It lives in exactly one place now, the scanner's per-day alert
    key plus sniper_book's one-open-per-symbol check, and this ledger reflects
    it through the explicit `selected` flag that mark_selected sets after an
    alert has actually gone out. Passed the strategy checks and selected for
    broadcast are two different facts and are recorded as two different
    fields."""
    try:
        if not ticket or not direction:
            return None
        if _standing_by():
            # an observation appended by a copy that is not allowed to alert on
            # it is still a row on the winner's volume, and the nightly grading
            # would carry it. record nothing while mute. see _standing_by.
            return None
        now = now_et or datetime.now(ET)
        day = f"{now:%Y-%m-%d}"
        stamp = f"{now:%H:%M:%S}"
        # the caller may already have minted the id and stamped it on the
        # ticket, which is the only way an alert stays linkable when this write
        # fails. One derivation either way, so the two can never drift.
        eid = candidate_id or candidate_id_for_ticket(
            symbol, direction, ticket, price, now)
        entry = float(ticket.get("entry", price))
        stop = float(ticket.get("stop", 0))
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        sign = 1 if direction == "BUY" else -1
        liq = ticket.get("target_liquidity") or (conf or {}).get(
            "target_liquidity")
        rec = {
            "event_id": eid,          # stable identity, links alert -> outcome
            "date": day, "time_et": stamp,
            "symbol": symbol, "direction": direction,
            "entry": round(entry, 6), "stop": round(stop, 6),
            "risk": round(risk, 6), "atr": round(float(atr or 0), 6),
            "targets": {
                "t04": round(entry + sign * 0.4 * risk, 6),
                "t1": round(entry + sign * 1.0 * risk, 6),
                "t2": round(entry + sign * 2.0 * risk, 6),
                "liq": round(float(liq), 6) if liq else None,
            },
            "grade": (conf or {}).get("grade"),
            "score": (conf or {}).get("score"),
            "passes": bool(passes),
            "reasons": list(reasons or [])[:6],
            "gap_atr": round(float(gap_atr), 3) if gap_atr is not None else None,
            "hour_et": int(hour_et) if hour_et is not None else now.hour,
            "sibling": bool(
                passes
                and (gap_atr is None or gap_atr >= SIBLING_GAP_MIN_ATR)
                and (hour_et if hour_et is not None else now.hour)
                >= SIBLING_MIN_HOUR_ET),
            "outcome": None,  # filled by fill_outcomes() after the close
            # passed the strategy checks is NOT the same fact as went out as
            # an alert. mark_selected() sets these, after the card was sent.
            "selected": False,
            "selected_at": None,
            "position_id": None,
        }
        with _locked():
            for r in _read_all():
                if _row_key(r) == eid:
                    return eid   # this exact observation is already recorded
            # an append, not a rewrite: it adds a line and destroys nothing, so
            # it still lands when the lock could not be taken across the dedup
            # read above. Losing an observation is the one failure this module
            # is not allowed to have.
            storage_io.append_jsonl(LEDGER, rec)
        return eid
    except Exception:
        return None


class MarkResult:
    """What mark_selected actually did, in one word.

    A bool could only ever say "not linked", and the three ways that happens
    need different answers: a MISSING row is an observation that was never
    recorded and never will be, so it is an orphan a person has to look at; a
    REFUSED write is a disk problem that a retry fixes; APPLIED is done. Replay
    has to tell them apart or it retries the unfixable forever and gives up on
    the fixable. Still truthy exactly when the link landed, so the existing
    caller's `if not ...` reads the same as before."""

    APPLIED = "applied"
    ROW_MISSING = "row_missing"
    WRITE_REFUSED = "write_refused"
    STANDING_BY = "standing_by"

    def __init__(self, status: str):
        self.status = status

    def __bool__(self) -> bool:
        return self.status == self.APPLIED

    def __repr__(self) -> str:
        return f"MarkResult({self.status})"


def mark_selected(cid: str, fired_at_et=None, position_id=None,
                  decision_id=None, intent_id=None,
                  delivery_confirmed=None) -> "MarkResult":
    """Record that ONE observation is the one the bot actually broadcast.

    `selected` means the strategy committed its decision. `delivery_confirmed`
    means Telegram returned an acknowledgment. Astra is explicit that these are
    two fields and not one: a queued send is not confirmed delivery, and a
    delivered card whose acknowledgment was lost is neither confirmed nor
    unsent. Nothing here ever infers delivery from the existence of a position.

    `fired_at_et` is the alert clock, deliberately kept separate from the row's
    time_et: time_et is the READ clock the outcome walk is anchored on, and
    moving it would move already published numbers."""
    try:
        if not cid:
            return MarkResult(MarkResult.ROW_MISSING)
        if _standing_by():
            # a mute copy sent no card, so it has no selection to record. it
            # said so once, in the shared file the winner reads. see
            # _standing_by.
            return MarkResult(MarkResult.STANDING_BY)
        if isinstance(fired_at_et, datetime):
            stamp = f"{fired_at_et:%Y-%m-%d %H:%M:%S}"
        else:
            stamp = str(fired_at_et) if fired_at_et is not None else None
        with _locked():
            records = _read_all()
            hit = False
            for r in records:
                if _row_key(r) == cid:
                    r["selected"] = True
                    r["selected_at"] = stamp
                    r["position_id"] = position_id
                    if decision_id is not None:
                        r["decision_id"] = decision_id
                    if intent_id is not None:
                        r["intent_id"] = intent_id
                    if delivery_confirmed is not None:
                        r["delivery_confirmed"] = bool(delivery_confirmed)
                    else:
                        r.setdefault("delivery_confirmed", False)
                    hit = True
            if not hit:
                return MarkResult(MarkResult.ROW_MISSING)
            # report the WRITE, not the in-memory edit. returning True on a
            # refused write told the caller the broadcast was linked when the
            # row on disk still says it was not, and _cohort only counts rows
            # carrying selected=True, so a card that really went out would be
            # missing from the forward win rate for good. the caller logs a
            # False, which is the only signal anyone gets that the link is
            # gone.
            return MarkResult(MarkResult.APPLIED if _write_all(records)
                              else MarkResult.WRITE_REFUSED)
    except Exception:
        return MarkResult(MarkResult.WRITE_REFUSED)


def unlinked_selected() -> list:
    """Rows the bot says it broadcast that carry no position. The orphan report
    reads this: a selected row with no position is a card that went out and
    then lost its trade, which is exactly what the journal exists to catch."""
    out = []
    for r in _read_all():
        if r.get("selected") and not r.get("position_id"):
            out.append({"candidate_id": _row_key(r), "date": r.get("date"),
                        "symbol": r.get("symbol"),
                        "direction": r.get("direction")})
    return out


def _walk_outcome(rec: dict, bars) -> dict:
    """Walk the day's bars after entry time: which level hit first, per tier.
    Same-bar stop+target = stop first (the honest, conservative call the
    round-4 artifact hunt taught us). Returns the outcome dict."""
    sign = 1 if rec["direction"] == "BUY" else -1
    entry, stop = rec["entry"], rec["stop"]
    risk = rec["risk"]
    tiers = {k: v for k, v in rec["targets"].items() if v is not None}
    hit = {k: None for k in tiers}   # True=win, False=stopped before target
    stopped = False
    mfe_r = 0.0
    for _, b in bars.iterrows():
        hi, lo = float(b["High"]), float(b["Low"])
        best = (hi - entry) if sign > 0 else (entry - lo)
        mfe_r = max(mfe_r, best / risk)
        stop_hit = lo <= stop if sign > 0 else hi >= stop
        for k, tgt in tiers.items():
            if hit[k] is not None:
                continue
            tgt_hit = hi >= tgt if sign > 0 else lo <= tgt
            if stop_hit:          # conservative: stop wins any tie
                hit[k] = False
            elif tgt_hit:
                hit[k] = True
        if stop_hit:
            stopped = True
            break
        if all(v is not None for v in hit.values()):
            break
    # anything not resolved by the close: mark by where price ended vs entry
    return {"stopped": stopped, "mfe_r": round(mfe_r, 3),
            "hit": hit, "graded_at": f"{datetime.now(ET):%Y-%m-%d %H:%M}"}


def _window_end(start: datetime) -> datetime:
    """The last minute of the row's OWN day: the edge the outcome walk stops
    at. Defined once because two callers depend on it now, the bar slice and
    the question of whether the outcome can be called final yet, and two
    copies of this boundary drifting apart would freeze rows early again."""
    return start.replace(hour=23, minute=59)


def _outcome_is_final(oc: dict, window_end: datetime,
                      now_et: datetime) -> bool:
    """Can this outcome still move if more bars arrive?

    Only two things settle it before the day is over, and both are properties
    of the walk itself: it stops at the stop, and it stops once every tier has
    resolved. From either state a later bar is never read, so freezing there
    is not early, it is finished. Anything else is only settled once the row's
    own window has closed.

    This is the whole 16:12 problem. Two symbols in the sniper roster are 24h
    FX and the download asks for prepost, so bars keep printing long after the
    equity close, and fill_outcomes only ever walks rows whose outcome is
    still None. Whatever the close-time pass wrote was frozen for good: a
    winner that resolved in the evening was dropped as undecided while a loser
    that stopped at lunch was kept, which quietly biases the recorded win rate
    down. A row graded late is fine. A row graded early is wrong."""
    if oc.get("stopped"):
        return True
    hit = oc.get("hit") or {}
    if hit and all(v is not None for v in hit.values()):
        return True
    return now_et > window_end


def _frame_start(df):
    """The earliest bar the provider ACTUALLY returned, or None when the frame
    cannot say. This is the real edge of the download window, read off the
    response instead of assumed, so it stays honest if the provider ever
    changes what it serves for a period of GRADE_PERIOD."""
    try:
        ts = df.index.min()
    except Exception:
        return None
    if ts is None or getattr(ts, "tzinfo", None) is None:
        return None
    return ts


def _out_of_reach(window_end: datetime, frame_start, now_et: datetime) -> bool:
    """True when the data source can no longer reach this row's day. That is
    a PERMANENT condition, not a hiccup: the window only ever moves forward.

    Both halves have to agree. The frame the provider just returned has to
    start after the row's whole day, so no bar in it can ever fall inside the
    row's window, AND the row has to be older than the window the download
    asks for. Either half alone would be wrong: a truncated response on a bad
    day would retire rows that are still perfectly reachable tomorrow, and the
    calendar on its own would retire a row the provider is still happy to
    serve."""
    if frame_start is None:
        return False
    if frame_start <= window_end:
        return False
    return (now_et.date() - window_end.date()).days > GRADE_LOOKBACK_DAYS


def _retired_outcome(reason: str, now_et: datetime) -> dict:
    """The marker a row gets when it can never be graded.

    Written INTO the row, so the retirement is auditable on the volume rather
    than being a line in a log nobody keeps, and shaped like an outcome so
    every reader keeps working. `hit` is empty, so no tier can count it, and
    `retired` is what the scoreboard filters on so it can never enter a
    published number."""
    return {"retired": True, "reason": reason, "hit": {},
            "stopped": None, "mfe_r": None,
            "retired_at": f"{now_et:%Y-%m-%d %H:%M}"}


def _is_retired(r: dict) -> bool:
    oc = r.get("outcome")
    return bool(isinstance(oc, dict) and oc.get("retired"))


def _grading_result(eligible=0, graded=0, unresolved=0, pending=0,
                    missing_data=0, failed_writes=0, permanent_failures=0,
                    retired=0, read_ok=True) -> dict:
    """Build one pass's counts, audit them, hand them back.

    The two derived fields are computed HERE and nowhere else. A caller that
    could set complete by hand is precisely how a pass that graded nothing got
    recorded as a finished day, so there is no way to pass one in:

        retryable_failures = missing_data + failed_writes + (unreadable ledger)
        complete           = read_ok and retryable_failures == 0

    complete is true with permanent_failures above zero on purpose. A row that
    cannot be parsed today cannot be parsed tomorrow either, so counting it as
    a reason to retry would hold the day open forever. `retired` partitions
    permanent_failures: those are the rows whose day the download can no
    longer reach.

    complete is also true with `pending` above zero, and that one is worth
    saying out loud. A row whose own day has not closed yet has not failed at
    all. Holding the day open for it would burn the entire retry budget
    between the close and midnight and park a perfectly healthy day as
    needs-attention, and the scheduler will not even look again before the
    next session's gate. The next pass walks every ungraded row of every past
    date, so a deferred row is graded then. Late, not wrong."""
    retryable = missing_data + failed_writes + (0 if read_ok else 1)
    res = {
        "eligible": eligible,
        "graded": graded,
        "unresolved": unresolved,
        "pending": pending,
        "missing_data": missing_data,
        "failed_writes": failed_writes,
        "retryable_failures": retryable,
        "permanent_failures": permanent_failures,
        "retired": retired,
        "read_ok": bool(read_ok),
        "complete": bool(read_ok) and retryable == 0,
    }
    _audit(res)
    return res


def _gradable(r: dict):
    """Can this row's OWN fields be interpreted at all? Returns the parsed
    entry timestamp, or raises.

    Validating before the walk is the whole permanent-versus-retryable line. A
    truncated or hand-edited row fails identically on every future pass, so it
    has to be called permanent and left out of the retry budget; everything
    else that goes wrong (a provider that raised, an empty frame, a walk that
    blew up unexpectedly) is retryable and keeps the day open. The old code
    ran both through one `except Exception: continue` and could tell neither
    apart, nor tell the caller about either."""
    start = datetime.strptime(f"{r['date']} {r['time_et']}",
                              "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
    if not r["symbol"]:
        raise ValueError("row carries no symbol to fetch bars for")
    if not r["direction"]:
        raise ValueError("row carries no direction")
    if not isinstance(r["targets"], dict):
        raise TypeError("row carries no target map")
    float(r["entry"])
    float(r["stop"])
    if float(r["risk"]) <= 0:
        raise ValueError("row carries a non-positive risk")
    if not _row_key(r):
        raise ValueError("row has no derivable identity to join an outcome to")
    return start


def fill_outcomes(now_et: datetime = None) -> dict:
    """EOD: grade every ungraded candidate from today's (or any past) bars.

    `now_et` is the clock the pass runs on, defaulting to the real one. It is
    a parameter because two of the decisions below are decisions about TIME:
    whether a row's outcome can be called final yet, and whether the download
    can still reach its day at all. The scheduler hands its own tick down so
    the grader and the job that called it can never disagree about when it is.

    Returns the pass's COUNTS, not a bare number. It used to return an int,
    and seven separate faults (an unreadable ledger, a missing dependency, a
    download that raised, an empty response, a frame with no bars yet, a row
    that will never parse, a lost write) all returned 0, which is also what a
    fully graded day returns. The scheduler could not tell any of them from
    success, so it recorded the day as graded on every one, and its own dedup
    guard then made the retry it was built to allow unreachable.

    Every eligible row now lands in exactly one bucket, and the buckets
    reconcile on every return:

        eligible == graded + pending + missing_data + failed_writes
                    + permanent_failures

    `unresolved` partitions `graded` rather than joining that sum: a finished
    row whose tier never resolved is still a finished row. `retired`
    partitions `permanent_failures` the same way. `complete` is the only field
    a scheduler may claim a day on, and it is derived."""
    now = now_et or datetime.now(ET)
    with _locked():
        records, read_ok = _read_all_status()
    if not read_ok:
        return _grading_result(read_ok=False)
    todo = [r for r in records if r.get("outcome") is None]
    eligible = len(todo)
    if not eligible:
        return _grading_result()
    try:
        import pandas as pd  # noqa: F401
        import yfinance as yf
    except ImportError as e:
        # retryable: the container can come back with the dependency present,
        # and nothing about these rows is wrong
        print(f"forward_ledger: grading needs pandas and yfinance ({e})")
        return _grading_result(eligible=eligible, missing_data=eligible)

    permanent_failures = 0
    missing_data = 0
    pending = 0
    walkable = []                      # (row, entry timestamp)
    for r in todo:
        try:
            walkable.append((r, _gradable(r)))
        except (ValueError, TypeError, KeyError) as e:
            permanent_failures += 1
            print(f"forward_ledger: row {r.get('event_id') or '?'} can never "
                  f"be graded ({e}); counted permanent, not retried")

    by_symbol = {}
    for r, start in walkable:
        by_symbol.setdefault(r["symbol"], []).append((r, start))

    outcomes = {}
    retired = {}                       # row key -> the ungradable marker
    walked = 0
    for symbol, recs in by_symbol.items():
        try:
            df = yf.download(symbol, period=GRADE_PERIOD, interval="5m",
                             prepost=True, progress=False, auto_adjust=False)
            if df is None or df.empty:
                raise ValueError("empty response")
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
            if getattr(df.index, "tz", None) is not None:
                df.index = df.index.tz_convert(ET)
        except Exception as e:
            # a whole symbol's rows used to vanish here without a word
            missing_data += len(recs)
            print(f"forward_ledger: no bars for {symbol} ({e}); "
                  f"{len(recs)} row(s) left for the next pass")
            continue
        frame_start = _frame_start(df)
        for r, start in recs:
            day_end = _window_end(start)
            if _out_of_reach(day_end, frame_start, now):
                # the day has fallen out of the download window, which only
                # moves forward, so no future pass can reach it either. this
                # used to land in missing_data, which is RETRYABLE, so one row
                # from an outage older than GRADE_PERIOD held every later day
                # open forever. permanent, and stamped on the row so the
                # retirement is auditable rather than silent.
                retired[_row_key(r)] = _retired_outcome(
                    f"no bars available: {r.get('date')} is older than the "
                    f"{GRADE_PERIOD} the grader can download", now)
                permanent_failures += 1
                print(f"forward_ledger: {symbol} {r.get('date')} "
                      f"{r.get('time_et')} is past the {GRADE_PERIOD} "
                      "download window; retiring it as ungradable instead of "
                      "retrying it forever")
                continue
            try:
                bars = df[(df.index > start) & (df.index <= day_end)]
            except Exception as e:
                # the ONE statement in this loop that used to sit outside every
                # try. a frame the vendor hands back with a naive or non
                # datetime index raises right here, and because nothing caught
                # it the whole pass unwound and threw away the outcomes already
                # walked for every OTHER symbol. one bad frame is one row's
                # problem, so it is retryable and local, like every other fault
                # in this loop.
                missing_data += 1
                print(f"forward_ledger: unusable frame for {symbol} "
                      f"{r.get('time_et')} ({e}); left for the next pass")
                continue
            if bars.empty:
                # the signal day has no bars yet (grading the same evening on
                # a delayed feed): retryable, the next pass gets them
                missing_data += 1
                continue
            try:
                oc = _walk_outcome(r, bars)
            except Exception as e:
                # the row itself already validated, so this is unexpected and
                # therefore retryable, not permanent
                missing_data += 1
                print(f"forward_ledger: walk failed for {symbol} "
                      f"{r.get('time_et')} ({e}); left for the next pass")
                continue
            if not _outcome_is_final(oc, day_end, now):
                # the walk ran out of bars with tiers still open and the row's
                # day is still running. writing this would freeze it, because
                # nothing ever re-walks a row that has an outcome. leave it.
                pending += 1
                continue
            outcomes[_row_key(r)] = oc
            walked += 1

    # Merge on write, never publish the snapshot. The download above takes
    # seconds to minutes, and the recorder appends throughout it from three
    # other threads. Republishing the pre-download snapshot truncated the file
    # back to it and destroyed every one of those observations: atomic replace
    # protects the bytes of one write and does nothing about a concurrent
    # update. The lock is NOT held across the download, or the sniper watch
    # would block for the whole grading pass.
    applied = []
    stamped = []                       # retirements, published the same trip
    with _locked():
        live = _read_all()
        for r in live:
            if r.get("outcome") is not None:
                continue
            key = _row_key(r)
            oc = outcomes.get(key)
            if oc:
                r["outcome"] = oc
                applied.append(r)
                continue
            marker = retired.get(key)
            if marker:
                r["outcome"] = marker
                stamped.append(r)
        # exactly one publish per pass, and its RETURN VALUE decides the
        # graded count. Counting off the in-memory mutation reported a lost
        # write as a graded day while every row on disk stayed ungraded.
        wrote = _write_all(live) if (applied or stamped) else True
    graded = len(applied) if wrote else 0
    failed_writes = walked - graded
    unresolved = 0
    if wrote:
        for r in applied:
            hit = (r.get("outcome") or {}).get("hit") or {}
            if any(v is None for v in hit.values()):
                unresolved += 1
    # a retirement that could not be published is still permanent, and it is
    # not also a failed write: it stays ungraded on disk and the next pass
    # retires it again. counting it twice would break the identity above.
    return _grading_result(eligible=eligible, graded=graded,
                           unresolved=unresolved, pending=pending,
                           missing_data=missing_data,
                           failed_writes=failed_writes,
                           permanent_failures=permanent_failures,
                           retired=len(retired))


def _cohort(records: list) -> list:
    """The observations that count as the day's entries.

    The recorder no longer caps a day, so `passes` alone would inflate the
    denominator behind the pre-committed promotion rule, and that would be a
    selection change on already-observed data rather than the recording change
    this is meant to be. Two eras therefore have to agree:

    - a row that CARRIES the `selected` key was written after the alert link
      existed, so it counts exactly when it says it was broadcast.
    - a row with no `selected` key at all predates that link. Every row on the
      live volume is in this state. Those keep the rule the old cap enforced
      implicitly: the first passing observation per symbol and direction, so
      the published numbers do not move by a single trade.

    The era is a property of the ROW, not of the date, and that distinction is
    the whole repair here. Deciding it per date, off the mere presence of the
    key on any row, meant the first row a new build wrote (selected False,
    because nothing had been broadcast yet) flipped its whole date into
    selected-only mode and evicted that day's real, graded, already-broadcast
    trade. Every date on the volume holds both shapes on the day a build like
    that ships, so that was not an edge case, it was the deploy.

    Mixing the two on one date cannot double count either: the flagged rows go
    in first and seed the per symbol and direction cap, so an unflagged row is
    only kept for a pair the broadcast record does not already cover.

    Retired rows are dropped BEFORE the cap is seeded, not after. Filtering
    them downstream in scoreboard was not enough: a retired row still claimed
    its (symbol, direction) pair here, the legacy graded row for that same pair
    was skipped as already covered, and then scoreboard dropped the retired row,
    so the pair left the published numbers altogether. Retiring a row the
    grader could no longer reach was quietly moving the forward win rate, which
    is the one thing _retired_outcome promises it cannot do."""
    by_date = {}
    for r in records:
        if _is_retired(r):
            continue
        by_date.setdefault(r.get("date"), []).append(r)
    out = []
    for _day, rows in by_date.items():
        seen = set()
        for r in rows:
            if "selected" not in r:
                continue
            if r.get("selected") and r.get("passes"):
                seen.add((r.get("symbol"), r.get("direction")))
                out.append(r)
        for r in rows:
            if "selected" in r or not r.get("passes"):
                continue
            key = (r.get("symbol"), r.get("direction"))
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
    return out


def scoreboard() -> dict:
    """Aggregate the forward record per tier + the sibling promotion check."""
    # a retired row carries an outcome so the grader stops retrying it, but it
    # is evidence of nothing: it must reach neither a numerator nor a
    # denominator here. see _retired_outcome.
    records = [r for r in _cohort(_read_all())
               if r.get("outcome") and not _is_retired(r)]
    tiers = {"t04": "0.4R (the live, verified target)",
             "t1": "1R (needs >50 of 100 to beat 0.4R)",
             "t2": "2R (needs >33 of 100 to beat 0.4R)",
             "liq": "structure target (the big one)"}
    out = {"n": len(records), "tiers": {}, "sibling": None}
    for k, label in tiers.items():
        graded = [r for r in records
                  if r["outcome"]["hit"].get(k) is not None]
        wins = sum(1 for r in graded if r["outcome"]["hit"][k])
        n = len(graded)
        out["tiers"][k] = {
            "label": label, "n": n, "wins": wins,
            "win_pct": round(100 * wins / n, 1) if n else None,
            "wilson_lb": round(wilson_lb(wins, n), 1) if n else None,
        }
    sib = [r for r in records if r.get("sibling")]
    sib_graded = [r for r in sib if r["outcome"]["hit"].get("t04") is not None]
    wins = sum(1 for r in sib_graded if r["outcome"]["hit"]["t04"])
    n = len(sib_graded)
    lb = wilson_lb(wins, n)
    out["sibling"] = {
        "n": n, "wins": wins,
        "win_pct": round(100 * wins / n, 1) if n else None,
        "wilson_lb": round(lb, 1),
        "bar": {"min_trades": SIBLING_MIN_TRADES,
                "wilson_lb_needed": BREAKEVEN_04R},
        "promote": n >= SIBLING_MIN_TRADES and lb > BREAKEVEN_04R,
    }
    return out


def nightly_summary() -> str:
    """One short plain-language block for the nightly digest. Empty string
    when there is nothing new to say."""
    sb = scoreboard()
    if not sb["n"]:
        return ""
    lines = [f"Sniper forward record: {sb['n']} live signals graded so far."]
    t = sb["tiers"]
    for k in ("t04", "t1", "t2", "liq"):
        s = t[k]
        if s["n"]:
            lines.append(f"- {s['label']}: wins {s['wins']} of {s['n']}"
                         f" ({s['win_pct']:.0f} of 100)")
    sib = sb["sibling"]
    if sib and sib["n"]:
        need = SIBLING_MIN_TRADES - sib["n"]
        if sib["promote"]:
            lines.append(
                f"PROMOTION READY: the stricter sibling config hit "
                f"{sib['wins']} of {sib['n']} with a safety-adjusted floor of "
                f"{sib['wilson_lb']:.0f} of 100 (bar: {BREAKEVEN_04R:.0f}). "
                "Tell Chudi to flip it live.")
        else:
            lines.append(
                f"Sibling config (stricter entries): {sib['wins']} of "
                f"{sib['n']} so far; needs {max(need, 0)} more trades and a "
                f"{BREAKEVEN_04R:.0f}+ safety floor before it can take over.")
    return "\n".join(lines)
