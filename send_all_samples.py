"""Send one TEST-labeled example of every message type the bot can produce,
so everyone knows what each looks like before going live. The stats on the
tier demos are EXAMPLE numbers chosen to show each tier's look — the real
exit alerts (sell-half / flip / stop / expiry) come from scanner.py --test."""
import sys
import time as time_mod

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import date

import cards
import config  # noqa: F401  (loads .env)
import telegram
from positions import Position
from quotes import Quote
from strategy import Setup

TEST = "🧪 TEST — EXAMPLE ONLY, NOT A REAL ALERT 🧪\n\n"
today = date.today()

messages = [
    TEST + "Morning report, RISK MODE 1 of 3:\n\n"
    + cards.morning_card("green", "No major releases scheduled. VIX 17, "
                         "overnight gap +0.2%. All clear.", today),
    TEST + "Morning report, RISK MODE 2 of 3:\n\n"
    + cards.morning_card("yellow", "Unemployment Claims at 8:30 AM ET; "
                         "VIX is 27 (elevated fear)", today),
    TEST + "Morning report, RISK MODE 3 of 3:\n\n"
    + cards.morning_card("red", "FOMC Statement at 2:00 PM ET (major release)",
                         today),
]

# entry cards at every tier — EXAMPLE stats, labeled as such
tier_demos = [
    ("🔴 RISKY tier", 72.0, 9.0),
    ("🟠 DECENT ODDS tier", 76.0, 11.0),
    ("🟢 GOOD ODDS tier", 81.0, 14.0),
    ("🟢🌟 GREAT ODDS tier", 87.0, 18.0),
]
for label, wr, ev in tier_demos:
    s = Setup(ticker="SPX", direction="call", strike=7300.0, spot=7297.2,
              mom_pct=0.21, reason="demo")
    pos = Position(
        id="demo", date=str(today), time_et="09:50:00", ticker="SPX",
        direction="call", right="C", strike=7300.0, expiry=str(today),
        entry_mid=4.40, entry_source="quote", entry_bid=4.20, entry_ask=4.60,
        spot_at_signal=7297.2, mom_pct=0.21,
        risk_pct=config.RISK_PER_TRADE_PCT, correlated=False, paper=False,
        risk_mode="green")
    stats = {"win_rate": wr, "avg_win_pct": 49.0, "avg_loss_pct": -28.0,
             "expectancy_pct": ev, "ev_pct": ev, "trades": 60,
             "start": "03/17/2026", "end": "06/10/2026",
             "label": "EXAMPLE NUMBERS for this demo",
             "costs_note": "after est. costs", "source": "backtest_new"}
    q = Quote(4.20, 4.60, 4.40, "live quote (example)", False)
    messages.append(TEST + f"Trade alert, {label}:\n\n"
                    + cards.entry_card(s, pos, q, stats, "green", "", today, today))

messages.append(
    TEST + "And the most common 'message' of all: NO message. "
    "If no setup passes the filter, the bot says nothing all day. "
    "No text = no trade. Silence is a decision, not a glitch.")

for i, msg in enumerate(messages, 1):
    errors = telegram.send(msg)
    print(f"{i}/{len(messages)} {'sent' if not errors else errors}")
    time_mod.sleep(1.5)  # keep Telegram happy, preserve order
