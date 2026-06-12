"""Does the edge exist outside the morning window? Same honest method as
backtest_new_rules.py (same entries logic, new exits, approx BS pricing,
slippage+fees), but run separately for each hour-of-day entry window.
Answers: should the bot alert all day, or is the morning special?

Writes reports/window_study.md and prints the table.
"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import time
from pathlib import Path

from backtest import load_data, metrics
from backtest_new_rules import run
from strategy import StrategyConfig

REPORTS = Path(__file__).parent / "reports"

WINDOWS = [
    ("9:45-10:30 (current)", time(9, 45), time(10, 30)),
    ("10:30-11:30", time(10, 30), time(11, 30)),
    ("11:30-12:30", time(11, 30), time(12, 30)),
    ("12:30-13:30", time(12, 30), time(13, 30)),
    ("13:30-14:30", time(13, 30), time(14, 30)),
    ("14:30-15:30", time(14, 30), time(15, 30)),
]

print("Loading data once...")
base = StrategyConfig()
intraday, daily = load_data(base)

lines = ["# Entry-window study — new exit rules, approx pricing", "",
         "Same signal, same exits, different time of day. One entry per "
         "direction per day inside each window.", "",
         "| window | setup | trades | win rate | expectancy/trade |",
         "|---|---|---|---|---|"]
print(f"{'window':<22} {'setup':<10} {'trades':>6} {'win%':>6} {'expect':>8}")
for name, start, end in WINDOWS:
    cfg = StrategyConfig()
    cfg.entry_start, cfg.entry_end = start, end
    trades = run(cfg, intraday, daily)
    for key in sorted({f"{t.ticker}:{'call' if t.right == 'C' else 'put'}"
                       for t in trades}):
        tk, dirname = key.split(":")
        right = "C" if dirname == "call" else "P"
        m = metrics([t for t in trades if t.ticker == tk and t.right == right])
        if not m or m["trades"] < 15:
            continue
        row = (name, key, m["trades"], m["win_rate"], m["expectancy_pct"])
        print(f"{name:<22} {key:<10} {m['trades']:>6} {m['win_rate']:>5.1f}% "
              f"{m['expectancy_pct']:>+7.1f}%")
        lines.append(f"| {name} | {key} | {m['trades']} | {m['win_rate']:.1f}% "
                     f"| {m['expectancy_pct']:+.1f}% |")

lines += ["", "Bar: a window/setup earns live alerts only at a (rounded) "
          "70%+ win rate AND positive expectancy, same as the morning."]
(REPORTS / "window_study.md").write_text("\n".join(lines), encoding="utf-8")
print(f"\nWrote {REPORTS / 'window_study.md'}")
