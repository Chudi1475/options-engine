# 2026-09-08 Singleton instance lease

BACKLOG item: "No singleton guard on the Telegram getUpdates consumer; a
second instance splits commands and double-alerts" (line 70). The warn half
shipped 2026-07-11; this closes the stand-down half.

## What was wrong

Nothing stopped a second copy. `telegram.get_messages` detected Telegram's 409
and `_warn_conflict` DMed the owner once a day, and that was all. Both copies
kept polling, both kept scanning, both kept monitoring, and both kept sending.

Two aggravating factors that were not written down anywhere before:

1. When a send fails, `notify` queues the text into `pending_sends`, which
   lives in the SHARED state.json. A naive "loser returns an error" gate would
   poison that queue and the WINNER would later flush and broadcast the
   loser's duplicates.
2. On a shared volume both copies write positions.json. A loser that keeps
   monitoring can mark a position closed in the file the winner then never
   alerts on. That is the money bug, and it is why the loser does not monitor
   at all rather than monitoring silently.

## What shipped

New module `instance_lock.py`. The AUTHORITY is an OS advisory lock on
`DATA_DIR/scanner.lock` (msvcrt on Windows, fcntl in the container), NOT the
state.json record.

That inversion is the whole design. The kernel releases the lock when the
holder's handles close, which includes SIGKILL and a container teardown, so a
crashed instance leaves NO stale lock and a railway restart under
`restartPolicy: ALWAYS` reclaims on its FIRST attempt: no timeout, no clock
read, no human. A record-only lease would need a staleness timeout, and every
timeout choice is a way to brick the bot on restart.

The `{instance_id, pid, host, seq, heartbeat_ts}` record under the state.json
key `instance_lease` is the observable half only: it names the holder in the
stand-down DM, in `/status`, and to the wedge detector.

Nothing branches on a wall clock. `heartbeat_ts` is display only, clamped to
"last renewal time unknown (clock skew)" when it is missing, non-numeric, more
than 60s in the observer's future, or more than 30 days old. Freshness is
judged solely by whether `seq` advances, measured against the observer's own
`time.monotonic()`.

Three states, no demotion path:

- ACTIVE. Unchanged behavior. Renews every 20s; `seq` is seeded from any
  existing record + 1 on takeover so it never moves backwards.
- ACTIVE (DEGRADED). Reached when no lock primitive is importable, when
  `os.open` raises, or when the lock call raises an errno that is NOT
  contention (ENOLCK / ENOSYS / EOPNOTSUPP on a volume that cannot lock). The
  bot runs normally and DMs the owner once. Failing closed here would let a
  lone bot silence ITSELF forever with no second copy anywhere.
- STANDBY. A live 5s retry loop, never a process exit. Promotes itself within
  one poll of the lock freeing, then runs `check_downtime_on_start` and DMs
  "promoted to active".

## Which sends a loser suppresses

| send path | before | after (standby) |
| --- | --- | --- |
| `Service.notify` (entry, half, trail, stop, expiry, morning, weekly, test) | broadcast | returns `[]`, no queue, no alerts.log |
| `telegram.send` (recap, weekly scoreboard) | broadcast | dropped at `_send_one` |
| `telegram.send_photo_all` (sniper charts) | broadcast | dropped at `_send_photo_raw` |
| `telegram.send_photo` (chart replies) | sent | dropped at `_send_photo_raw` |
| `telegram.send_to(chat, reply)` (chat replies) | sent | dropped, and unreachable anyway |
| `telegram.get_messages` (getUpdates) | polled | returns `([], offset)`, NO HTTP GET |
| `Service.flush_pending` (retry queue) | re-broadcast | returns early |
| BREAKING news thread | sent | thread not started, and its loop body skips |
| sniper watch thread | sent + wrote sniper_book | thread not started, and its loop body skips |
| once-a-day jobs (recap, digest, learn, weekly, grade_forward, holiday, health_eod) | sent | never reached, the loop continues first |
| `Service._hb_owner` (ops DM to owner) | sent | STILL SENT, via `ops=True` |

The `ops=True` channel is deliberate: a copy told to go quiet must still be
able to say why. It carries the stand-down DM naming the holder, the degraded
DM, the wedge warning and the promotion DM.

The `notify` early return is the single most important line. Returning an
error list instead would append to `pending_sends`, and the winner would
broadcast the loser's duplicates off the shared state.json.

Not polling getUpdates at all is what removes the 409 at its source in the
shared-volume case. The loser has no commands to answer because it never
receives one; the winner answers every command.

A wedged winner (lock still held, `seq` frozen for 180s of the observer's own
monotonic clock) gets a DM and nothing else. It WARNS, it never takes over.
Stealing from a live-but-hung process is the split brain this prevents.

Scope: only `--daemon` and the plain session run take the lock. `--dry-run` is
exempt (it sends nothing and writes its own book) and `--test` / `--weekly` /
`--setup` are manual one-shots that do not poll getUpdates.

## What this does NOT cover

A desktop `python scanner.py` running beside the cloud daemon shares no
filesystem: no shared state.json, no shared lock file. Neither the lock nor
the record can see that pair. It stays covered ONLY by the existing 409 warn
in `scanner._warn_conflict`.

Deployment 3c91c3bf's 409 is consistent with BOTH shapes and it is not known
which one fired, so this is NOT claimed to have closed that incident. After
this ships, the conflict warning going quiet means it was the shared-volume
shape; it continuing to fire means it was local-versus-cloud. That difference
is itself the diagnostic.

Also NOT covered: any other process using the same bot token.

Deliberately NOT in this change: an opt-in role marker (e.g. `SCANNER_ROLE`)
so a non-primary instance stands down on a 409. That would cover the
local-versus-cloud pair with no shared state, but it changes send behavior on
a signal Telegram controls, so it is a separate decision with the owner.

## Tests

`test_instance_lease.py`, 56 checks, all offline, registered in
`self_improve.run_checks()` and named in its prompt.

- L1 two instances, one lock. Includes a raw second fd to prove the platform
  itself refuses, not just our bookkeeping.
- L2 the anti-brick test. A released lock reclaims on the first try in under
  1s with no timeout, `seq` strictly greater and the new id in the record.
  Plus a REAL subprocess that takes the lock, is killed, and the parent then
  acquires within 2s. That is the railway `restartPolicy: ALWAYS` case.
- L3 the loser refuses every broadcast and `pending_sends` is byte-identical.
- L4 the loser can still warn the owner: `ops=True` reaches the transport,
  the same owner without `ops` does not.
- L5 the loser issues no getUpdates HTTP GET.
- L6 a real `Service` on a bounded `daemon()` run: monitor, scan and command
  poll never entered, positions.json byte-identical.
- L7 clock jumps change no decision, and a future or 30-day-old stamp renders
  as clock skew rather than a nonsense age.
- L8 fail open: no primitive, and an errno that is not contention, both keep
  the bot alerting and DM the owner once.
- L9 a wedged holder is reported once and never stolen from.
- L10 `instance_lock.py` is under the no-em-dash law.

Watched fail first, in order: `ModuleNotFoundError: No module named
'instance_lock'`, then `AttributeError: module 'telegram' has no attribute
'set_standby'` (L3 onward), then L10, then the two new L8 errno checks against
the pre-fix classification (ENOLCK and ENOSYS read as "contended").

Full offline suite green: test_pipeline, test_adduser,
test_no_hardcoded_stats, test_session_fixes, test_market_calendar,
test_review_regressions, test_ledger_integrity, test_review_import,
test_grading_integrity, test_instance_lease, test_no_em_dash,
test_review_packet_gate.

Two stubs in test_pipeline.py were updated to mirror the real signatures
(`_send_one(..., ops=False)` and `send_to(..., ops=False)`). No assertion was
changed or weakened.

Also verified with two REAL processes on a temp DATA_DIR: A went active, B
printed standby and DMed once naming A, and after A was force-killed B
promoted itself within one 5s poll, DMed "promoted to active", and continued
the seq forward.

## Still owed, with the owner watching

This changes WHO delivers an alert, which is a delivery change. Before it is
trusted: start the daemon locally, confirm `/status` shows the Instance line;
start a SECOND copy on the SAME DATA_DIR and confirm it prints standby, DMs
once, sends nothing else, and does not answer a `/status` typed into Telegram;
kill the first and confirm the second promotes and answers the next command.

No strategy behavior was touched: no `detect_setup`, no allow-list, no exit
thresholds, no entry windows, no rosters. No before/after signal-eligibility
diff is owed.
