#!/usr/bin/env bash
# Pro operator key lifecycle smoke test.
#
# Creates one temporary bounded pilot key through the CLI, verifies the non-secret
# operator audit event, revokes the key, verifies the revocation event, then checks
# the revoked token fails closed over the Pro API.
#
# This script intentionally does not print the temporary token.
#
# Usage:
#   sudo EXPECTED_SHA=<git-sha> /opt/13flow/deploy/smoke-pro-key-lifecycle.sh
#   SITE=https://staging.13flow.eu PRO_DB=/path/pro.db RUN_PY=/path/run.py ./deploy/smoke-pro-key-lifecycle.sh
set -uo pipefail

SITE=${SITE:-https://13flow.eu}
RUN_PY=${RUN_PY:-/opt/13flow/run.py}
PYTHON=${PYTHON:-/opt/13flow/.venv/bin/python}
PRO_DB=${PRO_DB:-/var/lib/13flow-pro/13flow-pro.db}
EXPECTED_SHA=${EXPECTED_SHA:-}

tmpdir=$(mktemp -d)
created_key_id=""
created_token=""

cleanup() {
  if [[ -n "$created_key_id" ]]; then
    "$PYTHON" "$RUN_PY" --pro-db "$PRO_DB" --revoke-api-key "$created_key_id" >/dev/null 2>&1 || true
  fi
  rm -rf "$tmpdir"
}
trap cleanup EXIT

fail=0
say(){ printf '%-66s %s\n' "$1" "$2"; }
ok(){ say "$1" "OK"; }
bad(){ say "$1" "FAIL - $2"; fail=1; }

json_check() {
  local label=$1 file=$2 code=$3
  python3 - "$file" "$code" <<'PY'
import json, sys
path, code = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
env = {"data": data, "ok": True, "msg": ""}
exec(code, {}, env)
if not env.get("ok"):
    raise SystemExit(env.get("msg") or "json check failed")
PY
  local rc=$?
  [[ $rc -eq 0 ]] && ok "$label" || bad "$label" "invalid JSON contract"
}

version="$tmpdir/version.json"
if curl -fsS --max-time 15 "$SITE/api/version" -o "$version"; then
  json_check "/api/version reachable" "$version" "ok = bool(data.get('git_sha') or data.get('commit')); msg = str(data)"
  if [[ -n "$EXPECTED_SHA" ]]; then
    python3 - "$version" "$EXPECTED_SHA" <<'PY'
import json, sys
data=json.load(open(sys.argv[1]))
got=data.get("commit") or data.get("git_sha")
raise SystemExit(0 if got == sys.argv[2] else f"expected {sys.argv[2]}, got {got}")
PY
    [[ $? -eq 0 ]] && ok "/api/version expected SHA" || bad "/api/version expected SHA" "mismatch"
  fi
else
  bad "/api/version reachable" "curl failed"
fi

label="Codex lifecycle smoke $(date -u +%Y%m%dT%H%M%SZ)"
create_out="$tmpdir/create-key.out"
if "$PYTHON" "$RUN_PY" \
    --pro-db "$PRO_DB" \
    --create-api-key "$label" \
    --api-key-scopes funds:read,quality:read,workspace:write \
    --api-key-rate-per-min 30 \
    --api-key-rate-per-day 100 \
    --api-key-expires-days 1 \
    --api-key-rotation-days 1 >"$create_out"; then
  chmod 600 "$create_out"
  created_key_id=$(awk -F': ' '/^  id: /{print $2; exit}' "$create_out")
  created_token=$(awk '/^13flow_live_/ {print; exit}' "$create_out")
  if [[ "$created_key_id" =~ ^[a-f0-9]{16}$ && "$created_token" == 13flow_live_* ]]; then
    ok "temporary pilot key created"
  else
    bad "temporary pilot key created" "could not parse key id/token"
  fi
else
  bad "temporary pilot key created" "CLI failed"
fi

if [[ -n "$created_token" ]]; then
  status_json="$tmpdir/status.json"
  if curl -fsS --max-time 15 "$SITE/api/pro/v1/status" \
      -H "Authorization: Bearer $created_token" -o "$status_json"; then
    json_check "temporary key authenticates before revoke" "$status_json" "
key = data.get('key') or {}
scopes = set(key.get('scopes') or [])
raw = str(data)
ok = (
    key.get('id') == '$created_key_id'
    and {'funds:read', 'quality:read', 'workspace:write'} <= scopes
    and '13flow_live_' not in raw
)
msg = str(data)[:1000]
"
  else
    bad "temporary key authenticates before revoke" "curl failed"
  fi
fi

events_created="$tmpdir/events-created.out"
if "$PYTHON" "$RUN_PY" --pro-db "$PRO_DB" --list-operator-events --operator-events-limit 20 >"$events_created"; then
  if grep -q "api_key.created" "$events_created" && grep -q "$created_key_id" "$events_created" && ! grep -q "13flow_live_" "$events_created"; then
    ok "operator event api_key.created"
  else
    bad "operator event api_key.created" "missing event or token leaked"
  fi
else
  bad "operator event api_key.created" "CLI failed"
fi

if [[ -n "$created_key_id" ]]; then
  revoke_out="$tmpdir/revoke.out"
  if "$PYTHON" "$RUN_PY" --pro-db "$PRO_DB" --revoke-api-key "$created_key_id" >"$revoke_out"; then
    grep -q "revoked" "$revoke_out" && ok "temporary key revoked" || bad "temporary key revoked" "unexpected CLI output"
  else
    bad "temporary key revoked" "CLI failed"
  fi
fi

events_revoked="$tmpdir/events-revoked.out"
if "$PYTHON" "$RUN_PY" --pro-db "$PRO_DB" --list-operator-events --operator-events-limit 20 >"$events_revoked"; then
  if grep -q "api_key.revoked" "$events_revoked" && grep -q "$created_key_id" "$events_revoked" && ! grep -q "13flow_live_" "$events_revoked"; then
    ok "operator event api_key.revoked"
  else
    bad "operator event api_key.revoked" "missing event or token leaked"
  fi
else
  bad "operator event api_key.revoked" "CLI failed"
fi

if [[ -n "$created_token" ]]; then
  revoked_status="$tmpdir/revoked-status.json"
  code=$(curl -sS --max-time 15 -o "$revoked_status" -w '%{http_code}' \
    "$SITE/api/pro/v1/status" -H "Authorization: Bearer $created_token" || echo 000)
  if [[ "$code" == "401" ]]; then
    json_check "revoked key fails closed" "$revoked_status" "
ok = data.get('error') in {'invalid_api_key', 'revoked_api_key'}
msg = str(data)
"
  else
    bad "revoked key fails closed" "got HTTP $code"
  fi
fi

if [[ $fail -eq 0 ]]; then
  echo
  echo "PRO KEY LIFECYCLE SMOKE: all good"
else
  echo
  echo "PRO KEY LIFECYCLE SMOKE: problems found"
fi
exit "$fail"
