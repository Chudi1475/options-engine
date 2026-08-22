# 2026-08-22: sniper in the US session only, exits graded like the backtest, brain goes quiet on an empty balance

Owner complaint (8/21 screenshots): sniper tickets at 06:03, 06:27 and 06:43 CT, a
"TARGET HIT" on a 0.26-point SPY target at 07:28 CT, "Claude usage limit hit" /
"wait is over" texts all day, a false "data feed returned nothing" heartbeat, six
BREAKING texts (three of them one Canada-tariff story), em dashes everywhere.

## Root causes found
- `fvg._SNIPER_MIN_HOUR_ET = 7` let the sniper fire from 07:00 ET on `prepost=True`
  bars. The round-6 backtest never held a pre-market stock bar and never signalled a
  stock before 10:00 ET (RTH-only data, SKIP_FIRST_BARS=4). Every pre-market stock
  ticket was outside the validated pattern; the "hit" was a delayed pre-market print
  crossing a target smaller than the data's noise.
- `assistant._looks_like_usage_limit` classified HTTP 400 "credit balance is too
  low" as a usage limit: 5h05m countdown, text, "wait is over" text, fail again,
  repeat. The balance is $0 (owner must top up).
- `Service.health_check` measured staleness from the last successful bars fetch, but
  nothing fetches bars once the last position closes: ten idle minutes read as a
  dead feed.
- `news.HOT_WORDS` flagged lawsuits, recalls and opinion columns on the market-wide
  wires; no story-level dedup across outlets.

## Shipped
- Window: `fvg._SNIPER_OPEN_ET = 09:50 ET` (8:50 AM CT) to the 16:00 close for all
  five symbols; `fvg.sniper_window_open()`; `SNIPER_RTH_SYMBOLS`; 5 completed
  session bars minimum; 12-minute bar-age guard.
- Bars: `market_tools._sniper_bars()` (completed bars, regular session for
  stocks/index, today only). The gate's direction, momentum, plan, ATR and
  confirming gap all come from that frame; the forward ledger records in the same
  units.
- Exits: `sniper_book._walk_bars()` grades completed bars stop-first (tie = loss)
  from the fill bar, polled price only as fallback; `exit_via` says which; stale
  rows from an earlier session settle flat and are never graded on a new day;
  SETTLE_ET 16:00 after the 15:55 bar; the watcher keeps stepping open trades until
  16:15 and force-settles them.
- Record: `rescore_round6_session.py` replays the round-6 winner with the window
  inside `passes()` (reproduces the published 133 first): OOS **43/53 = 81.1%**,
  avgR +0.154 (all 96/115 = 83.5%). `strategy_spec` reads it from
  `reports/chart_backtest_round6_session.json`; cards say "hit target 43 of 53 in
  out-of-sample testing (81 of 100)". Looks ledger 26 -> 27.
- Brain: persisted billing hold, one owner text per hold (only once Telegram
  accepted it), 6h probe, one "back online" text; rate-limit cooldowns silent;
  `is_outage_text()` keeps every outage note out of members' "Quick read".
- Heartbeat: feed-dead needs a fetch that was tried after the last success.
- News: `MACRO_HOT` on market wires, `HOT_WORDS` per ticker, `same_story()` dedup
  (light stemming), ETFs skip earnings lookups.
- Text: every em dash out of message strings (`test_no_em_dash.py` guards it,
  docstrings exempt); morning card names the sniper window; `/status` and
  `/health` show the window and the brain state; record line reads
  "N trades, W wins, S stops. Net xR. A 0.4R target needs 71 wins in 100 just to
  break even."; weekly "+303%" is labeled a sum with the per-trade average.

## Eligibility diff (rule 6)
24 replays leave (all forex, all outside 09:50-16:00 ET), 6 later EUR/USD signals
the blocked slot used to hide come in. Stock/index rows unchanged. Full lists in
the report's `dropped` / `added`.

## Tests
`test_session_fixes.py` (new, ~90 checks), `test_no_em_dash.py` (new), settle time
in `test_pipeline.py`. All wired into `self_improve.py`.

## Still open, owner's call
The live sniper record is at breakeven (20 of 28 at 0.4R = 0R). The forward ledger
says 2R from the same entries pays ~8x better per trade. Changing the target is a
strategy change and was not made.
