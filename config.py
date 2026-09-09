"""One config block for the whole bot. Every tunable lives here.

Any constant below can be overridden with an environment variable of the
same name (.env locally, dashboard variables in the cloud). Settings that
change at runtime (/setaccount, /risk override) live in state.json so they
survive restarts without touching this file.
"""

import json
import os
import threading
from pathlib import Path

import storage_io  # stdlib only, and it imports nothing from this repo, so a
                   # ledger or a lock can depend on it without dragging the
                   # transport or the data dir in at import time

REPO_DIR = Path(__file__).parent
_STATE_LOCK = threading.RLock()  # serialize state.json read-modify-write across
                                 # the main loop and the news-watcher thread


def load_env():
    """Minimal .env loader so no extra dependency is needed."""
    env_path = REPO_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# ---------------------------- CONFIG ----------------------------
TP_HALF_PCT = _f("TP_HALF_PCT", 25.0)        # sell HALF when option is +25% over entry mid
STOP_PCT = _f("STOP_PCT", -90.0)             # sell EVERYTHING at -90%. Walk-forward
                                             # validated (bt_exp_stop_walkforward):
                                             # -90 beat the old -70 on BOTH win rate
                                             # and expectancy in BOTH halves
                                             # (76.8% vs 73.2% win overall) and kept
                                             # the second half above the 70% floor.
                                             # Tradeoff, on record: deeper paper
                                             # drawdown per trade, and risk-based
                                             # sizing shrinks positions ~7/9, so
                                             # account compounding is slightly
                                             # slower. Win rate is the priority.
RUNNER_GIVEBACK_PCT = _f("RUNNER_GIVEBACK_PCT", 40.0)  # after banking half at +25%,
                                             # let the runner RUN; sell it only when
                                             # it gives back this many points from its
                                             # peak (backtest: beats the momentum-flip
                                             # trail — +~15% total return at the same
                                             # ~75% win rate, same drawdown)
RISK_PER_TRADE_PCT = _f("RISK_PER_TRADE_PCT", 1.0)    # full stop-out costs 1% of account
CORRELATED_RISK_PCT = _f("CORRELATED_RISK_PCT", 0.5)  # risk when same-direction trade already open
SPREAD_COST_PCT = _f("SPREAD_COST_PCT", 4.0)  # est. round-trip cost of crossing the spread (live stats)
MIN_WINRATE = _f("MIN_WINRATE", 70.0)        # never alert below this backtested win rate
GAP_UP_SKIP_PCT = _f("GAP_UP_SKIP_PCT", 1.0) # stand aside when SPX opens this far
                                             # above yesterday's close. Verified
                                             # regime study (bt_exp_regime_split):
                                             # gap-up>1% days won only 56.7% and
                                             # LOST money; skipping them lifted the
                                             # book to 76.7% win / +25.9%/trade and
                                             # beat baseline in walk-forward.
                                             # 0 disables the rule.
LIVE_STATS_MIN_TOTAL = 30                    # closed signals before live stats replace the backtest
LIVE_STATS_MIN_SETUP = 10                    # and at least this many for the specific setup
POLL_SECONDS = int(_f("POLL_SECONDS", 15))   # main loop cadence
NEWS_POLL_SECONDS = int(_f("NEWS_POLL_SECONDS", 12))  # breaking-news scan cadence
                                             # (own thread; lower = faster but
                                             # risks the free RSS feeds rate-limiting)
EXPIRY_WARN_MINUTES = int(_f("EXPIRY_WARN_MINUTES", 15))  # "close before expiry"
                                             # warning, minutes before 4 PM ET
                                             # (env-tunable like the other knobs)
# -----------------------------------------------------------------

# where runtime data lives — set DATA_DIR to a mounted volume in the cloud
DATA_DIR = Path(os.environ.get("DATA_DIR", str(REPO_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
POSITIONS_FILE = DATA_DIR / "positions.json"
ALERTS_LOG = DATA_DIR / "alerts.log"
ALERTS_JSONL = DATA_DIR / "alerts_sent.jsonl"
NEWS_SEEN_FILE = DATA_DIR / "news_seen.json"  # breaking-news dedup (own file so
                                              # the news thread never races state.json)


def paper_mode() -> bool:
    return os.environ.get("PAPER_MODE", "").strip().lower() in ("1", "true", "yes", "on")


def learn_enabled() -> bool:
    """Whether the nightly self-review runs. Defaults ON, so a missing env var
    behaves exactly as before; set LEARN_ENABLED=false to stop it.

    This is the single most expensive thing the bot does: learn.run reviews the
    day's calls AND deep-reviews up to 25 closed trades (learn.review_history),
    so one night is ~26 API calls whether or not anyone texted the bot.

    Alerts, exits, charts and the chat brain are unaffected. The FORWARD LEDGER
    used to be affected and the old docstring wrongly said otherwise: its
    deterministic grading sat inside learn.run, so turning this flag off also
    stopped the free evidence collection. Grading now runs as its own job
    (scanner.Service.maybe_grade_forward) and no longer depends on this flag or
    on any paid-API permission."""
    return os.environ.get("LEARN_ENABLED", "").strip().lower() \
        not in ("0", "false", "no", "off")


# ---------------------- paid-API spending policy ----------------------
# The Anthropic key this bot runs on is metered pay-per-use and is NOT the
# owner's Claude subscription. It has repeatedly run to $0 and taken the whole
# chat brain down with it. The rule from the owner is blunt: the credit is a
# last resort, so nothing the bot decides to do on its own may spend it.
#
# Every paid call therefore declares a PURPOSE and is checked here:
#   "chat"       a human texted the bot and is waiting on an answer.
#   "scheduled"  the bot decided by itself: the nightly review, the deep
#                trade-review backlog, the coach, the morning news check.
#   "probe"      the one-token ping that asks "is the balance back yet". It
#                costs a rounding error and it is the only thing that gets the
#                brain out of a billing hold on its own, so it is allowed
#                whenever the API is not switched off outright.
#
# API_MODE picks the policy:
#   "ask_only"  (default) answer humans, never spend on scheduled work.
#   "full"      scheduled work may spend too, still under the daily cap.
#   "off"       spend nothing at all; every caller takes its offline path.
#
# The scheduled work is not lost when it is refused, it moves to the desktop:
# learn.py --export-backlog / --import-reviews do the same reviews on the
# owner's own machine, where the reasoning is already paid for.
API_MODES = ("off", "ask_only", "full")
API_DEFAULT_MODE = "ask_only"
API_CALLS_KEY = "api_calls"       # state.json: {"date","chat","scheduled"}


def api_mode() -> str:
    """Current spending policy. An unset or unrecognized API_MODE means the
    safe default, never the permissive one."""
    m = os.environ.get("API_MODE", "").strip().lower()
    return m if m in API_MODES else API_DEFAULT_MODE


def api_daily_cap() -> int:
    """Hard ceiling on paid calls per ET day, counting every purpose. This is
    the backstop against a retry loop quietly draining the balance overnight,
    which has happened. 0 disables the ceiling."""
    try:
        n = int(float(os.environ.get("API_MAX_CALLS_PER_DAY", "") or 60))
    except ValueError:
        return 60
    return max(0, n)


def _et_today() -> str:
    """The ET calendar date. The container runs UTC, so anything that keys a
    daily counter off the local date rolls over mid-evening ET."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return str(datetime.now(ZoneInfo("America/New_York")).date())


def api_counts() -> dict:
    """Today's paid-call tally, zeroed on a new ET day."""
    rec = state_get(API_CALLS_KEY) or {}
    if not isinstance(rec, dict) or rec.get("date") != _et_today():
        return {"date": _et_today(), "chat": 0, "scheduled": 0, "probe": 0}
    return {"date": rec.get("date"), "chat": int(rec.get("chat") or 0),
            "scheduled": int(rec.get("scheduled") or 0),
            "probe": int(rec.get("probe") or 0)}


def api_allows(purpose: str = "chat"):
    """(allowed, reason). The reason is plain English and safe to show a human,
    because a refused scheduled job prints it and a refused chat reply says it."""
    mode = api_mode()
    if mode == "off":
        return False, "paid API is switched off (API_MODE=off)"
    if purpose == "probe":
        return True, ""   # a one-token ping, and the only route back online
    if purpose != "chat" and mode != "full":
        return False, (f"{purpose} work does not spend the metered API "
                       f"(API_MODE={mode}); run it on the desktop instead")
    cap = api_daily_cap()
    if cap and purpose != "chat":
        # The cap exists to backstop an UNATTENDED retry loop draining the
        # balance overnight. It deliberately does not apply to a person who
        # texted the bot: one chat turn can be several tool round-trips, so a
        # shared ceiling would have gone quiet on Chudi after about nine
        # messages and called it a spending limit. Probes are exempt too;
        # capping them would strand the brain offline for a whole day.
        c = api_counts()
        if c["scheduled"] >= cap:
            return False, (f"daily scheduled-call cap reached "
                           f"({c['scheduled']}/{cap}); resets at midnight ET")
    return True, ""


# Astra section 9 and gap M12: a per-purpose daily COUNT cannot attribute
# spend. One row per call at this choke point, carrying purpose, model,
# requested and actual tokens, estimated versus billed cost, latency and
# failure category. Never a prompt, never a key, never a recipient: the row
# describes the CALL, not what was said in it.
API_COST_FILE = DATA_DIR / "api_cost.jsonl"
API_COST_SCHEMA = 1


def _api_price_per_mtok(which: str):
    """USD per million tokens from the environment, or None.

    There is deliberately NO built-in price table. A hand-typed price goes
    stale silently and this repo does not carry numbers that trace to nothing,
    so an unpriced call estimates nothing and says why. The model and the
    token counts still land on the row, so a later reconciliation can price it
    from the vendor's own invoice."""
    raw = os.environ.get(f"API_PRICE_{which}_PER_MTOK", "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v >= 0 else None


def _api_cost_row(purpose, counted, model, requested_max_tokens, usage,
                  latency_ms, failure) -> dict:
    """Build one cost row. Unknown is null WITH A REASON, never zero and never
    an invented number: a row reporting 0 tokens for a call whose usage nobody
    returned is a fabricated measurement, and this log exists to be evidence."""
    from datetime import datetime, timezone
    row = {
        "schema_version": API_COST_SCHEMA,
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": str(purpose or "")[:32],
        "counted": bool(counted),
        "model": None,
        "requested_max_tokens": None,
        "input_tokens": None,
        "output_tokens": None,
        "estimated_usd": None,
        "billed_usd": None,
        # the vendor's response carries no dollar figure, so this field can
        # only be null here. It stays ON the row because Astra asked for
        # estimated VERSUS billed, and a field that silently disappears reads
        # as agreement between the two.
        "billed_reason": "the API response does not return a billed cost",
        "latency_ms": None,
        "failure_category": failure or None,
    }
    if model:
        row["model"] = str(model)[:64]
    else:
        row["model_reason"] = "the caller did not declare a model"
    if isinstance(requested_max_tokens, (int, float)) \
            and not isinstance(requested_max_tokens, bool):
        row["requested_max_tokens"] = int(requested_max_tokens)
    else:
        row["requested_reason"] = "no max_tokens was declared for this call"
    got = usage if isinstance(usage, dict) else {}
    tin, tout = got.get("input_tokens"), got.get("output_tokens")
    if isinstance(tin, (int, float)) and isinstance(tout, (int, float)):
        row["input_tokens"], row["output_tokens"] = int(tin), int(tout)
    else:
        row["tokens_reason"] = "the response returned no usage block"
    if isinstance(latency_ms, (int, float)) and not isinstance(latency_ms, bool):
        row["latency_ms"] = round(float(latency_ms), 1)
    else:
        row["latency_reason"] = "the caller did not time this call"
    p_in, p_out = _api_price_per_mtok("IN"), _api_price_per_mtok("OUT")
    if row["input_tokens"] is None:
        row["estimate_reason"] = "no token counts to price"
    elif p_in is None or p_out is None:
        row["estimate_reason"] = ("no price configured (set "
                                  "API_PRICE_IN_PER_MTOK and "
                                  "API_PRICE_OUT_PER_MTOK)")
    else:
        row["estimated_usd"] = round(
            row["input_tokens"] / 1e6 * p_in
            + row["output_tokens"] / 1e6 * p_out, 6)
        row["estimate_basis"] = f"in={p_in}/Mtok out={p_out}/Mtok"
    return row


def api_note_call(purpose: str = "chat", *, counted: bool = True,
                  model=None, requested_max_tokens=None, usage=None,
                  latency_ms=None, failure=None) -> None:
    """Count one paid call and write its cost row.

    Called after the request goes out, so a refusal never counts, and never
    raises: billing bookkeeping must not be able to break a reply.

    counted=False writes the row WITHOUT touching the daily tally. A call the
    vendor never billed (a policy refusal, a dead socket, a 500) is evidence
    worth keeping, but counting it would quietly redefine what the daily cap
    means, and that cap is a spending guard."""
    try:
        if counted:
            key = purpose if purpose in ("chat", "probe") else "scheduled"

            def bump(cur):
                rec = cur if isinstance(cur, dict) else {}
                if rec.get("date") != _et_today():
                    rec = {"date": _et_today(), "chat": 0, "scheduled": 0,
                           "probe": 0}
                rec[key] = int(rec.get(key) or 0) + 1
                return rec

            state_update(API_CALLS_KEY, bump)
    except Exception:
        pass
    try:
        # the row is an OBSERVER: a failed append must never reach the reply
        # path, and it is not durable, because an fsync per API call would put
        # a disk flush in front of a human waiting on an answer
        storage_io.append_jsonl(
            API_COST_FILE,
            _api_cost_row(purpose, counted, model, requested_max_tokens,
                          usage, latency_ms, failure))
    except Exception:
        pass


def api_usage_line() -> str:
    """One line for the owner's /health text."""
    c = api_counts()
    cap = api_daily_cap()
    used = c["chat"] + c["scheduled"]
    limit = f"/{cap}" if cap else ""
    return (f"paid API: {api_mode()}, {used}{limit} billable calls today "
            f"({c['chat']} chat, {c['scheduled']} scheduled, "
            f"{c['probe']} probe)")


class _StateUnavailable(Exception):
    """state.json exists but is momentarily unreadable (e.g. a volume hiccup).
    Raised on a WRITE path so we refuse to clobber real state with one key."""


def _quarantine_state(reason: str = "") -> None:
    """Move a corrupt state.json aside ONCE so the next write rebuilds clean
    state instead of refusing forever, and the bad file is kept for forensics."""
    bad = STATE_FILE.with_suffix(".corrupt")
    try:
        if STATE_FILE.exists() and not bad.exists():
            STATE_FILE.replace(bad)
            print(f"state.json {reason} -> moved to {bad.name}; rebuilding fresh state")
    except OSError:
        pass


_LAST_GOOD = {}  # last successfully-parsed state, served to readers over a hiccup


def state_health() -> str:
    """Whether state.json can be trusted right now, WITHOUT touching it.

    Returns "ok" (parsed, or genuinely absent, which a fresh volume is),
    "unreadable" (present and the OS will not hand it over) or "corrupt"
    (present and the bytes do not parse).

    Read only on purpose, and separate from load_state for that one reason.
    load_state QUARANTINES a corrupt file the first time it sees one, which
    resets morning_sent, recap_sent, weekly_sent, learn_sent and every other
    once a day guard in the same motion and can re-send a day of reports with
    nobody told. An ownership check has to be able to ASK the question before
    the process is allowed to send anything, without the asking being the
    thing that causes the damage. Nothing about the exit thresholds or the
    gate lives here; this is a file health probe."""
    res = storage_io.read_json(STATE_FILE)
    if res.status == "missing":
        return "ok"
    if res.status == "ok":
        return "ok" if isinstance(res.value, dict) else "corrupt"
    return res.status  # "unreadable" or "corrupt", already the right words


def load_state(strict: bool = False) -> dict:
    global _LAST_GOOD
    # storage_io tells the four answers apart: parsed, absent, present but
    # unopenable, present but unparseable. Collapsing the last two is what the
    # branches below have always been working around by hand.
    res = storage_io.read_json(STATE_FILE)
    if res.status == "ok":
        if isinstance(res.value, dict):
            _LAST_GOOD = res.value
        return res.value
    if res.status == "corrupt":
        # Corrupt/torn CONTENT won't heal itself. If we returned {} here, the
        # next state_set would persist {only_that_key} and wipe everything
        # else (requests, account_value, tg_offset, dedup keys...). Move the
        # bad file aside once so a later write rebuilds clean state.
        _quarantine_state(f"corrupt ({res.error})")
        return {}
    if res.status == "unreadable":
        # The file IS there but momentarily unreadable (mounted-volume
        # hiccup, save_state anticipates the same on its write side). On a
        # write path, refuse rather than clobber good-but-unreadable state.
        # On a read path serve the last-good snapshot (not {}), so a one-cycle
        # hiccup can't make a dedup check ("recap already sent?") re-fire.
        # NEVER quarantined: an unreadable file is not a proven bad one.
        if strict:
            raise _StateUnavailable(res.error)
        return dict(_LAST_GOOD)
    boot = os.environ.get("BOOTSTRAP_STATE", "").strip()
    if boot:  # first boot on a fresh volume: seed state (e.g. so a new cloud
        try:  # deploy doesn't re-send reports the local bot already sent)
            state = json.loads(boot)
            save_state(state)
            return state
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> bool:
    """Publish state.json through the shared protocol. Returns whether the
    bytes landed: a lost write of a dedup key (recap_sent/morning_sent) would
    duplicate a report, so it must never fail silently. The old version
    retried a denied replace exactly once, which is not enough for the
    WinError 5 that hits this machine about one run in ten."""
    with _STATE_LOCK:
        res = storage_io.write_json(STATE_FILE, state, indent=2)
        if not res.ok:
            print(f"save_state: could NOT persist {STATE_FILE.name}: "
                  f"{res.status} {res.error}")
        return bool(res.ok)


def state_get(key, default=None):
    return load_state().get(key, default)


def state_set(key, value) -> None:
    # load -> mutate -> save is ONE critical section, and it now needs two
    # locks to be one. _STATE_LOCK is a threading.RLock: it serialises the
    # main loop against the news-watcher thread and says nothing whatever
    # about a second process on the same volume. The file lock is what covers
    # that, and it has to be held across the whole transaction rather than
    # just the publish (Astra section 3).
    with _STATE_LOCK, storage_io.file_lock(STATE_FILE) as lk:
        if not lk.held:
            print(f"state_set({key!r}): state.json is held by another writer "
                  f"({lk.why}); write skipped rather than raced")
            return
        try:
            s = load_state(strict=True)
        except _StateUnavailable as e:  # don't overwrite real state we can't read
            print(f"state_set({key!r}): state.json unreadable, write skipped: {e}")
            return
        s[key] = value
        save_state(s)


def state_update(key, fn, default=None) -> None:
    """Atomic read-modify-write of one state key: fn(current) -> new value, with
    the whole get->modify->set under one lock. Use this (not state_get then
    state_set) whenever two threads mutate the same key — e.g. the main loop and
    the news-watcher both touching pending_sends — so neither loses the other's
    update. Both locks, for the reason state_set names."""
    with _STATE_LOCK, storage_io.file_lock(STATE_FILE) as lk:
        if not lk.held:
            print(f"state_update({key!r}): state.json is held by another "
                  f"writer ({lk.why}); write skipped rather than raced")
            return
        try:
            s = load_state(strict=True)
        except _StateUnavailable as e:  # don't overwrite real state we can't read
            print(f"state_update({key!r}): state.json unreadable, write skipped: {e}")
            return
        s[key] = fn(s.get(key, default))
        save_state(s)


# ---------------------------------------------------------------------------
# owner-identified reservations
# ---------------------------------------------------------------------------
# Astra A06, and it replaces the compensating release that shipped yesterday:
# "Releasing the day key makes the multi step sniper commit atomic" is WRONG.
# Compensation is not a transaction. It can race with a newer claimant or
# release a key after an ambiguous successful send. The fix is not a better
# release, it is an OWNER on the reservation plus explicit lifecycle states, so
# a release can only ever remove the claim the same operation made.
#
# Shape: {key: {"op": <decision id>, "state": "claimed"|"committed",
#               "at": iso, ...extra}}
# A legacy plain string ("09:52") is what is on the live volume today. It reads
# as COMMITTED by an unknown op, so nothing can release it: an old reservation
# whose owner cannot be established is not an unclaimed one.
RESERVED_CLAIMED = "claimed"
RESERVED_COMMITTED = "committed"


def _et_stamp() -> str:
    """ET wall clock for a reservation's audit field. Same ET-not-UTC reason as
    _et_today: the key itself is an ET day."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return f"{datetime.now(ZoneInfo('America/New_York')):%Y-%m-%d %H:%M:%S}"


def _reservation(value) -> dict:
    """One stored entry, normalised. A legacy string is committed-by-unknown."""
    if isinstance(value, dict):
        out = dict(value)
        out.setdefault("state", RESERVED_COMMITTED)
        out.setdefault("op", None)
        return out
    if value is None:
        return None
    return {"op": None, "state": RESERVED_COMMITTED, "legacy": str(value)}


def state_reservation(group: str, key: str):
    """The current holder of one reservation, or None. Read only."""
    return _reservation((state_get(group, {}) or {}).get(key))


def _reserve_txn(group, fn):
    """Read, modify and write one reservation group in ONE critical section,
    under both locks. state_update would do the read-modify-write, but the
    caller also needs the ANSWER (did I get it), and a separate read after the
    write is a second race, so the transaction is spelled out here."""
    with _STATE_LOCK, storage_io.file_lock(STATE_FILE) as lk:
        if not lk.held:
            print(f"reservation on {group!r}: state.json is held by another "
                  f"writer ({lk.why}); refusing rather than racing")
            return False
        try:
            s = load_state(strict=True)
        except _StateUnavailable as e:
            print(f"reservation on {group!r}: state.json unreadable, refused: {e}")
            return False
        cur = s.get(group)
        cur = dict(cur) if isinstance(cur, dict) else {}
        ok, new = fn(cur)
        if not ok:
            return False
        s[group] = new
        return bool(save_state(s))


def state_reserve(group: str, key: str, op_id: str, extra=None,
                  keep=None) -> bool:
    """Claim one key for one operation. True only if THIS op now holds it.

    Refuses when anyone else holds it, in any lifecycle state, and refuses a
    legacy string outright. `keep(key) -> bool` prunes the group in the same
    transaction, so the daily prune of yesterday's sniper keys cannot land as a
    separate write that a concurrent claim then loses."""
    def _fn(cur):
        if keep is not None:
            cur = {k: v for k, v in cur.items() if keep(k)}
        held = _reservation(cur.get(key))
        if held is not None and held.get("op") != op_id:
            return False, cur
        entry = {"op": str(op_id), "state": RESERVED_CLAIMED,
                 "at": _et_stamp()}
        if held is not None and held.get("state") == RESERVED_COMMITTED:
            return False, cur          # our own, already terminal; not re-claimable
        entry.update(extra or {})
        cur[key] = entry
        return True, cur
    return bool(_reserve_txn(group, _fn))


def state_commit_owned(group: str, key: str, op_id: str) -> bool:
    """Move this op's own claim to the terminal state. After this the key is
    never released by anyone: a request that was put on the wire may have
    arrived, and an ambiguous delivered request is not an unsent opportunity."""
    def _fn(cur):
        held = _reservation(cur.get(key))
        if held is None or held.get("op") != op_id:
            return False, cur
        held["state"] = RESERVED_COMMITTED
        cur[key] = held
        return True, cur
    return bool(_reserve_txn(group, _fn))


def state_release_owned(group: str, key: str, op_id: str) -> bool:
    """Hand back ONLY this op's own still-claimed reservation.

    Two conditions, both load bearing. The op must match, so a stand-down
    cannot delete a newer claimant's key (that is the A06 defect: the old code
    dropped whatever sat under the key). And the state must still be claimed,
    so a reservation that has already been committed, which is what any send
    attempt makes it, stays put."""
    def _fn(cur):
        held = _reservation(cur.get(key))
        if held is None:
            return False, cur
        if held.get("op") != op_id or held.get("state") != RESERVED_CLAIMED:
            return False, cur
        cur.pop(key, None)
        return True, cur
    return bool(_reserve_txn(group, _fn))


def account_value():
    """Account dollar size: /setaccount (state.json) beats the env var. A stored
    value is honored even if it's 0 (use presence, not truthiness)."""
    v = state_get("account_value")
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    for name in ("ACCOUNT_VALUE", "ACCOUNT_SIZE"):
        raw = os.environ.get(name, "").replace(",", "").replace("$", "").strip()
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    return None


def suggested_alloc_pct(risk_pct: float) -> float:
    """% of account to put in so a full stop-out costs exactly risk_pct.
    Derived from the live STOP_PCT, so a wider stop means a SMALLER position
    for the same dollar risk. strategy_spec.sizing_sentence() renders it."""
    stop = abs(STOP_PCT) / 100.0
    if stop <= 0:  # guard a STOP_PCT=0 env override from dividing by zero
        stop = 0.70
    return risk_pct / stop
