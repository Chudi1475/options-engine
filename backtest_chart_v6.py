"""Walk-forward backtest v6 of the /chart signal — round 6 (round 4 of the
80% grind).

Engine: unchanged from round 5 — MARKET entry at the next 5m open (the only
entry style that survived the round-4 honesty audit; no fill-order ambiguity
by construction). Conservative tie=loss (a 5m bar spanning stop AND target =
LOSS unless real 2m sub-bars prove the target was touched first), stop fills
at the stop, positions never span sessions. The simulator is imported from
backtest_chart_v4 unchanged; data stays FROZEN to the round-4 pickle cache
(2026-07-08), identical split (first 38 days IS / 39 days OOS from
2026-06-01).

STEP 1 (teach yourself): replay the ROUND-5 WINNER (grade A FVG, gap in
[1.1, 3.0) ATR, market entry next open, stop FVG far edge +0.1 ATR, TP 0.4R
all-out, day-eff < 0.85, entries >= 07:00 ET, drop BTC/GBP/Gold, max
1/symbol/day) and dissect EVERY loss: hour, symbol, gap, displacement,
premium/discount, sweep-before-FVG, run-from-open, day regime (efficiency +
range), stop distance (est. and realized), chase beyond CE, bars left in the
session, FVG age, MFE toward the 0.4R target. Decision tables print on the
IN-SAMPLE half only; the replay must reproduce reports/
chart_backtest_round5.json best.trades trade for trade before anything else
runs.

STEP 2 (fix and prove): a grid whose dimensions are chosen ONLY from the
STEP-1 in-sample tables (documented next to the constants below). TP is
FIXED at 0.4r this round (round-5 lesson 5: Wilson-wr ranking mechanically
crowns the tightest TP a grid offers — removing the knob removes the bias;
buf 0.1/0.2 stays, it does not shift the R-breakeven).
ANTI-OVERFIT GATE (non-negotiable, unchanged): configs are ranked ONLY on
the first half of calendar days (Wilson 95% lower bound of win rate, then
avg R). Walking down that ranking, the first config whose untouched second
half holds >= 60% wr with >= 60 trades and avg R > 0 wins. Peek cap 6.
PRE-COMMITTED FINAL RULE (fixed before any OOS number of this round is
computed): the procedural winner replaces the incumbent ONLY if its OOS win
rate strictly beats the incumbent's 77.8 AND its OOS avg R >= 0.05;
otherwise the incumbent stays best. The final best is then re-scored with
real 2m sub-bars (measurement, never selection). Cumulative OOS-look ledger
entering this round: 19.
"""

import json
import os
import pickle
import sys
import time
from datetime import datetime
from itertools import product

import numpy as np

import backtest_chart_v4 as v4

SCRATCH = (r"C:\Users\Chudi\AppData\Local\Temp\claude\C--Users-Chudi"
           r"\57d7f2c7-4e77-45b8-b3fa-04ab441a1a92\scratchpad")
STREAM_PKL = os.path.join(SCRATCH, "r5stream.pkl")

PRIOR_LOOKS = 19        # cumulative OOS-look ledger through round 5
MAX_PEEKS = 6           # hard cap on gate checks this round

TGT_R = 0.4
INC_OOS_WR = 77.8       # published round-5 OOS wr — the bar to beat
MIN_AVG_R = 0.05        # pre-committed OOS avg-R floor for a replacement

# round-5 winner = the incumbent being dissected in STEP 1
INC = {"tp": "0.4r", "buf": 0.1, "gapf": 1.1, "gapc": 3.0, "runc": None,
       "effc": 0.85, "chasec": None, "hours": "ge7", "hcap": None,
       "pdok": False, "swept": False, "unmitr": False, "agec": None,
       "riskc": None, "blmin": None, "efff": None,
       "drop": frozenset({"BTC/USD", "GBP/USD", "Gold"})}

# ---- the v6 grid: dimensions FILLED ONLY FROM THE STEP-1 IS TABLES ----------
# (reports/v6_run_step1.log; every option cites the IS row that motivated it)
TP_OPTS = ["0.4r"]                 # FIXED (round-5 lesson 5)
BUF_OPTS = [0.1, 0.2]
GAP_OPTS = [(1.0, 3.0), (1.1, 3.0)]  # floor 1.0 = trade-count relaxation for
#                                      the EUR-drop variant; cap 3.0 confirmed
RUN_OPTS = [None, 8.0]             # IS run>=8: 50.0% (2/4), totR -1.2
EFF_OPTS = [0.85]                  # confirmed round 5, kept fixed
EFFF_OPTS = [None, 0.15]           # IS day_eff<0.15 (dead chop): 75.0% (6/8)
CHASE_OPTS = [None]                # no IS signal (chase>=2.4 ran 91.7%)
HOUR_OPTS = ["ge7", "ge8"]         # IS hr8-9: 5/7; ge8 was a round-5 dim
HCAP_OPTS = [None]                 # IS hr 18/23: 2/2 wins — no basis
PDOK_OPTS = [False]                # no IS signal (85.7 vs 87.9)
SWEPT_OPTS = [False]               # no IS signal (89.7 no-sweep vs 84.6)
UNMIT_OPTS = [False]               # no IS signal (88.2 vs 86.3)
AGE_OPTS = [None]                  # no IS signal
RISK_OPTS = [None, 3.6]            # IS realized stop >=3.6 ATR: 71.4% (5/7),
#                                    totR 0.00 (filter uses est. decision-time
#                                    risk, same 3.6 printed bucket edge)
BL_OPTS = [None]                   # no IS signal (>=100 bars left ran WORSE)
DROP_OPTS = [
    frozenset({"BTC/USD", "GBP/USD", "Gold"}),             # incumbent
    frozenset({"BTC/USD", "GBP/USD", "Gold", "EUR/USD"}),  # IS 75.0% (12/16)
]
PERDAY = 1

MGMTS = list(product(TP_OPTS, BUF_OPTS))
MGMT_IDX = {m: k for k, m in enumerate(MGMTS)}
IS_GATE = {"min_trades": 60, "min_wr": 60.0}
OOS_GATE = {"min_trades": 60, "min_wr": 60.0}


def log(m):
    print(m, flush=True)


def load_stream():
    log("loading pickled stream (frozen r4cache data) ...")
    with open(STREAM_PKL, "rb") as f:
        return pickle.load(f)


def cfg_key(cfg):
    return tuple(cfg[k] for k in
                 ("tp", "buf", "gapf", "gapc", "runc", "effc", "chasec",
                  "hours", "hcap", "pdok", "swept", "unmitr", "agec",
                  "riskc", "blmin", "efff", "drop"))


def cfg_dict(cfg):
    return {"fvg_grade": "A", "entry": "mkt", "stop": "fvg",
            "stop_buffer_atr": cfg["buf"], "tp": cfg["tp"],
            "min_gap_atr": cfg["gapf"], "max_gap_atr": cfg["gapc"],
            "max_run_from_open_atr": cfg["runc"],
            "max_day_efficiency": cfg["effc"],
            "min_day_efficiency": cfg["efff"],
            "max_chase_atr_beyond_ce": cfg["chasec"],
            "hours": cfg["hours"], "max_entry_hour": cfg["hcap"],
            "pd_ok_required": cfg["pdok"],
            "sweep_required": cfg["swept"],
            "unmitigated_required": cfg["unmitr"],
            "max_fvg_age_bars": cfg["agec"],
            "max_est_risk_atr": cfg["riskc"],
            "min_bars_left": cfg["blmin"],
            "drop_symbols": sorted(cfg["drop"]),
            "max_per_symbol_day": PERDAY}


def cfg_words(cfg):
    bits = ["grade A FVG only",
            "market entry at next 5m open (no retrace wait)",
            f"stop beyond FVG far edge +{cfg['buf']} ATR",
            f"TP {cfg['tp'][:-1]}R all-out",
            f"FVG gap >= {cfg['gapf']} ATR"]
    if cfg["gapc"] is not None:
        bits.append(f"gap < {cfg['gapc']} ATR (cap)")
    if cfg["runc"] is not None:
        bits.append(f"skip if run-from-open >= {cfg['runc']} ATR")
    if cfg["effc"] is not None:
        bits.append(f"skip if day efficiency >= {cfg['effc']}")
    if cfg["efff"] is not None:
        bits.append(f"skip if day efficiency < {cfg['efff']}")
    if cfg["chasec"] is not None:
        bits.append(f"skip if price chased >= {cfg['chasec']} ATR beyond CE")
    bits.append("all hours" if cfg["hours"] == "all"
                else f"entries only {cfg['hours'][2:]}:00 ET or later")
    if cfg["hcap"] is not None:
        bits.append(f"no entries after {cfg['hcap']}:59 ET")
    if cfg["pdok"]:
        bits.append("CE in discount (buys) / premium (sells) required")
    if cfg["swept"]:
        bits.append("liquidity sweep before the FVG required")
    if cfg["unmitr"]:
        bits.append("unmitigated FVG only")
    if cfg["agec"] is not None:
        bits.append(f"FVG age <= {cfg['agec']} bars")
    if cfg["riskc"] is not None:
        bits.append(f"skip if est. stop distance >= {cfg['riskc']} ATR")
    if cfg["blmin"] is not None:
        bits.append(f">= {cfg['blmin']} bars left in the session")
    if cfg["drop"]:
        bits.append("dropped: " + ", ".join(sorted(cfg["drop"])))
    bits.append(f"max {PERDAY} trade/symbol/day")
    bits.append("6-bar cooldown")
    return "; ".join(bits)


def passes(cfg, s, f):
    if s["grade"] != "A":
        return False
    if s["gap_atr"] < cfg["gapf"]:
        return False
    if cfg["gapc"] is not None and s["gap_atr"] >= cfg["gapc"]:
        return False
    if cfg["runc"] is not None and s["run_atr"] >= cfg["runc"]:
        return False
    if cfg["effc"] is not None and s["day_eff"] >= cfg["effc"]:
        return False
    if cfg["efff"] is not None and s["day_eff"] < cfg["efff"]:
        return False
    if cfg["chasec"] is not None and f["chase"] >= cfg["chasec"]:
        return False
    if cfg["hours"] == "ge7" and s["hour"] < 7:
        return False
    if cfg["hours"] == "ge8" and s["hour"] < 8:
        return False
    if cfg["hcap"] is not None and s["hour"] > cfg["hcap"]:
        return False
    if cfg["pdok"] and not s["pd_ok"]:
        return False
    if cfg["swept"] and not s["swept"]:
        return False
    if cfg["unmitr"] and not s["unmit"]:
        return False
    if cfg["agec"] is not None and s["fvg_age"] > cfg["agec"]:
        return False
    if cfg["riskc"] is not None and f["est_risk"] >= cfg["riskc"]:
        return False
    if cfg["blmin"] is not None and f["bars_left"] < cfg["blmin"]:
        return False
    if s["sym"] in cfg["drop"]:
        return False
    return True


def replay(cfg, signals, feats, outc_m):
    """Serial replay honoring cooldown + perday, one config."""
    m = MGMT_IDX[(cfg["tp"], cfg["buf"])]
    trades = []
    cur_g, free_at, taken = -1, -1, 0
    for k, s in enumerate(signals):
        o = outc_m[m][k]
        if not o["valid"]:
            continue
        if not passes(cfg, s, feats[k]):
            continue
        g = s["gid"]
        if g != cur_g:
            cur_g, free_at, taken = g, -1, 0
        if s["i"] < free_at:
            continue
        if taken >= PERDAY:
            continue
        trades.append({"sig_k": k, "symbol": s["sym"], "day": s["day"],
                       "time": s["time"], "direction": s["dir"],
                       "hour": s["hour"], "dow": s["dow"], "weak": s["weak"],
                       "run_atr": round(s["run_atr"], 2),
                       "gap_atr": round(s["gap_atr"], 2),
                       "disp_atr": round(s["disp_atr"], 2),
                       "pd_zone": s["pd_zone"], "pd_ok": s["pd_ok"],
                       "swept": s["swept"], "unmit": s["unmit"],
                       "fvg_age": s["fvg_age"], "day_eff": s["day_eff"],
                       "range_atr": s["range_atr"],
                       "chase_atr": feats[k]["chase"],
                       "est_risk_atr": round(feats[k]["est_risk"], 2),
                       "bars_left": feats[k]["bars_left"],
                       "risk_atr": round(o["risk_atr"], 2),
                       "mfe_r": o["mfe_r"], "tie": o["tie"],
                       "r": round(o["r"], 3), "exit": o["tag"]})
        taken += 1
        free_at = o["exit_abs"] + 1 + v4.COOLDOWN_BARS
    return trades


def bucket_table(trades, name, keyfn):
    rows = {}
    for t in trades:
        b = keyfn(t)
        w, n, r = rows.get(b, (0, 0, 0.0))
        rows[b] = (w + (1 if t["r"] > 0 else 0), n + 1, r + t["r"])
    log(f"  {name:<38} {'wr%':>6} {'w/n':>9} {'totR':>7}")
    for b in sorted(rows, key=lambda x: str(x)):
        w, n, r = rows[b]
        log(f"    {str(b):<36} {100.0 * w / n:6.1f} {w:>4}/{n:<4} {r:>7.2f}")


def build_feats(signals, bars):
    feats = []
    for s in signals:
        H, L, C, O, _sub = bars[s["gid"]]
        i, n = s["i"], s["n"]
        buy = s["dir"] == "BUY"
        sgn = 1.0 if buy else -1.0
        price = float(C[i])
        stop_est = (s["fvg_bottom"] - 0.1 * s["atr"]) if buy \
            else (s["fvg_top"] + 0.1 * s["atr"])
        feats.append({"chase": sgn * (price - s["ce"]) / s["atr"],
                      "est_risk": sgn * (price - stop_est) / s["atr"],
                      "bars_left": int(n - 1 - (i + 1))})
    return feats


def step1(signals, bars, feats, outc_m, is_days):
    inc_trades = replay(INC, signals, feats, outc_m)
    r5 = json.load(open("reports/chart_backtest_round5.json"))
    ref = {(t["symbol"], t["time"], t["direction"]): round(t["r"], 3)
           for t in r5["best"]["trades"]}
    mine = {(t["symbol"], t["time"], t["direction"]): round(t["r"], 3)
            for t in inc_trades}
    assert mine == ref, "incumbent replay does not reproduce round-5 trades"
    inc_hs = v4.half_stats(inc_trades, is_days)
    log(f"\nSTEP 1: incumbent (round-5 winner) reproduces round 5 exactly "
        f"({len(inc_trades)} trades)")
    log(f"  IS  {inc_hs['is']['trades']} tr {inc_hs['is']['win_rate_pct']}% "
        f"{inc_hs['is']['avg_r']} avgR | OOS {inc_hs['oos']['trades']} tr "
        f"{inc_hs['oos']['win_rate_pct']}% {inc_hs['oos']['avg_r']} avgR")

    losses = [t for t in inc_trades if t["r"] <= 0]
    log(f"\n--- every loss ({len(losses)}), both halves labeled ---")
    log(f"{'half':<4} {'symbol':<8} {'time':<26} {'dir':<4} {'dow':<3} "
        f"{'hr':>2} {'gap':>5} {'disp':>5} {'pd':<11} {'swp':>3} {'unm':>3} "
        f"{'age':>3} {'run':>6} {'eff':>5} {'rng':>5} {'chase':>5} "
        f"{'risk':>5} {'bl':>3} {'mfe':>5} {'%tgt':>4} {'exit':<5}")
    for t in losses:
        half = "is" if t["day"] in is_days else "oos"
        log(f"{half:<4} {t['symbol']:<8} {t['time']:<26} {t['direction']:<4} "
            f"{t['dow']:<3} {t['hour']:>2} {t['gap_atr']:>5.2f} "
            f"{t['disp_atr']:>5.2f} {t['pd_zone']:<11} "
            f"{'Y' if t['swept'] else '.':>3} {'Y' if t['unmit'] else '.':>3} "
            f"{t['fvg_age']:>3} {t['run_atr']:>6.2f} {t['day_eff']:>5.2f} "
            f"{t['range_atr']:>5.2f} {t['chase_atr']:>5.2f} "
            f"{t['risk_atr']:>5.2f} {t['bars_left']:>3} {t['mfe_r']:>5.2f} "
            f"{100 * t['mfe_r'] / TGT_R:>3.0f}% {t['exit']:<5}")

    is_tr = [t for t in inc_trades if t["day"] in is_days]
    log(f"\n--- IN-SAMPLE decision tables ({len(is_tr)} trades; OOS untouched "
        f"by rule-building) ---")
    bucket_table(is_tr, "symbol", lambda t: t["symbol"])
    bucket_table(is_tr, "hour (ET)", lambda t: t["hour"])
    bucket_table(is_tr, "day of week", lambda t: t["dow"])
    bucket_table(is_tr, "gap_atr bucket",
                 lambda t: "1.1-1.4" if t["gap_atr"] < 1.4 else
                           "1.4-1.7" if t["gap_atr"] < 1.7 else
                           "1.7-2.2" if t["gap_atr"] < 2.2 else "2.2-3.0")
    bucket_table(is_tr, "displacement bucket",
                 lambda t: "<1.3" if t["disp_atr"] < 1.3 else
                           "1.3-1.8" if t["disp_atr"] < 1.8 else
                           "1.8-2.5" if t["disp_atr"] < 2.5 else ">=2.5")
    bucket_table(is_tr, "pd zone", lambda t: t["pd_zone"])
    bucket_table(is_tr, "pd location ok", lambda t: t["pd_ok"])
    bucket_table(is_tr, "liquidity sweep before FVG", lambda t: t["swept"])
    bucket_table(is_tr, "FVG unmitigated at signal", lambda t: t["unmit"])
    bucket_table(is_tr, "fvg age (bars)",
                 lambda t: "1" if t["fvg_age"] <= 1 else
                           "2-3" if t["fvg_age"] <= 3 else ">3")
    bucket_table(is_tr, "run-from-open (ATR)",
                 lambda t: "<0 counter" if t["run_atr"] < 0 else
                           "0-4" if t["run_atr"] < 4 else
                           "4-8" if t["run_atr"] < 8 else ">=8")
    bucket_table(is_tr, "day efficiency",
                 lambda t: "<0.15" if t["day_eff"] < 0.15 else
                           "0.15-0.45" if t["day_eff"] < 0.45 else
                           "0.45-0.65" if t["day_eff"] < 0.65 else ">=0.65")
    bucket_table(is_tr, "day range so far (ATR)",
                 lambda t: "<6" if t["range_atr"] < 6 else
                           "6-10" if t["range_atr"] < 10 else ">=10")
    bucket_table(is_tr, "chase beyond CE (ATR)",
                 lambda t: "<1.0" if t["chase_atr"] < 1.0 else
                           "1.0-1.6" if t["chase_atr"] < 1.6 else
                           "1.6-2.4" if t["chase_atr"] < 2.4 else ">=2.4")
    bucket_table(is_tr, "realized stop distance (ATR)",
                 lambda t: "<1.8" if t["risk_atr"] < 1.8 else
                           "1.8-2.6" if t["risk_atr"] < 2.6 else
                           "2.6-3.6" if t["risk_atr"] < 3.6 else ">=3.6")
    bucket_table(is_tr, "bars left in session",
                 lambda t: "<40" if t["bars_left"] < 40 else
                           "40-100" if t["bars_left"] < 100 else ">=100")
    bucket_table(is_tr, "direction", lambda t: t["direction"])
    bucket_table(is_tr, "weak-bias signal", lambda t: t["weak"])

    is_losses = [t for t in is_tr if t["r"] <= 0]
    log(f"\n  IS losses: {len(is_losses)} of {len(is_tr)}; ties "
        f"{sum(1 for t in is_losses if t['tie'])}; "
        f"eod exits {sum(1 for t in is_losses if t['exit'] == 'eod')}")
    if is_losses:
        log(f"  IS-loss MFE toward the 0.4R target: "
            f"{sorted(round(100 * t['mfe_r'] / TGT_R) for t in is_losses)} "
            f"(% of target)")
    return inc_trades, inc_hs, losses


def main():
    t_start = time.time()
    signals, bars, all_days, sub_cov = load_stream()
    log(f"{len(signals)} signals across {len(all_days)} days")
    mid = len(all_days) // 2
    is_days = set(all_days[:mid])
    split_date = all_days[mid]
    oos_flag = np.array([s["day"] not in is_days for s in signals], dtype=bool)
    log(f"split: {mid} IS days | {len(all_days) - mid} OOS days "
        f"(from {split_date}) — identical to rounds 4-5")

    feats = build_feats(signals, bars)

    log(f"precomputing {len(MGMTS)} market-entry outcomes per signal ...")
    outc_m = [[] for _ in MGMTS]
    for s in signals:
        H, L, C, O, _sub = bars[s["gid"]]
        for mi, (tp, buf) in enumerate(MGMTS):
            outc_m[mi].append(v4.simulate_mkt(s, H, L, C, O, tp, buf))

    inc_trades, inc_hs, losses = step1(signals, bars, feats, outc_m, is_days)

    if len(sys.argv) > 1 and sys.argv[1] == "step1":
        log(f"\nstep1-only run done in {time.time() - t_start:.0f}s")
        return

    # ---- STEP 2: sweep (IS-only ranking) -------------------------------------
    ns = len(signals)
    gradeA = np.array([s["grade"] == "A" for s in signals], dtype=bool)
    gap_a = np.array([s["gap_atr"] for s in signals])
    run_a = np.array([s["run_atr"] for s in signals])
    eff_a = np.array([s["day_eff"] for s in signals])
    hour_a = np.array([s["hour"] for s in signals])
    risk_a = np.array([f["est_risk"] for f in feats])
    sym_a = np.array([s["sym"] for s in signals])
    gid_a = np.array([s["gid"] for s in signals], dtype=np.int64)
    i_a = np.array([s["i"] for s in signals], dtype=np.int64)

    hour_masks = {"ge7": hour_a >= 7, "ge8": hour_a >= 8}
    drop_masks = {d: ~np.isin(sym_a, sorted(d)) for d in DROP_OPTS}
    gap_masks = {(gf, gc): (gap_a >= gf) & (gap_a < gc) for gf, gc in GAP_OPTS}
    run_masks = {rc: np.ones(ns, dtype=bool) if rc is None else (run_a < rc)
                 for rc in RUN_OPTS}
    efff_masks = {ef: np.ones(ns, dtype=bool) if ef is None else (eff_a >= ef)
                  for ef in EFFF_OPTS}
    risk_masks = {rk: np.ones(ns, dtype=bool) if rk is None else (risk_a < rk)
                  for rk in RISK_OPTS}
    effc_mask = eff_a < EFF_OPTS[0]

    valid_m, r_m, exit_m = [], [], []
    for mi in range(len(MGMTS)):
        valid_m.append(np.array([o["valid"] for o in outc_m[mi]], dtype=bool))
        r_m.append(np.array([o["r"] for o in outc_m[mi]]))
        exit_m.append(np.array([o["exit_abs"] for o in outc_m[mi]],
                               dtype=np.int64))

    grid = list(product(TP_OPTS, BUF_OPTS, GAP_OPTS, RUN_OPTS, EFFF_OPTS,
                        RISK_OPTS, HOUR_OPTS, DROP_OPTS))
    log(f"\nSTEP 2: sweeping {len(grid)} configs (IS-only ranking; TP fixed "
        f"0.4r, effc fixed 0.85, gap cap fixed 3.0) ...")
    rows = []
    for tp, buf, (gf, gc), rc, ef, rk, hrs, drop in grid:
        mi = MGMT_IDX[(tp, buf)]
        mask = (valid_m[mi] & gradeA & gap_masks[(gf, gc)] & run_masks[rc]
                & effc_mask & efff_masks[ef] & risk_masks[rk]
                & hour_masks[hrs] & drop_masks[drop])
        idx = np.nonzero(mask)[0]
        rr, ex = r_m[mi], exit_m[mi]
        cur_g, free_at, taken = -1, -1, 0
        is_w = is_n = oos_w = oos_n = 0
        is_r = oos_r = 0.0
        for k in idx:
            g = gid_a[k]
            if g != cur_g:
                cur_g, free_at, taken = g, -1, 0
            if i_a[k] < free_at:
                continue
            if taken >= PERDAY:
                continue
            r = rr[k]
            if oos_flag[k]:
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
            free_at = ex[k] + 1 + v4.COOLDOWN_BARS
        cfg = dict(INC, tp=tp, buf=buf, gapf=gf, gapc=gc, runc=rc, efff=ef,
                   riskc=rk, hours=hrs, drop=drop)
        rows.append({"cfg": cfg, "is": v4.stats(is_w, is_n, is_r),
                     "oos": v4.stats(oos_w, oos_n, oos_r),
                     "is_wilson": round(100 * v4.wilson_lb(is_w, is_n), 2)})

    inc_row = next(r for r in rows if cfg_key(r["cfg"]) == cfg_key(INC))
    assert inc_row["is"] == inc_hs["is"] and inc_row["oos"] == inc_hs["oos"], \
        "sweep cell for the incumbent disagrees with the replay"

    is_pass = [r for r in rows
               if r["is"]["trades"] >= IS_GATE["min_trades"]
               and r["is"]["win_rate_pct"] is not None
               and r["is"]["win_rate_pct"] >= IS_GATE["min_wr"]
               and r["is"]["avg_r"] is not None and r["is"]["avg_r"] > 0]
    is_pass.sort(key=lambda r: (-r["is_wilson"], -(r["is"]["avg_r"] or 0)))
    log(f"{len(is_pass)} of {len(rows)} configs pass the IS gate "
        f"(>=60 tr, >=60% wr, avg R>0)")
    inc_rank = next((k for k, r in enumerate(is_pass)
                     if cfg_key(r["cfg"]) == cfg_key(INC)), None)
    log(f"incumbent IS rank: "
        f"{inc_rank + 1 if inc_rank is not None else 'not passing'} "
        f"(wilson {inc_row['is_wilson']})")

    log("\ntop 15 by IS Wilson LB (IS numbers only — no OOS shown here):")
    for r in is_pass[:15]:
        log(f"  IS {r['is']['trades']:>3} tr {r['is']['win_rate_pct']:>5}% "
            f"{r['is']['avg_r']:>6} avgR wilson {r['is_wilson']:>5} | "
            f"{cfg_words(r['cfg'])}")

    # ---- OOS gate walk (every check counted; incumbent row costs no peek —
    #      its OOS numbers are already published in round 5) ------------------
    best = runner = None
    peeks = 0
    checked = []
    for r in is_pass:
        is_inc_row = cfg_key(r["cfg"]) == cfg_key(INC)
        if not is_inc_row:
            if peeks >= MAX_PEEKS:
                break
            peeks += 1
        o = r["oos"]
        holds = (o["trades"] >= OOS_GATE["min_trades"]
                 and o["win_rate_pct"] is not None
                 and o["win_rate_pct"] >= OOS_GATE["min_wr"]
                 and o["avg_r"] is not None and o["avg_r"] > 0)
        checked.append({"config": cfg_dict(r["cfg"]), "is": r["is"],
                        "oos": o, "held": holds,
                        "counted_peek": not is_inc_row})
        log(f"  {'inc ' if is_inc_row else f'peek {peeks}'}: OOS "
            f"{o['trades']} tr {o['win_rate_pct']}% {o['avg_r']} avgR -> "
            f"{'HOLDS' if holds else 'fails'} | {cfg_words(r['cfg'])}")
        if holds:
            if best is None:
                best = r
            elif runner is None:
                runner = r
                break

    if best is None:
        log("\nNO config held the OOS gate within the peek budget — the "
            "incumbent (already-published numbers, no new look) stays best.")
        best = inc_row

    # ---- PRE-COMMITTED FINAL RULE (fixed before this round's OOS numbers
    #      were computed, see module docstring): the procedural winner
    #      replaces the incumbent ONLY if OOS wr > 77.8 AND OOS avg R >= 0.05.
    proc = best
    if cfg_key(proc["cfg"]) == cfg_key(INC):
        final, final_name = inc_row, "incumbent"
        verdict = "the incumbent is itself the procedural winner"
    elif (proc["oos"]["win_rate_pct"] is not None
          and proc["oos"]["win_rate_pct"] > INC_OOS_WR
          and proc["oos"]["avg_r"] is not None
          and proc["oos"]["avg_r"] >= MIN_AVG_R):
        final, final_name = proc, "procedural_winner"
        verdict = (f"beats the incumbent's published {INC_OOS_WR}% OOS wr "
                   f"with avg R >= {MIN_AVG_R}")
    else:
        final, final_name = inc_row, "incumbent"
        verdict = (f"procedural winner did not beat the pre-committed bar "
                   f"(OOS wr > {INC_OOS_WR} and avg R >= {MIN_AVG_R}); "
                   f"incumbent stays")
    log(f"\npre-committed rule selects: {final_name} ({verdict})")

    # ---- 2m-verified measurement of the final best (never selection) --------
    def rescore_2m(cfg):
        tr5 = replay(cfg, signals, feats, outc_m)
        tr2, chg = [], 0
        for t in tr5:
            s = signals[t["sig_k"]]
            H, L, C, O, sub = bars[s["gid"]]
            ob = v4.simulate_mkt(s, H, L, C, O, cfg["tp"], cfg["buf"],
                                 sub=sub)
            if round(ob["r"], 3) != t["r"]:
                chg += 1
            tr2.append(dict(t, r=round(ob["r"], 3), exit=ob["tag"]))
        return tr2, chg

    new_meas = 0
    best_trades = replay(final["cfg"], signals, feats, outc_m)
    tr2_final, chg_final = rescore_2m(final["cfg"])
    hs2 = v4.half_stats(tr2_final, is_days)
    if cfg_key(final["cfg"]) != cfg_key(INC):
        new_meas += 1        # a genuinely new fixed-config 2m look
    log(f"2m-verified rescore of the final best: {chg_final} verdicts change "
        f"of {len(tr2_final)}")
    log(f"final best 2m-verified: IS {hs2['is']['trades']} tr "
        f"{hs2['is']['win_rate_pct']}% {hs2['is']['avg_r']} avgR | "
        f"OOS {hs2['oos']['trades']} tr {hs2['oos']['win_rate_pct']}% "
        f"{hs2['oos']['avg_r']} avgR")

    total_looks = PRIOR_LOOKS + peeks + new_meas
    honest_note = (
        f"Selection used ONLY the first half (market-entry engine, "
        f"conservative tie=loss); TP fixed at 0.4r a priori so Wilson "
        f"ranking cannot crown a tighter target. {peeks} OOS gate checks "
        f"this round + {new_meas} fixed-config looks. Cumulative OOS-look "
        f"ledger: {PRIOR_LOOKS} through round 5 + {peeks + new_meas} this "
        f"round = {total_looks} total looks. Related configs in a "
        f"{len(rows)}-point grid share information; treat OOS numbers as an "
        f"upper bound of true edge.")

    is_fail = {
        "run_ge_8": [t for t in inc_trades if t["day"] in is_days
                     and t["run_atr"] >= 8],
        "eff_lt_0.15": [t for t in inc_trades if t["day"] in is_days
                        and t["day_eff"] < 0.15],
        "risk_ge_3.6": [t for t in inc_trades if t["day"] in is_days
                        and t["risk_atr"] >= 3.6],
        "hour_8_9": [t for t in inc_trades if t["day"] in is_days
                     and t["hour"] in (8, 9)],
        "eur_usd": [t for t in inc_trades if t["day"] in is_days
                    and t["symbol"] == "EUR/USD"],
    }
    fail_patterns = {
        k: v4.stats(sum(1 for t in v if t["r"] > 0), len(v),
                    sum(t["r"] for t in v))
        for k, v in is_fail.items()}

    report = {
        "round": 6,
        "run_date": str(datetime.now().date()),
        "method": {
            "engine": "market entry at next 5m open only (round-4 honest "
                      "survivor; no fill-order ambiguity). Walk-forward, "
                      "production market_tools+fvg logic, no lookahead, "
                      "conservative tie=loss (bar spanning stop and target = "
                      "LOSS), stop fills at stop, positions never span "
                      "sessions. Simulator imported from backtest_chart_v4 "
                      "unchanged; incumbent replay reproduces the round-5 "
                      "report trade for trade before anything else runs.",
            "grid": "TP FIXED 0.4r x buf 0.1/0.2 x gap floor 1.0/1.1 (cap "
                    "3.0 fixed) x run cap none/8 x day-eff floor none/0.15 "
                    "(cap 0.85 fixed) x est-risk cap none/3.6 x hours "
                    "ge7/ge8 x drop-sets {inc, inc+EUR/USD}; perday fixed 1",
            "split": f"first {mid} calendar days = in-sample, remaining "
                     f"{len(all_days) - mid} days (from {split_date}) = "
                     "out-of-sample, untouched by selection and the "
                     "dissection tables",
            "selection": "rank IS-passing configs by Wilson 95% lower bound "
                         "of win rate then avg R; walk down checking the OOS "
                         "gate (>=60% wr, >=60 trades, avg R > 0); peek cap "
                         f"{MAX_PEEKS}; PRE-COMMITTED final rule: the "
                         f"procedural winner replaces the incumbent only if "
                         f"OOS wr > {INC_OOS_WR} and OOS avg R >= "
                         f"{MIN_AVG_R}",
            "cooldown_bars": v4.COOLDOWN_BARS,
            "skip_first_bars": v4.SKIP_FIRST_BARS,
            "data_frozen": "identical round-4 pickle cache (yfinance 5m+2m, "
                           "frozen 2026-07-08)",
        },
        "data": {"symbols": [s_[0] for s_ in v4.SYMBOLS],
                 "days_total": len(all_days), "is_days": mid,
                 "oos_days": len(all_days) - mid, "split_date": split_date,
                 "n_signals": len(signals)},
        "grid_size": len(rows),
        "incumbent": {
            "config": cfg_dict(INC), "rules": cfg_words(INC),
            "is": inc_row["is"], "oos": inc_row["oos"],
            "reproduces_round5_exactly": True,
            "is_rank_in_new_grid": (inc_rank + 1) if inc_rank is not None
            else None,
            "losses": [{k2: v for k2, v in t.items() if k2 != "sig_k"}
                       for t in losses],
            "is_failure_patterns": fail_patterns},
        "selection": {"is_gate": IS_GATE, "oos_gate": OOS_GATE,
                      "n_is_passing": len(is_pass), "oos_peeks": peeks,
                      "new_fixed_config_looks": new_meas,
                      "prior_looks": PRIOR_LOOKS,
                      "total_looks": total_looks,
                      "gate_walk": checked,
                      "final_rule": "pre-committed before any OOS number of "
                      "this round was computed: procedural winner replaces "
                      f"the incumbent only if OOS wr > {INC_OOS_WR} and OOS "
                      f"avg R >= {MIN_AVG_R}; TP fixed 0.4r a priori"},
        "procedural_winner": {"config": cfg_dict(proc["cfg"]),
                              "rules": cfg_words(proc["cfg"]),
                              "is": proc["is"], "oos": proc["oos"],
                              "verdict": verdict},
        "best": {"config": cfg_dict(final["cfg"]),
                 "rules": cfg_words(final["cfg"]),
                 "selected_as": final_name,
                 "is": final["is"], "oos": final["oos"],
                 "is_incumbent": cfg_key(final["cfg"]) == cfg_key(INC),
                 "trades_2m_rescore_changes": chg_final,
                 "oos_2m_verified": hs2["oos"],
                 "trades": [{k2: v for k2, v in t.items() if k2 != "sig_k"}
                            for t in best_trades]},
        "runner_up": ({"config": cfg_dict(runner["cfg"]),
                       "rules": cfg_words(runner["cfg"]),
                       "is": runner["is"], "oos": runner["oos"]}
                      if runner else None),
        "top20_by_in_sample": [{"config": cfg_dict(r["cfg"]), "is": r["is"],
                                "oos": r["oos"],
                                "is_wilson_lb": r["is_wilson"]}
                               for r in is_pass[:20]],
        "honest_note": honest_note,
    }
    os.makedirs("reports", exist_ok=True)
    path = os.path.join("reports", "chart_backtest_round6.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2,
                  default=lambda o: o.item() if hasattr(o, "item") else str(o))
    log(f"\nwrote {path}")

    log(f"\nBEST ({final_name}): {cfg_words(final['cfg'])}")
    log(f"  IS  {final['is']['trades']} tr {final['is']['win_rate_pct']}% wr "
        f"{final['is']['avg_r']} avgR {final['is']['total_r']} totR")
    log(f"  OOS {final['oos']['trades']} tr {final['oos']['win_rate_pct']}% "
        f"wr {final['oos']['avg_r']} avgR {final['oos']['total_r']} totR")
    if runner:
        log(f"RUNNER-UP: {cfg_words(runner['cfg'])}")
        log(f"  IS  {runner['is']['trades']} tr "
            f"{runner['is']['win_rate_pct']}% wr {runner['is']['avg_r']} avgR")
        log(f"  OOS {runner['oos']['trades']} tr "
            f"{runner['oos']['win_rate_pct']}% wr {runner['oos']['avg_r']} avgR")
    log(f"\nhonest note: {honest_note}")
    log(f"total runtime {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()
