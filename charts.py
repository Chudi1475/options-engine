"""Turn a market read into a picture the bot can text: a clean price line with
the entry, stop, and target drawn on it, so a signal lands like the chart
screenshots the guys already share.

matplotlib is in requirements.txt (so it's there in the cloud), but this module
NEVER hard-depends on it: if the import fails for any reason, render_* returns
(None, reason) and every caller falls back to the text card. Pictures are a
bonus, never a blocker.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _mpl():
    """Import matplotlib with the headless Agg backend, or return None."""
    try:
        import matplotlib
        matplotlib.use("Agg")  # no display on a server
        import matplotlib.pyplot as plt
        return plt
    except Exception:
        return None


def available() -> bool:
    return _mpl() is not None


def render_fvg(r: dict, bars=None):
    """A CANDLESTICK chart for one read with the Fair Value Gaps shaded as boxes,
    each with its consequent-encroachment (CE / 50%) line and a BISI/SIBI + grade
    tag, an arrow on the FVG that confirms the trade, the premium/discount
    equilibrium, and the CE-based entry/stop/target. This is the picture the ICT
    guys share. Returns (png_bytes, None) or (None, reason). FVGs are recomputed
    on the exact candles being drawn so the boxes always line up. Pass `bars` (an
    OHLC DataFrame) to chart a specific series instead of downloading; otherwise
    it pulls 5m bars for r['symbol']. Fully guarded: any failure returns
    (None, reason) and the caller falls back to render_signal or the text card."""
    plt = _mpl()
    if plt is None:
        return None, "charts need matplotlib (not installed here)"
    symbol = r.get("symbol")
    if not symbol and bars is None:
        return None, "no symbol to chart"
    fig = None
    try:
        import io

        import numpy as np
        import yfinance as yf
        from matplotlib.patches import Rectangle

        import fvg as fvg_mod

        if bars is not None:
            df = bars.copy()
        else:
            df = yf.download(symbol, period="2d", interval="5m",
                             progress=False, auto_adjust=False)
            if df is not None and hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
            if df is not None and not df.empty and getattr(df.index, "tz", None) is not None:
                try:
                    df.index = df.index.tz_convert(ET)
                except (TypeError, ValueError):
                    pass
        if df is None or df.empty or not {"Open", "High", "Low", "Close"}.issubset(df.columns):
            return None, "no intraday candles to chart"
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).tail(80)
        if len(df) < 5:
            return None, "not enough candles to chart"

        o = df["Open"].astype(float).values
        h = df["High"].astype(float).values
        low = df["Low"].astype(float).values
        c = df["Close"].astype(float).values
        x = list(range(len(df)))
        price = float(c[-1])

        dec = r.get("decimals", 2)
        plan = r.get("plan") or {}
        direction = plan.get("direction")

        # ATR over the drawn window, so gap-size filtering matches the picture
        atr = None
        if len(df) > 2:
            tr = np.maximum(h[1:] - low[1:],
                            np.maximum(np.abs(h[1:] - c[:-1]), np.abs(low[1:] - c[:-1])))
            if len(tr):
                atr = float(np.nanmean(tr[-14:]))
        bias = r.get("bias")
        fvgs = [f for f in fvg_mod.find_fvgs(df, atr, bias) if f["state"] != "filled"]
        fvgs.sort(key=lambda f: (f["score"], f["i"]), reverse=True)
        conf = fvg_mod.confirming_fvg(df, direction, price, atr, bias)
        hi_r, lo_r = float(df["High"].max()), float(df["Low"].min())
        eq = (hi_r + lo_r) / 2.0

        fig, ax = plt.subplots(figsize=(8, 4.8), dpi=115)
        fig.patch.set_facecolor("#0e0f13")
        ax.set_facecolor("#0e0f13")

        # candlesticks (drawn by index so overnight gaps don't leave dead space)
        for xi, oo, hh, ll, cc in zip(x, o, h, low, c):
            up = cc >= oo
            col = "#26a69a" if up else "#ef5350"
            ax.plot([xi, xi], [ll, hh], color=col, linewidth=0.8, zorder=2)
            body = max(abs(cc - oo), (hh - ll) * 1e-3 or 1e-9)
            ax.add_patch(Rectangle((xi - 0.3, min(oo, cc)), 0.6, body,
                                   facecolor=col, edgecolor=col, linewidth=0.4, zorder=3))

        right = x[-1] + 2
        # dealing-range equilibrium: premium above, discount below
        ax.axhline(eq, color="#6d6f76", linewidth=0.8, linestyle=":", alpha=0.7, zorder=1)
        ax.text(0, eq, " equilibrium (50%)", color="#6d6f76", fontsize=7,
                va="bottom", ha="left", zorder=5)

        # draw the strongest few actionable FVGs (best grade first); each shades
        # forward from its formation candle with a dashed CE (50%) line and a
        # BISI/SIBI + grade tag. The confirming one is brightest and outlined.
        for f in fvgs[:4]:
            i = f["i"]
            if i >= len(df):
                continue
            is_conf = bool(conf and f["i"] == conf["i"])
            bull = f["polarity"] == "bull"
            base = "#26a69a" if bull else "#ef5350"
            ax.add_patch(Rectangle((i - 0.5, f["bottom"]), right - (i - 0.5),
                                   f["top"] - f["bottom"], facecolor=base,
                                   alpha=0.32 if is_conf else 0.10,
                                   edgecolor=base if is_conf else "none",
                                   linewidth=1.6 if is_conf else 0, zorder=1))
            ax.plot([i - 0.5, right], [f["ce"], f["ce"]], color=base, linewidth=0.9,
                    linestyle="--", alpha=0.75 if is_conf else 0.35, zorder=2)
            tag = f["label"] + (" IFVG" if f.get("inverted") else "") + f" {f['grade']}"
            ax.text(i - 0.4, f["top"], f" {tag}", color=base, fontsize=7.5,
                    fontweight="bold", va="bottom", ha="left", zorder=6)

        # arrow pointing at the confirming FVG's CE (the entry)
        if conf:
            i = conf["i"]
            off = max(8, len(df) // 6)
            ax.annotate(f"{conf['label']} CE", xy=(i, conf["ce"]),
                        xytext=(max(0, i - off), conf["ce"]),
                        color="#ffd54f", fontsize=10.5, fontweight="bold", va="center",
                        arrowprops=dict(arrowstyle="->", color="#ffd54f", lw=1.8), zorder=7)

        def hline(level, color, label):
            if level is None:
                return
            ax.axhline(level, color=color, linewidth=1.1, linestyle="--", alpha=0.9, zorder=4)
            ax.text(x[-1], level, f" {label} {level:,.{dec}f}", color=color, fontsize=8,
                    va="center", ha="left", fontweight="bold", zorder=6)

        tk = (conf or {}).get("ticket")
        if tk:  # a confirming FVG -> the CE-based FVG trade is the star
            hline(tk.get("entry_ce"), "#ffd54f", "CE entry")
            hline(tk.get("stop"), "#ef5350", "SL")
            hline(tk.get("target_liquidity"), "#26a69a", "target")
        elif plan:
            hline(plan.get("entry"), "#d0d0d0", "entry")
            hline(plan.get("stop"), "#ef5350", "SL")
            hline(plan.get("target1"), "#7e9e6d", "TP1")
            hline(plan.get("target"), "#26a69a", "TP2")

        sym = r.get("ticker") or r.get("instrument", symbol)
        conv = (r.get("conviction") or "").upper()
        title = f"{direction + ' ' if direction else ''}{sym}"
        if conf:
            title += f"  ·  grade {conf['grade']} {conf['label']} · CE entry"
        elif conv:
            title += f"  ·  {conv}"
        ax.set_title(title, color="#f0f0f0", fontsize=13, fontweight="bold", loc="left")

        ax.set_xlim(-1, right + 1)
        step = max(1, len(df) // 6)
        ticks = x[::step]
        ax.set_xticks(ticks)
        ax.set_xticklabels([df.index[i].strftime("%H:%M") for i in ticks])
        ax.tick_params(colors="#8a8d93", labelsize=8)
        for spine in ax.spines.values():
            spine.set_color("#2a2d34")
        ax.grid(True, color="#1c1f26", linewidth=0.5, zorder=0)
        stamp = datetime.now(ET).strftime("%a %b %d %I:%M %p ET")
        ax.text(0.99, 0.02, f"{stamp}  ·  ~15m delayed, your call",
                transform=ax.transAxes, color="#5a5d63", fontsize=7,
                ha="right", va="bottom")
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        return buf.getvalue(), None
    except Exception as e:
        return None, f"couldn't render fvg ({e})"
    finally:
        if fig is not None:
            try:
                plt.close(fig)
            except Exception:
                pass


def render_signal(r: dict):
    """A price chart for one read, with entry/SL/TP lines when the read carries
    a plan. Returns (png_bytes, None) on success or (None, reason). The ENTIRE
    body is guarded: a picture is a bonus, so any failure (bad data, a matplotlib
    edge) returns (None, reason) and the caller falls back to the text card."""
    plt = _mpl()
    if plt is None:
        return None, "charts need matplotlib (not installed here)"
    symbol = r.get("symbol")
    if not symbol:
        return None, "no symbol to chart"
    fig = None
    try:
        import io

        import yfinance as yf

        df = yf.download(symbol, period="2d", interval="5m",
                         progress=False, auto_adjust=False)
        if df is not None and hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        if df is None or df.empty or "Close" not in df.columns:
            return None, "no intraday bars to chart"
        closes = df["Close"].dropna()
        if closes.empty:
            return None, "no intraday bars to chart"
        if getattr(df.index, "tz", None) is not None:
            try:
                df.index = df.index.tz_convert(ET)
            except (TypeError, ValueError):
                pass
        closes = df["Close"].dropna().tail(120)  # ~last 10 hours of 5m bars

        kind = r.get("kind", "stock")
        dec = r.get("decimals", 2)
        sym = r.get("ticker") or r.get("instrument", symbol)
        plan = r.get("plan") or {}
        up = plan.get("direction") == "BUY"

        fig, ax = plt.subplots(figsize=(8, 4.5), dpi=110)
        fig.patch.set_facecolor("#0e0f13")
        ax.set_facecolor("#0e0f13")
        line_color = "#26a69a" if up else "#ef5350"
        ax.plot(closes.index, closes.values, color=line_color, linewidth=1.6)

        def hline(level, color, label):
            if level is None:
                return
            ax.axhline(level, color=color, linewidth=1.2, linestyle="--", alpha=0.9)
            ax.text(closes.index[-1], level, f" {label} {level:,.{dec}f}",
                    color=color, fontsize=9, va="center", ha="left", fontweight="bold")

        if plan:
            hline(plan.get("entry"), "#d0d0d0", "entry")
            hline(plan.get("stop"), "#ef5350", "SL")
            hline(plan.get("target1"), "#7e9e6d", "TP1")
            hline(plan.get("target"), "#26a69a", "TP2")
            title = f"{plan['direction']} {sym}"
        else:
            title = f"{sym}"

        ax.set_title(title, color="#f0f0f0", fontsize=14, fontweight="bold", loc="left")
        ax.tick_params(colors="#8a8d93", labelsize=8)
        for spine in ax.spines.values():
            spine.set_color("#2a2d34")
        ax.grid(True, color="#1c1f26", linewidth=0.6)
        try:
            import matplotlib.dates as mdates
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=ET))
        except Exception:
            pass
        stamp = datetime.now(ET).strftime("%a %b %d %I:%M %p ET")
        ax.text(0.99, 0.02, f"{stamp}  ·  ~15m delayed, your call",
                transform=ax.transAxes, color="#5a5d63", fontsize=7,
                ha="right", va="bottom")
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        return buf.getvalue(), None
    except Exception as e:
        return None, f"couldn't render ({e})"
    finally:
        if fig is not None:
            try:
                plt.close(fig)
            except Exception:
                pass
