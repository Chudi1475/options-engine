"""Send a TEST-labeled sample trade card to Telegram, built from a real
historical replay so it looks exactly like a live alert."""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from zoneinfo import ZoneInfo

import yfinance as yf

from scanner import (MIN_WINRATE, build_card, load_backtest, load_env,
                     setup_stats, telegram_send)
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

load_env()
backtest = load_backtest()
for i in range(len(bars)):
    upto = bars.iloc[: i + 1]
    now = upto.index[-1].to_pydatetime()
    s = detect_setup("SPX", upto, now, cfg)
    if not s:
        continue
    stats = setup_stats(s, backtest)
    if stats and stats["win_rate"] >= MIN_WINRATE and stats["expectancy_pct"] > 0:
        card = ("🧪 TEST ALERT — NOT A REAL TRADE 🧪\n"
                f"(replay of {day} — this is what a live alert looks like)\n\n"
                + build_card(s, now, backtest, stats))
        errors = telegram_send(card)
        print("Sent." if not errors else f"Errors: {errors}")
        break
else:
    print("No passing setup found that day.")
