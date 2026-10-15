'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
function actual(name) {
  const match = source.match(new RegExp(`(?:^|\\n)(?:async\\s+)?function\\s+${name}\\s*\\([^]*?\\n\\}`, 'm'));
  assert.ok(match);
  return match[0];
}
function harness(replies) {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, {hidden: false, textContent: '', innerHTML: '', style: {},
      insertAdjacentHTML(_where, html) { this.innerHTML += html; }});
    return nodes.get(id);
  };
  const pending = [];
  const S = {jobId: 'synthetic-job', cursor: 12, pollFails: 0};
  const ctx = {$, S, clearTimeout() {}, setTimeout(fn, delay) { pending.push({fn, delay}); },
    async api() { const value = replies.shift(); if (value instanceof Error) throw value; return value; },
    esc: String, renderStream() {}, renderPartial() {}, saveJobRecovery() {}, showBanner() {}};
  vm.createContext(ctx);
  vm.runInContext([actual('classifyPollError'), actual('showResume'), actual('poll')].join('\n'), ctx);
  ctx.poll(0);
  return {$, S, pending, async tick() { assert.ok(pending.length); await pending.shift().fn(); }};
}
function network() { return Object.assign(new Error('connection refused'), {status: 0}); }
function response(state) { return {state, events: [], event_cursor: 13, unit_done: 2, unit_total: 8, calls: 4, elapsed: 10}; }

test('first network failure immediately shows unknown server state and preserves retry cursor', async () => {
  const h = harness([network()]);
  await h.tick();
  assert.equal(h.$('#p2h').textContent, '② 连接中断');
  assert.match(h.$('#p2tag').textContent, /状态待确认.*1s.*自动重试/);
  assert.equal(h.$('#spin').hidden, true);
  assert.equal(h.$('#st_eta').hidden, true);
  assert.doesNotMatch(h.$('#stream').innerHTML, /仍在.*跑|仍在.*运行/);
  assert.equal(h.pending[0].delay, 1000);
  assert.equal(h.S.cursor, 12);
});

for (const state of ['running', 'queued', 'cancelled', 'error']) {
  test(`successful ${state} response clears disconnected display`, async () => {
    const h = harness([network(), response(state)]);
    await h.tick();
    await h.tick();
    assert.equal(h.S.pollFails, 0);
    assert.equal(h.S.cursor, 13);
    assert.equal(h.$('#p2resume').hidden, true);
    assert.doesNotMatch(h.$('#p2h').textContent + h.$('#p2tag').textContent, /连接中断|状态待确认|自动重试/);
    assert.equal(h.$('#spin').hidden, !['running', 'queued'].includes(state));
    assert.equal(h.pending.length, ['running', 'queued'].includes(state) ? 1 : 0);
  });
}

test('401 stops retries without claiming that the server task is running', async () => {
  const h = harness([Object.assign(new Error('unauthorized'), {status: 401}), response('running')]);
  await h.tick();
  assert.match(h.$('#p2h').textContent, /登录已失效/);
  assert.match(h.$('#p2tag').textContent, /状态待确认/);
  assert.doesNotMatch(h.$('#stream').innerHTML, /仍在.*跑|仍在.*运行/);
  assert.equal(h.$('#spin').hidden, true);
  assert.equal(h.$('#p2resume').hidden, false);
  assert.equal(h.pending.length, 0);
  h.$('#btnresume').onclick();
  assert.match(h.$('#p2h').textContent, /正在重新连接/);
  assert.match(h.$('#p2tag').textContent, /状态待确认/);
  await h.tick();
  assert.equal(h.$('#p2h').textContent, '② 探测中');
  assert.equal(h.$('#spin').hidden, false);
});

test('twenty consecutive failures expose manual resume without claiming a one-minute duration', async () => {
  const h = harness(Array.from({length: 20}, network));
  for (let i = 0; i < 20; i++) await h.tick();
  assert.equal(h.pending.length, 0);
  assert.equal(h.$('#p2resume').hidden, false);
  assert.match(h.$('#p2tag').textContent, /状态待确认.*已停止自动重试/);
  assert.doesNotMatch(actual('poll'), /约 1 分钟/);
});
