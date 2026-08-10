# Backtest honesty rules
- No lookahead: only data available at decision time.
- Costs: 1.5% each-way slippage + per-contract fees, results net of costs.
- backtest.py = old exit grid · backtest_new_rules.py = LIVE rules (+25% half / flip trail / -30% stop).
- reports/backtest_results.json + backtest_new_rules.json are committed so fresh clones alert with real stats.
- BACKTESTED: YES only when the backtester really ran; print real metrics beside it.
