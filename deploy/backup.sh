#!/usr/bin/env bash

# PostgreSQL database backup for the Python project.
#
# Usage:
#   chmod +x deploy/backup.sh
#   ./deploy/backup.sh
#
# Optional environment variables:
#   AM_DATABASE_URL     PostgreSQL URI (required)
#   BACKUP_ROOT         backup root (default: /software/project/python/assets/databack)
#   BACKUP_RETAIN_DAYS  retention period in days (default: 10; 0 disables cleanup)
#   PG_CONTAINER        running PostgreSQL container ID/name (default: auto-detect pgvector)
#   PG_DUMP_IMAGE       Docker fallback image (default: postgres:16-alpine)

set -Eeuo pipefail

PG_HOST="${PG_HOST:-172.17.0.1}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-assets}"
PG_PASSWORD="${PG_PASSWORD:-}"
PG_DATABASE="${PG_DATABASE:-assets}"
AM_DATABASE_URL="${AM_DATABASE_URL:-}"
BACKUP_ROOT="${BACKUP_ROOT:-/software/project/python/assets/databack}"
BACKUP_RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-10}"
PG_CONTAINER="${PG_CONTAINER:-}"
PG_DUMP_IMAGE="${PG_DUMP_IMAGE:-postgres:16-alpine}"

if ! command -v gzip >/dev/null 2>&1; then
    echo "Error: gzip was not found in PATH." >&2
    exit 1
fi

if [[ -z "${AM_DATABASE_URL:-}" ]]; then
    echo "Error: AM_DATABASE_URL is empty." >&2
    exit 1
fi

# pg_dump accepts a connection URI through PGDATABASE. Keeping it in the
# environment avoids exposing the password in the process command line.
export PGDATABASE="${AM_DATABASE_URL}"

if ! [[ "${BACKUP_RETAIN_DAYS}" =~ ^[0-9]+$ ]]; then
    echo "Error: BACKUP_RETAIN_DAYS must be a non-negative integer." >&2
    exit 1
fi

TODAY="$(date +'%Y%m%d')"
DATETIME="$(date +'%Y%m%d%H%M%S')"
BACKUP_DIR="${BACKUP_ROOT}/${TODAY}"
SQL_FILE="${BACKUP_DIR}/postgres_${DATETIME}.sql"
ARCHIVE_FILE="${SQL_FILE}.gz"

cleanup_incomplete_backup() {
    rm -f -- "${SQL_FILE}" "${ARCHIVE_FILE}"
}
trap cleanup_incomplete_backup ERR INT TERM

mkdir -p -- "${BACKUP_DIR}"

echo "Backing up PostgreSQL database..."
echo "Backup directory: ${BACKUP_DIR}"

if command -v pg_dump >/dev/null 2>&1; then
    echo "Backup method: host pg_dump"
    pg_dump \
        --format=plain \
        --no-owner \
        --no-privileges \
        --file="${SQL_FILE}"
elif command -v docker >/dev/null 2>&1; then
    if [[ -z "${PG_CONTAINER}" ]]; then
        PG_CONTAINER="$(docker ps --format '{{.ID}} {{.Image}}' | awk '$2 ~ /pgvector/ && !found {print $1; found=1}')"
    fi

    if [[ -n "${PG_CONTAINER}" ]]; then
        echo "Backup method: running PostgreSQL container (${PG_CONTAINER})"
        docker exec \
            --env "PGHOST=127.0.0.1" \
            --env "PGPORT=${PG_PORT}" \
            --env "PGUSER=${PG_USER}" \
            --env "PGPASSWORD=${PG_PASSWORD}" \
            --env "PGDATABASE=${PG_DATABASE}" \
            "${PG_CONTAINER}" \
            pg_dump \
            --format=plain \
            --no-owner \
            --no-privileges > "${SQL_FILE}"
    else
        echo "Backup method: temporary Docker container (${PG_DUMP_IMAGE})"
        docker run --rm \
            --network host \
            --env "PGDATABASE=${PGDATABASE}" \
            "${PG_DUMP_IMAGE}" \
            pg_dump \
            --format=plain \
            --no-owner \
            --no-privileges > "${SQL_FILE}"
    fi
else
    echo "Error: neither pg_dump nor Docker was found." >&2
    exit 1
fi

gzip -9 -- "${SQL_FILE}"
trap - ERR INT TERM

echo "Backup created: ${ARCHIVE_FILE}"
du -h -- "${ARCHIVE_FILE}"

if (( BACKUP_RETAIN_DAYS > 0 )); then
    find "${BACKUP_ROOT}" \
        -mindepth 1 \
        -maxdepth 1 \
        -type d \
        -name '20??????' \
        -mtime "+${BACKUP_RETAIN_DAYS}" \
        -exec rm -rf -- {} +
fi

echo "PostgreSQL backup completed."
