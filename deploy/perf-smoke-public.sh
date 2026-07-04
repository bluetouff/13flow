#!/usr/bin/env bash
# Lightweight public-route latency smoke for 13FLOW.
#
# This is not a load test. It samples critical public routes after deploy so
# operators can distinguish "the functional smoke got larger" from "prod routes
# are getting slower".
#
# Usage:
#   ./deploy/perf-smoke-public.sh
#   SITE=https://staging.13flow.eu SAMPLES=7 ./deploy/perf-smoke-public.sh
#   WARN_MS=1500 FAIL_MS=3000 ./deploy/perf-smoke-public.sh
set -uo pipefail

SITE=${SITE:-https://13flow.eu}
SAMPLES=${SAMPLES:-5}
WARN_MS=${WARN_MS:-1500}
FAIL_MS=${FAIL_MS:-3000}
MAX_TIME=${MAX_TIME:-10}

routes=(
  "/api/version"
  "/api/live-status"
  "/api/product-status"
  "/api/commercial-readiness"
  "/api/funds"
  "/api/watchlist/discover"
)

fail=0
warn=0

ms_from_seconds() {
  awk -v seconds="$1" 'BEGIN { printf "%d", seconds * 1000 }'
}

sample_route() {
  local route=$1
  local i code seconds ms total max min avg status
  total=0
  max=0
  min=999999
  status="OK"
  for ((i = 1; i <= SAMPLES; i++)); do
    read -r code seconds < <(
      curl -sS -o /dev/null -w '%{http_code} %{time_total}' --max-time "$MAX_TIME" "$SITE$route" \
        || printf '000 0'
    )
    ms=$(ms_from_seconds "$seconds")
    if [[ ! "$code" =~ ^2 ]]; then
      status="FAIL"
      fail=1
    fi
    total=$((total + ms))
    ((ms > max)) && max=$ms
    ((ms < min)) && min=$ms
  done
  avg=$((total / SAMPLES))
  if ((max >= FAIL_MS)); then
    status="FAIL"
    fail=1
  elif ((max >= WARN_MS)); then
    [[ "$status" == "OK" ]] && status="WARN"
    warn=1
  fi
  printf '%-34s status=%-4s avg=%4sms min=%4sms max=%4sms samples=%s\n' \
    "$route" "$status" "$avg" "$min" "$max" "$SAMPLES"
}

echo "PUBLIC PERF SMOKE: site=$SITE samples=$SAMPLES warn_ms=$WARN_MS fail_ms=$FAIL_MS"
for route in "${routes[@]}"; do
  sample_route "$route"
done

if ((fail)); then
  echo "PUBLIC PERF SMOKE: fail"
  exit 1
fi
if ((warn)); then
  echo "PUBLIC PERF SMOKE: warn"
else
  echo "PUBLIC PERF SMOKE: ok"
fi
