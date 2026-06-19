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
import os
import threading
import time as time_mod
from dataclasses import asdict, dataclass, field, fields
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
    mfe_pct: float = None      # max favorable excursion (best % seen); None until
                               # the first mark, so a pure-loser reports its real
                               # (negative) peak instead of a fake 0%
    mae_pct: float = None      # max adverse excursion (worst % seen)
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
         est_pct, flipped: bool, old_bracket: dict, comparable: bool = True) -> list:
    """Advance one polling cycle. Returns a list of event dicts to alert on.

    mark       — best current option price (quote mid, else estimate)
    est_pct    — estimate-based P&L%, used as an early-warning floor for the
                 stop because real quotes can lag ~15 min. None if unavailable.
    flipped    — has the 15-min momentum measure turned against the trade?
    comparable — is `mark` priced the same WAY as entry (quote-vs-quote or
                 est-vs-est)? When False (a real quote on an estimate entry, or
                 only an estimate on a quote entry) the mark/entry ratio is
                 apples-to-oranges, so the ONLY trustworthy signal is the
                 model-to-model est_pct: it still drives the stop and the expiry
                 events, but it must NOT trip the +25% half off a fake number.
    """
    events = []
    ts = now.strftime("%H:%M:%S")
    pct = round(pos.pct_of(mark), 2)
    # effective P&L% for decisions: the mark ratio when comparable, else the
    # model-to-model estimate (fall back to the mark only if no estimate exists)
    eff = pct if comparable else (est_pct if est_pct is not None else pct)

    if pos.state != "closed":
        pos.last_mark = mark
        pos.last_mark_pct = eff
        pos.last_mark_source, pos.last_mark_time = mark_source, ts
        pos.mfe_pct = eff if pos.mfe_pct is None else max(pos.mfe_pct, eff)
        pos.mae_pct = eff if pos.mae_pct is None else min(pos.mae_pct, eff)

        stop_trigger = min(eff, est_pct) if est_pct is not None else eff
        if stop_trigger <= config.STOP_PCT:
            pos.final_exit = {"time": ts, "pct": eff, "mark": mark, "reason": "stop"}
            pos.final_pnl_pct = pos.weighted_final(eff)
            pos.state = "closed"
            events.append({"type": "stop", "pct": eff, "source": mark_source})
        elif pos.state == "open" and comparable and pct >= config.TP_HALF_PCT:
            pos.half_exit = {"time": ts, "pct": pct, "mark": mark}
            pos.state = "half_sold"
            events.append({"type": "sell_half", "pct": pct, "source": mark_source})
        elif pos.state == "half_sold" and flipped:
            pos.final_exit = {"time": ts, "pct": eff, "mark": mark,
                              "reason": "momentum flip"}
            pos.final_pnl_pct = pos.weighted_final(eff)
            pos.state = "closed"
            events.append({"type": "momentum_flip", "pct": eff,
                           "total_pct": pos.final_pnl_pct, "source": mark_source})

    # old-rules shadow on the same prices (for the honest weekly comparison).
    # Only advance it on a comparable mark — a cross-source ratio would rig it.
    o = pos.old_rules
    if comparable and o["status"] == "open":
        if pct <= old_bracket["stop_pct"]:
            o.update(status="closed", exit_pct=pct, exit_reason="old stop", exit_time=ts)
        elif pct >= old_bracket["target_pct"]:
            o.update(status="closed", exit_pct=pct, exit_reason="old target", exit_time=ts)

    expires_today = pos.expires_on() == now.date()
    if (expires_today and pos.state != "closed" and not pos.expiry_warned
            and now.time() >= WARN_T):
        pos.expiry_warned = True
        events.append({"type": "expiry_warn", "pct": eff, "source": mark_source})

    if expires_today and now.time() >= CLOSE_T:
        if pos.state != "closed":
            pos.final_exit = {"time": ts, "pct": eff, "mark": mark,
                              "reason": "expiry close"}
            pos.final_pnl_pct = pos.weighted_final(eff)
            pos.state = "closed"
        if o["status"] == "open":
            o.update(status="closed", exit_pct=(pct if comparable else eff),
                     exit_reason="old time stop", exit_time=ts)
    return events


class PositionBook:
    def __init__(self, path=None):
        self.path = path or config.POSITIONS_FILE
        self.positions = []
        self.load()

    def load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            raw = []
        # parse each record on its own and filter unknown keys, so ONE bad /
        # legacy / schema-evolved record can never crash the whole bot on boot
        # and silently drop every live position from monitoring
        allowed = {f.name for f in fields(Position)}
        out = []
        for p in raw:
            try:
                out.append(Position(**{k: v for k, v in p.items() if k in allowed}))
            except (TypeError, ValueError, AttributeError) as e:
                rid = p.get("id") if isinstance(p, dict) else repr(p)
                print(f"skipping unloadable position record {rid}: {e}")
        self.positions = out

    def save(self):
        # unique temp per pid/thread (mirrors config.save_state) so two writers
        # never clobber one shared tmp -> torn JSON or FileNotFoundError
        tmp = self.path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps([asdict(p) for p in self.positions], indent=1),
                           encoding="utf-8")
            try:
                tmp.replace(self.path)
            except (PermissionError, FileNotFoundError):  # mid-read / volume blip
                time_mod.sleep(0.2)
                try:
                    tmp.replace(self.path)
                except (PermissionError, FileNotFoundError) as e:
                    print(f"PositionBook.save: could NOT persist positions.json: {e}")
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

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
        using the last price it ever saw (labeled as such). If it was NEVER
        marked, do NOT invent a 0% result — that fabricates a breakeven/loss on
        the scoreboard (honesty rule). Settle it 'not graded' and leave
        final_pnl_pct None so closed()/live_stats exclude it."""
        ts = "16:00:00"
        if p.state != "closed":
            if p.last_mark_pct is None:  # no price ever seen -> ungraded
                p.final_exit = {
                    "time": ts, "pct": None,
                    "mark": p.last_mark if p.last_mark is not None else p.entry_mid,
                    "reason": "expired untracked (no price ever seen; not graded)"}
                p.final_pnl_pct = None
            else:
                p.final_exit = {"time": ts, "pct": p.last_mark_pct, "mark": p.last_mark,
                                "reason": "expired (bot offline at close; last known price)"}
                p.final_pnl_pct = p.weighted_final(p.last_mark_pct)
            p.state = "closed"
        if p.old_rules["status"] == "open":
            last_pct = p.last_mark_pct if p.last_mark_pct is not None else 0.0
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
