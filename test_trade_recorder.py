"""W06: record now. The observation recorder, and the faults it has to survive.

Astra's W06 row names five faults and three acceptance lines.

  Faults      chosen exit occurs early while the opposite exits later; a
              missing timestamp; a duplicate or out of order sample; a full
              storage queue; a restart during observation.
  Acceptance  common horizon paths survive the original exit; completeness and
              lost sample counts are measurable; the recorder cannot block
              monitoring.

The defect this package exists to close is A15. MFE recorded only until the
current exit is censored, so "live MFE never reached 2R" is not evidence
against a larger target: once the 0.4R exit stops the observation, nothing that
happens afterwards can appear in the record. Every path case below is about
that censoring.

The second defect is A12. A put against a call tests DIRECTION. A different SPY
call tests STRIKE SELECTION. Calling both "the opposite strike" conflates two
different questions, so the roles are named and separated here.

astra/RECORDER_SCHEMA.md is the contract. W06c reads that file and fails on any
field the schema names and the implementation drops, which is the only way a
schema written before the code stays binding after it.

Nothing here touches strategy. No threshold, no roster, no window, no allow
list. Every case is about what gets written down.

No network, no Telegram, no yfinance, no production storage.

Run:  python test_trade_recorder.py     (exit code 0 = all good)
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
import time as time_mod
from datetime import date, datetime, time, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# an isolated data dir BEFORE config is imported, so nothing here can read or
# write the real runtime state
_TMP = tempfile.mkdtemp(prefix="kelbot_recorder_")
_bot_test_os.environ["DATA_DIR"] = _TMP

import config            # noqa: E402
import market_calendar   # noqa: E402
import storage_io        # noqa: E402

# trade_recorder is the module this package introduces. Imported defensively ON
# PURPOSE: before the change it does not exist, and every case below then fails
# on its own unmet requirement instead of the whole file collapsing into one
# import error that says nothing about which requirement is missing.
try:
    import trade_recorder as tr  # noqa: E402
except ImportError:
    tr = None

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
    """The recorder's interface, or None. A case that needs an interface it
    cannot get FAILS. It never quietly skips, because a skipped case reads as
    coverage in the summary line and is not."""
    if tr is None:
        return None
    return getattr(tr, attr, None)


def fresh():
    """A recorder with no memory of the previous case, EXCEPT the durable
    observation registry, whose survival across a reset is the thing W06j
    tests."""
    if tr is None:
        return False
    reset = getattr(tr, "_reset_for_test", None)
    if reset is None:
        return False
    reset()
    return True


def clean():
    """fresh(), plus an empty observation registry. Used by every case that is
    not about the restart, so one case's leftovers cannot decide another's
    result."""
    if tr is None:
        return False
    try:
        tr.path_for("observations").unlink()
    except (OSError, AttributeError, ValueError):
        pass
    return fresh()


UTC = timezone.utc


def u(h, m, s=0, day=(2026, 9, 9)):
    return datetime(day[0], day[1], day[2], h, m, s, tzinfo=UTC).isoformat()


# ===========================================================================
print("--- W06a. the module exists, writes through the shared protocol, and "
      "starts a bounded queue ---")

check("W06a trade_recorder imports", tr is not None,
      "no trade_recorder.py in the repo")

for fn in ("record_candidate", "record_sample", "record_event",
           "register_contract", "record_health", "observe", "select_controls",
           "sample_once", "coverage", "capacity_estimate", "read_records",
           "common_horizon_utc", "contract_id_for", "drain_once", "start",
           "stop", "report_text"):
    check(f"W06a the recorder exposes {fn}", need(fn) is not None)

if tr is not None:
    src = (REPO / "trade_recorder.py").read_text(encoding="utf-8")
    # Constraint 9: storage_io is the write protocol. A sixth way to write a
    # file is exactly what W05 removed.
    hand_rolled = re.findall(r"\.write_text\(|\.write_bytes\(|open\([^)]*[\"']w",
                             src)
    check("W06a the recorder never hand rolls a file write", not hand_rolled,
          str(hand_rolled[:4]))
    check("W06a the recorder writes through storage_io",
          "import storage_io" in src)
    # Astra constraint 10: the recorder is an OBSERVER. A producer that waits
    # on a queue has already earned the right to delay a stop.
    check("W06a the producer never blocks on the queue",
          ".put(" not in src and "put_nowait" in src,
          "a blocking put in the producer path")
else:
    for n in ("W06a the recorder never hand rolls a file write",
              "W06a the recorder writes through storage_io",
              "W06a the producer never blocks on the queue"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06b. record types 1, 2, 3, 4 and 6 are written at all ---")

CAND = {}
if clean():
    CAND = dict(
        strategy_id="momentum", strategy_version="1", policy_hash="p" * 8,
        source_commit="4a8b8bc", deployment_id="dep-test", symbol="SPX",
        direction="call", session_date="2026-09-09",
        decision_at_utc=u(13, 50), observed_at_utc=u(13, 50, 2),
        input_bar_end_utc=u(13, 50), input_feed="yfinance 5m",
        input_values={"mom_pct": 0.42, "spot": 6510.0, "allow_list": True,
                      "rounding": "round-half-even", "risk_mode": "green"},
        gate_passed=True, reject_codes=[], selected=True,
        position_id="20260909-085000-SPX-C6510")
    cid = tr.record_candidate(**CAND)
    check("W06b record 1 returns a candidate id", isinstance(cid, str) and cid,
          repr(cid))

    con = tr.register_contract(underlying="SPX", option_right="C", strike=6510.0,
                               expiry_date="2026-09-09")
    check("W06b record 4 returns a contract id", isinstance(con, str) and con,
          repr(con))

    sid = tr.record_sample(
        candidate_id=cid, contract_id=con, contract_role="chosen",
        provider="yfinance", feed="yahoo option chain",
        provider_at_utc=u(13, 50), received_at_utc=u(13, 50, 3),
        requested_at_utc=u(13, 50, 2), bid=4.10, ask=4.30, bid_size=12,
        ask_size=9, underlying_price=6510.0, underlying_at_utc=u(13, 50, 1),
        price_basis="quote_mid", is_model=False, quality_flags=[],
        observation_end_utc=u(20, 0))
    check("W06b record 2 returns a sample id", isinstance(sid, str) and sid,
          repr(sid))

    eid = tr.record_event(
        position_id=CAND["position_id"], event_type="sell_half",
        trigger_at_utc=u(14, 5), trigger_sample_id=sid,
        trigger_basis="quote_mid", trigger_threshold=25.0, mark_used=5.30,
        mark_source="live quote", leg_quantity=1, deliveries=[
            {"delivery_id": "dl-1", "recipient_ref": "ab12cd34ef56",
             "recipient_index": 0, "send_attempt_at_utc": u(14, 5, 1),
             "delivery_status": "confirmed", "provider_message_id": 4242,
             "acknowledged_at_utc": u(14, 5, 2)}])
    check("W06b record 3 returns an event id", isinstance(eid, str) and eid,
          repr(eid))

    hid = tr.record_health(ownership_state="ACTIVE", instance_id="inst-test")
    check("W06b record 6 returns a health row", isinstance(hid, dict), repr(hid))

    tr.drain_once()
    for kind, n in (("candidates", 1), ("contracts", 1), ("samples", 1),
                    ("events", 1), ("health", 1)):
        rows = tr.read_records(kind)
        check(f"W06b {kind} landed on disk", len(rows) >= n,
              f"{len(rows)} rows")
else:
    for n in ("W06b record 1 returns a candidate id",
              "W06b record 4 returns a contract id",
              "W06b record 2 returns a sample id",
              "W06b record 3 returns an event id",
              "W06b record 6 returns a health row",
              "W06b candidates landed on disk", "W06b contracts landed on disk",
              "W06b samples landed on disk", "W06b events landed on disk",
              "W06b health landed on disk"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06c. the schema written before the code is still binding after "
      "it ---")


def schema_fields(section):
    """The field list RECORDER_SCHEMA.md declares for one record type."""
    text = SCHEMA_MD.read_text(encoding="utf-8")
    m = re.search(rf"^## {section}\..*?```\n(.*?)```", text, re.S | re.M)
    if not m:
        return []
    return [f.strip() for f in re.split(r"[,\s]+", m.group(1)) if f.strip()]


check("W06c the schema document is present", SCHEMA_MD.exists(), str(SCHEMA_MD))

WANT = {"1": "candidates", "2": "samples", "3": "events", "4": "contracts",
        "6": "health"}
if tr is not None and SCHEMA_MD.exists():
    for section, kind in WANT.items():
        want = schema_fields(section)
        rows = tr.read_records(kind)
        row = rows[0] if rows else {}
        if kind == "events":
            # the delivery half of record 3 is per recipient, so it is nested
            # rather than repeated on the event. Flatten for the comparison.
            row = dict(row)
            for d in (row.get("deliveries") or [{}]):
                row.update(d)
        missing = [f for f in want if f not in row]
        check(f"W06c record {section} carries every field the schema names",
              want and not missing, f"missing {missing}")
    # schema_version is a rule for every record, not one field on one record
    for kind in WANT.values():
        rows = tr.read_records(kind)
        check(f"W06c {kind} rows carry schema_version",
              bool(rows) and all("schema_version" in r for r in rows))
else:
    for section, kind in WANT.items():
        check(f"W06c record {section} carries every field the schema names",
              False, "no trade_recorder.py")
        check(f"W06c {kind} rows carry schema_version", False,
              "no trade_recorder.py")


# ===========================================================================
print("\n--- W06d. MISSING TIMESTAMP: unknown is null with a reason, never "
      "zero, false or an invented clock ---")

if clean():
    con = tr.register_contract(underlying="SPY", option_right="C", strike=651.0,
                               expiry_date="2026-09-09")
    sid = tr.record_sample(
        candidate_id="c-unknown", contract_id=con, contract_role="chosen",
        provider="yfinance", feed="yahoo option chain",
        provider_at_utc=None, received_at_utc=u(14, 30), requested_at_utc=None,
        bid=None, ask=None, bid_size=None, ask_size=None,
        underlying_price=None, underlying_at_utc=None,
        price_basis=None, is_model=None, quality_flags=[],
        missing_reason="chain returned no row for this strike",
        observation_end_utc=u(20, 0))
    tr.drain_once()
    row = [r for r in tr.read_records("samples") if r.get("sample_id") == sid]
    row = row[0] if row else {}
    check("W06d an absent provider timestamp stays null",
          row.get("provider_at_utc") is None, repr(row.get("provider_at_utc")))
    check("W06d the absent provider timestamp is NOT the received timestamp",
          row.get("provider_at_utc") != row.get("received_at_utc"))
    check("W06d an absent bid stays null and never becomes zero",
          row.get("bid") is None, repr(row.get("bid")))
    check("W06d an absent ask stays null and never becomes zero",
          row.get("ask") is None, repr(row.get("ask")))
    check("W06d is_model stays null rather than defaulting to false",
          row.get("is_model") is None, repr(row.get("is_model")))
    check("W06d the null carries a reason", bool(row.get("missing_reason")),
          repr(row.get("missing_reason")))
    # and a null with NO reason offered is still given one, never left bare
    sid2 = tr.record_sample(
        candidate_id="c-unknown", contract_id=con, contract_role="chosen",
        provider="yfinance", feed="yahoo option chain", provider_at_utc=None,
        received_at_utc=u(14, 31), requested_at_utc=None, bid=None, ask=None,
        bid_size=None, ask_size=None, underlying_price=None,
        underlying_at_utc=None, price_basis=None, is_model=None,
        quality_flags=[], observation_end_utc=u(20, 0))
    tr.drain_once()
    row2 = [r for r in tr.read_records("samples") if r.get("sample_id") == sid2]
    row2 = row2[0] if row2 else {}
    check("W06d a null with no stated reason still gets one",
          bool(row2.get("missing_reason")), repr(row2.get("missing_reason")))

    # the exit side of the same rule: an exit with no identified trigger sample
    eid = tr.record_event(position_id="pos-x", event_type="stop",
                          trigger_at_utc=u(15, 0), trigger_sample_id=None,
                          trigger_basis=None, trigger_threshold=-90.0,
                          mark_used=None, mark_source=None, leg_quantity=1,
                          deliveries=[])
    tr.drain_once()
    ev = [r for r in tr.read_records("events") if r.get("event_id") == eid]
    ev = ev[0] if ev else {}
    check("W06d an exit with no identified trigger sample says so",
          ev.get("trigger_sample_id") is None and bool(ev.get("missing_reason")),
          f"{ev.get('trigger_sample_id')!r} {ev.get('missing_reason')!r}")
else:
    for n in ("W06d an absent provider timestamp stays null",
              "W06d the absent provider timestamp is NOT the received timestamp",
              "W06d an absent bid stays null and never becomes zero",
              "W06d an absent ask stays null and never becomes zero",
              "W06d is_model stays null rather than defaulting to false",
              "W06d the null carries a reason",
              "W06d a null with no stated reason still gets one",
              "W06d an exit with no identified trigger sample says so"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06e. A15: the chosen exit happens early and the path keeps "
      "going ---")

if clean():
    day = (2026, 9, 9)
    horizon = tr.common_horizon_utc(date(2026, 9, 9))
    chosen = tr.register_contract(underlying="SPX", option_right="C",
                                  strike=6510.0, expiry_date="2026-09-09")
    opp = tr.register_contract(underlying="SPX", option_right="P",
                               strike=6510.0, expiry_date="2026-09-09")
    tr.observe(contract_id=chosen, contract_role="chosen",
               candidate_id="c-a15", position_id="pos-a15",
               observation_end_utc=horizon)
    tr.observe(contract_id=opp, contract_role="direction_control",
               candidate_id="c-a15", position_id="pos-a15",
               observation_end_utc=horizon)

    # the chosen contract runs up a little, the exit fires at 14:05, and the
    # real move happens afterwards. This is exactly the shape A15 describes.
    chosen_path = [(13, 50, 4.20), (13, 55, 4.60), (14, 0, 5.10),
                   (14, 5, 5.30), (14, 30, 7.90), (15, 0, 11.40)]
    opp_path = [(13, 50, 4.00), (13, 55, 3.60), (14, 0, 3.10),
                (14, 5, 2.90), (14, 30, 1.40), (15, 0, 0.60)]
    exit_at = u(14, 5)
    for (h, m, px), (_, _, opx) in zip(chosen_path, opp_path):
        for cid_, role, price in ((chosen, "chosen", px),
                                  (opp, "direction_control", opx)):
            tr.record_sample(
                candidate_id="c-a15", contract_id=cid_, contract_role=role,
                provider="yfinance", feed="yahoo option chain",
                provider_at_utc=u(h, m, day=day),
                received_at_utc=u(h, m, 2, day=day),
                requested_at_utc=u(h, m, 1, day=day),
                bid=price - 0.05, ask=price + 0.05, bid_size=5, ask_size=5,
                underlying_price=6510.0, underlying_at_utc=u(h, m, day=day),
                price_basis="quote_mid", is_model=False, quality_flags=[],
                observation_end_utc=horizon)
        if (h, m) == (14, 5):
            tr.record_event(position_id="pos-a15", event_type="sell_half",
                            trigger_at_utc=exit_at, trigger_sample_id=None,
                            trigger_basis="quote_mid", trigger_threshold=25.0,
                            mark_used=5.30, mark_source="live quote",
                            leg_quantity=1, deliveries=[])
    tr.drain_once()

    rows = tr.read_records("samples")
    after = [r for r in rows if r.get("received_at_utc", "") > exit_at]
    ch_after = [r for r in after if r.get("contract_id") == chosen]
    op_after = [r for r in after if r.get("contract_id") == opp]
    check("W06e the CHOSEN contract keeps being sampled after its own exit",
          len(ch_after) >= 2, f"{len(ch_after)} samples after the exit")
    check("W06e the direction control keeps being sampled after that exit",
          len(op_after) >= 2, f"{len(op_after)} samples after the exit")
    check("W06e both contracts share one common observation end",
          len({r.get("observation_end_utc") for r in rows}) == 1,
          str({r.get("observation_end_utc") for r in rows}))

    summ = tr.path_summary("c-a15", chosen) if need("path_summary") else {}
    check("W06e the recorded path exposes a summary", bool(summ), repr(summ))
    # the whole point: the best price the bot OBSERVED is 11.40 at 15:00, long
    # after the 5.30 exit. A recorder that stopped at the exit reports 5.30 and
    # A15 stays unanswerable for ever.
    check("W06e the observed maximum is the post exit high, not the exit mark",
          abs((summ.get("observed_max_mark") or 0) - 11.40) < 0.01,
          repr(summ.get("observed_max_mark")))
    check("W06e the summary says when the observed maximum happened",
          (summ.get("observed_max_at_utc") or "") > exit_at,
          repr(summ.get("observed_max_at_utc")))
    # Astra: fifteen second polling can miss intrasecond spikes. Nothing here
    # may read as a booked profit.
    check("W06e the summary is named as OBSERVED, never as achieved",
          all(k.startswith("observed_") or k in
              ("contract_id", "candidate_id", "samples", "cadence_s",
               "first_sample_at_utc", "last_sample_at_utc", "caveat",
               "observation_end_utc", "complete")
              for k in summ),
          str(sorted(summ)))
    check("W06e the summary carries the unobserved spike caveat",
          "not" in (summ.get("caveat") or "").lower()
          and bool(summ.get("caveat")), repr(summ.get("caveat")))
else:
    for n in ("W06e the CHOSEN contract keeps being sampled after its own exit",
              "W06e the direction control keeps being sampled after that exit",
              "W06e both contracts share one common observation end",
              "W06e the recorded path exposes a summary",
              "W06e the observed maximum is the post exit high, not the exit mark",
              "W06e the summary says when the observed maximum happened",
              "W06e the summary is named as OBSERVED, never as achieved",
              "W06e the summary carries the unobserved spike caveat"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06f. A12: direction control and strike control are different "
      "questions ---")

if clean():
    grid = [6480.0, 6490.0, 6500.0, 6510.0, 6520.0, 6530.0, 6540.0]
    picked = tr.select_controls(underlying="SPX", right="C", strike=6510.0,
                                spot=6505.0, expiry_date="2026-09-09",
                                strikes={"C": grid, "P": grid})
    d = (picked or {}).get("direction_control") or {}
    s = (picked or {}).get("strike_control") or {}
    check("W06f the direction control is the OPPOSITE right",
          d.get("right") == "P", repr(d))
    check("W06f the direction control keeps the same expiry",
          d.get("expiry_date") == "2026-09-09", repr(d))
    check("W06f the strike control keeps the SAME right",
          s.get("right") == "C", repr(s))
    check("W06f the strike control is a DIFFERENT strike",
          s.get("strike") not in (None, 6510.0), repr(s))
    check("W06f each control names the rule that picked it",
          bool(d.get("selection_rule")) and bool(s.get("selection_rule")),
          f"{d.get('selection_rule')!r} {s.get('selection_rule')!r}")
    check("W06f the strike control is NAMED, per A12",
          bool(s.get("name")), repr(s.get("name")))

    # "so future information cannot slip into the selection": the rule is a
    # function of the grid and the chosen contract, never of any price that
    # could only be known later. Two different price worlds, one answer.
    again = tr.select_controls(underlying="SPX", right="C", strike=6510.0,
                               spot=6505.0, expiry_date="2026-09-09",
                               strikes={"C": grid, "P": grid},
                               quotes={"C": {k: {"bid": 99.0, "ask": 99.5}
                                             for k in grid}})
    check("W06f the selection does not move when the quotes move",
          again == picked, f"{picked} vs {again}")
    check("W06f a control that cannot be listed is a coverage gap, not a "
          "silent drop",
          (tr.select_controls(underlying="SPX", right="C", strike=6510.0,
                              spot=6505.0, expiry_date="2026-09-09",
                              strikes={"C": [6510.0], "P": []})
           or {}).get("direction_control", {}).get("missing_reason"),
          "an unlistable control must say why")
else:
    for n in ("W06f the direction control is the OPPOSITE right",
              "W06f the direction control keeps the same expiry",
              "W06f the strike control keeps the SAME right",
              "W06f the strike control is a DIFFERENT strike",
              "W06f each control names the rule that picked it",
              "W06f the strike control is NAMED, per A12",
              "W06f the selection does not move when the quotes move",
              "W06f a control that cannot be listed is a coverage gap, not a "
              "silent drop"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06g. DUPLICATE AND OUT OF ORDER SAMPLES ---")

if clean():
    con = tr.register_contract(underlying="SPY", option_right="C", strike=651.0,
                               expiry_date="2026-09-09")
    common = dict(candidate_id="c-dup", contract_id=con, contract_role="chosen",
                  provider="yfinance", feed="yahoo option chain",
                  bid=4.10, ask=4.30, bid_size=1, ask_size=1,
                  underlying_price=651.0, price_basis="quote_mid",
                  is_model=False, observation_end_utc=u(20, 0))
    a = tr.record_sample(provider_at_utc=u(14, 0), received_at_utc=u(14, 0, 5),
                         requested_at_utc=u(14, 0, 4),
                         underlying_at_utc=u(14, 0), quality_flags=[], **common)
    b = tr.record_sample(provider_at_utc=u(14, 0), received_at_utc=u(14, 0, 20),
                         requested_at_utc=u(14, 0, 19),
                         underlying_at_utc=u(14, 0), quality_flags=[], **common)
    c = tr.record_sample(provider_at_utc=u(13, 59), received_at_utc=u(14, 0, 10),
                         requested_at_utc=u(14, 0, 9),
                         underlying_at_utc=u(13, 59), quality_flags=[], **common)
    tr.drain_once()
    rows = {r["sample_id"]: r for r in tr.read_records("samples")}
    check("W06g every sample is written, none silently dropped",
          {a, b, c} <= set(rows), f"{len(rows)} of 3")
    check("W06g sample ids are distinct", len({a, b, c}) == 3)
    seqs = [rows[x]["sample_sequence"] for x in (a, b, c) if x in rows]
    check("W06g the sequence is monotonic in arrival order",
          seqs == sorted(seqs) and len(set(seqs)) == len(seqs), str(seqs))
    check("W06g a repeat of the same provider timestamp is FLAGGED, not hidden",
          "repeat_provider_timestamp" in (rows.get(b, {}).get("quality_flags")
                                          or []),
          str(rows.get(b, {}).get("quality_flags")))
    check("W06g a provider timestamp that goes backwards is flagged",
          "out_of_order_provider" in (rows.get(c, {}).get("quality_flags") or []),
          str(rows.get(c, {}).get("quality_flags")))
    cov = tr.coverage()
    check("W06g duplicates are COUNTED so the denominator survives",
          (cov.get("duplicate_samples") or 0) >= 1, str(cov))
    check("W06g out of order arrivals are counted",
          (cov.get("out_of_order_samples") or 0) >= 1, str(cov))
else:
    for n in ("W06g every sample is written, none silently dropped",
              "W06g sample ids are distinct",
              "W06g the sequence is monotonic in arrival order",
              "W06g a repeat of the same provider timestamp is FLAGGED, not hidden",
              "W06g a provider timestamp that goes backwards is flagged",
              "W06g duplicates are COUNTED so the denominator survives",
              "W06g out of order arrivals are counted"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06h. STORAGE QUEUE FULL: saturation loses samples loudly, never "
      "the exit loop ---")

if clean():
    cap = tr.QUEUE_MAX if need("QUEUE_MAX") else 0
    check("W06h the queue is bounded", isinstance(cap, int) and 0 < cap < 100000,
          repr(cap))
    con = tr.register_contract(underlying="SPY", option_right="C", strike=651.0,
                               expiry_date="2026-09-09")
    tr.drain_once()   # start from an empty queue
    t0 = time_mod.monotonic()
    n = (cap or 10) + 200
    for i in range(n):
        tr.record_sample(candidate_id="c-full", contract_id=con,
                         contract_role="chosen", provider="yfinance",
                         feed="yahoo option chain", provider_at_utc=u(14, 0, 0),
                         received_at_utc=u(14, 0, 0), requested_at_utc=None,
                         bid=1.0, ask=1.1, bid_size=1, ask_size=1,
                         underlying_price=651.0, underlying_at_utc=u(14, 0),
                         price_basis="quote_mid", is_model=False,
                         quality_flags=[], observation_end_utc=u(20, 0))
    elapsed = time_mod.monotonic() - t0
    check("W06h a saturated queue never blocks the producer", elapsed < 2.0,
          f"{elapsed:.2f}s for {n} samples")
    cov = tr.coverage()
    check("W06h saturation increments a lost sample counter",
          (cov.get("dropped_samples") or 0) >= 100, str(cov.get("dropped_samples")))
    check("W06h saturation downgrades evidence completeness",
          cov.get("completeness") != "complete", repr(cov.get("completeness")))
    tr.drain_once()
    on_disk = storage_io.read_json(tr.path_for("coverage"))
    check("W06h the loss counter is DURABLE, not only in memory",
          on_disk.usable and (on_disk.value or {}).get("dropped_samples", 0) >= 100,
          f"{on_disk.status} {on_disk.value}")
    hrow = tr.record_health(ownership_state="ACTIVE", instance_id="inst-test")
    check("W06h the health record carries the dropped count",
          (hrow or {}).get("dropped_samples", 0) >= 100, str(hrow))
else:
    for n in ("W06h the queue is bounded",
              "W06h a saturated queue never blocks the producer",
              "W06h saturation increments a lost sample counter",
              "W06h saturation downgrades evidence completeness",
              "W06h the loss counter is DURABLE, not only in memory",
              "W06h the health record carries the dropped count"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06i. THE RECORDER CANNOT BLOCK MONITORING ---")

if clean():
    con = tr.register_contract(underlying="SPY", option_right="C", strike=651.0,
                               expiry_date="2026-09-09")

    # patched at the call the writer actually makes. A batching writer that
    # never touched this function would make the check vacuous, which is worse
    # than no check at all.
    real_append = storage_io.append_text

    def _hung(path, text, **kw):
        time_mod.sleep(0.35)
        return real_append(path, text, **kw)

    storage_io.append_text = _hung
    try:
        tr.start()
        t0 = time_mod.monotonic()
        for i in range(25):
            tr.record_sample(candidate_id="c-slow", contract_id=con,
                             contract_role="chosen", provider="yfinance",
                             feed="yahoo option chain",
                             provider_at_utc=u(14, 0, i % 60),
                             received_at_utc=u(14, 0, i % 60),
                             requested_at_utc=None, bid=1.0, ask=1.1,
                             bid_size=1, ask_size=1, underlying_price=651.0,
                             underlying_at_utc=u(14, 0), price_basis="quote_mid",
                             is_model=False, quality_flags=[],
                             observation_end_utc=u(20, 0))
        elapsed = time_mod.monotonic() - t0
        check("W06i a hung writer does not slow the producer down",
              elapsed < 0.5, f"{elapsed:.2f}s while the writer sleeps 0.35s/row")
    finally:
        storage_io.append_text = real_append
        tr.stop(timeout=5.0)

    # a record the writer cannot even encode must be counted and swallowed,
    # never raised back into a monitoring cycle
    fresh()
    bad = tr.record_sample(candidate_id="c-bad", contract_id=con,
                           contract_role="chosen", provider="yfinance",
                           feed="yahoo option chain", provider_at_utc=u(14, 0),
                           received_at_utc=u(14, 0), requested_at_utc=None,
                           bid=1.0, ask=1.1, bid_size=1, ask_size=1,
                           underlying_price=651.0, underlying_at_utc=u(14, 0),
                           price_basis="quote_mid", is_model=False,
                           quality_flags=[], observation_end_utc=u(20, 0),
                           extra={"unserialisable": object()})
    check("W06i an unwritable record still returns an id to the caller",
          isinstance(bad, str) and bad, repr(bad))
    try:
        tr.drain_once()
        raised = None
    except Exception as e:                                     # noqa: BLE001
        raised = e
    check("W06i an unwritable record never raises out of the writer",
          raised is None, repr(raised))
    check("W06i an unwritable record is counted as a write failure",
          (tr.coverage().get("write_failures") or 0) >= 1,
          str(tr.coverage()))
else:
    for n in ("W06i a hung writer does not slow the producer down",
              "W06i an unwritable record still returns an id to the caller",
              "W06i an unwritable record never raises out of the writer",
              "W06i an unwritable record is counted as a write failure"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06j. RESTART DURING OBSERVATION ---")

if clean():
    horizon = tr.common_horizon_utc(date(2026, 9, 9))
    con = tr.register_contract(underlying="SPX", option_right="C", strike=6510.0,
                               expiry_date="2026-09-09")
    tr.observe(contract_id=con, contract_role="chosen", candidate_id="c-boot",
               position_id="pos-boot", observation_end_utc=horizon)
    # the last sample the old process managed to take was YESTERDAY, so the
    # blackout is unambiguous whatever time of day this suite runs. A restart
    # with no blackout is not a gap and must not be reported as one.
    old = (2026, 9, 8)
    tr.record_sample(candidate_id="c-boot", contract_id=con,
                     contract_role="chosen", provider="yfinance",
                     feed="yahoo option chain", provider_at_utc=u(14, 0, day=old),
                     received_at_utc=u(14, 0, day=old), requested_at_utc=None,
                     bid=4.0, ask=4.2, bid_size=1, ask_size=1,
                     underlying_price=6510.0, underlying_at_utc=u(14, 0, day=old),
                     price_basis="quote_mid", is_model=False, quality_flags=[],
                     observation_end_utc=horizon)
    tr.save_observations()
    tr.drain_once()

    # the process dies here. A new one comes up with the same volume.
    fresh()
    live = {o["contract_id"]: o for o in tr.observations()}
    check("W06j the observation registry survives a restart", con in live,
          str(sorted(live)))
    check("W06j the resumed observation keeps the SAME common horizon",
          live.get(con, {}).get("observation_end_utc") == horizon,
          repr(live.get(con, {}).get("observation_end_utc")))
    tr.drain_once()
    gaps = [r for r in tr.read_records("health")
            if r.get("observation_gap_start")]
    check("W06j the downtime is recorded as a GAP with a reason",
          any((g.get("gap_reason") or "") for g in gaps), str(gaps[:2]))
    check("W06j the gap reason names the restart",
          any("restart" in (g.get("gap_reason") or "") for g in gaps),
          str([g.get("gap_reason") for g in gaps][:3]))
    check("W06j a gap downgrades completeness rather than being averaged away",
          tr.coverage().get("completeness") != "complete",
          repr(tr.coverage().get("completeness")))

    # ...and the other half of the same rule. A restart with no blackout is
    # NOT a gap. A completeness word that says degraded on every boot is a
    # word nobody reads, which is the same failure as not recording gaps.
    clean()
    horizon2 = tr.common_horizon_utc(date(2026, 9, 9))
    c2 = tr.register_contract(underlying="SPY", option_right="C", strike=651.0,
                              expiry_date="2026-09-09")
    tr.observe(contract_id=c2, contract_role="chosen", candidate_id="c-clean",
               position_id="pos-clean", observation_end_utc=horizon2,
               underlying="SPY", right="C", strike=651.0,
               expiry_date="2026-09-09")
    tr.record_sample(candidate_id="c-clean", contract_id=c2,
                     contract_role="chosen", provider="yfinance",
                     feed="yahoo option chain",
                     provider_at_utc=tr._utc_iso(),
                     received_at_utc=tr._utc_iso(), requested_at_utc=None,
                     bid=4.0, ask=4.2, bid_size=1, ask_size=1,
                     underlying_price=651.0, underlying_at_utc=tr._utc_iso(),
                     price_basis="quote_mid", is_model=False, quality_flags=[],
                     observation_end_utc=horizon2)
    tr.save_observations()
    tr.drain_once()
    fresh()
    tr.drain_once()
    fake = [r for r in tr.read_records("health") if r.get("observation_gap_start")]
    check("W06j a restart with no blackout reports NO gap", not fake,
          str([g.get("gap_reason") for g in fake][:2]))

    # a restart must not make the record grow. Duplicated metadata and a
    # re-opened bar are two ways a reboot turns into evidence, and the counts
    # this study runs on are counts of rows.
    clean()
    base = dict(strategy_id="momentum", strategy_version="1",
                policy_hash="p" * 8, source_commit="4a8b8bc",
                deployment_id="dep-test", session_date="2026-09-09",
                input_bar_end_utc=u(13, 50), input_feed="yfinance 5m",
                gate_passed=False, reject_codes=["no_setup"], selected=False,
                position_id=None)
    tr.register_contract(underlying="SPY", option_right="C", strike=651.0,
                         expiry_date="2026-09-09")
    first = tr.record_candidate(symbol="SPY", direction="call",
                                decision_at_utc=u(13, 50),
                                observed_at_utc=u(13, 50),
                                input_values={"mom_pct": 0.42}, **base)
    tr.drain_once()
    # a REAL restart: a new process on the same volume, with every row the old
    # life wrote still sitting there
    tr._reset_for_test(keep_files=True)
    tr.register_contract(underlying="SPY", option_right="C", strike=651.0,
                         expiry_date="2026-09-09")
    again = tr.record_candidate(symbol="SPY", direction="call",
                                decision_at_utc=u(13, 50, 15),
                                observed_at_utc=u(13, 50, 15),
                                input_values={"mom_pct": 0.42}, **base)
    tr.drain_once()
    check("W06j a restart does not duplicate contract metadata",
          len(tr.read_records("contracts")) == 1,
          str(len(tr.read_records("contracts"))))
    check("W06j a restart does not re-open a bar already recorded",
          again == first and len(tr.read_records("candidates")) == 1,
          f"{first} vs {again}, "
          f"{len(tr.read_records('candidates'))} rows")
else:
    for n in ("W06j the observation registry survives a restart",
              "W06j the resumed observation keeps the SAME common horizon",
              "W06j the downtime is recorded as a GAP with a reason",
              "W06j the gap reason names the restart",
              "W06j a gap downgrades completeness rather than being averaged away",
              "W06j a restart with no blackout reports NO gap",
              "W06j a restart does not duplicate contract metadata",
              "W06j a restart does not re-open a bar already recorded"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06k. the candidate unit: one completed input bar, not one poll ---")

if clean():
    base = dict(strategy_id="momentum", strategy_version="1",
                policy_hash="p" * 8, source_commit="4a8b8bc",
                deployment_id="dep-test", session_date="2026-09-09",
                input_bar_end_utc=u(13, 50), input_feed="yfinance 5m",
                gate_passed=False, reject_codes=["no_setup"], selected=False,
                position_id=None)
    ids = []
    for i in range(4):   # four 15 second rechecks of ONE unchanged bar
        ids.append(tr.record_candidate(
            symbol="SPX", direction="call", decision_at_utc=u(13, 50, i * 15),
            observed_at_utc=u(13, 50, i * 15),
            input_values={"mom_pct": 0.42, "spot": 6510.0}, **base))
        tr.record_scan_evaluation("momentum", "SPX")
    tr.drain_once()
    rows = [r for r in tr.read_records("candidates")
            if r.get("input_bar_end_utc") == u(13, 50)]
    check("W06k four rechecks of one unchanged bar are ONE candidate",
          len({r["candidate_id"] for r in rows}) == 1,
          str({r["candidate_id"] for r in rows}))
    check("W06k and are written once, not four times", len(rows) == 1,
          f"{len(rows)} rows")
    cov = tr.coverage()
    check("W06k scan evaluations are counted separately from candidates",
          (cov.get("scan_evaluations") or 0) >= 4
          and (cov.get("candidate_opportunities") or 0) == 1,
          f"evals={cov.get('scan_evaluations')} "
          f"cands={cov.get('candidate_opportunities')}")
    # a MEANINGFUL revision of the decision inputs is a new revision of the
    # same opportunity, not a silent overwrite and not a new opportunity
    tr.record_candidate(symbol="SPX", direction="call",
                        decision_at_utc=u(13, 51),
                        observed_at_utc=u(13, 51),
                        input_values={"mom_pct": 0.91, "spot": 6512.0}, **base)
    tr.drain_once()
    rows = [r for r in tr.read_records("candidates")
            if r.get("input_bar_end_utc") == u(13, 50)]
    check("W06k a revised decision input is a REVISION of the same candidate",
          len(rows) == 2 and {r.get("revision") for r in rows} == {0, 1},
          str([(r.get("candidate_id"), r.get("revision")) for r in rows]))

    # cluster_id: SPX and SPY co-fire in the same minute and are ONE unit
    spx = tr.record_candidate(symbol="SPX", direction="call",
                              decision_at_utc=u(13, 55),
                              observed_at_utc=u(13, 55),
                              input_values={"spot": 6510.0},
                              **dict(base, input_bar_end_utc=u(13, 55)))
    spy = tr.record_candidate(symbol="SPY", direction="call",
                              decision_at_utc=u(13, 55),
                              observed_at_utc=u(13, 55),
                              input_values={"spot": 651.0},
                              **dict(base, input_bar_end_utc=u(13, 55)))
    other = tr.record_candidate(symbol="TSLA", direction="put",
                                decision_at_utc=u(13, 55),
                                observed_at_utc=u(13, 55),
                                input_values={"spot": 410.0},
                                **dict(base, input_bar_end_utc=u(13, 55)))
    tr.drain_once()
    byid = {r["candidate_id"]: r for r in tr.read_records("candidates")}
    check("W06k SPX and SPY on the same bar share one cluster_id",
          byid.get(spx, {}).get("cluster_id")
          == byid.get(spy, {}).get("cluster_id") != None,
          f"{byid.get(spx, {}).get('cluster_id')} vs "
          f"{byid.get(spy, {}).get('cluster_id')}")
    check("W06k the opposite direction is a DIFFERENT cluster",
          byid.get(other, {}).get("cluster_id")
          != byid.get(spx, {}).get("cluster_id"))
    check("W06k a rejected candidate is recorded with its reject codes",
          all(r.get("reject_codes") for r in byid.values()
              if not r.get("gate_passed")),
          "a reject with no code is a reject nobody can count")
else:
    for n in ("W06k four rechecks of one unchanged bar are ONE candidate",
              "W06k and are written once, not four times",
              "W06k scan evaluations are counted separately from candidates",
              "W06k a revised decision input is a REVISION of the same candidate",
              "W06k SPX and SPY on the same bar share one cluster_id",
              "W06k the opposite direction is a DIFFERENT cluster",
              "W06k a rejected candidate is recorded with its reject codes"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06l. contract metadata: the clock the market keeps, not 16:00 ---")

if clean():
    half = date(2026, 11, 27)   # the Friday after Thanksgiving 2026
    check("W06l the calendar agrees that day is a half day",
          market_calendar.session_close(half) == time(13, 0),
          str(market_calendar.session_close(half)))
    cid = tr.register_contract(underlying="SPX", option_right="C", strike=7000.0,
                               expiry_date=half.isoformat())
    tr.drain_once()
    row = [r for r in tr.read_records("contracts") if r.get("contract_id") == cid]
    row = row[0] if row else {}
    check("W06l last_trading_at_utc is separate from expiry_date",
          row.get("last_trading_at_utc") and row.get("expiry_date")
          and row["last_trading_at_utc"][:10] == row["expiry_date"],
          f"{row.get('last_trading_at_utc')} / {row.get('expiry_date')}")
    check("W06l a half day dies at 13:00 ET, not 16:00",
          "18:00" in (row.get("last_trading_at_utc") or ""),
          repr(row.get("last_trading_at_utc")))
    check("W06l settlement is recorded separately from last trading",
          "settlement_at_utc" in row, str(sorted(row)))
    check("W06l the index contract is cash settled and European",
          row.get("settlement_style") == "cash"
          and row.get("exercise_style") == "european",
          f"{row.get('settlement_style')} {row.get('exercise_style')}")
    spy = tr.register_contract(underlying="SPY", option_right="C", strike=651.0,
                               expiry_date="2026-09-09")
    tr.drain_once()
    srow = [r for r in tr.read_records("contracts") if r.get("contract_id") == spy]
    srow = srow[0] if srow else {}
    check("W06l an ETF contract is physically settled and American",
          srow.get("settlement_style") == "physical"
          and srow.get("exercise_style") == "american",
          f"{srow.get('settlement_style')} {srow.get('exercise_style')}")
    check("W06l metadata is stored once and referenced, not repeated per tick",
          len([r for r in tr.read_records("contracts")
               if r.get("contract_id") == spy]) == 1)
    check("W06l the metadata names its own source and read time",
          bool(srow.get("metadata_source")) and bool(srow.get("metadata_at_utc")))
else:
    for n in ("W06l the calendar agrees that day is a half day",
              "W06l last_trading_at_utc is separate from expiry_date",
              "W06l a half day dies at 13:00 ET, not 16:00",
              "W06l settlement is recorded separately from last trading",
              "W06l the index contract is cash settled and European",
              "W06l an ETF contract is physically settled and American",
              "W06l metadata is stored once and referenced, not repeated per tick",
              "W06l the metadata names its own source and read time"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06m. one batched read covers every contract under observation ---")

if clean():
    horizon = tr.common_horizon_utc(date(2026, 9, 9))
    grid = [6490.0, 6500.0, 6510.0, 6520.0, 6530.0]
    chosen = tr.register_contract(underlying="SPX", option_right="C",
                                  strike=6510.0, expiry_date="2026-09-09")
    opp = tr.register_contract(underlying="SPX", option_right="P",
                               strike=6500.0, expiry_date="2026-09-09")
    tr.observe(contract_id=chosen, contract_role="chosen", candidate_id="c-b",
               position_id="pos-b", observation_end_utc=horizon)
    tr.observe(contract_id=opp, contract_role="direction_control",
               candidate_id="c-b", position_id="pos-b",
               observation_end_utc=horizon)

    calls = []

    def chain(underlying, expiry_date, now=None):
        calls.append((underlying, expiry_date))
        return {"provider": "yfinance", "feed": "yahoo option chain",
                "provider_at_utc": u(14, 0), "received_at_utc": u(14, 0, 2),
                "requested_at_utc": u(14, 0, 1), "underlying_price": 6510.0,
                "underlying_at_utc": u(14, 0),
                "strikes": {"C": grid, "P": grid},
                "rows": {("C", 6510.0): {"bid": 4.1, "ask": 4.3,
                                         "bid_size": 3, "ask_size": 4},
                         ("P", 6500.0): {"bid": 3.1, "ask": 3.3,
                                         "bid_size": 2, "ask_size": 2}}}

    got = tr.sample_once(chain, now=datetime(2026, 9, 9, 14, 0, tzinfo=UTC))
    tr.drain_once()
    check("W06m two contracts on one expiry cost ONE chain read",
          len(calls) == 1, str(calls))
    check("W06m the batched read produced a sample for each contract",
          got >= 2, str(got))
    rows = tr.read_records("samples")
    check("W06m every batched sample carries the common observation end",
          rows and all(r.get("observation_end_utc") == horizon for r in rows))

    # a contract the chain has no row for is a recorded gap, not an absence
    def thin(underlying, expiry_date, now=None):
        return {"provider": "yfinance", "feed": "yahoo option chain",
                "provider_at_utc": u(14, 1), "received_at_utc": u(14, 1, 2),
                "requested_at_utc": u(14, 1, 1), "underlying_price": 6510.0,
                "underlying_at_utc": u(14, 1),
                "strikes": {"C": grid, "P": grid},
                "rows": {("C", 6510.0): {"bid": 4.2, "ask": 4.4}}}

    tr.sample_once(thin, now=datetime(2026, 9, 9, 14, 1, tzinfo=UTC))
    tr.drain_once()
    missing = [r for r in tr.read_records("samples")
               if r.get("contract_id") == opp and r.get("bid") is None]
    check("W06m a contract with no chain row is recorded as missing, with a "
          "reason", missing and all(m.get("missing_reason") for m in missing),
          str(missing[:1]))

    # a pending control is resolved from the grid this read already paid for,
    # and how it was chosen survives the observation being retired
    tr.request_controls(candidate_id="c-b", position_id="pos-b",
                        underlying="SPX", right="C", strike=6510.0,
                        spot=6505.0, expiry_date="2026-09-09",
                        observation_end_utc=horizon,
                        decision_at_utc=u(13, 55))
    tr.sample_once(chain, now=datetime(2026, 9, 9, 14, 2, tzinfo=UTC))
    tr.drain_once()
    check("W06m the controls cost no extra chain read of their own",
          len(calls) == 2, f"{len(calls)} chain reads for two sampler passes "
                           "plus a control resolution")
    picked = [e for e in tr.read_records("events")
              if e.get("event_type") == "control_selected"]
    check("W06m each resolved control writes a durable selection row",
          len(picked) == 2, str(len(picked)))
    check("W06m the selection row names the rule, the decision time and the "
          "grid it read",
          picked and all(p.get("trigger_basis") and p.get("decision_at_utc")
                         and p.get("grid_read_at_utc") for p in picked),
          str(picked[:1]))
    check("W06m the selection row says the rule reads no price",
          picked and all(p.get("selection_is_price_blind") for p in picked))

    # after the common horizon the observation retires, and says it did
    tr.sample_once(chain, now=datetime(2026, 9, 10, 14, 0, tzinfo=UTC))
    tr.drain_once()
    check("W06m an observation past its common horizon retires",
          not tr.observations(), str(tr.observations()))
    check("W06m the selection provenance OUTLIVES the retired observation",
          len([e for e in tr.read_records("events")
               if e.get("event_type") == "control_selected"]) == 2,
          "a retired observation must not take how it was chosen with it")

    # a provider that raises is a counted provider error, never an exception
    # travelling back into the caller
    def broken(underlying, expiry_date, now=None):
        raise RuntimeError("chain read failed")

    tr.observe(contract_id=chosen, contract_role="chosen", candidate_id="c-b",
               position_id="pos-b", observation_end_utc=horizon)
    try:
        tr.sample_once(broken, now=datetime(2026, 9, 9, 14, 2, tzinfo=UTC))
        raised = None
    except Exception as e:                                     # noqa: BLE001
        raised = e
    tr.drain_once()
    check("W06m a failing chain read never raises at the caller", raised is None,
          repr(raised))
    check("W06m a failing chain read is counted as a provider error",
          (tr.coverage().get("provider_errors") or 0) >= 1,
          str(tr.coverage()))
else:
    for n in ("W06m two contracts on one expiry cost ONE chain read",
              "W06m the batched read produced a sample for each contract",
              "W06m every batched sample carries the common observation end",
              "W06m a contract with no chain row is recorded as missing, with a "
              "reason",
              "W06m the controls cost no extra chain read of their own",
              "W06m each resolved control writes a durable selection row",
              "W06m the selection row names the rule, the decision time and "
              "the grid it read",
              "W06m the selection row says the rule reads no price",
              "W06m an observation past its common horizon retires",
              "W06m the selection provenance OUTLIVES the retired observation",
              "W06m a failing chain read never raises at the caller",
              "W06m a failing chain read is counted as a provider error"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06n. capacity is estimated from measured bytes, not assumed ---")

if clean():
    con = tr.register_contract(underlying="SPY", option_right="C", strike=651.0,
                               expiry_date="2026-09-09")
    for i in range(40):
        tr.record_sample(candidate_id="c-cap", contract_id=con,
                         contract_role="chosen", provider="yfinance",
                         feed="yahoo option chain",
                         provider_at_utc=u(14, i % 60), received_at_utc=u(14, i % 60),
                         requested_at_utc=None, bid=1.0, ask=1.1, bid_size=1,
                         ask_size=1, underlying_price=651.0,
                         underlying_at_utc=u(14, i % 60), price_basis="quote_mid",
                         is_model=False, quality_flags=[],
                         observation_end_utc=u(20, 0))
    tr.drain_once()
    est = tr.capacity_estimate()
    check("W06n the estimate is built from MEASURED bytes per sample",
          (est.get("measured_bytes_per_sample") or 0) > 0, str(est))
    for k in ("contracts_per_session", "samples_per_contract_per_session",
              "retention_days", "estimated_bytes_per_session",
              "estimated_bytes_at_retention", "volume_bytes_total"):
        check(f"W06n the estimate declares {k}", k in est, str(sorted(est)))
    check("W06n the estimate is arithmetic, not a typed in number",
          abs((est.get("estimated_bytes_per_session") or 0)
              - (est.get("measured_bytes_per_sample") or 0)
              * (est.get("contracts_per_session") or 0)
              * (est.get("samples_per_contract_per_session") or 0)) < 1,
          str(est))
    cov = tr.coverage()
    check("W06n bytes_used is a real measurement of the recorder directory",
          (cov.get("bytes_used") or 0) > 0, str(cov.get("bytes_used")))
else:
    for n in ("W06n the estimate is built from MEASURED bytes per sample",
              "W06n the estimate declares contracts_per_session",
              "W06n the estimate declares samples_per_contract_per_session",
              "W06n the estimate declares retention_days",
              "W06n the estimate declares estimated_bytes_per_session",
              "W06n the estimate declares estimated_bytes_at_retention",
              "W06n the estimate declares volume_bytes_total",
              "W06n the estimate is arithmetic, not a typed in number",
              "W06n bytes_used is a real measurement of the recorder directory"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06o. no recipient identifier and no secret ever enters a "
      "record ---")

if clean():
    tr.record_event(position_id="pos-priv", event_type="stop",
                    trigger_at_utc=u(15, 0), trigger_sample_id=None,
                    trigger_basis="quote_mid", trigger_threshold=-90.0,
                    mark_used=0.4, mark_source="live quote", leg_quantity=1,
                    deliveries=[{"delivery_id": "dl-9",
                                 "recipient_ref": "ab12cd34ef56",
                                 "recipient_index": 0,
                                 "send_attempt_at_utc": u(15, 0, 1),
                                 "delivery_status": "unknown",
                                 "provider_message_id": None,
                                 "acknowledged_at_utc": None}])
    tr.drain_once()
    blob = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted((TMP / "recorder").glob("*"))
        if p.is_file())
    check("W06o no telegram style chat id appears in any recorder file",
          not re.search(r"\b-?\d{9,12}\b", blob), "a raw recipient id")
    check("W06o no api key shape appears in any recorder file",
          "sk-ant-" not in blob and not re.search(r"\d{9,10}:[A-Za-z0-9_-]{30,}",
                                                  blob))
    ev = tr.read_records("events")[-1]
    d = (ev.get("deliveries") or [{}])[0]
    check("W06o a delivery keeps an index and an opaque ref only",
          d.get("recipient_index") == 0 and d.get("recipient_ref") == "ab12cd34ef56"
          and "chat_id" not in d, str(d))
    check("W06o an unknown delivery stays unknown, never resolved by guessing",
          d.get("delivery_status") == "unknown"
          and d.get("acknowledged_at_utc") is None, str(d))
else:
    for n in ("W06o no telegram style chat id appears in any recorder file",
              "W06o no api key shape appears in any recorder file",
              "W06o a delivery keeps an index and an opaque ref only",
              "W06o an unknown delivery stays unknown, never resolved by guessing"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06p. the scanner actually calls it, on the paths that matter ---")

scanner_src = (REPO / "scanner.py").read_text(encoding="utf-8")
check("W06p scanner imports the recorder", "import trade_recorder" in scanner_src)
check("W06p the entry path records its decision",
      "record_candidate" in scanner_src)
check("W06p the monitoring path hands over the observation it already took",
      "record_sample" in scanner_src or "sample_from_monitor" in scanner_src)
check("W06p exits are recorded as evidence", "record_event" in scanner_src)
check("W06p the health record is written with the ownership state",
      "record_health" in scanner_src)

# Constraint: do NOT add a synchronous chain read to the monitoring path. The
# controls are sampled on the recorder's own thread; monitor_one hands over
# what it already fetched and nothing more.
if tr is not None:
    try:
        mod = ast.parse(scanner_src)
    except SyntaxError as e:                                   # noqa: BLE001
        mod = None
        note(f"scanner.py did not parse: {e}")
    calls = []
    if mod is not None:
        for node in ast.walk(mod):
            # the helpers count too. Putting the chain read one call deeper
            # than the loop is still putting it in front of a live stop.
            if (isinstance(node, ast.FunctionDef)
                    and node.name in ("monitor_one", "monitor_positions",
                                      "_record_monitor_sample",
                                      "_record_exit_events")):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        f = sub.func
                        name = getattr(f, "attr", getattr(f, "id", ""))
                        mod_name = getattr(getattr(f, "value", None), "id", "")
                        calls.append(f"{mod_name}.{name}" if mod_name else name)
    banned = [c for c in calls
              if c in ("trade_recorder.sample_once", "trade_recorder.drain_once",
                       "trade_recorder.flush", "trade_recorder.stop")]
    check("W06p the monitoring path never runs the sampler or the writer inline",
          not banned, str(banned))
    check("W06p the monitoring path calls the recorder at most through the "
          "non blocking producer",
          any(c.startswith("trade_recorder.") for c in calls), str(calls[:8]))
else:
    check("W06p the monitoring path never runs the sampler or the writer inline",
          False, "no trade_recorder.py")
    check("W06p the monitoring path calls the recorder at most through the "
          "non blocking producer", False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06r. END TO END: a real entry, a real exit, and a path that "
      "outlives it ---")

# The string checks above prove the calls are written. This proves they run,
# through scanner's own open_position and monitor_one, with no network: the
# chain, the news desk, the scoreboard and the card builders are stubbed and
# nothing is ever sent.
if clean():
    import scanner                                            # noqa: E402
    import positions as poslib                                # noqa: E402

    _real = {"nearest": scanner.quotes.nearest_listed_expiry,
             "quote": scanner.quotes.get_option_quote,
             "est": scanner.quotes.estimate_premium,
             "earn": scanner.news.earnings_inside,
             "hot": scanner.news.hot_headlines,
             "stats": scanner.scoreboard.stats_for_card,
             "card": scanner.cards.entry_card}

    class _Q:
        def __init__(self, bid, ask):
            self.bid, self.ask = bid, ask
            self.mid = round((bid + ask) / 2, 2)
            self.source = "live quote (may be ~15 min delayed)"
            self.is_estimate = False
            self.last_trade_at_utc = None

    _px = {"bid": 3.95, "ask": 4.05}
    scanner.quotes.nearest_listed_expiry = lambda t, d: d
    scanner.quotes.get_option_quote = lambda *a, **k: _Q(_px["bid"], _px["ask"])
    scanner.quotes.estimate_premium = lambda *a, **k: 4.00
    scanner.news.earnings_inside = lambda *a, **k: (False, None)
    scanner.news.hot_headlines = lambda *a, **k: []
    scanner.scoreboard.stats_for_card = lambda *a, **k: None
    scanner.cards.entry_card = lambda *a, **k: "ENTRY CARD"

    class _Setup:
        ticker = "SPY"
        direction = "call"
        strike = 500.0
        spot = 499.0
        mom_pct = 0.42
        reason = "test"

    try:
        svc = scanner.Service()
        svc.dry = False
        svc.notify = lambda text: []
        svc.notify_intent = lambda text, intent: []
        svc._hb_owner = lambda text: None
        svc.sigma = lambda t: 0.20
        svc.current_mode = lambda: ("green", "")
        svc.feed.latest_price = lambda s: 499.0
        svc.get_bars = lambda s, n: None
        now = datetime(2026, 9, 9, 14, 0, tzinfo=UTC).astimezone(scanner.ET)
        opened = svc.open_position(_Setup(), now, bar_end=u(13, 50),
                                   gate_values={"mom_pct": 0.42})
        tr.drain_once()
        cands = [r for r in tr.read_records("candidates") if r.get("selected")]
        check("W06r the accepted entry recorded a selected candidate",
              opened and len(cands) == 1, f"opened={opened} rows={len(cands)}")
        check("W06r the selected candidate names its position",
              cands and cands[0].get("position_id"), str(cands[:1]))
        check("W06r the selected candidate carries a real policy hash and "
              "commit",
              cands and cands[0].get("policy_hash")
              and cands[0].get("source_commit"),
              str({k: cands[0].get(k) for k in ("policy_hash", "source_commit")}
                  if cands else {}))
        check("W06r the entry registered a contract",
              len(tr.read_records("contracts")) == 1,
              str(len(tr.read_records("contracts"))))
        obs = tr.observations()
        check("W06r the chosen contract is under observation to a common "
              "horizon",
              len(obs) == 1 and obs[0].get("observation_end_utc"), str(obs))
        check("W06r the controls are queued for the next chain read, not for "
              "an extra one now",
              len(tr.pending_controls()) == 1, str(tr.pending_controls()))

        pos = [p for p in svc.book.positions if p.ticker == "SPY"][-1]
        # walk the price up past the half, so a real exit fires
        _px["bid"], _px["ask"] = 5.20, 5.30
        svc.monitor_one(pos, now.replace(hour=10, minute=5))
        tr.drain_once()
        evs = tr.read_records("events")
        legs = {e.get("event_type") for e in evs}
        check("W06r the entry and the exit are both recorded as evidence",
              "entry" in legs and "sell_half" in legs, str(sorted(legs)))
        half = [e for e in evs if e.get("event_type") == "sell_half"]
        check("W06r the exit points at the exact sample that fired it",
              half and half[0].get("trigger_sample_id"), str(half[:1]))
        check("W06r the exit records the threshold it was measured against",
              half and half[0].get("trigger_threshold") is not None,
              str(half[0].get("trigger_threshold")) if half else "")

        # THE A15 CASE, end to end. Close the position and keep polling: the
        # path has to keep arriving, and it has to reach a higher mark than
        # the exit did.
        exit_mark = half[0].get("mark_used") if half else 0
        pos.state = "closed"
        pos.final_pnl_pct = 30.0
        for hh, mm, px in ((10, 30, 6.50), (11, 0, 8.20), (11, 30, 9.90)):
            _px["bid"], _px["ask"] = px - 0.05, px + 0.05
            svc.monitor_one(pos, now.replace(hour=hh, minute=mm))
        tr.drain_once()
        after = [s for s in tr.read_records("samples")
                 if "after_position_close" in (s.get("quality_flags") or [])]
        check("W06r the chosen path keeps being recorded after the trade "
              "closes", len(after) >= 3, f"{len(after)} post close samples")
        summ = tr.path_summary(obs[0]["candidate_id"], obs[0]["contract_id"])
        check("W06r the observed maximum is well past the exit mark",
              (summ.get("observed_max_mark") or 0) > (exit_mark or 0) + 1.0,
              f"max {summ.get('observed_max_mark')} vs exit {exit_mark}")
        note("this is A15 in one line: the 0.4R exit no longer censors the "
             "record, so a larger target can finally be argued about")

        # a dry run reads the same bars and reaches the same decisions, so if
        # it wrote it would put a second copy of every row into the one record
        # the study counts rows in
        before = (len(tr.read_records("candidates")),
                  len(tr.read_records("samples")),
                  len(tr.read_records("events")))
        dry = scanner.Service(dry_run=True)
        dry.sigma = lambda t: 0.20
        dry.current_mode = lambda: ("green", "")
        dry.feed.latest_price = lambda s: 499.0
        dry.get_bars = lambda s, n: None
        dry.open_position(_Setup(), now, bar_end=u(13, 50),
                          gate_values={"mom_pct": 0.42})
        dpos = [p for p in dry.book.positions if p.ticker == "SPY"]
        if dpos:
            dry.monitor_one(dpos[-1], now.replace(hour=10, minute=5))
        tr.drain_once()
        after_dry = (len(tr.read_records("candidates")),
                     len(tr.read_records("samples")),
                     len(tr.read_records("events")))
        check("W06r a dry run writes nothing into the shared record",
              before == after_dry, f"{before} -> {after_dry}")
    finally:
        scanner.quotes.nearest_listed_expiry = _real["nearest"]
        scanner.quotes.get_option_quote = _real["quote"]
        scanner.quotes.estimate_premium = _real["est"]
        scanner.news.earnings_inside = _real["earn"]
        scanner.news.hot_headlines = _real["hot"]
        scanner.scoreboard.stats_for_card = _real["stats"]
        scanner.cards.entry_card = _real["card"]
else:
    for n in ("W06r the accepted entry recorded a selected candidate",
              "W06r the selected candidate names its position",
              "W06r the selected candidate carries a real policy hash and commit",
              "W06r the entry registered a contract",
              "W06r the chosen contract is under observation to a common horizon",
              "W06r the controls are queued for the next chain read, not for "
              "an extra one now",
              "W06r the entry and the exit are both recorded as evidence",
              "W06r the exit points at the exact sample that fired it",
              "W06r the exit records the threshold it was measured against",
              "W06r the chosen path keeps being recorded after the trade closes",
              "W06r the observed maximum is well past the exit mark",
              "W06r a dry run writes nothing into the shared record"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06s. the real chain reader hands the sampler the shape it "
      "expects ---")

# quotes.chain_snapshot is the only piece of this package that talks to a
# provider, so it is the only piece the cases above cannot reach. A shape
# mismatch between it and sample_once would show up nowhere until a live
# session, which is exactly the class of defect this package exists to stop.
if clean():
    import pandas as pd                                       # noqa: E402
    import quotes                                             # noqa: E402

    class _Chain:
        def __init__(self):
            rows = {"strike": [6500.0, 6510.0], "bid": [3.1, 4.1],
                    "ask": [3.3, 4.3], "lastPrice": [3.2, 4.2],
                    "lastTradeDate": [pd.Timestamp("2026-09-09T13:59:11Z"),
                                      pd.Timestamp("2026-09-09T13:59:44Z")]}
            self.calls = pd.DataFrame(rows)
            self.puts = pd.DataFrame(rows)

    class _Ticker:
        def __init__(self, sym):
            self.sym = sym

        def option_chain(self, exp):
            return _Chain()

    real_ticker = quotes.yf.Ticker
    quotes.yf.Ticker = _Ticker
    try:
        snap = quotes.chain_snapshot("SPX", "2026-09-09")
    finally:
        quotes.yf.Ticker = real_ticker

    check("W06s the snapshot names its provider and feed",
          snap.get("provider") and snap.get("feed"), str(snap.get("provider")))
    check("W06s the snapshot has both a requested and a received clock",
          snap.get("requested_at_utc") and snap.get("received_at_utc"),
          str({k: snap.get(k) for k in ("requested_at_utc", "received_at_utc")}))
    check("W06s the provider clock is NULL with a reason, never the receive "
          "clock",
          snap.get("provider_at_utc") is None and snap.get("missing_reason"),
          str(snap.get("provider_at_utc")))
    check("W06s strikes are keyed by right, the shape select_controls reads",
          isinstance(snap.get("strikes"), dict)
          and set(snap["strikes"]) == {"C", "P"}
          and snap["strikes"]["C"] == [6500.0, 6510.0],
          str(snap.get("strikes"))[:120])
    check("W06s rows are keyed by right and strike, the shape sample_once reads",
          ("C", 6510.0) in (snap.get("rows") or {}),
          str(sorted((snap.get("rows") or {}))[:4]))
    cell = (snap.get("rows") or {}).get(("C", 6510.0), {})
    check("W06s a row carries bid and ask, and a null size the feed never gives",
          cell.get("bid") == 4.1 and cell.get("ask") == 4.3
          and cell.get("bid_size") is None, str(cell))
    check("W06s the last TRADE time is carried and named as a trade time",
          "last_trade_at_utc" in cell and cell["last_trade_at_utc"], str(cell))
    # and the two halves actually fit: feed the real reader's output straight
    # into the sampler
    con = tr.register_contract(underlying="SPX", option_right="C",
                               strike=6510.0, expiry_date="2026-09-09")
    tr.observe(contract_id=con, contract_role="chosen", candidate_id="c-real",
               position_id="pos-real",
               observation_end_utc=tr.common_horizon_utc(date(2026, 9, 9)))
    got = tr.sample_once(lambda u_, e_, now=None: snap,
                         now=datetime(2026, 9, 9, 14, 0, tzinfo=UTC))
    tr.drain_once()
    rows = [r for r in tr.read_records("samples")
            if r.get("candidate_id") == "c-real"]
    check("W06s the real reader's output samples cleanly through sample_once",
          got == 1 and rows and rows[0].get("bid") == 4.1, f"{got} {rows[:1]}")
else:
    for n in ("W06s the snapshot names its provider and feed",
              "W06s the snapshot has both a requested and a received clock",
              "W06s the provider clock is NULL with a reason, never the "
              "receive clock",
              "W06s strikes are keyed by right, the shape select_controls reads",
              "W06s rows are keyed by right and strike, the shape sample_once "
              "reads",
              "W06s a row carries bid and ask, and a null size the feed never "
              "gives",
              "W06s the last TRADE time is carried and named as a trade time",
              "W06s the real reader's output samples cleanly through "
              "sample_once"):
        check(n, False, "no trade_recorder.py")


# ===========================================================================
print("\n--- W06q. the gate runs this file and guards the new module ---")

improve_src = (REPO / "self_improve.py").read_text(encoding="utf-8")
check("W06q test_trade_recorder is registered in the gate",
      '"test_trade_recorder.py"' in improve_src,
      "a test the release command never runs cannot establish a fix")

import test_no_em_dash as em   # noqa: E402
check("W06q trade_recorder is in the em dash guard",
      "trade_recorder.py" in em.GUARDED, str(em.GUARDED[-3:]))
if tr is not None:
    check("W06q no em dash in any recorder string",
          not em.offenders(REPO / "trade_recorder.py"),
          str(em.offenders(REPO / "trade_recorder.py")[:2]))
else:
    check("W06q no em dash in any recorder string", False, "no trade_recorder.py")


# ===========================================================================
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All trade recorder checks passed.")
