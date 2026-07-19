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
MCP_GROUP=${MCP_GROUP:-flowmcp}
BACKUP_DIR=${BACKUP_DIR:-/home/bluetouff}
MCP_UNIT=/etc/systemd/system/13flow-mcp.service
APACHE_VHOST=/etc/apache2/sites-available/13flow.conf
STATS_APACHE_FRAGMENT=/etc/apache2/13flow-stats.conf
STATS_HTPASSWD=/etc/apache2/13flow-stats.htpasswd
STATS_GENERATOR=/usr/local/libexec/13flow-generate-stats
STATS_UNIT=/etc/systemd/system/13flow-stats.service
STATS_TIMER=/etc/systemd/system/13flow-stats.timer
ZEN_DEFAULT_VHOST=/etc/apache2/sites-available/000-zen-default.conf
ZEN_DEFAULT_ENABLED=/etc/apache2/sites-enabled/000-zen-default.conf
ZEN_DEFAULT_ROOT=/var/www/html/zen-default
ZEN_DEFAULT_CERT_DIR=/etc/letsencrypt/live/zen-default
ZEN_DEFAULT_HOSTS=(toonux.org toonux.com l0g.me l0g.us w2p.org)
stats_runtime_enabled=0
zen_default_vhost_existed=0
zen_default_root_existed=0
zen_default_site_enabled=0
zen_default_cert_name=13flow.eu

if [[ $EUID -ne 0 ]]; then
  echo "Run me with sudo." >&2
  exit 1
fi
if [[ -z "$SHA" || -z "$SRC" ]]; then
  echo "Set SHA=<git-sha> and SRC=<checked-out-source-dir>." >&2
  exit 2
fi
if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "SHA must be an exact 40-character lowercase hexadecimal Git commit." >&2
  exit 2
fi
if [[ ! -d "$SRC" ]]; then
  echo "Source directory not found: $SRC" >&2
  exit 2
fi
for zen_source in \
  "$SRC/deploy/apache-zen-default.conf" \
  "$SRC/deploy/zen-default/index.html" \
  "$SRC/deploy/zen-default/zen.css"; do
  if [[ ! -f "$zen_source" || -L "$zen_source" ]]; then
    echo "Refusing deploy: missing or symlinked ZEN default source: $zen_source" >&2
    exit 3
  fi
done
if [[ -L "$SRC/deploy/zen-default" ]]; then
  echo "Refusing deploy: ZEN default source directory is a symlink." >&2
  exit 3
fi
if [[ -L "$ZEN_DEFAULT_VHOST" || -L "$ZEN_DEFAULT_ROOT" ]]; then
  echo "Refusing deploy: ZEN default vhost or document root is a symlink." >&2
  exit 3
fi
if [[ -e "$ZEN_DEFAULT_ROOT" && ! -d "$ZEN_DEFAULT_ROOT" ]]; then
  echo "Refusing deploy: ZEN default document root is not a directory." >&2
  exit 3
fi
if [[ -e "$ZEN_DEFAULT_VHOST" ]]; then
  zen_default_vhost_existed=1
  if [[ $(stat -c '%a:%U:%G' "$ZEN_DEFAULT_VHOST") != "644:root:root" ]]; then
    echo "Refusing deploy: ZEN default vhost ownership or mode is unsafe." >&2
    exit 3
  fi
fi
if [[ -d "$ZEN_DEFAULT_ROOT" ]]; then
  zen_default_root_existed=1
  if [[ $(stat -c '%a:%U:%G' "$ZEN_DEFAULT_ROOT") != "750:root:www-data" ]]; then
    echo "Refusing deploy: ZEN default document root ownership or mode is unsafe." >&2
    exit 3
  fi
fi
if [[ -e "$ZEN_DEFAULT_ENABLED" || -L "$ZEN_DEFAULT_ENABLED" ]]; then
  if [[ ! -L "$ZEN_DEFAULT_ENABLED" ]] || \
     [[ $(readlink -f "$ZEN_DEFAULT_ENABLED") != "$ZEN_DEFAULT_VHOST" ]]; then
    echo "Refusing deploy: ZEN default enabled-site entry is unsafe." >&2
    exit 3
  fi
  zen_default_site_enabled=1
fi
zen_default_cert_count=0
for cert_path in "$ZEN_DEFAULT_CERT_DIR/fullchain.pem" "$ZEN_DEFAULT_CERT_DIR/privkey.pem"; do
  [[ -e "$cert_path" ]] && zen_default_cert_count=$((zen_default_cert_count + 1))
done
if (( zen_default_cert_count == 1 )); then
  echo "Refusing deploy: the dedicated ZEN default certificate is incomplete." >&2
  exit 3
fi
if (( zen_default_cert_count == 2 )); then
  for zen_host in "${ZEN_DEFAULT_HOSTS[@]}"; do
    if ! openssl x509 -in "$ZEN_DEFAULT_CERT_DIR/fullchain.pem" \
      -noout -checkhost "$zen_host" >/dev/null; then
      echo "Refusing deploy: the ZEN default certificate does not cover $zen_host." >&2
      exit 3
    fi
  done
  zen_default_cert_name=zen-default
fi
if ! getent group "$MCP_GROUP" >/dev/null || ! id -u flowmcp >/dev/null 2>&1; then
  echo "Refusing deploy: dedicated flowmcp user/group is missing." >&2
  exit 3
fi
if [[ ! -x "$APP_DIR/.venv/bin/gunicorn" ]]; then
  echo "Refusing deploy: $APP_DIR/.venv/bin/gunicorn is missing or not executable." >&2
  exit 3
fi
if [[ ! -d "$APP_DIR/mcp-server/node_modules/@modelcontextprotocol/sdk" ]]; then
  echo "Refusing deploy: MCP node_modules are missing. Run npm ci in $APP_DIR/mcp-server first." >&2
  exit 3
fi

stats_runtime_count=0
for stats_path in \
  "$STATS_APACHE_FRAGMENT" "$STATS_HTPASSWD" "$STATS_GENERATOR" \
  "$STATS_UNIT" "$STATS_TIMER"; do
  [[ -f "$stats_path" ]] && stats_runtime_count=$((stats_runtime_count + 1))
done
if (( stats_runtime_count != 0 && stats_runtime_count != 5 )); then
  echo "Refusing deploy: private statistics installation is incomplete." >&2
  exit 3
fi
(( stats_runtime_count == 5 )) && stats_runtime_enabled=1
if (( stats_runtime_enabled )); then
  for stats_path in \
    "$STATS_APACHE_FRAGMENT" "$STATS_HTPASSWD" "$STATS_GENERATOR" \
    "$STATS_UNIT" "$STATS_TIMER"; do
    if [[ -L "$stats_path" ]]; then
      echo "Refusing deploy: statistics runtime file is a symlink: $stats_path" >&2
      exit 3
    fi
  done
  if [[ $(stat -c '%a:%U:%G' "$STATS_HTPASSWD") != "640:root:www-data" ]]; then
    echo "Refusing deploy: statistics password file ownership or mode is unsafe." >&2
    exit 3
  fi
  if ! id -u flowstats >/dev/null 2>&1 || \
     [[ ! -d /var/www/html/13flow-stats || -L /var/www/html/13flow-stats ]]; then
    echo "Refusing deploy: statistics user or output directory is missing or unsafe." >&2
    exit 3
  fi
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

rollback() {
  local rc=$?
  trap - ERR
  set +e
  echo "Deploy failed (rc=$rc); restoring previous code and service configs." >&2
  if [[ -d "${backup:-}" ]]; then
    rsync -a --delete "$backup"/ "$APP_DIR"/
  fi
  if [[ -f "${config_backup:-}/13flow-mcp.service" ]]; then
    cp -a "$config_backup/13flow-mcp.service" "$MCP_UNIT"
  fi
  if [[ -f "${config_backup:-}/13flow.conf" ]]; then
    cp -a "$config_backup/13flow.conf" "$APACHE_VHOST"
  fi
  if [[ -f "${config_backup:-}/zen-default-state-recorded" ]]; then
    if [[ -f "$config_backup/zen-default-vhost.existed" ]]; then
      cp -a "$config_backup/000-zen-default.conf" "$ZEN_DEFAULT_VHOST"
    else
      rm -f -- "$ZEN_DEFAULT_VHOST"
    fi
    if [[ -f "$config_backup/zen-default-root.existed" ]]; then
      install -d -o root -g www-data -m 750 "$ZEN_DEFAULT_ROOT"
      rsync -a --delete "$config_backup/zen-default-root"/ "$ZEN_DEFAULT_ROOT"/
    else
      rm -rf -- "$ZEN_DEFAULT_ROOT"
    fi
    if [[ -f "$config_backup/zen-default-site-enabled" ]]; then
      a2ensite 000-zen-default.conf >/dev/null
    else
      rm -f -- "$ZEN_DEFAULT_ENABLED"
    fi
  fi
  if (( stats_runtime_enabled )); then
    cp -a "$config_backup/13flow-stats.conf" "$STATS_APACHE_FRAGMENT"
    cp -a "$config_backup/13flow-generate-stats" "$STATS_GENERATOR"
    cp -a "$config_backup/13flow-stats.service" "$STATS_UNIT"
    cp -a "$config_backup/13flow-stats.timer" "$STATS_TIMER"
  fi
  systemctl daemon-reload
  systemctl restart 13flow
  if service_exists 13flow-pro.service; then
    systemctl restart 13flow-pro
  fi
  systemctl restart 13flow-mcp
  if (( stats_runtime_enabled )); then
    systemctl restart 13flow-stats.timer
    systemctl start 13flow-stats.service
  fi
  apache2ctl configtest && systemctl reload apache2
  exit "$rc"
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
config_backup="${backup}-etc"
trap rollback ERR
echo "==> [1/8] Backup current tree and active service configs"
cp -a "$APP_DIR" "$backup"
install -d -m 700 "$config_backup"
cp -a "$MCP_UNIT" "$config_backup/13flow-mcp.service"
cp -a "$APACHE_VHOST" "$config_backup/13flow.conf"
touch "$config_backup/zen-default-state-recorded"
if (( zen_default_vhost_existed )); then
  cp -a "$ZEN_DEFAULT_VHOST" "$config_backup/000-zen-default.conf"
  touch "$config_backup/zen-default-vhost.existed"
fi
if (( zen_default_root_existed )); then
  cp -a "$ZEN_DEFAULT_ROOT" "$config_backup/zen-default-root"
  touch "$config_backup/zen-default-root.existed"
fi
if (( zen_default_site_enabled )); then
  touch "$config_backup/zen-default-site-enabled"
fi
if (( stats_runtime_enabled )); then
  cp -a "$STATS_APACHE_FRAGMENT" "$config_backup/13flow-stats.conf"
  cp -a "$STATS_GENERATOR" "$config_backup/13flow-generate-stats"
  cp -a "$STATS_UNIT" "$config_backup/13flow-stats.service"
  cp -a "$STATS_TIMER" "$config_backup/13flow-stats.timer"
fi

echo "==> [2/8] Stop services"
systemctl stop 13flow-mcp || true
if service_exists 13flow-pro.service; then
  systemctl stop 13flow-pro || true
fi
systemctl stop 13flow || true

echo "==> [3/8] Sync code while preserving runtime dependencies"
rsync -a --delete \
  --exclude .git \
  --exclude .venv \
  --exclude mcp-server/node_modules \
  "$SRC"/ "$APP_DIR"/

echo "==> [4/8] Install locked MCP production dependencies"
cd "$APP_DIR/mcp-server"
npm ci --omit=dev --ignore-scripts
node -e '
const fs = require("node:fs");
const packageVersion = require("./package.json").version;
const manifestVersion = JSON.parse(fs.readFileSync("../server.json", "utf8")).version;
if (packageVersion !== manifestVersion) throw new Error("MCP package/manifest version mismatch");
'

echo "==> [5/8] Normalize code permissions without touching preserved runtimes"
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
# flowmcp can traverse the application root but can only read its own subtree.
chmod o+x "$APP_DIR"
chown -R root:"$MCP_GROUP" "$APP_DIR/mcp-server"
find "$APP_DIR/mcp-server" \
  -path "$APP_DIR/mcp-server/node_modules" -prune -o \
  -type d -exec chmod 750 {} +
find "$APP_DIR/mcp-server" \
  -path "$APP_DIR/mcp-server/node_modules" -prune -o \
  -type f -exec chmod 640 {} +

echo "==> [6/8] Install and validate systemd/Apache configuration"
install -o root -g root -m 644 \
  "$APP_DIR/mcp-server/deploy/13flow-mcp.service" "$MCP_UNIT"
install -d -o root -g www-data -m 750 "$ZEN_DEFAULT_ROOT"
install -o root -g www-data -m 640 \
  "$APP_DIR/deploy/zen-default/index.html" "$ZEN_DEFAULT_ROOT/index.html"
install -o root -g www-data -m 640 \
  "$APP_DIR/deploy/zen-default/zen.css" "$ZEN_DEFAULT_ROOT/zen.css"
sed "s#/live/13flow.eu/#/live/$zen_default_cert_name/#g" \
  "$APP_DIR/deploy/apache-zen-default.conf" \
  > "$config_backup/000-zen-default.rendered.conf"
install -o root -g root -m 644 \
  "$config_backup/000-zen-default.rendered.conf" "$ZEN_DEFAULT_VHOST"
a2ensite 000-zen-default.conf >/dev/null
install -o root -g root -m 644 \
  "$APP_DIR/deploy/apache-13flow.conf" "$APACHE_VHOST"
if (( stats_runtime_enabled )); then
  install -o root -g root -m 644 \
    "$APP_DIR/deploy/apache-13flow-stats.conf" "$STATS_APACHE_FRAGMENT"
  install -o root -g root -m 755 \
    "$APP_DIR/deploy/generate-stats.sh" "$STATS_GENERATOR"
  install -o root -g root -m 644 \
    "$APP_DIR/deploy/13flow-stats.service" "$STATS_UNIT"
  install -o root -g root -m 644 \
    "$APP_DIR/deploy/13flow-stats.timer" "$STATS_TIMER"
fi
apache2ctl configtest

echo "==> [7/8] Stamp deployed SHA through systemd"
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
  chown root:"$MCP_GROUP" /etc/13flow/13flow-mcp.env
  chmod 640 /etc/13flow/13flow-mcp.env
fi
systemctl daemon-reload

echo "==> [8/8] Restart and verify local services"
systemctl reset-failed 13flow 13flow-pro 13flow-mcp || true
systemctl restart 13flow
if service_exists 13flow-pro.service; then
  systemctl restart 13flow-pro
fi
systemctl restart 13flow-mcp
if (( stats_runtime_enabled )); then
  systemctl restart 13flow-stats.timer
  systemctl start 13flow-stats.service
fi

wait_url "13flow API" "http://127.0.0.1:8000/api/version" 20 1
if service_exists 13flow-pro.service; then
  wait_url "13flow Pro API" "http://127.0.0.1:8001/api/pro/v1/openapi.json" 20 1
fi
wait_url "13flow MCP" "http://127.0.0.1:8849/healthz" 20 1
wait_url "13flow agent statistics" "http://127.0.0.1:8849/stats" 20 1
systemctl reload apache2
if ! zen_default_headers=$(curl --silent --show-error --head --max-time 5 \
  --noproxy '*' --header 'Host: unconfigured.zen.invalid' http://127.0.0.1/); then
  echo "ZEN default Host-boundary probe failed." >&2
  exit 4
fi
if ! grep -qi '^X-Zen-Node: online' <<<"$zen_default_headers"; then
  echo "Unknown Host did not reach the isolated ZEN default vhost." >&2
  exit 4
fi
# A strict ModSecurity policy may reject the deliberately invalid Host above.
# Fetch the body with an explicitly allowed hostname instead.
if ! zen_default_html=$(curl --silent --show-error --fail --max-time 5 \
  --noproxy '*' --header 'Host: toonux.org' http://127.0.0.1/arbitrary/path); then
  echo "ZEN default page probe failed for an allowed hostname." >&2
  exit 4
fi
if ! grep -Fq 'powered by Debian GNU Linux' <<<"$zen_default_html" || \
   ! grep -Fq 'runned by bluetouff' <<<"$zen_default_html"; then
  echo "ZEN default page content is incomplete." >&2
  exit 4
fi
if ! known_host_status=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --max-time 5 --noproxy '*' --header 'Host: 13flow.eu' http://127.0.0.1/); then
  echo "Named 13flow.eu vhost probe failed." >&2
  exit 4
fi
if [[ "$known_host_status" != "301" ]]; then
  echo "Expected the named 13flow.eu HTTP vhost to remain a 301, got $known_host_status." >&2
  exit 4
fi
if [[ "$zen_default_cert_name" == "zen-default" ]]; then
  zen_tls_html=$(curl --silent --show-error --fail --max-time 8 \
    --noproxy '*' --resolve toonux.org:443:127.0.0.1 https://toonux.org/arbitrary/path)
  grep -Fq 'powered by Debian GNU Linux' <<<"$zen_tls_html"
fi
trap - ERR
echo "Safe deploy complete. Run:"
echo "  sudo EXPECTED_SHA=$SHA $APP_DIR/deploy/smoke-public.sh"
