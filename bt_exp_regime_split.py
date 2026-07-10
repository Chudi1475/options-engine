"""bt_exp_regime_split.py — REGIME SPLIT experiment.

Segments the allow-list baseline trades (give-back-40 exits, same entries as
the live scanner) by market regime measured AT ENTRY TIME:

  - VIX bucket at entry (<15, 15-20, 20-30, >30) from ^VIX 5m bars
    (fallback: prior daily VIX close)
  - overnight gap of the traded underlying (>+1% up, <-1% down, else no-gap)
  - prior-day trend direction (prior daily close vs prior daily open)

Reports per-regime win rate / expectancy per setup and pooled, then tests the
policy: SKIP or HALF-SIZE entries in the worst bucket of each dimension.
Entry signal is untouched; only size (0x / 0.5x / 1x) changes.

Sizing-fair account return per trade = ret_pct * RISK_PER_TRADE_PCT / |STOP_PCT|
(stop is constant -70 for every variant, so this is exact, not approximate).

Writes bt_exp_regime_split.json next to this file (NOT into reports/).
Usage: python bt_exp_regime_split.py
"""

import json
import sys
import time as time_mod

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import config
from backtest import BtTrade, load_data
from backtest_param_sweep import collect, sim
from strategy import StrategyConfig

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).parent
EXIT_PARAMS = (25, 40, -70)          # settled live exits: half +25 / give-back 40 / stop -70
MIN_BUCKET_N = 8                     # a bucket must have >= this many trades to be actionable
RISK = config.RISK_PER_TRADE_PCT     # 1.0 (% of account per full stop-out)
STOP_ABS = 70.0


def dl_retry(symbol, **kw):
    """yfinance download with backoff on throttle/empty."""
    for attempt in range(4):
        try:
            df = yf.download(symbol, progress=False, auto_adjust=False, **kw)
            if df is not None and not df.empty:
                if hasattr(df.columns, "levels"):
                    df.columns = df.columns.get_level_values(0)
                return df
        except Exception as e:  # noqa: BLE001
            print(f"  download {symbol} attempt {attempt + 1} failed: {e}")
        time_mod.sleep(3 * (attempt + 1))
    raise RuntimeError(f"could not download {symbol}")


def fetch_vix():
    v5 = dl_retry("^VIX", period="60d", interval="5m")
    v5.index = v5.index.tz_convert(ET)
    v1 = dl_retry("^VIX", period="1y", interval="1d")
    return v5, v1


def vix_at_entry(v5, v1, entry_ts):
    """Last ^VIX 5m close at or before the entry bar, same day.
    Fallback: most recent daily VIX close strictly before the entry date."""
    day = entry_ts.date()
    same_day = v5[(v5.index.date == day) & (v5.index <= entry_ts)]
    if not same_day.empty:
        return float(same_day["Close"].iloc[-1])
    prior = v1[v1.index.date < day]
    if prior.empty:
        return None
    return float(prior["Close"].iloc[-1])


def vix_bucket(v):
    if v is None:
        return "unknown"
    if v < 15:
        return "VIX<15"
    if v < 20:
        return "VIX 15-20"
    if v < 30:
        return "VIX 20-30"
    return "VIX>30"


def day_features(intraday_t, daily_t, day):
    """(gap_pct, gap_bucket, prior_trend) for the traded underlying on `day`.
    gap = today's first 5m open vs prior daily close.
    prior trend = sign of prior daily close - prior daily open."""
    day_bars = intraday_t[intraday_t.index.date == day]
    prior = daily_t[daily_t.index.date < day]
    if day_bars.empty or prior.empty:
        return None, "unknown", "unknown"
    today_open = float(day_bars["Open"].iloc[0])
    prior_close = float(prior["Close"].iloc[-1])
    gap = (today_open / prior_close - 1) * 100
    if gap > 1.0:
        gb = "gap up >1%"
    elif gap < -1.0:
        gb = "gap down >1%"
    else:
        gb = "no big gap"
    prior_open = float(prior["Open"].iloc[-1])
    trend = "prior-day UP" if prior_close >= prior_open else "prior-day DOWN"
    return gap, gb, trend


def grp_metrics(rows):
    """rows: list of dicts with ret_pct, pnl, weight (weight applied to sizing-
    fair return and dollars; win rate counts only weight>0 trades)."""
    taken = [r for r in rows if r["weight"] > 0]
    if not taken:
        return {"trades": 0, "win_rate": None, "expectancy_pct": None,
                "acct_ret_total": 0.0, "pnl_total": 0.0}
    wins = sum(1 for r in taken if r["pnl"] > 0)
    return {
        "trades": len(taken),
        "win_rate": wins / len(taken) * 100,
        "expectancy_pct": sum(r["ret_pct"] for r in taken) / len(taken),
        "acct_ret_total": sum(r["ret_pct"] * RISK / STOP_ABS * r["weight"] for r in rows),
        "pnl_total": sum(r["pnl"] * r["weight"] for r in rows),
    }


def main():
    cfg = StrategyConfig()
    print("Loading underlying data (yfinance, 60d of 5m)...")
    intraday, daily = load_data(cfg)
    print("Loading ^VIX (5m + daily)...")
    v5, v1 = fetch_vix()
    print("Collecting allow-list entries (same as live scanner)...")
    entries = collect(cfg, intraday, daily)
    print(f"{len(entries)} allow-list entries.")

    # --- baseline trades with the settled exits, plus regime tags -----------
    rows = []
    for e in entries:
        legs = sim(e, *EXIT_PARAMS)
        if not legs:
            continue
        t = BtTrade(e["ticker"], e["right"], e["strike"], e["now"],
                    e["entry_prem"], legs, e["expiry"])
        day = e["entry_ts"].date()
        vix = vix_at_entry(v5, v1, e["entry_ts"])
        gap, gapb, trend = day_features(intraday[e["ticker"]], daily[e["ticker"]], day)
        rows.append({
            "setup": f"{e['ticker']}:{'call' if e['right'] == 'C' else 'put'}",
            "date": str(day), "entry_ts": str(e["entry_ts"]),
            "ret_pct": t.ret_pct, "pnl": t.pnl,
            "vix": vix, "vix_bucket": vix_bucket(vix),
            "gap_pct": gap, "gap_bucket": gapb, "prior_trend": trend,
            "weight": 1.0,
        })

    dates = sorted(r["date"] for r in rows)
    window = f"{dates[0]} .. {dates[-1]}"
    print(f"\n{len(rows)} baseline trades, window {window}")

    def show(title, groups):
        print(f"\n--- {title} ---")
        print(f"{'bucket':<22} {'n':>4} {'win%':>6} {'exp%/tr':>8} {'acct%tot':>9} {'$pnl':>9}")
        for name, g in groups:
            m = grp_metrics(g)
            if m["trades"] == 0:
                print(f"{name:<22} {0:>4}      -        -         -         -")
                continue
            print(f"{name:<22} {m['trades']:>4} {m['win_rate']:>5.1f}% "
                  f"{m['expectancy_pct']:>+7.1f}% {m['acct_ret_total']:>+8.2f}% "
                  f"${m['pnl_total']:>8,.0f}")

    def buckets_of(dim, order=None):
        vals = order or sorted({r[dim] for r in rows})
        return [(v, [r for r in rows if r[dim] == v]) for v in vals]

    VIX_ORDER = ["VIX<15", "VIX 15-20", "VIX 20-30", "VIX>30", "unknown"]
    GAP_ORDER = ["gap down >1%", "no big gap", "gap up >1%", "unknown"]
    TREND_ORDER = ["prior-day UP", "prior-day DOWN", "unknown"]

    baseline = grp_metrics(rows)
    print(f"\nBASELINE (allow-list, give-back-40): {baseline['trades']} trades, "
          f"{baseline['win_rate']:.1f}% win, {baseline['expectancy_pct']:+.1f}%/tr, "
          f"acct {baseline['acct_ret_total']:+.2f}%, ${baseline['pnl_total']:,.0f}")

    per_setup_base = {}
    for s in sorted({r["setup"] for r in rows}):
        m = grp_metrics([r for r in rows if r["setup"] == s])
        per_setup_base[s] = m
        print(f"  {s}: {m['win_rate']:.1f}% win, {m['expectancy_pct']:+.1f}%/tr, "
              f"{m['trades']} trades, acct {m['acct_ret_total']:+.2f}%")

    dims = [("vix_bucket", VIX_ORDER), ("gap_bucket", GAP_ORDER),
            ("prior_trend", TREND_ORDER)]
    regime_tables = {}
    for dim, order in dims:
        present = [v for v in order if any(r[dim] == v for r in rows)]
        show(f"pooled by {dim}", buckets_of(dim, present))
        regime_tables[dim] = {v: grp_metrics([r for r in rows if r[dim] == v])
                              for v in present}
        # per-setup x regime
        for s in sorted({r["setup"] for r in rows}):
            srows = [r for r in rows if r["setup"] == s]
            groups = [(v, [r for r in srows if r[dim] == v]) for v in present]
            show(f"{s} by {dim}", groups)

    # --- pick worst actionable bucket per dimension --------------------------
    def worst_bucket(dim, order):
        cands = []
        for v in order:
            g = [r for r in rows if r[dim] == v]
            if len(g) >= MIN_BUCKET_N and v != "unknown":
                m = grp_metrics(g)
                cands.append((v, m["expectancy_pct"], len(g)))
        if not cands:
            return None
        return min(cands, key=lambda c: c[1])

    policies = {}
    print("\n=== POLICY TESTS (worst bucket per dimension, min n=%d) ===" % MIN_BUCKET_N)
    for dim, order in dims:
        wb = worst_bucket(dim, order)
        if wb is None:
            print(f"{dim}: no bucket with >= {MIN_BUCKET_N} trades; skipping")
            continue
        name, exp, n = wb
        print(f"\n{dim}: worst = '{name}' ({n} trades, {exp:+.1f}%/tr)")
        for label, w in (("SKIP", 0.0), ("HALF-SIZE", 0.5)):
            test = [dict(r, weight=(w if r[dim] == name else 1.0)) for r in rows]
            pm = grp_metrics(test)
            per_setup = {s: grp_metrics([r for r in test if r["setup"] == s])
                         for s in sorted({r["setup"] for r in test})}
            floor_ok = (pm["win_rate"] or 0) >= 70.0
            beats = pm["acct_ret_total"] > baseline["acct_ret_total"]
            print(f"  {label:<10} -> {pm['trades']} trades, win {pm['win_rate']:.1f}% "
                  f"(floor>=70: {'YES' if floor_ok else 'NO'}), "
                  f"acct {pm['acct_ret_total']:+.2f}% vs base "
                  f"{baseline['acct_ret_total']:+.2f}% "
                  f"({'BEATS' if beats else 'does not beat'}), "
                  f"${pm['pnl_total']:,.0f}")
            policies[f"{dim}|{name}|{label}"] = {
                "portfolio": pm, "per_setup": per_setup,
                "floor_ok": floor_ok, "beats_baseline": beats,
            }

    # --- stability check: does the worst bucket hold in both half-windows? ---
    mid = dates[len(dates) // 2]
    print(f"\n=== STABILITY (split at {mid}) ===")
    stability = {}
    for dim, order in dims:
        for half, cond in (("first", lambda r: r["date"] < mid),
                           ("second", lambda r: r["date"] >= mid)):
            sub = [r for r in rows if cond(r)]
            ranked = []
            for v in order:
                g = [r for r in sub if r[dim] == v and v != "unknown"]
                if len(g) >= 4:
                    ranked.append((v, grp_metrics(g)["expectancy_pct"], len(g)))
            ranked.sort(key=lambda c: c[1])
            stability[f"{dim}|{half}"] = ranked
            txt = ", ".join(f"{v}: {e:+.1f}% (n={n})" for v, e, n in ranked)
            print(f"  {dim} {half:>6} half: {txt}")

    out = {
        "experiment": "regime split (VIX buckets, overnight gap, prior-day trend)",
        "window": window,
        "exit_params": {"half": 25, "giveback": 40, "stop": -70},
        "pricing": "approximated Black-Scholes (optimistic for 0DTE) - relative only",
        "baseline": {"portfolio": baseline, "per_setup": per_setup_base},
        "regime_tables_pooled": regime_tables,
        "policies": policies,
        "stability": {k: [[v, e, n] for v, e, n in val] for k, val in stability.items()},
        "trades": rows,
    }
    (HERE / "bt_exp_regime_split.json").write_text(json.dumps(out, indent=2),
                                                   encoding="utf-8")
    print(f"\nWrote {HERE / 'bt_exp_regime_split.json'}")


if __name__ == "__main__":
    main()
