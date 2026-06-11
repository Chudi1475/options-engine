"""Replay a historical day through detect_setup to sanity-check the logic."""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from zoneinfo import ZoneInfo

import yfinance as yf

from scanner import MIN_WINRATE, build_card, load_backtest, setup_stats
from strategy import StrategyConfig, detect_setup

day = sys.argv[1] if len(sys.argv) > 1 else "2026-05-13"
end = day[:-2] + f"{int(day[-2:]) + 1:02d}"

ET = ZoneInfo("America/New_York")
cfg = StrategyConfig()
bars = yf.download("^GSPC", start=day, end=end, interval="5m",
                   progress=False, auto_adjust=False)
if hasattr(bars.columns, "levels"):
    bars.columns = bars.columns.get_level_values(0)
bars.index = bars.index.tz_convert(ET)

backtest = load_backtest()
fired = sent = 0
for i in range(len(bars)):
    upto = bars.iloc[: i + 1]
    now = upto.index[-1].to_pydatetime()
    s = detect_setup("SPX", upto, now, cfg)
    if s:
        fired += 1
        stats = setup_stats(s, backtest)
        ok = stats and stats["win_rate"] >= MIN_WINRATE and stats["expectancy_pct"] > 0
        if ok and sent == 0:
            sent += 1
            print(f"--- card that would be sent at {now:%H:%M} ET ---")
            print(build_card(s, now, backtest, stats))
            print()
        elif not ok and fired <= 3:
            why = "no stats" if not stats else (
                f"win rate {stats['win_rate']:.0f}%" if stats["win_rate"] < MIN_WINRATE
                else "negative expectancy")
            print(f"{now:%H:%M} {s.ticker} {s.direction}: would be SKIPPED ({why})")
print(f"{day}: {fired} setups formed, picky filter would text the first passing one")
