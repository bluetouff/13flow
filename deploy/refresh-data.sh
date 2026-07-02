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
export MAXQ=${MAXQ:-8}   # borne l'historique du refresh nocturne (jamais 52 trimestres)
export FORCE=${FORCE:-0} # FORCE=1 re-fetches/replaces stored filings after data fixes
force_arg=""
[[ "$FORCE" == "1" || "$FORCE" == "true" || "$FORCE" == "yes" ]] && force_arg="--force"

cd "$APP_DIR"

# Pull every tracked superinvestor; --enrich resolves CUSIP -> ticker via OpenFIGI.
# Use --max-quarters to bound history, or --sync "Fund Name" for a single fund.
"$VENV/bin/python" run.py --db "$DB" --sync-all --enrich ${MAXQ:+--max-quarters $MAXQ} $force_arg

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

# Precompute the Confluence screen (13F accumulation x live Form 4) into cache JSON so the
# public tier serves it instantly and never hits EDGAR per request. Non-fatal on failure.
if timeout 3600 "$VENV/bin/python" run.py --db "$DB" --confluence; then
  chmod 640 "$SMARTMONEY_CACHE_DIR"/confluence-*.json 2>/dev/null || true
else
  echo "  (confluence precompute skipped/failed — screen falls back to live/sample)"
fi
echo "refresh complete: $DB"
