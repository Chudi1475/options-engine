"""One protocol for putting this bot's files on disk, and an honest statement
of what it does and does not promise.

Why it exists
-------------
Every writer used to roll its own staging file, its own naming, its own retry
and its own idea of what a failure meant. positions.save retried a denied
replace once. sniper_book caught the same error, printed, and handed the
caller a row for a write that never landed. forward_ledger did not retry at
all and lost a whole grading pass to a blip. config had a thread lock, which
says nothing at all across two processes on one volume. And on this machine a
Windows PermissionError [WinError 5] really does hit roughly one run in ten,
on a different file each time. One protocol, one retry policy, one set of
statuses, one lock.

WHAT IS ATOMIC
    The publication of one file's whole contents. A reader sees the entire old
    file or the entire new one, never a mixture. That is a staged write plus
    os.replace, and it is all the atomicity a filesystem hands out for free.
    One appended JSONL line, written under that file's lock in one call, is
    the other atomic unit here.

WHAT IS NOT ATOMIC
    Anything spanning two files. positions.json, sniper_positions.json and
    state.json are three independent replacements. A crash between them leaves
    each file self consistent and the SET of them inconsistent, and nothing in
    this module can prevent that or detect it afterwards. There is no journal
    here. Cross file intent belongs to a durable event journal (Astra W02);
    this module does not pretend to be one, and a caller must not read a
    successful write_json as a committed multi file transaction.

WHAT IS NOT CONCURRENCY CONTROL
    The lock serialises cooperating writers that go through this module, on
    one filesystem, using a sidecar lock file beside the target. It is
    advisory. Another machine, a copied volume, an old binary that ignores the
    protocol, or a script that opens the file directly is not coordinated by
    it, and a network filesystem may not honour it at all. When the lock
    cannot be taken the caller is TOLD, and the caller decides. This module
    never quietly proceeds unlocked.

WHAT IS NOT UNLIMITED RETENTION
    The deployment volume is small. Free space is checked before a write and
    a full disk comes back as a status, not an exception and not a lie. Any
    caller that appends without bound owes its own rotation policy;
    volume_status is here so it can measure instead of assume.

The distinction the whole thing rests on
----------------------------------------
A file that is MISSING and a file that is UNREADABLE are different answers.
Collapsing them into an empty list is what let one truncated sniper book get
republished as a single row, taking two live stops with it. So every read
returns a status, `usable` means the value can be used, `trusted` means the
absence of a value is a fact rather than a mystery, and no writer in this repo
may publish over a file it could not read.
"""

import contextlib
import errno
import itertools
import json
import os
import random
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl as _fcntl
except ImportError:                    # Windows
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:                    # Linux
    _msvcrt = None

# Bounded, because Astra's acceptance line is "retries bounded". Five attempts
# with a doubling backoff is about 0.3s of waiting in the worst case, which is
# short enough to sit inside the 15 second poll loop and long enough to ride
# out the antivirus or indexer that holds a handle for a moment.
MAX_REPLACE_ATTEMPTS = 5
_BACKOFF_START_S = 0.02
_BACKOFF_CAP_S = 0.2

# the default wait for a file lock. Long enough that a cooperating writer
# finishing its publish is not a failure, short enough that a wedged holder
# does not stall a scan.
DEFAULT_LOCK_BUDGET_S = 1.0

# refuse a write that would leave the volume with less headroom than this. A
# 500 MB volume is not unlimited retention and must not be driven to zero.
FREE_MARGIN_BYTES = 256 * 1024

READ_STATUSES = ("ok", "missing", "unreadable", "corrupt")
WRITE_STATUSES = ("written", "replace_denied", "disk_full", "lock_unavailable",
                  "encode_failed", "failed")

# indirection on purpose: a fault test needs to make the publish fail the way
# Windows makes it fail, and it cannot do that if the name is bound inside a
# function body.
_replace = os.replace

# one staging name per publish, for the life of the process
_SEQ = itertools.count()

_COUNTERS = {
    "writes": 0,
    "write_failures": 0,
    "replace_retries": 0,
    "reads_unreadable": 0,
    "reads_corrupt": 0,
    "locks_unavailable": 0,
    "orphans_swept": 0,
}
_COUNTER_LOCK = threading.Lock()


def _bump(name, n=1):
    with _COUNTER_LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0) + n


def counters() -> dict:
    """A snapshot for the health record: writes, failures, retries, bad reads,
    unavailable locks, swept orphans. Counts, never file contents."""
    with _COUNTER_LOCK:
        return dict(_COUNTERS)


class LockUnavailable(Exception):
    """file_lock(require=True) could not take the lock. Raised rather than
    yielding an unheld lock, so a caller that asked for exclusivity cannot
    accidentally carry on without it."""


@dataclass
class ReadResult:
    """What came back, and whether it can be believed.

    status   ok        the file parsed
             missing   there is no file, which is a real and trustworthy state
             unreadable the file is there and the OS would not hand it over
             corrupt   the bytes are there and they do not parse
    usable   the value may be used
    trusted  the ANSWER may be used. A missing file is trusted (there is
             genuinely nothing recorded); an unreadable or corrupt one is not,
             and a caller that treats it as empty is inventing a fact.
    """
    status: str
    value: object = None
    error: str = ""
    path: object = None
    bad_lines: int = 0
    truncated: bool = False

    @property
    def usable(self) -> bool:
        return self.status == "ok"

    @property
    def trusted(self) -> bool:
        return self.status in ("ok", "missing")


@dataclass
class WriteResult:
    """Whether the bytes landed, and if not, why not in one word."""
    ok: bool
    status: str
    attempts: int = 0
    bytes: int = 0
    error: str = ""
    path: object = None

    def __bool__(self) -> bool:
        return bool(self.ok)


@dataclass
class Lock:
    """A held (or not held) file lock. `held` is the only thing a caller
    should branch on, and `why` is what it tells the operator when it stands
    down."""
    held: bool
    why: str = ""
    path: object = None
    fd: object = None
    dev: int = -1
    ino: int = -1

    def revalidate(self) -> bool:
        """Is the sidecar we locked still the sidecar at that path?

        Astra section 3: do not replace or unlink the lock file while owners
        exist. Nothing in this module ever does, but another program can, and
        if it does then a second process opening the NEW file gets a lock that
        does not exclude us and we both believe we own the file. Comparing the
        open handle against the path on disk is how that is caught. It is
        reported, never repaired: releasing and retaking would hand the other
        process the same illusion."""
        if not self.held:
            return False
        try:
            on_disk = os.stat(self.path)
        except OSError as e:
            self.held = False
            self.why = f"lock file replaced or removed ({e})"
            return False
        if self.fd is not None:
            try:
                mine = os.fstat(self.fd)
                cur = (mine.st_dev, mine.st_ino)
            except OSError as e:
                self.held = False
                self.why = f"lock file replaced ({e})"
                return False
        else:
            cur = (self.dev, self.ino)
        if cur != (on_disk.st_dev, on_disk.st_ino):
            self.held = False
            self.why = "lock file replaced under the holder"
            return False
        return True


# ---------------------------------------------------------------------------
# the lock
# ---------------------------------------------------------------------------
# Two layers, because the clobber has two shapes. The per file RLock stops the
# THREADS in this process interleaving a read with someone else's rewrite; the
# OS lock on a sidecar stops a SECOND PROCESS on the same volume doing it. One
# registry entry per resolved path, so two modules holding two Path objects
# for one file still share one lock, and a nested acquire cannot deadlock.
_REGISTRY = {}
_REGISTRY_LOCK = threading.Lock()
_DEPTH = threading.local()


def lock_path(path) -> Path:
    """The sidecar for a target file.

    name + '.lock', never with_suffix. with_suffix turned sniper_forward.jsonl
    into sniper_forward.lock, a name every sibling with the same stem would
    have collided on, so two unrelated files could have shared one lock and a
    third could have been serialised against nothing."""
    p = Path(path)
    return p.with_name(p.name + ".lock")


def _key(path) -> str:
    p = Path(path)
    try:
        return str(p.resolve())
    except OSError:
        return str(p.absolute())


def _entry(key):
    with _REGISTRY_LOCK:
        e = _REGISTRY.get(key)
        if e is None:
            e = threading.RLock()
            _REGISTRY[key] = e
        return e


def _depths() -> dict:
    d = getattr(_DEPTH, "d", None)
    if d is None:
        d = {}
        _DEPTH.d = d
    return d


def _holders() -> dict:
    """The Lock object this thread is holding for each file, so a nested
    acquire can hand back the one that owns the real handle."""
    h = getattr(_DEPTH, "h", None)
    if h is None:
        h = {}
        _DEPTH.h = h
    return h


def held(path) -> bool:
    """True when THIS thread currently holds the file lock for `path`. The
    question a whole file rewrite has to ask before it truncates anything."""
    return _depths().get(_key(path), 0) > 0


def _os_lock(sidecar, deadline):
    """An exclusive OS lock on the sidecar, or (None, why).

    Non blocking with the waiting done here, because msvcrt's blocking mode
    retries for ten seconds before giving up and that would stall a scan. The
    handle is released by the OS when the process dies, which is exactly why a
    sentinel FILE is not used: one would survive a crash and wedge the bot
    forever."""
    if _msvcrt is None and _fcntl is None:
        # Astra A03: unknown ownership is not ownership. No primitive means
        # the caller is told, never that it may assume exclusivity.
        return None, "no file lock primitive on this platform", -1, -1
    for _ in range(2):                 # one retry, for a sidecar swapped mid open
        try:
            fd = os.open(str(sidecar), os.O_RDWR | os.O_CREAT)
        except OSError as e:
            return None, f"lock file unavailable ({e})", -1, -1
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if _msvcrt is not None:
                    _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
                else:
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    _close_quietly(fd)
                    return None, "lock busy", -1, -1
                time.sleep(0.01)
        try:
            mine, on_disk = os.fstat(fd), os.stat(sidecar)
        except OSError as e:
            _os_unlock(fd)
            return None, f"lock file replaced ({e})", -1, -1
        if (mine.st_dev, mine.st_ino) == (on_disk.st_dev, on_disk.st_ino):
            return fd, "", mine.st_dev, mine.st_ino
        _os_unlock(fd)                 # somebody swapped it: try once more
    return None, "lock file replaced while acquiring", -1, -1


def _close_quietly(fd):
    try:
        os.close(fd)
    except OSError:
        pass


def _os_unlock(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if _msvcrt is not None:
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        elif _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        _close_quietly(fd)


@contextlib.contextmanager
def file_lock(path, budget_s=DEFAULT_LOCK_BUDGET_S, require=False):
    """Serialise a whole read modify write on one file.

    Yields a Lock. Check .held. An unheld lock is yielded, not raised, so a
    caller whose contract is "a hiccup must never break a scan" can stand down
    on its own terms; pass require=True to get LockUnavailable instead.

    Re entrant per thread and per file, so a save inside a state_set inside a
    grading pass takes the OS lock exactly once and releases it exactly once.
    Bounded by a monotonic deadline at every step, so a wedged holder degrades
    to a reported failure rather than a hung scan."""
    key = _key(path)
    sidecar = lock_path(path)
    depths = _depths()
    if depths.get(key, 0) > 0:         # already ours; do not take it twice
        rl = _entry(key)
        rl.acquire()
        depths[key] = depths[key] + 1
        try:
            # the SAME lock object the outer acquire is holding, so a nested
            # revalidate() checks the real handle instead of comparing the
            # sidecar against a fd this frame never opened
            yield _holders().get(key) or Lock(held=True, why="reentrant",
                                              path=sidecar)
        finally:
            depths[key] -= 1
            rl.release()
        return

    deadline = time.monotonic() + max(0.0, float(budget_s))
    rl = _entry(key)
    if not rl.acquire(timeout=max(0.0, deadline - time.monotonic())):
        _bump("locks_unavailable")
        lk = Lock(held=False, why="in process lock busy", path=sidecar)
        if require:
            raise LockUnavailable(f"{sidecar.name}: {lk.why}")
        yield lk
        return

    fd, why, dev, ino = _os_lock(sidecar, deadline)
    if fd is None:
        rl.release()
        _bump("locks_unavailable")
        lk = Lock(held=False, why=why, path=sidecar)
        if require:
            raise LockUnavailable(f"{sidecar.name}: {why}")
        yield lk
        return

    depths[key] = 1
    lk = Lock(held=True, why="", path=sidecar, fd=fd, dev=dev, ino=ino)
    _holders()[key] = lk
    try:
        yield lk
    finally:
        depths[key] = 0
        _holders().pop(key, None)
        _os_unlock(fd)
        rl.release()


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------
def read_json(path) -> ReadResult:
    """One JSON document, with a status. No version envelope is required and
    none is added: every file already on the volume reads exactly as it is."""
    p = Path(path)
    try:
        if not p.exists():
            return ReadResult(status="missing", path=p)
        raw = p.read_text(encoding="utf-8-sig")
    except OSError as e:
        _bump("reads_unreadable")
        return ReadResult(status="unreadable", error=str(e), path=p)
    except UnicodeDecodeError as e:
        _bump("reads_corrupt")
        return ReadResult(status="corrupt", error=str(e), path=p, truncated=True)
    try:
        return ReadResult(status="ok", value=json.loads(raw), path=p)
    except (json.JSONDecodeError, ValueError) as e:
        _bump("reads_corrupt")
        cut = "Unterminated" in str(e) or "Expecting" in str(e)
        return ReadResult(status="corrupt", error=str(e), path=p,
                          bad_lines=1, truncated=cut)


def read_jsonl(path) -> ReadResult:
    """One JSON object per line, with a status and a damage report.

    The parseable prefix is real evidence and is returned, but a file with a
    line the parser could not read, or with no terminator on its last line, is
    NOT reported as ok. bad_lines counts what was dropped; truncated says a
    writer looks to have been cut off mid line."""
    p = Path(path)
    try:
        if not p.exists():
            return ReadResult(status="missing", value=[], path=p)
        raw = p.read_text(encoding="utf-8-sig")
    except OSError as e:
        _bump("reads_unreadable")
        return ReadResult(status="unreadable", value=None, error=str(e), path=p)
    except UnicodeDecodeError as e:
        _bump("reads_corrupt")
        return ReadResult(status="corrupt", value=[], error=str(e), path=p,
                          truncated=True)
    out, bad = [], 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            bad += 1
    cut = bool(raw.strip()) and not raw.endswith("\n")
    if bad or cut:
        _bump("reads_corrupt")
        return ReadResult(status="corrupt", value=out, path=p,
                          bad_lines=bad, truncated=cut,
                          error=f"{bad} unparseable line(s)")
    return ReadResult(status="ok", value=out, path=p)


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------
def _classify(e) -> str:
    """One OSError, one word. A full disk wears three different numbers
    depending on the platform and none of them may read as a generic failure,
    because the operator's answer to a full volume is not the answer to a
    denied rename."""
    code = getattr(e, "errno", None)
    win = getattr(e, "winerror", None)
    if code is not None and code in (errno.ENOSPC,
                                     getattr(errno, "EDQUOT", errno.ENOSPC)):
        return "disk_full"
    if win in (39, 112):               # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
        return "disk_full"
    if isinstance(e, PermissionError) or win == 5:
        return "replace_denied"
    return "failed"


def _disk_free(directory):
    """Free bytes on the volume holding `directory`, or None when the
    platform will not say."""
    try:
        return shutil.disk_usage(str(directory)).free
    except OSError:
        return None


def volume_status(path) -> dict:
    """Free and used bytes on the volume a path lives on, for the health
    record. Astra section 4: a 500 MB volume cannot be treated as unlimited
    retention, so somebody has to be able to look."""
    p = Path(path)
    d = p if p.is_dir() else p.parent
    try:
        u = shutil.disk_usage(str(d))
        return {"path": str(d), "ok": True, "error": None,
                "free_bytes": int(u.free), "used_bytes": int(u.used),
                "total_bytes": int(u.total)}
    except OSError as e:
        return {"path": str(d), "ok": False, "error": str(e),
                "free_bytes": None, "used_bytes": None, "total_bytes": None}


def _tmp_for(path) -> Path:
    """A staging name nothing else can be using. Per process, per thread and
    per call: one shared temp name let two writers overwrite each other's
    staging file before either published, and on Windows it also produced a
    straight access denied on the rename.

    The per-call part is a counter, not a clock. Windows' monotonic clock ticks
    about every 15 ms, so two publishes in one tick got the SAME name from a
    timestamp, which is the collision this is here to prevent. next() on an
    itertools counter is one bytecode and cannot be interleaved."""
    p = Path(path)
    return p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}."
                       f"{next(_SEQ)}.tmp")


def _stage(tmp: Path, payload: bytes, durable: bool):
    if durable:
        with open(tmp, "wb") as f:
            f.write(payload)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass                   # no fsync on this handle; bytes are written
    else:
        tmp.write_bytes(payload)


def _fsync_dir(directory):
    """Flush the directory entry so the rename itself survives a machine
    crash. POSIX only, guarded, and it may never change a result that has
    already been published: the bytes are live either way."""
    if os.name != "posix":
        return
    fd = None
    try:
        fd = os.open(str(directory), os.O_RDONLY)
        os.fsync(fd)
    except OSError:
        pass
    finally:
        if fd is not None:
            _close_quietly(fd)


def _replace_with_retry(tmp: Path, target: Path):
    """Publish, riding out the transient denial.

    A real WinError 5 hits roughly one run in ten on the owner's machine,
    always on a different file, and it is gone a few milliseconds later when
    whatever held the handle lets go. Bounded at MAX_REPLACE_ATTEMPTS with a
    doubling backoff, so a permanent denial costs about a third of a second
    and then reports itself instead of spinning."""
    delay = _BACKOFF_START_S
    last = None
    for attempt in range(1, MAX_REPLACE_ATTEMPTS + 1):
        try:
            _replace(str(tmp), str(target))
            return attempt, None
        except (PermissionError, FileNotFoundError) as e:
            last = e
            if attempt >= MAX_REPLACE_ATTEMPTS:
                break
            _bump("replace_retries")
            time.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, _BACKOFF_CAP_S)
        except OSError as e:           # not a blip: a full disk, a bad path
            return attempt, e
    return MAX_REPLACE_ATTEMPTS, last


_SWEPT = set()
_SWEPT_LOCK = threading.Lock()
ORPHAN_AGE_S = 3600


def _sweep_once(directory):
    """Clear cold staging files the FIRST time this process publishes into a
    directory, and never again.

    Something has to actually call the sweep or it is decoration, and the repo
    root has been carrying three orphaned .tmp files for weeks to prove it. One
    glob per directory per process is cheap enough for the 15 second poll loop,
    and the age floor means a file another writer is staging right now is never
    a candidate."""
    key = str(directory)
    with _SWEPT_LOCK:
        if key in _SWEPT:
            return
        _SWEPT.add(key)
    sweep_orphans(directory, older_than_s=ORPHAN_AGE_S)


def _publish(path, payload: bytes, durable: bool,
             budget_s: float) -> WriteResult:
    """Stage, publish, clean up, and say exactly what happened.

    The payload is already bytes when this is called, on purpose: a
    serialisation failure must never be discovered halfway through truncating
    a live file."""
    p = Path(path)
    n = len(payload)
    with file_lock(p, budget_s=budget_s) as lk:
        if not lk.held:
            _bump("write_failures")
            return WriteResult(False, "lock_unavailable", 0, n, lk.why, p)

        free = _disk_free(p.parent)
        if free is not None and free < n + FREE_MARGIN_BYTES:
            _bump("write_failures")
            return WriteResult(False, "disk_full", 0, n,
                               f"{free} bytes free, need {n} plus headroom", p)

        _sweep_once(p.parent)
        tmp = _tmp_for(p)
        try:
            _stage(tmp, payload, durable)
        except OSError as e:
            _unlink_quietly(tmp)
            _bump("write_failures")
            return WriteResult(False, _classify(e), 0, n, str(e), p)
        except Exception as e:                                  # noqa: BLE001
            _unlink_quietly(tmp)
            _bump("write_failures")
            return WriteResult(False, "failed", 0, n, str(e), p)

        attempts, err = _replace_with_retry(tmp, p)
        if err is not None:
            _unlink_quietly(tmp)       # never leave an orphan staging file
            _bump("write_failures")
            return WriteResult(False, _classify(err), attempts, n, str(err), p)
        if durable:
            _fsync_dir(p.parent)
        _bump("writes")
        return WriteResult(True, "written", attempts, n, "", p)


def _unlink_quietly(p: Path):
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass


def write_json(path, obj, indent=1, durable=False,
               budget_s=DEFAULT_LOCK_BUDGET_S) -> WriteResult:
    """Publish one JSON document atomically. Encoding happens first, so an
    unserialisable object comes back as encode_failed with the old file
    untouched."""
    try:
        payload = json.dumps(obj, indent=indent).encode("utf-8")
    except (TypeError, ValueError) as e:
        _bump("write_failures")
        return WriteResult(False, "encode_failed", 0, 0, str(e), Path(path))
    return _publish(path, payload, durable, budget_s)


def write_jsonl_all(path, records, durable=False,
                    budget_s=DEFAULT_LOCK_BUDGET_S) -> WriteResult:
    """Publish a whole JSONL file atomically. Call it inside the file's lock:
    it truncates, so anything appended since `records` was read is gone."""
    try:
        text = "".join(json.dumps(r) + "\n" for r in records)
        payload = text.encode("utf-8")
    except (TypeError, ValueError) as e:
        _bump("write_failures")
        return WriteResult(False, "encode_failed", 0, 0, str(e), Path(path))
    return _publish(path, payload, durable, budget_s)


def write_lines(path, lines, durable=False,
                budget_s=DEFAULT_LOCK_BUDGET_S) -> WriteResult:
    """Publish a file of already rendered lines. The rewrite paths that copy a
    line they could not parse straight through need this: re encoding a line
    the parser rejected would destroy the very thing being preserved."""
    body = "\n".join(lines) + ("\n" if lines else "")
    return write_text(path, body, durable=durable, budget_s=budget_s)


def write_text(path, text, durable=False,
               budget_s=DEFAULT_LOCK_BUDGET_S) -> WriteResult:
    """Publish arbitrary text atomically. Same protocol as the JSON writers,
    so a torn digest can never reach a prompt."""
    try:
        payload = str(text).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as e:
        _bump("write_failures")
        return WriteResult(False, "encode_failed", 0, 0, str(e), Path(path))
    return _publish(path, payload, durable, budget_s)


def append_text(path, text, durable=False,
                budget_s=DEFAULT_LOCK_BUDGET_S) -> WriteResult:
    """Append text under the file's lock.

    An append is not a replace, so there is nothing to stage: the one thing
    that has to be true is that a whole file rewrite of the same file cannot
    be in flight while it happens, which is what the lock is for."""
    p = Path(path)
    payload = str(text)
    n = len(payload.encode("utf-8"))
    with file_lock(p, budget_s=budget_s) as lk:
        if not lk.held:
            _bump("write_failures")
            return WriteResult(False, "lock_unavailable", 0, n, lk.why, p)
        free = _disk_free(p.parent)
        if free is not None and free < n + FREE_MARGIN_BYTES:
            _bump("write_failures")
            return WriteResult(False, "disk_full", 0, n,
                               f"{free} bytes free, need {n} plus headroom", p)
        try:
            # opened through the caller's own object when it has one, so a
            # test that proxies a Path to model a process killed mid append
            # still reaches the handle it is trying to tear
            opener = path if hasattr(path, "open") else p
            with opener.open("a", encoding="utf-8") as f:
                f.write(payload)
                if durable:
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except (OSError, ValueError):
                        pass           # no fsync here; the append still landed
        except OSError as e:
            _bump("write_failures")
            return WriteResult(False, _classify(e), 1, n, str(e), p)
        _bump("writes")
        return WriteResult(True, "written", 1, n, "", p)


def append_jsonl(path, record, durable=False,
                 budget_s=DEFAULT_LOCK_BUDGET_S) -> WriteResult:
    """Append one JSONL row under the file's lock."""
    try:
        line = json.dumps(record) + "\n"
    except (TypeError, ValueError) as e:
        _bump("write_failures")
        return WriteResult(False, "encode_failed", 0, 0, str(e), Path(path))
    return append_text(path, line, durable=durable, budget_s=budget_s)


# The exact tail _tmp_for builds: ".<pid>.<thread id>.<counter>.tmp". The sweep
# matches on THIS and not on "*.tmp", because the two are not the same set and
# the difference is somebody else's file. DATA_DIR defaults to the repo root
# and _sweep_once fires on the first publish into every directory, so a bare
# "*.tmp" glob made the first PositionBook.save of the process delete every
# cold .tmp in the repo regardless of who wrote it or what it was.
_STAGING_NAME = re.compile(r"\.\d+\.\d+\.\d+\.tmp$")


def sweep_orphans(directory, older_than_s=3600) -> int:
    """Delete staging files a crash left behind, and only those.

    A process that dies between staging and publishing leaves a .tmp; the repo
    root has been carrying three of them for weeks. Only files this module's
    naming produces are touched, only after they have gone cold, and a lock
    sidecar is never a candidate: this module does not delete lock files."""
    n = 0
    now = time.time()
    try:
        entries = list(Path(directory).glob("*.tmp"))
    except OSError:
        return 0
    for p in entries:
        if not _STAGING_NAME.search(p.name):
            continue                   # not ours, so not ours to delete
        try:
            if now - p.stat().st_mtime >= older_than_s:
                p.unlink()
                n += 1
        except OSError:
            continue                   # still held, or gone already
    if n:
        _bump("orphans_swept", n)
    return n
