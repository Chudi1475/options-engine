"""W04 (import half): review import and lesson recovery under the named faults.

Astra's work package W04 names the faults this file has to survive:
"Interrupted import and digest; canonical loss with false WIN; conflicting
revisions; legacy mapping collision", and the acceptance is "Idempotent
import; factual outcome preserved". test_review_import.py already covers the
P05 round (R04, R04b, R04c, R05, R06, E2). Everything below is a fault that
round did NOT cover, and every one of them reproduced a real defect in
learn.py before its fix.

W04-A  LEGACY MAPPING COLLISION, repair path. A legacy lessons row could
       satisfy TWO reviews at once: both were counted as legacy_matched and
       skipped, so the second review's lesson was never derived, never
       written and never reached the digest. Two trades on one day with the
       same ticker and the same 'why' is all it takes, and canned lesson
       text collides the same way through the by_text index.

W04-B  LEGACY MAPPING COLLISION, migration path. When two reviews shared a
       mapping key, setdefault silently attributed the legacy row to
       whichever review was read first. The docstring promises a row is
       "never attached to a trade on a guess"; that is exactly what it was.

W04-C  CONFLICTING REVISIONS. _supersede_lessons returned 0 both for
       "nothing to supersede" and for "the rewrite failed", so a correction
       whose supersede write failed still appended its new lesson, left the
       superseded text ACTIVE in the digest beside it, and reported ok.

W04-D  INTERRUPTED IMPORT AND DIGEST. _append_lesson swallowed a lock or
       disk failure and returned nothing, so repair_lessons counted a lesson
       it had not written, marked it recorded for the rest of the pass, and
       the import line reported derived lessons that were never on file.

W04-E  CANONICAL LOSS WITH FALSE WIN. The factual outcome must survive a
       fabricated verdict, and an honest review must not be refused for one:
       _verdict_sign matched "win" and "right" as SUBSTRINGS, so an honest
       loss review containing the word "swing" or "unwind" read as a claimed
       WIN and refused the whole batch as a contradiction.

No network, no Telegram, no model API, no production storage, no git writes.

Run:  python test_import_recovery.py     (exit code 0 = all good)
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# an isolated data dir BEFORE config is imported, so nothing here can read or
# write the real runtime state
_TMP = tempfile.mkdtemp(prefix="kelbot_w04_import_")
_bot_test_os.environ["DATA_DIR"] = _TMP

import learn                 # noqa: E402
import positions as poslib   # noqa: E402
import storage_io            # noqa: E402

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


def committed(*rows):
    """Write rows straight into trade_reviews.jsonl, as a finished import
    would have left them."""
    learn.REVIEWS_FILE.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def review_row(pid, **kw):
    base = dict(id=pid, date="2026-09-04", ticker="SPX", direction="call",
                strike=7745.0, paper=False, final_pnl_pct=-50.0,
                verdict="WRONG", why="stopped out on the retest",
                cause="setup", cause_detail="", lesson=f"lesson for {pid}",
                revision=1)
    base.update(kw)
    return base


def batch(d, name, rows):
    f = d / name
    f.write_text(json.dumps({"reviews": rows}), encoding="utf-8")
    return f


def jsonl(p):
    if not Path(p).exists():
        return []
    return [json.loads(ln) for ln in
            Path(p).read_text(encoding="utf-8").splitlines() if ln.strip()]


def lesson_texts(active_only=False):
    out = []
    for e in jsonl(learn.LESSONS_LOG):
        if active_only and e.get("active") is False:
            continue
        out.extend(str(x) for x in (e.get("lessons") or []))
    return out


def digest():
    p = Path(learn.LESSONS_DIGEST)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def cap(fn, *a, **k):
    """(return value, captured stdout) for one call."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rv = fn(*a, **k)
    return rv, buf.getvalue()


class FailingWriteLines:
    """storage_io.write_lines refusing, the way a disk-full volume or the
    WinError 5 this machine hits about one run in ten does. Only write_lines
    is replaced, so the lesson APPEND still lands and the failure is isolated
    to the supersede rewrite."""

    def __init__(self, status="disk_full", error="no space"):
        self.status, self.error, self.calls = status, error, 0
        self._real = storage_io.write_lines

    def __enter__(self):
        def _fail(path, lines, **kw):
            self.calls += 1
            return storage_io.WriteResult(False, self.status, 1, 0,
                                          self.error, path)
        storage_io.write_lines = _fail
        return self

    def __exit__(self, *a):
        storage_io.write_lines = self._real
        return False


class UnheldLock:
    """storage_io.file_lock handing back a lock nobody holds for ONE named
    file, the way a file already held by another writer does. Named, because a
    blanket refusal would also stop the review rows from committing and that
    is a different fault: this one models the import that got its reviews down
    and then could not write their lessons."""

    class _Lock:
        held = False
        why = "held by another writer"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def __init__(self, name="lessons.jsonl"):
        self.name = name

    def __enter__(self):
        self._real = storage_io.file_lock

        def _maybe(path, *a, **k):
            if Path(str(path)).name == self.name:
                return self._Lock()
            return self._real(path, *a, **k)

        storage_io.file_lock = _maybe
        return self

    def __exit__(self, *a):
        storage_io.file_lock = self._real
        return False


# ==========================================================================
print("--- W04-A. legacy mapping collision: one legacy row, two reviews ---")
# ==========================================================================

@case("W04-A")
def _a():
    d = fresh("w04a_")
    install(pos("P1", -50.0), pos("P2", -40.0))
    # two committed reviews whose derived review line is IDENTICAL: same
    # ticker, same session, same 'why'. Two SPX trades stopped out on one
    # morning is an ordinary day, not a contrivance.
    r1 = review_row("P1", lesson="lesson one")
    r2 = review_row("P2", lesson="lesson two")
    committed(r1, r2)
    # ONE legacy lessons row from before the ids existed, matching that line
    learn.LESSONS_LOG.write_text(json.dumps({
        "session": "2026-09-04", "graded_at": "",
        "review": learn._derived_review_line(r1),
        "lessons": ["lesson one"], "watch_tomorrow": ""}) + "\n",
        encoding="utf-8")

    res, _ = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    check("W04-A one legacy row is claimed by exactly one review",
          res["legacy_matched"] == 1, f"legacy_matched={res['legacy_matched']}")
    check("W04-A the second review still derives its own lesson",
          res["derived"] == 1, f"derived={res['derived']}")
    check("W04-A the collided lesson is on file, not lost",
          "lesson two" in lesson_texts(), str(lesson_texts()))
    check("W04-A and it reaches the active digest",
          "lesson two" in digest())
    check("W04-A the run reports itself complete", res["ok"] is True,
          str(res["errors"]))

    # ...and doing it again writes nothing new: the second review is now
    # indexed by its own id, the first still matches the legacy row.
    before = jsonl(learn.LESSONS_LOG)
    res2, _ = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    check("W04-A a second repair run derives nothing new",
          res2["derived"] == 0 and jsonl(learn.LESSONS_LOG) == before,
          f"derived={res2['derived']}")

    # the claim is keyed by REVIEW ID, not by row position: the same review
    # read twice must not be handed a fresh lesson just because it already
    # took its own legacy row. Overcorrecting the collision fix that way would
    # duplicate every legacy-matched lesson.
    committed(r1, r1, r2)
    res3, _ = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    check("W04-A one review read twice still claims only its own legacy row",
          res3["derived"] == 0 and jsonl(learn.LESSONS_LOG) == before,
          f"derived={res3['derived']}")


@case("W04-A2")
def _a2():
    """The same collision through the by_text index: different 'why', same
    canned lesson text on the same session."""
    d = fresh("w04a2_")
    install(pos("P3", -50.0), pos("P4", -30.0))
    r1 = review_row("P3", why="first read", lesson="size down after two losses")
    r2 = review_row("P4", why="second read", lesson="size down after two losses")
    committed(r1, r2)
    learn.LESSONS_LOG.write_text(json.dumps({
        "session": "2026-09-04", "graded_at": "", "review": "old wording",
        "lessons": ["size down after two losses"], "watch_tomorrow": ""}) + "\n",
        encoding="utf-8")

    res, _ = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    check("W04-A2 a text collision claims the legacy row once",
          res["legacy_matched"] == 1, f"legacy_matched={res['legacy_matched']}")
    check("W04-A2 the second review is still derived",
          res["derived"] == 1, f"derived={res['derived']}")
    rows = [e for e in jsonl(learn.LESSONS_LOG)
            if e.get("source_review_id") == "P4"]
    check("W04-A2 the derived row names the review it came from",
          len(rows) == 1, str(len(rows)))


# ==========================================================================
print()
print("--- W04-B. legacy mapping collision: the migration may not guess ---")
# ==========================================================================

@case("W04-B")
def _b():
    d = fresh("w04b_")
    install(pos("P5", -50.0), pos("P6", -20.0))
    r1 = review_row("P5", lesson="lesson five")
    r2 = review_row("P6", lesson="lesson six")
    committed(r1, r2)
    # one legacy row whose review line matches BOTH reviews equally well
    learn.LESSONS_LOG.write_text(json.dumps({
        "session": "2026-09-04", "graded_at": "",
        "review": learn._derived_review_line(r1),
        "lessons": ["something older"], "watch_tomorrow": ""}) + "\n",
        encoding="utf-8")

    res, out = cap(learn.migrate_legacy_lessons, learn.REVIEWS_FILE)
    rows = jsonl(learn.LESSONS_LOG)
    attributed = rows[0].get("source_review_id")
    check("W04-B an ambiguous legacy row is never attached to a trade",
          attributed == learn.UNMAPPED, f"attributed to {attributed!r}")
    check("W04-B the migration maps nothing on a guess",
          res["mapped"] == 0, f"mapped={res['mapped']}")
    check("W04-B the ambiguity is reported, not silent",
          res.get("ambiguous") == 1, str(res))
    check("W04-B the row is still counted as looked at",
          res["unmapped"] == 1, str(res))
    check("W04-B a backup was written before the rewrite",
          bool(res["backup"]))

    # an UNAMBIGUOUS row still maps, so the guard did not just disable the
    # migration
    d = fresh("w04b2_")
    install(pos("P7", -50.0))
    r3 = review_row("P7", lesson="lesson seven")
    committed(r3)
    learn.LESSONS_LOG.write_text(json.dumps({
        "session": "2026-09-04", "graded_at": "",
        "review": learn._derived_review_line(r3),
        "lessons": ["lesson seven"], "watch_tomorrow": ""}) + "\n",
        encoding="utf-8")
    res2, _ = cap(learn.migrate_legacy_lessons, learn.REVIEWS_FILE)
    check("W04-B an unambiguous legacy row still maps",
          res2["mapped"] == 1 and res2.get("ambiguous", 0) == 0, str(res2))
    check("W04-B the mapped row carries the review id",
          jsonl(learn.LESSONS_LOG)[0].get("source_review_id") == "P7")


# ==========================================================================
print()
print("--- W04-C. conflicting revisions: a supersede that did not land ---")
# ==========================================================================

@case("W04-C")
def _c():
    d = fresh("w04c_")
    install(pos("P8", -50.0))
    committed(review_row("P8", revision=1, lesson="original lesson"))
    res1, _ = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    check("W04-C revision 1 derives its lesson", res1["derived"] == 1)

    # revision 2 corrects it, and the supersede rewrite fails
    committed(review_row("P8", revision=2, why="second read",
                         lesson="corrected lesson"))
    with FailingWriteLines() as fw:
        res2, out = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    check("W04-C the failed supersede was actually exercised", fw.calls >= 1)
    check("W04-C a supersede that did not land is not reported as ok",
          res2["ok"] is False, str(res2))
    check("W04-C it says which revision could not supersede",
          any("P8" in str(e) for e in res2["errors"]), str(res2["errors"]))
    check("W04-C nothing is counted as superseded",
          res2["superseded"] == 0, str(res2["superseded"]))
    active = lesson_texts(active_only=True)
    check("W04-C the digest never carries two active revisions of one review",
          not ("original lesson" in active and "corrected lesson" in active),
          str(active))
    check("W04-C the correction is not silently counted as derived",
          res2["derived"] == 0, str(res2["derived"]))

    # and the retry, with the disk back, finishes the work
    res3, _ = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    check("W04-C the retry supersedes and derives", res3["ok"] is True
          and res3["superseded"] == 1 and res3["derived"] == 1, str(res3))
    active = lesson_texts(active_only=True)
    check("W04-C after the retry only the correction is active",
          active == ["corrected lesson"], str(active))
    check("W04-C the superseded text stays on file for audit",
          "original lesson" in lesson_texts())


# ==========================================================================
print()
print("--- W04-D. interrupted import and digest: a lesson that never landed ---")
# ==========================================================================

@case("W04-D")
def _d():
    d = fresh("w04d_")
    install(pos("P10", -50.0))
    committed(review_row("P10", lesson="a lesson the disk refused"))

    with UnheldLock():
        res, out = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    check("W04-D a lesson that was not written is not counted as derived",
          res["derived"] == 0, f"derived={res['derived']}")
    check("W04-D the failure is reported, not swallowed",
          res["ok"] is False and res["errors"], str(res))
    check("W04-D nothing was actually written",
          lesson_texts() == [], str(lesson_texts()))

    # the retry, with the file free again, still derives it: the pass must not
    # have marked it recorded on the strength of a write that failed
    res2, _ = cap(learn.repair_lessons, learn.REVIEWS_FILE)
    check("W04-D the retry recovers the lesson", res2["derived"] == 1,
          f"derived={res2['derived']}")
    check("W04-D and it is on file now",
          "a lesson the disk refused" in lesson_texts(), str(lesson_texts()))


@case("W04-D2")
def _d2():
    """The whole import, interrupted the same way: the printed line and the
    structured result must both refuse to claim work that did not land."""
    d = fresh("w04d2_")
    install(pos("P11", -50.0))
    f = batch(d, "reviews.json", [review_row("P11", lesson="import lesson")])

    with UnheldLock():
        res, out = cap(learn.import_reviews_result, str(f))
    check("W04-D2 the import does not report itself ok",
          res["ok"] is False, str(res))
    check("W04-D2 the printed line does not claim lessons it never wrote",
          "1 lesson(s) derived" not in out, out.strip()[-160:])

    # re-running the same file finishes it: idempotent import, the acceptance
    res2, out2 = cap(learn.import_reviews_result, str(f))
    check("W04-D2 the retry completes the import", res2["ok"] is True, str(res2))
    check("W04-D2 the review row is not duplicated",
          len(jsonl(learn.REVIEWS_FILE)) == 1,
          str(len(jsonl(learn.REVIEWS_FILE))))
    check("W04-D2 the retry says the row was resumed, not imported again",
          res2["written"] == 0 and res2["resumed"] == 1, str(res2))
    check("W04-D2 the lesson is on file after the retry",
          "import lesson" in lesson_texts(), str(lesson_texts()))


# ==========================================================================
print()
print("--- W04-E. canonical loss with a false WIN ---")
# ==========================================================================

@case("W04-E")
def _e():
    d = fresh("w04e_")
    install(pos("P12", -50.0))
    # an honest LOSS verdict whose wording happens to contain the letters
    # w-i-n. An offline reviewer types this field by hand, so it carries
    # prose, not a keyword.
    f = batch(d, "honest.json", [review_row(
        "P12", verdict="wrong, the swing never came and it unwound",
        why="stopped out on the retest", lesson="wait for the retest")])
    res, out = cap(learn.import_reviews_result, str(f))
    check("W04-E an honest loss review is not refused for a substring",
          res["written"] == 1, f"{res['problems']}")
    row = jsonl(learn.REVIEWS_FILE)[0]
    check("W04-E the stored verdict is the canonical one",
          row["verdict"].startswith("WRONG"), row["verdict"])
    check("W04-E the canonical P&L comes from the tracked position",
          row["final_pnl_pct"] == -50.0, str(row["final_pnl_pct"]))


@case("W04-E2")
def _e2():
    """A verdict that really does claim a win on a canonical loss still gets
    refused, and a claim the matcher cannot read never overwrites the fact."""
    d = fresh("w04e2_")
    install(pos("P13", -50.0))
    f = batch(d, "false_win.json", [review_row("P13", verdict="RIGHT")])
    res, out = cap(learn.import_reviews_result, str(f))
    check("W04-E2 a fabricated WIN on a canonical loss is refused",
          res["written"] == 0 and res["problems"], str(res))
    check("W04-E2 nothing was committed by the refused batch",
          jsonl(learn.REVIEWS_FILE) == [])

    # a claim phrased so the matcher cannot grade it must still never become
    # the fact of the trade
    f2 = batch(d, "vague.json", [review_row("P13", verdict="banked it early")])
    res2, _ = cap(learn.import_reviews_result, str(f2))
    row = jsonl(learn.REVIEWS_FILE)[0]
    check("W04-E2 an ungradable claim does not overwrite the outcome",
          res2["written"] == 1 and row["verdict"].startswith("WRONG"),
          f"{res2['problems']} {row.get('verdict')!r}")
    check("W04-E2 the file's own claim is kept beside it for audit",
          row["verdict_claimed"] == "banked it early", str(row))
    check("W04-E2 the derived lesson carries no fabricated outcome",
          "banked it early" not in digest(), digest()[-200:])


# ==========================================================================
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All W04 import recovery checks passed.")
