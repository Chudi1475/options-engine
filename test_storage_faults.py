"""W05: one storage protocol, and the faults it has to survive.

Astra's W05 row names six faults and four acceptance lines. The six faults are
S1 windows replace permission, S2 disk full, S3 truncated file, S4 concurrent
append, S5 lock file inode replacement, S6 process death. The acceptance lines
are old committed data stays readable, retries are bounded, no silent fallback
to an empty book, and all writers use the same protocol. S7 and S8 pin the two
acceptance lines a fault case does not already cover.

The headline defect, and the reason this file exists at all:

  sniper_book._read() returned [] for a file it could not parse, which is the
  same answer it gives for a book with nothing in it. open_trade() then read
  [], saw no live sniper on the symbol, appended one row and published the
  whole list. Two open trades with live stops were replaced by a one row file
  and the caller was handed the row as if the write had worked. A truncated
  file is not an empty book.

Nothing here touches strategy: no threshold, no roster, no window, no
allow list. Every case is about what happens to bytes on a disk.

No network, no Telegram, no yfinance, no production storage.

Run:  python test_storage_faults.py     (exit code 0 = all good)
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.

import ast
import contextlib
import errno
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# an isolated data dir BEFORE config is imported, so nothing here can read or
# write the real runtime state
_TMP = tempfile.mkdtemp(prefix="kelbot_storage_")
_bot_test_os.environ["DATA_DIR"] = _TMP

import config          # noqa: E402
import forward_ledger  # noqa: E402
import learn           # noqa: E402
import positions       # noqa: E402
import sniper_book     # noqa: E402

# storage_io is the module this package introduces. Imported defensively ON
# PURPOSE: before the change it does not exist, and the cases that only need
# the EXISTING writers must still run and still go red on their own defect
# rather than all collapsing into one import error. Every case that needs the
# new interface says so, and a missing module fails it rather than skipping it.
try:
    import storage_io  # noqa: E402
except ImportError:
    storage_io = None

REPO = pathlib.Path(__file__).parent
TMP = pathlib.Path(_TMP)
_REAL_REPLACE = os.replace
_REAL_WRITE_TEXT = pathlib.Path.write_text
_REAL_WRITE_BYTES = pathlib.Path.write_bytes

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def note(text):
    print(f"       {text}")


def si(attr=None):
    """storage_io, or None. Keeps every use of the new interface honest: a
    case that needs it and cannot get it FAILS, it never quietly skips."""
    if storage_io is None:
        return None
    return storage_io if attr is None else getattr(storage_io, attr, None)


# ---------------------------------------------------------------------------
# fault injectors
# ---------------------------------------------------------------------------
class _ReplaceFault:
    """The real Windows [WinError 5] on tmp.replace(), on demand.

    It hits roughly one test run in ten on this machine, on a different file
    each time, so it is reproduced here rather than described. Fails the first
    `fails` publishes of one target file name and then behaves. pathlib's
    Path.replace looks os.replace up on the os module at CALL time, so
    stubbing os.replace covers every hand rolled writer; storage_io._replace
    is stubbed too, because that module captures the function once."""

    def __init__(self, target_name, fails, delay_s=0.0):
        self.target = target_name
        self.fails = fails
        self.delay_s = delay_s
        self.seen = 0
        self.attempts = 0

    def __call__(self, src, dst):
        if os.path.basename(str(dst)) == self.target:
            self.attempts += 1
            if self.delay_s:
                time.sleep(self.delay_s)
            self.seen += 1
            if self.seen <= self.fails:
                raise PermissionError(13, "WinError 5 access is denied")
        return _REAL_REPLACE(src, dst)


@contextlib.contextmanager
def replace_faults(target_name, fails, delay_s=0.0):
    f = _ReplaceFault(target_name, fails, delay_s)
    os.replace = f
    prev = si("_replace")
    if storage_io is not None:
        storage_io._replace = f
    try:
        yield f
    finally:
        os.replace = _REAL_REPLACE
        if storage_io is not None and prev is not None:
            storage_io._replace = prev


@contextlib.contextmanager
def disk_full(target_suffix=".tmp"):
    """ENOSPC out of the staging write, for every writer in both eras. The
    hand rolled writers stage with Path.write_text; storage_io encodes first
    and stages bytes, so both doors are shut."""
    def boom_text(self, data, **kw):
        if str(self).endswith(target_suffix):
            raise OSError(errno.ENOSPC, "No space left on device")
        return _REAL_WRITE_TEXT(self, data, **kw)

    def boom_bytes(self, data):
        if str(self).endswith(target_suffix):
            raise OSError(errno.ENOSPC, "No space left on device")
        return _REAL_WRITE_BYTES(self, data)

    pathlib.Path.write_text = boom_text
    pathlib.Path.write_bytes = boom_bytes
    try:
        yield
    finally:
        pathlib.Path.write_text = _REAL_WRITE_TEXT
        pathlib.Path.write_bytes = _REAL_WRITE_BYTES


def sandbox(name):
    d = TMP / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def a_position(pid="P1"):
    return positions.Position(
        id=pid, date="2026-09-08", time_et="09:55:00", ticker="SPY",
        direction="call", right="C", strike=650.0, expiry="2026-09-08",
        entry_mid=1.10, entry_source="quote")


def a_sniper_row(symbol, state="open"):
    return {"id": f"2026-09-08-095500-{symbol}-BUY", "candidate_id": None,
            "date": "2026-09-08", "time_et": "09:55:00",
            "symbol": symbol, "display": symbol, "direction": "BUY",
            "entry": 100.0, "stop": 99.0, "target": 100.4,
            "risk": 1.0, "decimals": 2, "state": state, "last_price": 100.0,
            "mfe_r": 0.0, "mae_r": 0.0, "exit_price": None,
            "exit_reason": None, "exit_time": None, "r": None,
            "entry_bar_ts": "2026-09-08 09:55:00-04:00", "last_bar_ts": None,
            "exit_via": None}


# ===========================================================================
print("--- S0. the shared protocol exists and says what it does not do ---")

check("S0 storage_io imports", storage_io is not None,
      "the shared write protocol does not exist yet")

for fn in ("ReadResult", "WriteResult", "LockUnavailable", "file_lock",
           "read_json", "read_jsonl", "write_json", "write_jsonl_all",
           "write_text", "append_jsonl", "append_text", "sweep_orphans",
           "volume_status", "counters", "held", "MAX_REPLACE_ATTEMPTS"):
    check(f"S0 storage_io.{fn} exists", si(fn) is not None)

doc = (getattr(storage_io, "__doc__", "") or "").lower()
# Astra section 4: a shared write helper is not a multi file transaction, and
# the module may not let a reader think otherwise.
check("S0 the docstring says one file's publish IS atomic",
      "atomic" in doc and "one file" in doc, doc[:80])
check("S0 the docstring says a change spanning two files is NOT atomic",
      "not atomic" in doc and "two files" in doc, doc[:80])
check("S0 the docstring says this is not a journal",
      "journal" in doc, doc[:80])
check("S0 the docstring says retention is not unlimited",
      "retention" in doc, doc[:80])

if storage_io is not None:
    rr = storage_io.ReadResult
    ok = rr(status="ok", value=[], path=TMP / "x")
    miss = rr(status="missing", value=None, path=TMP / "x")
    bad = rr(status="corrupt", value=None, path=TMP / "x")
    check("S0 an ok read is usable and trusted", ok.usable and ok.trusted)
    check("S0 a missing file is trusted but not usable",
          ok.usable and not miss.usable and miss.trusted)
    check("S0 a corrupt file is neither usable nor trusted",
          not bad.usable and not bad.trusted)
else:
    check("S0 read status semantics (usable / trusted)", False, "no storage_io")


# ===========================================================================
print("\n--- S1. the Windows replace permission fault, on every writer ---")
# This is the real one run in ten fault on this machine. Before this package
# only PositionBook.save retried, and it retried exactly once.

d = sandbox("s1")

# --- positions ---
pb = positions.PositionBook(path=d / "positions.json")
pb.positions = [a_position()]
with replace_faults("positions.json", 2):
    try:
        pb.save()
        raised = ""
    except Exception as e:                      # noqa: BLE001
        raised = f"{type(e).__name__}: {e}"
got = (d / "positions.json")
check("S1 positions.save survives two transient replace faults",
      not raised and got.exists()
      and len(json.loads(got.read_text(encoding="utf-8"))) == 1,
      raised or f"exists={got.exists()}")

# --- sniper_book ---
sniper_book.LEDGER = d / "sniper_positions.json"
sniper_book.LEDGER.write_text(json.dumps([a_sniper_row("MSFT")]), encoding="utf-8")
with replace_faults("sniper_positions.json", 2):
    try:
        row = sniper_book.open_trade("SPY", "SPY", "BUY", 100.0, 99.0, 100.4,
                                     "2026-09-08", "09:55:00")
        raised = ""
    except Exception as e:                      # noqa: BLE001
        row, raised = None, f"{type(e).__name__}: {e}"
book = json.loads(sniper_book.LEDGER.read_text(encoding="utf-8"))
check("S1 sniper_book survives two transient replace faults",
      not raised and len(book) == 2, raised or f"rows on disk={len(book)}")
check("S1 sniper_book never reports success for a write that did not land",
      (row is not None) == (len(book) == 2),
      f"returned={'row' if row else 'None'} rows={len(book)}")

# --- forward_ledger ---
forward_ledger.LEDGER = d / "sniper_forward.jsonl"
forward_ledger.LEDGER.write_text(json.dumps({"id": "a"}) + "\n", encoding="utf-8")
with replace_faults("sniper_forward.jsonl", 2):
    try:
        with forward_ledger._locked():
            wrote = forward_ledger._write_all([{"id": "a"}, {"id": "b"}])
        raised = ""
    except Exception as e:                      # noqa: BLE001
        wrote, raised = None, f"{type(e).__name__}: {e}"
rows = [l for l in forward_ledger.LEDGER.read_text(encoding="utf-8").splitlines() if l]
check("S1 forward_ledger._write_all survives two transient replace faults",
      not raised and wrote is True and len(rows) == 2,
      raised or f"wrote={wrote} rows={len(rows)}")

# --- config.save_state ---
config.STATE_FILE = d / "state.json"
config.STATE_FILE.write_text(json.dumps({"tg_offset": 7}), encoding="utf-8")
with replace_faults("state.json", 2):
    try:
        config.save_state({"tg_offset": 8, "recap_sent": "2026-09-08"})
        raised = ""
    except Exception as e:                      # noqa: BLE001
        raised = f"{type(e).__name__}: {e}"
st = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
check("S1 config.save_state survives two transient replace faults",
      not raised and st.get("tg_offset") == 8, raised or str(st))

# --- learn lessons rewrite ---
learn.LESSONS_LOG = d / "lessons.jsonl"
learn.LESSONS_LOG.write_text(
    "\n".join(json.dumps({"source_review_id": "R1", "review_revision": 1,
                          "active": True, "session": "2026-09-01",
                          "lessons": ["x"]}) for _ in range(2)) + "\n",
    encoding="utf-8")
with replace_faults("lessons.jsonl", 2):
    try:
        changed = learn._supersede_lessons("R1", 2)
        raised = ""
    except Exception as e:                      # noqa: BLE001
        changed, raised = None, f"{type(e).__name__}: {e}"
after = [json.loads(l) for l in
         learn.LESSONS_LOG.read_text(encoding="utf-8").splitlines() if l]
check("S1 learn's lessons rewrite survives two transient replace faults",
      not raised and changed == 2
      and all(r.get("active") is False for r in after),
      raised or f"changed={changed}")

# --- boundedness, the half of Astra's acceptance line that is about retries ---
if storage_io is None:
    check("S1 retries are bounded and never spin", False, "no storage_io")
else:
    target = d / "bounded.json"
    with replace_faults("bounded.json", 10_000) as f:
        t0 = time.monotonic()
        res = storage_io.write_json(target, {"a": 1})
        elapsed = time.monotonic() - t0
    check("S1 an always denied replace returns replace_denied, never raises",
          res.ok is False and res.status == "replace_denied", str(res))
    check("S1 the retry count is bounded by MAX_REPLACE_ATTEMPTS",
          res.attempts <= storage_io.MAX_REPLACE_ATTEMPTS
          and f.attempts <= storage_io.MAX_REPLACE_ATTEMPTS,
          f"attempts={res.attempts} calls={f.attempts}")
    check("S1 the retry budget is short enough for a 15s poll loop",
          elapsed < 1.5, f"{elapsed:.2f}s")
    left = [p.name for p in d.glob("bounded.json*.tmp")]
    check("S1 a failed publish leaves no orphan staging file", not left, str(left))


# ===========================================================================
print("\n--- S2. disk full is a status, not an exception and not a lie ---")

d = sandbox("s2")

pb = positions.PositionBook(path=d / "positions.json")
pb.positions = [a_position()]
pb.save()                                        # a good file to protect
before_bytes = (d / "positions.json").read_bytes()
with disk_full():
    try:
        pb.positions = [a_position(), a_position("P2")]
        pb.save()
        raised = ""
    except Exception as e:                       # noqa: BLE001
        raised = f"{type(e).__name__}: {e}"
check("S2 positions.save reports a full disk instead of raising at the caller",
      not raised, raised)
check("S2 the previous positions.json survives a full disk byte for byte",
      (d / "positions.json").read_bytes() == before_bytes)

sniper_book.LEDGER = d / "sniper_positions.json"
sniper_book.LEDGER.write_text(json.dumps([a_sniper_row("MSFT")]), encoding="utf-8")
before_bytes = sniper_book.LEDGER.read_bytes()
with disk_full():
    row = sniper_book.open_trade("SPY", "SPY", "BUY", 100.0, 99.0, 100.4,
                                 "2026-09-08", "09:55:00")
check("S2 sniper_book returns None when the trade could not be persisted",
      row is None, "it returned a row for a write that never landed")
check("S2 the previous sniper book survives a full disk byte for byte",
      sniper_book.LEDGER.read_bytes() == before_bytes)

# an exit CARD for a close the disk never took would leave the row open here
# and closed on three phones, and the next poll would text it again. No write,
# no card: the row stays open and the same exit is found and texted once the
# publish lands. This is a named behavior difference, so it is pinned.
sniper_book.LEDGER.write_text(json.dumps([a_sniper_row("MSFT")]), encoding="utf-8")
before_bytes = sniper_book.LEDGER.read_bytes()
with disk_full():
    closed = sniper_book.step("MSFT", 98.0)     # straight through the stop
check("S2 step texts no exit for a close that was never persisted",
      closed is None, "it returned a closed row for a write that never landed")
check("S2 the row is still open on disk after a failed exit write",
      json.loads(sniper_book.LEDGER.read_text(
          encoding="utf-8"))[0]["state"] == "open")
check("S2 the same exit is found again once the disk comes back",
      (sniper_book.step("MSFT", 98.0) or {}).get("exit_reason") == "stop")

if storage_io is None:
    check("S2 storage_io reports disk_full explicitly", False, "no storage_io")
    check("S2 volume_status measures the volume", False, "no storage_io")
else:
    with disk_full():
        res = storage_io.write_json(d / "x.json", {"a": 1})
    check("S2 storage_io maps ENOSPC to status disk_full",
          res.ok is False and res.status == "disk_full", str(res))
    check("S2 a full disk leaves no orphan staging file",
          not any(p.name.endswith(".tmp") for p in d.iterdir()),
          str([p.name for p in d.iterdir() if p.name.endswith(".tmp")]))
    # and the preflight, so a volume with no room is refused before it is
    # written to rather than discovered halfway through
    real_free = storage_io._disk_free
    try:
        storage_io._disk_free = lambda p: 0
        res = storage_io.write_json(d / "y.json", {"a": 1})
    finally:
        storage_io._disk_free = real_free
    check("S2 a preflight on a volume with no room returns disk_full",
          res.ok is False and res.status == "disk_full", str(res))
    check("S2 the preflight refuses before it writes anything",
          not (d / "y.json").exists())
    # Astra section 4: a 500 MB volume may not be treated as unlimited.
    vs = storage_io.volume_status(d)
    check("S2 volume_status reports free and used bytes",
          isinstance(vs.get("free_bytes"), int)
          and isinstance(vs.get("used_bytes"), int)
          and vs["free_bytes"] > 0, str(vs))
    # the Windows disk full winerrors are the same condition wearing a
    # different number, and neither may read as a generic failure
    for code, wine in ((errno.ENOSPC, None), (None, 39), (None, 112)):
        e = OSError()
        e.errno = code
        if wine is not None:
            e.winerror = wine
        check(f"S2 errno={code} winerror={wine} classifies as disk_full",
              storage_io._classify(e) == "disk_full",
              storage_io._classify(e))


# ===========================================================================
print("\n--- S3. a truncated file is not an empty book (THE HEADLINE) ---")

d = sandbox("s3")

# two live snipers with live stops, then the file is cut in half
sniper_book.LEDGER = d / "sniper_positions.json"
full = json.dumps([a_sniper_row("MSFT"), a_sniper_row("NVDA")], indent=1)
sniper_book.LEDGER.write_text(full[:len(full) // 2], encoding="utf-8")
truncated_bytes = sniper_book.LEDGER.read_bytes()

row = sniper_book.open_trade("SPY", "SPY", "BUY", 100.0, 99.0, 100.4,
                             "2026-09-08", "09:55:00")
check("S3 open_trade refuses to publish over a book it could not parse",
      sniper_book.LEDGER.read_bytes() == truncated_bytes,
      "the truncated book was replaced by a one row file, and two live "
      "stops went with it")
check("S3 open_trade returns None rather than a row it did not store",
      row is None, "it returned a row")

# has_open must stand aside on an unreadable book, not wave a second live
# sniper through onto a symbol that may already have one. scanner sends the
# card BEFORE open_trade, so a False here texts a ticket nothing will track.
check("S3 has_open stands aside on an unreadable book",
      sniper_book.has_open("MSFT") is True,
      "it reported no open trade for a book it could not read")
check("S3 step never publishes over an unreadable book",
      sniper_book.step("MSFT", 99.5) is None
      and sniper_book.LEDGER.read_bytes() == truncated_bytes)

# positions already gets this right. It is pinned so it stays right.
pb = positions.PositionBook(path=d / "positions.json")
pb.positions = [a_position()]
pb.save()
(d / "positions.json").write_text("[{\"id\": \"P1\", \"da", encoding="utf-8")
pb.reload()
check("S3 a truncated positions.json leaves memory exactly as it was",
      [p.id for p in pb.positions] == ["P1"])

# state.json: the quarantine is the designed answer, and the bad bytes have
# to survive it. What may never happen is a one key file written over content
# nobody could read.
config.STATE_FILE = d / "state.json"
config.STATE_FILE.write_text('{"tg_offset": 7, "recap_se', encoding="utf-8")
bad_bytes = config.STATE_FILE.read_bytes()
config.state_set("tg_offset", 9)
corrupt = d / "state.corrupt"
check("S3 a corrupt state.json is quarantined, not overwritten in place",
      corrupt.exists() and corrupt.read_bytes() == bad_bytes,
      f"corrupt sidecar exists={corrupt.exists()}")

# a jsonl whose last line was cut off: the parseable prefix is real evidence
# and must be returned, but the damage has to be visible.
if storage_io is None:
    check("S3 read_jsonl reports bad lines and truncation", False, "no storage_io")
else:
    p = d / "sniper_forward.jsonl"
    p.write_text(json.dumps({"id": "a"}) + "\n" + json.dumps({"id": "b"}) + "\n"
                 + '{"id": "c"', encoding="utf-8")
    res = storage_io.read_jsonl(p)
    check("S3 read_jsonl returns the parseable prefix",
          [r["id"] for r in (res.value or [])] == ["a", "b"], str(res.value))
    check("S3 read_jsonl counts the unparseable line",
          res.bad_lines == 1, str(res.bad_lines))
    check("S3 read_jsonl says the file is truncated",
          res.truncated is True, str(res.truncated))
    check("S3 a partly readable jsonl is not reported as ok",
          res.status == "corrupt" and not res.usable, res.status)

# and the distinction the whole acceptance line rests on
if storage_io is None:
    check("S3 missing and unreadable are different statuses", False, "no storage_io")
else:
    missing = storage_io.read_json(d / "not_here.json")
    corrupt_r = storage_io.read_json(sniper_book.LEDGER)
    check("S3 missing and unreadable are different statuses",
          missing.status == "missing" and corrupt_r.status == "corrupt"
          and missing.status != corrupt_r.status,
          f"{missing.status} vs {corrupt_r.status}")
    check("S3 a missing file is trusted, an unreadable one is not",
          missing.trusted and not corrupt_r.trusted)


# ===========================================================================
print("\n--- S4. concurrent append and concurrent read modify write ---")

d = sandbox("s4")
learn.LESSONS_LOG = d / "lessons.jsonl"
# a file with some weight, so the rewrite window is a real window
seed = [json.dumps({"session": f"seed{i}", "kind": "nightly", "lessons": ["s"],
                    "source_review_id": f"S{i}", "review_revision": 1,
                    "active": True}) for i in range(200)]
learn.LESSONS_LOG.write_text("\n".join(seed) + "\n", encoding="utf-8")

REWRITES = 10
errs, injected, pending = [], [], []


def one_lesson(session, text):
    learn._append_lesson({"session": session, "graded_at": "x",
                          "wins": 0, "losses": 0, "trades": [],
                          "review": "r", "lessons": [text],
                          "watch_tomorrow": "", "proposed_change": None})


def _inject(session):
    try:
        one_lesson(session, "appended mid rewrite")
    except Exception as e:                       # noqa: BLE001
        errs.append(f"append: {type(e).__name__}: {e}")


def stage_hook(self, data, **kw):
    """Land ONE append from another thread in the window between the
    rewriter's read and its publish, deterministically.

    A wall clock race on this machine hides the defect rather than showing it:
    Windows refuses os.replace outright while the appender holds the target
    open, so the rewrite that would have destroyed the line just fails. This
    puts the append exactly where the defect lives and waits a bounded moment
    for it. Unlocked, the append lands and is then republished away. Under one
    lock covering the whole read modify write, the appender is still waiting
    when the publish happens and its line survives."""
    r = _REAL_WRITE_TEXT(self, data, **kw) if isinstance(data, str) \
        else _REAL_WRITE_BYTES(self, data)
    if "lessons.jsonl" in str(self) and str(self).endswith(".tmp"):
        s = f"inj{len(injected)}"
        injected.append(s)
        t = threading.Thread(target=_inject, args=(s,))
        t.start()
        t.join(timeout=0.25)
        pending.append(t)
    return r


def slow_text(self, data, **kw):
    return stage_hook(self, data, **kw)


def slow_bytes(self, data):
    return stage_hook(self, data)


pathlib.Path.write_text = slow_text
pathlib.Path.write_bytes = slow_bytes
try:
    for i in range(REWRITES):
        try:
            # same session and kind every time, so the upsert path filters and
            # republishes the whole file on every pass
            one_lesson("hot", f"hot {i}")
        except Exception as e:                   # noqa: BLE001
            errs.append(f"rewrite: {type(e).__name__}: {e}")
finally:
    pathlib.Path.write_text = _REAL_WRITE_TEXT
    pathlib.Path.write_bytes = _REAL_WRITE_BYTES
for t in pending:
    t.join(10)

lines = [l for l in learn.LESSONS_LOG.read_text(encoding="utf-8").splitlines() if l]
parsed, torn = [], 0
for l in lines:
    try:
        parsed.append(json.loads(l))
    except json.JSONDecodeError:
        torn += 1
sessions = {r.get("session") for r in parsed}
lost = [s for s in injected if s not in sessions]

check("S4 no line is torn or interleaved by a concurrent append", torn == 0,
      f"{torn} unparseable lines")
check("S4 the rewriter actually rewrote", len(injected) >= REWRITES - 1,
      f"{len(injected)} rewrites staged")
check("S4 no append is destroyed by a concurrent whole file rewrite",
      not lost, f"{len(lost)} of {len(injected)} appended rows lost: {lost[:5]}")
check("S4 the seed rows are all still there",
      len([r for r in parsed if str(r.get("session", "")).startswith("seed")]) == 200,
      str(len([r for r in parsed if str(r.get("session", "")).startswith("seed")])))
check("S4 neither writer raised", not errs, str(errs[:3]))

# --- the ledger's own lock, and the hole in it -------------------------------
# forward_ledger._locked computed `outermost = got and depth == 0`, so when
# the in process RLock timed out the OS lock was never even attempted and the
# whole file rewrite ran completely unserialized. Fail open on the LOCK is not
# the same thing as fail open on the WRITE.
forward_ledger.LEDGER = d / "sniper_forward.jsonl"
forward_ledger.LEDGER.write_text(json.dumps({"id": "keep"}) + "\n", encoding="utf-8")
keep_bytes = forward_ledger.LEDGER.read_bytes()

if si("held") is None:
    check("S4 _locked reports whether it actually holds the lock", False,
          "no storage_io.held")
    check("S4 an unheld lock blocks a whole file rewrite", False, "no storage_io")
else:
    with forward_ledger._locked() as lk:
        check("S4 _locked reports whether it actually holds the lock",
              getattr(lk, "held", None) is True, str(lk))

    busy = threading.Event()
    done = threading.Event()

    def hog():
        with storage_io.file_lock(forward_ledger.LEDGER, budget_s=5.0) as h:
            busy.set()
            if h.held:
                done.wait(5)

    th = threading.Thread(target=hog, daemon=True)
    th.start()
    busy.wait(5)
    try:
        with forward_ledger._locked() as lk:
            unheld = getattr(lk, "held", None) is False
            wrote = forward_ledger._write_all([{"id": "clobber"}])
    finally:
        done.set()
        th.join(5)
    check("S4 a busy in process lock is reported as not held", unheld, str(lk))
    check("S4 an unheld lock blocks a whole file rewrite",
          wrote is False and forward_ledger.LEDGER.read_bytes() == keep_bytes,
          f"wrote={wrote}")

# --- two PROCESSES, so the OS lock is doing the work, not the RLock ----------
if storage_io is None:
    check("S4 the lock serializes two processes, not just two threads", False,
          "no storage_io")
else:
    target = d / "counter.json"
    storage_io.write_json(target, {"n": 0})
    child = REPO / "_s4_child.py"
    child.write_text(
        "import json, sys, pathlib\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "import os\n"
        "os.environ['BOT_TEST_MODE'] = '1'\n"
        "import storage_io\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "for _ in range(40):\n"
        "    with storage_io.file_lock(p, budget_s=10.0) as lk:\n"
        "        if not lk.held:\n"
        "            print('UNHELD'); sys.exit(2)\n"
        "        cur = storage_io.read_json(p).value or {'n': 0}\n"
        "        cur['n'] = cur['n'] + 1\n"
        "        storage_io.write_json(p, cur)\n"
        "print('OK')\n", encoding="utf-8")
    try:
        kids = [subprocess.Popen([sys.executable, str(child), str(target)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True) for _ in range(3)]
        outs = [k.communicate(timeout=120) for k in kids]
        final = storage_io.read_json(target)
        check("S4 the lock serializes two processes, not just two threads",
              all(k.returncode == 0 for k in kids)
              and final.usable and final.value.get("n") == 120,
              f"n={(final.value or {}).get('n')} "
              f"rcs={[k.returncode for k in kids]} {outs[0][1][:200]}")
    finally:
        try:
            child.unlink()
        except OSError:
            pass


# ===========================================================================
print("\n--- S5. the lock file is a sidecar, and it is never swapped out ---")

d = sandbox("s5")
forward_ledger.LEDGER = d / "sniper_forward.jsonl"
lp = forward_ledger._lock_path()
# with_suffix turned sniper_forward.jsonl into sniper_forward.lock, a name any
# sibling with the same stem collides on. name + '.lock' cannot collide.
check("S5 the ledger's sidecar is name + .lock, not with_suffix",
      lp.name == "sniper_forward.jsonl.lock", lp.name)

if storage_io is None:
    check("S5 file_lock uses a non colliding sidecar", False, "no storage_io")
    check("S5 storage_io never unlinks a lock file", False, "no storage_io")
    check("S5 a swapped lock file is detected, not silently shared", False,
          "no storage_io")
else:
    p = d / "book.json"
    check("S5 file_lock uses a non colliding sidecar",
          storage_io.lock_path(p).name == "book.json.lock",
          storage_io.lock_path(p).name)

    # Astra section 3: do not replace or unlink the lock file while owners
    # exist. The module has to be structurally incapable of it.
    src = (REPO / "storage_io.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    unlinks = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)
               and n.func.attr in ("unlink", "remove")
               and "lock" in ast.unparse(n.func.value).lower()]
    check("S5 storage_io never unlinks a lock file", not unlinks, str(unlinks))

    # the swap itself. On Windows the open handle usually refuses the delete;
    # that is reported as a SKIP line rather than left silently absent.
    with storage_io.file_lock(p) as lk:
        held_first = lk.held
        swapped = False
        try:
            storage_io.lock_path(p).unlink()
            storage_io.lock_path(p).write_text("", encoding="utf-8")
            swapped = True
        except OSError as e:
            note(f"SKIP S5 inode swap: this platform refuses it while the "
                 f"handle is open ({type(e).__name__})")
        if swapped:
            check("S5 a swapped lock file is detected, not silently shared",
                  lk.revalidate() is False and "replace" in (lk.why or ""),
                  f"held={lk.revalidate()} why={lk.why}")
        else:
            # the detection itself is still checked, on a lock whose sidecar
            # identity is deliberately wrong
            fake = storage_io.Lock(held=True, why="", path=storage_io.lock_path(p),
                                   fd=None, dev=-1, ino=-1)
            check("S5 a swapped lock file is detected, not silently shared",
                  fake.revalidate() is False and "replace" in (fake.why or ""),
                  f"why={fake.why}")
    check("S5 the lock was held before the swap", held_first is True)


# ===========================================================================
print("\n--- S6. process death: a released lock, an intact file, a swept tmp ---")

d = sandbox("s6")

if storage_io is None:
    check("S6 a dead holder's lock is immediately re acquirable", False,
          "no storage_io")
    check("S6 the real file still parses as its pre crash content", False,
          "no storage_io")
    check("S6 sweep_orphans clears the staging file the crash left", False,
          "no storage_io")
    check("S6 two files are left inconsistent and the module admits it", False,
          "no storage_io")
else:
    p = d / "book.json"
    storage_io.write_json(p, [{"id": "before the crash"}])
    before_bytes = p.read_bytes()
    child = REPO / "_s6_child.py"
    child.write_text(
        "import os, sys, pathlib, time\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "os.environ['BOT_TEST_MODE'] = '1'\n"
        "import storage_io\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "orphan = p.with_name(p.name + '.9999.1.1.tmp')\n"
        "orphan.write_text('half a book', encoding='utf-8')\n"
        "lk = storage_io.file_lock(p, budget_s=10.0)\n"
        "lk.__enter__()\n"
        "sys.stdout.write('LOCKED'); sys.stdout.flush()\n"
        "os._exit(1)\n", encoding="utf-8")
    try:
        r = subprocess.run([sys.executable, str(child), str(p)],
                           capture_output=True, text=True, timeout=120)
        note(f"child said {r.stdout.strip()!r}, exit {r.returncode}")
        t0 = time.monotonic()
        with storage_io.file_lock(p, budget_s=5.0) as lk:
            took = time.monotonic() - t0
            check("S6 a dead holder's lock is immediately re acquirable",
                  lk.held is True and took < 4.0,
                  f"held={lk.held} why={lk.why} took={took:.2f}s")
        check("S6 the real file still parses as its pre crash content",
              p.read_bytes() == before_bytes
              and storage_io.read_json(p).value == [{"id": "before the crash"}])
        orphan = p.with_name(p.name + ".9999.1.1.tmp")
        check("S6 the crash really left a staging file behind", orphan.exists())
        swept = storage_io.sweep_orphans(d, older_than_s=0)
        check("S6 sweep_orphans clears the staging file the crash left",
              swept >= 1 and not orphan.exists(), f"swept={swept}")

        # and something actually CALLS it: the first publish into a directory
        # sweeps that directory's cold orphans, so a crash's leavings do not
        # sit there for weeks the way the repo root's have
        cold = d / "sweepme"
        cold.mkdir(exist_ok=True)
        stale = cold / "book.json.1.1.1.tmp"
        stale.write_text("half a book", encoding="utf-8")
        os.utime(stale, (time.time() - 7200, time.time() - 7200))
        fresh = cold / "book.json.2.2.2.tmp"
        fresh.write_text("being staged right now", encoding="utf-8")
        storage_io.write_json(cold / "book.json", {"n": 1})
        check("S6 the first publish into a directory sweeps its cold orphans",
              not stale.exists(), "the orphan is still there")
        check("S6 the sweep never touches a staging file still in flight",
              fresh.exists(), "it deleted a temp another writer was staging")
    finally:
        try:
            child.unlink()
        except OSError:
            pass

    # The honest half of Astra's rule against pretending separate file
    # replacements are a transaction: they are not, so the test asserts the
    # inconsistency AND asserts the module says so out loud.
    f1, f2 = d / "one.json", d / "two.json"
    storage_io.write_json(f1, {"n": 0})
    storage_io.write_json(f2, {"n": 0})
    child = REPO / "_s6_child2.py"
    child.write_text(
        "import os, sys, pathlib\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "os.environ['BOT_TEST_MODE'] = '1'\n"
        "import storage_io\n"
        "a = pathlib.Path(sys.argv[1]); b = pathlib.Path(sys.argv[2])\n"
        "storage_io.write_json(a, {'n': 1})\n"
        "os._exit(1)\n", encoding="utf-8")
    try:
        subprocess.run([sys.executable, str(child), str(f1), str(f2)],
                       capture_output=True, text=True, timeout=120)
    finally:
        try:
            child.unlink()
        except OSError:
            pass
    a_n = (storage_io.read_json(f1).value or {}).get("n")
    b_n = (storage_io.read_json(f2).value or {}).get("n")
    check("S6 two files are left inconsistent and the module admits it",
          a_n == 1 and b_n == 0
          and "not atomic" in (storage_io.__doc__ or "").lower()
          and "two files" in (storage_io.__doc__ or "").lower(),
          f"a={a_n} b={b_n}")


# ===========================================================================
print("\n--- S7. old committed data still reads, with no migration imposed ---")

d = sandbox("s7")

# byte for byte fixtures of the CURRENT on disk shapes, including the legacy
# ones: a bare list with a null old_bracket and no candidate_id
legacy_positions = ('[\n {\n  "id": "SPY-20260901-0955",\n  "date": "2026-09-01",\n'
                    '  "time_et": "09:55:00",\n  "ticker": "SPY",\n'
                    '  "direction": "call",\n  "right": "C",\n  "strike": 640.0,\n'
                    '  "expiry": "2026-09-01",\n  "entry_mid": 1.05,\n'
                    '  "entry_source": "quote",\n  "old_bracket": null,\n'
                    '  "old_rules": {"status": "open", "exit_pct": null,\n'
                    '   "exit_reason": null, "exit_time": null}\n }\n]\n')
(d / "positions.json").write_text(legacy_positions, encoding="utf-8")
legacy_sniper = ('[\n {"id": "2026-09-01-095500-SPY-BUY", "date": "2026-09-01",\n'
                 '  "time_et": "09:55:00", "symbol": "SPY", "display": "SPY",\n'
                 '  "direction": "BUY", "entry": 640.0, "stop": 639.0,\n'
                 '  "target": 640.4, "risk": 1.0, "decimals": 2,\n'
                 '  "state": "closed", "last_price": 640.4, "r": 0.4,\n'
                 '  "exit_reason": "target"}\n]\n')
(d / "sniper_positions.json").write_text(legacy_sniper, encoding="utf-8")
(d / "state.json").write_text('{"tg_offset": 4021, "account_value": 5000}\n',
                              encoding="utf-8")
(d / "sniper_forward.jsonl").write_text(
    '{"id": "abc", "day": "2026-09-01", "outcome": null}\n', encoding="utf-8")
(d / "lessons.jsonl").write_text(
    '{"session": "2026-09-01", "lessons": ["one"]}\n', encoding="utf-8")

pb = positions.PositionBook(path=d / "positions.json")
check("S7 a legacy positions.json still loads its rows",
      [p.id for p in pb.positions] == ["SPY-20260901-0955"],
      str([p.id for p in pb.positions]))
sniper_book.LEDGER = d / "sniper_positions.json"
check("S7 a legacy sniper book still reads its closed record",
      sniper_book.record()["n"] == 1, str(sniper_book.record()))
config.STATE_FILE = d / "state.json"
check("S7 a committed state.json still loads",
      config.load_state().get("tg_offset") == 4021)
forward_ledger.LEDGER = d / "sniper_forward.jsonl"
check("S7 a committed forward ledger still reads",
      [r["id"] for r in forward_ledger._read_all()] == ["abc"])
learn.LESSONS_LOG = d / "lessons.jsonl"
check("S7 a committed lessons.jsonl still reads",
      len(learn._all_lessons()) == 1)

if storage_io is None:
    check("S7 the readers impose no version envelope", False, "no storage_io")
else:
    for name in ("positions.json", "sniper_positions.json", "state.json"):
        r = storage_io.read_json(d / name)
        check(f"S7 {name} reads ok with no envelope and no migration",
              r.status == "ok" and r.usable, r.status)
    r = storage_io.read_jsonl(d / "sniper_forward.jsonl")
    check("S7 a committed jsonl reads ok with no envelope",
          r.status == "ok" and r.bad_lines == 0, r.status)
    check("S7 the readers impose no version envelope",
          "schema_version" not in json.dumps(
              storage_io.read_json(d / "state.json").value))


# ===========================================================================
print("\n--- S8. all writers use the same protocol ---")
# Astra's acceptance line, turned into a scan rather than a claim. Scoped to
# the five modules W05 migrates; scanner and telegram own their own files and
# are a different package.

MIGRATED = ("positions.py", "sniper_book.py", "forward_ledger.py",
            "config.py", "learn.py")
hits = {"hand rolled tmp": [], "raw write_text": [], "raw append": []}
for name in MIGRATED:
    tree = ast.parse((REPO / name).read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue
        src = ast.unparse(n)
        if n.func.attr == "with_suffix" and ".tmp" in src:
            hits["hand rolled tmp"].append(f"{name}:{n.lineno}")
        if n.func.attr == "write_text" and not src.startswith("storage_io."):
            hits["raw write_text"].append(f"{name}:{n.lineno}")
        if n.func.attr == "open":
            mode = ""
            if n.args and isinstance(n.args[0], ast.Constant):
                mode = str(n.args[0].value)
            for kw in n.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            if "a" in mode:
                hits["raw append"].append(f"{name}:{n.lineno}")

for label, found in hits.items():
    check(f"S8 no {label} left in the migrated writers", not found, str(found))
check("S8 every migrated writer imports the shared protocol",
      all("import storage_io" in (REPO / n).read_text(encoding="utf-8")
          for n in MIGRATED),
      str([n for n in MIGRATED
           if "import storage_io" not in (REPO / n).read_text(encoding="utf-8")]))
check("S8 storage_io does not import config, so a ledger can depend on it",
      storage_io is None
      or "import config" not in (REPO / "storage_io.py").read_text(encoding="utf-8"))

# the counters the health record needs, so a write failure is countable rather
# than only printable
if storage_io is None:
    check("S8 counters expose writes and failures", False, "no storage_io")
else:
    c = storage_io.counters()
    for k in ("writes", "write_failures", "replace_retries", "reads_unreadable",
              "reads_corrupt", "locks_unavailable", "orphans_swept"):
        check(f"S8 counters expose {k}", k in c, str(sorted(c)))


# ===========================================================================
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All storage fault checks passed.")
