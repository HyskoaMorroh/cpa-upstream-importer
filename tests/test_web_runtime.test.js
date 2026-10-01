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
  await assert.rejects(api('/synthetic'), error => error.invalidResponse === true && error.status === 200);
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
  const row = {classList: {toggle() {}}};
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
  vm.runInContext(sourceFunction('syncPickUI') + '\n' + sourceFunction('bindResultEvents'), context);
  return {context, listeners, checkbox};
}

test('selection can be changed before the first plan completes', () => {
  const fixture = selectionContext();
  fixture.context.bindResultEvents();
  assert.doesNotThrow(() => fixture.listeners.change({target: fixture.checkbox}));
  assert.ok(fixture.context.S.picks.has('1\u0000codex'));
});

test('synchronizing pre-plan controls preserves deferred default selection', () => {
  const fixture = selectionContext();
  assert.doesNotThrow(() => fixture.context.syncPickUI());
  assert.equal(fixture.context.S.picks, null);
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
  for (const name of ['planInputKey', 'invalidatePlanPreview', 'refreshPlan', '_refreshPlanOnce']) {
    if (source.includes(`function ${name}(`)) vm.runInContext(sourceFunction(name), context);
  }
  return fixture;
}

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

async function showFixturePreview(context) {
  const result = {plan_id: 'visible-plan', plans: [], valid: true,
    validate_msg: 'fixture valid', lines_before: 1, lines_after: 2,
    diffs: [{section: 'codex', host: 'fixture.invalid', insert_at: 1, lines: ['synthetic change']}]};
  context.refreshPlan = async () => {
    context.S.planId = result.plan_id;
    context.S.planInputKey = context.planInputKey();
    return result;
  };
  await applyHandler('btnplan', context)();
  assert.match(context.$('#diffs').innerHTML, /synthetic change/);
}

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
