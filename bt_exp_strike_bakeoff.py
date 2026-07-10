"""EXPERIMENT: strike-selection bake-off on IDENTICAL entries and exits.

Policies compared (everything else held fixed — same entry timestamps, same
give-back exit engine with the live params +25 half / give-back 40 / -70 stop,
same approximate BS pricing, slippage, fees):

  first_otm   — current live rule: first strike at/above spot (calls),
                at/below spot (puts)
  second_otm  — one increment further OTM than first_otm
  delta40     — strike on the grid whose BS |delta| at entry is nearest 0.40
  delta25     — strike on the grid whose BS |delta| at entry is nearest 0.25

Delta is approximated with the same Black-Scholes machinery the harnesses
already use: call delta = N(d1), put delta = N(d1) - 1, sigma = 20d realized
vol, T = time to expiry at the entry bar. No lookahead: strike choice uses
only entry-time information.

Honesty: ~60 days of 5m data (yfinance cap), approximate/optimistic 0DTE
pricing — trust RELATIVE rankings only. Entries are the allow-list setups
(SPX:C, SPY:C, QCOM:C, TSLA:P). To keep entries IDENTICAL across policies,
an entry is kept only if EVERY policy's entry premium clears the $0.10 floor
(dropped count reported).

Writes bt_exp_strike_bakeoff.json (repo root — NOT reports/, which drives the
live eligibility gate).

Usage:
    python bt_exp_strike_bakeoff.py
"""

import json
import math
import sys
import time as _time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path

import pandas as pd

import config
from backtest import (CONTRACTS, RISK_FREE, SLIPPAGE, BtTrade, bs_price,
                      expiry_for, load_data, metrics, norm_cdf, realized_vol,
                      years_to_expiry)
from strategy import StrategyConfig, detect_setup, _round_strike

REPO_DIR = Path(__file__).parent
ALLOWED = {("SPX", "C"), ("SPY", "C"), ("QCOM", "C"), ("TSLA", "P")}
POLICIES = ("first_otm", "second_otm", "delta40", "delta25")


def bs_delta(S, K, T_years, sigma, right: str) -> float:
    """BS delta with the same conventions as bs_price. Returns |delta|."""
    if T_years <= 0 or sigma <= 0:
        # expired/degenerate: delta is 0 or 1 by moneyness
        itm = S > K if right == "C" else S < K
        return 1.0 if itm else 0.0
    d1 = (math.log(S / K) + (RISK_FREE + sigma**2 / 2) * T_years) / (sigma * math.sqrt(T_years))
    delta = norm_cdf(d1)
    return delta if right == "C" else 1.0 - delta  # |put delta| = N(d1)-1 in abs


def nearest_delta_strike(spot, increment, T_years, sigma, right, target):
    """Strike on the increment grid whose |delta| at entry is nearest target.
    Searches +/-60 increments around spot; ties go to the more-OTM strike."""
    base = _round_strike(spot, increment, up=(right == "C"))
    sign = 1.0 if right == "C" else -1.0
    best, best_err = base, float("inf")
    for k in range(-60, 61):
        strike = base + sign * k * increment
        if strike <= 0:
            continue
        err = abs(bs_delta(spot, strike, T_years, sigma, right) - target)
        # strict < with OTM-first scan order handled via tie-break on k
        if err < best_err - 1e-12 or (abs(err - best_err) <= 1e-12 and k > 0):
            best, best_err = strike, err
    return best


def collect_entries(cfg, intraday, daily):
    """Same entry logic as backtest_param_sweep.collect — strike NOT chosen yet."""
    rows = []
    for ticker in cfg.watchlist:
        bars_all = intraday[ticker]
        for day in sorted(set(bars_all.index.date)):
            day_bars = bars_all[bars_all.index.date == day]
            if day_bars.empty:
                continue
            sigma = realized_vol(daily[ticker]["Close"], day)
            if sigma <= 0:
                continue
            entered = set()
            for i in range(len(day_bars)):
                upto = day_bars.iloc[: i + 1]
                now = upto.index[-1].to_pydatetime()
                if not (cfg.entry_start <= now.time() <= cfg.entry_end):
                    continue
                setup = detect_setup(ticker, upto, now, cfg)
                if setup is None or setup.direction in entered:
                    continue
                entered.add(setup.direction)
                right = "C" if setup.direction == "call" else "P"
                if (ticker, right) not in ALLOWED:
                    continue
                expiry = expiry_for(ticker, now)
                rows.append({"bars_all": bars_all, "entry_ts": upto.index[-1],
                             "spot": setup.spot, "right": right, "sigma": sigma,
                             "expiry": expiry, "ticker": ticker, "now": now})
    return rows


def strike_for(entry, policy, cfg):
    ticker, right, spot = entry["ticker"], entry["right"], entry["spot"]
    inc = cfg.strike_increment.get(ticker, cfg.default_strike_increment)
    T = years_to_expiry(entry["now"], entry["expiry"])
    sign = 1.0 if right == "C" else -1.0
    if policy == "first_otm":
        return _round_strike(spot, inc, up=(right == "C"))
    if policy == "second_otm":
        return _round_strike(spot, inc, up=(right == "C")) + sign * inc
    if policy == "delta40":
        return nearest_delta_strike(spot, inc, T, entry["sigma"], right, 0.40)
    if policy == "delta25":
        return nearest_delta_strike(spot, inc, T, entry["sigma"], right, 0.25)
    raise ValueError(policy)


def sim_giveback(entry, strike, entry_prem):
    """Live give-back exit engine, verbatim from backtest_param_sweep.sim."""
    bars_all, entry_ts = entry["bars_all"], entry["entry_ts"]
    right, sigma, expiry = entry["right"], entry["sigma"], entry["expiry"]
    half_trig, giveback, stop = (config.TP_HALF_PCT, config.RUNNER_GIVEBACK_PCT,
                                 config.STOP_PCT)
    future = bars_all[(bars_all.index > entry_ts) & (bars_all.index <= pd.Timestamp(expiry))]
    legs, remaining, half_taken, last, peak = [], CONTRACTS, False, None, None
    for ts, row in future.iterrows():
        prem_net = bs_price(float(row["Close"]), strike,
                            years_to_expiry(ts.to_pydatetime(), expiry), sigma, right) * (1 - SLIPPAGE)
        ret = (prem_net / entry_prem - 1) * 100
        last = (ts, prem_net)
        peak = ret if peak is None else max(peak, ret)
        if ret <= stop:
            legs.append((ts, prem_net, remaining, "stop"))
            return legs
        if not half_taken and ret >= half_trig:
            h = remaining // 2
            legs.append((ts, prem_net, h, "half"))
            remaining -= h
            half_taken = True
            continue
        if half_taken and ret <= peak - giveback:
            legs.append((ts, prem_net, remaining, "give-back"))
            return legs
    if last is not None and remaining > 0:
        legs.append((last[0], last[1], remaining, "time"))
    return legs


def main():
    cfg = StrategyConfig()
    print("Loading data (yfinance, ~60d of 5m)...")
    for attempt in range(4):
        try:
            intraday, daily = load_data(cfg)
            break
        except Exception as e:  # yfinance throttle — back off and retry
            if attempt == 3:
                raise
            print(f"  load_data failed ({e}); retrying in {5 * (attempt + 1)}s...")
            _time.sleep(5 * (attempt + 1))
    print("Collecting allow-list entries (strike-agnostic)...")
    entries = collect_entries(cfg, intraday, daily)
    print(f"  {len(entries)} raw allow-list entries.")

    # Attach per-policy strike + entry premium; keep an entry only if ALL
    # policies clear the $0.10 premium floor, so every policy trades the
    # exact same entry set.
    kept, dropped = [], 0
    for e in entries:
        T = years_to_expiry(e["now"], e["expiry"])
        ok = True
        e["policy_data"] = {}
        for pol in POLICIES:
            strike = strike_for(e, pol, cfg)
            prem = bs_price(e["spot"], strike, T, e["sigma"], e["right"]) * (1 + SLIPPAGE)
            delta = bs_delta(e["spot"], strike, T, e["sigma"], e["right"])
            e["policy_data"][pol] = {"strike": strike, "entry_prem": prem,
                                     "delta": delta}
            if prem < 0.10:
                ok = False
        if ok:
            kept.append(e)
        else:
            dropped += 1
    print(f"  {len(kept)} entries kept, {dropped} dropped (some policy's premium < $0.10).")

    results = {}
    for pol in POLICIES:
        trades = []
        for e in kept:
            pd_ = e["policy_data"][pol]
            legs = sim_giveback(e, pd_["strike"], pd_["entry_prem"])
            if legs:
                trades.append((e, BtTrade(e["ticker"], e["right"], pd_["strike"],
                                          e["now"], pd_["entry_prem"], legs,
                                          e["expiry"])))
        per_setup = {}
        for ticker in sorted({e["ticker"] for e, _ in trades}):
            for right, dirname in (("C", "call"), ("P", "put")):
                sub = [t for e, t in trades if t.ticker == ticker and t.right == right]
                mm = metrics(sub)
                if mm:
                    mm["avg_entry_prem"] = sum(t.entry_premium for t in sub) / len(sub)
                    mm["avg_delta"] = (sum(e["policy_data"][pol]["delta"]
                                           for e, t in trades
                                           if t.ticker == ticker and t.right == right)
                                       / len(sub))
                    mm["avg_otm_pts"] = (sum(abs(t.strike - e["spot"])
                                             for e, t in trades
                                             if t.ticker == ticker and t.right == right)
                                         / len(sub))
                    per_setup[f"{ticker}:{dirname}"] = mm
        overall = metrics([t for _, t in trades])
        if overall:
            allt = [t for _, t in trades]
            overall["avg_entry_prem"] = sum(t.entry_premium for t in allt) / len(allt)
            overall["avg_delta"] = sum(e["policy_data"][pol]["delta"] for e, _ in trades) / len(allt)
        # sizing-fair account return per trade: live sizing pins max loss at
        # RISK% of account via alloc = RISK/|stop|; stop is identical across
        # policies, so acct%/trade = expectancy% * RISK/|stop|.
        acct = (overall["expectancy_pct"] * config.RISK_PER_TRADE_PCT
                / abs(config.STOP_PCT)) if overall else None
        results[pol] = {"overall": overall, "per_setup": per_setup,
                        "acct_ret_per_trade_pct": acct}

    # ---- report ----
    win_dates = (min(e["now"] for e in kept).strftime("%m/%d/%Y"),
                 max(e["now"] for e in kept).strftime("%m/%d/%Y"))
    print(f"\nSTRIKE BAKE-OFF — identical entries ({len(kept)}) and give-back "
          f"exits (+{config.TP_HALF_PCT:g} half / give {config.RUNNER_GIVEBACK_PCT:g} "
          f"/ stop {config.STOP_PCT:g}), window {win_dates[0]} - {win_dates[1]}")
    print("(approx BS pricing, optimistic for 0DTE — trust RELATIVE ranking; "
          "fixed 2 contracts for $ totals; acct%/tr is the sizing-fair number)\n")

    hdr = (f"{'policy':>10} {'setup':>10} {'n':>4} {'win%':>6} {'exp/tr':>8} "
           f"{'acct%/tr':>9} {'fixed$PnL':>10} {'maxDD':>9} {'avgPrem':>8} "
           f"{'avg|d|':>6} {'avgOTM':>7}")
    print(hdr)
    print("-" * len(hdr))
    for pol in POLICIES:
        r = results[pol]
        o = r["overall"]
        if not o:
            print(f"{pol:>10}  no trades")
            continue
        print(f"{pol:>10} {'ALL':>10} {o['trades']:>4} {o['win_rate']:>5.1f}% "
              f"{o['expectancy_pct']:>+7.1f}% {r['acct_ret_per_trade_pct']:>+8.3f}% "
              f"${o['total_pnl']:>8,.0f} ${o['max_drawdown']:>8,.0f} "
              f"${o['avg_entry_prem']:>7.2f} {o['avg_delta']:>6.2f} {'':>7}")
        for key, mm in sorted(r["per_setup"].items()):
            acct = mm["expectancy_pct"] * config.RISK_PER_TRADE_PCT / abs(config.STOP_PCT)
            print(f"{'':>10} {key:>10} {mm['trades']:>4} {mm['win_rate']:>5.1f}% "
                  f"{mm['expectancy_pct']:>+7.1f}% {acct:>+8.3f}% "
                  f"${mm['total_pnl']:>8,.0f} ${mm['max_drawdown']:>8,.0f} "
                  f"${mm['avg_entry_prem']:>7.2f} {mm['avg_delta']:>6.2f} "
                  f"{mm['avg_otm_pts']:>6.1f}p")
        print()

    out = {
        "experiment": "strike selection bake-off (identical entries + give-back exits)",
        "window": {"start": win_dates[0], "end": win_dates[1]},
        "entries_kept": len(kept), "entries_dropped_premium_floor": dropped,
        "exit_params": {"half": config.TP_HALF_PCT, "give": config.RUNNER_GIVEBACK_PCT,
                        "stop": config.STOP_PCT},
        "pricing": "approximated (Black-Scholes, 20d realized vol) — optimistic for 0DTE",
        "policies": {pol: {"overall": results[pol]["overall"],
                           "acct_ret_per_trade_pct": results[pol]["acct_ret_per_trade_pct"],
                           "per_setup": results[pol]["per_setup"]}
                     for pol in POLICIES},
    }
    out_path = REPO_DIR / "bt_exp_strike_bakeoff.json"
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
