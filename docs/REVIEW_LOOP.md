# The review loop

How to ask for an outside architecture review of this repo, and how to
validate what comes back. Every suggestion is a proposal. Nothing is merged,
pushed or deployed on the reviewer's word.

## The one rule

The outside reviewer never commits to this repo, never pushes and never
deploys. A proposal becomes code only when the owner applies it and the offline
gate passes on the owner's checkout. It goes live only when the owner pushes and
deploys it. A green gate is permission to deploy. It is not a deployment.

Nothing in the repo applies a review packet on its own. `validate_release.py`
does not commit, push or deploy. The unattended improvement engine in
`self_improve.py` is a separate path with its own fences: `AUTO_PUSH = False`,
and `run_checks` rejects any engine commit that touches a file in `PROTECTED`
(`fvg.py`, `config.py`, `.env`, `self_improve.py`).

## Roles

- **The outside reviewer** reads a source snapshot and returns a packet. It has
  no credentials, no remote and no deploy access. Never send it `.env`, runtime
  state such as `positions.json` or `state.json`, or the per trade record.
- **The owner's machine** applies the patch and runs the gate offline.
- **The owner** reads the evidence, pushes, deploys and runs the live checks.

## 1. Request a review

Send a source snapshot, not a clone with a remote attached. The review packet
and its builder `build_bundle.py` live in a sibling directory,
`kelbot-review-packet`, outside this repo.

Every request names four things. Leave one out and expect rework.

1. **The surface.** One per request.
2. **The constraint.** By default strategy behavior may not change. Paste the
   owner's standing constraints into the request: never tune on the frozen
   dataset, never type a number that does not trace to a report file, and print
   a before and after diff of which setups would alert for any change that
   touches signal eligibility.
3. **The regression owed.** Each fix ships with a test that fails on the old
   code and passes on the new code, registered in the gate. See "Adding a test".
4. **Out of scope.** If a proper fix needs a strategy change, the reviewer stops
   and says so instead of making it.

Risk critical surfaces and where they live:

| Surface | Main modules | Suites to watch |
| --- | --- | --- |
| Stops | `config.py`, `positions.py`, `sniper_book.py` | `test_hard_stop.py`, `test_pipeline.py`, `test_exit_styles.py` |
| Position book | `positions.py`, `sniper_book.py`, `forward_ledger.py` | `test_ledger_integrity.py`, `test_pipeline.py` |
| Alert provenance | `event_journal.py`, `fill_journal.py`, `instance_lock.py` | `test_event_recovery.py`, `test_journal_integrity.py`, `test_fill_journal.py`, `test_instance_lease.py`, `test_instance_lifecycle.py` |
| Calendar handling | `market_calendar.py` | `test_market_calendar.py` |

## 2. What comes back

- Findings, each with an id and a disposition. The comments in the gate tuple
  tie suites to work package ids (W00 through W07). Keep that convention.
- A patch.
- The regression tests for it.

A finding is closed by a test that fails on the old code. It is not closed by
the reviewer saying it is fixed.

## 3. Validate the packet before applying it

- The patch stays inside the surface the request named.
- Every finding has a test. Run each new test alone, with `BOT_TEST_MODE=1`,
  against the unpatched tree and confirm it fails.
- Read every hunk in a protected file line by line. In `self_improve.py` the
  only expected change is a new file name in the gate tuple. A change to
  `config.py` or `fvg.py` is a strategy change and needs an explicit owner
  decision, not a review finding.
- If signal eligibility is touched, the before and after alert diff is there
  and every line of it is explained.

Then apply the patch and commit locally. Commit first: the provenance test fails
on any module that git does not track, and a gate run on uncommitted code does
not describe a commit.

## 4. Run the gate offline

From the repo root, with the repo virtualenv interpreter:

```
python validate_release.py
```

`--timeout` sets the per suite deadline in seconds (default 180). `--output`
sets the evidence directory (default `release_evidence`).

What it does:

- Sets `BOT_TEST_MODE=1` and `MPLBACKEND=Agg`. Removes `API_MODE`,
  `LEARN_ENABLED`, the model API key, the Alpaca keys and the Telegram token
  from the child environment.
- Writes a `sitecustomize.py` into a temp directory on `PYTHONPATH`. It installs
  an audit hook that raises on any socket connect or host lookup that is not
  localhost. Every child interpreter that inherits that environment gets it,
  including grandchildren.
- Regenerates `release_manifest.json`, then runs every suite in the gate tuple
  one at a time. A suite past its deadline is recorded as exit 124 with "Suite
  exceeded deadline; no success claimed."
- Hashes every top level `.py` file before and after the suites. Any change
  sets `source_unchanged` to false.
- Runs `release_manifest.py --check` at the end.

It exits nonzero when manifest generation fails, any suite exits nonzero or
times out, a top level `.py` file changed during the run, or the final manifest
check fails. It does not start the bot.

Evidence in `release_evidence/`: one log per suite, `results.json` (rewritten
after every suite, so a crash still leaves a record), `SUMMARY.json` (`passed`,
`failed`, `skipped`, `source_unchanged`, `manifest_check_exit`,
`external_network_blocked`, `deployed: false`), `manifest_generation.log`,
`manifest_check.log`, and a copy of the manifest.

### What the manifest records

`release_manifest.py` builds the manifest at generation time: source commit,
parent, branch, a `clean` flag with the uncommitted file list, declared
requirements and installed versions, the declared values of the public config
tunables, deployment variable names with no values (`secrets_recorded: false`),
report lineage with byte hashes, the test inventory parsed from the gate tuple,
and a hash of every top level module. `hand_edited` is `false`. The file is
gitignored and rebuilt per release.

It does not claim the deployment runs the source commit. `railway up` uploads a
directory, so the platform records no commit. The `deployed` block is an
operator record kept in the script and marked `reported: true`,
`verified: false`. It does not claim any suite passed. Results belong to the
gate run.

### What the provenance test catches

`test_packet_provenance.py` fails when:

- the manifest is missing or does not regenerate byte for byte
- git gave no answer
- the required report is not `reports/chart_backtest_round6_session.json`, is
  not tracked, or its recorded hash is not the hash of its bytes
- a suite in the gate tuple is missing on disk
- a top level `test_*.py` is not in the gate tuple
- the manifest inventory differs from the gate tuple
- a module that calls the Telegram sender is missing from
  `test_no_em_dash.GUARDED`
- a recorded module hash no longer matches the file
- a module in the manifest is not tracked by git

## 5. Read the result

A nonzero exit is a refusal. Also refuse, by hand, when any of these is true:

1. **`skipped` is not zero.** `test_review_packet_gate.py` runs the packet's
   own suite. With no packet on the machine it prints
   `SKIP: no review packet at ...` and exits zero, so a deployed worker without
   the review kit still passes. The gate records that as
   `skipped_external_packet` and counts it in `skipped`, never in `passed`. The
   exit code stays zero, so check the number yourself. Point
   `KELBOT_PACKET_DIR` at the packet to run it.
2. **`clean` is false** in the manifest. The run did not describe a commit.
3. **A protected file changed** without an explicit owner decision.
4. **A new test did not fail** on the old code.

On research surfaces, an incomplete measurement is absent, not bad.
`grading_contract.evaluate_bars` reports `measurement_complete` and
`terminal_covered` as their own flags. `shadow_gate.evaluate` records
`disagreement: true` when the legacy gate passed and the shadow gate refused,
and always returns `changes_live_gate: false`. A disagreement goes to a human.
It is never an override.

## 6. Hand off, push, deploy

Only the owner does this, once, over the route the owner holds credentials for.
Before the first push, confirm the Railway project, service, environment and
volume, and whether a push triggers a deploy on its own. Then push, deploy and
run the live acceptance checks. The loop never touches the deployment volume.

## Adding a test

- Register the file by name, in double quotes, in the tuple inside
  `self_improve.run_checks`. `validate_release.py` runs only registered suites,
  and an unregistered top level `test_*.py` fails the provenance test.
- Put no closing parenthesis anywhere inside that tuple, comments included.
  `release_manifest._gate_tests()` finds the tuple with a pattern that stops at
  the first `)`. One inside breaks the parse and manifest generation exits with
  an error.
- Set `BOT_TEST_MODE=1` before any repo import.
- If you add a module that builds message text, add it to
  `test_no_em_dash.GUARDED`.

## Keeping review material private

- `.gitignore` excludes the reviewer handoff file patterns,
  `release_manifest.json`, `release_evidence/`, `recorder/`, `fills/`,
  `catalyst_data/`, `research_output/` and runtime state such as
  `positions.json`, `state.json` and `alerts_sent.jsonl`.
- `.railwayignore` and `.dockerignore` are identical today. Both exclude
  `kelbot-review-packet`, the old handoff directory, `measurements`,
  `release_evidence`, `recorder`, `fills`, `*.patch`, `*.zip` and runtime
  state. `railway up` uploads a directory, so these files decide what ships.
  Nothing checks that the two stay identical. Edit them together.
- A new review artifact path goes into `.gitignore` and both deploy ignore
  files in the same commit that creates it.

## Measuring rework

A round is one packet in and one gate run out. A finding is reworked when it
needs a second round: its patch failed the gate, or a later pass found a defect
inside the fix. Report per review: findings raised, findings landed in round
one, findings reworked, and skipped suites. The target is round one landings on
stops, the position book, alert provenance and calendar handling.

## First change under the loop

Issue #3, the hard stop, is the first risk critical change landed this way.

- **Request.** The issue named the surface (stops), the constraint (no change
  to the half, the give-back trail or the old-rules shadow) and the regression
  owed (an exit at -50).
- **Owner decision.** The default lives in `config.py`, a protected file, so
  the -50 default and keeping risk based sizing were decided by the owner, not
  taken from a review finding. The research behind it is in
  `docs/stop_loss_research.md`, including what it recommended that did not
  ship.
- **The change.** Each position stamps the stop that was live when it opened
  (`Position.stop_pct`). Open rows saved before that field existed reload
  through `positions.position_from_row` with the -90 they opened under, so a
  deploy never moves the stop on an open trade.
- **Validation.** Reviewers attacked the uncommitted diff from four angles:
  money paths, persistence contracts, alert text and the tests. Each finding
  had to survive a refutation pass before it was acted on. `test_hard_stop.py`
  was run against the tree from before the change to confirm it fails there,
  and it is registered in the gate, so `validate_release.py` runs it with every
  other suite.
