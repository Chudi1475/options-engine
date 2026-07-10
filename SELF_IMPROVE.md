# The always-on improvement loop

Built and installed 2026-07-10 with the owner's explicit authorization.
`self_improve.py` is the forever-running wrapper that launches headless
Claude Code sessions against this repo while you are away.

## What it does, each cycle
1. Runs one headless Claude session with SCOPED permissions (file tools +
   python + git only, no --dangerously-skip-permissions) and one job: pick
   ONE improvement from BACKLOG.md / data/lessons_digest.md /
   data/coach_proposals.jsonl / code health, implement it, verify offline
   (py_compile + test_pipeline.py + test_adduser.py), commit.
2. The wrapper re-runs those same gates itself and `git revert`s the commit
   if anything fails. It never trusts the session's word.
3. Texts the owner a one-line summary per cycle via the bot's Telegram token.
4. On a Claude usage-limit message: texts you, then resumes the moment usage
   is actually back: it parses the reset time straight out of the CLI's
   message ("resets 5am") and waits until then; if no time was given it
   knocks every 15 minutes with a tiny probe call. The fixed 5h05m wait is
   only the hard cap when everything else fails.
5. Never deploys (deploys stay `railway up --detach`, run by a human), never
   pushes unless AUTO_PUSH is flipped on, never touches the frozen sniper
   constants in fvg.py or the trading thresholds in config.py.

## Controls
- Pause: create a file named `SELF_IMPROVE_OFF` in the repo root.
- Logs: `logs/self_improve/<date>/`.
- Install as a Scheduled Task (starts at logon, restarts if it dies):

```powershell
Register-ScheduledTask -TaskName "options-engine self-improve" `
  -Action (New-ScheduledTaskAction `
    -Execute "C:\Users\Chudi\options-engine\.venv\Scripts\python.exe" `
    -Argument "C:\Users\Chudi\options-engine\self_improve.py" `
    -WorkingDirectory "C:\Users\Chudi\options-engine") `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn) `
  -Settings (New-ScheduledTaskSettingsSet -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit ([TimeSpan]::Zero))
```

## What already runs without it
The bot-side learning stack is live in the repo today and needs nothing
from this file: the nightly deep review (learn.py), the forward ledger
grading every sniper candidate at every target tier (forward_ledger.py),
and the coach agent that reconstructs the perfect scenario for every
imperfection and escalates recurring fixes (coach.py). The loop above adds
the last mile: turning escalated proposals into committed code while idle.
