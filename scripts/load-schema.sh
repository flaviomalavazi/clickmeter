#!/usr/bin/env bash
# Load the schema (DDL + seed data) into ClickHouse before running the benchmark.
#
#   scripts/load-schema.sh                              # loads schema/schema.sql.example
#   scripts/load-schema.sh schema/my-schema.sql         # custom file
#
# Honours the same env knobs as run.sh: CH_HOST, CH_PORT, CH_USER, CH_PASSWORD, CH_DATABASE.
# Connection settings come from config/benchmark.properties and credentials from
# .env (same source as the benchmark). Pre-set CH_* environment vars still win.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL_FILE="${1:-$ROOT/schema/schema.sql.example}"

# Seed CH_* from a key=value file (non-secret props + .env creds) without
# clobbering anything already exported in the environment.
seed_from() {
    [[ -f "$1" ]] || return 0
    while IFS='=' read -r key val; do
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        key="$(printf '%s' "$key" | tr -d '[:space:]')"
        [[ -z "$key" ]] && continue
        val="${val%\'}"; val="${val#\'}"; val="${val%\"}"; val="${val#\"}"
        case "$key" in
            ch_host)     : "${CH_HOST:=$val}" ;;
            ch_port)     : "${CH_PORT:=$val}" ;;
            ch_user)     : "${CH_USER:=$val}" ;;
            ch_password) : "${CH_PASSWORD:=$val}" ;;
            ch_protocol) : "${CH_PROTOCOL:=$val}" ;;
        esac
    done < "$1"
}
seed_from "$ROOT/config/benchmark.properties"
seed_from "$ROOT/.env"

CH_HOST="${CH_HOST:-localhost}"
CH_PORT="${CH_PORT:-8123}"
CH_USER="${CH_USER:-default}"
CH_PASSWORD="${CH_PASSWORD:-}"
CH_PROTOCOL="${CH_PROTOCOL:-http}"

if [[ ! -f "$SQL_FILE" ]]; then
    echo "ERROR: SQL file not found: $SQL_FILE" >&2
    exit 1
fi

echo "Loading $SQL_FILE into ${CH_PROTOCOL}://${CH_HOST}:${CH_PORT} ..."

curl -sS -X POST \
    --data-binary "@${SQL_FILE}" \
    -H "X-ClickHouse-User: ${CH_USER}" \
    -H "X-ClickHouse-Key: ${CH_PASSWORD}" \
    "${CH_PROTOCOL}://${CH_HOST}:${CH_PORT}/" \
    && echo "OK"
