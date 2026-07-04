# INSTALL_SERVER — deploy SmartMoney on a server

Step-by-step production deployment on **Ubuntu 22.04 / 24.04**. Adjust paths/commands for
other distros.

> ⚠️ **Read this first.** The API has **no built-in authentication** (this is a deliberate
> pre-launch gap — see `SECURITY.md`, finding #7). You must put it behind an authenticated
> reverse proxy with TLS. This guide does exactly that. **Never expose gunicorn's port
> directly to the internet.**

**Architecture this guide builds:**
```
Internet ──TLS──▶ nginx (443, HTTP Basic Auth) ──▶ gunicorn (127.0.0.1:8000) ──▶ SmartMoney ──▶ SQLite
                                                     ▲
                       systemd timer: run.py --alerts-run  (sync + deliver alerts)
```

---

## 0. Assumptions
- A fresh Ubuntu server with `sudo`.
- A domain pointing at it (e.g. `smartmoney.example.com`) for TLS.
- You have the project as `smartmoney.zip`.

## 1. Install system packages
```bash
sudo apt update
sudo apt install -y python3-venv python3-pip nginx apache2-utils
sudo apt install -y certbot python3-certbot-nginx     # for TLS (optional but recommended)
```

## 2. Create a service user and directories
```bash
sudo useradd --system --create-home --home-dir /opt/smartmoney --shell /usr/sbin/nologin smartmoney
sudo mkdir -p /var/lib/smartmoney /etc/smartmoney
sudo chown smartmoney:smartmoney /var/lib/smartmoney
```

## 3. Deploy the code
```bash
sudo unzip smartmoney.zip -d /opt/
sudo chown -R smartmoney:smartmoney /opt/smartmoney
cd /opt/smartmoney
sudo -u smartmoney python3 -m venv .venv
sudo -u smartmoney .venv/bin/pip install -e . gunicorn defusedxml
```
Confirm `dashboard.html` sits at `/opt/smartmoney/dashboard.html` (it ships at the project root).

## 4. Configure secrets (environment file)
```bash
sudo tee /etc/smartmoney/smartmoney.env >/dev/null <<'EOF'
SEC_UA=SmartMoney/1.0 you@example.com
OPENFIGI_APIKEY=
SMARTMONEY_DB=/var/lib/smartmoney/smartmoney.db
# SMARTMONEY_VALUE=1            # uncomment to enable per-request live valuation
# Email alerts (optional):
# SMTP_HOST=smtp.example.com
# SMTP_PORT=587
# SMTP_USER=alerts@example.com
# SMTP_PASS=...
# SMTP_FROM=alerts@example.com
EOF
sudo chown smartmoney:smartmoney /etc/smartmoney/smartmoney.env
sudo chmod 600 /etc/smartmoney/smartmoney.env
```

## 5. Load initial data (one-off, may take a while)
This pulls every tracked fund from EDGAR and resolves tickers; it respects SEC rate limits.
```bash
sudo -u smartmoney -H bash -c '
  cd /opt/smartmoney
  set -a; . /etc/smartmoney/smartmoney.env; set +a
  .venv/bin/python run.py --sync-all --enrich --db "$SMARTMONEY_DB"
  .venv/bin/python run.py --coverage --db "$SMARTMONEY_DB"
'
```

## 5b. Pro access
Core V1 has no browser accounts or self-serve checkout. Paid access is issued
with scoped Pro API keys from the Pro control-plane DB; see
`docs/PRO_API_ONBOARDING.md`.

## 6. Run the API as a systemd service (gunicorn)
```bash
sudo tee /etc/systemd/system/smartmoney-api.service >/dev/null <<'EOF'
[Unit]
Description=SmartMoney API (gunicorn)
After=network.target

[Service]
User=smartmoney
Group=smartmoney
WorkingDirectory=/opt/smartmoney
EnvironmentFile=/etc/smartmoney/smartmoney.env
ExecStart=/opt/smartmoney/.venv/bin/gunicorn --workers 3 --bind 127.0.0.1:8000 wsgi:app
Restart=on-failure
# hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/smartmoney

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now smartmoney-api
curl -s http://127.0.0.1:8000/api/funds | head -c 200    # sanity check
```

## 7. nginx reverse proxy + Basic Auth + TLS
Create a login (minimum viable auth — swap for SSO/OAuth at the proxy if you have it):
```bash
sudo htpasswd -c /etc/nginx/.smartmoney_htpasswd youruser
```
Site config:
```bash
sudo tee /etc/nginx/sites-available/smartmoney >/dev/null <<'EOF'
server {
    listen 80;
    server_name smartmoney.example.com;

    location / {
        auth_basic           "SmartMoney";
        auth_basic_user_file /etc/nginx/.smartmoney_htpasswd;

        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        client_max_body_size 1m;
    }
}
EOF
sudo ln -s /etc/nginx/sites-available/smartmoney /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```
Add TLS (certbot rewrites the site to 443 with an HTTP→HTTPS redirect):
```bash
sudo certbot --nginx -d smartmoney.example.com
```

## 8. Firewall
```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```
Port 8000 stays internal and is never opened.

## 8b. Billing
Stripe and mock checkout are not part of Core V1. Do not configure public
checkout on this deployment.

## 9. Scheduled sync + alert delivery (systemd timer)
`--alerts-run` syncs the funds that have subscriptions, then delivers any new-filing alerts.
```bash
sudo tee /etc/systemd/system/smartmoney-alerts.service >/dev/null <<'EOF'
[Unit]
Description=SmartMoney sync + alert dispatch
After=network.target

[Service]
Type=oneshot
User=smartmoney
WorkingDirectory=/opt/smartmoney
EnvironmentFile=/etc/smartmoney/smartmoney.env
ExecStart=/opt/smartmoney/.venv/bin/python run.py --alerts-run --db /var/lib/smartmoney/smartmoney.db
EOF

sudo tee /etc/systemd/system/smartmoney-alerts.timer >/dev/null <<'EOF'
[Unit]
Description=Run SmartMoney sync + alerts hourly

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now smartmoney-alerts.timer
```
13F filings have a 45-day lag, so `hourly` (or even `daily`) is more than enough. To also
refresh every tracked fund (not just subscribed ones), add a second oneshot service running
`run.py --sync-all --enrich --db ...`. Optionally add a **weekly** timer running
`run.py --resolve-sweep --db ...` to re-attempt the unresolved CUSIP tail.

## 10. Logs & operations
```bash
journalctl -u smartmoney-api -f
journalctl -u smartmoney-alerts --since today
systemctl list-timers | grep smartmoney
```

## 11. Backups
The whole database is one SQLite file. Take consistent snapshots with `.backup`:
```bash
sudo -u smartmoney sqlite3 /var/lib/smartmoney/smartmoney.db \
  ".backup '/var/lib/smartmoney/backup-$(date +%F).db'"
```
(Don't just `cp` the live `.db` — in WAL mode you'd also need the `-wal`/`-shm` files.
`.backup` handles this correctly.)

## 12. Updating
```bash
cd /opt/smartmoney
sudo -u smartmoney unzip -o /path/to/new/smartmoney.zip -d /opt/   # or git pull
sudo -u smartmoney .venv/bin/pip install -e .
sudo systemctl restart smartmoney-api
```

---

## Production hardening checklist (recap of SECURITY.md)
- [ ] gunicorn bound to `127.0.0.1` only; never expose it directly.
- [ ] Reverse proxy enforces auth + TLS (Basic Auth here is the floor — prefer SSO/OAuth).
- [ ] `debug` is off (it is, by default — never enable the Werkzeug debugger in prod).
- [ ] `defusedxml` installed (step 3) — hardens 13F XML parsing.
- [ ] Put outbound **webhook/email** traffic behind an egress allowlist/proxy — this closes
      the residual DNS-rebinding gap the app-level SSRF guard can't fully cover.
- [ ] `chmod 600` on the env file (step 4); restrict the DB file; rotate API keys.
- [ ] Browser auth, HIBP, SMTP verification and Stripe checkout are intentionally absent
      from Core V1.

## Scaling note
SQLite is fine for this read-heavy workload: WAL mode allows concurrent readers alongside the
single periodic writer (the alerts/sync timer). If you outgrow it, migrating to Postgres is
mechanical — the schema is standard SQL (window functions + one view) and the data layer is
plain SQL with no ORM lock-in.
