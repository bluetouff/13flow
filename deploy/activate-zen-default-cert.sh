#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

source_config=/opt/13flow/deploy/apache-zen-default.conf
active_config=/etc/apache2/sites-available/000-zen-default.conf
cert_dir=/etc/letsencrypt/live/zen-default
zen_hosts=(toonux.org toonux.com l0g.me l0g.us w2p.org)

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run this activator with sudo." >&2
  exit 2
fi
if [[ ! -f "$source_config" || -L "$source_config" || \
      ! -f "$active_config" || -L "$active_config" ]]; then
  echo "Deploy the ZEN default vhost before activating its certificate." >&2
  exit 3
fi
for cert_path in "$cert_dir/fullchain.pem" "$cert_dir/privkey.pem"; do
  if [[ ! -r "$cert_path" ]]; then
    echo "Missing dedicated ZEN default certificate file: $cert_path" >&2
    exit 3
  fi
done
for zen_host in "${zen_hosts[@]}"; do
  if ! openssl x509 -in "$cert_dir/fullchain.pem" \
    -noout -checkhost "$zen_host" >/dev/null; then
    echo "The dedicated certificate does not cover $zen_host." >&2
    exit 3
  fi
done

backup=$(mktemp /var/backups/000-zen-default.conf.before-cert.XXXXXX)
stage=$(mktemp /etc/apache2/sites-available/.000-zen-default.conf.XXXXXX)
cp -a -- "$active_config" "$backup"

rollback() {
  local rc=$?
  trap - ERR
  set +e
  if [[ -n "$stage" ]]; then
    rm -f -- "$stage"
  fi
  cp -a -- "$backup" "$active_config"
  apache2ctl configtest && systemctl reload apache2
  exit "$rc"
}
trap rollback ERR

sed 's#/live/13flow.eu/#/live/zen-default/#g' "$source_config" > "$stage"
chown root:root "$stage"
chmod 644 "$stage"
mv -f -- "$stage" "$active_config"
stage=""
apache2ctl configtest
systemctl reload apache2

for zen_host in "${zen_hosts[@]}"; do
  headers=$(curl --silent --show-error --head --fail --max-time 10 --noproxy '*' \
    --resolve "$zen_host:443:127.0.0.1" "https://$zen_host/")
  grep -qi '^X-Zen-Node: online' <<<"$headers"
done

trap - ERR
echo "Dedicated ZEN certificate active for: ${zen_hosts[*]}"
echo "Rollback copy retained at: $backup"
