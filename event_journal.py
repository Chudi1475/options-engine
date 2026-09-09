"""The append-only record of what the bot DECIDED and what it managed to send.

Why it exists
-------------
Three alert paths, three different orderings, none of them durable.

The momentum entry wrote the position and then sent the card. A crash in
between left a tracked position that would later fire exit cards for an entry
nobody was ever told about, and nothing anywhere said a card was owed.

The exit alert did the opposite, deliberately: it sent and then saved, with a
comment calling a crash in between "at worst a duplicate". It is not a
duplicate. positions.step has already closed the row in memory, so the next
boot re-reads the pre exit row, trips the same stop and books the same leg a
second time.

The sniper burned the day key, sent, tracked, and linked the observation LAST,
inside a try that only printed. A fault there lost the link for good: the card
went out and the row still said selected=False, so a delivered alert quietly
left the forward cohort (Astra A05).

None of those three orderings is fixable by reordering alone, because there is
no fourth place to write down "I committed to this and I do not yet know
whether it arrived". This module is that place.

WHAT IS DURABLE HERE
    One appended line in events-YYYY-MM-DD.jsonl, fsynced under that file's
    lock. Nothing rewrites a daily file, ever. open_intents.json is a
    MATERIALIZED VIEW of the intents that are not finished yet; it is rebuilt
    from EVERY daily file on the volume whenever it is missing or unreadable,
    so losing it loses no event.

    A stale view is treated as just as untrustworthy as a lost one, because it
    is. The files are append only, so the view carries how many lines of each
    daily file it has folded, and any file whose line count has moved on is
    folded again on the next load. A crash between the append and the publish,
    and a single denied index publish, both leave a perfectly READABLE index
    with an event missing from it, and the old "rebuild it when it is missing
    or unreadable" rule never fired for either.

    A daily file the OS will not hand over is NOT an empty one. read_jsonl says
    which of those two it is, that answer is checked here, and a view built
    over a file that could not be read in full is reported as damaged and never
    published over the index already on disk.

WHAT IS NOT A TRANSACTION
    This journal does not make positions.json, sniper_positions.json and
    state.json commit together. Nothing on a filesystem does. What it gives is
    an ordering with a recovery: the intent is on disk BEFORE the side effects,
    so any crash leaves a record that says what was owed, and replay finishes
    it exactly once instead of guessing from whether a position happens to
    exist.

WHY IT FAILS CLOSED
    forward_ledger fails OPEN on a lock it cannot take, and that is right for a
    recorder: losing one observation is worse than a delayed scan. This one is
    the opposite. An intent that was not recorded is an alert nobody can
    recover, so commit_intent RAISES JournalUnavailable rather than returning a
    half committed thing, and the caller's answer to that is to not create the
    entry at all (Astra section 3).

WHAT NEVER GOES IN
    A recipient's chat id. Delivery is tracked per recipient by INDEX plus an
    opaque ref, an HMAC over the id under a salt file that never leaves the
    volume. Astra: keep per recipient status for all three recipients without
    exporting their identifiers.

RETENTION, STATED RATHER THAN ASSUMED
    Daily files are never rotated or deleted by this module. The deployment
    volume is 500 MB and cannot be treated as unlimited, so health() reports
    bytes_used and the operator can measure instead of guess. A day of alerts
    is a few tens of kilobytes, so the honest reading is that this is fine for
    a long time and is NOT a policy. Only the index is compacted, only for
    RESOLVED intents older than INDEX_RETAIN_DAYS, and a compaction that would
    drop an unresolved intent is refused: that intent is the only record that
    something is still owed.

    That refusal is worth nothing unless the rebuild can still SEE the old
    intent, so a rebuild replays every daily file present, not a window. The
    cost is one pass over the events directory per process, at load. That cost
    grows with the number of daily files kept, which is the same number
    bytes_used measures, so it is visible rather than assumed.
"""

import hashlib
import hmac
import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import config
import storage_io

SCHEMA_VERSION = 1

# the five delivery states, exactly as the spec names them. unknown is the one
# that carries the whole point: the request may have reached Telegram and the
# client did not hear back, and that is not the same fact as a refusal.
PENDING = "pending"
ATTEMPTED = "attempted"
CONFIRMED = "confirmed"
FAILED = "failed"
UNKNOWN = "unknown"
DELIVERY_STATES = (PENDING, ATTEMPTED, CONFIRMED, FAILED, UNKNOWN)

# how many times replay may re-send one FAILED recipient before it stops and
# reports instead. bounded on purpose: a recipient who blocked the bot must not
# turn one card into an endless retry.
MAX_DELIVERY_RETRIES = 6

# the ONE error class no retry can ever fix: the offline suite's wire gag. A
# test mode drop is a deliberate local no-op and no other process will ever
# repair it, so it resolves as a reported drop.
#
# "standby" is deliberately NOT here, and getting that wrong was a real hole.
# A gagged copy's drop is exactly what the copy that owns the lease has to
# finish: the journal lives on the SHARED volume, so the winner's replay reads
# this same intent and delivers it. Marking it terminal would have resolved the
# intent with the card never sent by anybody, which is the failure the old
# compensating key release was flailing at. A gagged process cannot spin on it
# either, because replay only runs on the way into ACTIVE.
#
# An HTTP status is not here either, even a 403: a recipient who blocked the
# bot is retried like anyone else and falls out at MAX_DELIVERY_RETRIES, so the
# bound rather than a guess about permanence is what stops it.
TERMINAL_ERROR_CLASSES = ("test_mode",)

# how many days of RESOLVED intents the index keeps, so an exit leg committed
# just before midnight is still recognised as already committed after the date
# rolls. Unresolved intents are kept regardless of age and a compaction that
# would drop one is refused.
INDEX_RETAIN_DAYS = 2

_LOCK = threading.RLock()
_SEQ = [0]                 # per process, seeded from the daily file on first use
_SEEDED = [None]           # the day the counter was seeded for
_SALT = [None]
_INDEX_CACHE = [None]
_WRITE_FAILURES = [0]
_PROCESS = [None]          # this life of the bot, for telling a dead attempt apart
_SAID = set()              # console lines that must not repeat every write


class JournalUnavailable(Exception):
    """The durable record could not be written. Never swallowed: the caller's
    correct answer is to not create the unrecorded thing (Astra section 3)."""


def _process_token() -> str:
    """A value that changes when the process does.

    It goes into the ATTEMPTED line so a later reader can tell "I am inside
    that HTTP call right now" from "some earlier life of this bot died inside
    it". The pid on its own is not enough: pids get reused, and a reused one
    would make a dead attempt look live and get the card sent a second time.
    The random half is what makes the answer safe to act on."""
    with _LOCK:
        if _PROCESS[0] is None:
            _PROCESS[0] = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        return _PROCESS[0]


def _say_once(key: str, msg: str):
    """One console line per distinct condition per process.

    The damaged-view line is printed from _save_index, which runs on every
    journal write. A real outage would otherwise put the same sentence in the
    Railway log a few times a minute and bury everything else."""
    with _LOCK:
        if key in _SAID:
            return
        _SAID.add(key)
    print(msg)


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def events_dir():
    """DATA_DIR/events, made on demand. Read from config every call on purpose:
    the tests repoint DATA_DIR, and a path captured at import would write into
    the real runtime state."""
    d = config.DATA_DIR / "events"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise JournalUnavailable(f"cannot create {d}: {e}") from e
    return d


def daily_path(day=None):
    """The append only file for one ET calendar day."""
    day = day or _today()
    return events_dir() / f"events-{day}.jsonl"


def index_path():
    return events_dir() / "open_intents.json"


def _salt_path():
    return events_dir() / ".recipient_salt"


def _today() -> str:
    """The ET calendar day a line belongs to. ET, not UTC, because every other
    day key in this bot (the sniper reservation, the once a day report guards,
    the ledger rows) is an ET date, and two different notions of "today" on one
    volume is how a reservation stops matching its own journal."""
    return f"{_et_now():%Y-%m-%d}"


def _et_now():
    """ET now, without importing scanner (which would import half the bot)."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------
def candidate_id_for(**inputs) -> str:
    """A stable id for ONE observation, derived from the observation's own
    inputs.

    The point of hashing the INPUTS rather than a clock: Astra section 5 says a
    15 second recheck of the same unchanged bar is not four independent
    candidates. Feed the bar boundary, not the wall clock, and the same
    unchanged bar re-scanned every poll collapses to one candidate."""
    raw = "|".join(f"{k}={inputs[k]!r}" for k in sorted(inputs))
    return "c" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:15]


def new_decision_id() -> str:
    """The strategy's commitment, minted once and never derived from the
    candidate. Multiple observations of a setup are not permission to fire
    another trade, so the two ids must not be the same value wearing two
    names."""
    return "d" + uuid.uuid4().hex[:23]


def new_position_id(prefix="pos") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _salt() -> bytes:
    """The per volume secret the recipient refs are keyed under. Created once,
    never exported, never printed."""
    with _LOCK:
        if _SALT[0] is not None:
            return _SALT[0]
        p = _salt_path()
        try:
            if p.exists():
                _SALT[0] = bytes.fromhex(p.read_text(encoding="utf-8").strip())
                return _SALT[0]
        except (OSError, ValueError):
            pass
        raw = os.urandom(32)
        try:
            p.write_text(raw.hex(), encoding="utf-8")
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass           # windows does not do posix modes; not fatal
        except OSError:
            # an unwritable salt is a degraded ref (it changes next boot), not
            # a reason to leak a raw chat id into the record
            pass
        _SALT[0] = raw
        return raw


def recipient_ref(chat_id) -> str:
    """An opaque, stable-per-volume handle for one recipient. The raw id never
    enters a journal line, the index, a report or a health text."""
    return hmac.new(_salt(), str(chat_id).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:12]


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------
def _attempt_lost(d) -> bool:
    """An ATTEMPTED recipient whose attempt was written by a process that is
    gone. The request may have reached Telegram and it may never have left the
    machine, and there is no way to tell those apart from here: that is exactly
    what UNKNOWN is for, and it is why this one is never re-sent."""
    return (getattr(d, "status", "") == ATTEMPTED
            and str(getattr(d, "send_attempt_by", "") or "") != _process_token())


def _attempt_spent(d) -> bool:
    """An ATTEMPTED recipient this process has tried MAX_DELIVERY_RETRIES times
    and never heard an answer for. Bounded for the same reason a FAILED
    recipient is: one card must not become an endless retry."""
    return (getattr(d, "status", "") == ATTEMPTED
            and int(getattr(d, "attempts", 0) or 0) >= MAX_DELIVERY_RETRIES)


def _attempt_unsettled(d) -> bool:
    """Nothing further can be learned about this attempt by trying again."""
    return _attempt_lost(d) or _attempt_spent(d)


@dataclass
class Delivery:
    recipient_index: int
    recipient_ref: str
    status: str = PENDING
    attempts: int = 0
    parts_total: int = 1
    parts_confirmed: int = 0
    provider_message_id: object = None
    error_class: str = ""
    send_attempt_at_utc: str = ""
    acknowledged_at_utc: str = ""
    # which life of the bot wrote the ATTEMPTED line. Empty on any record
    # written before this field existed, and an empty value reads as "not this
    # process", which is the safe direction: report it, do not re-send it.
    send_attempt_by: str = ""

    @property
    def resolved(self) -> bool:
        """A recipient nothing further will be done for. UNKNOWN counts:
        it is never auto-retried, it is REPORTED, because re-sending an
        ambiguous acknowledgment is how one card becomes two trades."""
        if self.status in (CONFIRMED, UNKNOWN):
            return True
        if self.status == FAILED and (self.error_class in TERMINAL_ERROR_CLASSES
                                      or self.attempts >= MAX_DELIVERY_RETRIES):
            return True
        # The cap used to bound a FAILED recipient only. A recipient left
        # ATTEMPTED was unresolvable at ANY attempt count, so it sat in
        # retryable_recipients for ever and replay put the same card back on
        # the wire on every promotion into ACTIVE. The bound is the same bound.
        if _attempt_spent(self):
            return True
        return False


@dataclass
class Intent:
    journal_id: str
    kind: str
    candidate_id: str = ""
    decision_id: str = ""
    position_id: str = ""
    strategy_id: str = ""
    event_key: str = ""
    decided_at_utc: str = ""
    session_date: str = ""
    payload: dict = field(default_factory=dict)
    text: str = ""
    text_sha: str = ""
    recipients: list = field(default_factory=list)
    selected: bool = True
    delivery_confirmed: bool = False
    linked: dict = field(default_factory=dict)
    resolved: bool = False
    duplicate: bool = False

    def deliveries(self):
        return [d if isinstance(d, Delivery) else Delivery(**d)
                for d in self.recipients]

    def any_attempt(self) -> bool:
        """Has this intent ever put a request on the wire? The reservation
        lifecycle turns on exactly this question: after an attempt, an
        ambiguous delivered request is not an unsent opportunity."""
        return any(d.attempts > 0 or d.status != PENDING
                   for d in self.deliveries())

    def unresolved_recipients(self):
        return [d.recipient_index for d in self.deliveries() if not d.resolved]

    def retryable_recipients(self):
        """The recipients replay may put back on the wire.

        An ATTEMPTED one is included ONLY while this process wrote the attempt
        and has not spent its retries: the request may never have left the
        machine, so one more try is right. An attempt an earlier life of the
        bot left behind is NOT retried. It may already have arrived, and
        re-sending it is how one card becomes two reported trades."""
        return [d.recipient_index for d in self.deliveries()
                if not d.resolved and d.status in (PENDING, ATTEMPTED, FAILED)
                and not _attempt_unsettled(d)]


def _as_intent(d) -> Intent:
    if isinstance(d, Intent):
        return d
    allowed = {f for f in Intent.__dataclass_fields__}
    return Intent(**{k: v for k, v in d.items() if k in allowed})


# ---------------------------------------------------------------------------
# the append only files
# ---------------------------------------------------------------------------
def _next_seq(day) -> int:
    """A strictly increasing sequence within one daily file, seeded from what
    is already on disk so a restart does not reuse numbers."""
    if _SEEDED[0] != day:
        res = storage_io.read_jsonl(daily_path(day))
        if res.status == "unreadable":
            # An append CAN still land in a file whose bytes the OS will not
            # hand back: a range lock on the head, an indexer or a scanner
            # holding a read handle. Seeding from `res.value or []` restarted
            # the counter at 1 and quietly reused the numbers of the lines that
            # are really in there, so the ordering evidence of a whole day
            # stopped being evidence. A corrupt file is a different answer: its
            # unparseable lines are folded by nobody, so the parseable prefix
            # is a safe seed and the day keeps working.
            raise JournalUnavailable(
                f"cannot read the sequence top of {daily_path(day).name}: "
                f"{res.error}")
        top = 0
        for rec in (res.value or []):
            try:
                top = max(top, int(rec.get("seq") or 0))
            except (TypeError, ValueError):
                continue
        _SEQ[0] = top
        _SEEDED[0] = day
    _SEQ[0] += 1
    return _SEQ[0]


def _append(record: dict, day=None):
    """One line, fsynced, under the daily file's lock. Raises rather than
    returning a failure: every caller of this treats a lost line as a lost
    event, and there is no safe way to carry on from one."""
    day = day or _today()
    with _LOCK:
        record = dict(record)
        record["schema_version"] = SCHEMA_VERSION
        record["seq"] = _next_seq(day)
        record.setdefault("ts_utc", _utc_now_iso())
        res = storage_io.append_jsonl(daily_path(day), record, durable=True)
    if not res.ok:
        _WRITE_FAILURES[0] += 1
        raise JournalUnavailable(f"{res.status}: {res.error}")
    return record


# ---------------------------------------------------------------------------
# the index (a materialized view, never the only truth)
# ---------------------------------------------------------------------------
def _empty_index():
    # "applied" is how many durable lines of each daily file this view has
    # folded. It is the whole of what makes a STALE view detectable: the files
    # are append only, so a count that no longer matches the file is proof that
    # lines landed which this view never saw.
    return {"schema_version": SCHEMA_VERSION, "intents": {}, "keys": {},
            "applied": {}}


def _day_of(path) -> str:
    """The ET day one daily file holds, read from its own name."""
    name = getattr(path, "name", str(path))
    if name.startswith("events-") and name.endswith(".jsonl"):
        return name[len("events-"):-len(".jsonl")]
    return name


def _daily_files():
    """Every daily file on the volume, oldest first.

    EVERY one, not a window. _compact promises an unresolved intent is never
    dropped at any age and this module's own docstring promises that losing the
    index loses no event; a rebuild that walked back only INDEX_RETAIN_DAYS + 1
    days broke both silently, dropping anything still owed after three days and
    then publishing the truncated view over the good one. ISO day names sort
    chronologically, so sorted() is the replay order."""
    try:
        return sorted(events_dir().glob("events-*.jsonl"))
    except OSError as e:
        raise JournalUnavailable(f"cannot list {events_dir()}: {e}") from e


def _set_applied(idx: dict, day: str, n: int):
    idx.setdefault("applied", {})[day] = int(n)


def _bump_applied(idx: dict, day: str, n: int = 1):
    """Account for lines this view just folded from one daily file. A day
    already marked untrusted (-1) stays untrusted until a clean read of it."""
    ap = idx.setdefault("applied", {})
    cur = ap.get(day)
    if not isinstance(cur, int):
        ap[day] = int(n)
    elif cur >= 0:
        ap[day] = cur + int(n)


def _fold_file(idx: dict, path, damaged: list, skip_if_current=False):
    """Fold one daily file into the index, and CHECK the read's status.

    read_jsonl returns value=None for a file the OS would not hand over, so
    folding `res.value or []` turned an unreadable day into zero events with no
    error, no counter and no health line. That is the missing-versus-unreadable
    collapse storage_io was written to prevent, and it sat inside the fallback
    whose own comment says never to start from empty on a file we could not
    read. A corrupt file still yields its parseable prefix, which is real
    evidence and is folded, and the damage is recorded either way."""
    res = storage_io.read_jsonl(path)
    recs = [r for r in (res.value or []) if isinstance(r, dict)]
    seen = len(recs) + int(res.bad_lines or 0)
    day = _day_of(path)
    if res.status != "ok":
        damaged.append({"file": getattr(path, "name", str(path)),
                        "status": res.status, "bad_lines": int(res.bad_lines or 0),
                        "truncated": bool(res.truncated),
                        "error": str(res.error or "")[:120]})
    elif skip_if_current:
        cur = (idx.get("applied") or {}).get(day)
        if isinstance(cur, int) and cur == seen:
            return                      # nothing has landed since the last fold
    for rec in recs:
        _apply(idx, rec)
    # a damaged file's count is never trusted as up to date, so the next load
    # folds it again instead of skipping it as unchanged
    _set_applied(idx, day, seen if res.status == "ok" else -1)


def _catch_up(idx: dict) -> dict:
    """Fold every durable line the index on disk has not seen yet.

    A STALE index is perfectly READABLE, so the old rule (rebuild it when it is
    missing or unreadable) never fired for one. A crash between _append and
    _save_index leaves exactly that, and so does a single denied index publish:
    the line is durable, the view is not, and every read after it, including
    the event_key duplicate guard that stops an exit leg being reported twice,
    was answering from a book with a page torn out.

    Folding a file twice is a no op. Every line about one intent is appended to
    that intent's own daily file, and they are folded in file order, so a
    re-fold lands on the same state it built the first time."""
    damaged = []
    for p in _daily_files():
        _fold_file(idx, p, damaged, skip_if_current=True)
    if damaged:
        idx["damaged"] = damaged
    else:
        idx.pop("damaged", None)
    return idx


def _recheck_damage(idx: dict) -> list:
    """Read the damaged files again, and fold whatever they now give up.

    Without this the view would stay unpublishable for the whole life of the
    process even after the fault cleared, because the damage is only looked for
    on a cold load. The WinError 5 this is written for is an indexer or a
    scanner holding a handle and is usually gone a few milliseconds later. A
    file that has been moved away instead of repaired stays damaged, on
    purpose: its events are not in the view and saying otherwise would be the
    lie this whole path exists to stop."""
    still = []
    for d in list(idx.get("damaged") or []):
        _fold_file(idx, events_dir() / str(d.get("file") or ""), still)
    if still:
        idx["damaged"] = still
    else:
        idx.pop("damaged", None)
    return still


def _load_index() -> dict:
    with _LOCK:
        if _INDEX_CACHE[0] is not None:
            return _INDEX_CACHE[0]
        res = storage_io.read_json(index_path())
        if res.usable and isinstance(res.value, dict) \
                and isinstance(res.value.get("intents"), dict):
            # readable is not the same as current, so the view is caught up
            # against the files before anybody is allowed to answer from it
            _INDEX_CACHE[0] = _catch_up(res.value)
        else:
            # missing, unreadable or the wrong shape: rebuild from the files
            # that ARE the truth. Never start from empty on a file we could not
            # read, which is the mistake that made a truncated sniper book look
            # like an empty one.
            _INDEX_CACHE[0] = _rebuild(persist=False)
        return _INDEX_CACHE[0]


def _save_index(idx: dict):
    """Publish the view. A failure here is a degradation, not a lost event: the
    daily files still carry everything and _load_index folds them forward.

    A view carrying DAMAGE is not published at all. Overwriting a good index
    with one built over a file the OS would not hand over is how one unreadable
    day becomes a permanently empty one, and it is the same mistake that made a
    truncated sniper book look like an empty book. The daily files stay the
    truth. The damaged files are read again on the way in here, so the first
    journal write after a transient fault clears republishes on its own."""
    if idx.get("damaged"):
        try:
            _recheck_damage(idx)
        except JournalUnavailable as e:
            # publishing the view is a degradation path and must never become
            # the exception that makes an intent already on disk look
            # uncommitted to its caller
            print(f"event_journal: could not re-read the damaged day(s): {e}")
    dmg = idx.get("damaged") or []
    if dmg:
        # deliberately NOT counted as a write failure: nothing was attempted
        # and lost, this module chose not to publish. health()'s damaged_files
        # is the count that carries this, and keeping write_failures meaning
        # "a write we tried and lost" is what makes it worth reading.
        _say_once("damaged:" + ",".join(sorted(d["file"] for d in dmg)),
                  f"event_journal: NOT publishing {index_path().name}: "
                  + ", ".join(f"{d['file']} {d['status']}" for d in dmg[:3])
                  + ". Those days could not be read in full, so this view is "
                    "incomplete and must not replace the one on disk. The "
                    "daily files still carry every event.")
        return False
    res = storage_io.write_json(index_path(), _compact(idx), indent=1)
    if not res.ok:
        _WRITE_FAILURES[0] += 1
        print(f"event_journal: could not publish {index_path().name}: "
              f"{res.status} {res.error}; the daily files still carry every "
              "event and the index folds them forward on the next load")
    return bool(res.ok)


def _compact(idx: dict) -> dict:
    """Drop only RESOLVED intents older than the retention window. A
    compaction that would drop an unresolved intent is refused, because that
    intent is the only record that something is owed."""
    cutoff = (_et_now().date() - timedelta(days=INDEX_RETAIN_DAYS)).isoformat()
    keep = {}
    for jid, rec in (idx.get("intents") or {}).items():
        if not rec.get("resolved"):
            keep[jid] = rec                     # never dropped, at any age
            continue
        if str(rec.get("session_date") or "") >= cutoff:
            keep[jid] = rec
    keys = {k: v for k, v in (idx.get("keys") or {}).items() if v in keep}
    # the fold watermarks ride through compaction. Dropping them would make
    # every load re-fold every file and, worse, would re-admit the resolved
    # intents this pass just dropped.
    return {"schema_version": SCHEMA_VERSION, "intents": keep, "keys": keys,
            "applied": dict(idx.get("applied") or {})}


def _rebuild(persist=True) -> dict:
    """Replay every daily file into an index. This is what makes the index a
    view: delete it, lose nothing.

    Compacted before it is returned, so replaying all of history does not put
    all of history in memory: what survives is every unresolved intent at any
    age, plus the resolved ones inside the retention window."""
    idx = _empty_index()
    damaged = []
    for p in _daily_files():
        _fold_file(idx, p, damaged)
    idx = _compact(idx)
    if damaged:
        idx["damaged"] = damaged
    if persist:
        _save_index(idx)               # refuses on its own when damaged
    return idx


def _apply(idx: dict, rec: dict, day=None):
    """Fold one journal line into the index. The ONE place a record's meaning
    is interpreted, so a live update and a rebuild can never disagree.

    `day` is passed by the live write paths only, where exactly one line has
    just been appended to that day's file. A fold from disk counts the whole
    file instead, in _fold_file."""
    if day:
        _bump_applied(idx, day, 1)
    kind = rec.get("kind")
    jid = rec.get("journal_id")
    if not jid:
        return
    intents = idx.setdefault("intents", {})
    if kind == "intent":
        allowed = {f for f in Intent.__dataclass_fields__}
        row = {k: v for k, v in rec.items() if k in allowed}
        # the line's own "kind" is the RECORD type (intent / delivery / link /
        # resolve); the Intent's kind is what the alert IS (entry, exit,
        # sniper_entry, sniper_exit). Two different words in one field name is
        # how replay ended up dispatching every intent as kind "intent" and
        # re-linking none of them.
        row["kind"] = rec.get("intent_kind") or ""
        row.setdefault("resolved", False)
        intents[jid] = row
        if rec.get("event_key"):
            idx.setdefault("keys", {})[rec["event_key"]] = jid
        return
    row = intents.get(jid)
    if row is None:
        return                       # a delivery for an intent we never saw
    if kind == "delivery":
        for d in row.get("recipients", []):
            if d.get("recipient_index") == rec.get("recipient_index"):
                for f in ("status", "attempts", "parts_total",
                          "parts_confirmed", "provider_message_id",
                          "error_class", "send_attempt_at_utc",
                          "send_attempt_by", "acknowledged_at_utc"):
                    if rec.get(f) is not None:
                        d[f] = rec[f]
        row["delivery_confirmed"] = any(
            d.get("status") == CONFIRMED for d in row.get("recipients", []))
    elif kind == "link":
        row.setdefault("linked", {}).update(rec.get("linked") or {})
        for f in ("position_id", "candidate_id"):
            if rec.get(f):
                row[f] = rec[f]
    elif kind == "resolve":
        row["resolved"] = True


# ---------------------------------------------------------------------------
# the public write path
# ---------------------------------------------------------------------------
def commit_intent(kind, *, candidate_id, decision_id, position_id, payload,
                  recipients, text, strategy_id="", event_key=None) -> Intent:
    """Write the durable record of a decision BEFORE anything acts on it.

    Returns the Intent. Raises JournalUnavailable if the line did not land,
    and never returns a half committed one: the caller's contract is that a
    returned Intent is on disk.

    `recipients` is the list of chat ids to deliver to and is converted to
    opaque refs here, immediately, so a raw id cannot reach the file even by
    accident. `event_key` makes a commit idempotent: an exit leg re-derived
    after a crash finds its own earlier intent instead of committing a second
    one, which is what stops the same leg being reported twice."""
    day = _today()
    with _LOCK:
        idx = _load_index()
        if event_key:
            prior = (idx.get("keys") or {}).get(event_key)
            if prior and prior in (idx.get("intents") or {}):
                existing = _as_intent(idx["intents"][prior])
                existing.duplicate = True
                return existing
        jid = "j" + uuid.uuid4().hex[:23]
        deliveries = [
            asdict(Delivery(recipient_index=i, recipient_ref=recipient_ref(c)))
            for i, c in enumerate(recipients or [])
        ]
        rec = {
            "kind": "intent",
            "journal_id": jid,
            "intent_kind": str(kind),
            "candidate_id": str(candidate_id or ""),
            "decision_id": str(decision_id or ""),
            "position_id": str(position_id or ""),
            "strategy_id": str(strategy_id or ""),
            "event_key": str(event_key or ""),
            "decided_at_utc": _utc_now_iso(),
            "session_date": day,
            "payload": payload or {},
            # the text itself is kept so replay can finish the delivery, and
            # its hash is kept so a report can prove two sends were the same
            # card without quoting it
            "text": str(text or ""),
            "text_sha": hashlib.sha256(str(text or "").encode("utf-8"))
                                 .hexdigest()[:16],
            "recipients": deliveries,
            # selected means the STRATEGY committed. delivery_confirmed means
            # Telegram acknowledged. Two fields, never one.
            "selected": True,
            "delivery_confirmed": False,
            "linked": {},
            "resolved": False,
        }
        written = _append(dict(rec, kind="intent"))
        rec["seq"] = written["seq"]
        # only after the durable line lands does the view learn about it
        rec_for_index = dict(rec)
        rec_for_index["kind"] = "intent"
        rec_for_index["journal_id"] = jid
        _apply(idx, rec_for_index, day=day)
        _save_index(idx)
        out = _as_intent(idx["intents"][jid])
        out.kind = str(kind)
        return out


def record_attempt(journal_id, recipient_index):
    """One request is about to go on the wire. Written BEFORE the HTTP call so
    a process that dies inside the call still leaves the attempt on disk, which
    is the difference between an unknown delivery and an invisible one.

    The writer's identity goes on the line too, because the line alone could
    not tell those two apart: without it, an attempt left behind by a dead
    process looked exactly like one in flight and replay simply sent the card
    again."""
    return _delivery_line(journal_id, recipient_index, status=ATTEMPTED,
                          bump=True, send_attempt_at_utc=_utc_now_iso(),
                          send_attempt_by=_process_token())


def record_result(journal_id, recipient_index, status,
                  provider_message_id=None, error_class=None,
                  parts_total=None, parts_confirmed=None):
    if status not in DELIVERY_STATES:
        raise ValueError(f"unknown delivery status {status!r}")
    extra = {}
    if provider_message_id is not None:
        extra["provider_message_id"] = provider_message_id
    if error_class is not None:
        extra["error_class"] = str(error_class)
    if parts_total is not None:
        extra["parts_total"] = int(parts_total)
    if parts_confirmed is not None:
        extra["parts_confirmed"] = int(parts_confirmed)
    if status == CONFIRMED:
        extra["acknowledged_at_utc"] = _utc_now_iso()
    return _delivery_line(journal_id, recipient_index, status=status, **extra)


def _delivery_line(journal_id, recipient_index, status, bump=False, **extra):
    with _LOCK:
        idx = _load_index()
        row = (idx.get("intents") or {}).get(journal_id)
        if row is None:
            return False
        attempts = None
        for d in row.get("recipients", []):
            if d.get("recipient_index") == recipient_index:
                attempts = int(d.get("attempts") or 0) + (1 if bump else 0)
        rec = {"kind": "delivery", "journal_id": journal_id,
               "recipient_index": int(recipient_index), "status": status}
        if attempts is not None:
            rec["attempts"] = attempts
        rec.update(extra)
        day = row.get("session_date") or _today()
        _append(rec, day=day)
        _apply(idx, rec, day=day)
        _save_index(idx)
        return True


def mark_linked(journal_id, **ids):
    """Record that the intent reached its position and its ledger row. Astra:
    failed linkage must retry automatically and appear in an orphan report, so
    the linkage itself has to be a durable fact rather than a side effect
    somebody hopes happened."""
    with _LOCK:
        idx = _load_index()
        if journal_id not in (idx.get("intents") or {}):
            return False
        clean = {k: v for k, v in ids.items() if v is not None}
        day = idx["intents"][journal_id].get("session_date") or _today()
        rec = {"kind": "link", "journal_id": journal_id, "linked": clean}
        for f in ("position_id", "candidate_id"):
            if clean.get(f):
                rec[f] = clean[f]
        _append(rec, day=day)
        _apply(idx, rec, day=day)
        _save_index(idx)
        return True


def resolve(journal_id) -> bool:
    """Nothing further is owed for this intent."""
    with _LOCK:
        idx = _load_index()
        row = (idx.get("intents") or {}).get(journal_id)
        if row is None:
            return False
        day = row.get("session_date") or _today()
        rec = {"kind": "resolve", "journal_id": journal_id}
        _append(rec, day=day)
        _apply(idx, rec, day=day)
        _save_index(idx)
        return True


# ---------------------------------------------------------------------------
# reads and reports
# ---------------------------------------------------------------------------
def get(journal_id):
    row = (_load_index().get("intents") or {}).get(journal_id)
    return _as_intent(row) if row else None


def all_intents():
    return [_as_intent(r) for r in (_load_index().get("intents") or {}).values()]


def unresolved():
    return [i for i in all_intents() if not i.resolved]


def unknown_deliveries():
    """Every recipient whose acknowledgment was lost. Reported, never retried:
    the request may have arrived, and guessing turns one card into two.

    An attempt a dead process left behind, and one this process has stopped
    retrying, are counted here BEFORE replay writes the durable UNKNOWN line
    for them. A /health asked between the crash and the recovery must not
    answer zero for a card whose fate nobody knows."""
    out = []
    for i in all_intents():
        for d in i.deliveries():
            if d.status == UNKNOWN:
                why = d.error_class or "acknowledgment lost"
            elif _attempt_lost(d):
                why = "attempt_lost"          # died inside the send
            elif _attempt_spent(d):
                why = "attempt_unacknowledged"
            else:
                continue
            out.append({"journal_id": i.journal_id, "kind": i.kind,
                        "recipient_index": d.recipient_index,
                        "recipient_ref": d.recipient_ref,
                        "decision_id": i.decision_id,
                        "position_id": i.position_id,
                        "text_sha": i.text_sha, "why": why})
    return out


def orphans():
    """Committed intents that could not be finished, each with WHY.

    An orphan is not the same as an unresolved intent: unresolved may just mean
    the send is still in flight. An orphan is one whose linkage or delivery
    cannot proceed without a person looking.

    Read over EVERY intent, not just the unresolved ones. An intent whose
    ledger row will never appear has nothing left to retry, so replay closes
    it; if the report only listed unresolved intents, closing it would be the
    act that hid it."""
    out = []
    for i in all_intents():
        cls = None
        if i.linked.get("ledger_row_missing"):
            cls = "ledger_row_missing"
        elif i.linked.get("ledger_write_refused"):
            cls = "ledger_write_refused"
        elif i.linked.get("position_missing"):
            cls = "position_missing"
        else:
            stuck = [d for d in i.deliveries()
                     if d.status == FAILED and d.attempts >= MAX_DELIVERY_RETRIES]
            if stuck:
                cls = "delivery_exhausted"
            elif any(_attempt_spent(d) for d in i.deliveries()):
                # a SEPARATE class on purpose: this card may well have
                # arrived. Calling it exhausted would tell the owner it did
                # not, which is the one thing an unknown delivery must never
                # be reported as.
                cls = "delivery_unconfirmed"
        if cls:
            out.append({"journal_id": i.journal_id, "kind": i.kind,
                        "orphan_class": cls, "candidate_id": i.candidate_id,
                        "decision_id": i.decision_id,
                        "position_id": i.position_id})
    return out


def health() -> dict:
    """The section 5 health record's journal half. Counts, never contents, and
    never a recipient identifier."""
    un = unresolved()
    oldest = min((i.decided_at_utc for i in un if i.decided_at_utc), default=None)
    try:
        used = sum(p.stat().st_size for p in events_dir().glob("events-*.jsonl"))
    except OSError:
        used = None
    # damaged_files is not cosmetic: while it is non zero the view is known to
    # be incomplete, so every other count on this line is a floor rather than a
    # total, and open_intents.json is deliberately not being republished.
    dmg = len((_load_index().get("damaged") or []))
    return {"queue_depth": len(un), "write_failures": _WRITE_FAILURES[0],
            "bytes_used": used, "oldest_unresolved": oldest,
            "unknown_deliveries": len(unknown_deliveries()),
            "orphans": len(orphans()), "damaged_files": dmg}


def report_text() -> str:
    """The operator-facing lines for /health. No chat ids, no card text."""
    h = health()
    lines = [f"Durable events: {h['queue_depth']} unresolved, "
             f"{h['unknown_deliveries']} unknown deliveries, "
             f"{h['orphans']} orphans."]
    # said first, and said plainly: without it the three counts above read as a
    # clean bill of health on a day whose events nobody can see
    for d in (_load_index().get("damaged") or [])[:5]:
        lines.append(f"  DAMAGED: {d.get('file')} is {d.get('status')}. That "
                     "day could not be read in full, so the counts above are a "
                     "floor and open_intents.json is not being republished.")
    for o in orphans()[:5]:
        lines.append(f"  orphan {o['orphan_class']}: {o['kind']} "
                     f"decision {o['decision_id'][:10]}")
    for u in unknown_deliveries()[:5]:
        lines.append(f"  unknown delivery: recipient #{u['recipient_index']} "
                     f"on {u['kind']} {u['text_sha']}, may or may not have "
                     "arrived, not resent")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------
def replay(send=None, relink=None, now=None) -> dict:
    """Finish every intent that a crash left half done.

    `send(intent, indices) -> list of per recipient result dicts` delivers to
    exactly the recipients named and nobody else. `relink(intent) -> dict`
    re-applies the linkage idempotently and reports what it found. Both are
    supplied by scanner, so this module stays free of the transport and of the
    books.

    Rules, in order of how much damage getting them wrong does:
      - UNKNOWN is never resent. It may already have arrived.
      - CONFIRMED is never resent.
      - an ATTEMPTED recipient a dead process left behind, or one that has
        spent its retries, is SETTLED as UNKNOWN first and then obeys the rule
        above. It used to be handed straight back to send().
      - a FAILED recipient is retried up to MAX_DELIVERY_RETRIES, and only that
        recipient, never the whole broadcast.
      - linkage is re-applied before delivery, so a card that did go out is
        back in its cohort even if the resend then fails."""
    stats = {"intents": 0, "relinked": 0, "resent": 0, "unknown": 0,
             "resolved": 0, "orphans": 0}
    for intent in unresolved():
        stats["intents"] += 1
        # before anything is re-sent: an attempt nothing can resolve becomes a
        # recorded unknown rather than another card on the wire
        if _settle_attempts(intent):
            intent = get(intent.journal_id) or intent
        if relink is not None:
            try:
                found = relink(intent) or {}
            except Exception as e:                       # noqa: BLE001
                found = {"relink_error": str(e)}
            if found:
                mark_linked(intent.journal_id, **found)
                stats["relinked"] += 1
                intent = get(intent.journal_id) or intent
        todo = intent.retryable_recipients()
        if todo and send is not None:
            try:
                results = send(intent, todo) or []
            except Exception as e:                       # noqa: BLE001
                print(f"event_journal: replay send failed for "
                      f"{intent.journal_id}: {e}")
                results = []
            stats["resent"] += len(results)
            intent = get(intent.journal_id) or intent
            # and again after the pass: a send that returned nothing leaves
            # this recipient ATTEMPTED, and that is the attempt count that has
            # just reached the cap
            if _settle_attempts(intent):
                intent = get(intent.journal_id) or intent
        stats["unknown"] += sum(1 for d in intent.deliveries()
                                if d.status == UNKNOWN)
        if all(d.resolved for d in intent.deliveries()) \
                and not _link_pending(intent):
            resolve(intent.journal_id)
            stats["resolved"] += 1
    stats["orphans"] = len(orphans())
    return stats


def _settle_attempts(intent) -> int:
    """Turn an attempt nothing can resolve into a RECORDED unknown delivery.

    record_attempt's whole purpose is that a process which dies inside the HTTP
    call still leaves the attempt on disk. Nothing ever read that line back:
    the recipient stayed ATTEMPTED, retryable_recipients handed it to the next
    replay, and the card went out again. That is the duplicate the five state
    machine exists to prevent, arriving through the state that names it.

    The UNKNOWN is written as a durable line rather than computed, so the fact
    survives this process too. A write that fails leaves the recipient exactly
    as it was, unresolved and unsent, which is the fail closed direction: the
    next replay tries to settle it again."""
    n = 0
    for d in intent.deliveries():
        if not _attempt_unsettled(d):
            continue
        why = "attempt_lost" if _attempt_lost(d) else "attempt_unacknowledged"
        try:
            record_result(intent.journal_id, d.recipient_index, UNKNOWN,
                          error_class=why)
            n += 1
        except JournalUnavailable as e:
            print(f"event_journal: could not settle the unknown delivery for "
                  f"{intent.journal_id} recipient {d.recipient_index}: {e}. "
                  "It stays unresolved and is not re-sent.")
    return n


def _link_pending(intent) -> bool:
    """Is there still a link this intent is waiting on? A missing ledger row is
    NOT pending: it will never appear, so it is an orphan and the intent is
    allowed to close as one rather than being retried forever."""
    return bool(intent.linked.get("retry_link"))


def rebuild_index() -> int:
    with _LOCK:
        _INDEX_CACHE[0] = None
        idx = _rebuild(persist=True)
        _INDEX_CACHE[0] = idx
        return len(idx.get("intents") or {})


def _reset_for_test():
    """Drop every in-process cache. Only the tests call this; a live process
    has one journal for its whole life."""
    with _LOCK:
        _SEQ[0] = 0
        _SEEDED[0] = None
        _SALT[0] = None
        _INDEX_CACHE[0] = None
        _WRITE_FAILURES[0] = 0
        # a new token models the next BOOT, which is what makes an attempt this
        # life wrote distinguishable from one an earlier life left behind
        _PROCESS[0] = None
        _SAID.clear()
