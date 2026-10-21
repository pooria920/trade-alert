#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
command -v docker >/dev/null || { echo 'Docker CLI is required.' >&2; exit 1; }
endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}')"
if [[ -n "${DOCKER_HOST:-}" && -z "${DOCKER_CONTEXT:-}" ]]; then endpoint="$DOCKER_HOST"; fi
[[ "$endpoint" == unix://* || "$endpoint" == npipe://* ]] || { echo 'Use a local Docker socket; remote Docker contexts are refused.' >&2; exit 1; }
export DOCKER_HOST="$endpoint"
unset DOCKER_CONTEXT
[[ -f "$ROOT/.env" ]] || { echo 'Create the project .env first; see DEPLOYMENT.md.' >&2; exit 1; }
COMPOSE=(docker compose --project-name "${COMPOSE_PROJECT_NAME:-stock-alert-bot}" --project-directory "$ROOT" --env-file "$ROOT/.env" -f "$ROOT/compose.yaml")
data_path() {
  [[ "$1" == /app/data/* && "$1" != *'/../'* && "$1" != *'/./'* && "$1" == *.db ]] || { echo 'Use an explicit .db path below /app/data/.' >&2; exit 1; }
}
