"""EXPERIMENT: ADAPTIVE GIVE-BACK (regime-conditioned runner exit).

Question: does a give-back trail that widens on strong-trend days (50-60) and
tightens on chop days (25-30) beat the flat give-back-40 the bot runs live?

Day-type measure (no lookahead, computable at trade time):
    ER = |last_close - today_open| / total_absolute_path_traveled
computed on the TRADE'S OWN ticker's 5m bars for the current day, using only
bars at or before the decision bar. ER near 1 = clean trend; near 0 = chop.
Two flavors:
  - ENTRY-static: ER frozen at the entry bar, one give-back for the trade.
  - DYNamic: ER re-read every bar while managing the runner (still only past
    bars), so a day that turns trendy widens the trail mid-trade.

Everything else identical to the live exit engine: sell HALF at +25%, hard
stop -70%, trail arms only once peak >= +25%, time stop at expiry. Entries
are the exact live-scanner entries (StrategyConfig, direction="both") on the
allow-list, generated ONCE so all policies see identical trades.

Honesty: approximated Black-Scholes pricing (optimistic for 0DTE), 1.5%
slippage each way + fees, ~60d window, partly in-sample. Trust RELATIVE
ranking. All variants share the same -70 stop, so sizing-fair account return
= expectancy% * RISK / 70 — proportional to expectancy for every row.

Writes bt_exp_adaptive_giveback.json (repo root, NOT reports/).

Usage:
    python bt_exp_adaptive_giveback.py
"""

import json
import sys
import time as _time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

import config
from backtest import (CONTRACTS, ET, SLIPPAGE, BtTrade, bs_price, load_data,
                      metrics, years_to_expiry)
from backtest_runner_trail import ALLOWED, collect_entries, simulate_giveback
from strategy import StrategyConfig

HALF = config.TP_HALF_PCT        # +25
STOP = config.STOP_PCT           # -70
ARM = config.TP_HALF_PCT         # trail arms once peak >= +25 (same as live)
BASE_GIVE = config.RUNNER_GIVEBACK_PCT   # 40 — the flat baseline
FALLBACK_GIVE = BASE_GIVE        # when ER unreadable (<3 bars), act like live

OUT_JSON = Path(__file__).parent / "bt_exp_adaptive_giveback.json"


def load_with_retry(cfg, tries=4):
    """backtest.load_data, but retry each ticker if yfinance throttles."""
    intraday, daily = {}, {}
    for ticker, yfs in cfg.watchlist.items():
        i5 = d1 = None
        for attempt in range(tries):
            try:
                i5 = yf.download(yfs, period="60d", interval="5m",
                                 progress=False, auto_adjust=False)
                d1 = yf.download(yfs, period="1y", interval="1d",
                                 progress=False, auto_adjust=False)
            except Exception as exc:  # noqa: BLE001 — throttle/network, retry
                print(f"  {ticker}: download error ({exc}), retrying...")
                i5 = d1 = None
            if i5 is not None and len(i5) and d1 is not None and len(d1):
                break
            _time.sleep(5 * (attempt + 1))
        if i5 is None or not len(i5) or d1 is None or not len(d1):
            raise RuntimeError(f"could not download data for {ticker}")
        for df in (i5, d1):
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
        i5.index = i5.index.tz_convert(ET)
        intraday[ticker], daily[ticker] = i5, d1
        print(f"  {ticker}: {len(i5)} 5m bars "
              f"({i5.index[0].date()} -> {i5.index[-1].date()})")
    return intraday, daily


# ---------------------------------------------------------------- day-type ER
_ER_MIN_BARS = 3  # need >=3 completed bars for ER to mean anything


def _er_day_arrays(day_bars):
    """er[i] = |close_i - day_open| / path_traveled_through_close_i."""
    opens = day_bars["Open"].to_numpy(dtype=float)
    closes = day_bars["Close"].to_numpy(dtype=float)
    prices = np.concatenate(([opens[0]], closes))
    path = np.cumsum(np.abs(np.diff(prices)))
    net = np.abs(closes - opens[0])
    with np.errstate(divide="ignore", invalid="ignore"):
        er = np.where(path > 0, net / path, 0.0)
    return er


class ErLookup:
    """ER of a ticker's day at any bar timestamp, past bars only, cached."""

    def __init__(self, intraday):
        self.intraday = intraday
        self.cache = {}

    def at(self, ticker, ts):
        key = (ticker, ts.date())
        if key not in self.cache:
            day_bars = self.intraday[ticker][
                self.intraday[ticker].index.date == ts.date()]
            self.cache[key] = (day_bars.index, _er_day_arrays(day_bars))
        index, er = self.cache[key]
        i = index.get_indexer([ts])[0]
        if i < _ER_MIN_BARS - 1:
            return None
        return float(er[i])


# ------------------------------------------------- path precompute + policies
def build_paths(entries, er_lookup):
    """Premium path + per-bar ER for each allow-list entry, computed ONCE.
    Every policy afterwards is a cheap scan over the same floats."""
    paths = []
    for e in entries:
        if (e["ticker"], e["right"]) not in ALLOWED:
            continue
        bars_all = e["bars_all"]
        future = bars_all[(bars_all.index > e["entry_ts"])
                          & (bars_all.index <= pd.Timestamp(e["expiry"]))]
        steps = []
        for ts, row in future.iterrows():
            prem_net = bs_price(
                float(row["Close"]), e["strike"],
                years_to_expiry(ts.to_pydatetime(), e["expiry"]),
                e["sigma"], e["right"]) * (1 - SLIPPAGE)
            ret = (prem_net / e["entry_prem"] - 1) * 100
            steps.append((ts, prem_net, ret, er_lookup.at(e["ticker"], ts)))
        paths.append({
            "ticker": e["ticker"], "right": e["right"], "strike": e["strike"],
            "now": e["now"], "entry_prem": e["entry_prem"],
            "expiry": e["expiry"], "steps": steps,
            "er_entry": er_lookup.at(e["ticker"], e["entry_ts"]),
        })
    return paths


def sim_policy(p, give_fn):
    """Live exit engine with a policy-supplied give-back.
    give_fn(er_entry, er_now) -> give-back points at this bar."""
    legs, remaining, half_taken, last, peak = [], CONTRACTS, False, None, None
    for ts, prem_net, ret, er_now in p["steps"]:
        last = (ts, prem_net)
        peak = ret if peak is None else max(peak, ret)
        if ret <= STOP:
            legs.append((ts, prem_net, remaining, f"stop {STOP:g}%"))
            return legs
        if not half_taken and ret >= HALF:
            h = remaining // 2
            legs.append((ts, prem_net, h, f"half +{HALF:g}%"))
            remaining -= h
            half_taken = True
            continue  # trail from the NEXT bar (same as live engine)
        if half_taken and peak >= ARM:
            give = give_fn(p["er_entry"], er_now)
            if ret <= peak - give:
                legs.append((ts, prem_net, remaining,
                             f"give-back {give:g} (peak {peak:.0f}%)"))
                return legs
    if last is not None and remaining > 0:
        legs.append((last[0], last[1], remaining, "time stop"))
    return legs


def flat(g):
    return lambda ee, en: g


def entry_binary(thr, chop, trend):
    def fn(ee, en):
        if ee is None:
            return FALLBACK_GIVE
        return trend if ee >= thr else chop
    return fn


def entry_3zone(lo, hi, chop, trend):
    def fn(ee, en):
        if ee is None:
            return FALLBACK_GIVE
        if ee >= hi:
            return trend
        if ee <= lo:
            return chop
        return BASE_GIVE
    return fn


def dyn_binary(thr, chop, trend):
    def fn(ee, en):
        if en is None:
            return FALLBACK_GIVE
        return trend if en >= thr else chop
    return fn


def dyn_3zone(lo, hi, chop, trend):
    def fn(ee, en):
        if en is None:
            return FALLBACK_GIVE
        if en >= hi:
            return trend
        if en <= lo:
            return chop
        return BASE_GIVE
    return fn


def run_policy(paths, give_fn):
    trades = []
    for p in paths:
        legs = sim_policy(p, give_fn)
        if legs:
            trades.append(BtTrade(p["ticker"], p["right"], p["strike"],
                                  p["now"], p["entry_prem"], legs, p["expiry"]))
    return trades


def acct_ret(m):
    """Sizing-fair per-trade account return; same -70 stop everywhere so this
    is expectancy scaled by a shared constant."""
    return m["expectancy_pct"] * config.RISK_PER_TRADE_PCT / abs(STOP)


def per_setup(trades):
    out = {}
    for tk in {t.ticker for t in trades}:
        for right, dirname in (("C", "call"), ("P", "put")):
            m = metrics([t for t in trades if t.ticker == tk and t.right == right])
            if m:
                out[f"{tk}:{dirname}"] = m
    return out


def main():
    cfg = StrategyConfig()  # direction="both", live entry window
    print("Loading data (60d 5m + 1y daily per ticker, with retry)...")
    intraday, daily = load_with_retry(cfg)
    print("Collecting entries (identical across all policies)...")
    entries = sorted(collect_entries(cfg, intraday, daily), key=lambda e: e["now"])
    er_lookup = ErLookup(intraday)
    print("Precomputing premium paths + per-bar ER...")
    paths = build_paths(entries, er_lookup)
    win_lo = min(p["now"] for p in paths).date()
    win_hi = max(p["now"] for p in paths).date()
    print(f"{len(paths)} allow-list entries, window {win_lo} -> {win_hi}.\n")

    # sanity: path-based flat-40 must reproduce the existing simulate_giveback
    check_al = [e for e in entries if (e["ticker"], e["right"]) in ALLOWED]
    ref = []
    for e in check_al:
        legs = simulate_giveback(e["bars_all"], e["entry_ts"], e["entry_prem"],
                                 e["strike"], e["right"], e["sigma"],
                                 e["expiry"], cfg, BASE_GIVE)
        if legs:
            ref.append(BtTrade(e["ticker"], e["right"], e["strike"], e["now"],
                               e["entry_prem"], legs, e["expiry"]))
    mine = run_policy(paths, flat(BASE_GIVE))
    ref_pnl, my_pnl = sum(t.pnl for t in ref), sum(t.pnl for t in mine)
    assert abs(ref_pnl - my_pnl) < 1.0, (
        f"harness mismatch: simulate_giveback ${ref_pnl:,.2f} vs path sim "
        f"${my_pnl:,.2f}")
    print(f"Sanity check OK: flat-40 path sim == backtest_runner_trail "
          f"simulate_giveback (${my_pnl:,.0f}).\n")

    # ER-at-entry distribution, so the thresholds are interpretable
    ers = sorted(p["er_entry"] for p in paths if p["er_entry"] is not None)
    n_none = sum(1 for p in paths if p["er_entry"] is None)
    qs = {q: ers[int(q * (len(ers) - 1))] for q in (0.1, 0.25, 0.5, 0.75, 0.9)}
    print("ER at entry distribution (allow-list entries):")
    print("  " + "  ".join(f"p{int(q*100)}={v:.2f}" for q, v in qs.items())
          + f"  (unreadable: {n_none})\n")

    # does regime even separate outcomes under the CURRENT flat-40 exit?
    t1, t2 = ers[len(ers) // 3], ers[2 * len(ers) // 3]
    base_trades = mine
    by_key = {(t.ticker, t.right, t.entry_time): t for t in base_trades}
    print(f"Flat-40 baseline split by ER-at-entry terciles "
          f"(chop<= {t1:.2f} < mid < {t2:.2f} <=trend):")
    diag_buckets = {}
    for label, lo, hi in (("chop", -1, t1), ("mid", t1, t2), ("trend", t2, 2)):
        sel = [by_key[(p["ticker"], p["right"], p["now"])] for p in paths
               if p["er_entry"] is not None and lo < p["er_entry"] <= hi
               and (p["ticker"], p["right"], p["now"]) in by_key]
        m = metrics(sel)
        diag_buckets[label] = m
        if m:
            print(f"  {label:<6} {m['trades']:>3} trades  {m['win_rate']:>5.1f}% win  "
                  f"{m['expectancy_pct']:>+6.1f}% exp")
    print()

    # ---------------- policy grid ----------------
    policies = [("FLAT-40 (live baseline)", flat(40.0)),
                ("FLAT-30 (control)", flat(30.0)),
                ("FLAT-50 (control)", flat(50.0)),
                ("FLAT-60 (control)", flat(60.0))]
    give_pairs = [(25.0, 50.0), (30.0, 50.0), (25.0, 60.0), (30.0, 60.0)]
    for thr in (0.4, 0.5, 0.6):
        for chop, trend in give_pairs:
            policies.append((f"ENTRY-bin thr{thr:g} chop{chop:g}/trend{trend:g}",
                             entry_binary(thr, chop, trend)))
    for lo, hi in ((0.3, 0.6), (0.35, 0.65)):
        for chop, trend in give_pairs:
            policies.append((f"ENTRY-3z {lo:g}/{hi:g} chop{chop:g}/trend{trend:g}",
                             entry_3zone(lo, hi, chop, trend)))
    for thr in (0.4, 0.5, 0.6):
        for chop, trend in give_pairs:
            policies.append((f"DYN-bin thr{thr:g} chop{chop:g}/trend{trend:g}",
                             dyn_binary(thr, chop, trend)))
    for lo, hi in ((0.3, 0.6), (0.35, 0.65)):
        for chop, trend in give_pairs:
            policies.append((f"DYN-3z {lo:g}/{hi:g} chop{chop:g}/trend{trend:g}",
                             dyn_3zone(lo, hi, chop, trend)))

    results = []
    for name, fn in policies:
        trades = run_policy(paths, fn)
        m = metrics(trades)
        results.append((name, m, trades))

    base_m = results[0][1]
    header = (f"{'POLICY':<40} {'trd':>3} {'win':>6} {'exp/tr':>7} "
              f"{'acct%/tr':>8} {'totalP&L':>10} {'maxDD':>9} {'dExp':>6} {'dWin':>6}")
    lines = [f"ADAPTIVE GIVE-BACK EXPERIMENT — allow-list entries, "
             f"window {win_lo} -> {win_hi}",
             "(approx BS pricing, optimistic for 0DTE; same -70 stop everywhere",
             " so acct%/tr ranking == expectancy ranking; trust RELATIVE order)",
             "", header, "-" * len(header)]
    for name, m, _ in results:
        if not m:
            lines.append(f"{name:<40} no trades")
            continue
        lines.append(
            f"{name:<40} {m['trades']:>3} {m['win_rate']:>5.1f}% "
            f"{m['expectancy_pct']:>+6.1f}% {acct_ret(m):>+7.3f}% "
            f"${m['total_pnl']:>9,.0f} ${m['max_drawdown']:>8,.0f} "
            f"{m['expectancy_pct'] - base_m['expectancy_pct']:>+5.1f} "
            f"{m['win_rate'] - base_m['win_rate']:>+5.1f}")
    print("\n".join(lines))

    # best adaptive vs baseline: per-setup + walk-forward halves + diff count
    adaptives = [(n, m, tr) for n, m, tr in results[4:] if m]
    adaptives.sort(key=lambda r: -acct_ret(r[1]))
    best_name, best_m, best_trades = adaptives[0]
    base_trades_sorted = sorted(base_trades, key=lambda t: t.entry_time)
    best_by_key = {(t.ticker, t.right, t.entry_time): t for t in best_trades}
    n_diff = sum(1 for t in base_trades_sorted
                 if abs(best_by_key[(t.ticker, t.right, t.entry_time)].pnl
                        - t.pnl) > 0.01)

    print(f"\nBEST ADAPTIVE: {best_name}")
    print(f"  exits differing from flat-40: {n_diff}/{len(base_trades_sorted)} trades")
    print(f"\n{'per-setup':<12} {'flat-40 win/exp':>20} {'best adaptive win/exp':>24}")
    ps_base, ps_best = per_setup(base_trades), per_setup(best_trades)
    for key in sorted(ps_base):
        b, a = ps_base[key], ps_best.get(key)
        print(f"  {key:<10} {b['win_rate']:>7.1f}% {b['expectancy_pct']:>+7.1f}%   "
              f"{a['win_rate']:>10.1f}% {a['expectancy_pct']:>+7.1f}%")

    # split-half robustness on the best adaptive (overfit check)
    print("\nSplit-half check (best adaptive vs flat-40, same entries):")
    halves = {}
    nn = len(paths)
    for label, sel in (("first half", paths[: nn // 2]),
                       ("second half", paths[nn // 2:])):
        mb = metrics(run_policy(sel, flat(40.0)))
        ma = metrics(run_policy(sel, dict(policies)[best_name]))
        halves[label] = {"flat40": mb, "adaptive": ma}
        if mb and ma:
            print(f"  {label:<12} flat-40 {mb['win_rate']:>5.1f}%/{mb['expectancy_pct']:>+6.1f}%  "
                  f"adaptive {ma['win_rate']:>5.1f}%/{ma['expectancy_pct']:>+6.1f}%  "
                  f"(dExp {ma['expectancy_pct'] - mb['expectancy_pct']:+.1f}pp)")

    OUT_JSON.write_text(json.dumps({
        "experiment": "adaptive give-back (regime-conditioned runner exit)",
        "window": {"start": str(win_lo), "end": str(win_hi)},
        "pricing": "approximated (Black-Scholes, realized vol) — optimistic 0DTE",
        "er_quantiles": {f"p{int(q*100)}": v for q, v in qs.items()},
        "flat40_by_er_tercile": {k: v for k, v in diag_buckets.items()},
        "results": [{"policy": n, **{k: v for k, v in m.items()},
                     "acct_ret_per_trade": acct_ret(m)}
                    for n, m, _ in results if m],
        "best_adaptive": {"policy": best_name, "n_exits_differing": n_diff,
                          "per_setup": ps_best},
        "flat40_per_setup": ps_base,
        "split_half": halves,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {OUT_JSON}")


if __name__ == "__main__":
    main()
