import http from 'node:http';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { auth } from '@modelcontextprotocol/sdk/client/auth.js';
import proxyaddr from 'proxy-addr';
import { Address4, Address6, AddressError } from 'ip-address';
import { ipKeyGenerator } from 'express-rate-limit';
import uri from 'fast-uri';
import { jsx, Suspense, createContext } from 'hono/jsx';
import { renderToString, renderToReadableStream } from 'hono/jsx/dom/server';
import { Hono } from 'hono';
import { serveStatic as honoStatic } from 'hono/serve-static';
import { serveStatic as nodeStatic } from '@hono/node-server/serve-static';

// Exercise OAuth issuer isolation entirely in memory, using synthetic credentials.
const oauthIssuer = 'https://auth.example';
const oauthResource = 'https://resource.example/mcp';
function oauthFixture(discoveredIssuer, storedIssuer = oauthIssuer) {
  const requests = [];
  const savedTokens = [];
  const redirects = [];
  const provider = {
    redirectUrl: undefined,
    clientMetadata: { redirect_uris: [], grant_types: ['client_credentials'] },
    clientInformation: () => ({
      client_id: 'test-client', client_secret: 'test-only-secret', issuer: storedIssuer,
    }),
    tokens: () => undefined,
    saveTokens: (tokens) => { savedTokens.push(tokens); },
    prepareTokenRequest: () => new URLSearchParams({ grant_type: 'client_credentials' }),
    saveCodeVerifier: () => {},
    redirectToAuthorization: (url) => { redirects.push(url); },
    discoveryState: () => ({
      authorizationServerUrl: discoveredIssuer,
      resourceMetadata: { resource: oauthResource, authorization_servers: [discoveredIssuer] },
      authorizationServerMetadata: {
        issuer: discoveredIssuer,
        authorization_endpoint: `${discoveredIssuer}/authorize`,
        token_endpoint: `${discoveredIssuer}/token`,
        response_types_supported: ['code'],
        token_endpoint_auth_methods_supported: ['client_secret_post'],
      },
    }),
  };
  const options = {
    serverUrl: oauthResource,
    fetchFn: async (url, init) => {
      assert.equal(String(url), `${discoveredIssuer}/token`);
      requests.push({ url: String(url), body: new URLSearchParams(init.body) });
      return Response.json({ access_token: 'test-only-token', token_type: 'Bearer' });
    },
  };
  return { provider, options, requests, savedTokens, redirects };
}

for (const otherIssuer of ['https://other-auth.example', `${oauthIssuer}/other-tenant`]) {
  const fixture = oauthFixture(otherIssuer);
  await assert.rejects(auth(fixture.provider, fixture.options), /bound to authorization server/);
  assert.equal(fixture.requests.length, 0, 'issuer mismatch must stop before token submission');
  assert.equal(fixture.savedTokens.length, 0);
}
for (const storedIssuer of [oauthIssuer, 'HTTPS://AUTH.EXAMPLE:443/']) {
  const fixture = oauthFixture(oauthIssuer, storedIssuer);
  assert.equal(await auth(fixture.provider, fixture.options), 'AUTHORIZED');
  assert.equal(fixture.requests.length, 1);
  assert.equal(fixture.requests[0].body.get('client_id'), 'test-client');
  assert.equal(fixture.requests[0].body.get('client_secret'), 'test-only-secret');
  assert.equal(fixture.savedTokens[0].issuer, oauthIssuer);
}
const refreshFixture = oauthFixture(oauthIssuer);
refreshFixture.provider.redirectUrl = 'https://client.example/callback';
refreshFixture.provider.tokens = () => ({
  access_token: 'test-only-expired-token', token_type: 'Bearer',
  refresh_token: 'test-only-refresh-token', issuer: 'https://other-auth.example',
});
assert.equal(await auth(refreshFixture.provider, refreshFixture.options), 'REDIRECT');
assert.equal(refreshFixture.requests.length, 0, 'refresh tokens must remain bound to their issuer');
assert.equal(refreshFixture.redirects.length, 1);
assert.equal(refreshFixture.savedTokens.length, 0);
console.log('OAuth issuer isolation and same-issuer authorization: passed');

// A native IPv6 range must not trust IPv4 addresses through mapped notation.
for (const subnets of [['::/8'], ['::/8', '2001:db8::/32']]) {
  const trust = proxyaddr.compile(subnets);
  assert.equal(trust('192.0.2.1'), false);
  assert.equal(trust('::ffff:192.0.2.1'), false);
}
for (const subnets of [['::ffff:192.0.2.0/120'], ['::ffff:192.0.2.0/120', '2001:db8::/32']]) {
  const trust = proxyaddr.compile(subnets);
  assert.equal(trust('192.0.2.1'), true);
  assert.equal(trust('::ffff:192.0.2.1'), true);
  assert.equal(trust('198.51.100.1'), false);
  assert.equal(trust('::ffff:198.51.100.1'), false);
}
console.log('proxy-addr IPv4-mapped trust isolation and valid subnets: passed');

// Dependency regressions: NAT64/link-local classification and cross-family subnets.
for (const address of ['64:ff9b:1::', '64:ff9b:1:7f00:0:100::', '64:ff9b:1::7f00:1', '64:ff9b:1:ffff:ffff:ffff:ffff:ffff']) {
  assert.equal(new Address6(address).isPrivate(), true, address);
}
for (const address of ['64:ff9b:2::', '2606:4700:4700::1111']) {
  assert.equal(new Address6(address).isPrivate(), false, address);
}
for (const address of ['fe80::1', 'fe90::1', 'febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff']) {
  assert.equal(new Address6(address).isLinkLocal(), true, address);
}
assert.equal(new Address6('fec0::1').isLinkLocal(), false);
for (const method of ['isInSubnet', 'isHostInSubnet']) {
  assert.equal(new Address6('a00::1')[method](new Address4('10.0.0.0/8')), false);
  assert.equal(new Address4('32.1.13.184')[method](new Address6('2001:db8::/32')), false);
  assert.equal(new Address6('2001:db8::1')[method](new Address6('2001:db8::/32')), true);
  assert.equal(new Address4('10.0.0.1')[method](new Address4('10.0.0.0/8')), true);
}
// Small invalid inputs exercise the length guard without stressing the process.
for (const address of ['!'.repeat(1024), 'fffff:'.repeat(180)]) {
  assert.equal(Address6.isValid(address), false);
  assert.throws(() => new Address6(address), (error) => (
    error instanceof AddressError && !error.parseMessage && error.message.length < 256
  ));
}
assert.equal(Address6.isValid('ffff:ffff:ffff:ffff:ffff:ffff:255.255.255.255'), true);
assert.equal(Address6.isValid('fe80::1%eth0/64'), true);
// Preserve the actual consumer's rate-limit keys, including mapped IPv4.
assert.equal(ipKeyGenerator('192.0.2.1'), '192.0.2.1');
assert.equal(ipKeyGenerator('::ffff:192.0.2.1'), '192.0.2.1');
assert.equal(ipKeyGenerator('2001:db8:abcd:12::1'), '2001:db8:abcd::/56');
console.log('ip-address classification, bounded parsing and rate-limit compatibility: passed');

// Scheme-relative hosts must canonicalize encoded uppercase letters too.
for (const encoded of ['//%41.com', '//%61.%43OM']) {
  assert.equal(uri.parse(encoded).host, 'a.com');
  assert.equal(uri.equal(encoded, '//a.com'), true);
}
assert.equal(uri.equal('//A.com', '//a.com'), true);
assert.equal(uri.equal('//a.com', '//b.com'), false);
assert.equal(uri.resolve('https://example.org/schema/root.json', '../item.json'), 'https://example.org/item.json');
console.log('fast-uri host canonicalization and reference resolution: passed');

// Untrusted strings remain text at JSX boundaries and at the SSR root.
const markup = '<img src=x onerror=alert(1)>';
const escapedMarkup = '&lt;img src=x onerror=alert(1)&gt;';
const Context = createContext(null);
assert.equal(renderToString(markup), escapedMarkup);
assert.equal(await new Response(await renderToReadableStream(markup)).text(), escapedMarkup);
assert.equal(String(await jsx(Suspense, { fallback: 'loading' }, markup).toString()), escapedMarkup);
assert.equal(String(await jsx(Context.Provider, { value: null }, markup).toString()), escapedMarkup);
assert.equal(renderToString(jsx('p', null, markup)), `<p>${escapedMarkup}</p>`);
assert.equal(renderToString('ordinary text'), 'ordinary text');
console.log('Hono JSX boundary escaping and normal rendering: passed');

// Both packages contain their own static helper. Check ESM and CommonJS copies
// with harmless fixture filenames: residual percent encodings must stop before
// content lookup, while ordinary spaces, Unicode and prefix guards still work.
const require = createRequire(import.meta.url);
const staticRoot = await mkdtemp(join(tmpdir(), '13flow-static-security-'));
try {
  const files = new Map();
  await mkdir(join(staticRoot, 'assets'));
  for (const name of ['plain.txt', 'hello world.txt', '炎.txt', '100%.txt', '100%25.txt']) {
    const path = join(staticRoot, 'assets', name);
    const text = `Fixture: ${name}`;
    files.set(path, text);
    await writeFile(path, text);
  }
  const variants = [
    ['Hono ESM', honoStatic, true],
    ['Hono CommonJS', require('hono/serve-static').serveStatic, true],
    ['Node adapter ESM', nodeStatic, false],
    ['Node adapter CommonJS', require('@hono/node-server/serve-static').serveStatic, false],
  ];
  for (const [label, serveStatic, inMemory] of variants) {
    const found = [];
    const lookups = [];
    const app = new Hono();
    app.use('/assets/private/*', (c) => c.text('Denied', 401));
    app.use('/assets/*', serveStatic({
      root: staticRoot,
      onFound: (path) => { found.push(path); },
      ...(inMemory ? { getContent: async (path) => { lookups.push(path); return files.get(path); } } : {}),
    }));
    for (const name of ['plain.txt', 'hello world.txt', '炎.txt']) {
      const response = await app.request(`/assets/${encodeURIComponent(name)}`);
      assert.equal(response.status, 200, `${label}: ordinary filename`);
      assert.equal(await response.text(), `Fixture: ${name}`);
    }
    assert.equal((await app.request('/assets/private/fixture.txt')).status, 401, `${label}: prefix guard`);
    for (const name of ['100%.txt', '100%25.txt']) {
      found.length = 0;
      lookups.length = 0;
      const response = await app.request(`/assets/${encodeURIComponent(name)}`);
      assert.equal(response.status, 404, `${label}: residual encoding must be rejected`);
      assert.equal(found.length, 0, `${label}: no file served`);
      assert.equal(lookups.length, 0, `${label}: no content lookup`);
    }
  }
  console.log('Hono and Node static helpers (ESM/CommonJS): encoding isolation and valid filenames passed');
} finally {
  await rm(staticRoot, { recursive: true });
}

const mcpPort = Number(process.env.SECURITY_TEST_PORT || 18851);
const serverPath = fileURLToPath(new URL('./server.mjs', import.meta.url));
const confluenceFixture = {
  generated_at: '2026-10-21T00:00:00+00:00',
  metadata: {
    edgar_refresh_verified: true,
    edgar_refresh: { issuers_checked: 2, issuers_requested: 2, issuer_failures: 0,
      oldest_source_checked_at: '2026-10-20T23:00:00+00:00',
      non_issuer_filings: Array.from({ length: 25 }, (_, i) => ({ accession: `fixture-${i}` })) },
    universe_coverage: { excluded_tickers: Array.from({ length: 25 }, (_, i) => ({ ticker: `FIX${i}` })) },
  },
  signals: [{ ticker: 'FIXTURE', score: 0 }],
};

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
  if (req.url.startsWith('/api/watchlist/preview?')) {
    const tickers = new URL(req.url, 'http://fixture.example').searchParams.get('tickers').split(',');
    return res.end(JSON.stringify({ items: [], fixture_tickers: tickers }));
  }
  if (req.url.startsWith('/api/signals/confluence?')) {
    return res.end(JSON.stringify(confluenceFixture));
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
    MCP_PUBLIC_SITE: 'https://public-fixture.example',
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

  const { tools: declaredTools } = await client.listTools();
  for (const tool of declaredTools) {
    const hasProperties = Object.keys(tool.inputSchema.properties).length > 0;
    assert.equal(tool.inputSchema.additionalProperties, hasProperties ? false : undefined,
      `${tool.name}: advertised object contract`);
  }
  const watchlistSchema = declaredTools.find((tool) => tool.name === 'preview_watchlist').inputSchema;
  assert.deepEqual(watchlistSchema.required, ['tickers']);
  assert.equal(watchlistSchema.properties.tickers.minItems, 1);
  assert.equal(watchlistSchema.properties.tickers.maxItems, 25);
  assert.equal(watchlistSchema.properties.movement_limit.default, 3);
  const fundsSchema = declaredTools.find((tool) => tool.name === 'list_funds').inputSchema;
  assert.equal(fundsSchema.properties.limit.default, 25);
  assert.equal(fundsSchema.properties.limit.maximum, 50);
  console.log('Zod 4 advertised tool schemas, collection bounds and defaults: passed');

  // Small inputs exercise the pre-validation budget without stressing memory.
  for (const arguments_ of [
    Object.fromEntries(Array.from({ length: 129 }, (_, i) => [`fixture${i}`, 0])),
    { fixture: { nested: Array(129).fill(0) } },
  ]) {
    const bounded = await client.callTool({ name: 'get_live_status', arguments: arguments_ });
    assert.equal(bounded.isError, true, 'tool arguments must respect the element budget');
    assert.match(bounded.content[0].text, /element|limit|maximum/i);
  }
  const tickers = Array.from({ length: 25 }, (_, i) => `FIX${i}`);
  const largestWatchlist = await client.callTool({
    name: 'preview_watchlist', arguments: { tickers, movement_limit: 10, include_quality_details: true },
  });
  assert.equal(largestWatchlist.isError, false);
  assert.deepEqual(largestWatchlist.structuredContent.fixture_tickers, tickers);
  console.log('MCP pre-validation element budget and full 25-ticker watchlist: passed');
  const omittedArguments = await client.callTool({ name: 'get_live_status' });
  assert.equal(omittedArguments.isError, false, 'empty tool arguments remain optional');
  const atElementLimit = await client.callTool({
    name: 'get_live_status',
    arguments: Object.fromEntries(Array.from({ length: 128 }, (_, i) => [`fixture${i}`, 0])),
  });
  assert.equal(atElementLimit.isError, false, 'the element limit is inclusive');

  const confluence = await client.callTool({ name: 'get_confluence_signals', arguments: { window: 90, limit: 3 } });
  assert.equal(confluence.isError, false);
  const summary = confluence.structuredContent;
  assert.equal(summary.generated_at, confluenceFixture.generated_at);
  assert.equal(summary.metadata.edgar_refresh_verified, true);
  assert.equal(summary.metadata.edgar_refresh.oldest_source_checked_at,
    confluenceFixture.metadata.edgar_refresh.oldest_source_checked_at);
  assert.equal(summary.metadata.edgar_refresh.issuers_checked, 2);
  for (const [section, key] of [['universe_coverage', 'excluded_tickers'], ['edgar_refresh', 'non_issuer_filings']]) {
    assert.equal(summary.metadata[section][key].length, 10);
    assert.equal(summary.metadata[section][`${key}_available`], 25);
    assert.equal(summary.metadata[section][`${key}_returned`], 10);
    assert.equal(summary.metadata[section][`${key}_truncated`], true);
    assert.equal(confluenceFixture.metadata[section][key].length, 25, 'source diagnostics remain complete');
  }
  assert.equal(summary.source_url, 'https://public-fixture.example/api/signals/confluence?window=90');
  console.log('Confluence diagnostic summary: bounded, totals and source provenance preserved');

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
