"""Offline tests for the position lifecycle — no network, no Telegram.
Drives fake prices through positions.step() and checks every alert type,
the old-rules shadow, persistence, and the sizing math.

Run:  python test_pipeline.py     (exit code 0 = all good)
"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import cards
import config
import positions as poslib
from positions import Position, PositionBook

ET = ZoneInfo("America/New_York")
TODAY = date(2026, 6, 11)
BRACKET = {"target_pct": 15, "stop_pct": -60}
ENTRY = 4.00

# These scenarios were written against a -30% stop / +25% half; pin them so the
# test validates the exit MECHANISM regardless of how the live config is tuned.
config.STOP_PCT = -30.0
config.TP_HALF_PCT = 25.0
config.RUNNER_GIVEBACK_PCT = 40.0

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def mk_pos(expiry=TODAY, direction="call"):
    return Position(
        id="t", date=str(TODAY), time_et="09:50:00", ticker="SPX",
        direction=direction, right="C" if direction == "call" else "P",
        strike=7300.0, expiry=str(expiry), entry_mid=ENTRY,
        entry_source="quote", entry_bid=3.9, entry_ask=4.1,
        spot_at_signal=7297.0, mom_pct=0.2, risk_pct=1.0,
        correlated=False, paper=False, risk_mode="green",
        win_rate_quoted=72.0, ev_quoted=9.0)


def at(hh, mm):
    return datetime(2026, 6, 11, hh, mm, tzinfo=ET)


def mark(pct):
    return ENTRY * (1 + pct / 100)


def feed(pos, when, pct, est_pct=None, flipped=False, comparable=True):
    return poslib.step(pos, when, mark(pct), "test", est_pct, flipped, BRACKET,
                       comparable=comparable)


# --- scenario 1: half at +25, then let the runner run, exit on give-back ---
p = mk_pos()
evs = feed(p, at(10, 0), 10)
check("S1 no event at +10%", evs == [] and p.state == "open")
evs = feed(p, at(10, 5), 26)
check("S1 sell_half fires at +26%", [e["type"] for e in evs] == ["sell_half"])
check("S1 state half_sold", p.state == "half_sold")
check("S1 old shadow took +15 target", p.old_rules["status"] == "closed"
      and p.old_rules["exit_reason"] == "old target"
      and abs(p.old_rules["exit_pct"] - 26) < 0.01)
evs = feed(p, at(10, 10), 70)
check("S1 runner runs (no give-back near the peak)", evs == [])
check("S1 MFE tracked", abs(p.mfe_pct - 70) < 0.01)
evs = feed(p, at(10, 30), 28)
check("S1 runner give-back fires (peak 70 -> 28 = 42 back >= 40)",
      [e["type"] for e in evs] == ["runner_trail"])
check("S1 weighted final 0.5*26 + 0.5*28 = 27",
      p.final_pnl_pct is not None and abs(p.final_pnl_pct - 27.0) < 0.01,
      f"got {p.final_pnl_pct}")

# --- scenario 2: straight stop; old shadow keeps running after ---
p = mk_pos()
evs = feed(p, at(10, 0), -10)
check("S2 no event at -10%", evs == [])
evs = feed(p, at(10, 5), -32)
check("S2 stop fires at -32%", [e["type"] for e in evs] == ["stop"])
check("S2 closed at -32", p.state == "closed" and abs(p.final_pnl_pct + 32) < 0.01)
check("S2 old shadow still open (-32 > -60)", p.old_rules["status"] == "open")
evs = feed(p, at(11, 0), -61)
check("S2 closed pos emits no new events", evs == [])
check("S2 old shadow stops at -61", p.old_rules["status"] == "closed"
      and p.old_rules["exit_reason"] == "old stop")
check("S2 final unchanged by shadow marks",
      p.final_pnl_pct is not None and abs(p.final_pnl_pct + 32) < 0.01)

# --- scenario 3: half then stop on the remainder ---
p = mk_pos()
feed(p, at(10, 0), 27)
evs = feed(p, at(10, 20), -31)
check("S3 stop after half", [e["type"] for e in evs] == ["stop"])
check("S3 weighted final 0.5*27 + 0.5*(-31) = -2",
      p.final_pnl_pct is not None and abs(p.final_pnl_pct + 2.0) < 0.01,
      f"got {p.final_pnl_pct}")

# --- scenario 4: expiry warning then settle at the close ---
p = mk_pos()
evs = feed(p, at(15, 46), 5)
check("S4 expiry warning at 15:46", [e["type"] for e in evs] == ["expiry_warn"])
evs = feed(p, at(15, 47), 5)
check("S4 warning not repeated", evs == [])
evs = feed(p, at(16, 1), 3)
check("S4 settled at close, no extra alert", evs == [] and p.state == "closed")
check("S4 settle reason expiry close",
      p.final_exit is not None and p.final_exit["reason"] == "expiry close")
check("S4 old shadow time-stopped too", p.old_rules["status"] == "closed")

# --- scenario 5: estimate floors the stop only on non-comparable cycles ---
p = mk_pos()
evs = feed(p, at(10, 0), -20, est_pct=-31.0)
check("S5 comparable quote at -20 outranks the -31 estimate (no stop)",
      evs == [] and p.state == "open")
evs = feed(p, at(10, 5), -20, est_pct=-31.0, comparable=False)
check("S5 stop fires off the estimate once the mark is non-comparable",
      [e["type"] for e in evs] == ["stop"])
check("S5 recorded pct is the estimate (-31)",
      p.final_exit is not None and abs(p.final_exit["pct"] + 31) < 0.01)

# --- scenario 6: no trail exit before the half target ---
p = mk_pos()
evs = feed(p, at(10, 0), 10, flipped=True)
check("S6 flip before half target does nothing", evs == [] and p.state == "open")

# --- scenario 7: put direction mirrors (give-back trail) ---
p = mk_pos(direction="put")
feed(p, at(10, 0), 26)            # half at +26, peak 26
feed(p, at(10, 5), 60)            # runner runs, peak 60
evs = feed(p, at(10, 10), 18)     # 60 - 18 = 42 >= 40 -> give-back
check("S7 put runner give-back works", p.state == "closed"
      and [e["type"] for e in evs] == ["runner_trail"]
      and abs(p.final_pnl_pct - 22.0) < 0.01)

# --- scenario 8: persistence round-trip ---
test_path = config.DATA_DIR / "positions_test.json"
if test_path.exists():
    test_path.unlink()
book = PositionBook(test_path)
p = mk_pos()
feed(p, at(10, 5), 26)
book.add(p)
book2 = PositionBook(test_path)
check("S8 book reloads one position", len(book2.positions) == 1)
q = book2.positions[0]
check("S8 state survives restart", q.state == "half_sold"
      and q.half_exit["pct"] == p.half_exit["pct"]
      and q.old_rules == p.old_rules)

# --- scenario 9: position past expiry gets force-settled on reload ---
p_old = mk_pos(expiry=TODAY - timedelta(days=1))
p_old.last_mark_pct = -5.0
book2.add(p_old)
watch = book2.needs_monitoring(TODAY)
check("S9 expired position not monitored", all(x.id != p_old.id for x in watch)
      or p_old.state == "closed")
check("S9 force-settled with last known price", p_old.state == "closed"
      and "offline" in p_old.final_exit["reason"])
test_path.unlink()

# --- scenario 10: a non-comparable (estimate-only) cycle must NOT fake a give-back ---
p = mk_pos()
feed(p, at(10, 0), 26)            # half at +26 (comparable), peak 26
feed(p, at(10, 5), 80)            # comparable, peak 80
evs = feed(p, at(10, 10), 30, est_pct=30.0, comparable=False)  # cross-source read
check("S10 no give-back on a non-comparable cycle",
      evs == [] and p.state == "half_sold")
check("S10 peak not moved by a non-comparable cycle",
      p.mfe_pct is not None and abs(p.mfe_pct - 80) < 0.01)
evs = feed(p, at(10, 15), 38)     # comparable: 80-38=42 >= 40 -> give-back
check("S10 give-back fires on the next comparable cycle",
      [e["type"] for e in evs] == ["runner_trail"])

# --- scenario 11: the old-rules shadow is judged against the bracket pinned
# at entry, not whichever bracket the overnight backtest loaded today ---
p = mk_pos()
p.old_bracket = {"target_pct": 20, "stop_pct": -50}
feed(p, at(10, 0), 16)            # today's +15 target would have closed it
check("S11 pinned +20 target outranks today's +15 (shadow stays open)",
      p.old_rules["status"] == "open")
feed(p, at(10, 5), 21)
check("S11 shadow closes at the pinned +20 target",
      p.old_rules["status"] == "closed"
      and p.old_rules["exit_reason"] == "old target"
      and abs(p.old_rules["exit_pct"] - 21) < 0.01)

p = mk_pos()
p.old_bracket = {"target_pct": 20, "stop_pct": -50}
feed(p, at(10, 0), -55)           # today's -60 stop would have kept it open
check("S11 shadow stops at the pinned -50, not today's -60",
      p.old_rules["status"] == "closed"
      and p.old_rules["exit_reason"] == "old stop")

# a malformed pin (schema drift, a hand-edited positions.json) falls back to
# the caller's bracket instead of crashing the monitoring cycle
p = mk_pos()
p.old_bracket = {"target_pct": "20"}
feed(p, at(10, 0), 16)
check("S11 malformed pin falls back to today's bracket",
      p.old_rules["status"] == "closed"
      and p.old_rules["exit_reason"] == "old target")

# bool legs are ints to isinstance (True/False compare as 1/0), so a
# hand-edited {"stop_pct": false} would stop the shadow out at any red mark;
# they must count as malformed and fall back
p = mk_pos()
p.old_bracket = {"target_pct": True, "stop_pct": False}
feed(p, at(10, 0), -5)            # bool pin would read this as <= stop 0
check("S11 bool pin counts as malformed (shadow stays open at -5)",
      p.old_rules["status"] == "open")

# json.loads parses NaN into a float; both comparisons go False forever and
# the shadow could never close — malformed, fall back
p = mk_pos()
p.old_bracket = {"target_pct": float("nan"), "stop_pct": float("nan")}
feed(p, at(10, 0), 16)
check("S11 NaN pin counts as malformed (fallback +15 target closes it)",
      p.old_rules["status"] == "closed"
      and p.old_rules["exit_reason"] == "old target")

# legacy record (no pin at all) keeps the old passed-bracket behavior
p = mk_pos()
check("S11 legacy position has no pin by default", p.old_bracket is None)
feed(p, at(10, 0), 16)
check("S11 legacy position still uses today's bracket",
      p.old_rules["status"] == "closed"
      and p.old_rules["exit_reason"] == "old target")

# the pin survives a restart
pin_path = config.DATA_DIR / "positions_pin_test.json"
if pin_path.exists():
    pin_path.unlink()
pin_book = PositionBook(pin_path)
p = mk_pos()
p.old_bracket = {"target_pct": 20, "stop_pct": -50}
pin_book.add(p)
q = PositionBook(pin_path).positions[0]
check("S11 pinned bracket survives restart",
      q.old_bracket == {"target_pct": 20, "stop_pct": -50})
pin_path.unlink()

# --- sizing math ---
alloc = config.suggested_alloc_pct(1.0)
check("sizing: 1% risk / 30% stop = 3.33% of account", abs(alloc - 3.3333) < 0.01)
check("sizing: red day half risk = 1.67%",
      abs(config.suggested_alloc_pct(0.5) - 1.6667) < 0.01)

# --- cards render without crashing (no network) ---
from strategy import Setup
s = Setup(ticker="SPX", direction="call", strike=7300.0, spot=7297.0,
          mom_pct=0.21, reason="t")
p = mk_pos()
stats = {"win_rate": 56.7, "avg_win_pct": 49.0, "avg_loss_pct": -28.0,
         "expectancy_pct": 10.5, "ev_pct": 10.5, "trades": 60,
         "start": "03/17/2026", "end": "06/10/2026",
         "label": "NEW-RULES BACKTEST, approx pricing — live stats take over after 30 signals",
         "costs_note": "after est. costs", "source": "backtest_new",
         "old_win_rate": 71.7}
for mode in ("green", "yellow", "red"):
    txt = cards.entry_card(s, p, None, stats, mode, "test reason", TODAY, TODAY)
    check(f"entry card renders ({mode})", "BUY CALL" in txt and "Your call." in txt)
check("entry card shows EV headline", "EXPECTED: +10.5%" in
      cards.entry_card(s, p, None, stats, "green", "", TODAY, TODAY))
for fn, ev in ((cards.half_card, {"pct": 27.0, "source": "q"}),
               (cards.stop_card, {"pct": -31.0, "source": "q"}),
               (cards.expiry_card, {"pct": -8.0, "source": "q"})):
    check(f"{fn.__name__} renders", "Your call." in fn(p, ev))
p.half_exit = {"time": "10:05:00", "pct": 27.0, "mark": 5.08}
check("trail_card renders", "Your call." in
      cards.trail_card(p, {"pct": 18.0, "total_pct": 22.5, "source": "q"}))
for m in ("green", "yellow", "red"):
    check(f"morning card renders ({m})", "RISK MODE" in
          cards.morning_card(m, "reason here", TODAY))

# --- telegram message parsing (pure, no network) ---
import telegram as tg
auth = {"123"}
it = tg._parse_update({"message": {"chat": {"id": 123}, "text": "/risk red fomc"}}, auth)
check("tg: command parsed", it["kind"] == "command" and it["cmd"] == "/risk"
      and it["args"] == "red fomc")
it = tg._parse_update({"message": {"chat": {"id": 123}, "text": "how we lookin"}}, auth)
check("tg: plain text parsed", it["kind"] == "text" and it["text"] == "how we lookin")
it = tg._parse_update({"message": {"chat": {"id": 123}, "caption": "what u think",
                                   "photo": [{"file_id": "small"}, {"file_id": "big"}]}}, auth)
check("tg: photo takes largest size + caption",
      it["kind"] == "photo" and it["file_id"] == "big" and it["text"] == "what u think")
it = tg._parse_update({"message": {"chat": {"id": 123},
                                   "document": {"file_id": "d1", "file_name": "trades.csv",
                                                "mime_type": "text/csv"}}}, auth)
check("tg: document parsed", it["kind"] == "document" and it["file_name"] == "trades.csv")
it = tg._parse_update({"message": {"chat": {"id": 999}, "text": "/status"}}, auth)
check("tg: unauthorized chat surfaced as 'unknown' (not processed)",
      it["kind"] == "unknown" and it["chat_id"] == "999")
it = tg._parse_update({"message": {"chat": {"id": 123}, "voice": {"file_id": "v"}}}, auth)
check("tg: voice flagged unsupported", it["kind"] == "unsupported")

import assistant
check("assistant: disabled without API key (honest hint path)",
      not assistant.enabled() or bool(__import__("os").environ.get("ANTHROPIC_API_KEY")))

# --- personal W:L ledger (offline, isolated file) ---
assistant.TRADES_FILE = config.DATA_DIR / "user_trades_test.json"
if assistant.TRADES_FILE.exists():
    assistant.TRADES_FILE.unlink()
assistant.log_trade("u1", 1100, "SPX", "call, sold half +25")
assistant.log_trade("u1", -400, "QCOM")
assistant.log_trade("u2", 50)  # another user's ledger stays separate
s = assistant.score("u1")
check("ledger: 1W-1L for u1", s["wins"] == 1 and s["losses"] == 1
      and s["entries"] == 2)
check("ledger: total +700", abs(s["total_dollars"] - 700) < 0.01)
check("ledger: per-user separation", assistant.score("u2")["entries"] == 1)
check("ledger: score line renders", "1W - 1L" in assistant.score_line("u1"))
check("ledger: empty user prompt", "No trades logged yet"
      in assistant.score_line("nobody"))
tool_out = assistant._run_tool("log_trade_result",
                               {"profit_dollars": 200, "ticker": "spx"}, "u1")
check("ledger: tool path logs + uppercases ticker",
      '"SPX"' in tool_out and assistant.score("u1")["entries"] == 3)
# a $0 breakeven is a scratch, not a loss: score() used to bucket
# profit_dollars <= 0 as a loss, dragging win_rate down on flat trades.
assistant.log_trade("u1", 0, "SPX", "scratched at entry")
s = assistant.score("u1")
check("ledger: $0 trade is a scratch, not a loss",
      s["wins"] == 2 and s["losses"] == 1 and s["scratches"] == 1
      and s["entries"] == 4)
check("ledger: win rate over decided trades only (2 of 3, not 2 of 4)",
      s["win_rate_pct"] == 67)
check("ledger: score line shows the scratch",
      "2W - 1L - 1 scratch (67%)" in assistant.score_line("u1"))
s = assistant.score("u2")  # +50 only: no scratch clause in the line
check("ledger: no scratches -> no scratch clause",
      s["scratches"] == 0 and "scratch" not in assistant.score_line("u2"))
assistant.log_trade("u3", 0)  # all-scratch ledger must not divide by zero
s = assistant.score("u3")
check("ledger: all-scratch record renders at 0% without a loss",
      s["wins"] == 0 and s["losses"] == 0 and s["scratches"] == 1
      and s["win_rate_pct"] == 0 and "0W - 0L" in assistant.score_line("u3"))
assistant.TRADES_FILE.unlink()

# --- file intake: a cut-off text file must say so to the model ---
# _file_blocks used to slice at MAX_TEXT_FILE with no notice, so the brain
# answered whole-file questions (totals, last rows) from just the head,
# confidently and wrong. Now a cut file is marked at the top and the bottom.
_orig_dl = assistant.telegram.download_file
_orig_cap = assistant.MAX_TEXT_FILE
_dl_payload = {"data": b""}
try:
    # pin the cap like config.STOP_PCT above: the section tests the marking
    # MECHANISM, so a box running a BOT_MAX_TEXT_FILE override stays green
    assistant.MAX_TEXT_FILE = 20000
    assistant.telegram.download_file = (
        lambda fid, max_bytes=10 * 1024 * 1024: _dl_payload["data"])
    _doc = {"kind": "document", "file_id": "f1", "file_name": "trades.csv",
            "mime": "text/csv"}

    _dl_payload["data"] = b"date,pnl\n2026-06-11,+120\n"
    fb, err = assistant._file_blocks(_doc)
    check("files: small file renders exactly as before",
          err is None and fb[0]["text"] ==
          "[Contents of the file 'trades.csv' the user sent:]\n"
          "date,pnl\n2026-06-11,+120\n")

    _dl_payload["data"] = b"x" * assistant.MAX_TEXT_FILE
    fb, err = assistant._file_blocks(_doc)
    check("files: exactly-at-cap file is not marked truncated",
          err is None and "TRUNCATED" not in fb[0]["text"]
          and fb[0]["text"].startswith("[Contents of the file"))

    _full = ("HEAD_ROWS," + "x" * (assistant.MAX_TEXT_FILE - 10)
             + "y" * 4000 + "LAST_ROW,999")
    _dl_payload["data"] = _full.encode()
    fb, err = assistant._file_blocks(_doc)
    _t_text = fb[0]["text"]
    check("files: over-cap file keeps the head",
          err is None and "HEAD_ROWS," in _t_text)
    check("files: over-cap file drops the tail", "LAST_ROW,999" not in _t_text)
    check("files: truncation marked at top and bottom",
          _t_text.startswith(
              f"[First {assistant.MAX_TEXT_FILE:,} of {len(_full):,} ")
          and "[TRUNCATED at" in _t_text
          and "instead of guessing" in _t_text)
    check("files: body is exactly the first MAX_TEXT_FILE chars",
          _t_text.split("cut off:]\n", 1)[1].rsplit("\n[TRUNCATED", 1)[0]
          == _full[:assistant.MAX_TEXT_FILE])

    _dl_payload["data"] = b"\x00\x01"
    fb, err = assistant._file_blocks(
        {"kind": "document", "file_id": "f2", "file_name": "data.bin",
         "mime": "application/zip"})
    check("files: unknown type refused plainly (no em dash)",
          fb is None and "yet." in err and "—" not in err)
finally:
    assistant.telegram.download_file = _orig_dl
    assistant.MAX_TEXT_FILE = _orig_cap

# BOT_MAX_TEXT_FILE env knob: parsed on import, junk keeps the built-in.
# Exercised on a scratch import so this suite never depends on the box's env.
import importlib.util as _ilu
import os as _os
_orig_env_cap = _os.environ.get("BOT_MAX_TEXT_FILE")


def _fresh_cap(env_value):
    if env_value is None:
        _os.environ.pop("BOT_MAX_TEXT_FILE", None)
    else:
        _os.environ["BOT_MAX_TEXT_FILE"] = env_value
    spec = _ilu.spec_from_file_location("assistant_envknob", assistant.__file__)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MAX_TEXT_FILE


try:
    check("files: cap defaults to 20000 with no env", _fresh_cap(None) == 20000)
    check("files: BOT_MAX_TEXT_FILE raises the cap", _fresh_cap("50000") == 50000)
    check("files: junk env keeps the built-in", _fresh_cap("plenty") == 20000)
    check("files: non-positive env keeps the built-in", _fresh_cap("-5") == 20000)
finally:
    if _orig_env_cap is None:
        _os.environ.pop("BOT_MAX_TEXT_FILE", None)
    else:
        _os.environ["BOT_MAX_TEXT_FILE"] = _orig_env_cap

# --- chat brain: a computed deep answer must survive to the user ---
# respond() used to run at max_tokens=1200 (starving the always-on thinking of
# the Fable 5 brain), could burn all 5 rounds on tool calls, and threw away an
# already-computed ask_deep answer whenever the relay pass died or came back
# blank, deflecting with "came back empty".
assistant.HISTORY_FILE = config.DATA_DIR / "chat_history_test.json"
if assistant.HISTORY_FILE.exists():
    assistant.HISTORY_FILE.unlink()
_orig_post = assistant._post_anthropic
_orig_deep = assistant.deep_think
_msg = {"chat_id": "u9", "kind": "text", "text": "why did SPX rip today?"}


def _scripted(responses):
    """Fake _post_anthropic: pops scripted (body, err) pairs, records payloads."""
    seen = []

    def fake(payload, timeout):
        seen.append(payload)
        return responses.pop(0)
    return fake, seen


def _tooluse(name, args, tid="t1"):
    return ({"stop_reason": "tool_use",
             "content": [{"type": "tool_use", "id": tid, "name": name,
                          "input": args}]}, None)


def _text(t):
    return ({"stop_reason": "end_turn",
             "content": [{"type": "text", "text": t}]}, None)


def _refusal(t=""):
    """Fable 5 safety decline: HTTP 200, stop_reason 'refusal', content empty
    or holding a clipped fragment the classifier cut off."""
    content = [{"type": "text", "text": t}] if t else []
    return ({"stop_reason": "refusal", "content": content}, None)


try:
    assistant.deep_think = lambda q, context="": "THE DEEP ANSWER"

    # relay call errors AFTER ask_deep already answered: hand the answer over
    assistant._post_anthropic, seen = _scripted(
        [_tooluse("ask_deep", {"question": "why"}), (None, "529 overloaded")])
    r = assistant.respond(_msg, "ctx")
    check("brain: deep answer survives a dead relay call",
          r == "THE DEEP ANSWER", f"got {r!r}")
    check("brain: thinking headroom (max_tokens >= 4000)",
          all(p["max_tokens"] >= 4000 for p in seen),
          f"got {[p['max_tokens'] for p in seen]}")

    # relay call comes back blank: same fallback
    assistant._post_anthropic, seen = _scripted(
        [_tooluse("ask_deep", {"question": "why"}), _text("")])
    r = assistant.respond(_msg, "ctx")
    check("brain: deep answer survives a blank relay",
          r == "THE DEEP ANSWER", f"got {r!r}")

    # model keeps calling tools every round: the final pass must force text
    responses = [_tooluse("get_score", {}, tid=f"t{i}") for i in range(5)]
    responses.append(_text("final words"))
    assistant._post_anthropic, seen = _scripted(responses)
    r = assistant.respond(_msg, "ctx")
    check("brain: exhausted tool loop still ends in text",
          r == "final words", f"got {r!r}")
    check("brain: last pass forces text via tool_choice none",
          seen[-1].get("tool_choice") == {"type": "none"}
          and all("tool_choice" not in p for p in seen[:-1]),
          f"got {seen[-1].get('tool_choice')}")

    # nothing computed and a blank reply: the honest deflection remains
    assistant._post_anthropic, seen = _scripted([_text("")])
    r = assistant.respond(_msg, "ctx")
    check("brain: blank with no deep answer still deflects honestly",
          "came back empty" in r, f"got {r!r}")
    check("brain: deflection carries no em dash", "—" not in r, f"got {r!r}")

    # --- Fable 5 refusal handling: stop_reason 'refusal' is a safety ---
    # decline delivered as HTTP 200 with empty (or clipped) content. It used
    # to fall through to the "came back empty" deflection, which reads as
    # exactly the dodge the system prompt forbids.
    assistant._post_anthropic, seen = _scripted([_refusal()])
    r = assistant.respond(_msg, "ctx")
    check("brain: refusal gets an honest safety note, not a deflection",
          "safety line" in r and "came back empty" not in r, f"got {r!r}")

    # a refusal on the relay pass still cannot eat a computed deep answer
    assistant._post_anthropic, seen = _scripted(
        [_tooluse("ask_deep", {"question": "why"}), _refusal()])
    r = assistant.respond(_msg, "ctx")
    check("brain: deep answer survives a refused relay",
          r == "THE DEEP ANSWER", f"got {r!r}")

    # clipped refusal text is a cut-off thought: discard, never relay
    assistant._post_anthropic, seen = _scripted(
        [_refusal("well, the first step would be")])
    r = assistant.respond(_msg, "ctx")
    check("brain: partial refusal text is never relayed",
          "first step" not in r and "safety line" in r, f"got {r!r}")
finally:
    assistant._post_anthropic = _orig_post
    assistant.deep_think = _orig_deep
    if assistant.HISTORY_FILE.exists():
        assistant.HISTORY_FILE.unlink()
    assistant.HISTORY_FILE = config.DATA_DIR / "chat_history.json"

# --- chat brain: ask_deep escalates WITH context, not blind ---
# deep_think always accepted a context arg but _run_tool never passed it, so
# the deep brain got only the raw question: no live bot state, no learned
# playbook, no conversation. A follow-up like "what about the second one?"
# was unanswerable. respond() now assembles _deep_context and threads it in.
import json as _json
import os as _os

assistant.HISTORY_FILE = config.DATA_DIR / "chat_history_test.json"
_orig_digest = assistant.LESSONS_DIGEST
assistant.LESSONS_DIGEST = config.DATA_DIR / "lessons_digest_test.md"
try:
    # the plumbing: deep_think prepends the context block to the question
    _had_key = bool(_os.environ.get("ANTHROPIC_API_KEY", "").strip())
    if not _had_key:
        _os.environ["ANTHROPIC_API_KEY"] = "test-key"
    assistant._post_anthropic, seen = _scripted([_text("deep out")])
    out = assistant.deep_think("hard q?", context="BACKGROUND")
    check("deep ctx: deep_think prepends context to the question",
          out == "deep out" and
          seen[0]["messages"][0]["content"] == "BACKGROUND\n\nQuestion: hard q?",
          f"got {seen[0]['messages'][0]['content']!r}")

    # Fable 5 refusal on the one-shot paths: deep_think says which it was,
    # and complete/complete_deep return None so the nightly review falls back
    # down its chain instead of saving a clipped fragment as a lesson.
    assistant._post_anthropic, seen = _scripted([_refusal()])
    out = assistant.deep_think("hard q?")
    check("deep refusal: honest safety note, not 'came back empty'",
          "declined" in out and "empty" not in out, f"got {out!r}")
    assistant._post_anthropic, seen = _scripted([_refusal("clipped frag")])
    check("refusal: complete_deep returns None so callers fall back",
          assistant.complete_deep("sys", "user") is None)
    assistant._post_anthropic, seen = _scripted([_refusal("clipped frag")])
    check("refusal: complete returns None so callers fall back",
          assistant.complete("sys", "user") is None)
    if not _had_key:
        del _os.environ["ANTHROPIC_API_KEY"]

    # end to end: respond() hands ask_deep the state, playbook, and turns
    assistant.LESSONS_DIGEST.write_text(
        "- never chase a late SPX call after 10:30 CT", encoding="utf-8")
    assistant.HISTORY_FILE.write_text(_json.dumps({"u9": [
        {"role": "user", "content": "any setups today"},
        {"role": "assistant", "content": "two setups: SPX call and a QCOM put"},
    ]}), encoding="utf-8")
    captured = {}

    def _fake_deep(q, context=""):
        captured["q"], captured["context"] = q, context
        return "DEEP OUT"

    assistant.deep_think = _fake_deep
    assistant._post_anthropic, seen = _scripted(
        [_tooluse("ask_deep", {"question": "what about the second one?"}),
         _text("relayed")])
    r = assistant.respond(_msg, "book: 1 open SPX call")
    ctx = captured.get("context", "")
    check("deep ctx: question still passed through",
          r == "relayed" and captured.get("q") == "what about the second one?")
    check("deep ctx: live bot state included",
          "LIVE BOT STATE:" in ctx and "book: 1 open SPX call" in ctx, f"got {ctx!r}")
    check("deep ctx: learned playbook included",
          "WHAT I'VE LEARNED" in ctx and "never chase a late SPX call" in ctx)
    check("deep ctx: recent turns included",
          "RECENT CONVERSATION" in ctx and "SPX call and a QCOM put" in ctx)
    check("deep ctx: current message is the newest line",
          "User (the message being answered now): why did SPX rip today?" in ctx)

    # shaping: caps hold, oldest turns drop, non-text content never crashes
    ctx = assistant._deep_context(
        "state", [{"role": "user", "content": "x" * 3000}], "now?")
    check("deep ctx: long turns truncated to the cap",
          "x" * assistant.DEEP_CONTEXT_CHARS + " [...]" in ctx
          and "x" * (assistant.DEEP_CONTEXT_CHARS + 1) not in ctx)
    ctx = assistant._deep_context("state", [], "y" * 3000)
    check("deep ctx: long current message is marked cut, not silently sliced",
          "y" * assistant.DEEP_CONTEXT_CHARS + " [...]" in ctx
          and "y" * (assistant.DEEP_CONTEXT_CHARS + 1) not in ctx)
    turns = [{"role": "user", "content": f"turn T{i} here"} for i in range(10)]
    ctx = assistant._deep_context("state", turns, "")
    check("deep ctx: only the newest turns ride along",
          "turn T9 here" in ctx and "turn T4 here" in ctx
          and "turn T3 here" not in ctx)
    ctx = assistant._deep_context(
        "state", [{"role": "user", "content": ["blocks"]}], "")
    check("deep ctx: non-text history skipped without a conversation block",
          "RECENT CONVERSATION" not in ctx and "LIVE BOT STATE:" in ctx)
finally:
    assistant._post_anthropic = _orig_post
    assistant.deep_think = _orig_deep
    for _f in (assistant.HISTORY_FILE, assistant.LESSONS_DIGEST):
        if _f.exists():
            _f.unlink()
    assistant.HISTORY_FILE = config.DATA_DIR / "chat_history.json"
    assistant.LESSONS_DIGEST = _orig_digest

# --- market_tools: 15-min momentum is session-scoped, never the overnight gap ---
import pandas as pd

import forward_ledger
import market_tools as mt
import risk_gate

_orig_yf = mt.yf
_orig_ev = risk_gate.upcoming_events
_orig_rec = forward_ledger.record_candidate
# .env may carry real Alpaca keys; reads would then patch in a LIVE price and
# these offline tests would depend on the actual market. Force pure yfinance
# stubs here; the dedicated alpaca section below installs its own fake.
_orig_alpaca = mt._feed.alpaca
mt._feed.alpaca = None


class _FakeYF:
    """Stands in for yfinance: serves one daily and one 5m frame, offline."""
    def __init__(self, d1, m5):
        self.d1, self.m5 = d1, m5

    def download(self, symbol, period=None, interval=None, **kw):
        return self.d1 if interval == "1d" else self.m5


def _daily(last=105.0, n=25, end="2026-06-11"):
    idx = pd.date_range(end=end, periods=n, freq="B")
    close = [100.0] * (n - 1) + [last]
    return pd.DataFrame({"Open": close, "High": [x + 1 for x in close],
                         "Low": [x - 1 for x in close], "Close": close,
                         "Volume": [0] * n}, index=idx)


def _m5_frame(closes_yday, closes_today):
    idx = (pd.date_range("2026-06-10 15:30", periods=len(closes_yday),
                         freq="5min", tz=ET)
           .append(pd.date_range("2026-06-11 09:30", periods=len(closes_today),
                                 freq="5min", tz=ET)))
    cl = list(closes_yday) + list(closes_today)
    return pd.DataFrame({"Open": cl, "High": [x + 0.2 for x in cl],
                         "Low": [x - 0.2 for x in cl], "Close": cl,
                         "Volume": [0] * len(cl)}, index=idx)


try:
    risk_gate.upcoming_events = lambda *a, **k: []
    forward_ledger.record_candidate = lambda *a, **k: None

    # stock gapped up overnight, only 2 bars into today's session: a 3-bar
    # lookback lands on yesterday, so the old read called the gap "+5%
    # momentum" and flipped the bias. Session-scoped, momentum is simply not
    # readable yet: None, and the bias stays neutral (chop = wait).
    mt.yf = _FakeYF(_daily(), _m5_frame([100.0] * 6, [105.0, 105.0]))
    r = mt._do_read("TSLA", "TSLA", 2, "stock", "test")
    check("mom15: overnight gap not read as momentum",
          r.get("momentum_15min_pct") is None,
          f"got {r.get('momentum_15min_pct')}")
    check("mom15: bias neutral right after a gap-up open",
          r.get("bias") == "neutral", f"got {r.get('bias')}")

    # enough bars today: momentum, high and low all come from TODAY only
    mt.yf = _FakeYF(_daily(last=110.0),
                    _m5_frame([100.0] * 6,
                              [105.0, 106.0, 107.0, 108.0, 109.0, 110.0]))
    r = mt._do_read("TSLA", "TSLA", 2, "stock", "test")
    check("mom15: session-scoped value (110 vs 107 = +2.8%)",
          r.get("momentum_15min_pct") is not None
          and abs(r["momentum_15min_pct"] - 2.8) < 0.011,
          f"got {r.get('momentum_15min_pct')}")
    check("mom15: clean same-session push reads bullish",
          str(r.get("bias")).startswith("bull"), f"got {r.get('bias')}")
    check("mom15: session hi/lo from today's bars only",
          r.get("recent_session_high") is not None
          and abs(r["recent_session_high"] - 110.2) < 1e-9
          and abs(r["recent_session_low"] - 104.8) < 1e-9,
          f"got {r.get('recent_session_high')}/{r.get('recent_session_low')}")

    # --- prior_close / day_move_pct never span two sessions ---
    # normal RTH read (daily frame carries today's partial row): prior close
    # is yesterday's, and the day move is today's real move
    check("prior_close: normal RTH read uses yesterday's close",
          abs(r["prior_close"] - 100.0) < 1e-9
          and abs(r["day_move_pct"] - 10.0) < 0.011,
          f"got {r.get('prior_close')}/{r.get('day_move_pct')}")

    # pre-open / early session: yfinance hasn't posted today's daily row, so
    # iloc[-2] was TWO sessions back and the "day move" swallowed yesterday's
    # whole range (here +5% instead of the real +0.96% gap)
    mt.yf = _FakeYF(_daily(last=104.0, end="2026-06-10"),
                    _m5_frame([104.0] * 6, [105.0, 105.0]))
    r = mt._do_read("TSLA", "TSLA", 2, "stock", "test")
    check("prior_close: pre-open read steps back ONE session, not two",
          r.get("prior_close") is not None
          and abs(r["prior_close"] - 104.0) < 1e-9,
          f"got {r.get('prior_close')}")
    check("prior_close: pre-open day move is the gap only",
          r.get("day_move_pct") is not None
          and abs(r["day_move_pct"] - 0.96) < 0.011,
          f"got {r.get('day_move_pct')}")

    # 24h symbol: crypto daily rows are stamped on the UTC clock, and by 3pm
    # ET the current UTC day's PARTIAL row (~= live price) is already in the
    # frame. Sessions must be matched on the daily frame's own clock or that
    # partial row becomes the "prior close" and the day move reads ~0.
    _cl = [100.0] * 24 + [105.0]
    _d_utc = pd.DataFrame(
        {"Open": _cl, "High": [x + 1 for x in _cl],
         "Low": [x - 1 for x in _cl], "Close": _cl, "Volume": [0] * 25},
        index=pd.date_range(end="2026-06-11", periods=25, freq="D", tz="UTC"))
    _m5_utc = pd.DataFrame(
        {"Open": [105.0] * 6, "High": [105.2] * 6, "Low": [104.8] * 6,
         "Close": [105.0] * 6, "Volume": [0] * 6},
        index=pd.date_range("2026-06-11 18:00", periods=6, freq="5min",
                            tz="UTC"))
    mt.yf = _FakeYF(_d_utc, _m5_utc)
    r = mt._do_read("Bitcoin", "BTC-USD", 2, "crypto", "test")
    check("prior_close: UTC-stamped daily never serves the partial today row",
          r.get("prior_close") is not None
          and abs(r["prior_close"] - 100.0) < 1e-9
          and abs(r["day_move_pct"] - 5.0) < 0.011,
          f"got {r.get('prior_close')}/{r.get('day_move_pct')}")

    # market closed, no intraday at all: the last daily row IS the current
    # session, so prior close steps back one full row from it
    mt.yf = _FakeYF(_daily(last=110.0), pd.DataFrame())
    r = mt._do_read("TSLA", "TSLA", 2, "stock", "test")
    check("prior_close: daily-only fallback steps back one session",
          r.get("prior_close") is not None
          and abs(r["prior_close"] - 100.0) < 1e-9
          and r.get("stale") is True,
          f"got {r.get('prior_close')}/{r.get('stale')}")

    # one daily row: no earlier session exists -> None, never a fake number
    check("prior_close: single daily row -> None",
          mt._prior_daily_close(_daily(n=1)["Close"], None) is None)
finally:
    mt._feed.alpaca = _orig_alpaca
    mt.yf = _orig_yf
    risk_gate.upcoming_events = _orig_ev
    forward_ledger.record_candidate = _orig_rec

# --- market_tools: reads inherit the scanner's real-time Alpaca feed ---
# _do_read used to hard-code delayed yfinance even with Alpaca keys set, so a
# chat read and the live scanner quoted two different prices for one stock.


class _FakeAlpaca:
    """Stands in for AlpacaREST: counts calls, serves canned bars/trade."""
    def __init__(self, bars=None, price=None, price_ts=None, boom=False):
        self.bars, self.price, self.price_ts = bars, price, price_ts
        self.boom = boom
        self.calls = []

    def today_bars_5m(self, symbol, now):
        self.calls.append(("bars", symbol))
        if self.boom:
            raise RuntimeError("alpaca down")
        return self.bars

    def latest_trade(self, symbol):
        self.calls.append(("trade", symbol))
        if self.boom:
            raise RuntimeError("alpaca down")
        if self.price is None:
            raise RuntimeError("no trade")
        return self.price, self.price_ts


def _alpaca_frame(closes, start="2026-06-11 09:30"):
    idx = pd.date_range(start, periods=len(closes), freq="5min", tz=ET)
    return pd.DataFrame({"Open": closes, "High": [x + 0.2 for x in closes],
                         "Low": [x - 0.2 for x in closes], "Close": closes,
                         "Volume": [0] * len(closes)}, index=idx)


_orig_alpaca = mt._feed.alpaca
try:
    risk_gate.upcoming_events = lambda *a, **k: []
    forward_ledger.record_candidate = lambda *a, **k: None

    # keys set: today's delayed yf bars (3 bars, stuck at 107) are replaced by
    # alpaca's fresher six (up to 110), the price is the live IEX trade (its
    # timestamp beats the last bar's end), and the source label says so.
    # Yesterday's yf bars survive the patch.
    mt.yf = _FakeYF(_daily(last=110.0),
                    _m5_frame([100.0] * 6, [105.0, 106.0, 107.0]))
    fake = _FakeAlpaca(bars=_alpaca_frame([105.0, 106.0, 107.0,
                                           108.0, 109.0, 110.0]),
                       price=111.5,
                       price_ts=pd.Timestamp("2026-06-11 10:02", tz=ET))
    mt._feed.alpaca = fake
    r = mt._do_read("TSLA", "TSLA", 2, "stock", "test")
    check("alpaca read: price is the live IEX trade",
          r.get("price") == 111.5, f"got {r.get('price')}")
    check("alpaca read: source label flips to real-time",
          str(r.get("source", "")).startswith("alpaca"), f"got {r.get('source')}")
    check("alpaca read: asof stamps the trade's own time",
          r.get("asof") == "Thu 9:02 AM CT", f"got {r.get('asof')}")
    check("alpaca read: session high from the fresher bars",
          r.get("recent_session_high") is not None
          and abs(r["recent_session_high"] - 110.2) < 1e-9,
          f"got {r.get('recent_session_high')}")
    check("alpaca read: momentum from the fresher bars (110 vs 107 = +2.8%)",
          r.get("momentum_15min_pct") is not None
          and abs(r["momentum_15min_pct"] - 2.8) < 0.011,
          f"got {r.get('momentum_15min_pct')}")
    check("alpaca read: day move uses the live price",
          r.get("day_move_pct") is not None
          and abs(r["day_move_pct"] - 11.5) < 0.011,
          f"got {r.get('day_move_pct')}")
    cached = mt.cached_m5("TSLA")
    check("alpaca read: chart cache holds the merged frame, no dup bars",
          cached is not None and len(cached) == 12
          and not cached.index.duplicated().any(),
          f"got {None if cached is None else len(cached)} rows")

    # alpaca can't serve crypto, indexes, futures or FX: never even called
    fake2 = _FakeAlpaca(bars=_alpaca_frame([1.0]), price=1.0)
    mt._feed.alpaca = fake2
    mt.yf = _FakeYF(_d_utc, _m5_utc)
    r = mt._do_read("Bitcoin", "BTC-USD", 2, "crypto", "test")
    check("alpaca read: crypto stays on yfinance, alpaca untouched",
          fake2.calls == [] and r.get("source") == "test",
          f"calls {fake2.calls}, source {r.get('source')}")
    mt.yf = _FakeYF(_daily(last=110.0), _m5_frame([100.0] * 6, [105.0] * 4))
    r = mt._do_read("SPX", "^GSPC", 2, "stock", "test")
    check("alpaca read: index symbols stay on yfinance, alpaca untouched",
          fake2.calls == [] and r.get("source") == "test",
          f"calls {fake2.calls}, source {r.get('source')}")

    # a full alpaca outage leaves the plain yfinance read byte-identical
    boom = _FakeAlpaca(boom=True)
    mt._feed.alpaca = boom
    mt.yf = _FakeYF(_daily(last=110.0),
                    _m5_frame([100.0] * 6,
                              [105.0, 106.0, 107.0, 108.0, 109.0, 110.0]))
    r = mt._do_read("TSLA", "TSLA", 2, "stock", "test")
    check("alpaca read: outage keeps the yfinance read intact",
          r.get("price") == 110.0 and r.get("source") == "test"
          and abs(r["momentum_15min_pct"] - 2.8) < 0.011,
          f"got {r.get('price')}/{r.get('source')}")
    check("alpaca read: outage tried both endpoints before falling back",
          ("bars", "TSLA") in boom.calls and ("trade", "TSLA") in boom.calls,
          f"calls {boom.calls}")

    # bars endpoint dry but the trade tape alive AND fresh: price upgrades
    part = _FakeAlpaca(bars=None, price=108.25,
                       price_ts=pd.Timestamp("2026-06-11 10:05", tz=ET))
    mt._feed.alpaca = part
    r = mt._do_read("TSLA", "TSLA", 2, "stock", "test")
    check("alpaca read: fresh trade alone still upgrades the price",
          r.get("price") == 108.25
          and str(r.get("source", "")).startswith("alpaca"),
          f"got {r.get('price')}/{r.get('source')}")

    # IEX quiet (overnight/weekend): the 'latest' trade is OLDER than the
    # freshest bar held, so quoting it as live would be a lie. The bar close
    # wins and the delayed label stands.
    old = _FakeAlpaca(bars=None, price=93.0,
                      price_ts=pd.Timestamp("2026-06-11 09:00", tz=ET))
    mt._feed.alpaca = old
    r = mt._do_read("TSLA", "TSLA", 2, "stock", "test")
    check("alpaca read: a stale IEX trade never masquerades as live",
          r.get("price") == 110.0 and r.get("source") == "test",
          f"got {r.get('price')}/{r.get('source')}")

    # a trade with no parseable timestamp can't prove freshness: rejected
    nots = _FakeAlpaca(bars=None, price=93.0, price_ts=None)
    mt._feed.alpaca = nots
    r = mt._do_read("TSLA", "TSLA", 2, "stock", "test")
    check("alpaca read: timestampless trade rejected",
          r.get("price") == 110.0 and r.get("source") == "test",
          f"got {r.get('price')}/{r.get('source')}")

    # thin symbol: IEX printed nothing 9:35-9:40, so only the bars alpaca
    # actually printed replace their yfinance copies; the consolidated-tape
    # bars in the gap survive (a whole-span replace would delete them and
    # silently stretch the '15-min' momentum window)
    mt.yf = _FakeYF(_daily(last=110.0),
                    _m5_frame([100.0] * 6,
                              [105.0, 106.0, 107.0, 108.0, 109.0, 110.0]))
    thin_idx = pd.DatetimeIndex([pd.Timestamp("2026-06-11 09:30", tz=ET),
                                 pd.Timestamp("2026-06-11 09:45", tz=ET)])
    thin_bars = pd.DataFrame(
        {"Open": [105.5, 108.5], "High": [105.7, 108.7],
         "Low": [105.3, 108.3], "Close": [105.5, 108.5], "Volume": [0, 0]},
        index=thin_idx)
    mt._feed.alpaca = _FakeAlpaca(bars=thin_bars, price=None)
    r = mt._do_read("TSLA", "TSLA", 2, "stock", "test")
    cached = mt.cached_m5("TSLA")
    check("alpaca read: gap in IEX bars keeps the yfinance bars underneath",
          cached is not None and len(cached) == 12
          and cached.loc[pd.Timestamp("2026-06-11 09:45", tz=ET), "Close"] == 108.5
          and cached.loc[pd.Timestamp("2026-06-11 09:40", tz=ET), "Close"] == 107.0,
          f"got {None if cached is None else len(cached)} rows")

    # data_feed.latest_trade: price + ET timestamp parsed, absent ts -> None
    import data_feed as dfmod

    class _Resp:
        def __init__(self, js):
            self._js = js

        def raise_for_status(self):
            pass

        def json(self):
            return self._js

    _orig_req_get = dfmod.requests.get
    try:
        dfmod.requests.get = lambda *a, **k: _Resp(
            {"trade": {"p": 123.45, "t": "2026-06-11T14:02:00.5Z"}})
        rest = dfmod.AlpacaREST("k", "s")
        px, ts = rest.latest_trade("TSLA")
        check("alpaca trade: price and ET timestamp parsed",
              px == 123.45 and ts is not None and ts.hour == 10
              and ts.minute == 2, f"got {px}/{ts}")
        check("alpaca trade: latest_price still returns the bare float",
              rest.latest_price("TSLA") == 123.45)
        dfmod.requests.get = lambda *a, **k: _Resp({"trade": {"p": 9.5}})
        px, ts = rest.latest_trade("TSLA")
        check("alpaca trade: missing timestamp -> None, price intact",
              px == 9.5 and ts is None, f"got {px}/{ts}")
    finally:
        dfmod.requests.get = _orig_req_get

    # the signal card's freshness line follows the read's actual source
    _plan = {"direction": "BUY", "entry": 100.0, "stop": 99.0, "target1": 101.0,
             "target": 102.0, "risk": 1.0, "reward": 2.0, "rr": 2.0,
             "weak": False, "structure": True}
    _base = {"instrument": "TSLA", "ticker": "TSLA", "kind": "stock",
             "decimals": 2, "plan": _plan, "asof": "Thu 9:55 AM CT",
             "source": "alpaca real-time (IEX); history via yfinance"}
    card = cards.signal_card(_base)
    check("signal card: real-time read never claims a 15m delay",
          "real-time (IEX)" in card and "~15m delayed" not in card, card)
    card = cards.signal_card({**_base, "source": "yfinance, ~15-min delayed"})
    check("signal card: yfinance read keeps the delay warning",
          "~15m delayed" in card, card)
finally:
    mt._feed.alpaca = _orig_alpaca
    mt.yf = _orig_yf
    risk_gate.upcoming_events = _orig_ev
    forward_ledger.record_candidate = _orig_rec

# --- telegram splitting: a long reply must arrive in parts, not vanish ---
# Telegram rejects >4096-char messages with HTTP 400; send_to used to post
# the whole text in one call, so a long deep answer was silently dropped.
import telegram

short = "short reply"
check("tg split: short text untouched", telegram.split_message(short) == [short])

paras = [f"para {i} " + "x" * 500 for i in range(20)]
long_text = "\n\n".join(paras)
parts = telegram.split_message(long_text)
check("tg split: long text becomes several parts", len(parts) > 1)
check("tg split: every part fits the limit",
      all(0 < len(p) <= telegram.TG_MAX_CHARS for p in parts),
      f"part lengths {[len(p) for p in parts]}")
check("tg split: cuts land on paragraph boundaries",
      all(p.startswith("para ") for p in parts),
      f"starts {[p[:8] for p in parts]}")
check("tg split: no content lost",
      "".join(long_text.split()) == "".join(" ".join(parts).split()))

prose = "This is a sentence about the trade. " * 300  # no newlines anywhere
sparts = telegram.split_message(prose)
check("tg split: sentence-boundary cuts keep the period",
      len(sparts) > 1 and all(p.endswith(".") for p in sparts),
      f"tails {[p[-10:] for p in sparts]}")

blob = "y" * 9001  # one unbroken run: hard cut is the only option
bparts = telegram.split_message(blob)
check("tg split: unbroken run is hard-cut, nothing dropped",
      all(len(p) <= telegram.TG_MAX_CHARS for p in bparts)
      and "".join(bparts) == blob)

_orig_send_one = telegram._send_one
sent = []
telegram._send_one = lambda cid, t: sent.append((cid, t)) or None
try:
    err = telegram.send_to("123", long_text)
finally:
    telegram._send_one = _orig_send_one
check("tg send_to: long reply goes out in order with no error",
      err is None and [t for _, t in sent] == parts)

calls = []
def _fail_second(cid, t):
    calls.append(t)
    return "400 boom" if len(calls) == 2 else None
telegram._send_one = _fail_second
try:
    err = telegram.send_to("123", long_text)
finally:
    telegram._send_one = _orig_send_one
check("tg send_to: a part's send error is reported, later parts skipped",
      err == "400 boom" and len(calls) == 2)

# --- reload_tunables: the overnight backtest reaches the morning session ---
# Service used to load backtest_results.json / backtest_new_rules.json once in
# __init__ and never again, so a daemon alive for weeks gated entries on
# frozen stats. Now the reports are re-read when the trading date flips.
import json as _json
import tempfile
from pathlib import Path

import scanner as scannermod
import scoreboard

_reports = Path(tempfile.mkdtemp(prefix="reload_test_"))
_orig_reports_dir = scoreboard.REPORTS_DIR


def _write_report(name, obj):
    (_reports / name).write_text(_json.dumps(obj), encoding="utf-8")


svc = scannermod.Service.__new__(scannermod.Service)  # plumbing only, no feed/book
svc.day = None
svc.skipped_today = set()
svc.daily_closes = {}
svc.backtest_old = None
svc.backtest_new = None

try:
    scoreboard.REPORTS_DIR = _reports
    _write_report("backtest_results.json",
                  {"per_setup": {"SPX:call": {"win_rate": 71.0,
                                              "expectancy_pct": 4.0}},
                   "bracket": {"target_pct": 15, "stop_pct": -60}})
    svc.reload_tunables()  # what __init__ does at process start
    check("reload: initial load picks up report, bracket and cfg",
          svc.backtest_old["per_setup"]["SPX:call"]["win_rate"] == 71.0
          and svc.old_bracket == {"target_pct": 15, "stop_pct": -60}
          and svc.backtest_new is None
          and isinstance(svc.cfg, scannermod.StrategyConfig))

    # backtest.py rewrites the reports overnight; a daemon started the
    # evening before must pick that up on its FIRST reset_day of the morning
    _write_report("backtest_results.json",
                  {"per_setup": {"SPX:call": {"win_rate": 74.0,
                                              "expectancy_pct": 6.0}},
                   "bracket": {"target_pct": 20, "stop_pct": -50}})
    _write_report("backtest_new_rules.json",
                  {"per_setup": {"SPX:call": {"win_rate": 73.0,
                                              "expectancy_pct": 7.5}}})
    svc.skipped_today = {"SPX:call"}
    svc.reset_day(datetime(2026, 6, 12, 9, 31, tzinfo=ET))
    check("reload: date flip picks up the overnight backtest",
          svc.backtest_old["per_setup"]["SPX:call"]["win_rate"] == 74.0
          and svc.backtest_new["per_setup"]["SPX:call"]["expectancy_pct"] == 7.5
          and svc.old_bracket == {"target_pct": 20, "stop_pct": -50})
    check("reload: date flip still resets the day state",
          svc.day == date(2026, 6, 12) and svc.skipped_today == set())

    # a corrupt overnight write keeps yesterday's verified stats
    (_reports / "backtest_results.json").write_text("{not json",
                                                    encoding="utf-8")
    svc.reset_day(datetime(2026, 6, 13, 9, 31, tzinfo=ET))
    check("reload: corrupt report keeps previous stats, gate stays alive",
          svc.backtest_old["per_setup"]["SPX:call"]["win_rate"] == 74.0
          and svc.old_bracket == {"target_pct": 20, "stop_pct": -50})

    # same-day cycles must not re-read the files every poll
    _write_report("backtest_results.json",
                  {"per_setup": {}, "bracket": {"target_pct": 99,
                                                "stop_pct": -1}})
    svc.reset_day(datetime(2026, 6, 13, 14, 0, tzinfo=ET))
    check("reload: same-day cycle does not reload",
          svc.old_bracket == {"target_pct": 20, "stop_pct": -50})

    # a degenerate "bracket": null parses fine (so the report replaces the
    # previous one) but .get's default never applies to a PRESENT key —
    # unvalidated, dict(None) at entry pinning would then block every alert.
    # It must degrade to the built-in default bracket instead.
    _write_report("backtest_results.json",
                  {"per_setup": {"SPX:call": {"win_rate": 74.0,
                                              "expectancy_pct": 6.0}},
                   "bracket": None})
    svc.reset_day(datetime(2026, 6, 16, 9, 31, tzinfo=ET))
    check("reload: 'bracket': null degrades to the built-in default",
          svc.old_bracket == {"target_pct": 15, "stop_pct": -60})

    # same for legs that json parses but the shadow can't compare sanely
    _write_report("backtest_results.json",
                  {"per_setup": {}, "bracket": {"target_pct": True,
                                                "stop_pct": -60}})
    svc.reset_day(datetime(2026, 6, 17, 9, 31, tzinfo=ET))
    check("reload: bool bracket leg degrades to the built-in default",
          svc.old_bracket == {"target_pct": 15, "stop_pct": -60})
finally:
    scoreboard.REPORTS_DIR = _orig_reports_dir
    import shutil
    shutil.rmtree(_reports, ignore_errors=True)

# --- live_params.json: owner-tunable settings, strict all-or-nothing ---
# ALLOWED_SETUPS and the entry knobs used to be frozen in code, so applying
# even an APPROVED rule change meant a redeploy. reload_tunables() now reads
# live_params.json (any invalid key rejects the WHOLE file; built-ins are the
# fallback) and the owner-only /reload applies an edit immediately.
import live_params as livemod
import telegram

_lp_dir = Path(tempfile.mkdtemp(prefix="liveparams_test_"))
_lp_file = _lp_dir / "live_params.json"
_orig_lp_path = livemod.path
livemod.path = lambda: _lp_file
_lp_reports = Path(tempfile.mkdtemp(prefix="lp_reports_"))
_orig_reports_dir = scoreboard.REPORTS_DIR


def _write_lp(obj):
    body = obj if isinstance(obj, str) else _json.dumps(obj)
    _lp_file.write_text(body, encoding="utf-8")


lsvc = scannermod.Service.__new__(scannermod.Service)  # plumbing only
lsvc.day = None
lsvc.skipped_today = set()
lsvc.daily_closes = {}
lsvc.backtest_old = None
lsvc.backtest_new = None

try:
    scoreboard.REPORTS_DIR = _lp_reports
    (_lp_reports / "backtest_results.json").write_text(_json.dumps(
        {"per_setup": {"SPX:call": {"win_rate": 74.0, "expectancy_pct": 6.0}},
         "bracket": {"target_pct": 15, "stop_pct": -60}}), encoding="utf-8")
    _defaults = scannermod.StrategyConfig()

    # no file -> built-ins exactly (the file is opt-in, not required)
    lsvc.reload_tunables()
    check("live params: no file keeps built-ins",
          lsvc.ALLOWED_SETUPS == scannermod.Service.ALLOWED_SETUPS
          and lsvc.cfg.watchlist == _defaults.watchlist
          and lsvc.cfg.entry_start == _defaults.entry_start
          and lsvc.cfg.entry_end == _defaults.entry_end
          and lsvc.cfg.mom_bars == _defaults.mom_bars
          and lsvc.live_params_note == "no live_params.json; built-in settings")

    # a valid file overrides exactly what it names (case-normalized)
    _write_lp({"allowed_setups": ["spx:CALL", "TSLA:put"],
               "watchlist": {"spx": "^GSPC", "TSLA": "TSLA"},
               "entry_start": "09:50", "entry_end": "10:15", "mom_bars": 4})
    lsvc.reload_tunables()
    check("live params: valid file applies with normalized case",
          lsvc.ALLOWED_SETUPS == {"SPX:call", "TSLA:put"}
          and lsvc.cfg.watchlist == {"SPX": "^GSPC", "TSLA": "TSLA"}
          and lsvc.cfg.entry_start == time(9, 50)
          and lsvc.cfg.entry_end == time(10, 15)
          and lsvc.cfg.mom_bars == 4
          and lsvc.live_params_note.startswith("live_params.json applied"))

    # the applied allow-list actually gates: QCOM:call is built-in-allowed
    # but the file dropped it; SPX:call stays allowed AND must still clear
    # the real-stats bar (the honesty gate is not loosened by the file)
    _qcom = scannermod.Setup(ticker="QCOM", direction="call", strike=180.0,
                             spot=179.5, mom_pct=0.4, reason="test")
    _spx = scannermod.Setup(ticker="SPX", direction="call", strike=7300.0,
                            spot=7299.0, mom_pct=0.4, reason="test")
    check("live params: gate honors the file's allow-list",
          lsvc.gate_stats(_qcom) is None and lsvc.gate_stats(_spx) is not None)

    # a ticker REMOVED from the watchlist while its position is still open
    # must keep a working feed symbol: bare "SPX" fetches nothing, so the
    # built-in map is the fallback (else stop/half/trail alerts die silently)
    _write_lp({"watchlist": {"TSLA": "TSLA"}})
    lsvc.reload_tunables()
    check("live params: removed ticker falls back to the built-in feed map",
          lsvc.cfg.watchlist == {"TSLA": "TSLA"}
          and lsvc.yfs_for("SPX") == "^GSPC"
          and lsvc.yfs_for("TSLA") == "TSLA"
          and lsvc.yfs_for("ZZZ") == "ZZZ")

    # partial file: only the named key changes
    _write_lp({"mom_bars": 5})
    lsvc.reload_tunables()
    check("live params: partial file overrides only what it names",
          lsvc.cfg.mom_bars == 5
          and lsvc.ALLOWED_SETUPS == scannermod.Service.ALLOWED_SETUPS
          and lsvc.cfg.watchlist == _defaults.watchlist
          and lsvc.cfg.entry_start == _defaults.entry_start)

    # every bad shape rejects the WHOLE file; built-ins stay untouched
    _bad = [
        ("not json", "{not json"),
        ("not an object", _json.dumps([1, 2])),
        ("unknown key rejects even the valid keys",
         _json.dumps({"mom_bars": 4, "stop_pct": -50})),
        ("setup missing :direction", _json.dumps({"allowed_setups": ["SPXcall"]})),
        ("setup bad direction", _json.dumps({"allowed_setups": ["SPX:strangle"]})),
        ("setups empty", _json.dumps({"allowed_setups": []})),
        ("watchlist empty", _json.dumps({"watchlist": {}})),
        ("watchlist non-text symbol", _json.dumps({"watchlist": {"SPX": 5}})),
        ("watchlist multi-symbol value",
         _json.dumps({"watchlist": {"SPX": "SPY QQQ"}})),
        ("watchlist oversize",
         _json.dumps({"watchlist": {f"T{i}": "X" for i in range(13)}})),
        ("time not HH:MM", _json.dumps({"entry_start": "9am"})),
        ("time premarket", _json.dumps({"entry_start": "08:00"})),
        ("time before monitoring starts",
         _json.dumps({"entry_start": "09:35"})),
        ("oversize file", _json.dumps({"mom_bars": 4}) + " " * 70000),
        ("window empty",
         _json.dumps({"entry_start": "10:30", "entry_end": "10:00"})),
        ("one edge crosses the built-in other edge",
         _json.dumps({"entry_start": "11:00"})),
        ("mom_bars zero", _json.dumps({"mom_bars": 0})),
        ("mom_bars bool", _json.dumps({"mom_bars": True})),
        ("mom_bars text", _json.dumps({"mom_bars": "3"})),
    ]
    for _label, _body in _bad:
        _write_lp(_body)
        lsvc.reload_tunables()
        _ok = (lsvc.ALLOWED_SETUPS == scannermod.Service.ALLOWED_SETUPS
               and lsvc.cfg.mom_bars == _defaults.mom_bars
               and lsvc.cfg.watchlist == _defaults.watchlist
               and lsvc.cfg.entry_start == _defaults.entry_start
               and "REJECTED" in lsvc.live_params_note)
        check(f"live params: {_label} -> whole file rejected, built-ins stay",
              _ok, lsvc.live_params_note)

    # a non-UTF-8 file is a plain validation message, never a crash
    _lp_file.write_bytes(_json.dumps({"mom_bars": 4}).encode("utf-16"))
    lsvc.reload_tunables()
    check("live params: non-UTF-8 file rejected with a plain reason",
          lsvc.cfg.mom_bars == _defaults.mom_bars
          and "REJECTED" in lsvc.live_params_note
          and "UTF-8" in lsvc.live_params_note)

    # breaking the file after a good apply reverts to built-ins: the fallback
    # is deterministic (a fresh boot with the same bad file runs built-ins too)
    _write_lp({"mom_bars": 6})
    lsvc.reload_tunables()
    _write_lp("{broken")
    lsvc.reload_tunables()
    check("live params: a break after a good apply reverts to built-ins",
          lsvc.cfg.mom_bars == _defaults.mom_bars
          and "REJECTED" in lsvc.live_params_note)

    # the overnight date flip picks up an edit without a restart
    _write_lp({"mom_bars": 7})
    lsvc.day = date(2026, 6, 12)
    lsvc.reset_day(datetime(2026, 6, 15, 9, 31, tzinfo=ET))
    check("live params: date flip applies an overnight edit",
          lsvc.cfg.mom_bars == 7)

    # /reload: owner applies an edit immediately and gets the live settings
    # back; a non-owner is refused (ADMIN_CMDS)
    _write_lp({"entry_start": "09:50"})
    _orig_is_owner = telegram.is_owner
    telegram.is_owner = lambda cid: cid == "boss"
    try:
        _deny = lsvc.run_command("/reload", "", "stranger")
        _reply = lsvc.run_command("/reload", "", "boss")
    finally:
        telegram.is_owner = _orig_is_owner
    check("live params: /reload is owner-only", "owner-only" in (_deny or ""))
    check("live params: /reload applies and reports the live settings",
          _reply is not None and lsvc.cfg.entry_start == time(9, 50)
          and "09:50" in _reply and "Alert allow-list:" in _reply
          and "live_params.json applied" in _reply)
    # only SPX:call has stats in this test's report: the other allow-listed
    # setups must be flagged as unable to alert instead of silently no-oping
    check("live params: /reload flags allow-list entries with no stats",
          "SPY:call (no backtest stats yet, cannot alert)" in _reply
          and "SPX:call (" not in _reply)
    check("live params: /reload reply and notes carry no em dash",
          "—" not in _reply and "—" not in lsvc.live_params_note)
finally:
    livemod.path = _orig_lp_path
    scoreboard.REPORTS_DIR = _orig_reports_dir
    shutil.rmtree(_lp_dir, ignore_errors=True)
    shutil.rmtree(_lp_reports, ignore_errors=True)

# --- effective-settings text surfaces: no stale built-ins after an override ---
# reload_tunables() covered the scanner's DECISIONS, but /calls option lines,
# the green morning card, the chat brain's market tools, the nightly reviewer
# prompt, the digest header and the recap's ticker->Yahoo map still quoted the
# built-in window/watchlist/allow-list once live_params.json overrode them.
# They all render from live_params.effective() now, which reads the same file
# reload_tunables() applies and falls back to the built-ins the same way.
import learn as learnmod
import recap as recapmod

_fx_dir = Path(tempfile.mkdtemp(prefix="liveparams_fx_"))
_fx_file = _fx_dir / "live_params.json"
_fx_orig_path = livemod.path
_fx_reports = Path(tempfile.mkdtemp(prefix="fx_reports_"))
_fx_orig_reports = scoreboard.REPORTS_DIR
_fx_orig_llog, _fx_orig_ldig = learnmod.LESSONS_LOG, learnmod.LESSONS_DIGEST

try:
    livemod.path = lambda: _fx_file
    scoreboard.REPORTS_DIR = _fx_reports
    (_fx_reports / "backtest_results.json").write_text(_json.dumps(
        {"per_setup": {"SPX:call": {"win_rate": 74.0, "expectancy_pct": 6.0}},
         "bracket": {"target_pct": 15, "stop_pct": -60}}), encoding="utf-8")
    _fx_dflt = scannermod.StrategyConfig()

    # the CT window renderer: built-ins produce the exact classic sentence
    check("window text: built-ins render 8:45-9:30 AM CT",
          cards.entry_window_ct(_fx_dflt) == "8:45-9:30 AM CT")
    _fx_c = scannermod.StrategyConfig()
    _fx_c.entry_start, _fx_c.entry_end = time(11, 0), time(13, 30)
    check("window text: a cross-noon window names AM and PM",
          cards.entry_window_ct(_fx_c) == "10:00 AM-12:30 PM CT")
    _fx_c.entry_start, _fx_c.entry_end = time(13, 0), time(14, 0)
    check("window text: an afternoon window says PM once",
          cards.entry_window_ct(_fx_c) == "12:00-1:00 PM CT")

    # no file: effective() is the built-ins and the reviewer prompt is the
    # constant, byte for byte
    _ecfg, _eallow = livemod.effective()
    check("effective(): no file returns the built-ins",
          _ecfg.watchlist == _fx_dflt.watchlist
          and _ecfg.entry_start == _fx_dflt.entry_start
          and _ecfg.mom_bars == _fx_dflt.mom_bars
          and _eallow == livemod.DEFAULT_ALLOWED_SETUPS)
    check("learn: no overrides means the reviewer prompt IS the constant",
          learnmod.reviewer_system() == learnmod.REVIEWER_SYSTEM
          and "9:45-10:30" in learnmod.REVIEWER_SYSTEM
          and "SPX, SPY, TSLA, QCOM" in learnmod.REVIEWER_SYSTEM)

    # one comprehensive override: SPX off the lists, NVDA added, window moved
    _fx_file.write_text(_json.dumps(
        {"allowed_setups": ["TSLA:put"],
         "watchlist": {"TSLA": "TSLA", "NVDA": "NVDA"},
         "entry_start": "09:50", "entry_end": "10:15"}), encoding="utf-8")
    _ecfg, _eallow = livemod.effective()
    check("effective(): a valid file applies exactly like reload_tunables",
          _eallow == {"TSLA:put"}
          and _ecfg.watchlist == {"TSLA": "TSLA", "NVDA": "NVDA"}
          and _ecfg.entry_start == time(9, 50)
          and _ecfg.entry_end == time(10, 15))

    _fx_win = cards.entry_window_ct(_ecfg)
    check("window text: the overridden window renders in CT",
          _fx_win == "8:50-9:15 AM CT")
    check("cards: option_line quotes the effective window, not the built-in",
          f"entry window is {_fx_win}" in cards.option_line(
              "TSLA", {"price": 310.0, "in_entry_window": False,
                       "entry_window_ct": _fx_win}))
    _fx_card = cards.morning_card("green", "calm tape", date(2026, 7, 15),
                                  window_ct=_fx_win)
    check("cards: green morning card quotes the effective window",
          _fx_win in _fx_card and "8:45-9:30" not in _fx_card)

    # market_tools resolves the same file: membership, gate, replay
    check("market_tools: market_now rejects a ticker the file removed",
          "isn't on the watchlist" in (mt.market_now("SPX").get("error") or ""))
    _fx_day5m = mt._day_5m
    mt._day_5m = lambda yfs, day: None
    try:
        _fx_r = mt.analyze_day("NVDA", "2026-07-10")
        check("market_tools: analyze_day accepts a live-added ticker",
              "error" not in _fx_r
              and "No intraday data" in (_fx_r.get("note") or ""))
        _fx_r2 = mt.analyze_day("SPX", "2026-07-10")
        check("market_tools: analyze_day rejects with the effective list",
              "isn't on the watchlist" in (_fx_r2.get("error") or "")
              and "NVDA" in _fx_r2["error"])
    finally:
        mt._day_5m = _fx_day5m
    check("market_tools: _gate_ok refuses a setup the file removed",
          mt._gate_ok("SPX", "call")[0] is False)
    check("market_tools: _gate_ok still passes stats when allow-listed",
          mt._gate_ok("SPX", "call", {"SPX:call"})[0] is True)

    # the nightly reviewer and the digest header state the effective rules
    _fx_rs = learnmod.reviewer_system()
    check("learn: reviewer prompt states the effective window and names",
          "9:50-10:15" in _fx_rs and "TSLA, NVDA" in _fx_rs
          and "9:45-10:30" not in _fx_rs and "QCOM" not in _fx_rs)
    learnmod.LESSONS_LOG = _fx_dir / "lessons_fx.jsonl"
    learnmod.LESSONS_DIGEST = _fx_dir / "digest_fx.md"
    learnmod._rebuild_digest()
    check("learn: digest header quotes the effective entry window",
          "9:50-10:15 entry window"
          in learnmod.LESSONS_DIGEST.read_text(encoding="utf-8"))

    # the recap grades an added ticker against its real symbol and keeps the
    # built-in feed map for a ticker the file removed mid-history
    check("recap: symbol map honors overrides with the built-in fallback",
          recapmod.yfs_for(_ecfg, "NVDA") == "NVDA"
          and recapmod.yfs_for(_ecfg, "SPX") == "^GSPC"
          and recapmod.yfs_for(_ecfg, "ZZZ") == "ZZZ")

    # a broken file falls back to the built-ins everywhere at once
    _fx_file.write_text("{broken", encoding="utf-8")
    _ecfg, _eallow = livemod.effective()
    check("effective(): an invalid file falls back to the built-ins",
          _ecfg.watchlist == _fx_dflt.watchlist
          and _eallow == livemod.DEFAULT_ALLOWED_SETUPS
          and learnmod.reviewer_system() == learnmod.REVIEWER_SYSTEM)

    # the brain's static prompt and tool text can't go stale: the four names
    # and the CT window sentence now live only in LIVE BOT STATE
    import assistant as _fx_asst
    check("assistant: static prompt no longer hardcodes names or window",
          "8:45-9:30" not in _fx_asst.SYSTEM
          and "SPX, SPY, QCOM, TSLA" not in _fx_asst.SYSTEM
          and "Alert watchlist" in _fx_asst.SYSTEM)
    check("assistant: tool descriptions no longer hardcode the four names",
          "SPX, SPY, QCOM or TSLA" not in _json.dumps(_fx_asst.TOOLS)
          and "other than SPX" not in _json.dumps(_fx_asst.TOOLS))
finally:
    livemod.path = _fx_orig_path
    scoreboard.REPORTS_DIR = _fx_orig_reports
    learnmod.LESSONS_LOG, learnmod.LESSONS_DIGEST = _fx_orig_llog, _fx_orig_ldig
    shutil.rmtree(_fx_dir, ignore_errors=True)
    shutil.rmtree(_fx_reports, ignore_errors=True)

# --- learn digest: repeated lessons cannot crowd out real ones ---
# _deterministic_review appended the SAME "staying flat is correct" bullet
# every quiet night, and _rebuild_digest kept the last DIGEST_KEEP bullets
# raw, so two slow weeks filled the whole window the brain reads and evicted
# the real lessons. Quiet days now add no lesson, and the digest keeps only
# the newest copy of a repeated bullet.
import learn

_orig_llog, _orig_ldig = learn.LESSONS_LOG, learn.LESSONS_DIGEST
learn.LESSONS_LOG = config.DATA_DIR / "lessons_test.jsonl"
learn.LESSONS_DIGEST = config.DATA_DIR / "lessons_digest_test.md"


def _lentry(day, lessons):
    return {"session": day, "graded_at": day + " 21:30:00 EDT", "wins": 0,
            "losses": 0, "trades": [], "review": "r", "lessons": lessons,
            "watch_tomorrow": "", "proposed_change": None}


def _tag(day):
    fmt = "%#m/%#d" if sys.platform.startswith("win") else "%-m/%-d"
    return datetime.strptime(day, "%Y-%m-%d").strftime(fmt)


def _digest_bullets():
    txt = learn.LESSONS_DIGEST.read_text(encoding="utf-8")
    return [ln for ln in txt.splitlines() if ln.startswith("- ")]


try:
    for f in (learn.LESSONS_LOG, learn.LESSONS_DIGEST):
        if f.exists():
            f.unlink()

    quiet = learn._deterministic_review(
        {"session": "2026-06-11", "day_name": "Thu 6/11", "market": "",
         "trades": [], "wins": 0, "losses": 0})
    check("learn: quiet day teaches no lesson",
          quiet["lessons"] == [] and "stayed out" in quiet["review"])

    learn._append_lesson(_lentry("2026-06-01", ["real lesson A"]))
    learn._append_lesson(_lentry("2026-06-02", ["Same  old lesson."]))
    learn._append_lesson(_lentry("2026-06-03", ["same old lesson.", ""]))
    learn._append_lesson(_lentry("2026-06-05", None))  # legacy null entry
    learn._append_lesson(_lentry("2026-06-04", ["real lesson B"]))
    learn._rebuild_digest()
    got = _digest_bullets()
    old = [ln for ln in got if "old lesson" in ln]
    check("learn: repeated lesson keeps one copy, the newest",
          len(old) == 1 and f"({_tag('2026-06-03')})" in old[0],
          f"got {old}")
    check("learn: digest stays newest first",
          got == [f"- ({_tag('2026-06-04')}) real lesson B",
                  f"- ({_tag('2026-06-03')}) same old lesson.",
                  f"- ({_tag('2026-06-01')}) real lesson A"],
          f"got {got}")

    # a slow stretch of DIFFERENT nights repeating one bullet cannot evict
    # the real lessons (the original bug: the same quiet-day lesson landed
    # every night)
    for i in range(30):
        day = (date(2026, 6, 6) + timedelta(days=i)).isoformat()
        learn._append_lesson(_lentry(day, ["repeat me"]))
    learn._rebuild_digest()
    got = _digest_bullets()
    check("learn: 30 identical nights collapse to one bullet",
          sum("repeat me" in ln for ln in got) == 1, f"got {len(got)} bullets")
    check("learn: real lessons survive the burst",
          any("real lesson A" in ln for ln in got)
          and any("real lesson B" in ln for ln in got), f"got {got}")

    # the window still caps at DIGEST_KEEP, newest distinct bullets win
    for i in range(25):
        day = (date(2026, 7, 6) + timedelta(days=i)).isoformat()
        learn._append_lesson(_lentry(day, [f"distinct lesson {i:02d}"]))
    learn._rebuild_digest()
    got = _digest_bullets()
    check("learn: window capped at DIGEST_KEEP distinct bullets",
          len(got) == learn.DIGEST_KEEP, f"got {len(got)}")
    check("learn: newest distinct lessons hold the window",
          "distinct lesson 24" in got[0] and "distinct lesson 05" in got[-1],
          f"got {got[0]!r} .. {got[-1]!r}")
finally:
    for f in (learn.LESSONS_LOG, learn.LESSONS_DIGEST):
        if f.exists():
            f.unlink()
    learn.LESSONS_LOG, learn.LESSONS_DIGEST = _orig_llog, _orig_ldig

# --- learn brief: the reviewer sees what it already learned ---
# _day_brief carried only today's market and calls, so the nightly model
# re-derived lessons from scratch, restated ones it already had, and could
# never notice a repeated mistake. The brief now opens with the digest
# playbook plus the last few nightly reads and an explicit do-not-repeat
# instruction, before today's session.
learn.LESSONS_LOG = config.DATA_DIR / "lessons_test.jsonl"
learn.LESSONS_DIGEST = config.DATA_DIR / "lessons_digest_test.md"


def _lread(day, review):
    e = _lentry(day, ["lesson " + day])
    e["review"] = review
    return e


def _quiet_record(day):
    return {"session": day, "day_name": "Thu 6/11", "market": "",
            "trades": [], "wins": 0, "losses": 0}


try:
    for f in (learn.LESSONS_LOG, learn.LESSONS_DIGEST):
        if f.exists():
            f.unlink()

    brief = learn._day_brief(_quiet_record("2026-06-11"))
    check("brief: first night has no playbook block",
          brief.startswith("Session:") and "ALREADY LEARNED" not in brief,
          f"got {brief[:80]!r}")

    # an empty digest placeholder must not fabricate a playbook
    learn.LESSONS_DIGEST.write_text("header\n\n- (none yet)\n", encoding="utf-8")
    brief = learn._day_brief(_quiet_record("2026-06-11"))
    check("brief: '(none yet)' digest adds no block",
          "ALREADY LEARNED" not in brief)

    learn._append_lesson(_lread("2026-06-01", "read one"))
    learn._append_lesson(_lread("2026-06-02", "read two"))
    learn._append_lesson(_lread("2026-06-02", "read two rerun"))  # same session
    learn._append_lesson(_lread("2026-06-03",
                                "deep review SPX 2026-06-03: cause note"))
    learn._append_lesson(_lread("2026-06-04", "read four " + "y" * 300))
    learn._append_lesson(_lread("2026-06-05", "read five"))
    learn._append_lesson(_lread("2026-06-11", "tonight's own earlier read"))
    learn._rebuild_digest()
    brief = learn._day_brief(_quiet_record("2026-06-11"))
    check("brief: playbook and nightly reads lead the brief",
          brief.index("WHAT I ALREADY LEARNED") <
          brief.index("MY LAST FEW NIGHTLY READS") < brief.index("Session:"),
          f"got {brief[:120]!r}")
    check("brief: digest bullets included",
          f"- ({_tag('2026-06-05')}) lesson 2026-06-05" in brief)
    reads = [ln for ln in brief.splitlines() if ln.startswith("- 2026-")]
    check("brief: last 3 sessions newest first, one per session",
          [r[:12] for r in reads] == ["- 2026-06-05", "- 2026-06-04",
                                      "- 2026-06-02"], f"got {reads}")
    check("brief: rerun keeps the newest read for its session",
          any("read two rerun" in r for r in reads), f"got {reads}")
    check("brief: deep-review rows and tonight's own read stay out",
          "cause note" not in brief and "tonight's own" not in brief)
    check("brief: long reads truncate",
          all(len(r) <= 260 for r in reads),
          f"longest {max(len(r) for r in reads)}")
    check("brief: do-not-repeat instruction present before today",
          "Do not repeat a lesson" in brief.split("Session:")[0])

    # today's calls still render after the block on a trade day
    trade_rec = {
        "session": "2026-06-11", "day_name": "Thu 6/11", "market": "chopped",
        "wins": 1, "losses": 0,
        "trades": [{"ticker": "SPX", "direction": "call", "strike": 7300.0,
                    "texted": "09:50:00", "verdict": "RIGHT", "story": "ran",
                    "won": True, "closed": True,
                    "features": {"mom_pct": 0.2, "win_rate_quoted": 72.0,
                                 "risk_mode": "green", "entry_source": "quote"},
                    "outcome": {"final_pnl_pct": 40.0, "mfe_pct": 55.0,
                                "mae_pct": -5.0, "exit_reason": "trail",
                                "banked_half": True}}]}
    brief = learn._day_brief(trade_rec)
    check("brief: trade day keeps block plus calls",
          "WHAT I ALREADY LEARNED" in brief and "SPX 7300 CALL" in brief
          and brief.index("ALREADY LEARNED") < brief.index("SPX 7300"),
          f"got {brief[-200:]!r}")
finally:
    for f in (learn.LESSONS_LOG, learn.LESSONS_DIGEST):
        if f.exists():
            f.unlink()
    learn.LESSONS_LOG, learn.LESSONS_DIGEST = _orig_llog, _orig_ldig

# --- learn: paper practice calls cannot masquerade as live results ---
# PAPER_MODE positions flowed into the nightly review identically to real
# calls: the brief presented them to the reviewer as live trades, the
# deterministic fallback could write a practice outcome into the permanent
# playbook, the owner's nightly digest listed them untagged (recap, cards and
# scoreboard already tag [PAPER]) and the coach dossier carried no flag at
# all. Every surface now sees the tag.
import coach

learn.LESSONS_LOG = config.DATA_DIR / "lessons_test.jsonl"
learn.LESSONS_DIGEST = config.DATA_DIR / "lessons_digest_test.md"
_orig_posfile_pp = config.POSITIONS_FILE
_orig_et_now_pp = learn.et_now
_orig_mktctx_pp = learn._market_context
_pp_path = config.DATA_DIR / "positions_paper_test.json"
config.POSITIONS_FILE = _pp_path


def _pp_trade(ticker, paper, won=True, exit_reason="trail", mfe=55.0,
              banked_half=True):
    final = 40.0 if won else -70.0
    return {"ticker": ticker, "direction": "call", "strike": 7300.0,
            "texted": "09:50:00", "verdict": "RIGHT" if won else "WRONG",
            "story": "ran", "won": won, "closed": True, "paper": paper,
            "features": {"mom_pct": 0.2, "win_rate_quoted": 72.0,
                         "risk_mode": "green", "entry_source": "quote"},
            "outcome": {"final_pnl_pct": final, "mfe_pct": mfe,
                        "mae_pct": -5.0, "exit_reason": exit_reason,
                        "banked_half": banked_half}}


def _pp_rec(*trades):
    return {"session": "2026-06-11", "day_name": "Thu 6/11", "market": "",
            "trades": list(trades),
            "wins": sum(1 for t in trades if t["won"]),
            "losses": sum(1 for t in trades if t["closed"] and not t["won"])}


try:
    for f in (learn.LESSONS_LOG, learn.LESSONS_DIGEST):
        if f.exists():
            f.unlink()
    if _pp_path.exists():
        _pp_path.unlink()

    _pp_mixed = _pp_rec(_pp_trade("SPX", paper=True), _pp_trade("TSLA", paper=False))
    brief = learn._day_brief(_pp_mixed)
    check("paper: brief tags the practice call",
          "- [PAPER] SPX 7300 CALL" in brief, f"got {brief[-300:]!r}")
    check("paper: brief leaves the live call untagged",
          "\n- TSLA 7300 CALL" in brief)
    check("paper: brief tells the reviewer how to weigh practice calls",
          "no money moved" in brief)
    brief = learn._day_brief(_pp_rec(_pp_trade("TSLA", paper=False)))
    check("paper: all-live brief carries no [PAPER] anywhere",
          "[PAPER]" not in brief and "no money moved" not in brief)
    check("paper: reviewer prompt explains the tag",
          "[PAPER]" in learn.REVIEWER_SYSTEM)

    # the deterministic fallback cannot hedge its wording, so a practice
    # outcome writes no playbook lesson at all
    det = learn._deterministic_review(_pp_rec(_pp_trade("SPX", paper=True)))
    check("paper: fallback writes no lesson from a practice win",
          det["lessons"] == [], f"got {det['lessons']}")
    check("paper: fallback review says practice signals, no money moved",
          "practice signals" in det["review"], f"got {det['review']!r}")
    det = learn._deterministic_review(_pp_rec(
        _pp_trade("SPX", paper=True, won=False, exit_reason="stop",
                  banked_half=False)))
    check("paper: fallback writes no lesson from a practice stop-out",
          det["lessons"] == [], f"got {det['lessons']}")
    det = learn._deterministic_review(_pp_mixed)
    check("paper: live call still teaches its lesson in a mixed day",
          len(det["lessons"]) == 1 and "TSLA" in det["lessons"][0],
          f"got {det['lessons']}")
    det = learn._deterministic_review(_pp_rec(_pp_trade("TSLA", paper=False)))
    check("paper: all-live fallback review mentions no practice",
          "practice" not in det["review"], f"got {det['review']!r}")

    msg = learn._owner_message(_pp_mixed, {"review": "", "lessons": [],
                                           "watch_tomorrow": "",
                                           "proposed_change": None})
    check("paper: owner digest tags the practice call like the recap does",
          "[PAPER] SPX 7300 CALL" in msg and "\nTSLA 7300 CALL" in msg,
          f"got {msg!r}")

    # the flag rides from positions.json through _grade_positions into the
    # coach dossier, so both nightly reviewers see it
    _pp_book = PositionBook(_pp_path)
    for pid, is_paper in (("pp_paper", True), ("pp_real", False)):
        q = mk_pos()
        q.id = q.ticker = pid
        q.paper = is_paper
        q.state = "closed"
        q.final_pnl_pct = 12.0
        q.final_exit = {"time": "11:00:00", "pct": 12.0, "mark": mark(12.0),
                        "reason": "trail"}
        _pp_book.add(q)
    learn.et_now = lambda: datetime(2026, 6, 11, 21, 30, tzinfo=ET)
    learn._market_context = lambda d: ""
    _pp_tr = {t["ticker"]: t for t in learn._grade_positions(TODAY)}
    check("paper: graded trades carry the flag from positions.json",
          _pp_tr.get("pp_paper", {}).get("paper") is True
          and _pp_tr.get("pp_real", {}).get("paper") is False, f"got {_pp_tr}")
    _pp_doss = {t["ticker"]: t for t in coach.gather_dossier(TODAY)["trades"]}
    check("paper: coach dossier carries the flag",
          _pp_doss.get("pp_paper", {}).get("paper") is True
          and _pp_doss.get("pp_real", {}).get("paper") is False,
          f"got {_pp_doss}")
    check("paper: coach prompt explains the flag",
          '"paper": true' in coach.COACH_SYSTEM)

    # the per-trade deep review is the third writer into the digest; its
    # brief and archival record must carry the tag too
    import assistant as _pp_assistant
    _orig_reviews_pp = learn.REVIEWS_FILE
    _orig_en_pp = _pp_assistant.enabled
    _orig_cd_pp = _pp_assistant.cooldown_left_s
    _orig_comp_pp = _pp_assistant.complete
    learn.REVIEWS_FILE = config.DATA_DIR / "trade_reviews_paper_test.jsonl"
    try:
        if learn.REVIEWS_FILE.exists():
            learn.REVIEWS_FILE.unlink()
        _pp_briefs = []
        _pp_assistant.enabled = lambda: True
        _pp_assistant.cooldown_left_s = lambda: 0
        _pp_assistant.complete = lambda s, u, **k: _pp_briefs.append(u) or (
            '{"why": "clean push", "cause": "setup", '
            '"cause_detail": "", "lesson": ""}')
        _pp_n = learn.review_history()
        check("paper: deep review covers both trades", _pp_n == 2,
              f"got {_pp_n}")
        check("paper: deep-review brief tags the practice trade",
              any(b.startswith("[PAPER] Trade:") for b in _pp_briefs)
              and any(b.startswith("Trade:") for b in _pp_briefs),
              f"got {[b[:30] for b in _pp_briefs]}")
        check("paper: cause prompt explains the tag",
              "[PAPER]" in learn.CAUSE_SYSTEM)
        _pp_rows = {_json.loads(ln)["id"]: _json.loads(ln)
                    for ln in learn.REVIEWS_FILE.read_text(
                        encoding="utf-8-sig").splitlines() if ln.strip()}
        check("paper: trade_reviews.jsonl records the flag",
              _pp_rows.get("pp_paper", {}).get("paper") is True
              and _pp_rows.get("pp_real", {}).get("paper") is False,
              f"got {_pp_rows}")
    finally:
        if learn.REVIEWS_FILE.exists():
            learn.REVIEWS_FILE.unlink()
        learn.REVIEWS_FILE = _orig_reviews_pp
        _pp_assistant.enabled = _orig_en_pp
        _pp_assistant.cooldown_left_s = _orig_cd_pp
        _pp_assistant.complete = _orig_comp_pp
finally:
    for f in (learn.LESSONS_LOG, learn.LESSONS_DIGEST):
        if f.exists():
            f.unlink()
    if _pp_path.exists():
        _pp_path.unlink()
    learn.LESSONS_LOG, learn.LESSONS_DIGEST = _orig_llog, _orig_ldig
    config.POSITIONS_FILE = _orig_posfile_pp
    learn.et_now = _orig_et_now_pp
    learn._market_context = _orig_mktctx_pp

# --- learn fallback: a banked-half stop-out no longer teaches 'bank half' ---
# The deterministic stop-out lesson ("bank half into the spike instead of
# waiting") fired on any big-peak stop, including trades that DID bank half
# at +25% and then rode the runner to the hard stop (step() checks the stop
# before the give-back trail, so exit_reason is 'stop' even from half_sold).
# The permanent playbook then coached the bot to do the thing it already did.
# The banked-half case now draws the honest runner-leg lesson instead.
_bh = learn._deterministic_review(_pp_rec(
    _pp_trade("TSLA", paper=False, won=False, exit_reason="stop", mfe=38.0,
              banked_half=True)))
check("banked-half stop: lesson credits the half and blames the runner",
      len(_bh["lessons"]) == 1 and "Banking half was right" in _bh["lessons"][0]
      and "runner" in _bh["lessons"][0], f"got {_bh['lessons']}")
check("banked-half stop: the old 'bank half' advice is gone",
      "instead of waiting" not in _bh["lessons"][0], f"got {_bh['lessons']}")
check("banked-half stop: quotes the real peak",
      "+38%" in _bh["lessons"][0], f"got {_bh['lessons']}")
# _append_lesson diverts rule-change-phrased bullets to the proposal registry;
# the corrected lesson must read as an observation so it reaches the digest
check("banked-half stop: lesson survives the rule-change sanitizer",
      not learn._is_rule_change(_bh["lessons"][0]), f"got {_bh['lessons'][0]!r}")
check("banked-half stop: no dashes as punctuation in the lesson",
      "—" not in _bh["lessons"][0] and " - " not in _bh["lessons"][0]
      and "--" not in _bh["lessons"][0], f"got {_bh['lessons'][0]!r}")
_bh = learn._deterministic_review(_pp_rec(
    _pp_trade("SPX", paper=False, won=False, exit_reason="stop", mfe=22.0,
              banked_half=False)))
check("no-half stop: still teaches banking half into the spike",
      len(_bh["lessons"]) == 1 and "instead of waiting" in _bh["lessons"][0],
      f"got {_bh['lessons']}")
_bh = learn._deterministic_review(_pp_rec(
    _pp_trade("SPX", paper=True, won=False, exit_reason="stop", mfe=38.0,
              banked_half=True)))
check("banked-half stop on a paper trade: still writes no playbook lesson",
      _bh["lessons"] == [], f"got {_bh['lessons']}")

# --- learn digest: watch_tomorrow reaches the brain for exactly one session ---
# Each nightly review writes watch_tomorrow, the one line meant to shape the
# NEXT session, but _rebuild_digest only pulled the lessons array, so it was
# texted to the owner once and then evaporated. The newest watch is now pinned
# at the top of the digest as a dated FOR TODAY line and drops off once its
# target session (the next weekday after the review) has passed.
learn.LESSONS_LOG = config.DATA_DIR / "lessons_test.jsonl"
learn.LESSONS_DIGEST = config.DATA_DIR / "lessons_digest_test.md"
_orig_et_now = learn.et_now


def _lwatch(day, watch):
    e = _lentry(day, ["lesson " + day])
    e["watch_tomorrow"] = watch
    return e


def _digest_text():
    return learn.LESSONS_DIGEST.read_text(encoding="utf-8")


try:
    for f in (learn.LESSONS_LOG, learn.LESSONS_DIGEST):
        if f.exists():
            f.unlink()

    # Thursday night's review pins its watch line for Friday's session
    learn._append_lesson(_lwatch("2026-06-10", "old watch"))
    learn._append_lesson(_lwatch("2026-06-11", "fade the first spike"))
    learn.et_now = lambda: datetime(2026, 6, 11, 21, 30, tzinfo=ET)
    learn._rebuild_digest()
    txt = _digest_text()
    check("watch: newest watch pinned as a dated FOR TODAY line",
          f"FOR TODAY (my watch line from the {_tag('2026-06-11')} review): "
          "fade the first spike" in txt, f"got {txt!r}")
    check("watch: pin sits above the lesson bullets",
          txt.index("FOR TODAY") < txt.index("- ("), f"got {txt!r}")
    check("watch: pin is not itself a playbook bullet",
          not any(ln.startswith("- ") and "FOR TODAY" in ln
                  for ln in txt.splitlines()))
    check("watch: only the newest session's watch pins",
          "old watch" not in txt)

    # still valid on the Friday morning it was written for
    learn.et_now = lambda: datetime(2026, 6, 12, 9, 30, tzinfo=ET)
    learn._rebuild_digest()
    check("watch: valid through its target session",
          "fade the first spike" in _digest_text())

    # Friday night's watch survives the weekend into Monday
    learn._append_lesson(_lwatch("2026-06-12", "watch the gap fill"))
    learn.et_now = lambda: datetime(2026, 6, 15, 9, 30, tzinfo=ET)
    learn._rebuild_digest()
    check("watch: Friday watch survives the weekend into Monday",
          "watch the gap fill" in _digest_text())

    # but is gone by Tuesday, no stale guidance labeled as today's
    learn.et_now = lambda: datetime(2026, 6, 16, 9, 30, tzinfo=ET)
    learn._rebuild_digest()
    txt = _digest_text()
    check("watch: expired watch drops off the digest",
          "FOR TODAY" not in txt and "watch the gap fill" not in txt,
          f"got {txt!r}")
    check("watch: lessons survive the expired pin",
          f"- ({_tag('2026-06-12')}) lesson 2026-06-12" in txt, f"got {txt!r}")

    # deep-review rows (empty watch) and stale backfills cannot pin
    learn._append_lesson(_lentry("2026-06-16", ["deep lesson"]))
    learn._append_lesson(_lwatch("2026-06-01", "ancient backfill"))
    learn.et_now = lambda: datetime(2026, 6, 16, 21, 30, tzinfo=ET)
    learn._rebuild_digest()
    txt = _digest_text()
    check("watch: empty-watch and stale backfill rows leave no pin",
          "FOR TODAY" not in txt and "ancient backfill" not in txt,
          f"got {txt!r}")

    # a re-run of the same session keeps the newest copy of its watch, and a
    # later-appended backfill for an older date cannot steal the pin
    learn._append_lesson(_lwatch("2026-06-16", "first cut"))
    learn._append_lesson(_lwatch("2026-06-16", "second cut"))
    learn._append_lesson(_lwatch("2026-06-15", "yesterday backfill"))
    learn._rebuild_digest()
    txt = _digest_text()
    check("watch: same-session re-run keeps the newest watch",
          "second cut" in txt and "first cut" not in txt, f"got {txt!r}")
    check("watch: backfilled older session cannot steal the pin",
          "yesterday backfill" not in txt, f"got {txt!r}")

    # the reviewer's playbook block must not ingest the pin as a lesson
    brief = learn._day_brief(_quiet_record("2026-06-17"))
    check("watch: FOR TODAY line stays out of the reviewer brief",
          "FOR TODAY" not in brief and "second cut" not in brief,
          f"got {brief!r}")
finally:
    for f in (learn.LESSONS_LOG, learn.LESSONS_DIGEST):
        if f.exists():
            f.unlink()
    learn.LESSONS_LOG, learn.LESSONS_DIGEST = _orig_llog, _orig_ldig
    learn.et_now = _orig_et_now

# --- learn log idempotency: re-running a session upserts, backfills stay old ---
# run() appended a fresh row every call, so a --date backfill or a crash-refire
# between send and dedup-mark double-counted a session's lessons, and
# _rebuild_digest ordered bullets by file position, so a backfilled OLD night
# landed at the end of the file and masqueraded as the newest guidance. Nightly
# and coach rows now upsert by session (per-trade deep-review rows stay
# additive), and the digest plus the reviewer's nightly reads order by the
# actual session date.
learn.LESSONS_LOG = config.DATA_DIR / "lessons_test.jsonl"
learn.LESSONS_DIGEST = config.DATA_DIR / "lessons_digest_test.md"


def _lrow(day, review, lessons):
    e = _lentry(day, lessons)
    e["review"] = review
    return e


try:
    for f in (learn.LESSONS_LOG, learn.LESSONS_DIGEST):
        if f.exists():
            f.unlink()

    # a re-run of the same session replaces its nightly row, never stacks it
    learn._append_lesson(_lentry("2026-06-20", ["first pass"]))
    learn._append_lesson(_lentry("2026-06-20", ["second pass"]))
    rows = learn._all_lessons()
    check("upsert: re-run keeps one nightly row per session",
          len(rows) == 1 and rows[0]["lessons"] == ["second pass"],
          f"got {rows}")
    learn._rebuild_digest()
    txt = learn.LESSONS_DIGEST.read_text(encoding="utf-8")
    check("upsert: digest counts the re-run once",
          "second pass" in txt and "first pass" not in txt, f"got {txt!r}")

    # deep-review rows are per trade: two on one session both survive, and a
    # nightly re-run for that session leaves them alone
    learn._append_lesson(_lrow("2026-06-20", "deep review SPX 2026-06-20: a",
                               ["deep lesson one"]))
    learn._append_lesson(_lrow("2026-06-20", "deep review TSLA 2026-06-20: b",
                               ["deep lesson two"]))
    learn._append_lesson(_lentry("2026-06-20", ["third pass"]))
    rows = learn._all_lessons()
    check("upsert: deep-review rows stay additive across the upsert",
          sum(r["review"].startswith("deep review ") for r in rows) == 2
          and sum(not r["review"].startswith(("deep review ", "coach: "))
                  for r in rows) == 1, f"got {rows}")

    # the coach's row upserts independently of the nightly row
    learn._append_lesson(_lrow("2026-06-20", "coach: read one", ["coach A"]))
    learn._append_lesson(_lrow("2026-06-20", "coach: read two", ["coach B"]))
    rows = learn._all_lessons()
    coach_rows = [r for r in rows if r["review"].startswith("coach: ")]
    check("upsert: one coach row per session, newest wins",
          len(coach_rows) == 1 and coach_rows[0]["lessons"] == ["coach B"],
          f"got {coach_rows}")
    check("upsert: nightly row survives the coach upsert",
          any(r["lessons"] == ["third pass"] for r in rows), f"got {rows}")

    # a line the parser cannot read is preserved by the rewrite, not destroyed
    with learn.LESSONS_LOG.open("a", encoding="utf-8") as f:
        f.write("{corrupt json\n")
    learn._append_lesson(_lentry("2026-06-20", ["fourth pass"]))
    raw = learn.LESSONS_LOG.read_text(encoding="utf-8")
    check("upsert: unparseable line survives the rewrite",
          "{corrupt json" in raw, f"got {raw!r}")

    # a backfilled old session appended LAST cannot masquerade as the newest
    # guidance, in the digest or in the reviewer's nightly reads
    learn._append_lesson(_lentry("2026-06-21", ["newest real lesson"]))
    learn._append_lesson(_lentry("2026-06-02", ["ancient backfill lesson"]))
    learn._rebuild_digest()
    got = _digest_bullets()
    check("digest: bullets order by session date, not append order",
          "newest real lesson" in got[0] and "ancient backfill" in got[-1],
          f"got {got}")
    brief = learn._day_brief(_quiet_record("2026-06-22"))
    reads = [ln for ln in brief.splitlines() if ln.startswith("- 2026-")]
    check("brief: nightly reads order by session date, not append order",
          reads and reads[0].startswith("- 2026-06-21"), f"got {reads}")

    # a malformed legacy session (an int, junk text) sorts oldest and tags
    # as-is instead of aborting the whole digest rebuild
    bad = _lentry("2026-06-18", ["int session lesson"])
    bad["session"] = 20260618
    learn._append_lesson(bad)
    learn._rebuild_digest()
    got = _digest_bullets()
    check("digest: malformed legacy session cannot abort the rebuild",
          any("int session lesson" in ln for ln in got)
          and "newest real lesson" in got[0], f"got {got}")
finally:
    for f in (learn.LESSONS_LOG, learn.LESSONS_DIGEST):
        if f.exists():
            f.unlink()
    learn.LESSONS_LOG, learn.LESSONS_DIGEST = _orig_llog, _orig_ldig

# --- telegram 409 conflict: a second bot instance must not be invisible ---
# Two processes polling the same token make Telegram answer 409 Conflict.
# That body has no "result", so it used to parse as a quiet chat and the
# owner never learned commands were being split (and alerts possibly
# doubled). get_messages now records the conflict, and handle_commands DMs
# the owner about it once per day.
import os

from scanner import Service


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _Sess:
    def __init__(self):
        self.resp = None

    def get(self, *a, **k):
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


_CONFLICT_BODY = {"ok": False, "error_code": 409,
                  "description": "Conflict: terminated by other getUpdates "
                                 "request; make sure that only one bot "
                                 "instance is running"}
_orig_tg_session = telegram._session
_orig_tg_token = telegram._token
_orig_tg_send_to = telegram.send_to
_orig_state_file = config.STATE_FILE
_orig_env = {k: os.environ.get(k) for k in ("TELEGRAM_CHAT_IDS", "OWNER_CHAT_ID")}
sess = _Sess()
dm = []
try:
    telegram._session = sess
    telegram._token = lambda: "TEST"
    config.STATE_FILE = config.DATA_DIR / "state_conflict_test.json"
    if config.STATE_FILE.exists():
        config.STATE_FILE.unlink()
    os.environ["TELEGRAM_CHAT_IDS"] = "111"
    os.environ["OWNER_CHAT_ID"] = "111"

    # a 409 poll yields no items but raises the flag
    sess.resp = _Resp(409, _CONFLICT_BODY)
    items, max_id = telegram.get_messages()
    check("409: poll returns no items and keeps the offset",
          items == [] and max_id == 0)
    c = telegram.poll_conflict()
    check("409: conflict recorded with Telegram's description",
          c is not None and "one bot instance" in c, f"got {c!r}")
    check("409: reading the conflict clears it",
          telegram.poll_conflict() is None)

    # a clean poll parses normally and leaves no flag
    sess.resp = _Resp(200, {"ok": True, "result": [
        {"update_id": 7, "message": {"chat": {"id": 111}, "text": "/status"}}]})
    items, max_id = telegram.get_messages()
    check("409: clean poll still parses messages",
          len(items) == 1 and items[0]["kind"] == "command" and max_id == 7)
    check("409: clean poll leaves no conflict flag",
          telegram.poll_conflict() is None)

    # a network error stays a quiet empty poll, not a conflict
    import requests as _rq
    sess.resp = _rq.RequestException("boom")
    items, _ = telegram.get_messages()
    check("409: network error returns empty and flags nothing",
          items == [] and telegram.poll_conflict() is None)

    # end to end: handle_commands warns the owner once per day, not per poll
    svc = Service.__new__(Service)
    svc.dry = False
    telegram.send_to = lambda cid, text: dm.append((str(cid), text)) or None
    sess.resp = _Resp(409, _CONFLICT_BODY)
    svc.handle_commands()
    check("409: owner warned about the second instance",
          len(dm) == 1 and dm[0][0] == "111"
          and "another copy" in dm[0][1] and "409" in dm[0][1],
          f"got {dm!r}")
    svc.handle_commands()
    check("409: same-day repeat polls do not re-warn", len(dm) == 1)
finally:
    telegram._session = _orig_tg_session
    telegram._token = _orig_tg_token
    telegram.send_to = _orig_tg_send_to
    if config.STATE_FILE.exists():
        config.STATE_FILE.unlink()
    config.STATE_FILE = _orig_state_file
    for k, v in _orig_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

# --- weekly scoreboard: NEW-vs-OLD verdict scored on one common trade set ---
# weekly_report summed NEW over every closed trade but OLD only over trades
# whose old-rules shadow finished, then declared a winner from that
# apples-to-oranges diff. One trade whose shadow never closed (non-comparable
# marks all day) could hand NEW a phantom win. The verdict now diffs the two
# rule sets on the same trades and says how many it covers.


def wk_pos(pid, final, old_exit):
    p = mk_pos()
    p.id = pid
    p.state = "closed"
    p.final_pnl_pct = final
    if old_exit is not None:
        p.old_rules = {"status": "closed", "exit_pct": old_exit,
                       "exit_reason": "old target", "exit_time": "10:00:00"}
    return p


wbook = PositionBook.__new__(PositionBook)  # never touches positions.json
wbook.positions = [wk_pos("w1", -10.0, 15.0),
                   wk_pos("w2", 5.0, 15.0),
                   wk_pos("w3", 80.0, None)]  # shadow never finished
rep = scoreboard.weekly_report(wbook, None, None, TODAY)
check("weekly: headline total still counts every trade",
      "this week: +75%" in rep, f"got {rep!r}")
check("weekly: verdict diffs only trades both rule sets finished "
      "(was NEW by 45 on mismatched sets)",
      "This week's winner: OLD rules by 35 points" in rep, f"got {rep!r}")
check("weekly: report says which trades the verdict covers",
      "2 of 3 trades where the old shadow finished: +30%" in rep
      and "NEW rules on those same 2 trades: -5%" in rep, f"got {rep!r}")
cmp_lines = [ln for ln in rep.splitlines()
             if "old shadow" in ln or "those same" in ln or "winner" in ln]
check("weekly: comparison copy has no em dash",
      cmp_lines and all("—" not in ln for ln in cmp_lines),
      f"got {cmp_lines}")

wbook.positions = [wk_pos("w1", 40.0, 26.0), wk_pos("w2", -30.0, -30.0)]
rep = scoreboard.weekly_report(wbook, None, None, TODAY)
check("weekly: full-shadow week keeps the exact-same-entries wording",
      "on the exact same entries: -4%" in rep
      and "This week's winner: NEW rules by 14 points" in rep, f"got {rep!r}")

wbook.positions = [wk_pos("w1", 40.0, None)]
wbook.positions[0].old_rules = None  # legacy null record must not crash
rep = scoreboard.weekly_report(wbook, None, None, TODAY)
check("weekly: no finished shadow means no verdict at all",
      "winner" not in rep and "OLD exit rules" not in rep, f"got {rep!r}")

# the OLD label quotes exact numbers only when every compared trade shares
# ONE pinned bracket; a mixed or legacy-unpinned week says so instead of
# quoting +15/-60 numbers that may be wrong for some of the summed trades
wbook.positions = [wk_pos("w1", 40.0, 26.0), wk_pos("w2", -30.0, -30.0)]
for wp in wbook.positions:
    wp.old_bracket = {"target_pct": 20, "stop_pct": -50}
rep = scoreboard.weekly_report(wbook, None, None, TODAY)
check("weekly: uniform pinned week quotes that bracket's real numbers",
      "OLD exit rules (+20/-50) on the exact same entries" in rep,
      f"got {rep!r}")

wbook.positions[1].old_bracket = {"target_pct": 15, "stop_pct": -60}
rep = scoreboard.weekly_report(wbook, None, None, TODAY)
check("weekly: mixed-bracket week admits it instead of quoting one bracket",
      "OLD exit rules (each trade's own entry-day bracket)" in rep
      and "+20/-50" not in rep and "+15/-60" not in rep, f"got {rep!r}")

wbook.positions[1].old_bracket = None  # legacy trade, judged day by day
rep = scoreboard.weekly_report(wbook, None, None, TODAY)
check("weekly: a legacy unpinned trade also drops the numbers",
      "OLD exit rules (each trade's own entry-day bracket)" in rep,
      f"got {rep!r}")

# --- learn catch-up: a review missed during an outage is no longer lost ---
# maybe_learn only ever considered now.date(), so a bot down across the whole
# 21:00-23:45 window (a Friday-night redeploy rolling into the weekend, a
# crash loop) lost that session's review forever while recap and weekly both
# catch up. It now targets the most recent completed weekday session, catches
# up at most that one session, and grades any session (tonight's included)
# only when positions or that day's MORNING CARD prove the bot actually ran,
# so an outage day is never graded as a clean stay-out. The recap does not
# count as evidence: its catch-up fires on the same night tick right before
# maybe_learn, so a bot dead all day and restarted at 21:00 would look alive
# by its own recap. 2026-06-11 is a Thursday, 6/12 Friday, 6/13-14 weekend.

check("catchup: weekday past the window reviews today",
      scannermod.learn_session_due(datetime(2026, 6, 11, 23, 50, tzinfo=ET))
      == date(2026, 6, 11))
check("catchup: weekday before the window points at yesterday",
      scannermod.learn_session_due(datetime(2026, 6, 11, 20, 0, tzinfo=ET))
      == date(2026, 6, 10))
check("catchup: Saturday points at Friday",
      scannermod.learn_session_due(datetime(2026, 6, 13, 9, 0, tzinfo=ET))
      == date(2026, 6, 12))
check("catchup: Monday pre-open still points at Friday",
      scannermod.learn_session_due(datetime(2026, 6, 15, 6, 0, tzinfo=ET))
      == date(2026, 6, 12))
check("catchup: Monday past the window reviews Monday",
      scannermod.learn_session_due(datetime(2026, 6, 15, 23, 50, tzinfo=ET))
      == date(2026, 6, 15))

_orig_learn_run = learn.run
_orig_state_file2 = config.STATE_FILE
lruns = []
try:
    config.STATE_FILE = config.DATA_DIR / "state_learn_test.json"
    if config.STATE_FILE.exists():
        config.STATE_FILE.unlink()
    learn.run = lambda require_date=None, dry=False: lruns.append(require_date) or []

    lbook = PositionBook.__new__(PositionBook)  # never touches positions.json
    lbook.positions = []
    svc = Service.__new__(Service)
    svc.dry = False
    svc.book = lbook

    # dead all day, restarted at night: nothing proves the bot ran the
    # session, so tonight's review is skipped and marked, never graded
    svc.maybe_learn(datetime(2026, 6, 11, 23, 50, tzinfo=ET))
    check("outage guard: same night with no evidence is skipped, not graded",
          lruns == [] and config.state_get("learn_sent") == "2026-06-11")

    # the recap its own catch-up just sent is NOT evidence the bot was alive
    # during the session (it fires on the same night tick right before learn)
    config.state_set("learn_sent", None)
    config.state_set("recap_sent", "2026-06-11")
    svc.maybe_learn(datetime(2026, 6, 11, 23, 50, tzinfo=ET))
    check("outage guard: tonight's catch-up recap alone is not proof of life",
          lruns == [] and config.state_get("learn_sent") == "2026-06-11")

    # the normal same-night path: the morning card went out at 9:15, so the
    # quiet day was a choice; fires once, dedups after
    config.state_set("learn_sent", None)
    config.state_set("morning_sent", "2026-06-11")
    svc.maybe_learn(datetime(2026, 6, 11, 23, 50, tzinfo=ET))
    check("catchup: same night still runs tonight's review",
          lruns == ["2026-06-11"]
          and config.state_get("learn_sent") == "2026-06-11")
    svc.maybe_learn(datetime(2026, 6, 11, 23, 55, tzinfo=ET))
    check("catchup: once-per-session dedup holds", lruns == ["2026-06-11"])

    # bot down across Friday's whole window, back Saturday: Friday tracked a
    # position, so the weekend tick backfills that review instead of losing it
    fri = mk_pos()
    fri.date = "2026-06-12"
    lbook.positions = [fri]
    svc.maybe_learn(datetime(2026, 6, 13, 9, 0, tzinfo=ET))
    check("catchup: weekend tick backfills the missed Friday review",
          lruns == ["2026-06-11", "2026-06-12"]
          and config.state_get("learn_sent") == "2026-06-12")
    svc.maybe_learn(datetime(2026, 6, 14, 9, 0, tzinfo=ET))
    check("catchup: Sunday does not run it again", len(lruns) == 2)
    svc.maybe_learn(datetime(2026, 6, 15, 20, 0, tzinfo=ET))
    check("catchup: pre-window Monday tick never fires early", len(lruns) == 2)

    # outage day with no positions and no morning card: marked handled, never
    # graded. A Friday recap sent by the evening catch-up after a dead day
    # does not count as proof the bot ran the session.
    lbook.positions = []
    config.state_set("learn_sent", "2026-06-11")
    config.state_set("recap_sent", "2026-06-12")
    svc.maybe_learn(datetime(2026, 6, 13, 9, 0, tzinfo=ET))
    check("catchup: a day with no evidence the bot ran is skipped, not graded",
          len(lruns) == 2 and config.state_get("learn_sent") == "2026-06-12")

    # but Friday's morning card proves the bot was up that trading day, so a
    # quiet (zero-position) Friday still gets its review
    config.state_set("learn_sent", "2026-06-11")
    config.state_set("morning_sent", "2026-06-12")
    svc.maybe_learn(datetime(2026, 6, 13, 9, 0, tzinfo=ET))
    check("catchup: a quiet day whose morning card went out gets its review",
          lruns == ["2026-06-11", "2026-06-12", "2026-06-12"])

    # delivery errors on a catch-up: bounded retries keyed by the session
    learn.run = lambda require_date=None, dry=False: ["send failed"]
    config.state_set("learn_sent", "2026-06-11")
    config.state_set("learn_tries", {})
    svc.maybe_learn(datetime(2026, 6, 13, 9, 30, tzinfo=ET))
    check("catchup: first delivery error stays retryable, keyed by session",
          config.state_get("learn_sent") == "2026-06-11"
          and config.state_get("learn_tries") == {"2026-06-12": 1})
    svc.maybe_learn(datetime(2026, 6, 13, 9, 31, tzinfo=ET))
    check("catchup: second delivery error marks it done, no all-night retries",
          config.state_get("learn_sent") == "2026-06-12")

    # dry mode never reviews and never touches state
    svc.dry = True
    config.state_set("learn_sent", "2026-06-11")
    svc.maybe_learn(datetime(2026, 6, 13, 10, 0, tzinfo=ET))
    check("catchup: dry mode never runs or marks",
          config.state_get("learn_sent") == "2026-06-11")
finally:
    learn.run = _orig_learn_run
    if config.STATE_FILE.exists():
        config.STATE_FILE.unlink()
    config.STATE_FILE = _orig_state_file2

# --- est_entry backfill: throttled vol at entry no longer kills the stop ---
# If the vol download was throttled at entry, est_entry was stored 0.0 and
# est_pct stayed None for the position's whole life; the first stale/bid-less
# quote stretch then had NO stop signal at all. monitor_one now rebuilds the
# baseline (the model price at the recorded entry moment) the first cycle vol
# is available, so the estimate stop-floor works again.
import quotes


class _StubFeed:
    def __init__(self, px):
        self.px = px

    def latest_price(self, yfs):
        return self.px


class _StubBook:
    def __init__(self):
        self.saves = 0

    def save(self):
        self.saves += 1


def _monitor_svc(spot, sigma):
    svc = Service.__new__(Service)  # plumbing only: every I/O path is stubbed
    svc.cfg = scannermod.StrategyConfig()
    svc.feed = _StubFeed(spot)
    svc.book = _StubBook()
    svc.old_bracket = BRACKET
    svc.get_bars = lambda yfs, now: None
    svc.sigma = lambda t: sigma
    svc.notified = []
    svc.notify = lambda card: svc.notified.append(card) or []
    return svc


_orig_get_quote = quotes.get_option_quote
quotes.get_option_quote = lambda *a, **k: None  # chain unavailable all cycle
try:
    _expiry16 = datetime.combine(TODAY, time(16, 0), tzinfo=ET)
    _entry_dt = datetime(2026, 6, 11, 9, 50, tzinfo=ET)

    # vol back + underlying collapsed: baseline is rebuilt AT THE ENTRY MOMENT
    # and the restored stop-floor fires the exit that used to be impossible
    p = mk_pos()  # est_entry defaults to 0.0 = throttled at entry
    svc = _monitor_svc(spot=7200.0, sigma=0.25)
    svc.monitor_one(p, at(11, 0))
    want = quotes.estimate_premium(7297.0, 7300.0, "C", _expiry16,
                                   _entry_dt, 0.25)
    check("backfill: baseline rebuilt at the recorded entry moment",
          want > 0 and abs(p.est_entry - want) < 0.005,
          f"est_entry {p.est_entry}, want {want}")
    check("backfill: restored stop-floor fires on the collapsed underlying",
          p.state == "closed" and p.final_exit is not None
          and p.final_exit["reason"] == "stop" and len(svc.notified) == 1,
          f"state {p.state}, notified {len(svc.notified)}")
    check("backfill: closed position persisted", svc.book.saves == 1)

    # vol STILL throttled: no invented baseline, no false signal, cycle skipped
    p = mk_pos()
    svc = _monitor_svc(spot=7200.0, sigma=0.0)
    svc.monitor_one(p, at(11, 0))
    check("backfill: sigma 0 invents nothing and fires nothing",
          p.est_entry == 0.0 and p.state == "open" and svc.notified == [])

    # a healthy position's baseline is pinned at entry, never re-priced
    p = mk_pos()
    p.est_entry = 5.0
    svc = _monitor_svc(spot=7297.0, sigma=0.25)
    svc.monitor_one(p, at(10, 0))
    check("backfill: existing baseline is left untouched",
          p.est_entry == 5.0 and p.state == "open")

    # malformed legacy records skip the repair instead of killing the cycle
    p = mk_pos()
    p.time_et = "not-a-time"
    svc = _monitor_svc(spot=7200.0, sigma=0.25)
    svc.monitor_one(p, at(11, 0))
    check("backfill: malformed time_et skips repair, monitoring survives",
          p.est_entry == 0.0 and p.state == "open")

    p = mk_pos()
    p.spot_at_signal = 0.0
    svc = _monitor_svc(spot=7200.0, sigma=0.25)
    svc.monitor_one(p, at(11, 0))
    check("backfill: legacy record without spot_at_signal is skipped",
          p.est_entry == 0.0 and p.state == "open")
finally:
    quotes.get_option_quote = _orig_get_quote

# --- entry pins the old-rules bracket on the position ---
# reload_tunables re-chooses old_bracket every date flip from the overnight
# backtest, so a multi-day weekly's shadow used to be judged the next day
# under a bracket it was never opened on. open_position now stores a COPY of
# the entry-time bracket on the position, and step() judges the shadow by it.


class _EntryBook:
    def __init__(self):
        self.added = []

    def open_same_direction(self, direction):
        return False

    def add(self, pos):
        self.added.append(pos)


_orig_get_quote2 = quotes.get_option_quote
_orig_earnings = scannermod.news.earnings_inside
_orig_stats_card = scannermod.scoreboard.stats_for_card
try:
    quotes.get_option_quote = lambda *a, **k: None  # entry priced from estimate
    scannermod.news.earnings_inside = lambda t, e: (False, None)
    scannermod.scoreboard.stats_for_card = lambda *a, **k: {
        "win_rate": 72.0, "avg_win_pct": 30.0, "avg_loss_pct": -25.0,
        "expectancy_pct": 9.0, "ev_pct": 9.0, "trades": 60,
        "start": "01/01/2026", "end": "06/01/2026", "label": "test stats",
        "costs_note": "after est. costs", "source": "backtest_old"}
    svc = Service.__new__(Service)  # plumbing only: every I/O path is stubbed
    svc.dry = True                  # skips record_alert
    svc.book = _EntryBook()
    svc.backtest_old = svc.backtest_new = None
    svc.sigma = lambda t: 0.25
    svc.current_mode = lambda: ("green", "test")
    svc.notify = lambda card: []
    svc.old_bracket = {"target_pct": 15, "stop_pct": -60}
    entry_setup = Setup(ticker="SPX", direction="call", strike=7300.0,
                        spot=7297.0, mom_pct=0.21, reason="t")
    done = svc.open_position(entry_setup, at(9, 50))
    check("pin: open_position tracks the position",
          done is True and len(svc.book.added) == 1)
    ep = svc.book.added[0]
    check("pin: entry stores the entry-time bracket",
          ep.old_bracket == {"target_pct": 15, "stop_pct": -60})
    # the pin is a copy: tonight's reload mutating svc.old_bracket can't reach it
    svc.old_bracket["target_pct"] = 99
    check("pin: a later bracket reload cannot mutate the pin",
          ep.old_bracket == {"target_pct": 15, "stop_pct": -60})
    # the exact BACKLOG drift: next day the backtest chose +99/-90, but the
    # shadow still takes the +15 target it was opened under
    evs = poslib.step(ep, at(10, 0), ep.entry_mid * 1.16, "test", 16.0, False,
                      {"target_pct": 99, "stop_pct": -90}, comparable=False)
    check("pin: non-comparable cycle leaves the shadow open",
          ep.old_rules["status"] == "open")
    poslib.step(ep, at(10, 5), ep.entry_mid * 1.16, "estimated", 16.0, False,
                {"target_pct": 99, "stop_pct": -90}, comparable=True)
    check("pin: shadow takes the +15 target it opened under, not today's +99",
          ep.old_rules["status"] == "closed"
          and ep.old_rules["exit_reason"] == "old target")
finally:
    quotes.get_option_quote = _orig_get_quote2
    scannermod.news.earnings_inside = _orig_earnings
    scannermod.scoreboard.stats_for_card = _orig_stats_card

# --- once-per-day attempt cap: a crash between a job's send and its dedup
# mark can no longer re-broadcast the report indefinitely ---
# Every once-per-day job (morning card, recap, request digest, nightly
# review, weekly) sends first and writes its dedup key after, so a restart
# landing in between re-broadcast the whole report on every start (railway
# restartPolicy is ALWAYS, so a crash loop could spam it for hours), and
# weekly retried delivery errors unbounded all weekend. Each job now records
# its attempt BEFORE sending: the first re-broadcast after a crash is allowed
# (duplicate beats missing), the next is not, and delivery-error retries ride
# the same counter.
import intake
import recap as recapmod
import telegram

_orig_recap_main = recapmod.main
_orig_learn_run2 = learn.run
_orig_weekly_rep = scannermod.scoreboard.weekly_report
_orig_news_lines = scannermod.news.morning_lines
_orig_digest = intake.digest
_orig_owner_id = telegram.primary_owner_id
_orig_send_to = telegram.send_to
_orig_state_file3 = config.STATE_FILE
try:
    config.STATE_FILE = config.DATA_DIR / "state_attempts_test.json"
    if config.STATE_FILE.exists():
        config.STATE_FILE.unlink()

    def _job_svc():
        svc = Service.__new__(Service)  # plumbing only: no live I/O paths
        svc.dry = False
        return svc

    # morning card: crash between notify and the morning_sent write
    scannermod.news.morning_lines = lambda watchlist: []
    m_now = datetime(2026, 6, 11, 9, 0, tzinfo=ET)  # Thu, before entry window
    m_day = str(m_now.date())
    config.state_set("risk_auto", {"date": m_day, "mode": "green",
                                   "reason": "test", "gap": True})
    msvc = _job_svc()
    msvc.cfg = scannermod.StrategyConfig()
    msvc.mode, msvc.mode_reason = "green", ""
    msvc.morning_sent_for = None
    msvc.premarket_sent_for = None
    m_sent = []
    msvc.notify = lambda text: m_sent.append(text) or []
    msvc.morning_report(m_now)
    check("attempts: morning card sends once and marks sent",
          len(m_sent) == 1 and config.state_get("morning_sent") == m_day
          and config.state_get("morning_tries") == {m_day: 1})
    config.state_set("morning_sent", None)  # crashed before the mark
    msvc.morning_sent_for = None            # ...and restarted
    msvc.morning_report(m_now)
    check("attempts: one crash re-broadcast is allowed (duplicate beats missing)",
          len(m_sent) == 2 and config.state_get("morning_sent") == m_day)
    config.state_set("morning_sent", None)
    msvc.morning_sent_for = None
    msvc.morning_report(m_now)
    check("attempts: a morning restart loop cannot broadcast a third time",
          len(m_sent) == 2 and config.state_get("morning_sent") == m_day)

    # recap: same crash shape, plus STALE refunds and bounded delivery errors
    r_now = datetime(2026, 6, 11, 16, 10, tzinfo=ET)
    r_calls = []
    recapmod.main = lambda require_date=None: r_calls.append(require_date) or []
    rsvc = _job_svc()
    rsvc.maybe_recap(r_now)
    check("attempts: recap sends once and marks sent",
          r_calls == [m_day] and config.state_get("recap_sent") == m_day)
    config.state_set("recap_sent", None)
    rsvc.maybe_recap(r_now)
    config.state_set("recap_sent", None)
    rsvc.maybe_recap(r_now)
    check("attempts: recap restart loop capped at two broadcasts",
          len(r_calls) == 2 and config.state_get("recap_sent") == m_day)

    config.state_set("recap_sent", None)
    config.state_set("recap_tries", {})
    recapmod.main = lambda require_date=None: "STALE"
    for _ in range(5):
        rsvc.maybe_recap(r_now)
    check("attempts: STALE passes refund the attempt, never burn the cap",
          config.state_get("recap_sent") is None
          and config.state_get("recap_tries") == {m_day: 0})
    recapmod.main = lambda require_date=None: r_calls.append(require_date) or []
    rsvc.maybe_recap(r_now)
    check("attempts: recap still sends once the data catches up",
          len(r_calls) == 3 and config.state_get("recap_sent") == m_day)

    config.state_set("recap_sent", None)
    config.state_set("recap_tries", {})
    recapmod.main = lambda require_date=None: ["403: blocked"]
    rsvc.maybe_recap(r_now)
    check("attempts: first recap delivery error stays retryable",
          config.state_get("recap_sent") is None
          and config.state_get("recap_tries") == {m_day: 1})
    rsvc.maybe_recap(r_now)
    check("attempts: second recap delivery error marks done",
          config.state_get("recap_sent") == m_day)

    # nightly review: a crash re-fire re-runs the review once, not forever
    l_now = datetime(2026, 6, 11, 23, 50, tzinfo=ET)
    l_runs = []
    learn.run = lambda require_date=None, dry=False: l_runs.append(require_date) or []
    lsvc = _job_svc()
    lsvc.book = PositionBook.__new__(PositionBook)
    lsvc.book.positions = []
    config.state_set("learn_sent", None)
    config.state_set("learn_tries", {})
    lsvc.maybe_learn(l_now)
    config.state_set("learn_sent", None)
    lsvc.maybe_learn(l_now)
    config.state_set("learn_sent", None)
    lsvc.maybe_learn(l_now)
    check("attempts: a crash loop cannot re-run the nightly review a third time",
          len(l_runs) == 2 and config.state_get("learn_sent") == m_day)

    # weekly: crash cap, plus its FIRST bounded delivery-error retry (it used
    # to re-run and re-send every daemon tick all weekend)
    scannermod.scoreboard.weekly_report = lambda *a, **k: "WEEKLY REPORT"
    w_now = datetime(2026, 6, 12, 16, 10, tzinfo=ET)  # Friday after the close
    w_key = w_now.strftime("%G-W%V")
    wsvc = _job_svc()
    wsvc.book = None
    wsvc.backtest_old = None
    wsvc.backtest_new = None
    w_sent = []
    wsvc.notify = lambda text: w_sent.append(text) or []
    wsvc.maybe_weekly(w_now)
    check("attempts: weekly sends once and marks the ISO week",
          len(w_sent) == 1 and config.state_get("weekly_sent") == w_key)
    config.state_set("weekly_sent", None)
    wsvc.maybe_weekly(w_now)
    config.state_set("weekly_sent", None)
    wsvc.maybe_weekly(w_now)
    check("attempts: weekly restart loop capped at two broadcasts",
          len(w_sent) == 2 and config.state_get("weekly_sent") == w_key)

    config.state_set("weekly_sent", None)
    config.state_set("weekly_tries", {})
    wsvc.notify = lambda text: w_sent.append(text) or ["network down"]
    wsvc.maybe_weekly(w_now)
    check("attempts: first weekly delivery error stays retryable",
          config.state_get("weekly_sent") is None)
    wsvc.maybe_weekly(w_now)
    check("attempts: weekly delivery errors bounded, job marked done",
          config.state_get("weekly_sent") == w_key)

    # request digest (owner-only): same cap, same bounded error retries
    intake.digest = lambda: "OPEN REQUESTS"
    telegram.primary_owner_id = lambda: 111
    d_sent = []
    telegram.send_to = lambda chat, msg: d_sent.append(msg) or ""
    dsvc = _job_svc()
    dsvc.maybe_request_digest(r_now)
    check("attempts: request digest sends once and marks sent",
          len(d_sent) == 1 and config.state_get("request_digest_sent") == m_day)
    config.state_set("request_digest_sent", None)
    dsvc.maybe_request_digest(r_now)
    config.state_set("request_digest_sent", None)
    dsvc.maybe_request_digest(r_now)
    check("attempts: digest restart loop capped at two sends",
          len(d_sent) == 2 and config.state_get("request_digest_sent") == m_day)

    config.state_set("request_digest_sent", None)
    config.state_set("request_digest_tries", {})
    telegram.send_to = lambda chat, msg: "403: blocked"
    dsvc.maybe_request_digest(r_now)
    check("attempts: first digest send error stays retryable",
          config.state_get("request_digest_sent") is None)
    dsvc.maybe_request_digest(r_now)
    check("attempts: digest send errors bounded, job marked done",
          config.state_get("request_digest_sent") == m_day)

    # dry mode never counts an attempt or writes any dedup key
    config.state_set("weekly_sent", None)
    config.state_set("weekly_tries", {})
    drysvc = _job_svc()
    drysvc.dry = True
    drysvc.book = None
    drysvc.backtest_old = None
    drysvc.backtest_new = None
    drysvc.notify = lambda text: []
    drysvc.maybe_weekly(w_now)
    check("attempts: dry mode never counts or marks",
          config.state_get("weekly_sent") is None
          and config.state_get("weekly_tries") == {})
finally:
    recapmod.main = _orig_recap_main
    learn.run = _orig_learn_run2
    scannermod.scoreboard.weekly_report = _orig_weekly_rep
    scannermod.news.morning_lines = _orig_news_lines
    intake.digest = _orig_digest
    telegram.primary_owner_id = _orig_owner_id
    telegram.send_to = _orig_send_to
    if config.STATE_FILE.exists():
        config.STATE_FILE.unlink()
    config.STATE_FILE = _orig_state_file3

# --- nightly review: the deep brain writes it, with a real fallback chain ---
# synthesize ran the highest-leverage reasoning task in the system (the
# nightly review whose lessons steer every future reply) on the everyday
# model with no extended thinking. It now tries assistant.complete_deep
# (deep model, adaptive thinking) first, falls back to the everyday
# complete(), then to the deterministic review, so a deep-brain outage can
# never cost the night.

_VALID_DEEP = ('{"review": "deep read", "lessons": ["deep lesson"], '
               '"watch_tomorrow": "watch", "proposed_change": null}')
_VALID_EVERYDAY = ('{"review": "everyday read", "lessons": [], '
                   '"watch_tomorrow": "", "proposed_change": null}')

_orig_api_key = os.environ.get("ANTHROPIC_API_KEY")
_orig_post = assistant._post_anthropic
_orig_cdeep, _orig_comp = assistant.complete_deep, assistant.complete
_orig_llog5, _orig_ldig5 = learn.LESSONS_LOG, learn.LESSONS_DIGEST
learn.LESSONS_LOG = config.DATA_DIR / "lessons_test.jsonl"
learn.LESSONS_DIGEST = config.DATA_DIR / "lessons_digest_test.md"
try:
    # the payload complete_deep sends: deep model, adaptive thinking, the
    # caller's system prompt, and only the text blocks come back
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    seen = {}

    def _fake_post(payload, timeout):
        seen["payload"], seen["timeout"] = payload, timeout
        return {"content": [{"type": "thinking", "thinking": "hmm"},
                            {"type": "text", "text": "part one "},
                            {"type": "text", "text": "and two"}]}, None

    assistant._post_anthropic = _fake_post
    out = assistant.complete_deep("SYS PROMPT", "USER BRIEF")
    _p = seen["payload"]
    check("deep review: complete_deep runs the deep model with thinking on",
          _p["model"] == assistant.deep_model()
          and _p["thinking"] == {"type": "adaptive"}
          and _p["system"] == "SYS PROMPT"
          and _p["messages"] == [{"role": "user", "content": "USER BRIEF"}],
          f"got {_p}")
    check("deep review: complete_deep returns text blocks only",
          out == "part one and two", f"got {out!r}")

    assistant._post_anthropic = lambda payload, timeout: (None, "boom")
    check("deep review: API failure returns None so callers can fall back",
          assistant.complete_deep("s", "u") is None)

    assistant._post_anthropic = lambda payload, timeout: (
        {"content": [{"type": "thinking", "thinking": "x"}]}, None)
    check("deep review: an empty completion returns None, not ''",
          assistant.complete_deep("s", "u") is None)

    del os.environ["ANTHROPIC_API_KEY"]
    assistant._post_anthropic = _orig_post
    check("deep review: no API key means None with no network attempt",
          assistant.complete_deep("s", "u") is None)

    # synthesize prefers the deep answer; the everyday model is never called
    calls = []
    assistant.complete_deep = lambda s, u, **k: calls.append("deep") or _VALID_DEEP
    assistant.complete = lambda s, u, **k: calls.append("everyday") or _VALID_EVERYDAY
    got = learn.synthesize(_quiet_record("2026-06-11"))
    check("deep review: synthesize uses the deep answer when it lands",
          got["review"] == "deep read" and got["lessons"] == ["deep lesson"]
          and calls == ["deep"], f"got {got.get('review')!r}, calls {calls}")

    # a dead deep brain hands the night to the everyday model
    calls = []
    assistant.complete_deep = lambda s, u, **k: calls.append("deep") or None
    got = learn.synthesize(_quiet_record("2026-06-11"))
    check("deep review: dead deep brain falls back to the everyday model",
          got["review"] == "everyday read" and calls == ["deep", "everyday"],
          f"got {got.get('review')!r}, calls {calls}")

    # both brains get the same reviewer prompt and the same day brief
    seen_args = []
    assistant.complete_deep = lambda s, u, **k: seen_args.append((s, u)) or None
    assistant.complete = lambda s, u, **k: seen_args.append((s, u)) or None
    got = learn.synthesize(_quiet_record("2026-06-11"))
    # compare against reviewer_system(), not the REVIEWER_SYSTEM constant:
    # on a box with a real live_params.json the rendered prompt differs from
    # the built-in render, and that is correct, not a failure
    check("deep review: both brains get the same prompt and brief",
          len(seen_args) == 2 and seen_args[0] == seen_args[1]
          and seen_args[0][0] == learn.reviewer_system())
    check("deep review: no brain at all still writes the deterministic review",
          "stayed out" in got["review"], f"got {got.get('review')!r}")
finally:
    if _orig_api_key is None:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    else:
        os.environ["ANTHROPIC_API_KEY"] = _orig_api_key
    assistant._post_anthropic = _orig_post
    assistant.complete_deep, assistant.complete = _orig_cdeep, _orig_comp
    for f in (learn.LESSONS_LOG, learn.LESSONS_DIGEST):
        if f.exists():
            f.unlink()
    learn.LESSONS_LOG, learn.LESSONS_DIGEST = _orig_llog5, _orig_ldig5

# --- rule-change proposals: tracked, deduped, decidable, digest-safe ---
# proposed_change had no dedup or status tracking, so the same idea got
# re-pitched night after night with no record of any decision, and a rule
# change the model phrased as a plain lesson walked straight into the digest
# the brain reads. Proposals now live in state.json with a status, repeats
# are counted instead of re-pitched, /proposals lists and decides them, and
# rule-change-phrased lessons are routed into the same channel.

_orig_state_file4 = config.STATE_FILE
_orig_llog7, _orig_ldig7 = learn.LESSONS_LOG, learn.LESSONS_DIGEST
learn.LESSONS_LOG = config.DATA_DIR / "lessons_test.jsonl"
learn.LESSONS_DIGEST = config.DATA_DIR / "lessons_digest_test.md"
try:
    config.STATE_FILE = config.DATA_DIR / "state_proposals_test.json"
    for f in (config.STATE_FILE, learn.LESSONS_LOG, learn.LESSONS_DIGEST):
        if f.exists():
            f.unlink()

    check("proposals: threshold changes detected",
          learn._is_rule_change("Lower the win rate floor to 65% on trend days")
          and learn._is_rule_change("Take half at +30% instead of +25%")
          and learn._is_rule_change("Widen the entry window to 10:45")
          and learn._is_rule_change("Add QQQ to the watchlist")
          and learn._is_rule_change("Move the stop to -60% on green days"))
    check("proposals: behavioral lessons stay lessons",
          not learn._is_rule_change(
              "when a call spikes past +30% in the first 20 minutes, bank "
              "half immediately, do not wait for +25% to become give-back")
          and not learn._is_rule_change(
              "TSLA peaked +34% then stopped out. When a call spikes double "
              "digits early, bank half into the spike instead of waiting.")
          and not learn._is_rule_change(
              "SPX ran the playbook clean: banked half into strength then "
              "trailed the runner. Keep repeating this shape.")
          and not learn._is_rule_change(
              "Being picky is increasing my win rate."))

    _l = learn._sanitize({"review": "r",
                          "lessons": ["real behavioral lesson",
                                      "Lower the win rate floor to 65%"],
                          "watch_tomorrow": "", "proposed_change": None})
    check("proposals: sanitize routes the rule change out of lessons",
          _l["lessons"] == ["real behavioral lesson"]
          and _l["proposed_change"] == "Lower the win rate floor to 65%")

    # synthesize applies the sanitizer to whatever the brain returns
    _oc1, _oc2 = assistant.complete_deep, assistant.complete
    try:
        assistant.complete_deep = lambda s, u, **k: (
            '{"review": "r", "lessons": ["Tighten the gate to 75%"], '
            '"watch_tomorrow": "", "proposed_change": null}')
        assistant.complete = lambda s, u, **k: None
        _got = learn.synthesize(_quiet_record("2026-06-12"))
        check("proposals: synthesize sanitizes a rule change out of lessons",
              _got["lessons"] == []
              and _got["proposed_change"] == "Tighten the gate to 75%",
              f"got {_got}")
    finally:
        assistant.complete_deep, assistant.complete = _oc1, _oc2

    # tracking: reworded repeats count one row, once per session
    p1 = learn.track_proposal("Lower the stop to -60%", "2026-06-10")
    p2 = learn.track_proposal("  lower the stop to -60% ", "2026-06-11")
    p3 = learn.track_proposal("lower the stop to -60%.", "2026-06-11")
    check("proposals: same idea reworded counts one row",
          p1["repeat"] is False and p2["repeat"] is True
          and p2["times"] == 2 and p3["times"] == 2
          and len(learn.proposals_list()) == 1,
          f"got {learn.proposals_list()}")

    learn.track_proposal("Widen the entry window to 10:45", "2026-06-11")
    txt = learn.proposals_command("")
    check("proposals: view lists pending with stable numbers",
          "1. Lower the stop to -60%" in txt
          and "2. Widen the entry window to 10:45" in txt
          and "came up 2 nights" in txt, f"got {txt!r}")

    reply = learn.proposals_command("ok 1 run it next round")
    check("proposals: approve records the decision, changes nothing",
          "Approved" in reply and "backtest" in reply, f"got {reply!r}")
    _rows = learn.proposals_list()
    check("proposals: status persisted with the note",
          _rows[0]["status"] == "approved"
          and _rows[0]["note"] == "run it next round", f"got {_rows}")
    txt = learn.proposals_command("")
    check("proposals: a decided one leaves the pending list",
          "1. Widen the entry window to 10:45" in txt
          and "Lower the stop" not in txt.split("DECIDED:")[0]
          and "approved" in txt, f"got {txt!r}")

    reply = learn.proposals_command("no 1")
    check("proposals: reject stops the nagging",
          "Rejected" in reply
          and learn.proposals_list()[1]["status"] == "rejected")
    check("proposals: bad index is a gentle note",
          "No pending proposal" in learn.proposals_command("ok 9"))
    check("proposals: junk args get usage",
          "Usage" in learn.proposals_command("frobnicate")
          and "Usage" in learn.proposals_command("ok"))
    check("proposals: empty pending list says so",
          "No pending rule proposals" in learn.proposals_command(""))

    # owner message: full pitch once, a one-line reminder while pending,
    # silence once decided
    _rec = _quiet_record("2026-06-12")
    _les = {"review": "r", "lessons": [], "watch_tomorrow": "",
            "proposed_change": "Allow IWM for reads"}
    _new = learn._owner_message(_rec, _les,
                                {"status": "pending", "repeat": False})
    _rep = learn._owner_message(_rec, _les,
                                {"status": "pending", "repeat": True,
                                 "times": 3, "text": "Allow IWM for reads"})
    _rej = learn._owner_message(_rec, _les,
                                {"status": "rejected", "repeat": True})
    check("proposals: fresh proposal gets the full pitch",
          "PROPOSED RULE CHANGE" in _new and "/proposals" in _new)
    check("proposals: pending repeat is one reminder, not a re-pitch",
          "PROPOSED RULE CHANGE" not in _rep and "came up 3 nights" in _rep,
          f"got {_rep!r}")
    check("proposals: a rejected idea never nags again", "💡" not in _rej)

    # coach and deep-review rows are caught at the _append_lesson choke point
    learn._append_lesson({"session": "2026-06-12", "graded_at": "x",
                          "wins": 0, "losses": 0, "trades": [],
                          "review": "coach: solid day",
                          "lessons": ["real coach lesson",
                                      "Raise the minimum gap to 1.2 ATR"],
                          "watch_tomorrow": "", "proposed_change": None})
    learn._rebuild_digest()
    _dig = learn.LESSONS_DIGEST.read_text(encoding="utf-8")
    check("proposals: rule change in a coach lesson never reaches the digest",
          "real coach lesson" in _dig and "minimum gap" not in _dig,
          f"got {_dig!r}")
    check("proposals: routed coach lesson lands in the registry, sourced",
          any(r.get("source") == "coach" and "minimum gap" in r.get("key", "")
              for r in learn.proposals_list()),
          f"got {learn.proposals_list()}")
finally:
    for f in (config.STATE_FILE, learn.LESSONS_LOG, learn.LESSONS_DIGEST):
        if f.exists():
            f.unlink()
    config.STATE_FILE = _orig_state_file4
    learn.LESSONS_LOG, learn.LESSONS_DIGEST = _orig_llog7, _orig_ldig7

# --- night grading settles stragglers the bot was down for (learn.py) ---
# A 0DTE still 'open' at review time (bot down at the 16:00 settle) used to be
# graded STILL OPEN with a "doesn't expire today, I'm still watching it" story.
# _grade_positions now settles it in memory first (same honest semantics as
# needs_monitoring's force-expire) and never writes positions.json.
print()
print("--- night grading of expired stragglers ---")
_orig_posfile_ng = config.POSITIONS_FILE
_orig_et_now_ng = learn.et_now
_orig_mktctx_ng = learn._market_context
_ng_path = config.DATA_DIR / "positions_straggler_test.json"
config.POSITIONS_FILE = _ng_path
try:
    if _ng_path.exists():
        _ng_path.unlink()

    def _ng_pos(pid, expiry=TODAY, last_pct=None, state="open"):
        q = mk_pos(expiry=expiry)
        q.id = q.ticker = pid   # trades carry no id; the ticker tells them apart
        q.state = state
        if last_pct is not None:
            q.last_mark = mark(last_pct)
            q.last_mark_pct = last_pct
            q.last_mark_source = "quote mid"
            q.last_mark_time = "15:55:00"
        return q

    def _ng_at(day, hh, mm):
        return datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET)

    _ng_book = PositionBook(_ng_path)
    _ng_book.add(_ng_pos("ng_win", last_pct=12.0))
    _ng_book.add(_ng_pos("ng_blind"))                          # never marked
    _ng_book.add(_ng_pos("ng_weekly", expiry=TODAY + timedelta(days=4),
                         last_pct=5.0))
    _ng_bad = _ng_pos("ng_bad", last_pct=3.0)
    _ng_bad.expiry = "not-a-date"
    _ng_book.add(_ng_bad)

    learn.et_now = lambda: _ng_at(TODAY, 21, 30)
    _ng_raw_before = _ng_path.read_text(encoding="utf-8-sig")
    _ng_tr = {t["ticker"]: t for t in learn._grade_positions(TODAY)}
    _ng_raw_after = _ng_path.read_text(encoding="utf-8-sig")
    check("NG all four positions graded", len(_ng_tr) == 4,
          f"got {sorted(_ng_tr)}")
    _w = _ng_tr.get("ng_win", {})
    check("NG marked straggler settles for grading: closed and won",
          _w.get("closed") is True and _w.get("won") is True, f"got {_w}")
    check("NG settled straggler graded RIGHT, not STILL OPEN",
          "RIGHT" in str(_w.get("verdict")), f"got {_w.get('verdict')}")
    check("NG settle reason says the bot was offline",
          "offline" in str(_w.get("outcome", {}).get("exit_reason")),
          f"got {_w.get('outcome')}")
    check("NG grading never writes positions.json",
          _ng_raw_before == _ng_raw_after)
    _b = _ng_tr.get("ng_blind", {})
    check("NG never-marked straggler is NOT GRADED, not a win or a loss",
          _b.get("verdict") == "NOT GRADED" and not _b.get("won")
          and not _b.get("closed"), f"got {_b}")
    check("NG ungraded story says no result was invented",
          "without a grade" in str(_b.get("story")), f"got {_b.get('story')}")
    check("NG weekly that truly is open stays STILL OPEN",
          str(_ng_tr.get("ng_weekly", {}).get("verdict", ""))
          .startswith("STILL OPEN"), f"got {_ng_tr.get('ng_weekly')}")
    check("NG malformed expiry doesn't abort the sweep",
          str(_ng_tr.get("ng_bad", {}).get("verdict", ""))
          .startswith("STILL OPEN"), f"got {_ng_tr.get('ng_bad')}")

    learn.et_now = lambda: _ng_at(TODAY, 15, 59)
    _ng_tr2 = {t["ticker"]: t for t in learn._grade_positions(TODAY)}
    check("NG before the close nothing settles",
          str(_ng_tr2["ng_win"].get("verdict", "")).startswith("STILL OPEN"),
          f"got {_ng_tr2['ng_win'].get('verdict')}")
    learn.et_now = lambda: _ng_at(TODAY, 16, 0)
    _ng_tr3 = {t["ticker"]: t for t in learn._grade_positions(TODAY)}
    check("NG the 16:00 bell is the boundary",
          _ng_tr3["ng_win"].get("closed") is True)
    learn.et_now = lambda: _ng_at(TODAY + timedelta(days=1), 9, 0)
    _ng_tr4 = {t["ticker"]: t for t in learn._grade_positions(TODAY)}
    check("NG catch-up review on a later day settles too",
          _ng_tr4["ng_win"].get("closed") is True)

    learn._market_context = lambda d: ""
    learn.et_now = lambda: _ng_at(TODAY, 21, 30)
    _ng_rec = learn.grade_day(TODAY)
    check("NG grade_day counts the settled straggler as a win",
          _ng_rec["wins"] == 1 and _ng_rec["losses"] == 0,
          f"got wins={_ng_rec['wins']} losses={_ng_rec['losses']}")

    _ng_half = _ng_pos("ng_half", last_pct=10.0, state="half_sold")
    _ng_half.half_exit = {"time": "10:05:00", "pct": 26.0, "mark": mark(26.0)}
    _ng_book2 = PositionBook(_ng_path)
    _ng_book2.add(_ng_half)
    _ng_settled = _ng_book2.settle_overdue(TODAY, _ng_at(TODAY, 21, 30),
                                           save=False)
    check("NG settle_overdue settles exactly the overdue open stragglers",
          {q.ticker for q in _ng_settled} == {"ng_win", "ng_blind", "ng_half"},
          f"got {[q.ticker for q in _ng_settled]}")
    check("NG half-banked straggler settles to the weighted final (0.5*26 + 0.5*10)",
          _ng_half.final_pnl_pct is not None
          and abs(_ng_half.final_pnl_pct - 18.0) < 0.01,
          f"got {_ng_half.final_pnl_pct}")
    _ng_book3 = PositionBook(_ng_path)
    check("NG save=False left the disk copy open",
          all(q.state == "open" for q in _ng_book3.positions
              if q.ticker == "ng_win"))
    _ng_book3.settle_overdue(TODAY, _ng_at(TODAY, 21, 30))  # default save=True
    _ng_book4 = PositionBook(_ng_path)
    check("NG default save=True persists the settle",
          any(q.ticker == "ng_win" and q.state == "closed"
              for q in _ng_book4.positions))
finally:
    if _ng_path.exists():
        _ng_path.unlink()
    config.POSITIONS_FILE = _orig_posfile_ng
    learn.et_now = _orig_et_now_ng
    learn._market_context = _orig_mktctx_ng

# --- catch-up recap settles stragglers too (recap.py) ---
# The recap fired by the night catch-up after a late reboot used to tell the
# owner an expired 0DTE was STILL OPEN and being watched. recap.main now
# settles overdue positions in memory first (save=False, same settle the next
# session's needs_monitoring persists) and position_story grades the settled
# truth: a settle whose close the bot never saw says so, and a never-marked
# settle reads NOT GRADED. Same-day settles wait for SETTLE_AFTER (16:12,
# scanner's SESSION_END) so a live-day 16:05 recap can never front-run the
# monitoring loop's own bell settle during a feed outage.
print()
print("--- catch-up recap of expired stragglers ---")
import contextlib
import io

import pandas as pd

import recap as rcmod

_orig_posfile_rc = config.POSITIONS_FILE
_orig_et_now_rc = rcmod.et_now
_orig_fetch_rc = rcmod.fetch_5m
_orig_alerts_rc = rcmod.ALERTS_FILE
_orig_argv_rc = sys.argv[:]
_rc_path = config.DATA_DIR / "positions_recap_straggler_test.json"
config.POSITIONS_FILE = _rc_path
try:
    if _rc_path.exists():
        _rc_path.unlink()

    def _rc_pos(pid, expiry=TODAY, last_pct=None):
        q = mk_pos(expiry=expiry)
        q.id = q.ticker = pid
        if last_pct is not None:
            q.last_mark = mark(last_pct)
            q.last_mark_pct = last_pct
            q.last_mark_source = "quote mid"
            q.last_mark_time = "15:55:00"
        return q

    _rc_book = PositionBook(_rc_path)
    _rc_book.add(_rc_pos("rc_win", last_pct=12.0))
    _rc_book.add(_rc_pos("rc_blind"))                        # never marked
    _rc_book.add(_rc_pos("rc_weekly", expiry=TODAY + timedelta(days=4),
                         last_pct=5.0))

    # position_story on the settled shapes (no network needed)
    _rc_view = PositionBook(_rc_path)
    _rc_view.settle_overdue(TODAY, datetime(2026, 6, 11, 21, 30, tzinfo=ET),
                            save=False)
    _rc_by = {q.ticker: q for q in _rc_view.for_date(TODAY)}
    _v, _s = rcmod.position_story(_rc_by["rc_win"])
    check("RC offline settle grades RIGHT with the last-price caveat",
          _v.startswith("RIGHT") and "never saw the close" in _s
          and "last price" in _s, f"got {_v!r} {_s!r}")
    check("RC offline settle never claims it ran into the closing bell",
          "closing bell" not in _s, f"got {_s!r}")
    _v, _s = rcmod.position_story(_rc_by["rc_blind"])
    check("RC never-marked settle is NOT GRADED, not STILL OPEN",
          _v == "NOT GRADED" and "without a grade" in _s
          and "watching" not in _s, f"got {_v!r} {_s!r}")

    # a genuine bell-time expiry (bot alive, step() settled) keeps its story
    _rc_bell = _rc_pos("rc_bell", last_pct=8.0)
    _rc_bell.state = "closed"
    _rc_bell.final_pnl_pct = 8.0
    _rc_bell.final_exit = {"time": "16:00:00", "pct": 8.0, "mark": mark(8.0),
                           "reason": "expiry close"}
    _v, _s = rcmod.position_story(_rc_bell)
    check("RC live bell expiry story unchanged",
          "closing bell" in _s and "offline" not in _s, f"got {_s!r}")

    # end-to-end: the catch-up recap message itself (dry run, offline)
    def _rc_fetch(sym):
        idx = pd.date_range("2026-06-11 09:30", periods=79, freq="5min",
                            tz=ET)
        base = [7300 + i * 0.5 for i in range(len(idx))]
        return pd.DataFrame(
            {"Open": base, "High": [b + 2 for b in base],
             "Low": [b - 2 for b in base], "Close": [b + 1 for b in base]},
            index=idx)

    rcmod.fetch_5m = _rc_fetch
    rcmod.et_now = lambda: datetime(2026, 6, 11, 21, 30, tzinfo=ET)
    rcmod.ALERTS_FILE = config.DATA_DIR / "no_such_alerts_test.jsonl"
    sys.argv = [sys.argv[0], "--dry-run"]
    _rc_raw_before = _rc_path.read_text(encoding="utf-8-sig")
    _rc_buf = io.StringIO()
    with contextlib.redirect_stdout(_rc_buf):
        _rc_ret = rcmod.main()
    _rc_msg = _rc_buf.getvalue()
    _rc_raw_after = _rc_path.read_text(encoding="utf-8-sig")
    check("RC dry catch-up recap returns clean and stays write-free",
          _rc_ret == [] and _rc_raw_before == _rc_raw_after,
          f"got {_rc_ret!r}")
    check("RC catch-up recap grades the settled straggler as a win",
          "rc_win" in _rc_msg and "WE WERE RIGHT" in _rc_msg,
          f"got {_rc_msg!r}")
    check("RC exactly one STILL OPEN left: the real weekly",
          _rc_msg.count("STILL OPEN") == 1
          and "I'm still watching it" not in
          _rc_msg.split("rc_weekly")[0], f"got {_rc_msg!r}")
    check("RC ungraded head reads NOT GRADED, not WE WERE NOT GRADED",
          "NOT GRADED" in _rc_msg and "WE WERE NOT GRADED" not in _rc_msg,
          f"got {_rc_msg!r}")

    # during the live session window the monitoring loop owns settles: a
    # 16:05 recap during a bell-time feed outage must not text a grade the
    # loop could still contradict by persisting a fresher mark at 16:07
    rcmod.et_now = lambda: datetime(2026, 6, 11, 16, 5, tzinfo=ET)
    _rc_buf2 = io.StringIO()
    with contextlib.redirect_stdout(_rc_buf2):
        rcmod.main()
    _rc_msg2 = _rc_buf2.getvalue()
    check("RC before 16:12 the recap leaves settles to the monitoring loop",
          _rc_msg2.count("STILL OPEN") == 3 and "NOT GRADED" not in _rc_msg2,
          f"got {_rc_msg2!r}")

    # a next-morning catch-up (prior session) settles regardless of the clock
    rcmod.et_now = lambda: datetime(2026, 6, 12, 9, 0, tzinfo=ET)
    _rc_buf3 = io.StringIO()
    with contextlib.redirect_stdout(_rc_buf3):
        rcmod.main()
    _rc_msg3 = _rc_buf3.getvalue()
    check("RC next-morning catch-up settles the prior session too",
          "WE WERE RIGHT" in _rc_msg3 and _rc_msg3.count("STILL OPEN") == 1,
          f"got {_rc_msg3!r}")
finally:
    if _rc_path.exists():
        _rc_path.unlink()
    config.POSITIONS_FILE = _orig_posfile_rc
    rcmod.et_now = _orig_et_now_rc
    rcmod.fetch_5m = _orig_fetch_rc
    rcmod.ALERTS_FILE = _orig_alerts_rc
    sys.argv = _orig_argv_rc

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")
