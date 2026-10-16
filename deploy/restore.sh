#!/bin/sh
# Decrypts an encrypted backup and loads it into the running `web` service's
# docker volume. Run this from the server, after `docker compose up -d` has
# created the `portal_data` volume but BEFORE traffic starts hitting it (or
# stop the `web` service first to avoid writes racing the restore).
#
# Usage: ./restore.sh /path/to/portal-YYYYMMDD-HHMMSS.db.age
#
# Needs age's PRIVATE key, which lives on Fabian's Mac
# (~/.mtg-portal/backup-key.txt), not on the server -- decrypt locally and
# copy the plaintext .db across, or scp the private key over temporarily and
# remove it afterward. See ../deploy/SETUP.md.
set -eu

if [ $# -ne 1 ]; then
	echo "usage: $0 <encrypted-backup.db.age>" >&2
	exit 1
fi

ENCRYPTED="$1"
PLAIN="$(mktemp)"

trap 'rm -f "$PLAIN"' EXIT

if [ -z "${AGE_IDENTITY_FILE:-}" ]; then
	echo "Set AGE_IDENTITY_FILE to the path of your age private key" \
		"(e.g. ~/.mtg-portal/backup-key.txt) before running this script." >&2
	exit 1
fi

age -d -i "$AGE_IDENTITY_FILE" -o "$PLAIN" "$ENCRYPTED"

echo "Decrypted OK. Verifying integrity before touching the live volume..."
sqlite3 "$PLAIN" "PRAGMA integrity_check;" | grep -q '^ok$' || {
	echo "integrity check FAILED -- refusing to restore a possibly corrupt backup" >&2
	exit 1
}

VOLUME_MOUNT="$(docker volume inspect deploy_portal_data --format '{{ .Mountpoint }}' 2>/dev/null || true)"
if [ -z "$VOLUME_MOUNT" ]; then
	echo "Could not find the portal_data volume. Run 'docker compose up -d' once first" \
		"(it can fail to start without a database; that's fine, the volume still gets created)." >&2
	exit 1
fi

cp "$PLAIN" "$VOLUME_MOUNT/portal.db"
echo "Restored into $VOLUME_MOUNT/portal.db"
echo "Now run: docker compose up -d  (or 'restart web' if it was already running)"
