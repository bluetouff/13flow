#!/usr/bin/env bash
# Private Pro workspace smoke test.
#
# This is intentionally separate from smoke-public.sh because it requires a
# scoped Pro API key and creates/deletes a temporary workspace watchlist.
#
# Usage:
#   PRO_TOKEN=13flow_live_... /opt/13flow/deploy/smoke-pro-workspace.sh
#   SITE=https://staging.13flow.eu PRO_TOKEN=... ./deploy/smoke-pro-workspace.sh
set -uo pipefail

SITE=${SITE:-https://13flow.eu}
PRO_TOKEN=${PRO_TOKEN:-}
EXPECTED_SHA=${EXPECTED_SHA:-}

tmpdir=$(mktemp -d)
cleanup() {
  if [[ -n "${watchlist_id:-}" && -n "${PRO_TOKEN:-}" ]]; then
    curl -fsS --max-time 10 -X DELETE "$SITE/api/pro/v1/workspace/watchlists/$watchlist_id" \
      -H "Authorization: Bearer $PRO_TOKEN" >/dev/null 2>&1 || true
  fi
  rm -rf "$tmpdir"
}
trap cleanup EXIT

fail=0
watchlist_id=""
say(){ printf '%-62s %s\n' "$1" "$2"; }
ok(){ say "$1" "OK"; }
bad(){ say "$1" "FAIL - $2"; fail=1; }

if [[ -z "$PRO_TOKEN" ]]; then
  echo "ERROR: PRO_TOKEN is required; refusing unauthenticated Pro smoke" >&2
  exit 2
fi

curl_pro() {
  local method=$1 path=$2 out=$3 body=${4:-}
  if [[ -n "$body" ]]; then
    curl -fsS --max-time 25 -X "$method" "$SITE$path" \
      -H "Authorization: Bearer $PRO_TOKEN" \
      -H "Content-Type: application/json" \
      -o "$out" \
      --data "$body"
  else
    curl -fsS --max-time 25 -X "$method" "$SITE$path" \
      -H "Authorization: Bearer $PRO_TOKEN" \
      -o "$out"
  fi
}

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

extract_json() {
  local file=$1 expr=$2
  python3 - "$file" "$expr" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(eval(sys.argv[2], {}, {"data": data}))
PY
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

status="$tmpdir/status.json"
if curl_pro GET "/api/pro/v1/status" "$status"; then
  json_check "Pro status valid key" "$status" "
key = data.get('key') or {}
scopes = set(key.get('scopes') or [])
ok = data.get('api') == '13flow-pro' and {'funds:read', 'workspace:write'} <= scopes
msg = str(data)
"
else
  bad "Pro status valid key" "curl failed"
fi

create_body='{"name":"Codex smoke workspace","tickers":["AAPL","MSFT"],"filters":{"action":"alert","min_score":30},"alert_policy":{"enabled":false,"frequency":"manual"},"notes":"temporary smoke test"}'
created="$tmpdir/watchlist-created.json"
if curl_pro POST "/api/pro/v1/workspace/watchlists" "$created" "$create_body"; then
  json_check "workspace watchlist create" "$created" "
w = data.get('watchlist') or {}
ok = bool(w.get('id')) and w.get('name') == 'Codex smoke workspace' and w.get('tickers') == ['AAPL', 'MSFT']
msg = str(data)
"
  watchlist_id=$(extract_json "$created" "data['watchlist']['id']")
else
  bad "workspace watchlist create" "curl failed"
fi

if [[ -n "$watchlist_id" ]]; then
  snapshot="$tmpdir/snapshot.json"
  if curl_pro POST "/api/pro/v1/workspace/watchlists/$watchlist_id/signals/snapshot" "$snapshot"; then
    json_check "workspace signal snapshot" "$snapshot" "
snap = data.get('snapshot') or {}
alerts = data.get('alerts') or {}
delta = data.get('delta') or {}
ok = (
    snap.get('watchlist_id')
    and isinstance(snap.get('tickers'), list)
    and 'signals' not in snap
    and isinstance(alerts.get('candidates'), int)
    and isinstance(delta.get('current_count'), int)
)
msg = str(data)[:1000]
"
  else
    bad "workspace signal snapshot" "curl failed"
  fi

  alerts="$tmpdir/alerts.json"
  if curl_pro GET "/api/pro/v1/workspace/alerts?status=all&limit=10&watchlist_id=$watchlist_id" "$alerts"; then
    json_check "workspace alerts inbox" "$alerts" "
summary = data.get('summary') or {}
alerts = data.get('alerts') or []
ok = 'by_status' in summary and isinstance(alerts, list)
msg = str(data)[:1000]
"
  else
    bad "workspace alerts inbox" "curl failed"
  fi

  activity="$tmpdir/activity.json"
  if curl_pro GET "/api/pro/v1/workspace/activity?entity_type=watchlist&limit=10" "$activity"; then
    json_check "workspace activity feed" "$activity" "
events = data.get('activity') or []
types = {e.get('event_type') for e in events}
ok = 'watchlist.created' in types and 'signals.snapshot' in types
msg = str(data)[:1000]
"
  else
    bad "workspace activity feed" "curl failed"
  fi

  overview="$tmpdir/overview.json"
  if curl_pro GET "/api/pro/v1/workspace/overview" "$overview"; then
    json_check "workspace overview" "$overview" "
summary = data.get('summary') or {}
ok = (
    data.get('meta', {}).get('workspace_scope') == 'api_key'
    and isinstance(data.get('recent_activity'), list)
    and int(summary.get('activity_events') or 0) >= 1
)
msg = str(data)[:1000]
"
  else
    bad "workspace overview" "curl failed"
  fi

  deleted="$tmpdir/deleted.json"
  if curl_pro DELETE "/api/pro/v1/workspace/watchlists/$watchlist_id" "$deleted"; then
    json_check "workspace watchlist delete" "$deleted" "ok = data.get('deleted') is True; msg = str(data)"
    watchlist_id=""
  else
    bad "workspace watchlist delete" "curl failed"
  fi
fi

echo
if [[ $fail -eq 0 ]]; then
  echo "PRO WORKSPACE SMOKE: all good"
else
  echo "PRO WORKSPACE SMOKE: problems found"
  exit 1
fi
