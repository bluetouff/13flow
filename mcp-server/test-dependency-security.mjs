import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { PassThrough } from 'node:stream';
import { gzipSync, deflateSync, brotliCompressSync } from 'node:zlib';
import bodyParser from 'body-parser';
import uri from 'fast-uri';
import qs from 'qs';
import { Address6, AddressError } from 'ip-address';
import { z } from 'zod';
import { z as protocolZod } from 'zod/v4';

const failures = [];
async function check(label, run) {
  try {
    await run();
    console.log(`${label}: passed`);
  } catch {
    failures.push(label);
    console.error(`${label}: failed`);
  }
}

// Observe cleanup during successful parsing of ordinary compressed JSON. No
// malformed compressed input or crash-inducing trailing data is needed.
await check('body-parser decoder cleanup and ordinary compressed JSON', async () => {
  for (const [encoding, compress] of [['gzip', gzipSync], ['deflate', deflateSync], ['br', brotliCompressSync]]) {
    const expected = { fixture: 'ordinary JSON' };
    const encoded = compress(Buffer.from(JSON.stringify(expected)));
    const req = new PassThrough();
    req.headers = { 'content-type': 'application/json', 'content-encoding': encoding,
      'content-length': String(encoded.length) };
    let decoder;
    const pipe = req.pipe;
    req.pipe = function (destination, ...args) {
      decoder = destination;
      return pipe.call(this, destination, ...args);
    };
    try {
      await new Promise((resolve, reject) => {
        bodyParser.json()(req, {}, (error) => {
          if (error) return reject(error);
          try {
            assert.deepEqual(req.body, expected);
            assert.ok(decoder, 'compressed input created a decoder');
            assert.equal(decoder.destroyed, true, 'decoder is disposed before downstream handler');
            assert.ok(decoder.listenerCount('error') > 0, 'late decoder errors remain handled');
            resolve();
          } catch (failure) { reject(failure); }
        });
        req.end(encoded);
      });
    } finally {
      req.destroy();
      decoder?.destroy();
    }
  }
});

await check('ip-address linear zone parsing and valid scoped IPv6', () => {
  const require = createRequire(import.meta.url);
  const { RE_ZONE_STRING } = require('ip-address/dist/v6/constants.js');
  const original = RE_ZONE_STRING.exec;
  let legacyScans = 0;
  RE_ZONE_STRING.exec = function (...args) { legacyScans += 1; return original.apply(this, args); };
  try {
    const scoped = new Address6('fe80::1%fixture/64');
    assert.equal(scoped.zone, '%fixture');
    for (const terminator of ['\n', '\r', '\u2028', '\u2029']) {
      const candidate = `fe80::1%fixture${terminator}`;
      assert.equal(Address6.isValid(candidate), false);
      assert.throws(() => new Address6(candidate), AddressError);
    }
    assert.equal(legacyScans, 0, 'deprecated backtracking expression is never invoked');
  } finally {
    delete RE_ZONE_STRING.exec;
  }
});

await check('fast-uri authority rejection and legitimate URI controls', () => {
  for (const scheme of ['http', 'https', 'ws', 'wss']) {
    for (const authority of ['one@two@example.test', 'ex\tample.test', 'ex\nample.test', 'ex%09ample.test']) {
      assert.ok(uri.parse(`${scheme}://${authority}/`).error, 'malformed authority fails closed');
    }
  }
  assert.throws(() => uri.resolve('https://example.test/base', '//ex\tample.test/path'));
  const ordinary = uri.parse('https://fixture@example.test:8443/path');
  assert.equal(ordinary.error, undefined);
  assert.equal(ordinary.host, 'example.test');
  assert.equal(ordinary.port, 8443);
  assert.equal(uri.resolve('https://example.test/base/', '../item'), 'https://example.test/item');
});

await check('qs comma serialization handles non-coercible objects', () => {
  for (const encodeValuesOnly of [false, true]) {
    for (const value of [{ toString: 'fixture' }, Object.create(null), [{ toString: null }]]) {
      assert.doesNotThrow(() => qs.stringify({ fixture: [value] }, { arrayFormat: 'comma', encodeValuesOnly }));
    }
  }
  assert.equal(qs.stringify({ fixture: ['one', 'two'] }, { arrayFormat: 'comma' }), 'fixture=one%2Ctwo');
});

await check('Zod root and SDK schemas reject excess collections before element validation', () => {
  for (const schemaLibrary of [z, protocolZod]) {
    let visits = 0;
    const element = schemaLibrary.number().superRefine(() => { visits += 1; });
    const schema = schemaLibrary.array(element).max(2);
    assert.equal(schema.safeParse([1, 2, 3]).success, false);
    assert.equal(visits, 0, 'size rejection precedes per-element work');
    assert.deepEqual(schema.parse([1, 2]), [1, 2]);
    assert.equal(visits, 2, 'valid elements retain their validation');
  }
});

assert.equal(failures.length, 0, `Dependency security checks failed: ${failures.join(', ')}`);
