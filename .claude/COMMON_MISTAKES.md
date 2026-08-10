# Common Mistakes
1. NEVER place/modify/cancel orders. Analysis + alerts only; every card ends "Your call."
2. `BACKTESTED: YES` only if the backtester actually ran on real history, with real metrics printed beside it. No fabricated stats — show the computation.
3. Backtests: no lookahead, include 1.5%-each-way slippage + per-contract fees, report net of costs. Pricing is approximated Black-Scholes — say so on every card.
4. Never run two scanner copies at once (double alerts + Telegram API conflicts).
5. Don't hand-edit runtime state: positions.json, state.json, alerts_sent.jsonl, chat_history.json.
6. Never read or print `.env` (Telegram token, API keys).
7. Alerts only fire for setups with ≥70% backtested win rate AND positive expectancy — don't loosen this gate.
