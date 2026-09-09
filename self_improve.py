"""The always-on improvement engine: Claude codes this repo better while idle.

Explicitly authorized by the owner (Chudi) on 2026-07-10: "the whole point is
to teach itself... approve it." Design reviewed in SELF_IMPROVE.md.

Runs headless Claude Code sessions in a loop. Each cycle picks ONE
high-leverage improvement (coach proposals, BACKLOG.md, lessons, code
health), implements it, verifies it offline, and commits. When the Claude
usage limit hits, it counts down exactly 5 hours 5 minutes and resumes on
its own. Safety fences, enforced by THIS wrapper, not by trust:

- scoped tools only (file tools + python + git); NO permission-skip flag
- the wrapper re-runs compile + the test suite itself and auto-reverts any
  commit that fails; the session's word is never trusted
- never deploys (deploys stay manual: `railway up --detach`), never pushes
  unless AUTO_PUSH is flipped True by the owner
- the frozen sniper constants and trading thresholds are off-limits to the
  session by prompt AND checked by the wrapper gate below
- kill switch: create a file named SELF_IMPROVE_OFF in the repo root

Install as a Scheduled Task (starts at logon, restarts if it dies): see
SELF_IMPROVE.md. Logs: logs/self_improve/<date>/.
"""

import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent
PYEXE = str(REPO / ".venv" / "Scripts" / "python.exe")
CT = ZoneInfo("America/Chicago")

USAGE_WAIT_S = 5 * 3600 + 5 * 60   # the owner's rule: 5 hours 5 minutes
CYCLE_GAP_S = 120                  # breathe between cycles
CYCLE_TIMEOUT_S = 2 * 3600         # kill a runaway session
PAUSE_FILE = REPO / "SELF_IMPROVE_OFF"
AUTO_PUSH = False  # owner flips this to push each gated commit to origin

# files the loop must never change on its own; a commit touching them is
# reverted by the wrapper even if the tests pass
PROTECTED = {"fvg.py", "config.py", ".env", "self_improve.py"}

# scoped permissions: everything the engine needs, nothing more.
ALLOWED_TOOLS = (
    "Read Glob Grep Edit Write "
    "Bash(python:*) Bash(.venv/Scripts/python.exe:*) "
    "Bash(git add:*) Bash(git commit:*) Bash(git diff:*) "
    "Bash(git log:*) Bash(git status:*)"
)

USAGE_PATTERNS = re.compile(
    r"usage limit|session limit|rate.?limit|resets at|out of credits|"
    r"credit balance", re.IGNORECASE)

# "resets 5am", "resets at 5:30 pm", "resets 11pm (America/Chicago)" ...
RESET_STAMP = re.compile(
    r"resets(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE)
PROBE_EVERY_S = 15 * 60   # fallback: knock every 15 min until the door opens
RESUME_BUFFER_S = 180     # start a touch after the stated reset, not on it

PROMPT = """You are the autonomous improvement engine for options-engine, a
live options-alert Telegram bot. You run unattended. Do exactly ONE complete,
verified improvement this session, then stop.

Pick the highest-leverage item you can FULLY finish offline, in this order:
1. An escalated proposal in data/coach_proposals.jsonl (sightings >= 3) that
   is a pure code/plumbing change (not a trading-threshold change).
2. A concrete item from BACKLOG.md you can complete now.
3. A recurring pain point in data/lessons_digest.md or reports/edge_lessons.md
   fixable by a code change (not a strategy change).
4. A real bug, a latency win, a missing test, or a clarity fix in user-facing
   text (plain language, never an em dash in user-facing strings).

Hard rules:
- NEVER modify fvg.py, config.py, .env, or self_improve.py. Trading
  thresholds and the verified sniper constants change only with a new
  verified backtest round, which you cannot run here. If your best idea
  needs one, append it to BACKLOG.md under 'needs backtest' instead.
- Never send Telegram messages, never deploy, never push, never touch git
  history (no rebase/reset/amend).
- Verify before committing: compile every file you touched with
  .venv/Scripts/python.exe -m py_compile, then run
  .venv/Scripts/python.exe test_pipeline.py, test_adduser.py,
test_no_hardcoded_stats.py, test_session_fixes.py, test_market_calendar.py,
test_instance_lease.py and test_no_em_dash.py.
All must pass. If you change a rule number, change
it in config.py and let strategy_spec render it: never type a stat into a card,
a prompt or a doc, and run `python gen_docs.py` after a config change.
- Commit with git add <files> && git commit -m "<what and why, one line>".
- If nothing worthwhile is left, append 2-3 specific new ideas to BACKLOG.md
  and commit that instead. Never invent busywork.
"""


def log_dir() -> Path:
    d = REPO / "logs" / "self_improve" / datetime.now(CT).strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def say(msg: str):
    line = f"[{datetime.now(CT):%H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with (log_dir() / "engine.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def read_env() -> dict:
    """Tiny KEY=VALUE parse of the repo .env (no dotenv dependency)."""
    env = {}
    try:
        for line in (REPO / ".env").read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def telegram_notify(text: str):
    """Best effort; never crashes the engine."""
    env = read_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat = env.get("OWNER_CHAT_ID") or \
        (env.get("TELEGRAM_CHAT_IDS", "").split(",") or [""])[0].strip()
    if not token or not chat:
        return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=10)
    except Exception:
        pass


def git(*args) -> str:
    try:
        r = subprocess.run(["git"] + list(args), cwd=REPO, timeout=120,
                           capture_output=True, text=True, errors="replace")
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return f"git error: {e}"


def head_commit() -> str:
    return git("rev-parse", "HEAD").strip()


def changed_files(since: str) -> list:
    out = git("diff", "--name-only", f"{since}..HEAD")
    return [f.strip() for f in out.split() if f.strip()]


def run_checks(files: list) -> bool:
    """The wrapper's own gate; never trust the session's word for it."""
    try:
        touched_protected = [f for f in files
                             if Path(f).name in PROTECTED]
        if touched_protected:
            say(f"gate: PROTECTED file touched: {touched_protected}")
            return False
        py = [f for f in files if f.endswith(".py") and (REPO / f).exists()]
        if py:
            r = subprocess.run([PYEXE, "-m", "py_compile"] + py, cwd=REPO,
                               timeout=120, capture_output=True, text=True)
            if r.returncode != 0:
                say(f"gate: compile failed: {r.stderr[:300]}")
                return False
        # test_review_packet_gate.py reaches the review packet's own suite,
        # which lives outside this repo. it SKIPs when no packet is on the
        # machine, so a worker without the review kit still passes the gate.
        for test in ("test_pipeline.py", "test_adduser.py",
                     "test_no_hardcoded_stats.py", "test_session_fixes.py",
                     "test_market_calendar.py", "test_review_regressions.py",
                     "test_ledger_integrity.py", "test_review_import.py",
                     "test_grading_integrity.py", "test_instance_lease.py",
                     "test_no_em_dash.py", "test_review_packet_gate.py"):
            r = subprocess.run([PYEXE, test], cwd=REPO, timeout=600,
                               capture_output=True, text=True, errors="replace")
            if r.returncode != 0:
                say(f"gate: {test} failed: {(r.stdout or '')[-300:]}")
                return False
        return True
    except Exception as e:
        say(f"gate error (treating as fail): {e}")
        return False


def countdown(total_s: int, why: str):
    """The 5h05m usage-limit wait, with a live console countdown."""
    resume_at = datetime.now(CT) + timedelta(seconds=total_s)
    stamp = f"{resume_at:%I:%M %p CT}".lstrip("0")
    say(f"usage limit hit ({why[:120]}). Counting down "
        f"{total_s // 3600}h{(total_s % 3600) // 60:02d}m; resuming at {stamp}.")
    telegram_notify("🤖⏸️ Claude usage limit hit. The improvement engine is "
                    f"waiting 5h05m and resumes at {stamp}.")
    end = time.monotonic() + total_s
    tty = sys.stdout.isatty()
    last_log = 0.0
    while True:
        left = end - time.monotonic()
        if left <= 0:
            break
        h, rem = divmod(int(left), 3600)
        m, s = divmod(rem, 60)
        if tty:
            print(f"\r  resuming in {h}:{m:02d}:{s:02d}  ", end="", flush=True)
        elif time.monotonic() - last_log > 600:  # scheduled task: log/10 min
            say(f"waiting... {h}h{m:02d}m left")
            last_log = time.monotonic()
        time.sleep(min(60, max(1, left)))
    if tty:
        print()
    say("countdown done, resuming.")
    telegram_notify("🤖▶️ The 5h05m wait is over. Improvement engine is back "
                    "to making the bot better.")


def parse_reset_wait_s(out: str):
    """Pull the actual reset time out of the CLI's limit message and return
    seconds until then (+ a small buffer), or None if no stamp was given."""
    m = RESET_STAMP.search(out or "")
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "pm":
        hour += 12
    minute = int(m.group(2) or 0)
    now = datetime.now(CT)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return int((target - now).total_seconds()) + RESUME_BUFFER_S


def usage_is_back() -> bool:
    """One tiny probe call: succeeds only when usage has actually returned."""
    exe = shutil.which("claude")
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "-p", "reply with exactly: ok"],
                           cwd=REPO, timeout=180, capture_output=True,
                           text=True, errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0 and not USAGE_PATTERNS.search(out)
    except Exception:
        return False


def wait_for_usage(out: str):
    """Resume the moment usage is actually back: use the reset time the CLI
    printed when it gave one; otherwise probe every 15 minutes. The old
    fixed 5h05m stands only as the hard cap."""
    wait_s = parse_reset_wait_s(out)
    if wait_s is not None:
        countdown(min(wait_s, USAGE_WAIT_S + 3600),
                  "CLI said when usage resets; waiting until then")
        return
    say("limit message had no reset time; probing every 15 min instead")
    telegram_notify("🤖⏸️ Claude usage limit hit. No reset time given, so "
                    "the engine will knock every 15 minutes and resume the "
                    "moment usage is back (5h05m cap).")
    deadline = time.monotonic() + USAGE_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(min(PROBE_EVERY_S, max(1, deadline - time.monotonic())))
        if usage_is_back():
            say("probe succeeded: usage is back, resuming early")
            telegram_notify("🤖▶️ Usage is back. Improvement engine resumed "
                            "ahead of the 5h05m cap.")
            return
        say("probe: still limited, next knock in 15 min")
    say("hit the 5h05m cap, resuming")
    telegram_notify("🤖▶️ 5h05m cap reached. Improvement engine resuming.")


def claude_cmd() -> list:
    exe = shutil.which("claude")
    if not exe:
        say("claude CLI not found on PATH; cannot run. Exiting.")
        raise SystemExit(1)
    return [exe, "-p", PROMPT, "--allowedTools", ALLOWED_TOOLS]


def one_cycle(n: int) -> str:
    """Run one headless improvement session. Returns 'ok'|'limit'|'error'."""
    before = head_commit()
    logfile = log_dir() / f"cycle_{n:04d}.log"
    say(f"cycle {n}: session starting (log: {logfile.name})")
    try:
        r = subprocess.run(claude_cmd(), cwd=REPO, timeout=CYCLE_TIMEOUT_S,
                           capture_output=True, text=True, errors="replace")
        out = (r.stdout or "") + "\n" + (r.stderr or "")
    except subprocess.TimeoutExpired:
        say(f"cycle {n}: killed after {CYCLE_TIMEOUT_S // 60} min")
        return "error"
    try:
        logfile.write_text(out, encoding="utf-8")
    except OSError:
        pass
    one_cycle.last_out = out  # wait_for_usage parses the reset stamp from it

    if USAGE_PATTERNS.search(out) and (r.returncode != 0
                                       or "usage limit" in out.lower()):
        return "limit"

    after = head_commit()
    if after == before:
        say(f"cycle {n}: no commit landed")
        return "ok"
    files = changed_files(before)
    if run_checks(files):
        subject = git("log", "-1", "--format=%s").strip()
        say(f"cycle {n}: committed + gates passed: {subject}")
        telegram_notify(f"🤖✅ bot improved itself: {subject[:300]}")
        if AUTO_PUSH:
            git("push", "origin", "main")
    else:
        say(f"cycle {n}: gates FAILED, reverting {after[:8]}")
        rev = git("revert", "--no-edit", after)
        if "error" in rev.lower() or "conflict" in rev.lower():
            say(f"revert hit trouble, manual look needed: {rev[:200]}")
        telegram_notify("🤖↩️ an improvement failed its safety checks and "
                        "was rolled back automatically. Nothing broke.")
    return "ok"


def main():
    if "--dry-run" in sys.argv:
        say("dry run: exercising the machinery without invoking claude")
        env = read_env()
        say(f".env parse: token={'yes' if env.get('TELEGRAM_BOT_TOKEN') else 'no'}, "
            f"owner_chat={'yes' if (env.get('OWNER_CHAT_ID') or env.get('TELEGRAM_CHAT_IDS')) else 'no'}")
        fake = "Error: You've hit your usage limit. Your limit resets at 5am."
        assert USAGE_PATTERNS.search(fake), "usage pattern must match CLI text"
        say("usage-limit pattern matches the real CLI message: OK")
        assert not run_checks(["fvg.py"]), "protected-file gate must trip"
        say("protected-file gate trips on fvg.py: OK")
        assert run_checks([]), "empty change set must pass gates"
        say("full test-suite gate passes on clean tree: OK")
        countdown(3, "dry-run sample (3s stand-in for 18300s)")
        say("dry run complete")
        return
    say("improvement engine up. Pause anytime: create SELF_IMPROVE_OFF")
    telegram_notify("🤖 improvement engine is up: the bot now codes itself "
                    "while idle. Pause: SELF_IMPROVE_OFF file. Logs: "
                    "logs/self_improve/")
    n = 0
    while True:
        if PAUSE_FILE.exists():
            say("paused (SELF_IMPROVE_OFF exists); checking again in 5 min")
            time.sleep(300)
            continue
        n += 1
        try:
            result = one_cycle(n)
        except SystemExit:
            raise
        except Exception as e:
            say(f"cycle {n} crashed (continuing): {e}")
            result = "error"
        if result == "limit":
            wait_for_usage(getattr(one_cycle, "last_out", ""))
        else:
            time.sleep(CYCLE_GAP_S)


if __name__ == "__main__":
    main()
