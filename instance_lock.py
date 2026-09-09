"""Ownership of the one process allowed to poll Telegram and send.

Telegram allows exactly ONE getUpdates consumer per bot token. A second copy
gets 409 Conflict, the two split commands between them, and both fire every
alert. On a shared volume it is worse than noisy: both write positions.json,
so a second copy can mark a position closed in the file the first one then
never alerts on. That is the money bug, so a copy that is not the owner must
not monitor at all.

WHAT THIS LOCK CAN SEE
    Cooperating processes that open the SAME lock object on the SAME
    filesystem, subject to that filesystem's locking semantics. Two runs in one
    DATA_DIR are caught immediately, on the first attempt. That is the whole
    list.

WHAT THIS LOCK CANNOT SEE
    Everything in CANNOT_DETECT below, which is the machine readable version
    of the same sentence: another machine, another service or deployment, a
    replica, a copied or independently mounted volume, an old binary that
    ignores this protocol, a desktop Task Scheduler entry, a webhook, an ad hoc
    getUpdates script. Advisory locking only works when every writer
    participates, so a program that does not ask is not excluded by it. Nothing
    here may be read as proof that this process is the only one alive, and no
    owner facing line generated from this module claims otherwise.

DEPLOYMENT TOPOLOGY IS UNPROVEN FROM HERE
    Railway's volume documentation says it PREVENTS simultaneous active
    deployments mounted to the same service volume, so the shared volume
    rolling promotion story a local two process test seems to prove has never
    been reproduced on the platform. Treat it as unproven. The posture this
    release ships is the conservative one: a single service, no external
    production pollers, and a controlled handoff. Do not write code or text
    that asserts a topology nobody has observed.

Why the LOCK decides and the state.json record does not
-------------------------------------------------------
An OS advisory lock on DATA_DIR/scanner.lock is the authority for the part it
can see. The record {instance_id, pid, host, seq, heartbeat_ts} written to
state.json is only the observable half: it names the holder in a stand down DM,
in /status, and to the wedge detector.

That inversion is what survives railway's restartPolicy ALWAYS. The kernel
releases the lock when the holder's handles close, which includes SIGKILL and
a container teardown, so a crashed instance leaves NO stale lock and the
restarted container reclaims on its FIRST attempt: no timeout, no clock read,
no human. A record-only lease would need a staleness timeout, and every
timeout choice is a way to brick the bot on restart.

What that inversion does NOT buy is permission to act when the lock cannot be
read at all. Unknown ownership is not exclusive ownership. A process that
cannot establish ownership goes to BLOCKED and stays quiet, and the way back is
the lock becoming acquirable again, never a timer (see the state table below
and scanner.ensure_active).

The five states
---------------
    STARTING    booted, ownership not established. local checks only.
    STANDBY     another cooperating process holds the lock. bounded retries.
    RECOVERING  lock held, durable state not reconciled yet. no new entries and
                no fresh strategy notifications.
    ACTIVE      ownership held AND every mandatory store reconciled.
    BLOCKED     lock unsupported, storage unhealthy, recovery incomplete or
                ownership uncertain. local health reporting and bounded
                acquisition retries, never activation on a timer.

There is no ACTIVE-DEGRADED. A warning about observation QUALITY (a stale feed,
missing chart data) is a different thing and may fire while ownership is
genuinely held, but it never confers ownership.

Nothing branches on a wall clock. heartbeat_ts is for display only. Freshness
is judged solely by whether seq advances, measured against the OBSERVER's own
time.monotonic(). A container clock that jumps forward, backward, or sits
wrong forever changes no decision here.

This does NOT reuse forward_ledger._os_lock. That one is a 0.75s critical
section that unlocks; this one is held for the life of the process and needs a
tri-state answer (acquired / contended / unavailable), where the ledger's
helper collapses contention and no-primitive into the same None.
"""

import errno
import os
import socket
import threading
import time
import uuid

import config

# Platform: msvcrt on Windows, fcntl on the linux container the worker runs
# in. Both are stdlib, so neither adds a requirement, and both are released by
# the OS when the handle or the process dies. A sentinel lock FILE would
# survive a crash and wedge the bot forever, which is why one is not used.
try:
    import fcntl as _fcntl
except ImportError:                    # Windows
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:                    # Linux
    _msvcrt = None

# O_NOINHERIT on Windows, O_CLOEXEC on linux, 0 on anything that has neither.
# Same intent either way: the handle must not survive into a child.
_NOINHERIT = getattr(os, "O_NOINHERIT", 0) or getattr(os, "O_CLOEXEC", 0)

LOCK_NAME = "scanner.lock"
LEASE_KEY = "instance_lease"
RENEW_S = 20        # how often the holder republishes its record
STALE_S = 180       # seq frozen this long: warn the owner, never take over
STANDBY_POLL_S = 5  # how often a loser re-asks for the lock
ACQUIRE_BUDGET_S = 0.3  # a brief retry so a mid-flight release is not missed

# The ownership states, spelled once so /status, the DMs and the tests all read
# the same string instead of three hand-typed copies drifting apart.
STARTING = "STARTING"
STANDBY = "STANDBY"
RECOVERING = "RECOVERING"
ACTIVE = "ACTIVE"
BLOCKED = "BLOCKED"
STATES = (STARTING, STANDBY, RECOVERING, ACTIVE, BLOCKED)

# Everything a local advisory lock is blind to. This is a LIST rather than a
# paragraph so the honesty is testable: the module docstring, status_line and
# every owner facing stand down or blocked note are generated from it, and a
# check can assert they match. A hand typed sentence goes stale the first time
# somebody adds a way to run a second copy.
CANNOT_DETECT = (
    "another machine",
    "another Railway service or deployment",
    "a second replica of this service",
    "a copied or independently mounted volume",
    "an old binary that ignores this lock",
    "a desktop Task Scheduler entry",
    "a Telegram webhook configuration",
    "an ad hoc getUpdates script",
)


def cannot_detect_line() -> str:
    """One sentence naming what this lock cannot see. Rendered from
    CANNOT_DETECT so the text and the list can never disagree."""
    return ("This lock only sees processes that share this filesystem and "
            "cooperate with it. It cannot see " + ", ".join(CANNOT_DETECT) + ".")
# A copy that cannot read the lock at all (a read-only remount, EIO or ESTALE
# on the mount, fd exhaustion, a full volume) must not read that failure as "I
# am alone", whether or not it has ever seen a holder: see
# scanner.ensure_active, where it goes to BLOCKED. There is no BLIND_RESUME_S
# here and there is no timer of any other name, and that absence is deliberate.
# A timed way back, gated on the holder's published seq standing still, was
# tried and is unsound: the lease record lives on the SAME volume whose failure
# blinded this copy, so that fault also stops the live holder's renew from
# landing, and a frozen seq is a symptom of our own broken disk rather than
# proof the holder died. Acting on it puts two copies on the wire during
# exactly the fault the lease exists to survive. Nothing readable from here
# separates the two cases, so the mute copy stays mute and tells the owner once
# a day.

# The ONLY errnos that mean "somebody else holds it". Everything else an
# advisory lock can raise means the filesystem cannot lock at all: ENOLCK and
# EOPNOTSUPP on a share or an overlay that has no lock support, EINVAL and
# ENOSYS on a kernel that refuses the call. Those are not contention, they are
# "ownership cannot be established here", which is a different answer with a
# different owner facing message: the fix is a volume that can lock, not
# hunting for a second copy that does not exist. Both end in a quiet bot, so
# telling them apart is the only kindness available.
_CONTENDED_ERRNOS = {
    errno.EACCES,                                  # msvcrt LK_NBLCK on Windows
    errno.EAGAIN,                                  # fcntl LOCK_NB on Linux
    getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
    getattr(errno, "EDEADLK", errno.EACCES),
    getattr(errno, "EDEADLOCK", errno.EACCES),     # msvcrt's spelling
}

_ID = uuid.uuid4().hex[:12]
# The fd MUST live on a module global. A local would be garbage collected, and
# closing the handle releases the lock, so the process would quietly stop being
# the singleton while still believing it was.
_fd = None
# The pid that actually took the lock. A child that inherits or duplicates the
# handle would otherwise read _fd is not None as "I own this", and then two
# processes both believe they are the singleton off ONE acquisition. Astra
# section 3 names it: do not assume killing one parent releases a handle
# retained by a child.
_owner_pid = None
_seq = 0
_last_renew = 0.0
_lock = threading.RLock()

# The one place that answers "what am I allowed to do right now". scanner drives
# it, telegram and /status read it. Boot value is STARTING because a process
# that has not asked for the lock yet owns nothing, and that is the safe end of
# the question.
_state = (STARTING, "process booted, ownership not established")


def state() -> str:
    """The current ownership state, one of STATES."""
    return _state[0]


def state_reason() -> str:
    """Why the process is in that state, for /status and the ops DM."""
    return _state[1]


def set_state(name: str, reason: str = ""):
    """Record the state. Callers are scanner's state machine and the tests;
    nothing here acts on it, because acting on ownership is exactly the
    decision that must stay in one place."""
    global _state
    if name not in STATES:
        raise ValueError(f"unknown ownership state {name!r}")
    _state = (name, reason or "")


def instance_id() -> str:
    return _ID


def _host() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def lock_path():
    """The lock file, derived from the CURRENT DATA_DIR. Tests repoint
    config.DATA_DIR at a temp dir, so this can never be captured at import
    (same reason as forward_ledger._lock_path)."""
    return config.DATA_DIR / LOCK_NAME


def holding() -> bool:
    """True only for the process that actually TOOK the lock.

    The pid check is the whole point. A forked or spawned child inherits the
    parent's open handle and would otherwise answer True here off an
    acquisition it never made, so two processes would both believe they are the
    singleton. The handle is opened non inheritable as well; this is the second
    belt, for a duplicated fd or an interpreter that hands one down anyway."""
    return _fd is not None and _owner_pid == os.getpid()


def try_acquire(budget_s: float = ACQUIRE_BUDGET_S) -> str:
    """Ask for the singleton lock. Returns one of:

        "acquired"    this process now holds it
        "held"        it already did, nothing changed
        "contended"   another cooperating process holds it, stand down
        "unavailable" OWNERSHIP IS UNKNOWN: there is no lock primitive here, or
                      the lock file could not be opened at all

    "unavailable" is deliberately NOT "contended", because the two need
    different handling, and it is just as deliberately NOT a licence. It says
    the question could not be answered, and an unanswered ownership question is
    not ownership: see scanner.ensure_active, where it lands in BLOCKED. This
    function reports; it never decides.
    """
    global _fd, _owner_pid
    with _lock:
        if _fd is not None and _owner_pid == os.getpid():
            return "held"
        if _msvcrt is None and _fcntl is None:
            return "unavailable"
        try:
            # non inheritable on purpose: a child must not receive a handle
            # that keeps the lock alive after this process dies, which would
            # turn a crash into a wedge nobody can clear without finding the
            # child. os.open already defaults to this on py3, and saying so
            # here keeps it from being changed by accident.
            fd = os.open(str(lock_path()), os.O_RDWR | os.O_CREAT | _NOINHERIT)
            try:
                os.set_inheritable(fd, False)
            except (OSError, AttributeError):
                pass
        except OSError as e:
            print(f"instance lock: cannot open {lock_path()} ({e}), "
                  "so ownership is UNKNOWN here")
            return "unavailable"
        deadline = time.monotonic() + max(0.0, budget_s)
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if _msvcrt is not None:
                    _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
                else:
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                _fd = fd
                _owner_pid = os.getpid()
                return "acquired"
            except OSError as e:
                if e.errno not in _CONTENDED_ERRNOS:
                    # not "someone holds it", but "this filesystem cannot
                    # lock", so ownership cannot be established here at all.
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    print(f"instance lock: {lock_path()} cannot be locked on "
                          f"this filesystem ({e}), so ownership is UNKNOWN")
                    return "unavailable"
                # the other side may be one syscall away from releasing (a
                # rolling deploy tearing the old container down), so spend a
                # few hundred ms before declaring contention
                if time.monotonic() >= deadline:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    return "contended"
                time.sleep(0.01)


def release():
    """Drop the lock. This is also exactly what the kernel does at process
    death, which is what makes the reclaim path need no timeout.

    The lock FILE is left where it is, always. Unlinking or replacing it while
    any owner exists hands the next process a lock on a different inode, which
    excludes nobody, and then two copies both believe they own the token."""
    global _fd, _owner_pid
    with _lock:
        fd, _fd = _fd, None
        _owner_pid = None
        if fd is None:
            return
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if _msvcrt is not None:
                _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
            elif _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


def renew(force: bool = False) -> bool:
    """Republish the observable record, at most once every RENEW_S. Returns
    True when it wrote. seq is seeded from whatever record is already there on
    a takeover, so it never moves backwards and a watcher cannot read a fresh
    holder as a frozen one."""
    global _last_renew, _seq
    now = time.monotonic()
    if not force and _last_renew and (now - _last_renew) < RENEW_S:
        return False
    _last_renew = now

    def _bump(cur):
        global _seq
        prior = cur if isinstance(cur, dict) else {}
        base = _seq
        if prior.get("instance_id") != _ID:  # takeover: continue their count
            try:
                base = max(base, int(prior.get("seq", 0)))
            except (TypeError, ValueError):
                pass
        _seq = base + 1
        return {"instance_id": _ID, "pid": os.getpid(), "host": _host(),
                "seq": _seq, "heartbeat_ts": time.time()}

    try:
        config.state_update(LEASE_KEY, _bump, default={})
    except OSError as e:  # a volume hiccup must never end the process
        print(f"instance lock: lease renewal skipped ({e})")
        return False
    return True


def read_lease() -> dict:
    """The published record, or {} when there is none or it is not a dict."""
    try:
        rec = config.state_get(LEASE_KEY, {})
    except OSError:
        return {}
    return rec if isinstance(rec, dict) else {}


class SeqWatch:
    """How long a holder's seq has stood still, on the OBSERVER's own
    monotonic clock. Resets on any seq or instance_id change.

    The wall clock is never consulted, on purpose. A container whose clock is
    wrong, or jumps, must not be able to make a live winner look dead."""

    def __init__(self):
        self._key = None
        self._since = None

    def observe(self, lease, now=None) -> float:
        now = time.monotonic() if now is None else now
        lease = lease if isinstance(lease, dict) else {}
        key = (lease.get("instance_id"), lease.get("seq"))
        if key != self._key or self._since is None:
            self._key = key
            self._since = now
            return 0.0
        return max(0.0, now - self._since)


# A heartbeat_ts this far in the observer's future, or this far in its past, is
# a clock that disagrees with ours rather than an age worth printing.
_SKEW_FUTURE_S = 60
_SKEW_PAST_S = 30 * 86400


def _renewal_text(ts) -> str:
    try:
        age = time.time() - float(ts)
    except (TypeError, ValueError):
        return "last renewal time unknown (clock skew)"
    if age < -_SKEW_FUTURE_S or age > _SKEW_PAST_S:
        return "last renewal time unknown (clock skew)"
    age = max(0.0, age)
    if age < 90:
        return f"last renewal {age:.0f}s ago"
    return f"last renewal {age / 60:.0f} min ago"


def holder_line(lease=None) -> str:
    """One human line naming whoever holds the lease, for a DM or /status."""
    lease = read_lease() if lease is None else (lease if isinstance(lease, dict) else {})
    if not lease:
        return "no lease record published yet"
    return (f"instance {lease.get('instance_id', '?')}, "
            f"pid {lease.get('pid', '?')}, "
            f"host {lease.get('host', '?')}, "
            f"{_renewal_text(lease.get('heartbeat_ts'))}")


def status_line(state_name: str = None) -> str:
    """The /status line, one per ownership state. Reads the live record and the
    live state, never a typed-in value.

    The old version had a "degraded" line reading "active, NO singleton lock on
    this filesystem". That sentence was the unsafe default written down: it
    announced that ownership was unknown and that the bot was alerting anyway.
    There is no line for that any more, because there is no such state."""
    st = state() if state_name is None else state_name
    who = f"id {_ID}, pid {os.getpid()}"
    if st == BLOCKED:
        return (f"Instance: BLOCKED, NOT sending ({who}). "
                f"{state_reason() or 'ownership could not be established'}. "
                + cannot_detect_line())
    if st == STANDBY:
        return "Instance: STANDBY, another copy holds the lease: " + holder_line()
    if st == RECOVERING:
        return (f"Instance: RECOVERING, holding the lock but not yet sending "
                f"({who}). {state_reason() or 'reconciling saved state'}.")
    if st == STARTING:
        return f"Instance: STARTING, ownership not established yet ({who})."
    if not holding():
        # ACTIVE without the handle is a bookkeeping bug, not a state. Say so
        # rather than printing a confident "active" line nobody should trust.
        return (f"Instance: ACTIVE was recorded without the lock handle "
                f"({who}). Treat this as unknown ownership and restart me.")
    lease = read_lease()
    seq = lease.get("seq") if lease.get("instance_id") == _ID else 0
    return f"Instance: active ({who}, renewal {seq})"
