#!/usr/bin/env bash
# Full backfill for 13FLOW: ingest every fund in the registry, publish the DB for read-only
# serving, fix permissions, restart the web service, and verify. Idempotent — re-running only
# fetches quarters not already stored, so it's safe to run as often as you like.
#
#   sudo /opt/13flow/deploy/backfill.sh             # full history (slow without an OpenFIGI key)
#   sudo /opt/13flow/deploy/backfill.sh 8            # only the last 8 quarters (fast first pass)
#   sudo FORCE=1 /opt/13flow/deploy/backfill.sh 16   # re-fetch/replace stored filings
#
# Reads SEC_UA / OPENFIGI_APIKEY / SMARTMONEY_CACHE_DIR from the env file, so put your OpenFIGI
# key there (OPENFIGI_APIKEY="...") to make enrichment ~250x faster.
set -euo pipefail

# ---- config (override via environment if your paths differ) ----
APP_DIR=${APP_DIR:-/opt/13flow}
ENV_FILE=${ENV_FILE:-/etc/13flow/13flow.env}
DB=${DB:-/var/lib/13flow/13flow.db}
VENV_PY=${VENV_PY:-$APP_DIR/.venv/bin/python}
RUN=${RUN:-$APP_DIR/run.py}
SERVICE=${SERVICE:-13flow}
INGEST_USER=${INGEST_USER:-flowingest}
WEB_USER=${WEB_USER:-flowapp}
MAXQ=${1:-}                       # optional: cap the number of quarters per fund
FORCE=${FORCE:-0}                 # FORCE=1 re-fetches accessions already present

DATA_DIR=$(dirname "$DB")

if [[ $EUID -ne 0 ]]; then
  echo "Run me with sudo (I chown/chmod the DB and restart the service)." >&2
  exit 1
fi
[[ -f "$ENV_FILE" ]] || { echo "Env file not found: $ENV_FILE" >&2; exit 1; }
[[ -x "$VENV_PY" ]]  || { echo "Python venv not found: $VENV_PY" >&2; exit 1; }

quarters_arg=""
[[ -n "$MAXQ" ]] && quarters_arg="--max-quarters $MAXQ"
force_arg=""
[[ "$FORCE" == "1" || "$FORCE" == "true" || "$FORCE" == "yes" ]] && force_arg="--force"

echo "==> [1/4] Ingesting all funds${MAXQ:+ (last $MAXQ quarters)}${force_arg:+, forced} as $INGEST_USER ..."
# Run from a writable dir so the resolver cache lands in a writable place; load SEC_UA / key.
sudo -u "$INGEST_USER" bash -c '
  cd "'"$DATA_DIR"'"
  set -a; . "'"$ENV_FILE"'"; set +a
  : "${SMARTMONEY_CACHE_DIR:='"$DATA_DIR"'}"; export SMARTMONEY_CACHE_DIR
  "'"$VENV_PY"'" "'"$RUN"'" --db "'"$DB"'" --sync-all --enrich '"$quarters_arg"' '"$force_arg"'
'

echo "==> [2/4] Publishing DB (checkpoint WAL, switch to rollback journal) ..."
sudo -u "$INGEST_USER" "$VENV_PY" - "$DB" <<'PY'
import sqlite3, sys
db = sys.argv[1]
c = sqlite3.connect(db)
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c.execute("PRAGMA journal_mode=DELETE")   # no -wal/-shm -> clean mode=ro open by the web user
c.close()
print("   published:", db)
PY

echo "==> [3/4] Fixing ownership/permissions ..."
chown "$INGEST_USER:$WEB_USER" "$DB"
chmod 640 "$DB"
# keep the data dir setgid so future files inherit the web group
chown "$INGEST_USER:$WEB_USER" "$DATA_DIR"
chmod 2750 "$DATA_DIR"

echo "==> [4/4] Restarting $SERVICE and verifying ..."
systemctl restart "$SERVICE"
sleep 1
n=$(curl -s localhost:8000/api/funds | python3 -c "import sys,json;print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
if [[ "$n" -gt 0 ]]; then
  echo "    OK — $n funds served locally."
  echo "    Run 'sudo $APP_DIR/deploy/preflight.sh' to validate the public site."
else
  echo "    WARNING — /api/funds returned 0/err. Check: journalctl -u $SERVICE -n 30 --no-pager" >&2
  exit 1
fi
