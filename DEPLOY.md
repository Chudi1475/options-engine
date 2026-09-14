# putting the bot in the cloud

A bot that dies when your PC sleeps collects no evidence. In the cloud it
runs 24/7: morning risk report, entry alerts, every exit alert, the 3 PM
recap-style monitoring, weekly scoreboard, and Telegram commands answered
around the clock.

**Cost: $5/month flat on Railway** (the bot uses ~$1-2 of the $5 usage
credit that comes with the Hobby plan, so you never pay overage).

## IMPORTANT: only run ONE copy of the bot

Two copies = double alerts and broken Telegram commands (Telegram lets only
one reader per bot token). When the cloud bot is live, turn the local
scheduled tasks off:

```powershell
Disable-ScheduledTask -TaskName "options-engine scanner"
Disable-ScheduledTask -TaskName "options-engine recap"
```

(Re-enable them with `Enable-ScheduledTask` if you ever leave the cloud.)

## Railway, step by step (one evening, mostly waiting)

1. Install the CLI (needs Node, or use scoop):

```powershell
npm i -g @railway/cli
```

2. Log in (opens your browser — create the account there if you don't have
   one, then add the $5/mo Hobby plan):

```powershell
railway login
```

3. From the repo folder, create the project and set your secrets:

```powershell
cd C:\Users\Chudi\options-engine
railway init
railway variable set TELEGRAM_BOT_TOKEN=<your token from .env>
railway variable set "TELEGRAM_CHAT_IDS=<ids from .env>"
railway variable set DATA_DIR=/data
```

4. Add a persistent volume so positions/state survive redeploys:

```powershell
railway volume add --mount-path /data
```

5. Ship it and watch the logs:

```powershell
railway up --detach
railway logs
```

You should see "Daemon mode: running around the clock." Text the bot
`/status` — if it answers, the cloud bot is alive. Then disable the local
tasks (step 0 above).

Updating later: make changes locally, run `railway up --detach` again.
The volume keeps positions.json and state.json across deploys.

Notes:
- `railway.json` already tells Railway how to run the bot
  (`python -u scanner.py --daemon`, restart on crash, no web port needed).
- The repo also has a `Dockerfile` + `Procfile`, so the same code runs
  anywhere else (Render workers, Fly.io, a $5 VPS) without changes.
- The daily 3:05 PM recap stays a separate local task for now; the cloud
  bot covers everything else. To move it to the cloud too, ask for a cron
  service later — or just leave the local recap task enabled (it only
  reads data and sends one text, so it can't double-alert trades — but it
  DOES need positions.json, which lives in the cloud volume once you
  migrate. Simplest: run recap from the cloud as a second service, or keep
  the PC awake at 3:05 PM weekdays).

## faster eyes: Alpaca keys (free, 2 minutes)

The bot polls yfinance (~1 min delayed). With free Alpaca keys it
automatically switches stock tickers (QCOM, TSLA) to Alpaca's real-time
IEX feed — same signal logic, fresher prices. SPX has no Alpaca feed on
any tier, so it stays on yfinance either way.

1. Sign up at alpaca.markets (paper account is fine, no money needed)
2. Dashboard → "API Keys" → generate
3. Locally: put them in `.env`; cloud:
   `railway variable set ALPACA_API_KEY=... ALPACA_API_SECRET=...`
4. Restart the bot. `/status` will show "alpaca (real-time)" for stocks.
