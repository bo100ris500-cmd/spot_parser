#!/bin/bash
# Nightly backup helper for Ubuntu host
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${BACKUP_DIR:-$HOME/backups}"
mkdir -p "$OUT_DIR"
STAMP="$(date +%F)"
docker compose -f "$ROOT/docker-compose.yml" exec -T postgres \
  pg_dump -U "${POSTGRES_USER:-spot}" "${POSTGRES_DB:-spot_parser}" \
  | gzip > "$OUT_DIR/spot_${STAMP}.sql.gz"
echo "Wrote $OUT_DIR/spot_${STAMP}.sql.gz"
