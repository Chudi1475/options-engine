"""Offline tests for the 8/22 fixes: the sniper fires in the US session only,
on completed regular-session bars, grades exits bar by bar like its backtest,
quotes a report-backed record; the brain never texts about an empty credit
balance more than once; the feed-dead heartbeat needs a fetch that actually
failed; breaking news sends one text per story. No network, no Telegram.

Every check here failed before its fix and passes after it.

Run:  python test_session_fixes.py     (exit code 0 = all good)
"""

import json
import os
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

import config

ET = ZoneInfo("America/New_York")
failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def bars_frame(day: date, times, base=100.0):
    """A tiny OHLC frame at the given ET wall-clock (h, m) tuples."""
    idx = pd.DatetimeIndex([datetime(day.year, day.month, day.day, h, m,
                                     tzinfo=ET) for h, m in times])
    n = len(idx)
    o = [base + i * 0.01 for i in range(n)]
    c = [x + 0.005 for x in o]
    h = [x + 0.02 for x in c]
    lo = [x - 0.02 for x in o]
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c}, index=idx)


# ---------------------------------------------------------------------------
print("\n--- 1. the sniper window: US session only, from 09:50 ET ---")
import fvg

check("fvg: the entry floor is 09:50 ET (8:50 AM CT), not 07:00",
      fvg._SNIPER_OPEN_ET == time(9, 50))
check("fvg: the old 07:00 constant is gone, nothing can read it",
      not hasattr(fvg, "_SNIPER_MIN_HOUR_ET"))
check("fvg: 07:27 ET Friday (the 8/21 SPY pre-market ticket) is closed",
      not fvg.sniper_window_open(datetime(2026, 8, 21, 7, 27, tzinfo=ET)))
check("fvg: 09:49 ET is closed", not fvg.sniper_window_open(
    datetime(2026, 8, 21, 9, 49, tzinfo=ET)))
check("fvg: 09:50 ET is open", fvg.sniper_window_open(
    datetime(2026, 8, 21, 9, 50, tzinfo=ET)))
check("fvg: 15:59 ET is open", fvg.sniper_window_open(
    datetime(2026, 8, 21, 15, 59, tzinfo=ET)))
check("fvg: 16:00 ET is closed", not fvg.sniper_window_open(
    datetime(2026, 8, 21, 16, 0, tzinfo=ET)))
check("fvg: Saturday noon is closed", not fvg.sniper_window_open(
    datetime(2026, 8, 22, 12, 0, tzinfo=ET)))
check("fvg: the CT label renders 8:50 AM CT", fvg.sniper_open_ct_txt() == "8:50 AM CT")
check("fvg: the stock/index names are the regular-session set",
      fvg.SNIPER_RTH_SYMBOLS == {"^GSPC", "TSLA", "SPY"}
      and fvg.SNIPER_RTH_SYMBOLS < fvg.SNIPER_SYMBOLS)

day = date(2026, 8, 21)
sess = bars_frame(day, [(9, 30), (9, 35), (9, 40), (9, 45), (9, 50), (9, 55),
                        (10, 0)])
early = fvg.sniper_check(sess, "BUY", 100.05, 0.05, None, "SPY",
                         datetime(2026, 8, 21, 7, 27, tzinfo=ET))
late = fvg.sniper_check(sess, "BUY", 100.05, 0.05, None, "SPY",
                        datetime(2026, 8, 21, 10, 5, tzinfo=ET))
check("fvg: the gate names the window when the clock is before it",
      any("outside the US session window" in r for r in early["reasons"])
      and "8:50 AM CT" in " ".join(early["reasons"]))
check("fvg: the gate drops the clock reason inside the window",
      not any("session window" in r for r in late["reasons"]))
few = fvg.sniper_check(sess.iloc[:4], "BUY", 100.05, 0.05, None, "SPY",
                       datetime(2026, 8, 21, 9, 50, tzinfo=ET))
check("fvg: fewer than 5 completed session bars is refused (SKIP_FIRST_BARS=4)",
      any("completed session bars" in r for r in few["reasons"]))
check("fvg: 5 completed bars satisfy the bar floor",
      not any("completed session bars" in r for r in
              fvg.sniper_check(sess.iloc[:5], "BUY", 100.05, 0.05, None, "SPY",
                               datetime(2026, 8, 21, 9, 55, tzinfo=ET))["reasons"]))

# ---------------------------------------------------------------------------
print("\n--- 2. the sniper's bars: completed, regular session, today ---")
import market_tools as mt

two_day = pd.concat([
    bars_frame(day - timedelta(days=1), [(15, 50), (15, 55)]),
    bars_frame(day, [(4, 0), (7, 0), (9, 25), (9, 30), (9, 35), (9, 40), (9, 45),
                     (9, 50), (9, 55), (10, 0)]),
])
now = datetime(2026, 8, 21, 10, 2, tzinfo=ET)
spy = mt._sniper_bars(two_day, "SPY", now)
check("bars: SPY keeps only today's regular-session bars",
      list(t.strftime("%H:%M") for t in spy.index) ==
      ["09:30", "09:35", "09:40", "09:45", "09:50", "09:55"],
      str(list(spy.index)))
check("bars: the forming 10:00 bar is dropped at 10:02",
      all(t < time(10, 0) for t in spy.index.time))
check("bars: yesterday's bars are dropped", all(d == day for d in spy.index.date))
eur = mt._sniper_bars(two_day, "EURUSD=X", now)
check("bars: EUR/USD keeps pre-market bars (its data ran all day)",
      time(4, 0) in set(eur.index.time) and time(7, 0) in set(eur.index.time))
check("bars: EUR/USD still drops the forming bar and yesterday",
      all(t < time(10, 0) for t in eur.index.time)
      and all(d == day for d in eur.index.date))
rows = mt._bars_json(spy, 3)
check("bars: JSON rows are [iso, o, h, l, c] of the last n bars",
      rows is not None and len(rows) == 3 and len(rows[0]) == 5
      and rows[-1][0].startswith("2026-08-21T09:55") and rows[0][2] > rows[0][3])
check("bars: an empty frame gives None", mt._bars_json(spy.iloc[:0]) is None)

# ---------------------------------------------------------------------------
print("\n--- 3. sniper exits graded on completed bars, stop wins a tie ---")
import sniper_book

_orig_ledger = sniper_book.LEDGER
sniper_book.LEDGER = config.DATA_DIR / "sniper_positions_test2.json"
try:
    if sniper_book.LEDGER.exists():
        sniper_book.LEDGER.unlink()
    t_entry = datetime(2026, 8, 21, 10, 2, 17, tzinfo=ET)
    row = sniper_book.open_trade("SPY", "SPY", "BUY", entry=100.0, stop=99.0,
                                 target=100.4, day="2026-08-21",
                                 time_et="10:02:17", entry_ts=t_entry)
    check("book: the entry bar is pinned to the 5-minute bar of the fill",
          row is not None and row["entry_bar_ts"].startswith("2026-08-21T10:00"))

    def bar(h, m, hi, lo, c=None):
        ts = datetime(2026, 8, 21, h, m, tzinfo=ET).isoformat()
        return [ts, (hi + lo) / 2, hi, lo, c if c is not None else (hi + lo) / 2]

    # a bar BEFORE the fill bar touching the target must not count
    pre = sniper_book.step("SPY", 100.1, t_entry + timedelta(minutes=3),
                           bars=[bar(9, 55, 100.9, 99.8)])
    check("book: a bar before the fill bar never closes the trade", pre is None)
    # the fill bar retraces through the target intrabar and closes back below
    hit = sniper_book.step("SPY", 100.1, t_entry + timedelta(minutes=6),
                           bars=[bar(9, 55, 100.9, 99.8),
                                 bar(10, 0, 100.45, 99.9, 100.1)])
    check("book: a bar that traded through the target closes it as a win even "
          "though the poll price sits below the target",
          hit is not None and hit["exit_reason"] == "target"
          and abs(hit["r"] - 0.4) < 1e-9 and hit["exit_time"] == "10:00:00")
    check("book: best excursion came from the bar high, not the poll",
          hit is not None and abs(hit["mfe_r"] - 0.45) < 1e-6)
    check("book: a bar-graded exit says so", hit is not None and hit.get("exit_via") == "bar")

    sniper_book.open_trade("TSLA", "TSLA", "SELL", entry=300.0, stop=302.0,
                           target=299.2, day="2026-08-21", time_et="10:02:17",
                           entry_ts=t_entry)
    tie = sniper_book.step("TSLA", 299.5, t_entry + timedelta(minutes=6),
                           bars=[bar(10, 0, 302.5, 299.0)])
    check("book: one bar spanning stop AND target is a loss (stop wins the tie)",
          tie is not None and tie["exit_reason"] == "stop" and tie["r"] == -1.0)

    sniper_book.open_trade("EURUSD=X", "EUR/USD", "BUY", entry=1.1700,
                           stop=1.1690, target=1.1704, day="2026-08-21",
                           time_et="10:02:17", decimals=4, entry_ts=t_entry)
    same = [bar(10, 0, 1.1703, 1.1695), bar(10, 5, 1.1702, 1.1696)]
    a = sniper_book.step("EURUSD=X", 1.1701, t_entry + timedelta(minutes=9), bars=same)
    b = sniper_book.step("EURUSD=X", 1.1701, t_entry + timedelta(minutes=9), bars=same)
    open_row = sniper_book.open_rows()[0]
    check("book: bars already walked are not walked twice",
          a is None and b is None and open_row["last_bar_ts"].startswith("2026-08-21T10:05"))
    # the live price still closes a trade when no bars came along (fallback)
    fb = sniper_book.step("EURUSD=X", 1.1705, t_entry + timedelta(minutes=12))
    check("book: the polled price is still the fallback without bars",
          fb is not None and fb["exit_reason"] == "target" and fb.get("exit_via") == "poll")
    # the old signature (no bars) is untouched for every caller
    sniper_book.open_trade("^GSPC", "SPX", "BUY", 7000.0, 6990.0, 7004.0,
                           "2026-08-21", "10:02:17")
    legacy = sniper_book.step("^GSPC", 6989.0, t_entry + timedelta(minutes=12))
    check("book: legacy price-only stepping still books the stop",
          legacy is not None and legacy["exit_reason"] == "stop")
finally:
    if sniper_book.LEDGER.exists():
        sniper_book.LEDGER.unlink()
    sniper_book.LEDGER = _orig_ledger

# ---------------------------------------------------------------------------
print("\n--- 4. the record the cards quote comes from the session report ---")
import strategy_spec
import rescore_round6_session as rs

check("rescore: the script's floor equals the live gate's floor",
      rs.SESSION_OPEN_ET == fvg._SNIPER_OPEN_ET)
rep_path = config.REPO_DIR / "reports" / "chart_backtest_round6_session.json"
check("rescore: the session report exists", rep_path.exists())
rep = json.loads(rep_path.read_text(encoding="utf-8")) if rep_path.exists() else {}
check("rescore: the report carries the floor it was scored with",
      rep.get("session_open_et") == fvg._SNIPER_OPEN_ET.strftime("%H:%M"))
check("rescore: no stock trade existed before 10:00 ET in the source data",
      rep.get("stock_trades_before_10_et_in_source") == 0)
check("rescore: every dropped replay is outside the live window",
      rep.get("dropped") and all(
          not (fvg._SNIPER_OPEN_ET <= datetime.fromisoformat(d["time"]).time()
               < time(16, 0)) for d in rep["dropped"]))
check("rescore: the report is a true replay that reproduced round 6 first",
      rep.get("reproduced_published_selection_first") is True
      and "replay" in rep.get("method", "").lower())
check("rescore: the report is labeled a measurement, not a selection",
      rep.get("kind") == "measurement" and "selected" in rep.get("label", ""))
spec = strategy_spec.get()
check("spec: the sniper record is read from the session report file",
      spec.sniper.source.endswith("chart_backtest_round6_session.json"))
check("spec: the rate and count are the report's OUT-OF-SAMPLE pair",
      spec.sniper.win_rate == rep.get("oos", {}).get("win_rate_pct")
      and spec.sniper.trades == rep.get("oos", {}).get("trades")
      and spec.sniper.wins == rep.get("oos", {}).get("wins"))
card = spec.sniper_card_txt()
check("spec: the card claim pairs the rate with its own count",
      card.startswith(f"hit target {spec.sniper.wins} of {spec.sniper.trades}")
      and "out-of-sample" in card and "of 100" in card, card)
check("spec: the window text is the CT session floor",
      spec.sniper_window_txt() == "8:50 AM CT to the close, weekdays")
check("spec: the measured dict on a ticket carries wins, trades and source",
      set(spec.sniper_measured_dict()) >= {"win_rate", "trades", "wins", "source"})
check("spec: no sniper drift warning while gate and report agree",
      not any("sniper" in w.lower() for w in spec.drift_warnings()))
# the old hand-typed pairing is gone from the module that used to carry it
check("spec: fvg's fallback literal is not the stale '79 over 133' pairing",
      not (fvg.SNIPER_MEASURED.get("win_rate") == 79.0
           and fvg.SNIPER_MEASURED.get("trades") == 133))

# ---------------------------------------------------------------------------
print("\n--- 5. cards: the morning card says when the sniper can fire ---")
import cards

plain = cards.morning_card("green", "calm", date(2026, 8, 24), window_ct="8:45-9:30 AM CT")
with_line = cards.morning_card("green", "calm", date(2026, 8, 24),
                               window_ct="8:45-9:30 AM CT",
                               sniper_line=spec.sniper_watch_sentence())
check("cards: without the line the card is unchanged", "Sniper" not in plain)
check("cards: with the line the card names the sniper window",
      "from 8:50 AM CT to the close" in with_line and "SPY" in with_line)
check("cards: the sniper sentence carries no em dash", "—" not in with_line)

# ---------------------------------------------------------------------------
print("\n--- 6. scanner: record line, poll alignment, feed-dead heartbeat ---")
import scanner

svc = scanner.Service.__new__(scanner.Service)
rec = {"n": 28, "wins": 20, "losses": 8, "flats": 0, "total_r": 0.0,
       "win_pct": 71.4, "avg_r": 0.0}
line = scanner.Service.sniper_record_line(rec)
check("scanner: the record line separates wins, stops and flats and states "
      "the breakeven instead of '20 of 28 hit target, +0.00R'",
      "28 trades, 20 wins, 8 stops" in line and "flat" not in line
      and "Net +0.00R" in line and "71 wins in 100" in line, line)
check("scanner: polls land ~20s after the next 5-minute bar closes",
      abs(svc.sniper_poll_wait_s(datetime(2026, 8, 21, 10, 2, 17, tzinfo=ET))
          - (300 - 137 + 20)) < 1e-9
      and svc.sniper_poll_wait_s(datetime(2026, 8, 21, 10, 4, 55, tzinfo=ET)) == 25.0
      and svc.sniper_poll_wait_s(datetime(2026, 8, 21, 10, 0, 0, tzinfo=ET))
          == scanner.Service.SNIPER_WATCH_SECONDS)

sent = []
svc.dry = False
svc._hb_owner = lambda text: sent.append(text)
svc.health_stamp = lambda now: None
svc._health_last_stamp = 0.0
svc._feed_warned = False
svc._feed_none_warned = False
t_ok = datetime(2026, 8, 21, 11, 31, tzinfo=ET)   # last SPY runner sold 10:31 CT
svc._last_feed_ok = t_ok
svc._last_feed_try = t_ok
svc.health_check(t_ok + timedelta(minutes=11))
check("scanner: eleven idle minutes with nothing to fetch is NOT a dead feed",
      not sent, str(sent))
svc._last_feed_try = t_ok + timedelta(minutes=10, seconds=30)  # a fetch that failed
svc.health_check(t_ok + timedelta(minutes=11))
check("scanner: a fetch that was tried and came back empty IS a dead feed",
      len(sent) == 1 and "returned nothing" in sent[0])
check("scanner: the heartbeat text carries no em dash", "—" not in sent[0])
svc._last_feed_ok = t_ok + timedelta(minutes=12)
svc.health_check(t_ok + timedelta(minutes=12))
check("scanner: recovery still announces itself once",
      len(sent) == 2 and "recovered" in sent[1])
svc._last_feed_ok = None
svc._last_feed_try = None
svc._feed_none_warned = False
svc.health_check(datetime(2026, 8, 21, 10, 5, tzinfo=ET))
check("scanner: 'no data loaded yet' needs a fetch to have been tried",
      len(sent) == 2)

# ---------------------------------------------------------------------------
print("\n--- 7. news: one story one text, macro-only on the market wires ---")
import news

canada = ["Trump tariff deadline looms as U.S. and Canada say final trade deal is near",
          "The U.S. and Canada are scrambling to avoid new tariffs and seal a trade deal. Here's what could be in it.",
          "Trump tariff deadline looms as Canada says it's working to resolve 'trade issues' with U.S."]
check("news: the second outlet's Canada-tariff retelling is the same story",
      news.same_story(canada[1], [canada[0]]))
check("news: the third outlet's retelling is the same story as the first",
      news.same_story(canada[2], [canada[0]]))
check("news: the third retelling is caught against the first two",
      news.same_story(canada[2], canada[:2]))
check("news: a different story sharing one word is not merged",
      not news.same_story("Tesla recalls 3 million vehicles in China over door handle safety",
                          canada))
check("news: an empty title is never a duplicate", not news.same_story("", canada))
news._cache.clear()
now_s = __import__("time").time()
news._cache["m:CNBC"] = (now_s, [
    "Hims & Hers CEO says FTC lawsuit misunderstands how the company works",
    "'Too many people go bankrupt': Should Medicare provide catastrophic cover?",
    "Trump tariff deadline looms as U.S. and Canada say final trade deal is near"])
news._cache["m:MarketWatch"] = (now_s, ["Dividend stocks lost the yield war"])
news._cache["t:TSLA"] = (now_s, ["Tesla recalls 3 million vehicles in China"])
market = [t for _, t in news.hot_headlines()]
check("news: market-wide feeds flag the tariff story",
      any("tariff" in t for t in market))
check("news: market-wide feeds ignore a lawsuit and an opinion column",
      not any("lawsuit" in t or "bankrupt" in t for t in market), str(market))
check("news: the per-ticker feed still flags a recall on a name we trade",
      any("recall" in t for _, t in news.hot_headlines("TSLA")))
check("news: SPY never gets an earnings lookup (ETF, guaranteed 404)",
      news.next_earnings("SPY") is None and "SPY" in news.NO_EARNINGS)
news._cache.clear()

# ---------------------------------------------------------------------------
print("\n--- 8. brain: one text per empty balance, silence on rate limits ---")
import assistant
import telegram

_orig_state = config.STATE_FILE
_orig_post = assistant._api_session.post
_orig_send = telegram.send_to
_orig_owner = telegram.primary_owner_id
_orig_key = os.environ.get("ANTHROPIC_API_KEY")
config.STATE_FILE = config.DATA_DIR / "state_test_brain.json"
try:
    if config.STATE_FILE.exists():
        config.STATE_FILE.unlink()
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    texts = []
    telegram.send_to = lambda cid, text: texts.append((cid, text)) or None
    telegram.primary_owner_id = lambda: "1390237537"
    calls = []

    class Resp:
        def __init__(self, status, body, headers=None):
            self.status_code, self._body = status, body
            self.headers = headers or {}
            self.text = json.dumps(body)

        def json(self):
            return self._body

    script = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        return script.pop(0)

    assistant._api_session.post = fake_post
    billing = Resp(400, {"error": {"type": "invalid_request_error", "message":
                   "Your credit balance is too low to access the Anthropic API. "
                   "Please go to Plans & Billing to upgrade or purchase credits."}})
    script[:] = [billing]
    body, err = assistant._post_anthropic({"model": "x"}, 5)
    check("brain: an empty balance is a billing hold, not a countdown",
          body is None and assistant.billing_hold() is not None
          and assistant.cooldown_left_s() == 0)
    check("brain: the owner is told exactly once, with where to top up",
          len(texts) == 1 and "credits are empty" in texts[0][1]
          and "console.anthropic.com" in texts[0][1])
    check("brain: the reply is a short offline note, no 'Claude usage limit'",
          err == assistant.OFFLINE_TEXT and "limit" not in err.lower())
    n_calls = len(calls)
    body, err = assistant._post_anthropic({"model": "x"}, 5)
    check("brain: the next call inside the probe window never hits the API",
          len(calls) == n_calls and err == assistant.OFFLINE_TEXT)
    check("brain: ...and sends nothing", len(texts) == 1)
    # the probe window elapses: the API is tried again, fails again, still silent
    hold = assistant.billing_hold()
    hold["last_probe"] = hold["last_probe"] - assistant.BILLING_PROBE_S - 1
    config.state_set(assistant._BILLING_KEY, hold)
    script[:] = [billing]
    assistant._post_anthropic({"model": "x"}, 5)
    check("brain: a failed probe after the window re-arms the hold silently",
          len(calls) == n_calls + 1 and len(texts) == 1
          and assistant.billing_hold() is not None)
    check("brain: /status reads offline with the since-time",
          assistant.brain_status_line().startswith("offline")
          and "CT" in assistant.brain_status_line())
    # the owner tops up: the first successful probe clears the hold, one text
    hold = assistant.billing_hold()
    hold["last_probe"] = hold["last_probe"] - assistant.BILLING_PROBE_S - 1
    config.state_set(assistant._BILLING_KEY, hold)
    script[:] = [Resp(200, {"content": [{"type": "text", "text": "hi"}],
                            "stop_reason": "end_turn"})]
    body, err = assistant._post_anthropic({"model": "x"}, 5)
    check("brain: the first success clears the hold and announces it once",
          body is not None and assistant.billing_hold() is None
          and len(texts) == 2 and "back online" in texts[1][1])
    check("brain: /status reads online again", assistant.brain_status_line() == "online")
    # a genuine 429 usage window: silent countdown
    script[:] = [Resp(429, {"error": {"type": "rate_limit_error",
                                      "message": "You have exceeded your monthly usage limit"}},
                      {"retry-after": "3600"})]
    body, err = assistant._post_anthropic({"model": "x"}, 5)
    check("brain: a real usage limit arms a cooldown without any text",
          body is None and assistant.cooldown_left_s() > 0 and len(texts) == 2
          and "rate limit" in err)
    assistant.check_cooldown_recovery()
    config.state_set(assistant._COOLDOWN_KEY, 0)
    config.state_set(assistant._COOLDOWN_FLAG, True)
    assistant.check_cooldown_recovery()
    check("brain: recovery never texts, even with the legacy flag set",
          len(texts) == 2 and not config.state_get(assistant._COOLDOWN_FLAG, False))
    check("brain: a 400 that is not about money is not a hold",
          not assistant._looks_like_billing(400, "invalid_request_error",
                                            "max_tokens must be positive")
          and not assistant._looks_like_usage_limit(400, "", "credit", None))
finally:
    assistant._api_session.post = _orig_post
    telegram.send_to = _orig_send
    telegram.primary_owner_id = _orig_owner
    if _orig_key is None:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    else:
        os.environ["ANTHROPIC_API_KEY"] = _orig_key
    if config.STATE_FILE.exists():
        config.STATE_FILE.unlink()
    config.STATE_FILE = _orig_state

# ---------------------------------------------------------------------------
print("\n--- 9. review fixes: stale rows, the close, outage texts, news thread ---")
sniper_book.LEDGER = config.DATA_DIR / "sniper_positions_test3.json"
try:
    if sniper_book.LEDGER.exists():
        sniper_book.LEDGER.unlink()
    fri = datetime(2026, 8, 21, 15, 55, 20, tzinfo=ET)
    mon = datetime(2026, 8, 24, 9, 50, 20, tzinfo=ET)
    sniper_book.open_trade("SPY", "SPY", "BUY", 100.0, 99.0, 100.4, "2026-08-21",
                           "15:55:20", entry_ts=fri)
    sniper_book.step("SPY", 100.2, fri)   # last price seen that day
    mon_bars = [[datetime(2026, 8, 24, 9, 30, tzinfo=ET).isoformat(), 99.5, 99.6, 98.5, 98.7]]
    stale = sniper_book.step("SPY", 98.7, mon, bars=mon_bars)
    check("stale: a Friday leftover is settled flat on Monday, not stopped on "
          "Monday's gap", stale is not None and stale["exit_reason"] == "session end"
          and stale.get("exit_via") == "stale" and stale["exit_price"] == 100.2
          and abs(stale["r"] - 0.2) < 1e-9)
    svc2 = scanner.Service.__new__(scanner.Service)
    texts2 = []
    svc2.notify = lambda text: texts2.append(text)
    sniper_book.open_trade("TSLA", "TSLA", "SELL", 300.0, 302.0, 299.2,
                           "2026-08-21", "15:55:20", entry_ts=fri)
    svc2._step_sniper("TSLA", 301.0, mon, mon_bars)
    check("stale: the text says it was closed late and not graded on a new day",
          texts2 and "SESSION END" in texts2[-1] and "new day" in texts2[-1]
          and "STOPPED" not in texts2[-1])
    # the close: open at 15:55:20, still open at 15:56, settled once 16:00 passed
    sniper_book.open_trade("EURUSD=X", "EUR/USD", "BUY", 1.1700, 1.1690, 1.1704,
                           "2026-08-21", "15:55:20", decimals=4, entry_ts=fri)
    still = sniper_book.step("EURUSD=X", 1.1701, datetime(2026, 8, 21, 15, 56, tzinfo=ET))
    done_ = sniper_book.step("EURUSD=X", 1.1701, datetime(2026, 8, 21, 16, 0, 20, tzinfo=ET))
    check("close: a trade is not settled at 15:56 but is once 16:00 has passed",
          still is None and done_ is not None and done_["exit_reason"] == "session end"
          and sniper_book.SETTLE_ET == time(16, 0))
    # legacy rows (no entry_bar_ts) anchor on their own clock
    sniper_book.open_trade("^GSPC", "SPX", "BUY", 7000.0, 6990.0, 7004.0,
                           "2026-08-24", "10:02:17")
    legacy_row = sniper_book.open_rows()[0]
    check("legacy: a row opened without entry_ts still gets a fill bar",
          legacy_row["entry_bar_ts"].startswith("2026-08-24T10:00"))
    legacy_row["entry_bar_ts"] = None
    sniper_book._write(sniper_book._read()[:-1] + [legacy_row])
    old_bar = [[datetime(2026, 8, 24, 9, 30, tzinfo=ET).isoformat(), 6995.0, 6996.0, 6985.0, 6990.0]]
    res = sniper_book.step("^GSPC", 7001.0, mon + timedelta(minutes=20), bars=old_bar)
    check("legacy: bars from before a legacy row's own clock are never graded",
          res is None and sniper_book.has_open("^GSPC"))
finally:
    if sniper_book.LEDGER.exists():
        sniper_book.LEDGER.unlink()
    sniper_book.LEDGER = _orig_ledger

wm = scanner.Service.sniper_watch_mode
check("watch: entries inside the window",
      wm(datetime(2026, 8, 24, 10, 0, tzinfo=ET), False) == "entries")
check("watch: after the close the thread keeps stepping an open trade",
      wm(datetime(2026, 8, 24, 16, 0, 20, tzinfo=ET), True) == "step")
check("watch: after the close with nothing open it is off",
      wm(datetime(2026, 8, 24, 16, 0, 20, tzinfo=ET), False) == "off")
check("watch: stepping stops at 16:15", wm(datetime(2026, 8, 24, 16, 20, tzinfo=ET), True) == "off")
check("watch: weekends are off", wm(datetime(2026, 8, 22, 12, 0, tzinfo=ET), True) == "off")

svc3 = scanner.Service.__new__(scanner.Service)
for t in ("brain is resting after a rate limit. Back in 1h00m (around 3 PM CT).",
          assistant.OFFLINE_TEXT, "My brain is unavailable right now: x",
          "I read it but came back empty, try rephrasing?", "that one hit an error on my end", ""):
    check(f"outage: {t[:38]!r} is never a Quick read", assistant.is_outage_text(t))
check("outage: a real one-liner is not an outage",
      not assistant.is_outage_text("SPX could dip at the open on the tariff headline."))
_orig_state3 = config.STATE_FILE
config.STATE_FILE = config.DATA_DIR / "state_test_brain2.json"
_orig_respond = assistant.respond
_orig_key3 = os.environ.get("ANTHROPIC_API_KEY")
try:
    if config.STATE_FILE.exists():
        config.STATE_FILE.unlink()
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    called = []
    svc3.cfg = type("C", (), {"watchlist": {"SPX": "^GSPC"}})()
    svc3.status_text = lambda: "state"
    assistant.respond = lambda *a, **k: called.append(1) or "Brain is resting after a rate limit."
    config.state_set(assistant._COOLDOWN_KEY, __import__("time").time() + 3600)
    check("news thread: a resting brain returns no take and never calls the model",
          svc3._news_take("Tariffs") is None and not called)
    config.state_set(assistant._COOLDOWN_KEY, 0)
    check("news thread: an outage-shaped reply is still filtered",
          svc3._news_take("Tariffs") is None and called)
    assistant.respond = lambda *a, **k: "Tariff shock, expect a gap down on SPX."
    check("news thread: a real one-liner still goes out",
          svc3._news_take("Tariffs") == "Tariff shock, expect a gap down on SPX.")
    check("billing: a 429 that mentions credit is billing, not a countdown",
          assistant._looks_like_billing(429, "rate_limit_error", "insufficient credit")
          and not assistant._looks_like_usage_limit(429, "", "insufficient credit", None))
    # the one text counts only when Telegram accepted it
    attempts = []
    telegram.send_to = lambda cid, text: attempts.append(text) or "HTTP 502"
    telegram.primary_owner_id = lambda: "1"
    assistant._start_billing_hold("credit balance is too low")
    check("billing: a failed send leaves the text owed",
          len(attempts) == 1 and assistant.billing_hold()["notified"] is False)
    telegram.send_to = lambda cid, text: attempts.append(text) or None
    assistant._start_billing_hold("credit balance is too low")
    assistant._start_billing_hold("credit balance is too low")
    check("billing: the next refusal sends it, then it stays quiet",
          len(attempts) == 2 and assistant.billing_hold()["notified"] is True)
    config.state_set(assistant._BILLING_KEY, None)
finally:
    assistant.respond = _orig_respond
    telegram.send_to = _orig_send
    telegram.primary_owner_id = _orig_owner
    if _orig_key3 is None:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    else:
        os.environ["ANTHROPIC_API_KEY"] = _orig_key3
    if config.STATE_FILE.exists():
        config.STATE_FILE.unlink()
    config.STATE_FILE = _orig_state3

old_bars = bars_frame(day, [(9, 30), (9, 35), (9, 40), (9, 45), (9, 50), (9, 55)])
aged = fvg.sniper_check(old_bars, "BUY", 100.05, 0.05, None, "SPY",
                        datetime(2026, 8, 21, 10, 20, tzinfo=ET))
check("fvg: a signal bar twenty minutes old fails the gate as a stale feed",
      any("minutes old" in r for r in aged["reasons"]))
check("fvg: a fresh bar passes the age check",
      not any("minutes old" in r for r in
              fvg.sniper_check(old_bars, "BUY", 100.05, 0.05, None, "SPY",
                               datetime(2026, 8, 21, 10, 2, tzinfo=ET))["reasons"]))

# the breaking-news thread end to end: seed, dedup, routing
_orig_seen = config.NEWS_SEEN_FILE
config.NEWS_SEEN_FILE = config.DATA_DIR / "news_seen_test.json"
try:
    if config.NEWS_SEEN_FILE.exists():
        config.NEWS_SEEN_FILE.unlink()
    svc4 = scanner.Service.__new__(scanner.Service)
    sent4 = []
    svc4.notify = lambda text: sent4.append(text)
    svc4._news_take_and_send = lambda title: None
    svc4.cfg = type("C", (), {"watchlist": {"SPX": "^GSPC", "TSLA": "TSLA"}})()
    now_s2 = __import__("time").time()
    news._cache.clear()
    news._cache["m:CNBC"] = (now_s2, [canada[0], "Hims & Hers CEO says FTC lawsuit misunderstands the company"])
    news._cache["m:MarketWatch"] = (now_s2, ["Dividend stocks lost the yield war"])
    news._cache["t:TSLA"] = (now_s2, ["Tesla recalls 3 million vehicles in China"])
    svc4._scan_news_once()
    check("news thread: the day's first pass seeds silently", not sent4)
    news._cache["m:MarketWatch"] = (now_s2 + 1, [canada[1], "Fed emergency rate cut talk grows"])
    news._cache["m:CNBC"] = (now_s2 + 1, [canada[0], canada[2], "Hims & Hers CEO says FTC lawsuit misunderstands the company"])
    svc4._scan_news_once()
    check("news thread: two retellings of the seeded tariff story are not sent, "
          "the new Fed story is", len(sent4) == 1 and "Fed emergency" in sent4[0]
          and "BREAKING (MarketWatch)" in sent4[0], str(sent4))
    seen_now, _d = svc4._load_news_seen()
    check("news thread: the retellings are marked seen so they never fire later",
          canada[1] in seen_now and canada[2] in seen_now)
    check("news thread: a lawsuit headline on the market wire never fired",
          not any("lawsuit" in t for t in sent4))
    news._cache.clear()
finally:
    if config.NEWS_SEEN_FILE.exists():
        config.NEWS_SEEN_FILE.unlink()
    config.NEWS_SEEN_FILE = _orig_seen

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL SESSION-FIX TESTS PASSED")
