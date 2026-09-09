"""Singleton lease for the one process allowed to poll Telegram and send.

Telegram allows exactly ONE getUpdates consumer per bot token. A second copy
gets 409 Conflict, the two split commands between them, and both fire every
alert. On a shared volume it is worse than noisy: both write positions.json,
so a second copy can mark a position closed in the file the first one then
never alerts on. That is the money bug, so the loser must not monitor at all.

Why the LOCK decides and the state.json record does not
-------------------------------------------------------
An OS advisory lock on DATA_DIR/scanner.lock is the authority. The record
{instance_id, pid, host, seq, heartbeat_ts} written to state.json is only the
observable half: it names the holder in a stand-down DM, in /status, and to
the wedge detector.

That inversion is what survives railway's restartPolicy ALWAYS. The kernel
releases the lock when the holder's handles close, which includes SIGKILL and
a container teardown, so a crashed instance leaves NO stale lock and the
restarted container reclaims on its FIRST attempt: no timeout, no clock read,
no human. A record-only lease would need a staleness timeout, and every
timeout choice is a way to brick the bot on restart. Going silent is far worse
than the double alert this module prevents, so every ambiguous case here
resolves toward "keep alerting".

Nothing branches on a wall clock. heartbeat_ts is for display only. Freshness
is judged solely by whether seq advances, measured against the OBSERVER's own
time.monotonic(). A container clock that jumps forward, backward, or sits
wrong forever changes no decision here.

This does NOT reuse forward_ledger._os_lock. That one is a 0.75s critical
section that unlocks; this one is held for the life of the process and needs a
tri-state answer (acquired / contended / unavailable), where the ledger's
helper collapses contention and no-primitive into the same None.

What it can and cannot see
--------------------------
CAN: two processes sharing DATA_DIR, which is two railway containers on one
volume (a rolling-deploy overlap or an accidental double deploy) and two runs
in the same local DATA_DIR. Detection is immediate, on the first attempt.
CANNOT: the cloud daemon and a desktop "python scanner.py" at the same time.
Different filesystems, no shared state.json, no shared lock file. That pair
stays covered only by the existing 409 warning in scanner._warn_conflict.
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

LOCK_NAME = "scanner.lock"
LEASE_KEY = "instance_lease"
RENEW_S = 20        # how often the holder republishes its record
STALE_S = 180       # seq frozen this long: warn the owner, never take over
STANDBY_POLL_S = 5  # how often a loser re-asks for the lock
ACQUIRE_BUDGET_S = 0.3  # a brief retry so a mid-flight release is not missed
# A copy that has ALREADY stood down and then cannot read the lock at all (a
# read-only remount, EIO or ESTALE on the mount, fd exhaustion) must not read
# that failure as "I am alone": see scanner.ensure_active. There is no
# BLIND_RESUME_S here any more, and that absence is deliberate. A timed way
# back, gated on the holder's published seq standing still, was tried and is
# unsound: the lease record lives on the SAME volume whose failure blinded this
# copy, so that fault also stops the live holder's renew from landing, and a
# frozen seq is a symptom of our own broken disk rather than proof the holder
# died. Acting on it puts two copies on the wire during exactly the fault the
# lease exists to survive. Nothing readable from here separates the two cases,
# so the mute copy stays mute and tells the owner once a day.

# The ONLY errnos that mean "somebody else holds it". Everything else an
# advisory lock can raise means the filesystem cannot lock at all: ENOLCK and
# EOPNOTSUPP on a share or an overlay that has no lock support, EINVAL and
# ENOSYS on a kernel that refuses the call. Reading those as contention would
# make a bot on such a volume silence ITSELF forever, with no second copy
# anywhere. Fail open instead, exactly like a missing primitive.
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
_seq = 0
_last_renew = 0.0
_lock = threading.RLock()


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
    return _fd is not None


def try_acquire(budget_s: float = ACQUIRE_BUDGET_S) -> str:
    """Take the singleton lock. Returns one of:

        "acquired"    this process now holds it
        "held"        it already did, nothing changed
        "contended"   another live process holds it, stand down
        "unavailable" no lock primitive here, so nobody can be checked

    "unavailable" is deliberately NOT "contended": failing closed on a
    platform with no flock would brick the bot, and a silent bot is worse than
    a duplicate card. The caller keeps running and tells the owner once.
    """
    global _fd
    with _lock:
        if _fd is not None:
            return "held"
        if _msvcrt is None and _fcntl is None:
            return "unavailable"
        try:
            fd = os.open(str(lock_path()), os.O_RDWR | os.O_CREAT)
        except OSError as e:
            print(f"instance lock: cannot open {lock_path()} ({e}), "
                  "running without the guard")
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
                return "acquired"
            except OSError as e:
                if e.errno not in _CONTENDED_ERRNOS:
                    # not "someone holds it", but "this filesystem cannot
                    # lock". keep alerting rather than silence ourselves.
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    print(f"instance lock: {lock_path()} cannot be locked on "
                          f"this filesystem ({e}), running without the guard")
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
    death, which is what makes the reclaim path need no timeout."""
    global _fd
    with _lock:
        fd, _fd = _fd, None
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


def status_line(standby: bool = False, degraded: bool = False) -> str:
    """The /status line. Reads the live record, never a typed-in value."""
    if degraded:
        return (f"Instance: active, NO singleton lock on this filesystem "
                f"(id {_ID}, pid {os.getpid()}). A second copy would not be "
                f"caught here.")
    if standby or _fd is None:
        return "Instance: STANDBY, another copy holds the lease: " + holder_line()
    lease = read_lease()
    seq = lease.get("seq") if lease.get("instance_id") == _ID else 0
    return (f"Instance: active (id {_ID}, pid {os.getpid()}, "
            f"renewal {seq})")
