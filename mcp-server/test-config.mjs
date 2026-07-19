import { spawnSync } from 'node:child_process';
import { chmodSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const server = fileURLToPath(new URL('./server.mjs', import.meta.url));
const packageJson = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'));
const packageLock = JSON.parse(readFileSync(new URL('./package-lock.json', import.meta.url), 'utf8'));
const registryManifest = JSON.parse(readFileSync(new URL('../server.json', import.meta.url), 'utf8'));
if (packageJson.version !== packageLock.version
    || packageJson.version !== packageLock.packages?.['']?.version
    || packageJson.version !== registryManifest.version) {
  throw new Error('MCP release versions are not aligned');
}
if (!registryManifest.remotes?.some((remote) => remote.type === 'streamable-http'
    && remote.url === 'https://13flow.eu/api/mcp')) {
  throw new Error('Registry manifest does not declare the canonical HTTPS MCP endpoint');
}
console.log(`release contract: ${packageJson.version} aligned`);
const cleanKeys = [
  'NODE_ENV',
  'MCP_HOST', 'MCP_PORT', 'MCP_PATH', 'MCP_PUBLIC_SITE', 'MCP_13FLOW_API_BASE',
  'MCP_13FLOW_PRO_API_BASE', 'MCP_ALLOWED_HOSTS', 'MCP_ALLOWED_ORIGINS',
  'MCP_STATS_FILE', 'MCP_STATS_RETENTION_DAYS',
  'MCP_PRO_TOOLS_ENABLED', 'MCP_X402_ENABLED', 'MCP_X402_TEST_MODE',
  'MCP_X402_PAY_TO', 'MCP_X402_FACILITATOR_URL', 'MCP_X402_FACILITATOR_AUTH',
  'MCP_X402_FACILITATOR_AUTH_FILE', 'MCP_13FLOW_INTERNAL_PRO_TOKEN',
  'MCP_13FLOW_INTERNAL_PRO_TOKEN_FILE',
];

function expectStartupFailure(label, overrides, expectedMessage) {
  const env = { ...process.env };
  for (const key of cleanKeys) delete env[key];
  env.NODE_ENV = 'test';
  Object.assign(env, overrides);
  const result = spawnSync(process.execPath, [server], {
    env,
    encoding: 'utf8',
    timeout: 3000,
  });
  const output = `${result.stderr || ''}\n${result.stdout || ''}`;
  if (result.error?.code === 'ETIMEDOUT') throw new Error(`${label}: server started instead of failing closed`);
  if (result.status === 0) throw new Error(`${label}: startup unexpectedly succeeded`);
  if (!output.includes(expectedMessage)) {
    throw new Error(`${label}: expected ${JSON.stringify(expectedMessage)}, got ${JSON.stringify(output.slice(0, 1000))}`);
  }
  console.log(`${label}: rejected`);
}

expectStartupFailure('invalid integer config', { MCP_PORT: '8849junk' }, 'MCP_PORT must be an integer');
expectStartupFailure('invalid telemetry retention', { MCP_STATS_RETENTION_DAYS: '365' }, 'MCP_STATS_RETENTION_DAYS must be an integer');
expectStartupFailure('invalid boolean config', { MCP_PRO_TOOLS_ENABLED: 'perhaps' }, 'must be one of');
expectStartupFailure('non-loopback production bind', {
  NODE_ENV: 'production', MCP_HOST: '0.0.0.0',
}, 'MCP_HOST must be loopback in production');
expectStartupFailure('remote production public API', {
  NODE_ENV: 'production', MCP_13FLOW_API_BASE: 'https://api.example',
}, 'MCP_13FLOW_API_BASE must be loopback in production');
expectStartupFailure('unsafe production telemetry path', {
  NODE_ENV: 'production', MCP_STATS_FILE: '/tmp/agent-stats.json',
}, 'MCP_STATS_FILE must be /var/lib/13flow-mcp/agent-stats.json in production');
expectStartupFailure('Pro/public API boundary collapse', {
  NODE_ENV: 'production', MCP_PRO_TOOLS_ENABLED: '1',
  MCP_13FLOW_API_BASE: 'http://127.0.0.1:8000',
  MCP_13FLOW_PRO_API_BASE: 'http://127.0.0.1:8000',
}, 'Public and Pro API bases must be isolated in production');
expectStartupFailure('x402 without Pro tools', {
  MCP_X402_ENABLED: '1',
}, 'requires MCP_PRO_TOOLS_ENABLED=1');
expectStartupFailure('x402 test mode in production', {
  NODE_ENV: 'production', MCP_X402_TEST_MODE: '1',
}, 'must never be enabled in production');
expectStartupFailure('missing production release SHA', {
  NODE_ENV: 'production',
}, 'MCP_GIT_SHA is required in production');
expectStartupFailure('inline production secret', {
  NODE_ENV: 'production',
  MCP_PRO_TOOLS_ENABLED: '1',
  MCP_X402_ENABLED: '1',
  MCP_13FLOW_API_BASE: 'http://127.0.0.1:8000',
  MCP_13FLOW_PRO_API_BASE: 'http://127.0.0.1:8001',
  MCP_X402_PAY_TO: 'test-destination',
  MCP_X402_FACILITATOR_URL: 'https://facilitator.example',
  MCP_13FLOW_INTERNAL_PRO_TOKEN: 'must-not-be-inline',
}, 'must use the _FILE form in production');

const secretDir = mkdtempSync(join(tmpdir(), '13flow-mcp-secret-test-'));
try {
  const looseSecret = join(secretDir, 'loose');
  writeFileSync(looseSecret, 'test-token\n', { mode: 0o644 });
  expectStartupFailure('world-readable secret file', {
    MCP_PRO_TOOLS_ENABLED: '1',
    MCP_X402_ENABLED: '1',
    MCP_X402_PAY_TO: 'test-destination',
    MCP_X402_FACILITATOR_URL: 'https://facilitator.example',
    MCP_13FLOW_INTERNAL_PRO_TOKEN_FILE: looseSecret,
  }, 'must not be accessible to other users');

  const targetSecret = join(secretDir, 'target');
  const linkedSecret = join(secretDir, 'linked');
  writeFileSync(targetSecret, 'test-token\n', { mode: 0o600 });
  chmodSync(targetSecret, 0o600);
  symlinkSync(targetSecret, linkedSecret);
  expectStartupFailure('symlinked secret file', {
    MCP_PRO_TOOLS_ENABLED: '1',
    MCP_X402_ENABLED: '1',
    MCP_X402_PAY_TO: 'test-destination',
    MCP_X402_FACILITATOR_URL: 'https://facilitator.example',
    MCP_13FLOW_INTERNAL_PRO_TOKEN_FILE: linkedSecret,
  }, 'unreadable or unsafe');
} finally {
  rmSync(secretDir, { recursive: true, force: true });
}
