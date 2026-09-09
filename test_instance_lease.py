"""Offline tests for the singleton instance lease.

Only ONE copy of this bot may poll Telegram getUpdates and broadcast cards.
Telegram answers a second consumer with 409 Conflict, and on a shared volume
two copies both write positions.json, so a loser that keeps monitoring can
mark a position closed in the file the winner then never alerts on. That is
the money bug, so the loser must not monitor at all.

The design is deliberately lock-authoritative: an OS advisory lock on
DATA_DIR/scanner.lock decides who is active, and the {instance_id, pid, seq,
heartbeat_ts} record in state.json is only the observable half. The kernel
releases the lock when the holder's handles close, which includes SIGKILL and
container teardown, so a crashed instance leaves NO stale lock and a railway
restart (restartPolicy ALWAYS) reclaims on its first attempt with no timeout,
no clock read and no human. That is what L2 proves, and it is the check that
matters most: silencing the real instance is far worse than the double alert
this whole change prevents.

Every check here failed before the lease shipped and passes after it.

Run:  python test_instance_lease.py     (exit code 0 = all good)
"""


import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.
_bot_test_os.environ.setdefault("OWNER_CHAT_ID", "999999")
_bot_test_os.environ.setdefault("TELEGRAM_CHAT_IDS", "999999")
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config

REPO = Path(__file__).parent
failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# Everything the lease touches lives under DATA_DIR, so point the whole repo's
# runtime state at a throwaway directory. The real state.json and the real
# positions.json are never opened by this file.
TMP = Path(tempfile.mkdtemp(prefix="lease_test_"))
config.DATA_DIR = TMP
config.STATE_FILE = TMP / "state.json"
config.POSITIONS_FILE = TMP / "positions.json"
config.ALERTS_LOG = TMP / "alerts.log"
config.ALERTS_JSONL = TMP / "alerts_sent.jsonl"
config.NEWS_SEEN_FILE = TMP / "news_seen.json"
config._LAST_GOOD = {}

import forward_ledger  # noqa: E402
import instance_lock  # noqa: E402
import positions as poslib  # noqa: E402
import scanner  # noqa: E402
import sniper_book  # noqa: E402
import telegram  # noqa: E402


class _StopLoop(BaseException):
    """Breaks a bounded run of the daemon loop. A BaseException on purpose:
    daemon() catches Exception and keeps going, which is exactly right in
    production and exactly wrong for a test that wants two iterations."""


class _Resp:
    """A Telegram HTTP reply that says ok, so a send that IS allowed through
    completes instead of erroring into the retry queue."""
    ok = True
    status_code = 200
    text = '{"ok": true}'

    @staticmethod
    def json():
        return {"ok": True, "result": []}


class _Wire:
    """Stands in for telegram._session so a check can COUNT wire calls without
    making one. Nothing leaves the machine; the URL is recorded and dropped."""

    def __init__(self):
        self.posts = []
        self.gets = []

    def post(self, url, **kw):
        self.posts.append(url)
        return _Resp()

    def get(self, url, **kw):
        self.gets.append(url)
        return _Resp()


class _TimeShim:
    """scanner.time_mod with a sleep that does not sleep. Bounds a loop that
    would otherwise run forever, and keeps the suite fast."""

    def __init__(self, real, budget):
        self._real = real
        self.sleeps = 0
        self.budget = budget

    def sleep(self, _s):
        self.sleeps += 1
        if self.sleeps >= self.budget:
            raise _StopLoop()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _raw_lock(path):
    """Take the OS lock directly, bypassing the module's bookkeeping, so a
    check can stand in for the OTHER process. Returns an fd or None."""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
    try:
        if instance_lock._msvcrt is not None:
            instance_lock._msvcrt.locking(fd, instance_lock._msvcrt.LK_NBLCK, 1)
        elif instance_lock._fcntl is not None:
            instance_lock._fcntl.flock(
                fd, instance_lock._fcntl.LOCK_EX | instance_lock._fcntl.LOCK_NB)
        else:
            os.close(fd)
            return None
        return fd
    except OSError:
        os.close(fd)
        return None


def _raw_unlock(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if instance_lock._msvcrt is not None:
            instance_lock._msvcrt.locking(fd, instance_lock._msvcrt.LK_UNLCK, 1)
        elif instance_lock._fcntl is not None:
            instance_lock._fcntl.flock(fd, instance_lock._fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
print("\n--- L1. two instances, one lock ---")

instance_lock.release()
r1 = instance_lock.try_acquire()
check("L1 the first instance acquires the lock", r1 == "acquired", r1)
check("L1 the lock file lands in DATA_DIR", instance_lock.lock_path().parent == TMP,
      str(instance_lock.lock_path()))

fd_raw = _raw_lock(instance_lock.lock_path())
check("L1 the OS itself refuses a second lock on the same file", fd_raw is None)
if fd_raw is not None:
    _raw_unlock(fd_raw)

held_fd = instance_lock._fd
check("L1 the same process asking again is told held",
      instance_lock.try_acquire() == "held")
instance_lock._fd = None  # stand in for a SECOND process: same file, no fd
r2 = instance_lock.try_acquire()
check("L1 a second instance is told contended", r2 == "contended", r2)
instance_lock._fd = held_fd

# ---------------------------------------------------------------------------
print("\n--- L2. a dead holder is reclaimed with no timeout and no human ---")

instance_lock.renew(force=True)
lease_a = instance_lock.read_lease()
check("L2 the holder publishes an observable record",
      bool(lease_a.get("instance_id")) and isinstance(lease_a.get("seq"), int),
      json.dumps(lease_a))
seq_a, id_a = lease_a.get("seq"), lease_a.get("instance_id")

# release() closes the fd, which is the EXACT thing the kernel does when a
# process is killed. That is why this reclaim test is honest.
instance_lock.release()
t0 = time.monotonic()
r3 = instance_lock.try_acquire()
took = time.monotonic() - t0
check("L2 a dead holder's lock is reclaimed on the first try", r3 == "acquired", r3)
check("L2 the reclaim costs no timeout", took < 1.0, f"{took:.2f}s")

instance_lock._ID = "reclaimer001"  # pretend this is the restarted container
instance_lock._seq = 0
instance_lock.renew(force=True)
lease_b = instance_lock.read_lease()
check("L2 the record names the new instance", lease_b.get("instance_id") != id_a,
      json.dumps(lease_b))
check("L2 seq never moves backwards on takeover", lease_b.get("seq", 0) > seq_a,
      f"{lease_b.get('seq')} vs {seq_a}")

# and the same thing across a real process boundary: a killed child leaves
# nothing stale behind, which is the railway restartPolicy ALWAYS case.
child = TMP / "child_holder.py"
child.write_text(
    "import os, sys, time\n"
    f"sys.path.insert(0, {str(REPO)!r})\n"
    f"os.environ['DATA_DIR'] = {str(TMP)!r}\n"
    "os.environ['BOT_TEST_MODE'] = '1'\n"
    "import instance_lock\n"
    "print(instance_lock.try_acquire(), flush=True)\n"
    "time.sleep(120)\n", encoding="utf-8")
instance_lock.release()
proc = subprocess.Popen([sys.executable, str(child)], stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True)
try:
    first = (proc.stdout.readline() or "").strip()
    check("L2 a child process takes the lock", first == "acquired", first)
    check("L2 the parent is locked out while the child lives",
          instance_lock.try_acquire() == "contended")
    proc.kill()
    proc.wait(timeout=10)
    deadline, got = time.monotonic() + 2.0, ""
    while time.monotonic() < deadline:
        got = instance_lock.try_acquire()
        if got == "acquired":
            break
        time.sleep(0.05)
    check("L2 killing the holder frees the lock within 2s, no human in the loop",
          got == "acquired", got)
finally:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.stdout.close()
    except Exception:
        pass

# ---------------------------------------------------------------------------
print("\n--- L3. the loser refuses every broadcast, and poisons no queue ---")

svc = scanner.Service()
svc.dry = False
_real_test_mode = telegram.test_mode
_real_session = telegram._session
_real_token = telegram._token
wire = _Wire()
config.state_set("pending_sends", [{"text": "winner's own backlog", "tries": 0}])
before_pending = json.dumps(config.state_get("pending_sends", []), sort_keys=True)
try:
    # test_mode() short-circuits every send, which would hide the standby gate.
    # Turn it off and put a counting stub on the wire instead, so a leak shows
    # up as a recorded URL rather than as a text to a real phone.
    telegram.test_mode = lambda: False
    telegram._session = wire
    telegram._token = lambda: "TESTTOKEN"
    telegram.set_standby(True, "test")

    telegram.send("entry card")
    check("L3 telegram.send makes zero HTTP calls in standby", not wire.posts,
          str(wire.posts[:2]))
    telegram.send_photo_all(b"not-a-real-png", "sniper chart")
    check("L3 telegram.send_photo_all makes zero HTTP calls in standby",
          not wire.posts, str(wire.posts[:2]))
    check("L3 Service.notify returns no errors in standby",
          svc.notify("STOP card") == [])
    after_pending = json.dumps(config.state_get("pending_sends", []), sort_keys=True)
    check("L3 notify does NOT queue into the shared pending_sends",
          after_pending == before_pending, after_pending)
    check("L3 the loser writes no alerts.log line", not config.ALERTS_LOG.exists())
    svc.flush_pending()
    check("L3 flush_pending sends nothing in standby", not wire.posts,
          str(wire.posts[:2]))

    # -----------------------------------------------------------------------
    print("\n--- L4. the loser can still warn the owner ---")
    owner = telegram.primary_owner_id()
    n0 = len(wire.posts)
    telegram.send_to(owner, "ops note", ops=True)
    check("L4 an ops DM reaches the transport in standby", len(wire.posts) > n0)
    n1 = len(wire.posts)
    svc._hb_owner("heartbeat note")
    check("L4 _hb_owner reaches the transport in standby", len(wire.posts) > n1)
    n2 = len(wire.posts)
    telegram.send_to(owner, "ordinary alert")
    check("L4 a non-ops send to the same owner is still dropped",
          len(wire.posts) == n2)

    # -----------------------------------------------------------------------
    print("\n--- L5. the loser stops polling, which is what clears the 409 ---")
    items, off = telegram.get_messages()
    check("L5 get_messages returns nothing in standby", items == [])
    check("L5 get_messages issues no HTTP GET in standby", not wire.gets,
          str(wire.gets[:2]))
    check("L5 the stored offset is handed back untouched",
          off == int(config.state_get("tg_offset", 0)))
finally:
    telegram.set_standby(False)
    telegram.test_mode = _real_test_mode
    telegram._session = _real_session
    telegram._token = _real_token

# ---------------------------------------------------------------------------
print("\n--- L6. the loser does not monitor and does not write positions.json ---")

config.POSITIONS_FILE.write_text(json.dumps([{
    "id": "t1", "date": "2026-09-08", "time_et": "09:50:00", "ticker": "SPX",
    "direction": "call", "right": "C", "strike": 7300.0, "expiry": "2026-09-08",
    "entry_mid": 4.4, "entry_source": "quote", "entry_bid": 4.2, "entry_ask": 4.6,
    "spot_at_signal": 7297.2, "mom_pct": 0.21, "risk_pct": 1.0,
    "correlated": False, "paper": True, "risk_mode": "green",
    "state": "open"}], indent=2), encoding="utf-8")
pos_before = config.POSITIONS_FILE.read_bytes()

loser = scanner.Service()
loser.dry = False
touched = {"monitor": 0, "scan": 0, "commands": 0}
loser.monitor_positions = lambda now: touched.__setitem__("monitor", touched["monitor"] + 1)
loser.scan_entries = lambda now: touched.__setitem__("scan", touched["scan"] + 1)
loser.handle_commands = lambda timeout=0: touched.__setitem__("commands", touched["commands"] + 1)
_real_try = instance_lock.try_acquire
_real_time_mod = scanner.time_mod
try:
    instance_lock.try_acquire = lambda *a, **k: "contended"
    scanner.time_mod = _TimeShim(_real_time_mod, budget=3)
    try:
        loser.daemon()
    except _StopLoop:
        pass
    check("L6 the loser never monitors open positions", touched["monitor"] == 0,
          str(touched))
    check("L6 the loser never scans for entries", touched["scan"] == 0, str(touched))
    check("L6 the loser never polls for commands", touched["commands"] == 0,
          str(touched))
    check("L6 positions.json is byte-identical after the loser ran",
          config.POSITIONS_FILE.read_bytes() == pos_before)
    check("L6 the loser gagged the wire", telegram.standby()[0] is True)
    check("L6 standby is a live retry loop, not an exit",
          scanner.time_mod.sleeps >= 2, str(scanner.time_mod.sleeps))
finally:
    instance_lock.try_acquire = _real_try
    scanner.time_mod = _real_time_mod
    telegram.set_standby(False)

# ---------------------------------------------------------------------------
print("\n--- L7. no decision branches on the wall clock ---")

watch = instance_lock.SeqWatch()
future = {"instance_id": "wedged", "pid": 7, "host": "box",
          "seq": 7, "heartbeat_ts": time.time() + 86400}
check("L7 a first sighting is never already stale",
      watch.observe(future, now=1000.0) == 0.0)
frozen = watch.observe(future, now=1000.0 + instance_lock.STALE_S + 5)
check("L7 a frozen seq is judged frozen on the observer's own clock",
      frozen >= instance_lock.STALE_S, f"{frozen:.0f}s")
line = instance_lock.holder_line(future)
check("L7 a future heartbeat renders as clock skew, not a negative age",
      "clock skew" in line, line)
check("L7 the holder line still names who holds it",
      "wedged" in line and "7" in line, line)
old = {"instance_id": "wedged", "pid": 7, "host": "box",
       "seq": 8, "heartbeat_ts": time.time() - 31 * 86400}
check("L7 a 30-day-old stamp whose seq advanced is judged fresh",
      watch.observe(old, now=1000.0 + instance_lock.STALE_S + 6) == 0.0)
check("L7 a 30-day-old stamp also renders as clock skew",
      "clock skew" in instance_lock.holder_line(old))
check("L7 a garbage heartbeat does not crash the renderer",
      "clock skew" in instance_lock.holder_line({"instance_id": "x",
                                                 "heartbeat_ts": "nonsense"}))

# ---------------------------------------------------------------------------
print("\n--- L8. no lock primitive means BLOCKED, not alert anyway ---")

# REWRITTEN for the five-state ownership contract (Astra A03, decision 2).
# This block used to assert the opposite of what it asserts now: that a
# platform with no lock primitive kept alerting with the wire un-gagged, on the
# argument that a silent bot is worse than a duplicate card. Astra overturned
# that as an unsafe default, because unknown ownership is not exclusive
# ownership, and the same code rejected blind resume (L13) for exactly the
# reason it accepted this. The two assertions retired here are the old
# "ensure_active is True" and "the wire is NOT gagged"; everything else in the
# block, including the once-a-day damping and the errno classification below,
# is unchanged and still passing. test_instance_lifecycle section C is the full
# replacement coverage.

_saved_m, _saved_f = instance_lock._msvcrt, instance_lock._fcntl
instance_lock.release()
dms = []
degraded = scanner.Service()
degraded.dry = False
degraded._hb_owner = lambda text: dms.append(text)
try:
    instance_lock._msvcrt = None
    instance_lock._fcntl = None
    check("L8 try_acquire reports unavailable, not contended",
          instance_lock.try_acquire() == "unavailable")
    now = scanner.et_now()
    check("L8 a platform with no lock does NOT become active",
          degraded.ensure_active(now) is False)
    check("L8 the wire is gagged, because ownership is unknown",
          telegram.standby()[0] is True)
    check("L8 the state says BLOCKED",
          telegram.ownership_state() == instance_lock.BLOCKED,
          telegram.ownership_state())
    check("L8 the owner is told once", len(dms) == 1, str(len(dms)))
    degraded.ensure_active(now)
    check("L8 and is not told again every cycle", len(dms) == 1, str(len(dms)))
finally:
    instance_lock._msvcrt, instance_lock._fcntl = _saved_m, _saved_f
    telegram.set_standby(False)

# a volume that cannot lock at all (an overlay or a share: ENOLCK, ENOSYS,
# EOPNOTSUPP) raises OSError from the same call contention does. Reading that
# as contention would make a lone bot silence ITSELF forever, with no second
# copy anywhere, so only the "somebody holds it" errnos count as contention.
import errno as _errno  # noqa: E402


class _Refuser:
    LK_NBLCK = 0
    LK_UNLCK = 0

    def __init__(self, code):
        self.code = code

    def locking(self, *a):
        raise OSError(self.code, os.strerror(self.code))


for _code, _want in ((_errno.ENOLCK, "unavailable"),
                     (getattr(_errno, "ENOSYS", _errno.EINVAL), "unavailable"),
                     (_errno.EACCES, "contended"),
                     (_errno.EAGAIN, "contended")):
    instance_lock.release()
    _saved_m, _saved_f = instance_lock._msvcrt, instance_lock._fcntl
    try:
        instance_lock._msvcrt = _Refuser(_code)
        instance_lock._fcntl = None
        got = instance_lock.try_acquire(budget_s=0.0)
    finally:
        instance_lock._msvcrt, instance_lock._fcntl = _saved_m, _saved_f
    check(f"L8 errno {_code} is read as {_want}, not the other one",
          got == _want, got)

# ---------------------------------------------------------------------------
print("\n--- L9. a wedged holder is reported, never stolen from ---")

instance_lock.release()
other_fd = _raw_lock(instance_lock.lock_path())
check("L9 the stand-in holder took the lock", other_fd is not None)
config.state_set(instance_lock.LEASE_KEY, {
    "instance_id": "wedged9", "pid": 4242, "host": "railway",
    "seq": 11, "heartbeat_ts": time.time()})
watcher = scanner.Service()
watcher.dry = False
wedge_dms = []
watcher._hb_owner = lambda text: wedge_dms.append(text)
_real_time_mod = scanner.time_mod
try:
    scanner.time_mod = _TimeShim(_real_time_mod, budget=99)
    now = scanner.et_now()
    check("L9 the observer stands down", watcher.ensure_active(now) is False)
    check("L9 the observer gags its own wire", telegram.standby()[0] is True)
    # backdate the freeze counter through the public API: the holder's seq has
    # not moved for longer than STALE_S of the OBSERVER's monotonic clock
    lease_now = instance_lock.read_lease()
    watcher._seqwatch.observe(
        lease_now, now=time.monotonic() - instance_lock.STALE_S - 10)
    # a standing-by copy writes nothing to state.json, so its stand-down DM is
    # deduped in memory for the life of the boot instead of by the persisted
    # once-a-day flag. This watcher is a fresh Service, so its own stand-down
    # DM lands here too. Count the wedge warning by CONTENT, not by total.
    _before = config.state_get("hb_warned", {}) or {}
    watcher.standby_wait(now)
    check("L9 a wedged holder is reported to the owner",
          len([t for t in wedge_dms if "may be wedged" in t]) == 1,
          str(wedge_dms))
    watcher.standby_wait(now)
    check("L9 the wedge warning does not repeat every poll",
          len([t for t in wedge_dms if "may be wedged" in t]) == 1,
          str(len(wedge_dms)))
    # why that dedup is per boot and not per day: a mute copy must never do a
    # whole-file read-modify-write on the shared state. The only lock around
    # state.json is a THREAD lock, which says nothing across two processes on
    # one volume, so a loser recording its own flag can publish a stale
    # snapshot of the WINNER's keys (sniper_alerted, morning_sent, recap_sent)
    # over the copy that is actually working, and that is a missed or
    # duplicated alert. One extra DM per boot is the cheaper side of that.
    check("L9 a standing-by copy leaves the shared hb_warned untouched",
          (config.state_get("hb_warned", {}) or {}) == _before,
          str(config.state_get("hb_warned", {})))
    check("L9 the observer does NOT take the lock from a live holder",
          instance_lock.try_acquire() == "contended")
    check("L9 the observer holds no fd", instance_lock._fd is None)
finally:
    scanner.time_mod = _real_time_mod
    telegram.set_standby(False)
    if other_fd is not None:
        _raw_unlock(other_fd)

# ---------------------------------------------------------------------------
print("\n--- L10. the new module is under the no-em-dash law ---")

import test_no_em_dash as em  # noqa: E402

check("L10 instance_lock.py is in the guarded list",
      "instance_lock.py" in em.GUARDED, str(em.GUARDED))
check("L10 instance_lock.py carries no em dash in a message string",
      not em.offenders(REPO / "instance_lock.py"),
      str(em.offenders(REPO / "instance_lock.py"))[:200])

# ---------------------------------------------------------------------------
print("\n--- L11. a promoted standby re-reads what it cached at boot ---")

# PositionBook.load() only ever ran in __init__, main() builds ONE Service for
# the life of the process and standby_wait never exits, so a container that
# booted, lost the lock and sat in standby for an hour was still holding the
# positions.json it read at BOOT. Its first save() after promoting erased every
# row the copy that WAS active opened meanwhile, and those positions stopped
# being monitored for their stop, their half and their give-back with nobody
# told. That is the lease being more dangerous than the double alert it exists
# to prevent, so it is checked here.

instance_lock.release()
telegram.set_standby(False)


def _pos_row(pid, ticker, strike):
    return {"id": pid, "date": "2026-09-08", "time_et": "09:52:00",
            "ticker": ticker, "direction": "call", "right": "C",
            "strike": strike, "expiry": "2026-09-08", "entry_mid": 4.4,
            "entry_source": "quote", "entry_bid": 4.2, "entry_ask": 4.6,
            "spot_at_signal": 7297.2, "mom_pct": 0.21, "risk_pct": 1.0,
            "correlated": False, "paper": True, "risk_mode": "green",
            "state": "open"}


# B boots 09:20 with an empty book and immediately loses the lock.
config.POSITIONS_FILE.write_text("[]", encoding="utf-8")
standby_copy = scanner.Service()
standby_copy.dry = False
promo_dms = []
standby_copy._hb_owner = lambda text: promo_dms.append(text)
check("L11 the standby copy booted with an empty book",
      standby_copy.book.positions == [])

# A opens an SPX call at 09:52, persists it, and is torn down at 10:15.
config.POSITIONS_FILE.write_text(
    json.dumps([_pos_row("A-0952", "SPX", 7300.0)]), encoding="utf-8")
# and the promoted copy holds one row the file has never seen, so the fix has
# to MERGE. Re-reading and dropping what is only in memory is the same money
# bug pointing the other way.
standby_copy.book.positions.append(poslib.Position(**_pos_row("B-only", "TSLA", 250.0)))

reloads = {"n": 0}
_real_reload = standby_copy.reload_tunables


def _counting_reload():
    reloads["n"] += 1
    return _real_reload()


standby_copy.reload_tunables = _counting_reload
_real_try = instance_lock.try_acquire
try:
    # stand the copy down through the REAL path rather than by flipping the
    # wire flag by hand. The promotion ceremony keys on having actually stood
    # down, not on the gag, because the gag is now also on during STARTING and
    # a fresh boot must not announce itself as a promotion every restart.
    instance_lock.try_acquire = lambda *a, **k: "contended"
    check("L11 the copy stands down while the other one holds the lock",
          standby_copy.ensure_active(scanner.et_now()) is False)
    instance_lock.try_acquire = lambda *a, **k: "acquired"
    check("L11 the copy promotes the moment the lock frees",
          standby_copy.ensure_active(scanner.et_now()) is True)
finally:
    instance_lock.try_acquire = _real_try
    telegram.set_standby(False)

ids11 = [p.id for p in standby_copy.book.positions]
check("L11 the winner's open position survives the promotion",
      "A-0952" in ids11, str(ids11))
check("L11 a row only the promoted copy holds is kept, not dropped",
      "B-only" in ids11, str(ids11))
standby_copy.book.save()
on_disk11 = [p["id"] for p in
             json.loads(config.POSITIONS_FILE.read_text(encoding="utf-8"))]
check("L11 the first save after promotion does not erase the winner's row",
      "A-0952" in on_disk11, str(on_disk11))
check("L11 the promotion also re-reads the day's tunables",
      reloads["n"] >= 1, str(reloads["n"]))
# by CONTENT, not by total: standing down for real (above) also DMs the owner,
# same reason L9 counts its wedge warning that way.
check("L11 the owner is told it promoted",
      len([t for t in promo_dms if "promoted to active" in t]) == 1,
      str(promo_dms))

# ---------------------------------------------------------------------------
print("\n--- L12. a standby copy mutates no shared state, only the wire ---")

# The only stand-down guard used to sit at the TOP of _sniper_worker's loop.
# _scan_snipers_once had none, so a standby copy sent 0 cards and still burned
# the day/symbol key in the SHARED state.json, still opened a row in the shared
# sniper book, and still marked the forward-ledger row selected. The winner
# then saw the key already burned, never sent the entry alert at all, and later
# texted an exit card for a trade nobody was ever told about.

sniper_book.LEDGER = TMP / "sniper_positions.json"
forward_ledger.LEDGER = TMP / "sniper_forward.jsonl"

_TICKET = {"entry": 100.0, "stop": 99.0, "target": 100.4}
_FAKE_READ = {"instrument": "EUR/USD", "price": 100.0, "decimals": 2,
              "conviction": "high", "plan": {"direction": "BUY"},
              "recent_bars": None,
              "fvg": {"candidate_id": "cid-12",
                      "confirming": {"ticket": dict(_TICKET)}}}

import market_tools  # noqa: E402

_real_read_any = market_tools.read_any
_real_photo = telegram.send_photo_all
try:
    telegram.send_photo_all = lambda *a, **k: None
    market_tools.read_any = lambda *a, **k: dict(_FAKE_READ)
    config.state_set("sniper_alerted", {})
    if sniper_book.LEDGER.exists():
        sniper_book.LEDGER.unlink()

    mute = scanner.Service()
    mute.dry = False
    mute._hb_owner = lambda text: None
    telegram.set_standby(True, "test")
    mute._scan_snipers_once(scanner.et_now(), entries_allowed=True)
    check("L12 a standby copy burns no day/symbol key in shared state",
          not config.state_get("sniper_alerted", {}),
          json.dumps(config.state_get("sniper_alerted", {})))
    check("L12 a standby copy opens no row in the shared sniper book",
          sniper_book.open_rows() == [], str(sniper_book.open_rows())[:200])
    check("L12 a standby copy does not even create the sniper book file",
          not sniper_book.LEDGER.exists())

    # and the same when the stand-down lands MID-PASS, after the read. the
    # three writes are one commit, so a pass caught here leaves nothing behind.
    telegram.set_standby(False)

    def _read_then_stand_down(*a, **k):
        telegram.set_standby(True, "test")
        return dict(_FAKE_READ)

    market_tools.read_any = _read_then_stand_down
    config.state_set("sniper_alerted", {})
    mid = scanner.Service()
    mid.dry = False
    mid._hb_owner = lambda text: None
    mid._scan_snipers_once(scanner.et_now(), entries_allowed=True)
    check("L12 a pass that crosses into standby writes no day/symbol key",
          not config.state_get("sniper_alerted", {}),
          json.dumps(config.state_get("sniper_alerted", {})))
    check("L12 a pass that crosses into standby opens no book row",
          not sniper_book.LEDGER.exists())

    # the write layers themselves, not just the caller that used to be trusted
    if sniper_book.LEDGER.exists():
        sniper_book.LEDGER.unlink()  # so the refusal below is the only reason
    telegram.set_standby(True, "test")
    row12 = sniper_book.open_trade(
        symbol="^GSPC", display="S&P 500", direction="BUY", entry=100.0,
        stop=99.0, target=100.4, day="2026-09-08", time_et="10:00:00")
    check("L12 sniper_book.open_trade refuses to write in standby",
          row12 is None and not sniper_book.LEDGER.exists(), str(row12))

    telegram.set_standby(False)
    cid12 = forward_ledger.record_candidate(
        symbol="^GSPC", direction="BUY", price=100.0, atr=1.0,
        ticket=dict(_TICKET), conf={"conviction": "high"}, passes=True,
        reasons=[], gap_atr=1.5, hour_et=10, now_et=scanner.et_now())
    check("L12 a ledger row exists for the selection check", bool(cid12), str(cid12))
    before12 = forward_ledger.LEDGER.read_bytes()
    telegram.set_standby(True, "test")
    got12 = forward_ledger.mark_selected(cid12, fired_at_et=scanner.et_now(),
                                         position_id="p12")
    # `is False` before W02, falsy-with-a-reason after it. mark_selected now
    # returns a MarkResult because replay has to tell a MISSING observation row
    # (an orphan nobody can repair) from a REFUSED write (a disk fault a retry
    # fixes) from a stand-down. It is still falsy exactly when the link did not
    # land, so the refusal this check exists for is unchanged.
    check("L12 forward_ledger.mark_selected refuses to write in standby",
          not got12 and getattr(got12, "status", "")
          == forward_ledger.MarkResult.STANDING_BY, str(got12))
    check("L12 the forward ledger is byte-identical after the standby copy ran",
          forward_ledger.LEDGER.read_bytes() == before12)
finally:
    market_tools.read_any = _real_read_any
    telegram.send_photo_all = _real_photo
    telegram.set_standby(False)

# ---------------------------------------------------------------------------
print("\n--- L13. an unreadable lock is not permission to resume ---")

# try_acquire answers "unavailable" (not "contended") when os.open on the lock
# file raises: a read-only remount, EIO or ESTALE on the mount, fd exhaustion.
# ensure_active used to read that as permission to resume full active
# behaviour, with no re-verification that the contention it saw one tick
# earlier was gone. The STANDBY -> ACTIVE edge was completely unguarded.


class _FaultyOpen:
    """The os module with exactly ONE broken syscall, the one a mount fault
    breaks. Everything else is the real thing, so try_acquire runs for real."""

    def __init__(self, real, code):
        self._real = real
        self._code = code

    def open(self, *a, **k):
        raise OSError(self._code, os.strerror(self._code))

    def __getattr__(self, name):
        return getattr(self._real, name)


instance_lock.release()
telegram.set_standby(False)
config.state_set(instance_lock.LEASE_KEY, {
    "instance_id": "holder13", "pid": 99, "host": "railway",
    "seq": 3, "heartbeat_ts": time.time()})
holder13 = _raw_lock(instance_lock.lock_path())
check("L13 the stand-in holder took the lock", holder13 is not None)

l13 = scanner.Service()
l13.dry = False
l13._hb_owner = lambda text: None
now13 = scanner.et_now()
_real_os = instance_lock.os
try:
    check("L13 the copy stands down on real contention",
          l13.ensure_active(now13) is False)
    check("L13 and gags its own wire", telegram.standby()[0] is True)

    instance_lock.os = _FaultyOpen(_real_os, _errno.EIO)
    check("L13 a faulted lock read really does report unavailable",
          instance_lock.try_acquire() == "unavailable")
    check("L13 a copy that already saw a holder does NOT resume on it",
          l13.ensure_active(now13) is False)
    check("L13 and stays gagged", telegram.standby()[0] is True)

    # REWRITTEN with L8, same reversal. A copy that has observed nothing yet
    # used to be treated as a different case, and failed OPEN on this fault on
    # the grounds that a boot has seen no holder. It is the same question with
    # the same answer: the read failed, so ownership is unknown, so it is not
    # ownership. The two retired assertions here are the old "a fresh boot
    # still fails open" and "a fresh boot is not gagged".
    telegram.set_standby(False)
    booting13 = scanner.Service()
    booting13.dry = False
    booting13._hb_owner = lambda text: None
    check("L13 a fresh boot does NOT fail open on the same fault",
          booting13.ensure_active(now13) is False)
    check("L13 and a fresh boot is gagged too", telegram.standby()[0] is True)

    # the normal restart path, with the fault cleared: the winner is torn down,
    # the kernel drops its lock, and the mute copy promotes on its next tick
    # with no human and no timeout.
    instance_lock.os = _real_os
    telegram.set_standby(True, "test")
    _raw_unlock(holder13)
    holder13 = None
    check("L13 the winner going away still promotes the mute copy",
          l13.ensure_active(now13) is True)
    check("L13 and the wire is live again", telegram.standby()[0] is False)
    instance_lock.release()

    # There is deliberately NO way back while the lock stays unreadable, not
    # even a bounded one. A "blind resume" after the holder's published seq
    # stood still for a while used to live here, so a permanent mount fault
    # could not mute the bot forever. That premise does not hold: the lease
    # RECORD is on the SAME volume whose failure produced the unreadable lock,
    # so the fault that blinds this copy also stops the live holder's renew
    # from landing. A frozen seq is then a symptom of our own broken disk, not
    # evidence the holder died, and acting on it puts two copies on the wire
    # during exactly the fault the lease exists to survive. No local evidence
    # separates the two cases, so the copy stays mute and says so daily.
    telegram.set_standby(False)
    holder13 = _raw_lock(instance_lock.lock_path())
    blind13 = scanner.Service()
    blind13.dry = False
    blind_dms = []
    blind13._hb_owner = lambda text: blind_dms.append(text)
    check("L13 the second copy stands down first", blind13.ensure_active(now13) is False)
    instance_lock.os = _FaultyOpen(_real_os, _errno.EIO)
    check("L13 a frozen record alone is not enough while it is still fresh",
          blind13.ensure_active(now13) is False)
    # backdate the freeze counter through the public API: a fresh SeqWatch
    # seeded at an old monotonic reading is the holder's seq having stood
    # still that long on THIS observer's clock. (Re-seeding rather than
    # re-observing, because observe only re-stamps when the key changes.)
    blind13._seqwatch = instance_lock.SeqWatch()
    blind13._seqwatch.observe(
        instance_lock.read_lease(),
        now=time.monotonic() - instance_lock.STALE_S - 10_000)
    check("L13 a frozen record on the SAME broken volume is still not evidence",
          blind13.ensure_active(now13) is False)
    check("L13 and the copy is still gagged no matter how long it waits",
          telegram.standby()[0] is True)
    check("L13 the owner is told it will not un-mute itself",
          any("will not un-mute" in t for t in blind_dms), str(blind_dms))
    # the ONLY way back is the lock actually becoming readable again
    instance_lock.os = _real_os
    _raw_unlock(holder13)
    holder13 = None
    check("L13 a readable lock with the holder gone does bring it back",
          blind13.ensure_active(now13) is True)
    check("L13 and that resume un-gags the wire", telegram.standby()[0] is False)
    instance_lock.release()
finally:
    instance_lock.os = _real_os
    telegram.set_standby(False)
    if holder13 is not None:
        _raw_unlock(holder13)
    instance_lock.release()

# ---------------------------------------------------------------------------
instance_lock.release()
try:
    shutil.rmtree(TMP, ignore_errors=True)
except OSError:
    pass

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All instance-lease checks passed.")
