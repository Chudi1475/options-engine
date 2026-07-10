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


def _daily(last=105.0, n=25):
    idx = pd.date_range(end="2026-06-11", periods=n, freq="B")
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
finally:
    mt.yf = _orig_yf
    risk_gate.upcoming_events = _orig_ev
    forward_ledger.record_candidate = _orig_rec

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")
