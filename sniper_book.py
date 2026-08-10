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
  a poll that shows both hit is scored the same way, so the live record can
  never read better than the backtest would have.
- positions never span sessions. Anything still open at SETTLE_ET is closed
  flat at the last seen price and marked 'session end', never a win.

Everything is guarded: a ledger fault must never break a scan or an alert.
"""

import json
from datetime import time as _time

import config

LEDGER = config.DATA_DIR / "sniper_positions.json"

# the backtest never carried a position overnight; 15:55 ET is the last 5m bar
# of the equity session. Forex ran to the end of the ET day there, so settling
# it here too is STRICTER than the measurement, never looser.
SETTLE_ET = _time(15, 55)


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


def open_trade(symbol: str, display: str, direction: str, entry: float,
               stop: float, target: float, day: str, time_et: str,
               decimals: int = 2) -> dict:
    """Track a fired sniper. Returns the stored row, or None if it was
    rejected (bad levels, or one already open on this symbol today)."""
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


def step(symbol: str, price: float, now_et=None) -> dict:
    """Mark one live price against the open trade on `symbol`.

    Returns the row if it just CLOSED (caller texts the exit), else None.
    Stop is tested before target, so a poll showing both scores a loss.
    """
    try:
        if price is None:
            return None
        price = float(price)
        rows = _read()
        row = next((r for r in rows
                    if r.get("state") == "open" and r.get("symbol") == symbol),
                   None)
        if row is None:
            return None
        buy = row["direction"] == "BUY"
        row["last_price"] = price
        _excursion(row, price)

        stop_hit = price <= row["stop"] if buy else price >= row["stop"]
        tgt_hit = price >= row["target"] if buy else price <= row["target"]
        settle = (now_et is not None
                  and getattr(now_et, "time", lambda: None)() is not None
                  and now_et.time() >= SETTLE_ET)

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
        _write(rows)
        return row
    except (TypeError, ValueError, KeyError):
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
