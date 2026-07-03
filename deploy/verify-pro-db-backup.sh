#!/usr/bin/env bash
# Verify that an encrypted 13FLOW Pro DB backup can be decrypted and restored.
#
# This script never writes over production data. It decrypts the selected backup
# into a private temporary directory, extracts the SQLite snapshot, checks the
# manifest hash, runs PRAGMA integrity_check and verifies the expected Pro tables.
set -euo pipefail

BACKUP_DIR=${BACKUP_DIR:-/var/backups/13flow-pro}
BACKUP_FILE=${1:-${BACKUP_FILE:-}}
VERIFY_WORK_DIR=${VERIFY_WORK_DIR:-/var/lib/13flow-pro-backup/verify}
GPG_HOMEDIR=${GPG_HOMEDIR:-}
BACKUP_PASSPHRASE_FILE=${BACKUP_PASSPHRASE_FILE:-}

if [[ -z "$BACKUP_FILE" ]]; then
  BACKUP_FILE=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name '13flow-pro-*.tar.gz.gpg' -print | sort | tail -n 1)
fi
if [[ -z "$BACKUP_FILE" || ! -r "$BACKUP_FILE" ]]; then
  echo "ERROR: encrypted backup is not readable: ${BACKUP_FILE:-<none>}" >&2
  exit 1
fi
if [[ -n "$BACKUP_PASSPHRASE_FILE" && ! -r "$BACKUP_PASSPHRASE_FILE" ]]; then
  echo "ERROR: passphrase file is not readable: $BACKUP_PASSPHRASE_FILE" >&2
  exit 1
fi

umask 077
install -d -m 700 "$VERIFY_WORK_DIR"
tmpdir=$(mktemp -d "$VERIFY_WORK_DIR/restore.XXXXXX")
cleanup() {
  rm -rf "$tmpdir"
}
trap cleanup EXIT

archive="$tmpdir/backup.tar.gz"
extract_dir="$tmpdir/extract"
install -d -m 700 "$extract_dir"

gpg_args=(--batch --yes --output "$archive")
if [[ -n "$GPG_HOMEDIR" ]]; then
  gpg_args+=(--homedir "$GPG_HOMEDIR")
fi
if [[ -n "$BACKUP_PASSPHRASE_FILE" ]]; then
  gpg_args+=(--pinentry-mode loopback --passphrase-file "$BACKUP_PASSPHRASE_FILE" --no-symkey-cache)
fi
gpg "${gpg_args[@]}" --decrypt "$BACKUP_FILE"

while IFS= read -r member; do
  case "$member" in
    13flow-pro.db|manifest.txt) ;;
    *)
      echo "ERROR: unexpected archive member: $member" >&2
      exit 1
      ;;
  esac
done < <(tar -tzf "$archive")

tar -C "$extract_dir" -xzf "$archive" 13flow-pro.db manifest.txt
snapshot="$extract_dir/13flow-pro.db"
manifest="$extract_dir/manifest.txt"

if [[ ! -s "$snapshot" || ! -s "$manifest" ]]; then
  echo "ERROR: restored snapshot or manifest is missing" >&2
  exit 1
fi

expected_sha=$(awk -F= '$1 == "snapshot_sha256" {print $2}' "$manifest")
actual_sha=$(sha256sum "$snapshot" | awk '{print $1}')
if [[ -z "$expected_sha" || "$expected_sha" != "$actual_sha" ]]; then
  echo "ERROR: snapshot SHA-256 mismatch" >&2
  exit 1
fi

integrity=$(sqlite3 "$snapshot" "PRAGMA integrity_check;")
if [[ "$integrity" != "ok" ]]; then
  echo "ERROR: SQLite integrity check failed: $integrity" >&2
  exit 1
fi

required_tables=(
  api_keys
  api_key_usage
  api_audit
  saved_watchlists
  saved_watchlist_signal_snapshots
  saved_workspace_alerts
  saved_workspace_activity
)
for table in "${required_tables[@]}"; do
  exists=$(sqlite3 "$snapshot" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='$table';")
  if [[ "$exists" != "1" ]]; then
    echo "ERROR: required table missing from restored snapshot: $table" >&2
    exit 1
  fi
done

table_count() {
  local table=$1
  sqlite3 "$snapshot" "SELECT COUNT(*) FROM $table;"
}

echo "backup_file=$BACKUP_FILE"
echo "sqlite_integrity=ok"
echo "snapshot_sha256=$actual_sha"
echo "api_keys_total=$(table_count api_keys)"
echo "api_keys_active=$(sqlite3 "$snapshot" 'SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL;')"
echo "audit_rows=$(table_count api_audit)"
echo "saved_watchlists=$(table_count saved_watchlists)"
echo "signal_snapshots=$(table_count saved_watchlist_signal_snapshots)"
echo "workspace_alerts=$(table_count saved_workspace_alerts)"
echo "workspace_activity=$(table_count saved_workspace_activity)"
echo "RESTORE VERIFY OK"
