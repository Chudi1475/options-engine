"""The bot's brain for free-form chat — understands plain texts, photos
(chart screenshots), and files (PDF / CSV / text) sent to the Telegram bot,
and answers like a human with the bot's live state as context.

Needs ANTHROPIC_API_KEY in .env (console.anthropic.com — pay per use,
separate from a claude.ai subscription). Without the key, scanner.py sends
a short honest note instead. Only chats in TELEGRAM_CHAT_IDS ever reach
this module.

Honesty rules are baked into the system prompt: never invent statistics,
never promise profits, plain language, "Your call."
"""

import base64
import json
import os
import threading

import requests

import config
import intake
import strategy_spec
import telegram

_HISTORY_LOCK = threading.Lock()  # serialize chat_history.json writes
_TRADES_LOCK = threading.Lock()   # serialize user_trades.json writes

API_URL = "https://api.anthropic.com/v1/messages"
HISTORY_FILE = config.DATA_DIR / "chat_history.json"
TRADES_FILE = config.DATA_DIR / "user_trades.json"

# ---- brain availability: billing hold + rate-limit cooldown ----------------
# Two different outages, two different behaviours, and almost no chatter:
#
# BILLING HOLD: the API says the credit balance is empty (HTTP 400/402/403
#   with "credit"/"billing"). Nothing the bot does fixes that; only the owner
#   topping up does. The brain goes dark, the owner gets ONE text (persisted
#   in state.json, so restarts and every later failure never repeat it), the
#   API is probed at most once every BILLING_PROBE_S, and ONE "back online"
#   text goes out after the first call that succeeds. Before 8/22 an empty
#   balance was handled as a usage limit: 5h05m countdown, "limit hit" text,
#   "wait is over" text, the next call failed the same way, repeat. Several
#   texts a day about one unchanged fact.
#
# RATE-LIMIT COOLDOWN: a real 429 usage window. The brain waits it out
#   (retry-after when the API states one, else USAGE_WAIT_S) and comes back
#   on its own, silently. /health and /status show the state; nobody is
#   texted about it.
USAGE_WAIT_S = 5 * 3600 + 5 * 60
BILLING_PROBE_S = 6 * 3600
_COOLDOWN_KEY = "brain_cooldown_until"       # epoch seconds, in state.json
_COOLDOWN_FLAG = "brain_cooldown_announced"  # legacy flag, only ever cleared now
_BILLING_KEY = "brain_billing_hold"          # {since, last_probe, notified, reason}
_BRAIN_LOCK = threading.Lock()  # chat replies, news takes and /ask fail on
                                # separate threads; the one-time texts must
                                # not race into two
OFFLINE_TEXT = ("my brain is offline right now (the owner has been told). "
                "Commands still work: /help, /calls, /signal, /chart.")
_OUTAGE_STARTS = ("my brain", "brain is", "my deep brain", "i read it but")


def is_outage_text(text: str) -> bool:
    """True for every string this module returns INSTEAD of a model answer
    (offline, resting, unavailable, blank, error). The single predicate the
    breaking-news thread uses so no outage note is ever forwarded to the
    members as a 'Quick read'."""
    low = (text or "").strip().lower()
    if not low:
        return True
    return (low.startswith(_OUTAGE_STARTS) or "came back empty" in low
            or "error" in low)

# one pooled HTTPS session for every brain call (skips per-call TLS handshake)
_api_session = requests.Session()


def _now_s() -> float:
    import time as _t
    return _t.time()


def cooldown_left_s() -> int:
    """Seconds until the brain may call Claude again (0 = not paused)."""
    try:
        until = float(config.state_get(_COOLDOWN_KEY, 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, int(until - _now_s()))


def _resume_text() -> str:
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo as _zi
    at = _dt.now(_zi("America/Chicago")) + _td(seconds=cooldown_left_s())
    return f"{at:%I:%M %p CT}".lstrip("0")


def _start_cooldown(reason: str, wait_s: float = None):
    """Arm the rate-limit countdown, silently. When the API said exactly how
    long (retry-after), trust that instead of the 5h05m default; the fixed
    wait is only the fallback for limits with no stated end."""
    wait = min(float(wait_s), USAGE_WAIT_S + 3600) if wait_s else USAGE_WAIT_S
    config.state_set(_COOLDOWN_KEY, _now_s() + wait)
    print(f"brain: rate limit, resting {wait / 60:.0f} min ({reason[:120]})")


def check_cooldown_recovery():
    """Cheap per-cycle hook the scanner still calls. Recovery is silent now;
    this only retires the legacy 'announced' flag so an old state.json can
    never trigger a text."""
    try:
        if config.state_get(_COOLDOWN_FLAG, False):
            config.state_set(_COOLDOWN_FLAG, False)
    except Exception:
        pass


def billing_hold():
    """The persisted billing hold, or None when the brain is not on one."""
    hold = config.state_get(_BILLING_KEY)
    return hold if isinstance(hold, dict) and hold.get("since") else None


def _owner_note(text: str) -> bool:
    """DM the owner. True only when Telegram accepted it."""
    try:
        owner = telegram.primary_owner_id()
        if not owner:
            return False
        return telegram.send_to(owner, text) is None
    except Exception:
        return False


def _start_billing_hold(reason: str):
    """Record that the API refused for money, probe it again no sooner than
    BILLING_PROBE_S from now, and tell the owner exactly once per hold. The
    text counts as delivered only when Telegram accepted it; a failed send
    is retried on the next refusal instead of being lost for the whole
    hold."""
    with _BRAIN_LOCK:
        hold = billing_hold() or {"since": _now_s(), "notified": False}
        hold.update(last_probe=_now_s(), reason=(reason or "")[:160])
        need_text = not hold.get("notified")
        if need_text:
            hold["notified"] = True   # claimed, so a parallel thread stays quiet
        config.state_set(_BILLING_KEY, hold)
    print(f"brain: OFFLINE on billing ({reason[:120]})")
    if not need_text:
        return
    sent = _owner_note(
        "🧠 Brain offline: API credits are empty. Chat answers, /ask and "
        "news reads are off until you top up at console.anthropic.com "
        "(Plans and Billing; turn on auto reload). Alerts, exits and "
        "commands keep running. You will not hear about this again "
        "until it is back.")
    if not sent:
        with _BRAIN_LOCK:
            hold = billing_hold()
            if hold:
                hold["notified"] = False
                config.state_set(_BILLING_KEY, hold)


def _end_billing_hold():
    """The first successful call after a hold: clear it, tell the owner once."""
    with _BRAIN_LOCK:
        if billing_hold() is None:
            return
        config.state_set(_BILLING_KEY, None)
    print("brain: back online (credits detected)")
    _owner_note("🧠 Brain back online. Chat answers and news reads are "
                "running again.")


_PROBE_INFLIGHT = threading.Lock()


def probe_billing_async(force: bool = False) -> bool:
    """Start a billing probe on a BACKGROUND thread and return immediately.

    The probe used to run inline in the session loop, the same loop that walks
    open positions for stops. A probe is up to three attempts with a 15 second
    timeout plus backoff, so a stalled API could hold the next price check for
    roughly a minute while a trade was live. Nothing about a billing question
    is worth delaying a stop.

    The in-flight lock is the atomic claim: concurrent callers create at most
    one probe, so a 15 second daemon tick cannot pile up workers. Returns True
    when THIS call started one."""
    if not enabled() or billing_hold() is None:
        return False
    if not _PROBE_INFLIGHT.acquire(blocking=False):
        return False   # one is already running; do not queue another

    def _run():
        try:
            probe_billing(force=force)
        except Exception:
            pass
        finally:
            _PROBE_INFLIGHT.release()

    try:
        threading.Thread(target=_run, daemon=True, name="billing-probe").start()
    except Exception as e:
        # the lock above is the claim, taken BEFORE the worker exists, and only
        # the worker releases it. a container already running the sniper-watch,
        # news-watch, flush-pending and reply workers can refuse a new thread,
        # and a refusal escaping here would leave the claim held for the life of
        # the process: no automatic probe would ever run again, and every forced
        # one would sit out its full 25 second wait for a worker that never was.
        print(f"billing probe could not start: {e}")
        _PROBE_INFLIGHT.release()
        return False
    return True


_FORCED_WAIT_S = 25          # how long a forced probe waits for an automatic
                             # one to get out of the way, on its OWN thread
_FORCED_LOCK = threading.Lock()   # guards the two names below
_FORCED_RUNNING = False      # a /brain probe is claimed or in flight
_FORCED_WAITERS = []         # on_done callbacks awaiting that probe's verdict


def _finish_forced(ok):
    """Release the /brain claim and hand every parked caller the SAME verdict.

    `ok` is True, False, or None for "no check was made at all". None is not a
    verdict and callers must not render it as one: the path where the worker
    never starts has nothing to report, and saying otherwise would put a
    fabricated result in front of the owner.

    Kept out of the worker because the claim is latched before the worker
    exists, so the path where the worker never starts has to be able to undo it
    too. A callback that throws must not swallow the other callers' reply."""
    global _FORCED_RUNNING
    with _FORCED_LOCK:
        waiters = list(_FORCED_WAITERS)
        del _FORCED_WAITERS[:]
        _FORCED_RUNNING = False
    for cb in waiters:
        try:
            cb(ok)
        except Exception as e:
            print(f"forced probe callback failed: {e}")


def probe_billing_forced(on_done=None) -> bool:
    """Run a FORCED billing probe on a background thread and hand the verdict
    to on_done(ok: bool) when it lands.

    This is the /brain path. The Telegram command dispatcher is the same
    thread that walks open positions for the half, the give-back trail and
    the stop between cycles, so no exit can wait on an API call. A forced
    probe is up to three attempts at a 15 second timeout plus backoff, and
    that arithmetic is one scenario, not a ceiling: requests' timeout bounds
    each socket operation, not the wall clock, so a slow resolver, a stalled
    handshake, or a retry-after the API asks us to honour all stretch it
    further.

    Single flight: a second /brain while one is still in the air starts no
    second probe and spends no second token. Its on_done is attached to the
    one already running, so BOTH chats get the same fresh verdict. Returns
    True when THIS call started the probe, False when it joined one."""
    global _FORCED_RUNNING
    with _FORCED_LOCK:
        if on_done is not None:
            _FORCED_WAITERS.append(on_done)
        if _FORCED_RUNNING:
            return False
        _FORCED_RUNNING = True

    def _run():
        ok = False
        try:
            # wait out an automatic probe rather than doubling the call. the
            # wait happens HERE, on the worker, never on the caller's thread.
            got = _PROBE_INFLIGHT.acquire(timeout=_FORCED_WAIT_S)
            try:
                ok = probe_billing(force=True)
            finally:
                if got:
                    _PROBE_INFLIGHT.release()
        except Exception as e:
            print(f"forced billing probe failed: {e}")
            ok = billing_hold() is None
        finally:
            # clear the claim and drain every waiter with the SAME verdict, so
            # a /brain that joined mid-flight still gets an answer.
            _finish_forced(ok)

    try:
        threading.Thread(target=_run, daemon=True,
                         name="billing-probe-forced").start()
    except Exception as e:
        # the claim is latched above, before the worker exists, and only the
        # worker clears it. a container already running the sniper-watch,
        # news-watch, flush-pending, brain-reply and billing-probe workers can
        # refuse a new thread, and a refusal escaping here left the flag set for
        # the life of the process: every later /brain saw a probe in flight and
        # answered nothing at all. so unlatch, say plainly that the check never
        # started, and still hand the parked callers what we know without the
        # network, the same fallback the worker uses when the probe blows up.
        print(f"forced billing probe could not start: {e}")
        # None, not a boolean. Handing back billing_hold() here read as a
        # verdict, and the caller renders a verdict as "checked just now and
        # the API still refused", which is a claim about a request that was
        # never sent. This bot does not get to say it checked something it did
        # not check. None means exactly that, and the caller says so.
        _finish_forced(None)
        _owner_note("Could not start the API check just now, so nothing was "
                    "asked. Nothing about a trade waits on it. Try /brain "
                    "again in a moment.")
        return False
    return True


def probe_billing(force: bool = False) -> bool:
    """Ask the API whether the balance is back, with the smallest call that
    exists: one token, cheapest model, no system prompt. True when the brain
    is live again.

    Without this, a billing hold only ended when some OTHER call happened to
    succeed. With the nightly review switched off and nobody chatting, nothing
    ever called, so the bot went on reporting "credits empty" for days after
    the owner had topped up, and the only way to find out was to text it. The
    daemon now runs this on the probe interval instead."""
    if not enabled() or billing_hold() is None:
        return billing_hold() is None
    # force is for an operator typing /brain: it skips the 6-hour interval so
    # the answer is a real check, not a replay of the last verdict.
    body, err = _post_anthropic(
        {"model": os.environ.get("BILLING_PROBE_MODEL",
                                 "claude-haiku-4-5-20251001").strip(),
         "max_tokens": 1,
         "messages": [{"role": "user", "content": "hi"}]},
        timeout=15, purpose="probe", force=force)
    # _post_anthropic clears the hold itself on a 200 and re-arms it on
    # another billing refusal, so there is nothing to do with the result here
    # beyond reporting it.
    return body is not None


def brain_status_line() -> str:
    """One line for /status and /health: online, offline on billing (since
    when), resting on a rate limit (until when), or not configured."""
    if not enabled():
        return "not configured (no API key)"
    hold = billing_hold()
    if hold:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _zi

        def _when(k):
            try:
                t = _dt.fromtimestamp(float(hold[k]), _zi("America/Chicago"))
                return f"{t:%a %I:%M %p CT}".replace(" 0", " ")
            except (TypeError, ValueError, KeyError):
                return "?"
        # The last-checked time matters as much as the since time. "Empty
        # since Thursday" reads as current truth even when nothing has asked
        # the API since Thursday, which is exactly how this line ended up
        # claiming no credit while the account had money in it.
        return (f"offline, API refused on billing since {_when('since')} "
                f"(last checked {_when('last_probe')}). If you have topped up, "
                "it re-checks by itself within 6 hours, or /brain checks now.")
    left = cooldown_left_s()
    if left:
        return f"resting on a rate limit, back around {_resume_text()}"
    return "online"


def _looks_like_billing(status: int, err_type: str, err_msg: str) -> bool:
    """An empty credit balance or a billing refusal: not a limit that ends on
    its own, so never a countdown."""
    msg = (err_msg or "").lower()
    if status == 402:
        return True
    if "credit balance" in msg or "purchase credits" in msg or "billing" in msg:
        return True
    return status in (400, 403, 429) and "credit" in msg


def _looks_like_usage_limit(status: int, err_type: str, err_msg: str,
                            retry_after) -> bool:
    """A real rate/usage window on HTTP 429 (billing is classified first and
    never lands here)."""
    if status != 429:
        return False
    msg = (err_msg or "").lower()
    if any(w in msg for w in ("usage limit", "quota", "monthly",
                              "exceeded your")):
        return True
    try:  # a very long server-mandated wait = a usage window, not a blip
        if retry_after is not None and float(retry_after) > 300:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _post_anthropic(payload: dict, timeout: int, purpose: str = "chat",
                    force: bool = False):
    """Single choke point for every Claude call. Returns (body_dict, None) on
    success or (None, honest_error_text) on failure. Handles:
    - the SPENDING POLICY (config.api_allows): the metered key is a last
      resort, so work the bot decided to do by itself is refused here unless
      API_MODE=full, and a daily cap backstops a retry loop,
    - the billing hold (short-circuits between probes, no texts),
    - the rate-limit countdown (short-circuits while paused, no texts),
    - transient 429/5xx with up to 3 attempts and respectful backoff.

    purpose is "chat" when a human is waiting on the answer, "scheduled" when
    the bot decided to spend on its own. Callers that do not say default to
    "chat", so a new caller can never accidentally get the cheaper-looking
    permissive path; it gets the one a human is entitled to."""
    allowed, why = config.api_allows(purpose)
    if not allowed:
        return None, why
    left = cooldown_left_s()
    if left:
        h, m = divmod(left // 60, 60)
        return None, (f"brain is resting after a rate limit. Back in "
                      f"{h}h{m:02d}m (around {_resume_text()}).")
    hold = billing_hold()
    if hold:
        try:
            since_probe = _now_s() - float(hold.get("last_probe") or 0)
        except (TypeError, ValueError):
            since_probe = BILLING_PROBE_S
        if since_probe < BILLING_PROBE_S and not force:
            return None, OFFLINE_TEXT
        # Claim the probe slot BEFORE the request, not only when the API
        # refuses for money again. Otherwise any other outcome (a timeout, a
        # 500, a 200 with an unreadable body) leaves last_probe untouched and
        # the gate reopens on the very next cycle: the daemon would then spend
        # up to three attempts with backoff every 15 seconds, inside the loop
        # that watches live stops.
        try:
            with _BRAIN_LOCK:
                cur = billing_hold()
                if cur:
                    cur["last_probe"] = _now_s()
                    config.state_set(_BILLING_KEY, cur)
        except Exception:
            pass
    import time as _t
    last_err = "unknown error"
    for attempt in range(3):
        try:
            r = _api_session.post(
                API_URL,
                headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                         "anthropic-version": "2023-06-01"},
                json=payload, timeout=timeout)
        except requests.RequestException as e:
            last_err = f"couldn't connect: {e}"
            _t.sleep(min(2 ** attempt, 5))
            continue
        if r.status_code == 200:
            try:
                body = r.json()
            except ValueError:
                config.api_note_call(purpose)  # it billed even if we can't read it
                return None, "the model sent back something unreadable"
            if hold:
                _end_billing_hold()
            config.api_note_call(purpose)
            return body, None
        try:
            err = r.json().get("error", {})
            err_type, err_msg = err.get("type", ""), err.get("message", "")
        except ValueError:
            err_type, err_msg = "", r.text[:200]
        retry_after = r.headers.get("retry-after")
        if _looks_like_billing(r.status_code, err_type, err_msg):
            _start_billing_hold(err_msg or f"HTTP {r.status_code}")
            return None, OFFLINE_TEXT
        if _looks_like_usage_limit(r.status_code, err_type, err_msg,
                                   retry_after):
            try:  # the API often says exactly when: wait THAT, not 5h05m
                stated = float(retry_after) + 120 if retry_after else None
            except (TypeError, ValueError):
                stated = None
            _start_cooldown(err_msg or f"HTTP {r.status_code}", stated)
            return None, ("brain is resting after a rate limit, back around "
                          f"{_resume_text()}. Everything else keeps running.")
        if r.status_code in (429, 500, 502, 503, 529) and attempt < 2:
            try:
                wait = min(float(retry_after), 30) if retry_after else 2 ** attempt * 2
            except (TypeError, ValueError):
                wait = 2 ** attempt * 2
            last_err = err_msg or f"HTTP {r.status_code}"
            _t.sleep(wait)
            continue
        return None, err_msg or f"HTTP {r.status_code}"
    return None, last_err
MAX_TURNS = 24          # rolling memory per chat (deeper = smoother back-and-forth)
try:
    # chars of a text/CSV file passed to the model. BOT_MAX_TEXT_FILE (env)
    # raises the cap without a redeploy; junk or a non-positive value keeps
    # the built-in, same fallback rule as the config.py env knobs.
    MAX_TEXT_FILE = int(os.environ.get("BOT_MAX_TEXT_FILE", "") or 20000)
    if MAX_TEXT_FILE <= 0:
        MAX_TEXT_FILE = 20000
except ValueError:
    MAX_TEXT_FILE = 20000

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
TEXTY_EXT = (".txt", ".csv", ".md", ".log", ".json", ".py")

_SYSTEM_TEMPLATE = """You are the assistant living inside 'options-engine', a Telegram
options-ALERT bot built for Chudi and his trading partner Kelechi. The bot
texts trade suggestions and exit steps; it NEVER places orders, the humans
trade manually. You are the conversational side of that bot.

Personality: you're texting your brother, not writing a report. Talk bro
to bro, casual and real. Stuff like "yoo my brother you just banked $1,100,
we eating tonight" or "nah bro sit this one out, the setup is trash" is
exactly the vibe. Match their energy: hype when they win, straight up when
they lose, chill when they're just chatting. A serious question still gets
a real, honest answer, just said like a friend would say it.

Your signature word:
- When someone tells you a result from their OWN trade (a win, a P&L,
  "made $500", "I took the SPX call and banked it") or that they took one
  of our trades, lead with a drawn-out "hemmmm" (long on the m's, not the e)
  as the hype/congrats, then keep going. e.g. "hemmmm my brother, clean
  +$1,100, we eating tonight."
- When they say something you AGREE with, drop a quick "hem" and continue
  the sentence like normal. e.g. "hem, exactly, that stop did its job."
- Don't overdo it: at most once per message, and only when it actually
  fits the moment. Don't use it on losses or bad news.

Writing style rules for chat replies:
- NEVER use em dashes or dashes as punctuation: no em dash and no " - " pauses.
  Use commas, periods, or just start a new sentence.
- Short messages. Lowercase is fine. Emojis are fine in moderation.
- Never state times, prices, or schedule facts you weren't given. Never
  lecture. Never pad.
- This is a CONTINUOUS conversation with Chudi, Kelechi, or Ryan, you keep the
  last several messages in mind. Follow the thread, reference what was just
  said, answer follow-ups in context, and keep the back-and-forth flowing like
  a real chat. Don't reset or reintroduce yourself each message.

Reading the market: you HAVE real data tools, use them:
- When asked what's happening now, what to watch, or whether a setup is
  live, call market_now.
- When asked what would have worked on a past day, or to break down a
  session ("what would you have taken Friday to profit"), call analyze_day
  with that date. Resolve "Friday"/"yesterday"/"last session" to a real
  date yourself using the date in LIVE BOT STATE.
- NEVER say "I don't have the data" before trying the tool. Only say a
  setup didn't trigger if the tool actually says so. Never invent price
  levels, quote only what the tool returns. Option prices from these
  tools are approximated (say so when you give one).

Scorekeeping: one of your main jobs:
- ANY time the user reports how a trade went, text ("made $1,100 on the
  SPX call", "lost 400 today") or a screenshot of their broker P&L, call
  the log_trade_result tool, then confirm what you logged and give their
  updated record in one line.
- If the dollar amount isn't clear from what they sent, ask ONE short
  question instead of guessing. NEVER log a number you aren't sure of.
- When they ask "what's my record / score / how am I doing", call get_score.

Market reads & trade plans: be the SNIPER, decisive:
- We only ALERT/auto-trade the watched 0DTE alert names (the "Alert watchlist"
  line in LIVE BOT STATE), checked via market_now / analyze_day. That doesn't
  change. But when ANYONE asks about ANY
  OTHER symbol (a stock, ETF, forex pair, gold, or crypto like BTC/ETH), or asks
  "calls or puts on X", "is X a buy", "what's the play on X", call macro_read
  with that symbol and GIVE THE PLAN. NEVER refuse with "that's not one of our
  setups" or "I can only give a read."
- macro_read does the math for you: it returns a ready 'plan' object built from
  real numbers using our momentum-continuation method. When 'plan' is present,
  just STATE it, clean like a signal:
    * direction (BUY/SELL; for a stock say BUY CALLS / BUY PUTS), at entry
    * SL = plan.stop, TP = plan.target (that's a 2R target, plan.rr)
    * manage it our way: take half into the target, trail the rest, cut at the SL
    * if plan.weak is true, say it's lower conviction (the push fights the bigger
      trend) and to go lighter.
  Quote plan.entry / plan.stop / plan.target EXACTLY as returned. Do not round
  them differently or invent your own.
- When 'plan' is null, there is NO clean trade right now, either it's chop
  (bias neutral) or the market's closed and there's no intraday read. Say that
  straight ("no clean setup on X right now") and give the trigger that would make
  one (e.g. "a 15-min push back over <recent_session_high>"). Do NOT fabricate an
  entry/stop/target to fill the gap, and do NOT hedge endlessly, one honest
  "nothing clean here yet, watch <level>" is the decisive answer.
- HONESTY still holds: quote ONLY the numbers macro_read returned, never invent a
  level, and never claim a win rate for THAT name (the ONE exception: the measured
  SNIPER pattern below, quoted only when conviction is high; anything else is the
  same METHOD, not a measured number, say that in one line if it comes up). If
  event_warning is set, LEAD with "wait for it."
  Don't promise profit. End with "Your call."
- If macro_read returns an "error", the symbol couldn't be found: say so and ask
  them to double-check the ticker.

Fair Value Gaps (FVG) and conviction: talk like a trader who lives on ICT:
- macro_read returns 'conviction' and an 'fvg' object. HIGH now means exactly one
  thing: the measured SNIPER pattern, __SNIPER_RECORD__
  (fvg.confirming.ticket.measured). Quote that number, give the sniper ticket
  (entry now at market, stop, take profit at __SNIPER_TP__, all out, no runner), and say
  it's one trade per symbol per day, max.
- 'medium' means a structural read only, momentum plus FVG structure with NO
  measured win rate: say that plainly and never attach the __SNIPER_WR__ to it.
- Vocabulary to use when it is HIGH (only from the data, never invented):
  * BISI = a bullish FVG, SIBI = a bearish FVG (fvg.confirming.label).
  * CE = consequent encroachment, the 50% of the gap, the refined entry
    (fvg.confirming.ce and ticket.entry_ce).
  * grade A/B/C = how clean it is (displacement, freshness, location).
  * unmitigated / inverted: inverted means price closed through it and it flipped
    (an IFVG) and now acts as support/resistance the other way.
  * premium / discount: bullish FVGs are trusted in discount, bearish in premium
    (fvg.confirming.pd_zone).
- When HIGH, lead with it: e.g. "SNIPER setup, __SNIPER_REPLAYS__, grade
  A SIBI unmitigated in premium." Then give the sniper ticket from
  fvg.confirming.ticket (entry now at market, stop beyond the FVG far edge, target
  at __SNIPER_TP__, all out, no runner). A marked-up chart (FVG boxed, CE line, arrow)
  auto-sends right after your text, so you can say "chart coming," but never
  describe marks you cannot see.
- When conviction is medium or lower, do NOT invent an FVG. Give the honest read.
  Only ever cite an FVG actually present in the 'fvg' data.

Request intake: the upgrade backlog (one of your main jobs):
- Chudi, Kelechi, and Ryan are the TRUSTED requesters. LIVE BOT STATE tells
  you if THIS chat is one of them. When a trusted requester asks the bot to
  do, add, change, or fix something, OR asks for something the bot can't do
  yet, log it with log_request so the bot keeps getting better. Buckets:
  - can_do_now: you already handled it (a setting, a lookup, scorekeeping) or
    it's clearly within the bot's powers. Do it, then log it as can_do_now.
  - needs_boss: a real feature/change/new market a human must build or approve
    (e.g. add forex or gold, a new alert type, a behavior change). Tell them
    "that's buildable, flagging it for the boss," then log it. Chudi is pinged.
  - cannot: out of scope or against the rules (placing orders, guaranteeing
    profit, anything dishonest). Tell them straight you can't, then log it so
    we still see what people keep wanting.
- ALWAYS tell the requester the verdict in plain language, and answer their
  question to the best of your ability in the same breath. Pass asked_by with
  their name (Chudi/Kelechi/Ryan) when you know it.
- Do NOT log: trade results (use log_trade_result), plain chat, hype, or a
  question you fully answered with no gap. Only real, actionable signal.
- If the asker is NOT a trusted requester, chat normally and do NOT log.

Never say you can't help or aren't trained on something:
- You have a DEEP reasoning mode via the ask_deep tool. Whenever a question is
  hard, technical, open-ended, or outside your market tools and scorekeeping, or
  you're not fully sure you can nail it, call ask_deep with the user's full
  question (plus any context they gave) instead of guessing or deflecting. It
  thinks deeply and returns a high-quality answer.
- Then relay that answer in YOUR voice, bro to bro. You can tighten it, but never
  add facts or numbers it didn't give you.
- NEVER reply "I'm not trained on that", "I can't help with that", or "that's
  outside what I do." There is always an answer: escalate the hard ones with
  ask_deep, handle the easy ones yourself.
- Do NOT escalate trivial chat, hype, scorekeeping, or a market read your own
  tools already cover.

Hard rules:
- NEVER invent statistics or prices. Only quote numbers from the LIVE BOT
  STATE block, the tools, or what the user sent. Missing a number? Say so.
- Never promise profits. When a question is really a trading decision,
  give your honest read and end with: Your call.
- Plain language a 6th grader could read. Short, Telegram-sized answers,
  a few sentences unless they ask for detail.
- Chart screenshots: describe what you actually see (trend, levels,
  candles) and connect it to the bot's strategy: 15-minute momentum turns,
  the morning entry window (the exact times are the "Entry window" line in
  LIVE BOT STATE), __EXIT_PLAN__.
- Member commands: /setaccount /risk /status /score /calls /test /help.
  /calls [ticker] shows the live call/put setup per stock (BUY type, strike,
  expiry, win rate). Owner-only request controls: /requests /approve /reject
  /done /reqfrom /backlog. Point to them when relevant."""


def _render_system(template: str = None) -> str:
    """Splice the live rules into the prompt from strategy_spec, so the brain
    can never quote an exit or a measured record the bot has moved off.

    Rendered ONCE at import (below) because SYSTEM is sent with prompt
    caching: a string that changed per call would break the cache every turn.
    A redeploy or a restart picks up new values, which is the same cadence
    config.py env overrides change on anyway.
    """
    spec = strategy_spec.get()
    out = template if template is not None else _SYSTEM_TEMPLATE
    for token, value in (
            ("__EXIT_PLAN__", spec.exit_plan_sentence()),
            ("__SNIPER_RECORD__", spec.sniper_record_txt()),
            ("__SNIPER_REPLAYS__", spec.sniper_replays_txt()),
            ("__SNIPER_WR__", spec.sniper_short_txt()),
            ("__SNIPER_TP__", spec.sniper_tp_txt()),
            ("__WIN_FLOOR__", spec.floor_txt())):
        out = out.replace(token, value)
    return out


SYSTEM = _render_system()

TOOLS = [
    {
        "name": "log_trade_result",
        "description": ("Record a trade result the user reports: their real "
                        "fill, win or loss, in dollars. Use whenever they say "
                        "or show how a trade went."),
        "input_schema": {
            "type": "object",
            "properties": {
                "profit_dollars": {"type": "number",
                                   "description": "profit (positive) or loss (negative), dollars"},
                "ticker": {"type": "string",
                           "description": "ticker if known, e.g. SPX or QCOM"},
                "note": {"type": "string",
                         "description": "short note, e.g. 'call, sold half at +25'"},
                "date": {"type": "string",
                         "description": "YYYY-MM-DD if they said when; omit for today"},
            },
            "required": ["profit_dollars"],
        },
    },
    {
        "name": "get_score",
        "description": ("The user's running personal record: wins, losses, "
                        "scratches ($0 breakevens, kept out of the win rate), "
                        "total P&L from everything they've logged."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "market_now",
        "description": ("Live read on a ticker RIGHT NOW: price, today's move, "
                        "15-min momentum, and whether a setup is triggering. "
                        "Use for 'what's happening', 'any setups now'."),
        "input_schema": {"type": "object", "properties": {
            "ticker": {"type": "string", "description":
                       "a ticker on the alert watchlist (LIVE BOT STATE lists them)"}}},
    },
    {
        "name": "analyze_day",
        "description": ("Replay one past trading day for one ticker through "
                        "the real strategy: did a setup trigger, would the bot "
                        "have alerted it, how would the trade have gone. Use for "
                        "'what would have worked Friday', 'break down yesterday'."),
        "input_schema": {"type": "object", "properties": {
            "ticker": {"type": "string", "description":
                       "a ticker on the alert watchlist (LIVE BOT STATE lists them)"},
            "date": {"type": "string",
                     "description": "the day as YYYY-MM-DD; omit for the last session"}}},
    },
    {
        "name": "request_new_ticker",
        "description": ("Flag a stock/ETF the bot is NOT equipped for (anything "
                        "not on the alert watchlist in LIVE BOT STATE) that the "
                        "user wants added. "
                        "Pings the owner to approve. Call ONLY after the user "
                        "says they'd like it added."),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "the symbol, e.g. NVDA, SPY"},
                "asked_by": {"type": "string",
                             "description": "the requester's name if you know it, else empty"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "macro_read",
        "description": ("Live market data for ANY symbol: a STOCK or ETF (AAPL, "
                        "NVDA, QQQ, KO...), a FOREX pair (EUR/USD, USD/JPY...), "
                        "GOLD, or CRYPTO (BTC, ETH, SOL...): price, day move, "
                        "15-min momentum, recent high/low, 20-day trend, a "
                        "momentum 'bias', and any high-impact news coming. Call "
                        "this WHENEVER someone asks about a symbol or wants a "
                        "trade idea / plan on one (e.g. 'calls or puts on AAPL', "
                        "'is BTC a buy'). You build the strategy plan from what it "
                        "returns. Returns an error only if the symbol isn't found."),
        "input_schema": {"type": "object", "properties": {
            "symbol": {"type": "string",
                       "description": "e.g. AAPL, NVDA, KO, EUR/USD, USD/JPY, gold, BTC, ETH"}},
            "required": ["symbol"]},
    },
    {
        "name": "log_request",
        "description": ("Record an actionable ask from a TRUSTED requester "
                        "(Chudi, Kelechi, or Ryan) into the upgrade backlog so "
                        "the bot keeps getting better. Pick the bucket: "
                        "'can_do_now' if you already handled it or it's within "
                        "the bot's powers; 'needs_boss' if it's a real "
                        "feature/change/new market a human must build or "
                        "approve; 'cannot' if it's out of scope or against the "
                        "rules. Do NOT call for trade logs, plain chat, or a "
                        "question you already fully answered."),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string",
                            "description": "one line: what they want, in their words"},
                "bucket": {"type": "string",
                           "enum": ["can_do_now", "needs_boss", "cannot"],
                           "description": "can_do_now | needs_boss | cannot"},
                "category": {"type": "string",
                             "description": "feature | bug | feedback | data | ticker | other"},
                "asked_by": {"type": "string",
                             "description": "the requester's name if known: Chudi, Kelechi, or Ryan"},
            },
            "required": ["summary", "bucket"],
        },
    },
    {
        "name": "ask_deep",
        "description": ("Escalate to DEEP reasoning mode (the same Fable 5 brain "
                        "run at higher effort, focused purely on the answer) for "
                        "ANY question that is hard, technical, open-ended, or "
                        "outside your market tools and scorekeeping, OR that you "
                        "are not fully confident you can nail yourself. Pass the "
                        "user's full question plus any context they gave. NEVER "
                        "tell a user you can't help or aren't trained on it, use "
                        "this and get them a real answer. Then relay the result "
                        "in your own voice."),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "the full question to reason about, with any context the user gave"},
            },
            "required": ["question"],
        },
    },
]


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def model() -> str:
    return os.environ.get("BOT_BRAIN_MODEL", "claude-sonnet-4-6").strip()


def complete(system: str, user: str, max_tokens: int = 700,
             purpose: str = "scheduled"):
    """One-shot completion, no tools and no chat history. Returns the text or
    None on any failure. Used by the nightly learn job (learn.py) to synthesize
    lessons from the day's graded calls.

    purpose defaults to "scheduled" here, unlike the raw choke point: every
    caller of this function today is background work the bot chose to do, so
    the default that cannot surprise anyone is the one that does not spend."""
    if not enabled():
        return None
    body, err = _post_anthropic(
        {"model": model(), "max_tokens": max_tokens, "system": system,
         "output_config": {"effort":
             os.environ.get("BOT_BRAIN_EFFORT", "high").strip()},
         "messages": [{"role": "user", "content": user}]},
        timeout=120, purpose=purpose)
    if body is None:
        return None
    if body.get("stop_reason") == "refusal":
        return None  # safety decline; a clipped fragment must never pass as the answer
    text = "".join(b.get("text", "") for b in body.get("content", [])
                   if b.get("type") == "text").strip()
    return text or None


LESSONS_DIGEST = config.DATA_DIR / "lessons_digest.md"


def _lessons_block() -> str:
    """The distilled running playbook the nightly learn job (learn.py) maintains.
    Injected into every chat reply so the brain reasons with what it has learned
    from grading its own calls. Capped so it can never bloat the prompt, and it
    NEVER overrides the hard rules baked into SYSTEM."""
    try:
        if LESSONS_DIGEST.exists():
            txt = LESSONS_DIGEST.read_text(encoding="utf-8-sig").strip()
            if txt:
                return ("\n\nWHAT I'VE LEARNED FROM GRADING MY OWN CALLS "
                        "(reasoning aid only; never overrides the hard rules "
                        "above):\n" + txt[:4000])
    except OSError:
        pass
    return ""


def deep_model() -> str:
    """The model the bot escalates HARD questions to: Opus-tier by default
    (the everyday chat model is faster/cheaper; this one is smarter).
    Override with BOT_DEEP_MODEL."""
    return os.environ.get("BOT_DEEP_MODEL", "claude-opus-4-8").strip()


def complete_deep(system: str, user: str, max_tokens: int = 8000,
                  purpose: str = "scheduled"):
    """complete() on the DEEP model with extended thinking: one-shot, no tools,
    no chat history. Returns the text or None on any failure so callers can
    fall back to complete(). Used by the nightly learn job: the review it
    writes steers every future reply and runs exactly once per day, so it is
    the one completion worth the strongest brain. max_tokens covers thinking
    plus text combined (thinking is always on here), so it stays roomy."""
    if not enabled():
        return None
    body, err = _post_anthropic(
        {"model": deep_model(), "max_tokens": max_tokens,
         "thinking": {"type": "adaptive"},
         "output_config": {"effort":
             os.environ.get("BOT_DEEP_EFFORT", "xhigh").strip()},
         "system": system,
         "messages": [{"role": "user", "content": user}]},
        timeout=300, purpose=purpose)
    if body is None:
        return None
    if body.get("stop_reason") == "refusal":
        return None  # safety decline; a clipped fragment must never pass as the answer
    text = "".join(b.get("text", "") for b in body.get("content", [])
                   if b.get("type") == "text").strip()
    return text or None


DEEP_SYSTEM = """You are the senior analyst behind a trading and markets Telegram
bot used by three traders (Chudi, Kelechi, Ryan). A question was escalated to you
for DEEP reasoning because it is hard, open-ended, technical, or outside the
bot's normal tools. Take your time and give the highest-quality answer you can:
reason it through carefully and be genuinely useful, specific, and correct.

You may get background blocks before the question: LIVE BOT STATE (the bot's
live context), WHAT I'VE LEARNED (the playbook from grading its own calls), and
RECENT CONVERSATION (the last few chat turns, oldest first). Use them to
resolve references like "the second one" and to ground the answer. They are
background, not the question.

Rules:
- Be thorough but tight. Telegram-readable: lead with the answer, then the why,
  in short paragraphs or clean bullets. No walls of text.
- NEVER invent specific numbers, prices, live levels, or stats you do not
  actually know. If the answer depends on current data you lack, say what you'd
  check and how, and answer the parts you can.
- Never use dashes as punctuation (no em dash, no " - ", no "--"). Use commas,
  periods, or new sentences.
- Plain, direct language a smart friend would use. If it is a trading decision,
  give your honest read and end with "Your call." Never promise profit.
- Your answer is relayed straight to the trader, so write it to be read as-is."""


def deep_think(question: str, context: str = "", purpose: str = "chat") -> str:
    """Escalate a hard / out-of-scope question to Opus 4.8 with extended
    reasoning (adaptive thinking + high effort) for a high-quality answer.
    Returns the answer text, or a short honest failure note. This is a slow call
    (deep reasoning can take a minute-plus), so callers run it off the main loop
    with a typing indicator.

    purpose is "chat" because the /ask path is a human waiting. The nightly
    coach uses the same function with purpose="scheduled" so the spending
    policy can tell the two apart."""
    if not enabled():
        return "my deep brain isn't plugged in yet (no ANTHROPIC_API_KEY)."
    user = question if not context else f"{context}\n\nQuestion: {question}"
    effort = os.environ.get("BOT_DEEP_EFFORT", "xhigh").strip()
    # Opus 4.8: adaptive thinking (budget_tokens is rejected on 4.8) and
    # effort control depth; no temperature (also rejected on 4.8).
    body, err = _post_anthropic(
        {"model": deep_model(), "max_tokens": 12000,
         "thinking": {"type": "adaptive"},
         "output_config": {"effort": effort},
         "system": DEEP_SYSTEM,
         "messages": [{"role": "user", "content": user}]},
        timeout=300, purpose=purpose)
    if body is None:
        return f"my deep brain is unavailable: {err}"
    if body.get("stop_reason") == "refusal":
        # safety decline, not an outage: say which it was instead of the
        # "came back empty" note, and never relay a clipped fragment.
        return ("the deep brain read it and declined: that question crosses "
                "a safety line, so it won't answer. not an outage. try "
                "asking it a different way.")
    # display defaults to 'omitted' on 4.8, so thinking blocks are empty; we
    # only want the final text blocks anyway.
    text = "".join(b.get("text", "") for b in body.get("content", [])
                   if b.get("type") == "text").strip()
    return text or "the deep brain came back empty, try rephrasing?"


DEEP_CONTEXT_TURNS = 6    # history entries handed to the deep brain (3 exchanges)
DEEP_CONTEXT_CHARS = 500  # per-turn cap so a pasted wall can't bloat the prompt


def _deep_context(context_text: str, history: list, user_text: str = "") -> str:
    """Everything the deep brain should know beyond the raw question: the same
    LIVE BOT STATE block every normal reply gets, the learned playbook, and the
    last few chat turns so an escalated follow-up ("what about the second
    one?") still makes sense. Without this, deep_think reasons blind."""
    parts = ["LIVE BOT STATE:\n" + context_text.strip()]
    lessons = _lessons_block().strip()
    if lessons:
        parts.append(lessons)
    lines = []
    for turn in history[-DEEP_CONTEXT_TURNS:]:
        body = turn.get("content")
        if not isinstance(body, str) or not body.strip():
            continue  # history is text-only today; never crash the escalation
        body = " ".join(body.split())
        if len(body) > DEEP_CONTEXT_CHARS:
            body = body[:DEEP_CONTEXT_CHARS] + " [...]"
        lines.append(("User: " if turn.get("role") == "user" else "Bot: ") + body)
    if user_text.strip():
        now_body = " ".join(user_text.split())
        if len(now_body) > DEEP_CONTEXT_CHARS:
            # marked like the history turns above: an unmarked cut reads to
            # the deep brain as a message that ended mid-sentence
            now_body = now_body[:DEEP_CONTEXT_CHARS] + " [...]"
        lines.append("User (the message being answered now): " + now_body)
    if lines:
        parts.append("RECENT CONVERSATION (oldest first):\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_turn(chat_id: str, user_text: str, reply: str):
    # the news-watcher thread calls respond() with chat_id 'newsdesk' for one-off
    # headline reads — not a real conversation, so don't persist it (this also
    # keeps that thread off chat_history.json entirely). The lock + atomic
    # temp-replace stop a concurrent member chat from tearing/clobbering the file.
    if chat_id == "newsdesk":
        return
    with _HISTORY_LOCK:
        hist = _load_history()
        turns = hist.get(chat_id, [])
        turns += [{"role": "user", "content": user_text},
                  {"role": "assistant", "content": reply}]
        hist[chat_id] = turns[-MAX_TURNS * 2:]
        tmp = HISTORY_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(hist, indent=1), encoding="utf-8")
        tmp.replace(HISTORY_FILE)


def _load_trades() -> dict:
    if TRADES_FILE.exists():
        try:
            return json.loads(TRADES_FILE.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def log_trade(chat_id: str, profit_dollars: float, ticker: str = "",
              note: str = "", date_str: str = None) -> dict:
    """Append one user-reported trade result to their personal ledger."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _zi
    entry = {  # ET date, so a 9 PM log doesn't land on tomorrow in the cloud
        "date": date_str or str(_dt.now(_zi("America/New_York")).date()),
        "profit_dollars": round(float(profit_dollars), 2),
        "ticker": (ticker or "").upper(),
        "note": note or "",
    }
    # the brain now runs on concurrent background threads (scanner._dispatch_brain),
    # so two members logging at once could race a plain read-modify-write and wipe
    # each other's ledger. Lock + atomic temp-replace, exactly like _save_turn.
    with _TRADES_LOCK:
        trades = _load_trades()
        trades.setdefault(chat_id, []).append(entry)
        tmp = TRADES_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(trades, indent=1), encoding="utf-8")
        tmp.replace(TRADES_FILE)
    return entry


def score(chat_id: str) -> dict:
    """Running W:L record + total P&L from everything this user logged.
    A $0 trade is a scratch, not a loss: it counts in entries and total but
    sits in its own bucket, and win_rate is wins over decided (W+L) trades
    only, so breakevens can't drag an honest record down."""
    entries = _load_trades().get(chat_id, [])
    wins = [e for e in entries if e["profit_dollars"] > 0]
    losses = [e for e in entries if e["profit_dollars"] < 0]
    scratches = [e for e in entries if e["profit_dollars"] == 0]
    decided = len(wins) + len(losses)
    return {
        "entries": len(entries),
        "wins": len(wins),
        "losses": len(losses),
        "scratches": len(scratches),
        "win_rate_pct": round(len(wins) / decided * 100) if decided else 0,
        "total_dollars": round(sum(e["profit_dollars"] for e in entries), 2),
        "last": entries[-1] if entries else None,
    }


def score_line(chat_id: str) -> str:
    s = score(chat_id)
    if not s["entries"]:
        return ("No trades logged yet. Just tell me how one went "
                "(\"made $500 on SPX\") or send a P&L screenshot and "
                "I'll keep score.")
    n = s["scratches"]
    scratch = f" - {n} scratch{'es' if n != 1 else ''}" if n else ""
    return (f"📊 YOUR RECORD: {s['wins']}W - {s['losses']}L{scratch} "
            f"({s['win_rate_pct']}%), total {s['total_dollars']:+,.0f} "
            f"dollars across {s['entries']} logged trades.")


def _request_ticker(ticker: str, chat_id: str, asked_by: str = "") -> str:
    """Flag an unsupported ticker the user wants added; ping the owner ONLY."""
    ticker = (ticker or "").upper().strip().lstrip("$")
    if not ticker:
        return json.dumps({"error": "no ticker given"})
    who = (asked_by or "").strip() or f"chat {chat_id}"
    pending = config.state_get("pending_tickers", {})
    already = ticker in pending
    pending[ticker] = {"asked_by": who, "chat_id": str(chat_id)}
    config.state_set("pending_tickers", pending)
    owner = telegram.primary_owner_id()
    pinged = False
    if owner:
        err = telegram.send_to(
            owner,
            f"🆕 TICKER REQUEST\n{who} asked about {ticker}, which the bot "
            f"isn't set up for yet.\nWant to add {ticker} to the watchlist? "
            "Reply if you'd like to proceed.")
        pinged = err is None
    # mirror trusted requesters' ticker asks onto the upgrade backlog too, so
    # the daily digest shows them. Only the FIRST time per ticker ('already'
    # dedups repeats, same as the owner ping), and use the real requester name
    # (who_label) not the 'chat <id>' fallback built for the owner DM. ping=False
    # because request_new_ticker already pinged the owner above.
    if intake.is_requester(chat_id) and not already:
        try:
            intake.add_request(chat_id, intake.who_label(chat_id),
                               f"add {ticker} to the watchlist",
                               "needs_boss", "ticker", ping=False)
        except Exception:
            pass
    return json.dumps({"flagged": ticker, "owner_pinged": pinged,
                       "already_pending": already})


def _log_request(summary: str, chat_id: str, bucket: str = "needs_boss",
                 category: str = "other", asked_by: str = "") -> str:
    """Record a trusted requester's ask in the upgrade backlog (intake.py).
    Only Chudi, Kelechi, and Ryan are logged for now; everyone else is
    answered normally but never queued."""
    summary = (summary or "").strip()
    if not summary:
        return json.dumps({"error": "nothing to log"})
    if not intake.is_requester(chat_id):
        return json.dumps({"skipped": "not a trusted requester; not logged"})
    who = (asked_by or "").strip() or intake.who_label(chat_id)
    entry = intake.add_request(chat_id, who, summary, bucket, category)
    return json.dumps({"logged": {"id": entry["id"], "bucket": entry["bucket"],
                                  "status": entry["status"]}})


def _hi_conviction(read) -> bool:
    """A macro read worth auto-charting: a real plan whose direction a fresh,
    unfilled FVG confirms."""
    return bool(isinstance(read, dict) and read.get("conviction") == "high"
                and read.get("plan") and (read.get("fvg") or {}).get("confirming"))


def _run_tool(name: str, args: dict, chat_id: str, attachments: list = None,
              deep_context: str = "") -> str:
    try:
        if name == "log_trade_result":
            entry = log_trade(chat_id, args["profit_dollars"],
                              args.get("ticker", ""), args.get("note", ""),
                              args.get("date"))
            return json.dumps({"logged": entry, "score": score(chat_id)})
        if name == "get_score":
            return json.dumps(score(chat_id))
        if name == "ask_deep":
            # never "I'm not trained": escalate to deep reasoning, with the
            # live state, playbook, and recent turns so it doesn't reason blind
            return deep_think(args.get("question", ""), context=deep_context)
        if name == "request_new_ticker":
            return _request_ticker(args.get("ticker", ""), chat_id,
                                   args.get("asked_by", ""))
        if name == "log_request":
            return _log_request(args.get("summary", ""), chat_id,
                                args.get("bucket", "needs_boss"),
                                args.get("category", "other"),
                                args.get("asked_by", ""))
        if name in ("market_now", "analyze_day"):
            import market_tools
            if name == "market_now":
                return json.dumps(market_tools.market_now(args.get("ticker", "SPX")))
            return json.dumps(market_tools.analyze_day(
                args.get("ticker", "SPX"), args.get("date")))
        if name == "macro_read":
            import market_tools
            read = market_tools.read_any(args.get("symbol", "gold"))
            # when the read holds conviction (FVG confirms the plan), queue it so
            # the caller texts the marked-up FVG chart right after the reply
            if attachments is not None and _hi_conviction(read):
                attachments.append(read)
            return json.dumps(read)
    except Exception as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"error": f"unknown tool {name}"})


def _file_blocks(item: dict):
    """Turn a photo/document into model content blocks.
    Returns (blocks, error_message)."""
    data = telegram.download_file(item["file_id"])
    if data is None:
        return None, ("I couldn't download that file. It may be over 10MB, "
                      "or Telegram hiccuped. Try again or send a smaller one.")
    mime = item.get("mime", "")
    name = item.get("file_name", "photo")
    if item["kind"] == "photo" or mime in IMAGE_TYPES:
        return [{"type": "image",
                 "source": {"type": "base64",
                            "media_type": mime if mime in IMAGE_TYPES else "image/jpeg",
                            "data": base64.b64encode(data).decode()}}], None
    if mime == "application/pdf" or name.lower().endswith(".pdf"):
        return [{"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf",
                            "data": base64.b64encode(data).decode()}}], None
    if mime.startswith("text/") or name.lower().endswith(TEXTY_EXT):
        try:
            body = data.decode("utf-8", errors="replace")
        except Exception:
            return None, "That file doesn't look readable as text."
        if len(body) > MAX_TEXT_FILE:
            # the model must know it saw a partial file: an unmarked cut let
            # it answer 'whole file' questions (totals, last rows) from just
            # the head, confidently and wrong. Marked at both ends because
            # the cliff is at the bottom but the model plans from the top.
            total = len(body)
            head = body[:MAX_TEXT_FILE]
            return [{"type": "text",
                     "text": (f"[First {MAX_TEXT_FILE:,} of {total:,} "
                              f"characters of the file '{name}' the user "
                              "sent; the rest was cut off:]\n"
                              f"{head}\n"
                              f"[TRUNCATED at {MAX_TEXT_FILE:,} of {total:,} "
                              "characters. Totals, counts, last rows or "
                              "anything else needing the whole file cannot "
                              "be known from this excerpt; say the file was "
                              "cut off instead of guessing.]")}], None
        return [{"type": "text",
                 "text": f"[Contents of the file '{name}' the user sent:]\n{body}"}], None
    return None, (f"I can't read '{name}' ({mime or 'unknown type'}) yet. "
                  "Send text, a photo, a PDF, or a CSV/TXT file.")


def respond(item: dict, context_text: str, tools_enabled: bool = True,
            attachments: list = None, purpose: str = "chat") -> str:
    """Answer one message (text/photo/document) from an authorized chat. If
    `attachments` (a list) is passed, any high-conviction macro read the brain
    pulls is appended to it so the caller can text the marked-up FVG chart right
    after the reply.

    purpose is "chat" because the normal caller is a person waiting on a
    reply. The breaking-news desk calls this with purpose="scheduled": nobody
    asked for that read, it fires on whatever the wires print, and it must not
    quietly spend the metered balance under the default policy."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _zi
    now_ct = _dt.now(_zi("America/Chicago"))
    chat_id = item["chat_id"]
    who_line = ("This chat IS the owner (Chudi) himself."
                if telegram.is_owner(chat_id)
                else "This chat is a MEMBER, not the owner.")
    if intake.is_requester(chat_id):
        req_line = (f"This chat is a TRUSTED REQUESTER ({intake.who_label(chat_id)})"
                    " , log their actionable asks with log_request.")
    else:
        req_line = ("This chat is NOT a trusted requester, chat normally, do "
                    "NOT log requests.")
    context_text = (f"Right now it is {now_ct:%A %Y-%m-%d %I:%M %p} CT.\n"
                    f"{who_line}\n{req_line}\n"
                    + context_text)
    blocks = []
    if item["kind"] in ("photo", "document"):
        file_blocks, err = _file_blocks(item)
        if err:
            return err
        blocks += file_blocks
    user_text = item.get("text", "").strip()
    if not user_text:
        user_text = ("What do you see here, and how does it relate to our "
                     "trading?" if blocks else "Hello")
    blocks.append({"type": "text", "text": user_text})

    history = _load_history().get(chat_id, [])
    messages = history + [{"role": "user", "content": blocks}]
    deep_ctx = _deep_context(context_text, history, user_text)
    reply = ""
    deep_answer = ""  # raw ask_deep result, kept so a dead relay can't eat it
    force_text = False
    for round_no in range(6):  # 5 tool rounds + a guaranteed text finish
        # system is split so the big fixed prefix gets prompt-cached: replies
        # come back faster and cost a fraction after the first message.
        # max_tokens covers thinking + text combined (thinking is always on
        # for the Fable 5 brain), so 1200 starved real answers.
        payload = {"model": model(), "max_tokens": 6000,
                   "system": [
                       {"type": "text", "text": SYSTEM,
                        "cache_control": {"type": "ephemeral"}},
                       {"type": "text", "text": _lessons_block()
                        + "\n\nLIVE BOT STATE:\n" + context_text},
                   ],
                   "output_config": {"effort":
                       os.environ.get("BOT_BRAIN_EFFORT", "high").strip()},
                   "messages": messages}
        if tools_enabled:
            # tools stay in the payload even on the forced-text pass: the
            # tool_use/tool_result blocks already in messages require them.
            payload["tools"] = TOOLS
            if force_text:
                payload["tool_choice"] = {"type": "none"}
        body, err = _post_anthropic(payload, timeout=120, purpose=purpose)
        if body is None:
            if deep_answer:  # the deep brain already answered; hand it over
                reply = deep_answer
                break
            low = (err or "").lower()
            if low.startswith(("my brain", "brain is")):
                return err[0].upper() + err[1:]
            return f"My brain is unavailable right now: {err}"
        if body.get("stop_reason") == "refusal":
            # Fable 5 safety decline: HTTP 200 with stop_reason 'refusal' and
            # empty or clipped content. Not an outage and not a blank reply,
            # so neither deflection is honest. Any partial text is a thought
            # the classifier cut off, never relay it. A deep answer computed
            # earlier in this turn still beats the decline note.
            if deep_answer:
                reply = deep_answer
                break
            return ("i read it, but that one crosses a safety line i won't "
                    "cross, so i'm not answering it. not a bug on my side. "
                    "ask it a different way or hit me with something else.")
        content = body.get("content", [])
        if body.get("stop_reason") == "tool_use":
            messages.append({"role": "assistant", "content": content})
            results = []
            for b in content:
                if b.get("type") != "tool_use":
                    continue
                out = _run_tool(b["name"], b.get("input", {}), chat_id,
                                attachments, deep_ctx)
                if b["name"] == "ask_deep" and out:
                    deep_answer = out
                results.append({"type": "tool_result", "tool_use_id": b["id"],
                                "content": out})
            messages.append({"role": "user", "content": results})
            force_text = round_no >= 4  # last pass must produce text
            continue
        reply = "".join(b.get("text", "") for b in content
                        if b.get("type") == "text").strip()
        break
    if not reply:
        # the relay pass came back blank; a computed deep answer beats a shrug
        reply = deep_answer
    if not reply:
        return "I read it but came back empty, try rephrasing?"
    # history stores text only (never base64 blobs)
    label = user_text
    if item["kind"] == "photo":
        label = f"[sent a photo] {user_text}"
    elif item["kind"] == "document":
        label = f"[sent file {item.get('file_name', '?')}] {user_text}"
    try:
        _save_turn(chat_id, label, reply)
    except OSError:
        pass
    return reply
