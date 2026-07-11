"""Offline tests: the FVG chart draws the SAME gap the text card quoted.

The read computes r['fvg']['confirming'] on its own bars and the text quotes
those exact numbers. render_fvg must box that stored gap (anchored by its
candle timestamp), honor a read's explicit no-gap verdict, and only recompute
when the read carries no FVG info at all or the gap candle left the window.
No network, no Telegram: bars are synthetic and passed in directly.

Run:  python test_charts.py     (exit code 0 = all good)
"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

import charts
import fvg

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def make_bars(n=90, gap_at=None, tz="America/New_York", naive=False):
    """Synthetic 5m OHLC with (optionally) a clean bull FVG whose displacement
    candle sits at index `gap_at` (pattern gap_at-1, gap_at, gap_at+1)."""
    idx = pd.date_range("2026-07-10 04:00", periods=n, freq="5min",
                        tz=None if naive else tz)
    base = 100 + 0.01 * np.arange(n, dtype=float) + 0.05 * np.sin(np.arange(n) / 5)
    o = base.copy()
    c = base + 0.02
    h = np.maximum(o, c) + 0.05
    lo = np.minimum(o, c) - 0.05
    if gap_at is not None:
        k = gap_at
        o[k] = c[k - 1]
        c[k] = o[k] + 1.5          # displacement body
        h[k] = c[k] + 0.1
        lo[k] = o[k] - 0.02
        o[k + 1] = c[k]
        c[k + 1] = c[k] + 0.1
        lo[k + 1] = h[k - 1] + 0.5  # low above high[k-1] -> the gap
        h[k + 1] = c[k + 1] + 0.2
        # keep the tape above the gap afterwards so it reads unmitigated
        lift = lo[k + 1] + 0.3 - base[k + 2:]
        o[k + 2:] += lift
        c[k + 2:] += lift
        h[k + 2:] += lift
        lo[k + 2:] += lift
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c}, index=idx)


def stored_from(bars, k, sniper=False):
    """A confirming-FVG dict shaped like fvg.find_fvgs/confirming_fvg output,
    for the gap whose displacement candle is bars row k."""
    bottom = round(float(bars["High"].iloc[k - 1]), 6)
    top = round(float(bars["Low"].iloc[k + 1]), 6)
    ce = round((top + bottom) / 2, 6)
    d = {"type": "bull", "label": "BISI", "i": k, "time": str(bars.index[k]),
         "top": top, "bottom": bottom, "ce": ce,
         "size": round(top - bottom, 6), "disp": 1.8, "strong": True,
         "state": "unmitigated", "inverted": False, "polarity": "bull",
         "pd_zone": "discount", "grade": "A", "score": 6}
    if sniper:
        d["ticket"] = {"entry": ce, "stop": round(bottom - 0.05, 6),
                       "target": round(ce + 0.24, 6),
                       "measured": {"win_rate": 79.0, "trades": 133}}
    else:
        d["ticket"] = {"entry_ce": ce, "stop": round(bottom - 0.05, 6),
                       "target_liquidity": round(top + 1.5, 6),
                       "risk": round(ce - bottom + 0.05, 6), "rr": 2.8}
    return d


def make_r(stored="absent", sniper=False):
    """A read dict like market_tools returns. stored='absent' = no fvg block
    at all; stored=None = the read looked and found no confirming gap."""
    r = {"symbol": "TEST", "instrument": "Test", "ticker": "TEST",
         "decimals": 2, "bias": "bullish", "prior_close": 99.5,
         "plan": {"direction": "BUY", "entry": 101.0, "stop": 100.3,
                  "target": 102.5, "target1": 101.6, "risk": 0.7}}
    if stored != "absent":
        r["fvg"] = {"confirming": stored, "recent_unfilled": [], "sniper": sniper}
    return r


def render(r, bars, recompute):
    """render_fvg with fvg.confirming_fvg swapped for `recompute`; any raise
    inside render_fvg is swallowed by its own guard, so a poisoned recompute
    shows up as png=None."""
    real = fvg.confirming_fvg
    fvg.confirming_fvg = recompute
    try:
        return charts.render_fvg(r, bars=bars)
    finally:
        fvg.confirming_fvg = real


def boom(*a, **k):
    raise AssertionError("recompute called")


calls = []


def recorder(*a, **k):
    calls.append(1)
    return None


if not charts.available():
    print("SKIP: matplotlib not installed here, nothing to test")
    sys.exit(0)


# 1) stored gap inside the 64-bar window: anchored, recompute never runs
bars = make_bars(90, gap_at=70)
png, why = render(make_r(stored_from(bars, 70)), bars, boom)
check("stored gap anchored, no recompute", png is not None, str(why))

# 2) gap candle older than 64 bars: window stretches to keep it in frame
bars = make_bars(90, gap_at=10)
png, why = render(make_r(stored_from(bars, 10)), bars, boom)
check("window stretches for an old gap candle", png is not None, str(why))

# 3) gap candle too far back (>96 bars): no box at all — never a recomputed
# impostor gap the words did not describe (tags fall back to stored/plan)
bars = make_bars(200, gap_at=20)
calls.clear()
png, why = render(make_r(stored_from(bars, 20)), bars, recorder)
check("too-old gap renders without recompute", png is not None and len(calls) == 0,
      f"png={png is not None} recompute_calls={len(calls)}")

# 4) the read found NO confirming gap: chart honors that, no recompute
bars = make_bars(90, gap_at=70)
png, why = render(make_r(stored=None), bars, boom)
check("no-gap verdict honored, no recompute", png is not None, str(why))

# 5) read carries no fvg block at all (old caller): recompute fallback
bars = make_bars(90, gap_at=70)
calls.clear()
png, why = render(make_r(stored="absent"), bars, recorder)
check("no fvg info falls back to recompute", png is not None and len(calls) == 1,
      f"png={png is not None} recompute_calls={len(calls)}")

# 6) sniper read: stored gap + sniper ticket levels render without recompute
bars = make_bars(90, gap_at=70)
png, why = render(make_r(stored_from(bars, 70, sniper=True), sniper=True),
                  bars, boom)
check("sniper read draws stored gap and ticket", png is not None, str(why))

# 7) anchor matches by instant across timezones (read stamps ET, chart is CT)
bars = make_bars(90, gap_at=70)
ct_index = bars.index.tz_convert("America/Chicago")
pos = charts._anchor_pos(ct_index, str(bars.index[70]))
check("anchor matches ET stamp on CT index", pos == 70, f"pos={pos}")

# 8) naive/aware mismatch returns None instead of raising
naive = make_bars(90, gap_at=70, naive=True)
pos = charts._anchor_pos(naive.index, str(bars.index[70]))
check("naive/aware mismatch is safe", pos is None, f"pos={pos}")
pos = charts._anchor_pos(naive.index, str(naive.index[70]))
check("naive/naive still matches", pos == 70, f"pos={pos}")

# 9) a stored target far beyond the charted window stays IN frame: before
# the ylim fix the orphaned "Target" label expanded the canvas ~40% taller
import struct


def png_h(b):
    return struct.unpack(">II", b[16:24])[1]


bars = make_bars(90, gap_at=70)
near = stored_from(bars, 70)
far = stored_from(bars, 70)
far["ticket"]["target_liquidity"] = round(far["top"] + 8.0, 6)  # premarket spike
png_near, _ = render(make_r(near), bars, boom)
png_far, why = render(make_r(far), bars, boom)
check("far-away stored target renders", png_far is not None, str(why))
check("far-away target stays inside the frame",
      png_far is not None and png_near is not None
      and png_h(png_far) <= png_h(png_near) * 1.15,
      f"h_near={png_near and png_h(png_near)} h_far={png_far and png_h(png_far)}")

# 10) stored-dict validation: missing fields or wrong shape -> None
good = stored_from(make_bars(90, gap_at=70), 70)
check("validator accepts a real dict",
      charts._stored_confirming({"fvg": {"confirming": good}}) is good)
broken = dict(good)
del broken["time"]
check("validator rejects a dict missing 'time'",
      charts._stored_confirming({"fvg": {"confirming": broken}}) is None)
check("validator rejects non-dict",
      charts._stored_confirming({"fvg": {"confirming": "nope"}}) is None)
check("validator survives fvg=None",
      charts._stored_confirming({"fvg": None}) is None)

# 11) right-axis tag de-collision (_spread): clustered levels get pushed
# apart, isolated levels stay put, order survives, edges clamp
ty = charts._spread([100.0, 100.02, 100.04], [1.0, 1.0, 2.0], 90.0, 110.0)
check("clustered tags separate",
      ty[1] - ty[0] >= 1.0 * 1.12 - 1e-9 and ty[2] - ty[1] >= 1.5 * 1.12 - 1e-9,
      f"ty={ty}")
check("cluster keeps price order", ty[0] < ty[1] < ty[2], f"ty={ty}")
check("lowest tag of a cluster is not moved", ty[0] == 100.0, f"ty={ty}")
ty = charts._spread([100.0, 105.0], [1.0, 1.0], 90.0, 110.0)
check("tags with room stay at their level", ty == [100.0, 105.0], f"ty={ty}")
ty = charts._spread([109.9, 109.95], [1.0, 1.0], 90.0, 110.0)
check("stack pulled back inside the top edge",
      max(ty) + 0.5 <= 110.0 + 1e-9 and ty[1] - ty[0] >= 1.12 - 1e-9, f"ty={ty}")
ty = charts._spread([90.05], [2.0], 90.0, 110.0)
check("lone tag lifted off the bottom edge", ty[0] - 1.0 >= 90.0 - 1e-9,
      f"ty={ty}")
ty = charts._spread([100.04, 100.0], [1.0, 1.0], 90.0, 110.0)
check("unsorted input keeps per-index mapping", ty[0] > ty[1], f"ty={ty}")
check("no tags is fine", charts._spread([], [], 0.0, 1.0) == [])

# 12) end-to-end: a read whose entry/SL hug the live price (the CE-tap
# case that used to stack three unreadable boxes) still renders
bars = make_bars(90)
px = float(bars["Close"].iloc[-1])
r = make_r(stored=None)
r["plan"].update(entry=round(px - 0.01, 2), stop=round(px - 0.03, 2))
png, why = render(r, bars, boom)
check("clustered entry/SL/price read renders", png is not None, str(why))

# 13) order block helper: last opposite-close candle at/before the impulse
o5 = np.array([10.0, 10.2, 10.1, 10.3, 10.5])
c5 = np.array([10.2, 10.1, 10.3, 10.5, 11.5])  # only candle 1 closes down
ob = charts._order_block(o5, c5, 4, "bull")
check("bull OB scans past up-closes to the last down-close",
      ob == (1, 10.1, 10.2), f"ob={ob}")
o5 = np.array([10.0, 10.2, 10.3, 10.1, 10.5])
c5 = np.array([10.2, 10.3, 10.1, 10.0, 9.0])  # candle 3 also closes down
ob = charts._order_block(o5, c5, 4, "bull")
check("bull OB takes the candle nearest the impulse",
      ob == (3, 10.0, 10.1), f"ob={ob}")
ob = charts._order_block(np.array([10.0, 10.1, 10.3]),
                         np.array([10.1, 10.3, 9.5]), 2, "bear")
check("bear OB is the last up-close candle", ob == (1, 10.1, 10.3), f"ob={ob}")
n20 = np.arange(20, dtype=float)
ob = charts._order_block(n20, n20 + 0.1, 19, "bull")   # every candle up
check("no opposite close within reach -> no OB", ob is None, f"ob={ob}")
flat = np.full(6, 10.0)
ob = charts._order_block(flat, flat.copy(), 5, "bull")  # dojis everywhere
check("doji bodies never match", ob is None, f"ob={ob}")
ob = charts._order_block(np.array([10.2, 10.3]), np.array([10.0, 11.1]),
                         1, "bull")
check("OB may be the candle right before the impulse",
      ob == (0, 10.0, 10.2), f"ob={ob}")
o14 = np.ones(15) * 10.0
c14 = o14 + 0.1
o14[0], c14[0] = 10.2, 10.0                             # down-close, 14 back
ob = charts._order_block(o14, c14, 14, "bull")
check("a candle beyond max_back is not this impulse's OB", ob is None,
      f"ob={ob}")

# 14) wiring: render calls the OB helper with the WINDOW-LOCAL impulse index
# and finds the down-close candle planted two bars before the gap
bars = make_bars(90, gap_at=70)
j = 68                                                  # make bar 68 close down
op = float(bars["Open"].iloc[j])
bars.iloc[j, bars.columns.get_loc("Open")] = op + 0.06
bars.iloc[j, bars.columns.get_loc("Close")] = op - 0.06
bars.iloc[j, bars.columns.get_loc("High")] = op + 0.08
bars.iloc[j, bars.columns.get_loc("Low")] = op - 0.08
ob_calls = []
real_ob = charts._order_block


def ob_recorder(o, c, i_mid, kind, **kw):
    out = real_ob(o, c, i_mid, kind, **kw)
    ob_calls.append((i_mid, kind, out and out[0]))
    return out


charts._order_block = ob_recorder
try:
    png, why = render(make_r(stored_from(bars, 70)), bars, boom)
finally:
    charts._order_block = real_ob
# 90 bars -> window starts at 26, so impulse 70 -> 44 and OB 68 -> 42
check("OB drawn from the anchored window", png is not None, str(why))
check("OB helper got the window-local impulse and found the down candle",
      ob_calls == [(44, "bull", 42)], f"calls={ob_calls}")

# 15) inverted gap: the OB of the original impulse points the wrong way, skip
bars = make_bars(90, gap_at=70)
inv = stored_from(bars, 70)
inv["inverted"] = True
ob_calls.clear()
charts._order_block = ob_recorder
try:
    png, why = render(make_r(inv), bars, boom)
finally:
    charts._order_block = real_ob
check("inverted gap renders without an OB", png is not None and not ob_calls,
      f"png={png is not None} calls={ob_calls}")

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")
