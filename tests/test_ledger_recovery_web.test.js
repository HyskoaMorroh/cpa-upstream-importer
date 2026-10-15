'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
function actual(name) {
  const match = source.match(new RegExp(`(?:^|\\n)(?:async\\s+)?function\\s+${name}\\s*\\([^]*?\\n\\}`, 'm'));
  assert.ok(match, name);
  return match[0];
}

function policyView(policy) {
  const box = {textContent: ''};
  const ctx = {$: () => box, S: {ctx: {probe_policy: policy}}};
  vm.createContext(ctx);
  vm.runInContext(actual('renderProbePolicy'), ctx);
  ctx.renderProbePolicy();
  return box.textContent;
}

test('unknown reservation ownership is visible without declaring another process dead or clearing budget', () => {
  const text = policyView({mode: 'managed', authorized_sites: 8,
    reservation_recovery_status: 'unresolved', orphaned_inflight_count: 2, inflight_count: 1});
  assert.match(text, /2 个未结算请求的持有者无法确认/);
  assert.match(text, /历史请求结果待核对/);
  assert.match(text, /相关服务商暂停新请求/);
  assert.match(text, /不会自动重发.*不代表上游未执行/);
  assert.match(text, /当前执行实例确认有 1 个请求尚未结算/);
  assert.doesNotMatch(text, /已配置 8|进程已死|清空|已恢复|实测通过/);
});

test('active and clear reservations keep their different meanings and broken ledger takes precedence', () => {
  const active = policyView({mode: 'managed', reservation_recovery_status: 'active', inflight_count: 3});
  assert.match(active, /当前执行实例确认有 3 个请求尚未结算/);
  assert.doesNotMatch(active, /历史请求结果待核对/);
  const clear = policyView({mode: 'managed', reservation_recovery_status: 'clear', inflight_count: 0});
  assert.match(clear, /受控主动探测/);
  assert.doesNotMatch(clear, /尚未结算|历史请求结果待核对/);
  assert.equal(policyView({ledger_blocked: true, reason: '账本不可读',
    reservation_recovery_status: 'unresolved', inflight_count: 1}), '账本不可读');
});

test('unresolved status remains visible if its diagnostic count is unavailable', () => {
  assert.match(policyView({mode: 'managed', reservation_recovery_status: 'unresolved'}),
    /部分未结算请求的持有者无法确认/);
});

test('orphan event never pretends to be ordinary active waiting and escapes the reason', () => {
  let html = '';
  const ctx = {$: () => ({insertAdjacentHTML(_where, value) { html += value; }, scrollHeight: 0}),
    tag: host => host};
  vm.createContext(ctx);
  const escaping = source.slice(source.indexOf('const esc ='), source.indexOf('\nconst fmt ='));
  vm.runInContext(`${escaping}\n${actual('renderStream')}`, ctx);
  ctx.renderStream([{kind: 'policy-wait', code: 'orphaned_inflight',
    host: 'fixture.invalid', reason: '<script>unsafe</script>'}]);
  assert.match(html, /历史请求结果待核对/);
  assert.match(html, /未发送新请求/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /等待同服务商的当前请求完成|<script>/);
});

test('restart identifies lost tasks without equating lost local ownership to unexecuted requests', () => {
  const banners = [];
  const ctx = {S: {}, showBanner(message) { banners.push(message); }};
  vm.createContext(ctx);
  vm.runInContext(actual('noteBootId'), ctx);
  ctx.noteBootId('first-boot');
  assert.equal(banners.length, 0);
  ctx.noteBootId('second-boot');
  assert.equal(ctx.S.bootChanged, true);
  assert.match(banners[0], /核对探测账本和已写入配置/);
  assert.match(banners[0], /历史请求结果待核对.*重新提交不会解除阻塞/);
  assert.doesNotMatch(banners[0], /请重新提交|请重新发起探测/);
});

for (const status of [404, 410]) {
  test(`expired job ${status} stops polling and asks for evidence review before another probe`, async () => {
    const nodes = new Map();
    const $ = id => {
      if (!nodes.has(id)) nodes.set(id, {hidden: false, textContent: '', innerHTML: '',
        insertAdjacentHTML(_where, html) { this.innerHTML += html; }});
      return nodes.get(id);
    };
    const pending = [];
    const banners = [];
    const ctx = {$, S: {jobId: 'prior-job', planTask: {id: 'prior-plan'}, cursor: 3},
      clearTimeout() {}, setTimeout(fn) { pending.push(fn); }, esc: String,
      async api() { throw Object.assign(new Error('task unavailable'), {status}); },
      saveJobRecovery() {}, showBanner(message) { banners.push(message); }};
    vm.createContext(ctx);
    vm.runInContext(`${actual('classifyPollError')}\n${actual('poll')}`, ctx);
    ctx.poll(0);
    await pending.shift()();
    assert.equal(pending.length, 0);
    assert.equal(ctx.S.jobId, null);
    assert.equal(ctx.S.planTask, null);
    assert.equal($('#spin').hidden, true);
    assert.match($('#stream').innerHTML, /历史请求结果待核对.*重新提交不会解除阻塞/);
    assert.match(banners[0], /不能据此判断上游请求未执行/);
    assert.doesNotMatch($('#stream').innerHTML + banners[0], /请重新提交|请重新发起探测/);
  });
}
