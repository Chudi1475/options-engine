"""Compare single-exit vs two-stage (half then full) exit styles on the same
entries. Answers: is adding a second profit point on top of the first smarter?"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backtest import load_data, metrics, run_backtest
from strategy import StrategyConfig

CONFIGS = [
    ("current: all out at +15, stop -60",            None, 15, -60),
    ("half at +15, rest to +40, stop -60",             15, 40, -60),
    ("half at +15, rest to +60, stop -60",             15, 60, -60),
    ("half at +15, rest to +120, stop -60",            15, 120, -60),
    ("half at +25, rest to +90, stop -50",             25, 90, -50),
    ("kelechi's stated: half +60, full +120, stop -30", 60, 120, -30),
]

base = StrategyConfig(direction="call")
print("Loading data once...")
intraday, daily = load_data(base)

print(f"{'style':<48} {'win%':>6} {'expect':>8} {'P&L':>10} {'maxDD':>10}")
for name, half, full, stop in CONFIGS:
    cfg = StrategyConfig(direction="call")
    cfg.take_half_pct = float(half) if half is not None else None
    cfg.take_full_pct = float(full)
    cfg.stop_pct = float(stop)
    trades, _ = run_backtest(cfg, intraday, daily)
    m = metrics(trades)
    print(f"{name:<48} {m['win_rate']:>5.1f}% {m['expectancy_pct']:>+7.1f}% "
          f"{m['total_pnl']:>9,.0f} {m['max_drawdown']:>9,.0f}")
