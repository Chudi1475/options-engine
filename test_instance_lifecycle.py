"""Offline tests for the five-state ownership contract.

Astra finding A03 overturned the old default. "ACTIVE-DEGRADED may alert when
no lock primitive exists" was an unsafe default: unknown ownership is not
exclusive ownership, and a copy that cannot tell whether it owns the token must
not act as though it does. Decision 2 is the reversal, in one line: remove
permission to become active merely because locking is unavailable.

So the three states become five, exactly as the spec tabulates them:

    STARTING    booted, ownership not established. local checks only.
    STANDBY     another cooperating process holds the lock. retries only.
    RECOVERING  lock held, durable state not reconciled yet. no new entries,
                no fresh strategy notifications.
    ACTIVE      ownership held AND every mandatory store reconciled.
    BLOCKED     lock unsupported, storage unhealthy, recovery incomplete or
                ownership uncertain. local reporting and bounded retries only,
                never activation on a timer.

Two rules carry most of the weight. Promotion is ALL OR NOTHING across
positions, sniper positions, day reservations, pending deliveries, the Telegram
update offset, news-seen state and scheduled-job state: everything is parsed
into temporary objects and validated before anything replaces memory, and any
mandatory reload failure leaves the process RECOVERING or BLOCKED even though
an older in-memory book is sitting right there. Preserving stale memory is not
permission to resume. And a local advisory lock is NOT the whole answer: it
cannot see another machine, another service, a copied volume, an old binary, a
desktop Task Scheduler entry, a webhook or an ad hoc getUpdates script, so the
module says so in a machine-checkable list instead of a paragraph of prose.

The check that matters most is section B. A normal restart under railway's
restartPolicy ALWAYS must still reach ACTIVE with no human, across a real
process boundary, or this whole change has bricked the bot in exchange for a
guarantee nobody can use. It was green before this package and it stays green.

Run:  python test_instance_lifecycle.py     (exit code 0 = all good)
"""


import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.
_bot_test_os.environ.setdefault("OWNER_CHAT_ID", "999999")
_bot_test_os.environ.setdefault("TELEGRAM_CHAT_IDS", "999999")
import errno
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
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


def probe(name, fn, detail=""):
    """check() for something that may not exist yet. A missing interface is a
    normal FAIL line here instead of a traceback that would hide every later
    section, which matters when the whole point of the run is to watch a red
    list before the change and the same list green after it."""
    try:
        ok = fn()
    except Exception as e:
        check(name, False, f"{type(e).__name__}: {e}")
        return False
    check(name, bool(ok), str(detail))
    return bool(ok)


# Everything the lease touches lives under DATA_DIR, so point the whole repo's
# runtime state at a throwaway directory. The real state.json and the real
# positions.json are never opened by this file.
TMP = Path(tempfile.mkdtemp(prefix="lifecycle_test_"))
config.DATA_DIR = TMP
config.STATE_FILE = TMP / "state.json"
config.POSITIONS_FILE = TMP / "positions.json"
config.ALERTS_LOG = TMP / "alerts.log"
config.ALERTS_JSONL = TMP / "alerts_sent.jsonl"
config.NEWS_SEEN_FILE = TMP / "news_seen.json"
config._LAST_GOOD = {}

import instance_lock  # noqa: E402
import positions as poslib  # noqa: E402
import scanner  # noqa: E402
import sniper_book  # noqa: E402
import telegram  # noqa: E402

sniper_book.LEDGER = TMP / "sniper_positions.json"

STATES = ("STARTING", "STANDBY", "RECOVERING", "ACTIVE", "BLOCKED")
NOT_ACTIVE = ("STARTING", "STANDBY", "RECOVERING", "BLOCKED")


def state_name(which):
    """The module's constant for a state, or the plain string when the
    constants do not exist yet (before the change)."""
    return getattr(instance_lock, which, which)


def owned_state():
    """What the process currently believes it owns, or "" when the interface
    is not there yet."""
    fn = getattr(telegram, "ownership_state", None)
    return fn() if fn else ""


def set_state(which, reason="test"):
    """Drive the wire into one ownership state. Falls back to the old boolean
    gag so the sections that only care about behavior still run before the
    change."""
    fn = getattr(telegram, "set_ownership_state", None)
    if fn:
        fn(state_name(which), reason)
    else:
        telegram.set_standby(which != "ACTIVE", reason)


class _Resp:
    """A Telegram HTTP reply that says ok, so a send that IS allowed through
    completes instead of erroring into the retry queue."""
    ok = True
    status_code = 200
    text = '{"ok": true}'

    @staticmethod
    def json():
        return {"ok": True, "result": []}

    @staticmethod
    def raise_for_status():
        return None


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


class _RequestsShim:
    """telegram.requests with its get() diverted into a _Wire.

    print_chat_ids does NOT go through telegram._session: it calls the requests
    module directly, so stubbing the session alone left it reaching the real
    api.telegram.org. Found by running this file before the change, which is
    exactly what a wire test is for."""

    def __init__(self, real, wire):
        self._real = real
        self._wire = wire

    def get(self, url, **kw):
        return self._wire.get(url, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


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


class _PidShim:
    """The os module that answers a DIFFERENT pid, which is what a forked or
    spawned child sees after it inherits the parent's open handle."""

    def __init__(self, real):
        self._real = real

    def getpid(self):
        return self._real.getpid() + 1

    def __getattr__(self, name):
        return getattr(self._real, name)


class _JumpClock:
    """scanner.time_mod whose clocks leap forward. Any decision that waits for
    "long enough" fires immediately under this; a decision that waits for the
    LOCK does not move at all."""

    def __init__(self, real, step=1_000_000.0):
        self._real = real
        self._t = 1_000_000.0
        self._step = step
        self.sleeps = 0

    def sleep(self, _s):
        self.sleeps += 1
        self._t += self._step

    def monotonic(self):
        self._t += self._step
        return self._t

    def time(self):
        self._t += self._step
        return self._t

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


def _pos_row(pid, ticker, strike):
    return {"id": pid, "date": "2026-09-08", "time_et": "09:52:00",
            "ticker": ticker, "direction": "call", "right": "C",
            "strike": strike, "expiry": "2026-09-08", "entry_mid": 4.4,
            "entry_source": "quote", "entry_bid": 4.2, "entry_ask": 4.6,
            "spot_at_signal": 7297.2, "mom_pct": 0.21, "risk_pct": 1.0,
            "correlated": False, "paper": True, "risk_mode": "green",
            "state": "open"}


def _sniper_row(rid, symbol):
    # "state", not "status": sniper_book.open_rows filters on r["state"]
    return {"id": rid, "symbol": symbol, "display": symbol, "direction": "BUY",
            "entry": 100.0, "stop": 99.0, "target": 100.4, "day": "2026-09-08",
            "time_et": "10:00:00", "decimals": 2, "state": "open"}


def _fresh_service(dms=None):
    """A Service with its owner DMs captured instead of sent."""
    svc = scanner.Service()
    svc.dry = False
    svc._hb_owner = (lambda text: dms.append(text)) if dms is not None \
        else (lambda text: None)
    return svc


# a child that has to be readable without hanging the whole suite forever
def _readline(proc, seconds):
    """One line of a child's stdout, or "" if it does not arrive in time. A
    blocking readline on a wedged child would hang the release gate, which is
    the one failure mode a test must never introduce."""
    q = queue.Queue()

    def _pump():
        try:
            q.put(proc.stdout.readline())
        except Exception:
            q.put("")

    threading.Thread(target=_pump, daemon=True).start()
    try:
        return (q.get(timeout=seconds) or "").strip()
    except queue.Empty:
        return ""


def _read_report(proc, seconds):
    """The child's next JSON report line. The scanner prints its own startup
    chatter (live params, backtest notes) on the same stdout, so a plain
    readline picks up a log line and calls it a broken child."""
    deadline = time.monotonic() + seconds
    last = ""
    while time.monotonic() < deadline:
        line = _readline(proc, max(1.0, deadline - time.monotonic()))
        if not line:
            continue
        last = line
        try:
            rep = json.loads(line)
        except ValueError:
            continue
        if isinstance(rep, dict):
            return rep
    return {"_unparsed": last}


# The child preamble every subprocess shares: point at the temp volume, keep
# sends dead, then put a COUNTING transport in front of telegram with test_mode
# switched off, so a poll that should not happen shows up as a recorded URL
# rather than as a real getUpdates against the live token.
_CHILD_HEAD = f"""
import json, os, sys, time
sys.path.insert(0, {str(REPO)!r})
os.environ['DATA_DIR'] = {str(TMP)!r}
os.environ['BOT_TEST_MODE'] = '1'
os.environ['OWNER_CHAT_ID'] = '999999'
os.environ['TELEGRAM_CHAT_IDS'] = '999999'
import config
import instance_lock, telegram, scanner, positions as poslib, sniper_book


class _Resp:
    ok = True
    status_code = 200
    text = '{{"ok": true}}'

    @staticmethod
    def json():
        return {{"ok": True, "result": []}}

    @staticmethod
    def raise_for_status():
        return None


class _Wire:
    def __init__(self):
        self.posts = []
        self.gets = []

    def post(self, url, **kw):
        self.posts.append(url)
        return _Resp()

    def get(self, url, **kw):
        self.gets.append(url)
        return _Resp()


class _RequestsShim:
    def __init__(self, real, wire):
        self._real = real
        self._wire = wire

    def get(self, url, **kw):
        return self._wire.get(url, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


wire = _Wire()
telegram._session = wire
telegram.requests = _RequestsShim(telegram.requests, wire)
telegram._token = lambda: 'TESTTOKEN'
telegram.test_mode = lambda: False


def _state():
    fn = getattr(telegram, 'ownership_state', None)
    return fn() if fn else ''


svc = scanner.Service()
svc.dry = False
svc._hb_owner = lambda text: None
"""


# ---------------------------------------------------------------------------
print("\n--- A. two subprocesses: the loser owns nothing at all ---")

instance_lock.release()
telegram.set_standby(False)
set_state("ACTIVE", "section A parent")
config.POSITIONS_FILE.write_text(
    json.dumps([_pos_row("A-owner", "SPX", 7300.0)]), encoding="utf-8")
pos_bytes_a = config.POSITIONS_FILE.read_bytes()

resA = instance_lock.try_acquire()
check("A the parent takes the lock first", resA in ("acquired", "held"), resA)

child_a = TMP / "child_standby.py"
child_a.write_text(_CHILD_HEAD + """
now = scanner.et_now()
active = svc.ensure_active(now)
items, off = telegram.get_messages()
svc.book.positions = [poslib.Position(**{
    'id': 'CHILD-ROW', 'date': '2026-09-08', 'time_et': '10:05:00',
    'ticker': 'TSLA', 'direction': 'call', 'right': 'C', 'strike': 250.0,
    'expiry': '2026-09-08', 'entry_mid': 1.0, 'entry_source': 'quote',
    'spot_at_signal': 250.0, 'mom_pct': 0.1, 'risk_pct': 1.0,
    'correlated': False, 'paper': True, 'risk_mode': 'green',
    'state': 'open'})]
saved = bool(svc.book.save())
print(json.dumps({'active': bool(active), 'state': _state(),
                  'standby': bool(telegram.standby()[0]),
                  'gets': len(wire.gets), 'posts': len(wire.posts),
                  'items': len(items), 'saved': saved}), flush=True)
t0 = time.monotonic()
promoted = False
while time.monotonic() - t0 < 20.0:
    if svc.ensure_active(scanner.et_now()):
        promoted = True
        break
    time.sleep(0.2)
print(json.dumps({'promoted': promoted, 'state': _state(),
                  'standby': bool(telegram.standby()[0]),
                  'elapsed': round(time.monotonic() - t0, 2)}), flush=True)
""", encoding="utf-8")

proc = subprocess.Popen([sys.executable, str(child_a)], stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True)
try:
    rep = _read_report(proc, 180)
    if "_unparsed" in rep:
        check("A the standby child reported at all", False, str(rep)[:300])
        rep = {}
    if rep:
        check("A the second process does not become active",
              rep.get("active") is False, json.dumps(rep))
        check("A and it reports STANDBY, not a degraded active",
              rep.get("state") == "STANDBY", json.dumps(rep))
        check("A the loser gags its own wire", rep.get("standby") is True,
              json.dumps(rep))
        check("A the loser never calls getUpdates", rep.get("gets") == 0,
              json.dumps(rep))
        check("A the loser sends no card", rep.get("posts") == 0, json.dumps(rep))
        check("A the loser's save() is refused at the write",
              rep.get("saved") is False, json.dumps(rep))
    check("A positions.json is byte-identical after the loser ran",
          config.POSITIONS_FILE.read_bytes() == pos_bytes_a,
          config.POSITIONS_FILE.read_text(encoding="utf-8")[:200])
    # now hand the lock over: the survivor must promote itself, with no human
    instance_lock.release()
    rep2 = _read_report(proc, 90)
    if "_unparsed" in rep2:
        check("A the child reported its promotion", False, str(rep2)[:300])
        rep2 = {}
    if rep2:
        check("A the loser promotes on its own once the lock frees",
              rep2.get("promoted") is True, json.dumps(rep2))
        check("A and it lands in ACTIVE, not in a degraded state",
              rep2.get("state") == "ACTIVE", json.dumps(rep2))
        check("A the promoted copy is un-gagged",
              rep2.get("standby") is False, json.dumps(rep2))
finally:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.stdout.close()
    except Exception:
        pass
    instance_lock.release()

# ---------------------------------------------------------------------------
print("\n--- B. a killed owner does NOT brick the restart (do not brick) ---")

# This is the check the whole reversal is measured against. Removing the
# fail-open means a copy that cannot establish ownership goes quiet, so the
# ordinary railway restart has to still reach ACTIVE by itself, across a real
# process boundary, with no timeout and no human. It was green before this
# package and it must stay green; if it ever goes red the change is wrong.

instance_lock.release()
holder = TMP / "child_holder.py"
holder.write_text(
    "import os, sys, time\n"
    f"sys.path.insert(0, {str(REPO)!r})\n"
    f"os.environ['DATA_DIR'] = {str(TMP)!r}\n"
    "os.environ['BOT_TEST_MODE'] = '1'\n"
    "import instance_lock\n"
    "print(instance_lock.try_acquire(), flush=True)\n"
    "time.sleep(300)\n", encoding="utf-8")
hproc = subprocess.Popen([sys.executable, str(holder)], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
try:
    first = _readline(hproc, 60)
    check("B a child process takes the lock", first == "acquired", first)
    check("B the parent is locked out while the child lives",
          instance_lock.try_acquire() == "contended")
    hproc.kill()          # SIGKILL / TerminateProcess: no cleanup whatsoever
    hproc.wait(timeout=15)
    t0 = time.monotonic()
    got = instance_lock.try_acquire()
    tries = 1
    while got != "acquired" and time.monotonic() - t0 < 2.0:
        got = instance_lock.try_acquire()
        tries += 1
    took = time.monotonic() - t0
    check("B a hard-killed owner leaves no stale lock", got == "acquired", got)
    check("B the reclaim costs no timeout", took < 1.0, f"{took:.2f}s")
finally:
    if hproc.poll() is None:
        hproc.kill()
    try:
        hproc.stdout.close()
    except Exception:
        pass
    instance_lock.release()

# and the full restart path, not just the lock: a brand new process running the
# real ensure_active must reach ACTIVE on its own.
restart = TMP / "child_restart.py"
restart.write_text(_CHILD_HEAD + """
t0 = time.monotonic()
active = svc.ensure_active(scanner.et_now())
print(json.dumps({'active': bool(active), 'state': _state(),
                  'standby': bool(telegram.standby()[0]),
                  'elapsed': round(time.monotonic() - t0, 2)}), flush=True)
""", encoding="utf-8")
rproc = subprocess.Popen([sys.executable, str(restart)], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
try:
    repB = _read_report(rproc, 180)
    if "_unparsed" in repB:
        check("B the restarted process reported at all", False, str(repB)[:300])
        repB = {}
    if repB:
        check("B a restarted process reaches active with no human",
              repB.get("active") is True, json.dumps(repB))
        check("B and it reports ACTIVE", repB.get("state") == "ACTIVE",
              json.dumps(repB))
        check("B the restarted process is not gagged",
              repB.get("standby") is False, json.dumps(repB))
        check("B the restart waits on no timer", repB.get("elapsed", 99) < 10.0,
              json.dumps(repB))
finally:
    if rproc.poll() is None:
        rproc.kill()
    try:
        rproc.stdout.close()
    except Exception:
        pass

# ---------------------------------------------------------------------------
print("\n--- C. no lock primitive is BLOCKED, not a licence to alert ---")

# Astra A03, the headline reversal. The old code answered this case by
# un-gagging the wire, DMing "alerts keep flowing normally" and carrying on.
# Unknown ownership is not exclusive ownership.

instance_lock.release()
telegram.set_standby(False)
config.POSITIONS_FILE.write_text(
    json.dumps([_pos_row("C-owner", "SPX", 7300.0)]), encoding="utf-8")
sniper_book.LEDGER.write_text(json.dumps([_sniper_row("S-1", "^GSPC")]),
                              encoding="utf-8")
pos_bytes_c = config.POSITIONS_FILE.read_bytes()
sniper_bytes_c = sniper_book.LEDGER.read_bytes()

_saved_m, _saved_f = instance_lock._msvcrt, instance_lock._fcntl
_real_test_mode = telegram.test_mode
_real_session = telegram._session
_real_token = telegram._token
_real_requests = telegram.requests
wire = _Wire()
dms = []
blocked = _fresh_service(dms)
try:
    instance_lock._msvcrt = None
    instance_lock._fcntl = None
    telegram.test_mode = lambda: False
    telegram._session = wire
    telegram.requests = _RequestsShim(_real_requests, wire)
    telegram._token = lambda: "TESTTOKEN"
    now = scanner.et_now()
    check("C try_acquire still reports unavailable, not contended",
          instance_lock.try_acquire() == "unavailable")
    check("C a platform with no lock does NOT become active",
          blocked.ensure_active(now) is False)
    probe("C the state is BLOCKED", lambda: owned_state() == "BLOCKED",
          owned_state())
    probe("C instance_lock agrees it is BLOCKED",
          lambda: instance_lock.state() == instance_lock.BLOCKED)
    check("C the wire is gagged", telegram.standby()[0] is True)
    items, off = telegram.get_messages()
    check("C getUpdates is refused with no HTTP call",
          items == [] and not wire.gets, str(wire.gets[:2]))
    telegram.print_chat_ids()
    check("C print_chat_ids does not poll either", not wire.gets,
          str(wire.gets[:2]))
    blocked.book.positions = [poslib.Position(**_pos_row("C-blocked", "TSLA", 250.0))]
    check("C PositionBook.save is refused at the write",
          blocked.book.save() is False)
    check("C positions.json is untouched",
          config.POSITIONS_FILE.read_bytes() == pos_bytes_c)
    check("C sniper_book._write is refused",
          sniper_book._write([_sniper_row("C-bad", "TSLA")]) is False)
    check("C the sniper ledger is untouched",
          sniper_book.LEDGER.read_bytes() == sniper_bytes_c)
    check("C the owner is told once", len(dms) == 1, str(len(dms)))
    blocked.ensure_active(now)
    blocked.ensure_active(now)
    check("C and is not told again every cycle", len(dms) == 1, str(len(dms)))
    text = dms[0] if dms else ""
    check("C the DM does not promise alerts keep flowing",
          "keep flowing" not in text and "keeps flowing" not in text, text[:200])
    check("C the DM says the alerts have stopped",
          "NOT sending" in text or "not sending" in text, text[:200])
    check("C the DM names the platform log as the fallback",
          "log" in text.lower(), text[:200])
    probe("C the DM names what the lock cannot see",
          lambda: sum(1 for c in instance_lock.CANNOT_DETECT
                      if c.lower() in text.lower()) >= 3, text[:300])
finally:
    instance_lock._msvcrt, instance_lock._fcntl = _saved_m, _saved_f
    telegram.test_mode = _real_test_mode
    telegram._session = _real_session
    telegram.requests = _real_requests
    telegram._token = _real_token
    telegram.set_standby(False)
    instance_lock.release()

# ---------------------------------------------------------------------------
print("\n--- D. a lock read that FAILED is BLOCKED, on a fresh boot too ---")

# try_acquire answers "unavailable" when os.open on the lock file raises: a
# read-only remount, EIO or ESTALE on the mount, fd exhaustion, a full volume.
# The old code held the stand-down only for a copy that had ALREADY seen a live
# holder. A fresh boot resumed on the same fault, which is the fail-open again
# wearing a different hat.

_real_os = instance_lock.os
for code, label in ((errno.EIO, "EIO, a broken mount"),
                    (getattr(errno, "ESTALE", errno.EIO), "ESTALE, a moved mount"),
                    (getattr(errno, "EMFILE", errno.EIO), "EMFILE, fd exhaustion"),
                    (errno.ENOSPC, "ENOSPC, a full volume")):
    instance_lock.release()
    telegram.set_standby(False)
    set_state("STARTING", "fresh boot")
    booting = _fresh_service()
    try:
        instance_lock.os = _FaultyOpen(_real_os, code)
        check(f"D a faulted lock read reports unavailable ({label})",
              instance_lock.try_acquire() == "unavailable")
        check(f"D a FRESH boot does not fail open ({label})",
              booting.ensure_active(scanner.et_now()) is False)
        check(f"D and the fresh boot is gagged ({label})",
              telegram.standby()[0] is True)
        probe(f"D the fresh boot reports BLOCKED ({label})",
              lambda: owned_state() == "BLOCKED", owned_state())
    finally:
        instance_lock.os = _real_os
        telegram.set_standby(False)

# the copy that HAS already stood down behaves the same way, which it did
# before this change as well: same answer, one reason, one state.
instance_lock.release()
telegram.set_standby(False)
holder_d = _raw_lock(instance_lock.lock_path())
check("D the stand-in holder took the lock", holder_d is not None)
stood_down = _fresh_service()
try:
    check("D the copy stands down on real contention",
          stood_down.ensure_active(scanner.et_now()) is False)
    probe("D a real holder reads as STANDBY", lambda: owned_state() == "STANDBY",
          owned_state())
    instance_lock.os = _FaultyOpen(_real_os, errno.EIO)
    check("D a copy that already saw a holder stays down on a faulted read",
          stood_down.ensure_active(scanner.et_now()) is False)
    probe("D and it reports BLOCKED, not STANDBY",
          lambda: owned_state() == "BLOCKED", owned_state())
    check("D it is still gagged", telegram.standby()[0] is True)
finally:
    instance_lock.os = _real_os
    if holder_d is not None:
        _raw_unlock(holder_d)
    telegram.set_standby(False)
    instance_lock.release()

# ---------------------------------------------------------------------------
print("\n--- E. a promoted standby installs the holder's world, not its own ---")

instance_lock.release()
telegram.set_standby(False)
set_state("STARTING", "boot")

# the standby boots at 09:20 seeing row A and sniper row S
config.POSITIONS_FILE.write_text(
    json.dumps([_pos_row("E-A", "SPX", 7300.0)]), encoding="utf-8")
sniper_book.LEDGER.write_text(json.dumps([_sniper_row("E-S", "^GSPC")]),
                              encoding="utf-8")
config.state_set("tg_offset", 100)
config.NEWS_SEEN_FILE.write_text(
    json.dumps({"seen": ["boot headline"], "date": "2026-09-08"}),
    encoding="utf-8")
holder_e = _raw_lock(instance_lock.lock_path())
check("E the other copy holds the lock", holder_e is not None)
promo_dms = []
standby_copy = _fresh_service(promo_dms)
boot_ids = sorted(p.id for p in standby_copy.book.positions)
check("E the standby copy booted holding row A", boot_ids == ["E-A"], str(boot_ids))
check("E the standby copy stands down", standby_copy.ensure_active(scanner.et_now()) is False)

# meanwhile the holder opens row B, a second sniper, and moves the offset on
config.POSITIONS_FILE.write_text(
    json.dumps([_pos_row("E-A", "SPX", 7300.0), _pos_row("E-B", "TSLA", 250.0)]),
    encoding="utf-8")
sniper_book.LEDGER.write_text(
    json.dumps([_sniper_row("E-S", "^GSPC"), _sniper_row("E-H", "EURUSD=X")]),
    encoding="utf-8")
config.state_set("tg_offset", 777)
config.NEWS_SEEN_FILE.write_text(
    json.dumps({"seen": ["boot headline", "holder headline"],
                "date": "2026-09-08"}), encoding="utf-8")
seen_snapshots = [config.POSITIONS_FILE.read_text(encoding="utf-8")]

_raw_unlock(holder_e)
holder_e = None
promoted_e = standby_copy.ensure_active(scanner.et_now())
seen_snapshots.append(config.POSITIONS_FILE.read_text(encoding="utf-8"))
check("E the standby promotes once the holder goes away", promoted_e is True)
probe("E the promoted copy is ACTIVE", lambda: owned_state() == "ACTIVE",
      owned_state())
ids_e = sorted(p.id for p in standby_copy.book.positions)
check("E the holder's row survives the promotion", "E-B" in ids_e, str(ids_e))
check("E the standby's own row is kept, not dropped", "E-A" in ids_e, str(ids_e))
rows_e = [r.get("id") for r in sniper_book.open_rows()]
check("E the holder's sniper row is still tracked", "E-H" in rows_e, str(rows_e))
check("E the boot sniper row is still tracked", "E-S" in rows_e, str(rows_e))
check("E the Telegram offset is the holder's, not the boot value",
      int(config.state_get("tg_offset", 0)) == 777,
      str(config.state_get("tg_offset")))
standby_copy.book.save()
seen_snapshots.append(config.POSITIONS_FILE.read_text(encoding="utf-8"))
check("E the boot snapshot was never republished at any point",
      all("E-B" in s for s in seen_snapshots),
      json.dumps([s[:60] for s in seen_snapshots]))
probe("E the reconcile reports every mandatory store it checked",
      lambda: set(standby_copy._reconcile_report) >= {
          "positions", "sniper_positions", "day_reservations",
          "pending_deliveries", "tg_offset", "news_seen", "scheduled_jobs"},
      str(getattr(standby_copy, "_reconcile_report", None)))
check("E the owner is told it promoted",
      len([t for t in promo_dms if "promoted" in t]) == 1, str(promo_dms))
instance_lock.release()
telegram.set_standby(False)

# ---------------------------------------------------------------------------
print("\n--- F. promotion is all or nothing, stale memory is not permission ---")

# _promote used to wrap the re-read in try/except, print, and walk into ACTIVE
# ungagged on the boot-era snapshot. Astra: any mandatory reload failure leaves
# the process RECOVERING or BLOCKED, EVEN IF an older in-memory book is still
# available.


def _corrupt_case(label, break_it, repair):
    instance_lock.release()
    telegram.set_standby(False)
    set_state("STARTING", "boot")
    config.POSITIONS_FILE.write_text(
        json.dumps([_pos_row("F-good", "SPX", 7300.0)]), encoding="utf-8")
    sniper_book.LEDGER.write_text(json.dumps([_sniper_row("F-S", "^GSPC")]),
                                  encoding="utf-8")
    config.state_set("tg_offset", 5)
    svc = _fresh_service()
    before = sorted(p.id for p in svc.book.positions)
    break_it()
    got = svc.ensure_active(scanner.et_now())
    check(f"F {label}: the copy refuses to go active", got is False)
    probe(f"F {label}: it holds in RECOVERING or BLOCKED",
          lambda: owned_state() in ("RECOVERING", "BLOCKED"), owned_state())
    check(f"F {label}: the wire stays gagged", telegram.standby()[0] is True)
    after = sorted(p.id for p in svc.book.positions)
    check(f"F {label}: memory was not partially replaced", after == before,
          f"{before} -> {after}")
    check(f"F {label}: save is refused while recovery is incomplete",
          svc.book.save() is False)
    repair()
    back = svc.ensure_active(scanner.et_now())
    check(f"F {label}: it reaches active the moment the file is repaired",
          back is True)
    probe(f"F {label}: and reports ACTIVE", lambda: owned_state() == "ACTIVE",
          owned_state())
    instance_lock.release()
    telegram.set_standby(False)


_corrupt_case(
    "a truncated positions.json",
    lambda: config.POSITIONS_FILE.write_text('[{"id": "F-good", ',
                                             encoding="utf-8"),
    lambda: config.POSITIONS_FILE.write_text(
        json.dumps([_pos_row("F-good", "SPX", 7300.0)]), encoding="utf-8"))

_corrupt_case(
    "a truncated sniper ledger",
    lambda: sniper_book.LEDGER.write_text('[{"id": "F-S",', encoding="utf-8"),
    lambda: sniper_book.LEDGER.write_text(
        json.dumps([_sniper_row("F-S", "^GSPC")]), encoding="utf-8"))

_corrupt_case(
    "a non-integer tg_offset",
    lambda: config.state_set("tg_offset", "not-a-number"),
    lambda: config.state_set("tg_offset", 5))


def _break_state():
    config.STATE_FILE.write_text('{"tg_offset": 5, ', encoding="utf-8")
    config._LAST_GOOD = {}


def _fix_state():
    config.STATE_FILE.write_text(json.dumps({"tg_offset": 5}), encoding="utf-8")
    corrupt = config.STATE_FILE.with_suffix(".corrupt")
    if corrupt.exists():
        corrupt.unlink()


_corrupt_case("a truncated state.json", _break_state, _fix_state)

# a corrupt state.json must be CLASSIFIED, not quarantined, by the check the
# reconcile makes: quarantining it resets morning_sent, recap_sent, weekly_sent
# and every other once-a-day guard without anyone being told.
config.STATE_FILE.write_text('{"tg_offset": 5, ', encoding="utf-8")
config._LAST_GOOD = {}
probe("F a corrupt state.json is reported corrupt",
      lambda: config.state_health() == "corrupt", str(config.STATE_FILE))
probe("F and reading its health does not quarantine it",
      lambda: config.STATE_FILE.exists()
      and not config.STATE_FILE.with_suffix(".corrupt").exists())
_fix_state()

# and the partial-install rule: positions parses, the sniper ledger does not,
# so NOTHING is installed.
instance_lock.release()
telegram.set_standby(False)
set_state("STARTING", "boot")
config.POSITIONS_FILE.write_text(
    json.dumps([_pos_row("F-boot", "SPX", 7300.0)]), encoding="utf-8")
sniper_book.LEDGER.write_text(json.dumps([_sniper_row("F-S", "^GSPC")]),
                              encoding="utf-8")
partial = _fresh_service()
config.POSITIONS_FILE.write_text(
    json.dumps([_pos_row("F-boot", "SPX", 7300.0),
                _pos_row("F-new", "TSLA", 250.0)]), encoding="utf-8")
sniper_book.LEDGER.write_text('[{"id": "F-S",', encoding="utf-8")
check("F partial: the copy refuses",
      partial.ensure_active(scanner.et_now()) is False)
ids_p = sorted(p.id for p in partial.book.positions)
check("F partial: the good store was NOT installed on its own",
      ids_p == ["F-boot"], str(ids_p))
sniper_book.LEDGER.write_text(json.dumps([_sniper_row("F-S", "^GSPC")]),
                              encoding="utf-8")
check("F partial: both install together once both parse",
      partial.ensure_active(scanner.et_now()) is True)
ids_p2 = sorted(p.id for p in partial.book.positions)
check("F partial: and now the new row is in memory", "F-new" in ids_p2, str(ids_p2))
instance_lock.release()
telegram.set_standby(False)

# an ABSENT store is a clean empty store, not a recovery failure: a fresh
# railway volume has none of these files and must still boot.
instance_lock.release()
telegram.set_standby(False)
set_state("STARTING", "fresh volume")
for gone in (config.POSITIONS_FILE, sniper_book.LEDGER, config.NEWS_SEEN_FILE):
    if gone.exists():
        gone.unlink()
fresh_vol = _fresh_service()
check("F an absent store is clean, so a fresh volume still boots to active",
      fresh_vol.ensure_active(scanner.et_now()) is True)
probe("F and reports ACTIVE", lambda: owned_state() == "ACTIVE", owned_state())
instance_lock.release()
telegram.set_standby(False)

# ---------------------------------------------------------------------------
print("\n--- G. the poller on an independent volume, which the lock cannot see ---")

# Astra A04. A local advisory lock protects cooperating processes opening the
# same lock object on one filesystem. It says NOTHING about another machine,
# another service, a copied volume, an old binary, Task Scheduler, a webhook or
# an ad hoc getUpdates script. The code must not imply otherwise, and the one
# half it CAN enforce is that a process which is not ACTIVE never polls.

other_dir = TMP / "independent_volume"
other_dir.mkdir(exist_ok=True)
instance_lock.release()
own_lock = instance_lock.try_acquire()
other_fd = _raw_lock(other_dir / instance_lock.LOCK_NAME)
check("G two copies on two volumes BOTH acquire, and neither can see the other",
      own_lock == "acquired" and other_fd is not None,
      f"{own_lock} / {other_fd is not None}")
if other_fd is not None:
    _raw_unlock(other_fd)

probe("G instance_lock publishes what it cannot detect",
      lambda: isinstance(instance_lock.CANNOT_DETECT, tuple)
      and len(instance_lock.CANNOT_DETECT) >= 6,
      str(getattr(instance_lock, "CANNOT_DETECT", None)))
for want in ("machine", "volume", "service", "replica", "binary",
             "scheduler", "webhook", "getupdates"):
    probe(f"G the list names {want}",
          lambda w=want: any(w in c.lower()
                             for c in instance_lock.CANNOT_DETECT),
          str(getattr(instance_lock, "CANNOT_DETECT", None)))
probe("G the owner-facing honesty line is GENERATED from that list",
      lambda: all(c.lower() in instance_lock.cannot_detect_line().lower()
                  for c in instance_lock.CANNOT_DETECT),
      str(getattr(instance_lock, "cannot_detect_line", lambda: "")()))
probe("G the module docstring says the lock is not the whole answer",
      lambda: "CANNOT" in (instance_lock.__doc__ or "")
      and "Task Scheduler" in (instance_lock.__doc__ or ""))
probe("G the topology claim is recorded as unproven, not asserted",
      lambda: "unproven" in (instance_lock.__doc__ or "").lower(),
      (instance_lock.__doc__ or "")[:120])
instance_lock.release()

# the enforceable half: no non-ACTIVE state polls, checked at the wire
_real_test_mode = telegram.test_mode
_real_session = telegram._session
_real_token = telegram._token
_real_requests = telegram.requests
try:
    telegram.test_mode = lambda: False
    telegram._token = lambda: "TESTTOKEN"
    for st in NOT_ACTIVE:
        w = _Wire()
        telegram._session = w
        telegram.requests = _RequestsShim(_real_requests, w)
        set_state(st, "section G")
        items, off = telegram.get_messages()
        check(f"G {st} does not call getUpdates",
              items == [] and not w.gets, str(w.gets[:2]))
        telegram.print_chat_ids()
        check(f"G {st} does not poll through print_chat_ids either",
              not w.gets, str(w.gets[:2]))
    # and the state that IS allowed to, so the gate is a gate and not a wall
    w = _Wire()
    telegram._session = w
    telegram.requests = _RequestsShim(_real_requests, w)
    set_state("ACTIVE", "section G")
    telegram.get_messages()
    check("G ACTIVE is the one state that may poll", len(w.gets) == 1,
          str(w.gets[:2]))
finally:
    telegram.test_mode = _real_test_mode
    telegram._session = _real_session
    telegram.requests = _real_requests
    telegram._token = _real_token
    telegram.set_standby(False)

# the 409 warning must not assert a location it cannot know
conflict_dms = []
warner = _fresh_service(conflict_dms)
config.state_set("hb_warned", {})
warner._warn_conflict("Conflict: terminated by other getUpdates request")
ctext = conflict_dms[0] if conflict_dms else ""
check("G the 409 warning reaches the owner", bool(ctext), str(conflict_dms))
check("G the 409 warning does not claim it was an old deploy",
      "old deploy" not in ctext.lower(), ctext[:200])
check("G the 409 warning does not claim it was a local run",
      "local run" not in ctext.lower(), ctext[:200])
for want in ("service", "deployment", "replica", "scheduler", "webhook"):
    check(f"G the 409 warning lists {want} in the inventory to check",
          want in ctext.lower(), ctext[:300])
_live_token = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
check("G the 409 warning never prints the token",
      "TESTTOKEN" not in ctext and not (_live_token and _live_token in ctext),
      ctext[:200])

# ---------------------------------------------------------------------------
print("\n--- H. the state contract table, walked state by state ---")

instance_lock.release()
_real_test_mode = telegram.test_mode
_real_session = telegram._session
_real_token = telegram._token
config.POSITIONS_FILE.write_text(
    json.dumps([_pos_row("H-owner", "SPX", 7300.0)]), encoding="utf-8")
sniper_book.LEDGER.write_text(json.dumps([_sniper_row("H-S", "^GSPC")]),
                              encoding="utf-8")
try:
    telegram.test_mode = lambda: False
    telegram._token = lambda: "TESTTOKEN"
    for st in NOT_ACTIVE:
        w = _Wire()
        telegram._session = w
        set_state(st, "section H")
        pos_before = config.POSITIONS_FILE.read_bytes()
        snip_before = sniper_book.LEDGER.read_bytes()
        telegram.send("entry card")
        check(f"H {st} sends no card", not w.posts, str(w.posts[:2]))
        telegram.get_messages()
        check(f"H {st} runs no production poll", not w.gets, str(w.gets[:2]))
        book = poslib.PositionBook()
        book.positions = [poslib.Position(**_pos_row("H-intruder", "TSLA", 250.0))]
        check(f"H {st} may not write positions.json", book.save() is False)
        check(f"H {st} left positions.json byte-identical",
              config.POSITIONS_FILE.read_bytes() == pos_before)
        check(f"H {st} may not write the sniper ledger",
              sniper_book._write([_sniper_row("H-intruder", "TSLA")]) is False)
        check(f"H {st} left the sniper ledger byte-identical",
              sniper_book.LEDGER.read_bytes() == snip_before)
        # the ops channel is the ONE thing every state keeps: a copy told to be
        # quiet still has to be able to say why it is quiet
        n = len(w.posts)
        telegram.send_to(telegram.primary_owner_id(), "ops note", ops=True)
        check(f"H {st} can still reach the owner with an ops note",
              len(w.posts) > n)

    # RECOVERING in particular: no new entries, no fresh strategy notification
    set_state("RECOVERING", "section H")
    config.state_set("sniper_alerted", {})
    if sniper_book.LEDGER.exists():
        sniper_book.LEDGER.unlink()
    import market_tools  # noqa: E402
    _real_read_any = market_tools.read_any
    _real_photo = telegram.send_photo_all
    try:
        telegram.send_photo_all = lambda *a, **k: None
        market_tools.read_any = lambda *a, **k: {
            "instrument": "EUR/USD", "price": 100.0, "decimals": 2,
            "conviction": "high", "plan": {"direction": "BUY"},
            "recent_bars": None,
            "fvg": {"candidate_id": "cid-h", "confirming": {
                "ticket": {"entry": 100.0, "stop": 99.0, "target": 100.4}}}}
        rec = _fresh_service()
        rec._scan_snipers_once(scanner.et_now(), entries_allowed=True)
        check("H RECOVERING burns no day/symbol reservation",
              not config.state_get("sniper_alerted", {}),
              json.dumps(config.state_get("sniper_alerted", {})))
        check("H RECOVERING opens no new sniper row",
              not sniper_book.LEDGER.exists())
    finally:
        market_tools.read_any = _real_read_any
        telegram.send_photo_all = _real_photo

    # ACTIVE does all of it, so none of the above is an accident of the stub
    w = _Wire()
    telegram._session = w
    set_state("ACTIVE", "section H")
    config.POSITIONS_FILE.write_text(
        json.dumps([_pos_row("H-owner", "SPX", 7300.0)]), encoding="utf-8")
    telegram.send("entry card")
    check("H ACTIVE sends the card", bool(w.posts), str(w.posts[:2]))
    book = poslib.PositionBook()
    book.positions = [poslib.Position(**_pos_row("H-active", "TSLA", 250.0))]
    check("H ACTIVE writes positions.json", book.save() is True)
    check("H ACTIVE really landed the row",
          "H-active" in config.POSITIONS_FILE.read_text(encoding="utf-8"))
    check("H ACTIVE writes the sniper ledger",
          sniper_book._write([_sniper_row("H-active", "TSLA")]) is True)
finally:
    telegram.test_mode = _real_test_mode
    telegram._session = _real_session
    telegram._token = _real_token
    telegram.set_standby(False)
    instance_lock.release()

# ---------------------------------------------------------------------------
print("\n--- I. nothing promotes on a timer, no matter how long it waits ---")

instance_lock.release()
telegram.set_standby(False)
set_state("STARTING", "boot")
config.state_set(instance_lock.LEASE_KEY, {
    "instance_id": "frozen-holder", "pid": 4242, "host": "railway",
    "seq": 11, "heartbeat_ts": time.time()})
timer = _fresh_service()
_real_time_mod = scanner.time_mod
_real_os = instance_lock.os
try:
    scanner.time_mod = _JumpClock(_real_time_mod)
    instance_lock.os = _FaultyOpen(_real_os, errno.EIO)
    ever_active = False
    ever_open = False
    for _ in range(25):
        if timer.ensure_active(scanner.et_now()):
            ever_active = True
        if not telegram.standby()[0]:
            ever_open = True
    check("I a BLOCKED copy never activates, however far the clock jumps",
          ever_active is False)
    check("I and it never un-gags itself", ever_open is False)
    probe("I it is still BLOCKED at the end", lambda: owned_state() == "BLOCKED",
          owned_state())
    # the ONLY thing that promotes it is the lock actually becoming acquirable
    instance_lock.os = _real_os
    check("I the lock coming back is what promotes it, and nothing else",
          timer.ensure_active(scanner.et_now()) is True)
finally:
    instance_lock.os = _real_os
    scanner.time_mod = _real_time_mod
    telegram.set_standby(False)
    instance_lock.release()

# ---------------------------------------------------------------------------
print("\n--- J. the handle is retained, and an inherited one is not ownership ---")

instance_lock.release()
telegram.set_standby(False)
r1 = instance_lock.try_acquire()
fd1 = instance_lock._fd
r2 = instance_lock.try_acquire()
check("J asking twice is answered held, not re-acquired",
      r1 == "acquired" and r2 == "held", f"{r1}/{r2}")
check("J the same handle is retained, never reopened",
      instance_lock._fd is fd1 and fd1 is not None)
probe("J the handle is not inheritable by a child",
      lambda: os.get_inheritable(instance_lock._fd) is False)

lp = instance_lock.lock_path()
st_before = os.stat(str(lp))
holdkeeper = _fresh_service()
for _ in range(5):
    holdkeeper.ensure_active(scanner.et_now())
st_after = os.stat(str(lp))
check("J the lock file is never unlinked or replaced while an owner exists",
      lp.exists() and st_before.st_ino == st_after.st_ino
      and st_before.st_ctime_ns == st_after.st_ctime_ns,
      f"{st_before.st_ino}/{st_after.st_ino}")
check("J the handle survives every one of those cycles",
      instance_lock._fd is fd1)

_real_os = instance_lock.os
try:
    instance_lock.os = _PidShim(_real_os)
    probe("J a child that merely inherited the handle does not report holding",
          lambda: instance_lock.holding() is False)
finally:
    instance_lock.os = _real_os
check("J the real owner still reports holding", instance_lock.holding() is True)
instance_lock.release()
check("J release drops it", instance_lock.holding() is False
      and instance_lock._fd is None)

# ---------------------------------------------------------------------------
instance_lock.release()
telegram.set_standby(False)
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
print("All ownership lifecycle checks passed.")
