"""Offline tests for the trading calendar and the paid-API spending policy.

Two changes are covered here, and every check below fails before its change:

1. The bot knew nothing about market holidays. weekday() was the only test for
   "is the market open", so on the nine or ten closures a year it ran a full
   session against a dead feed, texted a morning card, fired forex sniper
   tickets on a US session that did not exist, and let the nightly review grade
   the empty day as a deliberate choice to stay out. Now market_calendar
   answers that question and everything downstream asks it, plus the bot texts
   a heads-up the evening before.

2. Anything the bot decided to do by itself could spend the metered Anthropic
   balance. That balance is not the owner's subscription, it has hit $0 more
   than once, and it takes the whole chat brain down when it does. Now every
   paid call declares a purpose and scheduled work does not spend by default.

No network, no Telegram, no repo scripts executed.

Run:  python test_market_calendar.py     (exit code 0 = all good)
"""

import os
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config as config_mod
import market_calendar as mc

ET = ZoneInfo("America/New_York")
failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# --------------------------------------------------------------------------
# 1. the holiday rules, against the real published NYSE calendars
# --------------------------------------------------------------------------
# These are the actual NYSE closure dates for each year, typed from the
# exchange's own holiday notices. They are the point of the whole module: if a
# rule drifts, one of these lists stops matching.
KNOWN = {
    2024: ["2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
           "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25"],
    2025: ["2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18",
           "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
           "2025-12-25"],
    2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
           "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"],
    2027: ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
           "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"],
}
for year, want in KNOWN.items():
    got = [str(d) for d in sorted(mc.holidays(year))]
    check(f"holidays {year} match the published NYSE calendar", got == want,
          f"got {got}")

KNOWN_HALF = {
    2024: ["2024-07-03", "2024-11-29", "2024-12-24"],
    2025: ["2025-07-03", "2025-11-28", "2025-12-24"],
    2026: ["2026-11-27", "2026-12-24"],   # Jul 3 is the holiday itself in 2026
    2027: ["2027-11-26"],                 # Jul 3 Sat, Dec 24 is the holiday
}
for year, want in KNOWN_HALF.items():
    got = [str(d) for d in sorted(mc.early_closes(year))]
    check(f"half days {year} match the published NYSE calendar", got == want,
          f"got {got}")

# the observed-date rule, both directions
check("Saturday holiday observes back to the Friday",
      mc.holiday_name(date(2026, 7, 3)) == "Independence Day",
      "Jul 4 2026 is a Saturday")
check("Sunday holiday observes forward to the Monday",
      mc.holiday_name(date(2027, 7, 5)) == "Independence Day",
      "Jul 4 2027 is a Sunday")
# the New Year's exception: the observed day would land in the prior year, and
# the NYSE simply does not close for it
for y in (2022, 2028, 2033):
    names = set(mc.holidays(y).values())
    check(f"no New Year's closure in {y} (Jan 1 is a Saturday)",
          "New Year's Day" not in names)
check("Dec 31 stays a trading day before a Saturday New Year",
      mc.is_trading_day(date(2021, 12, 31)))

# Juneteenth only from 2022, which matters because the backtests replay 2022+
check("Juneteenth is not a holiday in 2021",
      "Juneteenth" not in set(mc.holidays(2021).values()))
check("Juneteenth is a holiday from 2022",
      "Juneteenth" in set(mc.holidays(2022).values()))

# Good Friday, spot-checked against the real Easter dates
for y, gf in ((2024, "2024-03-29"), (2025, "2025-04-18"), (2026, "2026-04-03"),
              (2030, "2030-04-19")):
    got = [str(d) for d, n in mc.holidays(y).items() if n == "Good Friday"]
    check(f"Good Friday {y} is {gf}", got == [gf], f"got {got}")

check("a weekend is not a holiday, it is just a weekend",
      mc.holiday_name(date(2026, 9, 5)) is None and
      not mc.is_trading_day(date(2026, 9, 5)))
check("an unscheduled closure is honored",
      mc.holiday_name(date(2025, 1, 9)) is not None)
check("every emitted closure is a weekday",
      all(d.weekday() < 5 for y in range(2020, 2036)
          for d in mc.holidays(y)))
check("no date is both a closure and a half day",
      all(not (set(mc.holidays(y)) & set(mc.early_closes(y)))
          for y in range(2020, 2036)))

# session_close is what stops a half day being graded against bars that never
# printed
check("a regular session closes at 16:00 ET",
      mc.session_close(date(2026, 9, 8)) == time(16, 0))
check("a half day closes at 13:00 ET",
      mc.session_close(date(2026, 11, 27)) == time(13, 0))

# --------------------------------------------------------------------------
# 2. walking the calendar, which is what the eve notice is built on
# --------------------------------------------------------------------------
check("next_trading_day steps over a holiday",
      mc.next_trading_day(date(2026, 9, 4)) == date(2026, 9, 8),
      "Fri 9/4 -> Tue 9/8, because Mon 9/7 is Labor Day")
check("prev_trading_day steps back over a holiday",
      mc.prev_trading_day(date(2026, 9, 8)) == date(2026, 9, 4))
check("upcoming_closures finds a Monday holiday from the Friday",
      mc.upcoming_closures(date(2026, 9, 4)) == [(date(2026, 9, 7), "Labor Day")])
check("upcoming_closures is empty on an ordinary Tuesday",
      mc.upcoming_closures(date(2026, 9, 8)) == [])
check("upcoming_closures finds Thanksgiving from the Wednesday",
      mc.upcoming_closures(date(2026, 11, 25))
      == [(date(2026, 11, 26), "Thanksgiving")])
check("a Friday before an ordinary weekend announces nothing",
      mc.upcoming_closures(date(2026, 9, 11)) == [])

# the wording helper: this is the difference between a Friday text that reads
# right and one that claims Labor Day is "tomorrow" three days early
check("day_reference says tomorrow when it is tomorrow",
      mc.day_reference(date(2026, 11, 26), date(2026, 11, 25)) == "tomorrow")
check("day_reference names the weekday for a Monday holiday",
      mc.day_reference(date(2026, 9, 7), date(2026, 9, 4)) == "Monday")
check("day_reference says today for today",
      mc.day_reference(date(2026, 9, 7), date(2026, 9, 7)) == "today")
check("day_reference dates anything a week or more out",
      "Sep" in mc.day_reference(date(2026, 9, 14), date(2026, 9, 4)))

# --------------------------------------------------------------------------
# 3. the cards
# --------------------------------------------------------------------------
import cards

card = cards.holiday_card([(date(2026, 9, 7), "Labor Day")], date(2026, 9, 4),
                          date(2026, 9, 8))
check("holiday card names the day and the holiday",
      "Monday is Labor Day." in card, card)
check("holiday card says no trades will be sent",
      "no trades will be sent" in card)
check("holiday card says when we resume",
      "Back at it Tuesday." in card, card)
check("holiday card headline names the day off",
      card.startswith("📅 NO TRADING MONDAY"), card)
check("holiday card is empty with nothing to announce",
      cards.holiday_card([], date(2026, 9, 4), date(2026, 9, 8)) == "")

# the literal night-before case, which is what the owner asked for
eve = cards.holiday_card([(date(2026, 11, 26), "Thanksgiving")],
                         date(2026, 11, 25), date(2026, 11, 27))
check("the night-before card says tomorrow, not a weekday name",
      "NO TRADING TOMORROW" in eve and "Tomorrow is Thanksgiving." in eve, eve)
check("the night-before card says when we resume",
      "Back at it Friday." in eve, eve)
check("the card stays short: four lines, no filler",
      len([l for l in eve.splitlines() if l.strip()]) == 3, eve)

# Christmas Day 2026 is a Friday, so the resume day has to clear the weekend
xmas = cards.holiday_card([(date(2026, 12, 25), "Christmas Day")],
                          date(2026, 12, 24), date(2026, 12, 28))
check("a Friday holiday resumes the following Monday, not Saturday",
      "Back at it Monday." in xmas, xmas)

half = cards.half_day_card(date(2026, 11, 27), "the day after Thanksgiving",
                           date(2026, 11, 25))
check("half day card does not claim the market is closed",
      "NO TRADING" not in half and "SHORT DAY" in half, half)
check("half day card gives the early close in CT",
      "12:00 PM CT" in half, half)
check("half day card says setups still go out",
      "Setups still go out" in half, half)

# the house rule: no em dashes in anything a member reads
for name, text in (("holiday card", card), ("half day card", half)):
    check(f"no em dash in the {name}", "—" not in text and "–" not in text)

# --------------------------------------------------------------------------
# 4. the live loop asks the calendar, not weekday()
# --------------------------------------------------------------------------
import scanner

check("is_session_day is False on Labor Day",
      not scanner.is_session_day(date(2026, 9, 7)))
check("is_session_day is True on the Tuesday after",
      scanner.is_session_day(date(2026, 9, 8)))
check("is_session_day is False on a Saturday",
      not scanner.is_session_day(date(2026, 9, 5)))
check("session_end_for is 16:12 on a normal day",
      scanner.session_end_for(date(2026, 9, 8)) == time(16, 12))
check("session_end_for is 13:12 on a half day",
      scanner.session_end_for(date(2026, 11, 27)) == time(13, 12))

# the nightly review must point at the last real SESSION, never at a holiday
check("learn_session_due skips Labor Day back to the Friday",
      scanner.learn_session_due(datetime(2026, 9, 7, 22, 0, tzinfo=ET))
      == date(2026, 9, 4))
check("learn_session_due skips a Thanksgiving Thursday back to the Wednesday",
      scanner.learn_session_due(datetime(2026, 11, 26, 22, 0, tzinfo=ET))
      == date(2026, 11, 25))
check("learn_session_due still returns today on a normal trading night",
      scanner.learn_session_due(datetime(2026, 9, 8, 23, 50, tzinfo=ET))
      == date(2026, 9, 8))

# --------------------------------------------------------------------------
# 5. the sniper does not fire on a day the session never happened
# --------------------------------------------------------------------------
import fvg

check("sniper window is shut on Labor Day",
      not fvg.sniper_window_open(datetime(2026, 9, 7, 11, 0, tzinfo=ET)),
      "forex still quotes, but the round-6 config was validated on US sessions")
check("sniper window is open at the same hour the next day",
      fvg.sniper_window_open(datetime(2026, 9, 8, 11, 0, tzinfo=ET)))
check("sniper window still respects the 09:50 floor",
      not fvg.sniper_window_open(datetime(2026, 9, 8, 9, 49, tzinfo=ET)))
check("sniper window closes at 13:00 on a half day",
      fvg.sniper_window_open(datetime(2026, 11, 27, 12, 59, tzinfo=ET))
      and not fvg.sniper_window_open(datetime(2026, 11, 27, 13, 1, tzinfo=ET)))
check("sniper window is still open at 13:01 on a full day",
      fvg.sniper_window_open(datetime(2026, 11, 30, 13, 1, tzinfo=ET)))
check("the verified entry floor constant is untouched",
      fvg._SNIPER_OPEN_ET == time(9, 50))

import sniper_book

check("sniper settles at 16:00 on a normal day",
      sniper_book._settle_at(datetime(2026, 9, 8, 12, 0, tzinfo=ET)) == time(16, 0))
check("sniper settles at 13:00 on a half day",
      sniper_book._settle_at(datetime(2026, 11, 27, 12, 0, tzinfo=ET)) == time(13, 0))
check("sniper settle falls back to 16:00 for an undatable input",
      sniper_book._settle_at(None) == sniper_book.SETTLE_ET)

import positions as poslib

check("a 0DTE expires at 16:00 on a normal session",
      poslib.close_t(date(2026, 9, 8)) == time(16, 0))
check("a 0DTE expires at 13:00 on a half day",
      poslib.close_t(date(2026, 11, 27)) == time(13, 0),
      "the old fixed 16:00 held an expired position open for three hours")
check("the expiry warning tracks the real bell",
      poslib.warn_t(date(2026, 11, 27))
      == time(12, 60 - int(config_mod.EXPIRY_WARN_MINUTES))
      if int(config_mod.EXPIRY_WARN_MINUTES) <= 60 else True)
check("the expiry warning is unchanged on a normal day",
      poslib.warn_t(date(2026, 9, 8)) == poslib.WARN_T)
check("close_t falls back to 16:00 for an undatable input",
      poslib.close_t(None) == poslib.CLOSE_T)

# --------------------------------------------------------------------------
# 6. the paid-API spending policy
# --------------------------------------------------------------------------
import config

_saved_mode = os.environ.get("API_MODE")
_saved_cap = os.environ.get("API_MAX_CALLS_PER_DAY")


def _set(mode=None, cap=None):
    for k, v in (("API_MODE", mode), ("API_MAX_CALLS_PER_DAY", cap)):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


try:
    _set()  # nothing configured at all
    check("the default policy answers humans",
          config.api_allows("chat")[0])
    check("the default policy refuses work the bot chose to do",
          not config.api_allows("scheduled")[0],
          "this is the whole point: the nightly loop must not spend")
    check("the refusal explains itself in plain English",
          "desktop" in config.api_allows("scheduled")[1],
          config.api_allows("scheduled")[1])
    check("an unknown purpose is treated as scheduled, not as chat",
          not config.api_allows("whatever_new_caller")[0])

    _set(mode="off")
    check("API_MODE=off refuses a human too",
          not config.api_allows("chat")[0])

    _set(mode="full")
    check("API_MODE=full lets scheduled work spend",
          config.api_allows("scheduled")[0])

    _set(mode="garbage")
    check("an unrecognized API_MODE falls back to the safe policy, not the open one",
          config.api_mode() == config.API_DEFAULT_MODE
          and not config.api_allows("scheduled")[0])

    _set(mode="full", cap="0")
    check("cap 0 means no ceiling", config.api_daily_cap() == 0
          and config.api_allows("chat")[0])
    _set(mode="full", cap="not-a-number")
    check("a junk cap falls back to the built-in, it does not crash",
          config.api_daily_cap() == 60)

    # the daily ceiling, exercised against a real (temporary) state file
    import json
    import tempfile
    _saved_state = config.STATE_FILE
    tmpdir = tempfile.mkdtemp()
    config.STATE_FILE = __import__("pathlib").Path(tmpdir) / "state.json"
    try:
        _set(mode="full", cap="3")
        config.api_note_call("chat")
        config.api_note_call("scheduled")
        c = config.api_counts()
        check("calls are counted per purpose",
              c["chat"] == 1 and c["scheduled"] == 1, str(c))
        check("under the cap it still allows", config.api_allows("chat")[0])
        config.api_note_call("chat")
        check("the daily cap stops the next call",
              not config.api_allows("chat")[0],
              config.api_allows("chat")[1])
        check("the cap message says when it resets",
              "midnight ET" in config.api_allows("chat")[1])
        check("the usage line reports the tally",
              "3/3" in config.api_usage_line(), config.api_usage_line())
        # a stale day's tally must not carry over
        config.state_set(config.API_CALLS_KEY,
                         {"date": "1999-01-01", "chat": 99, "scheduled": 99})
        check("yesterday's tally does not count against today",
              config.api_counts()["chat"] == 0
              and config.api_allows("chat")[0])
    finally:
        config.STATE_FILE = _saved_state

    # the choke point itself refuses before it ever opens a socket
    import assistant
    _set(mode="ask_only")
    body, err = assistant._post_anthropic({"model": "x"}, 5, purpose="scheduled")
    check("_post_anthropic refuses scheduled work without touching the network",
          body is None and "does not spend" in (err or ""), str(err))
    check("assistant.complete defaults to the scheduled purpose",
          __import__("inspect").signature(assistant.complete)
          .parameters["purpose"].default == "scheduled")
    check("assistant.deep_think defaults to the chat purpose",
          __import__("inspect").signature(assistant.deep_think)
          .parameters["purpose"].default == "chat")
    check("_post_anthropic defaults to the chat purpose, the one a human is owed",
          __import__("inspect").signature(assistant._post_anthropic)
          .parameters["purpose"].default == "chat")

    # the morning news check posts to Anthropic on its own, outside the choke
    # point, so it has to apply the policy by hand
    import risk_gate
    _set(mode="ask_only")
    check("the morning news check is skipped when scheduled spend is off",
          risk_gate.anthropic_news_check(date(2026, 9, 8)) is None)

    # ----------------------------------------------------------------------
    # 7. getting out of a billing hold without a human poking it
    # ----------------------------------------------------------------------
    # The bot told its owner his credits were empty for days while the account
    # had money in it. A hold only ever ended when some unrelated call
    # happened to succeed, and with the nightly review off and nobody chatting
    # there were no calls, so the stale verdict just sat there being reported
    # as current.
    _set(mode="ask_only")
    check("a probe is allowed even when scheduled spend is not",
          config.api_allows("probe")[0]
          and not config.api_allows("scheduled")[0])
    _set(mode="off")
    check("API_MODE=off stops even the probe",
          not config.api_allows("probe")[0])
    _set(mode="ask_only")

    _saved_state2 = config.STATE_FILE
    tmpdir2 = tempfile.mkdtemp()
    config.STATE_FILE = __import__("pathlib").Path(tmpdir2) / "state.json"
    _saved_post = assistant._post_anthropic
    _saved_key2 = os.environ.get("ANTHROPIC_API_KEY")
    try:
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        check("probe_billing is a no-op when there is no hold",
              assistant.probe_billing() is True)

        # arm a hold, then let the probe find the balance restored
        posted = []

        def _ok_post(payload, timeout, purpose="chat"):
            posted.append((payload, purpose))
            assistant._end_billing_hold()
            return {"content": [{"type": "text", "text": "hi"}]}, None

        config.state_set(assistant._BILLING_KEY,
                         {"since": 1, "last_probe": 1, "notified": True})
        check("a hold is armed", assistant.billing_hold() is not None)
        assistant._post_anthropic = _ok_post
        back = assistant.probe_billing()
        check("probe_billing clears the hold once the API answers",
              back is True and assistant.billing_hold() is None)
        check("the probe is the cheapest call there is: one token",
              posted and posted[0][0]["max_tokens"] == 1, str(posted[:1]))
        check("the probe declares itself a probe, not a chat",
              posted and posted[0][1] == "probe")
        check("the probe sends no system prompt to pay for",
              posted and "system" not in posted[0][0])

        # the status line must say WHEN it last checked, not just when it broke
        config.state_set(assistant._BILLING_KEY,
                         {"since": 1, "last_probe": 1, "notified": True})
        line = assistant.brain_status_line()
        check("the status line admits when it last actually checked",
              "last checked" in line, line)
        check("the status line no longer states an empty balance as fact",
              "credits empty" not in line, line)
        check("the status line says how to force a check",
              "/brain" in line, line)
    finally:
        assistant._post_anthropic = _saved_post
        config.STATE_FILE = _saved_state2
        if _saved_key2 is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = _saved_key2
finally:
    _set(_saved_mode, _saved_cap)

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print(f"all good")
