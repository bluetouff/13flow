#!/usr/bin/env bash
# Consistent encrypted backup for the 13FLOW Pro control-plane DB.
#
# The script refuses to emit plaintext backups. Configure either:
#   GPG_RECIPIENT=<fingerprint-or-email>                  # preferred, public-key encryption
# or
#   BACKUP_PASSPHRASE_FILE=/etc/13flow/pro-backup.pass    # fallback, symmetric encryption
set -euo pipefail

PRO_DB=${PRO_DB:-/var/lib/13flow-pro/13flow-pro.db}
BACKUP_DIR=${BACKUP_DIR:-/var/backups/13flow-pro}
BACKUP_WORK_DIR=${BACKUP_WORK_DIR:-/var/lib/13flow-pro-backup/tmp}
RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-30}
GPG_RECIPIENT=${GPG_RECIPIENT:-}
GPG_HOMEDIR=${GPG_HOMEDIR:-}
BACKUP_PASSPHRASE_FILE=${BACKUP_PASSPHRASE_FILE:-}

if [[ ! -r "$PRO_DB" ]]; then
  echo "ERROR: Pro DB is not readable: $PRO_DB" >&2
  exit 1
fi
if [[ -z "$GPG_RECIPIENT" && -z "$BACKUP_PASSPHRASE_FILE" ]]; then
  echo "ERROR: configure GPG_RECIPIENT or BACKUP_PASSPHRASE_FILE; refusing plaintext backup" >&2
  exit 1
fi
if [[ -n "$BACKUP_PASSPHRASE_FILE" && ! -r "$BACKUP_PASSPHRASE_FILE" ]]; then
  echo "ERROR: passphrase file is not readable: $BACKUP_PASSPHRASE_FILE" >&2
  exit 1
fi

umask 077
install -d -m 700 "$BACKUP_DIR"
install -d -m 700 "$BACKUP_WORK_DIR"
tmpdir=$(mktemp -d "$BACKUP_WORK_DIR/backup.XXXXXX")
cleanup() {
  rm -rf "$tmpdir"
}
trap cleanup EXIT

ts=$(date -u +%Y%m%dT%H%M%SZ)
snapshot="$tmpdir/13flow-pro.db"
manifest="$tmpdir/manifest.txt"
archive="$tmpdir/13flow-pro-$ts.tar.gz"
out="$BACKUP_DIR/13flow-pro-$ts.tar.gz.gpg"

sqlite3 "file:$PRO_DB?mode=ro" ".backup '$snapshot'"
chmod 600 "$snapshot"

{
  echo "created_at_utc=$ts"
  echo "source_db=$PRO_DB"
  echo "sqlite_integrity=$(sqlite3 "$snapshot" 'PRAGMA integrity_check;')"
  echo "api_keys_total=$(sqlite3 "$snapshot" 'SELECT COUNT(*) FROM api_keys;')"
  echo "api_keys_active=$(sqlite3 "$snapshot" 'SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL;')"
  echo "audit_rows=$(sqlite3 "$snapshot" 'SELECT COUNT(*) FROM api_audit;')"
  echo "snapshot_sha256=$(sha256sum "$snapshot" | awk '{print $1}')"
} > "$manifest"

tar -C "$tmpdir" -czf "$archive" 13flow-pro.db manifest.txt

gpg_args=(--batch --yes --trust-model always --output "$out")
if [[ -n "$GPG_HOMEDIR" ]]; then
  gpg_args+=(--homedir "$GPG_HOMEDIR")
fi
if [[ -n "$GPG_RECIPIENT" ]]; then
  gpg "${gpg_args[@]}" --recipient "$GPG_RECIPIENT" --encrypt "$archive"
else
  gpg "${gpg_args[@]}" --symmetric --cipher-algo AES256 \
    --pinentry-mode loopback --passphrase-file "$BACKUP_PASSPHRASE_FILE" "$archive"
fi

chmod 600 "$out"
find "$BACKUP_DIR" -type f -name '13flow-pro-*.tar.gz.gpg' -mtime +"$RETENTION_DAYS" -delete
echo "$out"
