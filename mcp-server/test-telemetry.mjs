import { chmodSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { AgentTelemetry } from './telemetry.mjs';

const directory = mkdtempSync(join(tmpdir(), '13flow-agent-stats-'));
const file = join(directory, 'stats.json');
let clock = new Date('2026-07-01T12:00:00.000Z');
const now = () => new Date(clock.getTime());

try {
  const telemetry = new AgentTelemetry({
    file,
    retentionDays: 30,
    serverVersion: '1.0.0',
    gitSha: 'a'.repeat(40),
    now,
  });
  telemetry.recordHttp(200);
  telemetry.recordHttp(403);
  telemetry.recordRpc({
    method: 'initialize',
    params: { clientInfo: { name: 'Codex super-secret-client-id', version: 'private-version' } },
  });
  const successful = telemetry.wrapTool('get_live_status', async (args) => ({
    structuredContent: { ok: true, ignored: args.token },
    isError: false,
  }));
  await successful({ token: 'super-secret-tool-argument' });
  const failing = telemetry.wrapTool('get_stock', async () => { throw new Error('private-upstream-detail'); });
  await failing({ ticker: 'SECRET' }).catch(() => {});
  telemetry.recordTool('get_live_status', true, 20);
  telemetry.recordTool('get_stock', true, 5_000);
  await telemetry.flush(true);

  const raw = readFileSync(file, 'utf8');
  for (const forbidden of [
    'super-secret-client-id', 'private-version', 'super-secret-tool-argument',
    'private-upstream-detail', 'SECRET', '127.0.0.1', 'user-agent',
  ]) {
    if (raw.includes(forbidden)) throw new Error(`telemetry persisted forbidden value: ${forbidden}`);
  }

  const snapshot = telemetry.snapshot();
  if (snapshot.windows['7d'].initializations !== 1 || snapshot.windows['7d'].tool_calls !== 4) {
    throw new Error('7-day telemetry summary does not reconcile');
  }
  if (snapshot.windows['7d'].tool_successes !== 3 || snapshot.windows['7d'].tool_errors !== 1) {
    throw new Error('tool outcome telemetry does not reconcile');
  }
  if (snapshot.windows['7d'].max_tool_latency_ms !== 5_000
      || snapshot.windows['7d'].total_tool_duration_ms < 5_020) {
    throw new Error('tool duration telemetry does not reconcile');
  }
  if (snapshot.clients[0]?.family !== 'Codex' || snapshot.privacy.stores_arguments_prompts_or_responses !== false) {
    throw new Error('privacy-safe client aggregation is invalid');
  }

  const reloaded = new AgentTelemetry({ file, retentionDays: 30, now });
  if (reloaded.snapshot().lifetime.tool_calls !== 4 || reloaded.snapshot().source.persistence !== 'loaded') {
    throw new Error('persisted telemetry did not reload');
  }

  clock = new Date('2026-08-01T12:00:00.000Z');
  reloaded.recordRpc({ method: 'initialize', params: { clientInfo: { name: 'Claude' } } });
  const pruned = reloaded.snapshot();
  if (pruned.daily.some((day) => day.date === '2026-07-01')) throw new Error('expired daily telemetry was retained');
  if (pruned.windows['30d'].initializations !== 1 || pruned.lifetime.initializations !== 2) {
    throw new Error('retention window and lifetime counters do not reconcile');
  }

  chmodSync(file, 0o644);
  const unsafe = new AgentTelemetry({ file, retentionDays: 30, now });
  if (unsafe.snapshot().source.persistence !== 'reset_after_invalid_file'
      || unsafe.snapshot().lifetime.tool_calls !== 0) {
    throw new Error('unsafe telemetry file permissions did not fail closed');
  }

  console.log('agent telemetry: persistence, privacy, outcomes, retention and file mode verified');
} finally {
  rmSync(directory, { recursive: true, force: true });
}
