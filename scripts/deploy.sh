#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/compose-common.sh"
[[ $# == 3 && "$3" == --confirm-deploy ]] || { echo 'Usage: deploy.sh ABSOLUTE_BACKUP_DIRECTORY /app/data/DATABASE.db --confirm-deploy (local Docker only)' >&2; exit 1; }
data_path "$2"
"${COMPOSE[@]}" config --quiet
configured="$("${COMPOSE[@]}" run --rm --no-deps -T bot node --input-type=module -e 'import {resolve} from "node:path"; console.log(resolve(process.env.DATABASE_URL));')"
[[ "$configured" == "$2" ]] || { echo 'Backup path does not match the configured database. Aborting.' >&2; exit 1; }
bash "$ROOT/scripts/backup-db.sh" "$1" "$2"
# Build reviewed local code before downtime. Code fetch/release selection is an explicit operator step.
"${COMPOSE[@]}" build bot
"${COMPOSE[@]}" stop bot
"${COMPOSE[@]}" run --rm --no-deps -T bot node dist/database/migrate.js
"${COMPOSE[@]}" up -d --no-deps --scale bot=1 bot
"${COMPOSE[@]}" ps bot
echo 'Update started. Run healthcheck.sh and inspect bot logs. No unrelated services were changed.'
