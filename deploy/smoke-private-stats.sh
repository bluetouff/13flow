#!/usr/bin/env bash
set -Eeuo pipefail

SITE=${SITE:-https://13flow.eu}
headers=$(curl --silent --show-error --head --max-time 15 "$SITE/stats/")
status=$(printf '%s\n' "$headers" | awk 'NR==1 {print $2}')

if [[ "$status" != "401" ]]; then
  echo "Private stats: expected HTTP 401 without credentials, got ${status:-unknown}." >&2
  exit 1
fi
grep -qi '^WWW-Authenticate: Basic realm="13FLOW private statistics"' <<<"$headers"
grep -qi '^Cache-Control: private, no-store, max-age=0' <<<"$headers"
grep -qi '^X-Robots-Tag: noindex, nofollow, noarchive' <<<"$headers"
grep -qi "^Content-Security-Policy: .*script-src 'self' 'unsafe-inline' 'unsafe-eval'" <<<"$headers"
grep -qi "^Content-Security-Policy: .*connect-src 'none'" <<<"$headers"
grep -qi "^Content-Security-Policy: .*frame-ancestors 'none'" <<<"$headers"

csp_count=$(grep -ci '^Content-Security-Policy:' <<<"$headers")
if [[ "$csp_count" != "1" ]]; then
  echo "Private stats: expected exactly one CSP header, got $csp_count." >&2
  exit 1
fi

redirect_headers=$(curl --silent --show-error --head --max-time 15 "$SITE/stats")
redirect_status=$(printf '%s\n' "$redirect_headers" | awk 'NR==1 {print $2}')
redirect_location=$(printf '%s\n' "$redirect_headers" | awk 'tolower($1) == "location:" {print $2; exit}' | tr -d '\r')
if [[ ! "$redirect_status" =~ ^30 || "$redirect_location" != "/stats/" ]]; then
  echo "Private stats: /stats must redirect to /stats/, got $redirect_status ${redirect_location:-none}." >&2
  exit 1
fi

echo "PRIVATE STATS SMOKE: authentication and CSP boundary are correct"
