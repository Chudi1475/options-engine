"""One-off: validate Alpaca keys + live data."""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config  # noqa: F401
from data_feed import DataFeed

def run_validation():
    f = DataFeed()
    print("alpaca detected:", f.alpaca is not None)
    print("QCOM backend:", f.backend_for("QCOM"))
    print("SPX backend:", f.backend_for("^GSPC"))

    if not f.alpaca:
        print("❌ Validation stopped: Alpaca client is not initialized.")
        return

    # 1. Protect live network requests with try/except blocks
    try:
        p = f.alpaca.latest_price("QCOM")
        print(f"QCOM live trade: ${p}")
    except Exception as e:
        print(f"❌ Failed to fetch live price: {e}")
        p = None

    try:
        current_ny_time = datetime.now(ZoneInfo("America/New_York"))
        bars = f.alpaca.today_bars_5m("QCOM", current_ny_time)
        
        # 2. Check for BOTH None structures AND completely empty dataframes
        if bars is None or len(bars) == 0:
            print("QCOM 5m bars today: 0, last close: None (Market might be closed)")
        else:
            n = len(bars)
            last = round(float(bars["Close"].iloc[-1]), 2)
            print(f"QCOM 5m bars today: {n}, last close: {last}")
            
    except Exception as e:
        print(f"❌ Failed to fetch historic 5m bars: {e}")

if __name__ == "__main__":
    run_validation()
