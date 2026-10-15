'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
function sourceFunction(name) {
  const m = source.match(new RegExp(`(?:^|\\n)(?:async\\s+)?function\\s+${name}\\s*\\([^]*?\\n\\}`, 'm'));
  assert.ok(m);
  return m[0];
}
function recoveryContext(storage = new Map()) {
  const nodes = new Map();
  const ctx = {
    S: {jobId: 'job-fixture', cursor: 0, picks: new Set(['1\u0000codex']), overrides: {}, forced: {}},
    sessionStorage: {getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k)},
    $: k => { if (!nodes.has(k)) nodes.set(k, {checked: true, hidden: true, textContent: ''}); return nodes.get(k); },
    step() {}, poll() { ctx.polls = (ctx.polls || 0) + 1; },
  };
  vm.createContext(ctx);
  for (const name of ['planInputKey', 'saveJobRecovery', 'restoreJobRecovery']) vm.runInContext(sourceFunction(name), ctx);
  return {ctx, storage};
}
test('refresh restores the same safe job and input-bound planning task', () => {
  const {ctx, storage} = recoveryContext();
  ctx.S.planTask = {id: 'task-fixture', inputKey: ctx.planInputKey()};
  ctx.saveJobRecovery();
  const restored = recoveryContext(storage).ctx;
  restored.S.jobId = null;
  assert.equal(restored.restoreJobRecovery(), true);
  assert.equal(restored.S.jobId, 'job-fixture');
  assert.equal(restored.S.planTask.id, 'task-fixture');
  assert.equal(restored.S.planTask.inputKey, restored.planInputKey());
  assert.equal(restored.polls, 1);
  assert.deepEqual([...restored.S.picks], ['1\u0000codex']);
});
test('a task with stale inputs is not restored after refresh', () => {
  const {ctx, storage} = recoveryContext();
  ctx.S.planTask = {id: 'stale-task', inputKey: 'different-input'};
  ctx.saveJobRecovery();
  assert.equal(JSON.parse(storage.get('importer_job_recovery')).task, null);
});
test('manual secrets never enter recovery storage and repeated refresh stays guarded', () => {
  const {ctx, storage} = recoveryContext();
  ctx.S.overrides = {headers: {'Authorization': 'synthetic-secret'}};
  ctx.S.forced = {model: 'synthetic-model'};
  ctx.S.planTask = {id: 'task-fixture', inputKey: ctx.planInputKey()};
  ctx.saveJobRecovery();
  assert.doesNotMatch(storage.get('importer_job_recovery'), /synthetic-secret|Authorization|synthetic-model/);
  const restored = recoveryContext(storage).ctx;
  assert.equal(restored.restoreJobRecovery(), true);
  assert.equal(restored.S.planTask, null);
  assert.equal(restored.S.recoveryNeedsEdits, true);
  restored.saveJobRecovery();
  assert.equal(JSON.parse(storage.get('importer_job_recovery')).needsEdits, true);
});
test('expired and invalid recovery records are removed without polling', () => {
  for (const patch of [{savedAt: 0}, {jobId: 'https://invalid'}, {selected: ['secret']}, {task: '<bad>'}]) {
    const {ctx, storage} = recoveryContext();
    ctx.saveJobRecovery();
    storage.set('importer_job_recovery', JSON.stringify({...JSON.parse(storage.get('importer_job_recovery')), ...patch}));
    assert.equal(ctx.restoreJobRecovery(), false);
    assert.equal(storage.has('importer_job_recovery'), false);
    assert.equal(ctx.polls, undefined);
  }
});
test('finishing or restarting without a job clears recovery', () => {
  const {ctx, storage} = recoveryContext();
  ctx.saveJobRecovery();
  ctx.S.jobId = null;
  ctx.saveJobRecovery();
  assert.equal(storage.has('importer_job_recovery'), false);
});
async function tuning(diff, advices = [{item: 'request-retry', advice_key: 'request-retry', changed: true, current: 1, want: 2}]) {
  const nodes = new Map();
  const ctx = {
    TN: {id: ''}, TN_SEV: {warn: {cls: 'warn', label: 'Warning'}},
    esc: x => String(x ?? ''),
    $: k => { if (!nodes.has(k)) nodes.set(k, {hidden: true, disabled: false, innerHTML: '', textContent: '', insertAdjacentHTML() {}}); return nodes.get(k); },
    api: async () => ({tuning_id: 'synthetic-preview', attempt_sec: 3, edge_window_sec: 120,
      tiers: [], notes: [], problems: [], diff, advices})
  };
  vm.createContext(ctx);
  vm.runInContext(sourceFunction('tnInvalidatePreview'), ctx);
  vm.runInContext(sourceFunction('tnRun'), ctx);
  await ctx.tnRun();
  return {ctx, nodes};
}
test('a tuning advice list without a diff still exposes the write-back button', async () => {
  // Contract from 2026-10-01: an empty or absent diff must not hide the only
  // execution entry point, because the advice list is already actionable.
  const {ctx, nodes} = await tuning(undefined);
  assert.equal(nodes.get('#tnpreview').hidden, false);
  assert.equal(ctx.TN.id, 'synthetic-preview');
  assert.match(nodes.get('#tndiff').textContent, /没有回 diff/);
  assert.equal(nodes.get('#tncnt').textContent, '1 处');
});
test('an empty diff string behaves like an absent diff', async () => {
  const {nodes} = await tuning('');
  assert.equal(nodes.get('#tnpreview').hidden, false);
  assert.match(nodes.get('#tndiff').textContent, /没有回 diff/);
});
test('no changed advice keeps the write-back button hidden', async () => {
  const {nodes} = await tuning('-a: 1\n+a: 2', [{item: 'request-retry', changed: false, current: 2}]);
  assert.equal(nodes.get('#tnpreview').hidden, true);
});
test('a real tuning preview remains actionable with its readable label', async () => {
  const {ctx, nodes} = await tuning('-request-retry: 1\n+request-retry: 2');
  assert.equal(nodes.get('#tnpreview').hidden, false);
  assert.equal(ctx.TN.id, 'synthetic-preview');
  assert.match(nodes.get('#tnlist').innerHTML, /request-retry/);
});
test('a failed tuning refresh invalidates the previous actionable preview', async () => {
  const {ctx, nodes} = await tuning('fixture diff');
  ctx.api = async () => { throw new Error('fixture connection interrupted'); };
  await ctx.tnRun();
  assert.equal(ctx.TN.id, '');
  assert.equal(nodes.get('#tnpreview').hidden, true);
  assert.equal(nodes.get('#tnapply').disabled, true);
  assert.match(nodes.get('#tnbasis').innerHTML, /connection interrupted/);
});
test('a cancelled tuning request cannot resurrect an actionable preview', async () => {
  const {ctx, nodes} = await tuning('fixture diff');
  let complete;
  ctx.api = () => new Promise(resolve => { complete = resolve; });
  const pending = ctx.tnRun();
  ctx.tnInvalidatePreview();
  complete({tuning_id: 'obsolete', advices: [{changed: true}]});
  await pending;
  assert.equal(ctx.TN.id, '');
  assert.equal(nodes.get('#tnpreview').hidden, true);
  assert.equal(nodes.get('#tnapply').disabled, true);
});

function bulkPreviewContext() {
  const nodes = new Map();
  const ctx = {
    BM: {bulkId: 'previous-preview', revision: 'fixture-revision', sel: new Set()},
    esc: value => String(value ?? ''),
    $: key => {
      if (!nodes.has(key)) nodes.set(key, {hidden: false, disabled: false,
        innerHTML: '', textContent: '', scrollIntoView() {}});
      return nodes.get(key);
    },
    bmOps: () => [{op: 'fixture'}],
    api: async () => ({changed: 1, bulk_id: 'new-preview', diff: 'fixture diff'}),
  };
  vm.createContext(ctx);
  for (const name of ['bmInvalidatePreview', 'bmPreview']) vm.runInContext(sourceFunction(name), ctx);
  return {ctx, nodes};
}
test('bulk no-op selection invalidates an older writeback preview', async () => {
  const {ctx, nodes} = bulkPreviewContext();
  ctx.bmOps = () => [];
  await ctx.bmPreview('enable', true);
  assert.equal(ctx.BM.bulkId, '');
  assert.equal(nodes.get('#bmpreview').hidden, true);
  assert.equal(nodes.get('#bmapply').disabled, true);
});
test('an older bulk response cannot replace the latest preview', async () => {
  const {ctx, nodes} = bulkPreviewContext();
  let complete;
  ctx.api = () => new Promise(resolve => { complete = resolve; });
  const oldRequest = ctx.bmPreview('enable', true);
  ctx.api = async () => ({changed: 2, bulk_id: 'latest-preview', diff: 'latest diff'});
  await ctx.bmPreview('enable', false);
  complete({changed: 1, bulk_id: 'obsolete-preview', diff: 'obsolete diff'});
  await oldRequest;
  assert.equal(ctx.BM.bulkId, 'latest-preview');
  assert.equal(nodes.get('#bmdiff').textContent, 'latest diff');
});
test('a malformed bulk result never enables writing', async () => {
  for (const response of [{changed: 1}, {changed: NaN, bulk_id: 'invalid'}, {changed: -1}]) {
    const {ctx, nodes} = bulkPreviewContext();
    ctx.api = async () => response;
    await ctx.bmPreview('enable', true);
    assert.equal(ctx.BM.bulkId, '');
    assert.equal(nodes.get('#bmapply').disabled, true);
    assert.equal(nodes.get('#bmpreview').hidden, true);
  }
});
test('export timeout remains active while the response body is pending', async () => {
  const handler = source.slice(source.indexOf('    be.onclick = async () => {'), source.indexOf("\n  $('#pickrec').onclick"));
  const code = handler.slice(0, handler.lastIndexOf('\n  }'));
  const be = {disabled: false, textContent: 'Export'};
  const alerts = [];
  let deadline, timerCleared = false, cancelBody, bodyStarted;
  const started = new Promise(r => {bodyStarted = r;});
  const ctx = {
    be, S: {jobId: 'synthetic-job', token: 'synthetic-token'}, AbortController,
    setTimeout: (fn, ms) => { if (ms === 120000) deadline = fn; return 1; },
    clearTimeout: () => {timerCleared = true;}, alert: s => alerts.push(s),
    fetch: async (_url, options) => ({ok: true, headers: new Headers(),
      blob: () => new Promise((_r, reject) => {
        cancelBody = () => reject(new Error('fixture cleanup'));
        options.signal.addEventListener('abort', () => {const e = new Error('aborted'); e.name = 'AbortError'; reject(e);}, {once:true});
        bodyStarted();
      })})
  };
  vm.createContext(ctx); vm.runInContext(code, ctx);
  const pending = be.onclick();
  await started;
  try {
    assert.equal(timerCleared, false, 'headers alone must not clear the deadline');
    deadline();
    await pending;
    assert.match(alerts[0], /导出超时/);
    assert.equal(be.disabled, false);
  } finally {cancelBody(); await pending;}
});

function applyPickContext(plans, extra = {}) {
  const nodes = new Map();
  const node = () => ({hidden: false, disabled: false, innerHTML: '', textContent: '',
    classList: {toggle() {}}, dataset: {}});
  const ctx = {
    S: {plans, picks: null, jobId: extra.jobId || '', ctx: {}},
    $: k => { if (!nodes.has(k)) nodes.set(k, node()); return nodes.get(k); },
    $$: () => [],
    esc: v => String(v ?? ''),
    pk: (a, b) => `${a}\u0000${b}`,
    SECTION_LABEL: {},
    syncPickUI() { ctx.synced = (ctx.synced || 0) + 1; },
    schedulePlanRefresh() { ctx.refreshed = (ctx.refreshed || 0) + 1; },
    synced: 0,
  };
  vm.createContext(ctx);
  vm.runInContext(['sectionPickVerdict', 'sectionPickAllowed', 'applyPickPreset'].map(sourceFunction).join('\n'), ctx);
  return {ctx, nodes};
}

test('a preset click before the plan exists explains itself', () => {
  const {ctx} = applyPickContext(null);
  ctx.applyPickPreset('rec');
  assert.equal(ctx.synced, 1, 'the UI must be refreshed so the reason is shown');
  assert.match(ctx._pickWhy ?? '', /还没有探测结果/);
});

test('a preset click after a failed plan points at the plan step', () => {
  const {ctx} = applyPickContext(null, {jobId: 'synthetic-job'});
  ctx.applyPickPreset('all');
  assert.match(ctx._pickWhy ?? '', /方案还没算出来/);
});

test('a preset click with a plan still selects rows', () => {
  const plans = [{line_no: 1, host: 'fixture.example', sections: {
    'codex-api-key': {models: ['gpt-6-sol'], recommended: true},
    'claude-api-key': {models: [], recommended: true},
  }}];
  const {ctx} = applyPickContext(plans);
  ctx.applyPickPreset('rec');
  assert.equal(ctx.S.picks.size, 1, 'the section with models must be selected');
  assert.equal(ctx.refreshed, 1, 'selecting must schedule a plan refresh');
});

// Absent metadata means the banner knows nothing, so it must not announce a
// mode. Claiming "no upstream requests by default" was true only while probing
// was disabled by default; once managed probing became the default that same
// sentence told the operator the opposite of what the server would do.
test('policy banner reports an unknown state when metadata is absent', () => {
  const node = {textContent: ''};
  const context = {S: {ctx: {}}, $: () => node};
  vm.createContext(context);
  vm.runInContext(sourceFunction('renderProbePolicy'), context);
  context.renderProbePolicy();
  assert.match(node.textContent, /未知/);
  assert.doesNotMatch(node.textContent, /默认被动|不发送上游探测请求；分析/);
});

test('an explicitly disabled policy still says analysis only', () => {
  const node = {textContent: ''};
  const context = {S: {ctx: {probe_policy: {mode: 'passive', authorized_sites: 0}}}, $: () => node};
  vm.createContext(context);
  vm.runInContext(sourceFunction('renderProbePolicy'), context);
  context.renderProbePolicy();
  assert.match(node.textContent, /仅分析/);
  assert.match(node.textContent, /不发送/);
});

test('policy banner describes explicit authorization without promising safety', () => {
  const node = {textContent: ''};
  const context = {S: {ctx: {probe_policy: {mode: 'restricted', authorized_sites: 2, reason: '按站方许可设置'}}}, $: () => node};
  vm.createContext(context);
  vm.runInContext(sourceFunction('renderProbePolicy'), context);
  context.renderProbePolicy();
  assert.match(node.textContent, /2/);
  assert.match(node.textContent, /预算/);
  assert.doesNotMatch(node.textContent, /保证安全|不会封号/);
});

test('write receipt distinguishes skipped verification from a proven upstream', () => {
  const context = {esc: value => String(value)};
  vm.createContext(context);
  vm.runInContext(sourceFunction('applyReceiptHtml'), context);
  const text = context.applyReceiptHtml({local_written: true, state: 'done', reload_ok: true,
    verify_skipped: '按策略跳过网关生成验证'});
  assert.match(text, /已写盘/);
  assert.match(text, /未实测/);
});
