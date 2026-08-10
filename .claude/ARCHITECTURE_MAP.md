# Architecture Map
- `config.py` — every tunable + state.json helpers
- `strategy.py` — entry signal (15-min momentum turn; `# KELECHI RULE:` placeholders — do not alter signal logic casually)
- `scanner.py` — live service: entries 9:45–10:30 ET, monitoring, Telegram commands, weekly
- `positions.py` — position lifecycle, persistence (positions.json), old-rules shadow sim
- `quotes.py` / `data_feed.py` — Yahoo chains + yfinance; Alpaca upgrade for stocks
- `cards.py` — every Telegram message · `telegram.py` — transport
- `risk_gate.py` — morning GREEN/YELLOW/RED gate · `scoreboard.py` — live stats/weekly
- `recap.py` — 3:05 PM CT self-grading recap
- Deep docs on demand: README.md, DEPLOY.md, DATA_VENDORS.md, BACKLOG.md, SELF_IMPROVE.md
