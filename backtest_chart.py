"""Walk-forward backtest of the /chart signal using the REAL production logic.

Imports market_tools (_atr, plan_levels, _adaptive_dec, _flatten, MOM_* gates)
and fvg (confirming_fvg) instead of reimplementing them. At every 5m bar it
rebuilds the read exactly the way _do_read does, using ONLY bars up to that
moment (no lookahead), takes the plan if one fires, and simulates the card's
management: TP1 = 1R bank half + stop to entry, TP2 = 2R, hard stop = plan
stop, close at end of session. Conservative tie rule: a bar that spans both
the stop and the target counts as a LOSS.
"""

import json
import os

import pandas as pd
import yfinance as yf

import fvg
import market_tools as mt

# display, yf symbol, decimals, kind — mirrors MACRO_SYMBOLS / INDEX_SYMBOLS /
# resolve() in market_tools.py
SYMBOLS = [
    ("Gold",    "GC=F",     2, "gold"),
    ("EUR/USD", "EURUSD=X", 4, "forex"),
    ("GBP/USD", "GBPUSD=X", 4, "forex"),
    ("USD/JPY", "JPY=X",    2, "forex"),
    ("BTC/USD", "BTC-USD",  2, "crypto"),
    ("SPX",     "^GSPC",    2, "stock"),
    ("TSLA",    "TSLA",     2, "stock"),
    ("SPY",     "SPY",      2, "stock"),
]

PERIOD_5M = "55d"       # yfinance caps 5m history at 60 days
SKIP_FIRST_BARS = 4     # skip the first 4 bars of each session
COOLDOWN_BARS = 6       # no re-entry for 6 bars after an exit


def download(yfs):
    m5 = mt._flatten(yf.download(yfs, period=PERIOD_5M, interval="5m",
                                 progress=False, auto_adjust=False))
    d1 = mt._flatten(yf.download(yfs, period="6mo", interval="1d",
                                 progress=False, auto_adjust=False))
    if m5 is None or m5.empty or d1 is None or d1.empty:
        return None, None
    if getattr(m5.index, "tz", None) is not None:
        m5.index = m5.index.tz_convert(mt.ET)
    m5 = m5.dropna(subset=["Open", "High", "Low", "Close"])
    return m5, d1


def sma20_by_date(d1):
    """date -> 20-day SMA of daily closes STRICTLY BEFORE that date
    (the intraday walk never sees the current day's daily close)."""
    closes = d1["Close"].dropna()
    dates = [ts.date() for ts in closes.index]
    out = {}
    vals = list(closes.values)
    for k in range(len(vals)):
        if k >= 20:
            out[dates[k]] = sum(vals[k - 20:k]) / 20.0
    return out


def read_at_bar(day_bars_so_far, closes_so_far, sma20, dec, kind):
    """Rebuild the _do_read decision at this bar from bars up to now only.
    Returns (plan, conviction, price, atr, bias)."""
    price = float(closes_so_far.iloc[-1])
    dec = mt._adaptive_dec(price, kind, dec)

    # ATR the way production takes it: the current session's bars (its "last
    # ET day" slice); _atr itself uses the last 14 true ranges.
    atr = mt._atr(day_bars_so_far)
    if atr is None:
        return None, None, price, None, "neutral"

    # 15-min momentum exactly like _do_read: close vs close 3 bars back
    mom15 = None
    if len(closes_so_far) >= 4:
        mom15 = round((price / float(closes_so_far.iloc[-4]) - 1) * 100, 2)

    above = sma20 is not None and price > sma20

    bias = "neutral"
    if mom15 is not None and sma20 is not None and atr and price:
        mom_min = max(mt.MOM_FLOOR_PCT, mt.MOM_ATR_MULT * (atr / price * 100))
        if mom15 >= mom_min:
            bias = "bullish" if above else "bullish-weak"
        elif mom15 <= -mom_min:
            bias = "bearish" if not above else "bearish-weak"

    hi = mt._nan_none(day_bars_so_far["High"].max(), dec)
    lo = mt._nan_none(day_bars_so_far["Low"].min(), dec)

    plan = mt.plan_levels(round(price, dec), bias, atr, hi, lo, dec, kind)
    if plan is None:
        return None, None, price, atr, bias

    # conviction the way _do_read grades it: a graded A/B FVG confirming the
    # plan direction -> high, else the plan stands on momentum alone -> medium
    conviction = "medium"
    try:
        conf = fvg.confirming_fvg(day_bars_so_far, plan["direction"],
                                  price, atr, bias)
        if conf and conf["grade"] in ("A", "B"):
            conviction = "high"
    except Exception:
        pass
    return plan, conviction, price, atr, bias


def simulate(plan, later_bars):
    """Card management on the bars AFTER the signal bar (same session).
    Returns (r_total, exit_tag)."""
    entry, sl = plan["entry"], plan["stop"]
    tp1, tp2 = plan["target1"], plan["target"]
    risk = plan["risk"]
    buy = plan["direction"] == "BUY"
    banked = False  # TP1 hit, half off, stop at entry

    for _, b in later_bars.iterrows():
        hi, lo = float(b["High"]), float(b["Low"])
        if not banked:
            stop_hit = lo <= sl if buy else hi >= sl
            tp1_hit = hi >= tp1 if buy else lo <= tp1
            if stop_hit:            # conservative: stop wins any tie with TP1
                return -1.0, "stop"
            if tp1_hit:
                banked = True       # +0.5R banked, stop moves to entry
                # same bar could also run to TP2 — conservative: the runner's
                # stop (entry) wins a tie, so only take TP2 if the bar did NOT
                # also trade back through entry
                be_hit = lo <= entry if buy else hi >= entry
                tp2_hit = hi >= tp2 if buy else lo <= tp2
                if tp2_hit and not be_hit:
                    return 1.5, "tp2"
                if be_hit:
                    return 0.5, "breakeven"
        else:
            be_hit = lo <= entry if buy else hi >= entry
            tp2_hit = hi >= tp2 if buy else lo <= tp2
            if be_hit:              # conservative: breakeven wins the tie
                return 0.5, "breakeven"
            if tp2_hit:
                return 1.5, "tp2"

    # session over: close the remainder at the last price, pro-rated
    last = float(later_bars["Close"].iloc[-1]) if len(later_bars) else entry
    move_r = ((last - entry) if buy else (entry - last)) / risk
    if banked:
        return 0.5 + 0.5 * move_r, "eod_runner"
    return move_r, "eod"


def run_symbol(disp, yfs, dec, kind):
    m5, d1 = download(yfs)
    if m5 is None:
        print(f"  !! no data for {yfs}, skipping")
        return []
    smas = sma20_by_date(d1)
    trades = []
    dates = sorted(set(m5.index.date))
    for day in dates:
        day_df = m5[[d == day for d in m5.index.date]]
        n = len(day_df)
        if n < SKIP_FIRST_BARS + 2:
            continue
        sma20 = smas.get(day)
        closes = day_df["Close"]
        block_until = -1  # cooldown pointer (bar index within the day)
        i = SKIP_FIRST_BARS
        while i < n - 1:  # need at least one later bar to manage the trade
            if i < block_until:
                i += 1
                continue
            so_far = day_df.iloc[:i + 1]
            plan, conviction, price, atr, bias = read_at_bar(
                so_far, closes.iloc[:i + 1], sma20, dec, kind)
            if plan is None:
                i += 1
                continue
            later = day_df.iloc[i + 1:]
            r, exit_tag = simulate(plan, later)
            # find the exit bar to enforce the position lock + cooldown
            exit_off = len(later)  # eod default
            if exit_tag in ("stop", "tp2", "breakeven"):
                exit_off = _exit_offset(plan, later, exit_tag)
            trades.append({
                "symbol": disp, "yf": yfs, "day": str(day),
                "time": str(day_df.index[i]),
                "direction": plan["direction"], "bias": bias,
                "conviction": conviction,
                "entry": plan["entry"], "stop": plan["stop"],
                "tp1": plan["target1"], "tp2": plan["target"],
                "r": round(r, 3), "exit": exit_tag,
            })
            block_until = i + 1 + exit_off + COOLDOWN_BARS
            i = max(i + 1, block_until)
        # (loop naturally ends the day; positions never span sessions)
    return trades


def _exit_offset(plan, later, exit_tag):
    """Which later-bar closed the trade, replaying the same rules."""
    entry, sl = plan["entry"], plan["stop"]
    tp1, tp2 = plan["target1"], plan["target"]
    buy = plan["direction"] == "BUY"
    banked = False
    for off, (_, b) in enumerate(later.iterrows()):
        hi, lo = float(b["High"]), float(b["Low"])
        if not banked:
            if (lo <= sl if buy else hi >= sl):
                return off + 1
            if (hi >= tp1 if buy else lo <= tp1):
                banked = True
                if (lo <= entry if buy else hi >= entry):
                    return off + 1
                if (hi >= tp2 if buy else lo <= tp2):
                    return off + 1
        else:
            if (lo <= entry if buy else hi >= entry):
                return off + 1
            if (hi >= tp2 if buy else lo <= tp2):
                return off + 1
    return len(later)


def stats(trades):
    n = len(trades)
    if n == 0:
        return {"trades": 0, "wins": 0, "win_rate_pct": None, "avg_r": None,
                "total_r": 0.0}
    wins = sum(1 for t in trades if t["r"] > 0)
    total_r = sum(t["r"] for t in trades)
    return {"trades": n, "wins": wins,
            "win_rate_pct": round(100.0 * wins / n, 1),
            "avg_r": round(total_r / n, 3),
            "total_r": round(total_r, 2)}


def main():
    all_trades = []
    per_symbol = {}
    for disp, yfs, dec, kind in SYMBOLS:
        print(f"running {disp} ({yfs}) ...")
        tr = run_symbol(disp, yfs, dec, kind)
        all_trades.extend(tr)
        hi = [t for t in tr if t["conviction"] == "high"]
        med = [t for t in tr if t["conviction"] == "medium"]
        per_symbol[disp] = {
            "yf_symbol": yfs,
            "all": stats(tr),
            "high_conviction": stats(hi),
            "medium_conviction": stats(med),
            "by_exit": {tag: sum(1 for t in tr if t["exit"] == tag)
                        for tag in ("stop", "tp2", "breakeven",
                                    "eod", "eod_runner")},
        }
        print(f"  {len(tr)} trades ({len(hi)} high conviction)")

    hi_all = [t for t in all_trades if t["conviction"] == "high"]
    med_all = [t for t in all_trades if t["conviction"] == "medium"]
    report = {
        "method": {
            "bars": "5m yfinance, ~55 days",
            "logic": "market_tools plan_levels/_atr/_do_read bias rules + "
                     "fvg.confirming_fvg imported from production",
            "management": "TP1=1R bank half + stop to entry, TP2=2R, "
                          "conservative tie = loss, EOD flat",
            "skip_first_bars": SKIP_FIRST_BARS,
            "cooldown_bars": COOLDOWN_BARS,
        },
        "overall": {
            "all": stats(all_trades),
            "high_conviction": stats(hi_all),
            "medium_conviction": stats(med_all),
        },
        "per_symbol": per_symbol,
        "trades": all_trades,
    }
    os.makedirs("reports", exist_ok=True)
    path = os.path.join("reports", "chart_backtest.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nwrote {path} ({len(all_trades)} trades)\n")

    def row(name, s, s_hi):
        wr = f"{s['win_rate_pct']}%" if s["win_rate_pct"] is not None else "-"
        ar = s["avg_r"] if s["avg_r"] is not None else "-"
        hwr = (f"{s_hi['win_rate_pct']}%"
               if s_hi["win_rate_pct"] is not None else "-")
        har = s_hi["avg_r"] if s_hi["avg_r"] is not None else "-"
        print(f"{name:<10} {s['trades']:>6} {wr:>7} {ar!s:>7} "
              f"| {s_hi['trades']:>5} {hwr:>7} {har!s:>7}")

    print(f"{'symbol':<10} {'trades':>6} {'win%':>7} {'avgR':>7} "
          f"| {'hi-n':>5} {'hi-win%':>7} {'hi-avgR':>7}")
    print("-" * 62)
    for disp, _, _, _ in SYMBOLS:
        ps = per_symbol.get(disp)
        if ps:
            row(disp, ps["all"], ps["high_conviction"])
    print("-" * 62)
    row("TOTAL", stats(all_trades), stats(hi_all))


if __name__ == "__main__":
    main()
