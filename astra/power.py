"""Sample-size planning numbers for the preregistered study.

WHY THIS EXISTS
---------------
Astra's spec section 7 supplies scale illustrations: roughly 159 observations to
distinguish 80% from 71.429%, 276 to distinguish 78%, 962 to distinguish 75%,
one-sided 5% with 80% power. I do not want those numbers in a document as typed
digits, for the same reason the Wilson bound was wrong for weeks: a figure
copied from a document does not get re-derived when someone questions it.

So they are computed here, and PREREGISTRATION.md reads them from the JSON.

WHAT THESE NUMBERS ARE NOT
--------------------------
Astra is explicit and it must survive into any document quoting them: "These are
normal-approximation planning numbers before costs, clustering, selection and
incomplete outcomes. They are not ready-made sniper sample sizes."

Three reasons the real requirement is LARGER, all of which apply here:
  - clustering. SPX and SPY co-fire in the same minute, so 42 pairs are not 84
    independent observations. The effective n is nearer the number of sessions.
  - incomplete outcomes. Unresolved rows are not losses and cannot be silently
    dropped either.
  - costs. Breakeven at 71.4286% is the ZERO COST binary identity 1/(1+r). Any
    spread, fee or slippage moves the bar up, so the alternative being tested
    has to clear more than these numbers assume.

    python power.py
"""

import json
import math
import pathlib

OUT = pathlib.Path(__file__).parent / "power.json"

Z_ALPHA_ONE_SIDED_05 = 1.6448536269514722   # Phi^-1(0.95)
Z_BETA_80 = 0.8416212335729143              # Phi^-1(0.80)

TP_R = 0.4
BREAKEVEN = 1.0 / (1.0 + TP_R)              # 0.714285..., the zero-cost identity


def n_binary(p1: float, p0: float = BREAKEVEN,
             z_a: float = Z_ALPHA_ONE_SIDED_05, z_b: float = Z_BETA_80) -> int:
    """Independent-binary sample size, normal approximation, one sided.

        n = ( z_a*sqrt(p0(1-p0)) + z_b*sqrt(p1(1-p1)) )^2 / (p1-p0)^2

    Rounded UP, because a fractional observation does not exist and rounding
    down would quietly under-power the test."""
    d = p1 - p0
    if d <= 0:
        raise ValueError("the alternative must be above the null")
    num = (z_a * math.sqrt(p0 * (1 - p0)) + z_b * math.sqrt(p1 * (1 - p1))) ** 2
    return math.ceil(num / (d * d))


def n_paired(sd: float, improvement: float,
             z_a: float = Z_ALPHA_ONE_SIDED_05, z_b: float = Z_BETA_80) -> int:
    """Paired mean difference, in INDEPENDENT CLUSTERS not trades.

        n = ((z_a + z_b) * sd / improvement)^2

    The unit is the cluster (a session, or a correlated underlying exposure
    within a session). Feeding this trade counts is the error Astra names: 42
    SPX/SPY pairs are not 84 observations, and three recipients' fills are three
    execution experiences of ONE signal."""
    if improvement <= 0:
        raise ValueError("declare a positive economically useful improvement")
    return math.ceil((((z_a + z_b) * sd) / improvement) ** 2)


def main():
    binary = []
    for p1 in (0.80, 0.78, 0.75):
        binary.append({
            "alternative_win_rate_pct": round(100 * p1, 4),
            "null_is_breakeven_pct": round(100 * BREAKEVEN, 4),
            "detectable_difference_pct": round(100 * (p1 - BREAKEVEN), 4),
            "n_independent_observations": n_binary(p1),
        })

    # illustrative paired sizes across a range of per-cluster spreads. The real
    # sd is not known yet: it comes from the 20 session planning review, which
    # is the whole reason that checkpoint exists.
    paired = []
    for sd in (0.5, 0.75, 1.0):
        for imp in (0.10, 0.15, 0.20):
            paired.append({
                "paired_sd_R": sd,
                "declared_useful_improvement_R": imp,
                "n_independent_clusters": n_paired(sd, imp),
            })

    out = {
        "kind": "planning",
        "not_a_measurement": "these are sample sizes to plan with, not results",
        "test": "one sided, alpha 0.05, power 0.80, normal approximation",
        "z_alpha_one_sided_0.05": Z_ALPHA_ONE_SIDED_05,
        "z_beta_0.80": Z_BETA_80,
        "target_r": TP_R,
        "zero_cost_binary_breakeven_pct": round(100 * BREAKEVEN, 4),
        "binary_vs_breakeven": binary,
        "paired_mean_difference": paired,
        "why_the_real_requirement_is_larger": [
            "clustering: SPX and SPY co-fire in the same minute, so trades are "
            "not independent observations and the effective n is nearer the "
            "session count",
            "incomplete outcomes: unresolved rows are neither losses nor "
            "droppable",
            "costs: breakeven 71.4286% is the zero-cost identity 1/(1+r); any "
            "spread, fee or slippage raises the bar the alternative must clear",
            "selection: any target, symbol or strike mapping chosen after "
            "looking is exploratory and needs its own reserved evidence",
        ],
        "reconciles_with_spec": {
            "astra_said_159_for_80pct": n_binary(0.80) == 159,
            "astra_said_276_for_78pct": n_binary(0.78) == 276,
            "astra_said_962_for_75pct": n_binary(0.75) == 962,
        },
    }
    OUT.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT.name}")
    print(f"  breakeven {out['zero_cost_binary_breakeven_pct']}%  (null)")
    for b in binary:
        print(f"  detect {b['alternative_win_rate_pct']:.2f}% vs breakeven "
              f"(+{b['detectable_difference_pct']:.4f} pts) -> "
              f"n = {b['n_independent_observations']}")
    print("  spec reconciliation:", out["reconciles_with_spec"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
