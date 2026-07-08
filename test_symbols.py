"""Regression test for symbol resolution — every symbol class the /help card
advertises must resolve, and no index alias may map to a bare ticker Yahoo
doesn't serve (the old "/chart spx says no recent data" bug).

Offline by default (no network, just resolve() table checks):

    .venv/Scripts/python.exe test_symbols.py

With --net it also smoke-tests the live pipeline (read_any + charts.render_fvg)
for one symbol per class and fails loudly listing every failure:

    .venv/Scripts/python.exe test_symbols.py --net

Also runs under pytest (the --net half only when RUN_NET_TESTS=1 is set).
"""

import os
import sys

import market_tools

# ---------------------------------------------------------------------------
# what the help card promises ("/signal xauusd, /signal eurusd, /signal btc",
# "/<symbol> — read + plan on ANY stock, ETF, fx, gold, or crypto")
# ---------------------------------------------------------------------------
INDEX_ALIASES = ["spx", "sp500", "ndx", "vix", "us30", "dxy"]
GOLD_ALIASES = ["gold", "xauusd"]
FOREX_ALIASES = ["eurusd", "gbpusd", "usdjpy", "audusd", "usdcad", "usdchf"]
CRYPTO_ALIASES = ["btc", "eth", "sol"]
STOCK_ALIASES = ["tesla", "tsla", "qcom", "spy", "nvda", "apple"]

ALL_ALIASES = (INDEX_ALIASES + GOLD_ALIASES + FOREX_ALIASES
               + CRYPTO_ALIASES + STOCK_ALIASES)

# bare index-style tickers that do NOT exist on Yahoo Finance; if resolve()
# ever hands one of these to yfinance, /chart and /signal break for it
BARE_INDEX_TICKERS = {"SPX", "SP500", "NDX", "VIX", "US30", "DXY",
                      "DJI", "DOW", "XAUUSD", "RUT", "US100", "US500"}

# expected ticker for each stock alias (regression: "tesla" must hit TSLA)
STOCK_EXPECT = {"tesla": "TSLA", "tsla": "TSLA", "qcom": "QCOM",
                "spy": "SPY", "nvda": "NVDA", "apple": "AAPL"}

NET_SYMBOLS = ["spx", "gold", "eurusd", "btc", "tsla"]


def _resolve_failures():
    """Offline checks. Returns a list of human-readable failure strings."""
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    for sym in ALL_ALIASES:
        for variant in (sym, sym.upper()):
            r = market_tools.resolve(variant)
            check(r is not None, f"resolve({variant!r}) returned None")
            if r is None:
                continue
            check(isinstance(r, tuple) and len(r) == 4,
                  f"resolve({variant!r}) -> {r!r}, expected 4-tuple "
                  "(display, yf_symbol, decimals, kind)")
            if not (isinstance(r, tuple) and len(r) == 4):
                continue
            disp, yfs, dec, kind = r
            check(isinstance(disp, str) and disp,
                  f"resolve({variant!r}): empty display name")
            check(isinstance(yfs, str) and yfs,
                  f"resolve({variant!r}): empty yf symbol")
            check(isinstance(dec, int) and dec >= 0,
                  f"resolve({variant!r}): bad decimals {dec!r}")
            check(yfs.upper() not in BARE_INDEX_TICKERS,
                  f"resolve({variant!r}) maps to bare ticker {yfs!r}, "
                  "which Yahoo does not serve (needs ^/=X/=F/-USD form)")

    # class-specific shape checks (lowercase canonical form)
    for sym in INDEX_ALIASES:
        r = market_tools.resolve(sym)
        if not r:
            continue
        disp, yfs, dec, kind = r
        ok_form = yfs.startswith("^") or any(ch in yfs for ch in "=.-")
        if not ok_form:
            fails.append(f"index {sym!r} -> {yfs!r}: not a real Yahoo index "
                         "symbol (expected ^GSPC-style or DX-Y.NYB-style)")
    for sym in ("spx", "sp500"):
        r = market_tools.resolve(sym)
        if r and not r[1].startswith("^"):
            fails.append(f"{sym!r} must map to a ^ index symbol, got {r[1]!r}")

    for sym in GOLD_ALIASES:
        r = market_tools.resolve(sym)
        if not r:
            continue
        disp, yfs, dec, kind = r
        if kind != "gold":
            fails.append(f"gold alias {sym!r}: kind {kind!r} != 'gold'")
        if yfs != "GC=F":
            fails.append(f"gold alias {sym!r} -> {yfs!r}; expected COMEX "
                         "futures 'GC=F' (spot XAUUSD=X is dead on Yahoo)")

    for sym in FOREX_ALIASES:
        r = market_tools.resolve(sym)
        if not r:
            continue
        disp, yfs, dec, kind = r
        if kind != "forex":
            fails.append(f"forex alias {sym!r}: kind {kind!r} != 'forex'")
        if not yfs.endswith("=X"):
            fails.append(f"forex alias {sym!r} -> {yfs!r}; expected a "
                         "Yahoo '=X' pair")

    for sym in CRYPTO_ALIASES:
        r = market_tools.resolve(sym)
        if not r:
            continue
        disp, yfs, dec, kind = r
        if kind != "crypto":
            fails.append(f"crypto alias {sym!r}: kind {kind!r} != 'crypto'")
        if not yfs.endswith("-USD"):
            fails.append(f"crypto alias {sym!r} -> {yfs!r}; expected a "
                         "Yahoo '-USD' symbol")

    for sym, want in STOCK_EXPECT.items():
        r = market_tools.resolve(sym)
        if not r:
            continue
        disp, yfs, dec, kind = r
        if kind != "stock":
            fails.append(f"stock alias {sym!r}: kind {kind!r} != 'stock'")
        if yfs != want:
            fails.append(f"stock alias {sym!r} -> {yfs!r}; expected {want!r}")

    # the guard the bare /<symbol> command uses must agree with resolve()
    for sym in ALL_ALIASES:
        if not market_tools.known_symbol(sym):
            fails.append(f"known_symbol({sym!r}) is False but the help card "
                         "advertises it")

    return fails


def _net_failures():
    """Online smoke test: read_any + charts.render_fvg per symbol class."""
    import charts
    fails = []
    for sym in NET_SYMBOLS:
        try:
            r = market_tools.read_any(sym)
        except Exception as e:
            fails.append(f"read_any({sym!r}) raised {type(e).__name__}: {e}")
            continue
        if not isinstance(r, dict):
            fails.append(f"read_any({sym!r}) -> {type(r).__name__}, not dict")
            continue
        if "error" in r:
            fails.append(f"read_any({sym!r}) error: {r['error']}")
            continue
        if "note" in r and "price" not in r:
            fails.append(f"read_any({sym!r}) no data: {r['note']}")
            continue
        if not isinstance(r.get("price"), (int, float)) or r["price"] <= 0:
            fails.append(f"read_any({sym!r}) bad price: {r.get('price')!r}")
            continue
        try:
            png, reason = charts.render_fvg(r)
        except Exception as e:
            fails.append(f"render_fvg({sym!r}) raised {type(e).__name__}: {e}")
            continue
        if not png:
            fails.append(f"render_fvg({sym!r}) returned no image: {reason}")
        elif len(png) < 1000:
            fails.append(f"render_fvg({sym!r}) image suspiciously small "
                         f"({len(png)} bytes)")
    return fails


# ---------------------------------------------------------------------------
# pytest entry points
# ---------------------------------------------------------------------------
def test_resolve_offline():
    fails = _resolve_failures()
    assert not fails, "\n".join(fails)


def test_read_and_chart_net():
    if os.environ.get("RUN_NET_TESTS") != "1":
        try:
            import pytest
            pytest.skip("network test; set RUN_NET_TESTS=1 to run")
        except ImportError:
            return
    fails = _net_failures()
    assert not fails, "\n".join(fails)


# ---------------------------------------------------------------------------
# script entry point
# ---------------------------------------------------------------------------
def main(argv):
    net = "--net" in argv
    fails = _resolve_failures()
    n_offline = len(ALL_ALIASES)
    if fails:
        print(f"OFFLINE FAIL — {len(fails)} problem(s):")
        for f in fails:
            print(f"  - {f}")
    else:
        print(f"offline OK — {n_offline} help-card symbols resolve cleanly, "
              "no bare index tickers")

    if net:
        print(f"--net: smoke-testing read_any + render_fvg for "
              f"{', '.join(NET_SYMBOLS)} ...")
        net_fails = _net_failures()
        if net_fails:
            print(f"NET FAIL — {len(net_fails)} problem(s):")
            for f in net_fails:
                print(f"  - {f}")
        else:
            print(f"net OK — read_any + render_fvg worked for all "
                  f"{len(NET_SYMBOLS)} symbols")
        fails += net_fails

    if fails:
        print(f"\nFAILED: {len(fails)} total failure(s)")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
