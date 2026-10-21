# Production deployment preparation

Phase 6A prepares this repository only. No VPS access, production migration, deployment or live Telegram post has been performed. Run the commands below **on the intended server only after reviewing configuration and the release**. Helpers execute against the local Docker daemon; they contain no SSH commands.

See the [verification record and complete file inventory](docs/PRODUCTION-READINESS.md). Developers can repeat the isolated, network-disabled production-image check after building with `node scripts/smoke-production.mjs`; it uses synthetic data and UUID-named disposable local Docker resources, never the deployment project or its .env.

## Prerequisites and limits

- Ubuntu with Git, Bash/coreutils, Docker Engine and the Docker Compose plugin. Follow [Docker's Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/). The operator needs Docker access and permission to manage `/opt/stock-alert-bot` and backups.
- One instance per bot token and one stable Compose project. Defaults below use `stock-alert-bot`. Never scale above one. A separate SQLite OS lock beside the database rejects another app or migration process using the same data path and releases automatically on process death. It cannot protect against an unrelated host using the same token or a different database: Telegram 409 is a fatal conflict; stop the other poller.
- Node 24 LTS is inside the image; host Node/pnpm are unnecessary. Build and runtime share the pinned Debian image/digest so native SQLite binaries use the same ABI/platform. Linux amd64 is tested locally; rebuild and verify on the target architecture rather than copying a mismatched image. Review and refresh the pinned digest for security updates.
- Default limits: 512 MiB RAM, one CPU, 128 processes, read-only root, 32 MiB `/tmp`, no extra capabilities, no new privileges, no public ports, ordinary Compose networking. Only `/app/data`, explicit backup mounts and `/tmp` are writable. Tune resources against real watchlist/load measurements before increasing polling.
- Logs rotate at 10 MiB × three files. SQLite uses a named volume, WAL, foreign keys and a five-second busy timeout. Do not store it on a shared/network filesystem. Maintain disk space for snapshots, events, outbox and backups.

**Market-data gate:** the current TSETMC book has no verified source timestamp/completeness contract, so live queue snapshots and alerts remain blocked. API HTTP success is not valid market evidence. [TSETMC observations](docs/TSETMC.md) explain the unresolved contract. Test target-VPS HTTPS/DNS reachability separately; this phase has not done so. Do not bypass validation to make alerts appear operational.

## First deployment

Choose a repository URL containing the reviewed, committed source, lockfile and migrations. No private repository URL is assumed here.

```bash
read -r -p 'Repository URL: ' REPO_URL
sudo install -d -o "$USER" -g "$(id -gn)" /opt/stock-alert-bot
git clone "$REPO_URL" /opt/stock-alert-bot
cd /opt/stock-alert-bot
cp .env.example .env
chmod 600 .env
${EDITOR:-nano} .env
```

Set `NODE_ENV=production`, a real BotFather token, and authorized IDs. Every admin ID **must also appear in** `TELEGRAM_ALLOWED_USER_IDS`. Leave both admin/channel settings blank for private-only operation, or configure the numeric `-100…` channel ID and grant the bot administrator/posting rights. Each authorized private user/admin must initiate a private conversation. Subscribers do not need to start the bot.

Set `DATABASE_URL=/app/data/stock-alert.db`. Container paths outside `/app/data` are rejected by the image's `STOCK_ALERT_CONTAINER=true` guard, including disposable `/tmp` paths. `./data/stock-alert.db` resolves to the same persistent directory because the image workdir is `/app`. Do not override the container guard.

Keep `MARKET_MONITOR_FORCE_ACTIVE=false`. Production rejects this bypass, in-memory databases and `DEBUG` request logging. Configure explicit confirmed Tehran sessions/closure dates; the default empty schedule is deliberately closed. Preserve the remaining validated timeout/concurrency/history/outbox defaults unless a measured operational need justifies changing them. [.env.example](.env.example) and [README configuration](README.md#configuration) list settings.

Use this same project/function in every new operator shell:

```bash
cd /opt/stock-alert-bot
export COMPOSE_PROJECT_NAME=stock-alert-bot
dc() { docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file .env -f compose.yaml "$@"; }
dc config --quiet
dc build bot
dc run --rm --no-deps -T bot node dist/diagnostics.js
dc run --rm --no-deps -T bot node dist/database/migrate.js
dc up -d --no-deps --scale bot=1 bot
dc ps bot
dc logs --tail=100 bot
bash scripts/healthcheck.sh
```

Build must precede migrations on a first install because the migration command uses compiled code from the image. The named volume is created automatically with the image's UID/GID 1000 ownership. There is one service and no dependency containers. The diagnostics command before startup reports valid configuration with unavailable runtime health; it never contacts Telegram/TSETMC, prints IDs/tokens or migrates data. `dc config --quiet` validates without dumping expanded secrets; do not publish full Compose config or environment/inspect output.

Startup acquires the database instance lock, applies reviewed migrations, starts the shared collector, initializes Telegram, verifies the configured channel and begins long polling/outbox delivery. The healthcheck has a 60-second start period. Invalid channel permissions affect channel delivery, not private access. No automatic permission-test post is sent.

Before enabling real use, an operator must confirm Telegram access/channel permissions and target-VPS TSETMC connectivity. A non-publishing transport check can use:

```bash
dc run --rm --no-deps -T bot node --input-type=module -e 'const r = await fetch(new URL("/api/Instrument/GetInstrumentSearch/%D8%AE%D9%88%D8%AF%D8%B1%D9%88", process.env.TSETMC_BASE_URL), {signal: AbortSignal.timeout(10000)}); console.log({httpStatus:r.status}); if (!r.ok) process.exitCode=1;'
```

This sends one read-only TSETMC request and is **not** verification of book semantics. It has not been executed on your VPS in this phase. There is no equivalent automatic Telegram test message.

## Backup and retention

Create a dedicated host directory writable by image UID 1000. If your operator UID differs, use appropriate ACLs or run host file inspection/restore helpers with sudo; do not make backups world-readable.

```bash
sudo install -d -m 0700 -o 1000 -g 1000 /opt/stock-alert-backups
bash scripts/backup-db.sh /opt/stock-alert-backups /app/data/stock-alert.db
```

This works while the bot is running. The helper uses a one-off container, the existing data volume and an explicit host backup mount. The Node CLI uses the [SQLite online backup API](https://www.sqlite.org/backup.html), verifies source/destination integrity and foreign keys, and finalizes a portable DELETE-journal artifact requiring no WAL/SHM sidecars. It does not copy a live `.db` file or migrate the source. Filenames contain a canonical source-path hash, UTC timestamp and UUID; existing files are never overwritten. A completed backup path is printed. Verify free space and copy important backups off-host using your approved storage procedure.

Recommended policy: daily backups and a separate pre-update backup; retain at least fourteen days plus release recovery points. No deletion occurs by default. Explicitly approve pruning only generated, source-matching regular files older than fourteen days:

```bash
bash scripts/backup-db.sh /opt/stock-alert-backups /app/data/stock-alert.db 14 --confirm-prune
```

Unrelated files, symlinks and other source-path prefixes are untouched. Restoring to a different database path creates a new backup prefix; retain/archive older recovery points deliberately. Schedule the non-pruning command through the server's existing scheduler if desired; this repository does not install or alter cron/system services automatically.

## Updates and migrations

Keep the project name, volume and database path stable. If upgrading an earlier installation with another Compose project/volume, use **that existing project name** with `COMPOSE_PROJECT_NAME`; do not silently switch to a new empty volume. Inventory old data and back it up first. Never use `docker compose down -v`, volume prune or schema push for an update.

From `/opt/stock-alert-bot`, with the `dc` function above:

```bash
bash scripts/backup-db.sh /opt/stock-alert-backups /app/data/stock-alert.db
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
docker image tag tse-stock-alert-bot:local "tse-stock-alert-bot:rollback-$STAMP"
git status --short
git pull --ff-only
# Review the release, migrations, environment changes and local modifications.
bash scripts/deploy.sh /opt/stock-alert-backups /app/data/stock-alert.db --confirm-deploy
dc logs --tail=100 bot
bash scripts/healthcheck.sh
```

`deploy.sh` operates only on the reviewed local checkout. It validates configuration, verifies the explicit backup path matches the configured database, makes another consistent backup, builds before downtime, stops only `bot`, runs the dedicated locked migration command, and recreates only `bot` with one replica. It does not fetch code, SSH to a host, stop unrelated services or prune volumes/images. It requires `--confirm-deploy`. If build/backup fails the existing app stays up. If migration/start fails, it stops and leaves data intact for investigation; it never restores or resumes a potentially incompatible release automatically. A returned start command is not proof of healthy upstream services.

For a migration without an application update:

```bash
bash scripts/backup-db.sh /opt/stock-alert-backups /app/data/stock-alert.db
dc stop bot
dc run --rm --no-deps -T bot node dist/database/migrate.js
dc up -d --no-deps --scale bot=1 bot
```

Migrations remain append-only. Startup migration replay is safe, but maintenance deliberately requires the app stopped: the same instance lock rejects migration while it is running. The migration CLI does not need Telegram credentials when invoked natively; Compose still loads its configured env file. No new database schema migration is needed for Phase 6A. See [migration history](docs/MIGRATION.md).

## Health and diagnostics

```bash
bash scripts/healthcheck.sh
dc exec -T bot node dist/diagnostics.js
dc logs --tail=100 bot
dc ps bot
```

Health snapshots are atomic, private files in `/tmp`, updated every fifteen seconds from the actual collector progress, Telegram polling results and an SQLite read/write probe. A fresh timer alone is insufficient. Progress advances on monitor ticks and completed symbol requests; the stall allowance is poll interval + whole-symbol timeout + thirty seconds. Closed-session and empty-watchlist ticks still advance it. Heartbeats older than ninety seconds, dead processes, stopped/stalled monitor, database probe failure, and no successful Telegram polling/initialization for two minutes produce nonzero health status. Startup remains unhealthy until initialization finishes. Temporary Telegram failures are reported separately and can recover without a restart.

Market diagnostics distinguish closed, no monitored symbols, fresh data, unverified/stale data, temporary transport failure and persistent transport failure (at least five failed attempts on a symbol). TSETMC outages and unverified data are degraded upstream/data states, not reasons to restart an otherwise live worker: health can remain zero while alerts are unavailable. This is deliberate; use diagnostics/logs and `/status`, not container health alone, to assess data readiness. `liveBookContractVerified: false` remains explicit. Telegram diagnostics cover polling connectivity; per-message/channel failures remain in outbox/admin status, and may coexist with healthy polling.

Compose restarts exited containers, not merely unhealthy ones. No automatic kill/restart loop is installed. Investigate a stalled worker/database failure before restarting. SIGTERM cancels initialization, market work and delivery, joins tasks and closes SQLite/releases the lock within a fifteen-second application deadline; Compose allows twenty-five seconds. The online backup does not need to hold that application lock.

## Recovery and rollback

Restart only this service with `dc restart bot`. For Telegram failures, inspect safe logs/config diagnostics, token validity, outbound HTTPS/DNS and 409 conflicts from another poller. Never print/paste the token, raw request logging or expanded environment. For channel failures, use private `/admin` → channel status, fix administrator/posting rights and inspect failed outbox status. Temporary errors retry automatically; permanently failed occurrences are not silently republished.

For TSETMC failures, inspect `/status`, transport/backoff diagnostics and a controlled target-host connectivity check. Changing retry settings or force-opening a schedule cannot supply missing timestamp/completeness evidence. For database failures, inspect host free space, volume permissions and logs; do not delete WAL/SHM files or force-remove lock files. The OS lock is released on process death. If another process owns it, stop that process cleanly.

Restore only after choosing the intended backup and stopping the bot. The helper refuses a running project bot and **never overwrites an existing database, WAL or SHM target**:

```bash
dc stop bot
# Replace this path with the exact chosen generated backup filename.
read -r -p 'Absolute backup file: ' BACKUP
bash scripts/restore-db.sh "$BACKUP" /app/data/stock-alert-restored.db --confirm-restore
${EDITOR:-nano} .env
# Set DATABASE_URL=/app/data/stock-alert-restored.db; retain the original DB untouched.
dc run --rm --no-deps -T bot node dist/database/migrate.js
dc up -d --no-deps --scale bot=1 bot
bash scripts/healthcheck.sh
```

Choose another unused target filename for a later restore. Integrity/foreign-key checks run before success is reported. Restoring an older backup loses newer changes and can replay recent pending notifications; obsolete ones still expire and at-least-once delivery can duplicate an already accepted Telegram message. Review recovery impact before resuming. Do not automatically clean up original files.

Rollback an image only if the current schema is confirmed backward-compatible. Otherwise restore the pre-update backup to a new path and pair it with the matching image/release. Set `STOCK_ALERT_IMAGE_TAG=rollback-<the saved timestamp>` in `.env`, then use `dc up -d --no-deps --no-build --scale bot=1 bot` after stopping the current bot. Do not build over a saved rollback tag. Reset the desired build tag deliberately before a later update. Keep the previous image and backup until the new release is accepted; this project performs no automatic destructive rollback.
