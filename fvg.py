"""Fair Value Gap (FVG) detection, ICT / smart-money grade.

A Fair Value Gap is the 3-candle imbalance institutions leave when they move
price so fast one side never gets filled. This module does not just find the
gap; it grades it the way traders who use FVGs consistently actually do:

- DISPLACEMENT: the middle candle must be a strong, large-body impulse. An FVG
  without displacement is noise, not an institutional footprint.
- CONSEQUENT ENCROACHMENT (CE): the 50% midpoint of the gap, the most reactive
  level inside it and the refined entry ICT teaches.
- BISI / SIBI: the names traders say. BISI (Buyside Imbalance, Sellside
  Inefficiency) = a bullish FVG. SIBI = a bearish FVG.
- MITIGATION STATE: unmitigated -> entered -> ce_tapped -> filled, and the big
  one, INVERTED: when price CLOSES all the way through an FVG it flips polarity
  and becomes support/resistance the other way (an Inverse FVG / IFVG).
- PREMIUM / DISCOUNT: relative to the dealing-range equilibrium (50% of the
  recent swing range). Only trust bullish FVGs in discount, bearish in premium.
- GRADE (A/B/C): displacement + freshness + location + trend alignment, so a
  high-conviction read is earned, not just "a gap exists."

Pure functions over an OHLC DataFrame (needs Open/High/Low/Close), no network.
Honest single-timeframe read: real ICT layers a higher-timeframe bias over a
lower-timeframe entry; here the caller's momentum/20-day bias is the bias proxy
and the bars' own range is the dealing range.
"""

import pandas as pd

# a gap this small is micro-noise even if the wicks technically miss
_MIN_GAP_ATR = 0.05

# ---------------------------------------------------------------------------
# SNIPER pattern: the walk-forward-verified config from
# reports/chart_backtest_round6.json (procedural winner, OOS 79.0% / 62 trades,
# 133 trades total across IS+OOS). Every constant here mirrors that config;
# change them only with a new verified backtest round.
# ---------------------------------------------------------------------------
SNIPER_SYMBOLS = {"EURUSD=X", "JPY=X", "^GSPC", "TSLA", "SPY"}
_SNIPER_MIN_GAP_ATR = 1.0    # FVG gap floor (x ATR)
_SNIPER_MAX_GAP_ATR = 3.0    # FVG gap cap (x ATR)
_SNIPER_MIN_HOUR_ET = 7      # entries only 07:00 ET or later
_SNIPER_MAX_RUN_ATR = 8.0    # skip if price already ran this far from the open
_SNIPER_MAX_DAY_EFF = 0.85   # skip a one-way day: abs(close-open)/(high-low)
_SNIPER_MAX_RISK_ATR = 3.6   # skip if the stop sits this far away or more
_SNIPER_STOP_BUF_ATR = 0.1   # stop = FVG far edge +/- this buffer
_SNIPER_TP_R = 0.4           # take profit, all out, no runner
SNIPER_MEASURED = {"win_rate": 79.0, "trades": 133,
                   "basis": "walk-forward replay, chart_backtest_round6"}
# middle-candle body must clear this multiple of ATR to count as real
# displacement (strong = institutional impulse, not a drift)
_DISP_STRONG_ATR = 1.0
_DISP_VERY_STRONG_ATR = 1.8
# and it must be mostly body, not wick, to be a clean displacement candle
_DISP_BODY_RATIO = 0.5


def _last_close(bars):
    if "Close" in bars.columns:
        s = bars["Close"].dropna()
        if len(s):
            return float(s.iloc[-1])
    return float(bars["High"].dropna().iloc[-1])


def _dealing_range(bars):
    """The swing range price is trading inside, and its 50% equilibrium. Premium
    is above equilibrium, discount below."""
    hi = float(bars["High"].max())
    lo = float(bars["Low"].min())
    return hi, lo, (hi + lo) / 2.0


def _pd_zone(level, eq, span):
    """premium / discount / equilibrium for a price level in the range."""
    if span <= 0:
        return "equilibrium"
    d = (level - eq) / span
    if d > 0.05:
        return "premium"
    if d < -0.05:
        return "discount"
    return "equilibrium"


def find_fvgs(bars, atr=None, bias=None):
    """Every Fair Value Gap in `bars`, oldest first, each fully graded. Fields:
        type        'bull' / 'bear'  (its ORIGINAL polarity)
        label       'BISI' (bull) / 'SIBI' (bear)
        i, time     row + timestamp of the middle (displacement) candle
        top, bottom edges of the gap zone (price)
        ce          consequent encroachment, the 50% midpoint
        size        top - bottom
        disp        middle-candle body / ATR  (displacement strength)
        strong      bool: real institutional displacement
        state       'unmitigated' | 'entered' | 'ce_tapped' | 'filled' | 'inverted'
        inverted    bool: price closed clean through it -> polarity flipped (IFVG)
        polarity    effective side NOW: 'bull'/'bear' (flips when inverted)
        pd_zone     'premium' / 'discount' / 'equilibrium' of its CE
        grade       'A' / 'B' / 'C'
        score       raw grade score
    `bias` (optional): the caller's directional lean, 'bullish'/'bearish'
    (with or without a '-weak' suffix). Aligned FVGs grade higher.
    """
    need = {"Open", "High", "Low", "Close"}
    if bars is None or len(bars) < 3 or not need.issubset(bars.columns):
        return []
    o = bars["Open"].astype(float).values
    h = bars["High"].astype(float).values
    low = bars["Low"].astype(float).values
    c = bars["Close"].astype(float).values
    times = list(bars.index)
    n = len(bars)
    price = float(c[-1])
    hi_r, lo_r, eq = _dealing_range(bars)
    span = hi_r - lo_r
    if not atr or atr <= 0:
        # fall back to average true range of the window so thresholds still scale
        rng = [h[k] - low[k] for k in range(n) if not pd.isna(h[k])]
        atr = (sum(rng) / len(rng)) if rng else (0.002 * price)
    min_gap = _MIN_GAP_ATR * atr
    bias_bull = (bias or "").startswith("bull")
    bias_bear = (bias or "").startswith("bear")

    out = []
    for i in range(n - 2):
        c1h, c1l = h[i], low[i]
        c3h, c3l = h[i + 2], low[i + 2]
        mid = i + 1
        if pd.isna(c1h) or pd.isna(c1l) or pd.isna(c3h) or pd.isna(c3l):
            continue
        if c3l > c1h:               # bullish imbalance (BISI)
            top, bottom, kind, label = c3l, c1h, "bull", "BISI"
        elif c3h < c1l:             # bearish imbalance (SIBI)
            top, bottom, kind, label = c1l, c3h, "bear", "SIBI"
        else:
            continue
        size = top - bottom
        if size < min_gap:
            continue

        # displacement quality of the middle candle
        body = abs(c[mid] - o[mid])
        rng_mid = max(h[mid] - low[mid], 1e-9)
        disp = body / atr if atr else 0.0
        clean = (body / rng_mid) >= _DISP_BODY_RATIO
        # native bool: a numpy bool is not JSON-serializable and this dict is
        # json.dumps'd as the brain's tool result
        strong = bool(disp >= _DISP_STRONG_ATR and clean)

        ce = (top + bottom) / 2.0

        # mitigation state from the bars AFTER the gap formed
        aft_h = h[i + 3:]
        aft_l = low[i + 3:]
        aft_c = c[i + 3:]
        state, inverted = "unmitigated", False
        if len(aft_h):
            if kind == "bull":
                if (aft_c < bottom).any():
                    state, inverted = "inverted", True
                elif (aft_l <= bottom).any():
                    state = "filled"
                elif (aft_l <= ce).any():
                    state = "ce_tapped"
                elif (aft_l <= top).any():
                    state = "entered"
            else:
                if (aft_c > top).any():
                    state, inverted = "inverted", True
                elif (aft_h >= top).any():
                    state = "filled"
                elif (aft_h >= ce).any():
                    state = "ce_tapped"
                elif (aft_h >= bottom).any():
                    state = "entered"
        polarity = ("bear" if kind == "bull" else "bull") if inverted else kind
        zone = _pd_zone(ce, eq, span)

        # grade: earn conviction, do not just exist
        score = 0
        if strong:
            score += 2
            if disp >= _DISP_VERY_STRONG_ATR:
                score += 1
        if state == "unmitigated":
            score += 2
        elif state in ("entered", "ce_tapped"):
            score += 1
        # right half of the range for its live polarity
        if (polarity == "bull" and zone == "discount") or \
           (polarity == "bear" and zone == "premium"):
            score += 1
        if size >= 0.3 * atr:
            score += 1
        if (polarity == "bull" and bias_bull) or (polarity == "bear" and bias_bear):
            score += 1
        grade = "A" if score >= 6 else "B" if score >= 4 else "C"

        out.append({
            "type": kind, "label": label, "i": mid, "time": str(times[mid]),
            "top": round(float(top), 6), "bottom": round(float(bottom), 6),
            "ce": round(float(ce), 6), "size": round(float(size), 6),
            "disp": round(float(disp), 2), "strong": strong,
            "state": state, "inverted": inverted, "polarity": polarity,
            "pd_zone": zone, "grade": grade, "score": score,
        })
    return out


def _actionable(f):
    """An FVG is still tradeable if price has not already fully closed the gap
    (filled). Inverted ones stay actionable in their NEW polarity."""
    return f["state"] != "filled"


def confirming_fvg(bars, direction, price=None, atr=None, bias=None):
    """The single best FVG confirming `direction` ('BUY' / 'SELL'), or None.

    Confirmation = an actionable FVG whose LIVE polarity matches the trade
    (BUY wants a bullish gap as support at/under price; SELL a bearish gap as
    resistance at/over price), with real displacement, near enough to price to
    matter. Highest grade wins, then most recent. Returns the FVG dict enriched
    with a concrete CE-based ticket: entry (CE), stop (beyond the far edge with
    a buffer), and target (the next liquidity pool = dealing-range extreme)."""
    if direction not in ("BUY", "SELL") or bars is None or len(bars) < 3:
        return None
    if price is None:
        price = _last_close(bars)
    if not atr or atr <= 0:
        atr = 0.002 * price
    want = "bull" if direction == "BUY" else "bear"
    hi_r, lo_r, _ = _dealing_range(bars)
    reach = 2.5 * atr
    buf = 0.1 * atr

    cands = []
    for f in find_fvgs(bars, atr, bias):
        if f["polarity"] != want or not _actionable(f) or not f["strong"]:
            continue
        if want == "bull":
            # support: gap sits at/below price and price is within reach of it
            if f["bottom"] <= price and (price - f["top"]) <= reach:
                cands.append(f)
        else:
            if f["top"] >= price and (f["bottom"] - price) <= reach:
                cands.append(f)
    if not cands:
        return None
    best = max(cands, key=lambda f: (f["score"], f["i"]))

    if want == "bull":
        entry, stop, target = best["ce"], best["bottom"] - buf, hi_r
    else:
        entry, stop, target = best["ce"], best["top"] + buf, lo_r
    risk = abs(entry - stop)
    out = dict(best)
    out["ticket"] = {
        "entry_ce": round(float(entry), 6),
        "stop": round(float(stop), 6),
        "target_liquidity": round(float(target), 6),
        "risk": round(float(risk), 6),
        "rr": round(abs(target - entry) / risk, 2) if risk > 0 else None,
    }
    return out


def sniper_check(bars, direction, price, atr, conf, yf_symbol, now_et):
    """Gate a read against the verified SNIPER pattern (see SNIPER_* above,
    from reports/chart_backtest_round6.json). ALL conditions must pass:

      - yf_symbol is one of the five verified symbols (SNIPER_SYMBOLS)
      - `conf` is a grade A confirming FVG in the plan `direction`
      - FVG gap size >= 1.0*ATR and < 3.0*ATR
      - clock (now_et) is 07:00 ET or later
      - price has run < 8*ATR from today's session open in the trade direction
      - day efficiency abs(close-open)/(high-low) < 0.85
      - stop distance < 3.6*ATR, stop = FVG far edge -0.1*ATR (BUY, bottom
        side) / +0.1*ATR (SELL, top side)

    Returns a JSON-safe dict:
      {"passes": bool,
       "reasons": [str, ...]   # every failed condition, empty when passing
       "ticket": {"entry": price (market, now), "stop", "target" (0.4R all
                  out), "risk", "rr": 0.4} or None when no stop is computable}

    Pure (no network, no state) and fully guarded: bad input never raises, it
    just fails the check with a reason."""
    try:
        if direction not in ("BUY", "SELL"):
            return {"passes": False, "reasons": ["no plan direction"], "ticket": None}
        if bars is None or len(bars) == 0 or price is None or not atr or atr <= 0:
            return {"passes": False, "reasons": ["missing bars/price/ATR"], "ticket": None}
        price = float(price)
        atr = float(atr)
        reasons = []

        if yf_symbol not in SNIPER_SYMBOLS:
            reasons.append(f"{yf_symbol or '?'} is not a verified sniper symbol "
                           "(EUR/USD, USD/JPY, SPX, TSLA, SPY only)")

        if not conf or conf.get("grade") != "A":
            reasons.append("no grade A confirming FVG")
        else:
            size = float(conf.get("size") or 0.0)
            if size < _SNIPER_MIN_GAP_ATR * atr:
                reasons.append(f"FVG gap {size / atr:.2f} ATR under the "
                               f"{_SNIPER_MIN_GAP_ATR:g} ATR floor")
            elif size >= _SNIPER_MAX_GAP_ATR * atr:
                reasons.append(f"FVG gap {size / atr:.2f} ATR at/over the "
                               f"{_SNIPER_MAX_GAP_ATR:g} ATR cap")

        if now_et is None or not hasattr(now_et, "hour"):
            reasons.append("no ET clock")
        elif now_et.hour < _SNIPER_MIN_HOUR_ET:
            reasons.append(f"before {_SNIPER_MIN_HOUR_ET:02d}:00 ET")

        # today's session only: bars may span days when intraday fell back
        db = bars
        try:
            dates = bars.index.date
            last_day = max(dates)
            mask = [d == last_day for d in dates]
            if any(mask) and not all(mask):
                db = bars[mask]
        except (AttributeError, TypeError, ValueError):
            db = bars
        day_open = day_hi = day_lo = day_close = None
        try:
            o = db["Open"].astype(float).dropna()
            h = db["High"].astype(float).dropna()
            low = db["Low"].astype(float).dropna()
            c = db["Close"].astype(float).dropna()
            if len(o) and len(h) and len(low) and len(c):
                day_open = float(o.iloc[0])
                day_hi = float(h.max())
                day_lo = float(low.min())
                day_close = float(c.iloc[-1])
        except (KeyError, TypeError, ValueError, IndexError):
            pass
        if day_open is None:
            reasons.append("can't read today's session bars")
        else:
            run = (price - day_open) if direction == "BUY" else (day_open - price)
            if run >= _SNIPER_MAX_RUN_ATR * atr:
                reasons.append(f"already ran {run / atr:.1f} ATR from the open "
                               f"(cap {_SNIPER_MAX_RUN_ATR:g})")
            rng = day_hi - day_lo
            if rng > 0:
                eff = abs(day_close - day_open) / rng
                if eff >= _SNIPER_MAX_DAY_EFF:
                    reasons.append(f"one-way day, efficiency {eff:.2f} at/over "
                                   f"{_SNIPER_MAX_DAY_EFF:g}")

        ticket = None
        if conf and conf.get("top") is not None and conf.get("bottom") is not None:
            buf = _SNIPER_STOP_BUF_ATR * atr
            if direction == "BUY":
                stop = float(conf["bottom"]) - buf
            else:
                stop = float(conf["top"]) + buf
            risk = abs(price - stop)
            if risk >= _SNIPER_MAX_RISK_ATR * atr:
                reasons.append(f"stop {risk / atr:.1f} ATR away, at/over the "
                               f"{_SNIPER_MAX_RISK_ATR:g} ATR cap")
            if risk > 0:
                tgt = price + _SNIPER_TP_R * risk if direction == "BUY" \
                    else price - _SNIPER_TP_R * risk
                ticket = {"entry": round(price, 6), "stop": round(stop, 6),
                          "target": round(tgt, 6), "risk": round(risk, 6),
                          "rr": _SNIPER_TP_R}
            else:
                reasons.append("zero-distance stop")

        return {"passes": bool(not reasons and ticket), "reasons": reasons,
                "ticket": ticket}
    except Exception as e:  # never let the sniper gate take down a read
        return {"passes": False,
                "reasons": [f"sniper check failed ({type(e).__name__})"],
                "ticket": None}


def summary_line(f: dict, dec: int = 2) -> str:
    """One trader-voice line describing an FVG, e.g.
       'grade A bullish FVG (BISI) 4,120.50 to 4,126.80, CE 4,123.65, unmitigated, in discount'."""
    if not f:
        return ""
    side = "bullish" if f["polarity"] == "bull" else "bearish"
    name = "BISI" if f["polarity"] == "bull" else "SIBI"
    inv = " (inverted / IFVG)" if f.get("inverted") else ""
    return (f"grade {f['grade']} {side} FVG ({name}){inv} "
            f"{f['bottom']:,.{dec}f} to {f['top']:,.{dec}f}, CE {f['ce']:,.{dec}f}, "
            f"{f['state']}, in {f['pd_zone']}")
