# Stop-loss research: what the hard stop should be

Question asked: the live hard stop is -90% of premium. That is not what real
option traders use. Should it be around -30%?

Answer: **no. -30% is refuted by five independent lines of evidence, including
this repo's own data. The researched answer is -50%, and it cannot ship alone.**

Companion measurement: `bt_exp_stop_tight.json` (this repo, run 2026-08-07).
Everything below is sourced; nothing here is a number typed from memory.

---

## 1. What the strategy actually is

Long single-leg calls and puts. 0DTE on SPX/SPY, nearest weekly on TSLA/QCOM.
Entered on a 15-minute momentum continuation in the 9:45-10:30 ET window,
first strike beyond spot. Half sold at +25%, runner trailed by a 40-point
give-back, hard stop -90%. Sizing is risk-based: position = 1% of account
divided by the stop width.

This matters because almost all published 0DTE statistics describe premium
SELLERS and invert when applied to a buyer. Option Alpha's 25,000-trade 0DTE
study found 87% of positions were opened for a net credit. Seller stop-loss
research (Option Alpha: a 25% stop on 30-day 15-delta strangles returned 1,489%
vs 3,140% with no stop) says stops destroy returns, because a seller's loss is
unbounded and mean reversion rescues the position. Neither mechanic exists for
a buyer: loss is capped at 100% and theta runs against you the whole hold.

## 2. What actual long-premium intraday traders use

The center of gravity is **-50% of premium**, not -30%. Four independent
published plans land on the same number:

| source | rule |
|---|---|
| Options Cafe, backtested 0DTE SPY ORB | "Exit when the option price falls to -50% of the entry price", +100% target, 3:30pm time stop |
| SPXXL, 0DTE SPX ORB | "If the spread drops to 50% of your entry debit, exit" |
| Market Rebellion, published 0DTE plans | underlying trigger OR "the option decays to 50% of its value" |
| QuantVPS 0DTE survey | directional traders "enforce a strict 50% stop-loss policy" |

**The only real backtest of this exact strategy shape.** Options Cafe, buying
ATM 0DTE SPY on a morning breakout, Feb 2024 - Mar 2026: 303 trades, 41.3% win
rate, average winner $417.66 vs average loser $209.82 (1.99x payoff), profit
factor 1.40, +59.4% total, 7.6% max drawdown, 92-minute average hold. Exit
breakdown: **174 of 178 losses hit the -50% stop**; only 9 trades reached the
time stop. Even at -50%, a full stop is taken on 57% of all trades.

The one source that endorses 30% uses it only as an **afternoon-tightened
trailing stop** (0-dte.com: "starts loose, 50-75% in the morning, and tightens
to 30% by afternoon"). That is the opposite of applying 30% at a morning entry.

Options Cafe's own 105-configuration sweep found a 40% stop beat 50% in
isolation, but degraded when stacked with other optimized parameters; the
authors cite it as an overfitting warning and did not adopt it. That is the
closest evidence in favor of tightening, and it stops at 40%, not 30%.

## 3. Why -30% fails mechanically on 0DTE

**Theta alone fires it with zero adverse move.** With the index held exactly
flat from a 10:00 ET entry, premium remaining: 11:00 -10.7%, 12:00 -22.6%,
13:00 -35.9%, 14:00 -51.6%, 15:00 -71.5%. A -30% stop is breached by decay
alone by roughly 1pm on a trade whose thesis is still completely intact.
Corroborated by TOS Indicators ("a contract purchased at 10:00 AM may lose
60-80% of its value by 2:00 PM if the underlying stays flat") and by
Days to Expiry's hour-by-hour table (~20% hourly decay through the morning).
**A -30% premium stop on 0DTE is not a stop. It is a disguised ~90-minute
time stop that fires at whatever the spread happens to be quoting.**

**It is inside one bar of noise.** Converting each stop into the adverse SPX
move that triggers it, and dividing by a 1-sigma 15-minute move: -30% is
about 1.2 sigma, -50% about 2.2 sigma, -90% about 6.3 sigma. Volatility Box's
false-trigger research (200 S&P setups 2020-2024, validated on 595+ symbols)
puts stops below 1.0x ATR at a 65%+ false-trigger rate and 1.5x ATR at 38%.
Monte Carlo with the underlying going nowhere: P(touch -30%) is 37% over 30
minutes, **55% over 60 minutes**, 66% over 120. P(touch -90%) is 0.0% / 0.3% /
2.8%.

**It kills winners at a measured rate.** With the momentum thesis working, of
every path that eventually reached +25%, the -30% stop killed 25-29% of them
first; the -50% stop killed 10-13%.

**Slippage means it does not fill at -30%.** SPX 0DTE spreads run $0.50-$1.50
normally and $3.00+ during fast moves, and a triggered stop becomes a market
order. A -30% trigger realistically prints -40% to -50%, so the tighter the
stop, the larger slippage is as a share of intended risk.

**The theory says this horizon is the worst possible one for a stop.**
Kaminski & Lo, *When Do Stop-Loss Rules Stop Losses?* (Journal of Financial
Markets, 2013) prove the stopping premium is always negative under a random
walk, and positive only when return autocorrelation exceeds the Sharpe ratio
at the trade's own frequency. Baule, Schlie & Zhou (2025, TAQ tick data
2017-2021) find intraday autocorrelation reaches its **most negative value,
-2.70%, at exactly the 15-minute horizon**. This strategy's signal horizon is
15 minutes, so the condition for a tight stop to add expected return fails by
construction.

## 4. What this repo's own data says

`bt_exp_stop_tight.json`, 220 identical allow-list entries, 60 sessions,
half +25 / give-back 40 fixed, only the stop varying:

```
stop    win%   stop-out%   acct ret/trade   setups clearing the 70% gate
-30     48.2      52.7        0.2757              0 of 4
-50     60.0      39.5        0.2532              0 of 4
-70     68.2      30.0        0.2544              2 of 4
-80     70.9      26.8        0.2436              3 of 4
-90     74.1      23.6        0.2298              3 of 4   <- live
```

Two things fall out.

**Tightening to -30% silences the bot.** Every setup drops to 46-55% win rate,
nothing clears `MIN_WINRATE`, and the scanner never fires again. Going from
-90% to -30% roughly doubles the stop-out rate (23.6% -> 52.7%) and cuts win
rate by almost exactly the same amount (74.1% -> 48.2%): nearly every extra
stop-out is a trade that would otherwise have won.

**The earlier study picked -90% on the wrong metric.**
`bt_exp_stop_walkforward.json` only ever compared -70/-80/-90 and ranked them
on per-contract expectancy. On its own sizing-fair account-return column the
ranking is inverted (-70: 0.2796, -80: 0.2626, -90: 0.2420), because
risk-based sizing makes a wider stop buy a smaller position. So -90% was never
validated as best; it was best-of-three-wide-options on a metric that does not
match how the bot sizes.

Caveat that cuts against tight stops even harder: `backtest_param_sweep.sim`
checks the stop only at **5-minute bar closes**, so a real -30% stop would be
hit intrabar more often than the table shows. These tight-stop numbers are an
upper bound.

## 5. The finding that matters most

At a -90% stop, a **driftless random walk** banks the +25% half about 70% of
the time. The bot's 70% win-rate floor is therefore satisfied by pure noise:
it is measuring the stop, not the quality of the setup. External calibration
agrees that a genuine 70% long-premium win rate is implausible: OptionScout's
2026 retail report puts directional call and put buyers at 35-40%, and
Bogousslavsky & Muravyev's trader-level data on 309,471 option buy trades has
a median net return of 0% with a 75th percentile of only +11%.

At -50% the same rate is 63% at no edge and 74% at a strong edge, which makes
a 70% floor a real discriminator that only good setups clear.

**Before changing the stop, verify the 70% win rate reflects an edge at all
rather than the absence of a stop.** If it does not, no stop level fixes it.

## 6. Recommendation

Not -30%. Not -90%. **-50% of premium for SPX/SPY 0DTE, -45% for TSLA/QCOM
weeklies**, and never one flat number across all four tickers. -50% is where
the practitioner consensus, the only real backtest of this shape, the sigma
math, and this repo's own 40-point give-back trail (which is already about
-49% of entry premium) all converge. The runner is currently managed twice as
tightly as the initial position, which is backwards.

It cannot ship alone. Three things must go with it:

1. **A hard time stop, 60-90 minutes, flat by 3:00pm ET.** Widening away from
   -30% deliberately decouples the exit from theta, so the clock has to be
   handled explicitly. This removes the dead-money trades a -30% stop was
   trying to catch, at no noise cost.
2. **Keep -90% as a disaster backstop** for gaps, halts and non-fills, not as
   the primary control.
3. **Recalibrate `MIN_WINRATE`.** At -50% no current setup clears 70%, so the
   floor must drop (60% is the researched level) or the bot goes silent.

Separately, and larger than the stop: **the +25% half-off is the bigger
defect.** At roughly 0.96 sigma it is barely outside noise, and it truncates
the only part of the distribution that pays. Moving the first scale-out to
+50% or +100% improved risk-adjusted return more than any stop change tested.

## 7. The test that settles it with our own numbers

Pull every historical alert and record maximum adverse excursion **in
underlying points, not premium percent** (premium MAE is contaminated by theta
and overstates the room needed). Plot MAE for winners only and place the stop
just past the level containing 75-85% of winners. If 80% of winners never went
more than ~15 SPX points against us, -30% is right and this document is wrong.
Needs 50 trades minimum, 100+ for a distribution worth acting on.

## Sources

- Options Cafe, 0DTE SPY opening-range-breakout backtest (303 trades) — https://options.cafe/blog/0dte-opening-range-breakout-strategy-spy-backtested-results/
- SPXXL, 0DTE SPX ORB structure — https://spxxl.com/blog/orb-opening-range-breakout
- Market Rebellion, 0DTE plans with real examples — https://marketrebellion.com/news/trading-insights/how-to-trade-0dte-options-with-real-life-examples/
- QuantVPS, 0DTE SPY guide — https://www.quantvps.com/blog/0dte-spy-options
- 0-DTE.com, morning-loose/afternoon-tight trailing stop — https://0-dte.com/0dte-asymmetric-strategy/steal-my-0dte-strategy/
- TOS Indicators, TSLA and SPX 0DTE decay research — https://tosindicators.com/research/spx-0dte-options-data-breakdown-day-trading
- Volatility Box, volatility-adjusted stops and false-trigger rates — https://volatilitybox.com/research/
- DayTradingToolkit, underlying-based stop rule — https://daytradingtoolkit.com/strategies/day-trading-options-for-beginners
- Pat Crawley (SteadyOptions), why not to use premium stop orders — https://steadyoptions.com/articles/why-you-should-never-use-a-stop-loss-in-options-trading-r736/
- Kaminski & Lo, *When Do Stop-Loss Rules Stop Losses?*, J. Financial Markets 2013 — https://dspace.mit.edu/bitstream/handle/1721.1/114876/Lo_When%20Do%20Stop-Loss.pdf
- Baule, Schlie & Zhou, *The Term Structure of Intraday Return Autocorrelations*, CESA WP 14, 2025
- Bogousslavsky & Muravyev, *An Anatomy of Retail Option Trading*, SSRN 4682388
- Option Alpha, 0DTE study (87% opened for credit) and stop-loss backtests — https://optionalpha.com/blog/0dte-options-time-decay
