#!/usr/bin/env bash
# Diagnose public 13FLOW surface drift across DNS, Apache/proxy and app backend.
#
# This script is intentionally read-only and secret-free. It is meant to answer:
# - Which IP serves the public site?
# - Do different user agents see different HTML?
# - Do root/pro/confluence expose legacy demo/auth/checkout markers?
# - If run on the server, does the local backend differ from the public vhost?
#
# Usage:
#   ./deploy/diagnose-public-surface.sh
#   SITE=https://13flow.eu HTTP_SITE=http://13flow.eu ./deploy/diagnose-public-surface.sh
#   BACKEND=http://127.0.0.1:8000 ./deploy/diagnose-public-surface.sh

set -uo pipefail

SITE=${SITE:-https://13flow.eu}
HTTP_SITE=${HTTP_SITE:-http://13flow.eu}
BACKEND=${BACKEND:-http://127.0.0.1:8000}

tmpdir=$(mktemp -d)
cleanup() { rm -rf "$tmpdir"; }
trap cleanup EXIT

say() { printf '%-24s %s\n' "$1" "$2"; }
section() { printf '\n== %s ==\n' "$1"; }

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

dns_lookup() {
  local rr=$1
  if command -v dig >/dev/null 2>&1; then
    dig +short "$rr" 13flow.eu || true
  else
    getent ahosts 13flow.eu 2>/dev/null | awk '{print $1}' | sort -u || true
  fi
}

legacy_markers() {
  grep -Eo 'SAMPLE DATA|Sign in|Upgrade to Pro|EUR 12|€12|12/mo|Continue to checkout|data-view="alerts"' "$1" 2>/dev/null \
    | sort -u \
    | tr '\n' ',' \
    | sed 's/,$//' || true
}

header_value() {
  local file=$1 name=$2
  awk -v n="$name" 'tolower($1)==tolower(n ":"){sub(/^[^ ]+ /,""); print}' "$file" | tail -n 1 | tr -d '\r'
}

probe() {
  local label=$1 url=$2
  shift 2
  local body="$tmpdir/${label}.body"
  local hdr="$tmpdir/${label}.headers"
  rm -f "$body" "$hdr"

  local code ip bytes hash title old cache server location
  code=$(curl -sS -L --max-time 25 -D "$hdr" -o "$body" -w '%{http_code}' "$@" "$url" || printf 'ERR')
  ip=$(curl -sS -L --max-time 25 -o /dev/null -w '%{remote_ip}' "$@" "$url" || printf 'ERR')
  bytes=$(wc -c < "$body" 2>/dev/null | tr -d ' ' || printf '0')
  hash=$(sha256_file "$body" 2>/dev/null || printf '-')
  title=$(grep -io '<title>[^<]*' "$body" 2>/dev/null | head -n 1 | sed 's/<title>//I' || true)
  old=$(legacy_markers "$body")
  cache=$(header_value "$hdr" "cache-control")
  server=$(header_value "$hdr" "server")
  location=$(header_value "$hdr" "location")

  printf '%-18s code=%-4s ip=%-18s bytes=%-7s hash=%s\n' "$label" "$code" "$ip" "$bytes" "$hash"
  printf '  title=[%s]\n' "${title:-}"
  printf '  old_markers=[%s]\n' "${old:-}"
  printf '  cache=[%s] server=[%s] location=[%s]\n' "${cache:-}" "${server:-}" "${location:-}"
}

section "time"
date -u '+UTC %Y-%m-%dT%H:%M:%SZ'

section "dns"
say "A" "$(dns_lookup A | tr '\n' ' ' | sed 's/ $//')"
say "AAAA" "$(dns_lookup AAAA | tr '\n' ' ' | sed 's/ $//')"
say "CNAME" "$(dns_lookup CNAME | tr '\n' ' ' | sed 's/ $//')"

section "http redirect"
curl -sS -I --max-time 15 "$HTTP_SITE/" | sed -n '1,12p' || true

section "root matrix"
probe "default" "$SITE/"
probe "ipv4" "$SITE/" -4
probe "ipv6" "$SITE/" -6
probe "compressed" "$SITE/" --compressed
probe "mozilla" "$SITE/" -A 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36'
probe "googlebot" "$SITE/" -A 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'
probe "openai" "$SITE/" -A 'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OpenAI-SearchBot/1.0; +https://openai.com/searchbot'
probe "no-cache" "$SITE/" -H 'Cache-Control: no-cache' -H 'Pragma: no-cache'

section "key urls"
probe "api-version" "$SITE/api/version"
probe "pro" "$SITE/pro"
probe "confluence" "$SITE/confluence"
probe "app" "$SITE/app"
probe "validation" "$SITE/validation"

section "local backend if reachable"
if curl -sS --max-time 3 -o /dev/null "$BACKEND/api/version" >/dev/null 2>&1; then
  probe "backend-root" "$BACKEND/"
  probe "backend-version" "$BACKEND/api/version"
  probe "backend-pro" "$BACKEND/pro"
  probe "backend-confluence" "$BACKEND/confluence"
else
  say "backend" "not reachable at $BACKEND"
fi

section "interpretation"
cat <<'TXT'
If public probes are clean but a third-party web tool still shows old SAMPLE DATA,
the stale surface is likely inside that tool's cache/index, not Apache, DNS or the
13FLOW backend.

If public probes show old markers but backend probes are clean, inspect Apache
vhost rules, static aliases, reverse-proxy target, cache modules and stale files
under the document root.

If backend probes show old markers, the deployed code or runtime environment is
not the expected revision.
TXT
