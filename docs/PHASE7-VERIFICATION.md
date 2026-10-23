# Phase 7 delivery report

Verified locally on 2026-10-23 with Python 3.14.4. No VPS connection, production
database change, real Telegram notification or deployment was performed.
Existing unrelated/uncommitted edits were retained.

## Implemented

- Persistent per-rule occurrence suppression, with optional additional-drop
  escalation from original frozen per-leaf baselines, cooldown/interval gates,
  independent occurrence keys and unchanged recovery/rearming semantics.
- Strict notification policy, conservative defaults, Persian severity labels and
  supported-metric threshold classification for private/shared/composite alerts.
- Central Persian formatting, compact large numbers, source/announcement Tehran
  times, delayed-data notices, validated stock links, literal text and UTF-16 bounds.
- Persistent independent 5/15/30-minute queue observations, same-session tolerance,
  missing/expired statuses, crash recovery, continued collection after rule/watchlist
  removal and indexed bounded retention through existing cleanup.
- Owner-scoped paginated private history, current-admin shared history/outcomes,
  SQL-backed 30-day channel `/stats`, readonly aggregation outside the monitor loop,
  cancellation/join and authorization/destination rechecks after asynchronous reads.
- Existing-rule advanced admin settings without raw JSON: base severity, optional
  thresholds, escalation/interval/additional drop, cooldown and outcome tracking,
  with a confirmation summary, Back/Cancel, refreshed tokens and optimistic revisions.

Behavior, calculations and operational ceilings are described in
[ALERT-QUALITY.md](ALERT-QUALITY.md). Production migration/deployment instructions
are in [DEPLOYMENT.md](../DEPLOYMENT.md).

## File inventory for this task

Created:

- `trade_alert/quality.py`, `trade_alert/outcomes.py`
- `migrations/0006_alert_quality.sql`
- `tests/test_quality.py`
- `docs/ALERT-QUALITY.md`, `docs/PHASE7-VERIFICATION.md`

Modified application modules:

- `engine.py`, `composite.py`: occurrence policy and frozen-baseline escalation.
- `alerts.py`, `channels.py`: scoped settings/history/statistics services.
- `admin.py`, `bot.py`, `bot_text.py`, `delivery.py`: UI, presentation and delivery.
- `monitor.py`, `runtime.py`, `cleanup.py`, `config.py`: existing pipeline wiring,
  bounded reconciliation/retention and validated environment settings.

Modified tests: `tests/test_bot.py`, `tests/test_config.py`,
`tests/test_database.py`, `tests/test_persistence.py`. Legacy fixture expectations
were not regenerated or edited. Legacy comparisons explicitly project the original
fields and exclude new optional configuration/state metadata; new fields have
separate tests. The original schema is tested before applying the appended migrations.

Other modified files: `.env.example`, `AGENTS.md`, `README.md`, `DEPLOYMENT.md`,
`docs/MIGRATION.md`, `migrations/meta/_journal.json`. Pre-existing changes to
`DEPLOYMENT.fa.md`, `trade_alert/mtproto.py`, `tests/test_telethon.py` and `assets/`
were present before this task and are outside this change's inventory. Shared
bot/text/test files already had user changes; this task preserves them.

## Migration and environment

The additive migration adds optional rule/state policy columns, normal event
severity, indexed outcome observations and per-day suppression counters. Its new
journal entry is appended; previous SQL files and journal entries are unchanged.
No production data is reset or legacy owner guessed. See
[migration semantics](MIGRATION.md#phase-7), including why pending outcomes detach
from deleted events while retaining immutable provenance and authorization scope.

New environment defaults:

| Variable | Default | Validation |
| --- | --- | --- |
| `ALERT_OUTCOME_WINDOWS_MINUTES` | `5,15,30` | Distinct members of 5/15/30, no spaces |
| `ALERT_OUTCOME_TOLERANCE_SECONDS` | `30` | Integer 1..300 |
| `ALERT_OUTCOME_RETENTION_DAYS` | `30` | Integer 1..365 |

These do not enable tracking. Existing rules retain normal severity, recovery-based
suppression, escalation off and tracking off. No dependencies, ports, services,
polling frequency or Docker volume layout were added/changed.

## Final verification results

All commands ran from the repository root using `.venv/Scripts/python.exe`:

| Check | Actual result |
| --- | --- |
| `python -m mypy` | Passed; 35 source files |
| `python -m ruff check trade_alert tests` | All checks passed |
| `python -m unittest discover -s tests -v` | 225 tests passed in 31.258 seconds |
| `python -m ruff format --check trade_alert tests` | 59 files already formatted |
| `python -m pip check` | No broken requirements found |
| `python -m compileall -q trade_alert tests` | Passed, exit 0 |
| `docker build -t tse-stock-alert-bot:local .` | Passed on the available local engine |
| `python tests/smoke_image.py` | Passed with network-disabled disposable containers |
| `git diff --check` | Passed; Git only reported existing CRLF conversion notices |

The image smoke verified UID 1000 (`node`), private/shared fake delivery, healthy
closed-market state, SIGTERM shutdown, SQLite integrity/FKs and portable backup.
No production containers/volumes were used. Disposable resources were cleaned up.
TypeScript/Vitest checks do not apply to this repository's removed Node application;
strict mypy, unittest, compileall and the production Docker build are the current
stack's verification gates.

Migration tests upgrade disposable prior-schema databases, preserve old state/event/
outbox/watchlist data and migration hashes, check integrity/FKs, reopen idempotently,
and inject invalid Phase 7 SQL to verify rollback and subsequent recovery. The
unchanged captured legacy evaluator, composite, detector, schedule, configuration,
service, engine and delivery regressions pass alongside the new behavior tests.

## Limitations and deployment handoff

- BestLimits timestamp/completeness semantics remain unverified; live alerts remain
  blocked by the existing detector. No verification bypass was added.
- The existing accepted pipeline has queue prices, not verified last-traded/closing
  price observations. Outcome price metrics/returns remain NULL and are explicitly
  unavailable in history/statistics. Completed queue samples do not imply price
  samples or profitable trades.
- Direct retrieval of the TSETMC site failed locally. Link generation uses the
  documented instrument path cited in ALERT-QUALITY.md; current site availability
  is not promised.
- Recovery semantics, composite UNKNOWN handling and at-least-once transport
  behavior remain unchanged. A Telegram-accepted message can still duplicate after
  a crash before success is recorded.
- Statistics cover the currently configured shared destination and retained data;
  daily suppression buckets include the UTC day intersecting the cutoff. Deleted
  events leave event totals while separately retained outcomes can remain counted.
- Due reconciliation handles at most 1,000 rows per cycle. Large backlogs can take
  several cycles. Long summaries use an explicit ellipsis within Telegram limits.

For the next deployment, follow the exact commands in
[DEPLOYMENT.md](../DEPLOYMENT.md): preserve the existing project/volume, create a
checked external backup **before** build/stop/migrate, build a reviewed tagged image,
validate Compose, stop only the bot, run the locked migration, start one replica,
then inspect health privately. Leave the bot stopped if migration fails. Restore
only to a new explicit path after review; no automatic rollback or test post.
