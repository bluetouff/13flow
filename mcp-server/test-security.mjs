import http from 'node:http';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const mcpPort = Number(process.env.SECURITY_TEST_PORT || 18851);
const serverPath = fileURLToPath(new URL('./server.mjs', import.meta.url));

const fixture = http.createServer((req, res) => {
  res.setHeader('Content-Type', 'application/json');
  if (req.url === '/api/data-quality') {
    res.write('{"padding":"');
    for (let i = 0; i < 80; i += 1) res.write('x'.repeat(1024));
    return res.end('"}');
  }
  if (req.url === '/api/funds') {
    return setTimeout(() => res.end('[]'), 500);
  }
  if (req.url === '/api/live-status') {
    return res.end('{"public_state":"LIVE"}');
  }
  res.statusCode = 404;
  return res.end('{"error":"not_found"}');
});

await new Promise((resolve, reject) => {
  fixture.once('error', reject);
  fixture.listen(0, '127.0.0.1', resolve);
});
const fixturePort = fixture.address().port;

const child = spawn(process.execPath, [serverPath], {
  env: {
    ...process.env,
    NODE_ENV: 'test',
    MCP_PORT: String(mcpPort),
    MCP_13FLOW_API_BASE: `http://127.0.0.1:${fixturePort}`,
    MCP_MAX_UPSTREAM_BODY: String(64 * 1024),
    MCP_MAX_IN_FLIGHT: '2',
    MCP_RATE_MAX: '100',
  },
  stdio: ['ignore', 'ignore', 'pipe'],
});

let childStderr = '';
child.stderr.on('data', (chunk) => { childStderr += chunk.toString('utf8'); });

async function waitForServer() {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`MCP fixture exited early: ${childStderr}`);
    try {
      const response = await fetch(`http://127.0.0.1:${mcpPort}/mcp`, { method: 'GET' });
      if (response.status === 405) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`MCP fixture did not start: ${childStderr}`);
}

let client;
try {
  await waitForServer();
  const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${mcpPort}/mcp`));
  client = new Client({ name: '13flow-security-test', version: '1.0.0' });
  await client.connect(transport);

  const oversized = await client.callTool({
    name: 'get_data_quality',
    arguments: { threshold: 100, limit: 50 },
  });
  if (oversized.isError !== true) throw new Error('oversized upstream response was not rejected');
  if (JSON.stringify(oversized).length > 4096) throw new Error('oversized upstream body leaked into the MCP error');
  console.log('upstream response cap: enforced');

  const burst = await Promise.allSettled(Array.from({ length: 8 }, () => client.callTool({
    name: 'list_funds',
    arguments: { limit: 1 },
  })));
  const rejected = burst.filter((item) => item.status === 'rejected');
  const fulfilled = burst.filter((item) => item.status === 'fulfilled');
  if (rejected.length < 1 || fulfilled.length < 1) {
    throw new Error(`concurrency cap was not observable: ${fulfilled.length} fulfilled, ${rejected.length} rejected`);
  }
  console.log(`concurrency cap: enforced (${fulfilled.length} fulfilled, ${rejected.length} rejected)`);
} finally {
  await client?.close().catch(() => {});
  child.kill('SIGTERM');
  fixture.closeAllConnections?.();
  await new Promise((resolve) => fixture.close(resolve));
}
