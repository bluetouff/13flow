# 13FLOW Pro API onboarding runbook

This runbook is for operator-issued Pro API access. The public open build does
not expose self-serve checkout or account management.

## 1. Capture the inbound request

The public `/pro` page routes buyers to an operator-reviewed access request. Ask
for these fields before quoting or issuing a key:

- organization name and billing contact;
- intended workflow: research desk, data pipeline, MCP agent, monitoring;
- required scopes and expected request volume;
- preferred token delivery channel;
- expiry, rotation and revocation expectations;
- confirmation that 13FLOW is a research screen, not investment advice.

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

## 2. Select the access package

Use one of the public packages exposed by `/api/pro-offer`:

- **Pilot access** — one research desk or analyst validating 13F workflows.
- **Desk API** — repeatable internal dashboards, notebooks or data pipelines.
- **Agent / MCP workflow** — automated agent access to 13F context and quality metadata.

All packages are operator quoted for now. Do not expose public self-serve
checkout until pricing, terms, payment details and support boundaries are
ready.

## 3. Qualify the account

Record before creating a key:

- organization and label;
- intended workflow: research desk, data pipeline, MCP agent, monitoring;
- required scopes: usually `funds:read,quality:read`;
- rate limits;
- expiry and rotation cadence;
- delivery channel for the plaintext token.

Do not promise validated alpha, expected returns, probabilistic scores, complete
insider-only coverage, production x402 access, or full quantitative validation.
The public boundary is:

```bash
curl -fsS https://13flow.eu/api/product-status | python3 -m json.tool
```

## 4. Create the key

Run on production:

```bash
sudo /opt/13flow/.venv/bin/python /opt/13flow/run.py \
  --create-api-key "Client Label" \
  --pro-db /var/lib/13flow-pro/13flow-pro.db \
  --api-key-scopes funds:read,quality:read \
  --api-key-rate-per-min 120 \
  --api-key-rate-per-day 10000
```

The plaintext token is shown once. Store only the key id in the operator notes;
do not paste the token into tickets, Git, logs or chat.

## 5. First client probes

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

## 6. MCP probe

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

## 7. Audit verification

```bash
sudo sqlite3 /var/lib/13flow-pro/13flow-pro.db \
  "SELECT key_id, method, route, status, at FROM api_audit ORDER BY id DESC LIMIT 20;"
```

The new key id should appear on successful requests. Denied requests and
rate-limited requests should also create audit rows.

## 8. Production preflight

```bash
SHA=<deployed-sha>

printf "API token: "
read -r -s SMARTMONEY_PRO_TOKEN
printf "\n"
export SMARTMONEY_PRO_TOKEN

sudo -E /opt/13flow/.venv/bin/python /opt/13flow/run.py --preflight \
  --db /var/lib/13flow/13flow.db \
  --pro-db /var/lib/13flow-pro/13flow-pro.db \
  --require-pro \
  --expected-sha "$SHA"

unset SMARTMONEY_PRO_TOKEN
```

## 9. Rotation and revocation

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

## 8. Operational boundaries

- One active key per institution or internal service.
- Keep public `13flow.service` read-only and without Pro DB write path.
- Keep `13flow-pro.service` as the only web-facing writer to
  `/var/lib/13flow-pro/13flow-pro.db`.
- Do not enable x402 until `MCP_X402_PAY_TO`, `MCP_X402_FACILITATOR_URL` and
  the internal Pro token are configured and tested.
- Do not relaunch external historical-price scraping from production to satisfy
  full validation. Import a vetted CSV and validate it offline.
