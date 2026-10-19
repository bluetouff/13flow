#!/usr/bin/env bash
# Refresh the 13FLOW database from SEC EDGAR, then make it a self-contained snapshot
# the read-only web workers can serve. Run as the INGEST user (write access to the DB),
# NOT as the web user. Wire to a systemd timer or cron (see install guide).
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/13flow}
DB=${SMARTMONEY_DB:-/var/lib/13flow/13flow.db}
VENV=${VENV:-$APP_DIR/.venv}
# EDGAR requires a User-Agent with a contact email, or it returns 403.
export SEC_UA=${SEC_UA:-"13FLOW/1.0 you@example.com"}
# Caches go next to the DB (writable by the ingest user), never in the read-only install dir.
export SMARTMONEY_CACHE_DIR=${SMARTMONEY_CACHE_DIR:-$(dirname "$DB")}
export MAXQ=${MAXQ:-2}   # daily refresh: newest filings only, keep EDGAR volume low
export FORCE=${FORCE:-0} # FORCE=1 re-fetches/replaces stored filings after data fixes
export REPORT_DATE=${REPORT_DATE:-}
export SMARTMONEY_EDGAR_RATE_PER_SEC=${SMARTMONEY_EDGAR_RATE_PER_SEC:-1.0}
export SMARTMONEY_SYNC_SLEEP_SEC=${SMARTMONEY_SYNC_SLEEP_SEC:-30}
export SKIP_CONFLUENCE=${SKIP_CONFLUENCE:-0}
force_arg=""
[[ "$FORCE" == "1" || "$FORCE" == "true" || "$FORCE" == "yes" ]] && force_arg="--force"
report_arg=""
[[ -n "$REPORT_DATE" ]] && report_arg="--report-date $REPORT_DATE"

cd "$APP_DIR"

# Serialize SEC collectors. A Confluence slice yields within 50 minutes; allow
# it to finish before the daily 13F job, without running both request streams.
ingest_lock="$(dirname "$DB")/edgar-ingest.lock"
[[ ! -L "$ingest_lock" ]] || exit 3
exec 9>>"$ingest_lock"
flock --wait 3300 9

# Pull every tracked superinvestor; --enrich resolves CUSIP -> ticker via OpenFIGI.
# Use --max-quarters to bound history, or --sync "Fund Name" for a single fund.
"$VENV/bin/python" run.py --db "$DB" --sync-all --enrich ${MAXQ:+--max-quarters $MAXQ} \
  $force_arg $report_arg

# Collapse the WAL into the main file so a read-only (mode=ro) open needs no -wal/-shm.
"$VENV/bin/python" - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c.execute("PRAGMA journal_mode=DELETE")   # rollback journal -> clean mode=ro open by the web user
c.close()
PY

# Let the web group read the fresh snapshot (group = flowapp); owner stays the ingest user.
chmod 640 "$DB" 2>/dev/null || true
flock --unlock 9

# Precompute the Confluence screen (13F accumulation x live Form 4) into cache JSON so the
# public tier serves it instantly and never hits EDGAR per request.
if [[ "$SKIP_CONFLUENCE" == "1" || "$SKIP_CONFLUENCE" == "true" || "$SKIP_CONFLUENCE" == "yes" ]]; then
  echo "  (inline confluence skipped; handled by 13flow-confluence.service when installed)"
else
  if "$VENV/bin/python" run.py --db "$DB" --confluence --confluence-budget-seconds 3000; then
    chmod 640 "$SMARTMONEY_CACHE_DIR"/confluence-*.json 2>/dev/null || true
  else
    status=$?
    if [[ "$status" == "75" ]]; then
      echo "  (confluence work slice saved; resume with the Confluence service)"
      exit 75
    fi
    echo "  (confluence precompute failed; previous caches preserved)" >&2
    exit 1
  fi
fi
echo "refresh complete: $DB"
