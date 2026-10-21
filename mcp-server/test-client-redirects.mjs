import assert from 'node:assert/strict';
import http from 'node:http';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import {
  StreamableHTTPClientTransport,
  StreamableHTTPError,
} from '@modelcontextprotocol/sdk/client/streamableHttp.js';

// GHSA-6prh-2h8m-c8cw: exercise the SDK's default fetch, not a mock or an
// application redirect override. Both origins are ephemeral loopback fixtures;
// every header and body value is synthetic and never printed.
const MARKER_HEADER = 'x-13flow-test-marker';
const MARKER_VALUE = 'synthetic-redirect-marker';
const DEADLINE_MS = 3_000;
let passed = 0;
let failed = 0;

function counters() {
  return { requests: 0, markerHeaders: 0, bodies: [], initializes: 0, initialized: 0 };
}

async function readRequest(request, counts) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const body = Buffer.concat(chunks).toString('utf8');
  counts.requests += 1;
  if (request.headers[MARKER_HEADER] === MARKER_VALUE) counts.markerHeaders += 1;
  if (body) counts.bodies.push(body);
  return body;
}

function respondToMcp(request, response, body, counts) {
  if (request.method === 'GET') {
    response.writeHead(405).end();
    return;
  }
  if (request.method !== 'POST') {
    response.writeHead(405).end();
    return;
  }
  const message = JSON.parse(body);
  if (message.method === 'initialize') {
    counts.initializes += 1;
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({
      jsonrpc: '2.0',
      id: message.id,
      result: {
        protocolVersion: message.params.protocolVersion,
        capabilities: {},
        serverInfo: { name: '13flow-loopback-fixture', version: '1.0.0' },
      },
    }));
    return;
  }
  if (message.method === 'notifications/initialized') {
    counts.initialized += 1;
    response.writeHead(202).end();
    return;
  }
  response.writeHead(400).end();
}

async function fixture(handler) {
  const server = http.createServer((request, response) => {
    Promise.resolve(handler(request, response)).catch(() => {
      if (!response.headersSent) response.writeHead(500);
      response.end();
    });
  });
  server.requestTimeout = DEADLINE_MS;
  server.headersTimeout = DEADLINE_MS;
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  assert.ok(address && typeof address !== 'string', 'loopback fixture started');
  return {
    url: new URL(`http://127.0.0.1:${address.port}/mcp`),
    async close() {
      await new Promise((resolve, reject) => {
        server.close((error) => (error ? reject(error) : resolve()));
        server.closeAllConnections();
      });
    },
  };
}

async function connect(transport, client) {
  let timer;
  try {
    await Promise.race([
      client.connect(transport, { timeout: DEADLINE_MS }),
      new Promise((_, reject) => {
        timer = setTimeout(() => {
          void transport.close().catch(() => {});
          reject(new Error('loopback fixture deadline exceeded'));
        }, DEADLINE_MS);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function cleanup(client, transport, ...servers) {
  // Even an unexpected client-close failure must not leave a fixture listening.
  const clientResults = await Promise.allSettled([
    Promise.resolve().then(() => client?.close()),
    Promise.resolve().then(() => transport?.close()),
  ]);
  const serverResults = await Promise.allSettled(servers.filter(Boolean).map((server) => (
    Promise.resolve().then(() => server.close())
  )));
  assert.ok([...clientResults, ...serverResults].every((result) => result.status === 'fulfilled'),
    'clients and loopback fixtures close');
}

function makeClient(url) {
  // Do not supply redirectPolicy or fetch: the installed SDK default is tested.
  const transport = new StreamableHTTPClientTransport(url, {
    requestInit: { headers: { [MARKER_HEADER]: MARKER_VALUE } },
  });
  const client = new Client({ name: MARKER_VALUE, version: '1.0.0' });
  return { transport, client };
}

async function crossOrigin(status) {
  const destinationCounts = counters();
  const sourceCounts = counters();
  let source;
  let client;
  let transport;
  const destination = await fixture(async (request, response) => {
    const body = await readRequest(request, destinationCounts);
    respondToMcp(request, response, body, destinationCounts);
  });
  try {
    source = await fixture(async (request, response) => {
      await readRequest(request, sourceCounts);
      response.writeHead(status, { location: destination.url.href }).end();
    });
    ({ client, transport } = makeClient(source.url));
    await assert.rejects(connect(transport, client), (error) => (
      error instanceof StreamableHTTPError && error.code === status
    ), 'cross-origin redirect must reject with its HTTP status');
    assert.equal(sourceCounts.requests, 1, 'original initialize reaches source');
    assert.equal(sourceCounts.markerHeaders, 1, 'synthetic header reaches source');
    assert.equal(sourceCounts.bodies.length, 1, 'initialize body reaches source');
    assert.equal(destinationCounts.requests, 0, 'cross-origin destination receives no request');
    assert.equal(destinationCounts.markerHeaders, 0, 'synthetic header stays at source');
    assert.equal(destinationCounts.bodies.length, 0, 'initialize body stays at source');
  } finally {
    await cleanup(client, transport, source, destination);
  }
}

async function validHandshake(redirectStatus) {
  const redirectCounts = counters();
  const destinationCounts = counters();
  let client;
  let transport;
  const server = await fixture(async (request, response) => {
    if (redirectStatus && request.url === '/mcp') {
      await readRequest(request, redirectCounts);
      // A relative Location must remain usable without opting into redirects.
      response.writeHead(redirectStatus, { location: '/resolved-mcp' }).end();
      return;
    }
    const body = await readRequest(request, destinationCounts);
    respondToMcp(request, response, body, destinationCounts);
  });
  try {
    ({ client, transport } = makeClient(server.url));
    await connect(transport, client);
    assert.equal(client.getServerVersion()?.name, '13flow-loopback-fixture');
    assert.equal(destinationCounts.initializes, 1, 'initialize completes');
    assert.equal(destinationCounts.initialized, 1, 'initialized notification completes');
    assert.equal(destinationCounts.bodies.length, 2, 'both MCP POST bodies arrive');
    // An optional GET stream may race connection cleanup, so only POSTs count.
    for (const body of destinationCounts.bodies) {
      assert.equal(JSON.parse(body).jsonrpc, '2.0', 'MCP JSON-RPC remains valid');
    }
    if (redirectStatus) {
      assert.deepEqual(destinationCounts.bodies, redirectCounts.bodies,
        'relative redirect preserves both MCP POST bodies');
      assert.ok(destinationCounts.markerHeaders >= 2,
        'same-origin redirect preserves the synthetic header');
    }
  } finally {
    await cleanup(client, transport, server);
  }
}

async function test(label, action) {
  try {
    await action();
    passed += 1;
    console.log(`${label}: passed`);
  } catch {
    // Do not print SDK error objects: redirect errors can include destinations.
    failed += 1;
    console.error(`${label}: failed`);
  }
}

for (const status of [307, 308]) {
  await test(`default cross-origin HTTP ${status} rejection`, () => crossOrigin(status));
}
await test('ordinary MCP initialize and initialized', () => validHandshake());
for (const status of [307, 308]) {
  await test(`default same-origin relative HTTP ${status} handshake`, () => validHandshake(status));
}
console.log(`client redirect regressions: ${passed} passed, ${failed} failed`);
if (failed) process.exitCode = 1;
