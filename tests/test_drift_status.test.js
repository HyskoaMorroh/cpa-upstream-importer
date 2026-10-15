'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
function render(d) {
  const box = {};
  const context = {S: {ctx: {profile_drift: d}}, $: () => box, scheduleDriftPoll() {}};
  vm.createContext(context);
  const escaping = source.slice(source.indexOf('const esc ='), source.indexOf('\nconst fmt ='));
  const fn = source.slice(source.indexOf('function renderDrift()'), source.indexOf('// ── 运行环境'));
  vm.runInContext(`${escaping}\n${fn}\nrenderDrift();`, context);
  return box;
}
test('partial and failed evidence never renders green consistency', () => {
  const box = render({checked: true, partial: true, drifts: [], errors: ['<script>fixture</script>']});
  assert.equal(box.className, 'note w');
  assert.match(box.innerHTML, /覆盖范围未完整确认/);
  assert.match(box.innerHTML, /核对时间未知/);
  assert.match(box.innerHTML, /&lt;script&gt;/);
  assert.doesNotMatch(box.innerHTML, /<script>/);
});
test('expired or missing evidence timestamps stay limited', () => {
  for (const times of [{checked_at: 1, expires_at: 2}, {}]) {
    assert.equal(render({checked: true, drifts: [], ...times}).className, 'note w');
  }
});
test('fresh complete snapshot and failed check have distinct states', () => {
  const now = Date.now() / 1000;
  const box = render({checked: true, drifts: [], immutable: true, checked_at: now, expires_at: now + 600});
  assert.equal(box.className, 'note g');
  assert.match(box.innerHTML, /固定提交/);
  assert.match(render({checked: false}).innerHTML, /未获得可用核对结果/);
});

test('expired drift conclusion stays limited while its source snapshot is fresh', () => {
  const now = Date.now() / 1000;
  const box = render({checked: true, drifts: [], immutable: true, refreshing: true,
    checked_at: now, expires_at: now + 600});
  assert.equal(box.className, 'note w');
  assert.match(box.innerHTML, /结论已过期，后台正在重新核对/);
  assert.doesNotMatch(box.innerHTML, /画像基线一致/);
});
test('failed refresh snapshot never renders green even within old TTL', () => {
 const now=Date.now()/1000;
 const box=render({checked:true,drifts:[],immutable:true,stale:true,checked_at:now,expires_at:now+600});
 assert.equal(box.className,'note w');assert.match(box.innerHTML,/沿用历史缓存/);
});
