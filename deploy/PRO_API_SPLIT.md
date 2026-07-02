# 13FLOW Pro API service split

Goal: keep the public `13flow.service` strictly read-only and move all Pro API writes
to a dedicated service account and gunicorn listener.

## Runtime layout

- `13flow.service`
  - user: `flowapp`
  - listens on `127.0.0.1:8000`
  - reads `/var/lib/13flow/13flow.db`
  - must not define `SMARTMONEY_PRO_API`
  - must not define `SMARTMONEY_PRO_DB`
  - must not have `ReadWritePaths=/var/lib/13flow-pro`
- `13flow-pro.service`
  - user: `flowpro`
  - listens on `127.0.0.1:8001`
  - reads `/var/lib/13flow/13flow.db`
  - writes only `/var/lib/13flow-pro/13flow-pro.db`
  - serves only the Pro API via Apache `/api/pro/`
- `13flow-mcp.service`
  - user: `flowmcp`
  - public tools use `MCP_13FLOW_API_BASE=http://127.0.0.1:8000`
  - premium tools use `MCP_13FLOW_PRO_API_BASE=http://127.0.0.1:8001`

## Install

```bash
sudo adduser --system --group --no-create-home --home /nonexistent flowpro
sudo usermod -a -G flowapp flowpro

sudo install -o root -g flowpro -m 640 /opt/13flow/deploy/13flow-pro.env /etc/13flow/13flow-pro.env
sudo install -o root -g root -m 644 /opt/13flow/deploy/13flow-pro.service /etc/systemd/system/13flow-pro.service

sudo mkdir -p /var/lib/13flow-pro
sudo chown flowpro:flowpro /var/lib/13flow-pro
sudo chmod 750 /var/lib/13flow-pro
sudo chown flowpro:flowpro /var/lib/13flow-pro/13flow-pro.db
sudo chmod 640 /var/lib/13flow-pro/13flow-pro.db

sudo systemctl daemon-reload
sudo systemctl enable --now 13flow-pro
```

## Apache

Add `deploy/apache-13flow-pro.conf` inside the TLS virtual host before the catch-all
`ProxyPass /` rule:

```apache
ProxyPass        /api/pro/ http://127.0.0.1:8001/api/pro/ retry=0 timeout=30
ProxyPassReverse /api/pro/ http://127.0.0.1:8001/api/pro/
```

Remove the old public-service Pro drop-in if present:

```bash
TS=$(date +%Y%m%d-%H%M%S)
sudo mv /etc/systemd/system/13flow.service.d/pro-api.conf \
  /etc/systemd/system/13flow.service.d/pro-api.conf.disabled-$TS
sudo systemctl daemon-reload
```

## Verify

```bash
sudo systemctl restart 13flow
sudo systemctl restart 13flow-pro
sudo systemctl restart 13flow-mcp
sleep 2

curl -fsS http://127.0.0.1:8000/api/version
curl -fsS http://127.0.0.1:8001/api/pro/v1/openapi.json | python3 -m json.tool >/dev/null
curl -fsS https://13flow.eu/api/funds | python3 -c 'import json,sys; rows=json.load(sys.stdin); print("funds", len(rows), rows[0]["label"] if rows else None)'

sudo systemctl show 13flow -p ReadWritePaths -p EnvironmentFiles
sudo systemctl show 13flow-pro -p User -p Group -p SupplementaryGroups -p ReadWritePaths
```

The production preflight fails if `13flow.service` still exposes Pro API env or keeps
writable access to `/var/lib/13flow-pro`.
