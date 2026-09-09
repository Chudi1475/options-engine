# Astra findings A01 to A23: evidence and disposition

Required by the spec, section 11: "one row per finding with its evidence and
disposition". One row each, no bundling.

Disposition vocabulary, used strictly:

- **accepted** the finding is correct and my packet was wrong. Correction stated.
- **accepted, fixed** correct, and the code or document now reflects it, with the
  regression named.
- **accepted, scheduled** correct, and the fix belongs to a named work package
  that is not finished yet.
- **already true** the requirement was already satisfied before the review, with
  the evidence that shows it. Not a claim that the finding was wrong to raise.
- **partly accepted** part of the finding lands, part does not. Both halves stated.
- **cannot verify yet** I agree it is unresolved and I do not yet have the
  evidence to close it either way.

No finding is marked closed on the strength of a comment saying it is fixed.

---

| ID | Astra's finding, compressed | Disposition | Evidence and what changed |
|---|---|---|---|
| A01 | All remaining repairs are reported, not independently verified. Supply commit, diff, test commands, results, sanitized deployment snapshot. Distinguish implemented / reproduced / deployed. | **accepted, fixed** | This was the correct blocker. `release_manifest.py` now generates `release_manifest.json` from the repository: source commit, parent, branch, clean flag, installed dependency versions, sanitized config, report lineage, byte hashes of every module, and the test inventory. `test_packet_provenance.py` fails when the manifest and the repo disagree. The three states are explicit: `deployed.reported=true`, `deployed.verified=false`, `deployed.matches_source_commit=false`. |
| A02 | "No trading behavior changed because four strategy files were untouched" is the wrong criterion. Scheduling, feed composition, quote fallback, duplicate suppression and restored positions all affect alerts. Prove it with identical input replay. | **accepted, scheduled** | The criterion was wrong and I used it. Unchanged strategy files remain a necessary condition, not a sufficient one. Identical-input replay is release checklist item 5 and is built in W09 (`edge_replay.py`). Until that runs I make no unchanged-behavior claim; behavior changes are listed per package instead. |
| A03 | ACTIVE-DEGRADED alerting when no lock primitive exists is an unsafe default. Unknown ownership is not exclusive ownership. | **accepted, scheduled (W01)** | Correct, and it contradicted my own rejection of blind resume in the same subsystem. Fail-open is being removed and replaced by the five state contract from spec section 3, with BLOCKED as the terminal state for unknown ownership. |
| A04 | A local advisory lock does not protect against another machine, service, copied volume, old binary, or desktop Task Scheduler. Find the actual second Telegram consumer. | **accepted, fixed** | I ran the inventory. It is NOT another deployment: one Railway project, one service, `numReplicas: 1`, one running instance; the desktop Task Scheduler `options-engine scanner` entry is **Disabled**; `recap.py` never polls; `tg-claude-bot` is a different bot on a different token (compared by hash, never printed). The real cause: `telegram.get_messages` checked the lease but never `test_mode()`, so the offline suite polled the **live** token. That produced the 409 and, because a poll acknowledges updates through the offset, could swallow a command the cloud copy then never answered. Fixed at the wire with `allow_test_poll` opt-in; regression in `test_review_regressions.py`, proven fail-then-pass. |
| A05 | Marking selected after card and position still leaves crash windows. Durable intent, retries and reconciliation required. Never infer delivery from position existence. | **accepted, scheduled (W02)** | Correct. Ordering removed one blocking dependency; it never made the linkage durable. Event intent persisted before sending, per-recipient delivery records, orphan report. |
| A06 | Releasing the day key is compensation, not a transaction. It can race a newer claimant or release after an ambiguous successful send. | **accepted, scheduled (W02)** | Correct, and it directly supersedes the fix I shipped for this yesterday. Reservations will carry operation identity so only the owning operation can release one. |
| A07 | "All deep losses are pricing artifacts, not slippage; moving the stop would not have prevented any" is unsupported causal language. | **accepted** | My overclaim. What the data supports: every whole-position loss past -90% that carries an estimate-labeled mark is deeper than any quote-labeled one, minima -99.69% and -91.11%. What it does not support: that the estimate was too low, or that a tighter stop would not have been crossed. Corrected wording is in the reissued packet. |
| A08 | The 46 stop exits and 20 overshoots are different cohorts and must be scoped. A reason code is not a threshold-crossing measurement. | **accepted** | Correct. 46 stop-tagged exits across 157 rows; 29 across the 127-row July to September cohort. 20 whole-position returns below -90%, 21 final-leg returns. `measurements/stop_overshoot.py` will state the cohort and the leg basis on every figure. |
| A09 | SPX/SPY numbers reproduce on all 157 records; the 127-row cohort differs. Name the cohort in every table. | **partly accepted** | The arithmetic is confirmed by Astra on the same 157 rows, which is the reproduction I wanted. Accepted: I did not label the cohort. Every table now carries its row count and date range. |
| A10 | Medians are $996.50 and $97.50 on the full record, $981 and $91 July to September. Do not combine a full-record median with a 38-row subset count. | **accepted** | Correct, I rounded and then mixed bases. Both cohorts now labeled. |
| A11 | Ratios 2.199 and 1.985 are consistent with the mechanism but do not identify the cause. | **accepted** | My wording claimed identification. Liquidity, exposure, time and quote quality remain confounders. Restated as a prospective hypothesis, tested by recording a fixed same-direction alternative (W06). |
| A12 | "Opposite-strike entry quote" is ambiguous. A put tests direction; an alternative SPY call tests strike selection. Record explicit roles. | **accepted, scheduled (W06)** | Correct and it sharpens the design. Contract roles are `chosen`, `direction_control`, and named `strike_control`, per spec section 5. |
| A13 | 0.4R at breakeven mixes datasets: 32/49 = 65.306% live, 14/20 = 70% historical forward. | **accepted** | Correct, I conflated them. They are different cohorts and are now always reported separately. |
| A14 | Forward expectancies assume unresolved targets lose 1R. One 1R and two 2R rows are unresolved. Keep it as a sensitivity scenario. | **accepted** | Correct. Reported as resolved-only means with their true denominators (20 / 19 / 18) plus the explicit substitution scenario, never as measured terminal returns. |
| A15 | MFE is censored by the current exit, so "never reached 2R" cannot rule out a larger target. | **accepted** | A clean catch I had missed entirely. It invalidates the inference I drew. Fixed only by recording through a common horizon (W06). |
| A16 | Wilson 95% lower bound of 43/53 is 68.6413%, not 67.4%. | **accepted, fixed** | I carried 67.4% forward from the earlier brief without re-deriving it. Recomputed and corrected. The bound still does not clear 71.429%, which was the point either way. |
| A17 | 53 trades does not meet the 60-trade floor; it is a fixed-configuration measurement, not a fresh validation. Reconcile +0.154R. | **accepted** | Correct on both. The report was always labeled a measurement; I then quoted it as though it qualified. The avg R reconciliation is an open item, tracked in the missing-evidence export. |
| A18 | Next-bar entry removes one ordering issue, not all. Zero changes on one sample does not prove finer data can never matter. | **accepted** | Correct. The claim is narrowed to what was observed on that sample. |
| A19 | The gate used the old report for win rate but the new report's expectancy when available. Verify current behavior before replacing it. | **cannot verify yet** | I asserted a simpler story than the code. Re-reading `gate_stats` against the release commit and recording each statistic's report and policy fingerprint separately is part of W09. |
| A20 | A different current builder can have fixed an earlier reproduced defect. Compare hashes; close each finding against the tested version. | **accepted** | Correct method point. I wrote "could not reproduce, so the earlier findings were wrong". The right statement is that they do not reproduce against the current builder, whose source I had not supplied. |
| A21 | Extend the forward horizon only if the declared strategy horizon does. | **accepted, scheduled (W08)** | This reverses part of what I shipped. I extended FX grading past 16:12 while the live book closes at 16:00 and treated the two as comparable. Instrument and strategy horizon get declared in the grading contract first. |
| A22 | Routine closures derive from rules; exceptional ones need a maintained override. Verify against authoritative schedules. | **partly accepted, scheduled (W10)** | `market_calendar.py` already carries a hardcoded list of unscheduled closures and a `set_extra_closures()` runtime override, so the mechanism exists. Accepted: I claimed all closures derive from rules, which overstates it, and the verification against authoritative schedules is not automated. |
| A23 | "No free historical source exists" is too broad. Alpaca has historical option data, though indicative quotes are not a real bid and ask. | **accepted, scheduled (W10)** | Correct. The honest claim is narrower: no free source establishes a contemporaneous OPRA bid and ask for these contracts at these timestamps. Entitlement to be tested against the documented endpoint with the exact contract and date, result recorded either way. |

---

## Two corrections Astra supplied that I am adopting outright

**The spread claim.** Astra: a $0.41 entry gives a 40 percentage point give-back
amount of $0.164, which is wider than a $0.13 entry spread. My packet said the
give-back trail was narrower than the spread; the earlier handoff said it too.
Both are withdrawn. The 116 positive two-sided entry observations do reproduce,
and the largest spread is about 35.294% of mid, not 40%.

**The 20-of-28 sniper statement.** Astra reconciled it: the first 28 closed
trades were 20 targets and eight stops, net 0R, which was true at that snapshot.
It does not describe the later 53-ticket book. That closes a reconciliation I had
listed as unresolved.
