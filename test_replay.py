"""Replay a historical day through detect_setup + the picky filter and print
the entry card that would have been sent (entry priced with the labeled
estimate — historical quotes aren't free).

Usage: python test_replay.py [YYYY-MM-DD]
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

import cards
import config
import quotes
import scoreboard
from backtest import expiry_for, realized_vol
from positions import Position, PositionBook
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
d1 = yf.download("^GSPC", period="1y", interval="1d",
                 progress=False, auto_adjust=False)
if hasattr(d1.columns, "levels"):
    d1.columns = d1.columns.get_level_values(0)

bt_old = scoreboard.load_report("backtest_results.json")
bt_new = scoreboard.load_report("backtest_new_rules.json")
book = PositionBook(config.DATA_DIR / "positions_dryrun.json")

fired = sent = 0
for i in range(len(bars)):
    upto = bars.iloc[: i + 1]
    now = upto.index[-1].to_pydatetime()
    s = detect_setup("SPX", upto, now, cfg)
    if not s:
        continue
    fired += 1
    gate = (bt_old or {}).get("per_setup", {}).get(f"SPX:{s.direction}")
    ok = gate and gate["win_rate"] >= config.MIN_WINRATE and gate["expectancy_pct"] > 0
    if ok and sent == 0:
        sent += 1
        sigma = realized_vol(d1["Close"], now.date())
        expiry_dt = expiry_for("SPX", now)
        est = quotes.estimate_premium(s.spot, s.strike, "C", expiry_dt, now, sigma)
        display = scoreboard.stats_for_card("SPX", s.direction, book, bt_old, bt_new)
        pos = Position(
            id="replay", date=day, time_et=now.strftime("%H:%M:%S"),
            ticker="SPX", direction=s.direction,
            right="C" if s.direction == "call" else "P", strike=s.strike,
            expiry=str(expiry_dt.date()), entry_mid=est, entry_source="estimate",
            spot_at_signal=s.spot, mom_pct=s.mom_pct,
            risk_pct=config.RISK_PER_TRADE_PCT, correlated=False,
            paper=False, risk_mode="green")
        print(f"--- card that would be sent at {now:%H:%M} ET ---")
        print(cards.entry_card(s, pos, None, display, "green", "",
                               expiry_dt.date(), now.date()))
        print()
    elif not ok and fired <= 3:
        why = "no stats" if not gate else (
            f"win rate {gate['win_rate']:.0f}%" if gate["win_rate"] < config.MIN_WINRATE
            else "negative expectancy")
        print(f"{now:%H:%M} {s.ticker} {s.direction}: would be SKIPPED ({why})")
print(f"{day}: {fired} setups formed, picky filter would text the first passing one")
