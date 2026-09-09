# Deployment and rollback runbook

Required by spec section 11. Built to satisfy section 10's ten numbered steps,
in that order, plus the section 3 topology posture.

**This runbook does not authorize a deployment.** Astra: "This review does not
authorize deployment; return the concrete result for sign-off under the packet's
own release rule." Every step below produces evidence to be reviewed, and step 8
is where it stops until someone signs.

Two things are true at once and both must stay visible: the bot has run
unattended for months, and the code about to replace it changed the ownership
model, the send/persist order and every file write. The old version's stability
is not evidence for the new one.

---

## 0. Preconditions, checked before anything else

| Check | How | Blocks release if |
|---|---|---|
| Working tree clean | `git status --porcelain` empty | not empty |
| Manifest current | `python release_manifest.py --check` exits 0 | non-zero |
| Full gate green on Linux | see step 3 | any suite fails |
| No open modeled positions | `/status`, and `positions.json` has no `state != closed` row | a position is open and the window is not a planned interruption |
| No pending actionable events | journal `open_intents` empty, orphan report empty | anything unresolved |

The position check is section 10.8: deploy "when there are no open modeled
positions or pending actionable events where practical". If it is not practical,
the interruption's limits get written down, not waved past.

---

## 1. Freeze the candidate

```
cd C:\Users\Chudi\options-engine
python release_manifest.py
git rev-parse HEAD          # the candidate commit
git rev-parse HEAD^         # its parent
```

`release_manifest.json` records commit, parent, branch, clean flag, installed
dependency versions, sanitized config, report lineage, byte hashes of every
module, and the test inventory. It is generated, never hand written.

**Do not claim all files are unchanged from the reviewed snapshot.** Astra 10.1
says so explicitly, and A02 says an unchanged strategy-file list is the wrong
criterion anyway. The policy-change matrix in `POLICY_CHANGES.md` carries the
per-package behavior deltas instead.

The deployed commit is recorded SEPARATELY and marked
`reported: true, verified: false`, because `railway up` uploads a directory and
the platform stores no git commit. That gap is real and is not papered over.

---

## 2. Report provenance

The live sniper policy is the session-constrained round-6 config, so the
required report is `reports/chart_backtest_round6_session.json` and the full
round report is background. `test_packet_provenance.py` fails if the required
report is missing, untracked, or hashes differently from the shipped bytes.

**Do not rename another report to satisfy a builder.** If a summary artifact is
generated, its parent hash is recorded. The missing
`chart_backtest_round6_summary.json` is a provenance task, and it is NOT a
reason to run another tuning round on an exhausted dataset.

Open reconciliation, carried in `MISSING_EVIDENCE.md`: the session report's
43/53 and +0.154R do not reconcile from row outcomes alone. 43 full 0.4R wins
against ten full 1R losses implies +0.13585R, so session exits or a payoff
difference must account for the rest. Unreconciled means unreconciled.

---

## 3. Run the required suite on Linux

The deploy target is Linux; the desktop is Windows. Both matter, for different
reasons.

```
# on the Linux target (container or equivalent image)
export BOT_TEST_MODE=1
time python -c "import release_manifest as r; print(' '.join(r._gate_tests()))" \
  | xargs -n1 -I{} sh -c 'python {} >/dev/null 2>&1 && echo "PASS {}" || echo "FAIL {}"'
echo "exit: $?"
```

Record: exact command, exit code, named suites, **suite count and case count as
separate numbers**, environment, elapsed time.

Windows-specific writer tests run on Windows, because Windows remains a
supported run mode and the `tmp.replace` PermissionError only reproduces there.

**Rerun-until-green is not a fix.** Astra 10.3. The intermittent replace fault
is now handled by bounded retries in `storage_io`, and `test_storage_faults.py`
asserts the retry bound rather than relying on a lucky run. If a suite fails,
the failure is the result.

---

## 4. Deterministic failure injection

Not a smoke test. Each of these is injected and the resulting FILES and delivery
stubs are inspected, not just a returned boolean:

- kill before and after every persist and every send boundary
- fail a write, fail a reload
- block the billing worker
- truncate one input frame
- lose a send acknowledgment (the `unknown` delivery state)
- inject a retired grading row beside a valid one
- stop the lock owner mid-session
- verify no stale promotion after any of the above

Covered by `test_storage_faults.py`, `test_event_recovery.py`,
`test_instance_lifecycle.py` and `test_grading_jobs.py`. The point of listing
them here as well is that a release runs them deliberately and records what the
files looked like afterwards.

---

## 5. Prove policy preservation

This is the answer to A02 and it is the step that replaces "the strategy files
are untouched".

Replay one fixed recorded input stream through the OLD and CANDIDATE versions,
each with its own temporary state directory and **no network sends**. Compare:

- candidates generated
- gate eligibility decisions
- contracts chosen
- sizes
- exit triggers
- schedule decisions

**Every difference needs an ID, a causal explanation and an approved
classification.** No strategy tuning on this fixture, ever. A difference nobody
can explain blocks the release; it does not get classified as noise.

---

## 6. Stage recovery

Synthetic fixtures plus a PRIVATE COPY of historical state. Never the live
volume.

Validate: schema migration and rollback across open positions, partly sent
events, reviews and pending jobs. Test restoring from backup.

**Missing state is not an empty book** unless this is explicitly a first
initialization. That distinction is now enforced in code (`storage_io` returns
`missing` / `unreadable` / `corrupt` rather than an empty list) and it must be
verified here against real historical shapes.

---

## 7. Check release topology

An explicit operator step, and the one thing no code in this repo can do for
itself. Inventory every consumer of the production token:

- [ ] Railway service and its active deployment
- [ ] replica count (`numReplicas`)
- [ ] any second Railway project or environment
- [ ] desktop Task Scheduler entries
- [ ] ad hoc scripts and `getUpdates` utilities
- [ ] webhook configuration
- [ ] any old binary still running anywhere

Record what was OBSERVED, not what was assumed. **A Telegram 409 proves
competing requests, not their location**, and the local advisory lock is blind to
every item on that list except two processes sharing one filesystem.

Railway documents that it prevents simultaneous active deployments mounted to
the same service volume, so the shared-volume rolling-promotion story is
UNPROVEN on the platform. The posture this release ships is the conservative
one: a single service, no external production pollers, a controlled handoff.

**Do not deploy a second live scanner as a canary.** A replay or paper sender
needs its own storage and its own credentials.

Do not wait for ACTIVE readiness in a way that prevents the old owner from ever
being stopped.

*Observation from 2026-09-08, recorded because it is the current known state and
not because it substitutes for running this step at release time:* one Railway
project, one service, `numReplicas: 1`, one instance RUNNING; the desktop
`options-engine scanner` task Disabled; `recap.py` send-only; `tg-claude-bot` a
different bot on a different token. The 409 observed that day was the offline
test suite polling the live token, now fixed at the wire.

---

## 8. STOP. Prepare deployment evidence for review

Assemble: candidate package, proposed maintenance window, and the limitations of
any interruption.

**This is where the runbook halts.** Deployment needs a human sign-off under the
packet's own release rule. Nothing below runs until that happens.

---

## 9. After an approved deployment, verify separately

Confirm, as separate observations:

- the actual running commit
- ownership state (must be ACTIVE, not merely started)
- loaded positions match what was there before
- pending delivery reconciliation completed, orphan report empty
- effective AI settings (`API_MODE=ask_only`, `LEARN_ENABLED=false`)
- monitoring freshness

**A startup banner is not verification.** No live order and no group message is
required to prove a synthetic test; use the ops DM path and the health line.

---

## 10. Rollback

In this order, and the order matters:

1. **Halt candidate writers first.** Stop the process before touching data.
2. **Never blindly restore an old data snapshot over events created after the
   update.** Events written by the candidate are real events.
3. **Roll back code only if the old code can read the current schema.**
   Otherwise use the tested migration and reconciliation plan from step 6.
4. **Preserve the journal and every real fill record.** Those are primary
   evidence and are not regenerable.

Rollback is BLOCKED, and continued release use stops, on any of:

- unexpected open-position loss
- duplicate ownership observed
- unrecoverable writes

---

## Performance gates

Measured, not assumed:

| Gate | Requirement |
|---|---|
| Observation coverage | recorded, with gaps explicit |
| Unresolved status counts | bounded and reported |
| Orphan links | zero, or named |
| Duplicate logical events | zero |
| State conservation across restart | exact |
| Queue loss | counted durably, never silent |
| Monitor latency | p95 and max, against the 15s poll cadence |

No synchronous billing work and no synchronous recorder work in the monitoring
path. A finite timeout and a bounded queue must be TESTED, not assumed.

**A short shadow soak can find implementation faults. It cannot prove a trading
edge, and nothing in this runbook should be read as evidence of one.**
