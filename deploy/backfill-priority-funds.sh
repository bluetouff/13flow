#!/usr/bin/env bash
# Cautious one-shot backfill for the Pro-priority fund additions.
#
# Defaults are intentionally conservative: only the new quant/multistrat names,
# last 8 quarters, no FORCE, explicit sleeps, and no Confluence fan-out.
# Usage:
#   sudo /opt/13flow/deploy/backfill-priority-funds.sh
#   sudo MAXQ=4 SMARTMONEY_SYNC_SLEEP_SEC=60 /opt/13flow/deploy/backfill-priority-funds.sh
#   sudo FUND_LABELS="Renaissance Tech,Point72" /opt/13flow/deploy/backfill-priority-funds.sh
#   sudo FUND_LABELS="D. E. Shaw" REPORT_DATE=2024-03-31 FORCE=1 /opt/13flow/deploy/backfill-priority-funds.sh
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/13flow}
ENV_FILE=${ENV_FILE:-/etc/13flow/13flow.env}
DB=${DB:-/var/lib/13flow/13flow.db}
VENV_PY=${VENV_PY:-$APP_DIR/.venv/bin/python}
RUN=${RUN:-$APP_DIR/run.py}
SERVICE=${SERVICE:-13flow}
INGEST_USER=${INGEST_USER:-flowingest}
WEB_USER=${WEB_USER:-flowapp}
MAXQ=${MAXQ:-8}
FORCE=${FORCE:-0}
REPORT_DATE=${REPORT_DATE:-}
MIN_FUNDS=${MIN_FUNDS:-45}
SMARTMONEY_EDGAR_RATE_PER_SEC=${SMARTMONEY_EDGAR_RATE_PER_SEC:-0.5}
SMARTMONEY_SYNC_SLEEP_SEC=${SMARTMONEY_SYNC_SLEEP_SEC:-45}
FUND_LABELS=${FUND_LABELS:-"Renaissance Tech,Citadel Advisors,Millennium,AQR Capital,Two Sigma,D. E. Shaw,Point72,Farallon"}

DATA_DIR=$(dirname "$DB")

if [[ $EUID -ne 0 ]]; then
  echo "Run me with sudo (I chown/chmod the DB and restart the service)." >&2
  exit 1
fi
[[ -f "$ENV_FILE" ]] || { echo "Env file not found: $ENV_FILE" >&2; exit 1; }
[[ -x "$VENV_PY" ]]  || { echo "Python venv not found: $VENV_PY" >&2; exit 1; }

echo "==> [1/4] Backing up SQLite snapshot before priority backfill ..."
backup="$DATA_DIR/13flow-before-priority-backfill-$(date -u +%Y%m%dT%H%M%SZ).db"
install -o "$INGEST_USER" -g "$WEB_USER" -m 640 "$DB" "$backup"
echo "    backup: $backup"

echo "==> [2/4] Ingesting priority funds only as $INGEST_USER ..."
sudo -u "$INGEST_USER" bash -c '
  set -euo pipefail
  cd "'"$DATA_DIR"'"
  set -a; . "'"$ENV_FILE"'"; set +a
  : "${SMARTMONEY_CACHE_DIR:='"$DATA_DIR"'}"; export SMARTMONEY_CACHE_DIR
  export SMARTMONEY_EDGAR_RATE_PER_SEC="'"$SMARTMONEY_EDGAR_RATE_PER_SEC"'"
  force_arg=()
  if [[ "'"$FORCE"'" == "1" || "'"$FORCE"'" == "true" || "'"$FORCE"'" == "yes" ]]; then
    force_arg=(--force)
  fi
  report_date_arg=()
  if [[ -n "'"$REPORT_DATE"'" ]]; then
    report_date_arg=(--report-date "'"$REPORT_DATE"'")
  fi
  IFS="," read -r -a labels <<< "'"$FUND_LABELS"'"
  for raw in "${labels[@]}"; do
    label=$(printf "%s" "$raw" | sed "s/^ *//;s/ *$//")
    [[ -n "$label" ]] || continue
    echo "    sync: $label"
    "'"$VENV_PY"'" "'"$RUN"'" --db "'"$DB"'" --sync "$label" --enrich --max-quarters "'"$MAXQ"'" "${force_arg[@]}" "${report_date_arg[@]}"
    sleep "'"$SMARTMONEY_SYNC_SLEEP_SEC"'"
  done
'

echo "==> [3/4] Publishing DB and fixing permissions ..."
sudo -u "$INGEST_USER" "$VENV_PY" - "$DB" <<'PY'
import sqlite3, sys
db = sys.argv[1]
c = sqlite3.connect(db)
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c.execute("PRAGMA journal_mode=DELETE")
c.close()
print("   published:", db)
PY
chown "$INGEST_USER:$WEB_USER" "$DB"
chmod 640 "$DB"
chown "$INGEST_USER:$WEB_USER" "$DATA_DIR"
chmod 2750 "$DATA_DIR"

echo "==> [4/4] Restarting $SERVICE and verifying local contracts ..."
systemctl restart "$SERVICE"
sleep 1
curl -fsS http://127.0.0.1:8000/api/live-status | "$VENV_PY" -m json.tool >/tmp/13flow-live-status.json
curl -fsS http://127.0.0.1:8000/api/data-quality | "$VENV_PY" -m json.tool >/tmp/13flow-data-quality.json
"$VENV_PY" - <<'PY'
import json
status = json.load(open("/tmp/13flow-live-status.json"))
quality = json.load(open("/tmp/13flow-data-quality.json"))
print("    funds:", status["counts"]["funds"])
print("    latest_13f_quarter:", status.get("latest_13f_quarter"))
print("    quality:", quality["summary"])
min_funds = int("'"$MIN_FUNDS"'")
if status["counts"]["funds"] < min_funds:
    raise SystemExit(f"expected at least {min_funds} public funds after priority backfill")
if status.get("uses_synthetic_data"):
    raise SystemExit("synthetic data unexpectedly enabled")
PY
echo "    OK — priority backfill completed. Run deploy/smoke-public.sh after public deploy."
