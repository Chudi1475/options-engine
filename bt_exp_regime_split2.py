"""bt_exp_regime_split2.py — REGIME SPLIT experiment, verification + extension run.

Fresh end-to-end rerun of the regime-split experiment (same design as
bt_exp_regime_split.py, imported for its helpers) plus the pieces the first
run did not persist:

  1. per-setup x regime tables saved into the JSON
  2. half-window verification of every policy (does the policy beat the
     baseline on sizing-fair account return inside EACH half, with win rate?)
  3. mini walk-forward: pick the worst bucket using ONLY the first half,
     apply the skip/half-size on the SECOND half, score out-of-sample
  4. per-setup detail of the worst bucket (is it broad or one setup?)

Entry signal untouched. Only size (0x / 0.5x / 1x) changes.
Sizing-fair account return per trade = ret_pct * RISK / |STOP| (stop constant
-70 across variants, so exact). Writes bt_exp_regime_split2.json (NOT reports/).

Usage: python bt_exp_regime_split2.py
"""

import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path

from backtest import BtTrade, load_data
from backtest_param_sweep import collect, sim
from bt_exp_regime_split import (EXIT_PARAMS, MIN_BUCKET_N, RISK, STOP_ABS,
                                 day_features, fetch_vix, grp_metrics,
                                 vix_at_entry, vix_bucket)
from strategy import StrategyConfig

HERE = Path(__file__).parent

VIX_ORDER = ["VIX<15", "VIX 15-20", "VIX 20-30", "VIX>30", "unknown"]
GAP_ORDER = ["gap down >1%", "no big gap", "gap up >1%", "unknown"]
TREND_ORDER = ["prior-day UP", "prior-day DOWN", "unknown"]
DIMS = [("vix_bucket", VIX_ORDER), ("gap_bucket", GAP_ORDER),
        ("prior_trend", TREND_ORDER)]


def build_rows():
    cfg = StrategyConfig()
    print("Loading underlying data (yfinance, 60d of 5m)...")
    intraday, daily = load_data(cfg)
    print("Loading ^VIX (5m + daily)...")
    v5, v1 = fetch_vix()
    print("Collecting allow-list entries (same as live scanner)...")
    entries = collect(cfg, intraday, daily)
    print(f"{len(entries)} allow-list entries.")

    rows = []
    for e in entries:
        legs = sim(e, *EXIT_PARAMS)
        if not legs:
            continue
        t = BtTrade(e["ticker"], e["right"], e["strike"], e["now"],
                    e["entry_prem"], legs, e["expiry"])
        day = e["entry_ts"].date()
        vix = vix_at_entry(v5, v1, e["entry_ts"])
        gap, gapb, trend = day_features(intraday[e["ticker"]],
                                        daily[e["ticker"]], day)
        rows.append({
            "setup": f"{e['ticker']}:{'call' if e['right'] == 'C' else 'put'}",
            "date": str(day), "entry_ts": str(e["entry_ts"]),
            "ret_pct": t.ret_pct, "pnl": t.pnl,
            "vix": vix, "vix_bucket": vix_bucket(vix),
            "gap_pct": gap, "gap_bucket": gapb, "prior_trend": trend,
            "weight": 1.0,
        })
    return rows


def worst_bucket(rows, dim, order, min_n):
    cands = []
    for v in order:
        if v == "unknown":
            continue
        g = [r for r in rows if r[dim] == v]
        if len(g) >= min_n:
            m = grp_metrics(g)
            cands.append((v, m["expectancy_pct"], len(g)))
    return min(cands, key=lambda c: c[1]) if cands else None


def apply_policy(rows, dim, bucket, w):
    return [dict(r, weight=(w if r[dim] == bucket else 1.0)) for r in rows]


def main():
    rows = build_rows()
    dates = sorted(r["date"] for r in rows)
    window = f"{dates[0]} .. {dates[-1]}"
    setups = sorted({r["setup"] for r in rows})
    print(f"\n{len(rows)} baseline trades, window {window}")

    baseline = grp_metrics(rows)
    per_setup_base = {s: grp_metrics([r for r in rows if r["setup"] == s])
                      for s in setups}
    print(f"\nBASELINE: {baseline['trades']} trades, "
          f"{baseline['win_rate']:.1f}% win, {baseline['expectancy_pct']:+.1f}%/tr, "
          f"acct {baseline['acct_ret_total']:+.2f}%, ${baseline['pnl_total']:,.0f}")
    for s in setups:
        m = per_setup_base[s]
        print(f"  {s}: {m['win_rate']:.1f}% win, {m['expectancy_pct']:+.1f}%/tr, "
              f"{m['trades']} trades, acct {m['acct_ret_total']:+.2f}%")

    # --- regime tables: pooled AND per-setup (persisted this time) ----------
    regime_pooled, regime_per_setup = {}, {}
    for dim, order in DIMS:
        present = [v for v in order if any(r[dim] == v for r in rows)]
        regime_pooled[dim] = {}
        print(f"\n--- pooled by {dim} ---")
        print(f"{'bucket':<16} {'n':>4} {'win%':>6} {'exp%/tr':>8} {'acct%tot':>9}")
        for v in present:
            m = grp_metrics([r for r in rows if r[dim] == v])
            regime_pooled[dim][v] = m
            print(f"{v:<16} {m['trades']:>4} {m['win_rate']:>5.1f}% "
                  f"{m['expectancy_pct']:>+7.1f}% {m['acct_ret_total']:>+8.2f}%")
        regime_per_setup[dim] = {}
        for s in setups:
            srows = [r for r in rows if r["setup"] == s]
            regime_per_setup[dim][s] = {}
            for v in present:
                g = [r for r in srows if r[dim] == v]
                if g:
                    regime_per_setup[dim][s][v] = grp_metrics(g)
        for s in setups:
            parts = []
            for v in present:
                m = regime_per_setup[dim][s].get(v)
                if m:
                    parts.append(f"{v}: n={m['trades']} win {m['win_rate']:.0f}% "
                                 f"exp {m['expectancy_pct']:+.1f}%")
            print(f"  {s:<10} " + " | ".join(parts))

    # --- full-window policy tests -------------------------------------------
    mid = dates[len(dates) // 2]
    halves = [("first", lambda r: r["date"] < mid),
              ("second", lambda r: r["date"] >= mid)]
    policies = {}
    print(f"\n=== POLICY TESTS (worst bucket per dimension, min n={MIN_BUCKET_N}; "
          f"half-split at {mid}) ===")
    for dim, order in DIMS:
        wb = worst_bucket(rows, dim, order, MIN_BUCKET_N)
        if wb is None:
            print(f"{dim}: no actionable bucket")
            continue
        name, exp, n = wb
        print(f"\n{dim}: worst = '{name}' ({n} trades, {exp:+.1f}%/tr)")
        for label, w in (("SKIP", 0.0), ("HALF-SIZE", 0.5)):
            test = apply_policy(rows, dim, name, w)
            pm = grp_metrics(test)
            floor_ok = (pm["win_rate"] or 0) >= 70.0
            beats = pm["acct_ret_total"] > baseline["acct_ret_total"]
            # in-half verification
            half_check = {}
            for hname, cond in halves:
                hb = grp_metrics([r for r in rows if cond(r)])
                hp = grp_metrics([r for r in test if cond(r)])
                half_check[hname] = {
                    "base_acct": hb["acct_ret_total"], "pol_acct": hp["acct_ret_total"],
                    "pol_win": hp["win_rate"],
                    "beats": hp["acct_ret_total"] > hb["acct_ret_total"],
                }
            both = all(h["beats"] for h in half_check.values())
            per_setup = {s: grp_metrics([r for r in test if r["setup"] == s])
                         for s in setups}
            print(f"  {label:<10} -> win {pm['win_rate']:.1f}% "
                  f"(floor>=70: {'YES' if floor_ok else 'NO'}), "
                  f"acct {pm['acct_ret_total']:+.2f}% vs base "
                  f"{baseline['acct_ret_total']:+.2f}% "
                  f"({'BEATS' if beats else 'no'}), "
                  f"halves: " + ", ".join(
                      f"{h} {c['pol_acct']:+.2f}% vs {c['base_acct']:+.2f}% "
                      f"({'beats' if c['beats'] else 'no'})"
                      for h, c in half_check.items()) +
                  f" -> both halves: {'YES' if both else 'NO'}")
            policies[f"{dim}|{name}|{label}"] = {
                "portfolio": pm, "per_setup": per_setup, "floor_ok": floor_ok,
                "beats_baseline": beats, "half_check": half_check,
                "beats_in_both_halves": both,
            }

    # --- mini walk-forward: choose on first half, score on second ------------
    print("\n=== MINI WALK-FORWARD (pick worst on first half, apply on second) ===")
    first_rows = [r for r in rows if r["date"] < mid]
    second_rows = [r for r in rows if r["date"] >= mid]
    base2 = grp_metrics(second_rows)
    walkforward = {"split_date": mid,
                   "second_half_baseline": base2}
    print(f"second-half baseline: {base2['trades']} trades, win {base2['win_rate']:.1f}%, "
          f"acct {base2['acct_ret_total']:+.2f}%")
    for dim, order in DIMS:
        wb = worst_bucket(first_rows, dim, order, max(4, MIN_BUCKET_N // 2))
        if wb is None:
            continue
        name, exp, n = wb
        res = {}
        for label, w in (("SKIP", 0.0), ("HALF-SIZE", 0.5)):
            test = apply_policy(second_rows, dim, name, w)
            pm = grp_metrics(test)
            res[label] = {"portfolio": pm,
                          "beats": pm["acct_ret_total"] > base2["acct_ret_total"],
                          "floor_ok": (pm["win_rate"] or 0) >= 70.0}
            print(f"  {dim} worst-on-1st='{name}' (n1={n}, {exp:+.1f}%/tr) {label}: "
                  f"2nd-half acct {pm['acct_ret_total']:+.2f}% vs {base2['acct_ret_total']:+.2f}% "
                  f"({'BEATS' if res[label]['beats'] else 'no'}), win {pm['win_rate']:.1f}%")
        walkforward[dim] = {"picked_bucket": name, "first_half_exp": exp,
                            "first_half_n": n, "results": res}

    out = {
        "experiment": "regime split v2 (verification + per-setup tables + walk-forward)",
        "window": window,
        "exit_params": {"half": EXIT_PARAMS[0], "giveback": EXIT_PARAMS[1],
                        "stop": EXIT_PARAMS[2]},
        "pricing": "approximated Black-Scholes (optimistic for 0DTE) - relative only",
        "sizing_fair": f"acct ret/trade = ret_pct * {RISK} / {STOP_ABS}",
        "baseline": {"portfolio": baseline, "per_setup": per_setup_base},
        "regime_tables_pooled": regime_pooled,
        "regime_tables_per_setup": regime_per_setup,
        "policies": policies,
        "walkforward": walkforward,
        "n_trades": len(rows),
    }
    (HERE / "bt_exp_regime_split2.json").write_text(json.dumps(out, indent=2),
                                                    encoding="utf-8")
    print(f"\nWrote {HERE / 'bt_exp_regime_split2.json'}")


if __name__ == "__main__":
    main()
