# Cost report

Required by spec section 11, written to section 9's constraints.

Two cost surfaces exist and they are not the same money:

1. the metered Anthropic API key the bot uses to think
2. the Railway service that keeps it running

Neither is trade P&L, and section 9 is explicit that **fixed operating costs
belong beside trade P&L when discussing account growth.** A $500 account
carrying a fixed monthly cost is not the same instrument as one that is not.

---

## 1. What the AI key is, and why it has a policy at all

The bot's `ANTHROPIC_API_KEY` is **metered pay per use. It is not the owner's
subscription.** It has hit $0 more than once, and when it does the chat brain
goes down. The owner's standing rule is that the credit is a last resort.

There was also a live failure worth recording, because it is why the probe
purpose exists at all: a billing hold only ever cleared when some other call
happened to succeed. With scheduled work off and nobody chatting, nothing
called, so the bot reported "credits empty" for days after the account had been
topped up, and the status line stated that stale verdict as current fact.
Confirmed at the time against an account holding roughly $4 with 7 cents spent
for the month.

## 2. The policy, as implemented

Every paid call declares a **purpose** and passes one choke point,
`config.api_allows(purpose)`.

| Purpose | What it is | Spends under the default? |
|---|---|---|
| `chat` | a human texted and is waiting | **yes** |
| `scheduled` | the bot decided by itself: nightly review, the deep-review backlog, the coach, the morning web-search news check, the breaking-news auto-read | **no** |
| `probe` | a one-token ping asking whether the balance is back | yes, whenever the API is not switched off outright |

`API_MODE` selects the policy. Values: `off`, `ask_only`, `full`.
**Default and current production value: `ask_only`.**

- `ask_only` answers humans, never spends on scheduled work
- `full` lets scheduled work spend too, still under the daily cap
- `off` spends nothing; every caller takes its offline path

`API_MAX_CALLS_PER_DAY` defaults to **60** and caps **scheduled work only**.

### Why the cap does not apply to a human

Deliberate, and it fixed an observed failure. One chat turn is several tool
round-trips, so a shared ceiling went silent on the owner after about nine
messages and reported it as a spending limit. Probes are exempt for the same
class of reason: capping them would strand the brain offline for a whole day
with no way back.

**Astra section 9, accepted without argument: a call-count cap on scheduled work
is not a dollar cap on human chat.** Nothing in this design bounds spend in
dollars. A long chat session is unbounded by this policy. That is a real gap and
it is named here rather than left implied.

### Refused work is not lost work

Scheduled reviews move to the desktop: `learn.py --export-backlog` and
`--import-reviews` run the same per-trade reviews on the owner's own machine,
where the reasoning is already paid for. 102 trades were reviewed that way and
imported, and the pending backlog went to zero.

## 3. Current production settings

| Setting | Value | Changed by this release? |
|---|---|---|
| `API_MODE` | `ask_only` | **no** |
| `API_MAX_CALLS_PER_DAY` | 60, scheduled only | **no** |
| `LEARN_ENABLED` | `false` | **no** |
| `BOT_BRAIN_MODEL` | `claude-opus-5` | **no** |
| `BOT_DEEP_MODEL` | `claude-opus-5` | **no** |
| auto-reload | not enabled | **no** |

Astra section 9: "Keep `LEARN_ENABLED=false` and existing API spending policy
unchanged. Do not turn on auto-reload or switch models as part of this update."
Confirmed: none of the above moves in this release.

## 4. What is NOT yet logged, and it is required

Section 9 asks for per-call logging of: **purpose, model, requested and actual
tokens where returned, estimated versus billed cost, latency, and failure
category** without prompts containing private data or secrets.

**Today the bot logs a call COUNT per purpose per ET day, and nothing else.**
`config.api_counts()` returns `{date, chat, scheduled}` and
`api_usage_line()` renders one line for `/health`.

So the following cannot currently be answered:

- what any single call cost
- which model served it
- how many tokens went out and came back
- how estimated cost compares to billed cost
- how long calls take, and how that varies
- which failure category dominates

That is a genuine gap against section 9 and it is **open**. Closing it is a
small recorder beside the existing choke point: `api_note_call` already runs on
every paid call and is the natural place to write a row.

**Constraint on closing it, from section 9: log no prompt content.** Purpose,
model, token counts, cost, latency and failure category only. The chat brain
handles the owner's private material and a cost log is not a place for it.

## 5. Railway

| Item | Value |
|---|---|
| Project | `options-engine`, one service, one environment |
| Region | sfo |
| Replicas | 1 |
| Volume | 500 MB, mounted at `/data`, roughly 75 to 84 MB used |
| Restart policy | ALWAYS |
| Deploy method | `railway up` (directory upload, no git commit recorded) |

**Dollar figures are deliberately absent.** No billing page was read for this
report, so any monthly number here would be typed rather than observed, which is
exactly what the house rule forbids. It should be filled in from the account.

The 500 MB volume is a real constraint, not a formality. Section 4: it "cannot
be treated as unlimited retention". The event journal added in W02 has
**unbounded retention by design for this release** and reports `bytes_used` in
`/health` so growth can be measured before any cutoff is chosen. Choosing that
cutoff needs a number and a durable count of what was rotated, and neither
exists yet.

## 6. Data purchase, evaluated not authorized

| Option | Price | What it would buy |
|---|---|---|
| Alpaca Free | $0 | indicative option quotes, which are modified and **cannot verify a real historical bid and ask** |
| Alpaca Algo Trader Plus | $99 / month | real-time OPRA |

Astra section 9 and finding A23: no subscription or credential test has been
performed, and the pricing page's general historical coverage is **not** proof
that every option quote endpoint or index contract is available. Option bars and
trades cannot substitute for a contemporaneous bid and ask.

**Nothing is authorized here.** The next step is narrow and cheap: test the one
documented endpoint
(`GET https://data.alpaca.markets/v1beta1/options/quotes/latest`, with explicit
`feed=opra` or `feed=indicative`) against the existing entitlement with an exact
contract and date, and record the sanitized status and coverage either way,
including "unavailable". That is W10.

$99 a month against a $500 account is a 19.8% annualized drag before a single
trade. It belongs in the account-growth arithmetic, not in a footnote.

## 7. The honest summary

- **Changed by this release: nothing.** Mode, cap, models, learn flag all hold.
- **Known gap: per-call cost logging does not exist.** Counts only.
- **Known gap: no dollar cap on human chat**, by design, with a reason.
- **Known gap: journal retention is unbounded** on a 500 MB volume.
- **Not authorized: any data purchase.**
