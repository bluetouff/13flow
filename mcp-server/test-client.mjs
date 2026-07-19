import { readFileSync } from 'node:fs';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const URL_ = process.env.URL || 'http://127.0.0.1:8849/mcp';
const EXPECTED_VERSION = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')).version;
const EXPECT_PRO_TOOLS = process.env.EXPECT_PRO_TOOLS === '1';
const JSON_HEADERS = {
  'Content-Type': 'application/json',
  Accept: 'application/json, text/event-stream',
};

async function expectHttpStatus(label, expected, options) {
  const response = await fetch(URL_, options);
  const expectedStatuses = Array.isArray(expected) ? expected : [expected];
  if (!expectedStatuses.includes(response.status)) {
    throw new Error(`${label}: expected HTTP ${expectedStatuses.join(' or ')}, got ${response.status}`);
  }
  return response;
}

await expectHttpStatus('GET transport rejection', 405, { method: 'GET' });
await expectHttpStatus('invalid Origin rejection', 403, {
  method: 'POST',
  headers: { ...JSON_HEADERS, Origin: 'https://evil.example' },
  body: '{}',
});
await expectHttpStatus('invalid Host rejection', [400, 421], {
  method: 'POST',
  headers: { ...JSON_HEADERS, Host: 'evil.example' },
  body: '{}',
});
await expectHttpStatus('missing JSON content type', 415, {
  method: 'POST',
  headers: { Accept: JSON_HEADERS.Accept },
  body: '{}',
});
await expectHttpStatus('incomplete MCP Accept header', 406, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
  body: '{}',
});
await expectHttpStatus('malformed JSON rejection', 400, {
  method: 'POST',
  headers: JSON_HEADERS,
  body: '{',
});
await expectHttpStatus('JSON-RPC batch rejection', 400, {
  method: 'POST',
  headers: JSON_HEADERS,
  body: JSON.stringify([{
    jsonrpc: '2.0',
    id: 1,
    method: 'tools/list',
    params: {},
  }]),
});
await expectHttpStatus('unsupported MCP protocol version', 400, {
  method: 'POST',
  headers: { ...JSON_HEADERS, 'MCP-Protocol-Version': '1900-01-01' },
  body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list', params: {} }),
});

const oversizeTestBytes = Number.parseInt(process.env.OVERSIZE_TEST_BYTES || '0', 10);
if (oversizeTestBytes > 0) {
  await expectHttpStatus('oversized body rejection', 413, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'tools/list',
      params: { padding: 'x'.repeat(oversizeTestBytes) },
    }),
  });
}

const proCallBody = JSON.stringify({
  jsonrpc: '2.0',
  id: 1,
  method: 'tools/call',
  params: { name: 'pro.list_funds', arguments: {} },
});
if (EXPECT_PRO_TOOLS) {
  await expectHttpStatus('Pro tool fails closed without key or payment', 402, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: proCallBody,
  });
  await expectHttpStatus('client Authorization is not accepted as a Pro credential', 402, {
    method: 'POST',
    headers: { ...JSON_HEADERS, Authorization: 'Bearer client-oauth-token-must-not-pass-through' },
    body: proCallBody,
  });
  await expectHttpStatus('malformed Pro key fails closed', 402, {
    method: 'POST',
    headers: { ...JSON_HEADERS, 'X-13FLOW-Key': 'short' },
    body: proCallBody,
  });
}

await expectHttpStatus('OPTIONS transport rejection', 405, {
  method: 'OPTIONS',
  headers: JSON_HEADERS,
  body: JSON.stringify({
    jsonrpc: '2.0', id: 1, method: 'tools/list', params: {},
  }),
});

const transport = new StreamableHTTPClientTransport(new URL(URL_));
const client = new Client({ name: '13flow-mcp-test', version: '1.0.0' });
await client.connect(transport);

const capabilities = client.getServerCapabilities?.() || {};
if (capabilities.resources?.subscribe || capabilities.resources?.listChanged) {
  throw new Error('Server must not advertise live resource subscriptions');
}

if (process.env.SKIP_HEALTH !== '1') {
  const healthUrl = process.env.HEALTH_URL || new URL('/healthz', URL_).toString();
  const health = await fetch(healthUrl).then((res) => res.json());
  if (!health.ok || health.server?.version !== EXPECTED_VERSION || !health.server?.shaStatus) {
    throw new Error('healthz is incomplete or version-drifted');
  }
  console.log('healthz:', health.server.version, health.live?.public_state || 'unknown');
}

const statsUrl = process.env.STATS_URL || new URL('/stats', URL_).toString();
const stats = await fetch(statsUrl).then((response) => {
  if (!response.ok) throw new Error(`agent stats endpoint returned HTTP ${response.status}`);
  return response.json();
});
if (stats.schema_version !== 'agent_stats_v1'
    || stats.privacy?.stores_ip_addresses !== false
    || stats.privacy?.stores_arguments_prompts_or_responses !== false) {
  throw new Error('agent stats endpoint violates its aggregate privacy contract');
}

const { tools } = await client.listTools();
const toolNames = tools.map((tool) => tool.name);
console.log('tools:', toolNames.join(', '));
for (const required of [
  'get_live_status',
  'get_product_status',
  'get_research_readiness',
  'get_agent_stats',
  'list_funds',
  'get_fund',
  'get_stock',
  'preview_watchlist',
  'discover_watchlist',
  'get_confluence_signals',
  'get_signal_history',
  'get_confluence_methodology',
  'get_data_quality',
]) {
  if (!toolNames.includes(required)) throw new Error(`missing tool: ${required}`);
}
const proTools = ['get_payment_policy', 'pro.list_funds', 'pro.get_fund', 'pro.get_data_quality'];
for (const name of proTools) {
  if (EXPECT_PRO_TOOLS && !toolNames.includes(name)) throw new Error(`missing optional Pro tool: ${name}`);
  if (!EXPECT_PRO_TOOLS && toolNames.includes(name)) throw new Error(`Pro tool must be absent by default: ${name}`);
}
if (toolNames.includes('get_pro_offer')) throw new Error('retired get_pro_offer tool must not be advertised');
for (const tool of tools) {
  if (tool.annotations?.readOnlyHint !== true || tool.annotations?.destructiveHint !== false) {
    throw new Error(`unsafe or incomplete annotations: ${tool.name}`);
  }
}

const { resources } = await client.listResources();
const resourceUris = resources.map((resource) => resource.uri);
for (const required of [
  '13flow://mcp/server',
  '13flow://live-status',
  '13flow://product-status',
  '13flow://research-readiness',
  '13flow://agent-stats',
  '13flow://openapi',
  '13flow://methodology/confluence-v1',
  '13flow://data-quality',
  '13flow://funds',
]) {
  if (!resourceUris.includes(required)) throw new Error(`missing resource: ${required}`);
}
if (resourceUris.includes('13flow://pro-offer')) throw new Error('retired pro-offer resource must not be advertised');

const { resourceTemplates } = await client.listResourceTemplates();
const templates = resourceTemplates.map((template) => template.uriTemplate);
for (const required of ['13flow://funds/{cik}', '13flow://stocks/{ticker}', '13flow://signals/{ticker}/history']) {
  if (!templates.includes(required)) throw new Error(`missing resource template: ${required}`);
}

async function call(name, args = {}) {
  const result = await client.callTool({ name, arguments: args });
  if (!result.structuredContent || typeof result.structuredContent !== 'object') {
    throw new Error(`missing structuredContent for ${name}`);
  }
  if (result.isError) throw new Error(`${name} returned an execution error`);
  return result.structuredContent;
}

function assertBounded(name, payload, maxBytes) {
  const bytes = Buffer.byteLength(JSON.stringify(payload));
  if (bytes > maxBytes) throw new Error(`${name} context is too large: ${bytes} > ${maxBytes}`);
  console.log(`${name}:`, `${bytes} bytes`);
}

const status = await call('get_live_status');
if (!status.public_state || !status.source_url || !status.canonical_url) throw new Error('live status is incomplete');

const product = await call('get_product_status');
if (!product.validation?.current_artifact || product.research_readiness?.x402 !== 'not_enabled') {
  throw new Error('product status boundary is incomplete');
}

const readiness = await call('get_research_readiness');
if (!readiness.status || readiness.public_access_status?.mcp_boundary === undefined) {
  throw new Error('research readiness boundary is incomplete');
}

const agentStats = await call('get_agent_stats');
if (agentStats.schema_version !== 'agent_stats_v1'
    || agentStats.privacy?.initializations_are_not_unique_users !== true
    || agentStats.privacy?.stores_user_agents !== false) {
  throw new Error('get_agent_stats privacy or measurement boundary is incomplete');
}

const funds = await call('list_funds', { limit: 5 });
if (!Array.isArray(funds.funds) || funds.returned < 1 || funds.returned > 5 || funds.total < funds.returned) {
  throw new Error('fund pagination shape is invalid');
}
assertBounded('list_funds', funds, 15_000);

const firstCik = funds.funds[0].cik;
const fund = await call('get_fund', { cik: firstCik, position_limit: 2, move_limit: 2, history_limit: 2 });
if (!fund.result_window || fund.positions.length > 2 || fund.moves.length > 2 || !fund.canonical_url) {
  throw new Error('bounded fund detail shape is invalid');
}

const firstTicker = fund.positions.find((position) => position.ticker)?.ticker || 'AAPL';
const stock = await call('get_stock', { ticker: firstTicker, holder_limit: 3, movement_limit: 3 });
if (!stock.ticker || stock.holders.length > 3 || stock.movements.length > 3 || !stock.result_window) {
  throw new Error('bounded stock detail shape is invalid');
}
assertBounded('get_stock', stock, 20_000);

const preview = await call('preview_watchlist', { tickers: [firstTicker], movement_limit: 1 });
if (preview.metadata?.version !== 'watchlist_preview_v1' || preview.items?.some((item) => item.top_movements.length > 1)) {
  throw new Error('watchlist preview shape is invalid');
}
assertBounded('preview_watchlist', preview, 20_000);

const discovery = await call('discover_watchlist', { limit: 5, movement_limit: 1 });
if (discovery.metadata?.version !== 'watchlist_discovery_v1' || (discovery.items?.length || 0) > 5) {
  throw new Error('watchlist discovery shape is invalid');
}

const signals = await call('get_confluence_signals', { window: 90, limit: 3 });
if (!signals.result_window || (signals.signals?.length || 0) > 3) {
  throw new Error('bounded Confluence signal shape is invalid');
}
assertBounded('get_confluence_signals', signals, 25_000);

if (EXPECT_PRO_TOOLS) {
  const paymentPolicy = await call('get_payment_policy');
  if (!paymentPolicy.x402 || paymentPolicy.x402.fail_closed !== true) {
    throw new Error('payment policy does not declare fail-closed x402');
  }
}

await client.close();
