"""Fair Value Gap (FVG) detection — the 3-candle imbalance the ICT / smart-money
crowd trades.

A BULLISH FVG is a gap left when price jumped up so fast that candle 3's low
never overlapped candle 1's high. The empty zone between them (candle1.high up
to candle3.low) tends to act as support when price drifts back into it. A
BEARISH FVG is the mirror (candle3.high up to candle1.low), resistance on the
way back up. The middle candle is the displacement candle that made the move.

We use FVGs as a CONVICTION confirmer, not a standalone signal: a plan to BUY
holds more weight when a fresh, UNFILLED bullish FVG sits just under price and
price is still respecting it (has not traded all the way back through it).

Pure functions over an OHLC DataFrame (needs High/Low, uses Close when present),
no network, so it is cheap to call inside a read and easy to test.
"""

import pandas as pd


def _last_close(bars):
    if "Close" in bars.columns:
        s = bars["Close"].dropna()
        if len(s):
            return float(s.iloc[-1])
    return float(bars["High"].dropna().iloc[-1])


def find_fvgs(bars, atr=None):
    """Every Fair Value Gap in `bars`, oldest first. Each is a dict:
        type    'bull' or 'bear'
        i       index (row position) of the MIDDLE / displacement candle
        time    str timestamp of that candle
        top     upper edge of the gap zone (price)
        bottom  lower edge of the gap zone (price)
        size    top - bottom
        entered price has since traded back INTO the zone (partial mitigation)
        filled  price has since traded all the way THROUGH it (fully mitigated)
    A tiny noise gap is ignored: the gap must clear ~10% of ATR (or a hair of
    price when ATR is unknown), so a one-tick overlap miss does not count."""
    if bars is None or len(bars) < 3 or not {"High", "Low"}.issubset(bars.columns):
        return []
    highs = bars["High"].astype(float).values
    lows = bars["Low"].astype(float).values
    times = list(bars.index)
    n = len(bars)
    price = _last_close(bars)
    min_gap = (0.10 * atr) if (atr and atr > 0) else (0.0002 * price)

    out = []
    for i in range(n - 2):
        c1h, c1l = highs[i], lows[i]
        c3h, c3l = highs[i + 2], lows[i + 2]
        mid = i + 1
        if pd.isna(c1h) or pd.isna(c1l) or pd.isna(c3h) or pd.isna(c3l):
            continue
        if c3l > c1h:              # bullish imbalance
            top, bottom, kind = c3l, c1h, "bull"
        elif c3h < c1l:            # bearish imbalance
            top, bottom, kind = c1l, c3h, "bear"
        else:
            continue
        size = top - bottom
        if size < min_gap:
            continue
        after_h = highs[i + 3:]
        after_l = lows[i + 3:]
        entered = filled = False
        if len(after_h):
            if kind == "bull":
                entered = bool((after_l <= top).any())
                filled = bool((after_l <= bottom).any())
            else:
                entered = bool((after_h >= bottom).any())
                filled = bool((after_h >= top).any())
        out.append({"type": kind, "i": mid, "time": str(times[mid]),
                    "top": round(float(top), 6), "bottom": round(float(bottom), 6),
                    "size": round(float(size), 6),
                    "entered": entered, "filled": filled})
    return out


def confirming_fvg(bars, direction, price=None, atr=None, max_dist_atr=2.5):
    """The most recent UNFILLED FVG that confirms `direction` and sits near
    enough to price to actually matter. direction is 'BUY' or 'SELL' (anything
    else returns None). For a BUY we want a bullish FVG at or just below price
    (support price is holding above); for a SELL a bearish FVG at or just above
    price (resistance capping it). Returns the FVG dict or None."""
    if direction not in ("BUY", "SELL") or bars is None or len(bars) < 3:
        return None
    if price is None:
        price = _last_close(bars)
    want = "bull" if direction == "BUY" else "bear"
    fvgs = [f for f in find_fvgs(bars, atr) if f["type"] == want and not f["filled"]]
    if not fvgs:
        return None
    reach = (max_dist_atr * atr) if (atr and atr > 0) else (0.01 * price)
    picks = []
    for f in fvgs:
        if want == "bull":
            # gap sits below/at price and price is within reach of its top edge
            if f["bottom"] <= price and (price - f["top"]) <= reach:
                picks.append(f)
        else:
            if f["top"] >= price and (f["bottom"] - price) <= reach:
                picks.append(f)
    if not picks:
        return None
    return max(picks, key=lambda f: f["i"])  # most recent wins


def summary_line(fvg: dict, dec: int = 2) -> str:
    """One human line describing an FVG zone, for a text card."""
    if not fvg:
        return ""
    kind = "bullish" if fvg["type"] == "bull" else "bearish"
    return (f"{kind} FVG {fvg['bottom']:,.{dec}f} to {fvg['top']:,.{dec}f} "
            f"(unfilled)")
