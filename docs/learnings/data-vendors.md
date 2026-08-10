# Historical options data (see DATA_VENDORS.md for full table)
- Backtests currently use Black-Scholes approx pricing — no free historical chains exist. Every card says "approx pricing".
- Don't buy data yet: scoreboard builds a free honest dataset one live day at a time. Buy after 30+ live signals.
- When time comes: 1) Databento free ~$125 credit for a validation pull, 2) ThetaData Options Value $40/mo for 6y of 1-min SPX quotes.
- Caveat: BS on realized vol tends to underprice 0DTE SPX premium and overstate returns.
