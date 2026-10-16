#!/bin/sh
# Runs inside the `backup` service (see docker-compose.yml) every hour.
# Uses SQLite's own .backup command (safe on a live, in-use database -- unlike
# a plain file copy) and encrypts the result so a copied backup file alone is
# useless without the private key that never leaves Fabian's Mac
# (~/.mtg-portal/backup-key.txt -- see ../deploy/ACCOUNT-SETUP.md).
set -eu

DB_PATH="/data/portal.db"
OUT_DIR="/backups"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
PLAIN="$OUT_DIR/portal-$TIMESTAMP.db"
ENCRYPTED="$PLAIN.age"

if [ ! -f "$DB_PATH" ]; then
	echo "no database at $DB_PATH yet, skipping backup"
	exit 0
fi

if [ -z "${AGE_RECIPIENT:-}" ]; then
	echo "AGE_RECIPIENT is not set -- refusing to write an unencrypted backup" >&2
	exit 1
fi

mkdir -p "$OUT_DIR"
sqlite3 "$DB_PATH" ".backup '$PLAIN'"
age -r "$AGE_RECIPIENT" -o "$ENCRYPTED" "$PLAIN"
rm -f "$PLAIN"

echo "backup written: $ENCRYPTED"

# Keep 14 days locally in the container volume. This is a convenience copy,
# not the durable one -- deploy/SETUP.md's closing-a-round runbook has Fabian
# scp the latest encrypted backup off the server before it is destroyed.
find "$OUT_DIR" -name 'portal-*.db.age' -mtime +14 -delete
