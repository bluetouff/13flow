#!/usr/bin/env node

import { readFile } from 'node:fs/promises';

function argument(name, fallback = null) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

const manifest = JSON.parse(await readFile(new URL('../server.json', import.meta.url), 'utf8'));
const expectedVersion = argument('--version', manifest.version);
const attempts = Number.parseInt(argument('--attempts', '1'), 10);
const delayMs = Number.parseInt(argument('--delay-ms', '5000'), 10);
const expectedRemote = manifest.remotes.find((remote) => remote.type === 'streamable-http')?.url;
if (!expectedVersion || !expectedRemote) throw new Error('Registry version and Streamable HTTP remote are required');

const endpoint = new URL('https://registry.modelcontextprotocol.io/v0.1/servers');
endpoint.searchParams.set('search', manifest.name);

let lastError;
for (let attempt = 1; attempt <= attempts; attempt += 1) {
  try {
    const response = await fetch(endpoint, {
      headers: { Accept: 'application/json', 'User-Agent': '13flow-mcp-registry-verifier/1' },
      signal: AbortSignal.timeout(15_000),
    });
    if (!response.ok) throw new Error(`Registry HTTP ${response.status}`);
    const payload = await response.json();
    const entries = (payload.servers || []).filter((entry) => (entry.server ?? entry).name === manifest.name);
    const exact = entries.find((entry) => {
      const server = entry.server ?? entry;
      return server.version === expectedVersion
        && server.remotes?.some((remote) => remote.type === 'streamable-http' && remote.url === expectedRemote);
    });
    if (!exact) throw new Error(`${manifest.name} ${expectedVersion} is absent with remote ${expectedRemote}`);
    const official = exact._meta?.['io.modelcontextprotocol.registry/official'];
    process.stdout.write(`${JSON.stringify({
      ok: true,
      name: manifest.name,
      version: expectedVersion,
      remote: expectedRemote,
      isLatest: official?.isLatest ?? null,
    })}\n`);
    process.exit(0);
  } catch (error) {
    lastError = error;
    process.stderr.write(`Registry attempt ${attempt}/${attempts}: ${error.message}\n`);
    if (attempt < attempts) await new Promise((resolve) => setTimeout(resolve, delayMs));
  }
}
throw lastError;
