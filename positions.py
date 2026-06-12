"""Open-position tracking. Every alert becomes a tracked position that
survives restarts (positions.json). This module is pure logic — no Telegram,
no data fetching — so it can be tested offline. scanner.py feeds it prices;
it answers with events to alert on.

Exit system (constants in config.py):
- SELL HALF when the option is +TP_HALF_PCT over entry mid
- then trail the remaining half: exit when the same 15-min momentum measure
  used for entry flips against the trade
- hard stop: sell everything at STOP_PCT from entry mid, any time
- "close before expiry" warning EXPIRY_WARN_MINUTES before the 4 PM ET close

Each position also runs a shadow simulation of the OLD rules (+15% / -60%)
on the exact same price stream, so the weekly scoreboard can compare the two
honestly. A position closed by the new rules keeps getting marked until its
old-rules shadow also closes — otherwise the comparison would be rigged.
"""

import json
import time as time_mod
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time

import config

CLOSE_T = time(16, 0)
_warn = 16 * 60 - int(config.EXPIRY_WARN_MINUTES)
WARN_T = time(_warn // 60, _warn % 60)


@dataclass
class Position:
    id: str
    date: str                  # YYYY-MM-DD opened (ET)
    time_et: str               # HH:MM:SS opened
    ticker: str
    direction: str             # call | put
    right: str                 # C | P
    strike: float
    expiry: str                # YYYY-MM-DD
    entry_mid: float
    entry_source: str          # "quote" | "estimate"
    entry_bid: float = 0.0
    entry_ask: float = 0.0
    est_entry: float = 0.0     # model price at entry — the estimate-based P&L
                               # baseline, so estimates compare model-to-model
                               # (a BS estimate vs a real quote mid is apples
                               # to oranges and would fire false stops)
    spot_at_signal: float = 0.0
    mom_pct: float = 0.0
    risk_pct: float = 1.0
    correlated: bool = False
    paper: bool = False
    risk_mode: str = "green"
    stats_note: str = ""
    win_rate_quoted: float = 0.0
    ev_quoted: float = 0.0
    state: str = "open"        # open | half_sold | closed
    half_exit: dict = None     # {time, pct, mark}
    final_exit: dict = None    # {time, pct, mark, reason} — pct is the remaining leg
    final_pnl_pct: float = None  # weighted whole-position result
    mfe_pct: float = 0.0       # max favorable excursion (best % seen)
    mae_pct: float = 0.0       # max adverse excursion (worst % seen)
    last_mark: float = None
    last_mark_pct: float = None
    last_mark_source: str = ""
    last_mark_time: str = ""
    expiry_warned: bool = False
    old_rules: dict = field(default_factory=lambda: {
        "status": "open", "exit_pct": None, "exit_reason": None, "exit_time": None})

    def pct_of(self, mark: float) -> float:
        return (mark / self.entry_mid - 1) * 100 if self.entry_mid else 0.0

    def weighted_final(self, remaining_pct: float) -> float:
        """Whole-position % result: half banked at the half-exit, half at the end."""
        if self.half_exit:
            return round(0.5 * self.half_exit["pct"] + 0.5 * remaining_pct, 2)
        return round(remaining_pct, 2)

    def expires_on(self) -> date:
        return date.fromisoformat(self.expiry)

    def setup_key(self) -> str:
        return f"{self.ticker}:{self.direction}"


def step(pos: Position, now: datetime, mark: float, mark_source: str,
         est_pct, flipped: bool, old_bracket: dict) -> list:
    """Advance one polling cycle. Returns a list of event dicts to alert on.

    mark      — best current option price (quote mid, else estimate)
    est_pct   — estimate-based P&L%, used as an early-warning floor for the
                stop because real quotes can lag ~15 min. None if unavailable.
    flipped   — has the 15-min momentum measure turned against the trade?
    """
    events = []
    ts = now.strftime("%H:%M:%S")
    pct = round(pos.pct_of(mark), 2)

    if pos.state != "closed":
        pos.last_mark, pos.last_mark_pct = mark, pct
        pos.last_mark_source, pos.last_mark_time = mark_source, ts
        pos.mfe_pct = max(pos.mfe_pct, pct)
        pos.mae_pct = min(pos.mae_pct, pct)

        stop_trigger = min(pct, est_pct) if est_pct is not None else pct
        if stop_trigger <= config.STOP_PCT:
            pos.final_exit = {"time": ts, "pct": pct, "mark": mark, "reason": "stop"}
            pos.final_pnl_pct = pos.weighted_final(pct)
            pos.state = "closed"
            events.append({"type": "stop", "pct": pct, "source": mark_source})
        elif pos.state == "open" and pct >= config.TP_HALF_PCT:
            pos.half_exit = {"time": ts, "pct": pct, "mark": mark}
            pos.state = "half_sold"
            events.append({"type": "sell_half", "pct": pct, "source": mark_source})
        elif pos.state == "half_sold" and flipped:
            pos.final_exit = {"time": ts, "pct": pct, "mark": mark,
                              "reason": "momentum flip"}
            pos.final_pnl_pct = pos.weighted_final(pct)
            pos.state = "closed"
            events.append({"type": "momentum_flip", "pct": pct,
                           "total_pct": pos.final_pnl_pct, "source": mark_source})

    # old-rules shadow on the same prices (for the honest weekly comparison)
    o = pos.old_rules
    if o["status"] == "open":
        if pct <= old_bracket["stop_pct"]:
            o.update(status="closed", exit_pct=pct, exit_reason="old stop", exit_time=ts)
        elif pct >= old_bracket["target_pct"]:
            o.update(status="closed", exit_pct=pct, exit_reason="old target", exit_time=ts)

    expires_today = pos.expires_on() == now.date()
    if (expires_today and pos.state != "closed" and not pos.expiry_warned
            and now.time() >= WARN_T):
        pos.expiry_warned = True
        events.append({"type": "expiry_warn", "pct": pct, "source": mark_source})

    if expires_today and now.time() >= CLOSE_T:
        if pos.state != "closed":
            pos.final_exit = {"time": ts, "pct": pct, "mark": mark,
                              "reason": "expiry close"}
            pos.final_pnl_pct = pos.weighted_final(pct)
            pos.state = "closed"
        if o["status"] == "open":
            o.update(status="closed", exit_pct=pct,
                     exit_reason="old time stop", exit_time=ts)
    return events


class PositionBook:
    def __init__(self, path=None):
        self.path = path or config.POSITIONS_FILE
        self.positions = []
        self.load()

    def load(self):
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError):
                raw = []
            self.positions = [Position(**p) for p in raw]

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(p) for p in self.positions], indent=1),
                       encoding="utf-8")
        try:
            tmp.replace(self.path)
        except PermissionError:  # another process briefly reading the file
            time_mod.sleep(0.2)
            tmp.replace(self.path)

    def add(self, pos: Position):
        self.positions.append(pos)
        self.save()

    def needs_monitoring(self, today: date) -> list:
        """Positions that still need price updates: open ones, plus closed
        ones whose old-rules shadow is still open. Positions past expiry get
        force-settled instead (bot was offline at their close)."""
        out = []
        for p in self.positions:
            if p.state == "closed" and p.old_rules["status"] != "open":
                continue
            if p.expires_on() < today:
                self._force_expire(p)
                continue
            out.append(p)
        return out

    def _force_expire(self, p: Position):
        """Settle a position whose expiry passed while the bot was offline,
        using the last price it ever saw (labeled as such)."""
        ts = "16:00:00"
        last_pct = p.last_mark_pct if p.last_mark_pct is not None else 0.0
        if p.state != "closed":
            p.final_exit = {"time": ts, "pct": last_pct, "mark": p.last_mark,
                            "reason": "expired (bot offline at close; last known price)"}
            p.final_pnl_pct = p.weighted_final(last_pct)
            p.state = "closed"
        if p.old_rules["status"] == "open":
            p.old_rules.update(status="closed", exit_pct=last_pct,
                               exit_reason="old time stop (last known)", exit_time=ts)
        self.save()

    def open_same_direction(self, direction: str) -> bool:
        return any(p.state != "closed" and p.direction == direction
                   for p in self.positions)

    def opened_today(self, today: date) -> set:
        return {p.ticker for p in self.positions if p.date == str(today)}

    def closed(self) -> list:
        return [p for p in self.positions if p.final_pnl_pct is not None]

    def for_date(self, day) -> list:
        return [p for p in self.positions if p.date == str(day)]
