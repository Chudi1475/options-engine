"""Walk-forward backtest v3 of the /chart signal — loss dissection + fix round.

ROUND 3 of the honest grind. Engine honesty identical to backtest_chart_v2.py:
real production logic from market_tools + fvg, decisions at each 5m bar use
ONLY bars up to that moment, conservative tie rule (bar spanning stop and
target = LOSS), stop fills at the stop, positions never span sessions, CE
limit fills never better than the limit, fill bar checked stop-first.

STEP 1 (teach yourself): replay the v2 winner (grade A FVG, CE limit entry,
FVG-edge stop, 0.7R all-out, run<1.0 ATR, gap>=0.3 ATR, max 2/symbol/day) and
dissect EVERY loss: hour, symbol, gap size, displacement strength, premium/
discount of the CE, liquidity sweep before the FVG, distance already run,
day regime (trend vs chop), stop distance, and how close the trade got to the
0.7R target before dying (MFE). Decision tables printed on the IN-SAMPLE half
only; the OOS half stays untouched by rule-building.

STEP 2 (fix and prove): a grid over candidate rules built from those failure
patterns (sweep-required, CE-in-discount/premium, midday drop, session hours,
symbol drops from IS win rates, tighter targets 0.5/0.6R, displacement >= 1.5
or 1.8 ATR, trending-day requirement, unmitigated-FVG-only, gap >= 0.5).

ANTI-OVERFIT GATE (non-negotiable): configs are ranked ONLY on the first half
of calendar days (Wilson 95% lower bound of win rate, then avg R). Walking
down that in-sample ranking, the first config whose untouched second half
holds >= 60% wr with >= 60 trades and avg R > 0 is the winner. The number of
OOS looks taken is reported. If nothing holds, the top IS config's honest OOS
numbers are reported instead. Ties/ambiguity always resolve to LOSS.
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

# ---- the v3 grid (fixed a priori; drop-sets filled from IS win rates) -------
TP_OPTS = ["0.5r", "0.6r", "0.7r"]
RUN_OPTS = [None, 1.0]              # block when run-from-open >= X ATR
# gap options extended after the first two sweeps, both times from IS-only
# tables: sweep1 grid stopped at 0.5; the IS dissection showed gap>=1.0 at
# 78.5% IS vs 57.9% under 0.5 (-> added 0.7/1.0); an IS-only fine-bucket look
# inside the sweep-2 winner family showed gap>=1.1 at 91.1% IS (-> added
# 1.1/1.25). OOS peeks spent by earlier sweeps are counted in PRIOR_OOS_PEEKS.
GAP_OPTS = [0.3, 0.5, 0.7, 1.0, 1.1, 1.25]   # min FVG gap in ATR
PRIOR_OOS_PEEKS = 5                 # 3 in sweep 1 (gap<=0.5) + 2 in sweep 2
SWEEP_OPTS = [False, True]          # require liquidity sweep before the FVG
PD_OPTS = [False, True]             # require CE in discount (BUY)/premium (SELL)
HOUR_OPTS = ["all", "no_midday", "sess", "sess_no_midday"]
DISP_OPTS = [None, 1.5, 1.8]        # displacement body >= X ATR
REGIME_OPTS = [False, True]         # require trending day (efficiency >= 0.5)
STATE_OPTS = [False, True]          # require FVG still unmitigated at signal

MGMTS = TP_OPTS                     # entry=CE limit, stop=FVG edge, fixed (v2 winner)
MGMT_IDX = {m: k for k, m in enumerate(MGMTS)}

IS_GATE = {"min_trades": 60, "min_wr": 60.0}
OOS_GATE = {"min_trades": 60, "min_wr": 60.0}

V2_WINNER = {"tp": "0.7r", "run": 1.0, "gap": 0.3, "sweep": False,
             "pd": False, "hours": "all", "drop": frozenset(),
             "disp": None, "regime": False, "state": False}


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
    closes = d1["Close"].dropna()
    dates = [ts.date() for ts in closes.index]
    out = {}
    vals = list(closes.values)
    for k in range(len(vals)):
        if k >= 20:
            out[dates[k]] = sum(vals[k - 20:k]) / 20.0
    return out


def atr_series(H, L, C, n=14):
    """atr_at[i] == mt._atr(day_bars[:i+1]) for every i (verified in-run)."""
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
        if i + 1 < 3:
            continue
        lo_k = max(0, i - n + 1)
        v = (csum[i + 1] - csum[lo_k]) / (i + 1 - lo_k)
        if v > 0 and not np.isnan(v):
            out[i] = v
    return out


def sess_ok(kind, hour):
    if kind == "stock":
        return True
    if kind in ("forex", "gold"):
        return 2 <= hour < 11
    return 8 <= hour < 17


def swings(H, L):
    """Confirmed 2-bar-fractal swing highs/lows. A swing at j is only knowable
    from bar j+2 onward, which every user of this respects."""
    n = len(H)
    sl, sh = [], []
    for j in range(2, n - 2):
        if L[j] < L[j - 1] and L[j] < L[j - 2] and \
           L[j] < L[j + 1] and L[j] < L[j + 2]:
            sl.append((j, L[j]))
        if H[j] > H[j - 1] and H[j] > H[j - 2] and \
           H[j] > H[j + 1] and H[j] > H[j + 2]:
            sh.append((j, H[j]))
    return sl, sh


def swept_before_fvg(buy, fi, H, L, sl, sh, raid_win=5, max_age=36):
    """True if, in the bars just before/at the displacement candle (fi), price
    raided a PRIOR confirmed swing (below a swing low for buys, above a swing
    high for sells). Causal: the swing at j needs bars j+1, j+2 to confirm, so
    only swings with j <= s-3 count for a raid at bar s."""
    lo_s = max(2, fi - raid_win)
    pts = sl if buy else sh
    for s in range(lo_s, fi + 1):
        for j, lvl in pts:
            if j > s - 3:
                break
            if s - j > max_age:
                continue
            if (buy and L[s] < lvl) or (not buy and H[s] > lvl):
                return True
    return False


# ---- signal stream (built once, every bar, no lookahead) -------------------

def build_stream():
    signals = []
    bars = []
    all_days = set()
    gid = -1
    atr_checked = False
    for disp_name, yfs, dec0, kind in SYMBOLS:
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
                for i_chk in range(2, min(n, 40)):
                    ref = mt._atr(day_df.iloc[:i_chk + 1])
                    got = atr_at[i_chk]
                    ok = (ref is None and np.isnan(got)) or \
                        (ref is not None and not np.isnan(got)
                         and abs(ref - got) < 1e-9)
                    if not ok:
                        raise AssertionError(
                            f"atr_series mismatch at bar {i_chk}: {ref} vs {got}")
                atr_checked = True
                log("  atr_series verified against mt._atr, bar by bar")
            run_hi = np.maximum.accumulate(H)
            run_lo = np.minimum.accumulate(L)
            sl, sh = swings(H, L)
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
                    continue
                buy = plan["direction"] == "BUY"
                run = (price - sess_open) if buy else (sess_open - price)
                ts = day_df.index[i]
                fi = int(conf["i"])
                rng = float(run_hi[i]) - float(run_lo[i])
                eff = abs(price - sess_open) / rng if rng > 0 else 0.0
                pd_ok = (buy and conf["pd_zone"] == "discount") or \
                        (not buy and conf["pd_zone"] == "premium")
                signals.append({
                    "gid": gid, "i": i, "n": n,
                    "sym": disp_name, "kind": kind, "day": str(day),
                    "time": str(ts), "hour": int(ts.hour),
                    "dir": plan["direction"], "weak": bool(plan["weak"]),
                    "dec": dec, "atr": float(atr), "price": price,
                    "grade": conf["grade"],
                    "fvg_top": float(conf["top"]),
                    "fvg_bottom": float(conf["bottom"]),
                    "ce": float(conf["ce"]),
                    "gap_atr": float(conf["size"]) / float(atr),
                    "disp_atr": float(conf["disp"]),
                    "pd_zone": conf["pd_zone"], "pd_ok": bool(pd_ok),
                    "state": conf["state"],
                    "unmit": conf["state"] == "unmitigated",
                    "fvg_age": int(i - fi),
                    "swept": bool(swept_before_fvg(buy, fi, H, L, sl, sh)),
                    "run_atr": run / float(atr),
                    "day_eff": round(eff, 3),
                    "range_atr": round(rng / float(atr), 2),
                    "sess_ok": sess_ok(kind, int(ts.hour)),
                })
                n_sig += 1
        log(f"  {disp_name:<8} {n_sig:>5} A/B-FVG signals "
            f"({time.time() - t0:.0f}s)")
    return signals, bars, sorted(all_days)


# ---- per-signal management outcomes ----------------------------------------

INVALID = {"valid": False, "filled": False, "exit_abs": 0, "r": 0.0,
           "tag": "invalid", "risk_atr": 0.0, "mfe_r": 0.0}


def simulate_mgmt(sig, H, L, C, tp_style):
    """CE limit entry, FVG-edge stop, single all-out target. Conservative:
    stop checked before target on every bar including the fill bar. Also
    tracks MFE (max favorable excursion in R) up to and including exit."""
    i, n = sig["i"], sig["n"]
    buy = sig["dir"] == "BUY"
    dec, atr = sig["dec"], sig["atr"]

    entry = round(sig["ce"], dec)
    if (buy and entry >= sig["price"]) or (not buy and entry <= sig["price"]):
        filled, start = True, i + 1
    else:
        filled, start = False, None
        last_j = min(i + CE_TTL_BARS, n - 1)
        for j in range(i + 1, last_j + 1):
            if (buy and L[j] <= entry) or (not buy and H[j] >= entry):
                filled, start = True, j
                break

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
                "r": 0.0, "tag": "cancel", "risk_atr": risk_atr, "mfe_r": 0.0}

    k = float(tp_style[:-1])
    tgt = round(entry + k * risk, dec) if buy else round(entry - k * risk, dec)
    if tgt == entry:
        return INVALID
    r_win = abs(tgt - entry) / risk

    mfe = 0.0

    def done(j, r, tag):
        return {"valid": True, "filled": True, "exit_abs": j,
                "r": float(r), "tag": tag, "risk_atr": risk_atr,
                "mfe_r": round(float(mfe), 3)}

    for j in range(start, n):
        hi, lo = H[j], L[j]
        fav = ((hi - entry) if buy else (entry - lo)) / risk
        if fav > mfe:
            mfe = fav
        if (lo <= stop if buy else hi >= stop):
            return done(j, -1.0, "stop")
        if (hi >= tgt if buy else lo <= tgt):
            return done(j, r_win, "tp")
    last = float(C[n - 1])
    move_r = ((last - entry) if buy else (entry - last)) / risk
    return done(n - 1, move_r, "eod")


# ---- shared helpers ----------------------------------------------------------

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


def hours_pass(opt, hour, sess):
    if opt == "all":
        return True
    if opt == "no_midday":
        return hour not in (12, 13)
    if opt == "sess":
        return sess
    return sess and hour not in (12, 13)


def cfg_dict(cfg):
    tp, run, gap, sweep, pdreq, hours, drop, dsp, regime, state = cfg
    return {"fvg_grade": "A", "entry": "ce", "stop": "fvg", "tp": tp,
            "max_run_from_open_atr": run, "min_gap_atr": gap,
            "require_liquidity_sweep": sweep,
            "require_ce_discount_premium": pdreq, "hours": hours,
            "drop_symbols": sorted(drop), "min_displacement_atr": dsp,
            "require_trending_day": regime,
            "require_unmitigated_fvg": state,
            "max_2_per_symbol_day": True}


def cfg_words(cfg):
    tp, run, gap, sweep, pdreq, hours, drop, dsp, regime, state = cfg
    bits = ["grade A FVG only", "entry at FVG CE limit (TTL 1h)",
            "stop beyond FVG far edge +0.1 ATR",
            f"TP {tp[:-1]}R all-out"]
    bits.append(f"skip if run-from-open >= {run} ATR" if run is not None
                else "no extension filter")
    bits.append(f"FVG gap >= {gap} ATR")
    if sweep:
        bits.append("liquidity sweep of a prior swing required before the FVG")
    if pdreq:
        bits.append("CE in discount for buys / premium for sells")
    bits.append({"all": "all hours", "no_midday": "12:00-13:59 ET blocked",
                 "sess": "active-session hours only",
                 "sess_no_midday": "active-session hours, midday blocked"}[hours])
    if drop:
        bits.append("dropped: " + ", ".join(sorted(drop)))
    if dsp is not None:
        bits.append(f"displacement body >= {dsp} ATR")
    if regime:
        bits.append("trending day required (efficiency >= 0.5)")
    if state:
        bits.append("FVG still unmitigated at signal")
    bits.append("max 2 trades/symbol/day")
    return "; ".join(bits)


# ---- replay + dissection -----------------------------------------------------

def replay_config(cfg, signals, outcomes, oos_flag):
    tp, run, gap, sweep, pdreq, hours, drop, dsp, regime, state = cfg
    m = MGMT_IDX[tp]
    trades = []
    cur_g, free_at, taken = -1, -1, 0
    for k, s in enumerate(signals):
        o = outcomes[k][m]
        if not o["valid"]:
            continue
        if s["grade"] != "A":
            continue
        if run is not None and s["run_atr"] >= run:
            continue
        if s["gap_atr"] < gap:
            continue
        if sweep and not s["swept"]:
            continue
        if pdreq and not s["pd_ok"]:
            continue
        if not hours_pass(hours, s["hour"], s["sess_ok"]):
            continue
        if s["sym"] in drop:
            continue
        if dsp is not None and s["disp_atr"] < dsp:
            continue
        if regime and s["day_eff"] < 0.5:
            continue
        if state and not s["unmit"]:
            continue
        g = s["gid"]
        if g != cur_g:
            cur_g, free_at, taken = g, -1, 0
        if s["i"] < free_at:
            continue
        if taken >= 2:
            continue
        if not o["filled"]:
            free_at = o["exit_abs"] + 1
            continue
        trades.append({"symbol": s["sym"], "day": s["day"], "time": s["time"],
                       "direction": s["dir"], "hour": s["hour"],
                       "run_atr": round(s["run_atr"], 2),
                       "gap_atr": round(s["gap_atr"], 2),
                       "disp_atr": round(s["disp_atr"], 2),
                       "pd_zone": s["pd_zone"], "pd_ok": s["pd_ok"],
                       "swept": s["swept"], "unmit": s["unmit"],
                       "fvg_age": s["fvg_age"],
                       "day_eff": s["day_eff"], "range_atr": s["range_atr"],
                       "risk_atr": round(o["risk_atr"], 2),
                       "mfe_r": o["mfe_r"],
                       "r": round(o["r"], 3), "exit": o["tag"],
                       "half": "oos" if oos_flag[k] else "is"})
        taken += 1
        free_at = o["exit_abs"] + 1 + COOLDOWN_BARS
    return trades


def bucket_table(trades, name, keyfn):
    rows = {}
    for t in trades:
        b = keyfn(t)
        w, n = rows.get(b, (0, 0))
        rows[b] = (w + (1 if t["r"] > 0 else 0), n + 1)
    log(f"  {name:<34} {'wr%':>6} {'w/n':>9}")
    for b in sorted(rows, key=lambda x: str(x)):
        w, n = rows[b]
        log(f"    {str(b):<32} {100.0 * w / n:6.1f} {w:>4}/{n:<4}")


def dissect(trades, target_r):
    """The teach-yourself step. Tables on the IS half only (rule-building
    data); every individual loss printed for both halves, labeled."""
    is_tr = [t for t in trades if t["half"] == "is"]
    losses = [t for t in trades if t["r"] <= 0]
    log(f"\n===== STEP 1: v2-winner replay, {len(trades)} trades "
        f"({len(is_tr)} IS), {len(losses)} losses =====")
    log(f"\n--- every loss (both halves, labeled) ---")
    log(f"{'half':<4} {'symbol':<8} {'time':<26} {'dir':<4} {'hr':>2} "
        f"{'gap':>5} {'disp':>5} {'pd':<11} {'swp':<3} {'unm':<3} "
        f"{'age':>3} {'run':>6} {'eff':>5} {'rng':>5} {'risk':>5} {'mfe':>6} "
        f"{'pct_tgt':>7}")
    for t in losses:
        pct = 100.0 * t["mfe_r"] / target_r if target_r else 0.0
        log(f"{t['half']:<4} {t['symbol']:<8} {t['time']:<26} "
            f"{t['direction']:<4} {t['hour']:>2} {t['gap_atr']:>5.2f} "
            f"{t['disp_atr']:>5.2f} {t['pd_zone']:<11} "
            f"{'Y' if t['swept'] else '.':<3} {'Y' if t['unmit'] else '.':<3} "
            f"{t['fvg_age']:>3} {t['run_atr']:>6.2f} {t['day_eff']:>5.2f} "
            f"{t['range_atr']:>5.2f} {t['risk_atr']:>5.2f} "
            f"{t['mfe_r']:>6.2f} {pct:>6.0f}%")

    log(f"\n--- IN-SAMPLE decision tables (OOS untouched by rule-building) ---")
    bucket_table(is_tr, "symbol", lambda t: t["symbol"])
    bucket_table(is_tr, "hour (ET)", lambda t: t["hour"])
    bucket_table(is_tr, "gap_atr bucket",
                 lambda t: "<0.5" if t["gap_atr"] < 0.5 else
                           "0.5-1.0" if t["gap_atr"] < 1.0 else ">=1.0")
    bucket_table(is_tr, "displacement bucket",
                 lambda t: "<1.5" if t["disp_atr"] < 1.5 else
                           "1.5-1.8" if t["disp_atr"] < 1.8 else ">=1.8")
    bucket_table(is_tr, "pd location ok (disc buy/prem sell)",
                 lambda t: t["pd_ok"])
    bucket_table(is_tr, "liquidity sweep before FVG", lambda t: t["swept"])
    bucket_table(is_tr, "FVG unmitigated at signal", lambda t: t["unmit"])
    bucket_table(is_tr, "fvg age (bars)",
                 lambda t: "0-2" if t["fvg_age"] <= 2 else
                           "3-6" if t["fvg_age"] <= 6 else ">6")
    bucket_table(is_tr, "run-from-open bucket (ATR)",
                 lambda t: "<-2" if t["run_atr"] < -2 else
                           "-2..0" if t["run_atr"] < 0 else ">=0")
    bucket_table(is_tr, "day efficiency (trend vs chop)",
                 lambda t: "<0.3 chop" if t["day_eff"] < 0.3 else
                           "0.3-0.5" if t["day_eff"] < 0.5 else ">=0.5 trend")
    bucket_table(is_tr, "day range so far (ATR)",
                 lambda t: "<4" if t["range_atr"] < 4 else
                           "4-8" if t["range_atr"] < 8 else ">=8")
    bucket_table(is_tr, "risk (stop distance, ATR)",
                 lambda t: "<0.4" if t["risk_atr"] < 0.4 else
                           "0.4-0.8" if t["risk_atr"] < 0.8 else ">=0.8")
    is_losses = [t for t in is_tr if t["r"] <= 0]
    if is_losses:
        close_calls = sum(1 for t in is_losses if t["mfe_r"] >= 0.5 * target_r)
        log(f"\n  IS losses: {len(is_losses)}; "
            f"mean MFE {sum(t['mfe_r'] for t in is_losses) / len(is_losses):.2f}R "
            f"(target {target_r}R); "
            f"{close_calls} reached >= 50% of the target before dying")
        for lo_b, hi_b, lab in [(0.0, 0.001, "never favorable"),
                                (0.001, 0.25 * target_r, "<25% of tgt"),
                                (0.25 * target_r, 0.5 * target_r, "25-50%"),
                                (0.5 * target_r, 0.9 * target_r, "50-90%"),
                                (0.9 * target_r, 99, ">=90% (heartbreakers)")]:
            n = sum(1 for t in is_losses if lo_b <= t["mfe_r"] < hi_b)
            log(f"    MFE {lab:<24} {n}")
    # per-symbol IS win rates drive the drop-set candidates (IS only!)
    sym_wr = {}
    for t in is_tr:
        w, n = sym_wr.get(t["symbol"], (0, 0))
        sym_wr[t["symbol"]] = (w + (1 if t["r"] > 0 else 0), n + 1)
    return {s: (100.0 * w / n, n) for s, (w, n) in sym_wr.items()}


# ---- sweep -------------------------------------------------------------------

def sweep(signals, outcomes, oos_flag, drop_sets):
    ns = len(signals)
    gid = np.array([s["gid"] for s in signals], dtype=np.int64)
    i_arr = np.array([s["i"] for s in signals], dtype=np.int64)
    gradeA = np.array([s["grade"] == "A" for s in signals], dtype=bool)
    run_arr = np.array([s["run_atr"] for s in signals], dtype=np.float64)
    gap_arr = np.array([s["gap_atr"] for s in signals], dtype=np.float64)
    swept_arr = np.array([s["swept"] for s in signals], dtype=bool)
    pd_arr = np.array([s["pd_ok"] for s in signals], dtype=bool)
    hour_arr = np.array([s["hour"] for s in signals], dtype=np.int64)
    sess_arr = np.array([s["sess_ok"] for s in signals], dtype=bool)
    disp_arr = np.array([s["disp_atr"] for s in signals], dtype=np.float64)
    eff_arr = np.array([s["day_eff"] for s in signals], dtype=np.float64)
    unmit_arr = np.array([s["unmit"] for s in signals], dtype=bool)
    sym_arr = np.array([s["sym"] for s in signals])
    oos_arr = np.asarray(oos_flag, dtype=bool)
    midday_ok = ~np.isin(hour_arr, (12, 13))
    hour_masks = {"all": np.ones(ns, dtype=bool), "no_midday": midday_ok,
                  "sess": sess_arr, "sess_no_midday": sess_arr & midday_ok}
    drop_masks = {d: ~np.isin(sym_arr, sorted(d)) if d else
                  np.ones(ns, dtype=bool) for d in drop_sets}

    valid_m, filled_m, r_m, exit_m = [], [], [], []
    for m in range(len(MGMTS)):
        valid_m.append(np.array([outcomes[k][m]["valid"] for k in range(ns)],
                                dtype=bool))
        filled_m.append(np.array([outcomes[k][m]["filled"] for k in range(ns)],
                                 dtype=bool))
        r_m.append(np.array([outcomes[k][m]["r"] for k in range(ns)],
                            dtype=np.float64))
        exit_m.append(np.array([outcomes[k][m]["exit_abs"] for k in range(ns)],
                               dtype=np.int64))

    grid = list(product(TP_OPTS, RUN_OPTS, GAP_OPTS, SWEEP_OPTS, PD_OPTS,
                        HOUR_OPTS, drop_sets, DISP_OPTS, REGIME_OPTS,
                        STATE_OPTS))
    log(f"\nsweeping {len(grid)} configs over {ns} signals ...")
    rows = []
    t0 = time.time()
    for ci, cfg in enumerate(grid):
        tp, run, gap, swp, pdreq, hours, drop, dsp, regime, state = cfg
        m = MGMT_IDX[tp]
        mask = valid_m[m] & gradeA & (gap_arr >= gap) & \
            hour_masks[hours] & drop_masks[drop]
        if run is not None:
            mask &= run_arr < run
        if swp:
            mask &= swept_arr
        if pdreq:
            mask &= pd_arr
        if dsp is not None:
            mask &= disp_arr >= dsp
        if regime:
            mask &= eff_arr >= 0.5
        if state:
            mask &= unmit_arr
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
            if taken >= 2:
                continue
            if not fil[k]:
                free_at = ex[k] + 1
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
        if (ci + 1) % 2000 == 0:
            log(f"  {ci + 1}/{len(grid)} configs ({time.time() - t0:.0f}s)")
    log(f"sweep done in {time.time() - t0:.0f}s")
    return rows


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

    log("precomputing 3 management outcomes per signal (CE/FVG-stop fixed) ...")
    t0 = time.time()
    outcomes = []
    for s in signals:
        H, L, C = bars[s["gid"]]
        outcomes.append([simulate_mgmt(s, H, L, C, m) for m in MGMTS])
    log(f"  done in {time.time() - t0:.0f}s")

    # ---- STEP 1: replay the v2 winner and dissect every loss
    v2cfg = (V2_WINNER["tp"], V2_WINNER["run"], V2_WINNER["gap"],
             V2_WINNER["sweep"], V2_WINNER["pd"], V2_WINNER["hours"],
             V2_WINNER["drop"], V2_WINNER["disp"], V2_WINNER["regime"],
             V2_WINNER["state"])
    v2_trades = replay_config(v2cfg, signals, outcomes, oos_flag)
    sym_wr_is = dissect(v2_trades, target_r=0.7)

    # drop-set candidates from IS win rates ONLY (fixed thresholds, a priori)
    def below(th):
        return frozenset(s for s, (wr, n) in sym_wr_is.items()
                         if n >= 10 and wr < th)
    drop_sets = list(dict.fromkeys(
        [frozenset(), below(55.0), below(60.0), below(62.5)]))
    log(f"\nIS per-symbol win rates (v2-winner replay): "
        f"{ {s: (round(wr, 1), n) for s, (wr, n) in sym_wr_is.items()} }")
    log(f"drop-set candidates (from IS only): "
        f"{[sorted(d) for d in drop_sets]}")

    # ---- STEP 2: sweep
    rows = sweep(signals, outcomes, oos_flag, drop_sets)

    v2_row = next(r for r in rows if r["cfg"] == v2cfg)
    log(f"\nv2 winner inside v3 engine: "
        f"IS {v2_row['is']['trades']} tr {v2_row['is']['win_rate_pct']}% "
        f"{v2_row['is']['avg_r']} avgR | "
        f"OOS {v2_row['oos']['trades']} tr {v2_row['oos']['win_rate_pct']}% "
        f"{v2_row['oos']['avg_r']} avgR")

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
        honest_note = ("NO config held >= 60% win rate with >= 60 trades and "
                       "positive avg R on the untouched second half. ")
        if is_pass:
            best = is_pass[0]
            runner = is_pass[1] if len(is_pass) > 1 else None
            honest_note += (f"Best in-sample config's honest OOS numbers are "
                            f"reported instead ({oos_peeks} configs were "
                            f"checked against the gate).")
    else:
        honest_note = (f"Selection used ONLY the first half; the OOS gate was "
                       f"checked on {oos_peeks} IS-ranked configs this sweep "
                       f"plus {PRIOR_OOS_PEEKS} in the earlier round-3 sweeps "
                       f"= {oos_peeks + PRIOR_OOS_PEEKS} total OOS looks "
                       f"before best+runner-up were fixed. Related configs in "
                       f"a {len(rows)}-point grid share information, so treat "
                       f"the OOS number as an upper bound of true edge.")
        if runner is None:
            honest_note += " Only ONE config held the OOS gate; no runner-up."

    best_trades = replay_config(best["cfg"], signals, outcomes, oos_flag)
    top20 = sorted(rows, key=lambda r: (-r["is_wilson"],
                                        -(r["is"]["avg_r"] or -9)))[:20]

    v2_losses = [t for t in v2_trades if t["r"] <= 0]
    report = {
        "round": 3,
        "method": {
            "engine": "walk-forward, production market_tools+fvg logic, "
                      "no lookahead, conservative tie=loss, stop fills at "
                      "stop, positions never span sessions",
            "base": "v2 winner management fixed: grade A FVG, CE limit entry "
                    "(TTL 1h), stop beyond FVG far edge +0.1 ATR, max 2 "
                    "trades/symbol/day; v3 sweeps failure-pattern filters + "
                    "target 0.5/0.6/0.7R",
            "new_features": ["liquidity sweep of prior confirmed swing before "
                             "the FVG (2-bar fractal, causal)",
                             "CE premium/discount vs day-so-far dealing range",
                             "displacement body in ATR", "FVG mitigation state",
                             "day efficiency |close-open|/range (trend vs "
                             "chop)", "MFE toward target before death"],
            "split": f"first {mid} calendar days = in-sample, remaining "
                     f"{len(all_days) - mid} days (from {split_date}) = "
                     "out-of-sample, untouched by selection and by the "
                     "dissection tables",
            "selection": "rank IS-passing configs by Wilson 95% lower bound "
                         "of win rate, walk down checking the OOS gate "
                         "(>=60% wr, >=60 trades, avg R > 0)",
            "cooldown_bars": COOLDOWN_BARS,
            "skip_first_bars": SKIP_FIRST_BARS,
        },
        "data": {"symbols": [s[0] for s in SYMBOLS],
                 "days_total": len(all_days), "is_days": mid,
                 "oos_days": len(all_days) - mid, "split_date": split_date,
                 "n_signals": len(signals)},
        "grid_size": len(rows),
        "v2_winner_in_v3_engine": {"config": cfg_dict(v2cfg),
                                   "is": v2_row["is"], "oos": v2_row["oos"],
                                   "n_losses_total": len(v2_losses)},
        "is_symbol_win_rates_v2cfg": {s: {"wr": round(wr, 1), "n": n}
                                      for s, (wr, n) in sym_wr_is.items()},
        "selection": {"is_gate": IS_GATE, "oos_gate": OOS_GATE,
                      "n_is_passing": len(is_pass), "oos_peeks": oos_peeks},
        "best": {"config": cfg_dict(best["cfg"]),
                 "rules": cfg_words(best["cfg"]),
                 "is": best["is"], "oos": best["oos"],
                 "trades": best_trades},
        "runner_up": ({"config": cfg_dict(runner["cfg"]),
                       "rules": cfg_words(runner["cfg"]),
                       "is": runner["is"], "oos": runner["oos"]}
                      if runner else None),
        "top20_by_in_sample": [{"config": cfg_dict(r["cfg"]),
                                "is": r["is"], "oos": r["oos"],
                                "is_wilson_lb": r["is_wilson"]}
                               for r in top20],
        "honest_note": honest_note,
    }
    os.makedirs("reports", exist_ok=True)
    path = os.path.join("reports", "chart_backtest_round3.json")
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
