"""Send one TEST-labeled example of every message type the bot can produce,
so everyone knows what each looks like before going live. The win rates in
the tier demos are EXAMPLE numbers chosen to show each tier's look."""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime
from zoneinfo import ZoneInfo

from scanner import build_card, load_backtest, load_env, telegram_send
from strategy import Setup

ET = ZoneInfo("America/New_York")
load_env()
backtest = load_backtest()
now = datetime.now(ET).replace(hour=9, minute=50)

TEST = "🧪 TEST — EXAMPLE ONLY, NOT A REAL ALERT 🧪\n\n"

messages = [
    # --- the three morning day reports ---
    TEST + "Morning day report, type 1 of 3:\n\n"
    "🟢 NORMAL DAY: VIX 17, overnight gap +0.2%, no scheduled events listed. "
    "Standard rules apply.",

    TEST + "Morning day report, type 2 of 3:\n\n"
    "🟠 CAUTION DAY: VIX is 27 (elevated fear — moves get violent); "
    "scheduled event today: FOMC rate decision 2pm — expect fakeouts around "
    "the release. Setups still fire but consider half size.",

    TEST + "Morning day report, type 3 of 3:\n\n"
    "🔴 NO-TRADE DAY: VIX is 38 (panic level). The bot is standing down today. "
    "(On a day like this you get this one text and then silence — that "
    "silence is the bot protecting you.)",
]

# --- trade cards at every risk tier (example stats to show each look) ---
tier_demos = [
    ("🔴 RISKY tier (70-74%)", "SPX", "call", 7395.0, 7392.4, 0.14,
     {"win_rate": 72.0, "avg_win_pct": 39.0, "avg_loss_pct": -68.0,
      "expectancy_pct": 9.0, "trades": 60,
      "start": "03/17/2026", "end": "06/10/2026"}),
    ("🟠 DECENT ODDS tier (75-79%)", "QCOM", "call", 232.5, 231.8, 0.21,
     {"win_rate": 76.0, "avg_win_pct": 31.0, "avg_loss_pct": -64.0,
      "expectancy_pct": 6.5, "trades": 55,
      "start": "03/17/2026", "end": "06/10/2026"}),
    ("🟢 GOOD ODDS tier (80-84%)", "SPX", "call", 7410.0, 7406.1, 0.27,
     {"win_rate": 81.0, "avg_win_pct": 35.0, "avg_loss_pct": -61.0,
      "expectancy_pct": 14.0, "trades": 48,
      "start": "03/17/2026", "end": "06/10/2026"}),
    ("🟢🌟 GREAT ODDS tier (85%+)", "SPX", "call", 7400.0, 7397.7, 0.33,
     {"win_rate": 87.0, "avg_win_pct": 33.0, "avg_loss_pct": -58.0,
      "expectancy_pct": 18.0, "trades": 41,
      "start": "03/17/2026", "end": "06/10/2026"}),
    ("📉 PUT example (down day)", "SPX", "put", 7350.0, 7353.2, -0.22,
     {"win_rate": 74.0, "avg_win_pct": 30.0, "avg_loss_pct": -62.0,
      "expectancy_pct": 5.0, "trades": 37,
      "start": "03/17/2026", "end": "06/10/2026"}),
]

for label, ticker, direction, strike, spot, mom, stats in tier_demos:
    s = Setup(ticker=ticker, direction=direction, strike=strike, spot=spot,
              mom_pct=mom, reason="demo")
    messages.append(TEST + f"Trade alert, {label}:\n\n"
                    + build_card(s, now, backtest, stats))

messages.append(
    TEST + "And the most common 'message' of all: NO message. "
    "If no setup passes the 70%+ filter, the bot says nothing all day. "
    "No text = no trade. Silence is a decision, not a glitch.")

for i, msg in enumerate(messages, 1):
    errors = telegram_send(msg)
    print(f"{i}/{len(messages)} {'sent' if not errors else errors}")
    time.sleep(1.5)  # keep Telegram happy, preserve order
