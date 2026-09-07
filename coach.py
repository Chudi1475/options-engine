"""The coach: a second agent whose only job is teaching the bot to improve.

Every night the coach gets the full dossier of the bot's day: every trade
taken, every sniper candidate (fired, near-miss, and why), the gap-skip
decision, the breaking news that hit the wires, and the bot's own graded
outcomes. For EVERY imperfection it reconstructs the perfect scenario
(what the ideal bot would have done with the same information), explains
exactly why this bot fell short, names the root cause, and proposes the
smallest concrete change that would have captured perfection, while
calling out hindsight bias when the "fix" wouldn't generalize.

Proposals are tracked across nights in coach_proposals.jsonl. A one-off
idea is noise; the SAME proposal recurring on different days is signal,
and at 3 sightings the coach escalates it to the owner as ready to test.
Everything the coach concludes feeds the same lessons digest the chat
brain reads before every reply, which is how the bot teaches itself.

The coach runs on the bot's normal Claude path (assistant.deep_think), so
it respects the 5h05m usage-limit countdown automatically and never
executes code. Guardrail unchanged: parameter changes are proposed and
escalated, never silently applied; the frozen sniper constants stay
frozen without a new verified backtest round.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import config

ET = ZoneInfo("America/New_York")
REVIEWS = config.DATA_DIR / "coach_reviews.jsonl"
PROPOSALS = config.DATA_DIR / "coach_proposals.jsonl"
ESCALATE_AT = 3  # same proposal seen on this many different days -> escalate

COACH_SYSTEM = """You are the COACH: an elite, brutally honest trading-systems
reviewer. You are not the bot; you are the agent that teaches the bot. You
will get one trading day's full dossier from an options-alert bot: trades it
alerted, verified-pattern (sniper) candidates it fired or rejected with the
gate reasons, the day-skip decision, breaking news, and graded outcomes.

Your job, for EVERY imperfection in the day:
1. PERFECT SCENARIO: with the same information available at that moment, what
   would the perfect version of this bot have done? Be exact: prices, times,
   which rule fires.
2. WHY IT FELL SHORT: the precise mechanism, not a vibe. Data lag? A gate too
   tight or too loose? An exit rule? News it saw but did not weigh? Latency?
3. ROOT CAUSE: one of: data, entry_gate, exit_rule, sizing, news, latency,
   luck, none (if the bot actually played it perfectly and just lost, SAY SO;
   losing on a good process is not a mistake).
4. PROPOSAL: the smallest concrete, testable change that captures the miss
   (name the exact knob or rule), or null when the honest answer is "change
   nothing".
5. HINDSIGHT RISK: low/medium/high. Would this change have helped across many
   days, or only this one in the rear-view mirror? Overfit "fixes" hurt.

Hard honesty rules: never invent prices or events not in the dossier. Never
propose touching the frozen verified sniper constants without a new backtest
round; route those as backtest_request instead. A day with zero real
imperfections is a valid answer. A trade marked "paper": true was practice
mode (tracked, no money moved): learn from it, but weigh it lighter than a
live trade and never present a practice outcome as a live-money result.

Reply with STRICT JSON only:
{"imperfections": [{"what": str, "perfect_scenario": str, "why_fell_short":
str, "root_cause": str, "proposal": str|null, "proposal_key": str|null,
"hindsight_risk": "low"|"medium"|"high"}], "day_summary": str,
"top_lesson": str}
proposal_key = a short stable slug (e.g. "widen_sniper_hours") so the same
idea on different days gets the same key; null when proposal is null."""


def _jl_read(path) -> list:
    try:
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out
    except OSError:
        return []


def _jl_append(path, obj):
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj) + "\n")
    except OSError:
        pass


def gather_dossier(session_date) -> dict:
    """Everything the bot knew and did today, in one reviewable blob."""
    day = str(session_date)
    dossier = {"date": day, "trades": [], "sniper_candidates": [],
               "day_skip": None, "news": [], "live_knobs": {}}
    try:
        import learn
        record = learn.grade_day(session_date)
        dossier["trades"] = [
            {"ticker": t["ticker"], "direction": t["direction"],
             "verdict": t["verdict"], "story": t["story"],
             "paper": bool(t.get("paper")),
             "features": t.get("features"), "outcome": t.get("outcome")}
            for t in record.get("trades", [])]
        dossier["news"] = learn._breaking_news_for(day)
    except Exception as e:
        dossier["gather_error_trades"] = str(e)[:200]
    try:
        import forward_ledger
        dossier["sniper_candidates"] = [
            r for r in forward_ledger._read_all() if r.get("date") == day]
    except Exception as e:
        dossier["gather_error_ledger"] = str(e)[:200]
    try:
        skip_told = config.state_get("gap_up_skip_date", None)
        dossier["day_skip"] = ("skipped: SPX gap-up rule"
                               if skip_told == day else None)
    except Exception:
        pass
    try:
        dossier["live_knobs"] = {
            "TP_HALF_PCT": config.TP_HALF_PCT, "STOP_PCT": config.STOP_PCT,
            "RUNNER_GIVEBACK_PCT": config.RUNNER_GIVEBACK_PCT,
            "GAP_UP_SKIP_PCT": config.GAP_UP_SKIP_PCT,
            "MIN_WINRATE": config.MIN_WINRATE,
        }
    except Exception:
        pass
    return dossier


def _track_proposals(session_date, imperfections) -> list:
    """Count recurring proposal_keys across days; return escalation lines."""
    day = str(session_date)
    existing = _jl_read(PROPOSALS)
    by_key = {}
    for p in existing:
        by_key.setdefault(p.get("key"), []).append(p)
    escalations = []
    for imp in imperfections:
        key = imp.get("proposal_key")
        if not key or not imp.get("proposal"):
            continue
        prior = by_key.get(key, [])
        if any(p.get("date") == day for p in prior):
            continue  # one sighting per day
        _jl_append(PROPOSALS, {
            "date": day, "key": key, "proposal": imp["proposal"],
            "root_cause": imp.get("root_cause"),
            "hindsight_risk": imp.get("hindsight_risk"),
            "sightings": len(prior) + 1})
        if len(prior) + 1 == ESCALATE_AT:
            escalations.append(
                f"'{imp['proposal']}' has now come up on {ESCALATE_AT} "
                "different days. That is a pattern, not hindsight. Worth a "
                "real backtest round.")
    return escalations


def reflect(session_date) -> dict:
    """The nightly deep reflection. Returns {"summary": str} for the digest
    (empty summary when there was nothing to review or the brain is paused)."""
    try:
        import assistant
        if not assistant.enabled():
            return {"summary": ""}
        if assistant.cooldown_left_s() > 0:
            return {"summary": "Coach session deferred (brain is in its "
                                "usage-limit countdown). Tomorrow night "
                                "covers both days."}
        dossier = gather_dossier(session_date)
        if not dossier["trades"] and not dossier["sniper_candidates"] \
                and not dossier["day_skip"]:
            return {"summary": ""}  # nothing happened today
        raw = assistant.deep_think(
            "Coach this trading day. Reply with the strict JSON only.",
            context=COACH_SYSTEM + "\n\nDOSSIER:\n"
            + json.dumps(dossier, default=str)[:24000],
            purpose="scheduled")  # nobody is waiting on this; it never
                                  # spends the metered key unless API_MODE=full
        s, e = raw.find("{"), raw.rfind("}")
        parsed = json.loads(raw[s:e + 1]) if s != -1 else None
        if not isinstance(parsed, dict):
            return {"summary": ""}
        imps = parsed.get("imperfections") or []
        entry = {"date": str(session_date),
                 "reviewed_at": f"{datetime.now(ET):%Y-%m-%d %H:%M:%S}",
                 "imperfections": imps,
                 "day_summary": parsed.get("day_summary", ""),
                 "top_lesson": parsed.get("top_lesson", "")}
        _jl_append(REVIEWS, entry)
        escalations = _track_proposals(session_date, imps)
        # the top lesson feeds the digest the chat brain reads every reply:
        # this is the loop where the bot teaches itself
        if entry["top_lesson"]:
            try:
                import learn
                learn._append_lesson({
                    "session": str(session_date),
                    "graded_at": entry["reviewed_at"],
                    "wins": 0, "losses": 0, "trades": [],
                    "review": f"coach: {entry['day_summary']}"[:400],
                    "lessons": [entry["top_lesson"]],
                    "watch_tomorrow": "", "proposed_change": None})
            except Exception:
                pass
        n_real = sum(1 for i in imps if i.get("root_cause")
                     not in (None, "none", "luck"))
        lines = []
        if entry["day_summary"]:
            lines.append(f"Coach's read: {entry['day_summary']}")
        lines.append(f"Imperfections found: {n_real}"
                     + (f" (top: {imps[0]['what'][:120]})"
                        if n_real and imps else ""))
        if entry["top_lesson"]:
            lines.append(f"Lesson now in the playbook: {entry['top_lesson']}")
        lines += escalations
        return {"summary": "\n".join(lines), "entry": entry}
    except Exception as e:
        return {"summary": f"Coach session failed safely ({str(e)[:120]})."}
