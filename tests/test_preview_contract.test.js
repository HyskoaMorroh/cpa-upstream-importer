'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
function view(data) {
  const match = source.match(/(?:^|\n)function planDiffView\([^]*?\n\}/m);
  assert.ok(match, 'The preview must interpret the server diff contract');
  const context = {};
  vm.createContext(context);
  vm.runInContext(`${match[0]}\nthis.view = planDiffView;`, context);
  return context.view(data);
}
const patch = '--- config.yaml (before)\n+++ config.yaml (after)\n@@ -1,2 +1,2 @@\n-priority: 1\n+priority: 2\n # retained';

test('replacement uses unified diff, not every line of the new config as additions', () => {
  const result = view({preview_kind: 'incremental', unified_diff: patch,
    diffs: [{section: 'config.yaml', lines: Array(9645).fill('unchanged')} ]});
  assert.equal(result.text, patch);
  assert.equal(result.added, 1);
  assert.equal(result.removed, 1);
  assert.equal(result.replacement, true);
  assert.equal(result.hasChanges, true);
});

test('empty authoritative diff means no changes even if full replacement text exists', () => {
  const result = view({unified_diff: '', diffs: [{section: 'config.yaml', lines: ['unchanged']}]});
  assert.equal(result.hasChanges, false);
  assert.equal(result.added, 0);
  assert.equal(result.removed, 0);
});

test('truncated unified diff stays explicitly incomplete', () => {
  const result = view({unified_diff: patch, unified_diff_truncated: true});
  assert.equal(result.truncated, true);
  assert.equal(result.text, patch);
});

test('legacy insertion-only responses keep line boundaries and count inserted lines', () => {
  const result = view({diffs: [{section: 'codex-api-key', lines: ['  - api-key: "***"\n', '    priority: 2\n']}]});
  assert.equal(result.text, '+  - api-key: "***"\n+    priority: 2');
  assert.equal(result.added, 2);
  assert.equal(result.removed, 0);
  assert.equal(result.replacement, false);
});

test('legacy replacement without unified diff is not labelled entirely new', () => {
  const result = view({diffs: [{section: 'config.yaml', text: 'retained\nchanged\n', lines: ['retained', 'changed']}]});
  assert.equal(result.replacement, true);
  assert.equal(result.added, null);
  assert.equal(result.removed, null);
  assert.equal(result.text, 'retained\nchanged\n');
});

test('diff body starting with three pluses or minuses is not confused with file headers', () => {
  const result = view({unified_diff: '--- a\n+++ b\n@@ -1 +1 @@\n--- old-content\n+++ new-content'});
  assert.equal(result.added, 1);
  assert.equal(result.removed, 1);
});

test('hostile HTML and large diff stay literal text without expansion into spans', () => {
  const text = patch + '\n+<img src=x onerror=alert(1)>\n' + ' unchanged\n'.repeat(12000);
  assert.equal(view({unified_diff: text}).text, text);
  const handler = source.slice(source.indexOf("$('#btnplan').onclick"), source.indexOf("$('#btnreplan').onclick"));
  assert.match(handler, /textContent\s*=\s*(?:view|preview)\.text/);
  assert.doesNotMatch(handler, /x\.lines\.map/);
});

function renderGroups(groups, mode = 'all') {
  const nodes = new Map();
  const context = {
    BM: {sel: new Set()}, bmKey: g => `${g.section || ''}:${g.host || ''}`,
    bmFiltered: () => groups, bmCard: () => '<div>synthetic card</div>',
    bmSelected: () => [], bmPresets() {}, bmdSync() {}, BM_IDLE_HINT: 'select groups',
    $: selector => {
      if (!nodes.has(selector)) nodes.set(selector, {innerHTML: '', textContent: '', value: mode, dataset: {}});
      return nodes.get(selector);
    },
  };
  vm.createContext(context);
  const match = source.match(/(?:^|\n)function bmRender\([^]*?\n\}/m);
  assert.ok(match);
  vm.runInContext(`${match[0]}\nbmRender();`, context);
  return nodes;
}

test('cross-protocol priorities do not become same-protocol split warnings', () => {
  const nodes = renderGroups([{split: false, site_split: true, entries: [{priority: 3}]}]);
  assert.doesNotMatch(nodes.get('#bmscope').innerHTML, /1 组分裂/);
  assert.match(nodes.get('#bmscope').innerHTML, /跨段不同档.*允许/);
});

test('real splits remain visible and distinct from permitted cross-protocol differences', () => {
  const nodes = renderGroups([{split: true, site_split: true, entries: [{priority: 3}, {priority: 4}]}]);
  assert.match(nodes.get('#bmscope').innerHTML, /1 组分裂/);
});

test('an empty split filter explains the difference instead of implying lost routes', () => {
  assert.match(renderGroups([], 'split').get('#bmgrid').innerHTML, /同协议.*分裂/);
});
