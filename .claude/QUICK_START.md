# Quick Start
- Env: `.venv\Scripts\activate` · `pip install -r requirements.txt`
- Live bot: `python scanner.py` (one session) · `--daemon` (cloud) · `--dry-run` (print, no texts) · `--test` (all 5 alert types) · `--weekly` (scoreboard now)
- Backtests: `python backtest.py` · `backtest_new_rules.py` (live exit rules) · `backtest_long.py`
- Tests: `python test_pipeline.py` (offline, run before shipping) · `python replay_day.py YYYY-MM-DD`
- Research: `analyze_history.py` · `study_wins.py` · `risk_gate.py --study`
- Deploy: see DEPLOY.md (Railway). NEVER run two scanner copies at once.
