# 1-minute options data — window shopping (June 2026)

For the honest backtest upgrade: real historical option chains instead of
Black-Scholes approximation. **Don't buy yet** — the scoreboard is already
building a free honest dataset one live day at a time. Buy when there's
money to spare and 30+ live signals to validate against.

## the short list

| Vendor | Cheapest fit | History | SPX/SPXW included? | Notes |
|---|---|---|---|---|
| **ThetaData** "Options Value" | **$40/mo** | back to Jan 2020 | YES, all tiers | 1-min OHLC + NBBO quotes + greeks. Runs a local "Theta Terminal" (Java) exposing a REST API. Best monthly-subscription fit for us. |
| **Databento** (OPRA dataset) | **$0/mo + pay per download** (~$125 free signup credit) | back to Apr 2013 | YES (`SPX.OPT` parent symbol) | Minute NBBO (cbbo-1m) + trades. Best for a one-time bulk archive pull — the free credit alone may cover our tickers. |
| **Massive** (formerly Polygon.io) | $29/mo "Options Starter" | 2 years only | yes, but index *level* needs a separate $49/mo Indices plan | 1-min bars only at this tier — real quotes require the $199/mo Advanced plan. Weakest fit. |
| **OptionsDX** | ~$20-50 one-time per symbol-year | varies | yes | Flat CSV downloads (1-min with greeks/IV). No API, no subscription. Cheapest way to test whether real chains change our results. |
| CBOE DataShop | priced per order | deep | yes | Official source, custom CSV orders. Fine for a one-off; watch the add-ons. |

## recommendation when the time comes

1. **First $0:** open a Databento account, use the ~$125 signup credit to
   pull SPX/SPXW + QCOM 1-minute NBBO for the exact days the scoreboard has
   tracked live, and check: do real chains agree with what the bot alerted?
2. **If we want a rolling subscription:** ThetaData Options Value at $40/mo
   — 6+ years of 1-min SPX option quotes covers a proper multi-year honest
   backtest of the exact strategy.
3. Re-verify prices before buying — plans change. (All figures checked
   June 2026 against the vendors' own pricing pages.)

## why this matters

The current backtest prices options with Black-Scholes on realized vol and
says so on every card ("approx pricing"). Real chains would replace the
weakest assumption in the whole system — especially for 0DTE SPX, where BS
on realized vol tends to underprice premium and overstate returns.
