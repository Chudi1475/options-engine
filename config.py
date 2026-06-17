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


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            return {}
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
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        try:
            tmp.replace(STATE_FILE)
        except PermissionError:  # another process briefly reading the file
            import time
            time.sleep(0.2)
            try:
                tmp.replace(STATE_FILE)
            except (PermissionError, FileNotFoundError):
                pass


def state_get(key, default=None):
    return load_state().get(key, default)


def state_set(key, value) -> None:
    with _STATE_LOCK:  # load -> mutate -> save is one atomic critical section
        s = load_state()
        s[key] = value
        save_state(s)


def account_value():
    """Account dollar size: /setaccount (state.json) beats the env var."""
    v = state_get("account_value")
    if v:
        return float(v)
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
    risk 1% / stop 30% -> ~3.3% of account."""
    return risk_pct / (abs(STOP_PCT) / 100.0)
