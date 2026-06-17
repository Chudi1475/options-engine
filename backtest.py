"""Phase 3 — backtester.

Runs detect_setup()'s entry logic over every day of available 5-minute data,
prices the options with Black-Scholes on trailing realized vol (LABELED:
approximated options pricing — no free historical option chains exist), applies
slippage and per-contract fees, and the exit framework (+60% half, +120% full,
-30% stop, time stop at session end for 0DTE / expiry for weeklies).

Honesty notes baked in:
- Signals use only bars at or before decision time (no lookahead).
- Entry fills at model price +1.5%, exits at -1.5% (slippage), fees both ways.
- yfinance 5m history is capped at ~60 days. A longer backtest needs a paid
  options-data API (Polygon/ThetaData); SPX daily 0DTE has only existed since
  May 2022, so a "10 year" backtest of this exact strategy cannot exist.

Usage:
    python backtest.py
"""

import json
import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from strategy import StrategyConfig, detect_setup

ET = ZoneInfo("America/New_York")
REPORTS_DIR = Path(__file__).parent / "reports"

SLIPPAGE = 0.015          # pay 1.5% over model on entry, give up 1.5% on exit
FEE_PER_CONTRACT = 1.30   # commission + exchange, per contract per side
CONTRACTS = 2             # 2 lots so "sell half" is clean
MULTIPLIER = 100
RISK_FREE = 0.04


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S, K, T_years, sigma, right: str):
    """Black-Scholes, floored at intrinsic."""
    intrinsic = max(0.0, S - K) if right == "C" else max(0.0, K - S)
    if T_years <= 0 or sigma <= 0:
        return intrinsic
    d1 = (math.log(S / K) + (RISK_FREE + sigma**2 / 2) * T_years) / (sigma * math.sqrt(T_years))
    d2 = d1 - sigma * math.sqrt(T_years)
    if right == "C":
        px = S * norm_cdf(d1) - K * math.exp(-RISK_FREE * T_years) * norm_cdf(d2)
    else:
        px = K * math.exp(-RISK_FREE * T_years) * norm_cdf(-d2) - S * norm_cdf(-d1)
    return max(px, intrinsic)


@dataclass
class BtTrade:
    ticker: str
    right: str
    strike: float
    entry_time: datetime
    entry_premium: float   # per contract, after slippage
    exit_legs: list        # [(time, premium_after_slippage, n_contracts, label)]
    expiry: datetime

    @property
    def pnl(self) -> float:
        gross = sum((px - self.entry_premium) * n * MULTIPLIER for _, px, n, _ in self.exit_legs)
        fees = FEE_PER_CONTRACT * CONTRACTS * 2  # entry + exit sides
        return gross - fees

    @property
    def ret_pct(self) -> float:
        cost = self.entry_premium * CONTRACTS * MULTIPLIER + FEE_PER_CONTRACT * CONTRACTS
        return self.pnl / cost * 100


def realized_vol(daily_closes: pd.Series, asof_date, window=20) -> float:
    closes = daily_closes[daily_closes.index.date < asof_date].tail(window + 1)
    if len(closes) < window // 2:
        return 0.0
    rets = closes.pct_change().dropna()
    return float(rets.std() * math.sqrt(252))


def expiry_for(ticker: str, day: datetime) -> datetime:
    if ticker in ("SPX", "SPY"):
        exp_day = day  # 0DTE (both have daily expirations)
    else:
        exp_day = day + timedelta(days=(4 - day.weekday()) % 7)  # this week's Friday
    return exp_day.replace(hour=16, minute=0, second=0)


def years_to_expiry(now: datetime, expiry: datetime) -> float:
    return max((expiry - now).total_seconds(), 0) / (365.0 * 24 * 3600)


def simulate_exits(trade_bars, entry_t, entry_prem, strike, right, sigma, expiry, cfg):
    """Walk forward bar by bar applying the exit framework. Returns exit legs."""
    legs = []
    remaining = CONTRACTS
    half_taken = False
    last = None
    for ts, S in trade_bars:
        if ts <= entry_t:
            continue
        prem = bs_price(S, strike, years_to_expiry(ts, expiry), sigma, right)
        prem_net = prem * (1 - SLIPPAGE)
        ret = (prem_net / entry_prem - 1) * 100
        last = (ts, prem_net)
        if ret <= cfg.stop_pct:
            legs.append((ts, prem_net, remaining, f"stop {cfg.stop_pct:g}%"))
            return legs
        if (cfg.take_half_pct is not None and not half_taken
                and ret >= cfg.take_half_pct and remaining > 1):
            half = remaining // 2
            legs.append((ts, prem_net, half, f"half +{cfg.take_half_pct:g}%"))
            remaining -= half
            half_taken = True
        if ret >= cfg.take_full_pct:
            legs.append((ts, prem_net, remaining, f"full +{cfg.take_full_pct:g}%"))
            return legs
    if last is not None and remaining > 0:
        legs.append((last[0], last[1], remaining, "time stop"))
    return legs


def load_data(cfg):
    intraday, daily = {}, {}
    for ticker, yfs in cfg.watchlist.items():
        i5 = yf.download(yfs, period="60d", interval="5m", progress=False, auto_adjust=False)
        d1 = yf.download(yfs, period="1y", interval="1d", progress=False, auto_adjust=False)
        for df in (i5, d1):
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
        i5.index = i5.index.tz_convert(ET)
        intraday[ticker], daily[ticker] = i5, d1
    return intraday, daily


def run_backtest(cfg, intraday, daily):
    trades = []
    skipped_no_vol = 0
    for ticker in cfg.watchlist:
        bars_all = intraday[ticker]
        for day in sorted(set(bars_all.index.date)):
            day_bars = bars_all[bars_all.index.date == day]
            if day_bars.empty:
                continue
            sigma = realized_vol(daily[ticker]["Close"], day)
            if sigma <= 0:
                skipped_no_vol += 1
                continue
            entered_dirs = set()
            for i in range(len(day_bars)):
                upto = day_bars.iloc[: i + 1]
                now = upto.index[-1].to_pydatetime()
                if not (cfg.entry_start <= now.time() <= cfg.entry_end):
                    continue
                setup = detect_setup(ticker, upto, now, cfg)
                if setup is None or setup.direction in entered_dirs:
                    continue
                entered_dirs.add(setup.direction)  # one trade per direction per day
                right = "C" if setup.direction == "call" else "P"
                expiry = expiry_for(ticker, now)
                model_prem = bs_price(setup.spot, setup.strike,
                                      years_to_expiry(now, expiry), sigma, right)
                entry_prem = model_prem * (1 + SLIPPAGE)
                if entry_prem < 0.10:
                    continue
                # bars available for exits: rest of today, plus future days up
                # to expiry for weeklies (0DTE ends today by construction)
                future = bars_all[(bars_all.index > upto.index[-1])
                                  & (bars_all.index <= pd.Timestamp(expiry))]
                seq = [(ts.to_pydatetime(), float(row["Close"])) for ts, row in future.iterrows()]
                legs = simulate_exits(seq, now, entry_prem, setup.strike, right,
                                      sigma, expiry, cfg)
                if not legs:
                    continue
                trades.append(BtTrade(ticker, right, setup.strike, now,
                                      entry_prem, legs, expiry))
    return trades, skipped_no_vol


def metrics(trades):
    if not trades:
        return None
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    rets = [t.ret_pct for t in trades]
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "avg_win_pct": sum(t.ret_pct for t in wins) / len(wins) if wins else 0.0,
        "avg_loss_pct": sum(t.ret_pct for t in losses) / len(losses) if losses else 0.0,
        "expectancy_pct": sum(rets) / len(rets),
        "total_pnl": sum(t.pnl for t in trades),
        "max_drawdown": -max_dd,
        "start": min(t.entry_time for t in trades).strftime("%m/%d/%Y"),
        "end": max(t.entry_time for t in trades).strftime("%m/%d/%Y"),
    }


def fmt(m, title):
    if m is None:
        return [f"### {title}", "", "No trades.", ""]
    return [
        f"### {title}",
        "",
        f"- Trades: **{m['trades']}** ({m['start']} – {m['end']})",
        f"- Win rate: **{m['win_rate']:.1f}%**",
        f"- Avg win: **{m['avg_win_pct']:+.1f}%** | Avg loss: **{m['avg_loss_pct']:+.1f}%**",
        f"- Expectancy: **{m['expectancy_pct']:+.1f}% per trade**",
        f"- Total P&L (2 contracts/trade, net of slippage+fees): **${m['total_pnl']:,.0f}**",
        f"- Max drawdown: **${m['max_drawdown']:,.0f}**",
        "",
    ]


def exit_grid(intraday, daily):
    """Sweep exit brackets (calls only, no half leg) to show the win-rate vs
    payoff trade-off. Same entries every run; only exits change."""
    rows = []
    for target in (10, 15, 20, 25, 40, 60, 90, 120):
        for stop in (-20, -30, -40, -50, -60):
            cfg = StrategyConfig(direction="call")
            cfg.take_half_pct = None
            cfg.take_full_pct = float(target)
            cfg.stop_pct = float(stop)
            trades, _ = run_backtest(cfg, intraday, daily)
            m = metrics(trades)
            if m:
                rows.append((target, stop, m))
    return rows


def best_bracket(grid, min_trades=30, min_expectancy=2.0):
    """Highest win rate among brackets that still genuinely make money.
    A 71% win rate with ~0% expectancy is a coin flip dressed up — require
    a real edge per trade, not just a pretty win rate."""
    viable = [(t, s, m) for t, s, m in grid
              if m["expectancy_pct"] >= min_expectancy and m["trades"] >= min_trades]
    if not viable:
        return None
    return max(viable, key=lambda r: r[2]["win_rate"])


def main():
    REPORTS_DIR.mkdir(exist_ok=True)
    base_cfg = StrategyConfig()
    intraday, daily = load_data(base_cfg)
    trades, skipped = run_backtest(base_cfg, intraday, daily)
    calls = [t for t in trades if t.right == "C"]
    puts = [t for t in trades if t.right == "P"]

    lines = []
    add = lines.append
    add("# Backtest — derived momentum strategy")
    add("")
    add("**APPROXIMATED OPTIONS PRICING** — Black-Scholes on 20-day realized vol; "
        "no historical option chains were used because none are freely available. "
        "BS on realized vol typically UNDERPRICES 0DTE premium (no vol smile, no "
        "event premium), which inflates simulated returns. Treat percentages as "
        "optimistic. Slippage 1.5% each way + $1.30/contract/side included.")
    add("")
    add("**Window: ~60 trading days — the maximum free 5-minute history.** "
        "The strategy was derived on May 2026, which sits inside this window, so "
        "part of these results are in-sample. A 10-year backtest of this exact "
        "strategy is not possible: SPX daily 0DTE expirations have only existed "
        "since May 2022, and intraday options history requires a paid data feed.")
    add("")
    lines += fmt(metrics(trades), "All trades")
    lines += fmt(metrics(calls), "Calls (validated direction)")
    lines += fmt(metrics(puts), "Puts (mirror rule — NOT validated by the May study)")
    for ticker in {t.ticker for t in trades}:
        lines += fmt(metrics([t for t in trades if t.ticker == ticker]), f"{ticker} only")
    if skipped:
        add(f"({skipped} ticker-days skipped: not enough daily history for vol)")
        add("")

    add("## Exit bracket grid (calls only, single exit, no half leg)")
    add("")
    add("Tighter targets win more often but cap the winners — pick the row "
        "you actually want to trade. Same entries in every row.")
    add("")
    add("| Target | Stop | Win rate | Expectancy | Total P&L | Max DD | Trades |")
    add("|---|---|---|---|---|---|---|")
    grid = exit_grid(intraday, daily)
    for target, stop, m in sorted(grid, key=lambda r: -r[2]["win_rate"]):
        add(f"| +{target}% | {stop}% | **{m['win_rate']:.1f}%** "
            f"| {m['expectancy_pct']:+.1f}% | ${m['total_pnl']:,.0f} "
            f"| ${m['max_drawdown']:,.0f} | {m['trades']} |")
    add("")

    # scanner config: highest-win-rate bracket that still makes money,
    # with per-ticker/direction stats so each alert quotes its own number
    chosen = best_bracket(grid)
    per_setup = {}
    if chosen:
        target, stop, m_best = chosen
        cfg = StrategyConfig(direction="both")
        cfg.take_half_pct = None
        cfg.take_full_pct = float(target)
        cfg.stop_pct = float(stop)
        bt, _ = run_backtest(cfg, intraday, daily)
        for ticker in {t.ticker for t in bt}:
            for right, dirname in (("C", "call"), ("P", "put")):
                mm = metrics([t for t in bt if t.ticker == ticker and t.right == right])
                if mm:
                    per_setup[f"{ticker}:{dirname}"] = mm
        add(f"**Scanner bracket: +{target:g}% / {stop:g}%** "
            f"(win rate {m_best['win_rate']:.1f}%, expectancy "
            f"{m_best['expectancy_pct']:+.1f}%). Per-setup win rates "
            "(what each alert quotes):")
        add("")
        for key, mm in sorted(per_setup.items(), key=lambda kv: -kv[1]["win_rate"]):
            add(f"- {key}: **{mm['win_rate']:.1f}%** ({mm['trades']} trades, "
                f"expectancy {mm['expectancy_pct']:+.1f}%)")
        add("")

    out = REPORTS_DIR / "backtest.md"
    out.write_text("\n".join(lines), encoding="utf-8")

    log = pd.DataFrame([
        {
            "ticker": t.ticker, "right": t.right, "strike": t.strike,
            "entry_time": t.entry_time, "entry_premium": round(t.entry_premium, 2),
            "exits": "; ".join(f"{n}@{px:.2f} {lbl}" for _, px, n, lbl in t.exit_legs),
            "ret_pct": round(t.ret_pct, 1), "pnl": round(t.pnl, 2),
        }
        for t in sorted(trades, key=lambda t: t.entry_time)
    ])
    log.to_csv(REPORTS_DIR / "backtest_trades.csv", index=False)

    # machine-readable summary the scanner uses for alerts
    if chosen:
        target, stop, m_best = chosen
        (REPORTS_DIR / "backtest_results.json").write_text(json.dumps({
            "pricing": "approximated (Black-Scholes, realized vol)",
            "bracket": {"target_pct": target, "stop_pct": stop},
            "overall": m_best,
            "per_setup": per_setup,
        }, indent=2), encoding="utf-8")

    print(f"Wrote {out}")
    print(f"Wrote {REPORTS_DIR / 'backtest_trades.csv'}")
    print(f"Wrote {REPORTS_DIR / 'backtest_results.json'}")


if __name__ == "__main__":
    main()
