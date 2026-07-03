#!/usr/bin/env bash
# Safe rsync-style code deploy for 13FLOW production.
#
# Preserves host-built runtime dependencies that are intentionally not in Git:
#   - /opt/13flow/.venv
#   - /opt/13flow/mcp-server/node_modules
#
# Usage:
#   sudo SHA=<git-sha> SRC=/tmp/13flow-$SHA /opt/13flow/deploy/deploy-code-safe.sh
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/13flow}
SRC=${SRC:-}
SHA=${SHA:-}
WEB_GROUP=${WEB_GROUP:-flowapp}
BACKUP_DIR=${BACKUP_DIR:-/home/bluetouff}

if [[ $EUID -ne 0 ]]; then
  echo "Run me with sudo." >&2
  exit 1
fi
if [[ -z "$SHA" || -z "$SRC" ]]; then
  echo "Set SHA=<git-sha> and SRC=<checked-out-source-dir>." >&2
  exit 2
fi
if [[ ! -d "$SRC" ]]; then
  echo "Source directory not found: $SRC" >&2
  exit 2
fi
if [[ ! -x "$APP_DIR/.venv/bin/gunicorn" ]]; then
  echo "Refusing deploy: $APP_DIR/.venv/bin/gunicorn is missing or not executable." >&2
  exit 3
fi
if [[ ! -d "$APP_DIR/mcp-server/node_modules/@modelcontextprotocol/sdk" ]]; then
  echo "Refusing deploy: MCP node_modules are missing. Run npm ci in $APP_DIR/mcp-server first." >&2
  exit 3
fi

wait_url() {
  local label=$1 url=$2 attempts=${3:-20} sleep_s=${4:-1}
  local i
  for ((i=1; i<=attempts; i++)); do
    if curl -fsS --max-time 5 "$url"; then
      echo
      return 0
    fi
    if (( i < attempts )); then
      sleep "$sleep_s"
    fi
  done
  echo "Service did not become ready: $label ($url)" >&2
  return 1
}

service_exists() {
  systemctl cat "$1" >/dev/null 2>&1
}

stamp_sha() {
  local service=$1
  mkdir -p "/etc/systemd/system/$service.d"
  printf '[Service]\nEnvironment=SMARTMONEY_GIT_SHA=%s\n' "$SHA" \
    > "/etc/systemd/system/$service.d/version.conf"
}

backup="$BACKUP_DIR/13flow-backup-before-safe-deploy-$SHA-$(date -u +%Y%m%dT%H%M%SZ)"
echo "==> [1/6] Backup current tree to $backup"
cp -a "$APP_DIR" "$backup"

echo "==> [2/6] Stop services"
systemctl stop 13flow-mcp || true
if service_exists 13flow-pro.service; then
  systemctl stop 13flow-pro || true
fi
systemctl stop 13flow || true

echo "==> [3/6] Sync code while preserving runtime dependencies"
rsync -a --delete \
  --exclude .git \
  --exclude .venv \
  --exclude mcp-server/node_modules \
  "$SRC"/ "$APP_DIR"/

echo "==> [4/6] Normalize code permissions without touching preserved runtimes"
find "$APP_DIR" \
  -path "$APP_DIR/.venv" -prune -o \
  -path "$APP_DIR/mcp-server/node_modules" -prune -o \
  -exec chown root:"$WEB_GROUP" {} +
find "$APP_DIR" \
  -path "$APP_DIR/.venv" -prune -o \
  -path "$APP_DIR/mcp-server/node_modules" -prune -o \
  -type d -exec chmod 750 {} +
find "$APP_DIR" \
  -path "$APP_DIR/.venv" -prune -o \
  -path "$APP_DIR/mcp-server/node_modules" -prune -o \
  -type f -exec chmod 640 {} +
find "$APP_DIR/deploy" -maxdepth 1 -name '*.sh' -type f -exec chmod 750 {} +

echo "==> [5/6] Stamp deployed SHA through systemd"
stamp_sha 13flow.service
if service_exists 13flow-pro.service; then
  stamp_sha 13flow-pro.service
fi
if [[ -f /etc/13flow/13flow-mcp.env ]]; then
  if grep -q '^MCP_GIT_SHA=' /etc/13flow/13flow-mcp.env; then
    sed -i "s/^MCP_GIT_SHA=.*/MCP_GIT_SHA=$SHA/" /etc/13flow/13flow-mcp.env
  else
    printf '\nMCP_GIT_SHA=%s\n' "$SHA" >> /etc/13flow/13flow-mcp.env
  fi
fi
systemctl daemon-reload

echo "==> [6/6] Restart and verify local services"
systemctl reset-failed 13flow 13flow-pro 13flow-mcp || true
systemctl restart 13flow
if service_exists 13flow-pro.service; then
  systemctl restart 13flow-pro
fi
systemctl restart 13flow-mcp

wait_url "13flow API" "http://127.0.0.1:8000/api/version" 20 1
if service_exists 13flow-pro.service; then
  wait_url "13flow Pro API" "http://127.0.0.1:8001/api/pro/v1/openapi.json" 20 1
fi
wait_url "13flow MCP" "http://127.0.0.1:8849/healthz" 20 1
echo "Safe deploy complete. Run:"
echo "  sudo EXPECTED_SHA=$SHA $APP_DIR/deploy/smoke-public.sh"
