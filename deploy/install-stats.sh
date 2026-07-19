#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd -- "$script_dir/.." && pwd)
stats_user_name=flowstats
stats_output=/var/www/html/13flow-stats
htpasswd_file=/etc/apache2/13flow-stats.htpasswd
apache_fragment=/etc/apache2/13flow-stats.conf
generator=/usr/local/libexec/13flow-generate-stats
stats_unit=/etc/systemd/system/13flow-stats.service
stats_timer=/etc/systemd/system/13flow-stats.timer

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 2
fi

for source_file in \
  "$project_dir/deploy/apache-13flow-stats.conf" \
  "$project_dir/deploy/generate-stats.sh" \
  "$project_dir/deploy/smoke-private-stats.sh" \
  "$project_dir/deploy/13flow-stats.service" \
  "$project_dir/deploy/13flow-stats.timer"; do
  test -f "$source_file"
done
grep -Fq 'IncludeOptional /etc/apache2/13flow-stats.conf' \
  /etc/apache2/sites-available/13flow.conf || {
    echo "Deploy the current 13FLOW Apache vhost before installing statistics." >&2
    exit 3
  }

apt-get update
apt-get install -y --no-install-recommends apache2-utils goaccess gzip
a2enmod auth_basic authn_file headers alias dir setenvif

goaccess_help=$(/usr/bin/goaccess --help 2>&1 || true)
if [[ "$goaccess_help" != *"--anonymize-level"* ]]; then
  echo "The installed GoAccess version lacks the required IP anonymization level." >&2
  exit 3
fi

if ! getent group "$stats_user_name" >/dev/null; then
  groupadd --system "$stats_user_name"
fi
if ! id "$stats_user_name" >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/13flow-stats --create-home \
    --shell /usr/sbin/nologin --gid "$stats_user_name" --groups adm "$stats_user_name"
fi
usermod --gid "$stats_user_name" --append --groups adm "$stats_user_name"

install -d -o "$stats_user_name" -g www-data -m 2750 "$stats_output"
install -d -o root -g root -m 755 /usr/local/libexec
if ! runuser -u "$stats_user_name" -- test -r /var/log/apache2/13flow_access.log; then
  echo "The flowstats user cannot read the 13FLOW Apache access log." >&2
  exit 3
fi
for managed_path in "$htpasswd_file" "$apache_fragment" "$generator" "$stats_unit" "$stats_timer"; do
  if [[ -L "$managed_path" ]]; then
    echo "Refusing symlinked statistics runtime path: $managed_path" >&2
    exit 3
  fi
done

backup_dir=$(mktemp -d /var/backups/13flow-stats-install.XXXXXX)
managed_paths=("$htpasswd_file" "$apache_fragment" "$generator" "$stats_unit" "$stats_timer")
for managed_path in "${managed_paths[@]}"; do
  managed_name=$(basename -- "$managed_path")
  if [[ -e "$managed_path" ]]; then
    cp -a -- "$managed_path" "$backup_dir/$managed_name"
    touch "$backup_dir/$managed_name.existed"
  fi
done
password_stage=""
report_probe=""
rollback() {
  local rc=$?
  trap - ERR
  set +e
  echo "Statistics installation failed; restoring the previous runtime files." >&2
  for managed_path in "${managed_paths[@]}"; do
    managed_name=$(basename -- "$managed_path")
    if [[ -f "$backup_dir/$managed_name.existed" ]]; then
      cp -a -- "$backup_dir/$managed_name" "$managed_path"
    else
      rm -f -- "$managed_path"
    fi
  done
  rm -f -- "${password_stage:-}"
  rm -f -- "${report_probe:-}"
  systemctl daemon-reload
  apache2ctl configtest && systemctl reload apache2
  rm -rf -- "$backup_dir"
  exit "$rc"
}
trap rollback ERR

login=""
while [[ ! "$login" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; do
  read -r -p "Stats username [bluetouff]: " login
  login=${login:-bluetouff}
done

password=""
confirmation=""
while [[ ${#password} -lt 16 || "$password" != "$confirmation" ]]; do
  read -r -s -p "Stats password, 16 characters minimum: " password
  echo
  read -r -s -p "Confirm stats password: " confirmation
  echo
  if [[ ${#password} -lt 16 ]]; then
    echo "Password is too short." >&2
  elif [[ "$password" != "$confirmation" ]]; then
    echo "Passwords do not match." >&2
  fi
done

password_stage=$(mktemp /etc/apache2/.13flow-stats.htpasswd.XXXXXX)
if [[ -f "$htpasswd_file" ]]; then
  cp -- "$htpasswd_file" "$password_stage"
  printf '%s\n' "$password" | htpasswd -i -B -C 12 "$password_stage" "$login"
else
  printf '%s\n' "$password" | htpasswd -i -B -C 12 -c "$password_stage" "$login"
fi
basic_auth=$(printf '%s:%s' "$login" "$password" | base64 --wrap=0)
unset password confirmation
chown root:www-data "$password_stage"
chmod 640 "$password_stage"
mv -f -- "$password_stage" "$htpasswd_file"
password_stage=""

install -o root -g root -m 755 "$project_dir/deploy/generate-stats.sh" "$generator"
install -o root -g root -m 644 "$project_dir/deploy/13flow-stats.service" "$stats_unit"
install -o root -g root -m 644 "$project_dir/deploy/13flow-stats.timer" "$stats_timer"
install -o root -g root -m 644 "$project_dir/deploy/apache-13flow-stats.conf" "$apache_fragment"

apache2ctl configtest
systemctl daemon-reload
systemctl enable --now 13flow-stats.timer
systemctl start 13flow-stats.service
test -s "$stats_output/index.html"
test -s "$stats_output/goaccess.css"
test -s "$stats_output/goaccess.js"
systemctl reload apache2

"$project_dir/deploy/smoke-private-stats.sh"

authenticated_headers=$(
  printf 'header = "Authorization: Basic %s"\n' "$basic_auth" | \
    curl --config - --silent --show-error --head --max-time 10 https://13flow.eu/stats/
)
authenticated_status=$(printf '%s\n' "$authenticated_headers" | awk 'NR==1 {print $2}')
if [[ "$authenticated_status" != "200" ]]; then
  echo "Expected authenticated /stats/ to return HTTP 200, got ${authenticated_status:-unknown}." >&2
  exit 4
fi
authenticated_csp_count=$(grep -ci '^Content-Security-Policy:' <<<"$authenticated_headers")
if [[ "$authenticated_csp_count" != "1" ]] || \
   ! grep -qi "^Content-Security-Policy: .*script-src 'self' 'unsafe-inline' 'unsafe-eval'" \
     <<<"$authenticated_headers"; then
  echo "Authenticated /stats/ does not expose the single GoAccess CSP." >&2
  exit 4
fi

report_probe=$(mktemp /var/lib/13flow-stats/.report-probe.XXXXXX)
printf 'header = "Authorization: Basic %s"\n' "$basic_auth" | \
  curl --config - --silent --show-error --fail --max-time 10 \
    https://13flow.eu/stats/ --output "$report_probe"
unset basic_auth
grep -q 'goaccess.css' "$report_probe"
grep -q 'goaccess.js' "$report_probe"
rm -f -- "$report_probe"
report_probe=""

trap - ERR
rm -rf -- "$backup_dir"
echo "13FLOW private statistics installed at https://13flow.eu/stats/"
