'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../docs/配置教程.html'), 'utf8');
const script = html.match(/<script[^>]*>([^]*?)<\/script>/)[1];
for (const mode of ['clipboard', 'http', 'denied', 'clipboard-rejected']) {
  test('tutorial copying reports real outcome on ' + mode, async () => {
    let click, copied = '', selected = false, fallbackCalls = 0;
    const button = {parentElement: {querySelector: () => pre}, classList: {add() {}, remove() {}}, addEventListener(_event, fn) {click = fn;}};
    const pre = {textContent: 'docker compose ps', parentNode: {insertBefore() {}}};
    const document = {
      querySelectorAll(selector) {assert.equal(selector, '.copy'); return [button];},
      getElementById() {return {addEventListener() {}};},
      documentElement: {removeAttribute() {}, setAttribute() {}},
      createElement(tag) {return tag === 'button' ? button : {appendChild() {}};},
      createRange() {return {selectNodeContents() {selected = true;}};},
      execCommand(command) {assert.equal(command, 'copy'); fallbackCalls++; return mode !== 'denied';},
    };
    const navigator = mode === 'clipboard' ? {clipboard: {writeText: async text => {copied = text;}}} : mode === 'clipboard-rejected' ? {clipboard: {writeText: async () => {throw new Error('denied');}}} : {};
    const window = {getSelection: () => ({removeAllRanges() {selected = false;}, addRange() {selected = true;}})};
    vm.runInNewContext(script, {document, navigator, window, setTimeout() {}});
    assert.equal(typeof click, 'function');
    click(); await Promise.resolve(); await Promise.resolve();
    if (mode === 'denied') {
      assert.match(button.textContent, /请手动复制/);
      assert.doesNotMatch(button.textContent, /已复制/);
      assert.equal(selected, true, 'manual copy keeps selection');
    }
    else assert.match(button.textContent, /已复制/);
    if (mode === 'clipboard') {assert.equal(copied, pre.textContent); assert.equal(fallbackCalls, 0);}
    else assert.equal(fallbackCalls, 1);
    if (mode !== 'denied') assert.equal(selected, false);
  });
}

test('published tutorial gives every command block a copy control', () => {
  const blocks = [...html.matchAll(/<div class="codebox">([^]*?)<pre>([^]*?)<\/pre><\/div>/g)];
  const pres = [...html.matchAll(/<pre\b/g)].length;
  assert.ok(pres >= 4);
  assert.equal(blocks.length, pres);
  assert.equal([...html.matchAll(/<button class="copy"/g)].length, pres);
  for (const [, controls, command] of blocks) {
    assert.match(controls, /<button class="copy" type="button">复制<\/button>/);
    assert.ok(command.trim());
  }
});

test('published deployment examples distinguish shared mappings and recovery limits', () => {
  assert.match(html, /CPA_MANAGEMENT_TOKEN: "\$\{CPA_MANAGEMENT_PASSWORD:-\}"/);
  assert.match(html, /\.\/config\.yaml:\/data\/config\.yaml:Z/);
  assert.match(html, /importer-backups:\/backups:Z/);
  assert.match(html, /proxy_pass http:\/\/127\.0\.0\.1:8765;/);
  assert.match(html, /http:\/\/upstream-importer:8765/);
  assert.match(html, /id="config-example"/);
  assert.match(html, /api-key-entries:/);
  assert.match(html, /REPLACE_WITH_VERIFIED_MODEL_ID/);
  assert.match(html, /不要新建重复 services 或覆盖整份共享文件/);
  assert.match(html, /关闭或刷新页面不能视为取消后端任务/);
  assert.doesNotMatch(html, /手动停止任务会停止安排后续请求/);
});
