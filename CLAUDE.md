# CLAUDE.md

**Quick-start guide for Claude Code — complete details in linked docs**

---

## Project Overview

Options strategy engine: trade-history analysis, honest backtesting, and a live Telegram alert service with all-day position tracking. **Analysis and alerts only — it NEVER places, modifies, or cancels orders.** Every card ends "Your call."

**Tech Stack**: Python 3 (pandas, matplotlib, yfinance, curl_cffi) · Telegram Bot API · Railway (worker: `scanner.py --daemon`)

---

## Session Start Protocol ⚡

**MANDATORY** at start of each session:

```bash
# Load essential docs (~800 tokens - 2 min read)
✓ .claude/COMMON_MISTAKES.md      # ⚠️ CRITICAL - honesty rules, read FIRST
✓ .claude/QUICK_START.md          # Essential commands
✓ .claude/ARCHITECTURE_MAP.md     # Module map
```

**At task completion:**
- Create completion doc in `.claude/completions/YYYY-MM-DD-task-name.md`
- Move session file to `.claude/sessions/archive/` (if created)

**⚠️ NEVER auto-load:**
- Files in `.claude/completions/`, `.claude/sessions/`, `docs/archive/` (0 token cost)
- Runtime state: positions.json, state.json, *.jsonl, logs/, reports/, data/
- Deep docs (on demand only): README.md, DEPLOY.md, BACKLOG.md, DATA_VENDORS.md, SELF_IMPROVE.md, GUIDE.txt

---

## Non-Negotiables

1. No order execution, ever.
2. No fabricated stats; `BACKTESTED: YES` only when the backtester really ran.
3. Run `python test_pipeline.py` before shipping changes to scanner/positions/exits.
4. Never run two scanner copies at once.

---

**Last Updated**: 2026-07-12
**Optimized with**: [Claude Token Optimizer](https://github.com/nadimtuhin/claude-token-optimizer)

---

## Working rules (owner-issued, 2026-08-07)

This repo is an options-alert bot. It analyzes and texts Telegram cards.
It NEVER places orders. Never propose or add execution.

### Standing constraints

1. Do not change strategy behavior unless a task explicitly says to.
   Off limits without explicit instruction: fvg.py SNIPER constants,
   config.py exit thresholds (TP_HALF_PCT, STOP_PCT, RUNNER_GIVEBACK_PCT,
   MIN_WINRATE, RISK_PER_TRADE_PCT), the setup allow-list, entry windows,
   and the symbol rosters in strategy.py and fvg.py.
2. The frozen dataset in data/backtest_frozen/ has had 26 out-of-sample
   looks and is exhausted. Never tune, sweep, or select on it.
   Re-SCORING one already-fixed config is allowed, and every such run must
   be labeled a measurement in the report, not a selection.
3. Never introduce a number that does not trace to a report file.
   Any stat shown in a card, prompt, README, or digest must read from
   code, not be typed by hand.
4. Appendix A of KELBOT_BRIEF.md (Desktop#2/kelbot-review-packet) is a
   work queue, not a discussion topic. Do not write reviews, summaries,
   or restatements of it. Ship fixes.
5. Every change ships with a test that fails before the change and passes
   after. Add it to the suite self_improve.py runs.
6. Any change that touches signal eligibility must print a before/after
   diff of which setups would alert. Do not commit until that diff is
   explained line by line.

### How to work

Investigate before editing. Read the actual code path, do not trust the
brief. If the brief and the code disagree, the code wins and you flag it.
One task per session. When the task is done, stop and report. Do not
start the next item. If a task turns out to require a strategy change to
do properly, stop and say so instead of making the change.
