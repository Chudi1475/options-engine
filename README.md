# options-engine

Options strategy engine: trade history analysis, strategy definition, backtesting, and live setup alerts. **Analysis and alerts only — never places orders.**

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Place the Webull options order export at `data/Webull_Orders_Records_Options.csv` (CSV exports are gitignored).

## Phase 1 — trade history analyzer

```
python analyze_history.py [optional/path/to/orders.csv]
```

Outputs to `reports/`:

- `analysis.md` — full report: round trips, win rate, expectancy, distributions, stated-rules-vs-data check
- `equity_curve.png` — cumulative realized P&L
- `round_trips.csv` — FIFO-matched trade log (used by later phases)

How it works: parses OCC option symbols, keeps filled orders only, FIFO-matches buys to sells per contract into round trips. Sells with no prior buy and buys never sold are listed separately and excluded from the stats rather than guessed at.

## Roadmap

- Phase 2 — `strategy.py`: `detect_setup()` + `StrategyConfig` (entry rules to be filled in)
- Phase 3 — `backtest.py`: honest backtest with slippage + fees
- Phase 4 — `scanner.py`: live entry-window scanner with SMS trade cards (Twilio)
- Phase 5 — pre-market risk gate (stub: `is_trading_day_approved()`)
