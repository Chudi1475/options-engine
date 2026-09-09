"""Nightly self-review — the bot grades the day's own calls, writes down what
it learned, and gets a little sharper every night.

Runs unattended at a RANDOM time late each weekday evening (fired from
scanner.daemon via Service.maybe_learn, window 21:00-23:45 ET, seeded off the
date so a restart re-derives the SAME time instead of re-rolling or
double-firing). It reads the day's tracked positions (positions.json) plus any
legacy alerts, grades each one RIGHT/WRONG with the SAME logic the 4 PM recap
uses, then asks the Claude brain to distill 1-3 concrete lessons: what the good
calls had in common, what the mistakes had in common, and what to watch
tomorrow. Lessons are written to lessons.jsonl (one nightly row per session; a
re-run of an already-reviewed session replaces its earlier row instead of
double-counting it) and distilled into lessons_digest.md, which the
conversational brain reads on every reply, so accumulated learning actually
changes how it reasons. The newest
watch_tomorrow line is pinned at the top of that digest for exactly its
target session, so the one time-sensitive output actually shapes the next
day instead of evaporating overnight.

Guardrail: this NEVER auto-changes a trade rule, threshold, or the allow-list.
If a lesson implies a rule change, it is PROPOSED to the owner in the nightly
digest for a human to approve. The bot keeps its picky filter and its win-rate
floor until Chudi says otherwise. Proposals are tracked in state.json with a
status (pending, approved, rejected): a repeat of a pending idea is counted,
not re-pitched every night, /proposals lists and decides everything on the
table, and a rule change the model phrases as a plain lesson is routed into
that same channel instead of the digest the brain silently absorbs.

Usage:
    python learn.py                    # run tonight's review + send owner digest
    python learn.py --dry-run          # print everything, send nothing, write nothing
    python learn.py --date 2026-06-12  # review a specific past session (testing)
    python learn.py --export-backlog reviews.json    # offline review lane, out
    python learn.py --import-reviews reviews.json    # offline review lane, back
    python learn.py --repair-lessons   # finish derived work; prints a JSON
                                       # result, exits 1 if it did not finish
    python learn.py --migrate-legacy   # one-time: attach lesson rows written
                                       # before they carried a review id
"""

import hashlib
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config
import live_params
import strategy_spec
import telegram
from positions import PositionBook
from strategy import StrategyConfig

ET = ZoneInfo("America/New_York")

LESSONS_LOG = config.DATA_DIR / "lessons.jsonl"       # full append-only history
LESSONS_DIGEST = config.DATA_DIR / "lessons_digest.md"  # distilled playbook the brain reads
REVIEWS_FILE = config.DATA_DIR / "trade_reviews.jsonl"  # one deep review per closed trade, ever
DIGEST_KEEP = 20   # most-recent lesson bullets kept in the digest (recency wins)

# A review is identified by (trade id, revision). The revision defaults to 1
# and comes from the file; a HIGHER revision is a correction the importer
# accepts and supersedes with, the SAME revision with different judgement is a
# conflict it refuses, and a LOWER one is stale. payload_hash fingerprints the
# judgement fields only, so a re-import of byte-identical judgement is provably
# the same work and can be resumed instead of refused.
JUDGMENT_FIELDS = ("why", "cause", "cause_detail", "lesson", "reviewer")

# How much weight a lesson's text may carry. 'deterministic' came from the
# graded record itself, 'model_hypothesis' is a plausible explanation a model
# wrote, 'owner' is Chudi's own call. A row that declares nothing stays
# 'unclassified' rather than being given a class it never earned.
EVIDENCE_CLASSES = ("deterministic", "model_hypothesis", "owner", "unclassified")

# stamped on a legacy lesson row the migration could not map to any review.
# Never a guess: an unmapped row is reported, not attached to a trade.
UNMAPPED = "__unmapped__"


# ------------------- deep per-trade review (full history) -------------------

CAUSE_SYSTEM = """You are the trading bot doing a DEEP review of ONE past trade
to fully understand why it won or lost. Consider the setup itself AND outside
forces: breaking news, war or geopolitics, scandals, Fed or macro events (FOMC,
CPI, NFP), volatility regime, time decay, and execution. A trade tagged
[PAPER] was practice mode: tracked and graded, but no money moved. Learn from
it, but a lesson from a [PAPER] trade must say it came from practice, never
posing as a live result. Only reason from what you are shown; never invent
facts. No dashes as punctuation. Reply ONLY JSON:
{"why": "2-3 sentences, the honest root cause of the outcome",
 "cause": "setup|news|geopolitics|macro_event|volatility|time_decay|execution|unknown",
 "cause_detail": "one line naming the specific driver if any",
 "lesson": "one concrete, actionable rule this trade teaches (or empty string)"}"""


def _reviewed_ids() -> set:
    if not REVIEWS_FILE.exists():
        return set()
    ids = set()
    for line in REVIEWS_FILE.read_text(encoding="utf-8-sig").splitlines():
        try:
            ids.add(json.loads(line).get("id"))
        except json.JSONDecodeError:
            continue
    return ids


def _breaking_news_for(date_str: str) -> list:
    """Breaking-news titles the bot itself alerted on that date, mined from
    alerts.log ('--- YYYY-MM-DD hh:mm:ss' stamps + BREAKING lines)."""
    titles, cur = [], ""
    try:
        for line in config.ALERTS_LOG.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("--- "):
                cur = line[4:14]
            elif cur == date_str and "BREAKING" in line:
                titles.append(line.strip()[:160])
    except OSError:
        pass
    return titles[:8]


def review_history(max_new: int = 25) -> int:
    """Go back over EVERY closed trade not yet deep-reviewed: grade it, attribute
    the real cause (setup vs news vs war vs macro etc.), extract the lesson, and
    persist to trade_reviews.jsonl. Bounded per night so it can never run away;
    it catches up across nights until the whole history is covered."""
    import recap
    try:
        import assistant
    except Exception:
        assistant = None
    book = PositionBook()
    seen = _reviewed_ids()
    todo = [p for p in book.positions
            if getattr(p, "state", "") == "closed"
            and getattr(p, "final_pnl_pct", None) is not None
            and p.id not in seen]
    # This loop is one paid call per trade against a backlog of about a
    # hundred, which is most of what the metered balance ever gets spent on.
    # Under the default spending policy it does not run at all: the same
    # reviews happen on the desktop through --export-backlog and come back
    # through --import-reviews, and the ids dedup either way.
    allowed, why = config.api_allows("scheduled")
    if todo and not allowed:
        print(f"learn: {len(todo)} trade(s) waiting on a deep review, but {why}. "
              "Run: python learn.py --export-backlog reviews.json")
        return 0
    # brain paused (usage-limit countdown)? DEFER the whole batch to the next
    # night instead of permanently writing cause='unknown' for every trade —
    # once an id lands in trade_reviews.jsonl it is never re-reviewed.
    if todo and assistant is not None:
        try:
            if assistant.cooldown_left_s() > 0:
                print("learn: brain is in its usage-limit countdown; "
                      f"deferring {len(todo)} trade reviews to tomorrow night")
                return 0
        except Exception:
            pass
    done = 0
    for p in todo[:max_new]:
        verdict, story = recap.position_story(p)
        news = _breaking_news_for(p.date)
        tag = "[PAPER] " if getattr(p, "paper", False) else ""
        brief = (f"{tag}Trade: {p.ticker} {p.strike:g} {p.direction.upper()} on {p.date}, "
                 f"alerted {p.time_et} ET. Entry momentum {getattr(p, 'mom_pct', None)}, "
                 f"quoted win rate {getattr(p, 'win_rate_quoted', None)}, risk mode "
                 f"{getattr(p, 'risk_mode', None)}. Outcome: {verdict}, final "
                 f"{p.final_pnl_pct}%, peak {getattr(p, 'mfe_pct', None)}%, trough "
                 f"{getattr(p, 'mae_pct', None)}%, exit "
                 f"'{(getattr(p, 'final_exit', None) or {}).get('reason')}'. "
                 f"Story: {story}")
        if news:
            brief += "\nBreaking news the bot flagged that day:\n" + "\n".join(news)
        parsed, brain_live = None, False
        if assistant is not None:
            try:
                brain_live = assistant.enabled()
            except Exception:
                brain_live = False
        if brain_live:
            raw = assistant.complete(CAUSE_SYSTEM, brief, max_tokens=500)
            if raw:
                try:
                    t = raw.strip()
                    s, e = t.find("{"), t.rfind("}")
                    parsed = json.loads(t[s:e + 1]) if s != -1 else None
                except (json.JSONDecodeError, ValueError):
                    parsed = None
            if parsed is None:
                # the brain exists but this call failed: do NOT freeze this
                # trade as cause='unknown' forever. Usage limit -> stop the
                # batch; one-off hiccup -> retry this trade tomorrow night.
                try:
                    if assistant.cooldown_left_s() > 0:
                        print("learn: usage limit mid-batch; deferring the rest")
                        break
                except Exception:
                    pass
                continue
        if parsed is None:
            # no brain at all (no API key): keep the honest deterministic record
            parsed = {"why": story, "cause": "unknown", "cause_detail": "",
                      "lesson": ""}
        entry = {"id": p.id, "date": p.date, "ticker": p.ticker,
                 "direction": p.direction, "strike": p.strike,
                 "paper": bool(getattr(p, "paper", False)),
                 "final_pnl_pct": p.final_pnl_pct, "verdict": verdict,
                 "why": parsed.get("why", ""), "cause": parsed.get("cause", "unknown"),
                 "cause_detail": parsed.get("cause_detail", ""),
                 "lesson": (parsed.get("lesson") or "").strip(),
                 "reviewed_at": et_now().strftime("%Y-%m-%d %H:%M:%S %Z")}
        entry["revision"] = 1
        entry["payload_hash"] = _payload_hash(entry)
        with REVIEWS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        # a real lesson from a deep review feeds the same digest the brain
        # reads, so it carries its provenance: which trade it came from, how
        # much weight it has earned, and which review revision produced it.
        # Without source_review_id here, this path used to write rows the
        # repair guard could not see, and the next repair appended a second
        # copy of every one of them.
        if entry["lesson"]:
            _append_lesson({"session": p.date, "graded_at": entry["reviewed_at"],
                            "wins": 0, "losses": 0, "trades": [],
                            "review": f"deep review {p.ticker} {p.date}: {entry['why']}",
                            "lessons": [entry["lesson"]],
                            "watch_tomorrow": "", "proposed_change": None,
                            "source_review_id": p.id,
                            "source_payload_hash": entry["payload_hash"],
                            "review_revision": 1,
                            "trade_ids": [p.id],
                            "evidence_class": ("model_hypothesis" if brain_live
                                               else "deterministic")})
        done += 1
    return done


def et_now() -> datetime:
    return datetime.now(ET)


# ---------------------------- grade the day ----------------------------

def _grade_positions(session_date):
    """Grade every tracked position for the session using recap's exact
    RIGHT/WRONG logic. Works straight off positions.json, so it needs no
    network. A position still 'open' after its expiry close (the bot was down
    at the 16:00 settle) is first settled IN MEMORY with the same honest
    semantics the next session's monitoring would apply, so the review grades
    the settled truth instead of calling an expired 0DTE STILL OPEN and
    telling the owner it is still being watched. positions.json is never
    written from here: a --dry-run must stay write-free, and the daytime
    monitoring path stays the one writer (it re-derives the identical settle
    at the next open)."""
    import recap
    book = PositionBook()
    book.settle_overdue(session_date, et_now(), save=False)
    trades = []
    for p in book.for_date(session_date):
        # position_story grades the closed-but-ungraded settle honestly
        # (NOT GRADED) since the recap learned to settle stragglers too
        verdict, story = recap.position_story(p)
        final = getattr(p, "final_pnl_pct", None)
        trades.append({
            "ticker": p.ticker,
            "direction": p.direction,
            "strike": p.strike,
            "texted": p.time_et,
            "verdict": verdict,
            "story": story,
            "won": (final is not None and final > 0),
            "closed": (getattr(p, "state", "") == "closed" and final is not None),
            # practice signals must never masquerade as live results anywhere
            # downstream: the brief, the fallback lessons, the owner digest
            # and the coach dossier all key off this flag
            "paper": bool(getattr(p, "paper", False)),
            "features": {
                "mom_pct": getattr(p, "mom_pct", None),
                "win_rate_quoted": getattr(p, "win_rate_quoted", None),
                "risk_mode": getattr(p, "risk_mode", None),
                "entry_source": getattr(p, "entry_source", None),
            },
            "outcome": {
                "final_pnl_pct": final,
                "mfe_pct": getattr(p, "mfe_pct", None),
                "mae_pct": getattr(p, "mae_pct", None),
                "exit_reason": (getattr(p, "final_exit", None) or {}).get("reason"),
                "banked_half": bool(getattr(p, "half_exit", None)),
            },
        })
    return trades


def _market_context(session_date):
    """Best-effort one-liner on what the market did. Needs yfinance; if it's
    not reachable (offline test, feed hiccup) we just skip it rather than fail
    the whole review."""
    try:
        import recap
        spx = recap.fetch_5m("^GSPC")
        day = spx[spx.index.date == session_date]
        if len(day):
            return recap.market_story(day)
    except Exception as e:
        print(f"learn: market context unavailable ({e}); continuing without it")
    return ""


def grade_day(session_date):
    """Full structured record of the session: market context + every graded
    call. session_date is a datetime.date."""
    trades = _grade_positions(session_date)
    return {
        "session": str(session_date),
        "day_name": session_date.strftime("%a %#m/%#d") if sys.platform.startswith("win")
        else session_date.strftime("%a %-m/%-d"),
        "market": _market_context(session_date),
        "trades": trades,
        "wins": sum(1 for t in trades if t["won"]),
        "losses": sum(1 for t in trades if t["closed"] and not t["won"]),
    }


# ---------------------------- synthesize lessons ----------------------------

# The entry window and alert names are spliced in at review time from the
# EFFECTIVE settings (live_params-aware), so the reviewer grades against the
# rules actually running and cannot re-propose a change that already shipped
# via live_params.json. REVIEWER_SYSTEM keeps the built-in render; with no
# override file the nightly prompt is exactly that text.
_REVIEWER_TEMPLATE = """You are the trading brain of 'options-engine' doing your
own nightly review. You trade a 15-minute momentum continuation method on 0DTE
options (the 'Kelechi' style): spot the morning push in the __WINDOW__ ET
window, ride the continuation, __EXIT_PLAN__. You only alert
__NAMES__ and only above a __WIN_FLOOR__ backtested win rate.

Tonight you are grading YOUR OWN calls to get sharper. Be brutally honest with
yourself, like a trader journaling after the close. Find the real pattern
behind the wins and the losses, not platitudes. A lesson must be specific and
actionable ("when a call spikes past +30% in the first 20 minutes, bank half
immediately, do not wait for +25% to become give-back") not generic ("manage
risk"). If you genuinely see nothing new worth writing, say so.

A call tagged [PAPER] was practice mode: the bot tracked and graded it, but
no money moved. Learn from it, and weigh it lighter than a live call. A
lesson drawn mostly from [PAPER] calls must say so in its own words, and a
practice outcome is never stated as a live result.

Never invent numbers. Only reason from the calls you are shown. Never use
dashes of any kind as punctuation (no em dash, no " - ", no "--"); use commas,
periods, or a new sentence. Plain language.

Reply with ONLY a JSON object, no prose around it:
{
  "review": "2-4 sentences, bro to bro, honest read on the day",
  "lessons": ["one concrete rule", "..."],
  "watch_tomorrow": "one short line on what to watch or do differently",
  "proposed_change": null
}
Put 1 to 3 items in lessons (fewer is fine on a quiet day). Set proposed_change
to a specific string ONLY if a real trade-rule or threshold change is warranted
(it will be shown to the human for approval, never auto-applied); otherwise
null."""


def _render_reviewer(cfg) -> str:
    # the exit rules and the win-rate floor come from strategy_spec for the
    # same reason the window and names do: the reviewer has to grade against
    # the rules actually running, or it proposes changes to a bracket that
    # already moved
    spec = strategy_spec.get()
    return (_REVIEWER_TEMPLATE
            .replace("__WINDOW__", live_params.window_et(cfg))
            .replace("__NAMES__", ", ".join(cfg.watchlist))
            .replace("__EXIT_PLAN__", spec.exit_plan_sentence())
            .replace("__WIN_FLOOR__", spec.floor_txt()))


REVIEWER_SYSTEM = _render_reviewer(StrategyConfig())


def reviewer_system() -> str:
    """REVIEWER_SYSTEM rendered with the EFFECTIVE entry window and alert
    names (live_params-aware), so the nightly review grades against the
    configured rules, not built-ins an override may have replaced."""
    return _render_reviewer(live_params.effective()[0])


def _prior_learning_block(session: str) -> list:
    """Lines reminding the reviewer what it already learned, so it builds on
    prior lessons instead of re-deriving them from scratch every night, and
    can flag when today confirms or violates one. The playbook bullets come
    from the digest (already deduped and capped by _rebuild_digest); the
    nightly reads come from lessons.jsonl, newest session first, one per
    session."""
    lines = []
    try:
        if LESSONS_DIGEST.exists():
            bullets = [ln.strip() for ln in
                       LESSONS_DIGEST.read_text(encoding="utf-8-sig").splitlines()
                       if ln.strip().startswith("- ")]
            bullets = [b for b in bullets if b != "- (none yet)"]
            if bullets:
                lines.append("WHAT I ALREADY LEARNED (my playbook, newest first):")
                lines += bullets
    except OSError:
        pass
    reads, seen_days = [], set()
    for entry in _by_session_newest_first(_all_lessons()):
        review = (entry.get("review") or "").strip()
        day = entry.get("session", "")
        # skip tonight's own session (a re-run would echo itself) and the
        # per-trade deep-review rows, which are cause notes, not nightly reads
        if (not review or not day or day == session or day in seen_days
                or review.startswith("deep review ")):
            continue
        seen_days.add(day)
        reads.append(f"- {day}: {review[:240]}")
        if len(reads) == 3:
            break
    if reads:
        lines.append("MY LAST FEW NIGHTLY READS:")
        lines += reads
    if lines:
        lines.append("Do not repeat a lesson already in the playbook above, "
                     "only add genuinely new ones. If today confirms or "
                     "violates one of those lessons, name it in your review "
                     "instead of restating it.")
    return lines


def _day_brief(record) -> str:
    """Compact text the reviewer model reasons over: what it already learned
    first, so tonight's lessons build on prior ones instead of repeating them,
    then today's session."""
    lines = _prior_learning_block(record["session"])
    if lines:
        lines.append("")
    lines.append(f"Session: {record['day_name']} ({record['session']})")
    if record["market"]:
        lines.append(f"Market: {record['market']}")
    if not record["trades"]:
        lines.append("Calls today: NONE. The bot stayed out (no setup cleared "
                     "the filter, or the morning was not a clean momentum push).")
        return "\n".join(lines)
    lines.append(f"Calls today: {len(record['trades'])} "
                 f"({record['wins']} right, {record['losses']} wrong).")
    if any(t.get("paper") for t in record["trades"]):
        lines.append("Calls tagged [PAPER] were practice mode: tracked and "
                     "graded, but no money moved. Weigh them lighter than "
                     "live calls, and say so in any lesson drawn from them.")
    for t in record["trades"]:
        f = t["features"]
        o = t["outcome"]
        tag = "[PAPER] " if t.get("paper") else ""
        lines.append(
            f"- {tag}{t['ticker']} {t['strike']:g} {t['direction'].upper()} texted {t['texted']}: "
            f"{t['verdict']}. entry momentum {f.get('mom_pct')}, quoted win rate "
            f"{f.get('win_rate_quoted')}, risk mode {f.get('risk_mode')}, "
            f"entry priced from {f.get('entry_source')}. "
            f"outcome: final {o.get('final_pnl_pct')}%, peak {o.get('mfe_pct')}%, "
            f"trough {o.get('mae_pct')}%, exit '{o.get('exit_reason')}', "
            f"banked half: {o.get('banked_half')}. why: {t['story']}")
    return "\n".join(lines)


def _deterministic_review(record) -> dict:
    """Fallback when the Claude brain is unavailable (no ANTHROPIC_API_KEY or a
    network hiccup). Grounded, non-invented observations so the digest still
    grows and the loop keeps compounding."""
    trades = record["trades"]
    lessons = []
    if not trades:
        # no lesson on a quiet day: re-learning the same "staying flat is
        # correct" bullet every no-trade night just crowds the digest window.
        review = ("Quiet day, no setup cleared the filter so the bot stayed out. "
                  "No text is a position too, sitting out a choppy morning "
                  "protects the account.")
    else:
        review = (f"{len(trades)} call(s): {record['wins']} right, "
                  f"{record['losses']} wrong.")
        paper_n = sum(1 for t in trades if t.get("paper"))
        if paper_n:
            review += (f" {paper_n} of them [PAPER] practice signals, "
                       "no money moved.")
        for t in trades:
            o = t["outcome"]
            if t.get("paper"):
                # the model can hedge a practice-mode lesson in its own words;
                # this fallback cannot, so a paper outcome writes nothing into
                # the permanent playbook rather than posing as a live result
                continue
            if (not t["won"] and o.get("exit_reason") == "stop"
                    and (o.get("mfe_pct") or 0) >= 10):
                if o.get("banked_half"):
                    # half WAS banked, so "bank half into the spike" would be
                    # advice the trade already followed. A 'stop' exit on a
                    # half_sold position means the runner leg fell from the
                    # peak to the hard stop before the give-back trail fired
                    # (step() checks the stop first, and the trail only
                    # advances on comparable cycles), so that is the lesson.
                    lessons.append(
                        f"{t['ticker']} peaked +{o['mfe_pct']:.0f}% and still "
                        "stopped out even after banking half into the spike. "
                        "Banking half was right. The runner leg did the "
                        "damage, it fell from the peak to the hard stop "
                        "before the give-back trail could fire.")
                else:
                    lessons.append(
                        f"{t['ticker']} peaked +{o['mfe_pct']:.0f}% then stopped out. "
                        "When a call spikes double digits early, bank half into the "
                        "spike instead of waiting.")
            elif t["won"] and o.get("banked_half"):
                lessons.append(
                    f"{t['ticker']} ran the playbook clean: banked half into "
                    "strength then trailed the runner. Keep repeating this shape.")
    return {"review": review, "lessons": lessons[:3],
            "watch_tomorrow": "Same plan, wait for the text, be picky.",
            "proposed_change": None}


def synthesize(record) -> dict:
    """Ask the brain to distill lessons; fall back to a deterministic review.
    The review runs once per day and silently steers every future reply, so it
    goes to the deep brain (extended thinking) first; the everyday model
    answers when the deep call fails or comes back empty, and the
    deterministic review covers a night with no brain at all."""
    try:
        import assistant
        brief = _day_brief(record)
        system = reviewer_system()
        raw = (assistant.complete_deep(system, brief)
               or assistant.complete(system, brief, max_tokens=700))
        if raw:
            txt = raw.strip()
            if txt.startswith("```"):  # strip a ```json fence if the model added one
                txt = txt.strip("`")
                txt = txt[4:] if txt.lower().startswith("json") else txt
            start, end = txt.find("{"), txt.rfind("}")
            if start != -1 and end != -1:
                data = json.loads(txt[start:end + 1])
                data.setdefault("review", "")
                lessons = data.get("lessons") or []
                data["lessons"] = [str(x).strip() for x in lessons if str(x).strip()][:3]
                data.setdefault("watch_tomorrow", "")
                data.setdefault("proposed_change", None)
                return _sanitize(data)
    except Exception as e:
        print(f"learn: brain synthesis failed ({e}); using deterministic review")
    return _sanitize(_deterministic_review(record))


# ------------------------ rule-change proposal registry ------------------------

PROPOSALS_KEY = "rule_proposals"  # state.json: every rule change ever pitched

# A lesson that PROPOSES CHANGING a configured rule must reach the owner as a
# proposal, never the digest the brain silently absorbs. Detection is
# deliberately conservative (precision over recall): a missed one still lands
# under the digest header that says lessons never override the hard rules,
# but a false flag would hide a real lesson from the playbook. The reviewer
# prompt's own blessed example ("when a call spikes past +30%... bank half
# immediately, do not wait for +25%...") is behavioral guidance and must NOT
# match, so bare percents and words like "stop" or "half" alone never trigger:
# it takes a change-verb aimed at a rule noun, an explicit numeric swap, or an
# allow-list edit naming a ticker. Progressive "-ing" forms are excluded on
# purpose ("staying picky is increasing the win rate" describes an outcome).
_RULE_NOUNS = (r"(?:stops?|windows?|thresholds?|floors?|gates?|watchlists?|"
               r"allow[- ]?lists?|give[- ]?backs?|half[- ]?targets?|"
               r"take[- ]?half|sell[- ]?half|bank[- ]?half|"
               r"min(?:imum)?[- ]gap|entry (?:start|end))\b")
_CHANGE_PATTERNS = [
    # a change-verb aimed at a rule noun (up to 3 words between, so "lower
    # the win rate floor" and "raise the intraday stop" both count)
    re.compile(r"\b(?:chang|rais|lower|widen|narrow|tighten|loosen|extend|"
               r"mov|adjust|increas|decreas|bump|reduc|relax|shift)"
               r"(?:e|es|ed|s)?\s+(?:[-\w$%+.]+\s+){0,3}?" + _RULE_NOUNS,
               re.I),
    # an explicit numeric threshold swap: "at +30% instead of +25%"
    # ("instead of waiting" has no digit, so it stays a lesson)
    re.compile(r"\b(?:instead of|rather than)\s+[+-]?\d", re.I),
    # allow-list edits name a ticker: "Allow QQQ", "add IWM to the watchlist"
    re.compile(r"\b(?:[Aa]llow(?:ed|ing|s)?|[Aa]dd(?:ed|ing|s)?|"
               r"[Rr]emove[ds]?|[Dd]rop(?:ped|ping|s)?)\s+"
               r"(?:[Tt]he\s+)?[A-Z]{2,6}\b"),
]


def _is_rule_change(text) -> bool:
    """True when a lesson bullet is really a rule or threshold change in
    disguise. Those go to the owner for approval, never into the digest."""
    t = str(text or "")
    return any(p.search(t) for p in _CHANGE_PATTERNS)


def _sanitize(lesson: dict) -> dict:
    """Move rule-change-phrased bullets out of lessons and into
    proposed_change, so the guardrail (a human approves every rule change)
    holds even when the model phrases one as a plain lesson."""
    bullets = lesson.get("lessons") or []
    moved = [x for x in bullets if _is_rule_change(x)]
    if moved:
        lesson["lessons"] = [x for x in bullets if not _is_rule_change(x)]
        pc = str(lesson.get("proposed_change") or "").strip()
        lesson["proposed_change"] = " | ".join(([pc] if pc else []) + moved)
    return lesson


def _proposal_key(text) -> str:
    """Wording-tolerant identity: lowercased, whitespace collapsed, trailing
    punctuation dropped, so tonight's copy of last night's idea matches."""
    return " ".join(str(text or "").lower().split()).strip(".!? ")


def proposals_list() -> list:
    rows = config.state_get(PROPOSALS_KEY, [])
    return rows if isinstance(rows, list) else []


def track_proposal(text, session, source="nightly") -> dict:
    """Upsert one proposed rule change in state.json. The same idea proposed
    again gets its sighting counted (once per session), never a fresh nightly
    re-pitch; the returned row carries 'repeat' and 'status' so the owner
    message and /proposals can render it honestly. Never applies anything."""
    key = _proposal_key(text)
    if not key:
        return {}
    rows = proposals_list()
    for row in rows:
        if row.get("key") == key:
            if row.get("last") != str(session):
                row["times"] = int(row.get("times", 1)) + 1
                row["last"] = str(session)
                config.state_set(PROPOSALS_KEY, rows)
            out = dict(row)
            out["repeat"] = True
            return out
    row = {"key": key, "text": str(text).strip(), "first": str(session),
           "last": str(session), "times": 1, "status": "pending",
           "source": str(source)}
    rows.append(row)
    config.state_set(PROPOSALS_KEY, rows)
    out = dict(row)
    out["repeat"] = False
    return out


def _pending_sorted(rows) -> list:
    """Pending proposals in first-seen order. New ones append at the end, so
    the numbers /proposals shows stay stable while the owner replies."""
    return sorted([r for r in rows if r.get("status", "pending") == "pending"],
                  key=lambda r: (str(r.get("first") or ""),
                                 str(r.get("key") or "")))


def proposals_text() -> str:
    """/proposals: every rule change the reviews have pitched, with status,
    plus coach ideas recurring across days, in one place."""
    rows = proposals_list()
    pending = _pending_sorted(rows)
    lines = []
    if pending:
        lines.append(f"💡 PENDING RULE PROPOSALS ({len(pending)}):")
        for i, r in enumerate(pending, 1):
            times = int(r.get("times", 1))
            nights = "night" if times == 1 else "nights"
            lines.append(f"{i}. {r.get('text')}")
            lines.append(f"   first {r.get('first')}, came up {times} {nights}, "
                         f"from the {r.get('source', 'nightly')} review")
        lines.append("")
        lines.append("/proposals ok <n> · /proposals no <n>  "
                     "(a note can follow the n)")
        lines.append("Approving records the decision only. A trade threshold "
                     "still needs a verified backtest round before anything "
                     "ships.")
    else:
        lines.append("No pending rule proposals. 🎯")
    decided = [r for r in rows if r.get("status") in ("approved", "rejected")]
    if decided:
        decided.sort(key=lambda r: str(r.get("decided") or ""), reverse=True)
        lines.append("")
        lines.append("DECIDED:")
        for r in decided[:5]:
            lines.append(f"- {r.get('status')} {r.get('decided', '?')}: "
                         f"{str(r.get('text'))[:100]}")
    # the coach tracks its own proposals per day (coach_proposals.jsonl);
    # surface the recurring ones so this really is the whole table
    try:
        import coach
        by_key = {}
        for p in coach._jl_read(coach.PROPOSALS):
            if p.get("key"):
                by_key.setdefault(p["key"], []).append(p)
        recurring = sorted(((len(v), v[-1]) for v in by_key.values()
                            if len(v) >= 2), key=lambda x: -x[0])
        if recurring:
            lines.append("")
            lines.append("🧑‍🏫 COACH IDEAS RECURRING ACROSS DAYS:")
            for n, p in recurring[:5]:
                tag = (" (escalated, worth a backtest round)"
                       if n >= coach.ESCALATE_AT else "")
                lines.append(f"- {n} days: {str(p.get('proposal'))[:100]}{tag}")
    except Exception:
        pass
    return "\n".join(lines)


def _decide_proposal(n_str, status, note="") -> str:
    try:
        n = int(n_str)
    except (TypeError, ValueError):
        return ("Usage: /proposals ok <n>  or  /proposals no <n>  "
                "(numbers from /proposals)")
    rows = proposals_list()
    pending = _pending_sorted(rows)
    if not 1 <= n <= len(pending):
        return f"No pending proposal #{n}. /proposals shows what's on the table."
    row = pending[n - 1]  # same dict object as in rows, so the edit persists
    row["status"] = status
    row["decided"] = str(et_now().date())
    if note:
        row["note"] = str(note)[:300]
    config.state_set(PROPOSALS_KEY, rows)
    txt = str(row.get("text"))[:120]
    if status == "approved":
        return (f"Approved: {txt}\nNothing changes by itself. A trade "
                "threshold still needs a verified backtest round, and code "
                "ships through a reviewed change. It stays on the decided "
                "list so it never gets re-pitched.")
    return (f"Rejected: {txt}\nI'll stop bringing it up. It stays in the "
            "record so a future review can't pitch it fresh.")


def proposals_command(args="") -> str:
    """Owner command: list tracked rule-change proposals or decide one.
    /proposals · /proposals ok <n> [note] · /proposals no <n> [note]"""
    parts = str(args or "").split(None, 2)
    if parts:
        word = parts[0].lower()
        n = parts[1] if len(parts) > 1 else None
        note = parts[2] if len(parts) > 2 else ""
        if word in ("ok", "yes", "approve"):
            return _decide_proposal(n, "approved", note)
        if word in ("no", "reject"):
            return _decide_proposal(n, "rejected", note)
        return ("Usage: /proposals  ·  /proposals ok <n> [note]  ·  "
                "/proposals no <n> [note]")
    return proposals_text()


# ---------------------------- persist + deliver ----------------------------

def _lesson_kind(entry: dict) -> str:
    """Which writer produced a lessons.jsonl row. run() writes one 'nightly'
    row per session and coach.reflect one 'coach' row per session, so those
    upsert on a re-run; 'deep' rows are one PER TRADE (several can share a
    session date, each trade id reviewed at most once via _reviewed_ids), so
    they stay append-only."""
    review = str(entry.get("review") or "")
    if review.startswith("deep review "):
        return "deep"
    if review.startswith("coach: "):
        return "coach"
    return "nightly"


def strategy_fingerprint() -> str:
    """A 16 hex fingerprint of the strategy the bot was running when a lesson
    was written, so a rule from one era is never read as a rule of another.

    COMPUTED from an explicit, sorted projection of the live settings, never
    typed: no number in this file, and nothing here can change a rule. The
    projection is explicit on purpose. StrategySpec carries frozenset fields
    whose repr ordering is not a stable serialisation, so a blind asdict plus
    str would produce a fingerprint that drifts between runs and make every
    lesson look like it came from a different strategy."""
    try:
        spec = strategy_spec.get()
        proj = {
            "entry_window": str(spec.entry_window),
            "mom_bars": spec.mom_bars,
            "allowed_setups": sorted(str(x) for x in spec.allowed_setups),
            "watchlist": sorted(f"{k}={v}" for k, v in
                                dict(spec.watchlist).items()),
            "tp_half_pct": spec.tp_half_pct,
            "stop_pct": spec.stop_pct,
            "runner_giveback_pct": spec.runner_giveback_pct,
            "min_winrate": spec.min_winrate,
            "risk_per_trade_pct": spec.risk_per_trade_pct,
        }
        blob = json.dumps(proj, sort_keys=True, default=str)
    except Exception as e:      # a fingerprint is metadata, never a blocker
        blob = "unavailable:" + str(e)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _stamp_provenance(entry: dict) -> dict:
    """Fill in the provenance a lesson row needs to be re-read honestly later:
    which trades it came from, how much weight it has earned, which strategy
    was live, which review revision produced it, and whether it is still the
    active version of that lesson.

    Only fills what the caller did not declare. A row that names no evidence
    class stays 'unclassified' rather than being handed one it never earned;
    the digest tags a hypothesis as a hypothesis and leaves the rest alone."""
    e = dict(entry)
    if "trade_ids" not in e:
        ids = []
        for t in e.get("trades") or []:
            tid = t.get("id") if isinstance(t, dict) else None
            if tid:
                ids.append(str(tid))
        if not ids and e.get("source_review_id"):
            ids = [str(e["source_review_id"])]
        e["trade_ids"] = ids
    if e.get("evidence_class") not in EVIDENCE_CLASSES:
        e["evidence_class"] = "unclassified"
    e.setdefault("strategy_version", strategy_fingerprint())
    if e.get("source_review_id"):
        e.setdefault("review_revision", 1)
        e.setdefault("source_payload_hash", "")
    e.setdefault("active", True)
    e.setdefault("superseded_by", None)
    return e


def _append_lesson(entry: dict):
    """Persist one lessons.jsonl row. Nightly and coach rows UPSERT by
    (session, kind): re-running an already-reviewed session (a --date
    backfill, a crash-restart re-fire between send and dedup-mark) replaces
    the previous row instead of stacking a duplicate the digest would count
    twice. The first review of a session is still a pure append; a line the
    parser cannot read is preserved verbatim, never destroyed.

    Every row is stamped with its provenance on the way in, so a writer that
    does not know about the scheme (coach.reflect) still produces a row the
    digest and the repair path can reason about."""
    entry = _stamp_provenance(entry)
    moved = [x for x in entry.get("lessons") or [] if _is_rule_change(x)]
    if moved:
        # a rule change phrased as a lesson (coach and deep-review rows land
        # here unsanitized) goes to the proposal registry for a human to
        # decide, never into the digest the brain absorbs
        entry = dict(entry)
        entry["lessons"] = [x for x in entry["lessons"]
                            if not _is_rule_change(x)]
        for x in moved:
            try:
                track_proposal(x, entry.get("session") or str(et_now().date()),
                               source=_lesson_kind(entry))
            except Exception as e:
                print("learn: proposal tracking failed "
                      f"({e}); kept out of the digest anyway: {str(x)[:80]}")
    kind = _lesson_kind(entry)
    session = entry.get("session")
    if kind != "deep" and session and LESSONS_LOG.exists():
        try:
            lines = LESSONS_LOG.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            lines = None
        if lines is not None:
            kept, dropped = [], 0
            for line in lines:
                try:
                    old = json.loads(line)
                except json.JSONDecodeError:
                    kept.append(line)
                    continue
                if old.get("session") == session and _lesson_kind(old) == kind:
                    dropped += 1
                    continue
                kept.append(line)
            if dropped:
                try:
                    tmp = LESSONS_LOG.with_suffix(".jsonl.tmp")
                    tmp.write_text("\n".join(kept) + ("\n" if kept else ""),
                                   encoding="utf-8")
                    tmp.replace(LESSONS_LOG)
                except OSError as e:  # rewrite failed: append still lands below
                    print(f"learn: lesson upsert rewrite failed ({e}); appending")
    with LESSONS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _all_lessons() -> list:
    if not LESSONS_LOG.exists():
        return []
    out = []
    for line in LESSONS_LOG.read_text(encoding="utf-8-sig").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _by_session_newest_first(entries: list) -> list:
    """Entries ordered by their actual session date, newest first (file
    position breaks ties, later rows win), instead of raw append order: a
    backfilled OLD session lands at the END of the file and must not
    masquerade as the newest guidance in the digest or the reviewer's
    nightly reads. Rows with an unparseable session sort oldest."""
    def key(pair):
        i, entry = pair
        try:
            d = datetime.strptime(str(entry.get("session") or ""),
                                  "%Y-%m-%d").date()
        except ValueError:
            d = date.min
        return (d, i)
    return [e for _, e in sorted(enumerate(entries), key=key, reverse=True)]


def _next_weekday(d):
    """The next weekday after session date d, the session a watch_tomorrow
    line was written for. Holidays are ignored on purpose: expiring the line
    on a market holiday drops it one session early, the safe direction."""
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def _latest_watch():
    """(session_date, text) of the newest non-empty watch_tomorrow in the log,
    or None. Keyed by session date rather than file position so a backfilled
    old night cannot steal the pin; a re-run of the same session keeps its
    newest copy. Deep-review and coach rows write an empty watch, so they
    never pin."""
    best = None
    for entry in _all_lessons():
        watch = str(entry.get("watch_tomorrow") or "").strip()
        if not watch:
            continue
        try:
            day = datetime.strptime(entry.get("session", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if best is None or day >= best[0]:
            best = (day, watch)
    return best


def _rebuild_digest():
    """Rewrite the distilled playbook the brain reads: the most recent lesson
    bullets, newest SESSION first (not file append order, so a backfilled old
    night cannot sit on top as if it were last night's guidance), capped so
    the prompt never bloats. A lesson whose text repeats across nights keeps
    only its newest occurrence, so a stretch of look-alike days cannot fill
    the window and evict real lessons. The newest watch_tomorrow is pinned
    above the bullets as a dated FOR TODAY line that expires once its target
    session has passed.

    A row superseded by a later review revision is skipped: the corrected text
    is what belongs in the active playbook, while the original stays in
    lessons.jsonl for audit. A bullet whose row declares itself a model
    hypothesis is tagged as one, so a plausible explanation cannot read to the
    brain as an established finding."""
    bullets = []
    for entry in _by_session_newest_first(_all_lessons()):
        if entry.get("active") is False:
            continue          # superseded by a later revision of its review
        d = entry.get("session", "")
        tag = ""
        if d:
            try:
                tag = datetime.strptime(str(d), "%Y-%m-%d").strftime("%#m/%#d"
                    if sys.platform.startswith("win") else "%-m/%-d")
            except ValueError:  # malformed legacy row: keep it, tag it as-is
                tag = str(d)
        if entry.get("evidence_class") == "model_hypothesis":
            tag = (tag + ", hypothesis") if tag else "hypothesis"
        for lesson in entry.get("lessons") or []:
            bullets.append((tag, str(lesson)))
    seen, deduped = set(), []
    for tag, lesson in bullets:  # newest session first, its copy wins the dedup
        key = " ".join(lesson.lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append((tag, lesson))
    bullets = deduped[:DIGEST_KEEP]
    # pin the newest watch_tomorrow: it is the one output written to shape
    # the NEXT session, and it used to evaporate because only the lessons
    # array reached the digest. Expires once its target session (the next
    # weekday after the review) has passed, so a stretch with no nightly
    # review cannot leave week-old guidance labeled as today's.
    pin = ""
    latest = _latest_watch()
    if latest and _next_weekday(latest[0]) >= et_now().date():
        tag = latest[0].strftime("%#m/%#d" if sys.platform.startswith("win")
                                 else "%-m/%-d")
        pin = (f"FOR TODAY (my watch line from the {tag} review): "
               f"{latest[1]}\n\n")
    window = live_params.window_et(live_params.effective()[0])
    spec = strategy_spec.get()
    header = ("These are my own observations from grading my calls night after "
              "night. Apply them when reading setups. They NEVER override the "
              f"hard rules ({window} entry window, {spec.floor_txt()} "
              f"win-rate floor, {spec.exit_plan_short()}).\n")
    body = "\n".join(f"- ({tag}) {lesson}" for tag, lesson in bullets) or "- (none yet)"
    LESSONS_DIGEST.write_text(header + "\n" + pin + body + "\n", encoding="utf-8")


def _owner_message(record, lesson, prop=None) -> str:
    lines = [f"🌙 NIGHTLY REVIEW: {record['day_name']}", ""]
    if record["market"]:
        lines += ["THE MARKET: " + record["market"], ""]
    if record["trades"]:
        lines.append("TODAY'S CALLS:")
        for t in record["trades"]:
            hd = _t(t["texted"])
            tag = "[PAPER] " if t.get("paper") else ""
            lines.append(f"{tag}{t['ticker']} {t['strike']:g} {t['direction'].upper()} "
                         f"({hd}): {t['verdict']}")
        lines.append("")
    else:
        lines += ["TODAY'S CALLS: none, the bot stayed out.", ""]
    if lesson.get("review"):
        lines += [lesson["review"], ""]
    if lesson.get("lessons"):
        lines.append("WHAT I LEARNED:")
        lines += [f"- {x}" for x in lesson["lessons"]]
        lines.append("")
    if lesson.get("watch_tomorrow"):
        lines += ["WATCH TOMORROW: " + lesson["watch_tomorrow"], ""]
    pc = lesson.get("proposed_change")
    if pc:
        status = (prop or {}).get("status", "pending")
        if status == "pending" and (prop or {}).get("repeat"):
            times = int(prop.get("times", 1))
            lines += ["💡 Rule change still waiting on your call "
                      f"(came up {times} nights now): {prop.get('text', pc)}",
                      "/proposals to approve or reject it."]
        elif status == "pending":
            lines += ["💡 PROPOSED RULE CHANGE (needs your ok): " + pc,
                      "I will not change any trade rule on my own. "
                      "/proposals to approve or reject."]
        # approved or rejected: the owner already decided, no nightly nagging
    return "\n".join(lines).strip()


def _t(hms: str) -> str:
    try:
        return (datetime.strptime(hms, "%H:%M:%S") - timedelta(hours=1)).strftime("%I:%M %p CT").lstrip("0")
    except (ValueError, TypeError):
        return hms or "?"


def run(require_date=None, dry=False):
    """Grade the session, synthesize lessons, persist them, and text the owner.
    Returns a list of telegram delivery errors ([] on success)."""
    if require_date is None:
        session = et_now().date()
    elif isinstance(require_date, str):
        session = datetime.strptime(require_date, "%Y-%m-%d").date()
    else:
        session = require_date

    # deep-review every not-yet-reviewed closed trade (full history, bounded
    # per night) with cause attribution: setup vs news vs war vs macro etc.
    reviewed = 0
    if not dry:
        try:
            reviewed = review_history()
            if reviewed:
                _rebuild_digest()
        except Exception as e:
            print(f"learn: deep review skipped ({e})")

    # READ the forward ledger's nightly line. The grading itself does NOT
    # happen here any more: it is a free deterministic job on the scanner's
    # own schedule (Service.maybe_grade_forward), with its own retry budget,
    # its own needs-attention park, and an audit line per pass. A second
    # grader in here was unaudited and ran behind LEARN_ENABLED, so the day's
    # counts moved with a paid-AI switch even though the grading did not.
    ledger_note = ""
    if not dry:
        try:
            import forward_ledger
            ledger_note = forward_ledger.nightly_summary()
        except Exception as e:
            print(f"learn: sniper forward summary skipped ({e})")

    # the coach: a second agent that reviews the WHOLE day (trades, sniper
    # candidates, news, skips), reconstructs the perfect scenario for every
    # imperfection, and feeds the lesson back into the brain's playbook.
    # Runs late enough in the night that the scanner's grading job has already
    # filled the day's outcomes, so it reads a graded ledger.
    coach_note = ""
    if not dry:
        try:
            import coach
            coach_note = coach.reflect(session).get("summary", "")
        except Exception as e:
            print(f"learn: coach session skipped ({e})")

    record = grade_day(session)
    lesson = synthesize(record)
    prop = None
    if lesson.get("proposed_change"):
        if dry:  # preview the full pitch; never write the registry on a dry run
            prop = {"status": "pending", "repeat": False}
        else:
            prop = track_proposal(lesson["proposed_change"], record["session"])
    entry = {
        "session": record["session"],
        "graded_at": et_now().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "wins": record["wins"], "losses": record["losses"],
        "trades": [{"ticker": t["ticker"], "direction": t["direction"],
                    "verdict": t["verdict"], "final_pnl_pct": t["outcome"]["final_pnl_pct"]}
                   for t in record["trades"]],
        "review": lesson.get("review", ""),
        "lessons": lesson.get("lessons", []),
        "watch_tomorrow": lesson.get("watch_tomorrow", ""),
        "proposed_change": lesson.get("proposed_change"),
    }
    msg = _owner_message(record, lesson, prop)
    if reviewed:
        msg += (f"\n\n🔎 DEEP REVIEW: went back over {reviewed} past trade(s), "
                "attributed the real cause (setup vs news vs macro), and folded "
                "the lessons into my playbook.")
    if ledger_note:
        msg += "\n\n🎯 " + ledger_note
    if coach_note:
        msg += "\n\n🧑‍🏫 COACH: " + coach_note

    if dry:
        print("----- would append to lessons.jsonl -----")
        print(json.dumps(entry, indent=2))
        print("\n----- owner digest -----")
        print(msg)
        return []

    _append_lesson(entry)
    _rebuild_digest()
    owner = telegram.primary_owner_id()
    if not owner:
        print("learn: no owner configured; lessons saved, digest not sent.")
        return []
    err = telegram.send_to(owner, msg)
    if err:
        print(f"learn: owner digest send error: {err}")
        return [err]
    print(f"learn: review for {record['session']} saved and sent.")
    return []


# ------------------- the offline review lane -------------------
# review_history is the single most expensive thing this bot does: one paid
# call per closed trade, bounded at 25 a night, against a backlog of about a
# hundred. That balance is metered pay-per-use and has hit $0 more than once,
# taking the whole chat brain down with it.
#
# So the reviews move off the bot. export_backlog writes exactly the briefs
# review_history would have sent, the reasoning happens on the owner's own
# desktop, and import_reviews writes the answers back in the bot's own row
# format. The nightly loop then skips every one of them permanently, because
# _reviewed_ids dedups on the trade id and does not care who wrote the row.


def _brief_for(p, story: str) -> str:
    """The exact text review_history hands the model for one trade, so an
    offline review reasons over the same evidence the nightly one would."""
    tag = "[PAPER] " if getattr(p, "paper", False) else ""
    brief = (f"{tag}Trade: {p.ticker} {p.strike:g} {p.direction.upper()} on {p.date}, "
             f"alerted {p.time_et} ET. Entry momentum {getattr(p, 'mom_pct', None)}, "
             f"quoted win rate {getattr(p, 'win_rate_quoted', None)}, risk mode "
             f"{getattr(p, 'risk_mode', None)}. Outcome: {{verdict}}, final "
             f"{p.final_pnl_pct}%, peak {getattr(p, 'mfe_pct', None)}%, trough "
             f"{getattr(p, 'mae_pct', None)}%, exit "
             f"'{(getattr(p, 'final_exit', None) or {}).get('reason')}'. "
             f"Story: {story}")
    return brief


def export_backlog(path: str) -> int:
    """Write every not-yet-reviewed closed trade to a JSON file for offline
    review. Read-only: nothing in DATA_DIR is touched."""
    import recap
    book = PositionBook()
    seen = _reviewed_ids()
    todo = [p for p in book.positions
            if getattr(p, "state", "") == "closed"
            and getattr(p, "final_pnl_pct", None) is not None
            and p.id not in seen]
    out = []
    for p in todo:
        verdict, story = recap.position_story(p)
        out.append({
            "id": p.id, "date": p.date, "ticker": p.ticker,
            "direction": p.direction, "strike": p.strike,
            "paper": bool(getattr(p, "paper", False)),
            "final_pnl_pct": p.final_pnl_pct, "verdict": verdict,
            "brief": _brief_for(p, story).replace("{verdict}", verdict),
            "breaking_news": _breaking_news_for(p.date),
        })
    payload = {"system": CAUSE_SYSTEM,
               "row_schema": ["id", "date", "ticker", "direction", "strike",
                              "paper", "final_pnl_pct", "verdict", "why",
                              "cause", "cause_detail", "lesson", "reviewed_at",
                              "reviewer", "revision"],
               "revision_note": "revision defaults to 1. Re-importing the same "
                                "file is safe, it resumes whatever the last "
                                "run did not finish. To CORRECT a review "
                                "already imported, keep the id and raise "
                                "revision: that supersedes the old lesson "
                                "instead of being refused.",
               "verdict_note": "verdict is a fact of the trade, taken from the "
                               "tracked position. What the file says is kept "
                               "as verdict_claimed, and a claim that "
                               "contradicts the tracked P&L refuses the row.",
               "causes": ["setup", "news", "geopolitics", "macro_event",
                          "volatility", "time_decay", "execution", "unknown"],
               "already_reviewed": len(seen), "pending": len(out),
               "trades": out}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"exported {len(out)} pending trade(s) to {path} "
          f"({len(seen)} already reviewed)")
    return len(out)


def _closed_positions_by_id() -> dict:
    """Every closed, priced position the bot actually tracked, keyed by id.
    This is the authority an imported review has to resolve against."""
    out = {}
    try:
        for p in PositionBook().positions:
            if getattr(p, "state", "") == "closed" \
                    and getattr(p, "final_pnl_pct", None) is not None:
                out[p.id] = p
    except Exception as e:
        print(f"import: could not read positions ({e})")
    return out


def _as_bool(v):
    """Strict-ish boolean. bool("false") is True in python, which is how a
    JSON string "false" silently became a live trade flagged as practice."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0", ""):
            return False
    return None


def _review_revision(r) -> int:
    """The revision a review declares. Absent means the first one. A value
    that is not a whole number at or above 1 returns 0, which the caller
    reports as a problem instead of guessing what was meant."""
    v = r.get("revision", r.get("review_revision", 1))
    if isinstance(v, bool):
        return 0
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return n if n >= 1 else 0


def _payload_hash(r) -> str:
    """16 hex over the JUDGEMENT fields only, so a re-import of byte-identical
    judgement is provably the same work and can be resumed rather than refused,
    while a changed judgement at the same revision is provably a conflict.
    The outcome fields are deliberately excluded: they come from the tracked
    position, so they can never differ between two readings of one trade."""
    payload = {k: ("" if r.get(k) is None else str(r.get(k)).strip())
               for k in JUDGMENT_FIELDS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _review_key(review_id, revision) -> str:
    return f"{review_id}#r{int(revision)}"


def _reviewed_index() -> dict:
    """id -> the identity of the newest committed review for that trade:
    {'revision', 'payload_hash'}. payload_hash is None for a legacy row that
    predates the scheme, and a None hash is never treated as a conflict: it
    cannot be proven different, so the safe reading is that it is the same."""
    out = {}
    if not REVIEWS_FILE.exists():
        return out
    for line in REVIEWS_FILE.read_text(encoding="utf-8-sig").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        rid = row.get("id")
        if not rid:
            continue
        rev = _review_revision(row) or 1
        prev = out.get(rid)
        if prev is None or rev >= prev["revision"]:
            out[rid] = {"revision": rev, "payload_hash": row.get("payload_hash")}
    return out


def _verdict_sign(text):
    """+1, -1 or None for what a claimed verdict asserts about the outcome.
    Wording-tolerant on purpose: the canonical verdict carries an emoji, an
    offline reviewer may type WIN or LOSS."""
    t = " ".join(str(text or "").lower().split())
    if not t or "not graded" in t or "still open" in t:
        return None
    if "right" in t or "win" in t:
        return 1
    if "wrong" in t or "loss" in t or "lost" in t or "lose" in t:
        return -1
    return None


def _lesson_already_recorded(review_id: str) -> bool:
    """Whether this review's lesson is already in the lessons log. Lets an
    interrupted import repair its derived work without duplicating it.

    Both ids must be truthy to match. A legacy row carries no
    source_review_id, so without that guard a review row with no id matched
    the first legacy row and had its lesson silently dropped as already done.
    A read failure is raised, never swallowed: reporting 'not recorded' for
    every review on a volume blip would re-append the entire lesson history
    in one pass.

    This is the single-row form. repair_lessons reads the log ONCE through
    _recorded_lesson_index instead of re-parsing it per review row."""
    for e in _all_lessons():
        rid = e.get("source_review_id")
        if rid and review_id and rid == review_id:
            return True
    return False


def _recorded_lesson_index(lessons):
    """Read the lessons log ONCE and index it two ways.

    keys   : {(source_review_id, review_revision)} for rows that carry an id.
    legacy : rows with no usable id, indexed by the review string and by
             (session, normalised lesson text), which are the two things a
             derived row can be matched on without guessing."""
    keys, legacy_review, legacy_text = set(), {}, {}
    for i, e in enumerate(lessons):
        rid = e.get("source_review_id")
        if rid and rid != UNMAPPED:
            keys.add((str(rid), _review_revision(e) or 1))
            continue
        if rid == UNMAPPED:
            continue          # already looked at and reported as unmappable
        review = " ".join(str(e.get("review") or "").split())
        if review:
            legacy_review.setdefault(review, []).append(i)
        session = str(e.get("session") or "")
        for lesson in e.get("lessons") or []:
            key = (session, " ".join(str(lesson).lower().split()))
            if key[1]:
                legacy_text.setdefault(key, []).append(i)
    return {"keys": keys, "by_review": legacy_review, "by_text": legacy_text}


def _legacy_row_for(review_row, lesson_text, index):
    """The index of the legacy lessons row this review already produced, or
    None. Exact matches only: the reconstructed review string first, then the
    exact lesson text within the same session. Never a fuzzy guess."""
    review = " ".join(_derived_review_line(review_row).split())
    hit = index["by_review"].get(review)
    if hit:
        return hit[0]
    key = (str(review_row.get("date") or ""),
           " ".join(str(lesson_text).lower().split()))
    hit = index["by_text"].get(key)
    if hit:
        return hit[0]
    return None


def _derived_review_line(r) -> str:
    """The review string a derived lesson carries. Built in one place so the
    repair path and the legacy matcher cannot drift apart."""
    return (f"deep review {r.get('ticker','')} {r.get('date','')}: "
            f"{r.get('why','')}")


def _validate_rows(rows, closed, index):
    """Check EVERY row before anything is committed.

    Returns (fresh, already, problems).

    fresh    rows to write: never seen, or a strictly higher revision that
             corrects one already on file.
    already  rows whose identical judgement is already committed at the same
             revision. Nothing is written for them, but their derived work is
             still reconciled, which is what makes a retry of a partial batch
             resume instead of refuse.
    problems kill the whole batch. A half-applied import is worse than a
             refused one. A conflicting revision (same id, same revision,
             different judgement) is a problem on purpose: an import supplies
             judgement, it never silently overwrites a canonical review."""
    causes = {"setup", "news", "geopolitics", "macro_event", "volatility",
              "time_decay", "execution", "unknown"}
    fresh, already, problems = [], [], []
    ids_in_file = set()
    for i, r in enumerate(rows):
        where = f"row {i}"
        if not isinstance(r, dict):
            problems.append(f"{where}: not an object")
            continue
        rid = r.get("id")
        where = f"row {i} (id={rid!r})"
        if not rid or not isinstance(rid, str):
            problems.append(f"{where}: missing or non-string id")
            continue
        if rid in ids_in_file:
            problems.append(f"{where}: duplicated inside the file")
            continue
        ids_in_file.add(rid)
        rev = _review_revision(r)
        if rev < 1:
            problems.append(f"{where}: revision {r.get('revision')!r} is not "
                            "a whole number at or above 1")
            continue
        pos = closed.get(rid)
        if pos is None:
            problems.append(f"{where}: no closed position with this id. A "
                            "review must describe a trade the bot really took")
            continue
        cause = (r.get("cause") or "unknown")
        if not isinstance(cause, str) or cause.strip() not in causes:
            problems.append(f"{where}: cause {cause!r} is not one of "
                            f"{sorted(causes)}")
            continue
        paper = _as_bool(r.get("paper", getattr(pos, "paper", False)))
        if paper is None:
            problems.append(f"{where}: paper={r.get('paper')!r} is not a "
                            "boolean. A string 'false' is not False")
            continue
        if paper != bool(getattr(pos, "paper", False)):
            problems.append(f"{where}: paper={paper} contradicts the tracked "
                            f"position ({bool(getattr(pos, 'paper', False))})")
            continue
        bad_field = None
        for field in ("why", "cause_detail", "lesson"):
            if r.get(field) is not None and not isinstance(r.get(field), str):
                bad_field = field
                break
        if bad_field:
            problems.append(f"{where}: {bad_field} must be text")
            continue
        # the verdict is a canonical fact, so a claim that contradicts the
        # tracked P&L is reported rather than silently discarded
        claimed = _verdict_sign(r.get("verdict"))
        if claimed is not None:
            actual = 1 if (pos.final_pnl_pct or 0) > 0 else -1
            if claimed != actual:
                problems.append(
                    f"{where}: verdict {r.get('verdict')!r} contradicts the "
                    f"tracked final P&L ({pos.final_pnl_pct}). The verdict is "
                    "a fact of the trade, not a judgement the file supplies")
                continue
        phash = _payload_hash(r)
        known = index.get(rid)
        if known is None:
            fresh.append((r, pos, cause.strip(), rev, phash))
            continue
        if rev < known["revision"]:
            problems.append(f"{where}: revision {rev} is older than the "
                            f"committed revision {known['revision']}")
            continue
        if rev > known["revision"]:
            fresh.append((r, pos, cause.strip(), rev, phash))
            continue
        if known["payload_hash"] and known["payload_hash"] != phash:
            problems.append(
                f"{where}: revision {rev} is already committed with a "
                f"different judgement (on file {known['payload_hash']}, in "
                f"this file {phash}). Imports never overwrite a canonical "
                "review. Bump 'revision' to file this as a correction")
            continue
        already.append((r, pos, cause.strip(), rev, phash))
    return fresh, already, problems


def _read_reviews(reviews_file) -> list:
    """Every committed review row. An unparseable line is skipped, a file the
    process cannot read is raised: the caller has to record that, because
    treating an unreadable ledger as an empty one would re-derive everything."""
    rows = []
    if not reviews_file.exists():
        return rows
    for line in reviews_file.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def import_reviews_result(path: str, reviewer: str = "offline") -> dict:
    """Commit offline-written reviews into trade_reviews.jsonl and say exactly
    what happened. Returns
    {ok, written, resumed, refused, problems, lessons} .

    Every row must resolve to a real closed position, and the immutable trade
    facts (date, ticker, direction, strike, paper, final P&L, verdict) are
    copied FROM that position rather than trusted from the file, so an import
    can supply judgement but never rewrite what happened. The file's own
    verdict is kept beside it as verdict_claimed, so a fabricated claim is
    auditable instead of vanishing.

    Validation runs over the whole file first and any problem refuses the whole
    batch: a bad row that lands is never revisited. A row already committed at
    the SAME revision with the SAME judgement is not a problem, it is a
    resume, so re-running the exact file an interrupted import was given
    finishes the work instead of refusing it. A different judgement at that
    same revision is a conflict and does refuse; a higher revision is accepted
    as a correction that supersedes the earlier lesson.

    The accepted rows are appended in ONE fsynced write, so a batch has a
    single torn-write window rather than one per row, and the resume rule
    makes even that window recoverable by running the same file again."""
    out = {"ok": False, "written": 0, "resumed": 0, "refused": 0,
           "problems": [], "lessons": {}}
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        out["problems"] = [f"cannot read {path}: {e}"]
        print(f"import: cannot read {path}: {e}")
        return out
    rows = data.get("reviews") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        out["problems"] = ["expected a JSON list, or an object with a "
                           "'reviews' list"]
        print("import: expected a JSON list, or an object with a 'reviews' list")
        return out

    closed = _closed_positions_by_id()
    try:
        index = _reviewed_index()
    except OSError as e:
        out["problems"] = [f"cannot read the committed reviews: {e}"]
        print(f"import: cannot read the committed reviews ({e}); "
              "nothing written")
        return out

    fresh, already, problems = _validate_rows(rows, closed, index)
    if problems:
        out["problems"] = problems
        out["refused"] = len(problems)
        print(f"import: REFUSED. {len(problems)} problem(s), nothing written:")
        for p in problems[:20]:
            print(f"  - {p}")
        if len(problems) > 20:
            print(f"  ... and {len(problems) - 20} more")
        return out

    import recap
    stamp = et_now().strftime("%Y-%m-%d %H:%M:%S %Z")
    src = os.path.basename(str(path))
    lines = []
    for r, pos, cause, rev, phash in fresh:
        entry = {
            # immutable facts come from the tracked position, not the file
            "id": pos.id, "date": pos.date, "ticker": pos.ticker,
            "direction": pos.direction, "strike": pos.strike,
            "paper": bool(getattr(pos, "paper", False)),
            "final_pnl_pct": pos.final_pnl_pct,
            "verdict": recap.position_story(pos)[0],
            # judgement comes from the file, and so does the file's own
            # verdict claim, kept for audit and never used as the fact
            "verdict_claimed": str(r.get("verdict") or ""),
            "why": r.get("why", ""), "cause": cause,
            "cause_detail": r.get("cause_detail", ""),
            "lesson": (r.get("lesson") or "").strip(),
            "reviewed_at": r.get("reviewed_at") or stamp,
            "reviewer": r.get("reviewer") or reviewer,
            "revision": rev, "payload_hash": phash,
            "imported_from": src, "imported_at": stamp,
        }
        lines.append(json.dumps(entry) + "\n")

    if lines:
        with REVIEWS_FILE.open("a", encoding="utf-8") as f:
            f.write("".join(lines))
            f.flush()
            try:
                os.fsync(f.fileno())
            except (OSError, ValueError):
                pass          # no fsync on this handle; the append still landed
        out["written"] = len(lines)
    out["resumed"] = len(already)

    result = repair_lessons(REVIEWS_FILE)
    out["lessons"] = result
    out["ok"] = bool(result.get("ok"))
    resumed = (f"; {out['resumed']} row(s) resumed" if out["resumed"] else "")
    if result.get("digest_rebuilt"):
        tail = "digest rebuilt"
    else:
        why = "; ".join(str(e) for e in result.get("errors") or []) or "unknown"
        tail = f"digest NOT rebuilt ({why})"
    print(f"imported {out['written']} review(s){resumed}; "
          f"{result.get('derived', 0)} lesson(s) derived; {tail}")
    return out


def import_reviews(path: str, reviewer: str = "offline") -> int:
    """import_reviews_result, reduced to the count of rows newly written.
    Kept as the number the older callers and regressions read."""
    return import_reviews_result(path, reviewer)["written"]


def _supersede_lessons(review_id: str, revision: int) -> int:
    """Mark every earlier lesson row for this review inactive and point it at
    the revision that replaced it. The text STAYS in lessons.jsonl for audit;
    only the active digest drops it. Rewrites through a temp file, and a line
    the parser cannot read is copied through untouched."""
    if not LESSONS_LOG.exists():
        return 0
    lines = LESSONS_LOG.read_text(encoding="utf-8-sig").splitlines()
    kept, changed = [], 0
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if (row.get("source_review_id") == review_id
                and (_review_revision(row) or 1) < revision
                and row.get("active") is not False):
            row["active"] = False
            row["superseded_by"] = _review_key(review_id, revision)
            changed += 1
            kept.append(json.dumps(row))
            continue
        kept.append(line)
    if changed:
        tmp = LESSONS_LOG.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""),
                       encoding="utf-8")
        tmp.replace(LESSONS_LOG)
    return changed


def repair_lessons(reviews_file) -> dict:
    """Derive the lesson every committed review owes, then rebuild the digest,
    and say exactly what happened.

    Returns {ok, derived, superseded, skipped, legacy_matched, digest_rebuilt,
    errors}. The caller logs from this, so no message can claim a rebuild that
    did not happen.

    Idempotent on purpose: this is the repair path. A review whose lesson is
    already recorded at that revision is skipped; a HIGHER revision derives a
    new lesson and supersedes the earlier one. A legacy row this review
    already produced before the ids existed is matched, not duplicated. Each
    row is caught on its own, so one bad review cannot cost the rest their
    lessons, and the whole thing never raises at its callers."""
    result = {"ok": True, "derived": 0, "superseded": 0, "skipped": 0,
              "legacy_matched": 0, "digest_rebuilt": False, "errors": []}
    try:
        rows = _read_reviews(reviews_file)
    except OSError as e:
        result["ok"] = False
        result["errors"].append(f"reviews unreadable ({e}); nothing written")
        return result
    try:
        index = _recorded_lesson_index(_all_lessons())
    except Exception as e:
        # failing OPEN here would re-append the entire lesson history, so the
        # honest move is to write nothing and say why
        result["ok"] = False
        result["errors"].append(f"lessons log unreadable ({e}); nothing written")
        return result

    for r in rows:
        lesson = (r.get("lesson") or "").strip()
        if not lesson:
            continue
        rid = r.get("id")
        if not rid:
            result["ok"] = False
            result["errors"].append("a review row carries a lesson but no id; "
                                    "its lesson cannot be tracked")
            continue
        rev = _review_revision(r) or 1
        if (str(rid), rev) in index["keys"]:
            result["skipped"] += 1
            continue
        legacy_at = _legacy_row_for(r, lesson, index)
        if legacy_at is not None:
            # this review already produced that row, back when the rows
            # carried no id. Adopting it is --migrate-legacy's job; the one
            # thing this path must never do is write a second copy.
            result["legacy_matched"] += 1
            result["skipped"] += 1
            continue
        try:
            result["superseded"] += _supersede_lessons(str(rid), rev)
            _append_lesson({
                "session": r.get("date", ""),
                "graded_at": r.get("reviewed_at", ""),
                "wins": 0, "losses": 0, "trades": [],
                "review": _derived_review_line(r),
                "lessons": [lesson], "watch_tomorrow": "",
                "proposed_change": None,
                "source_review_id": str(rid),
                "source_payload_hash": r.get("payload_hash") or _payload_hash(r),
                "review_revision": rev,
                "trade_ids": [str(rid)],
                "evidence_class": "model_hypothesis",
            })
        except Exception as e:
            result["ok"] = False
            result["errors"].append(f"{_review_key(rid, rev)}: {e}")
            continue
        index["keys"].add((str(rid), rev))
        result["derived"] += 1

    try:
        _rebuild_digest()
        result["digest_rebuilt"] = True
    except Exception as e:
        result["ok"] = False
        result["errors"].append(f"digest rebuild failed ({e})")
    return result


def _derive_lessons_for(reviews_file) -> int:
    """repair_lessons, reduced to the count of lessons newly derived. Kept as
    the number the older callers and regressions read."""
    return repair_lessons(reviews_file)["derived"]


def migrate_legacy_lessons(reviews_file=None) -> dict:
    """One-time, deliberate repair of lessons written before the rows carried
    a source_review_id. Returns {ok, mapped, unmapped, rows, backup, errors}.

    A legacy row is matched to its review by exact equality of the
    reconstructed review string, and failing that by exact lesson text within
    the same session. Nothing else. A row that matches neither is stamped
    __unmapped__ and reported, never attached to a trade on a guess.

    A copy of the file is written first, the rewrite goes through a temp file,
    and a line the parser cannot read is copied through untouched."""
    result = {"ok": True, "mapped": 0, "unmapped": 0, "rows": 0,
              "backup": "", "errors": []}
    reviews_file = reviews_file if reviews_file is not None else REVIEWS_FILE
    if not LESSONS_LOG.exists():
        return result
    try:
        rows = _read_reviews(reviews_file)
        lines = LESSONS_LOG.read_text(encoding="utf-8-sig").splitlines()
    except OSError as e:
        result["ok"] = False
        result["errors"].append(f"cannot read ({e}); nothing written")
        return result

    by_review, by_text = {}, {}
    for r in rows:
        rid = r.get("id")
        if not rid:
            continue
        rev = _review_revision(r) or 1
        by_review.setdefault(" ".join(_derived_review_line(r).split()),
                             (str(rid), rev, r))
        lesson = " ".join(str(r.get("lesson") or "").lower().split())
        if lesson:
            by_text.setdefault((str(r.get("date") or ""), lesson),
                               (str(rid), rev, r))

    kept = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        result["rows"] += 1
        if row.get("source_review_id"):
            kept.append(line)
            continue
        review = " ".join(str(row.get("review") or "").split())
        hit = by_review.get(review)
        if hit is None:
            for lesson in row.get("lessons") or []:
                hit = by_text.get((str(row.get("session") or ""),
                                   " ".join(str(lesson).lower().split())))
                if hit:
                    break
        if hit is None:
            row["source_review_id"] = UNMAPPED
            row["migrated_from"] = "legacy"
            result["unmapped"] += 1
        else:
            rid, rev, r = hit
            row["source_review_id"] = rid
            row["review_revision"] = rev
            row["source_payload_hash"] = (r.get("payload_hash")
                                          or _payload_hash(r))
            row["trade_ids"] = row.get("trade_ids") or [rid]
            row["migrated_from"] = "legacy"
            result["mapped"] += 1
        kept.append(json.dumps(row))

    if not (result["mapped"] or result["unmapped"]):
        return result
    try:
        backup = LESSONS_LOG.with_name(
            LESSONS_LOG.name + "."
            + et_now().strftime("%Y%m%d%H%M%S") + ".pre-migrate")
        backup.write_text("\n".join(lines) + ("\n" if lines else ""),
                          encoding="utf-8")
        result["backup"] = backup.name
        tmp = LESSONS_LOG.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""),
                       encoding="utf-8")
        tmp.replace(LESSONS_LOG)
    except OSError as e:
        result["ok"] = False
        result["errors"].append(f"rewrite failed ({e}); nothing changed")
        result["mapped"], result["unmapped"] = 0, 0
    return result


def main() -> int:
    """Exit code, not a count: 0 means the requested work is durably done,
    1 means it is not. Nothing shells out to learn.py today (scanner calls
    learn.run and learn.review_history in process), so the codes are free to
    mean that."""
    if "--export-backlog" in sys.argv:
        i = sys.argv.index("--export-backlog")
        export_backlog(sys.argv[i + 1] if i + 1 < len(sys.argv)
                       else "review_backlog.json")
        return 0
    if "--repair-lessons" in sys.argv:
        # recovery path for an import that died between writing a review and
        # writing its lesson: idempotent, so it is always safe to run
        result = repair_lessons(REVIEWS_FILE)
        print(json.dumps(result, sort_keys=True))
        return 0 if (result["ok"] and result["digest_rebuilt"]) else 1
    if "--migrate-legacy" in sys.argv:
        # deliberate one-time adoption of lessons written before the rows
        # carried a source_review_id. Backs the file up before rewriting it.
        result = migrate_legacy_lessons(REVIEWS_FILE)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] else 1
    if "--import-reviews" in sys.argv:
        i = sys.argv.index("--import-reviews")
        if i + 1 >= len(sys.argv):
            print("usage: learn.py --import-reviews <file.json>")
            return 1
        return 0 if import_reviews_result(sys.argv[i + 1])["ok"] else 1
    dry = "--dry-run" in sys.argv
    date = None
    if "--date" in sys.argv:
        i = sys.argv.index("--date")
        if i + 1 < len(sys.argv):
            date = sys.argv[i + 1]
    run(require_date=date, dry=dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
