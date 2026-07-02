/**
 * 13FLOW MCP server.
 *
 * Streamable HTTP, stateless, JSON responses. The server is designed to sit
 * behind Apache at /api/mcp while listening only on 127.0.0.1.
 *
 * Public tools and resources are read-only. Pro tools can be authorized by an
 * existing 13FLOW Pro API key, or by an x402 v2 payment that is verified and
 * settled through a configured facilitator. x402 access fails closed when the
 * payment destination or facilitator is not configured.
 */
import http from 'node:http';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { McpServer, ResourceTemplate } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { ErrorCode, McpError } from '@modelcontextprotocol/sdk/types.js';
import { z } from 'zod';

const HOST = process.env.MCP_HOST || '127.0.0.1';
const PORT = Number.parseInt(process.env.MCP_PORT || '8849', 10);
const MCP_PATH = process.env.MCP_PATH || '/mcp';
const SITE = (process.env.MCP_PUBLIC_SITE || process.env.SITE || 'https://13flow.eu').replace(/\/$/, '');
const API_BASE = (process.env.MCP_13FLOW_API_BASE || 'http://127.0.0.1:8000').replace(/\/$/, '');
const PRO_API_BASE = (process.env.MCP_13FLOW_PRO_API_BASE || API_BASE).replace(/\/$/, '');
const MCP_VERSION = '0.1.0';
const MAX_BODY = Number.parseInt(process.env.MCP_MAX_BODY || String(1024 * 1024), 10);
const RATE_MAX = Number.parseInt(process.env.MCP_RATE_MAX || '120', 10);
const RATE_WINDOW_MS = 60_000;
const API_TIMEOUT_MS = Number.parseInt(process.env.MCP_API_TIMEOUT_MS || '10000', 10);
const PAYMENT_CACHE_TTL_MS = Number.parseInt(process.env.MCP_X402_PAYMENT_CACHE_TTL_MS || String(60 * 60 * 1000), 10);

const ALLOWED_HOSTS = (process.env.MCP_ALLOWED_HOSTS || '13flow.eu,www.13flow.eu,127.0.0.1,localhost')
  .split(',').map((s) => s.trim().toLowerCase()).filter(Boolean);
const ALLOWED_ORIGINS = (process.env.MCP_ALLOWED_ORIGINS || 'https://13flow.eu,https://www.13flow.eu')
  .split(',').map((s) => s.trim()).filter(Boolean);

const X402 = {
  enabled: /^1|true|yes$/i.test(process.env.MCP_X402_ENABLED || '0'),
  scheme: process.env.MCP_X402_SCHEME || 'exact',
  network: process.env.MCP_X402_NETWORK || 'eip155:8453',
  price: process.env.MCP_X402_PRICE || '$0.05',
  payTo: process.env.MCP_X402_PAY_TO || '',
  asset: process.env.MCP_X402_ASSET || '',
  facilitator: (process.env.MCP_X402_FACILITATOR_URL || '').replace(/\/$/, ''),
  facilitatorAuth: process.env.MCP_X402_FACILITATOR_AUTH || '',
  testMode: /^1|true|yes$/i.test(process.env.MCP_X402_TEST_MODE || '0'),
};
function secretValue(name) {
  const direct = process.env[name];
  if (direct) return direct.trim();
  const file = process.env[`${name}_FILE`];
  if (!file) return '';
  try {
    return readFileSync(file, 'utf8').trim();
  } catch {
    return '';
  }
}
const INTERNAL_PRO_TOKEN = secretValue('MCP_13FLOW_INTERNAL_PRO_TOKEN');

const PREMIUM_TOOLS = new Set(['pro.list_funds', 'pro.get_fund', 'pro.get_data_quality']);
const JsonAny = z.any();
const ToolOutput = z.object({}).catchall(JsonAny);
const StatusOutput = ToolOutput.extend({
  public_state: z.string().optional(),
  source: z.string().optional(),
  git_sha: z.string().optional(),
});
const FundsOutput = ToolOutput.extend({
  funds: z.array(z.any()),
  count: z.number(),
});
const PaymentOutput = ToolOutput.extend({
  x402: z.object({ enabled: z.boolean(), configured: z.boolean() }).catchall(JsonAny),
  pro_api_key: z.object({ supported: z.boolean() }).catchall(JsonAny),
});

function activeGitSha() {
  if (process.env.MCP_GIT_SHA) return process.env.MCP_GIT_SHA;
  if (process.env.SMARTMONEY_GIT_SHA) return process.env.SMARTMONEY_GIT_SHA;
  if (process.env.GITHUB_SHA) return process.env.GITHUB_SHA;
  try {
    return execFileSync('git', ['rev-parse', 'HEAD'], {
      cwd: process.cwd(),
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch {
    return 'unknown';
  }
}

const CURRENT_SHA = activeGitSha();
const SHA_STATUS = /^[0-9a-f]{40}$/i.test(CURRENT_SHA) ? 'verified-hex' : 'unknown';
const SERVER_INFO = {
  name: '13flow.eu',
  version: MCP_VERSION,
  sha: CURRENT_SHA,
  shaStatus: SHA_STATUS,
  transport: 'streamable-http',
  path: MCP_PATH,
  publicPath: '/api/mcp',
  publicUrl: `${SITE}/api/mcp`,
};

const buckets = new Map();
const paymentCache = new Map();

function clientIp(req) {
  const xff = req.headers['x-forwarded-for'];
  if (xff) {
    const parts = String(xff).split(',').map((s) => s.trim()).filter(Boolean);
    if (parts.length) return parts[parts.length - 1];
  }
  return req.socket.remoteAddress || 'unknown';
}

function rateLimited(ip) {
  const now = Date.now();
  let bucket = buckets.get(ip);
  if (!bucket || now - bucket.start >= RATE_WINDOW_MS) {
    bucket = { start: now, count: 0 };
    buckets.set(ip, bucket);
  }
  bucket.count += 1;
  return bucket.count > RATE_MAX;
}

setInterval(() => {
  const now = Date.now();
  for (const [key, bucket] of buckets) if (now - bucket.start >= RATE_WINDOW_MS) buckets.delete(key);
  for (const [key, item] of paymentCache) if (now - item.at >= PAYMENT_CACHE_TTL_MS) paymentCache.delete(key);
}, 5 * RATE_WINDOW_MS).unref();

function jsonHeaders(extra = {}) {
  return {
    'Content-Type': 'application/json',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer',
    'Cache-Control': 'no-store',
    'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    ...extra,
  };
}

function send(res, status, payload, extraHeaders = {}) {
  res.writeHead(status, jsonHeaders(extraHeaders));
  res.end(JSON.stringify(payload));
}

function hostAllowed(req) {
  const host = String(req.headers.host || '').toLowerCase().split(':')[0];
  return ALLOWED_HOSTS.includes(host);
}

function originAllowed(req) {
  const origin = req.headers.origin;
  if (!origin) return true;
  return ALLOWED_ORIGINS.includes(String(origin));
}

function apiUrl(path, base = API_BASE) {
  const clean = String(path || '').startsWith('/') ? String(path) : `/${path}`;
  return `${base}${clean}`;
}

async function fetchJson(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
  const { base, ...fetchOptions } = options;
  const headers = { Accept: 'application/json', ...(fetchOptions.headers || {}) };
  try {
    const res = await fetch(apiUrl(path, base || API_BASE), { ...fetchOptions, headers, signal: controller.signal });
    const text = await res.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch {
      payload = { raw: text.slice(0, 500) };
    }
    if (!res.ok) {
      const e = new Error(`13FLOW API ${res.status} for ${path}`);
      e.status = res.status;
      e.payload = payload;
      throw e;
    }
    return payload;
  } finally {
    clearTimeout(timer);
  }
}

function encodeB64Json(obj) {
  return Buffer.from(JSON.stringify(obj), 'utf8').toString('base64');
}

function decodeB64Json(value) {
  if (!value) return null;
  try {
    return JSON.parse(Buffer.from(String(value), 'base64').toString('utf8'));
  } catch {
    try {
      return JSON.parse(Buffer.from(String(value), 'base64url').toString('utf8'));
    } catch {
      return null;
    }
  }
}

function requestFingerprint(tool, body, requirement) {
  const payload = {
    tool,
    method: body?.method,
    params: body?.params || {},
    requirement: {
      scheme: requirement.scheme,
      network: requirement.network,
      price: requirement.price,
      payTo: requirement.payTo,
      asset: requirement.asset || null,
      resource: requirement.resource,
    },
  };
  return createHash('sha256').update(JSON.stringify(payload)).digest('hex');
}

function x402Configured() {
  return Boolean(X402.enabled && X402.payTo && X402.facilitator && INTERNAL_PRO_TOKEN);
}

function paymentRequirement(tool, body) {
  const requirement = {
    scheme: X402.scheme,
    network: X402.network,
    price: X402.price,
    payTo: X402.payTo || 'UNCONFIGURED',
    resource: `${SITE}/api/mcp#${encodeURIComponent(tool)}`,
    description: `13FLOW Pro MCP tool ${tool}`,
    mimeType: 'application/json',
    maxTimeoutSeconds: 60,
    extra: {
      service: '13FLOW',
      tool,
      sha: CURRENT_SHA,
      purpose: 'read-only pro data access',
    },
  };
  if (X402.asset) requirement.asset = X402.asset;
  return {
    x402Version: 2,
    error: x402Configured() ? 'payment required' : 'x402 not configured on this resource server',
    accepts: [requirement],
    extensions: {
      paymentIdentifier: { required: false },
    },
    requestFingerprint: requestFingerprint(tool, body, requirement),
  };
}

function extractPaymentIdentifier(paymentPayload) {
  const ext = paymentPayload?.extensions || paymentPayload?.payload?.extensions || paymentPayload?.authorization?.extensions;
  if (!ext || typeof ext !== 'object') return null;
  const candidates = [
    ext.paymentIdentifier,
    ext.payment_identifier,
    ext['payment-identifier'],
    ext['https://x402.org/extensions/payment-identifier'],
  ];
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate) return candidate;
    if (candidate && typeof candidate === 'object') {
      const id = candidate.paymentId || candidate.payment_id || candidate.id;
      if (typeof id === 'string' && id) return id;
    }
  }
  return null;
}

async function postFacilitator(path, body) {
  const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
  if (X402.facilitatorAuth) headers.Authorization = X402.facilitatorAuth;
  const res = await fetch(`${X402.facilitator}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
  const text = await res.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = { raw: text.slice(0, 500) }; }
  if (!res.ok) {
    const e = new Error(`facilitator ${path} returned ${res.status}`);
    e.status = res.status;
    e.payload = payload;
    throw e;
  }
  return payload;
}

function validVerification(payload) {
  return payload?.isValid === true || payload?.valid === true || payload?.success === true;
}

function validSettlement(payload) {
  return payload?.success === true || payload?.settled === true || payload?.status === 'settled';
}

async function verifyAndSettlePayment(req, tool, body, required) {
  const header = req.headers['payment-signature'];
  if (!header) return { ok: false, reason: 'missing_payment_signature' };
  const paymentPayload = decodeB64Json(header);
  if (!paymentPayload) return { ok: false, reason: 'invalid_payment_signature' };

  const paymentId = extractPaymentIdentifier(paymentPayload);
  const fingerprint = required.requestFingerprint;
  if (paymentId) {
    const cached = paymentCache.get(paymentId);
    if (cached) {
      if (cached.fingerprint !== fingerprint) return { ok: false, status: 409, reason: 'payment_identifier_reused_for_different_request' };
      return { ok: true, cached: true, settlement: cached.settlement, paymentId };
    }
  }

  if (X402.testMode) {
    if (paymentPayload?.test !== true) return { ok: false, reason: 'test_mode_payment_missing_flag' };
    const settlement = { success: true, test: true, paymentId: paymentId || null };
    if (paymentId) paymentCache.set(paymentId, { at: Date.now(), fingerprint, settlement });
    return { ok: true, settlement, paymentId };
  }

  if (!x402Configured()) return { ok: false, reason: 'x402_not_configured' };

  const selected = required.accepts[0];
  const facilitatorBody = {
    x402Version: required.x402Version,
    paymentPayload,
    paymentRequirements: selected,
    paymentDetails: selected,
    resource: selected.resource,
    tool,
  };
  const verification = await postFacilitator('/verify', facilitatorBody);
  if (!validVerification(verification)) return { ok: false, reason: 'payment_verification_failed', verification };

  const settlement = await postFacilitator('/settle', facilitatorBody);
  if (!validSettlement(settlement)) return { ok: false, reason: 'payment_settlement_failed', settlement };
  if (paymentId) paymentCache.set(paymentId, { at: Date.now(), fingerprint, settlement });
  return { ok: true, settlement, paymentId };
}

function premiumToolFromBody(body) {
  const messages = Array.isArray(body) ? body : [body];
  for (const msg of messages) {
    if (msg?.method === 'tools/call' && PREMIUM_TOOLS.has(msg?.params?.name)) return msg.params.name;
  }
  return null;
}

function proHeadersFromRequest(req, paymentGrant = null) {
  const auth = req.headers.authorization;
  const key = req.headers['x-13flow-key'];
  if (auth) return { Authorization: String(auth) };
  if (key) return { 'X-13FLOW-Key': String(key) };
  if (paymentGrant && INTERNAL_PRO_TOKEN) return { Authorization: `Bearer ${INTERNAL_PRO_TOKEN}` };
  return {};
}

function hasProApiKey(req) {
  return Boolean(req.headers.authorization || req.headers['x-13flow-key']);
}

function reply(payload, text) {
  return {
    content: [{ type: 'text', text: text || '13FLOW returned structured JSON. Use structuredContent for deterministic parsing.' }],
    structuredContent: payload,
    isError: Boolean(payload?.error),
  };
}

function resourceJson(uri, payload) {
  return {
    contents: [{
      uri,
      mimeType: 'application/json',
      text: JSON.stringify(payload, null, 2),
    }],
  };
}

function resourceSummary(uri, name, description) {
  return { uri, name, description, mimeType: 'application/json' };
}

function cleanCik(value) {
  const cik = String(value || '').replace(/^0+/, '') || '0';
  if (!/^[0-9]{1,10}$/.test(cik)) throw new McpError(ErrorCode.InvalidParams, 'Invalid CIK');
  return cik.padStart(10, '0');
}

function cleanTicker(value) {
  const ticker = String(value || '').trim().toUpperCase();
  if (!/^[A-Z0-9.\-]{1,12}$/.test(ticker)) throw new McpError(ErrorCode.InvalidParams, 'Invalid ticker');
  return ticker;
}

function removeLiveNotificationCapabilities(server) {
  const capabilities = server.server.getCapabilities();
  for (const scope of ['resources', 'tools', 'prompts']) {
    if (!capabilities[scope]) continue;
    delete capabilities[scope].listChanged;
  }
  if (capabilities.resources) delete capabilities.resources.subscribe;
}

function buildServer(context = {}) {
  const server = new McpServer({ name: '13flow.eu', version: MCP_VERSION });
  const proHeaders = context.proHeaders || {};
  const paymentGrant = context.paymentGrant || null;

  async function getLiveStatus() {
    return fetchJson('/api/live-status');
  }

  async function getFunds() {
    const rows = await fetchJson('/api/funds');
    return { funds: rows, count: Array.isArray(rows) ? rows.length : 0, source: `${SITE}/api/funds` };
  }

  async function getFund(cik) {
    return fetchJson(`/api/fund/${encodeURIComponent(cleanCik(cik))}`);
  }

  async function getStock(ticker) {
    const clean = cleanTicker(ticker);
    try {
      return await fetchJson(`/api/stocks/${encodeURIComponent(clean)}`);
    } catch (e) {
      if (e.status !== 404) throw e;
      const legacy = await fetchJson('/api/mcp', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          jsonrpc: '2.0',
          id: 1,
          method: 'tools/call',
          params: { name: 'stocks.get', arguments: { ticker: clean } },
        }),
      });
      return legacy?.result?.structuredContent || legacy?.structuredContent || { error: 'stock_not_found', ticker: clean };
    }
  }

  async function getSignals(window, limit) {
    const params = new URLSearchParams();
    if (window) params.set('window', String(window));
    if (limit) params.set('limit', String(limit));
    return fetchJson(`/api/signals/confluence${params.size ? `?${params}` : ''}`);
  }

  async function getSignalHistory(ticker, window, limit) {
    const params = new URLSearchParams();
    if (ticker) params.set('ticker', cleanTicker(ticker));
    if (window) params.set('window', String(window));
    if (limit) params.set('limit', String(limit));
    return fetchJson(`/api/signals/confluence/history${params.size ? `?${params}` : ''}`);
  }

  async function getDataQuality(threshold, limit) {
    const params = new URLSearchParams();
    if (threshold) params.set('threshold', String(threshold));
    if (limit) params.set('limit', String(limit));
    return fetchJson(`/api/data-quality${params.size ? `?${params}` : ''}`);
  }

  async function getMethodology() {
    return fetchJson('/api/methodology/confluence-v1');
  }

  async function getOpenapi() {
    return fetchJson('/api/openapi.json');
  }

  async function getPro(path) {
    if (!Object.keys(proHeaders).length && !paymentGrant) {
      throw new McpError(ErrorCode.InvalidRequest, 'Pro authorization missing');
    }
    return fetchJson(path, { base: PRO_API_BASE, headers: proHeaders });
  }

  server.registerResource(
    'server',
    '13flow://mcp/server',
    { title: '13FLOW MCP server', description: 'Server metadata, transport, payment and safety contract.', mimeType: 'application/json' },
    async (uri) => resourceJson(uri.toString(), {
      ...SERVER_INFO,
      apiBase: API_BASE.replace(/^http:\/\/127\.0\.0\.1:\d+$/, 'local-gunicorn'),
      proApiBase: PRO_API_BASE.replace(/^http:\/\/127\.0\.0\.1:\d+$/, 'local-gunicorn-pro'),
      security: {
        readOnly: true,
        stateless: true,
        maxBodyBytes: MAX_BODY,
        rateLimitPerMinute: RATE_MAX,
        proToolsRequire: ['13FLOW Pro API key', 'x402 verified settlement'],
      },
      x402: {
        enabled: X402.enabled,
        configured: x402Configured(),
        scheme: X402.scheme,
        network: X402.network,
        price: X402.price,
        payToConfigured: Boolean(X402.payTo),
        facilitatorConfigured: Boolean(X402.facilitator),
        internalProTokenConfigured: Boolean(INTERNAL_PRO_TOKEN),
        testMode: X402.testMode,
      },
    }),
  );

  server.registerResource('live-status', '13flow://live-status',
    { title: 'Live status', description: 'Public LIVE/DEMO/DEGRADED proof from 13FLOW.', mimeType: 'application/json' },
    async (uri) => resourceJson(uri.toString(), await getLiveStatus()));
  server.registerResource('openapi', '13flow://openapi',
    { title: 'OpenAPI', description: 'Public OpenAPI document.', mimeType: 'application/json' },
    async (uri) => resourceJson(uri.toString(), await getOpenapi()));
  server.registerResource('methodology-confluence-v1', '13flow://methodology/confluence-v1',
    { title: 'Confluence v1 methodology', description: 'Frozen public research contract.', mimeType: 'application/json' },
    async (uri) => resourceJson(uri.toString(), await getMethodology()));
  server.registerResource('data-quality', '13flow://data-quality',
    { title: 'Data quality', description: 'Read-only quality warnings and unit-scale checks.', mimeType: 'application/json' },
    async (uri) => resourceJson(uri.toString(), await getDataQuality(100, 100)));
  server.registerResource('funds', '13flow://funds',
    { title: 'Tracked funds', description: 'Tracked 13F manager universe.', mimeType: 'application/json' },
    async (uri) => resourceJson(uri.toString(), await getFunds()));

  server.registerResource(
    'fund',
    new ResourceTemplate('13flow://funds/{cik}', {}),
    { title: 'Fund portfolio', description: 'Latest public fund portfolio by CIK.', mimeType: 'application/json' },
    async (uri, variables) => resourceJson(uri.toString(), await getFund(variables.cik)),
  );
  server.registerResource(
    'stock',
    new ResourceTemplate('13flow://stocks/{ticker}', {}),
    { title: 'Ticker holders', description: 'Latest public 13F holders for a ticker.', mimeType: 'application/json' },
    async (uri, variables) => resourceJson(uri.toString(), await getStock(variables.ticker)),
  );
  server.registerResource(
    'signal-history',
    new ResourceTemplate('13flow://signals/{ticker}/history', {}),
    { title: 'Signal history', description: 'Append-only Confluence signal revisions for a ticker.', mimeType: 'application/json' },
    async (uri, variables) => resourceJson(uri.toString(), await getSignalHistory(variables.ticker, undefined, 250)),
  );

  server.registerTool('get_live_status', {
    description: 'Return verifiable public state: LIVE/DEMO/DEGRADED, commit, generated_at, data_as_of, 13F period and coverage.',
    inputSchema: {},
    outputSchema: StatusOutput,
    annotations: { readOnlyHint: true },
  }, async () => reply(await getLiveStatus()));

  server.registerTool('list_funds', {
    description: 'List the public tracked 13F manager universe with latest public fields.',
    inputSchema: {},
    outputSchema: FundsOutput,
    annotations: { readOnlyHint: true },
  }, async () => reply(await getFunds()));

  server.registerTool('get_fund', {
    description: 'Get one public fund portfolio by SEC CIK. Public endpoint, read-only.',
    inputSchema: { cik: z.string().min(1).max(10).describe('SEC CIK, with or without leading zeroes.') },
    outputSchema: ToolOutput,
    annotations: { readOnlyHint: true },
  }, async ({ cik }) => reply(await getFund(cik)));

  server.registerTool('get_stock', {
    description: 'Get current public 13F holders for one ticker.',
    inputSchema: { ticker: z.string().min(1).max(12).describe('US ticker symbol.') },
    outputSchema: ToolOutput,
    annotations: { readOnlyHint: true },
  }, async ({ ticker }) => reply(await getStock(ticker)));

  server.registerTool('get_confluence_signals', {
    description: 'Return public cached Confluence v1 signals. Scores are ordinal heuristic ranks, not probabilities.',
    inputSchema: {
      window: z.number().int().min(7).max(365).default(90).describe('Confluence window in days.'),
      limit: z.number().int().min(1).max(500).default(100).describe('Maximum signals returned by the public API when supported.'),
    },
    outputSchema: ToolOutput,
    annotations: { readOnlyHint: true },
  }, async ({ window, limit }) => reply(await getSignals(window, limit)));

  server.registerTool('get_signal_history', {
    description: 'Read append-only Confluence signal revisions for audit and replay.',
    inputSchema: {
      ticker: z.string().min(1).max(12).optional(),
      window: z.number().int().min(7).max(365).optional(),
      limit: z.number().int().min(1).max(1000).default(100),
    },
    outputSchema: ToolOutput,
    annotations: { readOnlyHint: true },
  }, async ({ ticker, window, limit }) => reply(await getSignalHistory(ticker, window, limit)));

  server.registerTool('get_confluence_methodology', {
    description: 'Return the frozen Confluence v1 methodology contract, including proof boundary and validation requirements.',
    inputSchema: {},
    outputSchema: ToolOutput,
    annotations: { readOnlyHint: true },
  }, async () => reply(await getMethodology()));

  server.registerTool('get_data_quality', {
    description: 'Return public read-only data-quality warnings. These are review signals, never automatic corrections.',
    inputSchema: {
      threshold: z.number().min(1).max(10000).default(100),
      limit: z.number().int().min(1).max(500).default(100),
    },
    outputSchema: ToolOutput,
    annotations: { readOnlyHint: true },
  }, async ({ threshold, limit }) => reply(await getDataQuality(threshold, limit)));

  server.registerTool('get_payment_policy', {
    description: 'Explain the Pro MCP authorization policy: existing API keys and x402 paid calls.',
    inputSchema: {},
    outputSchema: PaymentOutput,
    annotations: { readOnlyHint: true },
  }, async () => reply({
    pro_api_key: { supported: true },
    x402: {
      enabled: X402.enabled,
      configured: x402Configured(),
      scheme: X402.scheme,
      network: X402.network,
      price: X402.price,
      payment_headers: ['PAYMENT-REQUIRED', 'PAYMENT-SIGNATURE', 'PAYMENT-RESPONSE'],
      fail_closed: true,
      internal_pro_token_configured: Boolean(INTERNAL_PRO_TOKEN),
    },
    premium_tools: Array.from(PREMIUM_TOOLS),
  }));

  server.registerTool('pro.list_funds', {
    description: 'Pro: list funds with richer series and quality summary. Requires a Pro API key or verified x402 settlement.',
    inputSchema: {},
    outputSchema: ToolOutput,
    annotations: { readOnlyHint: true },
  }, async () => reply(await getPro('/api/pro/v1/funds')));

  server.registerTool('pro.get_fund', {
    description: 'Pro: get institutional fund detail, selected filing, previous filing, positions, moves and methodology.',
    inputSchema: {
      cik: z.string().min(1).max(10),
      basis: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
      include_holds: z.boolean().default(false),
      limit_positions: z.number().int().min(1).max(500).default(100),
      limit_moves: z.number().int().min(1).max(1000).default(200),
    },
    outputSchema: ToolOutput,
    annotations: { readOnlyHint: true },
  }, async ({ cik, basis, include_holds, limit_positions, limit_moves }) => {
    const params = new URLSearchParams();
    if (basis) params.set('basis', basis);
    params.set('include_holds', include_holds ? '1' : '0');
    params.set('limit_positions', String(limit_positions));
    params.set('limit_moves', String(limit_moves));
    return reply(await getPro(`/api/pro/v1/fund/${encodeURIComponent(cleanCik(cik))}?${params}`));
  });

  server.registerTool('pro.get_data_quality', {
    description: 'Pro: data-quality report with the authenticated Pro contract and audit trail.',
    inputSchema: {
      threshold: z.number().min(1).max(10000).default(100),
      limit: z.number().int().min(1).max(500).default(100),
    },
    outputSchema: ToolOutput,
    annotations: { readOnlyHint: true },
  }, async ({ threshold, limit }) => {
    const params = new URLSearchParams();
    params.set('threshold', String(threshold));
    params.set('limit', String(limit));
    return reply(await getPro(`/api/pro/v1/data-quality?${params}`));
  });

  removeLiveNotificationCapabilities(server);
  return server;
}

async function handleMcpRequest(req, res, body) {
  const premiumTool = premiumToolFromBody(body);
  let paymentGrant = null;
  if (premiumTool && !hasProApiKey(req)) {
    const required = paymentRequirement(premiumTool, body);
    const settlement = await verifyAndSettlePayment(req, premiumTool, body, required);
    if (!settlement.ok) {
      const status = settlement.status || 402;
      return send(res, status, {
        jsonrpc: '2.0',
        id: Array.isArray(body) ? null : body?.id ?? null,
        error: {
          code: -32002,
          message: 'Payment Required',
          data: {
            reason: settlement.reason,
            tool: premiumTool,
            x402: { enabled: X402.enabled, configured: x402Configured() },
          },
        },
      }, {
        'PAYMENT-REQUIRED': encodeB64Json(required),
      });
    }
    paymentGrant = settlement;
    res.setHeader('PAYMENT-RESPONSE', encodeB64Json({
      success: true,
      cached: Boolean(settlement.cached),
      settlement: settlement.settlement || null,
      paymentId: settlement.paymentId || null,
    }));
  }

  const server = buildServer({ proHeaders: proHeadersFromRequest(req, paymentGrant), paymentGrant });
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: undefined,
    enableJsonResponse: true,
  });
  res.on('close', () => { transport.close(); server.close(); });
  await server.connect(transport);
  await transport.handleRequest(req, res, body);
}

const httpServer = http.createServer((req, res) => {
  if (!hostAllowed(req)) return send(res, 421, { error: 'host not allowed' });
  if (!originAllowed(req)) return send(res, 403, { error: 'origin not allowed' });

  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  if (url.pathname === '/healthz') {
    return fetchJson('/api/live-status')
      .then((live) => send(res, 200, {
        ok: true,
        server: SERVER_INFO,
        live: {
          public_state: live?.public_state || null,
          generated_at: live?.generated_at || null,
          data_as_of: live?.data_as_of || null,
          latest_13f: live?.period_13f?.to || null,
        },
        x402: { enabled: X402.enabled, configured: x402Configured() },
      }))
      .catch((e) => send(res, 503, {
        ok: false,
        server: SERVER_INFO,
        error: e.message || 'live status unavailable',
        x402: { enabled: X402.enabled, configured: x402Configured() },
      }));
  }

  if (url.pathname !== MCP_PATH) return send(res, 404, { error: 'not found' });
  if (req.method === 'GET' || req.method === 'DELETE') {
    return send(res, 405, { jsonrpc: '2.0', error: { code: -32000, message: 'Method Not Allowed' }, id: null }, { Allow: 'POST' });
  }
  if (req.method !== 'POST') return send(res, 405, { error: 'method not allowed' }, { Allow: 'POST' });
  if (rateLimited(clientIp(req))) return send(res, 429, { error: 'too many requests' });

  let raw = '';
  let tooBig = false;
  req.on('data', (chunk) => {
    raw += chunk;
    if (raw.length > MAX_BODY) {
      tooBig = true;
      req.destroy();
    }
  });
  req.on('end', async () => {
    if (tooBig) return send(res, 413, { error: 'payload too large' });
    let body;
    try {
      body = raw ? JSON.parse(raw) : undefined;
    } catch {
      return send(res, 400, { jsonrpc: '2.0', error: { code: -32700, message: 'Parse error' }, id: null });
    }
    try {
      await handleMcpRequest(req, res, body);
    } catch (e) {
      if (!res.headersSent) {
        send(res, 500, { jsonrpc: '2.0', error: { code: -32603, message: 'Internal error' }, id: Array.isArray(body) ? null : body?.id ?? null });
      }
      console.error('[13flow-mcp] request failed:', e);
    }
  });
});

httpServer.listen(PORT, HOST, () => {
  console.error(`[13flow-mcp] listening on http://${HOST}:${PORT}${MCP_PATH} -> public ${API_BASE}, pro ${PRO_API_BASE}`);
});
