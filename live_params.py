"""Owner-tunable live settings, read from live_params.json on DATA_DIR.

The self-improving loop is propose-only: nightly reviews pitch rule changes
and Chudi approves or rejects them (/proposals), but applying even an
APPROVED change to the alert allow-list or the entry knobs used to mean
editing code and redeploying. This file is the no-redeploy path for exactly
the settings that were frozen in code:

    {
      "allowed_setups": ["SPX:call", "SPY:call", "QCOM:call", "TSLA:put"],
      "watchlist": {"SPX": "^GSPC", "SPY": "SPY", "TSLA": "TSLA"},
      "entry_start": "09:45",
      "entry_end": "10:30",
      "mom_bars": 3
    }

Every key is optional; a present key overrides the built-in default
(scanner.Service.ALLOWED_SETUPS / strategy.StrategyConfig). Validation is
all-or-nothing: one unknown key or bad value rejects the WHOLE file and the
scanner keeps its built-in settings, so a typo can never half-apply, and a
rejected file always reads back as "using built-in settings" in /reload.

Deliberate boundaries:
- The exit/risk knobs (TP_HALF_PCT, STOP_PCT, RUNNER_GIVEBACK_PCT,
  MIN_WINRATE, ...) are NOT here. config.py already lets those be set
  per-deploy with environment variables, and they only change with a
  verified backtest round anyway.
- The honesty gate is untouched: gate_stats still demands real backtest
  stats, the 70% win-rate floor and positive expectancy for any setup this
  file allows, so the file can widen what MAY alert but never what does.
- Overrides govern the scanner's DECISIONS (entry scanning, the alert gate,
  position monitoring, the morning card's news lines) and which tickers
  /calls and the bare-symbol shortcut treat as watched. TEXT surfaces that
  describe the rules (per-ticker /calls reads via market_tools, the brain's
  prompts, the recap, the window sentences in cards) render from the same
  effective settings: modules with no Service handle call effective() here,
  which reads this file fresh and falls back to the built-ins exactly like
  reload_tunables().

scanner.Service.reload_tunables() reads this file at boot and on every
trading-date flip; the owner-only /reload command applies an edit
immediately and replies with exactly what is live or why the file was
rejected.
"""

import json
import re
from datetime import datetime, time

import config
from strategy import StrategyConfig

FILE_NAME = "live_params.json"
KNOWN_KEYS = ("allowed_setups", "watchlist", "entry_start", "entry_end",
              "mom_bars")
# The built-in alert allow-list. scanner.Service.ALLOWED_SETUPS points here;
# it lives in this module so surfaces WITHOUT a Service handle (market_tools
# reads, the nightly review, the recap) can compute the same effective
# settings the scanner runs on, without importing the whole scanner.
DEFAULT_ALLOWED_SETUPS = frozenset(
    {"SPX:call", "SPY:call", "QCOM:call", "TSLA:put"})
_TICKER_RE = re.compile(r"^[A-Z0-9.^=-]{1,10}$")
_YF_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.^=-]{1,15}$")  # ^GSPC, GC=F, BTC-USD
MAX_WATCHLIST = 12          # a huge list would slow every 15s poll cycle
MAX_FILE_BYTES = 65536      # read on the main loop; a real file is ~200 bytes
MOM_BARS_MIN, MOM_BARS_MAX = 1, 12   # 5 min .. 1 hour of 5m bars
# entry edges must stay inside [09:45, 16:00] ET: position monitoring starts
# at scanner.MONITOR_START (09:45), so an earlier entry_start would open
# positions the loop is not yet watching (no stop/half/trail for minutes)
ENTRY_EARLIEST, ENTRY_LATEST = time(9, 45), time(16, 0)


def path():
    return config.DATA_DIR / FILE_NAME


def _err_setups(v, errors):
    if not isinstance(v, list) or not v:
        errors.append('allowed_setups must be a non-empty list like '
                      '["SPX:call", "TSLA:put"]')
        return None
    out, bad = set(), []
    for item in v:
        parts = item.split(":") if isinstance(item, str) else []
        if len(parts) == 2:
            ticker, direction = parts[0].strip().upper(), parts[1].strip().lower()
            if _TICKER_RE.match(ticker) and direction in ("call", "put"):
                out.add(f"{ticker}:{direction}")
                continue
        bad.append(repr(item))
    if bad:
        errors.append("allowed_setups entries must look like TICKER:call or "
                      "TICKER:put; bad: " + ", ".join(bad))
        return None
    return out


def _err_watchlist(v, errors):
    if not isinstance(v, dict) or not v:
        errors.append('watchlist must be a non-empty object like '
                      '{"SPX": "^GSPC", "TSLA": "TSLA"}')
        return None
    if len(v) > MAX_WATCHLIST:
        errors.append(f"watchlist has {len(v)} tickers; max {MAX_WATCHLIST} "
                      "(every ticker is polled every cycle)")
        return None
    out, bad = {}, []
    for k, val in v.items():
        ticker = str(k).strip().upper()
        # the value must be ONE plausible Yahoo symbol: a space or comma
        # would validate as a "non-empty string" but turn every download
        # into a broken multi-ticker request that kills the ticker's
        # scanning AND monitoring while /reload reports success
        if (not _TICKER_RE.match(ticker) or ticker in out
                or not isinstance(val, str)
                or not _YF_SYMBOL_RE.match(val.strip())):
            bad.append(repr(k))
            continue
        out[ticker] = val.strip()
    if bad:
        errors.append("watchlist entries must map a ticker to one Yahoo "
                      "symbol (no repeats, no spaces); bad: " + ", ".join(bad))
        return None
    return out


def _err_time(key, v, errors):
    try:
        t = datetime.strptime(str(v).strip(), "%H:%M").time()
    except ValueError:
        errors.append(f'{key} must be 24h "HH:MM" like "09:45"; got {v!r}')
        return None
    if not (ENTRY_EARLIEST <= t <= ENTRY_LATEST):
        errors.append(f"{key} must be between {ENTRY_EARLIEST:%H:%M} (when "
                      f"position monitoring starts) and {ENTRY_LATEST:%H:%M} "
                      f"ET; got {t:%H:%M}")
        return None
    return t


def _err_mom_bars(v, errors):
    if type(v) is not int or not (MOM_BARS_MIN <= v <= MOM_BARS_MAX):
        errors.append(f"mom_bars must be a whole number from {MOM_BARS_MIN} "
                      f"to {MOM_BARS_MAX} (5m bars in the momentum window); "
                      f"got {v!r}")
        return None
    return v


def load(p=None):
    """Read and validate live_params.json.

    Returns (params, errors, exists):
      params - dict of validated overrides with parsed types (setups as a
               set, times as datetime.time), or None when the file is absent
               or ANY check failed (all-or-nothing)
      errors - plain-language reasons, empty when params is usable
      exists - whether a file was found at all
    """
    p = p or path()
    if not p.exists():
        return None, [], False
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return None, [f"{p.name} is over {MAX_FILE_BYTES} bytes; a real "
                          "settings file is tiny"], True
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except OSError as e:
        return None, [f"could not read {p.name}: {e}"], True
    except UnicodeDecodeError:
        return None, [f"{p.name} is not UTF-8 text; save it as plain "
                      "UTF-8 JSON"], True
    except json.JSONDecodeError as e:
        return None, [f"{p.name} is not valid JSON: {e}"], True
    if not isinstance(raw, dict):
        return None, [f"{p.name} must be a JSON object of settings"], True

    errors = []
    unknown = sorted(set(raw) - set(KNOWN_KEYS))
    if unknown:
        errors.append("unknown key(s) " + ", ".join(unknown)
                      + "; valid keys: " + ", ".join(KNOWN_KEYS))
    out = {}
    if "allowed_setups" in raw:
        val = _err_setups(raw["allowed_setups"], errors)
        if val is not None:
            out["allowed_setups"] = val
    if "watchlist" in raw:
        val = _err_watchlist(raw["watchlist"], errors)
        if val is not None:
            out["watchlist"] = val
    for key in ("entry_start", "entry_end"):
        if key in raw:
            val = _err_time(key, raw[key], errors)
            if val is not None:
                out[key] = val
    if "mom_bars" in raw:
        val = _err_mom_bars(raw["mom_bars"], errors)
        if val is not None:
            out["mom_bars"] = val

    # the EFFECTIVE window must stay ordered even when only one edge is given
    # (skipped when an edge already failed to parse; that error says enough)
    given = [k for k in ("entry_start", "entry_end") if k in raw]
    if given and all(k in out for k in given):
        d = StrategyConfig()
        start = out.get("entry_start", d.entry_start)
        end = out.get("entry_end", d.entry_end)
        if start >= end:
            errors.append(f"entry window is empty: entry_start {start:%H:%M} "
                          f"is not before entry_end {end:%H:%M}")
    if errors:
        return None, errors, True
    return out, [], True


def apply(cfg, params, allowed=DEFAULT_ALLOWED_SETUPS):
    """Overlay validated overrides onto cfg IN PLACE and return the effective
    allow-list. The one mapping from file keys to live settings, shared by
    scanner.reload_tunables() and effective() so they can never disagree."""
    if "allowed_setups" in params:
        allowed = params["allowed_setups"]
    if "watchlist" in params:
        cfg.watchlist = params["watchlist"]
    if "entry_start" in params:
        cfg.entry_start = params["entry_start"]
    if "entry_end" in params:
        cfg.entry_end = params["entry_end"]
    if "mom_bars" in params:
        cfg.mom_bars = params["mom_bars"]
    return allowed


def effective():
    """(cfg, allowed_setups) exactly as the scanner sees them after a reload:
    the StrategyConfig built-ins with any VALID live_params.json applied, and
    deterministically the built-ins when the file is missing or invalid.
    For surfaces that describe or replay the rules but hold no Service
    (market_tools reads, learn, recap). Never raises."""
    cfg = StrategyConfig()
    allowed = DEFAULT_ALLOWED_SETUPS
    try:
        params, _errs, _exists = load()
    except Exception:
        params = None
    if params:
        allowed = apply(cfg, params)
    return cfg, allowed


def window_et(cfg) -> str:
    """The effective entry window as compact ET text, e.g. '9:45-10:30'."""
    return (f"{cfg.entry_start:%H:%M}".lstrip("0") + "-"
            + f"{cfg.entry_end:%H:%M}".lstrip("0"))


def summary(params):
    """One plain line saying exactly what the file overrides."""
    if not params:
        return "no overrides"
    bits = []
    if "allowed_setups" in params:
        bits.append("allow-list " + ", ".join(sorted(params["allowed_setups"])))
    if "watchlist" in params:
        bits.append("watchlist " + ", ".join(params["watchlist"]))
    for key in ("entry_start", "entry_end"):
        if key in params:
            bits.append(f"{key} {params[key]:%H:%M}")
    if "mom_bars" in params:
        bits.append(f"mom_bars {params['mom_bars']}")
    return " / ".join(bits)
