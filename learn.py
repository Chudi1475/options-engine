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
digest for a human to approve. The bot keeps its picky filter and 70% win-rate
floor until Chudi says otherwise.

Usage:
    python learn.py                    # run tonight's review + send owner digest
    python learn.py --dry-run          # print everything, send nothing, write nothing
    python learn.py --date 2026-06-12  # review a specific past session (testing)
"""

import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config
import telegram
from positions import PositionBook

ET = ZoneInfo("America/New_York")

LESSONS_LOG = config.DATA_DIR / "lessons.jsonl"       # full append-only history
LESSONS_DIGEST = config.DATA_DIR / "lessons_digest.md"  # distilled playbook the brain reads
REVIEWS_FILE = config.DATA_DIR / "trade_reviews.jsonl"  # one deep review per closed trade, ever
DIGEST_KEEP = 20   # most-recent lesson bullets kept in the digest (recency wins)


# ------------------- deep per-trade review (full history) -------------------

CAUSE_SYSTEM = """You are the trading bot doing a DEEP review of ONE past trade
to fully understand why it won or lost. Consider the setup itself AND outside
forces: breaking news, war or geopolitics, scandals, Fed or macro events (FOMC,
CPI, NFP), volatility regime, time decay, and execution. Only reason from what
you are shown; never invent facts. No dashes as punctuation. Reply ONLY JSON:
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
        brief = (f"Trade: {p.ticker} {p.strike:g} {p.direction.upper()} on {p.date}, "
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
                 "final_pnl_pct": p.final_pnl_pct, "verdict": verdict,
                 "why": parsed.get("why", ""), "cause": parsed.get("cause", "unknown"),
                 "cause_detail": parsed.get("cause_detail", ""),
                 "lesson": (parsed.get("lesson") or "").strip(),
                 "reviewed_at": et_now().strftime("%Y-%m-%d %H:%M:%S %Z")}
        with REVIEWS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        # a real lesson from a deep review feeds the same digest the brain reads
        if entry["lesson"]:
            _append_lesson({"session": p.date, "graded_at": entry["reviewed_at"],
                            "wins": 0, "losses": 0, "trades": [],
                            "review": f"deep review {p.ticker} {p.date}: {entry['why']}",
                            "lessons": [entry["lesson"]],
                            "watch_tomorrow": "", "proposed_change": None})
        done += 1
    return done


def et_now() -> datetime:
    return datetime.now(ET)


# ---------------------------- grade the day ----------------------------

def _grade_positions(session_date):
    """Grade every tracked position for the session using recap's exact
    RIGHT/WRONG logic. Works straight off positions.json, so it needs no
    network (0DTE positions are all closed by the time this runs)."""
    import recap
    book = PositionBook()
    trades = []
    for p in book.for_date(session_date):
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

REVIEWER_SYSTEM = """You are the trading brain of 'options-engine' doing your
own nightly review. You trade a 15-minute momentum continuation method on 0DTE
options (the 'Kelechi' style): spot the morning push in the 9:45-10:30 ET
window, ride the continuation, sell half at +25%, let the runner run and sell
when it gives back ~40 points from its peak, hard stop at -70%. You only alert
SPX, SPY, QCOM, TSLA and only above a 70% backtested win rate.

Tonight you are grading YOUR OWN calls to get sharper. Be brutally honest with
yourself, like a trader journaling after the close. Find the real pattern
behind the wins and the losses, not platitudes. A lesson must be specific and
actionable ("when a call spikes past +30% in the first 20 minutes, bank half
immediately, do not wait for +25% to become give-back") not generic ("manage
risk"). If you genuinely see nothing new worth writing, say so.

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
    for t in record["trades"]:
        f = t["features"]
        o = t["outcome"]
        lines.append(
            f"- {t['ticker']} {t['strike']:g} {t['direction'].upper()} texted {t['texted']}: "
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
        for t in trades:
            o = t["outcome"]
            if (not t["won"] and o.get("exit_reason") == "stop"
                    and (o.get("mfe_pct") or 0) >= 10):
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
    """Ask the brain to distill lessons; fall back to a deterministic review."""
    try:
        import assistant
        raw = assistant.complete(REVIEWER_SYSTEM, _day_brief(record), max_tokens=700)
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
                return data
    except Exception as e:
        print(f"learn: brain synthesis failed ({e}); using deterministic review")
    return _deterministic_review(record)


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


def _append_lesson(entry: dict):
    """Persist one lessons.jsonl row. Nightly and coach rows UPSERT by
    (session, kind): re-running an already-reviewed session (a --date
    backfill, a crash-restart re-fire between send and dedup-mark) replaces
    the previous row instead of stacking a duplicate the digest would count
    twice. The first review of a session is still a pure append; a line the
    parser cannot read is preserved verbatim, never destroyed."""
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
    session has passed."""
    bullets = []
    for entry in _by_session_newest_first(_all_lessons()):
        d = entry.get("session", "")
        tag = ""
        if d:
            try:
                tag = datetime.strptime(str(d), "%Y-%m-%d").strftime("%#m/%#d"
                    if sys.platform.startswith("win") else "%-m/%-d")
            except ValueError:  # malformed legacy row: keep it, tag it as-is
                tag = str(d)
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
    header = ("These are my own observations from grading my calls night after "
              "night. Apply them when reading setups. They NEVER override the "
              "hard rules (9:45-10:30 entry window, 70% win-rate floor, sell "
              "half at +25%, give-back 40 off peak, -70% stop).\n")
    body = "\n".join(f"- ({tag}) {lesson}" for tag, lesson in bullets) or "- (none yet)"
    LESSONS_DIGEST.write_text(header + "\n" + pin + body + "\n", encoding="utf-8")


def _owner_message(record, lesson) -> str:
    lines = [f"🌙 NIGHTLY REVIEW: {record['day_name']}", ""]
    if record["market"]:
        lines += ["THE MARKET: " + record["market"], ""]
    if record["trades"]:
        lines.append("TODAY'S CALLS:")
        for t in record["trades"]:
            hd = _t(t["texted"])
            lines.append(f"{t['ticker']} {t['strike']:g} {t['direction'].upper()} "
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
    if lesson.get("proposed_change"):
        lines += ["💡 PROPOSED RULE CHANGE (needs your ok): "
                  + lesson["proposed_change"],
                  "I will not change any trade rule on my own. Reply if you want it in."]
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

    # grade today's sniper candidates (hits/misses at every target tier) so
    # the forward ledger keeps earning real win rates for bigger targets
    ledger_note = ""
    if not dry:
        try:
            import forward_ledger
            graded = forward_ledger.fill_outcomes()
            ledger_note = forward_ledger.nightly_summary()
            if graded:
                print(f"learn: graded {graded} sniper candidate(s) forward")
        except Exception as e:
            print(f"learn: sniper forward grading skipped ({e})")

    # the coach: a second agent that reviews the WHOLE day (trades, sniper
    # candidates, news, skips), reconstructs the perfect scenario for every
    # imperfection, and feeds the lesson back into the brain's playbook.
    # Runs AFTER the ledger grading so it sees today's graded outcomes.
    coach_note = ""
    if not dry:
        try:
            import coach
            coach_note = coach.reflect(session).get("summary", "")
        except Exception as e:
            print(f"learn: coach session skipped ({e})")

    record = grade_day(session)
    lesson = synthesize(record)
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
    msg = _owner_message(record, lesson)
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


def main():
    dry = "--dry-run" in sys.argv
    date = None
    if "--date" in sys.argv:
        i = sys.argv.index("--date")
        if i + 1 < len(sys.argv):
            date = sys.argv[i + 1]
    run(require_date=date, dry=dry)


if __name__ == "__main__":
    main()
