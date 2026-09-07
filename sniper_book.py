"""Live exit tracking for SNIPER (FVG) alerts.

Why this is its own module and not a PositionBook
-------------------------------------------------
positions.PositionBook models an OPTIONS trade: strike, expiry, entry premium,
sell-half at +25%, a give-back runner trail, a -90% premium stop. A sniper is a
different animal: a SPOT trade on the underlying with one all-out target at
0.4R and a single stop beyond the FVG far edge. Forcing it into Position would
mean every sniper landed in scoreboard.live_stats alongside the 0DTE book and
silently corrupted the momentum win rate the entry gate quotes.

So this is a separate, deliberately small ledger with its own file.

What it fixes
-------------
Until now the sniper fired one Telegram card with an entry, a stop and a target
and then went permanently silent. Nothing tracked the trade, nothing texted an
exit, nothing recorded whether it won. The bot promoted the sniper as its
verified pattern while being structurally unable to observe a single live
outcome.

Rules, matched to the config the 79% was measured under (chart_backtest_round6)
------------------------------------------------------------------------------
- one all-out target at 0.4R, no runner, no scaling
- stop beyond the FVG far edge (the alert supplies both levels; this module
  never recomputes them, so the tracked trade is exactly the ticket that was
  texted)
- STOP WINS A TIE. The backtest scored any bar spanning both levels as a loss;
  a bar (or poll) that shows both hit is scored the same way, so the live
  record can never read better than the backtest would have.
- EXITS ARE GRADED ON COMPLETED 5-MINUTE BARS, the way the backtest walked
  them (backtest_chart_v4.simulate_mkt: from the fill bar on, stop first on
  each bar's low/high, then target). step() takes the recent bars the read
  carried and walks every bar since the entry bar that it has not walked
  yet. A polled last price is only the fallback when no bars came along, so
  a touch that happened and retraced between two polls is no longer missed,
  and a "hit" means the bar really traded through the level.
- positions never span sessions. Anything still open at SETTLE_ET is closed
  flat at the last seen price and marked 'session end', never a win.

Everything is guarded: a ledger fault must never break a scan or an alert.
"""

import json
from datetime import datetime, time as _time
from zoneinfo import ZoneInfo

import config
import market_calendar

LEDGER = config.DATA_DIR / "sniper_positions.json"
ET = ZoneInfo("America/New_York")

# the backtest never carried a position overnight: it graded the 15:55 bar
# (the last of the equity session) and closed whatever was left at that bar's
# close. Settling once the 16:00 ET close has passed does the same thing live:
# the 15:55 bar completes at 16:00, gets walked, and the remainder closes flat
# at the last price. Forex ran to the end of the ET day in the backtest, so
# settling it here too is STRICTER than the measurement, never looser.
SETTLE_ET = _time(16, 0)


def _settle_at(now_et) -> _time:
    """The settle clock for the day a position is being stepped on. Normally
    SETTLE_ET, but 13:00 ET on a half day: on the Friday after Thanksgiving the
    tape stops at 13:00 and the 16:00 rule would hold a trade open for three
    hours of bars that never print, then settle it against a last price that
    went stale at lunch. Returns SETTLE_ET for anything it cannot date."""
    try:
        return market_calendar.session_close(now_et.date())
    except (AttributeError, TypeError, ValueError):
        return SETTLE_ET


def _read() -> list:
    try:
        if not LEDGER.exists():
            return []
        data = json.loads(LEDGER.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []


def _write(rows: list) -> None:
    """Atomic publish, same discipline as config.save_state: a torn write here
    would lose an open trade's stop."""
    try:
        tmp = LEDGER.with_suffix(f".{id(rows)}.tmp")
        tmp.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        try:
            tmp.replace(LEDGER)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    except OSError as e:
        print(f"sniper_book: could NOT persist {LEDGER.name}: {e}")


def _bar_floor(ts: datetime) -> datetime:
    """The 5-minute bar a timestamp falls in (its open time)."""
    return ts.replace(minute=ts.minute - ts.minute % 5, second=0, microsecond=0)


def _parse_ts(value):
    """ISO text -> aware datetime, or None. Tolerates pandas' 'T'-less form."""
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("T", " "))
    except (TypeError, ValueError):
        return None


def _row_clock(day: str, time_et: str):
    """The row's own ET clock from its date + time_et fields, or None."""
    try:
        return datetime.fromisoformat(f"{day} {time_et}").replace(tzinfo=ET)
    except (TypeError, ValueError):
        return None


def open_trade(symbol: str, display: str, direction: str, entry: float,
               stop: float, target: float, day: str, time_et: str,
               decimals: int = 2, entry_ts=None) -> dict:
    """Track a fired sniper. Returns the stored row, or None if it was
    rejected (bad levels, or one already open on this symbol today).
    `entry_ts` (aware ET datetime or ISO text) pins the entry BAR so the
    bar-walk in step() starts from the fill bar, like the backtest."""
    try:
        if direction not in ("BUY", "SELL"):
            return None
        entry, stop, target = float(entry), float(stop), float(target)
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        # the ticket must point the right way for its direction, or the
        # stop/target test below would be inverted for the whole trade
        if direction == "BUY" and not (stop < entry < target):
            return None
        if direction == "SELL" and not (target < entry < stop):
            return None
        rows = _read()
        for r in rows:
            if r.get("state") == "open" and r.get("symbol") == symbol:
                return None  # one live sniper per symbol, as measured
        ets = _parse_ts(entry_ts) or _row_clock(day, time_et)
        row = {
            "id": f"{day}-{time_et.replace(':', '')}-{symbol}-{direction}",
            "date": day, "time_et": time_et,
            "symbol": symbol, "display": display, "direction": direction,
            "entry": entry, "stop": stop, "target": target,
            "risk": risk, "decimals": int(decimals),
            "state": "open", "last_price": entry,
            "mfe_r": 0.0, "mae_r": 0.0,
            "exit_price": None, "exit_reason": None, "exit_time": None,
            "r": None,
            # bar-walk bookkeeping: the fill bar, and the last bar graded
            "entry_bar_ts": _bar_floor(ets).isoformat() if ets else None,
            "last_bar_ts": None,
            "exit_via": None,  # 'bar' (completed 5m bar) or 'poll' (live print)
        }
        rows.append(row)
        _write(rows)
        return row
    except (TypeError, ValueError):
        return None


def _excursion(row: dict, price: float) -> None:
    """Track best/worst in R so a stopped trade still records how close it got."""
    sign = 1.0 if row["direction"] == "BUY" else -1.0
    move = (price - row["entry"]) * sign
    r = move / row["risk"] if row["risk"] else 0.0
    row["mfe_r"] = round(max(row.get("mfe_r") or 0.0, r), 3)
    row["mae_r"] = round(min(row.get("mae_r") or 0.0, r), 3)


def _walk_bars(row: dict, bars) -> str:
    """Grade every completed bar since the fill bar that has not been graded
    yet, in order. `bars` rows are [iso_ts, open, high, low, close]. Returns
    'stop' / 'target' when a bar closed the trade (row already updated with
    exit_price, exit_time and r), else ''. Stop first on every bar: a bar
    spanning both levels is a loss, exactly as the backtest scored it."""
    buy = row["direction"] == "BUY"
    start = _parse_ts(row.get("entry_bar_ts"))
    if start is None:  # a row written before entry_bar_ts existed
        clock = _row_clock(row.get("date"), row.get("time_et"))
        start = _bar_floor(clock) if clock else None
    if start is None:
        return ""      # no fill bar to anchor on: never grade blind
    last = _parse_ts(row.get("last_bar_ts"))
    sign = 1.0 if buy else -1.0
    for b in bars or []:
        try:
            ts = _parse_ts(b[0])
            hi, lo = float(b[2]), float(b[3])
        except (TypeError, ValueError, IndexError):
            continue
        if ts is None or ts < start:
            continue
        if ts.date() != start.date():
            continue   # never a bar from another session
        if last is not None and ts <= last:
            continue
        last = ts
        row["last_bar_ts"] = ts.isoformat()
        row["last_price"] = float(b[4]) if len(b) > 4 else row.get("last_price")
        # excursions from the bar's extremes, like the backtest's MFE
        _excursion(row, hi)
        _excursion(row, lo)
        stop_hit = lo <= row["stop"] if buy else hi >= row["stop"]
        tgt_hit = hi >= row["target"] if buy else lo <= row["target"]
        if stop_hit:                      # checked FIRST: ties are losses
            row.update(state="closed", exit_price=row["stop"],
                       exit_reason="stop", r=-1.0,
                       exit_time=f"{ts:%H:%M:%S}", exit_via="bar")
            return "stop"
        if tgt_hit:
            row.update(state="closed", exit_price=row["target"],
                       exit_reason="target",
                       r=round((row["target"] - row["entry"]) * sign
                               / row["risk"], 3),
                       exit_time=f"{ts:%H:%M:%S}", exit_via="bar")
            return "target"
    return ""


def step(symbol: str, price: float, now_et=None, bars=None) -> dict:
    """Mark the open trade on `symbol` against the completed bars the read
    carried (`bars`: rows of [iso_ts, open, high, low, close], oldest first)
    and then against the live `price`.

    Returns the row if it just CLOSED (caller texts the exit), else None.
    Stop is tested before target on every bar and on the poll, so anything
    showing both scores a loss.
    """
    try:
        rows = _read()
        row = next((r for r in rows
                    if r.get("state") == "open" and r.get("symbol") == symbol),
                   None)
        if row is None:
            return None
        buy = row["direction"] == "BUY"
        # A row left over from an earlier session is never graded against a
        # new day's bars or prints: it closes flat at the last price it saw,
        # marked stale, so no fabricated stop or target ever gets texted.
        try:
            today = str(now_et.date()) if now_et is not None else None
        except AttributeError:
            today = None
        if today and row.get("date") and row["date"] != today:
            sign = 1.0 if buy else -1.0
            last = float(row.get("last_price") or row["entry"])
            row.update(state="closed", exit_price=last,
                       exit_reason="session end",
                       r=round((last - row["entry"]) * sign / row["risk"], 3),
                       exit_time=f"{now_et:%H:%M:%S}", exit_via="stale")
            _write(rows)
            return row
        if bars and _walk_bars(row, bars):
            _write(rows)
            return row
        if price is None:
            if bars:
                _write(rows)              # persist the walked bars + excursion
            return None
        price = float(price)
        row["last_price"] = price
        _excursion(row, price)

        stop_hit = price <= row["stop"] if buy else price >= row["stop"]
        tgt_hit = price >= row["target"] if buy else price <= row["target"]
        settle = (now_et is not None
                  and getattr(now_et, "time", lambda: None)() is not None
                  and now_et.time() >= _settle_at(now_et))

        if stop_hit:                      # checked FIRST: ties are losses
            row.update(state="closed", exit_price=row["stop"],
                       exit_reason="stop", r=-1.0)
        elif tgt_hit:
            sign = 1.0 if buy else -1.0
            row.update(state="closed", exit_price=row["target"],
                       exit_reason="target",
                       r=round((row["target"] - row["entry"]) * sign
                               / row["risk"], 3))
        elif settle:
            sign = 1.0 if buy else -1.0
            row.update(state="closed", exit_price=price,
                       exit_reason="session end",
                       r=round((price - row["entry"]) * sign / row["risk"], 3))
        else:
            _write(rows)                  # persist the excursion, stay open
            return None

        row["exit_time"] = (f"{now_et:%H:%M:%S}" if now_et is not None else "")
        row["exit_via"] = "poll"
        _write(rows)
        return row
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def has_open(symbol: str) -> bool:
    return any(r.get("state") == "open" and r.get("symbol") == symbol
               for r in _read())


def open_rows() -> list:
    return [r for r in _read() if r.get("state") == "open"]


def record() -> dict:
    """The live sniper record. Separate from scoreboard.live_stats on purpose:
    these are spot R multiples, not option premium percentages, and the two
    must never be pooled."""
    closed = [r for r in _read() if r.get("state") == "closed"
              and isinstance(r.get("r"), (int, float))]
    wins = [r for r in closed if r["exit_reason"] == "target"]
    losses = [r for r in closed if r["exit_reason"] == "stop"]
    flats = [r for r in closed if r["exit_reason"] == "session end"]
    n = len(closed)
    total_r = round(sum(r["r"] for r in closed), 3)
    return {
        "n": n, "wins": len(wins), "losses": len(losses), "flats": len(flats),
        "win_pct": round(100.0 * len(wins) / n, 1) if n else None,
        "total_r": total_r,
        "avg_r": round(total_r / n, 3) if n else None,
    }
