"""Walk-forward backtest v2 of the /chart signal — honest filter sweep.

Same engine honesty as backtest_chart.py: real production logic imported from
market_tools + fvg, decisions at each 5m bar use ONLY bars up to that moment,
conservative tie rule (a bar spanning both stop and target = LOSS), positions
never span sessions, stop fills assumed at the stop (no positive slippage).

What v2 adds:
- The signal stream is evaluated at EVERY bar (not skipped while a v1 position
  was open), and every config replays the stream with its OWN position lock +
  cooldown, so each config's trade sequence is causal for that config.
- Base requirement baked into the stream (loss lesson #2): a confirming FVG of
  grade A or B. Momentum-only "medium conviction" cards are never issued.
- 12 management styles precomputed per signal:
    entry: market (signal close) | limit at the FVG CE (TTL 12 bars, engine
           busy while the order works, conservative fill = CE exactly, fill
           bar checked stop-first)
    stop:  production plan stop | just beyond the FVG far edge (0.1 ATR buffer)
    tp:    v1 two-stage (TP1=1R bank half, stop->entry, TP2=2R, tie=loss)
           | 1R single target, all out | 0.7R single target, all out
- Filter grid (loss lessons #1 and #3): trend-aligned only, grade-A only,
  extension filter (block when the directional run from session open >= 1.0 or
  1.5 ATR), active-session hours, min FVG gap 0.3 ATR, drop USD/JPY + EUR/USD,
  max 2 trades/symbol/day, risk cap 1.1 ATR (kills the decimal-rounding
  wide-stop artifacts).

ANTI-OVERFIT RULE: configs are ranked ONLY on the first half of calendar days
(Wilson 95% lower bound of win rate, then avg R). The second half is untouched
by selection; walking down the in-sample ranking, the first config whose
out-of-sample half holds >= 60% wr with >= 100 trades and positive avg R is
the winner, the second is the runner-up. The number of OOS looks taken during
that walk is reported. If nothing holds, the report says so plainly.
"""

import json
import math
import os
import sys
import time
from itertools import product

import numpy as np
import pandas as pd
import yfinance as yf

import fvg
import market_tools as mt

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

PERIOD_5M = "55d"
SKIP_FIRST_BARS = 4
COOLDOWN_BARS = 6
CE_TTL_BARS = 12          # a CE limit order works for 1 hour, then cancels
DROP_SYMBOLS = {"USD/JPY", "EUR/USD"}

# ---- the grid --------------------------------------------------------------
TREND_OPTS = [False, True]          # True = block "-weak" bias (against 20d SMA)
GRADE_OPTS = ["AB", "A"]
ENTRY_OPTS = ["market", "ce"]
STOP_OPTS = ["plan", "fvg"]
TP_OPTS = ["tp1tp2", "1r", "0.7r"]
RUN_OPTS = [None, 1.0, 1.5]         # block when run-from-open >= X ATR
SESS_OPTS = [False, True]
GAP_OPTS = [False, True]            # True = FVG size >= 0.3 ATR
DROP_OPTS = [False, True]           # True = drop USD/JPY + EUR/USD
MAX2_OPTS = [False, True]
CAP_OPTS = [None, 1.1]              # max risk in ATR

MGMTS = list(product(ENTRY_OPTS, STOP_OPTS, TP_OPTS))
MGMT_IDX = {m: k for k, m in enumerate(MGMTS)}

IS_GATE = {"min_trades": 100, "min_wr": 60.0}
OOS_GATE = {"min_trades": 100, "min_wr": 60.0}


def log(msg):
    print(msg, flush=True)


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
    """date -> 20-day SMA of daily closes STRICTLY BEFORE that date."""
    closes = d1["Close"].dropna()
    dates = [ts.date() for ts in closes.index]
    out = {}
    vals = list(closes.values)
    for k in range(len(vals)):
        if k >= 20:
            out[dates[k]] = sum(vals[k - 20:k]) / 20.0
    return out


def atr_series(H, L, C, n=14):
    """atr_at[i] == mt._atr(day_bars[:i+1]) for every i, vectorized.

    mt._atr on the day-so-far slice computes true ranges INSIDE the slice
    (first bar's TR = H-L, no prev close) then means the last 14. Because the
    slice always starts at bar 0 of the day, the TR array is the same for
    every i, so a rolling mean reproduces it exactly. Verified against
    mt._atr in __main__ before the run."""
    m = len(H)
    tr = np.empty(m)
    tr[0] = H[0] - L[0]
    if m > 1:
        pc = C[:-1]
        tr[1:] = np.maximum(H[1:] - L[1:],
                            np.maximum(np.abs(H[1:] - pc), np.abs(L[1:] - pc)))
    csum = np.concatenate(([0.0], np.cumsum(tr)))
    out = np.full(m, np.nan)
    for i in range(m):
        if i + 1 < 3:            # _atr needs >= 3 bars
            continue
        lo_k = max(0, i - n + 1)
        v = (csum[i + 1] - csum[lo_k]) / (i + 1 - lo_k)
        if v > 0 and not np.isnan(v):
            out[i] = v
    return out


def sess_ok(kind, hour):
    """Active-session filter. Stocks/indices already trade RTH-only on the 5m
    feed, so they always pass; fx/gold restricted to London+NY morning
    (02:00-10:59 ET); crypto to US active hours (08:00-16:59 ET)."""
    if kind == "stock":
        return True
    if kind in ("forex", "gold"):
        return 2 <= hour < 11
    return 8 <= hour < 17


# ---- signal stream (built once, every bar, no lookahead) -------------------

def build_stream():
    signals = []       # one dict per A/B-FVG-confirmed plan
    bars = []          # gid -> (H, L, C) numpy arrays for that symbol-day
    all_days = set()
    gid = -1
    atr_checked = False
    for disp, yfs, dec0, kind in SYMBOLS:
        t0 = time.time()
        m5, d1 = download(yfs)
        if m5 is None:
            log(f"  !! no data for {yfs}, skipping")
            continue
        smas = sma20_by_date(d1)
        n_sig = 0
        for day, day_df in m5.groupby(m5.index.date):
            n = len(day_df)
            if n < SKIP_FIRST_BARS + 2:
                continue
            all_days.add(str(day))
            H = day_df["High"].astype(float).values
            L = day_df["Low"].astype(float).values
            C = day_df["Close"].astype(float).values
            O = day_df["Open"].astype(float).values
            gid += 1
            bars.append((H, L, C))
            sess_open = float(O[0])
            sma20 = smas.get(day)
            atr_at = atr_series(H, L, C)
            if not atr_checked:
                # prove the vectorized ATR equals production mt._atr on the
                # day-so-far slice, bar by bar, before trusting it
                for i_chk in range(2, min(n, 40)):
                    ref = mt._atr(day_df.iloc[:i_chk + 1])
                    got = atr_at[i_chk]
                    ok = (ref is None and np.isnan(got)) or \
                        (ref is not None and not np.isnan(got)
                         and abs(ref - got) < 1e-9)
                    if not ok:
                        raise AssertionError(
                            f"atr_series mismatch at bar {i_chk}: "
                            f"{ref} vs {got}")
                atr_checked = True
                log("  atr_series verified against mt._atr, bar by bar")
            run_hi = np.maximum.accumulate(H)
            run_lo = np.minimum.accumulate(L)
            for i in range(SKIP_FIRST_BARS, n - 1):
                price = float(C[i])
                dec = mt._adaptive_dec(price, kind, dec0)
                atr = float(atr_at[i]) if not np.isnan(atr_at[i]) else None
                if atr is None:
                    continue
                mom15 = None
                if i >= 3:
                    mom15 = round((price / float(C[i - 3]) - 1) * 100, 2)
                above = sma20 is not None and price > sma20
                bias = "neutral"
                if mom15 is not None and sma20 is not None and atr and price:
                    mom_min = max(mt.MOM_FLOOR_PCT,
                                  mt.MOM_ATR_MULT * (atr / price * 100))
                    if mom15 >= mom_min:
                        bias = "bullish" if above else "bullish-weak"
                    elif mom15 <= -mom_min:
                        bias = "bearish" if not above else "bearish-weak"
                if bias == "neutral":
                    continue
                hi = mt._nan_none(float(run_hi[i]), dec)
                lo = mt._nan_none(float(run_lo[i]), dec)
                plan = mt.plan_levels(round(price, dec), bias, atr, hi, lo,
                                      dec, kind)
                if plan is None:
                    continue
                try:
                    conf = fvg.confirming_fvg(day_df.iloc[:i + 1],
                                              plan["direction"],
                                              price, atr, bias)
                except Exception:
                    conf = None
                if not conf or conf["grade"] not in ("A", "B"):
                    continue     # loss lesson #2: no momentum-only cards
                buy = plan["direction"] == "BUY"
                run = (price - sess_open) if buy else (sess_open - price)
                ts = day_df.index[i]
                signals.append({
                    "gid": gid, "i": i, "n": n,
                    "sym": disp, "kind": kind, "day": str(day),
                    "time": str(ts), "hour": int(ts.hour),
                    "dir": plan["direction"], "weak": bool(plan["weak"]),
                    "dec": dec, "atr": float(atr), "price": price,
                    "entry_mkt": plan["entry"], "stop_plan": plan["stop"],
                    "grade": conf["grade"],
                    "fvg_top": float(conf["top"]),
                    "fvg_bottom": float(conf["bottom"]),
                    "ce": float(conf["ce"]),
                    "gap_atr": float(conf["size"]) / float(atr),
                    "run_atr": run / float(atr),
                    "sess_ok": sess_ok(kind, int(ts.hour)),
                })
                n_sig += 1
        log(f"  {disp:<8} {n_sig:>5} A/B-FVG signals "
            f"({time.time() - t0:.0f}s)")
    return signals, bars, sorted(all_days)


# ---- per-signal management outcomes ----------------------------------------

INVALID = {"valid": False, "filled": False, "exit_abs": 0, "r": 0.0,
           "tag": "invalid", "risk_atr": 0.0}


def simulate_mgmt(sig, H, L, C, entry_style, stop_style, tp_style):
    """Outcome of taking this signal under one management style, assuming the
    engine is free. Conservative everywhere: stop checked before target on
    every bar including the fill bar."""
    i, n = sig["i"], sig["n"]
    buy = sig["dir"] == "BUY"
    dec, atr = sig["dec"], sig["atr"]

    # ---- entry
    if entry_style == "market":
        entry = sig["entry_mkt"]
        filled, start = True, i + 1
    else:
        entry = round(sig["ce"], dec)
        if (buy and entry >= sig["price"]) or \
           (not buy and entry <= sig["price"]):
            # CE is at/through the market: order is marketable now. Fill at
            # the CE price (conservative: never better than the limit).
            filled, start = True, i + 1
        else:
            filled, start = False, None
            last_j = min(i + CE_TTL_BARS, n - 1)
            for j in range(i + 1, last_j + 1):
                if (buy and L[j] <= entry) or (not buy and H[j] >= entry):
                    filled, start = True, j
                    break

    # ---- stop
    if stop_style == "plan":
        stop = sig["stop_plan"]
    else:
        stop = round(sig["fvg_bottom"] - 0.1 * atr, dec) if buy \
            else round(sig["fvg_top"] + 0.1 * atr, dec)
    if (buy and stop >= entry) or (not buy and stop <= entry):
        return INVALID
    risk = round(abs(entry - stop), dec)
    if risk <= 0:
        return INVALID
    risk_atr = risk / atr

    if not filled:
        return {"valid": True, "filled": False,
                "exit_abs": min(i + CE_TTL_BARS, n - 1),
                "r": 0.0, "tag": "cancel", "risk_atr": risk_atr}

    def done(j, r, tag):
        return {"valid": True, "filled": True, "exit_abs": j,
                "r": float(r), "tag": tag, "risk_atr": risk_atr}

    if tp_style == "tp1tp2":
        t1 = round(entry + risk, dec) if buy else round(entry - risk, dec)
        t2 = round(entry + 2 * risk, dec) if buy else round(entry - 2 * risk, dec)
        banked = False
        for j in range(start, n):
            hi, lo = H[j], L[j]
            if not banked:
                if (lo <= stop if buy else hi >= stop):
                    return done(j, -1.0, "stop")
                if (hi >= t1 if buy else lo <= t1):
                    banked = True
                    be = lo <= entry if buy else hi >= entry
                    tp2 = hi >= t2 if buy else lo <= t2
                    if tp2 and not be:
                        return done(j, 1.5, "tp2")
                    if be:
                        return done(j, 0.5, "breakeven")
            else:
                if (lo <= entry if buy else hi >= entry):
                    return done(j, 0.5, "breakeven")
                if (hi >= t2 if buy else lo <= t2):
                    return done(j, 1.5, "tp2")
        last = float(C[n - 1])
        move_r = ((last - entry) if buy else (entry - last)) / risk
        if banked:
            return done(n - 1, 0.5 + 0.5 * move_r, "eod_runner")
        return done(n - 1, move_r, "eod")

    k = 1.0 if tp_style == "1r" else 0.7
    tgt = round(entry + k * risk, dec) if buy else round(entry - k * risk, dec)
    if tgt == entry:
        return INVALID
    r_win = abs(tgt - entry) / risk
    for j in range(start, n):
        hi, lo = H[j], L[j]
        if (lo <= stop if buy else hi >= stop):
            return done(j, -1.0, "stop")
        if (hi >= tgt if buy else lo <= tgt):
            return done(j, r_win, "tp")
    last = float(C[n - 1])
    move_r = ((last - entry) if buy else (entry - last)) / risk
    return done(n - 1, move_r, "eod")


# ---- config sweep -----------------------------------------------------------

def wilson_lb(w, n, z=1.96):
    if n == 0:
        return 0.0
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - m


def stats(wins, n, tot_r):
    if n == 0:
        return {"trades": 0, "wins": 0, "win_rate_pct": None, "avg_r": None,
                "total_r": 0.0}
    return {"trades": n, "wins": wins,
            "win_rate_pct": round(100.0 * wins / n, 1),
            "avg_r": round(tot_r / n, 3), "total_r": round(tot_r, 2)}


def cfg_dict(cfg):
    trend, grade, entry, stop, tp, run, sess, gap, drop, max2, cap = cfg
    return {"trend_aligned_only": trend, "fvg_grade": grade,
            "entry": entry, "stop": stop, "tp": tp,
            "max_run_from_open_atr": run, "session_filter": sess,
            "min_gap_0.3_atr": gap, "drop_jpy_eurusd": drop,
            "max_2_per_symbol_day": max2, "max_risk_atr": cap}


def cfg_words(cfg):
    trend, grade, entry, stop, tp, run, sess, gap, drop, max2, cap = cfg
    bits = []
    bits.append("grade A FVG only" if grade == "A" else "grade A/B FVG")
    bits.append("trend-aligned only" if trend else "weak bias allowed")
    bits.append("entry at FVG CE limit (TTL 1h)" if entry == "ce"
                else "market entry at signal close")
    bits.append("stop beyond FVG far edge +0.1 ATR" if stop == "fvg"
                else "production plan stop")
    bits.append({"tp1tp2": "TP1=1R bank half + runner to 2R",
                 "1r": "TP 1R all-out", "0.7r": "TP 0.7R all-out"}[tp])
    bits.append(f"skip if run-from-open >= {run} ATR" if run is not None
                else "no extension filter")
    bits.append("active-session hours only" if sess else "all hours")
    if gap:
        bits.append("FVG gap >= 0.3 ATR")
    if drop:
        bits.append("USD/JPY + EUR/USD dropped")
    if max2:
        bits.append("max 2 trades/symbol/day")
    if cap is not None:
        bits.append(f"risk <= {cap} ATR")
    return "; ".join(bits)


def sweep(signals, outcomes, oos_flag):
    ns = len(signals)
    gid = np.array([s["gid"] for s in signals], dtype=np.int64)
    i_arr = np.array([s["i"] for s in signals], dtype=np.int64)
    weak = np.array([s["weak"] for s in signals], dtype=bool)
    gradeA = np.array([s["grade"] == "A" for s in signals], dtype=bool)
    run_arr = np.array([s["run_atr"] for s in signals], dtype=np.float64)
    sess_arr = np.array([s["sess_ok"] for s in signals], dtype=bool)
    gap_arr = np.array([s["gap_atr"] >= 0.3 for s in signals], dtype=bool)
    drop_arr = np.array([s["sym"] in DROP_SYMBOLS for s in signals], dtype=bool)
    oos_arr = np.asarray(oos_flag, dtype=bool)

    valid_m, filled_m, r_m, exit_m, ratr_m = [], [], [], [], []
    for m in range(len(MGMTS)):
        valid_m.append(np.array([outcomes[k][m]["valid"] for k in range(ns)],
                                dtype=bool))
        filled_m.append(np.array([outcomes[k][m]["filled"] for k in range(ns)],
                                 dtype=bool))
        r_m.append(np.array([outcomes[k][m]["r"] for k in range(ns)],
                            dtype=np.float64))
        exit_m.append(np.array([outcomes[k][m]["exit_abs"] for k in range(ns)],
                               dtype=np.int64))
        ratr_m.append(np.array([outcomes[k][m]["risk_atr"] for k in range(ns)],
                               dtype=np.float64))

    grid = list(product(TREND_OPTS, GRADE_OPTS, ENTRY_OPTS, STOP_OPTS, TP_OPTS,
                        RUN_OPTS, SESS_OPTS, GAP_OPTS, DROP_OPTS, MAX2_OPTS,
                        CAP_OPTS))
    log(f"sweeping {len(grid)} configs over {ns} signals ...")
    rows = []
    t0 = time.time()
    for ci, cfg in enumerate(grid):
        trend, grade, entry, stop, tp, run, sess, gap, drop, max2, cap = cfg
        m = MGMT_IDX[(entry, stop, tp)]
        mask = valid_m[m].copy()
        if trend:
            mask &= ~weak
        if grade == "A":
            mask &= gradeA
        if run is not None:
            mask &= run_arr < run
        if sess:
            mask &= sess_arr
        if gap:
            mask &= gap_arr
        if drop:
            mask &= ~drop_arr
        if cap is not None:
            mask &= ratr_m[m] <= cap
        idx = np.nonzero(mask)[0]
        fil, rr, ex, ii, gg, oo = (filled_m[m], r_m[m], exit_m[m],
                                   i_arr, gid, oos_arr)
        cur_g, free_at, taken = -1, -1, 0
        is_w = is_n = oos_w = oos_n = 0
        is_r = oos_r = 0.0
        for k in idx:
            g = gg[k]
            if g != cur_g:
                cur_g, free_at, taken = g, -1, 0
            if ii[k] < free_at:
                continue
            if max2 and taken >= 2:
                continue
            if not fil[k]:
                free_at = ex[k] + 1        # order cancelled, no cooldown
                continue
            r = rr[k]
            if oo[k]:
                oos_n += 1
                oos_r += r
                if r > 0:
                    oos_w += 1
            else:
                is_n += 1
                is_r += r
                if r > 0:
                    is_w += 1
            taken += 1
            free_at = ex[k] + 1 + COOLDOWN_BARS
        rows.append({"cfg": cfg,
                     "is": stats(is_w, is_n, is_r),
                     "oos": stats(oos_w, oos_n, oos_r),
                     "is_wilson": round(100 * wilson_lb(is_w, is_n), 2)})
        if (ci + 1) % 800 == 0:
            log(f"  {ci + 1}/{len(grid)} configs ({time.time() - t0:.0f}s)")
    log(f"sweep done in {time.time() - t0:.0f}s")
    return rows


def replay_config(cfg, signals, outcomes, oos_flag):
    """Re-run one config collecting full trade records (for loss replays)."""
    trend, grade, entry, stop, tp, run, sess, gap, drop, max2, cap = cfg
    m = MGMT_IDX[(entry, stop, tp)]
    trades = []
    cur_g, free_at, taken = -1, -1, 0
    for k, s in enumerate(signals):
        o = outcomes[k][m]
        if not o["valid"]:
            continue
        if trend and s["weak"]:
            continue
        if grade == "A" and s["grade"] != "A":
            continue
        if run is not None and s["run_atr"] >= run:
            continue
        if sess and not s["sess_ok"]:
            continue
        if gap and s["gap_atr"] < 0.3:
            continue
        if drop and s["sym"] in DROP_SYMBOLS:
            continue
        if cap is not None and o["risk_atr"] > cap:
            continue
        g = s["gid"]
        if g != cur_g:
            cur_g, free_at, taken = g, -1, 0
        if s["i"] < free_at:
            continue
        if max2 and taken >= 2:
            continue
        if not o["filled"]:
            free_at = o["exit_abs"] + 1
            continue
        trades.append({"symbol": s["sym"], "day": s["day"], "time": s["time"],
                       "direction": s["dir"], "grade": s["grade"],
                       "hour": s["hour"], "run_atr": round(s["run_atr"], 2),
                       "gap_atr": round(s["gap_atr"], 2),
                       "risk_atr": round(o["risk_atr"], 2),
                       "r": round(o["r"], 3), "exit": o["tag"],
                       "half": "oos" if oos_flag[k] else "is"})
        taken += 1
        free_at = o["exit_abs"] + 1 + COOLDOWN_BARS
    return trades


def loss_replay(trades):
    """What actually killed the losers of a config, from its replayed trades."""
    losses = [t for t in trades if t["r"] <= 0]
    wins = [t for t in trades if t["r"] > 0]
    out = {"n_trades": len(trades), "n_losses": len(losses)}
    if not losses:
        return out
    by_sym, by_hour, by_exit = {}, {}, {}
    for t in losses:
        by_sym[t["symbol"]] = by_sym.get(t["symbol"], 0) + 1
        by_hour[t["hour"]] = by_hour.get(t["hour"], 0) + 1
        by_exit[t["exit"]] = by_exit.get(t["exit"], 0) + 1
    out["losses_by_symbol"] = dict(sorted(by_sym.items(),
                                          key=lambda kv: -kv[1]))
    out["losses_by_exit"] = dict(sorted(by_exit.items(),
                                        key=lambda kv: -kv[1]))
    out["losses_by_hour"] = {str(h): by_hour[h] for h in sorted(by_hour)}
    out["avg_run_atr_losses"] = round(
        sum(t["run_atr"] for t in losses) / len(losses), 2)
    if wins:
        out["avg_run_atr_wins"] = round(
            sum(t["run_atr"] for t in wins) / len(wins), 2)
    out["avg_risk_atr_losses"] = round(
        sum(t["risk_atr"] for t in losses) / len(losses), 2)
    return out


def main():
    t_start = time.time()
    log("building signal stream (every bar, no lookahead, A/B FVG only) ...")
    signals, bars, all_days = build_stream()
    log(f"{len(signals)} signals across {len(all_days)} days")
    if not signals:
        log("no signals — aborting")
        sys.exit(1)

    mid = len(all_days) // 2
    is_days = set(all_days[:mid])
    split_date = all_days[mid]
    oos_flag = [s["day"] not in is_days for s in signals]
    n_oos_sig = sum(oos_flag)
    log(f"split: {len(is_days)} in-sample days | "
        f"{len(all_days) - mid} out-of-sample days (from {split_date}); "
        f"{len(signals) - n_oos_sig} IS / {n_oos_sig} OOS signals")

    log("precomputing 12 management outcomes per signal ...")
    t0 = time.time()
    outcomes = []
    for s in signals:
        H, L, C = bars[s["gid"]]
        outcomes.append([simulate_mgmt(s, H, L, C, *m) for m in MGMTS])
    log(f"  done in {time.time() - t0:.0f}s")

    rows = sweep(signals, outcomes, oos_flag)

    # baseline = v1's high-conviction behaviour inside this engine
    base_cfg = (False, "AB", "market", "plan", "tp1tp2",
                None, False, False, False, False, None)
    baseline = next(r for r in rows if r["cfg"] == base_cfg)
    log(f"\nbaseline (v1 high-conviction, no filters): "
        f"IS {baseline['is']['trades']} tr {baseline['is']['win_rate_pct']}% "
        f"{baseline['is']['avg_r']} avgR | "
        f"OOS {baseline['oos']['trades']} tr {baseline['oos']['win_rate_pct']}% "
        f"{baseline['oos']['avg_r']} avgR")

    # ---- selection: rank on IS only, then walk down checking the OOS gate
    is_pass = [r for r in rows
               if r["is"]["trades"] >= IS_GATE["min_trades"]
               and r["is"]["win_rate_pct"] is not None
               and r["is"]["win_rate_pct"] >= IS_GATE["min_wr"]
               and r["is"]["avg_r"] is not None and r["is"]["avg_r"] > 0]
    is_pass.sort(key=lambda r: (-r["is_wilson"], -(r["is"]["avg_r"] or 0)))
    log(f"{len(is_pass)} configs pass the in-sample gate "
        f"(>= {IS_GATE['min_trades']} trades, >= {IS_GATE['min_wr']}% wr, "
        f"avg R > 0)")

    best = runner = None
    oos_peeks = 0
    for r in is_pass:
        oos_peeks += 1
        o = r["oos"]
        holds = (o["trades"] >= OOS_GATE["min_trades"]
                 and o["win_rate_pct"] is not None
                 and o["win_rate_pct"] >= OOS_GATE["min_wr"]
                 and o["avg_r"] is not None and o["avg_r"] > 0)
        if holds:
            if best is None:
                best = r
            elif runner is None:
                runner = r
                break

    honest_note = ""
    if best is None:
        # nothing holds 60% OOS: report the top IS config's OOS truth
        honest_note = ("NO config held >= 60% win rate with >= 100 trades and "
                       "positive avg R on the untouched second half. ")
        if is_pass:
            best = is_pass[0]
            runner = is_pass[1] if len(is_pass) > 1 else None
            honest_note += (f"Best in-sample config's honest OOS numbers are "
                            f"reported instead ({oos_peeks} configs were "
                            f"checked against the gate).")
        elif rows:
            rows_sorted = sorted(
                rows, key=lambda r: (-(r["is_wilson"]),
                                     -(r["is"]["avg_r"] or -9)))
            best = rows_sorted[0]
            runner = rows_sorted[1]
            honest_note += ("Nothing even passed the in-sample gate; the "
                            "least-bad config is reported.")
    else:
        honest_note = (f"Selection used ONLY the first half; the OOS gate was "
                       f"checked on {oos_peeks} IS-ranked configs before "
                       f"best+runner-up were fixed. Multiple related configs "
                       f"in a {len(rows)}-point grid still share information, "
                       f"so treat the OOS number as an upper bound of true "
                       f"edge.")
        if runner is None:
            honest_note += " Only ONE config held the OOS gate; no runner-up."

    best_trades = replay_config(best["cfg"], signals, outcomes, oos_flag)
    best_losses = loss_replay(best_trades)
    runner_trades = (replay_config(runner["cfg"], signals, outcomes, oos_flag)
                     if runner else [])

    top20 = sorted(rows, key=lambda r: (-r["is_wilson"],
                                        -(r["is"]["avg_r"] or -9)))[:20]
    report = {
        "method": {
            "engine": "walk-forward, production market_tools+fvg logic, "
                      "no lookahead, conservative tie=loss, stop fills at "
                      "stop, positions never span sessions",
            "stream": "every 5m bar evaluated; card only exists with a "
                      "grade A/B confirming FVG (loss lesson: momentum-only "
                      "cards removed)",
            "ce_limit": f"CE limit entries work {CE_TTL_BARS} bars then "
                        "cancel; engine busy while the order works; fill "
                        "bar checked stop-first",
            "split": f"first {mid} calendar days = in-sample, remaining "
                     f"{len(all_days) - mid} days (from {split_date}) = "
                     "out-of-sample, untouched by selection",
            "selection": "rank IS-passing configs by Wilson 95% lower bound "
                         "of win rate, walk down checking the OOS gate "
                         "(>=60% wr, >=100 trades, avg R > 0)",
            "cooldown_bars": COOLDOWN_BARS,
            "skip_first_bars": SKIP_FIRST_BARS,
        },
        "data": {"symbols": [s[0] for s in SYMBOLS],
                 "days_total": len(all_days), "is_days": mid,
                 "oos_days": len(all_days) - mid, "split_date": split_date,
                 "n_signals": len(signals)},
        "grid_size": len(rows),
        "baseline_v1_high_conviction": {"config": cfg_dict(base_cfg),
                                        "is": baseline["is"],
                                        "oos": baseline["oos"]},
        "selection": {"is_gate": IS_GATE, "oos_gate": OOS_GATE,
                      "n_is_passing": len(is_pass), "oos_peeks": oos_peeks},
        "best": {"config": cfg_dict(best["cfg"]),
                 "rules": cfg_words(best["cfg"]),
                 "is": best["is"], "oos": best["oos"],
                 "loss_replay": best_losses,
                 "trades": best_trades},
        "runner_up": ({"config": cfg_dict(runner["cfg"]),
                       "rules": cfg_words(runner["cfg"]),
                       "is": runner["is"], "oos": runner["oos"],
                       "loss_replay": loss_replay(runner_trades)}
                      if runner else None),
        "top20_by_in_sample": [{"config": cfg_dict(r["cfg"]),
                                "is": r["is"], "oos": r["oos"],
                                "is_wilson_lb": r["is_wilson"]}
                               for r in top20],
        "honest_note": honest_note,
    }
    os.makedirs("reports", exist_ok=True)
    path = os.path.join("reports", "chart_backtest_v2.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    log(f"\nwrote {path}")

    def show(name, r):
        if not r:
            log(f"{name}: none")
            return
        log(f"{name}: {cfg_words(r['cfg'])}")
        log(f"   IS : {r['is']['trades']:>4} trades  "
            f"{r['is']['win_rate_pct']}% wr  {r['is']['avg_r']} avgR  "
            f"{r['is']['total_r']} totR")
        log(f"   OOS: {r['oos']['trades']:>4} trades  "
            f"{r['oos']['win_rate_pct']}% wr  {r['oos']['avg_r']} avgR  "
            f"{r['oos']['total_r']} totR")

    log("")
    show("BEST", best)
    log("")
    show("RUNNER-UP", runner)
    log(f"\nhonest note: {honest_note}")
    log(f"\ntotal runtime {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()
