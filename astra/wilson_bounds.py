"""Wilson 95% lower bounds for every win-rate cohort we quote.

WHY THIS EXISTS
---------------
Astra finding A16: the packet said the Wilson 95% lower bound of 43/53 was
67.4%. It is 68.6413%. That number had been carried forward from an earlier
brief, where it belonged to a different cohort, and re-quoted without being
re-derived. This file exists so no bound is ever typed by hand again.

Recomputing it also exposed something the single corrected figure hides: NONE of
the three cohorts we quote clears the 71.429% breakeven at its lower bound, and
the live book's bound is 51.3%. The corrected number was not a technicality.

Wilson score interval, lower bound, z = 1.96:

    center = (p + z^2/2n) / (1 + z^2/n)
    margin = (z / (1 + z^2/n)) * sqrt( p(1-p)/n + z^2/4n^2 )

Binary, zero-cost breakeven for an all-out target at R multiple r is
1/(1+r), which is 71.4286% at 0.4R. That is an arithmetic identity about a
zero-cost binary bet, not an observed guarantee about this strategy (A13).

    python wilson_bounds.py
"""

import json
import math
import pathlib

OUT = pathlib.Path(__file__).parent / "wilson_bounds.json"

TP_R = 0.4  # config: the live sniper target, all out

# (label, wins, n, what the cohort actually is)
COHORTS = [
    ("round6_session_oos", 43, 53,
     "reports/chart_backtest_round6_session.json, out of sample leg. A fixed "
     "configuration MEASUREMENT, and it does not meet the 60 trade floor the "
     "rounds used for a qualifying validation (Astra A17)."),
    ("live_sniper_resolved", 32, 49,
     "the 53 ticket live book, resolved rows only: 32 targets and 17 stops. The "
     "four session-end flats are excluded from the rate and reported separately, "
     "because calling a flat a loss flatters the stop."),
    ("forward_ledger_t04", 14, 20,
     "the 20 graded historical forward candidates at the 0.4R tier. A DIFFERENT "
     "cohort from the live book, and disjoint from it: the forward rows "
     "correspond to zero live alerts (Astra A13)."),
]


def wilson_lb(k: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval. Never a normal approximation:
    at these n the normal interval is wrong in the direction that flatters."""
    p = k / n
    d = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / d
    margin = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return center - margin


def main():
    breakeven = 1.0 / (1.0 + TP_R)
    rows = []
    for label, k, n, note in COHORTS:
        lb = wilson_lb(k, n)
        rows.append({
            "cohort": label,
            "wins": k,
            "n": n,
            "point_estimate_pct": round(100.0 * k / n, 3),
            "wilson95_lower_bound_pct": round(100.0 * lb, 4),
            "clears_breakeven": bool(lb > breakeven),
            "note": note,
        })
    out = {
        "kind": "measurement",
        "label": "Wilson 95% lower bounds for every quoted win-rate cohort. "
                 "Corrects Astra finding A16.",
        "z": 1.96,
        "target_r": TP_R,
        "zero_cost_binary_breakeven_pct": round(100.0 * breakeven, 4),
        "breakeven_is": "an arithmetic identity 1/(1+r) for a zero cost binary "
                        "bet, not an observed guarantee for this strategy",
        "superseded": {"claim": "Wilson 95% lower bound of 43/53 is 67.4%",
                       "correct_value_pct": round(100.0 * wilson_lb(43, 53), 4),
                       "source_of_error": "carried forward from an earlier brief "
                                          "where it described a different cohort, "
                                          "and re-quoted without re-derivation"},
        "cohorts": rows,
        "reading": "No cohort we quote clears the 71.4286% breakeven at its lower "
                   "bound. The live book's bound is 51.3%. Point estimates above "
                   "breakeven at these sample sizes are not evidence of an edge.",
    }
    OUT.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT.name}")
    print(f"  breakeven at {TP_R}R = {out['zero_cost_binary_breakeven_pct']}%")
    for r in rows:
        print(f"  {r['cohort']:22s} {r['wins']:>2}/{r['n']:<3} "
              f"{r['point_estimate_pct']:>7.3f}%  LB {r['wilson95_lower_bound_pct']:>7.4f}%  "
              f"clears={r['clears_breakeven']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
