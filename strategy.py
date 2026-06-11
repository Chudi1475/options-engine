"""Phase 2 — strategy definition.

`detect_setup()` is the single source of truth for what counts as a setup.
The current entry logic is DERIVED from the May 2026 winners-vs-losers study
(reports/win_study.md), not confirmed by Kelechi. Placeholders marked
# KELECHI RULE: are where his exact rules go once he answers.

In-sample stats behind the derived rule (May 2026, see win_study.md):
- 15-min momentum positive + 0-1 DTE: 79% win rate, 24 trades
- anti-setup (momentum down while above the open): 0% win rate, 6 trades
- entries in the first 15 min (momentum unreadable): 50% win rate, 28 trades
"""

from dataclasses import dataclass, field
from datetime import time

import pandas as pd


@dataclass
class StrategyConfig:
    # what to watch; yf_symbol is what we poll, trade_symbol what the card shows
    watchlist: dict = field(default_factory=lambda: {
        "SPX": "^GSPC",   # trade as SPXW 0DTE
        "TSLA": "TSLA",
        "QCOM": "QCOM",
    })
    timeframe: str = "5m"
    # KELECHI RULE: confirm the entry window. Data says his pre-9:45 entries
    # were coin flips, so the derived window starts when momentum is readable.
    entry_start: time = time(9, 45)
    entry_end: time = time(10, 30)
    dte_target: int = 0          # 0DTE for SPX; nearest weekly for stocks
    # "call" = validated direction only; "both" also fires the put mirror rule.
    # The May study had only 14 puts — the put rule is a mirror, NOT validated.
    direction: str = "both"
    mom_bars: int = 3            # 15 minutes of 5m bars
    # KELECHI RULE: strike selection. Placeholder = first strike above spot.
    strike_increment: dict = field(default_factory=lambda: {"SPX": 5.0})
    default_strike_increment: float = 2.5
    # exits (his stated framework — note the data shows he doesn't honor -30%)
    take_half_pct: float = 60.0
    take_full_pct: float = 120.0
    stop_pct: float = -30.0
    size_pct: float = 10.0


@dataclass
class Setup:
    ticker: str
    direction: str
    strike: float
    spot: float
    mom_pct: float
    reason: str


def _round_strike(spot: float, increment: float, up: bool = True) -> float:
    # first strike at or above spot for calls, at or below for puts (slightly OTM)
    if up:
        return float(-(-spot // increment) * increment)
    return float(spot // increment * increment)


def detect_setup(ticker: str, bars: pd.DataFrame, now, cfg: StrategyConfig):
    """Return a Setup if conditions hold on the latest bar, else None.

    `bars` = today's 5m bars up to `now` (no future bars). Uses only
    completed-bar closes, so the same code is honest in backtest and live.
    """
    if not (cfg.entry_start <= now.time() <= cfg.entry_end):
        return None
    if len(bars) < cfg.mom_bars + 1:
        return None  # momentum not readable yet

    px = float(bars["Close"].iloc[-1])
    day_open = float(bars["Open"].iloc[0])
    mom = (px / float(bars["Close"].iloc[-(cfg.mom_bars + 1)]) - 1) * 100
    above_open = px > day_open

    # Core derived rule (validated, 79% in-sample): momentum up -> call.
    # Mirror rule (NOT validated, only 14 puts in the study): momentum down
    # while also below the open -> put. Requiring below-open keeps the put
    # side off during chop and mirrors the 0-for-6 anti-setup finding.
    if mom > 0 and cfg.direction in ("call", "both"):
        direction = "call"
    elif mom < 0 and not above_open and cfg.direction in ("put", "both"):
        direction = "put"
    else:
        return None

    # KELECHI RULE: his actual trigger (level break? candle pattern? flow?).
    # Until he answers, momentum is the whole trigger.

    increment = cfg.strike_increment.get(ticker, cfg.default_strike_increment)
    strike = _round_strike(px, increment, up=direction == "call")
    above_open_txt = "above" if above_open else "below"
    reason = (f"15-min momentum {mom:+.2f}%, {above_open_txt} open "
              f"({(px / day_open - 1) * 100:+.2f}%) — derived rule, see win_study.md")
    return Setup(ticker=ticker, direction=direction, strike=strike,
                 spot=px, mom_pct=mom, reason=reason)
