#!/usr/bin/env bash
# Prepare an encrypted Pro DB backup for an off-production restore check.
#
# Production should usually hold only the public GPG key. This helper identifies
# an encrypted archive, prints its SHA-256, optionally writes a sidecar checksum,
# and emits the exact restore-host verification commands. It never decrypts.
set -euo pipefail

BACKUP_DIR=${BACKUP_DIR:-/var/backups/13flow-pro}
BACKUP_FILE=${1:-${BACKUP_FILE:-}}
WRITE_SHA256=${WRITE_SHA256:-0}
RESTORE_WORK_DIR=${RESTORE_WORK_DIR:-./13flow-pro-restore-verify}

if [[ -z "$BACKUP_FILE" ]]; then
  BACKUP_FILE=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name '13flow-pro-*.tar.gz.gpg' -print | sort | tail -n 1)
fi
if [[ -z "$BACKUP_FILE" || ! -r "$BACKUP_FILE" ]]; then
  echo "ERROR: encrypted backup is not readable: ${BACKUP_FILE:-<none>}" >&2
  exit 1
fi

backup_name=$(basename "$BACKUP_FILE")
backup_sha=$(sha256sum "$BACKUP_FILE" | awk '{print $1}')
backup_size=$(wc -c < "$BACKUP_FILE" | tr -d '[:space:]')

echo "backup_file=$BACKUP_FILE"
echo "backup_name=$backup_name"
echo "backup_sha256=$backup_sha"
echo "backup_size_bytes=$backup_size"

if [[ "$WRITE_SHA256" == "1" ]]; then
  sidecar="$BACKUP_FILE.sha256"
  printf '%s  %s\n' "$backup_sha" "$backup_name" > "$sidecar"
  chmod 600 "$sidecar"
  echo "sha256_sidecar=$sidecar"
fi

cat <<EOF

# Copy the encrypted archive and checksum to the host that owns the private key.
# Example from the restore host:
scp bluetouff@zen:$BACKUP_FILE .
scp bluetouff@zen:$BACKUP_FILE.sha256 .  # if WRITE_SHA256=1 was used

# On the restore host, from the directory containing $backup_name:
mkdir -p "$RESTORE_WORK_DIR"
sha256sum -c "$backup_name.sha256"
VERIFY_WORK_DIR="$RESTORE_WORK_DIR" /path/to/verify-pro-db-backup.sh "$backup_name"

# Expected final line:
# RESTORE VERIFY OK
EOF
