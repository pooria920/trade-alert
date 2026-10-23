# Phase 9 observed queue lifecycle

The existing Python 3.14 / Telethon / sqlite3 pipeline now detects queue lifecycle
transitions once per instrument and side. Rules do not collect data or own copies
of lifecycle state. No dependency, service, port or polling-frequency change was
introduced. Current live BestLimits evidence is still unverified and **cannot**
produce snapshots, lifecycle events or live alerts. Offline tests use synthetic
detector evidence; captured payload fixtures are never marked verified.

## Shared state and confirmation

`queue_lifecycle_states` keeps two current rows per observed instrument, keyed by
instrument/side, with a persisted session ID and statuses:

| Confirmed state | Meaning |
| --- | --- |
| `UNKNOWN` | No confirmed starting observation yet |
| `NO_QUEUE` | Confirmed none, without a continuous preceding disappearance |
| `ACTIVE` | Confirmed positive queue |
| `WEAKENED` | Confirmed reduction against a frozen observed-cycle baseline |
| `DISAPPEARED` | A continuous positive queue was followed by confirmed none |

Only detector-accepted fresh snapshots reach the tracker. The existing detector
requires a complete verified book, open trading status, source time and dated
permitted boundaries. Queue prices are never traded-price metrics. A verified
`none` is zero for both sides; the opposite queue is UNKNOWN, never disappearance.

Presence/absence and weakening confirmation use distinct, increasing source updates,
not receipts or polling attempts. Default confirmation is two observations. Failed,
stale, incomplete, halted or duplicate updates cannot increase confirmation counts.
The tracker persists candidates, source watermark, last confirmed volume/time,
coverage start, cycle count and weakening evidence. Restart preserves them; source
updates before process startup remain excluded by the existing engine.

Observation validity/reason is separate from the last confirmed state. On transport
or detector failure, the confirmed state stays visible as historical information,
while current validity is cleared and candidates are discarded. Closures do the
same. After invalidity, opposite-side observations, an excessive source-time gap or
a shared-policy change, fresh observations **re-prime without emitting transitions**.
A new session starts independent state and resets cycle count. No gap is reconstructed.

## Four transitions

- **Formation:** confirmed NO_QUEUE followed continuously by confirmed positive
  same-side queue. The first observed queue, including after startup/gaps, is a
  baseline rather than a formation event.
- **Weakening:** a meaningful percentage reduction in a continuously observed
  positive queue. The baseline is the highest observed volume since that queue
  cycle was confirmed, frozen at the beginning of the weakening occurrence.
  This avoids comparing a newly reformed/resumed queue with an older cycle's peak.
  The independent Phase 8 full-session observed peak is never reset.
- **Disappearance:** a confirmed ACTIVE/WEAKENED queue followed by confirmed none.
  The event preserves the actual last queue volume and its source timestamp.
  Closure, halt, failure and opposite-side data do not establish disappearance.
- **Reformation:** a confirmed queue after a continuous, confirmed disappearance
  in the same session. Each confirmation increments the session reformation count
  once. Unobserved cycles, restarts and data-return priming never increment it.

Weakening thresholds from eligible, validated private/channel rules augment the
shared detector's history threshold. A single update crossing several boundaries
persists one shared event, then each independently configured rule can match its
boundary. This prevents a 25% rule from waiting for a generic 30% history step.
Each boundary requires a five-percentage-point recovery before crossing again
(full recovery for thresholds <=5%). The baseline stays frozen within the shared
occurrence. Current configured boundaries and one escalation high-water mark are
retained; removed historical settings cannot grow state indefinitely.

The global history escalation step controls additional significant history records;
it does **not** opt any rule into notification escalation. Optional Phase 7 escalation
still requires both cooldowns, additional drop against original per-leaf evidence
and overall TRUE. Gaps/re-priming terminate old lifecycle weakening evidence, so
lower volume on data return cannot escalate an earlier occurrence. Confirmed
disappearance ends weakening; use a disappearance rule for the complete loss,
including a 100% threshold. Weakening itself requires a positive queue.

## Rule configuration and composite timing

For each example replace `BUY` with `SELL` for the other side:

| Authoritative JSON type | Config |
| --- | --- |
| `BUY_QUEUE_FORMED` | `{"minimumVolume":1000000}` |
| `BUY_QUEUE_WEAKENED` | `{"thresholdPercent":30,"minimumBaselineVolume":1000000}` |
| `BUY_QUEUE_CONFIRMED_DISAPPEARED` | `{"minimumPreviousVolume":1000000}` |
| `BUY_QUEUE_REFORMED` | `{"minimumVolume":1000000,"minimumDisappearanceSeconds":60}` |

The new confirmed-disappearance type deliberately differs from the original
`BUY/SELL_QUEUE_DISAPPEARED`, whose semantics/configuration remain unchanged.
Definitions remain strictly validated discriminated JSON, without SQL enum changes.
Volumes are safe nonnegative integers; percentages are >0 through 100 with up to
six decimals. Reformation duration is 0 through 86,400 seconds, default zero.
Unknown fields, non-finite values and boolean quantities are rejected.

Confirmation is one shared detector policy, not a per-user state machine. Rule
minimum volume and duration are filters at the **confirmed transition update**.
For example, a small formation filtered out by a rule's minimum does not become
a formation alert merely because volume grows later. A reformation before its
minimum duration is likewise not retained as a future TRUE condition.

Lifecycle predicates are TRUE only for matching events at the current snapshot's
source timestamp. No matching transition on a confirmed compatible update is FALSE;
unconfirmed/unavailable/opposite-side context is UNKNOWN. AND/OR use existing Kleene
logic. A reformation at volume 300,000 followed later by volume 1,000,000 cannot
satisfy `reformed AND volume >= 500,000` retroactively. Several matching children
still enqueue one rule notification; separate rules remain independent.

Rule creation/edit/enable and session change prime without firing. Pulse recovery
allows a later distinct transition once cooldown expires. Weakening additionally
retains frozen-baseline hysteresis. Old pulse evidence never satisfies a later
composite escalation. Existing atomic rule-state/event/outbox writes, revisions,
leases, send-time eligibility/age/permission checks and retry budgets remain active.
Delivery retains its documented at-least-once duplication possibility.

## Configuration

All settings are strict decimal integers. They configure detection, not subscriptions:

| Environment variable | Default | Range |
| --- | --- | --- |
| `LIFECYCLE_CONFIRMATION_SNAPSHOTS` | 2 | 1–10 |
| `LIFECYCLE_MAX_GAP_SECONDS` | 30 | 1–300 |
| `LIFECYCLE_WEAKENING_PERCENT` | 20 | 1–100 |
| `LIFECYCLE_ESCALATION_STEP_PERCENT` | 10 | 1–100 |
| `LIFECYCLE_MINIMUM_BASELINE_VOLUME` | 0 | 0–9007199254740991 |
| `LIFECYCLE_EVENT_RETENTION_DAYS` | 30 | 1–365 |

Choose the gap limit with the existing polling interval in mind; it must accommodate
normal distinct source updates to establish continuity. Detection still cannot
observe every change between polls. Raising gap limits does not verify missing data.
The shared minimum baseline is an additional weakening filter; it never supplies
missing volumes. Phase 7 severity/escalation/outcome settings stay per rule.

## Persian Telegram interface

Private `/alert` and private channel `/admin` creation now include
«هشدارهای رفتار صف» with «تشکیل صف جدید»، «ضعیف شدن صف»، «جمع شدن صف» and
«تشکیل دوباره صف». Select buy/sell, use million-share/percentage presets or enter
custom numbers, then confirm. Reformation duration is an optional collapsed step.
Editing preserves saved values; Back, Cancel, expiry, fresh tokens, optimistic
revisions and duplicate-confirmation protection remain active. Internal types/IDs
are not shown in lifecycle rule summaries. All callbacks remain private/authorized.

Admin-only private `/lifecycle` selects symbols from that admin's own watchlist and
eligible configured-channel instruments, never other users' private lists. It shows
both sides, persisted confirmed state, current validity, observed session peak,
last confirmed volume/time, formation/disappearance times, reformation count and
recent same-session events. Freshness also requires the current configured session.
Unverified, stale and unavailable information is explicit. Full-session coverage is
never claimed. Notifications show verified values, Tehran timestamps, severity,
lineage and the supported instrument link; they distinguish observation from advice.

## Persistence, retention and operation

Additive migration `0008_queue_lifecycle.sql` creates the shared state/event tables,
event uniqueness and indexed instrument/session history and event retention. Event
IDs are deterministic UUIDs from instrument, side, session, kind and source time.
Shared state and transition writes are transactional; replay cannot persist twice.
No old snapshots are backfilled into inferred events. Existing tables/migrations,
private/channel destinations, snapshots, session peaks and Phase 7 outcomes remain
unchanged. Rule removal does not delete shared lifecycle state or events.

Event history supports bounded instrument/session reads. Cleanup uses indexed
batches of at most 1,000, yields between batches, runs outside sessions and joins
before SQLite closes. Event retention does not delete current lifecycle state,
cycle counts or active evidence. Runtime adds no worker; the existing collector
calls detection once for each newly accepted shared snapshot. No extra upstream
requests are made per rule, user or lifecycle side. Health/shutdown behavior stays
with the existing collector, dispatcher, MTProto session and database lock.

See [manual Phase 9 upgrade](../DEPLOYMENT.md#manual-phase-9-upgrade). No deployment,
live database operation or Telegram test notification is part of development.
