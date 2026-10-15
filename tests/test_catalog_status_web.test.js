const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('catalog age renders as text and refresh preserves operator input', async () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
  const code = source.slice(source.indexOf('function renderCatalogStatus()'), source.indexOf('function renderBands()'));
  const nodes = {};
  function element() {
    return {children: [], setAttribute() {}, appendChild(n) { this.children.push(n); },
      replaceChildren() { this.children = []; }};
  }
  nodes.cfgmeta = {parentNode: {insertBefore(n) { nodes[n.id] = n; }}};
  const S = {ctx: {market_top_gen_why: '8 天前，已超过 7 天 <script>', sections: {keep: true}}};
  const context = {$: id => nodes[id.slice(1)], document: {createElement: element}, S,
    api: async () => ({market_top_gen: {}, market_top_gen_lines: {}, market_top_gen_why: ''})};
  vm.createContext(context);
  vm.runInContext(code + '\nrenderCatalogStatus();', context);
  assert.match(nodes.catalogstatus.children[0].textContent, /8 天前/);
  assert.match(nodes.catalogstatus.children[0].textContent, /实测通过/);
  assert.equal(nodes.catalogstatus.children[0].innerHTML, undefined);
  const before = S.ctx.sections;
  await nodes.catalogstatus.children[1].onclick();
  assert.equal(nodes.catalogstatus.hidden, true);
  assert.equal(S.ctx.sections, before);
});
