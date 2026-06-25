"""Hands for the bot's brain: real market-data lookups it can call mid-chat.

So when someone asks "what should I take Monday?" or "what would have worked
Friday?" the brain pulls actual prices and replays the real strategy instead
of saying "I don't have the data." Same engine the live scanner uses, same
honest approximated option pricing (labeled), no made-up levels.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

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
    if day > date.today() or day.weekday() >= 5:
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


def macro_read(symbol="gold"):
    """Read-only context on GOLD or a major FOREX pair: price, day move, 15-min
    momentum, recent high/low, and trend vs the 20-day average. Honest real
    numbers only (yfinance, ~15-min delayed). NOT one of our options setups and
    NOT financial advice — it's market context the brain turns into an honest
    read. Forex/gold trade nearly 24h, so there's no 'market closed' gate."""
    key = (symbol or "").lower().replace("/", "").replace(" ", "").replace("$", "").strip()
    info = MACRO_SYMBOLS.get(key)
    if not info:
        return {"error": f"'{symbol}' isn't one I read yet. I cover gold and the "
                "major FX pairs: EUR/USD, GBP/USD, USD/JPY, AUD/USD, USD/CAD, USD/CHF."}
    disp, yfs, dec = info
    try:
        d1 = _flatten(yf.download(yfs, period="1mo", interval="1d",
                                  progress=False, auto_adjust=False))
        m5 = _flatten(yf.download(yfs, period="2d", interval="5m",
                                  progress=False, auto_adjust=False))
    except Exception as e:
        return {"note": f"couldn't pull {disp} data right now ({e})."}
    if d1 is None or d1.empty:
        return {"note": f"no recent data for {disp} right now."}

    closes_d = d1["Close"].dropna()
    have_5m = m5 is not None and not m5.empty and not m5["Close"].dropna().empty
    price = float(m5["Close"].dropna().iloc[-1]) if have_5m else float(closes_d.iloc[-1])
    prior_close = float(closes_d.iloc[-2]) if len(closes_d) >= 2 else None

    mom15 = None
    if have_5m:
        c = m5["Close"].dropna()
        if len(c) >= 4:  # 15 min = 3 bars back
            mom15 = round((price / float(c.iloc[-4]) - 1) * 100, 2)

    hi = lo = None
    if have_5m:
        try:
            last_day = max(m5.index.date)
            day_bars = m5[[d == last_day for d in m5.index.date]]
        except Exception:
            day_bars = m5
        if not day_bars.empty:
            hi = round(float(day_bars["High"].max()), dec)
            lo = round(float(day_bars["Low"].min()), dec)

    sma20 = float(closes_d.tail(20).mean()) if len(closes_d) >= 5 else None

    # event awareness: gold + forex move hardest around scheduled news, so pull
    # the high-impact releases for THIS instrument's currencies (reuses the same
    # cached ForexFactory feed the risk gate uses).
    events, event_warning = [], None
    try:
        import risk_gate
        events = risk_gate.upcoming_events(
            risk_gate.MACRO_CCY.get(yfs, ["USD"]), within_hours=24)
    except Exception:
        events = []
    if events:
        nxt = events[0]
        m = nxt["mins_away"]
        tleft = f"{m}m" if m < 60 else f"{m // 60}h{m % 60:02d}m"
        event_warning = (f"⚠️ {nxt['title']} ({nxt['currency']}) in {tleft} "
                         f"({nxt['when']}), high impact. Expect a sharp move; "
                         "it can wait until the dust settles.")

    return {
        "instrument": disp, "symbol": yfs,
        "price": round(price, dec),
        "prior_close": round(prior_close, dec) if prior_close is not None else None,
        "day_move_pct": round((price / prior_close - 1) * 100, 2) if prior_close else None,
        "momentum_15min_pct": mom15,
        "recent_session_high": hi, "recent_session_low": lo,
        "twentyday_avg": round(sma20, dec) if sma20 else None,
        "vs_20day_avg": (None if not sma20 else "above" if price > sma20 else "below"),
        "event_warning": event_warning,
        "upcoming_events": events[:4],
        "source": "yfinance, ~15-min delayed (gold = COMEX futures GC=F)",
        "note": "Read-only market context, NOT a setup from our strategy and NOT "
                "financial advice. Give an honest read, flag any event_warning, "
                "and end with 'Your call.'",
    }
