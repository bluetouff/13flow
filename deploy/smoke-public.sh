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
SMOKE_TIMING=${SMOKE_TIMING:-1}
SMOKE_SLOW_SECONDS=${SMOKE_SLOW_SECONDS:-2}

tmpdir=$(mktemp -d)
cleanup() { rm -rf "$tmpdir"; }
trap cleanup EXIT

fail=0
smoke_start_tick=$(date +%s)
smoke_last_tick=$smoke_start_tick
smoke_check_count=0
smoke_slow_labels=()
smoke_slow_durations=()
say(){ printf '%-58s %s\n' "$1" "$2"; }
record_check_timing(){
  [[ "$SMOKE_TIMING" == "1" ]] || return 0
  local label=$1 now delta
  now=$(date +%s)
  delta=$((now - smoke_last_tick))
  smoke_last_tick=$now
  smoke_check_count=$((smoke_check_count + 1))
  if (( delta >= SMOKE_SLOW_SECONDS )); then
    smoke_slow_labels+=("$label")
    smoke_slow_durations+=("$delta")
  fi
}
ok(){ record_check_timing "$1"; say "$1" "OK"; }
bad(){ record_check_timing "$1"; say "$1" "FAIL - $2"; fail=1; }
print_timing_summary(){
  [[ "$SMOKE_TIMING" == "1" ]] || return 0
  local total now i
  now=$(date +%s)
  total=$((now - smoke_start_tick))
  echo
  echo "PUBLIC SMOKE TIMING: total=${total}s checks=${smoke_check_count} slow_threshold=${SMOKE_SLOW_SECONDS}s"
  if ((${#smoke_slow_labels[@]})); then
    echo "PUBLIC SMOKE SLOW CHECKS:"
    for i in "${!smoke_slow_labels[@]}"; do
      printf '  %4ss  %s\n' "${smoke_slow_durations[$i]}" "${smoke_slow_labels[$i]}"
    done
  fi
}

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

coverage_page="$tmpdir/coverage.html"
if fetch "/coverage" "$coverage_page"; then
  grep -q "Trusted Fund Coverage" "$coverage_page" \
    && grep -q "Signal Eligibility Rule" "$coverage_page" \
    && grep -q "Excluded Funds" "$coverage_page" \
    && grep -q "Trusted Sample" "$coverage_page" \
    && grep -q "automated_fail_closed" "$coverage_page" \
    && grep -q "/api/data-quality" "$coverage_page" \
    && grep -q "/methodology" "$coverage_page" \
    && grep -q "not a performance claim" "$coverage_page" \
    && ok "/coverage quality page" \
    || bad "/coverage quality page" "missing coverage quality contract"
  contains_none "/coverage has no legacy/auth/checkout copy" "$coverage_page" "${legacy_forbidden[@]}"
else
  bad "/coverage quality page" "curl failed"
fi

security_page="$tmpdir/security.html"
if fetch "/security" "$security_page"; then
  grep -q "Research Surface Security" "$security_page" \
    && grep -q "Machine-readable security posture" "$security_page" \
    && grep -q "Operator Checks" "$security_page" \
    && grep -q "Non-Claims" "$security_page" \
    && grep -q "tokens_echoed:false" "$security_page" \
    && grep -q "secrets_in_payloads:false" "$security_page" \
    && grep -q "third-party penetration test" "$security_page" \
    && ok "/security posture page" \
    || bad "/security posture page" "missing security posture contract"
  contains_none "/security has no legacy/auth/checkout copy" "$security_page" "${legacy_forbidden[@]}"
else
  bad "/security posture page" "curl failed"
fi

pilot_page="$tmpdir/pilot.html"
if fetch "/pilot" "$pilot_page"; then
  grep -q "Operator Review Intake" "$pilot_page" \
    && grep -q "Operator Note Template" "$pilot_page" \
    && grep -q "Required Fields" "$pilot_page" \
    && grep -q "public_form_submission=false" "$pilot_page" \
    && grep -q "server_side_pii_storage=false" "$pilot_page" \
    && grep -q "/api/pilot-intake" "$pilot_page" \
    && grep -q "/api/pilot-intake.md" "$pilot_page" \
    && ok "/pilot intake page" \
    || bad "/pilot intake page" "missing pilot intake contract"
  contains_none "/pilot has no legacy/auth/checkout copy" "$pilot_page" "${legacy_forbidden[@]}"
else
  bad "/pilot intake page" "curl failed"
fi

pilot_request_page="$tmpdir/pilot-request.html"
if fetch "/pilot/request" "$pilot_request_page"; then
  grep -q "Assisted Operator Request" "$pilot_request_page" \
    && grep -q "data-pilot-request-app" "$pilot_request_page" \
    && grep -q "/api/pilot-request-assist" "$pilot_request_page" \
    && grep -q "server_side_pii_storage:false" "$pilot_request_page" \
    && grep -q "public_submission_endpoint:none" "$pilot_request_page" \
    && grep -q "navigator.clipboard.writeText" "$pilot_request_page" \
    && ! grep -qi "localStorage" "$pilot_request_page" \
    && ! grep -qi "sessionStorage" "$pilot_request_page" \
    && ok "/pilot/request assisted page" \
    || bad "/pilot/request assisted page" "missing assisted request contract"
  contains_none "/pilot/request has no legacy/auth/checkout copy" "$pilot_request_page" "${legacy_forbidden[@]}"
else
  bad "/pilot/request assisted page" "curl failed"
fi

readiness_page="$tmpdir/readiness.html"
if fetch "/readiness" "$readiness_page"; then
  grep -q "Readiness Checklist" "$readiness_page" \
    && grep -q "External Operator Checks" "$readiness_page" \
    && grep -q "/api/research-readiness" "$readiness_page" \
    && grep -q "/api/pro/v1/admin/health" "$readiness_page" \
    && ! grep -q "/pro/admin" "$readiness_page" \
    && grep -q "validated alpha" "$readiness_page" \
    && ok "/readiness evidence page" \
    || bad "/readiness evidence page" "missing research readiness copy"
  contains_none "/readiness has no legacy/auth/checkout copy" "$readiness_page" "${legacy_forbidden[@]}"
else
  bad "/readiness evidence page" "curl failed"
fi

buyer_pack_page="$tmpdir/buyer-pack.html"
if fetch "/buyer-pack" "$buyer_pack_page"; then
  grep -q "13FLOW Research Review Pack" "$buyer_pack_page" \
    && grep -q "Proof Points" "$buyer_pack_page" \
    && grep -q "Research Checklist" "$buyer_pack_page" \
    && grep -q "Operator Questions" "$buyer_pack_page" \
    && grep -q "Terms Boundary" "$buyer_pack_page" \
    && grep -q "/api/buyer-pack" "$buyer_pack_page" \
    && grep -q "/api/buyer-pack.md" "$buyer_pack_page" \
    && grep -q "/buyer-pack/print" "$buyer_pack_page" \
    && grep -q "/pilot" "$buyer_pack_page" \
    && grep -q "/security" "$buyer_pack_page" \
    && ! grep -q "/pro/onboarding" "$buyer_pack_page" \
    && grep -q "not a performance claim" "$buyer_pack_page" \
    && ok "/buyer-pack review page" \
    || bad "/buyer-pack review page" "missing research pack contract"
  contains_none "/buyer-pack has no legacy/auth/checkout copy" "$buyer_pack_page" "${legacy_forbidden[@]}"
else
  bad "/buyer-pack review page" "curl failed"
fi

buyer_pack_print="$tmpdir/buyer-pack-print.html"
if fetch "/buyer-pack/print" "$buyer_pack_print"; then
  grep -q "13FLOW Research Review Pack" "$buyer_pack_print" \
    && grep -q "PDF-ready printable view" "$buyer_pack_print" \
    && grep -q "Review Scope" "$buyer_pack_print" \
    && grep -q "Terms Boundary" "$buyer_pack_print" \
    && grep -q "/api/buyer-pack.md" "$buyer_pack_print" \
    && grep -q "not investment advice" "$buyer_pack_print" \
    && ok "/buyer-pack printable page" \
    || bad "/buyer-pack printable page" "missing printable buyer pack contract"
  contains_none "/buyer-pack printable has no legacy/auth/checkout copy" "$buyer_pack_print" "${legacy_forbidden[@]}"
else
  bad "/buyer-pack printable page" "curl failed"
fi

buyer_pack_md="$tmpdir/buyer-pack.md"
if fetch "/api/buyer-pack.md" "$buyer_pack_md"; then
  grep -q "# 13FLOW Research Review Pack" "$buyer_pack_md" \
    && grep -q "## Proof Points" "$buyer_pack_md" \
    && grep -q "## Evidence Links" "$buyer_pack_md" \
    && grep -q "/coverage" "$buyer_pack_md" \
    && grep -q "not investment advice" "$buyer_pack_md" \
    && ok "/api/buyer-pack.md export" \
    || bad "/api/buyer-pack.md export" "missing markdown research pack contract"
else
  bad "/api/buyer-pack.md export" "curl failed"
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
    && ! grep -q "payment flow" "$developers_page" \
    && ! grep -q "/api/pro/v1/openapi.json" "$developers_page" \
    && ! grep -q "Pro tools are intentionally visible" "$developers_page" \
    && ok "/developers page" \
    || bad "/developers page" "missing open developer contract copy"
  contains_none "/developers has no legacy/auth/checkout copy" "$developers_page" "${legacy_forbidden[@]}"
else
  bad "/developers page" "curl failed"
fi

pro_terms_code=$(curl -sS -o "$tmpdir/legal-pro-api.html" -w '%{http_code}' --max-time 20 "$SITE/legal/pro-api" || echo 000)
if [[ "$pro_terms_code" =~ ^30[12378]$ ]]; then
  ok "/legal/pro-api redirects to neutral legal page"
else
  bad "/legal/pro-api redirects to neutral legal page" "got HTTP $pro_terms_code"
fi

pro_code=$(curl -sS -o "$tmpdir/pro.html" -w '%{http_code}' --max-time 20 "$SITE/pro" || echo 000)
if [[ "$pro_code" =~ ^30[12378]$ ]]; then
  ok "/pro redirects to sandbox"
else
  bad "/pro redirects to sandbox" "got HTTP $pro_code"
fi

fr_pages=(
  "/fr|Construit pour les builders|Accueil FR"
  "/fr/sandbox|Sandbox en 60 secondes|Sandbox FR"
  "/fr/developers|Développeurs|Developers FR"
  "/fr/alternatives|Pourquoi pas sec-api|Alternatives FR"
  "/fr/trust-artifact|Trust layer, pas alpha|Trust artifact FR"
  "/fr/status|Statut|Status FR"
  "/fr/coverage|Couverture|Coverage FR"
  "/fr/validation|Validation|Validation FR"
  "/fr/security|Sécurité de la surface research|Security FR"
  "/fr/methodology|Méthodologie|Methodology FR"
  "/fr/methodology/app|Contrat d'interprétation|App methodology FR"
  "/fr/methodology/mcp|Contrat MCP|MCP methodology FR"
  "/fr/faq|Deux filings. Une piste de recherche.|FAQ FR"
  "/fr/about|Intelligence filing, construite dans le labo l0g|About FR"
  "/fr/legal|Conditions claires pour un outil public de recherche filing|Legal FR"
)
for item in "${fr_pages[@]}"; do
  IFS='|' read -r path needle label <<<"$item"
  out="$tmpdir/${path//\//_}.html"
  if fetch "$path" "$out"; then
    grep -q "$needle" "$out" \
      && grep -q 'hreflang="en"' "$out" \
      && grep -q 'hreflang="fr"' "$out" \
      && ! grep -q '\$19' "$out" \
      && ok "$label" \
      || bad "$label" "missing FR/i18n contract copy"
    contains_none "$label has no legacy/auth/checkout copy" "$out" "490 EUR / month" "Continue to checkout"
  else
    bad "$label" "curl failed"
  fi
done

pro_onboarding_page="$tmpdir/pro-onboarding.html"
if fetch "/pro/onboarding" "$pro_onboarding_page"; then
  grep -q "Integration Diagnostic" "$pro_onboarding_page" \
    && grep -q "data-pro-onboarding-app" "$pro_onboarding_page" \
    && grep -q "13flow.pro.onboarding.token" "$pro_onboarding_page" \
    && grep -q "/api/pro/v1/onboarding" "$pro_onboarding_page" \
    && grep -q "sessionStorage" "$pro_onboarding_page" \
    && grep -q "Authorization" "$pro_onboarding_page" \
    && grep -q "token_echoed" "$pro_onboarding_page" \
    && grep -q "token_in_url_allowed" "$pro_onboarding_page" \
    && ! grep -qi "localStorage" "$pro_onboarding_page" \
    && ! grep -qi "checkout" "$pro_onboarding_page" \
    && ok "/pro/onboarding diagnostic page" \
    || bad "/pro/onboarding diagnostic page" "missing onboarding diagnostic contract"
else
  bad "/pro/onboarding diagnostic page" "curl failed"
fi

pro_workspace_page="$tmpdir/pro-workspace.html"
if fetch "/pro/workspace" "$pro_workspace_page"; then
  grep -q "Workspace Cockpit" "$pro_workspace_page" \
    && grep -q "data-pro-workspace-app" "$pro_workspace_page" \
    && grep -q "sessionStorage" "$pro_workspace_page" \
    && grep -q "Edit Watchlist" "$pro_workspace_page" \
    && grep -q "Save changes" "$pro_workspace_page" \
    && grep -q "Workspace Report" "$pro_workspace_page" \
    && grep -q "workspaceReportRefresh" "$pro_workspace_page" \
    && grep -q "renderWorkspaceReport" "$pro_workspace_page" \
    && grep -q "Export JSON" "$pro_workspace_page" \
    && grep -q "Export CSV" "$pro_workspace_page" \
    && grep -q "downloadWorkspaceExport" "$pro_workspace_page" \
    && grep -q "Scheduled alerts" "$pro_workspace_page" \
    && grep -q "alert_policy: {enabled: alertEnabled" "$pro_workspace_page" \
    && grep -q "workspace/overview" "$pro_workspace_page" \
    && grep -q "workspaceAlertStatus" "$pro_workspace_page" \
    && grep -q "workspaceAlertTicker" "$pro_workspace_page" \
    && grep -q "workspaceAlertMinSeverity" "$pro_workspace_page" \
    && grep -q "workspaceAlertMinScore" "$pro_workspace_page" \
    && grep -q "workspaceAlertSort" "$pro_workspace_page" \
    && grep -q "visibleAlerts" "$pro_workspace_page" \
    && grep -q "Ack visible" "$pro_workspace_page" \
    && grep -q "Dismiss visible" "$pro_workspace_page" \
    && grep -q "Alert Details" "$pro_workspace_page" \
    && grep -q "data-alert-detail" "$pro_workspace_page" \
    && grep -q 'method: "PATCH"' "$pro_workspace_page" \
    && grep -q 'method: "PUT"' "$pro_workspace_page" \
    && ! grep -qi "localStorage" "$pro_workspace_page" \
    && ! grep -qi "checkout" "$pro_workspace_page" \
    && ok "/pro/workspace cockpit page" \
    || bad "/pro/workspace cockpit page" "missing workspace cockpit contract"
else
  bad "/pro/workspace cockpit page" "curl failed"
fi

pro_admin_page="$tmpdir/pro-admin.html"
admin_code=$(curl -sS -o "$pro_admin_page" -w '%{http_code}' --max-time 20 "$SITE/pro/admin" || echo 000)
if [[ "$admin_code" == "302" || "$admin_code" == "401" || "$admin_code" == "404" ]]; then
  ok "/pro/admin is not public ($admin_code)"
else
  bad "/pro/admin is not public" "got $admin_code"
fi
admin_login_code=$(curl -sS -o "$tmpdir/pro-admin-login.html" -w '%{http_code}' --max-time 20 "$SITE/pro/admin/login" || echo 000)
if [[ "$admin_login_code" == "200" ]]; then
  grep -q "13FLOW Admin" "$tmpdir/pro-admin-login.html" \
    && ! grep -q "data-pro-admin-app" "$tmpdir/pro-admin-login.html" \
    && ok "/pro/admin/login is login-only" \
    || bad "/pro/admin/login is login-only" "admin app leaked"
else
  [[ "$admin_login_code" == "404" ]] \
    && ok "/pro/admin/login unavailable when admin auth disabled" \
    || bad "/pro/admin/login login-only" "got $admin_login_code"
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
readiness = data.get('research_readiness') or {}
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

readiness="$tmpdir/research-readiness.json"
if fetch "/api/research-readiness" "$readiness"; then
  json_check "/api/research-readiness contract" "$readiness" "
snapshot = data.get('snapshot') or {}
quality = snapshot.get('quality_gate') or {}
public_checks = {item.get('id'): item for item in (data.get('public_checks') or [])}
external_checks = {item.get('id'): item for item in (data.get('external_checks') or [])}
ok = (
    data.get('status') in {'controlled_pilot_ready', 'controlled_pilot_ready_with_disclosures'}
    and data.get('sales_motion') == 'open_research_first'
    and data.get('self_serve_checkout') is False
    and data.get('public_quote_ready') is False
    and int(quality.get('trusted_funds') or 0) > 0
    and quality.get('human_review_required_for_routine_publication') is False
    and (public_checks.get('public_live_data') or {}).get('status') == 'pass'
    and (external_checks.get('pro_workspace_smoke') or {}).get('status') == 'external_required'
    and 'validated alpha' in (data.get('do_not_claim_yet') or [])
)
msg = str(data)[:1000]
"
else
  bad "/api/research-readiness fetch" "curl failed"
fi

security="$tmpdir/security-posture.json"
if fetch "/api/security-posture" "$security"; then
  json_check "/api/security-posture contract" "$security" "
public = data.get('public_surface') or {}
pro = data.get('pro_surface') or {}
privacy = data.get('privacy') or {}
quality = data.get('data_quality') or {}
links = {item.get('href') for item in (data.get('evidence_links') or [])}
ok = (
    data.get('status') == 'controlled_pilot_security_ready'
    and public.get('mode') == 'read_only_open_build'
    and public.get('synthetic_data') is False
    and pro.get('token_in_url_allowed') is False
    and privacy.get('tokens_echoed') is False
    and privacy.get('secrets_in_payloads') is False
    and quality.get('manual_13f_review_required_for_routine_publication') is False
    and '/api/security-posture' in links
    and '/api/openapi.json' in links
    and '/coverage' in links
    and '/validation' in links
    and 'third-party penetration test' in (data.get('non_claims') or [])
)
msg = str(data)[:1000]
"
else
  bad "/api/security-posture fetch" "curl failed"
fi

pilot="$tmpdir/pilot-intake.json"
if fetch "/api/pilot-intake" "$pilot"; then
  json_check "/api/pilot-intake contract" "$pilot" "
privacy = data.get('privacy') or {}
fields = {item.get('id'): item for item in (data.get('required_fields') or [])}
links = {item.get('href') for item in (data.get('evidence_links') or [])}
ok = (
    data.get('status') == 'operator_review_required'
    and data.get('self_serve_checkout') is False
    and data.get('public_form_submission') is False
    and data.get('public_submission_endpoint') is None
    and privacy.get('server_side_pii_storage') is False
    and privacy.get('token_collection') is False
    and privacy.get('secret_collection') is False
    and fields.get('organization', {}).get('required') is True
    and fields.get('requested_scopes', {}).get('purpose') == 'least-privilege key issuance'
    and '/api/pilot-intake.md' in links
    and '/security' in links
)
msg = str(data)[:1000]
"
else
  bad "/api/pilot-intake fetch" "curl failed"
fi

pilot_assist="$tmpdir/pilot-request-assist.json"
if fetch "/api/pilot-request-assist" "$pilot_assist"; then
  json_check "/api/pilot-request-assist contract" "$pilot_assist" "
schema = data.get('input_schema') or {}
privacy = data.get('privacy') or {}
admin = data.get('admin_transform') or {}
sample = data.get('sample_request') or {}
raw = str(data)
ok = (
    data.get('public_submission_endpoint') is None
    and data.get('server_side_pii_storage') is False
    and data.get('request_persisted') is False
    and data.get('web_worker_creates_tokens') is False
    and data.get('tokens_collected') is False
    and 'organization' in (schema.get('required') or [])
    and 'admin:read' in (schema.get('forbidden_customer_scopes') or [])
    and admin.get('endpoint') == '/api/pro/v1/admin/pilot-request-assist'
    and admin.get('stores_request') is False
    and sample.get('organization')
    and privacy.get('payloads_logged') is False
    and '13flow_live_' not in raw
)
msg = str(data)[:1000]
"
else
  bad "/api/pilot-request-assist fetch" "curl failed"
fi

pilot_md="$tmpdir/pilot-intake.md"
if fetch "/api/pilot-intake.md" "$pilot_md"; then
  grep -q "# 13FLOW Pilot Intake" "$pilot_md" \
    && grep -q "Public form submission: false" "$pilot_md" \
    && grep -q "## Operator Note Template" "$pilot_md" \
    && grep -q "requested_scopes" "$pilot_md" \
    && grep -q "/security" "$pilot_md" \
    && ok "/api/pilot-intake.md export" \
    || bad "/api/pilot-intake.md export" "missing markdown pilot intake contract"
else
  bad "/api/pilot-intake.md export" "curl failed"
fi

buyer_pack="$tmpdir/buyer-pack.json"
if fetch "/api/buyer-pack" "$buyer_pack"; then
  json_check "/api/buyer-pack contract" "$buyer_pack" "
snapshot = data.get('snapshot') or {}
terms = data.get('terms_boundary') or {}
links = {item.get('href') for item in (data.get('evidence_links') or [])}
ok = (
    data.get('status') in {'controlled_pilot_ready', 'controlled_pilot_ready_with_disclosures'}
    and data.get('sales_motion') == 'open_research_first'
    and data.get('self_serve_checkout') is False
    and data.get('public_quote_ready') is False
    and int(snapshot.get('trusted_funds') or 0) > 0
    and terms.get('operator_review_required') is True
    and 'validated alpha' in (data.get('do_not_claim_yet') or [])
    and '/pro/onboarding' not in links
    and '/pilot' in links
    and '/coverage' in links
    and '/security' in links
    and '/api/research-readiness' in links
    and (data.get('security_boundary') or {}).get('status') == 'controlled_pilot_security_ready'
    and (data.get('pilot_intake') or {}).get('public_form_submission') is False
    and any('Private keys are scoped' in item for item in (data.get('proof_points') or []))
)
msg = str(data)[:1000]
"
else
  bad "/api/buyer-pack fetch" "curl failed"
fi

offer="$tmpdir/pro-offer.json"
if fetch "/api/pro-offer" "$offer"; then
  json_check "/api/pro-offer retired compatibility" "$offer" "
surface = data.get('public_surface') or {}
commercial = data.get('commercial_model') or {}
limits = data.get('default_limits') or {}
ok = (
    data.get('status') == 'public_offer_retired'
    and surface.get('name') == '13FLOW Open Research Surface'
    and surface.get('payment_flow') is False
    and surface.get('browser_account') is False
    and commercial.get('status') == 'paused'
    and commercial.get('recommended_packages') == []
    and int(limits.get('sandbox_rate_per_min') or 0) == 20
)
msg = str(data)[:1000]
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

watchlist_discover="$tmpdir/watchlist-discover.json"
if fetch "/api/watchlist/discover?limit=10" "$watchlist_discover"; then
  json_check "/api/watchlist/discover contract" "$watchlist_discover" "
meta = data.get('metadata') or {}
items = data.get('items') or []
summary = data.get('summary') or {}
ok = (
    meta.get('version') == 'watchlist_discovery_v1'
    and meta.get('source') == 'trusted_ticker_flow'
    and meta.get('human_review_required_for_routine_publication') is False
    and isinstance(meta.get('filters'), dict)
    and int(meta.get('returned_count') or 0) <= 10
    and isinstance(items, list)
    and set(summary.keys()) >= {'alerts', 'watch', 'monitor', 'blocked'}
    and (not items or (
        bool(items[0].get('ticker'))
        and items[0].get('action') in {'alert', 'watch', 'monitor', 'blocked'}
        and bool(items[0].get('score'))
        and bool(items[0].get('discovery'))
    ))
)
msg = str(data)[:1000]
"
else
  bad "/api/watchlist/discover fetch" "curl failed"
fi

watchlist_filtered="$tmpdir/watchlist-discover-filtered.json"
if fetch "/api/watchlist/discover?limit=10&action=alert&min_score=50" "$watchlist_filtered"; then
  json_check "/api/watchlist/discover filtered contract" "$watchlist_filtered" "
meta = data.get('metadata') or {}
items = data.get('items') or []
filters = meta.get('filters') or {}
ok = (
    meta.get('version') == 'watchlist_discovery_v1'
    and filters.get('action') == ['alert']
    and float(filters.get('min_score') or 0) == 50.0
    and int(meta.get('returned_count') or 0) <= 10
    and int(meta.get('filtered_count') or 0) >= int(meta.get('returned_count') or 0)
    and all((item.get('action') == 'alert' and float((item.get('score') or {}).get('score') or 0) >= 50) for item in items)
)
msg = str(data)[:1000]
"
else
  bad "/api/watchlist/discover filtered fetch" "curl failed"
fi

for path in /api/data-quality /api/methodology/confluence-v1 /api/i18n /api/openapi.json; do
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
required = ['/api/live-status', '/api/product-status', '/api/research-readiness', '/api/security-posture', '/api/pilot-intake', '/api/pilot-intake.md', '/api/pilot-request-assist', '/api/buyer-pack', '/api/buyer-pack.md', '/api/i18n', '/api/funds', '/api/watchlist/discover', '/api/mcp', '/api/methodology/confluence-v1']
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

print_timing_summary
echo
if [[ $fail -eq 0 ]]; then
  echo "PUBLIC SMOKE: all good"
else
  echo "PUBLIC SMOKE: problems found"
  exit 1
fi
