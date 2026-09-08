"""Forward evidence ledger for the FVG sniper: the bot's self-teaching memory.

Every sniper candidate that forms live gets recorded here with the exact
ticket (entry/stop) and what EVERY target tier would pay: the verified 0.4R,
1R, 2R, and the structural liquidity target. At the end of each day the
outcomes are filled in from the day's bars. Over weeks this builds the
honest, walk-forward dataset that either EARNS bigger targets a win-rate
claim or proves they don't pay, and it tracks the round-6 "sibling" config
(gap >= 1.1 ATR and entries >= 08:00 ET, 81.1% on 53 OOS trades) toward its
pre-committed promotion bar: >= 60 forward trades AND a Wilson 95% lower
bound above the 71.4% breakeven for 0.4R all-out.

Nothing here changes live behavior. It watches, records, grades, and tells
the owner when the evidence clears the bar. Fully guarded: a ledger hiccup
must never break a read or an alert.
"""

import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import config

ET = ZoneInfo("America/New_York")
LEDGER = config.DATA_DIR / "sniper_forward.jsonl"

# pre-committed promotion rule for the round-6 sibling (edge_lessons round 6):
SIBLING_MIN_TRADES = 60
BREAKEVEN_04R = 71.4          # percent needed for 0.4R all-out to break even
SIBLING_GAP_MIN_ATR = 1.1
SIBLING_MIN_HOUR_ET = 8


def wilson_lb(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson 95% lower bound on a win rate, in percent. 0 when n == 0."""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return 100.0 * (centre - margin) / denom


def _read_all() -> list:
    try:
        if not LEDGER.exists():
            return []
        out = []
        for line in LEDGER.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out
    except OSError:
        return []


def _write_all(records: list):
    try:
        tmp = LEDGER.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                       encoding="utf-8")
        tmp.replace(LEDGER)
    except OSError:
        pass


def event_id(day, symbol, direction, time_et, entry, stop) -> str:
    """A stable identity for one candidate observation.

    Derived from the event's own facts so that a retry after a crash or a
    duplicated read produces the SAME id and collapses, while a genuinely
    different look later the same morning produces a different one. This is
    what lets an alert, its forward observation and its graded outcome be
    linked to each other instead of being matched by guesswork on the day."""
    import hashlib
    raw = f"{day}|{symbol}|{direction}|{time_et}|{entry}|{stop}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def record_candidate(symbol: str, direction: str, price: float, atr: float,
                     ticket: dict, conf: dict, passes: bool, reasons: list,
                     gap_atr=None, hour_et=None, now_et: datetime = None):
    """Log one live sniper candidate. Called from the read path; must be fast
    and can never raise.

    Deduplication is on the EVENT, not on the day. The old rule returned on any
    existing row for (date, symbol, direction), so the first candidate of the
    day won forever: a 09:35 near miss permanently suppressed the 09:50 alert
    that actually fired, and that real entry then had no forward observation to
    reconcile against. Retries of one event still collapse, because the id is
    derived from the event's own facts."""
    try:
        if not ticket or not direction:
            return
        now = now_et or datetime.now(ET)
        day = f"{now:%Y-%m-%d}"
        stamp = f"{now:%H:%M:%S}"
        eid = event_id(day, symbol, direction, stamp,
                       ticket.get("entry", price), ticket.get("stop"))
        existing = _read_all()
        for r in existing:
            if r.get("event_id") == eid:
                return  # a retry of THIS event, not a new one
        if passes:
            # At most one ACCEPTED entry per symbol+direction+day, which is the
            # validated one-trade-per-symbol-per-day shape. Rejects are never
            # suppressed: they are the opportunity denominator.
            for r in existing:
                if (r.get("date") == day and r.get("symbol") == symbol
                        and r.get("direction") == direction and r.get("passes")):
                    return
        entry = float(ticket.get("entry", price))
        stop = float(ticket.get("stop", 0))
        risk = abs(entry - stop)
        if risk <= 0:
            return
        sign = 1 if direction == "BUY" else -1
        liq = ticket.get("target_liquidity") or (conf or {}).get(
            "target_liquidity")
        rec = {
            "event_id": eid,          # stable identity, links alert -> outcome
            "date": day, "time_et": stamp,
            "symbol": symbol, "direction": direction,
            "entry": round(entry, 6), "stop": round(stop, 6),
            "risk": round(risk, 6), "atr": round(float(atr or 0), 6),
            "targets": {
                "t04": round(entry + sign * 0.4 * risk, 6),
                "t1": round(entry + sign * 1.0 * risk, 6),
                "t2": round(entry + sign * 2.0 * risk, 6),
                "liq": round(float(liq), 6) if liq else None,
            },
            "grade": (conf or {}).get("grade"),
            "score": (conf or {}).get("score"),
            "passes": bool(passes),
            "reasons": list(reasons or [])[:6],
            "gap_atr": round(float(gap_atr), 3) if gap_atr is not None else None,
            "hour_et": int(hour_et) if hour_et is not None else now.hour,
            "sibling": bool(
                passes
                and (gap_atr is None or gap_atr >= SIBLING_GAP_MIN_ATR)
                and (hour_et if hour_et is not None else now.hour)
                >= SIBLING_MIN_HOUR_ET),
            "outcome": None,  # filled by fill_outcomes() after the close
        }
        with LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _walk_outcome(rec: dict, bars) -> dict:
    """Walk the day's bars after entry time: which level hit first, per tier.
    Same-bar stop+target = stop first (the honest, conservative call the
    round-4 artifact hunt taught us). Returns the outcome dict."""
    sign = 1 if rec["direction"] == "BUY" else -1
    entry, stop = rec["entry"], rec["stop"]
    risk = rec["risk"]
    tiers = {k: v for k, v in rec["targets"].items() if v is not None}
    hit = {k: None for k in tiers}   # True=win, False=stopped before target
    stopped = False
    mfe_r = 0.0
    for _, b in bars.iterrows():
        hi, lo = float(b["High"]), float(b["Low"])
        best = (hi - entry) if sign > 0 else (entry - lo)
        mfe_r = max(mfe_r, best / risk)
        stop_hit = lo <= stop if sign > 0 else hi >= stop
        for k, tgt in tiers.items():
            if hit[k] is not None:
                continue
            tgt_hit = hi >= tgt if sign > 0 else lo <= tgt
            if stop_hit:          # conservative: stop wins any tie
                hit[k] = False
            elif tgt_hit:
                hit[k] = True
        if stop_hit:
            stopped = True
            break
        if all(v is not None for v in hit.values()):
            break
    # anything not resolved by the close: mark by where price ended vs entry
    return {"stopped": stopped, "mfe_r": round(mfe_r, 3),
            "hit": hit, "graded_at": f"{datetime.now(ET):%Y-%m-%d %H:%M}"}


def fill_outcomes() -> int:
    """EOD: grade every ungraded candidate from today's (or any past) bars.
    Returns how many records were graded."""
    records = _read_all()
    todo = [r for r in records if r.get("outcome") is None]
    if not todo:
        return 0
    try:
        import pandas as pd  # noqa: F401
        import yfinance as yf
    except ImportError:
        return 0
    graded = 0
    by_symbol = {}
    for r in todo:
        by_symbol.setdefault(r["symbol"], []).append(r)
    for symbol, recs in by_symbol.items():
        try:
            df = yf.download(symbol, period="5d", interval="5m",
                             prepost=True, progress=False, auto_adjust=False)
            if df is None or df.empty:
                continue
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
            if getattr(df.index, "tz", None) is not None:
                df.index = df.index.tz_convert(ET)
        except Exception:
            continue
        for r in recs:
            try:
                start = datetime.strptime(
                    f"{r['date']} {r['time_et']}", "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=ET)
                day_end = start.replace(hour=23, minute=59)
                bars = df[(df.index > start) & (df.index <= day_end)]
                if bars.empty:
                    # signal day has no bars yet (e.g. grading same evening
                    # on a delayed feed): leave for the next pass
                    continue
                r["outcome"] = _walk_outcome(r, bars)
                graded += 1
            except Exception:
                continue
    if graded:
        _write_all(records)
    return graded


def scoreboard() -> dict:
    """Aggregate the forward record per tier + the sibling promotion check."""
    records = [r for r in _read_all()
               if r.get("outcome") and r.get("passes")]
    tiers = {"t04": "0.4R (the live, verified target)",
             "t1": "1R (needs >50 of 100 to beat 0.4R)",
             "t2": "2R (needs >33 of 100 to beat 0.4R)",
             "liq": "structure target (the big one)"}
    out = {"n": len(records), "tiers": {}, "sibling": None}
    for k, label in tiers.items():
        graded = [r for r in records
                  if r["outcome"]["hit"].get(k) is not None]
        wins = sum(1 for r in graded if r["outcome"]["hit"][k])
        n = len(graded)
        out["tiers"][k] = {
            "label": label, "n": n, "wins": wins,
            "win_pct": round(100 * wins / n, 1) if n else None,
            "wilson_lb": round(wilson_lb(wins, n), 1) if n else None,
        }
    sib = [r for r in records if r.get("sibling")]
    sib_graded = [r for r in sib if r["outcome"]["hit"].get("t04") is not None]
    wins = sum(1 for r in sib_graded if r["outcome"]["hit"]["t04"])
    n = len(sib_graded)
    lb = wilson_lb(wins, n)
    out["sibling"] = {
        "n": n, "wins": wins,
        "win_pct": round(100 * wins / n, 1) if n else None,
        "wilson_lb": round(lb, 1),
        "bar": {"min_trades": SIBLING_MIN_TRADES,
                "wilson_lb_needed": BREAKEVEN_04R},
        "promote": n >= SIBLING_MIN_TRADES and lb > BREAKEVEN_04R,
    }
    return out


def nightly_summary() -> str:
    """One short plain-language block for the nightly digest. Empty string
    when there is nothing new to say."""
    sb = scoreboard()
    if not sb["n"]:
        return ""
    lines = [f"Sniper forward record: {sb['n']} live signals graded so far."]
    t = sb["tiers"]
    for k in ("t04", "t1", "t2", "liq"):
        s = t[k]
        if s["n"]:
            lines.append(f"- {s['label']}: wins {s['wins']} of {s['n']}"
                         f" ({s['win_pct']:.0f} of 100)")
    sib = sb["sibling"]
    if sib and sib["n"]:
        need = SIBLING_MIN_TRADES - sib["n"]
        if sib["promote"]:
            lines.append(
                f"PROMOTION READY: the stricter sibling config hit "
                f"{sib['wins']} of {sib['n']} with a safety-adjusted floor of "
                f"{sib['wilson_lb']:.0f} of 100 (bar: {BREAKEVEN_04R:.0f}). "
                "Tell Chudi to flip it live.")
        else:
            lines.append(
                f"Sibling config (stricter entries): {sib['wins']} of "
                f"{sib['n']} so far; needs {max(need, 0)} more trades and a "
                f"{BREAKEVEN_04R:.0f}+ safety floor before it can take over.")
    return "\n".join(lines)
