# 13FLOW Pro API onboarding runbook

This runbook is for operator-issued Pro API access. The public open build does
not expose self-serve checkout or account management.

## Controlled pilot scope

The default paid path is a controlled pilot only. Do not add browser auth,
self-serve payment, CRM sync, public prospect storage or custom dashboards to
close the first sales conversations.

Pilot access includes:

- public evidence surfaces and OpenAPI contracts;
- Pro API read endpoints for funds, fund detail, data quality and watchlist
  discovery;
- saved workspace watchlists, snapshots, reports and exports;
- MCP public tools and Pro tools that fail closed without a valid key;
- operator-issued, expiring API keys with rate limits, audit rows and rotation.

Pilot access excludes:

- investment advice, price targets, validated alpha or performance guarantee;
- public signup, checkout, invoices or automated billing;
- customer `admin:read` scope;
- redistribution rights unless covered by explicit written terms;
- bespoke data expansion beyond the current quality-gated 13F, Form 4 and
  confluence boundary.

Before issuing a key, the release-readiness endpoint must return `go: true`:

```bash
curl -fsS https://13flow.eu/api/pro/v1/admin/release-readiness \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  | python3 -m json.tool
```

## 1. Capture the inbound request

The public `/pro` page routes buyers to an operator-reviewed access request. Ask
for these fields before quoting or issuing a key:

- organization name and billing contact;
- intended workflow: research desk, data pipeline, MCP agent, monitoring;
- required scopes and expected request volume;
- preferred token delivery channel;
- expiry, rotation and revocation expectations;
- confirmation that 13FLOW is a research screen, not investment advice.

The same checklist, qualification questions and reply template are exposed in:

```bash
curl -fsS https://13flow.eu/api/pro-offer | python3 -m json.tool
```

Suggested reply structure:

```text
Thanks for the 13FLOW Pro API request.

Before I issue a scoped pilot key, please confirm:
- Organization / billing contact:
- Workflow:
- Expected request volume:
- Required scopes:
- Preferred secure token delivery channel:
- Rotation / expiry expectation:
- You accept the current validation boundary:
```

Create a local operator note before issuing a key:

```text
organization:
contact:
package: Technical pilot review | API integration review | MCP integration review
workflow:
scopes: funds:read,quality:read,workspace:write
rate_limits: 120/min, 10000/day
token_delivery_channel:
expires_at:
rotation_due_at:
key_id:
first_probe_status: pending
audit_verified_at:
boundary_acknowledged: false
release_readiness_go: false
```

## 2. Select the access package

Use one of the public packages exposed by `/api/pro-offer`:

- **Technical pilot review** — one bounded evaluator checking whether 13FLOW
  fits a real workflow.
- **API integration review** — internal dashboard, notebook or data-pipeline
  evaluation after the first pilot probes.
- **MCP integration review** — agent workflow evaluation where Pro tools must
  fail closed without a key.

No package has public pricing in this repository. Do not expose public
self-serve checkout, enterprise-style offers, pricing notes, prospect emails or
marketing correspondence in GitHub.

The maintainability gate for what belongs in the first paid pilot is
`docs/CORE_V1_BOUNDARY.md`.

Pro, MCP and redistribution terms live in
`docs/PRO_MCP_REDISTRIBUTION_TERMS.md`.

## 3. Qualify the account

Record before creating a key:

- organization and label;
- intended workflow: research desk, data pipeline, MCP agent, monitoring;
- required scopes: usually `funds:read,quality:read,workspace:write`;
- rate limits;
- expiry and rotation cadence;
- delivery channel for the plaintext token.

Do not promise validated alpha, expected returns, probabilistic scores, complete
insider-only coverage, production x402 access, or full quantitative validation.
The validation builder can join a reviewed local Form 4 transaction artifact via
`--validation-form4`, but that is a feature-contract capability, not a published
performance claim.
The public boundary is:

```bash
curl -fsS https://13flow.eu/api/product-status | python3 -m json.tool
```

## 4. Operator preflight

Run these checks on the deployed SHA before creating or renewing a customer key:

```bash
SHA=<deployed-sha>

sudo EXPECTED_SHA="$SHA" /opt/13flow/deploy/smoke-public.sh
sudo EXPECTED_SHA="$SHA" PRO_TOKEN="$PRO_TOKEN" /opt/13flow/deploy/smoke-pro-workspace.sh
sudo EXPECTED_SHA="$SHA" /opt/13flow/deploy/smoke-pro-key-lifecycle.sh

curl -fsS https://13flow.eu/api/pro/v1/admin/release-readiness \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  | python3 -m json.tool
```

Hold issuance if release readiness returns blockers, if data quality is not
fail-closed, or if the buyer asks for `admin:read`, self-serve checkout,
redistribution, investment advice or unsupported validation claims.

## 5. Create the key

Run on production:

```bash
sudo /opt/13flow/.venv/bin/python /opt/13flow/run.py \
  --create-api-key "Client Label" \
  --pro-db /var/lib/13flow-pro/13flow-pro.db \
  --api-key-scopes funds:read,quality:read,workspace:write \
  --api-key-rate-per-min 120 \
  --api-key-rate-per-day 10000 \
  --api-key-expires-days 30 \
  --api-key-rotation-days 21
```

The plaintext token is shown once. Store only the key id in the operator notes;
do not paste the token into tickets, Git, logs or chat.

After creation, confirm non-secret operator evidence:

```bash
sudo /opt/13flow/.venv/bin/python /opt/13flow/run.py \
  --pro-db /var/lib/13flow-pro/13flow-pro.db \
  --list-operator-events \
  --operator-events-limit 10
```

## 6. First client probes

Use the customer's token without echoing it:

```bash
printf "API token: "
read -r -s TOKEN
printf "\n"

curl -fsS -H "Authorization: Bearer $TOKEN" \
  https://13flow.eu/api/pro/v1/status

curl -fsS -H "Authorization: Bearer $TOKEN" \
  https://13flow.eu/api/pro/v1/funds \
  -o /tmp/13flow-pro-funds.json

curl -fsS -H "Authorization: Bearer $TOKEN" \
  "https://13flow.eu/api/pro/v1/fund/0001067983?include_holds=0&limit_positions=20&limit_moves=50" \
  -o /tmp/13flow-pro-fund-sample.json

unset TOKEN
```

The bounded fund-detail call must include `positions_total`,
`positions_returned`, `changes_total` and `changes_returned` so clients can
detect truncation deterministically.

Pilot handoff is only complete when the buyer confirms they can parse:

- the status response;
- the funds response;
- one bounded fund-detail response;
- truncation counters;
- data-quality warnings;
- workspace overview, saved watchlist creation and export responses if
  `workspace:write` is included.

## 7. MCP probe

```bash
curl -fsS https://13flow.eu/api/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_product_status","arguments":{}}}' \
  | python3 -m json.tool
```

Then test a Pro tool with the token:

```bash
printf "API token: "
read -r -s TOKEN
printf "\n"

curl -fsS https://13flow.eu/api/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Authorization: Bearer $TOKEN" \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"pro.list_funds","arguments":{}}}' \
  | python3 -m json.tool

unset TOKEN
```

Without a token or configured x402 payment, Pro MCP tools must fail closed.

## 8. Audit verification

```bash
sudo sqlite3 /var/lib/13flow-pro/13flow-pro.db \
  "SELECT key_id, method, route, status, at FROM api_audit ORDER BY id DESC LIMIT 20;"
```

The new key id should appear on successful requests. Denied requests and
rate-limited requests should also create audit rows.

## 9. Customer handoff

Send normal-channel notes without token material:

- key id, label, scopes, expiry and rotation date;
- link to `/pro/onboarding`, `/pro/workspace`, `/api/pro/v1/openapi.json` and
  `/legal/pro-api`;
- the customer-safe `curl` probes from `/api/pro/v1/admin/buyer-handoff`;
- the statement that 13FLOW is a research screen over public filings, not
  investment advice.

The token itself goes only through the customer-approved secure channel and is
not repeated in chat, email archives, tickets, URLs or browser storage.

## 10. Production preflight

Use the smoke scripts and release-readiness endpoint in section 4 for routine
production go/no-go checks. Keep `run.py --preflight` for local operator
diagnostics when you need a deeper DB-level check.

## 11. Rotation and revocation

List keys:

```bash
sudo /opt/13flow/.venv/bin/python /opt/13flow/run.py \
  --list-api-keys \
  --pro-db /var/lib/13flow-pro/13flow-pro.db
```

Revoke a key:

```bash
sudo /opt/13flow/.venv/bin/python /opt/13flow/run.py \
  --revoke-api-key <key_id> \
  --pro-db /var/lib/13flow-pro/13flow-pro.db
```

Re-test with the revoked token and expect `401`.

## 12. Operational boundaries

- One active key per institution or internal service.
- Keep public `13flow.service` read-only and without Pro DB write path.
- Keep `13flow-pro.service` as the only web-facing writer to
  `/var/lib/13flow-pro/13flow-pro.db`.
- Do not enable x402 until `MCP_X402_PAY_TO`, `MCP_X402_FACILITATOR_URL` and
  the internal Pro token are configured and tested.
- Do not relaunch external historical-price or Form 4 fan-out from production to
  satisfy full validation. Import vetted local artifacts and validate them offline.
- After a long Form 4 export, follow `docs/POST_RUN_FORM4_VALIDATION.md` before
  deploying queued code, validating the CSV or building a joined Confluence
  dataset.
