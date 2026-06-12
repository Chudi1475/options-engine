"""One-off: validate Alpaca keys + live data."""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime
from zoneinfo import ZoneInfo

import config  # noqa: F401
from data_feed import DataFeed

f = DataFeed()
print("alpaca detected:", f.alpaca is not None)
print("QCOM backend:", f.backend_for("QCOM"))
print("SPX backend:", f.backend_for("^GSPC"))
if f.alpaca:
    p = f.alpaca.latest_price("QCOM")
    print(f"QCOM live trade: ${p}")
    bars = f.alpaca.today_bars_5m("QCOM", datetime.now(ZoneInfo("America/New_York")))
    n = 0 if bars is None else len(bars)
    last = None if bars is None else round(float(bars["Close"].iloc[-1]), 2)
    print(f"QCOM 5m bars today: {n}, last close: {last}")
