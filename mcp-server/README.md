# 13FLOW MCP Server

This directory contains the production MCP surface for 13FLOW.

It is intentionally separate from the Flask application:

- Flask serves the public website, public API and Pro API.
- This Node service exposes a Model Context Protocol endpoint over Streamable HTTP.
- Apache publishes the service at `https://13flow.eu/api/mcp`.
- The service calls the local Flask API at `http://127.0.0.1:8000` and never touches EDGAR.

The design mirrors the l0g.fr MCP architecture: stateless transport, one MCP server per
request, bounded JSON bodies, Host/Origin validation, per-IP rate limiting, health checks,
structured tool output and read-only annotations.

## Capability boundary

Public tools are free and read-only:

- `get_live_status`
- `get_product_status`
- `list_funds`
- `get_fund`
- `get_stock`
- `get_confluence_signals`
- `get_signal_history`
- `get_confluence_methodology`
- `get_data_quality`
- `get_payment_policy`

Public resources include `13flow://product-status`, a machine-readable go-to-market
boundary that states what can be sold now, what must not be claimed yet, and why full
quantitative validation is still blocked until a vetted adjusted-price CSV is imported.

Premium tools are read-only but gated:

- `pro.list_funds`
- `pro.get_fund`
- `pro.get_data_quality`

Premium calls are accepted only when one of these conditions is true:

1. The request includes a valid existing 13FLOW Pro API credential:
   `Authorization: Bearer <token>` or `X-13FLOW-Key: <token>`.
2. The request includes an x402 v2 `PAYMENT-SIGNATURE` that is verified and settled through
   the configured facilitator.

If neither condition holds, the server returns HTTP `402 Payment Required` with a
`PAYMENT-REQUIRED` header. The body remains JSON-RPC shaped so MCP clients can display a
clear failure.

## x402 policy

13FLOW uses x402 as a machine-to-machine payment gate for individual Pro MCP tool calls.
The server advertises one accepted payment option for each paid tool:

- scheme: `exact` by default
- network: `eip155:8453` by default
- price: `MCP_X402_PRICE`
- destination: `MCP_X402_PAY_TO`
- resource: `https://13flow.eu/api/mcp#<tool>`

The server is fail-closed:

- if `MCP_X402_ENABLED=1` is absent, no x402 paid access is granted;
- if `MCP_X402_PAY_TO` is absent, no x402 paid access is granted;
- if `MCP_X402_FACILITATOR_URL` is absent, no x402 paid access is granted;
- if `MCP_13FLOW_INTERNAL_PRO_TOKEN` or `MCP_13FLOW_INTERNAL_PRO_TOKEN_FILE` is absent,
  no x402 paid access is granted;
- a payment must pass facilitator `/verify` and `/settle` before the MCP tool runs.

The implementation follows x402 v2 HTTP headers:

- `PAYMENT-REQUIRED`: server to client, base64 JSON payment requirement;
- `PAYMENT-SIGNATURE`: client to server, base64 JSON payment payload;
- `PAYMENT-RESPONSE`: server to client, base64 JSON settlement response.

The optional payment-identifier extension is accepted. When present, the server stores a
short-lived fingerprint-bound settlement cache so retries for the exact same request do not
execute as a different paid operation. Use Redis or another shared store before running
multiple MCP replicas behind a load balancer.

## Environment

```bash
MCP_HOST=127.0.0.1
MCP_PORT=8849
MCP_PATH=/mcp
MCP_PUBLIC_SITE=https://13flow.eu
MCP_13FLOW_API_BASE=http://127.0.0.1:8000
# Optional but recommended in production: premium tools call the isolated Pro API service.
MCP_13FLOW_PRO_API_BASE=http://127.0.0.1:8001
MCP_ALLOWED_HOSTS=13flow.eu,www.13flow.eu,127.0.0.1,localhost
MCP_ALLOWED_ORIGINS=https://13flow.eu,https://www.13flow.eu
MCP_RATE_MAX=120
MCP_GIT_SHA=<deploy sha>

MCP_X402_ENABLED=1
MCP_X402_SCHEME=exact
MCP_X402_NETWORK=eip155:8453
MCP_X402_PRICE=$0.05
MCP_X402_PAY_TO=0x...
MCP_X402_ASSET=<optional asset identifier>
MCP_X402_FACILITATOR_URL=https://...
MCP_X402_FACILITATOR_AUTH="Bearer ..."

# Server-side Pro credential used only after a verified x402 settlement.
# Prefer the _FILE form with a root-readable/flowmcp-readable 0640 secret.
MCP_13FLOW_INTERNAL_PRO_TOKEN_FILE=/etc/13flow/mcp-internal-pro-token
```

`MCP_X402_TEST_MODE=1` is only for local tests. It must not be enabled in production.

## Local run

```bash
cd mcp-server
npm install
npm run check
MCP_13FLOW_API_BASE=https://13flow.eu npm start
```

In another terminal:

```bash
cd mcp-server
URL=http://127.0.0.1:8849/mcp npm test
```

Probe the x402 gate without a Pro key:

```bash
curl -i http://127.0.0.1:8849/mcp \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"pro.list_funds","arguments":{}}}'
```

Expected result: `HTTP/1.1 402 Payment Required` with a `PAYMENT-REQUIRED` header.

Probe an existing Pro key path:

```bash
printf "API token: "
read -r -s TOKEN
printf "\n"

curl -fsS http://127.0.0.1:8849/mcp \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"pro.list_funds","arguments":{}}}'

unset TOKEN
```

## Production deployment

Install dependencies once:

```bash
cd /opt/13flow/mcp-server
sudo npm ci --omit=dev
sudo chown -R root:flowapp /opt/13flow/mcp-server
```

Install the systemd unit from `deploy/13flow-mcp.service`, set environment variables in
`/etc/13flow/13flow-mcp.env`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now 13flow-mcp
curl -fsS http://127.0.0.1:8849/healthz | python3 -m json.tool
```

Apache must allow POST only on `/api/mcp` and proxy that path to the Node service. Keep the
main site method allow-list in place so other public endpoints remain read-only.

## Methodology contract

The MCP server is only a delivery protocol. It does not redefine the investment signal.

Confluence v1 remains the frozen public research contract:

- score is an ordinal heuristic rank, not a probability or expected return;
- default weights are heuristic until live historical validation is published;
- live status, data quality, accession counts and coverage stay public;
- the append-only signal history is the audit trail for signal revisions;
- Pro output must keep enough metadata to reproduce the selected filing, basis date,
  positions, moves and data-quality caveats.

No MCP tool calls EDGAR directly. Freshness and 13F coverage come from the already published
market database and cache files.
