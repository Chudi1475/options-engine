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
STOP_PCT = _f("STOP_PCT", -70.0)             # sell EVERYTHING at -70% (half already
                                             # banked at +25%; backtest: wider stop =
                                             # fewer noise stop-outs -> 72% win rate,
                                             # higher total return, lower drawdown)
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
LIVE_STATS_MIN_TOTAL = 30                    # closed signals before live stats replace the backtest
LIVE_STATS_MIN_SETUP = 10                    # and at least this many for the specific setup
POLL_SECONDS = int(_f("POLL_SECONDS", 15))   # main loop cadence
NEWS_POLL_SECONDS = int(_f("NEWS_POLL_SECONDS", 12))  # breaking-news scan cadence
                                             # (own thread; lower = faster but
                                             # risks the free RSS feeds rate-limiting)
EXPIRY_WARN_MINUTES = 15                     # "close before expiry" warning, minutes before 4 PM ET
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


def load_state(strict: bool = False) -> dict:
    global _LAST_GOOD
    if STATE_FILE.exists():
        try:
            parsed = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(parsed, dict):
                _LAST_GOOD = parsed
            return parsed
        except json.JSONDecodeError as e:
            # Corrupt/torn CONTENT won't heal itself. If we returned {} here, the
            # next state_set would persist {only_that_key} and wipe everything
            # else (requests, account_value, tg_offset, dedup keys...). Move the
            # bad file aside once so a later write rebuilds clean state.
            _quarantine_state(f"corrupt ({e})")
            return {}
        except OSError as e:
            # The file IS there but momentarily unreadable (mounted-volume
            # hiccup — save_state anticipates the same on its write side). On a
            # write path, refuse rather than clobber good-but-unreadable state.
            # On a read path serve the last-good snapshot (not {}), so a one-cycle
            # hiccup can't make a dedup check ("recap already sent?") re-fire.
            if strict:
                raise _StateUnavailable(str(e))
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


def save_state(state: dict) -> None:
    # unique temp per thread so two concurrent savers never clobber one shared
    # tmp file (which would publish a torn state.json or raise FileNotFoundError)
    with _STATE_LOCK:
        tmp = STATE_FILE.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            try:
                tmp.replace(STATE_FILE)
            except (PermissionError, FileNotFoundError):  # mid-read, or the
                import time                                # volume hiccuped
                time.sleep(0.2)
                try:
                    tmp.replace(STATE_FILE)
                except (PermissionError, FileNotFoundError) as e:
                    # don't fail silently — a lost write of a dedup key
                    # (recap_sent/morning_sent) would duplicate a report
                    print(f"save_state: could NOT persist state.json: {e}")
        finally:
            try:  # never leave an orphan temp file on a failed publish
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


def state_get(key, default=None):
    return load_state().get(key, default)


def state_set(key, value) -> None:
    with _STATE_LOCK:  # load -> mutate -> save is one atomic critical section
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
    update."""
    with _STATE_LOCK:
        try:
            s = load_state(strict=True)
        except _StateUnavailable as e:  # don't overwrite real state we can't read
            print(f"state_update({key!r}): state.json unreadable, write skipped: {e}")
            return
        s[key] = fn(s.get(key, default))
        save_state(s)


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
    Derived from the live STOP_PCT — e.g. risk 1% / stop 70% -> ~1.43%."""
    stop = abs(STOP_PCT) / 100.0
    if stop <= 0:  # guard a STOP_PCT=0 env override from dividing by zero
        stop = 0.70
    return risk_pct / stop
