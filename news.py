"""News layer — what could make SPX or QCOM flip today?

Three free sources, no API keys needed:
1. EARNINGS DATES (yfinance). An option that lives through an earnings
   report is a different bet entirely (binary gap + IV crush), so alerts
   whose option life contains an earnings date are SKIPPED and the morning
   report says why.
2. RSS HEADLINES from major outlets — Yahoo Finance (per ticker), CNBC top
   news, MarketWatch top stories — scanned for high-impact keywords (war,
   tariffs, bans, downgrades, fraud, guidance cuts, chip export rules...).
   Hits show up on the morning report and as a warning line on cards.
3. Scheduled releases + VIX + gap live in risk_gate.py (FOMC/CPI/PPI/NFP).

Honesty: keyword matching is crude on purpose — it catches the big obvious
stuff and always shows WHICH headline triggered, so a human can judge it.
With ANTHROPIC_API_KEY set, risk_gate.py adds a real web-search news check
on top of this.
"""

import re
import time as time_mod
import xml.etree.ElementTree as ETree
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

ET = ZoneInfo("America/New_York")


def _today():
    """Today in ET. The deploy container runs UTC, so _today() there is
    already tomorrow after ~8pm ET and would drop a same-day earnings date."""
    return datetime.now(ET).date()

MARKET_FEEDS = [
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
]
TICKER_FEED = ("Yahoo Finance",
               "https://feeds.finance.yahoo.com/rss/2.0/headline"
               "?s={sym}&region=US&lang=en-US")

# words that historically mean "today is not a normal day" for a ticker
# or the whole market. Case-insensitive. Keep blunt — false positives just
# add a warning line, false negatives are the expensive kind.
HOT_WORDS = re.compile(
    r"\bwar\b|invasion|missile|nuclear|air ?strike|escalat|attack"
    r"|tariff|sanction|export (?:ban|curb|control|rule)|chip ban"
    r"|halts? trading|trading halt|plunge|crash|default|downgrade"
    r"|fraud|SEC charge|investigat|probe|subpoena|lawsuit"
    r"|cuts? (?:guidance|outlook|forecast)|guidance cut|misses estimates"
    r"|bankrupt|recall|CEO (?:resign|fired|steps down)|data breach"
    r"|emergency (?:meeting|rate)|circuit breaker",
    re.IGNORECASE)

# 30-minute in-memory cache so the loop never hammers the feeds
_cache = {}
CACHE_SECONDS = 1800


def _fetch_titles(url: str) -> list:
    r = requests.get(url, timeout=10,
                     headers={"User-Agent": "Mozilla/5.0 (options-engine)"})
    r.raise_for_status()
    root = ETree.fromstring(r.content)
    return [(it.findtext("title") or "").strip()
            for it in root.iter("item")][:25]


def _cached(key, fn, ttl=CACHE_SECONDS):
    hit = _cache.get(key)
    if hit and time_mod.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception:
        return hit[1] if hit else []  # stale beats blind, blind beats crash
    _cache[key] = (time_mod.time(), val)
    return val


def hot_headlines(ticker=None, ttl=CACHE_SECONDS) -> list:
    """Flagged (outlet, title) pairs — per-ticker if given, else market-wide.
    Pass a small ttl for intraday breaking-news watching."""
    if ticker:
        outlet, url = TICKER_FEED
        titles = _cached(f"t:{ticker}",
                         lambda: _fetch_titles(url.format(sym=ticker)), ttl)
        feeds = [(outlet, titles)]
    else:
        feeds = [(name, _cached(f"m:{name}", lambda u=url: _fetch_titles(u), ttl))
                 for name, url in MARKET_FEEDS]
    out = []
    for outlet, titles in feeds:
        for t in titles:
            if t and HOT_WORDS.search(t):
                out.append((outlet, t))
    return out


def all_hot(watchlist: dict, ttl=CACHE_SECONDS) -> list:
    """Market-wide + per-stock-ticker hot headlines, deduped."""
    out, seen = [], set()
    for outlet, title in (hot_headlines(ttl=ttl)
                          + [h for tk in watchlist if tk != "SPX"
                             for h in hot_headlines(tk, ttl=ttl)]):
        if title not in seen:
            seen.add(title)
            out.append((outlet, title))
    return out


def _cached2(key, fn, ttl=CACHE_SECONDS):
    """Like _cached but also returns fetched_ok=False when the live fetch raised
    and we fell back to stale/empty. Shares the same cache keys as _cached, so a
    healthy fetch here warms the regular path too. A fresh cache hit counts ok."""
    hit = _cache.get(key)
    if hit and time_mod.time() - hit[0] < ttl:
        return hit[1], True
    try:
        val = fn()
    except Exception:
        return (hit[1] if hit else []), False
    _cache[key] = (time_mod.time(), val)
    return val, True


def all_hot_healthy(watchlist: dict, ttl=CACHE_SECONDS):
    """Same hot (outlet, title) list as all_hot, PLUS all_ok=False if ANY feed's
    live fetch failed (fell back to stale/empty). The breaking-news seeder uses
    this so the day is only marked seeded once every feed has truly fetched —
    otherwise a feed that recovers later replays its pre-startup headlines as
    false BREAKING alerts."""
    feeds = []
    for name, url in MARKET_FEEDS:
        titles, ok = _cached2(f"m:{name}", lambda u=url: _fetch_titles(u), ttl)
        feeds.append((name, titles, ok))
    outlet, url = TICKER_FEED
    for tk in watchlist:
        if tk == "SPX":
            continue
        titles, ok = _cached2(f"t:{tk}",
                              lambda s=tk: _fetch_titles(url.format(sym=s)), ttl)
        feeds.append((outlet, titles, ok))
    out, seen, all_ok = [], set(), True
    for outlet, titles, ok in feeds:
        all_ok = all_ok and ok
        for t in titles:
            if t and t not in seen and HOT_WORDS.search(t):
                seen.add(t)
                out.append((outlet, t))
    return out, all_ok


def next_earnings(ticker: str):
    """Next scheduled earnings date for a stock, or None (indexes have none)."""
    if ticker.startswith("^") or ticker == "SPX":
        return None

    def fetch():
        t = yf.Ticker(ticker)
        dates = []
        try:
            cal = t.calendar  # dict-like in current yfinance
            raw = cal.get("Earnings Date") if hasattr(cal, "get") else None
            if raw:
                dates = [d if isinstance(d, date) else d.date() for d in raw]
        except Exception:
            pass
        if not dates:
            try:
                df = t.get_earnings_dates(limit=8)
                dates = [d.date() for d in df.index]
            except Exception:
                return []
        today = _today()
        future = sorted(d for d in dates if d >= today)
        return future[:1]

    res = _cached(f"e:{ticker}", fetch)
    return res[0] if res else None


def earnings_inside(ticker: str, expiry: date):
    """(blocked, earnings_date): does an earnings report land inside this
    option's life? If yes, the trade is a coin flip on the report — skip."""
    e = next_earnings(ticker)
    if e is not None and _today() <= e <= expiry:
        return True, e
    return False, e


def morning_lines(watchlist: dict) -> list:
    """Extra lines for the morning report: earnings radar + hot headlines."""
    lines = []
    for ticker in watchlist:
        if ticker == "SPX":
            continue
        e = next_earnings(ticker)
        if e is not None and (e - _today()).days <= 7:
            when = "TODAY" if e == _today() else e.strftime("%a %m/%d")
            lines.append(f"📅 {ticker} earnings {when} — alerts whose option "
                         "lives through it get skipped.")
    flagged = hot_headlines()
    for ticker in watchlist:
        if ticker != "SPX":
            flagged += hot_headlines(ticker)
    seen = set()
    for outlet, title in flagged:
        if title in seen:
            continue
        seen.add(title)
        if len(seen) > 3:
            break
        lines.append(f"📰 {outlet}: {title}")
    if flagged:
        lines.append("(Headlines above tripped the hot-word scan — read "
                     "them yourself before sizing up.)")
    return lines


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for tk in ("QCOM", "TSLA"):
        print(f"{tk} next earnings: {next_earnings(tk)}")
    print("market hot headlines:", *hot_headlines() or ["none"], sep="\n  ")
    for tk in ("QCOM", "TSLA"):
        print(f"{tk} hot headlines:", *hot_headlines(tk) or ["none"], sep="\n  ")
