#!/usr/bin/env bash
# Post-deploy sanity check for the 13FLOW open build. Run from anywhere:
#   ./deploy/preflight.sh            # checks https://13flow.eu
#   SITE=https://staging.13flow.eu ./deploy/preflight.sh
# Exits non-zero if anything looks wrong. Needs curl.
set -uo pipefail
SITE=${SITE:-https://13flow.eu}
fail=0
say(){ printf '%-52s %s\n' "$1" "$2"; }
ok(){ say "$1" "OK"; }
bad(){ say "$1" "FAIL — $2"; fail=1; }

# 1) HTTP must redirect to HTTPS
code=$(curl -s -o /dev/null -w '%{http_code}' "http://13flow.eu/" || echo 000)
[[ "$code" =~ ^30 ]] && ok "http -> https redirect ($code)" || bad "http -> https redirect" "got $code, expected 301/302"

# 2) HTTPS is up and the app reports the OPEN build
cfg=$(curl -fsS "$SITE/api/config" || echo "")
echo "$cfg" | grep -q '"open": *true' && ok "open build active (/api/config)" || bad "open build active" "open != true: $cfg"
echo "$cfg" | grep -q '"alerts": *false' && ok "alerts disabled" || bad "alerts disabled" "$cfg"

# 3) Private surface must be gone (404, not 401)
for p in /api/auth/me /api/subscriptions /api/billing/config; do
  c=$(curl -s -o /dev/null -w '%{http_code}' "$SITE$p")
  [[ "$c" == "404" ]] && ok "removed: $p (404)" || bad "removed: $p" "got $c, expected 404"
done

# 4) Writes rejected at the edge (Apache LimitExcept)
c=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$SITE/api/funds")
[[ "$c" == "403" || "$c" == "405" ]] && ok "POST blocked at edge ($c)" || bad "POST blocked" "got $c"

# 5) Security headers
H=$(curl -sSI "$SITE/")
grep -qi '^strict-transport-security:' <<<"$H" && ok "HSTS present" || bad "HSTS present" "missing"
grep -qi '^content-security-policy:.*nonce-' <<<"$H" && ok "nonce CSP on HTML" || bad "nonce CSP" "missing/!nonce"
grep -qi '^x-frame-options: *DENY' <<<"$H" && ok "X-Frame-Options DENY" || bad "X-Frame-Options" "missing"
grep -qi '^x-content-type-options: *nosniff' <<<"$H" && ok "nosniff" || bad "nosniff" "missing"
grep -qiE '^content-security-policy:.*unsafe-inline[^;]*; *script' <<<"$H" && bad "script CSP" "unsafe-inline in script-src" || ok "no unsafe-inline in script-src"

# 6) Public data actually loads
n=$(curl -fsS "$SITE/api/funds" | grep -o '"cik"' | wc -l | tr -d ' ')
[[ "$n" -gt 0 ]] && ok "funds served ($n)" || bad "funds served" "0 funds — seed/ingest the DB"

echo
[[ $fail -eq 0 ]] && echo "PREFLIGHT: all good ✓" || { echo "PREFLIGHT: problems found ✗"; exit 1; }
