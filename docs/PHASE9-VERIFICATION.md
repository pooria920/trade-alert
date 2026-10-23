# Phase 9 local verification

Verified locally on 2026-10-23 with Python 3.14.4 from the repository `.venv`.
The attachment's Node/TypeScript references were adapted to the current Python /
Telethon / sqlite3 application. Existing uncommitted Phase 7/8 changes were retained.
No VPS connection, production database/container change, live Telegram notification
or deployment was performed.

## Implemented

Shared per-instrument/side lifecycle state and source-timestamped event history now
support confirmed formation, weakening, disappearance and reformation. Confirmation,
gap handling, state validity, cycle counts and frozen weakening baselines persist.
First/gapped/unavailable observations re-prime quietly; opposite-side data remains
UNKNOWN. Events are source-update pulses in the existing composite context.

Private and channel rules support all four predicates, existing cooldown/severity/
outbox policies, Persian presets/custom input, editing and confirmation. Optional
weakening escalation uses the original occurrence evidence and existing Phase 7
policy; returning data cannot escalate a discontinued lifecycle occurrence.
Admin-only private `/lifecycle` shows scoped persisted state/history and honest
freshness/coverage. No per-rule market requests, new worker or dependency was added.

See [LIFECYCLE.md](LIFECYCLE.md) for exact states, configurations, confirmation,
weakening-cycle peak semantics, event timing, recovery and limitations.

## Files changed for Phase 9

- New business module: `trade_alert/lifecycle.py`.
- Evaluation/context: `trade_alert/definitions.py`, `evaluators.py`, `composite.py`,
  `engine.py`.
- Runtime/collection: `trade_alert/config.py`, `monitor.py`, `cleanup.py`, `runtime.py`.
- Telegram: `trade_alert/admin.py`, `bot.py`, `bot_text.py`.
- Migration: new `migrations/0008_queue_lifecycle.sql`; one appended journal entry.
- Tests: new `tests/test_lifecycle.py`; extended `test_bot.py`, `test_monitor.py`;
  new-settings projection in `test_config.py` and migration-count updates in
  `test_database.py`. Captured reference expectations were not changed.
- Documentation/config examples: `.env.example`, `AGENTS.md`, `README.md`,
  `DEPLOYMENT.md`, `docs/MIGRATION.md`, new `docs/LIFECYCLE.md` and this report.

## Actual final checks

All Python commands ran from the repository root with `.venv/Scripts/python.exe`:

| Command | Result |
| --- | --- |
| `python -m mypy` | PASS, 36 source files |
| `python -m ruff check trade_alert tests` | PASS |
| `python -m unittest discover -s tests -v` | PASS, 261 tests in 18.755 seconds |
| `python -m ruff format --check trade_alert tests` | PASS, 62 files |
| `python -m pip check` | PASS, no broken requirements |
| `python -m compileall -q trade_alert tests` | PASS |
| `docker build -t tse-stock-alert-bot:local .` | PASS, local Docker Desktop |
| `python tests/smoke_image.py` | PASS, disposable network-disabled container/UUID volume |
| `git diff --check` | PASS |

The image smoke result reported UID 1000, synthetic private/channel targets, healthy
MTProto/database/closed-market state, joined SIGTERM shutdown and portable checked
backup. No production deployment helpers ran. New Persian menu labels passed the
Persian cleanup/lint pass with zero lint issues.

The frozen reference cases also passed: 96 schedule, 156 configuration, 94 definition,
489 composite, 81 detector and 536 evaluator cases. Original tracked migration SQL
and captured reference files have no Git diff.

## New regression coverage

- Both sides and all four event kinds; repeated cycles and actual volume/source
  timestamp lineage; first-observed queue suppression.
- Multiple confirmations, interrupted candidates, duplicate source updates,
  short restart persistence, real SQLite close/reopen and stable event identifiers.
- Stale/late/unpersisted snapshots, opposite-side UNKNOWN, session changes and
  long gaps; unavailable data preserves confirmed state and returns without
  fabricated disappearance/reformation/weakening.
- Threshold equality/minimums, buy/sell setting isolation, per-boundary hysteresis,
  shared frozen baselines, quality escalation and occurrence invalidation after gaps.
- Unconfirmed disappearance cannot recover a weakening alert.
- Formation volume and reformation duration filters are pulse-local, not deferred
  permanent TRUE states; composite conditions must match the same update.
- Private/channel fan-out from one shared event, independent configured rules,
  cooldowns, replay suppression and fake outbox sends.
- Private/channel Persian wizards, presets, custom values, Back, edits, duration,
  stale tokens and duplicate saves; summary admin/group/symbol-scope restrictions.
- Collector integration: two users and channel rules shared exactly 12 synthetic
  fetches across 12 polling attempts, despite multiple rules. Halts and transport
  failures invalidated observations without transitions; closure added no fetch.
- Transaction rollback/retry and event retention preserving current lifecycle state.

## Migration safety and compatibility

Migration 0008 only creates `queue_lifecycle_states`, `queue_lifecycle_events`, event
uniqueness and indexed instrument/session history/retention. It does not alter or
backfill existing tables, definitions, snapshots or destinations. Shared lifecycle
history/state has no rule-delete cascade. Event retention never deletes state.

A disposable Phase 8 database upgraded with unchanged existing table contents,
crossing state, observed peaks and first eight migration hashes; integrity and
foreign-key checks passed. Existing populated Phase 7 upgrade regression coverage
also passed with channel/private rules, notification preferences, recovery, retry
counts and historical outcomes. Startup/manual migration still uses the instance
lock. NULL-owner rules remain quarantined.

Existing disappearance rules retain their original semantics; the new confirmed
lifecycle predicate has a distinct JSON type. Earlier private/channel/composite
strategies, Phase 7 policies/outcomes, health and shutdown checks all passed their
existing regressions.

## Remaining limits and later deployment

Live BestLimits data remains unverified and cannot create live lifecycle alerts.
Polling cannot reconstruct unobserved transitions. Confirmations are a shared
detector policy; individual minimum-volume/duration settings filter the confirmed
event update. Weakening uses a confirmed continuous queue-cycle peak, while the
separate observed session peak remains intact. Full-session coverage is never
guaranteed. Price-return fields remain unavailable; queue prices are not substituted.
Outbox delivery retains its documented at-least-once duplication possibility.

Exact later operator commands are in
[Manual Phase 9 upgrade](../DEPLOYMENT.md#manual-phase-9-upgrade): checked online
backup using the current image, build the reviewed image, validate Compose, stop
only the bot, locked offline migration on the same named volume, start one replica
and check health. No automatic rollback, named-volume deletion or test posts.
