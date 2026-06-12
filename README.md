# options-engine

Options strategy engine: trade history analysis, strategy definition, honest backtesting, and a live Telegram alert service with all-day position tracking. **Analysis and alerts only — never places orders.**

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # fill in the Telegram token + chat ids
```

## The live bot (`scanner.py`)

```
python scanner.py            # one trading session (Task Scheduler mode)
python scanner.py --daemon   # run forever (cloud mode, see DEPLOY.md)
python scanner.py --dry-run  # print cards instead of texting
python scanner.py --setup    # print chat IDs of people who messaged the bot
python scanner.py --test     # fire a fake signal through all 5 alert types
python scanner.py --weekly   # send the weekly scoreboard now
```

What it does each trading day (ET):

- **morning** — risk mode DM: 🟢 GREEN / 🟡 YELLOW / 🔴 RED, from the free
  ForexFactory econ calendar (FOMC/CPI/PPI/NFP = RED) + VIX + overnight gap
  (+ optional web-search news check with `ANTHROPIC_API_KEY`). RED halves
  all suggested sizes. Override any time: text `/risk red` to the bot.
- **9:45–10:30** — entry window. The signal (15-min momentum turn,
  `strategy.py`) is unchanged from the win study; alerts only fire for
  setups with a ≥70% backtested win rate **and** positive expectancy.
  Entry cards lead with **expected value per trade** (the honest stat),
  show the option's live bid/ask + a limit price, and size every trade so
  a full stop-out costs exactly 1% of the account (`/setaccount`).
- **all day** — every alert becomes a tracked position (`positions.json`,
  survives restarts). The bot texts each exit step: **SELL HALF at +25%**,
  then a momentum-flip trail for the rest, **hard stop −30%**, and a
  **close-before-expiry** warning 15 min before the bell. Each position
  also runs an old-rules (+15/−60) shadow sim on the same prices.
- **Friday after close** — weekly scoreboard: live win rate, EV/trade,
  new-rules vs old-rules totals, vs what the backtests claimed.

Telegram commands: `/setaccount 25000`, `/risk green|yellow|red`,
`/status`, `/test`, `/help`.

`PAPER_MODE=true` in `.env` tags every card `[PAPER]` for a practice trial.

## Module map

| file | job |
|---|---|
| `config.py` | every tunable in one block + state.json helpers |
| `strategy.py` | the entry signal (unchanged; `# KELECHI RULE:` placeholders) |
| `scanner.py` | the live service: entries, monitoring, commands, weekly |
| `positions.py` | position lifecycle + persistence + old-rules shadow |
| `quotes.py` | Yahoo option-chain bid/ask, labeled BS estimate fallback |
| `cards.py` | every Telegram message, 6th-grader readable |
| `risk_gate.py` | GREEN/YELLOW/RED morning gate (calendar + VIX + gap) |
| `scoreboard.py` | live stats, weekly report, live-replaces-backtest logic |
| `data_feed.py` | yfinance default; auto-upgrades stocks to Alpaca real-time |
| `recap.py` | daily 3:05 PM CT self-grading recap |

## Backtests

```
python backtest.py             # 60d of 5-min bars: old exit grid + per-setup stats
python backtest_new_rules.py   # the LIVE exit rules (+25 half / flip trail / -30)
python backtest_long.py        # 2y hourly cousin + 5y daily proxy (labeled)
```

Both backtests use approximated Black-Scholes pricing (no free historical
chains exist) with 1.5%-each-way slippage and per-contract fees, and say so
on every card. `reports/backtest_results.json` and
`reports/backtest_new_rules.json` are committed so a fresh clone can alert
with real stats. See `DATA_VENDORS.md` for the paid-data upgrade path.

## Tests

```
python test_pipeline.py        # offline: every exit path, persistence, sizing
python test_replay.py 2026-05-13   # replay a historic day end to end
python scanner.py --test       # live: all 5 alert types to your phone
```

## History & research

```
python analyze_history.py      # phase 1: Webull export -> round trips report
python study_wins.py           # winners-vs-losers study (the signal's origin)
python risk_gate.py --study    # 5y VIX/gap regime study
```

## Deploying

`DEPLOY.md` — Railway in ~6 commands ($5/mo), persistent volume, plus free
Alpaca keys for real-time stock data. One rule: never run two copies at
once (double alerts + Telegram conflicts).

## Sharing

`GUIDE.txt` is the plain-English explainer to send anyone who receives the
alerts — what every message means and how to execute on Robinhood.
