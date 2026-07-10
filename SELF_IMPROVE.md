# The always-on improvement loop (waiting on your go-ahead)

One piece of the autonomy stack cannot install itself: a forever-running
wrapper that launches headless Claude Code sessions against this repo while
you are away. Claude's safety layer (correctly) requires the owner to
approve creating an unattended agent with code-execution rights, so this
file is the reviewed design. To build it, open Claude Code in this repo
interactively and say: "build self_improve.py exactly per SELF_IMPROVE.md"
and approve the prompts.

## What it does, each cycle
1. Runs one headless Claude session with SCOPED permissions (file tools +
   python + git only, no --dangerously-skip-permissions) and one job: pick
   ONE improvement from BACKLOG.md / data/lessons_digest.md /
   data/coach_proposals.jsonl / code health, implement it, verify offline
   (py_compile + test_pipeline.py + test_adduser.py), commit.
2. The wrapper re-runs those same gates itself and `git revert`s the commit
   if anything fails. It never trusts the session's word.
3. Texts the owner a one-line summary per cycle via the bot's Telegram token.
4. On a Claude usage-limit message: texts you, counts down exactly 5 hours
   5 minutes, resumes on its own.
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
