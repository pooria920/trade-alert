#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/compose-common.sh"
[[ $# == 0 ]] || { echo 'Usage: healthcheck.sh' >&2; exit 1; }
"${COMPOSE[@]}" exec -T bot node dist/healthcheck.js
