# Phase 7: alert quality and historical observations

Implemented in the existing Python/Telethon/sqlite3 application. No TypeScript,
grammY, Zod, Drizzle runtime, new indicator, worker service or polling loop is added.
Strict Python validators and unittest provide the equivalent validation/test gates.
The current unverified BestLimits adapter remains unable to emit live alerts.

## Notification policy

`definition` remains authoritative for evaluation. Optional `quality_config` is
separate JSON validated by `parse_policy`; unknown fields, nonfinite numbers,
coerced booleans and nonincreasing thresholds/severity are rejected. NULL settings
mean normal severity, no thresholds, escalation off, additional drop 25 points,
minimum escalation interval 300 seconds, outcome tracking off. Malformed stored
settings fail closed. Severity choices are normal/important/critical, displayed as
عادی / مهم / بحرانی. Base severity is the admin-selected floor. Thresholds can
raise that priority using validated satisfied drop/disappearance metrics; a
composite uses the largest supported satisfied-leaf drop. Queue-volume minima do
not supply a percentage-drop metric. Missing metrics never imply critical severity.
These labels/thresholds are descriptive configuration, without financial validation.

Mandatory deduplication suppresses snapshot replays and continuing occurrences per
rule/revision/session. Optional escalation changes that per-rule behavior; it never
disables replay/recovery safeguards. At least one originally satisfied drop leaf
must gain the configured additional percentage points relative to that leaf's highest
notified drop. Its initial baseline remains fixed. Both rule cooldown and escalation
interval must pass, and the entire expression must evaluate TRUE with frozen
triggering drop baselines. Small oscillations/new unmatched leaves cannot meet that
gate. Disappearance/volume-only rules have no drop escalation. Escalation keys append
an occurrence sequence and are independent across private/channel rules on one symbol.
Edits prime anew and invalidate pending messages by revision. Restart retains state;
pre-start source updates never trigger. Old upgraded latched states with no quality
evidence stay latched and cannot escalate until a later normally rearmed occurrence.

Recovery keeps the existing typed strategies and frozen recovery_data. Drops recover
at five percentage points above their trigger boundary (full for thresholds <=5%).
Disappearance needs a confirmed original-side queue meeting its minimum. Composite
recovery needs a conclusively FALSE latched expression and expired cooldown;
volume minima recover strictly below 95%. UNKNOWN/missing/opposite-side evidence
does not imply recovery. Escalation does not replace recovery evidence or rearm.

Daily suppression counts include valid, newly accepted continued-condition updates
that would otherwise repeat an alert, including changes too small/early to escalate.
Snapshot replay, stale updates, UNKNOWN and invalid data do not inflate them.
Sparse suppression logs occur at power-of-two counts; no whole updates or raw errors
are logged. A crash after Telegram accepted a send but before DB success still has
the existing at-least-once duplicate risk; logical deduplication cannot remove it.

## Presentation and access

Formatting is centralized in bot_text and delivery; evaluation never builds Telegram
text. Source time and announcement time use Tehran, explicitly Gregorian. Escalations
identify the initial occurrence and their fixed baseline. All text uses no parse mode
(including notes/symbols), so HTML/Markdown strings cannot inject formatting. UTF-16
budgets bound composite notifications and notes. Composite summaries list only
confirmed matches and distinguish AND/OR. Very long text gets an explicit ellipsis.

Stock links validate the existing string instrument code and use
`https://tsetmc.com/instInfo/{insCode}`. The path is corroborated by the
[redirect implementation's documented example](https://github.com/5j9/tsetmc-redirect).
Direct site retrieval failed locally; no live market request/test notification is
needed to generate a link. Plain links reuse existing transport; link previews are
disabled. This verifies the documented URL pattern, not current site availability.

Advanced settings are hidden until selected from an existing rule. Current admins
can edit shared rules collectively and their own private rules only, using existing
service ownership checks. Severity, optional important/critical thresholds, escalation,
additional drop/interval, rule cooldown and outcome tracking are configurable without
raw JSON. The summary shows the recovery-based deduplication behavior; Back/Cancel,
ten-minute lifetime, refreshed tokens, optimistic revisions and permission rechecks
protect saves. Simple rule creation is unchanged. Group messages/callbacks are rejected.

Private `/history` paginates through owner/private-scope AlertService queries. The
admin panel has configured-channel history with outcomes and no creator metadata.
`/stats` is current-admin only, configured-channel scoped, using SQL aggregates over
the last 30 days. It counts retained events by severity, completed/insufficient/expired/
pending observations per window, available price samples and daily suppression counts.
Aggregation uses a readonly SQLite connection in a thread, with a consistent read
transaction, cancellation progress handler and a joined worker; the monitor's event
loop stays available. Admin membership/destination are checked again after the read.
Daily counts include the UTC day intersecting the 30-day cutoff. Deleted event records
leave the event totals; independently retained outcomes can still be counted. Statistics
are observations, with no trade success/win-rate or predictive-accuracy claim.

## Outcomes and missing data

Each opted-in notification event, including an escalation, atomically schedules its
configured independent windows alongside state/event/outbox. Targets use immutable
market source time; the baseline is that event's accepted current queue volume/side.
`pending` means no eligible observation yet. A target outside the original configured
session starts `insufficient_data`. A first in-window original-side queue or confirmed
none completes the queue observation; an opposite-side observation is insufficient.
None is real zero, not a failed request. An initial zero baseline keeps percentage
change NULL. Queue return = `(observed - initial) / initial * 100`, only for positive
finite baselines and nonnegative finite observations. Actual observation time is stored.

The first accepted source timestamp at/after target and at/before target+tolerance
must match the original session. No prior observation is substituted. Source freshness
is checked at collection; restart reads only timely collected persisted snapshots in
that same interval/session, even if the callback had not run before a crash. No
substantially later or next-session data substitutes for a missing observation.
If no evidence arrives before the cutoff or session end, status becomes `expired` on
the next monitor cycle. Halts/API failures produce no invented queue state. A configured
session is a profile-wide operational calendar, not an instrument-specific exchange
calendar. Targets near close can therefore be unavailable rather than completed.

The monitor unions pending eligible symbols with private watchlists/shared rules at
the existing polling frequency and concurrency limit. No additional HTTP requests per
event/subscriber/window. Pending observations survive rule/watchlist removal, but user
revocation/channel reconfiguration removes collection eligibility. Completing outcomes
never sends follow-up Telegram messages. Due reconciliation is bounded at 1,000 rows
per cycle; a large backlog may require several cycles, using persisted timely snapshots.
Existing cleanup deletes old outcome and suppression rows in indexed batches of at
most 1,000 total with event-loop yields, including outside sessions, and joins on stop.
Alert history and user configuration are not deleted by retention.

**Price limitation:** QueueSnapshot.price is a queue order price, not last-traded or
closing price. This pipeline supplies no verified traded-price metric; initial/observed
price, metric name and price change remain NULL. History and `/stats` explicitly state
that price data is unavailable. A completed queue observation is not a completed price
sample. The guarded percentage helper supports the calculation, but price reporting
requires a separately verified, consistently named/unit-preserving pipeline metric.
No new price fetch, queue-price substitution or unverified metric is introduced.

## Environment and operations

`ALERT_OUTCOME_WINDOWS_MINUTES=5,15,30` accepts distinct members of 5/15/30 without
spaces; `ALERT_OUTCOME_TOLERANCE_SECONDS=30` accepts 1..300;
`ALERT_OUTCOME_RETENTION_DAYS=30` accepts 1..365. Settings are validated at startup
and do not enable a rule's tracking. Tolerance/windows are frozen in each scheduled
row; changing env affects new events. Retention uses current configuration.
See [migration](MIGRATION.md#phase-7) and [manual deployment](../DEPLOYMENT.md).
