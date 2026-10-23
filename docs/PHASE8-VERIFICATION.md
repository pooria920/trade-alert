# Phase 8 local verification

Verified on 2026-10-23 with the repository `.venv`, Python 3.14.4. Work was local;
no VPS, deployed container, live database or real Telegram notification was used.
Existing uncommitted Phase 7 work was retained. The attachment's TypeScript,
grammY, Zod and Vitest references were adapted to the current Python stack.

## Delivered

- Inclusive buy/sell absolute thresholds, with persisted per-leaf readiness,
  initial-condition suppression and configurable hysteresis.
- Buy/sell drops from the highest observed session volume, separate shared peaks,
  persisted source/coverage timestamps, minimum peak and frozen recovery baseline.
- Existing buy/sell rolling-drop evaluation extended to 15 minutes, preserving
  1/5-minute semantics and bounded, same-session historical lookup.
- New leaves in existing channel AND/OR trees, private/channel Persian wizards,
  presets/custom input, optional recovery settings, editing and confirmations.
- Existing leased outbox, cooldowns, Phase 7 escalation/outcomes and authorization.
  No new dependencies, environment settings, polling frequency or upstream requests.

`ThresholdConfig`, `PeakConfig`, `queue_threshold`, `queue_peak_drop` and their
recovery functions join the existing typed strategy mechanism. `DropConfig` now
accepts 1, 5 or 15 minutes. Configuration examples and exact lifecycle semantics
are in [QUEUE-STRATEGIES.md](QUEUE-STRATEGIES.md).

## Files changed for this phase

- Engine/configuration: `trade_alert/definitions.py`, `evaluators.py`, `composite.py`,
  `engine.py`, `snapshots.py`, `cleanup.py`.
- Telegram: `trade_alert/admin.py`, `bot.py`, `bot_text.py`, `delivery.py`.
- Database: new `migrations/0007_queue_strategies.sql`; appended
  `migrations/meta/_journal.json` entry.
- Tests: new `tests/test_strategies.py`; extended `test_bot.py`, `test_quality.py`;
  migration-count updates in `test_database.py`; additive state-column exclusion
  from the frozen legacy trace comparison in `test_persistence.py`.
- Documentation: `README.md`, `AGENTS.md`, `DEPLOYMENT.md`, `docs/MIGRATION.md`, new
  `docs/QUEUE-STRATEGIES.md` and this report. `.env.example` needs no Phase 8 additions.

## Actual results

All commands below ran from the repository root using `.venv/Scripts/python.exe`:

| Check | Result |
| --- | --- |
| `python -m mypy` | PASS, 35 source files |
| `python -m ruff check trade_alert tests` | PASS |
| `python -m unittest discover -s tests -v` | PASS, 240 tests, 20.367 seconds |
| `python -m ruff format --check trade_alert tests` | PASS, 60 files |
| `python -m pip check` | PASS, no broken requirements |
| `python -m compileall -q trade_alert tests` | PASS |
| `docker build -t tse-stock-alert-bot:local .` | PASS on local Docker Desktop |
| `python tests/smoke_image.py` | PASS, network-disabled disposable container/UUID volume |
| `git diff --check` | PASS; Git reported line-ending normalization notices |

The container smoke check reported UID 1000, synthetic private/channel targets,
healthy MTProto/database/closed-market state, SIGTERM shutdown and portable backup.
It used fake transports and did not run deployment helpers. Persian menu/recovery
copy was also checked with the Persian cleanup/lint tools (zero lint issues).

Unchanged captured reference outputs passed: 96 schedule, 156 configuration,
94 definition, 489 composite, 81 detector and 536 evaluator cases. Expectations
were not regenerated from the Python implementation.

New coverage includes both sides/directions, equality, none/UNKNOWN, readiness,
hysteresis, edits/enabling, restarts, session reset, peaks/minimums, invalid/stale/
late/replayed evidence, bounded 1/5/15-minute baselines, missing samples, composite
recovery, private/channel outbox routing, Persian UI tokens/back/edit/save, peak
escalation and historical outcomes with NULL traded-price metrics.

## Migration and compatibility

Migration 0007 only adds nullable `alert_states.strategy_data`, shared
`queue_session_peaks` and its retention index. Existing SQL files and original
journal entries are retained. A populated pre-Phase-8 disposable database upgraded
with unchanged prior migration hashes, rules/destinations, notification preferences,
frozen recovery, events, retry counts, pending outcomes and suppression counts.
Integrity/foreign-key checks passed. NULL-owner records remain quarantined.

Legacy channel `minimumVolume` definitions retain level-trigger/recovery semantics;
new crossing definitions use `thresholdVolume`. Existing drop/disappearance rules,
private/channel destinations, Phase 7 policies/outcomes and captured engine traces
remain compatible. No automatic conversion or re-ownership occurs.

## Remaining limits and manual deployment

BestLimits evidence remains unverified, so live alerts are still blocked. Peaks
are highest **observed** volumes, with no guarantee of full-session coverage. Polling
can miss intrainterval changes. Gaps outside tolerance cannot supply rolling
baselines or fabricated previous threshold values. Traded-price returns remain
NULL. Delivery retains its documented at-least-once duplication possibility.

Exact later operator commands are in
[Manual Phase 8 upgrade](../DEPLOYMENT.md#manual-phase-8-upgrade): checked online
backup using the current image, build reviewed Phase 8 image, validate Compose,
stop only the bot, locked offline migration on the same named volume, then start
one replica and check health. No volume removal, live test posts, automatic
rollback or deployment was performed.
