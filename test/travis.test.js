import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { TravisClient } from '../src/travis.js';
import { trimLog } from '../src/format.js';
import { parseAllowedWrites } from '../src/config.js';

test('client only ever issues GET requests with API v3 headers', async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify({ builds: [], jobs: [] }), { status: 200 });
  };
  const client = new TravisClient({ token: 't', fetchImpl });

  await client.getBuild(1);
  await client.getBuildJobs(1);
  await client.listBuilds('owner/repo', { branch: 'main', limit: 5 });

  assert.equal(calls.length, 3);
  for (const { init } of calls) {
    assert.equal(init.method, 'GET');
    assert.equal(init.headers['Travis-API-Version'], '3');
    assert.equal(init.headers.Authorization, 'token t');
  }
  assert.match(calls[2].url, /\/repo\/owner%2Frepo\/builds\?branch\.name=main&limit=5/);
});

test('writes are limited to build restart/cancel, anything else is refused before any request', async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), method: init.method });
    return new Response(JSON.stringify({ '@type': 'pending' }), { status: 202 });
  };
  const client = new TravisClient({ token: 't', fetchImpl });

  await client.restartBuild(42);
  await client.cancelBuild(42);
  assert.deepEqual(
    calls.map(({ url, method }) => `${method} ${new URL(url).pathname}`),
    ['POST /build/42/restart', 'POST /build/42/cancel'],
  );

  for (const path of [
    '/repo/1/requests',
    '/repo/1/env_vars',
    '/job/42/restart',
    '/job/42/log',
    '/build/42/restart/../../repo/1/requests',
    '/build/abc/cancel',
  ]) {
    assert.throws(() => client.post(path), /Refusing write/, path);
  }
  assert.equal(calls.length, 2);
});

test('source never issues PUT/PATCH/DELETE nor touches env vars, settings or build requests', () => {
  const source = readFileSync(new URL('../src/travis.js', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /'(PUT|PATCH|DELETE)'/);
  assert.doesNotMatch(source, /env_vars|settings|\/requests/);
});

test('TRAVIS_ALLOW_WRITE is opt-in and rejects unknown actions', () => {
  assert.deepEqual(parseAllowedWrites(undefined), []);
  assert.deepEqual(parseAllowedWrites(''), []);
  assert.deepEqual(parseAllowedWrites(' Restart , cancel,restart '), ['restart', 'cancel']);
  assert.throws(() => parseAllowedWrites('restart,trigger'), /unknown action\(s\) trigger/);
});

test('client refuses to start without a token', () => {
  assert.throws(() => new TravisClient({ token: '' }), /TRAVIS_API_TOKEN/);
});

test('trimLog strips ANSI codes and keeps the tail', () => {
  const raw = ['\u001b[32mok\u001b[0m', 'a', 'b', 'c'].join('\n');
  const log = trimLog(raw, { tailLines: 2 });
  assert.equal(log.text, 'b\nc');
  assert.equal(log.totalLines, 4);
  assert.equal(log.truncated, true);
  assert.equal(trimLog(raw, {}).text.startsWith('ok'), true);
});

test('trimLog drops travis_time and travis_fold markers', () => {
  const raw = ['travis_fold:start:install', '$ composer install', 'travis_time:end:abc:start=1', 'done'].join('\n');
  assert.equal(trimLog(raw, {}).text, '$ composer install\ndone');
});

test('trimLog grep returns matches with context and gap markers', () => {
  const raw = Array.from({ length: 20 }, (_, i) => (i === 10 ? 'PHPUnit FAILURES!' : `line ${i}`)).join('\n');
  const log = trimLog(raw, { grep: 'failures', contextLines: 1 });
  assert.equal(log.text, '--- line 10 ---\nline 9\nPHPUnit FAILURES!\nline 11');
});
