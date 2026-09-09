"""W07: optional real fills. The manual fill journal, and the seven ways a
human reply can go wrong.

Astra's W07 row names seven faults and three acceptance lines.

  Faults      duplicate reply, ambiguous candidate, partial close, over close,
              late correction, no reply, and two users on one signal.
  Acceptance  integer quantities and costs reconcile; no response remains
              unknown; modeled returns never become claimed broker fills.

The defect this package exists to close is the honesty one. The bot computes a
modeled return from a Black Scholes mark and calls it a result. A person's
broker fill is a different kind of fact, and the moment those two are added
together the number that comes out is neither. evidence_class keeps them apart
at every read, and W07l is the case that proves nothing in the module will
merge them even when asked.

The second defect is a counting one. Three recipients get one card. If each
replies, that is three EXECUTION EXPERIENCES of one signal, not three signals,
and a denominator that says three is the error the preregistered study is built
to avoid (astra/PREREGISTRATION.md section 4). W07j is that case.

This lane is OPTIONAL. A user who never replies costs nothing, their rows stay
unknown rather than becoming "no trade", and nothing here ever asks them to
trade or reminds them to report. W07i and W07n hold that line.

astra/RECORDER_SCHEMA.md record 5 is the contract. W07b reads that file and
fails on any field the schema names and the implementation drops, which is the
only way a schema written before the code stays binding after it.

Nothing here touches strategy. No threshold, no roster, no window, no allow
list. Every case is about what a reply means and what gets written down.

No network, no Telegram, no yfinance, no production storage.

Run:  python test_fill_journal.py     (exit code 0 = all good)
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.

import ast
import json
import pathlib
import re
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# an isolated data dir BEFORE config is imported, so nothing here can read or
# write the real runtime state
_TMP = tempfile.mkdtemp(prefix="kelbot_fills_")
_bot_test_os.environ["DATA_DIR"] = _TMP

import config            # noqa: E402
import storage_io        # noqa: E402

# fill_journal is the module this package introduces. Imported defensively ON
# PURPOSE: before the change it does not exist, and every case below then fails
# on its own unmet requirement instead of the whole file collapsing into one
# import error that says nothing about which requirement is missing.
try:
    import fill_journal as fj  # noqa: E402
except ImportError:
    fj = None

REPO = pathlib.Path(__file__).parent
TMP = pathlib.Path(_TMP)
SCHEMA_MD = REPO / "astra" / "RECORDER_SCHEMA.md"

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def note(text):
    print(f"       {text}")


def need(attr):
    """The journal's interface, or None. A case that needs an interface it
    cannot get FAILS. It never quietly skips, because a skipped case reads as
    coverage in the summary line and is not."""
    if fj is None:
        return None
    return getattr(fj, attr, None)


def clean():
    """A journal with no memory of the previous case."""
    if fj is None:
        return False
    reset = getattr(fj, "_reset_for_test", None)
    if reset is None:
        return False
    reset()
    return True


# The two recipients used throughout. Opaque refs, exactly as event_journal
# hands them over: no chat id ever reaches this module or its files.
U1 = "aa11bb22cc33"
U2 = "dd44ee55ff66"

ENTRY_TEXT = ("📈 BUY CALL: SPY 640, expires today\n"
              "Entry about $1.35\nYour call.")
STOP_TEXT = "🛑 STOP: SELL EVERYTHING\nSPY 640 call is down -62%.\nYour call."


def entry_card(pos="posA", mids=None, symbol="SPY"):
    """One alerted entry, in the shape scanner hands to the journal."""
    return {
        "position_id": pos, "candidate_id": "cand_" + pos,
        "contract_id": "SPY260909C00640000", "symbol": symbol,
        "kind": "entry", "text": ENTRY_TEXT, "state": "open",
        "message_ids": mids if mids is not None else {U1: [7001], U2: [8001]},
    }


def exit_card(pos="posA", mids=None):
    return {
        "position_id": pos, "candidate_id": "cand_" + pos,
        "contract_id": "SPY260909C00640000", "symbol": "SPY",
        "kind": "exit", "text": STOP_TEXT, "state": "open",
        "message_ids": mids if mids is not None else {U1: [7002], U2: [8002]},
    }


def say(user, text, *, candidates, mid=None, reply_to=None, reply_text="",
        now="2026-09-09T15:05:00+00:00"):
    """One inbound Telegram message, handed to the journal the way scanner
    hands it over. Returns the journal's disposition dict."""
    return fj.handle_message(
        user_ref=user, text=text, candidates=candidates, message_id=mid,
        reply_message_id=reply_to, reply_text=reply_text, now_utc=now)


# ===========================================================================
print("--- W07a. the module exists, writes through the shared protocol, and "
      "never reaches the wire itself ---")

check("W07a fill_journal imports", fj is not None,
      "no fill_journal.py in the repo")

for fn in ("parse_report", "looks_like_report", "resolve_target",
           "handle_message", "record_fill", "record_response", "note_signal",
           "fills", "open_quantity", "reconcile", "coverage",
           "unknown_signals", "performance_view", "assert_not_mixed",
           "read_records", "path_for", "report_text"):
    check(f"W07a the journal exposes {fn}", need(fn) is not None)

if fj is not None:
    src = (REPO / "fill_journal.py").read_text(encoding="utf-8")
    # Constraint 9: storage_io is the write protocol. A sixth way to write a
    # file is exactly what W05 removed.
    hand_rolled = re.findall(r"\.write_text\(|\.write_bytes\(|open\([^)]*[\"']w",
                             src)
    check("W07a the journal never hand rolls a file write", not hand_rolled,
          str(hand_rolled[:4]))
    check("W07a the journal writes through storage_io",
          "storage_io." in src)
    # It is a RECORDER, not a sender. A module that can text somebody is a
    # module that can nag somebody, and this lane may never nag.
    tree = ast.parse(src)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    check("W07a the journal never imports telegram", "telegram" not in imports,
          str(sorted(imports)))
else:
    for n in ("never hand rolls a file write", "writes through storage_io",
              "never imports telegram"):
        check(f"W07a the journal {n}", False, "no fill_journal.py")


# ===========================================================================
print("\n--- W07b. record 5 carries every field astra/RECORDER_SCHEMA.md "
      "names, and no field is quietly dropped ---")


def schema_fields(section: str):
    text = SCHEMA_MD.read_text(encoding="utf-8")
    m = re.search(rf"^## {section}\..*?```\n(.*?)```", text, re.S | re.M)
    if not m:
        return []
    return [f.strip() for f in re.split(r"[,\s]+", m.group(1)) if f.strip()]


check("W07b the schema document is present", SCHEMA_MD.exists(), str(SCHEMA_MD))

WANT5 = schema_fields("5")
check("W07b the schema names record 5's fields", len(WANT5) >= 15,
      f"parsed {WANT5}")

if fj is not None and WANT5:
    clean()
    cands = [entry_card()]
    r = say(U1, "2 @ 1.35", candidates=cands, mid=9001, reply_to=7001,
            reply_text=ENTRY_TEXT)
    rows = fj.read_records("fills")
    row = rows[0] if rows else {}
    missing = [f for f in WANT5 if f not in row]
    check("W07b a written fill carries every field record 5 names",
          bool(rows) and not missing, f"missing {missing}")
    check("W07b fill rows carry schema_version",
          bool(rows) and all("schema_version" in x for x in rows))
    check("W07b the fill is stored under the shared data dir, not the repo",
          bool(rows) and str(TMP) in str(fj.path_for("fills")),
          str(fj.path_for("fills")))
else:
    for n in ("a written fill carries every field record 5 names",
              "fill rows carry schema_version",
              "the fill is stored under the shared data dir, not the repo"):
        check(f"W07b {n}", False, "no fill_journal.py")


# ===========================================================================
print("\n--- W07c. INTEGER QUANTITIES: one contract cannot sell half ---")

if fj is not None:
    p = fj.parse_report("2 @ 1.35")
    check("W07c a plain quantity and price parses",
          p.get("ok") is True and p.get("quantity") == 2
          and abs((p.get("price") or 0) - 1.35) < 1e-9, str(p))
    check("W07c the parsed quantity is a python int, not a float",
          isinstance(p.get("quantity"), int)
          and not isinstance(p.get("quantity"), bool), str(type(p.get("quantity"))))

    for text in ("bought 2 @ 1.35", "2 contracts @ $1.35", "filled 2 at 1.35",
                 "2x1.35", "/fill 2 @ 1.35"):
        q = fj.parse_report(text)
        check(f"W07c the grammar accepts {text!r}",
              q.get("ok") is True and q.get("quantity") == 2, str(q))

    half = fj.parse_report("2.5 @ 1.35")
    check("W07c a fractional quantity is refused, not rounded",
          half.get("ok") is False and half.get("reason") == "fractional_quantity",
          str(half))
    check("W07c the refusal does not invent a quantity",
          half.get("quantity") is None, str(half.get("quantity")))

    zero = fj.parse_report("0 @ 1.35")
    check("W07c a zero quantity is refused", zero.get("ok") is False, str(zero))

    neg = fj.parse_report("sold -1 @ 2.05")
    check("W07c a negative quantity is refused", neg.get("ok") is False, str(neg))

    # a fractional report is a RESPONSE, so it must not vanish
    clean()
    cands = [entry_card()]
    d = say(U1, "2.5 @ 1.35", candidates=cands, mid=9101, reply_to=7001,
            reply_text=ENTRY_TEXT)
    check("W07c a fractional reply is answered, not swallowed",
          d.get("status") == "rejected" and bool(d.get("reply")), str(d))
    check("W07c a fractional reply writes no fill row",
          len(fj.read_records("fills")) == 0)
    check("W07c the refusal says why in words the owner can act on",
          "contract" in (d.get("reply") or "").lower(), d.get("reply"))
else:
    for n in ("a plain quantity and price parses",
              "the parsed quantity is a python int, not a float",
              "a fractional quantity is refused, not rounded",
              "the refusal does not invent a quantity",
              "a zero quantity is refused", "a negative quantity is refused",
              "a fractional reply is answered, not swallowed",
              "a fractional reply writes no fill row",
              "the refusal says why in words the owner can act on"):
        check(f"W07c {n}", False, "no fill_journal.py")


# ===========================================================================
print("\n--- W07d. AMBIGUOUS CANDIDATE: a reply that could mean two trades is "
      "asked about, never guessed ---")

if fj is not None:
    clean()
    two = [entry_card("posA", {U1: [7001]}),
           entry_card("posB", {U1: [7009]}, symbol="QCOM")]
    two[1]["contract_id"] = "QCOM260909C00170000"

    linked = say(U1, "2 @ 1.35", candidates=two, mid=9201, reply_to=7009,
                 reply_text=ENTRY_TEXT)
    check("W07d a reply linked to a specific alert resolves to that alert",
          linked.get("status") == "accepted"
          and (linked.get("fill") or {}).get("position_id") == "posB",
          str(linked))
    check("W07d the resolution basis is recorded, not assumed",
          (linked.get("fill") or {}).get("resolution_basis") == "reply_link",
          str((linked.get("fill") or {}).get("resolution_basis")))

    clean()
    loose = say(U1, "2 @ 1.35", candidates=two, mid=9202)
    check("W07d an unlinked reply with two live candidates is AMBIGUOUS",
          loose.get("status") == "ambiguous", str(loose))
    check("W07d the ambiguous reply writes no fill", not fj.read_records("fills"))
    check("W07d the question names both candidates so the owner can pick",
          "SPY" in (loose.get("reply") or "") and "QCOM" in (loose.get("reply") or ""),
          loose.get("reply"))

    clean()
    named = say(U1, "qcom 2 @ 1.35", candidates=two, mid=9203)
    check("W07d naming the ticker resolves what the reply link could not",
          named.get("status") == "accepted"
          and (named.get("fill") or {}).get("position_id") == "posB", str(named))
    check("W07d a weaker resolution is labelled as the weaker one it was",
          (named.get("fill") or {}).get("resolution_basis") == "explicit_symbol",
          str((named.get("fill") or {}).get("resolution_basis")))

    clean()
    sole = say(U1, "2 @ 1.35", candidates=[entry_card("posA", {U1: [7001]})],
               mid=9204)
    check("W07d with exactly one live candidate there is nothing to be "
          "ambiguous about", sole.get("status") == "accepted", str(sole))
    check("W07d and the ack says which trade it was logged against, so a wrong "
          "guess is visible", "SPY" in (sole.get("reply") or ""),
          sole.get("reply"))

    clean()
    none_open = say(U1, "2 @ 1.35", candidates=[], mid=9205)
    check("W07d a report with no candidate at all is answered, not dropped",
          none_open.get("status") in ("ambiguous", "rejected")
          and bool(none_open.get("reply")), str(none_open))

    # a reply linked to ANOTHER recipient's copy of the card is not this
    # user's evidence
    clean()
    other = say(U2, "2 @ 1.35", candidates=[entry_card("posA", {U1: [7001]})],
                mid=9206, reply_to=7001, reply_text=ENTRY_TEXT)
    check("W07d a message id belonging to another recipient does not resolve "
          "by link", (other.get("fill") or {}).get("resolution_basis")
          != "reply_link", str(other))
    check("W07d but their own copy of the card body still resolves it",
          (other.get("fill") or {}).get("resolution_basis") == "reply_text",
          str((other.get("fill") or {}).get("resolution_basis")))

    # a short fragment identifies nothing. Only ONE of these two cards happens
    # to contain "Your call." here, so without a length floor the fragment
    # would resolve a trade on a coincidence.
    clean()
    frag_cands = [entry_card("posA", {U1: [7001]}),
                  entry_card("posB", {U1: [7009]}, symbol="QCOM")]
    frag_cands[0]["text"] = "📈 BUY CALL: SPY 640, expires today"
    frag_cands[1]["text"] = "📉 BUY PUT: QCOM 170, expires Friday\nYour call."
    frag = say(U1, "2 @ 1.35", candidates=frag_cands, mid=9207,
               reply_to=None, reply_text="Your call.")
    check("W07d a fragment shorter than a card never picks a trade",
          frag.get("status") == "ambiguous", str(frag))
else:
    for n in ("a reply linked to a specific alert resolves to that alert",
              "the resolution basis is recorded, not assumed",
              "an unlinked reply with two live candidates is AMBIGUOUS",
              "the ambiguous reply writes no fill",
              "the question names both candidates so the owner can pick",
              "naming the ticker resolves what the reply link could not",
              "a weaker resolution is labelled as the weaker one it was",
              "with exactly one live candidate there is nothing to be "
              "ambiguous about",
              "and the ack says which trade it was logged against, so a wrong "
              "guess is visible",
              "a report with no candidate at all is answered, not dropped",
              "a message id belonging to another recipient does not resolve "
              "by link"):
        check(f"W07d {n}", False, "no fill_journal.py")


# ===========================================================================
print("\n--- W07e. DUPLICATE REPLY: one fill, whichever way the same message "
      "arrives twice ---")

if fj is not None:
    clean()
    cands = [entry_card()]
    a = say(U1, "2 @ 1.35", candidates=cands, mid=9301, reply_to=7001,
            reply_text=ENTRY_TEXT)
    b = say(U1, "2 @ 1.35", candidates=cands, mid=9301, reply_to=7001,
            reply_text=ENTRY_TEXT)
    check("W07e the same telegram message replayed is recognised as a duplicate",
          a.get("status") == "accepted" and b.get("status") == "duplicate",
          f"{a.get('status')} then {b.get('status')}")
    check("W07e a replayed message writes exactly one fill",
          len(fj.read_records("fills")) == 1,
          str(len(fj.read_records("fills"))))
    check("W07e the duplicate still gets an answer",
          bool(b.get("reply")), str(b))

    # the human double tap: the same report typed twice, seconds apart
    clean()
    say(U1, "2 @ 1.35", candidates=cands, mid=9302, reply_to=7001,
        reply_text=ENTRY_TEXT, now="2026-09-09T15:05:00+00:00")
    c = say(U1, "2 @ 1.35", candidates=cands, mid=9303, reply_to=7001,
            reply_text=ENTRY_TEXT, now="2026-09-09T15:05:20+00:00")
    check("W07e the same report typed twice within the window is a duplicate",
          c.get("status") == "duplicate", str(c))
    check("W07e the double tap writes exactly one fill",
          len(fj.read_records("fills")) == 1)
    check("W07e the open quantity did not double",
          fj.open_quantity("posA", U1) == 2, str(fj.open_quantity("posA", U1)))

    # ...and a genuine second purchase is NOT swallowed as a duplicate
    d = say(U1, "add 2 @ 1.35", candidates=cands, mid=9304, reply_to=7001,
            reply_text=ENTRY_TEXT, now="2026-09-09T15:05:40+00:00")
    check("W07e an explicit second buy is not swallowed by the duplicate rule",
          d.get("status") == "accepted", str(d))
    check("W07e and it adds to the open quantity",
          fj.open_quantity("posA", U1) == 4, str(fj.open_quantity("posA", U1)))
else:
    for n in ("the same telegram message replayed is recognised as a duplicate",
              "a replayed message writes exactly one fill",
              "the duplicate still gets an answer",
              "the same report typed twice within the window is a duplicate",
              "the double tap writes exactly one fill",
              "the open quantity did not double",
              "an explicit second buy is not swallowed by the duplicate rule",
              "and it adds to the open quantity"):
        check(f"W07e {n}", False, "no fill_journal.py")


# ===========================================================================
print("\n--- W07f. PARTIAL CLOSE: half the contracts out is not the trade out ---")

if fj is not None:
    clean()
    cands = [entry_card(), exit_card()]
    say(U1, "2 @ 1.35", candidates=cands, mid=9401, reply_to=7001,
        reply_text=ENTRY_TEXT)
    part = say(U1, "1 @ 2.05", candidates=cands, mid=9402, reply_to=7002,
               reply_text=STOP_TEXT)
    check("W07f replying to an EXIT card is read as a close, not a second buy",
          part.get("status") == "accepted"
          and (part.get("fill") or {}).get("buy_or_sell") == "sell", str(part))
    check("W07f one of two contracts out leaves one open",
          fj.open_quantity("posA", U1) == 1, str(fj.open_quantity("posA", U1)))
    rec = fj.reconcile("posA")
    per = (rec.get("by_user") or {}).get(U1) or {}
    check("W07f the reconciliation calls it partially closed, not closed",
          per.get("status") == "partially_closed", str(per))
    check("W07f a partial close does not claim a round trip result",
          per.get("round_trip_cash_cents") is None, str(per))

    out = say(U1, "sold 1 @ 1.90", candidates=cands, mid=9403)
    check("W07f the last contract out closes it", out.get("status") == "accepted"
          and fj.open_quantity("posA", U1) == 0, str(fj.open_quantity("posA", U1)))
    per = (fj.reconcile("posA").get("by_user") or {}).get(U1) or {}
    check("W07f and only then is there a round trip to report",
          per.get("status") == "closed"
          and per.get("round_trip_cash_cents") is not None, str(per))
else:
    for n in ("replying to an EXIT card is read as a close, not a second buy",
              "one of two contracts out leaves one open",
              "the reconciliation calls it partially closed, not closed",
              "a partial close does not claim a round trip result",
              "the last contract out closes it",
              "and only then is there a round trip to report"):
        check(f"W07f {n}", False, "no fill_journal.py")


# ===========================================================================
print("\n--- W07g. OVER CLOSE: you cannot sell three of two ---")

if fj is not None:
    clean()
    cands = [entry_card(), exit_card()]
    say(U1, "2 @ 1.35", candidates=cands, mid=9501, reply_to=7001,
        reply_text=ENTRY_TEXT)
    over = say(U1, "sold 3 @ 2.05", candidates=cands, mid=9502, reply_to=7002,
               reply_text=STOP_TEXT)
    check("W07g selling more than is open is refused",
          over.get("status") == "rejected", str(over))
    check("W07g the refusal names the over close",
          over.get("reason") == "over_close", str(over.get("reason")))
    check("W07g the ledger never goes negative",
          fj.open_quantity("posA", U1) == 2, str(fj.open_quantity("posA", U1)))
    check("W07g and the sell was not written as a fill",
          len(fj.read_records("fills")) == 1,
          str(len(fj.read_records("fills"))))
    check("W07g the owner is told what he actually has open",
          "2" in (over.get("reply") or ""), over.get("reply"))
    check("W07g the refused report is still recorded as a response",
          any(r.get("status") == "rejected" for r in fj.read_records("responses")))

    # the honest repair: correct the OPEN quantity, then the close fits
    fix = say(U1, "correction bought 3 @ 1.35", candidates=cands, mid=9503,
              reply_to=7001, reply_text=ENTRY_TEXT)
    check("W07g correcting the open quantity is accepted",
          fix.get("status") == "revision", str(fix))
    ok = say(U1, "sold 3 @ 2.05", candidates=cands, mid=9504, reply_to=7002,
             reply_text=STOP_TEXT)
    check("W07g and then the close of three fits",
          ok.get("status") == "accepted"
          and fj.open_quantity("posA", U1) == 0, str(ok))

    # the other way in: shrinking an OPENING leg below what has already been
    # sold. A correction is a revision, not an exemption from arithmetic.
    shrink = say(U1, "correction bought 1 @ 1.35", candidates=cands, mid=9505,
                 reply_to=7001, reply_text=ENTRY_TEXT)
    check("W07g a correction that would leave a negative count is refused too",
          shrink.get("status") == "rejected"
          and shrink.get("reason") == "correction_would_go_negative",
          str(shrink))
    check("W07g and the ledger is still what it was",
          fj.open_quantity("posA", U1) == 0
          and (fj.reconcile("posA")["by_user"][U1]["bought_quantity"]) == 3,
          str(fj.reconcile("posA")["by_user"].get(U1)))
else:
    for n in ("selling more than is open is refused",
              "the refusal names the over close",
              "the ledger never goes negative",
              "and the sell was not written as a fill",
              "the owner is told what he actually has open",
              "the refused report is still recorded as a response",
              "correcting the open quantity is accepted",
              "and then the close of three fits"):
        check(f"W07g {n}", False, "no fill_journal.py")


# ===========================================================================
print("\n--- W07h. LATE CORRECTION: a correction is a revision, never an "
      "overwrite ---")

if fj is not None:
    clean()
    cands = [entry_card(), exit_card()]
    first = say(U1, "2 @ 1.35", candidates=cands, mid=9601, reply_to=7001,
                reply_text=ENTRY_TEXT, now="2026-09-09T14:00:00+00:00")
    say(U1, "sold 2 @ 2.05", candidates=cands, mid=9602, reply_to=7002,
        reply_text=STOP_TEXT, now="2026-09-09T18:00:00+00:00")
    # the correction arrives the NEXT DAY, after the trade is long closed
    late = say(U1, "correction: entry was 2 @ 1.41", candidates=cands,
               mid=9603, reply_to=7001, reply_text=ENTRY_TEXT,
               now="2026-09-10T13:00:00+00:00")
    check("W07h a late correction is accepted as a revision",
          late.get("status") == "revision", str(late))
    lf = late.get("fill") or {}
    ff = first.get("fill") or {}
    check("W07h the revision names the row it supersedes",
          lf.get("supersedes_fill_id") == ff.get("fill_id"),
          f"{lf.get('supersedes_fill_id')} vs {ff.get('fill_id')}")
    check("W07h the revision number goes up",
          lf.get("revision") == (ff.get("revision") or 0) + 1,
          f"{ff.get('revision')} -> {lf.get('revision')}")
    check("W07h the original row is still on disk, not overwritten",
          any(r.get("fill_id") == ff.get("fill_id")
              for r in fj.read_records("fills")))
    check("W07h the original row is unchanged on disk",
          any(r.get("fill_id") == ff.get("fill_id")
              and abs(float(r.get("fill_price") or 0) - 1.35) < 1e-9
              for r in fj.read_records("fills")))
    live = fj.fills("posA", U1)
    check("W07h the live view shows only the newest revision of that leg",
          sum(1 for r in live if r.get("buy_or_sell") == "buy") == 1, str(live))
    check("W07h and it is the corrected price",
          any(abs(float(r.get("fill_price")) - 1.41) < 1e-9
              for r in live if r.get("buy_or_sell") == "buy"), str(live))
    check("W07h a correction does not reopen a closed leg",
          fj.open_quantity("posA", U1) == 0, str(fj.open_quantity("posA", U1)))
else:
    for n in ("a late correction is accepted as a revision",
              "the revision names the row it supersedes",
              "the revision number goes up",
              "the original row is still on disk, not overwritten",
              "the original row is unchanged on disk",
              "the live view shows only the newest revision of that leg",
              "and it is the corrected price",
              "a correction does not reopen a closed leg"):
        check(f"W07h {n}", False, "no fill_journal.py")


# ===========================================================================
print("\n--- W07i. NO REPLY: unknown, with a reason. Not zero, not no trade, "
      "and never nagged ---")

if fj is not None:
    clean()
    fj.note_signal(position_id="posA", candidate_id="cand_posA",
                   contract_id="SPY260909C00640000", symbol="SPY",
                   recipients=[U1, U2], alerted_at_utc="2026-09-09T14:30:00+00:00")
    cov = fj.coverage()
    check("W07i an alert with no reply is counted as a signal",
          cov.get("signals") == 1, str(cov))
    check("W07i a signal with no reply is UNKNOWN, not a no trade",
          cov.get("signals_unknown") == 1 and cov.get("signals_reported") == 0,
          str(cov))
    unk = fj.unknown_signals()
    check("W07i the unknown carries a reason, not a silence",
          len(unk) == 1 and unk[0].get("reason") == "no_reply", str(unk))
    check("W07i the unknown is classed unknown, never a fill",
          len(unk) == 1 and unk[0].get("evidence_class") == fj.UNKNOWN,
          str(unk))
    check("W07i an unreported signal invents no quantity",
          len(unk) == 1 and unk[0].get("quantity") is None, str(unk))
    check("W07i and no fill row was written for it",
          not fj.read_records("fills"))

    # one recipient reports, the other never does. The silent one stays unknown
    say(U1, "2 @ 1.35", candidates=[entry_card()], mid=9701, reply_to=7001,
        reply_text=ENTRY_TEXT)
    cov = fj.coverage()
    check("W07i one reporter makes the SIGNAL reported",
          cov.get("signals_reported") == 1 and cov.get("signals_unknown") == 0,
          str(cov))
    check("W07i but the silent recipient is still counted as unreported",
          cov.get("recipients_unreported") == 1, str(cov))

    # the silence is counted PER SIGNAL. A fill on a position nobody noted
    # must not cancel out a real silence somewhere else, which one subtraction
    # across the whole volume would do.
    say(U1, "2 @ 1.35", candidates=[entry_card("posQ")], mid=9702,
        now="2026-09-09T15:30:00+00:00")
    cov = fj.coverage()
    check("W07i an unnoted fill elsewhere does not erase a real silence",
          cov.get("recipients_unreported") == 1, str(cov))

    # the nag guard: nothing here produces an unsolicited message
    src = (REPO / "fill_journal.py").read_text(encoding="utf-8")
    check("W07i the journal has no reminder, nag or chase entry point",
          not re.search(r"\bdef +(remind|nag|chase|prompt_for_fill|"
                        r"ask_for_fill)\b", src))
    scan = (REPO / "scanner.py").read_text(encoding="utf-8")
    mon = re.search(r"\n    def monitor_positions\(.*?\n    def ", scan, re.S)
    check("W07i the monitor loop never calls the fill journal",
          mon is not None and "fill_journal" not in (mon.group(0) if mon else ""),
          "the exit loop must not carry an observer's work")
else:
    for n in ("an alert with no reply is counted as a signal",
              "a signal with no reply is UNKNOWN, not a no trade",
              "the unknown carries a reason, not a silence",
              "the unknown is classed unknown, never a fill",
              "an unreported signal invents no quantity",
              "and no fill row was written for it",
              "one reporter makes the SIGNAL reported",
              "but the silent recipient is still counted as unreported",
              "the journal has no reminder, nag or chase entry point",
              "the monitor loop never calls the fill journal"):
        check(f"W07i {n}", False, "no fill_journal.py")


# ===========================================================================
print("\n--- W07j. TWO USERS ON ONE SIGNAL: three fills of one card are three "
      "execution experiences, not three signals ---")

if fj is not None:
    clean()
    fj.note_signal(position_id="posA", candidate_id="cand_posA",
                   contract_id="SPY260909C00640000", symbol="SPY",
                   recipients=[U1, U2], alerted_at_utc="2026-09-09T14:30:00+00:00")
    cands = [entry_card()]
    r1 = say(U1, "2 @ 1.35", candidates=cands, mid=9801, reply_to=7001,
             reply_text=ENTRY_TEXT)
    r2 = say(U2, "1 @ 1.40", candidates=cands, mid=9802, reply_to=8001,
             reply_text=ENTRY_TEXT)
    check("W07j both recipients' reports are accepted",
          r1.get("status") == "accepted" and r2.get("status") == "accepted",
          f"{r1.get('status')}, {r2.get('status')}")
    cov = fj.coverage()
    check("W07j the signal denominator stays at ONE",
          cov.get("signals") == 1, str(cov))
    check("W07j the execution experiences count TWO",
          cov.get("execution_experiences") == 2, str(cov))
    check("W07j the two experiences are not merged into one quantity",
          fj.open_quantity("posA", U1) == 2
          and fj.open_quantity("posA", U2) == 1,
          f"{fj.open_quantity('posA', U1)} / {fj.open_quantity('posA', U2)}")
    rec = fj.reconcile("posA")
    check("W07j the reconciliation is per user, not pooled",
          set((rec.get("by_user") or {})) == {U1, U2}, str(list(rec.get("by_user") or {})))
    check("W07j the reconciliation says so out loud: one signal",
          rec.get("signals") == 1 and rec.get("execution_experiences") == 2,
          str({k: rec.get(k) for k in ("signals", "execution_experiences")}))
    check("W07j no recipient identifier is stored, only the opaque ref",
          all(re.fullmatch(r"[0-9a-f]{6,32}", str(r.get("user_ref") or ""))
              for r in fj.read_records("fills")),
          str([r.get("user_ref") for r in fj.read_records("fills")]))

    # U2's over close must not be paid for out of U1's contracts
    bad = say(U2, "sold 2 @ 2.05", candidates=[exit_card()], mid=9803,
              reply_to=8002, reply_text=STOP_TEXT)
    check("W07j one user cannot close another user's contracts",
          bad.get("status") == "rejected"
          and bad.get("reason") == "over_close", str(bad))
else:
    for n in ("both recipients' reports are accepted",
              "the signal denominator stays at ONE",
              "the execution experiences count TWO",
              "the two experiences are not merged into one quantity",
              "the reconciliation is per user, not pooled",
              "the reconciliation says so out loud: one signal",
              "no recipient identifier is stored, only the opaque ref",
              "one user cannot close another user's contracts"):
        check(f"W07j {n}", False, "no fill_journal.py")


# ===========================================================================
print("\n--- W07k. COSTS RECONCILE: integer cents, both legs, fees on every "
      "one ---")

if fj is not None:
    clean()
    cands = [entry_card(), exit_card()]
    say(U1, "2 @ 1.35 fees 1.30", candidates=cands, mid=9901, reply_to=7001,
        reply_text=ENTRY_TEXT)
    say(U1, "sold 2 @ 2.05 fees 1.30", candidates=cands, mid=9902,
        reply_to=7002, reply_text=STOP_TEXT)
    per = (fj.reconcile("posA").get("by_user") or {}).get(U1) or {}
    # 2 x 1.35 x 100 = 27000c out, plus 130c fees = -27130
    # 2 x 2.05 x 100 = 41000c in,  less 130c fees = +40870
    check("W07k the buy leg is a debit in whole cents",
          per.get("bought_cash_cents") == -27130, str(per.get("bought_cash_cents")))
    check("W07k the sell leg is a credit in whole cents",
          per.get("sold_cash_cents") == 40870, str(per.get("sold_cash_cents")))
    check("W07k the round trip reconciles to the sum of its legs",
          per.get("round_trip_cash_cents") == 13740,
          str(per.get("round_trip_cash_cents")))
    check("W07k every cash figure is an integer number of cents",
          all(isinstance(per.get(k), int) for k in
              ("bought_cash_cents", "sold_cash_cents", "round_trip_cash_cents")),
          str({k: type(per.get(k)).__name__ for k in
               ("bought_cash_cents", "sold_cash_cents", "round_trip_cash_cents")}))
    check("W07k fees are carried, not dropped",
          per.get("fees_cents") == 260, str(per.get("fees_cents")))
    check("W07k the quantities balance",
          per.get("bought_quantity") == 2 and per.get("sold_quantity") == 2
          and per.get("open_quantity") == 0, str(per))
    check("W07k the reconciliation says it balances",
          per.get("reconciles") is True, str(per))

    # a float that cannot be represented exactly must still land on the cent
    clean()
    say(U1, "3 @ 0.07", candidates=cands, mid=9903, reply_to=7001,
        reply_text=ENTRY_TEXT)
    per = (fj.reconcile("posA").get("by_user") or {}).get(U1) or {}
    check("W07k 3 at 0.07 is exactly 2100 cents, not 2099 or 2100.0000001",
          per.get("bought_cash_cents") == -2100,
          str(per.get("bought_cash_cents")))
else:
    for n in ("the buy leg is a debit in whole cents",
              "the sell leg is a credit in whole cents",
              "the round trip reconciles to the sum of its legs",
              "every cash figure is an integer number of cents",
              "fees are carried, not dropped", "the quantities balance",
              "the reconciliation says it balances",
              "3 at 0.07 is exactly 2100 cents, not 2099 or 2100.0000001"):
        check(f"W07k {n}", False, "no fill_journal.py")


# ===========================================================================
print("\n--- W07l. EVIDENCE CLASS: a modeled return never becomes a claimed "
      "broker fill ---")

if fj is not None:
    clean()
    cands = [entry_card()]
    say(U1, "2 @ 1.35", candidates=cands, mid=9950, reply_to=7001,
        reply_text=ENTRY_TEXT)
    rows = fj.read_records("fills")
    check("W07l a reported fill is classed as a reported fill",
          bool(rows) and rows[0].get("evidence_class") == fj.REPORTED_FILL,
          str(rows[0].get("evidence_class") if rows else None))
    check("W07l the three classes are named and distinct",
          len({fj.REPORTED_FILL, fj.MODELED, fj.UNKNOWN}) == 3)

    # the one line that matters: nothing can write a modeled number in here
    threw = None
    try:
        fj.record_fill(user_ref=U1, position_id="posA", candidate_id="cand_posA",
                       contract_id="SPY260909C00640000", buy_or_sell="sell",
                       quantity=2, fill_price=2.05,
                       evidence_class=fj.MODELED,
                       reported_at_utc="2026-09-09T18:00:00+00:00")
        threw = False
    except Exception as e:                                    # noqa: BLE001
        threw = type(e).__name__
    check("W07l record_fill REFUSES a modeled evidence class",
          threw not in (False, None), f"it accepted one ({threw})")
    check("W07l and the refusal wrote nothing",
          len(fj.read_records("fills")) == 1,
          str(len(fj.read_records("fills"))))

    check("W07l the journal exposes the mixing guard",
          need("assert_not_mixed") is not None)
    mixed = None
    try:
        fj.assert_not_mixed([{"evidence_class": fj.REPORTED_FILL},
                             {"evidence_class": fj.MODELED}])
        mixed = False
    except Exception as e:                                    # noqa: BLE001
        mixed = type(e).__name__
    check("W07l mixing a modeled row with a reported one raises",
          mixed not in (False, None), f"it allowed the mix ({mixed})")

    view = fj.performance_view(modeled={"posA": -62.0, "posZ": 18.0})
    check("W07l the performance view keeps the classes in separate buckets",
          set(view) >= {"reported", "modeled_only", "unknown"}, str(list(view)))
    check("W07l a position with a reported fill is not counted as modeled only",
          "posA" not in (view.get("modeled_only") or {}), str(view.get("modeled_only")))
    check("W07l a position with no report stays in the modeled bucket alone",
          "posZ" in (view.get("modeled_only") or {}), str(view.get("modeled_only")))
    check("W07l the view refuses to produce one combined number",
          view.get("combined_total") is None and bool(view.get("why_no_combined")),
          str({k: view.get(k) for k in ("combined_total", "why_no_combined")}))

    src = (REPO / "fill_journal.py").read_text(encoding="utf-8")
    check("W07l the module says in words that the two are different facts",
          "modeled" in src and "broker" in src)
else:
    for n in ("a reported fill is classed as a reported fill",
              "the three classes are named and distinct",
              "record_fill REFUSES a modeled evidence class",
              "and the refusal wrote nothing",
              "the journal exposes the mixing guard",
              "mixing a modeled row with a reported one raises",
              "the performance view keeps the classes in separate buckets",
              "a position with a reported fill is not counted as modeled only",
              "a position with no report stays in the modeled bucket alone",
              "the view refuses to produce one combined number",
              "the module says in words that the two are different facts"):
        check(f"W07l {n}", False, "no fill_journal.py")


# ===========================================================================
print("\n--- W07m. EXECUTION TIME: asked for once, or left unknown. Never the "
      "message time ---")

if fj is not None:
    clean()
    cands = [entry_card()]
    MSG_TIME = "2026-09-09T15:05:00+00:00"
    d = say(U1, "2 @ 1.35", candidates=cands, mid=9960, reply_to=7001,
            reply_text=ENTRY_TEXT, now=MSG_TIME)
    f = d.get("fill") or {}
    check("W07m an unreported execution time stays null",
          f.get("executed_at_utc") is None, str(f.get("executed_at_utc")))
    check("W07m the null carries a reason",
          bool(f.get("executed_at_reason")), str(f.get("executed_at_reason")))
    check("W07m the message time is NOT substituted for it",
          f.get("executed_at_utc") != MSG_TIME
          and f.get("executed_at_utc") != f.get("reported_at_utc"),
          f"{f.get('executed_at_utc')} vs {f.get('reported_at_utc')}")
    check("W07m the report time IS recorded, because it is a real fact",
          f.get("reported_at_utc") == MSG_TIME, str(f.get("reported_at_utc")))
    check("W07m the ack asks for the time once", "time" in (d.get("reply") or "").lower(),
          d.get("reply"))

    later = say(U1, "time 9:47", candidates=cands, mid=9961, reply_to=7001,
                reply_text=ENTRY_TEXT, now="2026-09-09T15:20:00+00:00")
    check("W07m a later time answer is a revision of the same fill",
          later.get("status") == "revision", str(later))
    lf = later.get("fill") or {}
    check("W07m the revision fills in the execution time",
          bool(lf.get("executed_at_utc")), str(lf.get("executed_at_utc")))
    check("W07m the reported ET time is stored as UTC",
          str(lf.get("executed_at_utc") or "").startswith("2026-09-09T13:47"),
          str(lf.get("executed_at_utc")))
    # the owner reads his cards in CT and the contract trades on ET, so a bare
    # "9:47" is genuinely ambiguous. The reading is recorded and said out loud
    # rather than baked silently into the UTC value.
    check("W07m the clock the time was read in is recorded, not assumed "
          "silently", lf.get("executed_at_zone") == "ET assumed",
          str(lf.get("executed_at_zone")))
    check("W07m and the ack names that reading so it can be corrected",
          "ET" in (later.get("reply") or "")
          and "assumed" in (later.get("reply") or "")
          and "ct" in (later.get("reply") or ""), later.get("reply"))
    ct = fj.parse_report("2 @ 1.35 at 9:47 ct", on_day="2026-09-09")
    check("W07m an explicitly Central time is read as Central",
          str(ct.get("executed_at_utc") or "").startswith("2026-09-09T14:47")
          and ct.get("executed_at_zone") == "CT",
          f"{ct.get('executed_at_utc')} {ct.get('executed_at_zone')}")
    check("W07m and the corrected fill keeps the original quantity and price",
          lf.get("quantity") == 2 and abs(float(lf.get("fill_price")) - 1.35) < 1e-9,
          str(lf))

    inline = fj.parse_report("2 @ 1.35 at 9:47")
    check("W07m a time given inline is parsed, not treated as a price",
          inline.get("ok") is True and inline.get("quantity") == 2
          and abs((inline.get("price") or 0) - 1.35) < 1e-9
          and bool(inline.get("executed_at_utc")), str(inline))

    # ONCE. Repeating the question on every fill of a busy session is the nag
    # this lane is not allowed to become.
    again = say(U1, "add 1 @ 1.36", candidates=cands, mid=9962, reply_to=7001,
                reply_text=ENTRY_TEXT, now="2026-09-09T15:30:00+00:00")
    check("W07m a second fill the same day is NOT asked for a time again",
          again.get("status") == "accepted"
          and "time 9:47" not in (again.get("reply") or ""),
          again.get("reply"))
    check("W07m and its own execution time is still left unknown, not filled "
          "in from the earlier one",
          (again.get("fill") or {}).get("executed_at_utc") is None
          and bool((again.get("fill") or {}).get("executed_at_reason")),
          str(again.get("fill")))

    # asked ONCE. A second unrelated report does not re-ask about the first
    src = (REPO / "fill_journal.py").read_text(encoding="utf-8")
    check("W07m the time question is attached to the ack, not to a scheduler",
          not re.search(r"\bwhile +True\b|\bthreading\b|\bTimer\b", src))
else:
    for n in ("an unreported execution time stays null",
              "the null carries a reason",
              "the message time is NOT substituted for it",
              "the report time IS recorded, because it is a real fact",
              "the ack asks for the time once",
              "a later time answer is a revision of the same fill",
              "the revision fills in the execution time",
              "the reported ET time is stored as UTC",
              "and the corrected fill keeps the original quantity and price",
              "a time given inline is parsed, not treated as a price",
              "the time question is attached to the ack, not to a scheduler"):
        check(f"W07m {n}", False, "no fill_journal.py")


# ===========================================================================
print("\n--- W07n. NO RESPONSE REMAINS UNKNOWN: every inbound reply gets a "
      "disposition, and a fault in the journal never costs a command ---")

if fj is not None:
    clean()
    cands = [entry_card()]
    seen = []
    for i, (text, mid) in enumerate((("2 @ 1.35", 9970),
                                     ("2.5 @ 1.35", 9971),
                                     ("sold 9 @ 2.05", 9972),
                                     ("bought at", 9973),
                                     ("correction 3 @ 1.35", 9974))):
        d = say(U1, text, candidates=cands, mid=mid, reply_to=7001,
                reply_text=ENTRY_TEXT,
                now=f"2026-09-09T15:{10 + i:02d}:00+00:00")
        seen.append(d)
    check("W07n every reply came back with a status",
          all(d.get("status") in fj.STATUSES for d in seen),
          str([d.get("status") for d in seen]))
    check("W07n every reply came back with something to say",
          all(bool(d.get("reply")) for d in seen),
          str([bool(d.get("reply")) for d in seen]))
    resp = fj.read_records("responses")
    check("W07n every reply left a durable response row",
          len(resp) == len(seen), f"{len(resp)} rows for {len(seen)} replies")
    check("W07n no response row is left without a disposition",
          all(r.get("status") in fj.STATUSES for r in resp),
          str([r.get("status") for r in resp]))
    check("W07n an incomplete report is asked about, not guessed at",
          seen[3].get("status") == "incomplete", str(seen[3]))

    # a message that is not a report at all belongs to the brain, untouched
    check("W07n a plain question is not a fill report",
          fj.looks_like_report("why this strike?", is_reply_to_alert=True) is False)
    check("W07n a bare number in a question is not a fill report",
          fj.looks_like_report("is 1.35 a good price?",
                               is_reply_to_alert=True) is False)
    check("W07n a quantity and price in a reply IS a fill report",
          fj.looks_like_report("2 @ 1.35", is_reply_to_alert=True) is True)
    check("W07n /fill is always a fill report",
          fj.looks_like_report("/fill 2 @ 1.35") is True)

    # the observer contract: a broken journal costs the reply, not the command
    before = len(fj.read_records("responses"))
    real = fj.record_response
    try:
        def boom(**kw):
            raise OSError("volume gone")
        fj.record_response = boom
        blown = say(U1, "2 @ 1.35", candidates=cands, mid=9975, reply_to=7001,
                    reply_text=ENTRY_TEXT)
        raised = False
    except Exception:                                          # noqa: BLE001
        blown, raised = {}, True
    finally:
        fj.record_response = real
    check("W07n a write fault does not propagate out of handle_message",
          raised is False, "it raised into the command loop")
    check("W07n and the failure is reported honestly rather than as success",
          blown.get("status") == "write_failed"
          or blown.get("write_failed") is True, str(blown))
    note(f"responses on disk before the fault: {before}")
else:
    for n in ("every reply came back with a status",
              "every reply came back with something to say",
              "every reply left a durable response row",
              "no response row is left without a disposition",
              "an incomplete report is asked about, not guessed at",
              "a plain question is not a fill report",
              "a bare number in a question is not a fill report",
              "a quantity and price in a reply IS a fill report",
              "/fill is always a fill report",
              "a write fault does not propagate out of handle_message",
              "and the failure is reported honestly rather than as success"):
        check(f"W07n {n}", False, "no fill_journal.py")


# ===========================================================================
print("\n--- W07o. the wiring: the interface exists where a person can reach "
      "it, and the gate runs this file ---")

scan_src = (REPO / "scanner.py").read_text(encoding="utf-8")
cards_src = (REPO / "cards.py").read_text(encoding="utf-8")
tg_src = (REPO / "telegram.py").read_text(encoding="utf-8")

check("W07o scanner routes inbound messages through the fill journal",
      "fill_journal" in scan_src)
check("W07o telegram carries the reply link on an inbound message",
      "reply_to_message" in tg_src)
# scoped to the INBOUND parser on purpose: telegram.py already carried a
# "message_id" key on the OUTBOUND send result, so an unscoped search would
# have passed before the change and proved nothing.
_parse = re.search(r"\ndef _parse_update\(.*?\n(?=def )", tg_src, re.S)
check("W07o the inbound parser carries the message id, for duplicate detection",
      _parse is not None and "message_id" in _parse.group(0),
      "get_messages acks after processing, so a replay must be recognisable")
check("W07o the inbound parser carries the reply link",
      _parse is not None and "reply_to" in _parse.group(0))
check("W07o /fill is a command a NON owner can use too",
      "/fill" in scan_src
      and not re.search(r'ADMIN_CMDS = \{[^}]*"/fill"', scan_src, re.S),
      "logging your own fill must not be owner only: two users on one signal")
check("W07o the entry card mentions the optional log, once",
      cards_src.count("reply with your fill") <= 1
      and "your fill" in cards_src.lower())
check("W07o the card never asks the owner to trade",
      not re.search(r"take this trade so|so I can (record|measure)", cards_src,
                    re.I))

gate = re.search(r"for test in \(([^)]*)\):", (REPO / "self_improve.py")
                 .read_text(encoding="utf-8"), re.S)
check("W07o test_fill_journal.py is registered in the self improvement gate",
      gate is not None and "test_fill_journal.py" in gate.group(1),
      "a test the release command never runs cannot establish a fix")

import test_no_em_dash as em  # noqa: E402
check("W07o fill_journal is punctuation guarded",
      "fill_journal.py" in em.GUARDED, str(em.GUARDED[-3:]))
if fj is not None:
    check("W07o no em dash in the journal's own strings",
          not em.offenders(REPO / "fill_journal.py"),
          str(em.offenders(REPO / "fill_journal.py")[:2]))
else:
    check("W07o no em dash in the journal's own strings", False,
          "no fill_journal.py")


# ===========================================================================
print("\n--- W07p. the wiring, exercised: a real journalled alert, a real "
      "reply, a real row ---")

# Source text proves an interface EXISTS. This proves it WORKS: a card is
# committed to the durable event journal exactly as scanner commits one, the
# delivery records the telegram message id exactly as the send path does, and
# the reply comes back through scanner's own routing rather than through the
# journal's front door.
if fj is not None:
    import event_journal    # noqa: E402
    import scanner          # noqa: E402

    clean()
    event_journal._reset_for_test()
    CARD = ("📈 BUY CALL: SPY 640, expires today\nEntry about $1.35\n"
            "Your call.")
    it = event_journal.commit_intent(
        "entry", candidate_id="candX", decision_id="decX", position_id="posX",
        strategy_id="momentum",
        payload={"position": {"ticker": "SPY", "right": "C", "strike": 640.0,
                              "expiry": "2026-09-09"},
                 "ticker": "SPY"},
        recipients=["111", "222"], text=CARD)
    event_journal.record_result(it.journal_id, 0, event_journal.CONFIRMED,
                               provider_message_id=5555)
    event_journal.record_result(it.journal_id, 1, event_journal.CONFIRMED,
                               provider_message_id=7777)

    svc = scanner.Service.__new__(scanner.Service)
    svc.dry = False

    class _Book:
        positions = []
    svc.book = _Book()

    cands = svc.fill_candidates()
    check("W07p scanner rebuilds the candidate from the durable journal",
          len(cands) == 1 and cands[0]["position_id"] == "posX", str(cands))
    check("W07p and it carries the per recipient message ids the send path "
          "recorded",
          bool(cands) and 5555 in sum((cands[0]["message_ids"] or {}).values(),
                                      []), str(cands[0].get("message_ids")
                                               if cands else None))
    check("W07p and the contract is named, not guessed",
          bool(cands) and cands[0]["contract_id"] == "SPY260909C00640000",
          str(cands[0].get("contract_id") if cands else None))

    item = {"kind": "text", "chat_id": "111", "text": "2 @ 1.35",
            "message_id": 91, "sent_at_utc": "2026-09-09T15:00:00+00:00",
            "reply_to": {"message_id": 5555, "text": CARD, "from_bot": True}}
    said = svc.try_fill_report(item)
    check("W07p a reply to the card comes back with an ack", bool(said),
          str(said))
    rows = fj.fills("posX")
    check("W07p and one fill row landed against that position",
          len(rows) == 1 and rows[0]["quantity"] == 2, str(rows))
    check("W07p resolved by the reply link, not by a guess",
          bool(rows) and rows[0]["resolution_basis"] == "reply_link",
          str(rows[0].get("resolution_basis") if rows else None))
    check("W07p the row carries the opaque recipient ref, never the chat id",
          bool(rows) and rows[0]["user_ref"] == event_journal.recipient_ref("111")
          and "111" not in json.dumps(rows[0]), str(rows[0].get("user_ref")
                                                    if rows else None))

    # the SECOND recipient replies to THEIR copy, whose message id this
    # process only knows because the journal recorded it per recipient
    item2 = dict(item, chat_id="222", message_id=92, text="1 @ 1.40",
                 reply_to={"message_id": 7777, "text": CARD, "from_bot": True})
    svc.try_fill_report(item2)
    rec = fj.reconcile("posX")
    check("W07p two recipients on one card are two experiences of one signal",
          rec["signals"] == 1 and rec["execution_experiences"] == 2, str(rec))

    # a question about the same card is still a question
    ask = svc.try_fill_report(dict(item, message_id=93,
                                   text="why this strike?"))
    check("W07p a question replying to the same card is left for the brain",
          ask is None, str(ask))

    # and a dry run writes nothing at all into the shared journal
    svc.dry = True
    before = len(fj.fills("posX"))
    svc.try_fill_report(dict(item, message_id=94, text="add 1 @ 1.35"))
    check("W07p a dry run never writes into the shared fill journal",
          len(fj.fills("posX")) == before, str(fj.fills("posX")))
    svc.dry = False
    event_journal._reset_for_test()
else:
    for n in ("scanner rebuilds the candidate from the durable journal",
              "and it carries the per recipient message ids the send path "
              "recorded", "and the contract is named, not guessed",
              "a reply to the card comes back with an ack",
              "and one fill row landed against that position",
              "resolved by the reply link, not by a guess",
              "the row carries the opaque recipient ref, never the chat id",
              "two recipients on one card are two experiences of one signal",
              "a question replying to the same card is left for the brain",
              "a dry run never writes into the shared fill journal"):
        check(f"W07p {n}", False, "no fill_journal.py")


# ===========================================================================
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All W07 fill journal checks passed.")
