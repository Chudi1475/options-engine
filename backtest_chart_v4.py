"""Walk-forward backtest v4 of the /chart signal — round 4 (round 2 of the 80% grind).

Engine honesty identical to backtest_chart_v2/v3: real production logic from
market_tools + fvg, decisions at each 5m bar use ONLY bars up to that moment,
conservative tie rule (bar spanning stop and target = LOSS), stop fills at the
stop, positions never span sessions, CE limit fills never better than the
limit, fill bar checked stop-first.

STEP 1 (teach yourself): replay the round-3 winner (grade A FVG, CE limit
entry TTL 1h, stop FVG far edge +0.1 ATR, TP 0.5R all-out, run<1.0 ATR,
gap>=1.0 ATR, all hours, drop GBP/USD+SPX+SPY, max 2/symbol/day) on frozen
fresh data and dissect EVERY loss: hour, symbol, gap, displacement, premium/
discount, sweep-before-FVG, run-from-open, day regime, stop distance, MFE
toward the 0.5R target, whether the loss was a bar-granularity TIE, and what
real 2m sub-bar data says about each loss (resolved win / still loss / no 2m
coverage). Decision tables are printed on the IN-SAMPLE half only.

STEP 2 (fix and prove): a tight a-priori grid over management and filters:
  TP 0.4/0.5/0.6R x stop buffer 0.1/0.2 ATR (wider buffer mechanically cuts
  tie-losses) x min gap 1.0/1.1/1.25 x run filter x midday block x drop-sets
  (from IS-only symbol tables) x max 1 vs 2 trades/symbol/day.
ANTI-OVERFIT GATE (non-negotiable, unchanged): configs are ranked ONLY on the
first half of calendar days (Wilson 95% lower bound of win rate, then avg R).
Walking down that in-sample ranking, the first config whose untouched second
half holds >= 60% wr with >= 60 trades and avg R > 0 is the winner. Every OOS
gate check is counted and reported (7 prior looks from round 3).

EXECUTION-UPGRADE MEASUREMENT (engine B): after the winner is fixed, the same
config is re-scored with real 2m sub-bar data ordering intra-5m-bar events.
The 5m bar remains the arbiter of WHAT was touched; 2m bars only order the
events; any 2m bar that itself spans both levels = LOSS; missing or
inconsistent 2m data = fall back to the conservative 5m rule. 2m coverage
starts 2026-05-20/25/27 (stocks/fx/gold) and 2026-06-05 (BTC), so it covers
the whole OOS half but only the tail of the IS half — engine B is a
measurement of the fixed config, never a selection input.

Data is FROZEN to a pickle cache (scratchpad/r4cache) so every rerun of this
round sees the identical dataset.
"""

import json
import math
import os
import pickle
import sys
import time
from datetime import datetime
from itertools import product

import numpy as np
import pandas as pd
import yfinance as yf

import fvg
import market_tools as mt

CACHE = (r"C:/Users/Chudi/AppData/Local/Temp/claude/C--Users-Chudi/"
         r"57d7f2c7-4e77-45b8-b3fa-04ab441a1a92/scratchpad/r4cache")

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
FIVE_MIN_NS = 300 * 10**9

# ---- the v4 grid (fixed a priori; drop-sets filled from IS win rates) -------
TP_OPTS = ["0.4r", "0.5r", "0.6r", "0.7r"]
BUF_OPTS = [0.1, 0.2]               # stop buffer beyond FVG far edge, in ATR
GAP_OPTS = [1.0, 1.1, 1.25]         # min FVG gap in ATR (1.0 = confirmed r3)
RUN_OPTS = [None, 1.0]              # block when run-from-open >= X ATR
HOUR_OPTS = ["all", "no_midday"]
PERDAY_OPTS = [1, 2]                # max trades per symbol per day
PRIOR_OOS_PEEKS = 7                 # spent in round 3 (documented there)

MGMTS = list(product(TP_OPTS, BUF_OPTS))
MGMT_IDX = {m: k for k, m in enumerate(MGMTS)}

IS_GATE = {"min_trades": 60, "min_wr": 60.0}
OOS_GATE = {"min_trades": 60, "min_wr": 60.0}

# round-3 winner = the incumbent being dissected in STEP 1
INCUMBENT = {"tp": "0.5r", "buf": 0.1, "run": 1.0, "gap": 1.0,
             "hours": "all",
             "drop": frozenset({"GBP/USD", "SPX", "SPY"}), "perday": 2}


def log(msg):
    print(msg, flush=True)


def load_frozen(yfs):
    fn = os.path.join(CACHE, yfs.replace("=", "_").replace("^", "_")
                      .replace("/", "_") + ".pkl")
    with open(fn, "rb") as f:
        d = pickle.load(f)
    m5, m2, d1 = d["m5"], d["m2"], d["d1"]
    if m5 is None or m5.empty or d1 is None or d1.empty:
        return None, None, None
    for df in (m5, m2):
        if df is not None and getattr(df.index, "tz", None) is not None:
            df.index = df.index.tz_convert(mt.ET)
    m5 = m5.dropna(subset=["Open", "High", "Low", "Close"])
    if m2 is not None:
        m2 = m2.dropna(subset=["Open", "High", "Low", "Close"])
    return m5, m2, d1


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


# ---- signal stream (built once, every bar, no lookahead) --------------------

def build_stream():
    signals = []
    bars = []          # per gid: (H5, L5, C5, sub) — sub[j] = (h2[], l2[]) | None
    all_days = set()
    gid = -1
    atr_checked = False
    sub_cov = {}
    for disp_name, yfs, dec0, kind in SYMBOLS:
        t0 = time.time()
        m5, m2, d1 = load_frozen(yfs)
        if m5 is None:
            log(f"  !! no data for {yfs}, skipping")
            continue
        smas = sma20_by_date(d1)
        m2_by_day = {}
        if m2 is not None and not m2.empty:
            for dday, g in m2.groupby(m2.index.date):
                m2_by_day[dday] = g
        n_sig = 0
        cov_have = cov_tot = 0
        for day, day_df in m5.groupby(m5.index.date):
            n = len(day_df)
            if n < SKIP_FIRST_BARS + 2:
                continue
            all_days.add(str(day))
            H = day_df["High"].astype(float).values
            L = day_df["Low"].astype(float).values
            C = day_df["Close"].astype(float).values
            O = day_df["Open"].astype(float).values
            # map 2m sub-bars into each 5m bar (by bar-start timestamp)
            sub = [None] * n
            g2 = m2_by_day.get(day)
            if g2 is not None and len(g2) >= 2:
                ts2 = g2.index.as_unit("ns").asi8   # force ns (yf gives [s])
                H2 = g2["High"].astype(float).values
                L2 = g2["Low"].astype(float).values
                ts5 = day_df.index.as_unit("ns").asi8
                lo_idx = np.searchsorted(ts2, ts5, side="left")
                hi_idx = np.searchsorted(ts2, ts5 + FIVE_MIN_NS, side="left")
                for j in range(n):
                    if hi_idx[j] - lo_idx[j] >= 2:
                        sub[j] = (H2[lo_idx[j]:hi_idx[j]],
                                  L2[lo_idx[j]:hi_idx[j]])
            cov_tot += n
            cov_have += sum(1 for s_ in sub if s_ is not None)
            gid += 1
            bars.append((H, L, C, O, sub))
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
                    "dow": ts.strftime("%a"),
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
        sub_cov[disp_name] = (cov_have, cov_tot)
        log(f"  {disp_name:<8} {n_sig:>5} A/B-FVG signals, 2m sub-bar "
            f"coverage {100.0 * cov_have / max(cov_tot, 1):.0f}% "
            f"({time.time() - t0:.0f}s)")
    return signals, bars, sorted(all_days), sub_cov


# ---- per-signal management outcomes -----------------------------------------

INVALID = {"valid": False, "filled": False, "exit_abs": 0, "r": 0.0,
           "tag": "invalid", "risk_atr": 0.0, "mfe_r": 0.0,
           "tie": False, "fbwin": False}


def levels(sig, tp, buf):
    """Entry/stop/target shared by both engines. Returns None if invalid."""
    buy = sig["dir"] == "BUY"
    dec, atr = sig["dec"], sig["atr"]
    entry = round(sig["ce"], dec)
    stop = round(sig["fvg_bottom"] - buf * atr, dec) if buy \
        else round(sig["fvg_top"] + buf * atr, dec)
    if (buy and stop >= entry) or (not buy and stop <= entry):
        return None
    risk = round(abs(entry - stop), dec)
    if risk <= 0:
        return None
    k = float(tp[:-1])
    tgt = round(entry + k * risk, dec) if buy else round(entry - k * risk, dec)
    if tgt == entry:
        return None
    return entry, stop, tgt, risk, abs(tgt - entry) / risk


def simulate_a(sig, H, L, C, tp, buf, strict=False):
    """Engine A (legacy, identical semantics to v2/v3): CE limit entry,
    FVG-edge stop + buf, single all-out target, conservative stop-first on
    every bar including the fill bar. Tracks MFE, tie-loss flag and
    win-on-fill-bar flag.

    strict=True is ENGINE C: identical except the fill bar can never produce
    a win — when the limit is touched and the target is spanned by the SAME
    5m bar, the order of fill vs target touch is unknowable at 5m, so the
    win is denied and the position stays open (stop checks unchanged). This
    closes the anti-conservative fill-bar credit that 2m data exposed, and
    it is symmetric across both halves (no 2m dependency)."""
    i, n = sig["i"], sig["n"]
    buy = sig["dir"] == "BUY"
    lv = levels(sig, tp, buf)
    if lv is None:
        return INVALID
    entry, stop, tgt, risk, r_win = lv
    risk_atr = risk / sig["atr"]

    if (buy and entry >= sig["price"]) or (not buy and entry <= sig["price"]):
        filled, start, pre = True, i + 1, True
    else:
        filled, start, pre = False, None, False
        last_j = min(i + CE_TTL_BARS, n - 1)
        for j in range(i + 1, last_j + 1):
            if (buy and L[j] <= entry) or (not buy and H[j] >= entry):
                filled, start = True, j
                break
    if not filled:
        return {"valid": True, "filled": False,
                "exit_abs": min(i + CE_TTL_BARS, n - 1),
                "r": 0.0, "tag": "cancel", "risk_atr": risk_atr,
                "mfe_r": 0.0, "tie": False, "fbwin": False}

    mfe = 0.0

    def done(j, r, tag, tie=False, fbwin=False):
        return {"valid": True, "filled": True, "exit_abs": int(j),
                "r": float(r), "tag": tag, "risk_atr": float(risk_atr),
                "mfe_r": round(float(mfe), 3), "tie": bool(tie),
                "fbwin": bool(fbwin)}

    for j in range(start, n):
        hi, lo = H[j], L[j]
        fav = ((hi - entry) if buy else (entry - lo)) / risk
        if fav > mfe:
            mfe = fav
        stop_hit = (lo <= stop) if buy else (hi >= stop)
        tgt_hit = (hi >= tgt) if buy else (lo <= tgt)
        if stop_hit:
            return done(j, -1.0, "stop", tie=tgt_hit)
        if tgt_hit:
            if strict and j == start and not pre:
                continue        # engine C: no win credit on the fill bar
            return done(j, r_win, "tp", fbwin=(j == start and not pre))
    last = float(C[n - 1])
    move_r = ((last - entry) if buy else (entry - last)) / risk
    return done(n - 1, move_r, "eod")


def simulate_mkt(sig, H, L, C, O, tp, buf, sub=None):
    """Market entry at the NEXT 5m open (no retrace wait, no limit order, so
    no fill-order ambiguity by construction). Stop beyond the FVG far edge
    +buf ATR, single all-out target. Conservative stop-first on every bar;
    a bar spanning stop and target = LOSS unless real 2m sub-bars (when
    provided) prove the target was hit first (tie=loss at 2m granularity).
    Engines A and C are identical for this entry style."""
    i, n = sig["i"], sig["n"]
    buy = sig["dir"] == "BUY"
    dec, atr = sig["dec"], sig["atr"]
    entry = float(O[i + 1])
    stop = round(sig["fvg_bottom"] - buf * atr, dec) if buy \
        else round(sig["fvg_top"] + buf * atr, dec)
    if (buy and stop >= entry) or (not buy and stop <= entry):
        return INVALID
    risk = abs(entry - stop)
    if risk <= 0:
        return INVALID
    k = float(tp[:-1])
    tgt = entry + k * risk if buy else entry - k * risk
    risk_atr = risk / atr
    mfe = 0.0
    tie2m = None

    def done(j, r, tag, tie=False):
        return {"valid": True, "filled": True, "exit_abs": int(j),
                "r": float(r), "tag": tag, "risk_atr": float(risk_atr),
                "mfe_r": round(float(mfe), 3), "tie": bool(tie),
                "fbwin": False, "fine_used": 0, "fb_deny": None,
                "tie2m": tie2m}

    for j in range(i + 1, n):
        hi, lo = H[j], L[j]
        fav = ((hi - entry) if buy else (entry - lo)) / risk
        if fav > mfe:
            mfe = fav
        stop_hit = (lo <= stop) if buy else (hi >= stop)
        tgt_hit = (hi >= tgt) if buy else (lo <= tgt)
        if stop_hit and tgt_hit and sub is not None:
            hin = H[j + 1] if j + 1 < n else hi
            lon = L[j + 1] if j + 1 < n else lo
            if _sub_ok(sub[j], hi, lo, atr, hin, lon):
                for h2, l2 in zip(*sub[j]):
                    s2 = (l2 <= stop) if buy else (h2 >= stop)
                    t2 = (h2 >= tgt) if buy else (l2 <= tgt)
                    if s2:
                        tie2m = "loss"
                        return done(j, -1.0, "stop", tie=True)
                    if t2:
                        tie2m = "win"
                        return done(j, k, "tp")
        if stop_hit:
            return done(j, -1.0, "stop", tie=tgt_hit)
        if tgt_hit:
            return done(j, k, "tp")
    last = float(C[n - 1])
    move_r = ((last - entry) if buy else (entry - last)) / risk
    return done(n - 1, move_r, "eod")


def _sub_ok(sub_j, H5, L5, atr, H5n, L5n):
    """2m sub-bars usable for ordering: exist, and do not exceed the union of
    this and the NEXT 5m bar's range by more than 0.1 ATR (bad-print guard;
    the :04 2m bar legitimately straddles into the next 5m bar)."""
    if sub_j is None:
        return False
    h2, l2 = sub_j
    tol = 0.1 * atr
    return (h2.max() <= max(H5, H5n) + tol) and \
           (l2.min() >= min(L5, L5n) - tol)


def simulate_b(sig, H, L, C, sub, tp, buf):
    """Engine B (execution-upgrade measurement): same trade, but intra-5m-bar
    event ORDER is resolved with real 2m sub-bars where available. The 5m bar
    stays the arbiter of what was touched; a 2m bar that itself spans both
    levels = LOSS; missing/inconsistent 2m = conservative 5m fallback (engine
    A semantics for that bar). Never a selection input."""
    i, n = sig["i"], sig["n"]
    buy = sig["dir"] == "BUY"
    lv = levels(sig, tp, buf)
    if lv is None:
        return INVALID
    entry, stop, tgt, risk, r_win = lv
    risk_atr = risk / sig["atr"]
    atr = sig["atr"]

    mfe = 0.0
    fine_used = 0
    fb_deny = None       # 'pre_fill' | 'same2m' — why a fill-bar win was denied
    tie2m = None         # 'win' | 'loss' — how a stop+target tie bar resolved

    def done(j, r, tag):
        return {"valid": True, "filled": tag != "cancel", "exit_abs": int(j),
                "r": float(r), "tag": tag, "risk_atr": float(risk_atr),
                "mfe_r": round(float(mfe), 3), "tie": False, "fbwin": False,
                "fine_used": fine_used, "fb_deny": fb_deny, "tie2m": tie2m}

    state = "open" if ((buy and entry >= sig["price"]) or
                       (not buy and entry <= sig["price"])) else "pending"
    for j in range(i + 1, n):
        hi, lo = H[j], L[j]
        hin = H[j + 1] if j + 1 < n else hi
        lon = L[j + 1] if j + 1 < n else lo
        if state == "pending":
            if j > i + CE_TTL_BARS:
                return done(min(i + CE_TTL_BARS, n - 1), 0.0, "cancel")
            fill_hit = (lo <= entry) if buy else (hi >= entry)
            if not fill_hit:
                continue
            # fill happens inside this bar; order fill/stop/target
            fav = ((hi - entry) if buy else (entry - lo)) / risk
            if fav > mfe:
                mfe = fav
            stop_hit = (lo <= stop) if buy else (hi >= stop)
            tgt_hit = (hi >= tgt) if buy else (lo <= tgt)
            if _sub_ok(sub[j], hi, lo, atr, hin, lon):
                fine_used += 1
                h2s, l2s = sub[j]
                got_fill = False
                same2m_tgt = False
                res = None
                for h2, l2 in zip(h2s, l2s):
                    f2 = (l2 <= entry) if buy else (h2 >= entry)
                    s2 = (l2 <= stop) if buy else (h2 >= stop)
                    t2 = (h2 >= tgt) if buy else (l2 <= tgt)
                    if not got_fill:
                        if not f2:
                            continue
                        got_fill = True
                        if s2:          # fill + stop in one 2m bar -> loss
                            res = ("stop", -1.0)
                            break
                        if t2:          # fill + target in one 2m bar:
                            same2m_tgt = True   # order unknown -> no win yet
                        continue
                    if s2:
                        res = ("stop", -1.0)
                        break
                    if t2:
                        res = ("tp", r_win)
                        break
                if not got_fill:
                    # 2m data contradicts the 5m fill -> conservative fallback
                    if stop_hit:
                        return done(j, -1.0, "stop")
                    if tgt_hit:
                        return done(j, r_win, "tp")
                    state = "open"
                    continue
                if res is not None:
                    return done(j, res[1], res[0])
                # filled on 2m, nothing decisive after the fill
                if stop_hit:
                    return done(j, -1.0, "stop")
                if tgt_hit:     # 5m credited a fill-bar win; 2m denies it
                    fb_deny = "same2m" if same2m_tgt else "pre_fill"
                state = "open"
                continue
            # no usable sub-bars: engine A semantics on the fill bar
            if stop_hit:
                return done(j, -1.0, "stop")
            if tgt_hit:
                return done(j, r_win, "tp")
            state = "open"
            continue
        # state == open
        fav = ((hi - entry) if buy else (entry - lo)) / risk
        if fav > mfe:
            mfe = fav
        stop_hit = (lo <= stop) if buy else (hi >= stop)
        tgt_hit = (hi >= tgt) if buy else (lo <= tgt)
        if stop_hit and tgt_hit:
            if _sub_ok(sub[j], hi, lo, atr, hin, lon):
                fine_used += 1
                h2s, l2s = sub[j]
                for h2, l2 in zip(h2s, l2s):
                    s2 = (l2 <= stop) if buy else (h2 >= stop)
                    t2 = (h2 >= tgt) if buy else (l2 <= tgt)
                    if s2:              # stop first (or same 2m bar) -> loss
                        tie2m = "loss"
                        return done(j, -1.0, "stop")
                    if t2:
                        tie2m = "win"
                        return done(j, r_win, "tp")
            return done(j, -1.0, "stop")   # unresolved tie stays a LOSS
        if stop_hit:
            return done(j, -1.0, "stop")
        if tgt_hit:
            return done(j, r_win, "tp")
    if state == "pending":
        return done(min(i + CE_TTL_BARS, n - 1), 0.0, "cancel")
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


def hours_pass(opt, hour):
    if opt == "all":
        return True
    return hour not in (12, 13)


def cfg_dict(cfg, entry="ce"):
    tp, buf, run, gap, hours, drop, perday = cfg
    return {"fvg_grade": "A", "entry": entry, "stop": "fvg",
            "stop_buffer_atr": buf, "tp": tp,
            "max_run_from_open_atr": run, "min_gap_atr": gap,
            "hours": hours, "drop_symbols": sorted(drop),
            "max_per_symbol_day": perday}


def cfg_words(cfg, entry="ce"):
    tp, buf, run, gap, hours, drop, perday = cfg
    bits = ["grade A FVG only",
            "entry at FVG CE limit (TTL 1h / 12 bars)" if entry == "ce"
            else "market entry at next 5m open (no retrace wait)",
            f"stop beyond FVG far edge +{buf} ATR",
            f"TP {tp[:-1]}R all-out"]
    bits.append(f"skip if run-from-open >= {run} ATR" if run is not None
                else "no extension filter")
    bits.append(f"FVG gap >= {gap} ATR")
    bits.append("all hours" if hours == "all" else "12:00-13:59 ET blocked")
    if drop:
        bits.append("dropped: " + ", ".join(sorted(drop)))
    bits.append(f"max {perday} trade{'s' if perday > 1 else ''}/symbol/day")
    bits.append("6-bar cooldown")
    return "; ".join(bits)


# ---- replay ------------------------------------------------------------------

def sig_passes(s, cfg):
    tp, buf, run, gap, hours, drop, perday = cfg
    if s["grade"] != "A":
        return False
    if run is not None and s["run_atr"] >= run:
        return False
    if s["gap_atr"] < gap:
        return False
    if not hours_pass(hours, s["hour"]):
        return False
    if s["sym"] in drop:
        return False
    return True


def replay_config(cfg, signals, get_o):
    """get_o(k) -> outcome dict for signal k under cfg's management."""
    perday = cfg[6]
    trades = []
    cur_g, free_at, taken = -1, -1, 0
    for k, s in enumerate(signals):
        o = get_o(k)
        if not o["valid"]:
            continue
        if not sig_passes(s, cfg):
            continue
        g = s["gid"]
        if g != cur_g:
            cur_g, free_at, taken = g, -1, 0
        if s["i"] < free_at:
            continue
        if taken >= perday:
            continue
        if not o["filled"]:
            free_at = o["exit_abs"] + 1
            continue
        trades.append({"sig_k": k, "symbol": s["sym"], "day": s["day"],
                       "time": s["time"], "direction": s["dir"],
                       "hour": s["hour"], "dow": s["dow"],
                       "weak": s["weak"], "trade_no": taken + 1,
                       "run_atr": round(s["run_atr"], 2),
                       "gap_atr": round(s["gap_atr"], 2),
                       "disp_atr": round(s["disp_atr"], 2),
                       "pd_zone": s["pd_zone"], "pd_ok": s["pd_ok"],
                       "swept": s["swept"], "unmit": s["unmit"],
                       "fvg_age": s["fvg_age"],
                       "day_eff": s["day_eff"], "range_atr": s["range_atr"],
                       "risk_atr": round(o["risk_atr"], 2),
                       "mfe_r": o["mfe_r"], "tie": o["tie"],
                       "fbwin": o["fbwin"],
                       "r": round(o["r"], 3), "exit": o["tag"]})
        taken += 1
        free_at = o["exit_abs"] + 1 + COOLDOWN_BARS
    return trades


def half_stats(trades, is_days):
    out = {}
    for half in ("is", "oos"):
        sel = [t for t in trades
               if (t["day"] in is_days) == (half == "is")]
        w = sum(1 for t in sel if t["r"] > 0)
        out[half] = stats(w, len(sel), sum(t["r"] for t in sel))
    return out


# ---- dissection --------------------------------------------------------------

def bucket_table(trades, name, keyfn):
    rows = {}
    for t in trades:
        b = keyfn(t)
        w, n = rows.get(b, (0, 0))
        rows[b] = (w + (1 if t["r"] > 0 else 0), n + 1)
    log(f"  {name:<36} {'wr%':>6} {'w/n':>9}")
    for b in sorted(rows, key=lambda x: str(x)):
        w, n = rows[b]
        log(f"    {str(b):<34} {100.0 * w / n:6.1f} {w:>4}/{n:<4}")


def dissect(trades, is_days, target_r, b_verdicts):
    is_tr = [t for t in trades if t["day"] in is_days]
    losses = [t for t in trades if t["r"] <= 0]
    log(f"\n===== STEP 1: incumbent replay, {len(trades)} trades "
        f"({len(is_tr)} IS), {len(losses)} losses =====")
    log(f"\n--- every loss (both halves, labeled; 2m = engine-B verdict) ---")
    log(f"{'half':<4} {'symbol':<8} {'time':<26} {'dir':<4} {'dow':<3} "
        f"{'hr':>2} {'gap':>5} {'disp':>5} {'pd':<11} {'swp':<3} "
        f"{'run':>6} {'eff':>5} {'rng':>5} {'risk':>5} {'mfe':>6} "
        f"{'pct_tgt':>7} {'tie':>3} {'#':>1} {'2m':<9}")
    for t in losses:
        pct = 100.0 * t["mfe_r"] / target_r if target_r else 0.0
        half = "is" if t["day"] in is_days else "oos"
        bv = b_verdicts.get(t["sig_k"], "n/a")
        log(f"{half:<4} {t['symbol']:<8} {t['time']:<26} "
            f"{t['direction']:<4} {t['dow']:<3} {t['hour']:>2} "
            f"{t['gap_atr']:>5.2f} {t['disp_atr']:>5.2f} {t['pd_zone']:<11} "
            f"{'Y' if t['swept'] else '.':<3} "
            f"{t['run_atr']:>6.2f} {t['day_eff']:>5.2f} "
            f"{t['range_atr']:>5.2f} {t['risk_atr']:>5.2f} "
            f"{t['mfe_r']:>6.2f} {pct:>6.0f}% "
            f"{'Y' if t['tie'] else '.':>3} {t['trade_no']:>1} {bv:<9}")

    log(f"\n--- IN-SAMPLE decision tables (OOS untouched by rule-building) ---")
    bucket_table(is_tr, "symbol", lambda t: t["symbol"])
    bucket_table(is_tr, "hour (ET)", lambda t: t["hour"])
    bucket_table(is_tr, "day of week", lambda t: t["dow"])
    bucket_table(is_tr, "gap_atr fine bucket",
                 lambda t: "1.0-1.1" if t["gap_atr"] < 1.1 else
                           "1.1-1.25" if t["gap_atr"] < 1.25 else
                           "1.25-1.5" if t["gap_atr"] < 1.5 else ">=1.5")
    bucket_table(is_tr, "displacement bucket",
                 lambda t: "<1.5" if t["disp_atr"] < 1.5 else
                           "1.5-1.8" if t["disp_atr"] < 1.8 else ">=1.8")
    bucket_table(is_tr, "pd location ok", lambda t: t["pd_ok"])
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
                 lambda t: "<0.6" if t["risk_atr"] < 0.6 else
                           "0.6-0.8" if t["risk_atr"] < 0.8 else ">=0.8")
    bucket_table(is_tr, "direction", lambda t: t["direction"])
    bucket_table(is_tr, "weak-bias signal", lambda t: t["weak"])
    bucket_table(is_tr, "trade # within symbol-day", lambda t: t["trade_no"])

    is_losses = [t for t in is_tr if t["r"] <= 0]
    if is_losses:
        ties = sum(1 for t in is_losses if t["tie"])
        log(f"\n  IS losses: {len(is_losses)}; bar-granularity ties "
            f"(exit bar spanned stop AND target): {ties}")
        log(f"  mean MFE {sum(t['mfe_r'] for t in is_losses) / len(is_losses):.2f}R"
            f" (target {target_r}R)")
        for lo_b, hi_b, lab in [(0.0, 0.001, "never favorable"),
                                (0.001, 0.25 * target_r, "<25% of tgt"),
                                (0.25 * target_r, 0.5 * target_r, "25-50%"),
                                (0.5 * target_r, 0.9 * target_r, "50-90%"),
                                (0.9 * target_r, 99, ">=90% (heartbreakers)")]:
            n = sum(1 for t in is_losses if lo_b <= t["mfe_r"] < hi_b)
            log(f"    MFE {lab:<24} {n}")
    is_wins = [t for t in is_tr if t["r"] > 0]
    fb = sum(1 for t in is_wins if t["fbwin"])
    log(f"  IS wins: {len(is_wins)}; awarded on the fill bar itself "
        f"(fill+target same 5m bar, order unknown at 5m): {fb}")


# ---- sweep -------------------------------------------------------------------

def sweep(signals, outcomes, oos_flag, drop_sets):
    ns = len(signals)
    gid = np.array([s["gid"] for s in signals], dtype=np.int64)
    i_arr = np.array([s["i"] for s in signals], dtype=np.int64)
    gradeA = np.array([s["grade"] == "A" for s in signals], dtype=bool)
    run_arr = np.array([s["run_atr"] for s in signals], dtype=np.float64)
    gap_arr = np.array([s["gap_atr"] for s in signals], dtype=np.float64)
    hour_arr = np.array([s["hour"] for s in signals], dtype=np.int64)
    sym_arr = np.array([s["sym"] for s in signals])
    oos_arr = np.asarray(oos_flag, dtype=bool)
    midday_ok = ~np.isin(hour_arr, (12, 13))
    hour_masks = {"all": np.ones(ns, dtype=bool), "no_midday": midday_ok}
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

    grid = list(product(TP_OPTS, BUF_OPTS, RUN_OPTS, GAP_OPTS, HOUR_OPTS,
                        drop_sets, PERDAY_OPTS))
    log(f"\nsweeping {len(grid)} configs over {ns} signals ...")
    rows = []
    t0 = time.time()
    for ci, (tp, buf, run, gap, hours, drop, perday) in enumerate(grid):
        cfg = (tp, buf, run, gap, hours, drop, perday)
        m = MGMT_IDX[(tp, buf)]
        mask = valid_m[m] & gradeA & (gap_arr >= gap) & \
            hour_masks[hours] & drop_masks[drop]
        if run is not None:
            mask &= run_arr < run
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
            if taken >= perday:
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
    log(f"sweep done in {time.time() - t0:.0f}s")
    return rows


def classify_b(trades_a, b_out, is_days):
    """Transition table A -> B for a fixed config's trades, per half."""
    counts = {}
    for t in trades_a:
        half = "is" if t["day"] in is_days else "oos"
        ob = b_out[t["sig_k"]]
        a_win, b_win = t["r"] > 0, ob["r"] > 0
        if a_win and b_win:
            key = "win->win"
        elif a_win and not b_win:
            if ob["fb_deny"] == "pre_fill":
                key = f"win->{ob['tag']} (2m: tgt touched BEFORE fill)"
            elif ob["fb_deny"] == "same2m":
                key = f"win->{ob['tag']} (2m: fill+tgt same 2m bar, denied)"
            elif ob["fine_used"] == 0:
                key = f"win->{ob['tag']} (no 2m data)"
            else:
                key = f"win->{ob['tag']} (2m-ordered)"
        elif not a_win and b_win:
            key = "loss->WIN (2m: tgt hit before stop)" \
                if ob["tie2m"] == "win" else "loss->WIN (2m-ordered)"
        else:
            key = "loss->loss"
        k2 = (half, key)
        counts[k2] = counts.get(k2, 0) + 1
    return counts


def main():
    t_start = time.time()
    log("building signal stream from FROZEN cache "
        "(every bar, no lookahead, A/B FVG only) ...")
    signals, bars, all_days, sub_cov = build_stream()
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

    log(f"precomputing {len(MGMTS)}x3 management outcomes per signal "
        f"(TP x stop-buffer; CE engine A legacy, CE engine C strict, "
        f"market-at-next-open) ...")
    t0 = time.time()
    outcomes_a, outcomes_c, outcomes_m = [], [], []
    for s in signals:
        H, L, C, O, _sub = bars[s["gid"]]
        outcomes_a.append([simulate_a(s, H, L, C, tp, buf)
                           for tp, buf in MGMTS])
        outcomes_c.append([simulate_a(s, H, L, C, tp, buf, strict=True)
                           for tp, buf in MGMTS])
        outcomes_m.append([simulate_mkt(s, H, L, C, O, tp, buf)
                           for tp, buf in MGMTS])
    log(f"  done in {time.time() - t0:.0f}s")

    # ---- STEP 1: replay the incumbent (round-3 winner) and dissect every loss
    inc_cfg = (INCUMBENT["tp"], INCUMBENT["buf"], INCUMBENT["run"],
               INCUMBENT["gap"], INCUMBENT["hours"], INCUMBENT["drop"],
               INCUMBENT["perday"])
    m_inc = MGMT_IDX[(INCUMBENT["tp"], INCUMBENT["buf"])]
    inc_trades = replay_config(inc_cfg, signals,
                               lambda k: outcomes_a[k][m_inc])
    # engine-B verdict for each incumbent trade (real 2m ordering)
    b_verdicts = {}
    b_out_inc = {}
    for t in inc_trades:
        k = t["sig_k"]
        s = signals[k]
        H, L, C, O, sub = bars[s["gid"]]
        ob = simulate_b(s, H, L, C, sub, INCUMBENT["tp"], INCUMBENT["buf"])
        b_out_inc[k] = ob
        if t["r"] <= 0:
            if ob["fine_used"] == 0:
                b_verdicts[k] = "no2m"
            elif ob["r"] > 0:
                b_verdicts[k] = "WIN@2m"
            else:
                b_verdicts[k] = "loss@2m"
    dissect(inc_trades, is_days, target_r=0.5, b_verdicts=b_verdicts)

    # incumbent under engines A, B, C (fixed config, measurement only)
    inc_b_trades = [dict(t, r=round(b_out_inc[t["sig_k"]]["r"], 3))
                    for t in inc_trades]
    inc_c_trades = replay_config(inc_cfg, signals,
                                 lambda k: outcomes_c[k][m_inc])
    inc_a_hs = half_stats(inc_trades, is_days)
    inc_b_hs = half_stats(inc_b_trades, is_days)
    inc_c_hs = half_stats(inc_c_trades, is_days)
    log(f"\n--- incumbent under all three engines (fixed config) ---")
    for nm, hs in (("A (legacy 5m, fill-bar credit)", inc_a_hs),
                   ("B (2m-ordered, tie=loss at 2m)", inc_b_hs),
                   ("C (strict 5m, no fill-bar win)", inc_c_hs)):
        log(f"  engine {nm:<33} IS {hs['is']['trades']:>3} tr "
            f"{hs['is']['win_rate_pct']}% wr {hs['is']['avg_r']} avgR | "
            f"OOS {hs['oos']['trades']:>3} tr {hs['oos']['win_rate_pct']}% wr "
            f"{hs['oos']['avg_r']} avgR")
    inc_transition = classify_b(inc_trades, b_out_inc, is_days)
    log(f"\n--- A->B transition anatomy, incumbent (fixed config) ---")
    for (half, key), c in sorted(inc_transition.items()):
        log(f"  {half:<4} {key:<52} {c}")

    # ---- drop-set candidates from a no-drop replay, IS half ONLY, engine C
    base_cfg = (INCUMBENT["tp"], INCUMBENT["buf"], INCUMBENT["run"],
                INCUMBENT["gap"], INCUMBENT["hours"], frozenset(),
                INCUMBENT["perday"])
    base_trades_c = replay_config(base_cfg, signals,
                                  lambda k: outcomes_c[k][m_inc])
    sym_wr = {}
    for t in base_trades_c:
        if t["day"] not in is_days:
            continue
        w, n = sym_wr.get(t["symbol"], (0, 0))
        sym_wr[t["symbol"]] = (w + (1 if t["r"] > 0 else 0), n + 1)
    sym_wr = {s_: (100.0 * w / n, n) for s_, (w, n) in sym_wr.items()}

    def below(th):
        return frozenset(s_ for s_, (wr, n) in sym_wr.items()
                         if n >= 8 and wr < th)
    drop_sets = list(dict.fromkeys(
        [frozenset(), below(55.0), below(60.0), below(62.5),
         frozenset({"GBP/USD", "SPX"}), INCUMBENT["drop"]]))
    log(f"\nIS per-symbol win rates (no-drop replay, ENGINE C): "
        f"{ {s_: (round(wr, 1), n) for s_, (wr, n) in sym_wr.items()} }")
    log(f"drop-set candidates (IS-only + round-3 carryovers): "
        f"{[sorted(d) for d in drop_sets]}")

    # ---- STEP 2: sweep; SELECTION runs on honest engines only
    #      (CE entry scored by strict engine C + market entry, merged)
    log("\n== sweep: CE entry under ENGINE C (strict, selection) ==")
    rows_c = sweep(signals, outcomes_c, oos_flag, drop_sets)
    for r in rows_c:
        r["entry"] = "ce"
    log("== sweep: MARKET entry at next open (no fill ambiguity) ==")
    rows_m = sweep(signals, outcomes_m, oos_flag, drop_sets)
    for r in rows_m:
        r["entry"] = "mkt"
    log("== sweep: CE entry under ENGINE A (legacy, continuity only) ==")
    rows_a = sweep(signals, outcomes_a, oos_flag, drop_sets)
    a_by_cfg = {r["cfg"]: r for r in rows_a}
    rows_sel = rows_c + rows_m

    inc_row_a = a_by_cfg[inc_cfg]
    log(f"\nincumbent engine A: "
        f"IS {inc_row_a['is']['trades']} tr "
        f"{inc_row_a['is']['win_rate_pct']}% {inc_row_a['is']['avg_r']} avgR"
        f" | OOS {inc_row_a['oos']['trades']} tr "
        f"{inc_row_a['oos']['win_rate_pct']}% {inc_row_a['oos']['avg_r']} avgR")

    is_pass = [r for r in rows_sel
               if r["is"]["trades"] >= IS_GATE["min_trades"]
               and r["is"]["win_rate_pct"] is not None
               and r["is"]["win_rate_pct"] >= IS_GATE["min_wr"]
               and r["is"]["avg_r"] is not None and r["is"]["avg_r"] > 0]
    is_pass.sort(key=lambda r: (-r["is_wilson"], -(r["is"]["avg_r"] or 0)))
    log(f"{len(is_pass)} configs pass the in-sample gate under honest "
        f"scoring (of {len(rows_sel)})")
    # honest IS landscape, top 10 by Wilson LB regardless of gate
    by_wilson = sorted((r for r in rows_sel if r["is"]["trades"] >= 60),
                       key=lambda r: (-r["is_wilson"],
                                      -(r["is"]["avg_r"] or -9)))
    log("top 10 honest-engine configs by IS Wilson LB (>=60 IS trades):")
    for r in by_wilson[:10]:
        log(f"  [{r['entry']}] IS {r['is']['trades']:>3} tr "
            f"{r['is']['win_rate_pct']:>5}% wr {r['is']['avg_r']:>7} avgR "
            f"wilson {r['is_wilson']:>5} | {cfg_words(r['cfg'], r['entry'])}")

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
        if is_pass:
            best = is_pass[0]
            runner = is_pass[1] if len(is_pass) > 1 else None
            honest_note = ("NO config held the OOS gate under honest "
                           f"scoring. Top honest in-sample config's OOS "
                           f"numbers reported instead ({oos_peeks} gate "
                           f"checks spent).")
        else:
            best = by_wilson[0] if by_wilson else rows_sel[0]
            runner = by_wilson[1] if len(by_wilson) > 1 else None
            oos_peeks = 1
            honest_note = ("NO config even passed the IN-SAMPLE gate "
                           "(>=60 trades, >=60% wr, avg R > 0) under honest "
                           "scoring — the strategy family has no honest "
                           "edge in this data. The top honest-IS config's "
                           "OOS numbers are reported for completeness "
                           "(1 OOS look).")
    else:
        honest_note = (f"Selection used ONLY the first half under honest "
                       f"scoring; {oos_peeks} OOS gate checks this sweep.")
    honest_note += (f" Cumulative OOS-look ledger: {PRIOR_OOS_PEEKS} round-3 "
                    f"looks + 2 engine-A gate checks spent by this round's "
                    f"first (pre-engine-C) sweep + 3 fixed-config "
                    f"measurements (incumbent B, incumbent C, best B) + "
                    f"{oos_peeks} honest gate checks = "
                    f"{PRIOR_OOS_PEEKS + 2 + 3 + oos_peeks} total looks. "
                    f"Related configs in a {len(rows_sel)}-point grid share "
                    f"information; treat OOS numbers as an upper bound of "
                    f"true edge.")

    # best config trades + engine-B measurement of the FIXED best config
    m_best = MGMT_IDX[(best["cfg"][0], best["cfg"][1])]
    best_entry = best["entry"]
    out_best_sel = outcomes_c if best_entry == "ce" else outcomes_m
    best_trades_c = replay_config(best["cfg"], signals,
                                  lambda k: out_best_sel[k][m_best])
    b_out_best = {}
    for t in best_trades_c:
        k = t["sig_k"]
        s = signals[k]
        H, L, C, O, sub = bars[s["gid"]]
        if best_entry == "ce":
            b_out_best[k] = simulate_b(s, H, L, C, sub,
                                       best["cfg"][0], best["cfg"][1])
        else:
            b_out_best[k] = simulate_mkt(s, H, L, C, O,
                                         best["cfg"][0], best["cfg"][1],
                                         sub=sub)
    best_b_trades = [dict(t, r=round(b_out_best[t["sig_k"]]["r"], 3))
                     for t in best_trades_c]
    best_b_hs = half_stats(best_b_trades, is_days)
    best_a_row = a_by_cfg[best["cfg"]] if best_entry == "ce" else best
    best_transition = classify_b(best_trades_c, b_out_best, is_days)

    top20 = sorted(rows_sel, key=lambda r: (-r["is_wilson"],
                                            -(r["is"]["avg_r"] or -9)))[:20]

    inc_losses = [t for t in inc_trades if t["r"] <= 0]
    report = {
        "round": 4,
        "run_date": str(datetime.now().date()),
        "method": {
            "engine": "walk-forward, production market_tools+fvg logic, no "
                      "lookahead, conservative tie=loss, stop fills at stop, "
                      "positions never span sessions. THREE scoring engines: "
                      "A = legacy 5m (rounds 2-3 comparable; credits a win "
                      "when fill and target land in the same 5m bar — "
                      "PROVEN anti-conservative this round); B = real 2m "
                      "sub-bars order intra-5m-bar events (tie=loss at 2m, "
                      "missing/inconsistent 2m falls back to A; 2m covers "
                      "the whole OOS half, only the tail of IS; measurement "
                      "only, never selection); C = strict 5m (fill bar can "
                      "never produce a win; symmetric across halves) — the "
                      "SELECTION engine this round",
            "grid": "TP 0.4/0.5/0.6/0.7R x stop buffer 0.1/0.2 ATR x gap "
                    "1.0/1.1/1.25 x run None/1.0 x hours all/no_midday x "
                    "drop-sets (IS-derived) x max 1/2 per symbol-day",
            "split": f"first {mid} calendar days = in-sample, remaining "
                     f"{len(all_days) - mid} days (from {split_date}) = "
                     "out-of-sample, untouched by selection and by the "
                     "dissection tables",
            "selection": "rank engine-C IS-passing configs by Wilson 95% "
                         "lower bound of win rate, walk down checking the "
                         "OOS gate (>=60% wr, >=60 trades, avg R > 0)",
            "cooldown_bars": COOLDOWN_BARS,
            "skip_first_bars": SKIP_FIRST_BARS,
            "data_frozen": "yfinance 5m+2m cached 2026-07-08 (scratchpad/"
                           "r4cache); 2m coverage per symbol below",
        },
        "data": {"symbols": [s_[0] for s_ in SYMBOLS],
                 "days_total": len(all_days), "is_days": mid,
                 "oos_days": len(all_days) - mid, "split_date": split_date,
                 "n_signals": len(signals),
                 "sub_2m_coverage_pct": {s_: round(100.0 * h / max(t_, 1), 1)
                                         for s_, (h, t_) in sub_cov.items()}},
        "grid_size": len(rows_sel),
        "incumbent": {"config": cfg_dict(inc_cfg),
                      "engine_a": {"is": inc_row_a["is"],
                                   "oos": inc_row_a["oos"]},
                      "engine_b": {"is": inc_b_hs["is"],
                                   "oos": inc_b_hs["oos"]},
                      "engine_c": {"is": inc_c_hs["is"],
                                   "oos": inc_c_hs["oos"]},
                      "n_losses_total": len(inc_losses),
                      "loss_2m_verdicts": {
                          v: sum(1 for x in b_verdicts.values() if x == v)
                          for v in ("WIN@2m", "loss@2m", "no2m")},
                      "a_to_b_transitions": {
                          f"{h}|{k2}": c for (h, k2), c
                          in sorted(inc_transition.items())}},
        "is_symbol_win_rates_nodrop_engine_c": {
            s_: {"wr": round(wr, 1), "n": n}
            for s_, (wr, n) in sym_wr.items()},
        "selection": {"is_gate": IS_GATE, "oos_gate": OOS_GATE,
                      "n_is_passing": len(is_pass), "oos_peeks": oos_peeks,
                      "prior_oos_peeks": PRIOR_OOS_PEEKS},
        "best": {"config": cfg_dict(best["cfg"], best_entry),
                 "rules": cfg_words(best["cfg"], best_entry),
                 "engine_c": {"is": best["is"], "oos": best["oos"]},
                 "engine_a": {"is": best_a_row["is"],
                              "oos": best_a_row["oos"]},
                 "engine_b": {"is": best_b_hs["is"], "oos": best_b_hs["oos"]},
                 "a_to_b_transitions": {
                     f"{h}|{k2}": c for (h, k2), c
                     in sorted(best_transition.items())},
                 "trades_engine_c": [
                     {k2: v for k2, v in t.items() if k2 != "sig_k"}
                     for t in best_trades_c]},
        "runner_up": ({"config": cfg_dict(runner["cfg"], runner["entry"]),
                       "rules": cfg_words(runner["cfg"], runner["entry"]),
                       "engine_c": {"is": runner["is"], "oos": runner["oos"]}}
                      if runner else None),
        "top20_by_in_sample_honest": [{"config": cfg_dict(r["cfg"],
                                                          r["entry"]),
                                       "is": r["is"], "oos": r["oos"],
                                       "is_wilson_lb": r["is_wilson"]}
                                      for r in top20],
        "honest_note": honest_note,
    }
    os.makedirs("reports", exist_ok=True)
    path = os.path.join("reports", "chart_backtest_round4.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2,
                  default=lambda o: o.item() if hasattr(o, "item") else str(o))
    log(f"\nwrote {path}")

    def show(name, r):
        if not r:
            log(f"{name}: none")
            return
        log(f"{name}: {cfg_words(r['cfg'], r.get('entry', 'ce'))}")
        log(f"   IS (C): {r['is']['trades']:>4} trades  "
            f"{r['is']['win_rate_pct']}% wr  {r['is']['avg_r']} avgR  "
            f"{r['is']['total_r']} totR")
        log(f"   OOS(C): {r['oos']['trades']:>4} trades  "
            f"{r['oos']['win_rate_pct']}% wr  {r['oos']['avg_r']} avgR  "
            f"{r['oos']['total_r']} totR")

    log("")
    show("BEST (selected under strict engine C)", best)
    log(f"   same config, engine A (legacy):  "
        f"IS {best_a_row['is']['trades']} tr "
        f"{best_a_row['is']['win_rate_pct']}% wr "
        f"{best_a_row['is']['avg_r']} avgR | "
        f"OOS {best_a_row['oos']['trades']} tr "
        f"{best_a_row['oos']['win_rate_pct']}% wr "
        f"{best_a_row['oos']['avg_r']} avgR")
    log(f"   same config, engine B (2m-ordered): "
        f"IS {best_b_hs['is']['trades']} tr "
        f"{best_b_hs['is']['win_rate_pct']}% wr "
        f"{best_b_hs['is']['avg_r']} avgR | "
        f"OOS {best_b_hs['oos']['trades']} tr "
        f"{best_b_hs['oos']['win_rate_pct']}% wr "
        f"{best_b_hs['oos']['avg_r']} avgR")
    log(f"\n--- A->B transition anatomy, BEST config ---")
    for (half, key), c in sorted(best_transition.items()):
        log(f"  {half:<4} {key:<52} {c}")
    log("")
    show("RUNNER-UP", runner)
    log(f"\nhonest note: {honest_note}")
    log(f"\ntotal runtime {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()
