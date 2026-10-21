#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/compose-common.sh"
[[ $# == 2 || $# == 4 ]] || { echo 'Usage: backup-db.sh ABSOLUTE_HOST_DIRECTORY /app/data/DATABASE.db [DAYS --confirm-prune]' >&2; exit 1; }
[[ "$1" == /* && -d "$1" ]] || { echo 'Backup directory must exist and be absolute; grant UID 1000 write access.' >&2; exit 1; }
data_path "$2"
args=()
if [[ $# == 4 ]]; then
  [[ "$3" =~ ^[1-9][0-9]{0,3}$ && "$3" -le 3650 && "$4" == --confirm-prune ]] || { echo 'Retention requires 1–3650 days and explicit --confirm-prune.' >&2; exit 1; }
  args=("$3" --confirm-prune)
fi
directory="$(realpath -- "$1")"
"${COMPOSE[@]}" run --rm --no-deps -T -v "$directory:/backups" bot node dist/database/backup.js backup "$2" /backups "${args[@]}"
