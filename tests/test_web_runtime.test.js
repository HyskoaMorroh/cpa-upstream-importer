'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');

function sourceFunction(name) {
  const match = source.match(new RegExp(`(?:^|\\n)(?:async\\s+)?function\\s+${name}\\s*\\([^]*?\\n\\}`, 'm'));
  assert.ok(match, `Actual app.js function ${name} must be available`);
  return match[0];
}

test('policy waits explain the blocked phase without implying a failed probe', () => {
  let html = '';
  const box = {insertAdjacentHTML(_position, value) { html += value; }, scrollHeight: 0};
  const context = {$: () => box, tag: host => host};
  vm.createContext(context);
  const escaping = source.slice(source.indexOf('const esc ='), source.indexOf('\nconst fmt ='));
  vm.runInContext(`${escaping}\n${sourceFunction('renderStream')}\nthis.render = renderStream;`, context);
  context.render([
    {kind: 'policy-wait', host: 'fixture.invalid', code: 'inflight', reason: '<script>fixture</script>'},
    {kind: 'policy-wait', host: 'fixture.invalid', code: 'minimum_interval', reason: 'provider interval'},
  ]);
  assert.match(html, /等待同服务商的当前请求完成/);
  assert.match(html, /等待请求间隔/);
  assert.match(html, /未发送新请求/);
  assert.match(html, /&lt;script&gt;fixture&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>|\[policy-wait\]/);
});

test('new and historical failed profile events render without claiming verified identity', () => {
  let html = '';
  const box = {insertAdjacentHTML(_position, value) { html += value; }, scrollHeight: 0};
  const context = {$: () => box, tag: host => host, SECTION_LABEL: {compat: '兼容'}};
  vm.createContext(context);
  const escaping = source.slice(source.indexOf('const esc ='), source.indexOf('\nconst fmt ='));
  const padding = source.match(/^const pad = .*;$/m)[0];
  vm.runInContext(`${escaping}\n${padding}\n${sourceFunction('renderStream')}\nthis.render = renderStream;`, context);
  for (const kind of ['profile-failure-observed', 'profile-gate-passed', 'profile-failure-only', 'profile-gate-only']) {
    html = '';
    context.render([{kind, host: 'fixture.invalid', section: 'compat', profile: '<fixture-profile>', tier: 2, then: '鉴权', status: '401'}]);
    assert.match(html, /<div class="(?:note|warn)">/, kind);
    assert.match(html, /&lt;fixture-profile&gt;/, kind);
    assert.match(html, /鉴权/, kind);
    assert.match(html, /身份尚未验证/, kind);
    assert.doesNotMatch(html, /身份已验证|画像身份已过|换(?:把)?\s*[Kk]ey\s*即可用|<fixture-profile>/, kind);
    assert.doesNotMatch(html, new RegExp(`\\[${kind}\\]`), kind);
  }
});

test('zero-request and interrupted jobs are not labelled probing complete', () => {
  const context = {};
  vm.createContext(context);
  vm.runInContext(`${sourceFunction('probeCompletion')}\nthis.classify = probeCompletion;`, context);
  const passive = context.classify({calls: 0, results: []});
  assert.equal(passive.title, '② 未完成实测');
  assert.match(passive.detail, /不代表通过/);
  const partial = context.classify({calls: 2, results: [{sections: {
    'codex-api-key': {probe_policy_code: 'provider_budget'},
  }}]});
  assert.equal(partial.title, '② 探测部分完成');
  assert.match(partial.detail, /1 个段/);
  assert.equal(context.classify({calls: 2, results: []}).title, '② 探测结束');
});

test('managed policy and broken ledgers have distinct visible explanations', () => {
  const box = {textContent: ''};
  const context = {$: () => box, S: {ctx: {probe_policy: {mode: 'managed'}}}};
  vm.createContext(context);
  vm.runInContext(`${sourceFunction('renderProbePolicy')}\nthis.render = renderProbePolicy;`, context);
  context.render();
  assert.match(box.textContent, /受控主动探测/);
  assert.doesNotMatch(box.textContent, /不发送上游探针/);
  context.S.ctx.probe_policy = {mode: 'passive', ledger_blocked: true, reason: '账本写入失败'};
  context.render();
  assert.match(box.textContent, /账本写入失败/);
});

function apiWith(fetch) {
  const context = {
    fetch,
    AbortController,
    setTimeout,
    clearTimeout,
    API_TIMEOUT_MS: 1000,
    S: {token: 'synthetic-test-token'},
    noteBootId() {},
    _proxyErrText: () => 'Non-JSON response',
  };
  vm.createContext(context);
  vm.runInContext(`${sourceFunction('api')}\nthis.callApi = api;`, context);
  return context.callApi;
}

test('the API deadline includes an unfinished response body', async () => {
  let cancelBody = () => {};
  const api = apiWith(async (_url, options) => ({
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: new Headers(),
    text: () => new Promise((_resolve, reject) => {
      cancelBody = () => reject(new Error('test cleanup'));
      options.signal.addEventListener('abort', () => {
        const error = new Error('aborted');
        error.name = 'AbortError';
        reject(error);
      }, {once: true});
    }),
  }));
  let hardTimer;
  try {
    const outcome = await Promise.race([
      api('/synthetic', {timeoutMs: 20}).then(
        data => ({data}), error => ({error})),
      new Promise(resolve => {
        hardTimer = setTimeout(() => resolve({stillPending: true}), 200);
      }),
    ]);
    assert.equal(outcome.error?.timeout, true,
      'reading the body must reject with the application timeout, not stay pending');
    assert.equal(outcome.error.status, 0);
  } finally {
    clearTimeout(hardTimer);
    cancelBody();
  }
});

test('a broken response body is classified as a connection failure', async () => {
  const api = apiWith(async () => ({
    ok: true,
    status: 200,
    headers: new Headers(),
    text: async () => { throw new TypeError('body stream disconnected'); },
  }));
  await assert.rejects(api('/synthetic'), error => error.network === true && error.status === 0);
});

test('HTTP 200 HTML cannot become a successful application result', async () => {
  const api = apiWith(async () => new Response('<html>proxy fallback</html>', {
    status: 200,
    headers: {'Content-Type': 'text/html'},
  }));
  await assert.rejects(api('/synthetic'), error => error.invalidResponse === true && error.status === 200);
});

test('HTTP 200 with an empty body is not a successful application result', async () => {
  const api = apiWith(async () => new Response('', {status: 200}));
  // 空体必须抛，且必须同时带 network —— 反代截断是瞬时故障，轮询循环要能
  // 按可重试处理。判成硬错会让一次抖动终止整个定档/写回任务。
  // 另外错误文案不得以标点开头：原实现落进 JSON.parse('') 后拼出的消息
  // 字面上是「（HTTP 200，响应不是有效 JSON）」，前面什么都没有，
  // 而每个调用点都原样 esc() 渲染给用户看。
  await assert.rejects(api('/synthetic'), error => error.invalidResponse === true
    && error.status === 200 && error.network === true && error.emptyBody === true
    && /^[^（(]/.test(error.message) && /HTTP 200/.test(error.message));
});

test('a normal JSON response preserves the returned data', async () => {
  const api = apiWith(async () => new Response(JSON.stringify({state: 'running', task_id: 'fixture'}), {
    status: 202,
    headers: {'Content-Type': 'application/json'},
  }));
  assert.deepEqual(JSON.parse(JSON.stringify(await api('/synthetic'))),
    {state: 'running', task_id: 'fixture'});
});

test('HTTP errors preserve their status and Retry-After', async () => {
  const api = apiWith(async () => new Response(JSON.stringify({error: 'busy', retryable: true}), {
    status: 429,
    headers: {'Content-Type': 'application/json', 'Retry-After': '5'},
  }));
  await assert.rejects(api('/synthetic'), error => error.status === 429 && error.retryAfter === 5);
});

test('an explicit HTTP 204 response does not require a JSON body', async () => {
  const api = apiWith(async () => new Response(null, {status: 204}));
  assert.deepEqual(JSON.parse(JSON.stringify(await api('/synthetic'))), {});
});

function applyContext(first, states = []) {
  const nodes = new Map();
  const calls = [];
  const context = {
    BM: {bulkId: 'fixture-bulk', sel: new Set(['fixture-row'])},
    TN: {id: 'fixture-tuning'},
    $: selector => {
      if (!nodes.has(selector)) nodes.set(selector, {disabled: false, hidden: false, textContent: '', innerHTML: ''});
      return nodes.get(selector);
    },
    esc: value => String(value ?? ''),
    cpaRestartHint: () => 'manual reload',
    bmPush: () => ({}),
    bmLoad: async () => { context.refreshes += 1; },
    tnRun: async () => { context.refreshes += 1; },
    refreshes: 0,
    setTimeout: callback => { queueMicrotask(callback); return 0; },
    clearTimeout() {},
    api: async route => {
      calls.push(route);
      if (!route.startsWith('/api/apply-status/')) return first;
      if (!states.length) throw new Error('synthetic polling interruption');
      return states.shift();
    },
  };
  vm.createContext(context);
  for (const name of ['classifyPollError', 'pollApply', 'tnPoll', 'awaitApplyReceipt', 'applyReceiptHtml']) {
    if (source.includes(`function ${name}(`)) vm.runInContext(sourceFunction(name), context);
  }
  return {context, nodes, calls};
}

function applyHandler(id, context) {
  const start = source.indexOf(`$('#${id}').onclick = async () => {`);
  assert.ok(start >= 0, `Actual ${id} handler must be available`);
  const end = source.indexOf('\n};', start);
  assert.ok(end > start);
  vm.runInContext(source.slice(start, end + 3), context);
  return context.$(`#${id}`).onclick;
}

test('bulk writeback waits for its task and checks the real write receipt', async () => {
  const fixture = applyContext({task_id: 'fixture-task', state: 'running', local_written: false}, [{
    task_id: 'fixture-task', state: 'done', local_written: true,
    notes: ['changed'], backup: 'fixture.backup', reload_ok: true, push_ok: true,
  }]);
  await applyHandler('bmapply', fixture.context)();
  assert.ok(fixture.calls.some(route => route.startsWith('/api/apply-status/')));
  assert.equal(fixture.context.BM.bulkId, '');
  assert.equal(fixture.context.refreshes, 1);
});

test('a rejected bulk write does not clear the preview or selections', async () => {
  const fixture = applyContext({task_id: 'fixture-task', state: 'running', local_written: false}, [{
    state: 'error', local_written: false, error: 'synthetic write failure',
  }]);
  await applyHandler('bmapply', fixture.context)();
  assert.equal(fixture.context.BM.bulkId, 'fixture-bulk');
  assert.equal(fixture.context.BM.sel.size, 1);
  assert.equal(fixture.context.refreshes, 0);
  assert.match(fixture.context.$('#bmapplymsg').innerHTML, /synthetic write failure/);
});

test('a failed reload remains visible after a bulk local write', async () => {
  const fixture = applyContext({task_id: 'fixture-task', state: 'running', local_written: false}, [{
    state: 'error', local_written: true, error: 'synthetic reload failure',
    reload_ok: false, push_ok: false, notes: ['changed'],
  }]);
  await applyHandler('bmapply', fixture.context)();
  assert.match(fixture.context.$('#bmapplymsg').innerHTML, /synthetic reload failure/);
  assert.doesNotMatch(fixture.context.$('#bmapplymsg').innerHTML, /var\(--ok\)/);
});

test('tuning cannot announce a write without local_written evidence', async () => {
  const fixture = applyContext({state: 'done'});
  await applyHandler('tnapply', fixture.context)();
  assert.equal(fixture.context.TN.id, 'fixture-tuning');
  assert.equal(fixture.context.refreshes, 0);
  assert.doesNotMatch(fixture.context.$('#tnmsg').innerHTML, /已写回|已写盘/);
});

test('lost polling does not invent a local write for a queued task', async () => {
  const fixture = applyContext(null);
  const result = await fixture.context.pollApply('fixture-task', {local_written: false});
  assert.equal(result, null);
  assert.match(fixture.context.$('#applymsg').innerHTML, /结果未知/);
  assert.doesNotMatch(fixture.context.$('#applymsg').innerHTML, /配置已写盘/);
});

test('lost polling retains a previously confirmed local write', async () => {
  const fixture = applyContext(null, [{state: 'running', local_written: true, elapsed: 1}]);
  const result = await fixture.context.pollApply('fixture-task', {local_written: false});
  assert.equal(result, null);
  assert.match(fixture.context.$('#applymsg').innerHTML, /写盘/);
  assert.doesNotMatch(fixture.context.$('#applymsg').innerHTML, /重载成功|验证通过/);
});

test('an invalid task state is not accepted as successful completion', async () => {
  const fixture = applyContext(null, [{unexpected: true}]);
  const result = await fixture.context.pollApply('fixture-task', {local_written: false});
  assert.equal(result, null);
});

function bulkDraftContext(priority) {
  const group = {section: 'codex', host: 'fixture.invalid', entries: [
    {index: 0, priority: 100, enabled: true, fingerprint: 'fixture'},
  ]};
  const draft = {section: group.section, host: group.host, enabled: false};
  if (priority !== undefined) draft.priority = priority;
  const key = (section, host) => `${section}\u0000${host}`;
  const context = {BMD: new Map([[key(group.section, group.host), draft]]),
    BM: {groups: [group]}, bmSelected: () => [group], bmdKey: key};
  vm.createContext(context);
  vm.runInContext(sourceFunction('bmDraftOps') + '\n' + sourceFunction('bmOps'), context);
  return context;
}

test('an enable-only draft does not suppress a bulk priority change', () => {
  const operations = bulkDraftContext().bmOps('setpri', 900);
  assert.equal(operations.filter(item => item.action === 'priority')[0]?.value, 900);
  assert.equal(operations.filter(item => item.action === 'disable').length, 1);
});

test('an explicit draft priority still wins over a bulk default', () => {
  const operations = bulkDraftContext(777).bmOps('setpri', 900);
  const priorities = operations.filter(item => item.action === 'priority');
  assert.equal(priorities.length, 1);
  assert.equal(priorities[0].value, 777);
});

function priorityContext(manual) {
  const input = {value: manual === undefined ? '' : String(manual)};
  const row = {querySelector: selector => selector === '.pi' ? input : null};
  const context = {
    S: {forced: {}, overrides: manual === undefined ? {} : {'1': {codex: {priority: manual}}}},
    cssq: String,
    document: {querySelector: selector => selector.includes('tr.wrow') ? null : row},
  };
  vm.createContext(context);
  vm.runInContext(sourceFunction('fillPlanIntoRows'), context);
  return {input, fill: priority => context.fillPlanIntoRows({plans: [
    {line_no: 1, host: 'fixture.invalid', sections: {codex: {priority, models: []}}},
  ]})};
}

test('automatic priority follows the latest completed plan', () => {
  const fixture = priorityContext();
  fixture.fill(100);
  fixture.fill(900);
  assert.equal(String(fixture.input.value), '900');
});

test('an invalid automatic priority cannot leave the old number visible', () => {
  const fixture = priorityContext();
  fixture.fill(100);
  fixture.fill(0);
  assert.equal(fixture.input.value, '');
});

test('planning does not overwrite a valid manual priority', () => {
  const fixture = priorityContext(777);
  fixture.fill(900);
  assert.equal(String(fixture.input.value), '777');
});

function selectionContext() {
  const listeners = {};
  const nodes = new Map();
  const selection = {textContent: ''};
  const row = {classList: {toggle() {}}, querySelector: () => selection};
  const checkbox = {checked: true, dataset: {rid: '1', sec: 'codex'},
    closest: selector => selector === '.sel' ? checkbox : selector === 'tr' ? row : null};
  const box = {dataset: {}, addEventListener: (event, handler) => { listeners[event] = handler; }};
  const context = {
    S: {picks: null, overrides: {}, forced: {}},
    $: selector => {
      if (selector === '#results') return box;
      if (!nodes.has(selector)) nodes.set(selector, {});
      return nodes.get(selector);
    },
    $$: selector => selector === '#results .sel' ? [checkbox] : [],
    pk: (rid, section) => `${rid}\u0000${section}`,
    _pickWhy: '', esc: String, schedulePlanRefresh() {},
  };
  vm.createContext(context);
  vm.runInContext(['sectionPickVerdict', 'sectionPickAllowed', 'planEvidenceSummary', 'syncPickUI', 'bindResultEvents', 'applyPickPreset'].map(sourceFunction).join('\n'), context);
  return {context, listeners, checkbox, selection};
}

test('selection can be changed before the first plan completes', () => {
  const fixture = selectionContext();
  fixture.context.bindResultEvents();
  assert.doesNotThrow(() => fixture.listeners.change({target: fixture.checkbox}));
  assert.ok(fixture.context.S.picks.has('1\u0000codex'));
  assert.equal(fixture.selection.textContent, '已选择，等待方案验证');
  assert.equal(fixture.context.$('#btnplan').disabled, true);
  fixture.checkbox.checked = false;
  fixture.listeners.change({target: fixture.checkbox});
  assert.equal(fixture.selection.textContent, '未勾选，不写入');
});

test('synchronizing pre-plan controls preserves deferred default selection', () => {
  const fixture = selectionContext();
  assert.doesNotThrow(() => fixture.context.syncPickUI());
  assert.equal(fixture.context.S.picks, null);
});

test('all and manual selection cannot write any of the 163 stopped retained sections', () => {
  const fixture = selectionContext();
  const codes = [...Array(48).fill('policy_wait_timeout'), ...Array(74).fill('provider_stopped'), ...Array(41).fill('provider_job_stopped')];
  fixture.context.S.plans = codes.map((probe_policy_code, i) => ({line_no: i + 1, sections: {
    codex: {model_source: 'prior', models: ['retained-model'], writable: false, probe_policy_code},
  }}));
  fixture.context.applyPickPreset('all');
  assert.equal(fixture.context.S.picks.size, 0);
  assert.equal(fixture.context.$('#btnplan').disabled, true);
  assert.equal(fixture.checkbox.disabled, true);
  fixture.context.bindResultEvents();
  fixture.checkbox.checked = true;
  fixture.listeners.change({target: fixture.checkbox});
  assert.equal(fixture.context.S.picks.size, 0);
  assert.equal(fixture.selection.textContent, '不可写入');
  fixture.context.S.picks = new Set(codes.map((_, i) => `${i + 1}\u0000codex`));
  fixture.context.syncPickUI();
  assert.equal(fixture.context.S.picks.size, 0);
  assert.equal(fixture.context.$('#btnplan').disabled, true);
});

test('selection honors both serialized write eligibility and latest stop verdict', () => {
  const fixture = selectionContext();
  fixture.context.S.plans = [{line_no: 1, sections: {
    codex: {writable: false},
    claude: {writable: true},
    compat: {writable: true, model_source: 'catalog'},
  }}];
  fixture.context.S.results = [{row: {line_no: 1}, sections: {
    claude: {probe_policy_code: 'provider_stopped'}, compat: {usable: true},
  }}];
  fixture.context.S.picks = new Set(['1\u0000codex', '1\u0000claude', '1\u0000compat']);
  fixture.context.syncPickUI();
  assert.deepEqual([...fixture.context.S.picks], ['1\u0000compat']);
  assert.equal(fixture.context.$('#btnplan').disabled, false);
  assert.match(fixture.context.$('#pickstat').innerHTML, /本轮实测可用 0 段/);
});

test('eligible plans remain selectable manually and through presets after a blocked refresh', () => {
  const fixture = selectionContext();
  const section = {writable: false, models: ['verified-model'], model_source: 'probed', recommended: true};
  fixture.context.S.plans = [{line_no: 1, sections: {codex: section}}];
  fixture.context.S.picks = new Set();
  fixture.context.syncPickUI();
  assert.equal(fixture.checkbox.disabled, true);
  section.writable = true;
  fixture.context.syncPickUI();
  assert.equal(fixture.checkbox.disabled, false);
  fixture.context.bindResultEvents();
  fixture.checkbox.checked = true;
  fixture.listeners.change({target: fixture.checkbox});
  assert.equal(fixture.context.S.picks.size, 1);
  assert.equal(fixture.context.$('#btnplan').disabled, false);
  assert.equal(fixture.selection.textContent, '已勾选写入');
  for (const mode of ['all', 'rec']) {
    fixture.context.applyPickPreset(mode);
    assert.equal(fixture.context.S.picks.size, 1);
    assert.equal(fixture.context.$('#btnplan').disabled, false);
  }
  fixture.context.applyPickPreset('none');
  assert.equal(fixture.context.S.picks.size, 0);
  assert.equal(fixture.context.$('#btnplan').disabled, true);
});

function planningContext() {
  const fixture = applyContext(null);
  const context = fixture.context;
  Object.assign(context, {
    S: {jobId: 'fixture-job', planId: 'previous', picks: new Set(['1\u0000codex']),
      overrides: {}, forced: {}, token: 'synthetic-test-token'},
    _planInFlight: null, _planRerun: false, _planTimer: null,
    syncPickUI() {}, fillPlanIntoRows() {}, applyPickPreset() {},
    planWarnings: () => '', fmt: String, SECTION_LABEL: {}, step() {},
    $$: () => [], planInputKey: () => 'old-fixture-key',
  });
  for (const selector of ['#p4', '#o_mgmt', '#o_client', '#o_probation']) {
    const node = context.$(selector);
    node.value = '';
    node.checked = true;
    node.scrollIntoView = () => {};
  }
  for (const name of ['planInputKey', 'saveJobRecovery', 'invalidatePlanPreview', 'refreshPlan', '_refreshPlanOnce', 'planDiffView', 'planBlockedItems']) {
    if (source.includes(`function ${name}(`)) vm.runInContext(sourceFunction(name), context);
  }
  return fixture;
}

test('blocked preview lists section reasons even when skipped is empty', () => {
  const context = vm.createContext({SECTION_LABEL: {codex: 'Codex'}});
  vm.runInContext(sourceFunction('planBlockedItems'), context);
  const items = context.planBlockedItems({plans: [{line_no: 7, skipped: {}, sections: {
    codex: {writable: false, probe_policy_reason: 'provider stopped',
      write_blocked: 'model evidence missing', skip_reason: 'provider stopped'},
    allowed: {writable: true},
  }}]});
  assert.equal(items.length, 1);
  assert.match(items[0], /第 7 行 · Codex/);
  assert.match(items[0], /provider stopped；model evidence missing/);
  assert.equal(items[0].match(/provider stopped/g).length, 1);
});

test('blocked preview preserves skipped entries and explains missing reasons', () => {
  const context = vm.createContext({SECTION_LABEL: {}});
  vm.runInContext(sourceFunction('planBlockedItems'), context);
  const items = context.planBlockedItems({plans: [{line_no: 1,
    skipped: {legacy: 'already exists'}, sections: {unknown: {writable: false}}}]});
  assert.equal(items.length, 2);
  assert.ok(items.some(x => x.includes('already exists')));
  assert.ok(items.some(x => x.includes('本项未满足写入条件')));
});

test('all single-flight callers receive the latest queued plan', async () => {
  const {context} = planningContext();
  let releaseOld;
  let calls = 0;
  context._refreshPlanOnce = async () => {
    calls += 1;
    if (calls === 1) return new Promise(resolve => { releaseOld = resolve; });
    return {plan_id: 'latest-plan'};
  };
  const first = context.refreshPlan(true);
  const preview = context.refreshPlan(false);
  releaseOld({plan_id: 'old-plan'});
  assert.equal((await preview).plan_id, 'latest-plan');
  assert.equal((await first).plan_id, 'latest-plan');
});

test('a response for old selections does not replace the current plan', async () => {
  const {context} = planningContext();
  let release;
  context.api = async () => new Promise(resolve => { release = resolve; });
  const pending = context._refreshPlanOnce(true);
  context.S.picks.add('2\u0000codex');
  release({plan_id: 'stale-plan', plans: [], diffs: []});
  assert.equal(await pending, null);
  assert.equal(context.S.planId, 'previous');
  assert.equal(context._planRerun, true);
});

test('interrupted planning resumes its original task without another submission', async () => {
  const {context} = planningContext();
  context.setTimeout = resolve => { queueMicrotask(resolve); return 0; };
  const calls = [];
  const responses = [{plan_task_id: 'fixture-task', state: 'running'}, {state: 'unexpected'}];
  context.api = async route => { calls.push(route); return responses.shift(); };
  assert.equal(await context._refreshPlanOnce(true), null);
  assert.equal(context.S.planTask.id, 'fixture-task');
  responses.push({state: 'done', result: {plan_id: 'resumed-plan', plans: []}});
  assert.equal((await context._refreshPlanOnce(true)).plan_id, 'resumed-plan');
  assert.equal(calls.filter(route => route === '/api/plan').length, 1);
  assert.equal(context.S.planTask.id, 'fixture-task');
  assert.equal(context.S.planTask.inputKey, context.planInputKey());
});

test('completed planning task is invalidated when selected inputs change', async () => {
  const {context} = planningContext();
  context.setTimeout = resolve => { queueMicrotask(resolve); return 0; };
  context.S.planTask = {id: 'completed-task', inputKey: context.planInputKey()};
  context.S.picks.add('2\u0000codex');
  const calls = [];
  context.api = async route => {
    calls.push(route);
    return {plan_id: 'new-selection-plan', plans: []};
  };
  assert.equal((await context._refreshPlanOnce(true)).plan_id, 'new-selection-plan');
  assert.deepEqual(calls, ['/api/plan']);
  assert.equal(context.S.planTask, null);
});

test('changed selections leave obsolete running planning promptly', async () => {
  const {context} = planningContext();
  context.setTimeout = resolve => {
    context.S.picks.add('2\u0000codex');
    queueMicrotask(resolve);
    return 0;
  };
  const calls = [];
  context.api = async route => {
    calls.push(route);
    return {plan_task_id: 'obsolete-task', state: 'running'};
  };
  assert.equal(await context._refreshPlanOnce(true), null);
  assert.equal(context._planRerun, true);
  assert.equal(calls.length, 1);
  assert.equal(context.S.planId, 'previous');
});

test('a plan without a usable ID cannot replace the last validated plan', async () => {
  for (const plan_id of [undefined, '', '   ', 42]) {
    const {context} = planningContext();
    context.api = async () => ({plan_id, plans: []});
    assert.equal(await context._refreshPlanOnce(true), null);
    assert.equal(context.S.planId, 'previous');
    assert.ok(context.$('#planmeta').innerHTML.length > 0);
  }
});

test('a finished planning task with no result is not labelled as a timeout', async () => {
  const {context} = planningContext();
  context.setTimeout = resolve => { queueMicrotask(resolve); return 0; };
  const responses = [{plan_task_id: 'fixture-task', state: 'running'}, {state: 'done'}];
  context.api = async () => responses.shift();
  assert.equal(await context._refreshPlanOnce(true), null);
  assert.equal(context.S.planTask, null);
  assert.doesNotMatch(context.$('#planmeta').innerHTML, /360|超时/);
});

async function showFixturePreview(context) {
  const result = {plan_id: 'visible-plan', plans: [], valid: true,
    validate_msg: 'fixture valid', lines_before: 1, lines_after: 2,
    diffs: [{section: 'codex', host: 'fixture.invalid', insert_at: 1, lines: ['synthetic change']}]};
  context.refreshPlan = async () => {
    context.S.planId = result.plan_id;
    context.S.planInputKey = context.planInputKey();
    return result;
  };
  const children = new Map();
  context.$('#diffs').querySelector = selector => {
    if (!children.has(selector)) children.set(selector, {textContent: ''});
    return children.get(selector);
  };
  await applyHandler('btnplan', context)();
  assert.match(context.$('#diffs').querySelector('pre').textContent, /synthetic change/);
  assert.doesNotMatch(context.$('#diffs').innerHTML, /synthetic change/);
}

test('all-blocked preview shows escaped reasons, return guidance and disabled write', async () => {
  const {context} = planningContext();
  vm.runInContext(source.slice(source.indexOf('const esc ='), source.indexOf('\nconst fmt =')), context);
  const result = {plan_id: 'blocked-plan', valid: true, validate_msg: 'no changes',
    lines_before: 1, lines_after: 1, diffs: [], unified_diff: '',
    plans: [{line_no: 1, skipped: {}, sections: {codex: {
      writable: false, write_blocked: '<script>missing evidence</script>',
      probe_policy_reason: 'provider stopped',
    }}}]};
  context.refreshPlan = async () => {
    context.S.planId = result.plan_id;
    context.S.planInputKey = context.planInputKey();
    return result;
  };
  await applyHandler('btnplan', context)();
  assert.match(context.$('#planmeta').innerHTML, /provider stopped/);
  assert.doesNotMatch(context.$('#planmeta').innerHTML, /<script>/);
  assert.match(context.$('#diffs').innerHTML, /1 项未写入/);
  assert.match(context.$('#diffs').innerHTML, /返回探测结果与调整/);
  assert.equal(context.$('#btnapply').disabled, true);
  assert.equal(context.$('#p4').hidden, false);
});

test('confirm submits the displayed plan rather than a later background plan', async () => {
  const {context} = planningContext();
  await showFixturePreview(context);
  context.S.planId = 'invisible-plan';
  let sent;
  context.api = async (_route, options) => {
    sent = options.body;
    return {state: 'error', local_written: false, error: 'synthetic rejection'};
  };
  await applyHandler('btnapply', context)();
  assert.equal(sent?.plan_id, 'visible-plan');
});

test('changed input invalidates an already displayed preview', async () => {
  const {context} = planningContext();
  await showFixturePreview(context);
  context.S.picks.add('2\u0000codex');
  let requests = 0;
  context.api = async () => {
    requests += 1;
    return {state: 'error', local_written: false};
  };
  await applyHandler('btnapply', context)();
  assert.equal(requests, 0);
  assert.equal(context.$('#btnapply').disabled, true);
});

for (const reloadOk of [true, false]) {
  test(`confirmed disk write clears recovery even when reload success is ${reloadOk}`, async () => {
    const {context} = planningContext();
    const storage = new Map();
    context.sessionStorage = {
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
    };
    await showFixturePreview(context);
    context.S.planTask = {id: 'fixture-task', inputKey: context.planInputKey()};
    context.saveJobRecovery();
    assert.ok(storage.has('importer_job_recovery'));
    context.api = async () => ({local_written: true, state: reloadOk ? 'done' : 'error',
      reload_ok: reloadOk, reload_msg: 'synthetic reload result', verified: []});
    await applyHandler('btnapply', context)();
    assert.equal(context.S.jobId, null);
    assert.equal(context.S.planTask, null);
    assert.equal(context.S.previewPlanId, '');
    assert.equal(context.$('#pdone').hidden, false);
    assert.equal(storage.has('importer_job_recovery'), false);
    context.saveJobRecovery(); // The pagehide handler must not resurrect it.
    assert.equal(storage.has('importer_job_recovery'), false);
  });
}

test('rejected disk write retains the current job recovery', async () => {
  const {context} = planningContext();
  const storage = new Map();
  context.sessionStorage = {
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  await showFixturePreview(context);
  context.saveJobRecovery();
  const recovery = storage.get('importer_job_recovery');
  context.api = async () => ({local_written: false, state: 'error', error: 'synthetic rejection'});
  await applyHandler('btnapply', context)();
  assert.equal(context.S.jobId, 'fixture-job');
  assert.equal(storage.get('importer_job_recovery'), recovery);
});

function probeContext(api, pickedSections = () => undefined) {
  const nodes = new Map();
  const context = {
    S: {jobId: null, parsedValid: 1, probeSubmitting: false},
    $: selector => {
      if (!nodes.has(selector)) nodes.set(selector, {
        disabled: false, hidden: false, checked: false, value: '', innerHTML: '', textContent: '',
      });
      return nodes.get(selector);
    },
    api,
    esc: value => String(value),
    // applyHandler 只把 onclick 的函数体搬进一个裸 context —— app.js 其余
    // 顶层定义**不在**里面。所以处理器新引用一个顶层助手（2026-10-09 的
    // pickedSections）时，这里不同步补桩，三个用例会一起炸成
    // `ReferenceError: pickedSections is not defined`，而浏览器里完全正常。
    // 真实缺陷与桩缺失长得一模一样，所以这个桩必须显式存在、并被下面
    // 「手动选段要带进请求」那条用例实际断言到。
    pickedSections,
    alert() {}, confirm: () => true, step() {},
    saveJobRecovery: () => { context.recoverySaves += 1; }, recoverySaves: 0,
    poll: () => { context.pollCount += 1; }, pollCount: 0,
  };
  vm.createContext(context);
  return context;
}

test('a slow probe start cannot submit two jobs', async () => {
  let release;
  let requests = 0;
  const pending = new Promise(resolve => { release = resolve; });
  const context = probeContext(() => { requests += 1; return pending; });
  const run = applyHandler('btnprobe', context);
  const first = run();
  const second = run();
  const wasDisabled = context.$('#btnprobe').disabled;
  release({job_id: 'one-fixture-job'});
  await Promise.all([first, second]);
  assert.equal(requests, 1);
  assert.equal(wasDisabled, true);
  assert.equal(context.pollCount, 1);
  assert.equal(context.S.jobId, 'one-fixture-job');
  assert.equal(context.recoverySaves, 1);
  assert.equal(context.S.probeSubmitting, false);
});

test('probe submission failure unlocks the button without starting polling', async () => {
  const context = probeContext(async () => { throw new Error('synthetic start failure'); });
  await applyHandler('btnprobe', context)();
  assert.equal(context.$('#btnprobe').disabled, false);
  assert.equal(context.S.probeSubmitting, false);
  assert.equal(context.pollCount, 0);
  assert.match(context.$('#parsemsg').innerHTML, /synthetic start failure/);
});

test('probe response without a job id cannot enter a running screen', async () => {
  const context = probeContext(async () => ({}));
  await applyHandler('btnprobe', context)();
  assert.equal(context.pollCount, 0);
  assert.equal(context.S.jobId, null);
  assert.ok(context.$('#parsemsg').innerHTML.length > 0);
  assert.equal(context.$('#btnprobe').disabled, false);
});

// 段前置闸的前端一侧（2026-10-09 的新开关）。两种状态的语义不同，必须分别钉住：
//   · 手动勾选 -> opts.sections 带着勾选的段，后端零请求直接采用；
//   · 全不勾   -> opts.sections 必须是 undefined 而**不是** []，
//                 `[]` 到后端是「一段都不探」的字面意思，会变成一个请求都不发。
test('a manual section pick reaches the request and an empty pick stays undefined', async () => {
  let sent;
  const picked = probeContext(async (route, options) => {
    sent = options.body; return {job_id: 'picked-fixture-job'};
  }, () => ['claude-api-key', 'openai-compatibility']);
  await applyHandler('btnprobe', picked)();
  assert.deepEqual(sent.opts.sections, ['claude-api-key', 'openai-compatibility']);

  let sentAuto;
  const auto = probeContext(async (route, options) => {
    sentAuto = options.body; return {job_id: 'auto-fixture-job'};
  });
  await applyHandler('btnprobe', auto)();
  assert.equal(sentAuto.opts.sections, undefined);
  assert.ok(!('sections' in JSON.parse(JSON.stringify(sentAuto.opts))),
    'an unpicked section list must not serialise as [] or null');
});

function diagnosticContext(responses) {
  const calls = [];
  const progress = [];
  const box = {innerHTML: '', set textContent(value) { progress.push(value); }};
  const context = {
    S: {diagTaskId: '', diagInputKey: ''},
    api: async (route, options) => {
      calls.push({route, options});
      const next = responses.shift();
      if (next instanceof Error) throw next;
      assert.ok(next, 'fixture ran out of diagnostic responses');
      return next;
    },
    setTimeout: resolve => { queueMicrotask(resolve); return 0; },
    clearTimeout() {},
    $: () => box,
  };
  vm.createContext(context);
  vm.runInContext(sourceFunction('classifyPollError'), context);
  vm.runInContext(sourceFunction('fetchDiagnostic'), context);
  return {context, calls, progress};
}

const diagnosticBody = {url: 'https://fixture.invalid', key: 'fixture-key', full: false};
const diagnosticResult = {host: 'fixture.invalid', sections: {}, full: false};

test('diagnostic initialization progress retains elapsed and old-server fallback', async () => {
  const fixture = diagnosticContext([
    {job_id: 'fixture-diag', kind: 'diag'},
    {kind: 'diag', state: 'running', elapsed: 20, phase_message: '正在初始化探测器与源身份快照…'},
    {kind: 'diag', state: 'running', elapsed: 22},
    {kind: 'diag', state: 'done', result: diagnosticResult},
  ]);
  await fixture.context.fetchDiagnostic(diagnosticBody);
  assert.ok(fixture.progress.some(text => text.includes('正在初始化探测器与源身份快照… 20 秒')));
  assert.ok(fixture.progress.some(text => text.includes('后台诊断中… 22 秒')));
  assert.equal(fixture.calls.filter(c => c.options?.method === 'POST').length, 1);
  assert.equal(fixture.calls.filter(c => c.route.startsWith('/api/job/')).length, 3);
});

test('diagnostics poll their read-only job instead of waiting on the start POST', async () => {
  const fixture = diagnosticContext([
    {job_id: 'fixture-diag', kind: 'diag', state: 'pending', poll_ms: 1},
    {kind: 'diag', state: 'running', elapsed: 40},
    {kind: 'diag', state: 'done', result: diagnosticResult},
  ]);
  const result = await fixture.context.fetchDiagnostic(diagnosticBody);
  assert.equal(result, diagnosticResult);
  assert.equal(fixture.calls.filter(c => c.options?.method === 'POST').length, 1);
  assert.equal(fixture.calls[0].options.body.async, true);
  assert.equal(fixture.calls[0].options.body.full, false);
  assert.equal(fixture.calls.filter(c => c.route.startsWith('/api/job/')).length, 2);
  assert.equal(fixture.context.S.diagTaskId, '');
});

test('diagnostics accept an older synchronous server response', async () => {
  const fixture = diagnosticContext([diagnosticResult]);
  assert.equal(await fixture.context.fetchDiagnostic(diagnosticBody), diagnosticResult);
  assert.equal(fixture.calls.length, 1);
});

test('a transient diagnostic poll failure never repeats the start POST', async () => {
  const disconnected = Object.assign(new Error('synthetic disconnect'), {status: 0, network: true});
  const fixture = diagnosticContext([
    {job_id: 'fixture-diag', kind: 'diag'}, disconnected,
    {kind: 'diag', state: 'done', result: diagnosticResult},
  ]);
  await fixture.context.fetchDiagnostic(diagnosticBody);
  assert.equal(fixture.calls.filter(c => c.options?.method === 'POST').length, 1);
  assert.equal(fixture.calls.filter(c => c.route.startsWith('/api/job/')).length, 2);
});

test('a diagnostic interrupted by authentication can resume the same job', async () => {
  const unauthorized = Object.assign(new Error('synthetic login expired'), {status: 401});
  const fixture = diagnosticContext([{job_id: 'fixture-diag', kind: 'diag'}, unauthorized]);
  await assert.rejects(fixture.context.fetchDiagnostic(diagnosticBody));
  assert.equal(fixture.context.S.diagTaskId, 'fixture-diag');
  fixture.context.api = async (route, options) => {
    fixture.calls.push({route, options});
    return {kind: 'diag', state: 'done', result: diagnosticResult};
  };
  await fixture.context.fetchDiagnostic(diagnosticBody);
  assert.equal(fixture.calls.filter(c => c.options?.method === 'POST').length, 1);
});

test('a done diagnostic without a result is an error, not an empty successful screen', async () => {
  const fixture = diagnosticContext([{job_id: 'fixture-diag', kind: 'diag'}, {state: 'done'}]);
  await assert.rejects(fixture.context.fetchDiagnostic(diagnosticBody));
  assert.equal(fixture.context.S.diagTaskId, '');
});

for (const [label, plan, verdict, expected] of [
  ['blocked recommendation', {writable:true,recommended:true,write_blocked:'no evidence',score:90}, {usable:true}, '不可写入'],
  ['unwritable recommendation', {writable:false,recommended:true,score:90}, {usable:true}, '不可写入'],
  ['failed recommendation', {writable:true,recommended:true,score:90}, {usable:false}, '本轮未验证可用 · 需人工确认'],
  ['zero score recommendation', {writable:true,recommended:true,score:0}, {usable:true}, '本轮未验证可用 · 需人工确认'],
  ['missing evidence', {writable:true,recommended:true,score:90}, {}, '本轮未验证可用 · 需人工确认'],
  ['verified recommendation', {writable:true,recommended:true,score:90}, {usable:true}, '建议写入'],
  ['manual choice', {writable:true,recommended:false,score:90}, {usable:true}, '需人工确认'],
  ['prior with stale usable evidence', {model_source:'prior',writable:true,recommended:true,score:90}, {usable:true}, '仅保留原配置 · 本轮未验证'],
  ['stopped with stale usable evidence', {model_source:'probed',writable:true,recommended:true,score:90}, {usable:true,probe_policy_code:'provider_stopped'}, '本轮探测受限或停止 · 不可写入'],
]) {
  test('write status respects '+label, () => {
    const context=vm.createContext({});
    vm.runInContext(sourceFunction('planWriteStatus'),context);
    assert.equal(context.planWriteStatus(plan,verdict).text,expected);
  });
}

test('stopped write status retains prior label and exposes escaped policy details', () => {
  const context = vm.createContext({});
  const escaping = source.slice(source.indexOf('const esc ='), source.indexOf('\nconst fmt ='));
  vm.runInContext(`${escaping}\n${sourceFunction('planWriteStatus')}\nthis.escape = esc;`, context);
  for (const model_source of ['prior', 'probed']) {
    const status = context.planWriteStatus({model_source}, {
      probe_policy_code: 'provider_job_stopped',
      probe_policy_reason: '旧限制记录复核未确认成功 <img src=x onerror=bad()>',
    });
    assert.match(status.text, /旧限制记录复核未确认成功/);
    if (model_source === 'prior') assert.match(status.text, /仅保留原配置 · 本轮未验证/);
    assert.match(context.escape(status.text), /&lt;img/);
    assert.doesNotMatch(context.escape(status.text), /<img/);
  }
  assert.ok(source.includes('${esc(status.text)}</span>'));
  assert.ok(source.includes('${esc(tag)}</span>${sc}'));
});

test('prior configuration names are not presented as past successful probes', () => {
  assert.equal(source.includes('先前一轮的实测沉淀'),false);
  assert.ok(source.includes('不代表曾经实测通过'));
});

test('full redetect counts retained models separately from current successful probes', () => {
  const context = vm.createContext({});
  vm.runInContext(sourceFunction('planEvidenceSummary'), context);
  const codes = [...Array(48).fill('policy_wait_timeout'), ...Array(74).fill('provider_stopped'), ...Array(41).fill('provider_job_stopped')];
  const plans = codes.map((code, line_no) => ({line_no, sections: {codex: {
    model_source: 'prior', writable: true, recommended: false, probe_policy_code: code,
  }}}));
  const results = codes.map((code, line_no) => ({row: {line_no}, sections: {codex: {
    usable: true, probe_policy_code: code,
  }}}));
  const summary = context.planEvidenceSummary(plans, results);
  assert.match(summary, /本轮实测可用 0 段/);
  assert.match(summary, /仅保留原配置 163 段/);
  assert.match(summary, /受限或停止 163 段/);
  assert.match(summary, /实际改动以差异预览为准/);
});

test('catalog and stopped results never increase current callable evidence count', () => {
  const context = vm.createContext({});
  vm.runInContext(sourceFunction('planEvidenceSummary'), context);
  const summary = context.planEvidenceSummary([{line_no: 1, sections: {
    a: {model_source: 'probed'}, b: {model_source: 'catalog'}, c: {model_source: 'probed'},
  }}], [{row: {line_no: 1}, sections: {
    a: {usable: true}, b: {usable: true}, c: {usable: true, probe_policy_code: 'provider_stopped'},
  }}]);
  assert.match(summary, /本轮实测可用 1 段/);
  assert.match(summary, /仅有目录 1 段/);
  assert.match(summary, /受限或停止 1 段/);
});


test('bulk filtering reports hidden selections without dropping their operations', () => {
  const groups = Array.from({length: 54}, (_, i) => ({id: String(i), entries: [{enabled: true}], split: false}));
  let visible = groups.slice(0, 8);
  const nodes = new Map();
  const context = {
    $: id => { if (!nodes.has(id)) nodes.set(id, {dataset: {}, value: ''}); return nodes.get(id); },
    BM: {sel: new Set(groups.map(g => g.id))},
    BM_IDLE_HINT: 'select groups',
    bmFiltered: () => visible,
    bmSelected: () => groups.filter(g => context.BM.sel.has(g.id)),
    bmKey: g => g.id, bmCard: () => '<article></article>', bmPresets() {}, bmdSync() {},
  };
  vm.createContext(context);
  vm.runInContext(sourceFunction('bmRender') + '\nthis.render = bmRender;', context);
  context.render();
  assert.match(nodes.get('#bmn2').textContent, /可见选中 8 组，隐藏选中 46 组；总计 54 组/);
  assert.equal(nodes.get('#bmsetpri').disabled, false);
  visible = [];
  context.render();
  assert.match(nodes.get('#bmn2').textContent, /可见选中 0 组，隐藏选中 54 组；总计 54 组/);
  assert.equal(nodes.get('#bmsetpri').disabled, false);
  context.BM.sel.clear();
  context.render();
  assert.match(nodes.get('#bmn2').textContent, /总计 0 组/);
  assert.equal(nodes.get('#bmsetpri').disabled, true);
});
