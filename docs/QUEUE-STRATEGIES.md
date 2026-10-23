# Phase 8 queue strategies

Implemented in the existing Python 3.14 / Telethon / sqlite3 application. No new
dependencies, upstream requests, polling loop, delivery worker or environment flags.
Only newly inserted detector-accepted, fresh source updates enter evaluation.
BestLimits still lacks a verified timestamp/completeness contract, so live alerts
remain blocked. Tests use synthetic offline evidence.

## Authoritative configuration

Strict JSON definitions remain in `alert_rules.definition`. Replace `BUY` with
`SELL` for the other queue. Private ownership/watchlists and channel admin scope
are unchanged; channel rules remain independent of private preferences/watchlists.

| Type | Configuration | Inclusive trigger |
| --- | --- | --- |
| `BUY_QUEUE_VOLUME_BELOW` | `{"thresholdVolume":2000000,"hysteresisPercent":5}` | volume <= 2,000,000 |
| `BUY_QUEUE_VOLUME_ABOVE` | `{"thresholdVolume":2000000,"hysteresisPercent":5}` | volume >= 2,000,000 |
| `BUY_QUEUE_PEAK_DROP_PERCENT` | `{"thresholdPercent":50,"minimumPeakVolume":1000000,"hysteresisPercent":5}` | drop >= 50% from highest observed buy queue volume |
| `BUY_QUEUE_DROP_PERCENT` | `{"thresholdPercent":30,"windowMinutes":15,"minimumBaselineVolume":1000000}` | drop >= 30% from the historical baseline |

Volumes are safe integers. Thresholds must be positive; baseline/peak minimums
may be zero. Drop percentages are greater than zero through 100 inclusive with
up to six decimals. Hysteresis defaults to 5, allows 0 through 100 and six decimals.
Booleans, non-finite values, unsupported windows and unknown fields are rejected.

Existing channel `VOLUME_ABOVE` definitions with `minimumVolume` retain their old
level semantics and strictly-below-95% recovery. Existing UI shortcuts still create
them. The new volume category creates `thresholdVolume` crossing definitions.
Keeping values while editing preserves the original configuration and semantics.
Original drop/disappearance definitions are unchanged.

## Crossing, recovery and deduplication

Creation, edits, enabling and new sessions prime without firing. Each new threshold
leaf must observe its recovery side before it can trigger; readiness persists.
A rule created while satisfied, or first seeing UNKNOWN then satisfied data, stays
quiet until recovery. Readiness also requires the configured hysteresis margin.

Below-threshold recovery requires `volume > threshold * (1 + hysteresis/100)`.
Above-threshold recovery requires `volume < threshold * (1 - hysteresis/100)`.
These comparisons are strict, even at zero hysteresis: equality triggers. With
100% hysteresis an above-threshold rule cannot arm/rearm, since volume cannot be
negative. Cooldown must also expire before a triggered rule rearms.

Threshold messages require a compatible bounded previous observation so that the
previous volume is real. A gap beyond history tolerance delays evaluation until
compatible observations resume; no previous value is fabricated.

Peak recovery freezes the triggering peak and requires volume at least
`peak * (100 - max(0, triggerPercent - hysteresisPercent))/100`. A new qualifying
peak can establish a later cycle after recovery. Alerts never reset the shared peak.
A peak rule created against an already satisfied observed peak waits for recovery.
Rolling drops retain their original five-percentage-point recovery margin.

Each occurrence emits once. Missing, stale, incomplete or opposite-side evidence
never counts as recovery. Restart cannot rearm an active occurrence. Optional Phase
7 escalation retains the original frozen per-leaf baseline, both cooldowns and an
overall TRUE result; it never replaces recovery evidence or resets peaks.

## Observed peaks and rolling baselines

One indexed `queue_session_peaks` row per instrument/session stores separate buy
and sell peaks, their actual source timestamps and first/last accepted observation
times. Updates are shared across rules/users. Replayed, stale, late or unpersisted
updates cannot raise peaks. New sessions use independent rows; opposite queues
never erase prior peaks. Positive minimum peaks are enforced; zero baselines are
insufficient. Detector-confirmed none is zero and can mean a 100% drop. Missing or
invalid data and opposite-side queues remain UNKNOWN.

Coverage from actual session start is **never guaranteed**, including after an
outage. Messages always say “highest observed queue volume” (بیشترین حجم مشاهده‌شده).
`first_observed_at` describes observation coverage, not market opening. Peaks survive
restart, rule deletion and normal snapshot cleanup; inactive session rows expire
at the snapshot retention cutoff, in indexed batches of at most 1,000 with yields.
Peak source timestamps are never replaced with receipt/cleanup times or prices.

Rolling windows are exactly 1, 5 or 15 minutes. The repository looks at-or-before
`source time - window`, within `SNAPSHOT_HISTORY_TOLERANCE_SECONDS`, in the same
session. Missing, stale, opposite-side, zero or below-minimum baselines are UNKNOWN.
Consecutive polls and arbitrarily old history never substitute for the configured
window. Replayed source times cannot trigger or increase suppression counts.
Roughly 10-second polling cannot detect every change between observations.

## Composite rules, delivery and outcomes

All new strategies work in the existing channel AND/OR builder (eight leaves;
stored definitions allow four group levels). Only overall TRUE triggers. FALSE
dominates AND, TRUE dominates OR; otherwise UNKNOWN propagates. Matched leaf evidence
freezes at trigger. The whole latched expression must become conclusively FALSE
and cooldown expire to rearm; UNKNOWN cannot rearm. Multiple matches emit one event.

The shared monitor still collects once per deduplicated instrument at its existing
cadence. Indexed peak/history results are shared for an update. Existing outbox
leases, send-time authorization/permission/revision/age checks and retry budgets
remain active. At-least-once delivery can duplicate an accepted message if success
cannot be recorded. Outcomes use accepted snapshots at the same cadence, including
pending instruments after removal. Traded-price and return fields remain NULL.

## Persian interactions

Private: `/alert` → نماد → «حجم صف به عدد مشخصی برسد» →
«حجم صف خرید کمتر یا مساوی مقدار مشخص» → `۲۰۰۰۰۰۰` → «تأیید و ذخیره».
The summary shows the inclusive boundary. «تنظیم فاصلهٔ بازگشت» optionally changes
the default recovery margin without a mandatory technical step.

Observed peak: «صف نسبت به بیشترین حجم کاهش یابد» → صف خرید یا فروش →
«۵۰٪» (or custom numeric input) → minimum observed volume, default `۰` → save.
Rapid drop: «صف در مدت کوتاه سریع کاهش یابد» → صف خرید یا فروش → «۵ دقیقه» →
«۳۰٪» (or custom input) → minimum baseline, default `۰` → save.

Use private `/admin` for shared-destination composites. Back, Cancel, ten-minute
expiry, refreshed step tokens, optimistic edit revisions and duplicate-confirmation
protection remain active. Group commands/callbacks remain rejected. Messages show
actual observed volumes, reason, severity and Tehran source time. Peak messages
explicitly disclaim full-session coverage.

## Migration and verification

`0007_queue_strategies.sql` adds nullable `alert_states.strategy_data`, the shared
peak table and its retention index. No rule, destination, legacy field, event,
outbox or Phase 7 outcome is rewritten. Existing states retain recovery and gain
NULL strategy data. Prior SQL files/entries are preserved; a journal entry is
appended. Definitions need no SQL enum constraints or duplicated snapshot storage.

Startup migrates under the existing OS-backed instance lock. Manual migration
requires the bot stopped. See [manual upgrade](../DEPLOYMENT.md#manual-phase-8-upgrade).
Offline tests cover crossings, both sides, restart/session behavior, peaks/minimums,
1/5/15-minute baselines, missing evidence/replays, composites, private/channel
delivery, Phase 7 escalation/outcomes, UI editing/tokens and disposable upgrades.
Frozen TypeScript reference fixtures remain unchanged.
