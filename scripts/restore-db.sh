#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/compose-common.sh"
[[ $# == 3 && "$3" == --confirm-restore ]] || { echo 'Usage: restore-db.sh ABSOLUTE_BACKUP_FILE /app/data/NEW_DATABASE.db --confirm-restore' >&2; exit 1; }
[[ "$1" == /* && -f "$1" ]] || { echo 'Backup must be an existing absolute file path.' >&2; exit 1; }
data_path "$2"
[[ -z "$("${COMPOSE[@]}" ps --status running -q bot)" ]] || { echo 'Stop this project bot before restoring. Existing target files are never overwritten.' >&2; exit 1; }
backup="$(realpath -- "$1")"
"${COMPOSE[@]}" run --rm --no-deps -T -v "$(dirname -- "$backup"):/restore:ro" bot node dist/database/backup.js restore "/restore/$(basename -- "$backup")" "$2" --confirm-restore
echo 'Restored to the new path. Keep the original, update DATABASE_URL in .env, migrate, then start bot.'
