# Corrected status, 2026-09-09

Required by spec section 11. This is the honest state at the moment work
stopped, which was forced by the owner's API limit, not by completion.

**Release decision unchanged: HOLD. Nothing is deployed.** Railway still runs
`3c91c3bf`, now many commits behind. Its service source is null, so no push can
deploy.

## Work packages

| Package | State | Evidence |
|---|---|---|
| W00 bind evidence | **done, committed** `e254b2e` | `release_manifest.py`, `test_packet_provenance.py` |
| W05 consistent storage | **done, fixed, committed** `4a8b8bc` | `storage_io.py`, `test_storage_faults.py` |
| W01 exclusive ownership | **done, fixed, committed** `4a8b8bc` | `instance_lock.py`, `test_instance_lifecycle.py` |
| W02 recoverable events | **done, fixed, committed** `4a8b8bc` | `event_journal.py`, `test_event_recovery.py`, `test_journal_integrity.py` |
| W03 grading jobs | **built, 27 findings open, committed as-is** `6c793e0` | `test_grading_jobs.py` |
| W04 review and billing isolation | built, findings open, `6c793e0` | `test_import_recovery.py`, `test_probe_dispatch.py` |
| W06 recorder | built, findings open, `6c793e0` | `trade_recorder.py`, `test_trade_recorder.py` |
| W07 fill journal | built, findings open, `6c793e0` | `fill_journal.py`, `test_fill_journal.py` |
| W08 shared grading contract | **not started** | |
| W09 shadow gate and edge replay | **not started** | |
| W10 market data correctness | **not started** | |
| W11 catalyst lane | **not started** | |

## The 27 open findings against batch B

All in `astra/_batchB_findings.json` with file, claim and scenario. Confirmed
by independent refutation; 2 of 29 raw were refuted. Severity: 10 high, 11
medium, 6 low. The ones that matter most:

1. Every record-1 row the live scanner writes carries `decision_id: null`, and
   the null's reason is factually false.
2. The recorder's "durable loss counter" is not durable: rebuilt from zero at
   import, never reloaded.
3. Two disjoint `candidate_id` namespaces are written into one record set.
4. The fill lane's denominator omits the sniper lane entirely.
5. A carried Black-Scholes mark is recorded with `is_model: false`.
6. `scoreboard()` stamps the new session-close horizon on rows graded under the
   old midnight walk, which are never regraded.
7. A candidate recorded inside the session's last 5m bar is permanently
   ungradable but classified retryable, so its day's job never completes.

None of these are fixed. Batch B should not be trusted until they are.

## Release checklist (section 10): 0 of 10 executed

Runbook written (`RUNBOOK.md`). No step has been run.

## Return package (section 11)

| Export | State |
|---|---|
| Corrected status | this file |
| Finding disposition A01 to A23 | `FINDING_DISPOSITION.md`, done |
| Policy change matrix | `POLICY_CHANGES.md`, covers batch A only |
| Runbook | `RUNBOOK.md`, done |
| Recorder schema | `RECORDER_SCHEMA.md`, done; W06 deviates in 5 places, listed in the batch B result |
| Coverage report | `recorder/coverage.json` exists from W06, unreviewed |
| Grader reconciliation | `grader_reconciliation.json`, done, closes M08 |
| Shadow gate disagreements | **not built** (needs W09) |
| Study preregistration | `PREREGISTRATION.md`, done |
| Cost report | `COST.md`, done; M12 per-call logging was added in W04, unreviewed |
| Missing evidence | `MISSING_EVIDENCE.md`, done, M08 closed |

## Verification gaps, stated plainly

- Batch A's fix round: the four re-verify lenses hit the API limit. Those 19
  fixes carry fail-first regressions and my own checks (dry-run tripwire: zero
  outbound HTTP; torn news_seen: not a blocker). No independent pass.
- Batch B: reviewed, 27 confirmed, **unfixed**.
- The full gate is green at 26 suites, 1000+ declared cases. Three consecutive
  rounds proved a green gate hides real defects here. Treat it as necessary, not
  sufficient.

## Corrections to my own earlier reporting, made during this work

- Wilson lower bound of 43/53 is 68.6413%, not 67.4%. No quoted cohort clears
  the 71.43% breakeven at its lower bound; the live book's is 51.3%.
- The session report reconciles exactly. Its end-of-day rows average -0.516R,
  not 0. On the backtest's own denominator the live sniper rate is 60.4%, so
  the live shortfall is 20.7 points, not 15.8.
- "All deep losses are pricing artifacts" was causal overreach (A07).
- The 46/20 stop-overshoot figures mixed two cohorts (A08).

## What has not changed

`fvg.py`, `strategy.py`, `live_params.py` untouched. No exit or gate constant
in `config.py` moved. `MIN_WINRATE` 70. `API_MODE` ask_only.
`LEARN_ENABLED` false. Models unchanged. No order execution, ever.
