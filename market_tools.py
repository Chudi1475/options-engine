"""Hands for the bot's brain: real market-data lookups it can call mid-chat.

So when someone asks "what should I take Monday?" or "what would have worked
Friday?" the brain pulls actual prices and replays the real strategy instead
of saying "I don't have the data." Same engine the live scanner uses, same
honest approximated option pricing (labeled), no made-up levels.
"""

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import config
import scoreboard
from backtest import (CONTRACTS, SLIPPAGE, bs_price, expiry_for, realized_vol,
                      years_to_expiry)
from backtest_new_rules import simulate_new_exits
from data_feed import DataFeed
from positions import PositionBook
from strategy import StrategyConfig, detect_setup, momentum_pct

ET = ZoneInfo("America/New_York")
_cfg = StrategyConfig()
_feed = DataFeed()

# Gold + major forex the bot can give an honest READ on (not options setups —
# we don't trade these as 0DTE options). Each: alias -> (display, yf symbol,
# price decimals). yfinance has no working spot-gold symbol (XAUUSD=X is dead),
# so gold uses COMEX futures GC=F. FX uses Yahoo's =X pairs.
MACRO_SYMBOLS = {
    "gold": ("Gold", "GC=F", 2), "xau": ("Gold", "GC=F", 2),
    "xauusd": ("Gold", "GC=F", 2), "gc": ("Gold", "GC=F", 2),
    "eurusd": ("EUR/USD", "EURUSD=X", 4), "eur": ("EUR/USD", "EURUSD=X", 4),
    "gbpusd": ("GBP/USD", "GBPUSD=X", 4), "gbp": ("GBP/USD", "GBPUSD=X", 4),
    "cable": ("GBP/USD", "GBPUSD=X", 4),
    "usdjpy": ("USD/JPY", "JPY=X", 2), "jpy": ("USD/JPY", "JPY=X", 2),
    "audusd": ("AUD/USD", "AUDUSD=X", 4), "aud": ("AUD/USD", "AUDUSD=X", 4),
    "usdcad": ("USD/CAD", "CAD=X", 4), "cad": ("USD/CAD", "CAD=X", 4),
    "usdchf": ("USD/CHF", "CHF=X", 4), "chf": ("USD/CHF", "CHF=X", 4),
}

# Popular, heavily-traded stocks + ETFs available as READ-ONLY /command lookups
# (e.g. /aapl, /calls nvda, "how's apple"). These are NOT alert tickers — the
# scanner only ever alerts on StrategyConfig.watchlist, so listing them here
# gives quick reads without ever texting anyone about them. {TICKER: (disp, yf, dp)}
LOOKUP_STOCKS = {t: (t, t, 2) for t in (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "NFLX", "AMD", "TSM",
    "PLTR", "COIN", "MSTR", "SMCI", "SOFI", "BABA", "UBER", "DIS", "JPM", "BAC",
    "BA", "INTC", "MU", "LLY", "QQQ", "IWM", "DIA", "GLD", "SLV", "ARKK",
)}

# Indices (and index-like names) need Yahoo's ^ symbols — a bare "SPX" ticker
# doesn't exist on yfinance, which used to make /chart spx say "no recent
# data". alias -> (display, yf symbol, decimals)
INDEX_SYMBOLS = {
    "spx": ("SPX", "^GSPC", 2), "sp500": ("SPX", "^GSPC", 2),
    "spx500": ("SPX", "^GSPC", 2), "us500": ("SPX", "^GSPC", 2),
    "s&p500": ("SPX", "^GSPC", 2), "s&p": ("SPX", "^GSPC", 2),
    "ndx": ("NDX", "^NDX", 2), "nas100": ("NDX", "^NDX", 2),
    "nasdaq100": ("NDX", "^NDX", 2), "us100": ("NDX", "^NDX", 2),
    "vix": ("VIX", "^VIX", 2),
    "us30": ("DOW", "^DJI", 2), "dow30": ("DOW", "^DJI", 2),
    "dji": ("DOW", "^DJI", 2),
    "dxy": ("DXY", "DX-Y.NYB", 2), "dollarindex": ("DXY", "DX-Y.NYB", 2),
    "russell2000": ("RUT", "^RUT", 2), "us2000": ("RUT", "^RUT", 2),
}

# friendly name -> ticker so a chat like "how's nvidia" resolves
STOCK_ALIASES = {
    "tesla": "TSLA", "qualcomm": "QCOM",
    "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA", "amazon": "AMZN",
    "google": "GOOGL", "alphabet": "GOOGL", "meta": "META", "facebook": "META",
    "broadcom": "AVGO", "netflix": "NFLX", "palantir": "PLTR", "coinbase": "COIN",
    "microstrategy": "MSTR", "supermicro": "SMCI", "alibaba": "BABA",
    "uber": "UBER", "disney": "DIS", "jpmorgan": "JPM", "boeing": "BA",
    "intel": "INTC", "micron": "MU", "lilly": "LLY", "nasdaq": "QQQ",
    "russell": "IWM", "dow": "DIA", "silver": "SLV",
}


def _flatten(df):
    if df is not None and hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    return df


def _daily_closes(yf_symbol):
    d1 = _flatten(yf.download(yf_symbol, period="1y", interval="1d",
                              progress=False, auto_adjust=False))
    return d1["Close"]


def _day_5m(yf_symbol, day: date):
    df = _flatten(yf.download(yf_symbol, start=day.isoformat(),
                              end=(day + timedelta(days=1)).isoformat(),
                              interval="5m", progress=False, auto_adjust=False))
    if df is None or df.empty:
        return None
    df.index = df.index.tz_convert(ET)
    return df


def _gate_ok(ticker, direction):
    """Same eligibility the live scanner uses: rounded 70%+ win rate AND
    positive expectancy under our real exits."""
    bt_old = scoreboard.load_report("backtest_results.json") or {}
    bt_new = scoreboard.load_report("backtest_new_rules.json") or {}
    key = f"{ticker}:{direction}"
    old = bt_old.get("per_setup", {}).get(key)
    new = bt_new.get("per_setup", {}).get(key)
    if not old:
        return False, None
    if round(old["win_rate"]) < config.MIN_WINRATE:
        return False, old
    exp = (new or old)["expectancy_pct"]
    return exp > 0, old


def _weighted_pct(legs, entry_prem):
    if not legs or entry_prem <= 0:
        return None
    return round(sum((px / entry_prem - 1) * 100 * (n / CONTRACTS)
                     for _, px, n, _ in legs), 1)


def _resolve_day(date_str):
    """Turn an ISO date / None into a real trading day we have data for."""
    if date_str:
        try:
            return date.fromisoformat(date_str[:10]), None
        except ValueError:
            pass
    spx = _flatten(yf.download("^GSPC", period="5d", interval="5m",
                               progress=False, auto_adjust=False))
    if spx is None or spx.empty:
        return None, "couldn't reach market data right now"
    spx.index = spx.index.tz_convert(ET)
    return max(set(spx.index.date)), None


def analyze_day(ticker="SPX", date_str=None):
    """Replay one day for one ticker through the real strategy: did a setup
    trigger, would we have alerted it, and how would the trade have gone?"""
    ticker = (ticker or "SPX").upper()
    if ticker not in _cfg.watchlist:
        return {"error": f"{ticker} isn't on the watchlist "
                f"({', '.join(_cfg.watchlist)})."}
    day, err = _resolve_day(date_str)
    if err:
        return {"note": err}
    if day > datetime.now(ET).date() or day.weekday() >= 5:  # ET today, not the
        # container's UTC date (which is already tomorrow after ~8pm ET)
        return {"note": f"{day} is not a completed trading day, so there's no "
                "real data to analyze. The market is closed weekends."}
    yfs = _cfg.watchlist[ticker]
    bars = _day_5m(yfs, day)
    if bars is None or bars.empty:
        return {"note": f"No intraday data for {ticker} on {day} (free history "
                "only goes back ~60 days, and weekends/holidays are blank)."}

    o = float(bars["Open"].iloc[0])
    c = float(bars["Close"].iloc[-1])
    hi = float(bars["High"].max())
    lo = float(bars["Low"].min())
    overview = {
        "ticker": ticker, "date": str(day),
        "open": round(o, 2), "close": round(c, 2),
        "high": round(hi, 2), "low": round(lo, 2),
        "day_move_pct": round((c / o - 1) * 100, 2),
    }

    # if the bot ACTUALLY traded this day, report the real tracked numbers,
    # never an estimate — this is the honest answer to "what happened"
    try:
        real = [p for p in PositionBook().positions
                if p.ticker == ticker and p.date == str(day)
                and p.final_pnl_pct is not None]
    except Exception:
        real = []
    if real:
        p = real[0]
        overview["result"] = {
            "actually_traded_live": True,
            "time": datetime.strptime(p.time_et, "%H:%M:%S").strftime(
                "%I:%M %p ET").lstrip("0"),
            "direction": p.direction, "strike": p.strike,
            "entry_price": p.entry_mid, "entry_source": p.entry_source,
            "half_sold_at_pct": p.half_exit["pct"] if p.half_exit else None,
            "final_exit_pct": (p.final_exit or {}).get("pct"),
            "final_reason": (p.final_exit or {}).get("reason"),
            "whole_trade_pct": p.final_pnl_pct,
            "peak_pct": round(p.mfe_pct) if p.mfe_pct is not None else None,
            "worst_pct": round(p.mae_pct) if p.mae_pct is not None else None,
            "note": "REAL tracked numbers from that day, not an estimate.",
        }
        return overview

    try:
        sigma = realized_vol(_daily_closes(yfs), day)
    except Exception:
        sigma = 0.0

    formed = []
    for i in range(len(bars)):
        upto = bars.iloc[: i + 1]
        now = upto.index[-1].to_pydatetime()
        s = detect_setup(ticker, upto, now, _cfg)
        if s and not any(f["direction"] == s.direction for f in formed):
            ok, stats = _gate_ok(ticker, s.direction)
            formed.append({"time": now.strftime("%I:%M %p ET").lstrip("0"),
                           "direction": s.direction, "strike": s.strike,
                           "mom_pct": round(s.mom_pct, 2), "passes_filter": ok,
                           "setup": s, "now": now,
                           "win_rate": round(stats["win_rate"]) if stats else None})

    if not formed:
        overview["result"] = ("No setup triggered in the morning window "
                              "(9:45-10:30 ET). The 15-min momentum never lined "
                              "up the way the strategy needs, so the bot would "
                              "have stayed silent. No text = no trade.")
        return overview

    picks = [f for f in formed if f["passes_filter"]] or formed
    f = picks[0]
    right = "C" if f["direction"] == "call" else "P"
    expiry = expiry_for(ticker, f["now"])
    entry_prem = bs_price(f["setup"].spot, f["strike"],
                          years_to_expiry(f["now"], expiry), sigma, right) * (1 + SLIPPAGE)
    legs = simulate_new_exits(bars, bars.index[bars.index <= f["now"]][-1],
                              entry_prem, f["strike"], right, sigma, expiry, _cfg)
    result = {
        "would_alert": f["passes_filter"],
        "time": f["time"], "direction": f["direction"], "strike": f["strike"],
        "momentum_pct": f["mom_pct"], "win_rate_quoted": f["win_rate"],
        "entry_est_price": round(entry_prem, 2),
        "pricing": "ESTIMATE — approximated option pricing (no free historical "
        "chains). Real 0DTE fills differ, often a lot. Frame as a rough idea.",
        "exit_result_pct": _weighted_pct(legs, entry_prem),
        "exit_steps": [f"{n} of {CONTRACTS} at {round((px/entry_prem-1)*100):+d}% "
                       f"({lbl})" for _, px, n, lbl in legs],
    }
    if not f["passes_filter"]:
        result["why_skipped"] = (f"A {f['direction']} setup formed at {f['time']}, "
                                 "but it doesn't clear the 70%-win-rate + "
                                 "positive-expectancy bar, so the bot would skip "
                                 "it rather than force a weak trade.")
    overview["result"] = result
    return overview


def market_now(ticker="SPX"):
    """Live read on a ticker right now: price, today's move, 15-min momentum,
    and whether a setup is triggering this moment."""
    ticker = (ticker or "SPX").upper()
    if ticker not in _cfg.watchlist:
        return {"error": f"{ticker} isn't on the watchlist."}
    yfs = _cfg.watchlist[ticker]
    now = datetime.now(ET)
    if now.weekday() >= 5 or not (time(9, 30) <= now.time() <= time(16, 0)):
        day, _ = _resolve_day(None)
        return {"note": "The market is closed right now.", "last_session": str(day),
                "tip": "Ask me to analyze the last session, or send a chart and "
                "I'll read it."}
    try:
        bars = _feed.today_bars(yfs, now)
        price = _feed.latest_price(yfs)
    except Exception as e:
        return {"note": f"couldn't pull live data ({e})"}
    if bars is None or bars.empty:
        return {"note": "no completed bars yet today"}
    o = float(bars["Open"].iloc[0])
    mom = momentum_pct(bars, _cfg)
    out = {
        "ticker": ticker, "time": now.strftime("%I:%M %p ET").lstrip("0"),
        "price": round(price or float(bars["Close"].iloc[-1]), 2),
        "day_open": round(o, 2),
        "move_from_open_pct": round(((price or float(bars["Close"].iloc[-1])) / o - 1) * 100, 2),
        "momentum_15min_pct": round(mom, 2) if mom is not None else None,
        "in_entry_window": _cfg.entry_start <= now.time() <= _cfg.entry_end,
        "data": _feed.backend_for(yfs),
    }
    if out["in_entry_window"]:
        s = detect_setup(ticker, bars, now, _cfg)
        if s:
            ok, stats = _gate_ok(ticker, s.direction)
            out["live_setup"] = {
                "direction": s.direction, "strike": s.strike,
                "would_alert": ok,
                "win_rate": round(stats["win_rate"]) if stats else None}
        else:
            out["live_setup"] = None
    return out


def _norm(symbol: str) -> str:
    return (symbol or "").lower().replace("/", "").replace(" ", "").replace("$", "").strip()


# crypto the bot can read (yfinance COIN-USD). alias -> base symbol.
CRYPTO = {
    "btc": "BTC", "bitcoin": "BTC", "eth": "ETH", "ethereum": "ETH",
    "sol": "SOL", "solana": "SOL", "doge": "DOGE", "dogecoin": "DOGE",
    "xrp": "XRP", "ripple": "XRP", "ada": "ADA", "cardano": "ADA",
    "bnb": "BNB", "ltc": "LTC", "litecoin": "LTC", "link": "LINK",
    "avax": "AVAX", "dot": "DOT", "matic": "MATIC", "shib": "SHIB",
    "trx": "TRX", "ton": "TON", "sui": "SUI", "near": "NEAR",
}

_TICKER_RE = re.compile(r"[A-Z]{1,6}([.\-][A-Z]{1,3})?$")


def resolve(symbol):
    """Map ANY user symbol -> (display, yf_symbol, decimals, kind). Handles gold,
    forex, crypto, the curated stock list, and any plausible stock/ETF ticker.
    Returns None if it doesn't even look like a symbol (so /typos stay silent)."""
    raw = (symbol or "").strip()
    key = _norm(raw)
    if not key:
        return None
    if key in MACRO_SYMBOLS:
        disp, yfs, dec = MACRO_SYMBOLS[key]
        return (disp, yfs, dec, "gold" if yfs == "GC=F" else "forex")
    if key in INDEX_SYMBOLS:
        disp, yfs, dec = INDEX_SYMBOLS[key]
        return (disp, yfs, dec, "stock")
    base = CRYPTO.get(key) or (CRYPTO.get(key[:-3]) if key.endswith("usd") else None)
    if base:
        return (f"{base}/USD", f"{base}-USD", 2, "crypto")
    tkr = STOCK_ALIASES.get(key, raw.upper().lstrip("$").strip())
    if tkr in LOOKUP_STOCKS:
        disp, yfs, dec = LOOKUP_STOCKS[tkr]
        return (disp, yfs, dec, "stock")
    if _TICKER_RE.match(tkr):
        return (tkr, tkr, 2, "stock")
    return None


# A 15-min push only counts as a real signal if it CLEARS the symbol's own
# noise: the move must be at least MOM_ATR_MULT times one bar's typical range
# (ATR as a % of price), and at least MOM_FLOOR_PCT in absolute terms. Below
# that it's chop, and the honest answer is "no clean trade, watch <level>".
# This stops a dead/overnight tape from dressing a sub-noise wiggle (e.g. a
# -0.07% blip) up as a SELL with a noise-tight stop.
MOM_ATR_MULT = 1.0
MOM_FLOOR_PCT = 0.08


def _adaptive_dec(price, kind, dec):
    """Crypto spans BTC (~$100k) to SHIB (~$0.00002); a flat 2 dp rounds the
    cheap coins to 0.0 (false price) and gives $1-5 coins like XRP/ADA stops too
    coarse to scalp. Pick decimals from the price magnitude for crypto;
    everything else keeps its declared precision."""
    if kind != "crypto" or price is None:
        return dec
    if price >= 1000:
        return 2
    if price >= 1:
        return 4
    if price >= 0.01:
        return 5
    return 8


def _nan_none(x, dec):
    """Round to dec, but turn NaN/None into a real None (so 'nan' never reaches
    a user or a JSON dump)."""
    if x is None or pd.isna(x):
        return None
    return round(float(x), dec)


def _atr(bars, n=14):
    """Average True Range over the last n bars — our volatility unit for sizing
    the stop. Real numbers only; None when there aren't enough bars."""
    if bars is None or bars.empty:
        return None
    hlc = bars[["High", "Low", "Close"]].dropna()
    if len(hlc) < 3:
        return None
    prev_close = hlc["Close"].shift(1)
    tr = pd.concat([hlc["High"] - hlc["Low"],
                    (hlc["High"] - prev_close).abs(),
                    (hlc["Low"] - prev_close).abs()], axis=1).max(axis=1)
    val = float(tr.tail(n).mean())
    return val if val > 0 and not pd.isna(val) else None


def _signal_symbol(disp, kind):
    """The symbol a trader actually places the order on: gold -> XAUUSD,
    EUR/USD -> EURUSD, BTC/USD -> BTCUSD, a stock -> its ticker."""
    if kind == "gold":
        return "XAUUSD"
    if kind in ("forex", "crypto"):
        return disp.replace("/", "")
    return disp


def plan_levels(price, bias, atr, hi, lo, dec, kind):
    """A concrete trade plan built ONLY from real numbers: ATR sizes the risk,
    recent structure (session high/low) anchors the stop when it sits a sane
    distance away, and a fixed 2R sets the target. Returns None when there's no
    clean directional lean (chop = no trade) or no volatility estimate. Never
    invents a level."""
    if not bias or bias == "neutral" or not price or not atr or atr <= 0:
        return None
    bull = bias.startswith("bull")
    weak = bias.endswith("weak")
    pad = atr * 0.15  # sit the stop just PAST the level, not exactly on it
    if bull:
        struct = lo
        use_struct = struct is not None and 0.3 * atr <= (price - struct) <= 1.5 * atr
        stop = (struct - pad) if use_struct else (price - atr)
        direction = "BUY"
    else:
        struct = hi
        use_struct = struct is not None and 0.3 * atr <= (struct - price) <= 1.5 * atr
        stop = (struct + pad) if use_struct else (price + atr)
        direction = "SELL"
    # Derive EVERYTHING from the rounded entry + rounded stop, so the displayed
    # risk and the 1R/2R targets always reconcile ("entry +/- 2 * risk_shown"
    # exactly equals TP2 on the card, no penny drift). Validate the displayed
    # values: a stop a fraction of a tick away rounds to risk 0.00, and a SELL on
    # a cheap, volatile name could push the 2R target to/under 0. Either is a
    # nonsense ticket, so bail to None and fall through to the honest
    # "no clean trade, watch <level>" read.
    e = round(price, dec)
    s = round(stop, dec)
    rk = round(abs(e - s), dec)
    if rk <= 0 or s == e:
        return None
    t1 = round(e + rk, dec) if bull else round(e - rk, dec)
    t2 = round(e + 2 * rk, dec) if bull else round(e - 2 * rk, dec)
    if t1 <= 0 or t2 <= 0:
        return None
    return {
        "direction": direction,
        "entry": e, "stop": s,
        "target1": t1, "target": t2,
        "risk": rk, "reward": round(2 * rk, dec),
        "rr": 2.0,
        "weak": weak,
        "structure": bool(use_struct),
    }


def _do_read(disp, yfs, dec, kind, source):
    """Shared read engine for any symbol: price, day move, 15-min momentum,
    recent high/low, 20-day trend, a momentum/trend BIAS, an ATR-sized trade
    PLAN, plus high-impact news. Real numbers only (yfinance, ~15-min delayed)."""
    tried = [yfs]
    try:
        d1 = _flatten(yf.download(yfs, period="1mo", interval="1d",
                                  progress=False, auto_adjust=False))
        m5 = _flatten(yf.download(yfs, period="2d", interval="5m",
                                  progress=False, auto_adjust=False))
    except Exception as e:
        return {"note": f"couldn't pull {disp} data right now ({e})."}
    if (d1 is None or d1.empty) and kind == "stock":
        # Cheap salvage before giving up: many indices only exist on Yahoo
        # with a ^ prefix (RUT, FTSE, N225...), and a 6-letter all-alpha
        # symbol like EURJPY is a Yahoo "=X" forex pair. Each variant costs
        # one daily download; the 5m frame is only fetched for the winner.
        fallbacks = []
        if not yfs.startswith("^"):
            fallbacks.append(("^" + yfs, kind, dec))
        if len(yfs) == 6 and yfs.isalpha():
            fallbacks.append((yfs + "=X", "forex",
                              2 if yfs.upper().endswith("JPY") else 4))
        for alt, alt_kind, alt_dec in fallbacks:
            tried.append(alt)
            try:
                alt_d1 = _flatten(yf.download(alt, period="1mo", interval="1d",
                                              progress=False, auto_adjust=False))
                if alt_d1 is None or alt_d1.empty:
                    continue
                alt_m5 = _flatten(yf.download(alt, period="2d", interval="5m",
                                              progress=False, auto_adjust=False))
            except Exception:
                continue
            yfs, kind, dec = alt, alt_kind, alt_dec
            d1, m5 = alt_d1, alt_m5
            break
    if d1 is None or d1.empty:
        return {"note": f"no recent data for {disp} right now "
                f"(tried {', '.join(tried)}). If that's a typo, check /help "
                "for formats: stock/ETF tickers (AAPL, QQQ), indices (spx, "
                "ndx, us30, vix), forex pairs (eurusd, eurjpy), gold, and "
                "crypto (btc, eth, sol)."}

    closes_d = d1["Close"].dropna()
    have_5m = m5 is not None and not m5.empty and not m5["Close"].dropna().empty
    # normalize the intraday index to ET. yfinance hands back UTC for the 24h
    # markets (forex/gold/crypto), so without this the "last session" filter
    # below splits on the wrong calendar day and hi/lo cover a sliver of bars.
    if have_5m and getattr(m5.index, "tz", None) is not None:
        try:
            m5.index = m5.index.tz_convert(ET)
        except (TypeError, ValueError):
            pass
    price = float(m5["Close"].dropna().iloc[-1]) if have_5m else float(closes_d.iloc[-1])
    prior_close = float(closes_d.iloc[-2]) if len(closes_d) >= 2 else None

    # how old is the last bar? Used to tell a LIVE read from a stale one (market
    # closed / weekend), so we never dress a last-session close up as a live
    # entry. yfinance is ~15 min delayed, so a generous 90-min window keeps a
    # normal live read from being flagged while a closed market (bars hours old)
    # always is. 24/7 crypto is never stale by this rule.
    asof = None
    stale = not have_5m  # no intraday at all -> the daily fallback IS stale
    if have_5m:
        try:
            last_ts = m5.index[-1].to_pydatetime()
            asof = last_ts.strftime("%a %I:%M %p ET").replace(" 0", " ")
            stale = (datetime.now(ET) - last_ts) > timedelta(minutes=90)
        except (AttributeError, ValueError, TypeError):
            stale = False  # can't tell -> don't falsely flag a live read

    # crypto precision depends on the live price (SHIB needs 8 dp, BTC needs 2)
    dec = _adaptive_dec(price, kind, dec)

    mom15 = None
    if have_5m:
        c = m5["Close"].dropna()
        if len(c) >= 4:  # 15 min = 3 bars back
            mom15 = round((price / float(c.iloc[-4]) - 1) * 100, 2)

    # recent session high/low for structure. Prefer the last ET day of 5m bars;
    # fall back to the last DAILY bar when intraday is empty (market closed /
    # weekend) so the level fields aren't null when someone chats after hours.
    hi = lo = None
    day_bars = None
    if have_5m:
        try:
            last_day = max(m5.index.date)
            day_bars = m5[[d == last_day for d in m5.index.date]]
        except Exception:
            day_bars = m5
        if day_bars is not None and not day_bars.empty:
            hi = _nan_none(day_bars["High"].max(), dec)
            lo = _nan_none(day_bars["Low"].min(), dec)
    if hi is None or lo is None:  # intraday gap -> use the last completed day
        try:
            hi = _nan_none(d1["High"].dropna().iloc[-1], dec)
            lo = _nan_none(d1["Low"].dropna().iloc[-1], dec)
        except (KeyError, IndexError):
            pass

    # ATR (volatility unit for the stop): the 5m timeframe they actually scalp
    # when the market's open, else the daily range.
    atr = _atr(day_bars if (day_bars is not None and not day_bars.empty) else m5)
    if atr is None:
        atr = _atr(d1)

    # a true 20-day average needs 20 closes; with fewer, don't dress a short
    # mean up as "the 20-day".
    sma20 = float(closes_d.tail(20).mean()) if len(closes_d) >= 20 else None
    above = sma20 is not None and price > sma20

    # momentum-continuation bias (our highest-WR posture): take a 15-min push
    # that agrees with the 20-day trend; flat momentum = chop = wait. The
    # "-weak" tags mark a push that fights the trend (lower conviction). The
    # trigger is volatility-normalized: the push must clear the symbol's own
    # noise (MOM_ATR_MULT * ATR%), not a flat threshold, so a quiet tape stays
    # chop instead of firing a fake signal.
    bias = "neutral"
    if mom15 is not None and sma20 is not None and atr and price:
        mom_min = max(MOM_FLOOR_PCT, MOM_ATR_MULT * (atr / price * 100))
        if mom15 >= mom_min:
            bias = "bullish" if above else "bullish-weak"
        elif mom15 <= -mom_min:
            bias = "bearish" if not above else "bearish-weak"

    plan = plan_levels(round(price, dec), bias, atr, hi, lo, dec, kind)

    # event awareness: forex uses both pair currencies; everything else watches
    # USD high-impact prints (Fed/CPI/NFP move stocks, gold and crypto too).
    event_ccy, events = ["USD"], []
    try:
        import risk_gate
        if kind == "forex":
            event_ccy = risk_gate.MACRO_CCY.get(yfs, ["USD"])
        events = risk_gate.upcoming_events(event_ccy, within_hours=24)
    except Exception:
        events = []
    event_warning = None
    blackout = False
    if events:
        nxt = events[0]
        m = nxt["mins_away"]
        tleft = f"{m}m" if m < 60 else f"{m // 60}h{m % 60:02d}m"
        blackout = m <= 30  # too close to a high-impact print to enter
        event_warning = (f"⚠️ {nxt['title']} ({nxt['currency']}) in {tleft} "
                         f"({nxt['when']}), high impact. Expect a sharp move; "
                         + ("wait until it's out and the dust settles."
                            if blackout else "it can wait until the dust settles."))

    # Don't show a plan as a live entry when the price is stale (market closed)
    # or a high-impact event is imminent. macro_line routes a null plan to the
    # honest read_card ("no clean trade right now, watch <level>").
    if stale or blackout:
        plan = None

    # Fair Value Gap conviction (ICT / smart-money grade). We never trade on an
    # FVG alone; a graded, displacement-backed FVG that confirms the momentum
    # plan RAISES conviction (and gives a CE-based entry/stop/target), a weak or
    # missing one leaves it on momentum alone. bias feeds FVG grading so a gap
    # with the trend scores higher.
    fvg_info, conviction = None, None
    try:
        import fvg as _fvg
        fbars = day_bars if (day_bars is not None and not day_bars.empty) \
            else (m5 if have_5m else None)
        if fbars is not None and not fbars.empty:
            direction = plan["direction"] if plan else None
            conf = _fvg.confirming_fvg(fbars, direction, price, atr, bias)
            allf = _fvg.find_fvgs(fbars, atr, bias)
            actionable = [f for f in allf if f["state"] != "filled"]
            # the strongest few to draw, best grade first
            actionable.sort(key=lambda f: (f["score"], f["i"]), reverse=True)
            fvg_info = {"confirming": conf, "recent_unfilled": actionable[:6]}
            if plan and conf and conf["grade"] in ("A", "B"):
                conviction = "high"
            elif plan:
                conviction = "medium"
            elif any(f["strong"] for f in actionable):
                conviction = "watch"
    except Exception:
        fvg_info, conviction = None, None

    return {
        "instrument": disp, "ticker": _signal_symbol(disp, kind),
        "symbol": yfs, "kind": kind, "decimals": dec,
        "fvg": fvg_info, "conviction": conviction,
        "price": round(price, dec), "asof": asof, "stale": stale,
        "prior_close": round(prior_close, dec) if prior_close is not None else None,
        "day_move_pct": round((price / prior_close - 1) * 100, 2) if prior_close else None,
        "momentum_15min_pct": mom15,
        "recent_session_high": hi, "recent_session_low": lo,
        "atr": round(atr, dec) if atr is not None else None,
        "twentyday_avg": round(sma20, dec) if sma20 is not None else None,
        "vs_20day_avg": (None if sma20 is None else "above" if above else "below"),
        "bias": bias,
        "plan": plan, "blackout": blackout,
        "event_warning": event_warning,
        "upcoming_events": events[:4],
        "source": source,
        "disclaimer": "A ready trade PLAN is in the 'plan' field (direction, entry, "
                "stop, TP1 at 1R, TP2 at 2R) built from these REAL numbers. State it "
                "plainly. If 'plan' is null there's no clean trade: it's chop, the "
                "market's closed (see 'stale'/'asof'), or a high-impact event is "
                "imminent (see 'blackout'/event_warning). Say so and give the trigger "
                "that would make a trade. Lead with any event_warning; quote only "
                "these levels, never invent one or a win rate; end with 'Your call.' "
                "The 'conviction' field ('high' when a graded, displacement-backed "
                "Fair Value Gap in 'fvg.confirming' confirms the plan, 'medium' when "
                "the plan stands on momentum alone) tells you how hard to lean. When "
                "high, name it like an ICT trader: quote fvg.confirming.grade and "
                "label (BISI=bullish, SIBI=bearish), its zone (bottom to top), its CE "
                "(consequent encroachment, the 50% entry), whether it is unmitigated "
                "or inverted (IFVG), and whether it sits in premium/discount. Give the "
                "FVG ticket from fvg.confirming.ticket (entry_ce, stop beyond the far "
                "edge, target_liquidity) as the smart-money refinement alongside the "
                "momentum plan. A marked-up FVG chart is auto-sent right after your "
                "text. Never invent an FVG or its levels; only cite what 'fvg' returned.",
    }


def read_any(symbol):
    """Universal read for ANY symbol: stock, ETF, forex, gold, or crypto."""
    info = resolve(symbol)
    if not info:
        return {"error": f"'{symbol}' doesn't look like a symbol I can read. Try a "
                "stock/ETF ticker, gold, a forex pair, or a coin (BTC, ETH, SOL...)."}
    disp, yfs, dec, kind = info
    src = "yfinance, ~15-min delayed" + (" (gold = COMEX futures GC=F)" if yfs == "GC=F" else "")
    return _do_read(disp, yfs, dec, kind, src)


def known_symbol(symbol):
    """Cheap membership check (no network) for the bare /<symbol> command guard.
    Delegates to resolve() so the guard and the real resolver can never disagree."""
    return resolve(symbol) is not None


# back-compat aliases (older callers / tests)
any_read = read_any
macro_read = read_any
stock_read = read_any
