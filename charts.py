"""Turn a market read into a picture the bot can text.

render_fvg(): the chart the guys share, styled EXACTLY like the TradingView
light-theme markups from the group chat: white background, candlesticks, a gray
analysis zone with a fib-style level ladder (level (price) labels on its left),
a diagonal trendline, an orange channel around recent price action, red alert
lines with red price tags pinned to the right axis, a dotted red current-price
line with a price+time tag, a Target label at the objective, PLUS the FVG boxes
(BISI/SIBI + grade) and CE line that are the bot's own edge.

render_signal(): simple dark price-line fallback with entry/SL/TP.

matplotlib is in requirements.txt (cloud-safe), but this module NEVER
hard-depends on it: any failure returns (None, reason) and callers fall back to
the text card. Pictures are a bonus, never a blocker.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")  # Chudi's time: every displayed time is Central

# TradingView light palette
_BG = "#ffffff"
_GRID = "#e9ebf0"
_TXT = "#131722"
_MUT = "#787b86"
_GREEN = "#089981"
_RED = "#f23645"
_ORANGE = "#f7931a"
_LEVEL = "#5d606b"      # dark fib level lines
_ZONE = "#9598a1"       # gray analysis zone fill
_LBL = "#4a4e59"        # fib label text


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
    """TradingView-light FVG chart (see module docstring). Pass `bars` (OHLC
    DataFrame) to chart a specific series; otherwise downloads 5m bars for
    r['symbol']. Returns (png_bytes, None) or (None, reason); fully guarded."""
    plt = _mpl()
    if plt is None:
        return None, "charts need matplotlib (not installed here)"
    if not isinstance(r, dict):  # keep the (None, reason) contract even on a bad read
        return None, "bad read, nothing to chart"
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

        # ---------------- data ----------------
        if bars is not None:
            df = bars.copy()
        else:
            df = yf.download(symbol, period="2d", interval="5m", prepost=True,
                             progress=False, auto_adjust=False)
            if df is not None and hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
            if df is not None and not df.empty and getattr(df.index, "tz", None) is not None:
                try:
                    df.index = df.index.tz_convert(CT)
                except (TypeError, ValueError):
                    pass
        if df is None or df.empty or not {"Open", "High", "Low", "Close"}.issubset(df.columns):
            return None, "no intraday candles to chart"
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).tail(64)
        if len(df) < 8:
            return None, "not enough candles to chart"

        o = df["Open"].astype(float).values
        h = df["High"].astype(float).values
        low = df["Low"].astype(float).values
        c = df["Close"].astype(float).values
        x = np.arange(len(df))
        n = len(df)
        price = float(c[-1])
        dec = r.get("decimals", 2)
        plan = r.get("plan") or {}
        direction = plan.get("direction")

        atr = None
        tr = np.maximum(h[1:] - low[1:],
                        np.maximum(np.abs(h[1:] - c[:-1]), np.abs(low[1:] - c[:-1])))
        if len(tr):
            atr = float(np.nanmean(tr[-14:]))
        bias = r.get("bias")
        fvgs = [f for f in fvg_mod.find_fvgs(df, atr, bias) if f["state"] != "filled"]
        fvgs.sort(key=lambda f: (f["score"], f["i"]), reverse=True)
        conf = fvg_mod.confirming_fvg(df, direction, price, atr, bias)
        tk = (conf or {}).get("ticket")
        hi_r, lo_r = float(np.max(h)), float(np.min(low))
        span = max(hi_r - lo_r, 1e-9)
        sell = (direction == "SELL") or (not direction and price < (hi_r + lo_r) / 2)

        def fmt(v):
            return f"{v:,.{dec}f}"

        # ---------------- figure ----------------
        fig, ax = plt.subplots(figsize=(7.6, 8.2), dpi=120)
        fig.patch.set_facecolor(_BG)
        ax.set_facecolor(_BG)
        for sp in ax.spines.values():
            sp.set_visible(False)
        ax.yaxis.tick_right()
        ax.grid(True, color=_GRID, linewidth=0.8, zorder=0)
        ax.tick_params(colors=_MUT, labelsize=9, length=0)
        for lab in ax.get_yticklabels():
            lab.set_color(_TXT)
            lab.set_fontweight("bold")

        right = n + 2

        # gray analysis zone anchored at the FVG / swing extreme
        i0 = conf["i"] if conf else (int(np.argmax(h)) if sell else int(np.argmin(low)))
        x0 = max(0, i0 - 1)

        # fib-style ladder over the dealing range, extensions in trade direction
        fib = [1.0, 0.786, 0.618, 0.5, 0.382, 0.236, 0.0]
        exts = [-0.27, -0.414, -0.618] if sell else [1.27, 1.414, 1.618]
        levels = [(f, lo_r + f * span) for f in fib + (exts if direction else [])]
        zone_lo = min(p for _, p in levels)
        zone_hi = max(p for _, p in levels)
        ax.add_patch(Rectangle((x0, zone_lo), right - x0, zone_hi - zone_lo,
                               facecolor=_ZONE, alpha=0.28, edgecolor="none", zorder=1))
        for f, p in levels:
            ax.plot([x0, right], [p, p], color=_LEVEL, linewidth=1.3, zorder=2)
            ax.text(x0 - 0.7, p, f"{f:g} ({fmt(p)})", color=_LBL, fontsize=8.2,
                    va="center", ha="right", zorder=6)

        # diagonal trendline: swing extreme through the latest counter-extreme
        try:
            if sell:
                i1 = int(np.argmax(h))
                seg = h[i1 + 3:]
                if len(seg) >= 2:
                    i2 = i1 + 3 + int(np.argmax(seg))
                    sl = (h[i2] - h[i1]) / max(i2 - i1, 1)
                    if sl < 0:
                        ax.plot([i1, right], [h[i1], h[i1] + sl * (right - i1)],
                                color=_LEVEL, linewidth=1.6, zorder=3)
            else:
                i1 = int(np.argmin(low))
                seg = low[i1 + 3:]
                if len(seg) >= 2:
                    i2 = i1 + 3 + int(np.argmin(seg))
                    sl = (low[i2] - low[i1]) / max(i2 - i1, 1)
                    if sl > 0:
                        ax.plot([i1, right], [low[i1], low[i1] + sl * (right - i1)],
                                color=_LEVEL, linewidth=1.6, zorder=3)
        except Exception:
            pass

        # FVG boxes (the bot's own edge), light tint + badge, CE dashed
        for f in ([conf] if conf else []) + [f for f in fvgs if not (conf and f["i"] == conf["i"])][:2]:
            i = f["i"]
            if i >= n:
                continue
            is_conf = bool(conf and f["i"] == conf["i"])
            base = _GREEN if f["polarity"] == "bull" else _RED
            ax.add_patch(Rectangle((i - 0.5, f["bottom"]), right - (i - 0.5),
                                   f["top"] - f["bottom"], facecolor=base,
                                   alpha=0.16 if is_conf else 0.09,
                                   edgecolor=base, linewidth=1.1 if is_conf else 0.6,
                                   zorder=2))
            ax.plot([i - 0.5, right], [f["ce"], f["ce"]], color=base, linewidth=0.9,
                    linestyle="--", alpha=0.8 if is_conf else 0.4, zorder=3)
            tag = f["label"] + (" IFVG" if f.get("inverted") else "") + f" · {f['grade']}"
            ax.text(i - 0.3, f["top"], tag, color=base, fontsize=7.6, fontweight="bold",
                    va="bottom", ha="left", zorder=7,
                    bbox=dict(boxstyle="round,pad=0.22", facecolor=_BG,
                              edgecolor=base, linewidth=0.7, alpha=0.95))

        # candlesticks ON TOP of the zone, TradingView colors
        for xi, oo, hh, ll, cc in zip(x, o, h, low, c):
            col = _GREEN if cc >= oo else _RED
            ax.plot([xi, xi], [ll, hh], color=col, linewidth=1.0, zorder=4)
            body = max(abs(cc - oo), span * 1e-4)
            ax.add_patch(Rectangle((xi - 0.31, min(oo, cc)), 0.62, body,
                                   facecolor=col, edgecolor=col, linewidth=0.4, zorder=5))

        # orange channel around the recent run
        try:
            k = min(24, max(10, n // 3))
            xs = x[-k:].astype(float)
            fitl = np.poly1d(np.polyfit(xs, c[-k:], 1))
            up = float(np.max(h[-k:] - fitl(xs)))
            dn = float(np.max(fitl(xs) - low[-k:]))
            k0, k1 = xs[0], xs[-1]
            pts_x = [k0, k1, k1, k0, k0]
            pts_y = [fitl(k0) + up, fitl(k1) + up, fitl(k1) - dn, fitl(k0) - dn, fitl(k0) + up]
            ax.plot(pts_x, pts_y, color=_ORANGE, linewidth=2.0, zorder=6,
                    solid_joinstyle="round")
        except Exception:
            pass

        # red alert lines + red right-axis tags (entry/CE and SL), like the app
        def red_tag(y, two_line=None, dotted=False):
            if y is None:
                return
            ax.plot([0, right], [y, y], color=_RED,
                    linewidth=1.2 if dotted else 2.2,
                    linestyle=(0, (2, 3)) if dotted else "-", zorder=6)
            txt = two_line if two_line else fmt(y)
            ax.annotate(txt, xy=(right, y), xytext=(8, 0),
                        textcoords="offset points", va="center", ha="left",
                        fontsize=9, fontweight="bold", color="#ffffff",
                        linespacing=1.3,
                        bbox=dict(boxstyle="round,pad=0.32", facecolor=_RED,
                                  edgecolor="none"),
                        annotation_clip=False, zorder=9)

        entry_lvl = tk.get("entry_ce") if tk else plan.get("entry")
        sl_lvl = tk.get("stop") if tk else plan.get("stop")
        tp_lvl = tk.get("target_liquidity") if tk else plan.get("target")
        red_tag(entry_lvl)
        red_tag(sl_lvl)
        # dotted current price with price + time tag (exactly like the app)
        now_et = datetime.now(CT)
        red_tag(price, two_line=f"{fmt(price)}\n{now_et:%H:%M}", dotted=True)

        # arrows for extra read: projected path from the last candle to the
        # target, and a pointer into the confirming FVG entry zone
        if tp_lvl is not None:
            try:
                ax.annotate("", xy=(min(n + 4, right - 0.5), tp_lvl),
                            xytext=(n - 1, price),
                            arrowprops=dict(arrowstyle="-|>", color=_TXT, lw=2.0,
                                            connectionstyle="arc3,rad=-0.25"),
                            zorder=8)
            except Exception:
                pass
        if conf:
            off = max(6, n // 6)
            ax.annotate("entry", xy=(conf["i"] + 0.5, conf["ce"]),
                        xytext=(max(0, conf["i"] - off), conf["ce"]),
                        color=_TXT, fontsize=9, fontweight="bold", va="center",
                        ha="right",
                        arrowprops=dict(arrowstyle="->", color=_TXT, lw=1.6),
                        zorder=8)

        # Target label + bullseye at the objective
        if tp_lvl is not None:
            tx = x0 + 0.58 * (right - x0)
            ax.text(tx, tp_lvl, "Target ", color=_TXT, fontsize=13,
                    fontweight="bold", style="italic", va="center", ha="right",
                    zorder=8,
                    bbox=dict(boxstyle="round,pad=0.25", facecolor="#ffffff",
                              edgecolor="none", alpha=0.85))
            for sz, colr in ((130, _RED), (60, "#ffffff"), (18, _RED)):
                ax.scatter([tx + (right - x0) * 0.02], [tp_lvl], s=sz, color=colr,
                           zorder=9, edgecolors="none")

        # ---------------- cosmetics ----------------
        name = r.get("instrument") or r.get("ticker") or symbol
        fig.text(0.055, 0.965, str(name), color=_TXT, fontsize=15.5,
                 fontweight="bold", ha="left", va="top")
        prior = r.get("prior_close")
        line2_y = 0.932
        if prior:
            chg = price - float(prior)
            pct = chg / float(prior) * 100
            fig.text(0.055, line2_y,
                     f"{fmt(price)} {chg:+,.{dec}f} ({pct:+.2f}%)",
                     color=(_RED if chg < 0 else _GREEN), fontsize=11.5,
                     ha="left", va="top")
        if conf:
            fig.text(0.055, line2_y - 0.028,
                     f"grade {conf['grade']} {conf['label']} · CE entry"
                     + (f" · {tk['rr']:g}R" if tk and tk.get('rr') else ""),
                     color=_MUT, fontsize=9.5, ha="left", va="top")

        ax.set_xlim(-1.5, right + 0.5)
        ymin = min(zone_lo, float(np.min(low)), sl_lvl or zone_lo, price)
        ymax = max(zone_hi, float(np.max(h)), entry_lvl or zone_hi, price)
        pad = (ymax - ymin) * 0.05
        ax.set_ylim(ymin - pad, ymax + pad)
        step = max(1, n // 5)
        ax.set_xticks(x[::step])
        ax.set_xticklabels([df.index[i].strftime("%H:%M") for i in x[::step]])
        fig.text(0.985, 0.012,
                 f"{now_et:%a %b %d %I:%M %p CT}  ·  ~15m delayed, your call",
                 color=_MUT, fontsize=7, ha="right", va="bottom")
        fig.subplots_adjust(top=0.885, bottom=0.055, left=0.03, right=0.87)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor(),
                    bbox_inches="tight")
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
    a plan. Returns (png_bytes, None) on success or (None, reason). Fully
    guarded: any failure returns (None, reason) and the caller falls back to
    the text card."""
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

        df = yf.download(symbol, period="2d", interval="5m", prepost=True,
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
                df.index = df.index.tz_convert(CT)
            except (TypeError, ValueError):
                pass
        closes = df["Close"].dropna().tail(120)

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
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=CT))
        except Exception:
            pass
        stamp = datetime.now(CT).strftime("%a %b %d %I:%M %p CT")
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
