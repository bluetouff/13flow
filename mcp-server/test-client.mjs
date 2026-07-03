import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const URL_ = process.env.URL || 'http://127.0.0.1:8849/mcp';
const transport = new StreamableHTTPClientTransport(new URL(URL_));
const client = new Client({ name: '13flow-mcp-test', version: '1.0.0' });
await client.connect(transport);

const capabilities = client.getServerCapabilities?.() || {};
if (capabilities.resources?.subscribe || capabilities.resources?.listChanged) {
  throw new Error('Server must not advertise live resource subscriptions');
}

const healthUrl = new URL('/healthz', URL_).toString();
const health = await fetch(healthUrl).then((res) => res.json());
if (!health.ok || !health.server?.version || !health.server?.shaStatus) {
  throw new Error('healthz is incomplete');
}
console.log('healthz:', health.server.version, health.live?.public_state || 'unknown');

const { tools } = await client.listTools();
const toolNames = tools.map((tool) => tool.name);
console.log('tools:', toolNames.join(', '));
for (const required of [
  'get_live_status',
  'get_product_status',
  'list_funds',
  'get_fund',
  'get_stock',
  'get_confluence_signals',
  'get_signal_history',
  'get_confluence_methodology',
  'get_data_quality',
  'get_payment_policy',
  'pro.list_funds',
  'pro.get_fund',
  'pro.get_data_quality',
]) {
  if (!toolNames.includes(required)) throw new Error(`missing tool: ${required}`);
}

const { resources } = await client.listResources();
const resourceUris = resources.map((resource) => resource.uri);
for (const required of [
  '13flow://mcp/server',
  '13flow://live-status',
  '13flow://product-status',
  '13flow://openapi',
  '13flow://methodology/confluence-v1',
  '13flow://data-quality',
  '13flow://funds',
]) {
  if (!resourceUris.includes(required)) throw new Error(`missing resource: ${required}`);
}

const { resourceTemplates } = await client.listResourceTemplates();
const templates = resourceTemplates.map((template) => template.uriTemplate);
for (const required of ['13flow://funds/{cik}', '13flow://stocks/{ticker}', '13flow://signals/{ticker}/history']) {
  if (!templates.includes(required)) throw new Error(`missing template: ${required}`);
}

async function call(name, args = {}) {
  const result = await client.callTool({ name, arguments: args });
  if (!result.structuredContent || typeof result.structuredContent !== 'object') {
    throw new Error(`missing structuredContent for ${name}`);
  }
  return result.structuredContent;
}

const status = await call('get_live_status');
if (!status.public_state) throw new Error('live status has no public_state');
console.log('live:', status.public_state, status.git_sha || status.commit || 'unknown');

const product = await call('get_product_status');
if (!product.validation?.current_artifact || product.commercial_readiness?.x402 !== 'not_enabled') {
  throw new Error('product status boundary is incomplete');
}
console.log('product:', product.validation.status);

const funds = await call('list_funds');
if (!Array.isArray(funds.funds) || funds.count < 1) throw new Error('fund list is empty');
console.log('funds:', funds.count);

const firstCik = funds.funds[0].cik;
const fund = await call('get_fund', { cik: firstCik });
if (!fund.fund && !fund.label && !fund.filing) throw new Error('fund detail shape is unexpected');
console.log('fund:', firstCik);

const stock = await call('get_stock', { ticker: 'TSM' });
if (!stock.ticker) throw new Error('stock lookup shape is unexpected');
console.log('stock:', stock.ticker, stock.holder_count ?? 'n/a');

const paymentPolicy = await call('get_payment_policy');
if (!paymentPolicy.x402 || paymentPolicy.x402.fail_closed !== true) {
  throw new Error('payment policy does not declare fail-closed x402');
}
console.log('payment:', JSON.stringify(paymentPolicy.x402));

await client.close();
