"""Regressions for the defects found in the 2026-09-08 independent review.

Each check below reproduced a real defect before its fix. The reproductions are
kept as tests so the same mistake cannot come back.

P01  Stretch targets took their sign from a direction variable that had already
     been superseded by the sniper plan, so a BUY ticket printed 1R and 2R
     BELOW entry: levels that are losses at the moment the card is sent.

P02  The forward ledger deduplicated on (symbol, direction, date), so the first
     candidate of the day won forever. A 9:35 near miss therefore erased the
     9:50 alert that actually fired, which is why live alerts had no matching
     forward observation at their real entry.

No network, no Telegram, no model API, no production storage.

Run:  python test_review_regressions.py     (exit code 0 = all good)
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.

import json
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# an isolated data dir BEFORE config is imported, so nothing here can read or
# write the real runtime state
_TMP = tempfile.mkdtemp(prefix="kelbot_test_")
_bot_test_os.environ["DATA_DIR"] = _TMP

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# --------------------------------------------------------------------------
# P01: stretch targets must lie on the correct side of entry
# --------------------------------------------------------------------------
import market_tools

st = market_tools.stretch_targets

# the exact reproduction from the review: a BUY ticket while the earlier,
# superseded read said SELL
buy = st(100.0, 99.0, "BUY")
check("P01: a BUY ticket puts 1R and 2R ABOVE entry",
      buy == {"target_1r": 101.0, "target_2r": 102.0}, str(buy))

sell = st(100.0, 101.0, "SELL")
check("P01: a SELL ticket puts 1R and 2R BELOW entry",
      sell == {"target_1r": 99.0, "target_2r": 98.0}, str(sell))

check("P01: targets never sit on the losing side of entry",
      buy["target_1r"] > 100.0 and buy["target_2r"] > buy["target_1r"]
      and sell["target_1r"] < 100.0 and sell["target_2r"] < sell["target_1r"])

# the ticket and the direction disagreeing is exactly the confusion that
# produced the defect, so refuse rather than print something plausible
check("P01: a BUY whose stop sits above entry is refused",
      st(100.0, 101.0, "BUY") is None)
check("P01: a SELL whose stop sits below entry is refused",
      st(100.0, 99.0, "SELL") is None)
check("P01: a zero-distance stop is refused",
      st(100.0, 100.0, "BUY") is None)
for bad in (None, "", "LONG", "buy ", 5):
    check(f"P01: direction {bad!r} is refused unless it is BUY or SELL",
          st(100.0, 99.0, bad) is not None if str(bad).strip().upper() == "BUY"
          else st(100.0, 99.0, bad) is None)
check("P01: unusable numbers return None instead of raising",
      st(None, 99.0, "BUY") is None and st("x", 99.0, "BUY") is None
      and st(100.0, None, "BUY") is None)

# lowercase is accepted, because the ticket side is written in several places
check("P01: direction matching is case insensitive",
      st(100.0, 99.0, "buy") == {"target_1r": 101.0, "target_2r": 102.0})

# and the call site must hand it the SNIPER direction, not the superseded one
src = Path(__file__).with_name("market_tools.py").read_text(encoding="utf-8-sig")
call = src[src.index('ticket["direction"] = sdir'):][:400]
check("P01: the call site passes the sniper direction, not the outer one",
      "stretch_targets(ticket.get(\"entry\")" in call and "sdir)" in call, call[:200])
check("P01: the ticket now carries its own direction for downstream readers",
      'ticket["direction"] = sdir' in src)
check("P01: the old sign-from-outer-direction expression is gone",
      '_sign = 1 if direction == "BUY" else -1' not in src)

# --------------------------------------------------------------------------
# P02: an earlier rejected candidate must not erase a later real alert
# --------------------------------------------------------------------------
import config
import forward_ledger

check("P02: the test is using an isolated data dir, not production",
      str(config.DATA_DIR) == _TMP, f"{config.DATA_DIR} vs {_TMP}")

_LEDGER = Path(_TMP) / "sniper_forward.jsonl"


def _rows():
    if not _LEDGER.exists():
        return []
    return [json.loads(l) for l in
            _LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]


forward_ledger.LEDGER = _LEDGER
if _LEDGER.exists():
    _LEDGER.unlink()

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _cand(hh, mm, passes, entry, stop, reasons=(), gap=1.6):
    forward_ledger.record_candidate(
        symbol="EURUSD=X", direction="BUY", price=entry, atr=0.0012,
        ticket={"entry": entry, "stop": stop},
        conf={"grade": "A" if passes else "B"},
        passes=passes, reasons=list(reasons), gap_atr=gap, hour_et=hh,
        now_et=datetime(2026, 9, 8, hh, mm, tzinfo=ET))


# 09:35 near miss, then the 09:50 alert that actually fired
_cand(9, 35, False, 1.1000, 1.0990, ["gap 0.8 ATR, under the 1.0 floor"], gap=0.8)
_cand(9, 50, True, 1.1020, 1.1008)

rows = _rows()
passing = [r for r in rows if r.get("passes")]
rejects = [r for r in rows if not r.get("passes")]

check("P02: the 09:50 passing candidate survives the earlier 09:35 reject",
      len(passing) == 1 and passing[0].get("time_et") == "09:50:00",
      f"rows={[(r.get('time_et'), r.get('passes')) for r in rows]}")
check("P02: the deliberate 09:35 rejection is still retained",
      len(rejects) == 1 and rejects[0].get("time_et") == "09:35:00",
      f"rows={[(r.get('time_et'), r.get('passes')) for r in rows]}")
check("P02: both events are recorded, not collapsed into one",
      len(rows) == 2, f"got {len(rows)} rows")

# a retry of the SAME event must not become a second trade
before = len(_rows())
_cand(9, 50, True, 1.1020, 1.1008)
check("P02: re-recording the same event does not create a duplicate",
      len(_rows()) == before, f"{before} -> {len(_rows())}")

# a DIFFERENT passing look later the same day must not become a second entry
# either: the validated shape is one accepted trade per symbol per day
_cand(11, 15, True, 1.1050, 1.1038)
check("P02: a second accepted entry the same day is still refused",
      len([r for r in _rows() if r.get("passes")]) == 1,
      f"passing={[r.get('time_et') for r in _rows() if r.get('passes')]}")

# but a later REJECT is still recorded, because rejects are the denominator
_cand(13, 5, False, 1.1070, 1.1058, ["day efficiency 0.9, one-way tape"])
check("P02: later rejects keep accumulating as opportunity evidence",
      len([r for r in _rows() if not r.get("passes")]) == 2,
      f"rejects={[r.get('time_et') for r in _rows() if not r.get('passes')]}")

# every recorded event needs a stable identity to link alert -> observation
ids = [r.get("event_id") for r in _rows()]
check("P02: every row carries a unique event id",
      all(ids) and len(set(ids)) == len(ids), str(ids))

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all good")
