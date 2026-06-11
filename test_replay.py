"""Replay a historical day through detect_setup to sanity-check the logic."""
import sys
from zoneinfo import ZoneInfo

import yfinance as yf

from scanner import build_card
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

fired = 0
for i in range(len(bars)):
    upto = bars.iloc[: i + 1]
    now = upto.index[-1].to_pydatetime()
    s = detect_setup("SPX", upto, now, cfg)
    if s:
        fired += 1
        if fired <= 2:
            print(f"--- setup at {now:%H:%M} ET ---")
            print(build_card(s, now))
            print()
print(f"{day}: {fired} bars in the entry window fired")
