#!/usr/bin/env bash
# Daily cyclical backup — ONE generation, overwritten every run (user's choice).
# Covers the two things git can't: the Postgres DB (pg_dump custom format) and
# the uploads tree (property deeds/plans, art images, bank statements).
# Each artifact is written to a .tmp and atomically renamed, so a failed run
# never destroys the previous good backup.
#
# Installed in SAdmin's crontab at 21:30 UTC (3:00 AM IST) daily:
#   30 21 * * * /var/www/mis-portal/workers/backup_daily.sh >> /home/SAdmin/backups/mis-portal/backup.log 2>&1
# (log lives in the backup dir — /var/log needs root to create new files)
set -euo pipefail

ENV_FILE=/var/www/mis-portal/.env
BACKUP_DIR=/home/SAdmin/backups/mis-portal
UPLOADS_ROOT=/var/www/uploads

envval() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

DB_HOST=$(envval DB_HOST); DB_HOST=${DB_HOST:-localhost}
DB_NAME=$(envval DB_NAME); DB_NAME=${DB_NAME:-mis_portal}
DB_USER=$(envval DB_USER); DB_USER=${DB_USER:-postgres}
export PGPASSWORD="$(envval DB_PASSWORD)"

mkdir -p "$BACKUP_DIR"
echo "[$(date -u '+%F %T')] backup starting"

pg_dump -h "$DB_HOST" -U "$DB_USER" -Fc "$DB_NAME" > "$BACKUP_DIR/db.dump.tmp"
mv -f "$BACKUP_DIR/db.dump.tmp" "$BACKUP_DIR/db.dump"

tar -czf "$BACKUP_DIR/uploads.tar.gz.tmp" -C "$(dirname "$UPLOADS_ROOT")" "$(basename "$UPLOADS_ROOT")"
mv -f "$BACKUP_DIR/uploads.tar.gz.tmp" "$BACKUP_DIR/uploads.tar.gz"

date -u '+%F %T UTC' > "$BACKUP_DIR/last-success.txt"
echo "[$(date -u '+%F %T')] backup done: $(du -sh "$BACKUP_DIR/db.dump" "$BACKUP_DIR/uploads.tar.gz" | tr '\n' ' ')"
