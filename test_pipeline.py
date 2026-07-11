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
assistant.TRADES_FILE.unlink()

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
finally:
    scoreboard.REPORTS_DIR = _orig_reports_dir
    import shutil
    shutil.rmtree(_reports, ignore_errors=True)

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
    learn._append_lesson(_lentry("2026-06-03", None))  # legacy null entry
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

    # a slow stretch repeating one bullet cannot evict the real lessons
    for _ in range(30):
        learn._append_lesson(_lentry("2026-06-05", ["repeat me"]))
    learn._rebuild_digest()
    got = _digest_bullets()
    check("learn: 30 identical nights collapse to one bullet",
          sum("repeat me" in ln for ln in got) == 1, f"got {len(got)} bullets")
    check("learn: real lessons survive the burst",
          any("real lesson A" in ln for ln in got)
          and any("real lesson B" in ln for ln in got), f"got {got}")

    # the window still caps at DIGEST_KEEP, newest distinct bullets win
    for i in range(25):
        learn._append_lesson(_lentry("2026-06-08", [f"distinct lesson {i:02d}"]))
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

# --- learn catch-up: a review missed during an outage is no longer lost ---
# maybe_learn only ever considered now.date(), so a bot down across the whole
# 21:00-23:45 window (a Friday-night redeploy rolling into the weekend, a
# crash loop) lost that session's review forever while recap and weekly both
# catch up. It now targets the most recent completed weekday session, catches
# up at most that one session, and only when positions or that day's recap
# prove the bot actually ran, so an outage day is never graded as a clean
# stay-out. 2026-06-11 is a Thursday, 6/12 Friday, 6/13-14 the weekend.

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

    # the normal same-night path is unchanged: fires once, dedups after
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

    # outage day with no positions and no recap: marked handled, never graded
    lbook.positions = []
    config.state_set("learn_sent", "2026-06-11")
    svc.maybe_learn(datetime(2026, 6, 13, 9, 0, tzinfo=ET))
    check("catchup: a day with no evidence the bot ran is skipped, not graded",
          len(lruns) == 2 and config.state_get("learn_sent") == "2026-06-12")

    # but a recorded Friday recap proves the bot was alive at the close, so a
    # quiet (zero-position) Friday still gets its review
    config.state_set("learn_sent", "2026-06-11")
    config.state_set("recap_sent", "2026-06-12")
    svc.maybe_learn(datetime(2026, 6, 13, 9, 0, tzinfo=ET))
    check("catchup: a quiet day with a recorded recap still gets its review",
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

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")
