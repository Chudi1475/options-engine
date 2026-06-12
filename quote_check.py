"""One-off plumbing check: can we read real option chains for our tickers?"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

import quotes

ET = ZoneInfo("America/New_York")
now = datetime.now(ET)
today = now.date()
friday = today + timedelta(days=(4 - today.weekday()) % 7)

for ticker, expiry in (("SPX", today), ("QCOM", friday), ("TSLA", friday)):
    sym = quotes.CHAIN_SYMBOL.get(ticker, ticker)
    try:
        t = yf.Ticker(sym)
        exps = t.options or ()
        print(f"{ticker} ({sym}): {len(exps)} expirations, first 3: {exps[:3]}")
        exp = expiry.isoformat()
        if exp not in exps:
            exp = exps[0] if exps else None
            print(f"  target expiry {expiry} not listed (market closed?) — using {exp}")
        if exp:
            chain = t.option_chain(exp)
            calls = chain.calls
            mid_row = calls.iloc[len(calls) // 2]
            q = quotes.get_option_quote(ticker, "C", float(mid_row["strike"]),
                                        date.fromisoformat(exp))
            print(f"  sample strike {mid_row['strike']:g}: "
                  f"bid={mid_row['bid']} ask={mid_row['ask']} last={mid_row['lastPrice']}")
            print(f"  get_option_quote -> {q}")
    except Exception as e:
        print(f"{ticker}: FAILED — {e}")
