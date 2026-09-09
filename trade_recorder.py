"""W06: the observation recorder. What the bot saw, written down while it can
still be written down.

WHY THIS EXISTS
---------------
This is the only part of the system that can ever answer whether the entry has
an edge, because it is the only part that records what would have happened on
the roads not taken.

Astra finding A15, in one sentence: MFE recorded only until the current exit is
censored. Once a 0.4R exit closes the observation, nothing that happens
afterwards can appear in the record, so "live MFE never reached 2R" is not
evidence against a larger target. It is evidence that nobody was looking. This
module keeps looking, on the chosen contract AND on the predeclared controls,
through one common end time, after the trade closes.

Astra finding A12: a put against a call tests DIRECTION. A different SPY call
tests STRIKE SELECTION. Those are two different questions, so the roles are
named separately and the selection rule for each is fixed in advance, in code,
where it can be read before the data arrives.

astra/RECORDER_SCHEMA.md is the contract. It was written before this file so
that this file could be held to it rather than describe itself afterwards.
Record types 1, 2, 3, 4 and 6 live here. Record 5, the manual fill journal, is
W07 and is deliberately absent.

WHAT THIS IS NOT
----------------
It is not a promise of every market quote. It records every sample the bot
actually observed, at the cadence it actually polled, and it records the gaps.
Fifteen second polling cannot see an intrasecond spike, so no field here is
named for a high that was reached: they are named for a high that was OBSERVED.
An unobserved high is not a booked profit.

It is not allowed to matter to trading. The recorder is an OBSERVER. Producers
never block: a full queue increments a durable loss counter, downgrades
completeness and drops the sample. If this module ever delays a stop it has
done more harm than the evidence is worth (Astra section 5).

It never opens a file itself. storage_io is the write protocol for this repo
and a sixth way to write a file is exactly what W05 removed.

    python -c "import trade_recorder as t; print(t.report_text())"
"""

import hashlib
import json
import os
import queue
import statistics
import threading
import time as _time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import config
import market_calendar
import storage_io

SCHEMA_VERSION = 1
ET = ZoneInfo("America/New_York")

# The bounded queue. Bounded on purpose: an unbounded one turns a slow disk
# into unbounded memory and then into a killed process, which is a worse way
# to lose the same samples. Sized for roughly one full session of every
# contract under observation, so only a genuinely stuck writer saturates it.
QUEUE_MAX = int(os.environ.get("RECORDER_QUEUE_MAX", "4000"))

# How long a contract under observation may go without a sample before the
# silence is written down as a gap. Three missed passes at the default sampler
# cadence, so one slow poll is not reported as an outage.
GAP_AFTER_S = float(os.environ.get("RECORDER_GAP_AFTER_S", "180"))

# The sampler's own cadence. It is NOT the monitoring cadence and must never
# become it: the monitoring loop hands over observations it already made for
# free, and this thread is what pays for the extra contracts.
SAMPLE_SECONDS = float(os.environ.get("RECORDER_SAMPLE_SECONDS", "60"))

# Retention used by the capacity estimate. Astra section 5: the volume is
# 500 MB and journal retention is currently unbounded, so somebody has to be
# able to multiply the four numbers out.
RETENTION_DAYS = int(os.environ.get("RECORDER_RETENTION_DAYS", "90"))

# Contract roles. chosen, direction_control and a NAMED strike_control are the
# schema's three. selection_pool is the fourth and it is an addition, not a
# replacement: Astra requires that every quote used to choose among
# alternatives is recorded, and those quotes are samples with a role.
CHOSEN = "chosen"
DIRECTION_CONTROL = "direction_control"
STRIKE_CONTROL = "strike_control"
SELECTION_POOL = "selection_pool"

# Cash settled, European exercise. Everything else is treated as an equity or
# ETF option. This is a CONVENTION, not a vendor contract master, and the
# metadata row says so in metadata_source.
INDEX_UNDERLYINGS = ("SPX", "NDX", "RUT", "VIX", "XSP")

_CAVEAT = ("Sampled at a fixed cadence. A high between two samples was never "
           "observed, so this is not a booked profit and an intrasecond spike "
           "is not captured.")

# Reason CODES, expanded here once instead of a sentence repeated on every row.
# The rule is that an unknown carries a reason; it is not that the reason has to
# be a paragraph. At the measured row size, prose on every sample costs about a
# tenth of a 500 MB volume for text that is identical on millions of rows.
REASON_CODES = {
    "no_provider_timestamp":
        "the provider gave no quote timestamp for this row. A yahoo option "
        "chain carries none at all and the feed is documented as roughly 15 "
        "minutes delayed",
    "no_two_sided_quote":
        "there was no bid and ask for this contract on this pass",
    "no_chain_row":
        "the chain returned no row for this contract on this pass",
    "chain_read_failed":
        "the provider call for this expiry raised, so nothing was observed "
        "for any contract on it this pass",
    "no_underlying_price":
        "the feed gave no underlying price this cycle, so the bot could not "
        "price the contract at all",
    "stale_mark_carried":
        "no live option price this cycle; the bot carried its last known mark "
        "and this sample is not a fresh observation",
    "no_trigger_sample":
        "no sample was named as the trigger for this exit, so it cannot be "
        "re derived from the record",
    "no_delivery_record":
        "no delivery record was attached to this event",
    "no_decision_id":
        "the strategy made no commitment on this observation, so there is no "
        "decision to name",
    "no_tick_size":
        "the minimum increment depends on the series and the price band and "
        "this bot has no contract master to read it from",
    "no_monitor_pass_yet":
        "monitoring has not reported a completed pass yet",
    "selection_window_no_row":
        "inside the selection window but the chain returned no row for it",
    "coverage_unmeasurable":
        "the record carries no sample or open time, so its coverage cannot be "
        "measured",
}


def explain(reason) -> str:
    """One reason field, in words. Codes stay in the rows and the sentences
    live here, so a reader never has to guess and a volume never pays for the
    same sentence a million times."""
    if not reason:
        return ""
    return "; ".join(REASON_CODES.get(c, c) for c in str(reason).split())

# ---------------------------------------------------------------------------
# state. All of it in one place so _reset_for_test is one function and cannot
# forget half of it.
# ---------------------------------------------------------------------------
_LOCK = threading.RLock()
_Q = queue.Queue(maxsize=QUEUE_MAX)
_WRITER = [None]          # the dedicated writer thread, or None
_STOP = threading.Event()
_SAMPLER = [None]
_SAMPLER_STOP = threading.Event()
_DRAIN_LOCK = threading.Lock()

_COUNTS = {}
_CONTRACTS = {}           # contract_id -> metadata row already written
_CANDIDATES = {}          # candidate key -> {"id", "revision", "fingerprint"}
_SEQ = {}                 # contract_id -> next sample_sequence
_LAST = {}                # contract_id -> {"provider_at", "received_at", "sha"}
_OBS = {}                 # (candidate_id, contract_id) -> observation row
_PENDING = {}             # (candidate_id, underlying, expiry) -> control request
_MONITOR_OK = [None]      # last time monitoring completed a pass
_LAST_SAMPLE_AT = [None]
_BYTES = [0]
_DROP_SAID = [0]
_PERSIST = [0.0, ()]      # last coverage write: monotonic stamp, counters seen
_RESUMED = [False]
_UNDER = {}               # symbol -> (price, at_utc) last seen BY MONITORING
_DIRSIZE = [0.0, 0]       # cached directory size: monotonic stamp, bytes

# How many listed strikes either side of the chosen one count as "the
# alternatives the rule chose among". Bounded on purpose: an SPX expiry lists
# thousands of strikes and writing every one of them as a selection pool row
# would spend the 500 MB volume on quotes no rule could ever have picked.
SELECTION_POOL_WIDTH = int(os.environ.get("RECORDER_POOL_WIDTH", "10"))

# How often the durable coverage file may be rewritten. A duration, so it uses
# the monotonic clock: a container clock that jumps must change no decision.
COVERAGE_PERSIST_S = 30.0


def _now_s() -> float:
    """A DURATION clock. Monotonic on purpose: a container whose wall clock
    jumps must not change a retry interval or a throttle window."""
    return _time.monotonic()


def _zero_counts():
    return {
        "scan_evaluations": 0, "candidate_opportunities": 0,
        "candidate_revisions": 0, "samples_recorded": 0,
        "samples_missing": 0, "duplicate_samples": 0,
        "out_of_order_samples": 0, "dropped_samples": 0,
        "write_failures": 0, "provider_errors": 0, "gaps": 0,
        "events_recorded": 0, "bytes_written": 0,
    }


_COUNTS.update(_zero_counts())


# ---------------------------------------------------------------------------
# clocks and paths
# ---------------------------------------------------------------------------
def _utc_now():
    return datetime.now(timezone.utc)


def _utc_iso(dt=None):
    return (dt or _utc_now()).isoformat()


def _et_today() -> str:
    """The ET calendar day a row belongs to. ET and not UTC, because every
    other day key in this bot is an ET date, and two notions of today on one
    volume is how a row stops matching its own session."""
    return f"{datetime.now(ET):%Y-%m-%d}"


def recorder_dir() -> Path:
    """DATA_DIR/recorder, made on demand. Read from config every call on
    purpose: the tests repoint DATA_DIR and a path captured at import time
    would write into the real runtime state."""
    d = config.DATA_DIR / "recorder"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass          # the write itself reports the failure, with a status
    return d


def path_for(kind: str, day=None) -> Path:
    """Where one record type lands. The four high volume types are partitioned
    by ET day so a rotation can drop a day of path archive and keep the compact
    event and coverage records, which Astra section 5 requires."""
    if kind in ("candidates", "samples", "events", "health"):
        return recorder_dir() / f"{kind}-{day or _et_today()}.jsonl"
    if kind == "contracts":
        return recorder_dir() / "contracts.jsonl"
    if kind == "coverage":
        return recorder_dir() / "coverage.json"
    if kind == "observations":
        return recorder_dir() / "observations.json"
    raise ValueError(f"unknown record kind {kind!r}")


def read_records(kind: str, day=None) -> list:
    """Every row of one record type. With no day, every day on the volume, so
    a study reading a multi day cohort does not have to know the file layout.

    A file the parser could not fully read returns the parseable prefix, which
    is real evidence, and storage_io has already counted the damage."""
    if kind in ("coverage", "observations"):
        res = storage_io.read_json(path_for(kind))
        return [res.value] if res.usable and res.value is not None else []
    paths = []
    if kind == "contracts" or day is not None:
        paths = [path_for(kind, day)]
    else:
        paths = sorted(recorder_dir().glob(f"{kind}-*.jsonl"))
    out = []
    for p in paths:
        res = storage_io.read_jsonl(p)
        out.extend(res.value or [])
    return out


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------
def _new_id(prefix: str) -> str:
    """An id with a letter prefix and no separator, deliberately. A bare digit
    run in a record is indistinguishable from a chat id to anything scanning
    these files for a leaked recipient, and the point of an opaque id is that
    nobody has to squint at it."""
    return prefix + uuid.uuid4().hex[:14]


def contract_id_for(underlying: str, right: str, strike: float,
                    expiry_date) -> str:
    """One stable name for one contract, OCC shaped: underlying, expiry, right,
    strike in thousandths. Stable across restarts, which is what lets metadata
    be stored ONCE and referenced from every tick."""
    d = expiry_date if isinstance(expiry_date, date) else \
        date.fromisoformat(str(expiry_date))
    return (f"{str(underlying).upper()}{d:%y%m%d}{str(right).upper()[:1]}"
            f"{int(round(float(strike) * 1000)):08d}")


def _parse_contract_id(cid: str):
    """underlying, right, strike, expiry from the id. Used only as a fallback
    when the metadata table has not been loaded, so an observation resumed
    after a restart still knows which chain to read."""
    try:
        i = 0
        while i < len(cid) and cid[i].isalpha():
            i += 1
        under, rest = cid[:i], cid[i:]
        d = date(2000 + int(rest[0:2]), int(rest[2:4]), int(rest[4:6]))
        right = rest[6]
        strike = int(rest[7:15]) / 1000.0
        return under, right, strike, d.isoformat()
    except (ValueError, IndexError):
        return None, None, None, None


def cluster_id_for(session_date: str, input_bar_end_utc, direction) -> str:
    """The unit the preregistered study analyses on.

    SPX and SPY co-fire in the same minute on the same read. Without this,
    42 correlated pairs get counted as 84 independent observations, which is
    the exact error the whole study design exists to avoid. Same session, same
    bar, same direction is ONE cluster."""
    raw = f"{session_date}|{input_bar_end_utc}|{direction}"
    return "clu" + _sha(raw)[:12]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def common_horizon_utc(session_date, last_trading_at_utc=None) -> str:
    """The common end time every contract in one comparison is observed to.

    Declared here, in advance, and identical for the chosen contract and every
    control: the closing bell of the session the decision was made in, clamped
    to the contract's own last trading time. Fixed in code rather than chosen
    per trade, because a horizon picked after the fact is how a comparison
    gets its answer from its own scope.

    A position carried past that session has its later path OUTSIDE this
    declared horizon. That is a stated limitation of the declared comparison,
    not a hidden gap: the coverage record says so, and the following session
    opens its own window."""
    d = session_date if isinstance(session_date, date) else \
        date.fromisoformat(str(session_date))
    close = market_calendar.session_close(d)
    end = datetime.combine(d, close, tzinfo=ET).astimezone(timezone.utc)
    if last_trading_at_utc:
        try:
            other = datetime.fromisoformat(str(last_trading_at_utc))
            if other < end:
                end = other
        except (TypeError, ValueError):
            pass
    return end.isoformat()


# ---------------------------------------------------------------------------
# the queue. Everything above the writer is a producer and NONE of it blocks.
# ---------------------------------------------------------------------------
def _bump(name, n=1):
    with _LOCK:
        _COUNTS[name] = _COUNTS.get(name, 0) + n


def _enqueue(kind: str, record: dict) -> bool:
    """Hand one row to the writer, or lose it loudly.

    put_nowait and never put. A blocking put is the single line that would let
    a slow disk delay an exit, and Astra's constraint on this module is that it
    must never do that. Saturation increments a durable loss counter, is logged
    where the platform can see it even when the disk is the problem, and
    downgrades evidence completeness."""
    try:
        _Q.put_nowait((kind, record))
        return True
    except queue.Full:
        _bump("dropped_samples")
        with _LOCK:
            n = _COUNTS["dropped_samples"]
            say = n == 1 or n - _DROP_SAID[0] >= 500
            if say:
                _DROP_SAID[0] = n
        if say:
            print(f"recorder: queue full, {n} records lost so far. Evidence "
                  "completeness is degraded for this session.")
        return False


def start():
    """Start the dedicated writer, and resume whatever this volume was still
    observing. Idempotent, so a caller that is not sure whether the daemon
    already started it can just call it."""
    with _LOCK:
        first = not _RESUMED[0]
        _RESUMED[0] = True
    if first:
        # A restart is a hole in the path. Resuming here, before the first
        # sample of the new life, is what makes the hole a recorded gap
        # instead of silence that later reads as full coverage.
        _resume_state()
    with _LOCK:
        if _WRITER[0] is not None and _WRITER[0].is_alive():
            return
        _STOP.clear()
        t = threading.Thread(target=_writer_loop, name="recorder-writer",
                             daemon=True)
        _WRITER[0] = t
        t.start()


def stop(timeout: float = 5.0):
    """Stop the writer and drain what is still queued, within a bound.

    Bounded on purpose. A shutdown that waits forever on a wedged disk is the
    same outage as a blocking producer, one process lifetime later."""
    _STOP.set()
    t = _WRITER[0]
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    _WRITER[0] = None
    drain_once(budget_s=timeout)
    _persist_coverage(force=True)
    _save_observations()


def _writer_loop():
    while not _STOP.is_set():
        try:
            drain_once(block_s=0.5)
        except Exception as e:                                 # noqa: BLE001
            # the writer thread is the last line. It may not die, because a
            # dead writer turns every later sample into a silent loss.
            _bump("write_failures")
            print(f"recorder: writer error, continuing: {e}")


def drain_once(block_s: float = 0.0, budget_s: float = 30.0) -> int:
    """Write everything currently queued. Returns the number of rows written.

    NEVER call this from the monitoring path. It is the writer's own body, and
    the tests call it to make a queue deterministic. The producer side is
    record_* and only record_*."""
    if not _DRAIN_LOCK.acquire(timeout=max(0.0, budget_s)):
        return 0
    try:
        batch = []
        if block_s > 0:
            try:
                batch.append(_Q.get(timeout=block_s))
            except queue.Empty:
                return 0
        while True:
            try:
                batch.append(_Q.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return 0
        return _write_batch(batch)
    finally:
        _DRAIN_LOCK.release()


def _write_batch(batch) -> int:
    """One append per target file, not one per row.

    Rows are encoded individually so ONE unencodable record is counted and
    dropped rather than taking the whole batch with it."""
    by_path = {}
    for kind, rec in batch:
        try:
            line = json.dumps(rec)
        except (TypeError, ValueError) as e:
            _bump("write_failures")
            print(f"recorder: a {kind} row could not be encoded and was "
                  f"dropped: {e}")
            continue
        by_path.setdefault(path_for(kind), []).append(line)
    written = 0
    for path, lines in by_path.items():
        text = "\n".join(lines) + "\n"
        res = storage_io.append_text(path, text)
        if not res.ok:
            res = storage_io.append_text(path, text)   # one bounded retry
        if res.ok:
            written += len(lines)
            with _LOCK:
                _BYTES[0] += res.bytes
                _COUNTS["bytes_written"] += res.bytes
        else:
            _bump("write_failures", len(lines))
            print(f"recorder: {len(lines)} row(s) lost writing {path.name}: "
                  f"{res.status}")
    _persist_coverage()
    return written


def _persist_coverage(force: bool = False):
    """The loss counter has to survive the process that lost the samples.

    Written by the writer, never by a producer: a producer that stopped to
    write a counter would be the blocking call this whole design removes.

    Throttled because the writer wakes twice a second and a full file replace
    at that rate is a lot of churn on a 500 MB volume for a number that only
    has to be right, not instantaneous. A change always lands within the
    window, and stop() forces it."""
    with _LOCK:
        snapshot = tuple(sorted(_COUNTS.items()))
        due = (force or snapshot != _PERSIST[1]
               or (_now_s() - _PERSIST[0]) >= COVERAGE_PERSIST_S)
        if not due:
            return
        _PERSIST[0] = _now_s()
        _PERSIST[1] = snapshot
    storage_io.write_json(path_for("coverage"), coverage())


# ---------------------------------------------------------------------------
# record 1: event identity and decision
# ---------------------------------------------------------------------------
def _fingerprint(values, gate_passed, reject_codes, selected) -> str:
    def _round(v):
        if isinstance(v, float):
            return round(v, 6)
        if isinstance(v, dict):
            return {k: _round(v[k]) for k in sorted(v)}
        if isinstance(v, (list, tuple)):
            return [_round(x) for x in v]
        return v
    try:
        raw = json.dumps([_round(values or {}), bool(gate_passed),
                          sorted(reject_codes or []), bool(selected)],
                         sort_keys=True)
    except (TypeError, ValueError):
        raw = repr(values)
    return _sha(raw)[:16]


def record_candidate(*, strategy_id, symbol, direction, session_date,
                     decision_at_utc, observed_at_utc, input_bar_end_utc,
                     input_feed, input_values, gate_passed, reject_codes,
                     selected, position_id=None, decision_id=None,
                     candidate_id=None, strategy_version="", policy_hash="",
                     source_commit="", deployment_id="", extra=None):
    """Record type 1. What the bot saw and what it decided, written for every
    completed input bar opportunity and not only for alerts.

    THE UNIT IS THE BAR, not the poll. Astra section 5: a fifteen second
    recheck of the same unchanged bar is not four independent candidates. The
    candidate id is derived from the strategy, symbol, direction and the BAR,
    so four rechecks collapse to one row; a meaningful change in the decision
    inputs writes a REVISION of that same candidate rather than a second
    opportunity or a silent overwrite. Scan evaluations are counted separately
    so both numbers stay available."""
    key = f"{strategy_id}|{symbol}|{direction}|{session_date}|{input_bar_end_utc}"
    cid = candidate_id or ("cnd" + _sha(key)[:14])
    fp = _fingerprint(input_values, gate_passed, reject_codes, selected)
    with _LOCK:
        prior = _CANDIDATES.get(key)
        if prior is not None and prior["fingerprint"] == fp:
            return prior["id"]                # the same bar, unchanged
        revision = 0 if prior is None else prior["revision"] + 1
        _CANDIDATES[key] = {"id": cid, "revision": revision, "fingerprint": fp}
        _COUNTS["candidate_opportunities"] += (1 if prior is None else 0)
        _COUNTS["candidate_revisions"] += (0 if prior is None else 1)
    missing = []
    if decision_id is None:
        missing.append("no_decision_id")
    row = {
        "schema_version": SCHEMA_VERSION, "record": "candidate",
        "candidate_id": cid, "decision_id": decision_id,
        "strategy_id": strategy_id, "strategy_version": strategy_version,
        "policy_hash": policy_hash, "source_commit": source_commit,
        "deployment_id": deployment_id, "session_date": session_date,
        "cluster_id": cluster_id_for(session_date, input_bar_end_utc, direction),
        "decision_at_utc": decision_at_utc, "observed_at_utc": observed_at_utc,
        "symbol": symbol, "direction": direction,
        "input_bar_end_utc": input_bar_end_utc, "input_feed": input_feed,
        "input_values": input_values, "gate_passed": bool(gate_passed),
        "reject_codes": list(reject_codes or []), "selected": bool(selected),
        "position_id": position_id, "revision": revision,
        "recorded_at_utc": _utc_iso(),
        "missing_reason": " ".join(missing) or None,
    }
    _merge_extra(row, extra)
    _enqueue("candidates", row)
    return cid


def record_scan_evaluation(strategy_id: str, symbol: str, n: int = 1):
    """One look at one symbol. Counted, never written as a row.

    Astra section 5 asks for scan evaluations and candidate opportunities to
    stay separately available. This is the denominator that says how often the
    bot looked; record_candidate is the one that says how many distinct chances
    it actually had."""
    _bump("scan_evaluations", n)


# ---------------------------------------------------------------------------
# record 2: quote and path sample
# ---------------------------------------------------------------------------
def record_sample(*, candidate_id, contract_id, contract_role, provider, feed,
                  provider_at_utc, received_at_utc, requested_at_utc,
                  bid, ask, bid_size, ask_size, underlying_price,
                  underlying_at_utc, price_basis, is_model, quality_flags,
                  observation_end_utc, missing_reason=None, mark=None,
                  position_id=None, extra=None):
    """Record type 2. One observed quote on one contract.

    THREE CLOCKS, deliberately. provider_at_utc is when the provider says the
    quote was current, received_at_utc is when we got it, requested_at_utc is
    when we asked. Yahoo option quotes run roughly fifteen minutes delayed and
    collapsing these into one field is exactly how a stale quote gets treated
    as live.

    Unknown is null WITH A REASON. Never zero, never false, never the received
    time standing in for a provider time we were never given."""
    flags = list(quality_flags or [])
    reasons = [missing_reason] if missing_reason else []
    with _LOCK:
        seq = _SEQ.get(contract_id, 0) + 1
        _SEQ[contract_id] = seq
        last = _LAST.get(contract_id)
    sha = _sha(f"{provider_at_utc}|{bid}|{ask}|{underlying_price}")[:16]
    if last is not None:
        # A provider timestamp we have already seen means the feed served the
        # same quote again. That is real evidence about the cadence we actually
        # got, so it is FLAGGED and counted rather than hidden by dropping it.
        if provider_at_utc is not None and provider_at_utc == last["provider_at"]:
            flags.append("repeat_provider_timestamp")
            if sha == last["sha"]:
                flags.append("identical_to_previous")
            _bump("duplicate_samples")
        elif (provider_at_utc is not None and last["provider_at"] is not None
                and str(provider_at_utc) < str(last["provider_at"])):
            flags.append("out_of_order_provider")
            _bump("out_of_order_samples")
        if (received_at_utc is not None and last["received_at"] is not None
                and str(received_at_utc) < str(last["received_at"])):
            flags.append("out_of_order_receipt")
            _bump("out_of_order_samples")
    with _LOCK:
        _LAST[contract_id] = {"provider_at": provider_at_utc,
                              "received_at": received_at_utc, "sha": sha}
    if provider_at_utc is None:
        reasons.append("no_provider_timestamp")
    if bid is None or ask is None:
        reasons.append("no_two_sided_quote")
    if mark is None and bid is not None and ask is not None:
        mark = round((float(bid) + float(ask)) / 2.0, 4)
    sid = _new_id("smp")
    row = {
        "schema_version": SCHEMA_VERSION, "record": "sample",
        "sample_id": sid, "candidate_id": candidate_id,
        "contract_id": contract_id, "contract_role": contract_role,
        "sample_sequence": seq, "provider": provider, "feed": feed,
        "provider_at_utc": provider_at_utc, "received_at_utc": received_at_utc,
        "requested_at_utc": requested_at_utc,
        "bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size,
        "underlying_price": underlying_price,
        "underlying_at_utc": underlying_at_utc, "price_basis": price_basis,
        "is_model": is_model, "quality_flags": flags,
        "missing_reason": " ".join(r for r in reasons if r) or None,
        "observation_end_utc": observation_end_utc,
        "position_id": position_id, "mark": mark,
        "recorded_at_utc": _utc_iso(),
    }
    _merge_extra(row, extra)
    if bid is None and ask is None and mark is None:
        _bump("samples_missing")
    else:
        _bump("samples_recorded")
    with _LOCK:
        _LAST_SAMPLE_AT[0] = received_at_utc or _utc_iso()
        obs = _OBS.get((candidate_id, contract_id))
        if obs is not None:
            obs["last_sample_at_utc"] = received_at_utc or _utc_iso()
    _enqueue("samples", row)
    return sid


def _merge_extra(row, extra):
    """Caller supplied context, never allowed to overwrite a schema field."""
    for k, v in (extra or {}).items():
        if k not in row:
            row[k] = v


# ---------------------------------------------------------------------------
# record 3: decision and exit evidence
# ---------------------------------------------------------------------------
def record_event(*, position_id, event_type, trigger_at_utc, trigger_sample_id,
                 trigger_basis, trigger_threshold, mark_used, mark_source,
                 leg_quantity, deliveries=None, candidate_id=None,
                 decision_id=None, extra=None):
    """Record type 3. Which exact observation fired an exit, and what happened
    to the card that reported it.

    trigger_sample_id points at the sample that fired it, so an exit can be
    re derived rather than re asserted. When no sample can be named, the id is
    null with a reason: an exit whose trigger cannot be identified is a real
    and reportable state, and inventing one would destroy the only thing this
    record is for.

    Delivery status is per recipient, by index and opaque ref. No recipient
    identifier is ever written. unknown means the request may have reached
    Telegram and we never learned whether it did, and it stays unknown rather
    than being resolved by guessing."""
    rows = []
    for d in (deliveries or []):
        rows.append({
            "delivery_id": d.get("delivery_id"),
            "recipient_index": d.get("recipient_index"),
            "recipient_ref": d.get("recipient_ref"),
            "send_attempt_at_utc": d.get("send_attempt_at_utc") or None,
            "delivery_status": d.get("delivery_status"),
            "provider_message_id": d.get("provider_message_id"),
            "acknowledged_at_utc": d.get("acknowledged_at_utc") or None,
        })
    missing = []
    if trigger_sample_id is None:
        missing.append("no_trigger_sample")
    if not rows:
        missing.append("no_delivery_record")
    eid = _new_id("evt")
    row = {
        "schema_version": SCHEMA_VERSION, "record": "event",
        "event_id": eid, "position_id": position_id,
        "candidate_id": candidate_id, "decision_id": decision_id,
        "event_type": event_type, "trigger_at_utc": trigger_at_utc,
        "trigger_sample_id": trigger_sample_id, "trigger_basis": trigger_basis,
        "trigger_threshold": trigger_threshold, "mark_used": mark_used,
        "mark_source": mark_source, "leg_quantity": leg_quantity,
        "deliveries": rows, "recorded_at_utc": _utc_iso(),
        "missing_reason": "; ".join(missing) or None,
    }
    _merge_extra(row, extra)
    _bump("events_recorded")
    _enqueue("events", row)
    return eid


def deliveries_from_intent(intent) -> list:
    """The delivery half of record 3, read off an event_journal intent.

    Read rather than re derived, because the journal is already the authority
    on what was attempted and what came back, and a second opinion about
    delivery is how selected and delivery_confirmed drift apart (A05)."""
    out = []
    try:
        for d in intent.deliveries():
            out.append({
                "delivery_id": f"{getattr(intent, 'journal_id', '')}"
                               f"#{d.recipient_index}",
                "recipient_index": d.recipient_index,
                "recipient_ref": d.recipient_ref,
                "send_attempt_at_utc": d.send_attempt_at_utc or None,
                "delivery_status": d.status,
                "provider_message_id": d.provider_message_id,
                "acknowledged_at_utc": d.acknowledged_at_utc or None,
            })
    except Exception:                                          # noqa: BLE001
        return []
    return out


# ---------------------------------------------------------------------------
# record 4: contract metadata
# ---------------------------------------------------------------------------
def register_contract(*, underlying, option_right, strike, expiry_date,
                      metadata_source=None, extra=None) -> str:
    """Record type 4. Stored ONCE per contract and referenced from every tick.

    last_trading_at_utc and settlement_at_utc are separate from expiry_date on
    purpose. A half day closes at 13:00 and a 0DTE contract dies when the
    market shuts, not at a fixed 16:00. That exact assumption already produced
    a real bug in this repo: a modeled minus 28% on a day that truly lost
    minus 77%, because the pricer's clock and the market's clock disagreed.

    The style fields are CONVENTIONS read off the underlying, not a vendor
    contract master. metadata_source says so, and tick_size stays null with a
    reason rather than inventing an increment this bot cannot look up."""
    cid = contract_id_for(underlying, option_right, strike, expiry_date)
    with _LOCK:
        if cid in _CONTRACTS:
            return cid
        _CONTRACTS[cid] = True
    d = expiry_date if isinstance(expiry_date, date) else \
        date.fromisoformat(str(expiry_date))
    last_trade = datetime.combine(d, market_calendar.session_close(d),
                                  tzinfo=ET).astimezone(timezone.utc).isoformat()
    index = str(underlying).upper() in INDEX_UNDERLYINGS
    row = {
        "schema_version": SCHEMA_VERSION, "record": "contract",
        "contract_id": cid, "underlying": str(underlying).upper(),
        "option_right": str(option_right).upper()[:1], "strike": float(strike),
        "expiry_date": d.isoformat(), "last_trading_at_utc": last_trade,
        # PM settled for the 0DTE and weekly series this bot trades, so
        # settlement lands with the bell. An AM settled series would not, and
        # nothing here can tell them apart, which is what the source field is
        # for.
        "settlement_at_utc": last_trade,
        "settlement_style": "cash" if index else "physical",
        "exercise_style": "european" if index else "american",
        "multiplier": 100, "currency": "USD",
        "tick_size": None,
        "metadata_source": metadata_source or
                           "static conventions plus market_calendar, not a "
                           "vendor contract master",
        "metadata_at_utc": _utc_iso(),
        "missing_reason": "no_tick_size",
    }
    _merge_extra(row, extra)
    _enqueue("contracts", row)
    return cid


# ---------------------------------------------------------------------------
# controls, chosen by a rule fixed IN ADVANCE
# ---------------------------------------------------------------------------
def select_controls(*, underlying, right, strike, spot, expiry_date, strikes,
                    quotes=None) -> dict:
    """The predeclared controls for one chosen contract.

    A12, which is the whole reason these are two entries and not one: a put
    against a call tests DIRECTION, and a different call at another strike
    tests STRIKE SELECTION. Different questions, different rows, different
    names.

    THE RULE USES NO PRICES. It is a function of the listed strike grid, the
    chosen strike and the spot at the decision, and nothing else. `quotes` is
    accepted and deliberately ignored: that is the mechanical guarantee that
    future information cannot slip into the selection, and the regression
    proves it by moving every quote and demanding the same answer.

    A control that cannot be listed returns a missing_reason and stays in the
    coverage denominator. It is never quietly dropped, because a comparison
    that silently excludes its own hard cases is not a comparison."""
    right = str(right).upper()[:1]
    other = "P" if right == "C" else "C"
    same_grid = sorted(float(s) for s in (strikes or {}).get(right, []))
    other_grid = sorted(float(s) for s in (strikes or {}).get(other, []))
    exp = expiry_date if isinstance(expiry_date, str) else str(expiry_date)

    # DIRECTION control: the opposite right at the strike that mirrors the
    # chosen contract's moneyness across spot, snapped to the nearest listed
    # strike. Same expiry, always.
    dc = {"right": other, "strike": None, "expiry_date": exp,
          "selection_rule": "mirrored_moneyness_nearest_listed",
          "name": "direction_control", "missing_reason": None}
    if other_grid and spot:
        target = float(spot) - (float(strike) - float(spot))
        dc["strike"] = min(other_grid, key=lambda s: abs(s - target))
    else:
        dc["missing_reason"] = ("no listed strike on the opposite right for "
                                "this expiry at the decision time")

    # STRIKE control: the same right, one listed strike further out of the
    # money than the chosen one. Named, per A12.
    sc = {"right": right, "strike": None, "expiry_date": exp,
          "selection_rule": "next_otm_listed_strike",
          "name": "strike_control_next_otm", "missing_reason": None}
    if right == "C":
        further = [s for s in same_grid if s > float(strike)]
    else:
        further = [s for s in same_grid if s < float(strike)]
    if further:
        sc["strike"] = further[0] if right == "C" else further[-1]
    else:
        sc["missing_reason"] = ("no listed strike further out of the money on "
                                "this expiry at the decision time")
    return {"direction_control": dc, "strike_control": sc}


# ---------------------------------------------------------------------------
# the observation registry: what is still being watched, and until when
# ---------------------------------------------------------------------------
def observe(*, contract_id, contract_role, candidate_id, position_id,
            observation_end_utc, underlying=None, right=None, strike=None,
            expiry_date=None):
    """Watch one contract through the common horizon.

    This is the A15 fix in one function. The registry does not care whether the
    position is open: it holds the contract until observation_end_utc, so the
    chosen contract keeps being sampled after its own exit and the controls are
    sampled over exactly the same window. A comparison over two different
    windows is not a comparison."""
    if underlying is None:
        underlying, right, strike, expiry_date = _parse_contract_id(contract_id)
    row = {"contract_id": contract_id, "contract_role": contract_role,
           "candidate_id": candidate_id, "position_id": position_id,
           "observation_end_utc": observation_end_utc,
           "underlying": underlying, "right": right, "strike": strike,
           "expiry_date": expiry_date, "opened_at_utc": _utc_iso(),
           "last_sample_at_utc": None}
    with _LOCK:
        _OBS[(candidate_id, contract_id)] = row
    _save_observations()
    return row


def observations() -> list:
    with _LOCK:
        return [dict(v) for v in _OBS.values()]


def chosen_observation(position_id):
    """The chosen contract this position is being observed on, or None.

    Looked up here rather than stored on the position row, so a restart that
    reloads positions.json and this registry lands on the same observation
    without a schema migration on the book."""
    for o in observations():
        if o.get("position_id") == position_id and o.get("contract_role") == CHOSEN:
            return o
    return None


def request_controls(*, candidate_id, position_id, underlying, right, strike,
                     spot, expiry_date, observation_end_utc, decision_at_utc):
    """Ask for the predeclared controls to be resolved on the next chain read.

    Deferred deliberately, and it is NOT a lookahead. select_controls is a
    function of the listed strike grid, the chosen strike and the spot AT THE
    DECISION, and of no price at all, so resolving it against a grid read a
    minute later cannot let a later price choose the control. What it does buy
    is that the entry path pays for no extra chain read, and the monitoring
    path pays for none ever.

    The delay is written down anyway: the control observation carries the
    decision time and the time its grid was read, so anyone can check the claim
    instead of believing it."""
    row = {"candidate_id": candidate_id, "position_id": position_id,
           "underlying": str(underlying).upper(), "right": str(right)[:1],
           "strike": float(strike), "spot": float(spot) if spot else None,
           "expiry_date": str(expiry_date),
           "observation_end_utc": observation_end_utc,
           "decision_at_utc": decision_at_utc,
           "requested_at_utc": _utc_iso()}
    with _LOCK:
        _PENDING[(candidate_id, str(underlying).upper(), str(expiry_date))] = row
    _save_observations()
    return row


def pending_controls() -> list:
    with _LOCK:
        return [dict(v) for v in _PENDING.values()]


def save_observations():
    """Persist the registry now.

    record_sample updates each observation's last sample stamp in MEMORY only,
    because a file write per sample would put a disk in front of the exit loop.
    The sampler persists once per pass and a clean shutdown persists here, so
    the stamp a restart measures its blackout against is at worst one cadence
    stale rather than missing."""
    _save_observations()


def _save_observations():
    storage_io.write_json(path_for("observations"),
                          {"schema_version": SCHEMA_VERSION,
                           "saved_at_utc": _utc_iso(),
                           "observations": observations(),
                           "pending_controls": pending_controls()})


def _resolve_controls(req, snap, now_iso) -> int:
    """Turn one pending request into two observed controls, and write down
    every quote that was in front of the rule when it chose."""
    strikes = snap.get("strikes") or {}
    picked = select_controls(underlying=req["underlying"], right=req["right"],
                             strike=req["strike"], spot=req["spot"],
                             expiry_date=req["expiry_date"], strikes=strikes)
    cells = snap.get("rows") or {}
    n = 0
    for role, sel in ((DIRECTION_CONTROL, picked["direction_control"]),
                      (STRIKE_CONTROL, picked["strike_control"])):
        if sel.get("strike") is None:
            # a control that could not be listed stays in the coverage
            # denominator as a named gap, never as a row nobody wrote
            _record_gap(req.get("decision_at_utc"), now_iso,
                        f"{role} for {req['candidate_id']} could not be "
                        f"listed: {sel.get('missing_reason')}")
            continue
        cid = register_contract(underlying=req["underlying"],
                                option_right=sel["right"], strike=sel["strike"],
                                expiry_date=req["expiry_date"])
        observe(contract_id=cid, contract_role=role,
                candidate_id=req["candidate_id"],
                position_id=req["position_id"],
                observation_end_utc=req["observation_end_utc"],
                underlying=req["underlying"], right=sel["right"],
                strike=sel["strike"], expiry_date=req["expiry_date"])
        grid_at = snap.get("received_at_utc") or now_iso
        with _LOCK:
            o = _OBS.get((req["candidate_id"], cid))
            if o is not None:
                o["selection_rule"] = sel["selection_rule"]
                o["selection_name"] = sel["name"]
                o["decision_at_utc"] = req.get("decision_at_utc")
                o["grid_read_at_utc"] = grid_at
        # One compact, DURABLE row saying how this control was chosen. The
        # observation registry carries the same facts, but the registry is
        # working state and a retired observation is removed from it, so the
        # provenance of the selection would disappear exactly when the study
        # came to read it. This row survives a path archive rotation, which is
        # what Astra section 5 asks the compact records to do.
        record_event(
            position_id=req.get("position_id"), event_type="control_selected",
            trigger_at_utc=grid_at, trigger_sample_id=None,
            trigger_basis=sel["selection_rule"], trigger_threshold=None,
            mark_used=None, mark_source=None, leg_quantity=None,
            deliveries=[], candidate_id=req["candidate_id"],
            extra={"contract_role": role, "contract_id": cid,
                   "control_name": sel["name"],
                   "chosen_contract_strike": req["strike"],
                   "chosen_contract_right": req["right"],
                   "spot_at_decision": req["spot"],
                   "decision_at_utc": req.get("decision_at_utc"),
                   "grid_read_at_utc": grid_at,
                   "selection_is_price_blind": True,
                   "selection_note":
                       "the rule reads the listed strike grid, the chosen "
                       "strike and the spot at the decision, and no price at "
                       "all, so resolving it against a grid read after the "
                       "decision cannot let a later price choose the control"})
        n += 1
    # Every quote the rule had in front of it, so a later reader can confirm
    # that no future information could have entered the selection.
    #
    # BOUNDED, and the bound is recorded on every row. The alternatives this
    # rule can reach are the listed strikes near the chosen one, on both
    # rights; an SPX expiry lists thousands more that no rule could pick, and
    # writing them all would spend a 500 MB volume on quotes that answer
    # nothing.
    picked_strikes = {(sel["right"], sel["strike"])
                      for sel in (picked["direction_control"],
                                  picked["strike_control"])
                      if sel.get("strike") is not None}
    pool = set(picked_strikes)
    for right in ("C", "P"):
        grid = sorted(float(s) for s in (strikes or {}).get(right, []))
        if not grid:
            continue
        near = sorted(grid, key=lambda s: abs(s - float(req["strike"])))
        for s in near[:SELECTION_POOL_WIDTH * 2 + 1]:
            pool.add((right, s))
    for (right, strike) in sorted(pool):
        cell = cells.get((right, strike)) or {}
        if strike is None:
            continue
        pool_id = contract_id_for(req["underlying"], right, strike,
                                  req["expiry_date"])
        record_sample(
            candidate_id=req["candidate_id"], contract_id=pool_id,
            contract_role=SELECTION_POOL, provider=snap.get("provider"),
            feed=snap.get("feed"), provider_at_utc=snap.get("provider_at_utc"),
            received_at_utc=snap.get("received_at_utc") or now_iso,
            requested_at_utc=snap.get("requested_at_utc"),
            bid=cell.get("bid"), ask=cell.get("ask"),
            bid_size=cell.get("bid_size"), ask_size=cell.get("ask_size"),
            underlying_price=snap.get("underlying_price"),
            underlying_at_utc=snap.get("underlying_at_utc"),
            price_basis="quote_mid" if cell.get("bid") is not None else None,
            is_model=False if cell.get("bid") is not None else None,
            quality_flags=["selection_pool"],
            missing_reason=(None if cell else "selection_window_no_row"),
            observation_end_utc=req["observation_end_utc"],
            position_id=req.get("position_id"),
            extra={"selection_for": req["candidate_id"],
                   "selection_is_price_blind": True,
                   "selection_pool_width": SELECTION_POOL_WIDTH,
                   "selection_was_chosen": (right, strike) in picked_strikes})
    return n


def _load_observations():
    """Resume every observation this volume was still in the middle of.

    A restart is a hole in the path, and a hole that is not written down
    silently becomes "we observed everything". Every resumed contract whose
    last sample is older than the gap threshold gets a coverage row saying how
    long the record went dark and why."""
    res = storage_io.read_json(path_for("observations"))
    if not res.usable or not isinstance(res.value, dict):
        return 0
    now = _utc_iso()
    n = 0
    for req in (res.value.get("pending_controls") or []):
        try:
            key = (req.get("candidate_id"), req.get("underlying"),
                   str(req.get("expiry_date")))
        except AttributeError:
            continue
        with _LOCK:
            _PENDING[key] = dict(req)
    for row in (res.value.get("observations") or []):
        try:
            key = (row.get("candidate_id"), row.get("contract_id"))
        except AttributeError:
            continue
        with _LOCK:
            _OBS[key] = dict(row)
        n += 1
        last = row.get("last_sample_at_utc") or row.get("opened_at_utc")
        if not last:
            # a row with neither stamp cannot have its coverage measured at
            # all, and saying that is the honest answer. Guessing a start would
            # turn an unmeasurable window into a measured one
            _record_gap(None, now,
                        "process restart: the record for "
                        f"{row.get('contract_id')} carries no sample or open "
                        "time, so its coverage cannot be measured")
            continue
        end = row.get("observation_end_utc")
        if end and str(end) <= now:
            # the window already closed while nobody was running. That is only
            # a gap if the record went dark BEFORE the horizon: an observation
            # sampled right up to the bell and then left in the file overnight
            # is complete, and calling it a gap would make completeness say
            # degraded on every boot and therefore mean nothing.
            if _older_than(last, end, GAP_AFTER_S):
                _record_gap(last, end,
                            "process restart: the observation window for "
                            f"{row.get('contract_id')} ended while this copy "
                            "was down")
        elif _older_than(last, now, GAP_AFTER_S):
            _record_gap(last, now,
                        "process restart: no samples were taken for "
                        f"{row.get('contract_id')} while this copy was down")
    return n


def _resume_state():
    """Everything a new life of the bot has to know about the old one.

    Without this a restart re-writes contract metadata that is already on the
    volume and re-opens candidates it already recorded, and the record grows a
    duplicate for every reboot rather than for every real event.

    sample_sequence is deliberately NOT resumed. It is monotonic within one
    process life, and rebuilding it would mean reading the whole day of samples
    at every boot to learn a number that received_at_utc already orders."""
    _load_contracts()
    _load_candidates()
    return _load_observations()


def _load_contracts():
    """Contract ids already written, so metadata stays stored ONCE."""
    for row in read_records("contracts"):
        cid = row.get("contract_id") if isinstance(row, dict) else None
        if cid:
            with _LOCK:
                _CONTRACTS[cid] = True


def _load_candidates():
    """Today's candidate keys and revisions, so a restart mid session does not
    re-open a bar this volume already has a row for."""
    for row in read_records("candidates", day=_et_today()):
        if not isinstance(row, dict):
            continue
        key = (f"{row.get('strategy_id')}|{row.get('symbol')}|"
               f"{row.get('direction')}|{row.get('session_date')}|"
               f"{row.get('input_bar_end_utc')}")
        rev = int(row.get("revision") or 0)
        with _LOCK:
            prior = _CANDIDATES.get(key)
            if prior is None or rev >= prior["revision"]:
                _CANDIDATES[key] = {
                    "id": row.get("candidate_id"), "revision": rev,
                    "fingerprint": _fingerprint(
                        row.get("input_values"), row.get("gate_passed"),
                        row.get("reject_codes"), row.get("selected"))}


def _older_than(then, now_iso, seconds) -> bool:
    if not then:
        return True
    try:
        a = datetime.fromisoformat(str(then))
        b = datetime.fromisoformat(str(now_iso))
    except (TypeError, ValueError):
        return False
    return (b - a).total_seconds() > seconds


def _record_gap(start, end, reason):
    _bump("gaps")
    _enqueue("health", _health_row(observation_gap_start=start,
                                   observation_gap_end=end, gap_reason=reason))


# ---------------------------------------------------------------------------
# the batched sampler. This is the ONLY thing that costs a network read, and
# it never runs on the monitoring thread.
# ---------------------------------------------------------------------------
def sample_once(chain_fn, now=None) -> int:
    """One pass over every contract under observation, batched per chain.

    Astra section 5: batch additional contracts where the feed supports it. One
    option chain read covers every strike and both rights on one expiry, so N
    contracts on one expiry cost ONE provider call, not N.

    A contract the chain has no row for is recorded as a MISSING sample with a
    reason, not left out. A provider that raises is a counted provider error
    and never an exception travelling back into a caller."""
    now = now or _utc_now()
    now_iso = _utc_iso(now)
    with _LOCK:
        retired = [k for k, o in _OBS.items()
                   if o.get("observation_end_utc")
                   and str(o["observation_end_utc"]) <= now_iso]
        for k in retired:
            _OBS.pop(k, None)
        # a control request whose window closed before any chain read could
        # resolve it is dead, not pending. Left in the file it would be retried
        # every pass for ever, and the control it names could never be observed
        # anyway: its horizon has already passed.
        expired = [dict(v) for k, v in list(_PENDING.items())
                   if v.get("observation_end_utc")
                   and str(v["observation_end_utc"]) <= now_iso]
        for req in expired:
            _PENDING.pop((req.get("candidate_id"), req.get("underlying"),
                          str(req.get("expiry_date"))), None)
    for req in expired:
        _record_gap(req.get("requested_at_utc"), now_iso,
                    "the predeclared controls for "
                    f"{req.get('candidate_id')} were never resolved before "
                    "the common horizon closed, so that comparison has no "
                    "control leg")
    if retired or expired:
        _save_observations()
    groups = {}
    for o in observations():
        groups.setdefault((o.get("underlying"), o.get("expiry_date")),
                          []).append(o)
    written = 0
    for (under, exp), rows in groups.items():
        try:
            snap = chain_fn(under, exp, now=now)
        except Exception as e:                                 # noqa: BLE001
            _bump("provider_errors")
            snap = None
            reason = "chain_read_failed"
            for o in rows:
                end = o.get("observation_end_utc")
                record_sample(
                    candidate_id=o.get("candidate_id"),
                    contract_id=o.get("contract_id"),
                    contract_role=o.get("contract_role"),
                    provider="unknown", feed="unknown", provider_at_utc=None,
                    received_at_utc=now_iso, requested_at_utc=now_iso,
                    bid=None, ask=None, bid_size=None, ask_size=None,
                    underlying_price=None, underlying_at_utc=None,
                    price_basis=None, is_model=None, quality_flags=["no_read"],
                    missing_reason=reason, observation_end_utc=end,
                    position_id=o.get("position_id"))
                written += 1
            continue
        if not isinstance(snap, dict):
            _bump("provider_errors")
            continue
        # the predeclared controls are resolved here, off the entry path and
        # off the monitoring path, from the grid this read already paid for
        with _LOCK:
            due = [(k, dict(v)) for k, v in _PENDING.items()]
        for key, req in due:
            if (req.get("underlying"), req.get("expiry_date")) != (under, exp):
                continue
            try:
                written += _resolve_controls(req, snap, now_iso)
            except Exception as e:                             # noqa: BLE001
                print(f"recorder: could not resolve controls for "
                      f"{req.get('candidate_id')}: {e}")
                continue
            with _LOCK:
                _PENDING.pop(key, None)
            _save_observations()
            # the controls this read just created belong in THIS pass, tagged
            # with their role, not one cadence later
            rows = [o for o in observations()
                    if (o.get("underlying"), o.get("expiry_date")) == (under, exp)]
        cells = snap.get("rows") or {}
        # the underlying the chain read did not give us, borrowed from what
        # monitoring already fetched, with ITS timestamp rather than this
        # read's, and flagged so nobody reads it as part of the quote
        borrowed = None
        if snap.get("underlying_price") is None:
            with _LOCK:
                borrowed = _UNDER.get(str(under or "").upper())
        for o in rows:
            key = (str(o.get("right") or "")[:1], float(o.get("strike") or 0))
            cell = cells.get(key)
            miss = None if cell else "no_chain_row"
            cell = cell or {}
            record_sample(
                candidate_id=o.get("candidate_id"),
                contract_id=o.get("contract_id"),
                contract_role=o.get("contract_role"),
                provider=snap.get("provider"), feed=snap.get("feed"),
                provider_at_utc=snap.get("provider_at_utc"),
                received_at_utc=snap.get("received_at_utc") or now_iso,
                requested_at_utc=snap.get("requested_at_utc"),
                bid=cell.get("bid"), ask=cell.get("ask"),
                bid_size=cell.get("bid_size"), ask_size=cell.get("ask_size"),
                underlying_price=(snap.get("underlying_price")
                                  if borrowed is None else borrowed[0]),
                underlying_at_utc=(snap.get("underlying_at_utc")
                                   if borrowed is None else borrowed[1]),
                price_basis="quote_mid" if cell.get("bid") is not None else None,
                is_model=False if cell.get("bid") is not None else None,
                quality_flags=(["underlying_from_monitor"] if borrowed
                               else []),
                missing_reason=miss,
                observation_end_utc=o.get("observation_end_utc"),
                position_id=o.get("position_id"),
                extra={"last_trade_at_utc": cell.get("last_trade_at_utc"),
                       "selection_rule": o.get("selection_rule")})
            written += 1
    if written:
        # the registry's last_sample stamps are what a restart measures its
        # own blackout against, so they are persisted once per pass rather
        # than left in memory for a crash to lose
        _save_observations()
    return written


def start_sampler(chain_fn, interval_s: float = None):
    """Run the batched sampler on its OWN thread.

    Its own thread and not the monitoring loop, because the constraint on this
    package is that no synchronous chain read is ever added to the path that
    walks a live stop. The monitoring loop hands over what it already fetched
    and pays nothing."""
    with _LOCK:
        if _SAMPLER[0] is not None and _SAMPLER[0].is_alive():
            return
        _SAMPLER_STOP.clear()
        every = float(interval_s or SAMPLE_SECONDS)

        def _loop():
            while not _SAMPLER_STOP.is_set():
                try:
                    if _OBS:
                        sample_once(chain_fn)
                except Exception as e:                         # noqa: BLE001
                    _bump("provider_errors")
                    print(f"recorder: sampler pass failed, continuing: {e}")
                _SAMPLER_STOP.wait(every)

        t = threading.Thread(target=_loop, name="recorder-sampler", daemon=True)
        _SAMPLER[0] = t
        t.start()


def stop_sampler(timeout: float = 3.0):
    _SAMPLER_STOP.set()
    t = _SAMPLER[0]
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    _SAMPLER[0] = None


# ---------------------------------------------------------------------------
# record 6: health and coverage
# ---------------------------------------------------------------------------
def note_monitor_ok(at_utc=None):
    """Monitoring completed a pass. Kept here so the health record can say
    whether the loop that matters was still running when the samples stopped."""
    _MONITOR_OK[0] = at_utc or _utc_iso()


def note_underlying(symbol, price, at_utc):
    """The last underlying price MONITORING saw, with its own timestamp.

    The sampler thread has no market feed and must never grow one: a second
    price source would be a second thing to rate limit and a second clock to
    confuse. It borrows this, and the sample says where it came from rather
    than implying the chain read produced it."""
    with _LOCK:
        _UNDER[str(symbol).upper()] = (price, at_utc)


def _dir_bytes() -> int:
    """Bytes on disk under the recorder directory, cached for a minute.

    Real bytes and not this process's running total, because the volume is
    500 MB and what matters is what is ON it, including everything written by
    the lives of the bot that came before this one."""
    now = _now_s()
    if _DIRSIZE[1] and (now - _DIRSIZE[0]) < 60.0:
        return _DIRSIZE[1]
    total = 0
    try:
        for p in recorder_dir().iterdir():
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return _DIRSIZE[1] or _BYTES[0]
    _DIRSIZE[0], _DIRSIZE[1] = now, total
    return total


def coverage() -> dict:
    """The coverage denominator, in one dict.

    dropped_samples and the gap count ARE the denominator. Astra section 5: a
    gap that is not recorded is a gap that silently becomes "we observed
    everything", and a completeness word that only ever says complete is not a
    measurement."""
    with _LOCK:
        c = dict(_COUNTS)
    lost = (c["dropped_samples"] + c["write_failures"] + c["gaps"]
            + c["provider_errors"] + c["samples_missing"])
    c["completeness"] = "complete" if lost == 0 else "degraded"
    c["bytes_used"] = _dir_bytes() or _BYTES[0]
    c["observations_live"] = len(_OBS)
    c["queue_depth"] = _Q.qsize()
    c["last_sample_at_utc"] = _LAST_SAMPLE_AT[0]
    c["updated_at_utc"] = _utc_iso()
    return c


def _health_row(**over) -> dict:
    c = coverage()
    row = {
        "schema_version": SCHEMA_VERSION, "record": "health",
        "instance_id": over.pop("instance_id", None),
        "ownership_state": over.pop("ownership_state", None),
        "last_successful_monitor_at_utc": _MONITOR_OK[0],
        "last_sample_at_utc": _LAST_SAMPLE_AT[0],
        "queue_depth": _Q.qsize(),
        "dropped_samples": c["dropped_samples"],
        "write_failures": c["write_failures"],
        "provider_errors": c["provider_errors"],
        "bytes_used": c["bytes_used"],
        "observation_gap_start": None, "observation_gap_end": None,
        "gap_reason": None,
        "completeness": c["completeness"],
        "observations_live": c["observations_live"],
        "samples_recorded": c["samples_recorded"],
        "samples_missing": c["samples_missing"],
        "duplicate_samples": c["duplicate_samples"],
        "out_of_order_samples": c["out_of_order_samples"],
        "scan_evaluations": c["scan_evaluations"],
        "candidate_opportunities": c["candidate_opportunities"],
        "recorded_at_utc": _utc_iso(),
        "missing_reason": None if _MONITOR_OK[0] else "no_monitor_pass_yet",
    }
    row.update(over)
    return row


def record_health(*, ownership_state=None, instance_id=None, extra=None):
    """Record type 6. Whether the copy that wrote this row was allowed to act,
    and how much of the record it actually managed to keep."""
    row = _health_row(ownership_state=ownership_state, instance_id=instance_id)
    _merge_extra(row, extra)
    _enqueue("health", row)
    return row


def capacity_estimate() -> dict:
    """Measured bytes per sample times contracts times samples per session
    times retention days. Astra section 5 asks for exactly that multiplication
    and it is done here rather than asserted, because the volume is 500 MB and
    retention is currently unbounded.

    Every input is measured or declared. Nothing here is a typed in number
    standing in for a measurement."""
    with _LOCK:
        rows = _COUNTS["samples_recorded"] + _COUNTS["samples_missing"]
        written = _COUNTS["bytes_written"]
    per = round(written / rows, 2) if rows else 0.0
    # what is actually under observation, or the declared planning figure of a
    # chosen contract plus its two controls when nothing is open yet
    contracts = len(_OBS) or 3
    session_s = 6.5 * 3600
    per_contract = int(session_s / max(SAMPLE_SECONDS, 1))
    per_session = per * contracts * per_contract
    vol = storage_io.volume_status(recorder_dir())
    return {
        "measured_bytes_per_sample": per,
        "measured_rows": rows,
        "contracts_per_session": contracts,
        "samples_per_contract_per_session": per_contract,
        "sampler_cadence_s": SAMPLE_SECONDS,
        "retention_days": RETENTION_DAYS,
        "estimated_bytes_per_session": per_session,
        "estimated_bytes_at_retention": per_session * RETENTION_DAYS,
        "volume_bytes_total": vol.get("total_bytes"),
        "volume_bytes_free": vol.get("free_bytes"),
        "bytes_used_now": _dir_bytes() or _BYTES[0],
        "basis": "measured bytes per written row times contracts under "
                 "observation times session length over the sampler cadence "
                 "times retention days",
    }


# ---------------------------------------------------------------------------
# reading a path back
# ---------------------------------------------------------------------------
def path_summary(candidate_id, contract_id) -> dict:
    """What the recorded path actually shows for one contract.

    Every value is named OBSERVED, and that is not decoration. The bot polls;
    it does not watch. A maximum between two samples was never seen by anything
    in this system and calling it a high reached would be the same overstatement
    A15 is about, one level down."""
    rows = [r for r in read_records("samples")
            if r.get("contract_id") == contract_id
            and (candidate_id is None or r.get("candidate_id") == candidate_id)]
    rows.sort(key=lambda r: (r.get("received_at_utc") or "",
                             r.get("sample_sequence") or 0))
    marks = [(r.get("mark"), r.get("provider_at_utc") or r.get("received_at_utc"))
             for r in rows if r.get("mark") is not None]
    stamps = [r.get("received_at_utc") for r in rows if r.get("received_at_utc")]
    cadence = None
    if len(stamps) > 1:
        gaps = []
        for a, b in zip(stamps, stamps[1:]):
            try:
                gaps.append((datetime.fromisoformat(b)
                             - datetime.fromisoformat(a)).total_seconds())
            except (TypeError, ValueError):
                continue
        cadence = round(statistics.median(gaps), 1) if gaps else None
    hi = max(marks, key=lambda m: m[0]) if marks else (None, None)
    lo = min(marks, key=lambda m: m[0]) if marks else (None, None)
    missing = sum(1 for r in rows if r.get("mark") is None)
    return {
        "contract_id": contract_id, "candidate_id": candidate_id,
        "samples": len(rows), "cadence_s": cadence,
        "first_sample_at_utc": stamps[0] if stamps else None,
        "last_sample_at_utc": stamps[-1] if stamps else None,
        "observation_end_utc": rows[-1].get("observation_end_utc")
                               if rows else None,
        "observed_first_mark": marks[0][0] if marks else None,
        "observed_last_mark": marks[-1][0] if marks else None,
        "observed_max_mark": hi[0], "observed_max_at_utc": hi[1],
        "observed_min_mark": lo[0], "observed_min_at_utc": lo[1],
        "observed_missing_samples": missing,
        "complete": bool(rows) and missing == 0,
        "caveat": _CAVEAT,
    }


def report_text() -> str:
    """The /health line. Plain, and it never claims a completeness it does not
    have."""
    c = coverage()
    mb = (c["bytes_used"] or 0) / 1048576.0
    n = c["observations_live"]
    bits = [f"Recorder: {c['samples_recorded']} samples",
            f"{n} contract{'' if n == 1 else 's'} watched",
            f"completeness {c['completeness']}"]
    if c["dropped_samples"]:
        bits.append(f"{c['dropped_samples']} lost to a full queue")
    if c["write_failures"]:
        bits.append(f"{c['write_failures']} unwritable")
    if c["gaps"]:
        bits.append(f"{c['gaps']} recorded gap(s)")
    if c["samples_missing"]:
        bits.append(f"{c['samples_missing']} with no quote")
    return ", ".join(bits) + f", {mb:.2f} MB used."


# ---------------------------------------------------------------------------
def _reset_for_test(keep_files: bool = False):
    """Wipe the in memory state and the day files, KEEP the durable registry.

    The registry is kept deliberately: W06's restart case is exactly the claim
    that an observation survives the process that opened it, and a reset that
    deleted it would test nothing.

    keep_files=True keeps everything else too, which is what an actual restart
    looks like: a new process, the same volume, every row the old life wrote
    still on it. A reset that deletes the day files models a fresh volume and
    cannot show a reboot duplicating a row."""
    stop_sampler(timeout=0.5)
    _STOP.set()
    t = _WRITER[0]
    if t is not None and t.is_alive():
        t.join(timeout=2.0)
    _WRITER[0] = None
    while True:
        try:
            _Q.get_nowait()
        except queue.Empty:
            break
    with _LOCK:
        _COUNTS.clear()
        _COUNTS.update(_zero_counts())
        _CONTRACTS.clear()
        _CANDIDATES.clear()
        _SEQ.clear()
        _LAST.clear()
        _OBS.clear()
        _PENDING.clear()
        _MONITOR_OK[0] = None
        _LAST_SAMPLE_AT[0] = None
        _BYTES[0] = 0
        _DROP_SAID[0] = 0
        _PERSIST[0], _PERSIST[1] = 0.0, ()
        _RESUMED[0] = False
    d = recorder_dir()
    if not keep_files:
        for pattern in ("candidates-*.jsonl", "samples-*.jsonl",
                        "events-*.jsonl", "health-*.jsonl", "contracts.jsonl",
                        "coverage.json"):
            for p in d.glob(pattern):
                try:
                    p.unlink()
                except OSError:
                    pass
    _resume_state()
