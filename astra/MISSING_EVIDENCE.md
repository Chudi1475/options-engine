# Missing evidence

Required by spec section 11. What is absent, why, what it costs, and what would
close it. Astra: "When the test cannot be run, state why and retain the blocker."

The rule applied throughout: an item stays on this list until someone has
actually looked. "Probably fine" is not a closure, and neither is "the code
says".

---

## M01. The deployed commit is unverifiable from the platform

**Absent:** proof that the running container is built from any particular
commit.

**Why:** deploys here use `railway up`, which uploads a directory. The platform
records a deployment id and no git commit. There is no build-from-repo link to
interrogate.

**Cost:** every claim of the form "the fix is live" is unfalsifiable. This is
Astra A01 in its sharpest form.

**Recorded as:** `release_manifest.json` marks
`deployed.reported=true, verified=false, matches_source_commit=false` and says
why. The deployment id **is** verifiable and is recorded.

**Closes when:** the service is connected to the git repo so deploys carry a
commit, or a build stamp is written into the image and echoed by `/status`.
The second is cheap and does not change the deploy method.

---

## M02. Platform topology has never been verified as a release step

**Absent:** a recorded inventory of every consumer of the production Telegram
token, taken as part of a release.

**Why:** it was never a step. It exists now as runbook step 7.

**Cost:** the singleton lock is blind to most of the list, so this inventory is
the only real control. Astra: "A Telegram conflict proves competing requests,
not their location."

**Partially offset:** an ad hoc inventory on 2026-09-08 found one project, one
service, `numReplicas: 1`, one instance; the desktop scanner task Disabled;
`recap.py` send-only; `tg-claude-bot` on a different token. That is an
observation, not a release step, and it does not substitute for one.

**Closes when:** runbook step 7 is executed and its output recorded.

---

## M03. The independent-volume poller cannot be detected at all

**Absent:** any mechanism that sees a second consumer on a different machine or
volume.

**Why:** by construction. An advisory lock only excludes cooperating processes
on the same filesystem.

**Cost:** the class of duplicate the lock was built for is only partly covered.

**What exists instead:** the enforceable half is "a non-owner never polls", and
that is enforced and tested. `instance_lock.CANNOT_DETECT` lists the blind spots
in machine-readable form so no owner-facing text can claim coverage it lacks.

**Closes when:** it does not, within this architecture. It is a permanent
limitation and is documented as one.

---

## M04. Railway rolling-promotion topology is unproven

**Absent:** a platform reproduction of two copies sharing one volume during a
deploy.

**Why:** Railway documents that it PREVENTS simultaneous active deployments
mounted to the same service volume. A local two-process test does not establish
the deployment topology.

**Cost:** the promotion path is designed against a scenario nobody has observed
on the platform.

**Posture taken:** the conservative one, per Astra: single service, no external
production pollers, controlled handoff.

**Closes when:** someone reproduces it on the platform, or the design stops
depending on it.

---

## M05. No contemporaneous historical option bid and ask

**Absent:** the real bid and ask for any specific contract at any past
timestamp.

**Why:** no free source provides it. Alpaca has historical option data from
February 2024, but its free feed is **indicative**, meaning quotes are modified,
and OPRA is restricted to subscribers. Bars and trades are not a bid and ask.

**Cost:** this is the single largest gap and it blocks several questions
outright. No option multiple in any case study can be verified. Contract
feasibility at a past timestamp is unverifiable. The -90 versus -50 stop
question cannot be settled. Every "modeled" mark stays unverified.

**Correction to an earlier claim:** the packet said "no free historical source
exists". Too broad, per A23. The accurate statement is the narrower one above.

**Closes when:** the documented latest-quote endpoint is tested against the
existing entitlement with an exact contract and date and the sanitized result
recorded, or the $99/month OPRA tier is bought. Neither is authorized. This is
W10.

---

## M06. No broker fills

**Absent:** a single confirmed execution.

**Why:** the bot has never placed an order and no broker export exists.
`USER_TRADE_EVIDENCE.csv` in the earlier packet is deliberately empty.

**Cost:** every performance figure in this entire record is a tracked signal
graded against the bot's own price feed. The bot models a position; it never
bought one. There is no quantity on any row, and the half-sale is an accounting
assumption a one-contract position could not execute.

**Closes when:** the manual fill journal (W07) collects real fills. Astra's
constraint holds: **do not ask the user to trade merely to create evidence.**

---

## M07. Live alerts and forward-ledger rows are disjoint

**Absent:** linkage between the 53 live sniper tickets and the 20 graded forward
candidates.

**Why:** 28 of 53 live tickets have no forward-ledger row, and all 20 graded
rows correspond to zero live alerts, because the position tracker did not exist
until 2026-08-09.

**Cost:** the target comparison (0.4R versus 1R versus 2R) is computed on a
cohort that is not the live book, and the two cannot be pooled.

**Fixed going forward:** W02's durable intent carries `candidate_id`,
`decision_id` and `position_id` through the commit, with an orphan report for
failures.

**Not recoverable historically.** Astra: "Do not guess historical IDs; any
recovered links need explicit provenance and ambiguity flags."

---

## M08. CLOSED. The session report's average R reconciles exactly

**Was absent:** an explanation for the gap between the reported +0.154R and what
the row outcomes imply.

**Closed 2026-09-09** by `grader_reconciliation.py`, which reproduces the report
from its own 53 out-of-sample rows:

| exit | n | mean R | identical? |
|---|---|---|---|
| `tp` | 43 | **+0.4000** | yes |
| `stop` | 8 | **-1.0000** | yes |
| `eod` | 2 | **-0.5160** | no |

Total +8.168R, average +0.1541R. Both match the report to the published
precision.

**Why it looked unreconcilable:** Astra's arithmetic assumed ten full 1R losses,
giving +0.13585R. There are eight full stops and **two end-of-day exits that are
not flat**, averaging -0.516R. The loss side totals -9.032R, not -10R.

**A hypothesis this killed:** I expected stop overshoot, a gap through the stop
booking worse than -1R. Refuted. Every out-of-sample stop is exactly -1.000R,
which means the simulator books stops **at the threshold**. Per A18 that is an
idealisation, not a conservatism, and a real gap can fill worse. That limitation
now has a name.

**What it exposed, and this is the bigger item:** the backtest and the live book
count session-end exits differently. The backtest puts its `eod` rows in the
denominator (43/53 = 81.1%). I excluded the live book's four session-end rows
(32/49 = 65.3%) arguing a flat is not a loss, when the backtest's own equivalent
rows average -0.516R. **On the backtest's convention the live rate is 60.4%, so
the live-versus-backtest shortfall is 20.7 points, not the 15.8 I published.**
Every table now states its convention.

## M09. Exit-time quotes are not recorded

**Absent:** the quote at the moment of every exit.

**Why:** the recorder captures entry quotes only.

**Cost:** every exit-side hypothesis is untestable. The give-back-versus-spread
question cannot be answered at all. Whether a modeled stop mark was near an
executable price is unknowable.

**Closes when:** W06 lands. This is the cheapest high-value fix on the list.

---

## M10. The opposite and alternative contracts are not observed

**Absent:** any path for the contract not chosen.

**Why:** the bot only ever watched what it picked.

**Cost:** the random-direction control cannot be computed, so **the entry-edge
question cannot be answered at all today**. The SPX/SPY strike-grid hypothesis
cannot be tested. MFE is censored by the current exit, so "never reached 2R" is
not evidence against a larger target (A15).

**Closes when:** W06 records `chosen`, `direction_control` and named
`strike_control` roles through a common horizon.

---

## M11. No rejected-candidate ledger for the momentum lane

**Absent:** the momentum equivalent of the sniper's 233-row candidate ledger.

**Why:** it was never built.

**Cost:** the momentum lane's selection rate is unmeasurable, so the denominator
behind its win rate cannot be audited.

**Closes when:** W06 extends recording to every completed input-bar opportunity,
per strategy, symbol and direction.

---

## M12. Per-call AI cost is not logged

**Absent:** model, tokens, estimated versus billed cost, latency and failure
category per call.

**Why:** only a per-purpose daily count exists.

**Cost:** spend cannot be attributed, and there is no dollar cap on human chat.

**Closes when:** a row is written at the existing `api_note_call` choke point.
Constraint: **no prompt content**, ever.

---

## M13. fsync is off

**Absent:** proof that a completed write survives a machine crash.

**Why:** `storage_io` supports `durable=True` but defaults to off everywhere
except the review-import batch append. Turning it on needs a latency measurement
on the Railway network-backed volume, which cannot be taken from the desktop.

**Cost:** a machine crash can still lose a write the OS had not flushed. Current
behavior matches the old code exactly, so this is not a regression, but it is
not closed either.

**Closes when:** the measurement is taken on the deploy volume.

---

## M14. Journal retention is unbounded

**Absent:** a retention policy for the daily event journal.

**Why:** deliberate for this release. Daily files are never rotated or deleted.

**Cost:** a 500 MB volume is finite.

**Partially offset:** `health()` reports `bytes_used`, so growth is measurable
before a cutoff is chosen.

**Closes when:** a number is chosen, with a durable count of what was rotated.

---

## M15. The catalyst case studies are unverified

**Absent:** exact event dates, public discovery times, contract IDs and
obtainable quotes for the LULU, DELL, NVDA and HOOD stories.

**Why:** they came from screenshots and recollection. The screenshot CSV
contains no usable rows. The Burry research is missing entirely.

**Cost:** they are hypotheses, not evidence, and must not enter any denominator.

**Astra's instruction, followed:** "Do not fill these gaps with a plausible
narrative."
