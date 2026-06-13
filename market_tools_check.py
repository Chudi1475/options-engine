"""Check the brain's market tools return real, sane data."""
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import market_tools

print("=== analyze_day SPX (last session) ===")
print(json.dumps(market_tools.analyze_day("SPX"), indent=2, default=str))
print("\n=== analyze_day QCOM (last session) ===")
print(json.dumps(market_tools.analyze_day("QCOM"), indent=2, default=str))
print("\n=== market_now SPX (weekend -> closed) ===")
print(json.dumps(market_tools.market_now("SPX"), indent=2, default=str))
print("\n=== analyze_day bad ticker ===")
print(json.dumps(market_tools.analyze_day("AAPL"), default=str))
