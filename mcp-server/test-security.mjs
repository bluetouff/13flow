import http from 'node:http';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { auth } from '@modelcontextprotocol/sdk/client/auth.js';
import proxyaddr from 'proxy-addr';
import { Address4, Address6, AddressError } from 'ip-address';
import { ipKeyGenerator } from 'express-rate-limit';
import uri from 'fast-uri';
import { jsx, Suspense, createContext } from 'hono/jsx';
import { renderToString, renderToReadableStream } from 'hono/jsx/dom/server';

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
