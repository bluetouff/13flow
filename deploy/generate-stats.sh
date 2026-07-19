#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

log_directory=${LOG_DIRECTORY:-/var/log/apache2}
active_log=${ACCESS_LOG:-$log_directory/13flow_access.log}
output_directory=${STATS_OUTPUT_DIR:-/var/www/html/13flow-stats}

if [[ -L "$active_log" || ! -f "$active_log" || ! -r "$active_log" ]]; then
  echo "Refusing unreadable or non-regular access log: $active_log" >&2
  exit 2
fi
if [[ -L "$output_directory" || ! -d "$output_directory" || ! -w "$output_directory" ]]; then
  echo "Refusing unsafe or unwritable output directory: $output_directory" >&2
  exit 2
fi

temporary_report=$(mktemp --suffix=.html "$output_directory/.index.XXXXXX")
cleanup() { rm -f -- "$temporary_report"; }
trap cleanup EXIT

mapfile -t compressed_logs < <(
  find "$log_directory" -maxdepth 1 -type f -name '13flow_access.log.*.gz' -print | sort -V -r
)

{
  for log_file in "${compressed_logs[@]}"; do
    gzip --decompress --stdout -- "$log_file"
  done
  if [[ -f "$active_log.1" && ! -L "$active_log.1" && -r "$active_log.1" ]]; then
    cat -- "$active_log.1"
  fi
  cat -- "$active_log"
} | /usr/bin/goaccess - \
  --no-global-config \
  --log-format=COMBINED \
  --anonymize-ip \
  --anonymize-level=2 \
  --no-query-string \
  --keep-last=90 \
  --external-assets \
  --html-report-title='13FLOW, statistiques des 90 derniers jours' \
  --html-prefs='{"theme":"darkPurple","perPage":20,"layout":"vertical","showTables":true}' \
  --tz=Europe/Paris \
  --no-progress \
  --no-parsing-spinner \
  --output="$temporary_report"

chmod 640 "$temporary_report"
mv -f -- "$temporary_report" "$output_directory/index.html"
for asset in goaccess.css goaccess.js; do
  test -f "$output_directory/$asset"
  chmod 640 "$output_directory/$asset"
done
trap - EXIT
