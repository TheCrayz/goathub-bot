#!/usr/bin/env bash
# M-19 (2026-06-12): Tägliches Backup der goathub.db — siehe BACKUP.md.
#
# WICHTIG: niemals `cp goathub.db` auf einer LIVE-DB — bei WAL-Mode wäre die
# Kopie potenziell inkonsistent (WAL-Datei fehlt / mid-checkpoint). sqlite3
# ".backup" nutzt die Online-Backup-API und ist auch bei laufendem Service
# konsistent.
#
# Läuft täglich via goathub-backup.timer als User goathub.
# Env-Overrides: GOATHUB_DB_PATH, GOATHUB_BACKUP_DIR, GOATHUB_BACKUP_RETENTION_DAYS
set -euo pipefail

DB_PATH="${GOATHUB_DB_PATH:-/var/www/goathub-bot/goathub.db}"
BACKUP_DIR="${GOATHUB_BACKUP_DIR:-/var/backups/goathub}"
RETENTION_DAYS="${GOATHUB_BACKUP_RETENTION_DAYS:-14}"

if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "backup_db: sqlite3 nicht installiert (apt install sqlite3)" >&2
    exit 1
fi
if [ ! -f "$DB_PATH" ]; then
    echo "backup_db: DB nicht gefunden: $DB_PATH" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" || true

TS="$(date -u +%Y%m%d-%H%M%S)"
TARGET="$BACKUP_DIR/goathub-$TS.db"

sqlite3 "$DB_PATH" ".backup '$TARGET'"
gzip -9 "$TARGET"
# Enthält User-Emails, bcrypt-Hashes und (verschlüsselte) HL-Agent-Keys —
# nur Owner darf lesen.
chmod 600 "$TARGET.gz"

# Retention: alles älter als RETENTION_DAYS Tage löschen.
find "$BACKUP_DIR" -name 'goathub-*.db.gz' -type f -mtime +"$RETENTION_DAYS" -delete

echo "backup_db: OK → $TARGET.gz ($(du -h "$TARGET.gz" | cut -f1)), Retention ${RETENTION_DAYS}d"
