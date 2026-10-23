# Deployment

Follow the [Persian VPS deployment guide](DEPLOYMENT.fa.md) for the first Python
installation, MTProto/Fake-TLS proxy configuration, group setup, health and backups.
Docker builds from the repository root. No VPS deployment or live notification
has been performed during development.

## Manual Phase 9 upgrade

These are later operator instructions, not development actions. Review the source
revision, appended journal entry and additive `0008_queue_lifecycle.sql` first.
Use the existing project name, database filename and named volume. Optional
`LIFECYCLE_*` settings in `.env.example` control shared confirmation/history;
they do not change polling, verify market data or enable rule notifications.

On the existing deployment host in the reviewed repository directory (substitute
the installation's actual project name/path if different):

```sh
dc() { docker compose --project-name stock-alert-bot --env-file .env -f compose.yaml "$@"; }

# Use the currently deployed image for a checked online backup BEFORE build/stop.
# The existing external backup directory must be writable by UID/GID 1000.
dc run --rm --no-deps -T -v /opt/stock-alert-backups:/backups bot \
  python -m trade_alert.maintenance backup /app/data/stock-alert.db /backups

# Continue only after the backup reports its integrity-checked artifact.
docker build -t tse-stock-alert-bot:phase9 .
export STOCK_ALERT_IMAGE_TAG=phase9
dc config --quiet

# Stop only this bot; preserve its volume, DB/WAL/SHM and instance-lock files.
dc stop -t 25 bot
dc run --rm --no-deps -T bot \
  python -m trade_alert.maintenance migrate /app/data/stock-alert.db

# Continue only after migration succeeds. Start exactly one bot, without test posts.
dc up -d --no-deps --no-build --scale bot=1 bot
dc ps bot
dc exec -T bot python -m trade_alert.health
```

Check each stage's exit status before proceeding; this is not an unattended script.
Persist `STOCK_ALERT_IMAGE_TAG=phase9` in the operator's deployment environment and
allow startup/probe time before health inspection. Inspect old rules and new Persian
lifecycle confirmations privately; `/lifecycle` honestly shows absent/unverified
observations until accepted evidence exists. No old snapshots are backfilled into
events. Current BestLimits limitations still prevent live alerts.

If backup/build fails, leave the current bot running. If migration fails, leave it
stopped and investigate. Recovery uses a checked backup restored only to a new
explicit path after image/schema review; no automatic rollback or volume deletion.
See [lifecycle behavior and limits](docs/LIFECYCLE.md).

## Manual Phase 8 upgrade

Instructions for a later operator only; development does not execute production
commands. Use the existing project name, named volume and database path. Review
the source diff, `0007_queue_strategies.sql` and its appended journal entry first.
If upgrading from before Phase 7, review `0006_alert_quality.sql` too. No new
environment variables are required. Keep one bot per token/host.

On the deployment host in the existing repository directory, with the reviewed
source revision installed (substitute the real existing project/path if different):

```sh
dc() { docker compose --project-name stock-alert-bot --env-file .env -f compose.yaml "$@"; }

# Back up using the currently deployed image before building/stopping/migrating.
# The external backup directory must already be writable by UID/GID 1000.
dc run --rm --no-deps -T -v /opt/stock-alert-backups:/backups bot \
  python -m trade_alert.maintenance backup /app/data/stock-alert.db /backups

# Continue only after the backup reports its integrity-checked artifact.
docker build -t tse-stock-alert-bot:phase8 .
export STOCK_ALERT_IMAGE_TAG=phase8
dc config --quiet

# Stop only this bot, preserving its named data volume and instance-lock file.
dc stop -t 25 bot
dc run --rm --no-deps -T bot \
  python -m trade_alert.maintenance migrate /app/data/stock-alert.db

# Continue only after migration succeeds. No extra replicas or test messages.
dc up -d --no-deps --no-build --scale bot=1 bot
dc ps bot
dc exec -T bot python -m trade_alert.health
```

Run each stage only if the preceding stage succeeded; these are not an unattended
deployment script. Persist `STOCK_ALERT_IMAGE_TAG=phase8` in the operator's deployment
environment. Allow startup/probe time before health checks; inspect structured logs
privately if needed. Inspect old private/channel rules and new rule confirmations
through private bot commands, without posting test messages. BestLimits remains
unverified and cannot generate live alerts. Sessions and polling settings are unchanged.

If backup/build fails, leave the current bot running. If migration fails, leave it
stopped and investigate. No automatic rollback: restore a checked backup only to a
new explicit DB path, then review image/schema compatibility before starting. Never
overwrite existing DB/WAL/SHM files, remove the live lock or delete named volumes.
See [strategy semantics and limitations](docs/QUEUE-STRATEGIES.md).

## Manual Phase 7 upgrade

These commands are instructions for a later operator-controlled update. Development
must not execute them against production. Keep the deployed Compose project name,
database filename and named volume unchanged; substitute the actual project/path if
installation differs. Use the reviewed source revision and keep secrets out of Git.
The new migration is `0006_alert_quality.sql`; existing settings stay conservative.

On the deployment host, in the existing repository directory:

```sh
# Match the existing installation; do not create a second Compose project.
dc() { docker compose --project-name stock-alert-bot --env-file .env -f compose.yaml "$@"; }

# 1. Back up with the currently deployed image BEFORE building/stopping/migrating.
# Existing external backup directory must be writable by UID/GID 1000.
dc run --rm --no-deps -T -v /opt/stock-alert-backups:/backups bot \
  python -m trade_alert.maintenance backup /app/data/stock-alert.db /backups

# 2. Only continue after the backup command reports its checked artifact.
# Review the source diff and the additive migration; install the reviewed revision.
# Add optional outcome env settings from .env.example. They do not enable tracking.
docker build -t tse-stock-alert-bot:phase7 .
export STOCK_ALERT_IMAGE_TAG=phase7
dc config --quiet

# 3. Stop only this bot. No volume deletion, down -v, unrelated containers or replicas.
dc stop -t 25 bot

# 4. Locked, offline migration using the new image and the same named volume.
dc run --rm --no-deps -T bot \
  python -m trade_alert.maintenance migrate /app/data/stock-alert.db

# 5. Only after migration succeeds, start one bot. Do not publish test notifications.
dc up -d --no-deps --no-build --scale bot=1 bot
dc ps bot
dc exec -T bot python -m trade_alert.health
```

Persist `STOCK_ALERT_IMAGE_TAG=phase7` in the operator's deployment environment for
subsequent Compose invocations. Allow startup/probe time before reading health;
inspect structured logs privately if health fails. Empty sessions and unverified
market data must still be reported honestly. Check existing private/channel rules,
admin permissions and pending messages through the private bot interface. Use no
test posts to verify channel permission. Tracking/escalation remain off until a
current admin explicitly changes and confirms an individual rule's advanced settings.

If build/backup fails, keep the old deployment running. If migration fails, leave
the bot stopped and investigate; no automatic rollback. For recovery, stop the bot,
restore the verified backup to a **new explicit** database path with the maintenance
CLI described in the Persian guide, review that path/image combination, then decide
whether to migrate/start. Never overwrite DB/WAL/SHM or remove the instance lock.

See [Phase 7 behavior/limitations](docs/ALERT-QUALITY.md). Price-return statistics are
unavailable until the existing pipeline supplies verified traded prices; queue
prices are never substituted. No live alert can bypass the existing evidence gate.
