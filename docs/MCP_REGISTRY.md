# 13FLOW MCP Registry release

The canonical remote server is `https://13flow.eu/api/mcp`. The official Registry name is
`io.github.bluetouff/13flow` and the source manifest is `/server.json` in this repository.

Registry publication is immutable metadata publication. A version must be unique, and the
matching MCP daemon must be deployed before that version is published.

## 1. Local validation

From the expected checkout:

```bash
pwd
git remote -v
git status --short --branch
cd mcp-server
npm ci
npm run check
npm run test:config
npm run test:security
npm audit --omit=dev
cd ..
mcp-publisher validate
```

The expected repository is `https://github.com/bluetouff/13flow.git`. The versions in
`server.json`, `mcp-server/package.json`, `mcp-server/package-lock.json` and MCP health output
must match.

## 2. Local integration test

Start the MCP daemon against a trusted API target:

```bash
cd mcp-server
MCP_13FLOW_API_BASE=https://13flow.eu npm start
```

In another terminal:

```bash
cd mcp-server
URL=http://127.0.0.1:8849/mcp npm test
```

The test checks transport headers, invalid Host/Origin rejection, discovery, read-only
annotations, bounded tool results, canonical URLs and absence of all Pro tools by default.

## 3. Deploy and prove the public endpoint

Deploy the exact Git SHA with `deploy/deploy-code-safe.sh`, then run:

```bash
sudo EXPECTED_SHA=<exact-40-character-sha> /opt/13flow/deploy/smoke-public.sh
curl -fsS http://127.0.0.1:8849/healthz | python3 -m json.tool
```

The public smoke must prove the new tool list and confirm that private/payment tools are absent
and cannot execute. Do not treat a Git push or a valid manifest as live-server proof.

Before publication, also verify the deployed unit runs as `flowmcp`, binds only to loopback,
has `IPAddressDeny=any`, and reads a root-owned `0640` environment file. The Registry daemon
must keep `MCP_PRO_TOOLS_ENABLED=0` and `MCP_X402_ENABLED=0`.

## 4. Publish once

Authentication expires quickly, so log in only after code, deployment and public checks are
complete:

```bash
cd /opt/13flow
mcp-publisher validate
mcp-publisher login github
mcp-publisher publish
```

GitHub authentication requires the `io.github.bluetouff/*` namespace. Do not republish the
same version after success because Registry versions are immutable.

## 5. Verify Registry convergence

```bash
cd /opt/13flow/mcp-server
npm run registry:verify -- --version 1.0.0 --attempts 12 --delay-ms 5000
```

Keep the three proofs separate:

1. the repository and versioned manifest;
2. the deployed MCP daemon and public endpoint;
3. the official Registry entry and exact remote URL.
