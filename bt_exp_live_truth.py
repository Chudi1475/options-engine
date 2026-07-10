"""LIVE vs BACKTEST ground-truth reconstruction (READ-ONLY).

Parses the Railway deploy-log JSON dump (rw_logs.json in scratchpad) into
dated trading sessions, extracts every entry + exit event per setup, and
computes the LIVE new-rules per-trade P&L using the SAME weighting the live
bot uses (positions.py weighted_final: 0.5*half + 0.5*runner; a bare stop =
full position at the stop mark). Also derives an APPROXIMATE old-rules
shadow (sell full at first +target cross, else -stop) from the same logged
marks, clearly flagged as approximate because the true shadow lives only in
positions.json on the volume.

No network, no writes to repo state, no volume access. Input is the log dump
already captured to scratchpad.
"""
import json
import re
import sys
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOG = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:\Users\Chudi\AppData\Local\Temp\claude\C--Users-Chudi\be955aed-7b67-4b7b-aa31-5eb6b3bf266a\scratchpad\rw_logs.json"

rows = []
with open(LOG, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass

# ET date from UTC timestamp (EDT = UTC-4 in Jun/Jul)
def et_date(ts):
    # ts like 2026-06-30T13:50:01.xxxZ ; subtract 4h -> ET date
    from datetime import datetime, timedelta
    dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S") - timedelta(hours=4)
    return dt.strftime("%Y-%m-%d")

RE_ENTRY = re.compile(r"(\d\d:\d\d:\d\d) alert sent: (\w+) ([\d.]+) (call|put) entry \$([\d.]+) \((quote|estimate)\)")
RE_EXIT  = re.compile(r"(\d\d:\d\d:\d\d) (\w+): (sell_half|runner_trail|stop) at ([+-][\d.]+)%")
RE_WATCH = re.compile(r"(\d\d:\d\d:\d\d) watching: (.+)")

# split into sessions on "Scanner running"
sessions = []
cur = None
for r in rows:
    msg = r.get("message", "")
    ts = r.get("timestamp", "")
    if "Scanner running (live alerts)" in msg:
        if cur:
            sessions.append(cur)
        cur = {"date": et_date(ts) if ts else "?", "events": []}
        continue
    if cur is None:
        continue
    m = RE_ENTRY.search(msg)
    if m:
        cur["events"].append(("entry", m.group(1), m.group(2), m.group(4),
                              float(m.group(3)), float(m.group(5)), m.group(6)))
        continue
    m = RE_EXIT.search(msg)
    if m:
        cur["events"].append(("exit", m.group(1), m.group(2), m.group(3),
                              float(m.group(4))))
        continue
    m = RE_WATCH.search(msg)
    if m:
        cur["events"].append(("watch", m.group(1), m.group(2)))
if cur:
    sessions.append(cur)

TP_HALF = 25.0
GIVEBACK = 40.0
STOP = -70.0
# old-rules shadow bracket (positions.py docstring: +15%/-60%)
OLD_TGT, OLD_STOP = 15.0, -60.0

# backtest claims
CLAIMS = {
    "SPX:call": (78.0, 31.0),
    "SPY:call": (78.0, 26.0),
    "QCOM:call": (71.0, 17.0),
    "TSLA:put": (67.0, 6.0),
}

trades = []  # dict per entered setup per session
for s in sessions:
    date = s["date"]
    # collect entries and the exit events keyed by ticker (in order)
    entries = {}
    for ev in s["events"]:
        if ev[0] == "entry":
            _, t, tk, direction, strike, prem, src = ev
            entries[tk] = {"date": date, "ticker": tk, "dir": direction,
                           "strike": strike, "entry": prem, "src": src,
                           "half": None, "runner": None, "stop": None,
                           "entry_t": t, "last_watch": None}
    for ev in s["events"]:
        if ev[0] == "exit":
            _, t, tk, kind, pct = ev
            if tk not in entries:
                # exit for a symbol with no entry this session (leftover / prior day)
                entries.setdefault(tk, {"date": date, "ticker": tk, "dir": "?",
                                        "strike": None, "entry": None, "src": "?",
                                        "half": None, "runner": None, "stop": None,
                                        "entry_t": None, "last_watch": None,
                                        "orphan": True})
            e = entries[tk]
            if kind == "sell_half":
                e["half"] = pct
            elif kind == "runner_trail":
                e["runner"] = pct
            elif kind == "stop":
                e["stop"] = pct
        elif ev[0] == "watch":
            # parse "SPX -18.8%, SPY -0.8%" -> last seen per ticker
            for part in ev[2].split(","):
                m = re.search(r"(\w+)\s+([+-][\d.]+)%", part)
                if m and m.group(1) in entries:
                    entries[m.group(1)]["last_watch"] = float(m.group(2))
    for tk, e in entries.items():
        trades.append(e)

def new_rules_pnl(e):
    """Whole-position % under live new rules, from logged legs."""
    half, runner, stop = e["half"], e["runner"], e["stop"]
    note = ""
    if stop is not None and half is None:
        return stop, "full stop, no half"          # full position stopped
    if half is not None and runner is not None:
        return 0.5 * half + 0.5 * runner, "half+runner"
    if half is not None and stop is not None:
        return 0.5 * half + 0.5 * stop, "half then stop"
    if half is not None and runner is None and stop is None:
        # runner rode to EOD/expiry; best proxy = last watched mark
        if e["last_watch"] is not None:
            return 0.5 * half + 0.5 * e["last_watch"], "half + EOD(last watch)"
        return 0.5 * half + 0.5 * half, "half only (runner unknown->half)"
    if half is None and runner is None and stop is None:
        # never hit half or stop; rode to expiry
        if e["last_watch"] is not None:
            return e["last_watch"], "no exit -> EOD(last watch)"
        return None, "no exit, no marks"
    if runner is not None and half is None:
        return runner, "runner w/o logged half"
    return None, "unresolved"

def old_rules_pnl(e):
    """APPROX old shadow (+15/-60) from logged marks. Coarse: uses the half
    cross as the +target proxy (old sells full there) and the stop/last mark
    for the downside. Flagged approximate."""
    half, runner, stop = e["half"], e["runner"], e["stop"]
    # if it reached the +25 half, it certainly crossed +15 old target earlier
    if half is not None:
        return max(half, OLD_TGT), "old target hit (~+15 to half mark)"
    if stop is not None:
        # crashed without ever crossing +15; old -60 stop would fire on the way
        return max(stop, OLD_STOP) if stop > OLD_STOP else OLD_STOP, "old -60 stop"
    if e["last_watch"] is not None and e["last_watch"] >= OLD_TGT:
        return OLD_TGT, "old target (rode up)"
    if e["last_watch"] is not None:
        return max(e["last_watch"], OLD_STOP), "old EOD/last"
    return None, "unresolved"

print("=" * 70)
print("SESSIONS FOUND:", len(sessions), "->", [s["date"] for s in sessions])
print("=" * 70)
for e in trades:
    npnl, nnote = new_rules_pnl(e)
    opnl, onote = old_rules_pnl(e)
    tag = " [ORPHAN/prior-day]" if e.get("orphan") else ""
    legs = f"half={e['half']} runner={e['runner']} stop={e['stop']} lastwatch={e['last_watch']}"
    print(f"\n{e['date']} {e['ticker']}:{e['dir']}  entry=${e['entry']} ({e['src']}){tag}")
    print(f"   legs: {legs}")
    print(f"   NEW pnl = {npnl if npnl is None else round(npnl,2)}%  ({nnote})")
    print(f"   OLD~pnl = {opnl if opnl is None else round(opnl,2)}%  ({onote})")

# aggregate per setup (exclude orphans and unresolved)
print("\n" + "=" * 70)
print("PER-SETUP LIVE RECORD (new rules) vs BACKTEST CLAIM")
print("=" * 70)
agg = defaultdict(list)
agg_old = defaultdict(list)
for e in trades:
    if e.get("orphan") or e["dir"] == "?":
        continue
    key = f"{e['ticker']}:{e['dir']}"
    n, _ = new_rules_pnl(e)
    o, _ = old_rules_pnl(e)
    if n is not None:
        agg[key].append(n)
    if o is not None:
        agg_old[key].append(o)

for key in sorted(set(agg) | set(CLAIMS)):
    ns = agg.get(key, [])
    os_ = agg_old.get(key, [])
    if ns:
        wr = 100 * sum(1 for x in ns if x > 0) / len(ns)
        exp = sum(ns) / len(ns)
        total = sum(ns)
    else:
        wr = exp = total = float("nan")
    cw, ce = CLAIMS.get(key, (float("nan"), float("nan")))
    print(f"\n{key}:  n={len(ns)}")
    print(f"   LIVE new : win {wr:.0f}%  exp {exp:+.1f}%/trade  total {total:+.1f}%  marks={[round(x,1) for x in ns]}")
    if os_:
        owr = 100 * sum(1 for x in os_ if x > 0) / len(os_)
        oexp = sum(os_) / len(os_)
        print(f"   OLD~shadow: win {owr:.0f}%  exp {oexp:+.1f}%/trade  marks={[round(x,1) for x in os_]}")
    print(f"   BACKTEST : win {cw:.0f}%  exp {ce:+.1f}%/trade  ->  "
          f"gap {exp-ce:+.1f} exp, {wr-cw:+.0f} win" if ns else "   (no live trades)")

# portfolio win rate floor
all_new = [x for k in agg for x in agg[k]]
if all_new:
    pwr = 100 * sum(1 for x in all_new if x > 0) / len(all_new)
    print("\n" + "=" * 70)
    print(f"PORTFOLIO (all setups): n={len(all_new)}  win {pwr:.0f}%  "
          f"exp {sum(all_new)/len(all_new):+.1f}%/trade  (70% floor: "
          f"{'HOLDS' if pwr>=70 else 'BREACHED'})")
