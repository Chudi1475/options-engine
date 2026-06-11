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

## Winners-vs-losers study

```
python study_wins.py
```

Reconstructs the market state at every entry (5-min bars, no lookahead) and compares winners against losers. Writes `reports/win_study.md` and `reports/trips_with_features.csv`. The derived setup feeds `strategy.py`.

## Strategy (`strategy.py`)

`detect_setup()` + `StrategyConfig`. Current entry logic is derived from the win study (15-min momentum positive, 0-1 DTE, after 9:45 ET) — `# KELECHI RULE:` placeholders mark where confirmed rules go. Sanity-check it against any historical day:

```
python test_replay.py 2026-05-13
```

## Live scanner with Telegram alerts (`scanner.py`)

Polls 5-min bars during the entry window (9:45-10:30 ET), texts a trade card to every chat in `TELEGRAM_CHAT_IDS` when a setup forms. 30-min per-ticker cooldown, everything logged to `alerts.log`. **Alerts only — never places orders.**

Setup (once):

1. In Telegram, message **@BotFather** → `/newbot` → copy the token into `.env` as `TELEGRAM_BOT_TOKEN`
2. Both people open the new bot and send it any message
3. `python scanner.py --setup` → prints both chat IDs → put them in `.env` as `TELEGRAM_CHAT_IDS=id1,id2`
4. `python scanner.py --test` → both phones should get a test message

Run it each morning (or via Task Scheduler before 9:45 ET):

```
python scanner.py            # live
python scanner.py --dry-run  # prints cards instead of texting
```

Cards say `BACKTESTED: NO` until the Phase 3 backtester has actually run.

## Backtests

```
python backtest.py        # 60 days of 5-min bars: full strategy + exit-bracket grid
python backtest_long.py   # 2y hourly cousin + 5y daily proxy (labeled)
```

`backtest.py` also writes `reports/backtest_results.json` — the scanner reads it to quote real per-setup win rates and the chosen exit bracket on every alert. Alerts only go out for setups with a backtested win rate ≥ 70% **and** positive expectancy (configurable via `MIN_WINRATE` env var).

## Risk gate (`risk_gate.py`)

```
python risk_gate.py            # today's verdict: 🟢 normal / 🟠 caution / 🔴 stand down
python risk_gate.py --study    # rebuild reports/news_study.md (5y VIX/gap study)
```

The scanner runs this at startup and texts the day verdict to everyone. Add CPI/FOMC/etc. dates to `data/event_days.txt` by hand.

## Sharing

`GUIDE.txt` is a plain-English explainer to send anyone who receives the alerts — what the texts mean and how to execute them (Robinhood steps included).
