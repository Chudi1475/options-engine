"""Regressions for P05: review import recovery and learning integrity.

Every check below reproduced a real defect in learn.py before its fix.

R04   An import that wrote the review row and then failed to write the lesson
      left the review committed and the lesson missing. Re-running the SAME
      file was routed into "already reviewed", which refused the whole batch,
      so the normal retry could never resume the missing work. Only the
      separate --repair-lessons flag recovered, while the docstring promised
      a plain re-run would.

R04b  The completion line concatenated the words "digest rebuilt"
      unconditionally, so a run whose derivation or rebuild had just failed
      still reported a rebuild that never happened.

R04c  Each accepted row got its own append, so an N row batch had N torn
      write windows. A batch torn after row 1 could never be finished,
      because the retry hit the R04 refusal on row 1.

R05   The docstring says the immutable facts, verdict included, are copied
      from the tracked position. Every other field was, but the verdict was
      read straight from the untrusted file, so a fabricated WIN could be
      filed against a canonical loss and then feed the lessons digest.

R06   The idempotence guard matched on source_review_id, and the online deep
      review wrote its lessons without that key, so the first repair run
      after any online review appended a second copy of every one of them.

R06b  The same guard returned True for a review id of None whenever any
      legacy row was present, and failed open on a read error, which would
      re-append the entire lesson history in one pass.

E2    Lesson rows carried no source trade ids, evidence class, strategy
      version or review revision, and a corrected review could never reach
      the digest: the guard saw the id already recorded and derived nothing,
      so the superseded text stayed in the active playbook forever.

No network, no Telegram, no model API, no production storage, no git writes.

Run:  python test_review_import.py     (exit code 0 = all good)
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.

import io
import json
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# an isolated data dir BEFORE config is imported, so nothing here can read or
# write the real runtime state
_TMP = tempfile.mkdtemp(prefix="kelbot_import_test_")
_bot_test_os.environ["DATA_DIR"] = _TMP

import config          # noqa: E402
import learn           # noqa: E402
import positions as poslib   # noqa: E402
import recap           # noqa: E402

REPO = Path(__file__).parent
failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def case(label):
    """Run one block immediately; an exception inside it is a failure for that
    block, never a crash that hides every later block."""
    def deco(fn):
        try:
            fn()
        except Exception as e:
            check(f"{label}: block completed", False,
                  f"{type(e).__name__}: {e}")
        return fn
    return deco


# ---------------------------- fixtures ----------------------------

def fresh(prefix):
    """A per-case temp dir with learn's three files redirected into it."""
    d = Path(tempfile.mkdtemp(prefix=prefix, dir=_TMP))
    learn.REVIEWS_FILE = d / "trade_reviews.jsonl"
    learn.LESSONS_LOG = d / "lessons.jsonl"
    learn.LESSONS_DIGEST = d / "lessons_digest.md"
    return d


def pos(pid, pnl, ticker="SPX", day="2026-09-04", paper=False):
    p = poslib.Position(id=pid, date=day, time_et="09:50:01", ticker=ticker,
                        direction="call", right="C", strike=7745.0,
                        expiry=day, entry_mid=7.85, entry_source="quote")
    p.state, p.final_pnl_pct, p.paper = "closed", pnl, paper
    return p


def install(*positions):
    book = poslib.PositionBook()
    book.positions = list(positions)
    learn.PositionBook = lambda *a, **k: book
    return book


def row(pid, **kw):
    base = dict(id=pid, date="2026-09-04", ticker="SPX", direction="call",
                strike=7745.0, paper=False, final_pnl_pct=-50.0,
                verdict="WRONG", why="stopped out on the retest",
                cause="setup", cause_detail="", lesson=f"lesson for {pid}")
    base.update(kw)
    return base


def write_batch(d, name, rows):
    f = d / name
    f.write_text(json.dumps({"reviews": rows}), encoding="utf-8")
    return f


def jsonl(p):
    if not Path(p).exists():
        return []
    return [json.loads(ln) for ln in
            Path(p).read_text(encoding="utf-8").splitlines() if ln.strip()]


def cap(fn, *a, **k):
    """(return value, captured stdout) for one call."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rv = fn(*a, **k)
    return rv, buf.getvalue()


def boom(msg):
    def _raise(*a, **k):
        raise OSError(msg)
    return _raise


class TornAppend:
    """Path proxy whose append writes only the FIRST line, then dies. Models a
    process killed mid append, which is the one window a single fsynced write
    still leaves open."""

    def __init__(self, real):
        self._real = real

    def open(self, *a, **k):
        return _TornFile(self._real.open(*a, **k))

    def exists(self):
        return self._real.exists()

    def read_text(self, *a, **k):
        return self._real.read_text(*a, **k)

    def write_text(self, *a, **k):
        return self._real.write_text(*a, **k)

    def with_suffix(self, *a, **k):
        return self._real.with_suffix(*a, **k)

    def __fspath__(self):
        return str(self._real)


class _TornFile:
    def __init__(self, real):
        self._real = real

    def write(self, s):
        first = str(s).split("\n")[0]
        self._real.write(first + "\n")
        self._real.flush()
        raise OSError("process killed mid append")

    def flush(self):
        self._real.flush()

    def fileno(self):
        return self._real.fileno()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._real.close()
        return False


# ==========================================================================
# R04: a retry of the same partial batch resumes the missing lesson
# ==========================================================================
@case("R04")
def _r04():
    d = fresh("r04_")
    install(pos("LOSS-1", -50.0))
    f = write_batch(d, "in.json", [row("LOSS-1")])

    real_append = learn._append_lesson
    learn._append_lesson = boom("disk full")
    try:
        n1, out1 = cap(learn.import_reviews, str(f))
    finally:
        learn._append_lesson = real_append

    check("R04: the review row commits even though its lesson write failed",
          n1 == 1 and len(jsonl(learn.REVIEWS_FILE)) == 1, f"wrote {n1}")
    check("R04: the failed lesson write is reported, not swallowed",
          "lesson" in out1.lower() and len(jsonl(learn.LESSONS_LOG)) == 0,
          f"out={out1!r}")

    n2, out2 = cap(learn.import_reviews, str(f))
    check("R04: re-running the same file is not refused as already reviewed",
          "REFUSED" not in out2, f"out={out2!r}")
    check("R04: the retry resumes the missing lesson",
          len(jsonl(learn.LESSONS_LOG)) == 1,
          f"lessons={jsonl(learn.LESSONS_LOG)}")
    check("R04: the retry writes no duplicate review row",
          len(jsonl(learn.REVIEWS_FILE)) == 1 and n2 == 0, f"retry wrote {n2}")
    check("R04: the retry says the row was resumed, not newly imported",
          "resum" in out2.lower(), f"out={out2!r}")

    n3, _ = cap(learn.import_reviews, str(f))
    check("R04: a third run adds nothing",
          n3 == 0 and len(jsonl(learn.LESSONS_LOG)) == 1)


# ==========================================================================
# R04b: a failed digest rebuild is never logged as a success
# ==========================================================================
@case("R04b")
def _r04b():
    d = fresh("r04b_")
    install(pos("LOSS-2", -50.0))
    f = write_batch(d, "in.json", [row("LOSS-2")])

    real_rebuild = learn._rebuild_digest
    learn._rebuild_digest = boom("digest disk full")
    try:
        n, out = cap(learn.import_reviews, str(f))
        res, _ = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    finally:
        learn._rebuild_digest = real_rebuild

    check("R04b: the import never claims a rebuild that did not happen",
          "digest rebuilt" not in out and not learn.LESSONS_DIGEST.exists(),
          f"out={out!r}")
    check("R04b: the import says the digest was NOT rebuilt",
          "NOT rebuilt" in out, f"out={out!r}")
    check("R04b: the structured result reports the rebuild failure",
          res.get("digest_rebuilt") is False and res.get("ok") is False
          and any("digest" in str(e) for e in res.get("errors", [])),
          f"res={res}")

    res2, _ = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    check("R04b: a clean repair reports ok and a real rebuild",
          res2.get("ok") is True and res2.get("digest_rebuilt") is True
          and learn.LESSONS_DIGEST.exists(), f"res={res2}")


# ==========================================================================
# R04c: a batch torn mid write is recoverable by re-running the same file
# ==========================================================================
@case("R04c")
def _r04c():
    d = fresh("r04c_")
    install(pos("A-1", -50.0), pos("B-2", -50.0))
    f = write_batch(d, "batch.json", [row("A-1"), row("B-2")])

    real_reviews = learn.REVIEWS_FILE
    learn.REVIEWS_FILE = TornAppend(real_reviews)
    try:
        cap(learn.import_reviews, str(f))
    except OSError:
        pass
    finally:
        learn.REVIEWS_FILE = real_reviews

    landed = [r["id"] for r in jsonl(learn.REVIEWS_FILE)]
    check("R04c: the tear leaves the batch half written",
          landed == ["A-1"], f"landed={landed}")

    n, out = cap(learn.import_reviews, str(f))
    after = [r["id"] for r in jsonl(learn.REVIEWS_FILE)]
    check("R04c: re-running the torn batch lands the missing row",
          sorted(after) == ["A-1", "B-2"], f"after={after} out={out!r}")
    check("R04c: the already committed row is not duplicated",
          after.count("A-1") == 1, f"after={after}")
    check("R04c: both lessons reconcile after the retry",
          len(jsonl(learn.LESSONS_LOG)) == 2,
          f"lessons={jsonl(learn.LESSONS_LOG)}")


# ==========================================================================
# R05: the verdict is canonical, the file's claim is audited not trusted
# ==========================================================================
@case("R05")
def _r05():
    d = fresh("r05_")
    p = pos("LOSS-3", -90.83)
    install(p)
    canonical = recap.position_story(p)[0]

    f = write_batch(d, "lie.json", [row("LOSS-3", verdict="WIN",
                                        final_pnl_pct=300.0)])
    n, out = cap(learn.import_reviews, str(f))
    check("R05: a verdict contradicting the canonical P&L is refused",
          n == 0 and "REFUSED" in out, f"n={n} out={out!r}")
    check("R05: the refusal names the contradiction",
          "verdict" in out.lower(), f"out={out!r}")
    check("R05: nothing was written by the refused import",
          jsonl(learn.REVIEWS_FILE) == [])

    f2 = write_batch(d, "ok.json", [row("LOSS-3")])
    n2, _ = cap(learn.import_reviews, str(f2))
    stored = jsonl(learn.REVIEWS_FILE)[0]
    check("R05: an honest row is accepted", n2 == 1)
    check("R05: the stored verdict is the canonical one, not the file's",
          stored["verdict"] == canonical, f"got {stored['verdict']!r}")
    check("R05: the file's claim is preserved separately for audit",
          stored.get("verdict_claimed") == "WRONG", f"got {stored}")
    check("R05: the canonical P&L still comes from the position",
          stored["final_pnl_pct"] == -90.83, f"got {stored}")


# ==========================================================================
# a malformed batch makes no initial writes
# ==========================================================================
@case("malformed")
def _malformed():
    d = fresh("bad_")
    install(pos("OK-1", -50.0))

    bad = d / "bad.json"
    bad.write_text("{not json at all", encoding="utf-8")
    n1, _ = cap(learn.import_reviews, str(bad))

    notlist = d / "notlist.json"
    notlist.write_text(json.dumps({"reviews": {"id": "OK-1"}}),
                       encoding="utf-8")
    n2, _ = cap(learn.import_reviews, str(notlist))

    mixed = write_batch(d, "mixed.json", [row("OK-1"), row("GHOST")])
    n3, out3 = cap(learn.import_reviews, str(mixed))

    check("malformed: unreadable JSON writes nothing", n1 == 0)
    check("malformed: a non-list payload writes nothing", n2 == 0)
    check("malformed: one bad row refuses the whole batch",
          n3 == 0 and "REFUSED" in out3, f"out={out3!r}")
    check("malformed: no review file was created by any of them",
          jsonl(learn.REVIEWS_FILE) == [],
          f"got {jsonl(learn.REVIEWS_FILE)}")
    check("malformed: no lesson was derived by any of them",
          jsonl(learn.LESSONS_LOG) == [])


# ==========================================================================
# a conflicting revision does not overwrite silently
# ==========================================================================
@case("conflict")
def _conflict():
    d = fresh("conflict_")
    install(pos("CONF-1", -50.0))

    f1 = write_batch(d, "r1.json", [row("CONF-1", lesson="the first reading")])
    n1, _ = cap(learn.import_reviews, str(f1))
    check("conflict: revision 1 commits", n1 == 1)
    first = jsonl(learn.REVIEWS_FILE)[0]

    f2 = write_batch(d, "r1b.json",
                     [row("CONF-1", lesson="a different judgment, same rev")])
    n2, out2 = cap(learn.import_reviews, str(f2))
    check("conflict: a different judgment at the same revision is refused",
          n2 == 0 and "REFUSED" in out2, f"n={n2} out={out2!r}")
    check("conflict: the refusal names both payload hashes",
          first.get("payload_hash", "?") in out2
          and learn._payload_hash(row("CONF-1",
                                      lesson="a different judgment, same rev"))
          in out2, f"out={out2!r}")
    check("conflict: the committed row is unchanged",
          jsonl(learn.REVIEWS_FILE) == [first])

    f3 = write_batch(d, "r0.json",
                     [row("CONF-1", revision=0, lesson="stale")])
    n3, out3 = cap(learn.import_reviews, str(f3))
    check("conflict: a stale revision is refused",
          n3 == 0 and "REFUSED" in out3, f"n={n3} out={out3!r}")

    f4 = write_batch(d, "r2.json",
                     [row("CONF-1", revision=2, lesson="the corrected reading")])
    n4, _ = cap(learn.import_reviews, str(f4))
    check("conflict: a higher revision is accepted as a correction", n4 == 1)


# ==========================================================================
# E2: a changed lesson revision supersedes rather than being dropped
# ==========================================================================
@case("revision")
def _revision():
    d = fresh("rev_")
    install(pos("E2-1", -50.0))

    f1 = write_batch(d, "r1.json",
                     [row("E2-1",
                          lesson="never take SPX calls into the reversal")])
    cap(learn.import_reviews, str(f1))
    dig1 = learn.LESSONS_DIGEST.read_text(encoding="utf-8")
    check("revision: the first lesson reaches the digest",
          "into the reversal" in dig1, f"got {dig1!r}")

    f2 = write_batch(d, "r2.json",
                     [row("E2-1", revision=2,
                          why="CORRECTED, the fill was the cause",
                          lesson="CORRECTED, the fill was the cause")])
    n2, out2 = cap(learn.import_reviews, str(f2))
    check("revision: the corrected revision commits", n2 == 1, f"out={out2!r}")

    rows = jsonl(learn.LESSONS_LOG)
    old = [r for r in rows if "into the reversal" in " ".join(r.get("lessons") or [])]
    new = [r for r in rows if "CORRECTED" in " ".join(r.get("lessons") or [])]
    check("revision: the superseded text stays in the log for audit",
          len(old) == 1 and len(new) == 1, f"rows={rows}")
    check("revision: the superseded row is marked inactive and points forward",
          old and old[0].get("active") is False
          and old[0].get("superseded_by") == "E2-1#r2", f"old={old}")
    dig2 = learn.LESSONS_DIGEST.read_text(encoding="utf-8")
    check("revision: the active digest carries only the correction",
          "CORRECTED" in dig2 and "into the reversal" not in dig2,
          f"got {dig2!r}")

    before = len(jsonl(learn.LESSONS_LOG))
    res, _ = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    check("revision: a second repair run is a no-op",
          len(jsonl(learn.LESSONS_LOG)) == before
          and res.get("derived") == 0, f"res={res}")


# ==========================================================================
# R06: a legacy lesson with no source_review_id migrates once, never doubles
# ==========================================================================
LEGACY = {"session": "2026-09-04", "graded_at": "2026-09-04 22:10:00 EDT",
          "wins": 0, "losses": 0, "trades": [],
          "review": "deep review SPX 2026-09-04: stopped out on the retest",
          "lessons": ["lesson for LEG-1"], "watch_tomorrow": "",
          "proposed_change": None}          # no source_review_id at all

LEGACY_REVIEW = {"id": "LEG-1", "date": "2026-09-04", "ticker": "SPX",
                 "direction": "call", "strike": 7745.0, "paper": False,
                 "final_pnl_pct": -50.0, "verdict": "WRONG",
                 "why": "stopped out on the retest", "cause": "setup",
                 "cause_detail": "", "lesson": "lesson for LEG-1",
                 "reviewed_at": "2026-09-04 22:10:00 EDT",
                 "reviewer": "offline"}


@case("R06")
def _r06():
    d = fresh("r06_")
    install(pos("LEG-1", -50.0))
    learn.LESSONS_LOG.write_text(json.dumps(LEGACY) + "\n", encoding="utf-8")
    learn.REVIEWS_FILE.write_text(json.dumps(LEGACY_REVIEW) + "\n",
                                  encoding="utf-8")

    res, _ = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    rows = jsonl(learn.LESSONS_LOG)
    check("R06: the repair does not append a second copy of a legacy lesson",
          len(rows) == 1, f"rows={rows}")
    check("R06: the repair reports the legacy row it matched",
          res.get("legacy_matched") == 1 and res.get("derived") == 0,
          f"res={res}")

    res2, _ = cap(learn.migrate_legacy_lessons, learn.REVIEWS_FILE)
    rows2 = jsonl(learn.LESSONS_LOG)
    check("R06: the migration stamps the legacy row in place, once",
          len(rows2) == 1 and rows2[0].get("source_review_id") == "LEG-1"
          and rows2[0].get("migrated_from") == "legacy", f"rows={rows2}")
    check("R06: the migration reports what it mapped",
          res2.get("mapped") == 1 and res2.get("ok") is True, f"res={res2}")

    res3, _ = cap(learn.migrate_legacy_lessons, learn.REVIEWS_FILE)
    check("R06: re-running the migration changes nothing",
          jsonl(learn.LESSONS_LOG) == rows2 and res3.get("mapped") == 0,
          f"res={res3}")


@case("R06 unmapped")
def _r06_unmapped():
    d = fresh("r06u_")
    install(pos("LEG-1", -50.0))
    orphan = dict(LEGACY, lessons=["a lesson no review ever produced"],
                  review="deep review SPX 2026-09-04: unrelated")
    learn.LESSONS_LOG.write_text(json.dumps(orphan) + "\n", encoding="utf-8")
    learn.REVIEWS_FILE.write_text(json.dumps(LEGACY_REVIEW) + "\n",
                                  encoding="utf-8")

    res, _ = cap(learn.migrate_legacy_lessons, learn.REVIEWS_FILE)
    rows = jsonl(learn.LESSONS_LOG)
    check("R06: an unmatchable legacy row is marked, never guessed",
          len(rows) == 1
          and rows[0].get("source_review_id") == learn.UNMAPPED,
          f"rows={rows}")
    check("R06: the migration reports the unmapped row",
          res.get("unmapped") == 1 and res.get("mapped") == 0, f"res={res}")
    check("R06: the migration wrote a pre-migration backup",
          any(p.name.startswith("lessons.jsonl.")
              for p in learn.LESSONS_LOG.parent.iterdir()),
          f"files={[p.name for p in learn.LESSONS_LOG.parent.iterdir()]}")


# ==========================================================================
# R06b: the idempotence guard is closed, not fail-open
# ==========================================================================
@case("R06b")
def _r06b():
    d = fresh("r06b_")
    install(pos("LEG-1", -50.0))
    learn.LESSONS_LOG.write_text(json.dumps(LEGACY) + "\n", encoding="utf-8")

    check("R06b: a review with no id does not match a legacy row",
          learn._lesson_already_recorded(None) is False)
    check("R06b: an unseen id is still reported as not recorded",
          learn._lesson_already_recorded("NEVER-SEEN") is False)

    learn.REVIEWS_FILE.write_text(json.dumps(LEGACY_REVIEW) + "\n",
                                  encoding="utf-8")
    real_all = learn._all_lessons
    learn._all_lessons = boom("volume blip")
    try:
        res, _ = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    finally:
        learn._all_lessons = real_all
    check("R06b: an unreadable lessons log records an error, writes nothing",
          res.get("ok") is False and res.get("derived") == 0
          and len(jsonl(learn.LESSONS_LOG)) == 1, f"res={res}")


# ==========================================================================
# E2: every derived lesson carries its provenance
# ==========================================================================
@case("E2 provenance")
def _e2_prov():
    d = fresh("e2p_")
    install(pos("PROV-1", -50.0))
    f = write_batch(d, "in.json", [row("PROV-1")])
    cap(learn.import_reviews, str(f))
    lesson = jsonl(learn.LESSONS_LOG)[0]

    for key in ("trade_ids", "evidence_class", "strategy_version",
                "review_revision", "source_payload_hash", "active"):
        check(f"E2: the derived lesson carries {key}", key in lesson,
              f"keys={sorted(lesson)}")
    check("E2: trade_ids names the real source trade",
          lesson.get("trade_ids") == ["PROV-1"], f"got {lesson.get('trade_ids')}")
    check("E2: an offline review is recorded as a model hypothesis",
          lesson.get("evidence_class") == "model_hypothesis",
          f"got {lesson.get('evidence_class')}")
    check("E2: the digest marks a hypothesis as a hypothesis",
          "hypothesis" in learn.LESSONS_DIGEST.read_text(encoding="utf-8"),
          learn.LESSONS_DIGEST.read_text(encoding="utf-8"))

    v1, v2 = learn.strategy_fingerprint(), learn.strategy_fingerprint()
    check("E2: the strategy fingerprint is stable inside one process",
          v1 == v2 and len(v1) == 16 and v1 == lesson.get("strategy_version"),
          f"{v1} vs {v2} vs {lesson.get('strategy_version')}")


@case("E2 online review")
def _e2_online():
    d = fresh("e2o_")
    p = pos("ONLINE-1", -50.0)
    install(p)
    import assistant

    saved = (assistant.enabled, assistant.cooldown_left_s, assistant.complete)
    api_mode = _bot_test_os.environ.get("API_MODE")
    _bot_test_os.environ["API_MODE"] = "full"
    try:
        assistant.enabled = lambda: True
        assistant.cooldown_left_s = lambda: 0
        assistant.complete = lambda s, u, **k: (
            '{"why": "the retest failed", "cause": "setup", '
            '"cause_detail": "", "lesson": "wait for the retest to hold"}')
        n, _ = cap(learn.review_history)
    finally:
        assistant.enabled, assistant.cooldown_left_s, assistant.complete = saved
        if api_mode is None:
            _bot_test_os.environ.pop("API_MODE", None)
        else:
            _bot_test_os.environ["API_MODE"] = api_mode

    check("E2: the online deep review ran", n == 1, f"got {n}")
    lesson = jsonl(learn.LESSONS_LOG)[0]
    check("E2: the online deep review stamps its source review id",
          lesson.get("source_review_id") == "ONLINE-1", f"got {lesson}")
    check("E2: the online deep review stamps its trade id and class",
          lesson.get("trade_ids") == ["ONLINE-1"]
          and lesson.get("evidence_class") == "model_hypothesis",
          f"got {lesson}")

    res, _ = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    check("E2: a repair after an online review derives no duplicate",
          len(jsonl(learn.LESSONS_LOG)) == 1 and res.get("derived") == 0,
          f"res={res} rows={jsonl(learn.LESSONS_LOG)}")


# ==========================================================================
# the rule-change proposal guardrail still holds on the import path
# ==========================================================================
@case("proposal guardrail")
def _proposals():
    d = fresh("prop_")
    install(pos("RULE-1", -50.0))
    saved_state = config.STATE_FILE
    config.STATE_FILE = d / "state.json"
    try:
        f = write_batch(d, "in.json",
                        [row("RULE-1",
                             lesson="Raise the minimum gap to 1.2 ATR")])
        cap(learn.import_reviews, str(f))
        dig = learn.LESSONS_DIGEST.read_text(encoding="utf-8")
        check("guardrail: an imported rule change never reaches the digest",
              "minimum gap" not in dig, f"got {dig!r}")
        check("guardrail: it lands in the proposal registry instead",
              any("minimum gap" in r.get("key", "")
                  for r in learn.proposals_list()),
              f"got {learn.proposals_list()}")
    finally:
        config.STATE_FILE = saved_state


# ==========================================================================
# the deterministic gates stay independent of the digest
# ==========================================================================
@case("gate independence")
def _gates():
    # learn writes it; assistant and coach read it; self_improve names it in a
    # prompt for the improvement agent. None of those is a trading gate. A new
    # reader anywhere else would put a model hypothesis inside a decision.
    allowed = {"learn.py", "assistant.py", "coach.py", "self_improve.py"}
    needles = ("LESSONS_DIGEST", "lessons_digest", "lessons.jsonl")
    offenders = []
    for path in sorted(REPO.glob("*.py")):
        if path.name in allowed or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if any(n in text for n in needles):
            offenders.append(path.name)
    check("gates: no deterministic module reads the lessons digest",
          offenders == [], f"offenders={offenders}")


# ==========================================================================
# the explicit repair command returns structured success or failure
# ==========================================================================
@case("repair exit code")
def _exit_code():
    def run_repair(data_dir):
        env = dict(_bot_test_os.environ)
        env["DATA_DIR"] = str(data_dir)
        env["BOT_TEST_MODE"] = "1"
        return subprocess.run([sys.executable, "learn.py", "--repair-lessons"],
                              cwd=str(REPO), env=env, capture_output=True,
                              text=True, errors="replace", timeout=180)

    good = Path(tempfile.mkdtemp(prefix="repair_ok_", dir=_TMP))
    r = run_repair(good)
    line = [ln for ln in (r.stdout or "").splitlines() if ln.strip().startswith("{")]
    parsed = None
    if line:
        try:
            parsed = json.loads(line[-1])
        except json.JSONDecodeError:
            parsed = None
    check("exit: a clean repair prints one structured JSON line",
          parsed is not None, f"stdout={r.stdout!r} stderr={r.stderr[-300:]!r}")
    check("exit: a clean repair exits 0",
          r.returncode == 0, f"rc={r.returncode} stderr={r.stderr[-300:]!r}")
    check("exit: the structured line reports the rebuild",
          isinstance(parsed, dict) and parsed.get("digest_rebuilt") is True
          and parsed.get("ok") is True, f"parsed={parsed}")

    bad = Path(tempfile.mkdtemp(prefix="repair_bad_", dir=_TMP))
    (bad / "lessons_digest.md").mkdir()      # the rebuild cannot write here
    r2 = run_repair(bad)
    check("exit: a repair that cannot rebuild the digest exits 1",
          r2.returncode == 1, f"rc={r2.returncode} stdout={r2.stdout!r}")
    check("exit: the failure is reported in the structured line",
          "digest_rebuilt" in (r2.stdout or "")
          and "false" in (r2.stdout or "").lower(), f"stdout={r2.stdout!r}")


print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all good")
