# Fixed study preregistration

Required by spec section 11. Written to satisfy section 7. **Registered before
collection begins**, which is the only thing that makes it a preregistration
rather than a description of whatever the data turned out to say.

Nothing here is a result. Every number is a plan, and the sample sizes are
computed by `power.py` rather than typed, so they cannot silently drift.

---

## 0. The one question this study exists to answer

**Does the momentum entry have an edge, or is the 74.8% win rate an artifact of
the exit grid?**

The concern, stated precisely so it can be killed or confirmed: with a stop at
-90% and a half sale at +25%, a driftless process between those barriers reaches
the upper one first with probability 90/115 = **78.26%**. The recorded
half-sale frequency is 99/127 = **77.95%**.

That resemblance is a REASON TO TEST, not a verdict. Astra is explicit and it is
worth repeating because the numbers are seductive: options have time decay,
changing volatility, jumps, spreads, finite horizons and discrete monitoring, and
the half-sale frequency is not the final win rate. A continuous barrier model is
not an option pricing model.

If the entry has no edge, the 70% gate is measuring the stop and this whole
strategy needs rethinking. That is the outcome this study must be able to reach.

---

## 1. Declared policy under test

The **current live momentum policy, unchanged**: 15-minute momentum on today's
5m bars, entry window 09:45 to 10:30 ET, allow-list `{SPX:call, SPY:call,
QCOM:call, TSLA:put}`, half at +25%, 40-point give-back trail, hard stop -90%.

No parameter of it moves during collection. Astra decision 1. A study whose
subject changes mid-collection measures nothing.

---

## 2. Primary comparison

Two quantities, both required, declared in advance:

1. **Absolute net expectancy** of the chosen policy, net of the declared cost
   convention.
2. **Incremental net advantage over the declared control**: the chosen contract
   minus the predeclared opposite contract, at the same timestamp.

With both net returns available at an accepted timestamp:

- a fair random direction returns **half the sum** of the two
- the chosen-direction advantage is **half the difference**

**The opposite return is never fabricated by negating the chosen one.** An
option put return is not the negative of a call return; that shortcut would
manufacture the answer.

### Controls, all predeclared

| Control | Role | Selection rule |
|---|---|---|
| `direction_control` | tests DIRECTION | opposite right, same expiry, same absolute delta when a trustworthy timestamped delta exists, otherwise a fixed moneyness rule, labeled either way |
| `strike_control` | tests STRIKE SELECTION | same direction, a different strike near the chosen moneyness or delta. This is the SPX/SPY grid hypothesis (A11, A12) |
| nontrigger time control | tests TIMING | one predeclared non-signal timestamp per session |
| always-call reference | exposes directional drift | reported alongside, never as the primary |

A direction control and a strike control are different questions. Astra A12:
calling all alternatives "opposite strikes" conflates them.

**Missing or unaffordable controls stay in the coverage denominator.** A
timestamp where the control could not be priced is a coverage gap, not an
excluded row.

---

## 3. Cost convention, declared before collection

- Fees and spread are applied to every leg of both the chosen contract and every
  control, identically.
- Integer contract quantities only. **One contract cannot sell half.** The
  legacy fractional model is preserved for comparability and labeled as such; it
  is never published as an executable one-contract return.
- The zero-cost binary breakeven of 71.4286% at 0.4R is an arithmetic identity,
  not an achievable bar. Costs raise it.

---

## 4. Unit of observation, and dependence

The unit is the **independent cluster**, not the trade.

- SPX and SPY co-fire in the same minute. 42 pairs are **not** 84 observations.
- Three recipients' fills are three execution experiences of **one** signal.
- Same-session, same-direction exposure is one cluster.

Analysis groups by trading session and correlated underlying exposure. Any
number computed as though trades were independent is reported as such and is not
the primary.

---

## 5. Economically useful improvement

Declared before collection, so the study cannot be re-scoped afterwards to
whatever it happened to find:

**A per-cluster mean improvement of +0.15R over the direction control.**

Rationale, stated so it can be argued with: below roughly +0.10R the result is
inside the plausible range of the cost and missing-path sensitivities in section
9, so it could not support a decision even if it were real.

---

## 6. Sample size

Computed in `power.json`, one-sided alpha 0.05, power 0.80.

Binary, against the 71.4286% breakeven null:

| To detect | Difference | n (independent observations) |
|---|---|---|
| 80.0% | +8.5714 pts | **159** |
| 78.0% | +6.5714 pts | **276** |
| 75.0% | +3.5714 pts | **962** |

Paired mean difference, in independent clusters:
`n = ((1.645 + 0.842) * sd / improvement)^2`

At the declared +0.15R improvement: 69 clusters at sd 0.5, 155 at sd 0.75, 275
at sd 1.0. **The sd is not known yet.** Obtaining it is the entire purpose of
the 20-session checkpoint below.

**These are normal-approximation planning numbers before costs, clustering,
selection and incomplete outcomes. They are not ready-made sample sizes**, and
the real requirement is larger for the reasons in `power.json`.

---

## 7. Checkpoints, and what each one may and may not do

| After | Review | May do | May NOT do |
|---|---|---|---|
| 5 sessions | data integrity | fix recording defects, coverage gaps, schema errors | look at outcomes |
| 20 sessions | variance and coverage planning | estimate the paired sd, fix the confirmation n, assess coverage | choose a more profitable target, stop early on a good result |

After the 20-session review the confirmation sample is **fixed and frozen**, and
a **separate prospective confirmation cohort** is collected against it. Planning
data is not confirmation data.

**No repeated peeking at conventional intervals, and no stopping at the first
favorable result.** Either the fixed endpoint, or a valid sequential method
declared in advance. There is no third option.

**90 calendar days is a reporting milestone, not statistical proof.**

---

## 8. Promotion rule

All of the following, together:

1. A **complete and trustworthy record**: coverage measured, unresolved statuses
   counted, orphans zero or named.
2. **One-sided 95% lower confidence bound above zero** for absolute net
   expectancy.
3. **One-sided 95% lower confidence bound above zero** for the single primary
   incremental comparison.
4. Session-level dependence respected in both bounds.
5. Acceptable drawdown under a **separately agreed** trial risk limit.

Interpretation, fixed now:

- a confidence interval spanning zero is **inconclusive**, not encouraging
- an upper bound at or below zero **supports rejecting** the tested improvement
- a point estimate above breakeven with a lower bound below it is **not
  evidence of an edge**. All three cohorts we currently quote fail exactly this
  way (`wilson_bounds.json`)

**Additional targets, symbol drops and strike mappings are EXPLORATORY** unless
independently reserved or covered by a declared multiplicity procedure. The 0.4R
versus 1R versus 2R comparison is a named example: picking the best of three on
an exhausted sample is not confirmation.

---

## 9. Sensitivity, published with the result

- to **missing paths**: what the conclusion becomes if unresolved outcomes are
  treated as losses, as wins, and as excluded
- to **execution assumptions**: integer quantities, fees, spread crossing

**If any plausible treatment of those gaps reverses the conclusion, there is no
promotion.** That rule is set now, before anyone knows which way it points.

---

## 10. What this study cannot answer

Stated up front so it is never quietly claimed later:

- It cannot establish an option's percentage return, affordability or fill from
  underlying R comparisons alone.
- It cannot verify a historical bid and ask. No free source establishes a
  contemporaneous OPRA quote for these contracts at these timestamps.
- It cannot prove profitable compounding. Account growth is measured only from
  real cash flows and fills, with deposits and withdrawals separated, and
  summed option percentages are not account growth.
- It cannot settle the -90 versus -50 stop question. That needs real
  contemporaneous option paths, and MAE/MFE bounding does not cover every
  admissible event ordering.
