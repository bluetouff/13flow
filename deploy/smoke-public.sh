#!/usr/bin/env bash
# Public production/staging smoke test for 13FLOW.
#
# This is the crawler-visible truth gate. It does not call EDGAR and does not
# need secrets. It fails if production falls back to demo/sample UI, exposes
# auth/checkout copy in the open build, breaks live metadata, or loses MCP.
#
# Usage:
#   ./deploy/smoke-public.sh
#   SITE=https://staging.13flow.eu ./deploy/smoke-public.sh
#   EXPECTED_SHA=<git-sha> ./deploy/smoke-public.sh
set -uo pipefail

SITE=${SITE:-https://13flow.eu}
HTTP_SITE=${HTTP_SITE:-http://13flow.eu}
EXPECTED_SHA=${EXPECTED_SHA:-}
REQUIRE_MCP=${REQUIRE_MCP:-1}

tmpdir=$(mktemp -d)
cleanup() { rm -rf "$tmpdir"; }
trap cleanup EXIT

fail=0
say(){ printf '%-58s %s\n' "$1" "$2"; }
ok(){ say "$1" "OK"; }
bad(){ say "$1" "FAIL - $2"; fail=1; }

fetch() {
  local path=$1 out=$2
  curl -fsS --max-time 20 "$SITE$path" -o "$out"
}

redirects_to() {
  local label=$1 path=$2 target=$3
  local headers code location
  headers=$(curl -sS -D - -o /dev/null --max-time 15 "$SITE$path" || true)
  code=$(printf '%s\n' "$headers" | awk 'NR==1{print $2}')
  location=$(printf '%s\n' "$headers" | awk 'tolower($1)=="location:"{print $2; exit}' | tr -d '\r')
  if [[ "$code" =~ ^30 && "$location" == "$target" ]]; then
    ok "$label"
  else
    bad "$label" "got code=${code:-none} location=${location:-none}, expected 30x -> $target"
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

contains_none() {
  local label=$1 file=$2
  shift 2
  local hit=""
  for needle in "$@"; do
    if grep -Fq "$needle" "$file"; then
      hit=$needle
      break
    fi
  done
  [[ -z "$hit" ]] && ok "$label" || bad "$label" "forbidden text found: $hit"
}

code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$HTTP_SITE/" || echo 000)
[[ "$code" =~ ^30 ]] && ok "http redirects to https ($code)" || bad "http redirects to https" "got $code"

root="$tmpdir/root.html"
if fetch "/" "$root"; then
  ok "root html fetch"
  contains_none "root has no demo/auth/checkout copy" "$root" \
    "SAMPLE DATA" "Sign in" "Upgrade to Pro" "Continue to checkout" "€12" 'data-view="alerts"'
  grep -Fq "Live data status: LIVE EDGAR." "$root" \
    && ok "root crawler live proof" \
    || bad "root crawler live proof" "missing LIVE EDGAR marker"
else
  bad "root html fetch" "curl failed"
fi

legacy_forbidden=(
  "SAMPLE DATA"
  "Sign in"
  "Upgrade to Pro"
  "Continue to checkout"
  "€12"
  "fonts.googleapis.com"
  "fonts.gstatic.com"
  "aucun compte"
  "aucun cookie"
  "pas de compte"
  "pas de cookie"
  "dur à truquer"
  "signal rare"
)

for path in /faq /legal; do
  out="$tmpdir/${path//\//_}.html"
  if fetch "$path" "$out"; then
    ok "$path fetch"
    contains_none "$path has no legacy/legal contradiction" "$out" "${legacy_forbidden[@]}"
  else
    bad "$path fetch" "curl failed"
  fi
done

redirects_to "legacy /dashboard.html redirects app" "/dashboard.html" "/app"
redirects_to "canonical /confluence opens app Confluence" "/confluence" "/app#confluence"
redirects_to "legacy /faq.html redirects canonical" "/faq.html" "/faq"
redirects_to "legacy /mentions-legales redirects canonical" "/mentions-legales" "/legal"
redirects_to "legacy /mentions-legales.html redirects canonical" "/mentions-legales.html" "/legal"

status_page="$tmpdir/status.html"
if fetch "/status" "$status_page"; then
  grep -q "Evidence status" "$status_page" \
    && grep -q "/api/live-status" "$status_page" \
    && grep -q "/api/product-status" "$status_page" \
    && grep -q "Publishable as full validation" "$status_page" \
    && ok "/status evidence page" \
    || bad "/status evidence page" "missing public evidence copy"
  contains_none "/status has no legacy/auth/checkout copy" "$status_page" "${legacy_forbidden[@]}"
else
  bad "/status evidence page" "curl failed"
fi

validation_page="$tmpdir/validation.html"
if fetch "/validation" "$validation_page"; then
  grep -q "Current Confluence evidence pack" "$validation_page" \
    && grep -q "Mechanical Evidence" "$validation_page" \
    && grep -q "Descriptive Metrics" "$validation_page" \
    && grep -q "Public validation claim" "$validation_page" \
    && grep -q "It does not prove validated alpha" "$validation_page" \
    && ok "/validation evidence page" \
    || bad "/validation evidence page" "missing validation boundary copy"
  contains_none "/validation has no legacy/auth/checkout copy" "$validation_page" "${legacy_forbidden[@]}"
else
  bad "/validation evidence page" "curl failed"
fi

for path in /methodology /methodology/app /methodology/mcp; do
  out="$tmpdir/${path//\//_}.html"
  if fetch "$path" "$out"; then
    ok "$path fetch"
    contains_none "$path has no legacy/auth/checkout copy" "$out" "${legacy_forbidden[@]}"
  else
    bad "$path fetch" "curl failed"
  fi
done

developers_page="$tmpdir/developers.html"
if fetch "/developers" "$developers_page"; then
  grep -q "MCP tools/list" "$developers_page" \
    && grep -q "/api/openapi.json" "$developers_page" \
    && grep -q "/api/pro/v1/openapi.json" "$developers_page" \
    && grep -q "Pro tools are intentionally visible" "$developers_page" \
    && ok "/developers page" \
    || bad "/developers page" "missing developer contract copy"
  contains_none "/developers has no legacy/auth/checkout copy" "$developers_page" "${legacy_forbidden[@]}"
else
  bad "/developers page" "curl failed"
fi

pro_terms_page="$tmpdir/legal-pro-api.html"
if fetch "/legal/pro-api" "$pro_terms_page"; then
  grep -q "Pro API, MCP and x402 terms" "$pro_terms_page" \
    && grep -q "Self-serve checkout is disabled" "$pro_terms_page" \
    && grep -q "No public package pricing" "$pro_terms_page" \
    && grep -q "Access can be declined" "$pro_terms_page" \
    && grep -q "No resale, redistribution" "$pro_terms_page" \
    && ok "/legal/pro-api terms" \
    || bad "/legal/pro-api terms" "missing Pro API terms copy"
  contains_none "/legal/pro-api has no legacy/auth/checkout copy" "$pro_terms_page" "${legacy_forbidden[@]}"
else
  bad "/legal/pro-api terms" "curl failed"
fi

pro_page="$tmpdir/pro.html"
if fetch "/pro" "$pro_page"; then
  grep -q "13FLOW Pro API" "$pro_page" \
    && grep -q "/api/pro-offer" "$pro_page" \
    && grep -q "Request access" "$pro_page" \
    && grep -q "Technical pilot review" "$pro_page" \
    && grep -q "Operator lead kit" "$pro_page" \
    && grep -q "not publicly quoted" "$pro_page" \
    && ! grep -q "490 EUR / month" "$pro_page" \
    && ok "/pro offer page" \
    || bad "/pro offer page" "missing Pro API packaging copy"
else
  bad "/pro offer page" "curl failed"
fi

version="$tmpdir/version.json"
if fetch "/api/version" "$version"; then
  json_check "/api/version contract" "$version" "
commit = data.get('commit') or data.get('git_sha')
ok = data.get('open') is True and data.get('demo') is False and data.get('public_state') == 'LIVE' and bool(commit)
msg = str(data)
"
  if [[ -n "$EXPECTED_SHA" ]]; then
    python3 - "$version" "$EXPECTED_SHA" <<'PY'
import json, sys
data=json.load(open(sys.argv[1]))
sha=sys.argv[2]
got=data.get("commit") or data.get("git_sha")
raise SystemExit(0 if got == sha else f"expected {sha}, got {got}")
PY
    [[ $? -eq 0 ]] && ok "/api/version expected SHA" || bad "/api/version expected SHA" "mismatch"
  fi
else
  bad "/api/version fetch" "curl failed"
fi

live="$tmpdir/live-status.json"
if fetch "/api/live-status" "$live"; then
  json_check "/api/live-status LIVE contract" "$live" "
counts = data.get('counts') or {}
accessions = data.get('accessions') or {}
period = data.get('period_13f') or {}
quality = data.get('quality_summary') or {}
ok = (
    data.get('public_state') == 'LIVE'
    and data.get('uses_synthetic_data') is False
    and bool(data.get('generated_at'))
    and bool(data.get('data_as_of'))
    and bool(period.get('to'))
    and int(counts.get('funds') or 0) > 0
    and int(counts.get('latest_filings') or 0) > 0
    and int(accessions.get('latest_count') or 0) > 0
    and 'unit_scale_candidates' in quality
)
msg = str(data)
"
else
  bad "/api/live-status fetch" "curl failed"
fi

config="$tmpdir/config.json"
if fetch "/api/config" "$config"; then
  json_check "/api/config open build" "$config" "
features = data.get('features') or {}
ok = data.get('open') is True and not features.get('auth') and not features.get('billing') and not features.get('alerts')
msg = str(data)
"
else
  bad "/api/config fetch" "curl failed"
fi

product="$tmpdir/product-status.json"
if fetch "/api/product-status" "$product"; then
  json_check "/api/product-status GTM boundary" "$product" "
validation = data.get('validation') or {}
artifact = validation.get('current_artifact') or {}
metrics = validation.get('metrics_snapshot') or {}
offer = data.get('offer_boundary') or {}
readiness = data.get('commercial_readiness') or {}
ok = (
    data.get('public_state') == 'LIVE'
    and validation.get('status') == 'mechanical_evidence_ready_for_review_metrics_unreviewed'
    and readiness.get('public_api') == 'live_read_only'
    and readiness.get('mcp') == 'available_read_only'
    and readiness.get('x402') == 'not_enabled'
    and artifact.get('evidence_review_status') == 'mechanical_evidence_ready_for_review'
    and artifact.get('row_error_count') == 0
    and artifact.get('public_validation_claim') is False
    and artifact.get('publishable_as_full_validation') is False
    and metrics.get('n') == 113
    and metrics.get('rank_ic') == -0.003655
    and 'validated alpha' in (offer.get('do_not_claim_yet') or [])
)
msg = str(data)
"
else
  bad "/api/product-status fetch" "curl failed"
fi

offer="$tmpdir/pro-offer.json"
if fetch "/api/pro-offer" "$offer"; then
  json_check "/api/pro-offer packaging" "$offer" "
offer = data.get('offer') or {}
limits = data.get('default_limits') or {}
not_yet = data.get('not_included_yet') or []
commands = data.get('operator_commands') or {}
truth = data.get('truth_boundary') or {}
artifact = truth.get('current_artifact') or {}
plans = data.get('plans') or []
buyer_checklist = data.get('buyer_checklist') or []
sales_packet = data.get('sales_packet') or {}
note_schema = sales_packet.get('operator_note_schema') or {}
commercial = data.get('commercial_model') or {}
packages = commercial.get('recommended_packages') or []
ok = (
    offer.get('name') == '13FLOW Pro API'
    and offer.get('self_serve_checkout') is False
    and (offer.get('contact') or {}).get('email') == 'admin@toonux.com'
    and [p.get('name') for p in plans] == ['Technical pilot review', 'API integration review', 'MCP integration review']
    and 'organization name and billing contact' in buyer_checklist
    and 'Before I issue a scoped pilot key' in (sales_packet.get('lead_reply_template') or '')
    and note_schema.get('package') == 'Technical pilot review | API integration review | MCP integration review'
    and commercial.get('pricing_status') == 'paused_until_terms_and_capacity_are_ready'
    and packages and packages[0].get('price_eur_per_month') == 'not publicly quoted'
    and (commercial.get('do_not_discount_below') or {}).get('full_live_api_access_eur_per_month') is None
    and int(limits.get('rate_per_min') or 0) == 120
    and 'validated alpha' in not_yet
    and bool(commands.get('create_key'))
    and artifact.get('publishable_as_full_validation') is False
)
msg = str(data)
"
else
  bad "/api/pro-offer fetch" "curl failed"
fi

method_app="$tmpdir/methodology-app.json"
if fetch "/api/methodology/app" "$method_app"; then
  json_check "/api/methodology/app contract" "$method_app" "
sources = data.get('primary_sources') or []
state = data.get('current_state') or {}
interp = data.get('user_interpretation') or []
ok = (
    data.get('title') == '13FLOW application methodology'
    and state.get('public_state') == 'LIVE'
    and any(s.get('name') == 'SEC EDGAR filing and data APIs' for s in sources)
    and interp and interp[0].startswith('13F filings are delayed')
    and 'validated alpha' in (data.get('not_claimed') or [])
)
msg = str(data)
"
else
  bad "/api/methodology/app fetch" "curl failed"
fi

method_mcp="$tmpdir/methodology-mcp.json"
if fetch "/api/methodology/mcp" "$method_mcp"; then
  json_check "/api/methodology/mcp contract" "$method_mcp" "
contract = data.get('contract') or []
security = data.get('security') or {}
ok = (
    data.get('title') == '13FLOW MCP methodology'
    and any('fail closed' in item for item in contract)
    and 'Authorization: Bearer <token>' in (security.get('credential_headers') or [])
)
msg = str(data)
"
else
  bad "/api/methodology/mcp fetch" "curl failed"
fi

funds="$tmpdir/funds.json"
if fetch "/api/funds" "$funds"; then
  json_check "/api/funds non-empty" "$funds" "
ok = isinstance(data, list) and len(data) > 0 and bool(data[0].get('cik')) and bool(data[0].get('label'))
msg = str(data[:1] if isinstance(data, list) else data)
"
else
  bad "/api/funds fetch" "curl failed"
fi

for path in /api/data-quality /api/methodology/confluence-v1 /api/openapi.json; do
  out="$tmpdir/${path//\//_}.json"
  if fetch "$path" "$out"; then
    ok "$path fetch"
  else
    bad "$path fetch" "curl failed"
  fi
done

openapi="$tmpdir/_api_openapi.json"
if [[ -s "$openapi" ]]; then
  json_check "/api/openapi.json public paths" "$openapi" "
paths = data.get('paths') or {}
required = ['/api/live-status', '/api/product-status', '/api/pro-offer', '/api/funds', '/api/mcp', '/api/methodology/confluence-v1']
missing = [p for p in required if p not in paths]
ok = not missing
msg = 'missing paths: ' + ', '.join(missing)
"
fi

pro_status=$(curl -sS -o "$tmpdir/pro-status.txt" -w '%{http_code}' --max-time 15 "$SITE/api/pro/v1/status" || echo 000)
if [[ "$pro_status" == "401" ]] && grep -qi 'WWW-Authenticate: Bearer realm="13flow-pro"' <(curl -sSI "$SITE/api/pro/v1/status" || true); then
  ok "Pro API unauthenticated challenge"
else
  bad "Pro API unauthenticated challenge" "got HTTP $pro_status"
fi

if [[ "$REQUIRE_MCP" == "1" ]]; then
  mcp_tools="$tmpdir/mcp-tools.json"
  if curl -fsS --max-time 20 "$SITE/api/mcp" \
      -H 'Content-Type: application/json' \
      -H 'Accept: application/json, text/event-stream' \
      --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
      -o "$mcp_tools"; then
    json_check "MCP tools/list public contract" "$mcp_tools" "
tools = ((data.get('result') or {}).get('tools') or [])
names = {t.get('name') for t in tools}
required = {'get_live_status', 'get_product_status', 'get_pro_offer', 'list_funds', 'get_payment_policy', 'pro.list_funds'}
missing = sorted(required - names)
ok = not missing
msg = 'missing MCP tools: ' + ', '.join(missing)
"
  else
    bad "MCP tools/list public contract" "curl failed"
  fi

  mcp_pro_status=$(curl -sS -o "$tmpdir/mcp-pro.json" -w '%{http_code}' --max-time 20 "$SITE/api/mcp" \
      -H 'Content-Type: application/json' \
      -H 'Accept: application/json, text/event-stream' \
      --data '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"pro.list_funds","arguments":{}}}' \
      || echo 000)
  [[ "$mcp_pro_status" == "402" ]] \
    && ok "MCP Pro tool fail-closed without payment/key" \
    || bad "MCP Pro tool fail-closed without payment/key" "got HTTP $mcp_pro_status"
fi

echo
if [[ $fail -eq 0 ]]; then
  echo "PUBLIC SMOKE: all good"
else
  echo "PUBLIC SMOKE: problems found"
  exit 1
fi
