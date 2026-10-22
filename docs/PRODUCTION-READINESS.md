# Phase 6A verification record — 2026-10-21

## Bale adaptation — 2026-10-22

The Node.js runtime now targets Bale through grammY at `https://tapi.bale.ai`, with `BALE_*` settings and a fresh database. Existing SQL migrations are unchanged. Typecheck, lint, tests, formatting and build passed under Node.js 24.22.0. Windows passed 197 tests with one Linux-only test skipped; the Docker build passed all 198 tests. The local network-disabled production smoke test passed startup, private/channel outbox delivery, health, backup/restore, instance exclusion and SIGTERM shutdown. All messaging responses were mocked; no live Bale request, notification or VPS deployment was performed. The original verification record follows.

Repository preparation only. No VPS connection, production database change, remote deployment or live Telegram message was performed.

## Audit findings and changes

| Finding                                                            | Resolution                                                                                                                                                                                               |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Event-loop heartbeat could hide a stalled collector                | Health now uses collector progress, database write probe and actual Telegram polling results; idle sessions remain healthy                                                                               |
| No shared-volume application/migration exclusion                   | A separate OS-backed SQLite lock excludes concurrent owners and releases on process death; canonical paths prevent symlink aliases and hard-linked DB paths are rejected                                 |
| Missing backup/restore tooling                                     | Online backup API, verified portable single-file artifacts, unique names, explicit source/destination paths, opt-in scoped retention, restore to a new path only                                         |
| Mutable major-only image and unrestricted Compose runtime          | Digest-pinned Node 24.22.0 Debian, matching native build/runtime, production-only dependencies, clean compiled output, memory/CPU/PID limits, read-only root, dropped capabilities and no-new-privileges |
| Admin settings could grant access outside the authorized user list | Startup now requires every admin in TELEGRAM_ALLOWED_USER_IDS                                                                                                                                            |
| Production schedule/memory/debug/storage footguns                  | Reject schedule bypass, in-memory DB, DEBUG request logging and container database paths outside /app/data                                                                                               |
| No repeatable operational procedure                                | Local-only explicit helper scripts, full installation/update/migration/recovery/rollback runbook, safe configuration diagnostics                                                                         |

The existing alert engine, evaluators, authorization handlers, parameterized queries, source validation, rate limits and outbox delivery guarantees were retained. No Phase 6A schema migration or new alert type was needed.

## Verification performed

- `pnpm install --frozen-lockfile`: passed.
- Typecheck, ESLint, Prettier check and compiled production build: passed.
- Windows Vitest: 181 passed, one Ubuntu-only helper test skipped.
- Linux amd64 Docker build: all 182 tests passed, including Bash syntax and mocked helper safety checks; native better-sqlite3 compiled successfully for the runtime platform.
- `pnpm audit --prod`: no known vulnerabilities reported at verification time; this is not a guarantee against future advisories.
- `docker build -t tse-stock-alert-bot:local .`: passed. Compose configuration validation with `.env.example` and no secret resolution: passed.
- Existing legacy/Phase 4 migration-preservation tests passed. A migrator and second application were rejected while the real app owned the test DB; stopped migration replay succeeded.
- WAL online backup, unique naming, source/destination integrity, foreign keys, scoped opt-in retention, existing-target refusal and restore to a new path passed against temporary databases.
- `node scripts/smoke-production.mjs`: passed against disposable containers/volume with networking disabled. Tested the actual compiled main process with mocked grammY transport; private/channel pending outbox delivery, closed-market health, instance exclusion, online backup while running, restore from a read-only backup mount, container replacement with state/outbox preserved, no duplicate send, and SIGTERM exit zero.
- The smoke runtime used non-root, read-only root, no capabilities/new privileges, 512 MiB/one CPU/128 PID limits and a bounded tmpfs. It verified no `.env`, source/tests, TypeScript/tsx/Vitest or source-map/schema-snapshot artifacts in the runtime image.

The runtime smoke's Telegram transport fixture intercepts both native Fetch and grammY's Node-fetch adapter. This fixture is created only in a temporary test directory and mounted into network-disabled test containers; it is not an application verification bypass. The script creates only UUID-named disposable Docker resources and removes its known artifacts without recursive host deletion. Run it locally after building the image; never substitute production data or a live env file.

## Created files

- `.gitattributes`
- `DEPLOYMENT.md`, `docs/PRODUCTION-READINESS.md`
- `scripts/build.mjs`, `scripts/compose-common.sh`, `scripts/backup-db.sh`, `scripts/restore-db.sh`, `scripts/deploy.sh`, `scripts/healthcheck.sh`, `scripts/smoke-production.mjs`
- `src/database/backup.ts`, `src/database/instance-lock.ts`, `src/diagnostics.ts`
- `tests/operations.test.ts`

## Modified files

- `.env.example`, `.gitignore`, `.dockerignore`, `Dockerfile`, `compose.yaml`, `package.json`, `tsconfig.build.json`
- `README.md`, `AGENTS.md`, `docs/MIGRATION.md`
- `scripts/copy-migrations.mjs`
- `src/config/env.ts`, `src/main.ts`, `src/database/migrate.ts`, `src/shared/health.ts`, `src/healthcheck.ts`, `src/market/market-monitor.ts`, `src/monitoring/monitoring-status.ts`
- `tests/bot.test.ts`, `tests/channels.test.ts`, `tests/fixtures/monitoring.ts`, `tests/health.test.ts`, `tests/lifecycle.test.ts`

## Manual configuration and remaining risks

Use [DEPLOYMENT.md](../DEPLOYMENT.md) for the exact first-VPS deployment commands. Supply the repository URL/reviewed release, real bot token, allowed/admin IDs, optional channel ID/posting rights, confirmed Tehran calendar/closures, stable Compose project/volume, database path, host backup permissions/schedule and off-host recovery storage. No real values are supplied by this phase.

Live TSETMC source timestamps/completeness remain unverified: live alerts are still gated off, even with healthy transport. Target-VPS Telegram/TSETMC access and actual channel rights have not been tested. Only Linux amd64 was verified; other architectures and real workloads/resource limits need validation. Data/events/outbox disk growth and backups need operational monitoring. Same-path file locking cannot enforce singleton polling on another host or database. Telegram delivery remains at least once; acceptance-before-recording crashes and restoring an older backup can duplicate recent notifications. Keep rollback images/schema-compatible backups and refresh the pinned base image/dependencies deliberately.
