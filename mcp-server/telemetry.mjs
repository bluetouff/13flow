import { closeSync, constants as fsConstants, fstatSync, openSync, readFileSync } from 'node:fs';
import { rename, writeFile } from 'node:fs/promises';
import { performance } from 'node:perf_hooks';

const SCHEMA_VERSION = 'agent_stats_v1';
const MAX_FILE_BYTES = 1024 * 1024;
const CLIENT_FAMILIES = new Set([
  'Codex', 'ChatGPT', 'Claude', 'Cursor', 'Windsurf', 'VS Code', 'Cline',
  'MCP Inspector', '13FLOW probes', 'Other MCP clients',
]);
const COUNTER_KEYS = [
  'http_requests', 'http_2xx', 'http_4xx', 'http_5xx', 'rpc_requests',
  'initializations', 'tool_calls', 'tool_successes', 'tool_errors',
  'tool_duration_ms', 'tool_max_ms',
];

function safeCount(value, maximum = Number.MAX_SAFE_INTEGER) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 0) return 0;
  return Math.min(number, maximum);
}

function safeDuration(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return 0;
  return Math.min(Math.round(number), 60_000);
}

function safeIso(value, fallback) {
  if (typeof value !== 'string' || value.length > 40) return fallback;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? fallback : parsed.toISOString();
}

function emptyCounters() {
  return Object.fromEntries(COUNTER_KEYS.map((key) => [key, 0]));
}

function normalizeCounters(value) {
  const counters = emptyCounters();
  for (const key of COUNTER_KEYS) counters[key] = safeCount(value?.[key]);
  return counters;
}

function normalizeClientMap(value) {
  const output = {};
  if (!value || typeof value !== 'object' || Array.isArray(value)) return output;
  for (const family of CLIENT_FAMILIES) {
    const count = safeCount(value[family]);
    if (count) output[family] = count;
  }
  return output;
}

function validToolName(name) {
  return typeof name === 'string' && /^[A-Za-z0-9._-]{1,64}$/.test(name);
}

function normalizeToolMap(value) {
  const output = {};
  if (!value || typeof value !== 'object' || Array.isArray(value)) return output;
  for (const [name, counters] of Object.entries(value).slice(0, 64)) {
    if (!validToolName(name)) continue;
    output[name] = normalizeCounters(counters);
  }
  return output;
}

function clientFamily(name) {
  const value = String(name || '').slice(0, 200).toLowerCase();
  if (/13flow.*(?:test|probe)|release-audit|security-test/.test(value)) return '13FLOW probes';
  if (/codex/.test(value)) return 'Codex';
  if (/chatgpt|openai/.test(value)) return 'ChatGPT';
  if (/claude|anthropic/.test(value)) return 'Claude';
  if (/cursor/.test(value)) return 'Cursor';
  if (/windsurf|codeium/.test(value)) return 'Windsurf';
  if (/visual studio|vscode/.test(value)) return 'VS Code';
  if (/\bcline\b/.test(value)) return 'Cline';
  if (/mcp.?inspector|\binspector\b/.test(value)) return 'MCP Inspector';
  return 'Other MCP clients';
}

function dayKey(date) {
  return date.toISOString().slice(0, 10);
}

function dayKeyOffset(date, offsetDays) {
  const shifted = new Date(date.getTime());
  shifted.setUTCDate(shifted.getUTCDate() + offsetDays);
  return dayKey(shifted);
}

function emptyDay() {
  return { ...emptyCounters(), clients: {}, tools: {} };
}

function normalizeDay(value) {
  return {
    ...normalizeCounters(value),
    clients: normalizeClientMap(value?.clients),
    tools: normalizeToolMap(value?.tools),
  };
}

function addCounter(target, key, amount = 1) {
  target[key] = safeCount((target[key] || 0) + amount);
}

function summarizeDays(days) {
  const summary = { ...emptyCounters(), clients: {}, tools: {} };
  for (const day of days) {
    for (const key of COUNTER_KEYS) {
      if (key === 'tool_max_ms') {
        summary[key] = Math.max(safeCount(summary[key]), safeCount(day[key]));
      } else {
        addCounter(summary, key, safeCount(day[key]));
      }
    }
    for (const [family, count] of Object.entries(day.clients || {})) {
      summary.clients[family] = safeCount((summary.clients[family] || 0) + count);
    }
    for (const [name, counters] of Object.entries(day.tools || {})) {
      if (!summary.tools[name]) summary.tools[name] = emptyCounters();
      for (const key of COUNTER_KEYS) {
        if (key === 'tool_max_ms') {
          summary.tools[name][key] = Math.max(
            safeCount(summary.tools[name][key]),
            safeCount(counters[key]),
          );
        } else {
          addCounter(summary.tools[name], key, safeCount(counters[key]));
        }
      }
    }
  }
  return summary;
}

function publicSummary(counters) {
  const calls = safeCount(counters.tool_calls);
  const successes = safeCount(counters.tool_successes);
  return {
    http_requests: safeCount(counters.http_requests),
    http_2xx: safeCount(counters.http_2xx),
    http_4xx: safeCount(counters.http_4xx),
    http_5xx: safeCount(counters.http_5xx),
    rpc_requests: safeCount(counters.rpc_requests),
    initializations: safeCount(counters.initializations),
    tool_calls: calls,
    tool_successes: successes,
    tool_errors: safeCount(counters.tool_errors),
    total_tool_duration_ms: safeCount(counters.tool_duration_ms),
    tool_success_rate: calls ? Number((successes / calls).toFixed(4)) : null,
    average_tool_latency_ms: calls ? Math.round(safeCount(counters.tool_duration_ms) / calls) : null,
    max_tool_latency_ms: safeCount(counters.tool_max_ms),
  };
}

function publicBreakdown(map) {
  return Object.entries(map || {})
    .map(([name, counters]) => ({ name, ...publicSummary(counters) }))
    .sort((a, b) => b.tool_calls - a.tool_calls || a.name.localeCompare(b.name));
}

export class AgentTelemetry {
  constructor({ file = '', retentionDays = 30, serverVersion = 'unknown', gitSha = 'unknown', now, logger } = {}) {
    this.file = file;
    this.retentionDays = Math.max(7, Math.min(90, safeCount(retentionDays) || 30));
    this.serverVersion = String(serverVersion || 'unknown').slice(0, 40);
    this.gitSha = /^[0-9a-f]{40}$/i.test(String(gitSha || '')) ? String(gitSha).toLowerCase() : 'unknown';
    this.now = typeof now === 'function' ? now : () => new Date();
    this.logger = typeof logger === 'function' ? logger : () => {};
    this.dirty = false;
    this.pendingFlush = null;
    this.loadStatus = file ? 'new' : 'disabled';
    this.state = this.#newState();
    if (file) this.#load();
  }

  #newState() {
    const timestamp = this.now().toISOString();
    return {
      schema_version: SCHEMA_VERSION,
      since: timestamp,
      updated_at: timestamp,
      totals: emptyCounters(),
      clients: {},
      tools: {},
      days: {},
    };
  }

  #load() {
    let fd;
    try {
      fd = openSync(this.file, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
      const stat = fstatSync(fd);
      if (!stat.isFile() || stat.size < 2 || stat.size > MAX_FILE_BYTES) throw new Error('invalid telemetry file size or type');
      if ((stat.mode & 0o077) !== 0) throw new Error('telemetry file permissions must be 0600 or stricter');
      if (typeof process.getuid === 'function' && stat.uid !== process.getuid()) {
        throw new Error('telemetry file must be owned by the service user');
      }
      const raw = JSON.parse(readFileSync(fd, 'utf8'));
      if (raw?.schema_version !== SCHEMA_VERSION) throw new Error('unsupported telemetry schema');
      const fallback = this.now().toISOString();
      this.state = {
        schema_version: SCHEMA_VERSION,
        since: safeIso(raw.since, fallback),
        updated_at: safeIso(raw.updated_at, fallback),
        totals: normalizeCounters(raw.totals),
        clients: normalizeClientMap(raw.clients),
        tools: normalizeToolMap(raw.tools),
        days: {},
      };
      for (const [day, value] of Object.entries(raw.days || {})) {
        if (/^\d{4}-\d{2}-\d{2}$/.test(day)) this.state.days[day] = normalizeDay(value);
      }
      this.#prune();
      this.loadStatus = 'loaded';
    } catch (error) {
      if (error?.code !== 'ENOENT') {
        this.loadStatus = 'reset_after_invalid_file';
        this.dirty = true;
        this.logger(`telemetry reset: ${error?.message || 'invalid file'}`);
      }
    } finally {
      if (fd !== undefined) closeSync(fd);
    }
  }

  #prune() {
    const now = this.now();
    const first = dayKeyOffset(now, -(this.retentionDays - 1));
    const last = dayKey(now);
    for (const key of Object.keys(this.state.days)) {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(key) || key < first || key > last) delete this.state.days[key];
    }
  }

  #day() {
    const key = dayKey(this.now());
    if (!this.state.days[key]) this.state.days[key] = emptyDay();
    this.#prune();
    return this.state.days[key];
  }

  #touch() {
    this.state.updated_at = this.now().toISOString();
    this.dirty = true;
  }

  recordHttp(status) {
    const code = safeCount(status, 999);
    const day = this.#day();
    addCounter(this.state.totals, 'http_requests');
    addCounter(day, 'http_requests');
    const key = code >= 500 ? 'http_5xx' : code >= 400 ? 'http_4xx' : code >= 200 && code < 300 ? 'http_2xx' : null;
    if (key) {
      addCounter(this.state.totals, key);
      addCounter(day, key);
    }
    this.#touch();
  }

  recordRpc(body) {
    if (!body || typeof body !== 'object' || Array.isArray(body)) return;
    const day = this.#day();
    addCounter(this.state.totals, 'rpc_requests');
    addCounter(day, 'rpc_requests');
    if (body.method === 'initialize') {
      const family = clientFamily(body.params?.clientInfo?.name);
      addCounter(this.state.totals, 'initializations');
      addCounter(day, 'initializations');
      this.state.clients[family] = safeCount((this.state.clients[family] || 0) + 1);
      day.clients[family] = safeCount((day.clients[family] || 0) + 1);
    }
    this.#touch();
  }

  recordTool(name, success, durationMs) {
    if (!validToolName(name)) return;
    const duration = safeDuration(durationMs);
    const day = this.#day();
    if (!this.state.tools[name]) this.state.tools[name] = emptyCounters();
    if (!day.tools[name]) day.tools[name] = emptyCounters();
    for (const target of [this.state.totals, day, this.state.tools[name], day.tools[name]]) {
      addCounter(target, 'tool_calls');
      addCounter(target, success ? 'tool_successes' : 'tool_errors');
      addCounter(target, 'tool_duration_ms', duration);
      target.tool_max_ms = Math.max(safeCount(target.tool_max_ms), duration);
    }
    this.#touch();
  }

  wrapTool(name, handler) {
    return async (...args) => {
      const started = performance.now();
      let success = false;
      try {
        const result = await handler(...args);
        success = result?.isError !== true;
        return result;
      } finally {
        this.recordTool(name, success, performance.now() - started);
      }
    };
  }

  snapshot() {
    this.#prune();
    const orderedDays = Object.entries(this.state.days).sort(([a], [b]) => a.localeCompare(b));
    const daily = orderedDays.map(([date, value]) => ({
      date,
      ...publicSummary(value),
      clients: { ...value.clients },
      tools: publicBreakdown(value.tools),
    }));
    const last = (count) => {
      const first = dayKeyOffset(this.now(), -(count - 1));
      return summarizeDays(orderedDays.filter(([date]) => date >= first).map(([, value]) => value));
    };
    return {
      schema_version: SCHEMA_VERSION,
      generated_at: this.now().toISOString(),
      updated_at: this.state.updated_at,
      since: this.state.since,
      retention_days: this.retentionDays,
      server: { version: this.serverVersion, git_sha: this.gitSha },
      windows: { '7d': publicSummary(last(7)), '30d': publicSummary(last(30)) },
      lifetime: publicSummary(this.state.totals),
      clients: Object.entries(this.state.clients).map(([family, count]) => ({ family, initializations: count }))
        .sort((a, b) => b.initializations - a.initializations || a.family.localeCompare(b.family)),
      tools: publicBreakdown(this.state.tools),
      daily,
      privacy: {
        aggregate_only: true,
        stores_ip_addresses: false,
        stores_user_agents: false,
        stores_client_versions: false,
        stores_arguments_prompts_or_responses: false,
        client_family_source: 'self-declared MCP clientInfo.name mapped to fixed families',
        initializations_are_not_unique_users: true,
      },
      source: {
        endpoint: '/api/agent-stats',
        grain: 'UTC day, fixed client family and registered tool name',
        persistence: this.file ? this.loadStatus : 'process-memory-only',
      },
    };
  }

  async flush(force = false) {
    if (!this.file || (!this.dirty && !force)) return false;
    if (this.pendingFlush) return this.pendingFlush;
    const payload = `${JSON.stringify(this.state)}\n`;
    if (Buffer.byteLength(payload) > MAX_FILE_BYTES) throw new Error('telemetry payload exceeds file limit');
    const temporary = `${this.file}.${process.pid}.tmp`;
    this.dirty = false;
    this.pendingFlush = (async () => {
      try {
        await writeFile(temporary, payload, { encoding: 'utf8', mode: 0o600 });
        await rename(temporary, this.file);
        this.loadStatus = 'loaded';
        return true;
      } catch (error) {
        this.dirty = true;
        this.logger(`telemetry flush failed: ${error?.message || 'write failed'}`);
        return false;
      } finally {
        this.pendingFlush = null;
      }
    })();
    return this.pendingFlush;
  }
}
