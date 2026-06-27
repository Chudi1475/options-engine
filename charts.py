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
