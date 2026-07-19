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
- `get_research_readiness`
- `list_funds`
- `get_fund`
- `get_stock`
- `preview_watchlist`
- `discover_watchlist`
- `get_confluence_signals`
- `get_signal_history`
- `get_confluence_methodology`
- `get_data_quality`
- `get_agent_stats`

Public resources include `13flow://product-status`, `13flow://research-readiness` and
`13flow://agent-stats`.
They expose the current validation, data-quality and operator-review boundaries without
turning the retired public commercial offer into an agent-facing contract.

`get_agent_stats`, the resource and `GET /stats` expose the same privacy-safe counters used
by `/agents`, `/fr/agents` and `/api/agent-stats`. They store UTC daily aggregates only:
fixed client families, registered tool names, handler outcomes and duration counters. The
telemetry store never retains IP addresses, User-Agents, client versions, raw client names,
arguments, prompts, responses or keys. An initialization is a handshake, not a unique user.

Context-heavy tools are bounded by default. `list_funds` returns compact paginated rows,
fund and ticker tools expose explicit result-window counts, and watchlist tools limit
movement evidence while keeping canonical web and API URLs in structured output.

Optional Pro tools are read-only, but are not registered or advertised unless an operator
deliberately sets `MCP_PRO_TOOLS_ENABLED=1`:

- `pro.list_funds`
- `pro.get_fund`
- `pro.get_data_quality`

When that separate profile is enabled, calls are accepted only when one of these conditions
is true:

1. The request includes a valid existing 13FLOW Pro API key in `X-13FLOW-Key`.
2. The request includes an x402 v2 `PAYMENT-SIGNATURE` that is verified and settled through
   the configured facilitator.

Client `Authorization` headers are never accepted as MCP Pro credentials and are never
forwarded downstream. This prevents an MCP OAuth token from being passed through to a
different API audience.

If neither condition holds, the server returns HTTP `402 Payment Required` with a
`PAYMENT-REQUIRED` header. The body remains JSON-RPC shaped so MCP clients can display a
clear failure.

## x402 policy

13FLOW can use x402 as a machine-to-machine payment gate for individual Pro MCP tool calls.
It is disabled by default, absent from the Registry profile and not part of the current public
offer. Enabling it also requires an explicit Pro profile and a fresh operational security
review.
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
- if `MCP_13FLOW_INTERNAL_PRO_TOKEN_FILE` is absent, no x402 paid access is granted;
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
# Required only for an explicitly enabled isolated Pro profile.
MCP_13FLOW_PRO_API_BASE=http://127.0.0.1:8001
MCP_ALLOWED_HOSTS=13flow.eu,www.13flow.eu,127.0.0.1,localhost
MCP_ALLOWED_ORIGINS=https://13flow.eu,https://www.13flow.eu
MCP_RATE_MAX=120
MCP_MAX_RATE_BUCKETS=10000
MCP_MAX_BODY=1048576
MCP_MAX_UPSTREAM_BODY=8388608
MCP_MAX_IN_FLIGHT=32
MCP_MAX_CONNECTIONS=128
MCP_MAX_PAYMENT_CACHE=500
MCP_STATS_RETENTION_DAYS=30
# Production is pinned to this systemd StateDirectory path.
MCP_STATS_FILE=/var/lib/13flow-mcp/agent-stats.json
MCP_API_TIMEOUT_MS=10000
MCP_REQUEST_TIMEOUT_MS=15000
MCP_GIT_SHA=<deploy sha>

# Keep this disabled on the public Registry daemon.
MCP_PRO_TOOLS_ENABLED=0

# Optional isolated profile only (do not uncomment on the Registry daemon):
# MCP_PRO_TOOLS_ENABLED=1
# MCP_X402_ENABLED=1
# MCP_X402_SCHEME=exact
# MCP_X402_NETWORK=eip155:8453
# MCP_X402_PRICE=$0.05
# MCP_X402_PAY_TO=0x...
# MCP_X402_ASSET=<optional asset identifier>
# MCP_X402_FACILITATOR_URL=https://...
# Production secrets must use root-owned files, never inline environment values.
# MCP_X402_FACILITATOR_AUTH_FILE=/etc/13flow/mcp-facilitator-auth

# Server-side Pro credential used only after a verified x402 settlement.
# Prefer the _FILE form with a root-readable/flowmcp-readable 0640 secret.
# MCP_13FLOW_INTERNAL_PRO_TOKEN_FILE=/etc/13flow/mcp-internal-pro-token
```

`MCP_X402_TEST_MODE=1` is only for local tests. It must not be enabled in production.

## Local run

```bash
cd mcp-server
npm ci
npm run check
MCP_13FLOW_API_BASE=https://13flow.eu npm start
```

In another terminal:

```bash
cd mcp-server
URL=http://127.0.0.1:8849/mcp npm test
```

The test client verifies transport headers, Host/Origin rejection, JSON-RPC batch rejection,
protocol version handling, body limits, tool/resource discovery, read-only annotations,
result-size bounds, canonical provenance links and absence of Pro tools by default. Run
`npm run test:config` and `npm run test:security` for fail-closed configuration, oversized
upstream response and concurrency-cap regressions. `npm run test:telemetry` verifies
persistence, retention, metric reconciliation and absence of raw client/tool data. Set
`SKIP_HEALTH=1` only when testing
through a public proxy that intentionally keeps `/healthz` private.

## Official MCP Registry

The root [`server.json`](../server.json) declares the public Streamable HTTP endpoint as
`io.github.bluetouff/13flow`. Validate the manifest before publication:

```bash
cd /opt/13flow
mcp-publisher validate
```

After the matching server version is deployed and the public smoke passes, authenticate as
late as possible, publish once, then verify the exact version and remote URL:

```bash
mcp-publisher login github
mcp-publisher publish
cd mcp-server
npm run registry:verify -- --version 1.0.0
```

Registry publication is metadata publication. It does not deploy the MCP daemon, so the live
endpoint and Registry entry must be verified independently.

Prove that the default Registry profile does not execute a private tool:

```bash
curl -i http://127.0.0.1:8849/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"pro.list_funds","arguments":{}}}'
```

Expected result: a JSON-RPC unknown-tool error. `pro.list_funds` must also be absent from
`tools/list`.

An isolated, explicitly enabled Pro profile accepts only the dedicated key header:

```bash
printf "API token: "
read -r -s TOKEN
printf "\n"

curl -fsS http://127.0.0.1:8849/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "X-13FLOW-Key: $TOKEN" \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"pro.list_funds","arguments":{}}}'

unset TOKEN
```

## Production deployment

Install dependencies once:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin flowmcp
cd /opt/13flow/mcp-server
sudo npm ci --omit=dev
sudo chown -R root:flowmcp /opt/13flow/mcp-server
sudo chmod o+x /opt/13flow
```

Install the systemd unit from `deploy/13flow-mcp.service`, set environment variables in
`/etc/13flow/13flow-mcp.env`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now 13flow-mcp
curl -fsS http://127.0.0.1:8849/healthz | python3 -m json.tool
curl -fsS http://127.0.0.1:8849/stats | python3 -m json.tool
```

The unit creates `/var/lib/13flow-mcp` as a mode `0700` systemd `StateDirectory` and grants
the daemon no other persistent write path. `agent-stats.json` is written mode `0600` through
a bounded temporary file and rename; production refuses an alternate telemetry path.

Apache must allow POST only on `/api/mcp` and proxy that path to the Node service. Keep the
main site method allow-list in place so other public endpoints remain read-only. The supplied
vhost caps the request at 1 MiB and removes client-supplied forwarding headers before proxying.
The supplied systemd unit denies non-loopback networking; enabling an external x402
facilitator therefore requires a deliberate network-policy override and a new review. It
also removes every Pro/x402 activation and secret variable after reading the environment file,
so this public unit cannot be converted into the private profile by an accidental env edit.

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
