# Policy change matrix

Required by spec section 11. Every behavior delta in this release, classified,
with the reason and the regression that pins it.

**This exists because "the strategy files are untouched" is the wrong criterion.**
Astra A02: "Scheduling, feed composition, quote fallback, duplicate suppression
and restored positions affect alerts and simulated exits." Unchanged strategy
files remain a necessary condition. They were never a sufficient one, and this
matrix is what replaces that claim.

## Behavior classes

| Class | Meaning | Needs owner approval? |
|---|---|---|
| **STRATEGY** | changes which setups alert, at what level, or how a position is graded | yes, and none appear below |
| **ALERT AVAILABILITY** | changes whether a message that used to arrive still arrives | **yes** |
| **DELIVERY** | changes retry, ordering or duplicate behavior of a send | yes |
| **PERSISTENCE** | changes what lands on disk and when | no, but must be tested |
| **MEASUREMENT** | changes a recorded or reported number, not a decision | no |
| **OPERATIONAL** | logging, health text, tooling | no |

**STRATEGY count in this release: zero.** `fvg.py`, `config.py` exit
thresholds, `strategy.py`, the allow-list, entry windows and symbol rosters are
byte-identical. Verified by `git diff --name-only` against each and by
`release_manifest.json` module hashes.

---

## The changes that reduce alert availability

These are the ones that need a decision, so they come first. Each is a case
where the bot now says nothing where it used to speak.

| ID | Change | Why | Regression |
|---|---|---|---|
| **A1** | A copy that cannot establish ownership goes **BLOCKED and silent**, including on a fresh boot. Previously it kept alerting. | Astra A03 and decision 2: "Unknown ownership is not exclusive ownership." This reverses the old fail-open default, which contradicted the same subsystem's own rejection of blind resume. | `test_instance_lifecycle.py` |
| **A2** | **RECOVERING**: after taking the lock the process stays gagged until every mandatory store parses. A truncated `positions.json`, sniper ledger, `state.json` or a non-integer `tg_offset` holds it quiet and retrying. | Astra section 3: "Any mandatory reload failure leaves the process RECOVERING or BLOCKED, even if an older in-memory book is still available." | `test_instance_lifecycle.py` |
| **A3** | **STARTING gag**: the wire is muted before the first ownership answer, closing the window between process start and the first `ensure_active`. | Same contract. A copy with no answer yet is not an owner. | `test_instance_lifecycle.py` |
| **A4** | An entry alert whose durable journal append fails **does not go out**, and the ticker is retried next cycle rather than burned. | Astra section 3: "If durable intent creation fails, do not create an unrecorded new actionable entry. Raise an explicit health condition." | `test_event_recovery.py` |
| **A5** | `sniper_book.has_open` returns **True** on an unreadable book, so the scanner skips that symbol for the pass: no read, no alert. | The alternative is worse: the card is sent before `open_trade` runs, so returning False texts a ticket that then cannot be tracked, and risks a second live sniper on a symbol that already has one. **This is a one-line flip if the owner prefers the other trade-off.** | `test_storage_faults.py` |
| **A6** | A send whose acknowledgment is lost is recorded `unknown` and is **never auto-retried**. Previously it was queued and re-broadcast. | Astra section 3: "uncertain network acknowledgments must remain visible rather than silently retried as new trades." Cost: a card that did not arrive is not resent automatically. | `test_event_recovery.py` |

**Six changes, all in the same direction: quieter under fault.** That is the
deliberate posture of this release. It is also the posture that most needs a
human to agree with it, because the failure mode it accepts is silence.

---

## Delivery and ordering

| ID | Change | Why | Regression |
|---|---|---|---|
| D1 | Entry: **journal intent, then position, then link, then send.** Was send, then write, then link. | A05: a delivered alert followed by a crash before linkage vanishes from the selected cohort. | `test_event_recovery.py` |
| D2 | Exit: **persist, then send.** Inverts the old "a crash in between is at worst a duplicate" comment. | Safe only because `commit_intent` is idempotent on `(position_id, leg)`. | `test_event_recovery.py` |
| D3 | Sniper commit: **claim, intent, position, ledger link, send, delivery records, commit reservation.** `mark_selected` now runs BEFORE the send. | A06: "Compensation is not a transaction." The old day-key release could race a newer claimant. | `test_event_recovery.py` |
| D4 | A partial recipient failure retries **only the unresolved recipients**, instead of re-broadcasting the whole text to everyone. | Fewer duplicates. A permanent 403 on one recipient no longer costs the others a duplicate. | `test_event_recovery.py` |
| D5 | A card the wire dropped because this copy was in standby **stays owed** and is delivered by the copy holding the lease. | Previously written off. | `test_event_recovery.py` |
| D6 | Reservations carry **operation identity**; only the owning operation can release one. | A06. | `test_event_recovery.py` |

---

## Persistence

| ID | Change | Why | Regression |
|---|---|---|---|
| P1 | One shared write protocol (`storage_io`) across `positions`, `sniper_book`, `forward_ledger`, `config`, `learn`. | Astra W05 acceptance: "all writers use the same protocol". | `test_storage_faults.py` S8, an AST scan that turns the sentence into a test |
| P2 | Reads return **`ok` / `missing` / `unreadable` / `corrupt`** instead of an empty list. | W05 acceptance: "no silent fallback to an empty book". | `test_storage_faults.py` S3 |
| P3 | **`sniper_book.open_trade` no longer republishes a one-row file over an unreadable book.** | This was a live money bug: it erased two live sniper stops and returned the row as if it had worked. | `test_storage_faults.py` S3 |
| P4 | `sniper_book.step` returns None when the publish fails, so **no exit card is texted for a close the disk never took**. The row stays open and the exit is found again next poll. | Previously the card went out while the row stayed open, and the next poll texted it again. | `test_storage_faults.py` |
| P5 | Bounded retries on the Windows `tmp.replace` fault, across all five writers. Max 5 attempts, doubling backoff capped at 0.2s. | The fault is real and hits roughly one run in ten. Only `positions.py` retried before. | `test_storage_faults.py` S1 |
| P6 | `forward_ledger._write_all` **refuses** to publish when the ledger lock is not held. | Closed a hole where a busy in-process lock meant no OS lock was attempted at all, and the whole-file rewrite ran unserialised. | `test_ledger_integrity.py` |
| P7 | `config.state_set` / `state_update` skip the write when the state lock cannot be taken in 1.0s. | Only a second process on the same volume can cause this. | `test_storage_faults.py` |
| P8 | `positions.PositionBook.save` and `add` return a bool; `save` no longer lets an ENOSPC propagate out of a scan. `PositionBook.last_read_status` added. | Lets promotion refuse on an unreadable book rather than guessing from a None. | `test_storage_faults.py` |
| P9 | `learn._build_digest` **raises** on a failed digest publish instead of printing. | Without it the switch to a status-returning writer would have silently converted a failure into a claimed rebuild. Caught by `test_review_import`. | `test_review_import.py` |
| P10 | A zero-byte `.lock` sidecar now sits beside every file the protocol writes, and is never deleted. | Astra section 3: "Do not replace or unlink the lock file while owners exist." `.gitignore` gained `*.lock`. | `test_storage_faults.py` S5 |

---

## Ownership internals

| ID | Change | Why |
|---|---|---|
| O1 | `positions.save` refuses when the process is not the owner. `sniper_book._write` asks "am I the owner" rather than "am I standing by", so RECOVERING and BLOCKED also refuse. | A non-owner must not mutate shared state. |
| O2 | `telegram.get_messages` and `print_chat_ids` refuse to poll **in any state but ACTIVE**. | This is the only enforceable half of the second-consumer problem. |
| O3 | `--setup` asks for the lock before polling. Previously unconditional. | It could swallow a command the live daemon should have answered. |
| O4 | A BLOCKED copy no longer calls `instance_lock.renew()`. | A copy that owns nothing publishes nothing. |
| O5 | `instance_lock.holding()` returns False when the calling pid is not the acquiring pid; the fd is opened non-inheritable. | A forked child that inherited the handle is not an owner. |
| O6 | `ACTIVE-DEGRADED` is **deleted**. `status_line` signature changed from `(standby=, degraded=)` to `(state_name=None)`. | A03: it was the unsafe default written down. |
| O7 | `--dry-run` stays outside the ownership machine and still answers commands, but now declares its state explicitly. | Inheriting the STARTING default would have silently stopped a dry run answering `/status`. |

---

## Measurement and operational

| ID | Change | Why |
|---|---|---|
| M1 | The 409 warning no longer asserts a location. It says a 409 proves competing requests only, then lists the inventory to check. | A04. The old text named "an old deploy, or a local run" as if it knew. |
| M2 | `instance_lock.CANNOT_DETECT` is a machine-readable list, and every owner-facing line is generated from it. | So the honesty is testable and a hand-typed sentence cannot go stale. |
| M3 | `telegram.get_messages` respects `BOT_TEST_MODE`, with an explicit `allow_test_poll` opt-in for the three suites that drive the poller against their own stub. | The offline suite was polling the live token: a 409 for the cloud bot, and a poll acknowledges updates, so a command could be swallowed on the desktop. |
| M4 | Momentum alerts carry `candidate_id` and `decision_id`. **No card text changes, no eligibility changes.** | Linkage for the study. |
| M5 | `/health` gains unresolved intents, unknown deliveries, orphans and the journal health condition. Recipient ids never included, only an index and an opaque ref. | Coverage must be visible. |
| M6 | `forward_ledger.mark_selected` returns a `MarkResult` rather than a bool, still falsy exactly when the link did not land. | Replay must distinguish an orphan from a refused write from a stand-down. |
| M7 | `config.load_state` quarantines a `state.json` with a bad **encoding** as well as bad JSON. | A `UnicodeDecodeError` previously escaped uncaught. |

---

## Deliberate deviations from the spec, declared

| What | Why |
|---|---|
| Astra's table puts "replay local durable intents" in **RECOVERING**. It is implemented one step later, at the RECOVERING to ACTIVE transition. | W01 gags the wire in every non-ACTIVE state, so a resend attempted in RECOVERING would be dropped and then recorded as a delivery failure. The reconcile still happens in RECOVERING; only the delivery half moved. |
| News, ops DMs, heartbeats, the morning card, the recap and charts stay on the **unjournaled** path. | Deliberate scope limit, so the retry behavior of every report does not change at once. Their sends have no per-recipient delivery record yet. |
| `fsync` is available but **off by default**. | Needs a latency measurement on the Railway network-backed volume, which cannot be taken from the desktop. Current behavior therefore matches the old code exactly. |

---

## Status of this matrix

**Incomplete.** It covers W00, W05, W01 and W02. W03, W04, W06 through W11 will
add rows, and the release checklist's step 5 (identical-input replay through old
and candidate) is what will confirm this list is exhaustive rather than merely
diligent. Until that replay runs, **no claim is made that these are all of the
behavior changes.**
