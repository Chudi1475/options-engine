"""Phase 1 — Webull options order history analyzer.

Parses a Webull options order export, FIFO-matches fills into round trips,
and writes a markdown report (reports/analysis.md) plus an equity curve PNG.

Usage:
    python analyze_history.py [path/to/orders.csv]
"""

import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

DEFAULT_CSV = Path(__file__).parent / "data" / "Webull_Orders_Records_Options.csv"
REPORTS_DIR = Path(__file__).parent / "reports"

OCC_RE = re.compile(r"^([A-Z]+?)(\d{6})([CP])(\d{8})$")
MULTIPLIER = 100

# Stated rules from the trading partner, checked against the data at the end.
STATED = {
    "win_rate": 0.52,
    "avg_win_pct": 152.0,
    "avg_loss_pct": -58.0,
    "round_trips": 272,
    "calls": 149,
    "puts": 14,
    "entry_window": (time(9, 30), time(10, 30)),
}


@dataclass
class Lot:
    qty: int
    price: float
    filled_time: datetime
    order_idx: int


@dataclass
class RoundTrip:
    symbol: str
    underlying: str
    expiry: datetime
    right: str  # C or P
    strike: float
    qty: int
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * MULTIPLIER * self.qty

    @property
    def ret_pct(self) -> float:
        return (self.exit_price - self.entry_price) / self.entry_price * 100

    @property
    def dte(self) -> int:
        return (self.expiry.date() - self.entry_time.date()).days

    @property
    def hold(self):
        return self.exit_time - self.entry_time


def parse_occ(symbol: str):
    m = OCC_RE.match(symbol.strip())
    if not m:
        return None
    root, ymd, right, strike = m.groups()
    expiry = datetime.strptime(ymd, "%y%m%d")
    return root, expiry, right, int(strike) / 1000


def parse_time(raw: str):
    if not isinstance(raw, str) or not raw.strip():
        return None
    # "05/29/2026 10:27:08 EDT" -> drop the tz abbreviation (all rows are ET)
    parts = raw.strip().rsplit(" ", 1)
    stamp = parts[0] if len(parts) == 2 and parts[1].isalpha() else raw.strip()
    return datetime.strptime(stamp, "%m/%d/%Y %H:%M:%S")


def load_orders(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df.columns = [c.strip() for c in df.columns]
    parsed = df["Symbol"].map(parse_occ)
    bad = df[parsed.isna()]
    if not bad.empty:
        print(f"WARNING: {len(bad)} rows with unparseable symbols skipped:")
        print(bad["Symbol"].tolist())
    df = df[parsed.notna()].copy()
    df[["underlying", "expiry", "right", "strike"]] = pd.DataFrame(
        parsed.dropna().tolist(), index=df.index
    )
    df["filled_qty"] = pd.to_numeric(df["Filled"], errors="coerce").fillna(0).astype(int)
    df["avg_price"] = pd.to_numeric(df["Avg Price"], errors="coerce")
    df["filled_time"] = df["Filled Time"].map(parse_time)
    df["placed_time"] = df["Placed Time"].map(parse_time)
    return df


def fifo_match(fills: pd.DataFrame):
    """FIFO-match buy fills to sell fills per contract symbol.

    Returns (round_trips, unmatched_sells, open_lots).
    Each matched (buy order, sell order) pairing is one round trip; a sell
    that consumes several buy lots produces several round trips, so the
    round-trip count can exceed the order count.
    """
    trips: list[RoundTrip] = []
    unmatched_sells = []
    open_lots = []

    for symbol, grp in fills.groupby("Symbol"):
        grp = grp.sort_values("filled_time")
        meta = grp.iloc[0]
        lots: deque[Lot] = deque()
        for idx, row in grp.iterrows():
            if row["Side"] == "Buy":
                lots.append(Lot(row["filled_qty"], row["avg_price"], row["filled_time"], idx))
                continue
            remaining = row["filled_qty"]
            while remaining > 0 and lots:
                lot = lots[0]
                take = min(lot.qty, remaining)
                trips.append(
                    RoundTrip(
                        symbol=symbol,
                        underlying=meta["underlying"],
                        expiry=meta["expiry"],
                        right=meta["right"],
                        strike=meta["strike"],
                        qty=take,
                        entry_price=lot.price,
                        exit_price=row["avg_price"],
                        entry_time=lot.filled_time,
                        exit_time=row["filled_time"],
                    )
                )
                lot.qty -= take
                remaining -= take
                if lot.qty == 0:
                    lots.popleft()
            if remaining > 0:
                unmatched_sells.append((symbol, remaining, row["avg_price"], row["filled_time"]))
        for lot in lots:
            open_lots.append((symbol, lot.qty, lot.price, lot.filled_time, meta["expiry"]))

    trips.sort(key=lambda t: t.exit_time)
    return trips, unmatched_sells, open_lots


def bucket_dte(d: int) -> str:
    if d <= 0:
        return "0 DTE"
    if d == 1:
        return "1 DTE"
    if d <= 5:
        return "2-5 DTE"
    if d <= 30:
        return "6-30 DTE"
    return ">30 DTE"


def time_bucket(t: datetime) -> str:
    minute = 0 if t.minute < 30 else 30
    start = t.replace(minute=minute, second=0)
    return start.strftime("%H:%M")


def fmt_money(x: float) -> str:
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def dist_table(counter: dict, total: int, headers=("Bucket", "Trades", "%")) -> list[str]:
    lines = [f"| {headers[0]} | {headers[1]} | {headers[2]} |", "|---|---|---|"]
    for key, n in counter.items():
        lines.append(f"| {key} | {n} | {n / total * 100:.1f}% |")
    return lines


def build_report(df, trips, unmatched_sells, open_lots, csv_path: Path) -> str:
    filled = df[(df["Status"] == "Filled") & (df["filled_qty"] > 0)]
    cancelled = df[df["Status"] != "Filled"]
    lines = []
    add = lines.append

    add("# Trade History Analysis — Webull Options Orders")
    add("")
    add(f"Source: `{csv_path.name}` — {len(df)} orders "
        f"({len(filled)} filled, {len(cancelled)} cancelled/other), "
        f"{filled['filled_time'].min():%m/%d/%Y} to {filled['filled_time'].max():%m/%d/%Y}.")
    add("")

    # --- calls vs puts (order level) ---
    rc = filled["right"].value_counts()
    add(f"Filled orders by type: **{rc.get('C', 0)} calls / {rc.get('P', 0)} puts**.")
    add("")

    # --- round trip stats ---
    n = len(trips)
    add("## Round trips (FIFO-matched)")
    add("")
    add(f"**{n} round trips** matched from filled orders. A round trip is one "
        "matched (buy order → sell order) lot pairing; one sell that closes "
        "several buys counts as several round trips.")
    add("")

    wins = [t for t in trips if t.pnl > 0]
    losses = [t for t in trips if t.pnl < 0]
    flat = n - len(wins) - len(losses)
    win_rate = len(wins) / n * 100
    avg_win = sum(t.ret_pct for t in wins) / len(wins)
    avg_loss = sum(t.ret_pct for t in losses) / len(losses)
    payoff = abs(avg_win / avg_loss)
    exp_pct = (len(wins) / n) * avg_win + (len(losses) / n) * avg_loss
    total_pnl = sum(t.pnl for t in trips)
    avg_pnl = total_pnl / n

    add(f"- Win rate: **{win_rate:.1f}%** ({len(wins)}W / {len(losses)}L / {flat} flat)")
    add(f"- Avg winner: **+{avg_win:.1f}%** | Avg loser: **{avg_loss:.1f}%** | "
        f"Payoff ratio: **{payoff:.2f}**")
    add(f"- Expectancy: **{exp_pct:+.1f}% per trade** ({fmt_money(avg_pnl)} avg P&L)")
    add(f"- Total realized P&L: **{fmt_money(total_pnl)}** "
        "(matched round trips only, before fees — Webull export has no fee column)")
    add("")

    # --- underlying distribution ---
    add("## Underlying distribution")
    add("")
    by_und = defaultdict(lambda: [0, 0.0])
    for t in trips:
        by_und[t.underlying][0] += 1
        by_und[t.underlying][1] += t.pnl
    add("| Underlying | Round trips | % | Realized P&L |")
    add("|---|---|---|---|")
    for und, (cnt, pnl) in sorted(by_und.items(), key=lambda kv: -kv[1][0]):
        add(f"| {und} | {cnt} | {cnt / n * 100:.1f}% | {fmt_money(pnl)} |")
    add("")

    # --- DTE distribution ---
    add("## DTE at entry")
    add("")
    dte_counts = defaultdict(int)
    for t in trips:
        dte_counts[bucket_dte(t.dte)] += 1
    order = ["0 DTE", "1 DTE", "2-5 DTE", "6-30 DTE", ">30 DTE"]
    lines += dist_table({k: dte_counts[k] for k in order if k in dte_counts}, n,
                        ("DTE", "Round trips", "%"))
    add("")

    # --- time of day ---
    add("## Entry time of day (30-min buckets, ET)")
    add("")
    ent = defaultdict(int)
    for t in trips:
        ent[time_bucket(t.entry_time)] += 1
    lines += dist_table(dict(sorted(ent.items())), n, ("Entry bucket", "Round trips", "%"))
    add("")
    add("## Exit time of day (30-min buckets, ET)")
    add("")
    ext = defaultdict(int)
    for t in trips:
        ext[time_bucket(t.exit_time)] += 1
    lines += dist_table(dict(sorted(ext.items())), n, ("Exit bucket", "Round trips", "%"))
    add("")

    # --- hold times ---
    add("## Hold times")
    add("")
    holds = sorted(t.hold for t in trips)
    same_day = sum(1 for t in trips if t.entry_time.date() == t.exit_time.date())
    median_hold = holds[len(holds) // 2]
    add(f"- Same-day round trips: **{same_day}/{n} ({same_day / n * 100:.1f}%)**")
    add(f"- Median hold: **{median_hold}** | Shortest: {holds[0]} | Longest: {holds[-1]}")
    add("")

    # --- equity curve ---
    add("## Equity curve")
    add("")
    add("![equity curve](equity_curve.png)")
    add("")
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for t in trips:
        cum += t.pnl
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    add(f"Max drawdown (realized, peak-to-trough): **{fmt_money(-max_dd)}**")
    add("")

    # --- best / worst ---
    add("## Best and worst trades")
    add("")
    add("| | Symbol | Qty | Entry → Exit | Return | P&L | Exit date |")
    add("|---|---|---|---|---|---|---|")
    by_pnl = sorted(trips, key=lambda t: t.pnl)
    for label, t in [("Best", x) for x in reversed(by_pnl[-5:])] + [("Worst", x) for x in by_pnl[:5]]:
        add(f"| {label} | {t.symbol} | {t.qty} | {t.entry_price:.2f} → {t.exit_price:.2f} "
            f"| {t.ret_pct:+.1f}% | {fmt_money(t.pnl)} | {t.exit_time:%m/%d/%y} |")
    add("")

    # --- unmatched ---
    add("## Unmatched fills (excluded from stats above)")
    add("")
    if unmatched_sells:
        add(f"**{len(unmatched_sells)} sell fill(s) with no prior buy** in this export "
            "(likely positions opened before the export window):")
        for sym, qty, px, ts in unmatched_sells:
            add(f"- {sym}: sold {qty} @ {px:.2f} on {ts:%m/%d/%y %H:%M}")
        add("")
    if open_lots:
        add(f"**{len(open_lots)} buy lot(s) never sold in this export.** Those past expiry "
            "either expired worthless or settled — the export doesn't say, so they are "
            "**not** counted as wins or losses. If they expired worthless, true P&L is "
            "lower than reported above:")
        exposure = 0.0
        for sym, qty, px, ts, exp in open_lots:
            status = "expired" if exp.date() < filled["filled_time"].max().date() else "may still be open"
            cost = px * MULTIPLIER * qty
            exposure += cost
            add(f"- {sym}: {qty} @ {px:.2f} bought {ts:%m/%d/%y} ({status}, cost {fmt_money(cost)})")
        add(f"\nTotal cost of never-sold lots: **{fmt_money(exposure)}** — worst case, "
            "all of this is additional loss.")
        add("")
    if not unmatched_sells and not open_lots:
        add("None — every filled buy was matched to a filled sell.")
        add("")

    # --- stated rules vs data ---
    add("## Stated rules vs. the data")
    add("")
    checks = []
    checks.append(
        f"- Stated **149 calls / 14 puts** → data shows **{rc.get('C', 0)} calls / "
        f"{rc.get('P', 0)} puts** (filled orders)."
    )
    checks.append(
        f"- Stated **272 round trips** → data shows **{n}** FIFO-matched round trips."
    )
    checks.append(
        f"- Stated **~52% win rate** → data shows **{win_rate:.1f}%**."
    )
    checks.append(
        f"- Stated **avg winner +152% / avg loser -58%** → data shows "
        f"**{avg_win:+.1f}% / {avg_loss:.1f}%**."
    )
    in_window = sum(
        1 for t in trips
        if STATED["entry_window"][0] <= t.entry_time.time() <= STATED["entry_window"][1]
    )
    checks.append(
        f"- Stated entries concentrated **9:30-10:30 ET** → "
        f"**{in_window / n * 100:.1f}%** of entries fall in that window."
    )
    checks.append(
        f"- Stated most positions closed same morning → **{same_day / n * 100:.1f}%** "
        "closed same day."
    )
    lines += checks
    add("")
    add("Numbers above are computed from this export only. Any stated figure that "
        "doesn't match was measured on data not present in this file.")
    add("")
    return "\n".join(lines)


def plot_equity(trips, out_path: Path):
    times = [t.exit_time for t in trips]
    cum = []
    total = 0.0
    for t in trips:
        total += t.pnl
        cum.append(total)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(times, cum, linewidth=1.5)
    ax.axhline(0, color="gray", linewidth=0.8, linestyle="--")
    ax.set_title("Realized P&L — cumulative (matched round trips)")
    ax.set_ylabel("P&L ($)")
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main():
    csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path}")
    REPORTS_DIR.mkdir(exist_ok=True)

    df = load_orders(csv_path)
    fills = df[(df["Status"] == "Filled") & (df["filled_qty"] > 0) & df["avg_price"].notna()]
    trips, unmatched_sells, open_lots = fifo_match(fills)
    if not trips:
        sys.exit("No round trips could be matched — check the CSV format.")

    plot_equity(trips, REPORTS_DIR / "equity_curve.png")
    report = build_report(df, trips, unmatched_sells, open_lots, csv_path)
    out = REPORTS_DIR / "analysis.md"
    out.write_text(report, encoding="utf-8")

    # trade log for later phases
    log = pd.DataFrame(
        [
            {
                "symbol": t.symbol, "underlying": t.underlying, "right": t.right,
                "strike": t.strike, "expiry": t.expiry.date(), "qty": t.qty,
                "entry_time": t.entry_time, "exit_time": t.exit_time,
                "entry_price": t.entry_price, "exit_price": t.exit_price,
                "dte": t.dte, "ret_pct": round(t.ret_pct, 2), "pnl": round(t.pnl, 2),
            }
            for t in trips
        ]
    )
    log.to_csv(REPORTS_DIR / "round_trips.csv", index=False)
    print(f"Wrote {out}")
    print(f"Wrote {REPORTS_DIR / 'equity_curve.png'}")
    print(f"Wrote {REPORTS_DIR / 'round_trips.csv'}")


if __name__ == "__main__":
    main()
