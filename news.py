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

# The market-wide feeds (CNBC top news, MarketWatch top stories) carry every
# lawsuit, recall and opinion piece on the wire. Only the macro shocks that
# move the whole tape count there; the company-level words (lawsuit, recall,
# downgrade, guidance cut...) stay in HOT_WORDS for the per-TICKER feeds,
# where a hit is about a name we actually trade. 8/21 fired six BREAKING
# texts, three of them one Canada-tariff story told three ways and one a
# Medicare opinion column that said "bankrupt".
MACRO_HOT = re.compile(
    r"war|invasion|missile|nuclear|air ?strike|escalat"
    r"|tariff|sanction|export (?:ban|curb|control|rule)|chip ban"
    r"|halts? trading|trading halt|circuit breaker|plunge|crash"
    r"|emergency (?:meeting|rate)|rate (?:cut|hike|decision)|fed"
    r"|government shutdown|debt ceiling|sovereign default",
    re.IGNORECASE)

# ETFs and indexes report no earnings; asking Yahoo for a calendar they do
# not have is a guaranteed 404 on every fetch.
NO_EARNINGS = {"SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "ARKK", "SPX",
               "VIX", "VOO", "VTI", "TLT", "XLF", "XLE", "XLK", "SMH"}

# 30-minute in-memory cache so the loop never hammers the feeds
_cache = {}
CACHE_SECONDS = 1800

_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "as",
         "at", "by", "is", "are", "was", "were", "it", "its", "this", "that",
         "with", "from", "says", "said", "say", "here", "what", "could",
         "would", "will", "after", "before", "over", "into", "about", "than",
         "amid", "new", "how", "why", "who", "his", "her", "their", "they",
         "has", "have", "had", "but", "not", "out", "more", "still", "just"}


def topic_key(title: str) -> frozenset:
    """The words that carry a headline's topic: lowercase, 4+ letters, no
    stop words, digits kept. Two headlines about the same event share most of
    these even when the outlets phrase them differently."""
    words = re.findall(r"[a-z0-9][a-z0-9'$.-]*", (title or "").lower())
    out = set()
    for w in words:
        w = w.strip("'.-")
        if len(w) < 4 or w in _STOP:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]   # tariffs/tariff, recalls/recall: one topic word
        out.add(w)
    return frozenset(out)


def same_story(title: str, sent_titles, min_shared: int = 3,
               min_overlap: float = 0.4) -> bool:
    """True when `title` retells a story already in `sent_titles`: at least
    `min_shared` topic words in common AND they cover at least `min_overlap`
    of the shorter headline's topic words. Guards the breaking-news thread
    against texting one tariff story three times as three outlets pick it
    up, while two different stories that merely share a name still both
    fire."""
    key = topic_key(title)
    if not key:
        return False
    for prev in sent_titles or []:
        pk = topic_key(prev)
        if not pk:
            continue
        shared = len(key & pk)
        if shared >= min_shared and shared / min(len(key), len(pk)) >= min_overlap:
            return True
    return False


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
    hot = HOT_WORDS if ticker else MACRO_HOT
    for outlet, titles in feeds:
        for t in titles:
            if t and hot.search(t):
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
        # the two market-wide feeds are the first two entries (MARKET_FEEDS);
        # everything after them is a per-ticker feed
        hot = HOT_WORDS if outlet == TICKER_FEED[0] else MACRO_HOT
        for t in titles:
            if t and t not in seen and hot.search(t):
                seen.add(t)
                out.append((outlet, t))
    return out, all_ok


def next_earnings(ticker: str):
    """Next scheduled earnings date for a stock, or None (indexes and ETFs
    have none)."""
    if ticker.startswith("^") or ticker.upper() in NO_EARNINGS:
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
            lines.append(f"📅 {ticker} earnings {when}. Alerts whose option "
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
        lines.append("(Headlines above tripped the hot-word scan. Read "
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
