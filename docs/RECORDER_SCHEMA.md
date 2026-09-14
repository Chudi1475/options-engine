# Recorder schema

Required by spec section 11, specified by section 5. This is the **contract W06
and W07 implement against**, written before the code so the code is held to it
rather than describing itself afterwards.

Astra's framing, kept: "Six small record types. Fields listed below are the
minimum contract, not six independent enterprise systems."

---

## Rules that apply to every record

| Rule | Meaning |
|---|---|
| **Unknown is null, with a reason** | Never zero, never false, never an invented timestamp. A missing fill time stays missing; it does not become the message time. |
| **Timestamps are UTC, offsets preserved for display** | The container runs UTC and the owner reads CT. Both must be reconstructable. |
| **Durations and retry intervals use a monotonic clock** | A container clock that jumps must change no decision. |
| **Contract metadata is referenced, not repeated** | `contract_id` on the tick, the metadata once. |
| **No recipient identifiers, ever** | An index and an opaque ref only. Three recipients, none exportable. |
| **No prompt content and no secrets** | Applies to the AI cost rows too. |

---

## 1. Event identity and decision

The row that says what the bot saw and what it decided. Written for **every
completed input-bar opportunity**, not only for alerts.

```
schema_version, candidate_id, decision_id, strategy_id, strategy_version,
policy_hash, source_commit, deployment_id, session_date, cluster_id,
decision_at_utc, observed_at_utc, symbol, direction, input_bar_end_utc,
input_feed, input_values, gate_passed, reject_codes, selected, position_id
```

**Three ids, three different jobs.** `candidate_id` is allocated at observation
creation. `decision_id` is immutable and separate. `position_id` names the
modeled position. Astra: "Multiple observations of a setup are not the same as
permission to fire another trade."

`cluster_id` exists because SPX and SPY co-fire in the same minute. It is the
unit the preregistered study analyses on. Without it, 42 pairs get counted as 84
observations, which is the error the whole study design is built to avoid.

`input_values` means the small set actually used in the decision: momentum, open
price, spot, FVG inputs where applicable, the allow-list result, the raw gate
statistics, the rounding rule, the report IDs and the risk regime.

`policy_hash` and `source_commit` are what let a row be replayed later against
the exact policy that produced it.

### What counts as one candidate

**The unit is each completed input-bar opportunity, per strategy, symbol and
direction**, plus meaningful revisions of its decision inputs.

A 15-second recheck of the same unchanged bar is **not** four candidates. Scan
evaluations and candidate opportunities are counted separately so both numbers
stay available.

Record all gate failures where available, with a reason when later checks were
not evaluated. **Do not download news or option chains just to manufacture a
reject reason.**

---

## 2. Quote and path sample

```
sample_id, candidate_id, contract_id, contract_role, sample_sequence, provider,
feed, provider_at_utc, received_at_utc, requested_at_utc, bid, ask, bid_size,
ask_size, underlying_price, underlying_at_utc, price_basis, is_model,
quality_flags, missing_reason, observation_end_utc
```

**Three clocks, deliberately.** `provider_at_utc` is when the provider says the
quote was current, `received_at_utc` is when we got it, `requested_at_utc` is
when we asked. Yahoo option quotes run roughly 15 minutes delayed; collapsing
these into one field is how a stale quote gets treated as live.

`is_model` and `price_basis` separate a Black-Scholes estimate from a real
quote. This is the field that would have prevented the overstatement in A07: six
of the deep losses carried modeled marks and nothing in the record said so at a
glance.

### Contract roles

| Role | Purpose |
|---|---|
| `chosen` | what the bot actually alerted on |
| `direction_control` | the opposite direction, selected at the same decision time by a rule fixed in advance |
| `strike_control` (named) | same direction, different strike, near the chosen moneyness or delta |

Astra A12: a put versus a call tests **direction**; an alternative SPY call tests
**strike selection**. They are different questions and calling both "the
opposite strike" conflates them.

**Record every quote used to choose among alternatives**, so future information
cannot slip into the selection.

### What the path promise actually is

Record the entire path **the bot actually observes**, including the chosen and
control contracts, through a common end time, **even after the current trade
closes**.

That last clause is the point. Astra A15: MFE recorded only until the current
exit is censored, so "MFE never reached 2R" says nothing about whether a larger
target was reachable.

This is **every observed sample, not a promise of every market quote.** Start by
reusing monitoring observations at the existing cadence, batch additional
contracts where supported, and record gaps. **Fifteen-second polling can miss
intrasecond spikes; an unobserved high is not a booked profit.**

If budget or rate limits prevent complete paths, **preserve the missingness and
keep the affected comparison inconclusive.**

---

## 3. Decision and exit evidence

```
event_id, position_id, event_type, trigger_at_utc, trigger_sample_id,
trigger_basis, trigger_threshold, mark_used, mark_source, leg_quantity,
delivery_id, recipient_ref, send_attempt_at_utc, delivery_status,
provider_message_id, acknowledged_at_utc
```

`trigger_sample_id` points at the exact quote sample that fired the exit, so an
exit can be re-derived rather than re-asserted.

### Delivery states

| State | Meaning |
|---|---|
| `pending` | intent written, not yet attempted |
| `attempted` | request sent |
| `confirmed` | Telegram acknowledged |
| `failed` | definitively rejected |
| `unknown` | **the request may have reached Telegram and we did not learn whether it did** |

`unknown` is the one that matters. Telegram delivery is not universally exactly
once. An uncertain acknowledgment stays visible rather than being silently
retried as a new trade.

**`selected` and `delivery_confirmed` are separate fields.** `selected` means
the strategy committed its decision; `delivery_confirmed` means Telegram
answered. **A queued send is not confirmed delivery**, and delivery is never
inferred from a position existing (A05).

Per-recipient status is kept for all three recipients **without exporting their
identifiers**: an index plus an opaque ref.

---

## 4. Contract metadata

```
contract_id, underlying, option_right, strike, expiry_date, last_trading_at_utc,
settlement_at_utc, settlement_style, exercise_style, multiplier, currency,
tick_size, metadata_source, metadata_at_utc
```

Stored once per contract and referenced from every tick.

`last_trading_at_utc` and `settlement_at_utc` are separate from `expiry_date` on
purpose. A half day closes at 13:00, and a 0DTE contract dies when the market
shuts. A fixed 16:00 assumption already produced a real bug: a modeled -28% on a
day that truly lost -77%, because the pricer's clock and the market's clock
disagreed.

For the catalyst lane this section is load-bearing: **after-close earnings
cannot be traded through using an option that stops trading before the
release.**

---

## 5. Manual fill event

```
fill_id, revision, user_ref, candidate_id, position_id, contract_id,
buy_or_sell, quantity, fill_price, fees, executed_at_utc, reported_at_utc,
evidence_class, evidence_reference, supersedes_fill_id
```

**The interface:** reply to a specific alert with quantity and price, then reply
to close with the actual quantity and price.

- Ask for execution time if missing, **or leave it unknown**. Never substitute
  the message time.
- **No reply means unknown, not no trade.**
- Corrections are **revisions** (`revision`, `supersedes_fill_id`), never
  overwrites.
- No broker order execution is added, ever.
- **Do not ask the user to trade merely to create evidence.**

`evidence_class` is what keeps this honest: a modeled return and a reported fill
are different classes and a modeled return never becomes a claimed broker fill.

Edge cases the regression must cover: duplicate reply, ambiguous candidate,
partial close, over-close, late correction, no reply, and two users replying to
one signal. Integer quantities and costs must reconcile.

---

## 6. Health and coverage

```
instance_id, ownership_state, last_successful_monitor_at_utc, last_sample_at_utc,
queue_depth, dropped_samples, write_failures, provider_errors, bytes_used,
observation_gap_start, observation_gap_end, gap_reason
```

`ownership_state` is one of the five states, so a health row says whether the
copy that wrote it was allowed to act.

`dropped_samples` and the gap fields are the coverage denominator. **A gap that
is not recorded is a gap that silently becomes "we observed everything".**

`bytes_used` exists because the volume is 500 MB and retention is currently
unbounded.

---

## Recorder behavior constraints

**A bounded queue with a dedicated writer.** Saturation must:

1. increment a durable or externally logged loss counter
2. downgrade evidence completeness
3. **never silently block exit monitoring**

That third one is the hard constraint. The recorder is an observer. If it ever
delays a stop, it has done more harm than the evidence is worth.

**Capacity is estimated, not assumed:** measured bytes per sample, times
contracts, times samples per session, times retention days. When path archives
rotate, retain the compact event and coverage records. **Never silently delete
observations a pending study needs.**

---

## Status

**This document is the specification. None of it is implemented yet.** W06
builds records 1, 2, 3, 4 and 6; W07 builds record 5. Any field this schema
names and the implementation omits is a gap to report, not a field to quietly
drop.
