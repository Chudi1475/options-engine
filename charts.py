"""Turn a market read into a picture the bot can text.

Two renderers:
- render_fvg(): the ICT / smart-money chart the guys share, in a clean trading-app
  layout (candles + volume strip, bold color-coded right-axis price tags,
  full-width level lines, the Fair Value Gap boxed with its consequent-
  encroachment line and a BISI/SIBI + grade tag, premium/discount equilibrium,
  and a compact info panel with the whole CE-based ticket).
- render_signal(): a simple price-line fallback with entry/SL/TP.

matplotlib is in requirements.txt (so it's there in the cloud), but this module
NEVER hard-depends on it: if anything fails, render_* returns (None, reason) and
every caller falls back to the text card. Pictures are a bonus, never a blocker.
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
    """The ICT FVG chart in a clean trading-app layout. Candlesticks + a volume
    strip; the Fair Value Gaps boxed (confirming one brightest) each with a CE
    (50%) line and a BISI/SIBI + grade tag; the dealing-range equilibrium; bold
    color-coded right-axis price tags with full-width level lines for the CE
    entry / stop / target; and a compact info panel carrying the whole ticket so
    the candles stay clean. FVGs are recomputed on the exact candles drawn.

    Pass `bars` (an OHLC DataFrame) to chart a specific series instead of
    downloading; otherwise it pulls 5m bars for r['symbol']. Returns
    (png_bytes, None) or (None, reason); fully guarded so any failure falls back
    to render_signal or the text card."""
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

        # ---------------- data ----------------
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
        vol = df["Volume"].astype(float).values if "Volume" in df.columns else None
        has_vol = vol is not None and np.isfinite(np.nansum(vol)) and np.nansum(vol) > 0
        x = list(range(len(df)))
        price = float(c[-1])
        dec = r.get("decimals", 2)
        plan = r.get("plan") or {}
        direction = plan.get("direction")

        # ATR + FVGs on the drawn candles (so boxes line up with the picture)
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

        # ---------------- palette ----------------
        BG, GRID, TXT, MUT = "#0b0d11", "#191c23", "#e8e9ec", "#8a8d93"
        GREEN, RED, GOLD, GREY = "#26a69a", "#ef5350", "#ffcf4a", "#c9ccd1"

        # ---------------- figure: price (+ volume strip) ----------------
        fig = plt.figure(figsize=(9.4, 5.5), dpi=120)
        fig.patch.set_facecolor(BG)
        if has_vol:
            gs = fig.add_gridspec(2, 1, height_ratios=[5, 1], hspace=0.05)
            ax = fig.add_subplot(gs[0])
            axv = fig.add_subplot(gs[1], sharex=ax)
        else:
            ax = fig.add_subplot(111)
            axv = None
        for a in (ax, axv):
            if a is None:
                continue
            a.set_facecolor(BG)
            for sp in a.spines.values():
                sp.set_color("#2a2d34")
        right = x[-1] + 2

        # candles
        for xi, oo, hh, ll, cc in zip(x, o, h, low, c):
            up = cc >= oo
            col = GREEN if up else RED
            ax.plot([xi, xi], [ll, hh], color=col, linewidth=0.9, zorder=3)
            body = max(abs(cc - oo), (hh - ll) * 1e-3 or 1e-9)
            ax.add_patch(Rectangle((xi - 0.32, min(oo, cc)), 0.64, body,
                                   facecolor=col, edgecolor=col, linewidth=0.4, zorder=4))

        # volume strip
        if has_vol:
            for xi, vv, oo, cc in zip(x, vol, o, c):
                if np.isnan(vv):
                    continue
                axv.add_patch(Rectangle((xi - 0.32, 0), 0.64, vv, alpha=0.5,
                                        facecolor=(GREEN if cc >= oo else RED), edgecolor="none"))
            axv.set_ylim(0, (np.nanmax(vol) or 1.0) * 1.15)
            axv.set_yticks([])
            axv.text(0.008, 0.82, "vol", transform=axv.transAxes, color=MUT, fontsize=7, va="top")

        # dealing-range equilibrium (premium above / discount below)
        ax.plot([0, right], [eq, eq], color="#6d6f76", linewidth=0.8,
                linestyle=(0, (1, 3)), alpha=0.75, zorder=2)
        ax.text(0.3, eq, "equilibrium", color="#7c7f86", fontsize=7.5,
                va="bottom", ha="left", zorder=5)

        # liquidity pools: the swing high/low where stops rest and price gets
        # drawn (buy-side / sell-side liquidity) — the read retail charts skip.
        for lvl, lab, va in ((hi_r, "BSL", "bottom"), (lo_r, "SSL", "top")):
            ax.plot([0, right], [lvl, lvl], color="#8a7fb0", linewidth=0.7,
                    linestyle=(0, (1, 4)), alpha=0.6, zorder=1)
            ax.text(0.4, lvl, f" {lab} liquidity", color="#9a8fbf", fontsize=7,
                    va=va, ha="left", zorder=5)

        # FVG boxes: the confirming one + up to 2 more, label boxed at the left
        show = ([conf] if conf else []) + \
               [f for f in fvgs if not (conf and f["i"] == conf["i"])][:2]
        for f in show:
            i = f["i"]
            if i >= len(df):
                continue
            is_conf = bool(conf and f["i"] == conf["i"])
            base = GREEN if f["polarity"] == "bull" else RED
            ax.add_patch(Rectangle((i - 0.5, f["bottom"]), right - (i - 0.5),
                                   f["top"] - f["bottom"], facecolor=base,
                                   alpha=0.30 if is_conf else 0.11,
                                   edgecolor=base, linewidth=1.7 if is_conf else 0.8, zorder=1))
            ax.plot([i - 0.5, right], [f["ce"], f["ce"]], color=base,
                    linewidth=1.0 if is_conf else 0.7, linestyle="--",
                    alpha=0.85 if is_conf else 0.4, zorder=2)
            tag = f["label"] + (" IFVG" if f.get("inverted") else "") + f" · {f['grade']}"
            ax.text(i - 0.2, f["top"], tag, color=base, fontsize=8, fontweight="bold",
                    va="bottom", ha="left", zorder=6,
                    bbox=dict(boxstyle="round,pad=0.2", facecolor=BG,
                              edgecolor=base, linewidth=0.8, alpha=0.9))

        # arrow to the confirming FVG's CE (the entry)
        if conf:
            i = conf["i"]
            off = max(8, len(df) // 6)
            ax.annotate("CE", xy=(i, conf["ce"]), xytext=(max(0, i - off), conf["ce"]),
                        color=GOLD, fontsize=10, fontweight="bold", va="center",
                        arrowprops=dict(arrowstyle="->", color=GOLD, lw=1.8), zorder=7)

        # bold right-axis price tags (app style) + full-width level line
        def level_tag(y, color, label):
            if y is None:
                return
            ax.plot([0, right], [y, y], color=color, linewidth=1.0,
                    linestyle=(0, (5, 3)), alpha=0.5, zorder=3)
            ax.annotate(f"{label} {y:,.{dec}f}", xy=(right, y), xytext=(7, 0),
                        textcoords="offset points", va="center", ha="left",
                        fontsize=8.5, fontweight="bold", color="#0b0d11",
                        bbox=dict(boxstyle="round,pad=0.3", facecolor=color, edgecolor="none"),
                        annotation_clip=False, zorder=8)

        tk = (conf or {}).get("ticket")
        if tk:
            level_tag(tk.get("entry_ce"), GOLD, "CE")
            level_tag(tk.get("stop"), RED, "SL")
            level_tag(tk.get("target_liquidity"), GREEN, "TP")
        elif plan:
            level_tag(plan.get("entry"), GREY, "entry")
            level_tag(plan.get("stop"), RED, "SL")
            level_tag(plan.get("target"), GREEN, "TP")
        level_tag(price, "#5b6472", "px")  # current price, muted so it never fights the levels

        # compact info panel (top-left) carries the numbers so candles stay clean
        lines = []
        if conf:
            lines.append(f"grade {conf['grade']} {conf['label']}"
                         + (" · IFVG" if conf.get("inverted") else ""))
            lines.append(f"{conf['state']} · {conf['pd_zone']}")
            if tk:
                lines.append(f"CE  {tk['entry_ce']:,.{dec}f}")
                lines.append(f"SL  {tk['stop']:,.{dec}f}")
                rr = tk.get("rr")
                lines.append(f"TP  {tk['target_liquidity']:,.{dec}f}"
                             + (f"   {rr:g}R" if rr else ""))
        else:
            conv = (r.get("conviction") or "").upper()
            if conv:
                lines.append(f"conviction {conv}")
            if plan.get("entry") is not None:
                lines.append(f"entry {plan['entry']:,.{dec}f}")
                if plan.get("stop") is not None:
                    lines.append(f"SL  {plan['stop']:,.{dec}f}")
        if lines:
            # info panel on the RIGHT (block hugs the right edge, text stays
            # left-aligned inside) so every number lives on one side of the chart
            # next to the price tags, no left-right eye scanning
            ax.text(0.987, 0.97, "\n".join(lines), transform=ax.transAxes, va="top",
                    ha="right", ma="left", fontsize=9, color=TXT, fontweight="bold",
                    zorder=9, linespacing=1.55, family="monospace",
                    bbox=dict(boxstyle="round,pad=0.5", facecolor="#12151b",
                              edgecolor="#2a2d34", alpha=0.94))

        # ---------------- cosmetics ----------------
        sym = r.get("ticker") or r.get("instrument", symbol)
        title = f"{direction + '  ' if direction else ''}{sym}"
        if conf:
            title += f"     grade {conf['grade']} {conf['label']} · CE entry"
        elif r.get("conviction"):
            title += f"     {r['conviction'].upper()}"
        ax.set_title(title, color=TXT, fontsize=13.5, fontweight="bold", loc="left", pad=9)

        ax.set_xlim(-1, right + max(4, len(df) * 0.04))
        ax.margins(y=0.09)
        ax.grid(True, color=GRID, linewidth=0.5, zorder=0)
        bottom_ax = axv if axv is not None else ax
        step = max(1, len(df) // 7)
        bottom_ax.set_xticks(x[::step])
        bottom_ax.set_xticklabels([df.index[i].strftime("%H:%M") for i in x[::step]])
        bottom_ax.tick_params(colors=MUT, labelsize=8)
        if axv is not None:
            ax.tick_params(labelbottom=False, colors=MUT, labelsize=8)
            axv.grid(False)
        else:
            ax.tick_params(colors=MUT, labelsize=8)

        stamp = datetime.now(ET).strftime("%a %b %d %I:%M %p ET")
        fig.text(0.995, 0.008, f"{stamp}  ·  ~15m delayed, your call",
                 color="#5a5d63", fontsize=7, ha="right", va="bottom")
        fig.tight_layout(rect=(0, 0.02, 1, 1))

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
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
