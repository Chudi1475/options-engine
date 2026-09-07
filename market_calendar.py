"""US equity market trading calendar: holidays, half days, session dates.

Why this exists: every scheduled job in this bot used weekday() as a stand-in
for "the market is open today". That is wrong about nine to ten weekdays a
year. On those days the old code ran a full session, sent a morning card,
scanned for entries against a feed that never ticks, and then graded the empty
day as "we stayed out, being picky", which is a fabricated lesson the brain
reads back on every reply.

Design rules:
- Pure standard library. No pandas, no network, no vendor calendar package.
  This module is imported by the live loop and must never be able to fail on a
  dependency or a rate limit.
- Everything is computed from RULES, not a hand-maintained list of dates, so it
  stays correct in 2031 without anyone editing a file. The only literal dates
  are the unscheduled one-off closures, which no rule can derive.
- Dates in, dates out. This module knows nothing about the bot. Callers keep
  their own timezone handling and pass an ET calendar date.

The rules (NYSE / US equities):
  New Year's Day       Jan 1, observed
  MLK Day              third Monday in January
  Washington's Bday    third Monday in February
  Good Friday          the Friday before Easter Sunday
  Memorial Day         last Monday in May
  Juneteenth           Jun 19, observed, market holiday from 2022 on
  Independence Day     Jul 4, observed
  Labor Day            first Monday in September
  Thanksgiving         fourth Thursday in November
  Christmas Day        Dec 25, observed

"Observed" means: a Saturday holiday moves back to the Friday before, a Sunday
holiday moves forward to the Monday after. New Year's Day is the one exception.
When Jan 1 lands on a Saturday the market does NOT close on Dec 31, because the
observed day would fall in the previous year, so that year simply has no New
Year's closure.

Half days close at 13:00 ET instead of 16:00 ET:
  Jul 3          when both Jul 3 and Jul 4 are weekdays
  the Friday after Thanksgiving
  Dec 24         when both Dec 24 and Dec 25 are weekdays
"""

from datetime import date, time, timedelta

# The regular close, and the early close on a half day. Both ET wall clock.
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# Juneteenth became a federal holiday in June 2021; the NYSE first closed for
# it in 2022. Before that it was a normal trading day, which matters because
# the backtests replay 2022 to 2026 history.
JUNETEENTH_FROM = 2022

# Unscheduled closures. A rule cannot derive a national day of mourning, so the
# handful that exist live here. Keep this list short and sourced; anything you
# cannot point at a real NYSE notice for does not belong in it.
ONE_OFF_CLOSURES = {
    date(2018, 12, 5): "National Day of Mourning (George H. W. Bush)",
    date(2025, 1, 9): "National Day of Mourning (Jimmy Carter)",
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth given weekday of a month, e.g. the third Monday of January.
    weekday uses date.weekday(): Monday is 0."""
    d = date(year, month, 1)
    shift = (weekday - d.weekday()) % 7
    return d + timedelta(days=shift + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last given weekday of a month, e.g. the last Monday of May."""
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def easter(year: int) -> date:
    """Easter Sunday, Gregorian calendar, by the anonymous Meeus algorithm.
    Good Friday is two days earlier and is the only moving religious holiday
    the NYSE observes."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lam) // 451
    month, day = divmod(h + lam - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date:
    """Move a fixed-date holiday to the day the market actually closes."""
    if d.weekday() == 5:            # Saturday, closed the Friday before
        return d - timedelta(days=1)
    if d.weekday() == 6:            # Sunday, closed the Monday after
        return d + timedelta(days=1)
    return d


def holidays(year: int) -> dict:
    """Every full market closure in a calendar year, as {date: name}.

    Observed dates are what comes back, so a caller never has to apply the
    weekend rule itself. A closure whose observed date is pushed into the next
    year (New Year's Day on a Saturday) is dropped, which matches the NYSE:
    the market trades a normal session on that Dec 31."""
    out = {}

    ny = _observed(date(year, 1, 1))
    if ny.year == year:
        # Jan 1 on a Saturday would observe back to Dec 31 of the prior year.
        # The NYSE does not close for that, so the year has no New Year's day.
        out[ny] = "New Year's Day"

    out[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    out[_nth_weekday(year, 2, 0, 3)] = "Presidents Day"
    out[easter(year) - timedelta(days=2)] = "Good Friday"
    out[_last_weekday(year, 5, 0)] = "Memorial Day"

    if year >= JUNETEENTH_FROM:
        out[_observed(date(year, 6, 19))] = "Juneteenth"

    out[_observed(date(year, 7, 4))] = "Independence Day"
    out[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    out[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving"

    xmas = _observed(date(year, 12, 25))
    if xmas.year == year:
        out[xmas] = "Christmas Day"

    for d, name in ONE_OFF_CLOSURES.items():
        if d.year == year:
            out[d] = name

    # A holiday whose observed date lands on a weekend cannot be a closure.
    # Nothing above should produce one, but a bad edit here would silently
    # start skipping real trading days, so refuse to emit it.
    return {d: n for d, n in out.items() if d.weekday() < 5}


def early_closes(year: int) -> dict:
    """Every 13:00 ET half day in a calendar year, as {date: reason}."""
    out = {}
    full = holidays(year)

    jul3 = date(year, 7, 3)
    if jul3.weekday() < 5 and date(year, 7, 4).weekday() < 5 and jul3 not in full:
        out[jul3] = "the day before Independence Day"

    # The Friday after Thanksgiving. Thanksgiving is always a Thursday, so this
    # is always a Friday and always a half day.
    out[_nth_weekday(year, 11, 3, 4) + timedelta(days=1)] = "the day after Thanksgiving"

    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5 and date(year, 12, 25).weekday() < 5 and dec24 not in full:
        out[dec24] = "Christmas Eve"

    return {d: r for d, r in out.items() if d.weekday() < 5 and d not in full}


def holiday_name(d: date):
    """The closure name if the market is shut for a holiday that day, else None.
    Weekends are not holidays; they are just weekends."""
    if d.weekday() >= 5:
        return None
    return holidays(d.year).get(d)


def is_holiday(d: date) -> bool:
    return holiday_name(d) is not None


def is_trading_day(d: date) -> bool:
    """True when US equities have a session that day, half day or not."""
    return d.weekday() < 5 and not is_holiday(d)


def early_close_reason(d: date):
    """The reason string if that date is a 13:00 ET half day, else None."""
    if not is_trading_day(d):
        return None
    return early_closes(d.year).get(d)


def is_early_close(d: date) -> bool:
    return early_close_reason(d) is not None


def session_close(d: date) -> time:
    """When the market actually closes that day. Callers that settle positions
    or grade a day off the last bar must use this, not a hard-coded 16:00, or a
    half day gets graded against three hours of bars that do not exist."""
    return EARLY_CLOSE if is_early_close(d) else REGULAR_CLOSE


def next_trading_day(d: date) -> date:
    """The next session strictly after d."""
    n = d + timedelta(days=1)
    while not is_trading_day(n):
        n += timedelta(days=1)
    return n


def prev_trading_day(d: date) -> date:
    """The last session strictly before d."""
    p = d - timedelta(days=1)
    while not is_trading_day(p):
        p -= timedelta(days=1)
    return p


def upcoming_closures(d: date) -> list:
    """Holidays that fall between d and the next session, as [(date, name)].

    This is what the eve-of-holiday notice is built on. Run it on a session's
    evening and it answers "what is shut before we trade again", which is the
    honest question. On the Friday before Labor Day it returns Monday, because
    the weekend is skipped over rather than treated as news. On an ordinary
    Tuesday it returns nothing.

    A holiday pair like Christmas landing next to a weekend comes back as a
    list in date order, so the notice can name both instead of only the first.
    """
    out = []
    n = d + timedelta(days=1)
    stop = next_trading_day(d)
    while n < stop:
        name = holiday_name(n)
        if name:
            out.append((n, name))
        n += timedelta(days=1)
    return out


def day_reference(target: date, today: date) -> str:
    """How a person would say a date out loud, relative to today.

    "tomorrow" when it really is tomorrow, otherwise the weekday name, and the
    calendar date once it is far enough out that the weekday alone is
    ambiguous. The eve notice goes out on the last session before a closure,
    which for a Monday holiday is the Friday evening, so this is the difference
    between a text that reads right and one that says a holiday is "tomorrow"
    three days early."""
    delta = (target - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if 2 <= delta <= 6:
        return target.strftime("%A")
    # %-d is a glibc extension and blows up on Windows, where this repo is
    # also developed, so the day number is appended by hand.
    return f"{target:%A %b} {target.day}"


def describe(d: date) -> str:
    """One honest line about a date, for logs and the /health text."""
    name = holiday_name(d)
    if name:
        return f"{d} is {name}, market closed"
    if d.weekday() >= 5:
        return f"{d} is a weekend, market closed"
    reason = early_close_reason(d)
    if reason:
        return f"{d} is a half day ({reason}), market closes 13:00 ET"
    return f"{d} is a regular session"


def main():
    """python market_calendar.py [year] prints the calendar for a year, so the
    schedule can be eyeballed against the real NYSE notice without importing
    anything that touches the live bot."""
    import sys
    from datetime import datetime
    from zoneinfo import ZoneInfo
    # ET, never the local/container date: this repo runs on a UTC container
    # where the naive date is already tomorrow after about 8pm ET.
    year = (int(sys.argv[1]) if len(sys.argv) > 1
            else datetime.now(ZoneInfo("America/New_York")).year)
    print(f"Market closures {year}")
    for d, name in sorted(holidays(year).items()):
        print(f"  {d} {d:%a}  {name}")
    print(f"Half days {year} (13:00 ET close)")
    for d, reason in sorted(early_closes(year).items()):
        print(f"  {d} {d:%a}  {reason}")


if __name__ == "__main__":
    main()
