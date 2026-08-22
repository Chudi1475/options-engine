"""StrategySpec: the one object every TEXT surface reads its numbers from.

Why this module exists
----------------------
The live exit rules used to be hand-typed into the cards, the AI prompts, the
nightly digest header and the README. They drifted. The stop read "-70%" in
three separate places long after config.py had moved to -90%, the entry card
described an old "+15% target" shadow bracket the bot had stopped using, and
the sniper record was retyped as "79% / 133" in five files instead of being
read from fvg.SNIPER_MEASURED. A reader had no way to tell which number was
live and which was stale.

This module is the single read path, so one edit in config.py or one new
report file shows up on every surface at once.

The rule this module enforces
-----------------------------
It contains NO trading literal. Every number is read from:

    config.py                  live tunables (exits, risk sizing, gates)
    strategy.py / live_params  entry window, momentum, watchlist, allow-list
    fvg.py                     the SNIPER gate constants and measured record
    reports/*.json             measured stats, each carrying its source path

A number that cannot be traced to one of those does not belong on a card, in
a prompt, in a digest, or in a doc. `test_no_hardcoded_stats.py` enforces
that on the surfaces listed in SURFACES below.

Nothing here changes trading behavior. It only changes where text reads from.
Report files are read defensively: a missing or corrupt report makes the
matching helper return an empty string, so a surface goes quiet rather than
quoting a number that no longer has a source.
"""

import json
from datetime import time as _time
import re
from dataclasses import dataclass

import config
import fvg
import live_params
import positions as poslib

# ---------------------------------------------------------------------------
# Report files. These are PATHS, not numbers: every stat below is read out of
# one of them at runtime, never retyped.
# ---------------------------------------------------------------------------
REPORT_OLD_RULES = "reports/backtest_results.json"
REPORT_NEW_RULES = "reports/backtest_new_rules.json"
# the live sniper record: the round-6 winner re-scored under the live session
# floor (a measurement of a fixed config, see rescore_round6_session.py).
# The un-floored source report stays as the fallback.
REPORT_SNIPER = "reports/chart_backtest_round6_session.json"
REPORT_SNIPER_SOURCE = "reports/chart_backtest_round6.json"
REPORT_REGIME = "bt_exp_regime_split.json"

# The text surfaces that must not hand-type a stat. Kept here so the guard
# test and this module can never disagree about what is covered.
SURFACES = ("cards.py", "assistant.py", "learn.py", "coach.py", "recap.py",
            "scoreboard.py", "charts.py", "market_tools.py", "scanner.py",
            "forward_ledger.py", "README.md", "GUIDE.txt")

_REPORT_CACHE = {}


def _read_report(rel: str):
    """Parsed report dict, or None. Cached on mtime, never raises."""
    path = config.REPO_DIR / rel
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    hit = _REPORT_CACHE.get(rel)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    _REPORT_CACHE[rel] = (stamp, data)
    return data


def _num(value):
    """A finite number, or None. Guards a hand-corrupted report leg."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value == value and value not in (float("inf"),
                                                     float("-inf")) else None


def _pct(value) -> str:
    """Render a percent the way the cards already do (no trailing zeros)."""
    return f"{value:g}%"


@dataclass(frozen=True)
class Measured:
    """A stat that came out of a report file, carrying its provenance.

    Falsy when the report was missing or unreadable, so a caller can drop the
    claim instead of printing a number with no source behind it.
    """

    win_rate: float = None
    trades: int = None
    source: str = ""
    basis: str = ""
    wins: int = None

    def __bool__(self) -> bool:
        return self.win_rate is not None

    def of_100(self) -> str:
        """The 'N of 100' phrasing the cards use. '' when unknown."""
        if not self:
            return ""
        return f"{self.win_rate:.0f} of 100"

    def record(self) -> str:
        """Win rate over trade count and basis. '' when unknown."""
        if not self:
            return ""
        txt = f"{self.win_rate:g}%"
        if self.trades:
            txt += f" over {self.trades} {self.basis or 'trades'}"
        return txt


@dataclass(frozen=True)
class StrategySpec:
    """Every live rule and measured record, resolved. Build with `get()`."""

    # --- exits (config.py) ---
    tp_half_pct: float
    stop_pct: float
    runner_giveback_pct: float
    expiry_warn_minutes: int
    # --- risk sizing (config.py) ---
    risk_per_trade_pct: float
    correlated_risk_pct: float
    full_alloc_pct: float
    correlated_alloc_pct: float
    spread_cost_pct: float
    # --- the alert gate (config.py) ---
    min_winrate: float
    gap_up_skip_pct: float
    live_stats_min_total: int
    live_stats_min_setup: int
    # --- entry (strategy.py + live_params.json) ---
    entry_window: str
    mom_bars: int
    mom_minutes: int
    watchlist: dict
    allowed_setups: frozenset
    # --- the old-rules shadow bracket (report-backed) ---
    old_target_pct: float
    old_stop_pct: float
    old_bracket_source: str
    # --- the SNIPER chart pattern (fvg.py + its report) ---
    sniper_symbols: frozenset
    sniper_min_gap_atr: float
    sniper_max_gap_atr: float
    sniper_open_et: _time
    sniper_rth_symbols: frozenset
    sniper_max_run_atr: float
    sniper_max_day_eff: float
    sniper_max_risk_atr: float
    sniper_stop_buf_atr: float
    sniper_tp_r: float
    sniper: Measured
    # --- other measured records ---
    gap_up_measured: Measured
    gap_other_measured: Measured

    # ---------------------------------------------------------------- text --
    # These are what surfaces call. Wording matches what already shipped; only
    # the numbers moved from the string into this module.

    def half_txt(self) -> str:
        return f"+{self.tp_half_pct:g}%"

    def stop_txt(self) -> str:
        return _pct(self.stop_pct)

    def giveback_txt(self) -> str:
        return f"{self.runner_giveback_pct:g} points"

    def exit_plan_sentence(self) -> str:
        """The live exit rules as one sentence. The canonical phrasing."""
        return (f"sell half at {self.half_txt()}, then let the runner run and "
                f"sell it when it gives back {self.giveback_txt()} from its "
                f"peak, hard stop {self.stop_txt()}")

    def exit_plan_short(self) -> str:
        """The compact form used in digest headers and prompt rule lists."""
        return (f"half at {self.half_txt()}, give-back "
                f"{self.runner_giveback_pct:g} off peak, "
                f"{self.stop_txt()} stop")

    def giveback_example(self) -> str:
        """A worked example of the give-back trail, derived from the rule so it
        stays sensible if the rule moves."""
        peak = self.runner_giveback_pct * 1.5
        return f"example: +{peak:g}% falling to +{peak - self.runner_giveback_pct:g}%"

    def floor_txt(self) -> str:
        return f"{self.min_winrate:g}%"

    def floor_sentence(self) -> str:
        return (f"wins at least {self.min_winrate:g} of 100 in testing")

    def old_bracket_txt(self) -> str:
        """The shadow bracket actually in force, as 'target / stop' text."""
        return (f"+{self.old_target_pct:g}% target / "
                f"{self.old_stop_pct:g}% stop")

    def entry_window_txt(self) -> str:
        return self.entry_window

    def momentum_txt(self) -> str:
        return f"{self.mom_minutes}-minute momentum"

    def sniper_record_txt(self) -> str:
        """The measured record as 'N% win rate over M out-of-sample
        walk-forward replays'. '' when the report is missing, so the claim
        drops instead of going stale."""
        if not self.sniper:
            return ""
        return (f"{self.sniper.win_rate:g}% win rate over "
                f"{self.sniper.trades} {self.sniper.basis or 'replays'}")

    def sniper_card_txt(self) -> str:
        """The entry-card claim: 'won 40 of 48 out-of-sample replays (83 of
        100)'. The rate is always shown next to ITS OWN count. '' with no
        report."""
        if not self.sniper:
            return ""
        if self.sniper.wins is not None and self.sniper.trades:
            return (f"hit target {self.sniper.wins} of {self.sniper.trades} "
                    f"in out-of-sample testing ({self.sniper.of_100()})")
        return (f"{self.sniper.of_100()} over {self.sniper.trades} "
                f"{self.sniper.basis or 'replays'}")

    def sniper_measured_dict(self) -> dict:
        """The record as the JSON-safe dict carried on a sniper ticket."""
        if not self.sniper:
            return {}
        return {"win_rate": self.sniper.win_rate, "trades": self.sniper.trades,
                "wins": self.sniper.wins, "basis": self.sniper.basis,
                "source": self.sniper.source}

    def sniper_open_ct_txt(self) -> str:
        """The sniper entry floor as CT wall-clock text ('8:50 AM CT')."""
        from datetime import datetime as _dt, timedelta as _td
        t = (_dt(2000, 1, 3, self.sniper_open_et.hour,
                 self.sniper_open_et.minute) - _td(hours=1))
        return t.strftime("%I:%M %p").lstrip("0") + " CT"

    def sniper_window_txt(self) -> str:
        """'8:50 AM CT to the close, weekdays': when a sniper can fire."""
        return f"{self.sniper_open_ct_txt()} to the close, weekdays"

    def sniper_watch_sentence(self) -> str:
        """The morning-card line: what the sniper watches and from when."""
        names = sorted(self.sniper_symbol_names())
        listed = ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 \
            else (names[0] if names else "")
        return (f"Sniper chart pattern: watching {listed} from "
                f"{self.sniper_open_ct_txt()} to the close. Nothing fires "
                "before that.")

    def sniper_symbol_names(self) -> list:
        """Human names for the verified symbols (Yahoo codes stay internal)."""
        names = {"EURUSD=X": "EUR/USD", "JPY=X": "USD/JPY", "^GSPC": "SPX"}
        return [names.get(s, s) for s in self.sniper_symbols]

    def sniper_short_txt(self) -> str:
        """'83% verified' for chart titles (whole number: a chart chip is not
        the place for a decimal). '' with no report."""
        return f"{self.sniper.win_rate:.0f}% verified" if self.sniper else ""

    def sniper_replays_txt(self) -> str:
        """The compact 'N% on M out-of-sample replays' chart caption. '' with
        no report."""
        if not self.sniper:
            return ""
        return (f"{self.sniper.win_rate:.0f}% on {self.sniper.trades} "
                "out-of-sample replays")

    def sniper_tp_txt(self) -> str:
        return f"{self.sniper_tp_r:g}R"

    def breakeven_at(self, target_r: float):
        """Win rate a target of `target_r` needs to beat a 1R-risk trade.
        Derived, never typed: 1R risk against an nR target breaks even at
        1/(1+n)."""
        return None if target_r <= 0 else 100.0 / (1.0 + target_r)

    def sniper_breakeven(self):
        return self.breakeven_at(self.sniper_tp_r)

    def gap_skip_sentence(self) -> str:
        """Why the bot stands aside on a big gap up, with both measured sides.
        Degrades to the qualitative claim if the regime report is missing."""
        base = (f"Big gap-up days lose money for this playbook")
        if self.gap_up_measured and self.gap_other_measured:
            return (base + f" (won just {self.gap_up_measured.of_100()} in "
                    f"testing vs {self.gap_other_measured.of_100()} on every "
                    "other day)")
        return base

    def sizing_sentence(self) -> str:
        return (f"sized so a full stop-out costs "
                f"{self.risk_per_trade_pct:g}% of the account "
                f"(about {self.full_alloc_pct:.2f}% of it per trade)")

    # ---------------------------------------------------------------- audit --

    def drift_warnings(self) -> list:
        """Where a report's own declared rules disagree with what runs live.

        This is the check that would have caught the -70 vs -90 stop: the
        committed new-rules report still declares the stop it was generated
        under, so if config.py has moved on, every EV and win rate quoted from
        that report describes a bracket the bot no longer trades.
        """
        out = []
        rep = _read_report(REPORT_NEW_RULES) or {}
        rules = rep.get("rules") if isinstance(rep.get("rules"), dict) else {}
        for key, live, name in (
                ("tp_half_pct", self.tp_half_pct, "take-half"),
                ("stop_pct", self.stop_pct, "stop"),
                ("runner_giveback_pct", self.runner_giveback_pct,
                 "runner give-back")):
            was = _num(rules.get(key))
            if was is not None and was != live:
                out.append(
                    f"{REPORT_NEW_RULES} was generated with {name} {was:g} but "
                    f"config.py now runs {live:g}: every stat quoted from that "
                    "report describes a bracket the bot no longer trades")
        # the sniper record must describe the session floor the gate runs
        snip = _read_report(REPORT_SNIPER) or {}
        floor = snip.get("session_open_et") if isinstance(snip, dict) else None
        live_floor = self.sniper_open_et.strftime("%H:%M")
        if floor and floor != live_floor:
            out.append(
                f"{REPORT_SNIPER} was scored with the session floor {floor} ET "
                f"but fvg.py now opens at {live_floor} ET: the sniper record on "
                "the cards describes a window the gate no longer runs")
        return out


def _timeframe_minutes(cfg) -> int:
    """Bar length in minutes, parsed from cfg.timeframe ('5m')."""
    match = re.match(r"(\d+)", str(getattr(cfg, "timeframe", "")))
    return int(match.group(1)) if match else 0


def _measured_from_setup(report: dict, path: str, basis: str) -> Measured:
    over = report.get("overall") if isinstance(report, dict) else None
    if not isinstance(over, dict):
        return Measured(source=path, basis=basis)
    return Measured(win_rate=_num(over.get("win_rate")),
                    trades=_num(over.get("trades")), source=path, basis=basis)


def _sniper_measured() -> Measured:
    """The SNIPER record, read from its report: the OUT-OF-SAMPLE pair (rate
    with its own count) of the session re-score. Falls back to the un-floored
    round-6 report's OOS leg, then to fvg.SNIPER_MEASURED, then to an empty
    record so the claim drops instead of going stale."""
    basis = "out-of-sample replays"
    rep = _read_report(REPORT_SNIPER) or {}
    oos = rep.get("oos") if isinstance(rep, dict) else None
    if isinstance(oos, dict) and _num(oos.get("win_rate_pct")) is not None:
        return Measured(win_rate=_num(oos.get("win_rate_pct")),
                        trades=_num(oos.get("trades")),
                        wins=_num(oos.get("wins")),
                        source=REPORT_SNIPER, basis=basis)
    src = _read_report(REPORT_SNIPER_SOURCE) or {}
    oos = (src.get("best") or {}).get("oos") if isinstance(src, dict) else None
    if isinstance(oos, dict) and _num(oos.get("win_rate_pct")) is not None:
        return Measured(win_rate=_num(oos.get("win_rate_pct")),
                        trades=_num(oos.get("trades")),
                        wins=_num(oos.get("wins")),
                        source=REPORT_SNIPER_SOURCE, basis=basis)
    raw = getattr(fvg, "SNIPER_MEASURED", None)
    if not isinstance(raw, dict):
        return Measured(source=REPORT_SNIPER, basis=basis)
    return Measured(win_rate=_num(raw.get("win_rate")),
                    trades=_num(raw.get("trades")),
                    wins=_num(raw.get("wins")),
                    source="fvg.SNIPER_MEASURED (report missing)",
                    basis=str(raw.get("basis") or basis))


def _regime_measured(bucket: str) -> Measured:
    """One gap bucket out of the regime-split study."""
    rep = _read_report(REPORT_REGIME) or {}
    tables = rep.get("regime_tables_pooled")
    row = (tables or {}).get("gap_bucket", {}).get(bucket) \
        if isinstance(tables, dict) else None
    if not isinstance(row, dict):
        return Measured(source=REPORT_REGIME, basis="trades")
    return Measured(win_rate=_num(row.get("win_rate")),
                    trades=_num(row.get("trades")),
                    source=REPORT_REGIME, basis="trades")


def _old_bracket():
    """The old-rules shadow bracket the scanner actually pins at entry: the
    report's bracket when valid, else the same built-in default the scanner
    falls back to. Read from the same place so the card text can never
    describe a bracket the shadow is not being judged under."""
    rep = _read_report(REPORT_OLD_RULES) or {}
    loaded = rep.get("bracket")
    if poslib.valid_bracket(loaded):
        return (_num(loaded.get("target_pct")), _num(loaded.get("stop_pct")),
                REPORT_OLD_RULES)
    fallback = poslib.DEFAULT_OLD_BRACKET
    return (fallback["target_pct"], fallback["stop_pct"],
            "positions.DEFAULT_OLD_BRACKET (report bracket unreadable)")


def get() -> StrategySpec:
    """Build the spec from the live settings and the reports, right now.

    Cheap enough to call per card: config is already in memory and the report
    files are mtime-cached. Built fresh each call so a /reload of
    live_params.json or a redeployed report is picked up without a restart.
    """
    cfg, allowed = live_params.effective()
    old_target, old_stop, old_src = _old_bracket()
    return StrategySpec(
        tp_half_pct=config.TP_HALF_PCT,
        stop_pct=config.STOP_PCT,
        runner_giveback_pct=config.RUNNER_GIVEBACK_PCT,
        expiry_warn_minutes=config.EXPIRY_WARN_MINUTES,
        risk_per_trade_pct=config.RISK_PER_TRADE_PCT,
        correlated_risk_pct=config.CORRELATED_RISK_PCT,
        full_alloc_pct=config.suggested_alloc_pct(config.RISK_PER_TRADE_PCT),
        correlated_alloc_pct=config.suggested_alloc_pct(
            config.CORRELATED_RISK_PCT),
        spread_cost_pct=config.SPREAD_COST_PCT,
        min_winrate=config.MIN_WINRATE,
        gap_up_skip_pct=config.GAP_UP_SKIP_PCT,
        live_stats_min_total=config.LIVE_STATS_MIN_TOTAL,
        live_stats_min_setup=config.LIVE_STATS_MIN_SETUP,
        entry_window=live_params.window_et(cfg),
        mom_bars=cfg.mom_bars,
        mom_minutes=cfg.mom_bars * _timeframe_minutes(cfg),
        watchlist=dict(cfg.watchlist),
        allowed_setups=frozenset(allowed),
        old_target_pct=old_target,
        old_stop_pct=old_stop,
        old_bracket_source=old_src,
        sniper_symbols=frozenset(fvg.SNIPER_SYMBOLS),
        sniper_min_gap_atr=fvg._SNIPER_MIN_GAP_ATR,
        sniper_max_gap_atr=fvg._SNIPER_MAX_GAP_ATR,
        sniper_open_et=fvg._SNIPER_OPEN_ET,
        sniper_rth_symbols=frozenset(fvg.SNIPER_RTH_SYMBOLS),
        sniper_max_run_atr=fvg._SNIPER_MAX_RUN_ATR,
        sniper_max_day_eff=fvg._SNIPER_MAX_DAY_EFF,
        sniper_max_risk_atr=fvg._SNIPER_MAX_RISK_ATR,
        sniper_stop_buf_atr=fvg._SNIPER_STOP_BUF_ATR,
        sniper_tp_r=fvg._SNIPER_TP_R,
        sniper=_sniper_measured(),
        gap_up_measured=_regime_measured("gap up >1%"),
        gap_other_measured=_regime_measured("no big gap"),
    )


if __name__ == "__main__":  # `python strategy_spec.py` prints what is live
    s = get()
    print("EXIT PLAN     :", s.exit_plan_sentence())
    print("EXIT SHORT    :", s.exit_plan_short())
    print("GIVEBACK EG   :", s.giveback_example())
    print("ENTRY WINDOW  :", s.entry_window_txt(), "ET  /", s.momentum_txt())
    print("GATE          :", s.floor_sentence())
    print("SIZING        :", s.sizing_sentence())
    print("OLD SHADOW    :", s.old_bracket_txt(), f"[{s.old_bracket_source}]")
    print("SNIPER        :", s.sniper_record_txt() or "(no report)",
          f"[{s.sniper.source}]")
    print("SNIPER TP     :", s.sniper_tp_txt(),
          f"breakeven {s.sniper_breakeven():.1f} of 100")
    print("GAP SKIP      :", s.gap_skip_sentence())
    print("ALLOW-LIST    :", ", ".join(sorted(s.allowed_setups)))
    for w in s.drift_warnings():
        print("DRIFT WARNING :", w)
