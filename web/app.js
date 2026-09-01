/* 投喂台 —— 前端逻辑。原生 JS，无构建步骤，VPS 上直接跑。
 *
 * 进度回报走 HTTP 轮询（与 CPAMP 现有做法一致），不引入 SSE/WebSocket。
 * token 存 sessionStorage —— 关标签页即失效，不留在 localStorage 里。
 */
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => [].slice.call(document.querySelectorAll(s));
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (n) => (n == null ? '—' : Number(n).toLocaleString('en-US'));

const SECTION_LABEL = {
  'gemini-api-key': 'gemini',
  'codex-api-key': 'codex',
  'claude-api-key': 'claude',
  'openai-compatibility': 'compat',
};

// 判定类别 → 徽标样式。与后端 classify 的类别名一一对应。
const CAT_PILL = {
  '可用': 'p-ok', '余额': 'p-w', '限流': 'p-w', '边缘': 'p-w', '反测活': 'p-w',
  '临时': 'p-w', '限频': 'p-w', '门禁': 'p-b', 'IP封': 'p-b', '死路': 'p-b',
  '鉴权': 'p-b', '注入': 'p-i', '未知': 'p-m',
};

const THEMES = ['midnight', 'parchment', 'neon'];

const S = {
  token: '',
  ctx: null,
  jobId: null,
  cursor: 0,
  timer: null,
  results: null,
  planId: null,
  plans: null,
  overrides: {},        // {host: {section: {...}}}
  // 人工接管：{host: {section: [模型, ...]}}。探测判不可用但操作员确知可用的段。
  // 很多中转站不给测活（探针短消息被拦、分组限客户端），而真实对话正常。
  forced: {},
  picks: null,          // Set("host\u0000section")，null = 尚未初始化
  reuseSaved: 0,        // 形态复用省下的请求数
  reuseSeen: null,      // 已计数过的 shape-reused 事件键（防重拉重复累加）
  diagYaml: null,       // 诊断结果的 YAML 片段 {段: 文本}。不进 HTML 属性
  keepOpen: '',         // 重渲染后要重新展开哪个 headers 编辑器（pk(host,sec)）
  parsedValid: 0,       // 上次解析出的有效行数。决定「开始探测」能不能点
};

const pk = (host, sec) => `${host}\u0000${sec}`;

// ── 主题：三套，存 localStorage（这个不含秘密，可持久） ──
function applyTheme(t) {
  if (!THEMES.includes(t)) t = THEMES[0];
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem('importer_theme', t); } catch { /* 隐私模式 */ }
  $$('#themes button').forEach((b) => b.classList.toggle('on', b.dataset.t === t));
}
$('#themes').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-t]');
  if (b) applyTheme(b.dataset.t);
});
(() => {
  let t = THEMES[0];
  try { t = localStorage.getItem('importer_theme') || t; } catch { /* ignore */ }
  applyTheme(t);
})();

// ── 鉴权 ──
async function api(path, opts = {}) {
  const o = Object.assign({ headers: {} }, opts);
  o.headers['Authorization'] = 'Bearer ' + S.token;
  if (o.body && typeof o.body !== 'string') {
    o.headers['Content-Type'] = 'application/json';
    o.body = JSON.stringify(o.body);
  }
  const r = await fetch(path, o);
  const txt = await r.text();
  let data;
  try { data = JSON.parse(txt); } catch { data = { error: txt.slice(0, 400) }; }
  if (!r.ok) throw Object.assign(new Error(data.error || r.statusText),
    { data, status: r.status });
  return data;
}

function step(n) {
  $$('.rstep').forEach((el) => {
    const s = +el.dataset.s;
    el.classList.toggle('on', s === n);
    el.classList.toggle('done', s < n);
  });
}

async function boot() {
  const q = new URLSearchParams(location.search);
  S.token = q.get('token') || sessionStorage.getItem('importer_token') || '';
  if (S.token) {
    sessionStorage.setItem('importer_token', S.token);
    // 别把 token 留在地址栏 —— 会进浏览器历史
    if (q.get('token')) history.replaceState(null, '', location.pathname);
  }
  if (!S.token) { $('#gate').hidden = false; return; }
  try {
    S.ctx = await api('/api/context');
  } catch (e) {
    sessionStorage.removeItem('importer_token');
    $('#gate').hidden = false;
    if (e.status !== 401) {
      $('#gate .panel').insertAdjacentHTML('beforeend',
        `<div class="err">${esc(e.message)}</div>`);
    }
    return;
  }
  $('#app').hidden = false;
  const entries = Object.values(S.ctx.sections).reduce((a, b) => a + b.entries, 0);
  $('#cfgmeta').textContent =
    `${S.ctx.lines.toLocaleString()} 行 · ${entries} 条目`;
  renderBands();
  applyResources();
  renderDrift();
  updateBudget();
}

// ── 画像基线漂移 ──
// CPA 升级换了默认头而画像梯没跟上时，探测发的形态与 CPA 实际转发的不一致，
// 「探测通了但 CPA 不通」或反之都会发生。这里把核对结果显示出来 ——
// 包括「没能核对」这一种，那比让人以为全都比过了要好。
function renderDrift() {
  const box = $('#driftbox');
  if (!box) return;
  const d = S.ctx && S.ctx.profile_drift;
  if (!d) { box.hidden = true; return; }

  if (!d.checked) {
    box.className = 'note';
    box.innerHTML = `<b>画像基线未核对。</b>${esc(d.why || '')}`;
    box.hidden = false;
    return;
  }

  const warns = (d.drifts || []).filter((x) => x.severity === 'warn');
  const infos = (d.drifts || []).filter((x) => x.severity !== 'warn');

  // 版本标注：源码 commit 与运行中 CPA 的 commit。两者不一致时下面的比对
  // 是按源码做的，而实际转发用旧二进制 —— 那种情形单独拎出来说。
  const ver = [];
  if (d.source_commit) ver.push(`源码 ${esc(d.source_commit)}`);
  if (d.runtime_commit) ver.push(`运行中 ${esc(d.runtime_commit)}`);
  const verText = ver.length ? ` · ${ver.join(' / ')}` : '';

  if (!d.drifts.length) {
    box.className = 'note g';
    let t = `<b>画像基线一致</b>（依据：${esc(d.source)}${verText}）`;
    if (d.partial) {
      t += `。未覆盖：${esc((d.uncovered || []).join('、'))}`;
    }
    box.innerHTML = t;
    box.hidden = false;
    return;
  }

  box.className = warns.length ? 'note w' : 'note';
  const rows = d.drifts.map((x) => {
    const mark = x.severity === 'warn' ? '⚠' : '·';
    const note = x.note ? `<div class="hint" style="margin-left:1.4em">${esc(x.note)}</div>` : '';
    return `<div>${mark} ${esc(x.what)}：画像梯 <code>${esc(x.ours)}</code>`
      + ` · CPA <code>${esc(x.theirs)}</code></div>${note}`;
  }).join('');
  box.innerHTML =
    `<b>画像基线漂移 ${d.drifts.length} 处</b>`
    + (warns.length ? `（${warns.length} 处需处理）` : '')
    + `（依据：${esc(d.source)}${verText}）`
    + `<div style="margin-top:6px">${rows}</div>`
    + `<div class="hint" style="margin-top:6px">漂移意味着探测发的形态与 CPA `
    + `实际转发的不一致 —— 可能出现「探测通了但 CPA 不通」或反之。</div>`;
  box.hidden = false;
}

// ── 运行环境与推荐并发 ──
// 容器里 os.cpu_count() 是宿主机核数，所以推荐值由后端读 cgroup 算出（见
// cpa_probe/resources.py）。这里只负责显示依据 —— 一个凭空出现的数字用户
// 没法判断该不该改，所以把「4 核 × 12 = 48」这句原样摊开。
function applyResources() {
  const r = S.ctx && S.ctx.resources;
  const hint = $('#o_workers_hint');
  if (!r) { if (hint) hint.textContent = '读不到运行环境，用默认值 30'; return; }

  S.recWorkers = r.recommended_workers;
  const inp = $('#o_max_workers');
  if (inp) inp.value = r.recommended_workers;

  const mem = r.memory_mb ? `${(r.memory_mb / 1024).toFixed(1)}G` : '未知';
  const where = r.in_container ? '容器' : '宿主机';
  if (hint) {
    hint.textContent =
      `推荐 ${r.recommended_workers}（${where} ${r.cpus} 核 / ${mem}）· ${r.reason}`;
  }
  const box = $('#o_workers_notes');
  if (box && r.notes && r.notes.length) {
    box.innerHTML = r.notes.map((n) => `<div>· ${esc(n)}</div>`).join('');
    box.hidden = false;
  }
}

$('#o_workers_auto').onclick = () => {
  if (S.recWorkers) {
    $('#o_max_workers').value = S.recWorkers;
    updateBudget();
  }
};

// ── 请求预算估算 ──
// 每段画像档数写死在这里是有意的：它来自 profiles.py 的梯子长度，而那个
// 不会随配置变。估的是**最坏情形**（四段全不通、走完整梯），因为用户要判断
// 的正是「最坏要花多少」。
const LADDER_LEN = { gemini: 3, codex: 5, claude: 7, compat: 6 };
const SEEDS = { gemini: 2, codex: 2, claude: 2, compat: 3 };

function updateBudget() {
  const el = $('#budget_text');
  if (!el) return;

  const reuse = $('#o_reuse_verdict').checked;
  const attempts = parseInt($('#o_max_attempts').value, 10) || 10;
  const ctx = $('#o_ctx').checked;
  const swap = parseInt($('#o_swap').value, 10) || 0;

  // 最坏：四段全不通。baseline 每段每种子 1 次 + 画像梯
  let worst = 0;
  let best = 0;
  for (const k of Object.keys(LADDER_LEN)) {
    const seeds = SEEDS[k];
    worst += seeds;                                   // baseline
    worst += LADDER_LEN[k] * (reuse ? 1 : seeds);     // 画像梯
    best += 1;                                        // 首个种子就通
  }
  // 通的段还要验模型 + 换模采样 + 上下文二分
  const perOkSection = attempts + (swap > 1 ? swap : 0) + (ctx ? 6 : 0);
  best += perOkSection * 4;

  const sites = (S.ctx && S.ctx.existing_count) || 0;
  const full = $('#o_full_redetect').checked;
  const n = full ? sites + 1 : 1;
  const workers = parseInt($('#o_max_workers').value, 10) || 1;
  const gap = parseFloat($('#o_gap').value) || 0;

  // 耗时：每站的请求在四段间并行，同段内串行且受 gap 约束。
  // 粗估单站墙钟 = (单段最坏请求数) × (响应 1.5s + gap)
  const perSection = Math.ceil(worst / 4);
  const perSite = perSection * (1.5 + gap);
  const mins = (n / Math.max(1, workers)) * perSite / 60;

  let txt = `：单站全不通约 ${worst} 次请求`
    + `，全通约 ${best} 次`;
  if (full && sites) {
    txt += ` · ${n} 个站 × ${workers} 并发 ≈ ${mins.toFixed(1)} 分钟`;
  }
  if (!reuse) {
    const saved = Object.keys(LADDER_LEN)
      .reduce((a, k) => a + LADDER_LEN[k] * (SEEDS[k] - 1), 0);
    txt += ` · 关掉画像复用多花 ${saved} 次/站`;
  }
  el.textContent = txt;
}

['#o_max_models', '#o_max_attempts', '#o_reuse_verdict', '#o_ctx',
 '#o_swap', '#o_gap', '#o_max_workers', '#o_full_redetect'].forEach((sel) => {
  const el = $(sel);
  if (el) el.addEventListener('change', updateBudget);
  if (el && el.type === 'number') el.addEventListener('input', updateBudget);
});

$('#toksave').onclick = () => {
  const v = $('#tokin').value.trim();
  if (!v) return;
  sessionStorage.setItem('importer_token', v);
  location.reload();
};
$('#tokin').onkeydown = (e) => { if (e.key === 'Enter') $('#toksave').click(); };

// ── 档位谱 ──
// 站是死的还是活的 —— 决定「挡住它」有没有代价。
// dead   = weight:0，CPA 的 selector 已把它整个剔除（强信号）
// unwell = config.yaml 注释里记着实测不可用（弱信号，可能过期）
function hostState(b, host) {
  const h = String(host || '').toLowerCase();
  if ((b.dead_hosts || []).some((x) => String(x).toLowerCase() === h)) return 'dead';
  if ((b.unhealthy_hosts || []).some((x) => String(x).toLowerCase() === h)) return 'unwell';
  return 'live';
}

function renderBands() {
  const box = $('#bands');
  box.innerHTML = S.ctx.section_order.map((sec) => {
    const b = S.ctx.sections[sec];
    const gapSet = new Map(b.gaps.map(([lo, hi]) => [hi, [lo, hi]]));
    const rows = [];
    b.tiers.forEach((p) => {
      // 逐站标出死活 —— 「这一档挡住 9 个站」与「这一档挡住 9 个**死**站」
      // 是完全不同的两件事，只报数字会让人误判风险。
      const hs = (b.hosts_at[String(p)] || []).map((h) => {
        const st = hostState(b, h);
        if (st === 'dead') {
          return `<span class="hdead" title="weight:0 —— CPA 已把它逐出调度池，挡住它零代价">${esc(h)} ✗</span>`;
        }
        if (st === 'unwell') {
          return `<span class="hunwell" title="config.yaml 注释里记着实测不可用（弱信号，可能过期）">${esc(h)} ⚠</span>`;
        }
        return esc(h);
      }).join('、');
      rows.push(`<div class="tier"><span class="p">${p}</span>
        <span class="h">${hs}</span></div>`);
      const g = gapSet.get(p);
      if (g) {
        rows.push(`<div class="tier gap"><span class="p">${g[0]}↔${g[1]}</span>
          <span class="h">空档 ${g[1] - g[0]}</span></div>`);
      }
    });

    // 顶层是不是单点 —— 层级隔离下这决定「该段一挂就整段不可用」
    const topHosts = b.hosts_at[String(b.top)] || [];
    const topLive = topHosts.filter((h) => hostState(b, h) === 'live');
    let warn = '';
    if (topHosts.length && topLive.length === 0) {
      warn = `<div class="tier bad">顶层 ${b.top} 的站全部实测不可用 ——
        <b>该段现在应该完全不可用</b>。层级隔离下下层一个都轮不到。</div>`;
    } else if (new Set(topHosts).size === 1) {
      warn = `<div class="tier warn">顶层只有 1 个站（${esc(topHosts[0])}）——
        <b>单点</b>。它一挂整段立刻不可用，下层站一个都顶不上。</div>`;
    }

    // 注释里提到却匹配不上任何站的短名 —— 静默漏判的可见化
    let unmatched = '';
    if ((b.unmatched_notes || []).length) {
      unmatched = `<div class="tier warn">注释里的
        ${b.unmatched_notes.length} 个短名匹配不上任何站
        （${esc(b.unmatched_notes.join('、'))}）——
        它们的「实测不可用」结论<b>没作用到定档上</b>。
        成因：别名表只能从 compat 段的 name 字段建，
        把这些站也加进 compat 段即可修复。</div>`;
    }

    const nDead = (b.dead_hosts || []).length;
    const nUnwell = (b.unhealthy_hosts || []).length;
    const health = (nDead || nUnwell)
      ? ` · <span class="hint">${nDead ? nDead + ' 死' : ''}${nDead && nUnwell ? ' / ' : ''}${nUnwell ? nUnwell + ' 疑' : ''}</span>`
      : '';

    return `<div class="band">
      <div class="bt"><b>${esc(SECTION_LABEL[sec] || sec)}</b>
        <span>${b.entries} 条目 · ${b.tiers.length} 档 · 顶 ${b.top}${health}</span></div>
      ${warn}${unmatched}${rows.join('')}
    </div>`;
  }).join('');
  $('#pbands').hidden = false;
}

// ── 输入 ──
function readFile(f) {
  if (!f) return;
  const rd = new FileReader();
  rd.onload = () => { $('#input').value = rd.result; doParse(); };
  rd.readAsText(f, 'utf-8');
}
$('#drop').onclick = () => $('#file').click();
$('#file').onchange = (e) => readFile(e.target.files[0]);
['dragover', 'dragenter'].forEach((ev) => $('#drop').addEventListener(ev, (e) => {
  e.preventDefault(); $('#drop').classList.add('over');
}));
['dragleave', 'drop'].forEach((ev) => $('#drop').addEventListener(ev, (e) => {
  e.preventDefault(); $('#drop').classList.remove('over');
}));
$('#drop').addEventListener('drop', (e) => readFile(e.dataTransfer.files[0]));
$('#o_ctx').onchange = () => { $('#costnote').hidden = !$('#o_ctx').checked; };

$('#btnparse').onclick = doParse;

async function doParse() {
  const text = $('#input').value;
  if (!text.trim()) { $('#parsemsg').textContent = '输入为空'; return; }
  let d;
  try { d = await api('/api/parse', { method: 'POST', body: { text } }); }
  catch (e) {
    $('#parsemsg').innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
    return;
  }

  // 同主机分组 —— 让「第 2 个 key 起复用形态」这件事在解析阶段就可见
  const byHost = {};
  d.valid.forEach((r) => { (byHost[r.host] = byHost[r.host] || []).push(r); });
  const hosts = Object.keys(byHost);
  const dupHosts = hosts.filter((h) => byHost[h].length > 1);

  const rows = d.valid.map((r, i) => {
    const n = byHost[r.host].length;
    const first = byHost[r.host][0] === r;
    return `<tr>
      <td class="num">${r.line_no}</td>
      <td class="m"><b>${esc(r.host)}</b>${n > 1
        ? ` <span class="pill p-i">${first ? '首个 · 全量探测' : '复用形态'}</span>` : ''}</td>
      <td class="m">${esc(r.key_masked)}</td>
      <td class="m" style="color:var(--ink-3)">${S.ctx.section_order
        .map((s) => `${SECTION_LABEL[s]}: ${esc(r.bases[s].replace(/^https?:\/\//, ''))}`)
        .join('<br>')}</td>
    </tr>`;
  }).join('');

  const bad = d.invalid.map((r) => `<tr class="off">
    <td class="num">${r.line_no}</td>
    <td colspan="3"><span class="pill p-b">${esc(r.error)}</span>
      <div class="mlist" style="margin-top:5px">${esc(r.raw || '')}</div></td>
  </tr>`).join('');

  $('#parsebody').innerHTML = `
    <div class="stat">
      <span>有效 <b>${d.valid.length}</b></span>
      <span>无效 <b>${d.invalid.length}</b></span>
      <span>主机 <b>${hosts.length}</b></span>
    </div>
    ${dupHosts.length ? `<div class="note g">
      ${dupHosts.length} 个主机有多个 Key（${esc(dupHosts.join('、'))}）——
      <b>段形态只探一次</b>，之后每个 Key 只验凭证本身。
      预计省下约 ${d.valid.length - hosts.length} 轮全量探测。</div>` : ''}
    <div class="tw"><table>
      <thead><tr>
        <th style="width:56px">行</th><th>主机</th>
        <th style="width:190px">Key（脱敏）</th><th>四段 base-url</th>
      </tr></thead>
      <tbody>${rows}${bad}</tbody></table></div>`;
  $('#pparse').hidden = false;
  S.parsedValid = d.valid.length;
  syncProbeBtn();
  $('#parsemsg').textContent = d.valid.length
    ? `${d.valid.length} 行可探测 · ${hosts.length} 个主机` : '没有有效行';
}

// 「开始探测」什么时候能点。
//
// 两种情形都成立，不能只看有没有粘贴账号：
//   · 增量模式 —— 必须有有效行，否则没东西可探
//   · 全量重探 —— **不需要新账号**。「只体检既有站，不加新的」是常见需求
//     （config.yaml 用久了想复核哪些还能用），后端 _api_probe 早就支持
//     （`not res.valid and not full_redetect` 才拒绝），但前端按钮一直卡着
//     「解析出有效行」这一个条件，于是那条路点不进去。
function syncProbeBtn() {
  const full = $('#o_full_redetect') && $('#o_full_redetect').checked;
  const hasRows = (S.parsedValid || 0) > 0;
  $('#btnprobe').disabled = !(hasRows || full);
}

// ── 全量重探勾选框交互 ──
$('#o_full_redetect').addEventListener('change', (e) => {
  const checked = e.target.checked;
  $('#o_max_workers_row').hidden = !checked;
  $('#full_redetect_warning').hidden = !checked;
  // 勾上就能点「开始探测」，哪怕输入框是空的 —— 「只体检既有站」是独立需求
  syncProbeBtn();

  const box = $('#existing_count_text');
  if (checked) {
    const n = S.ctx && S.ctx.existing_count;
    if (n != null) {
      // /api/context 在启动时已经拉过，直接用 —— 少一次请求，也避免
      // 勾选后要等网络才显示数字
      box.textContent = n > 0
        ? `config.yaml 中有 ${n} 个既有条目。留空上面的输入框即可「只体检既有站」。`
        : '未检测到既有条目（config.yaml 可能为空）。';
      S.existingCount = n;
      return;
    }
    api('/api/context').then((ctx) => {
      S.ctx = ctx;
      const c = ctx.existing_count || 0;
      S.existingCount = c;
      box.textContent = c > 0
        ? `config.yaml 中有 ${c} 个既有条目。留空上面的输入框即可「只体检既有站」。`
        : '未检测到既有条目（config.yaml 可能为空）。';
    }).catch(() => {
      box.textContent = '无法读取既有条目数量。';
    });
  }
});

// ── 单站诊断 ──
// 与批量导入是两个不同的意图，所以不共用流水线：这里只回答「这个站要什么头」，
// 不建 Job、不生成方案、不写回。想导入就点「填进上面」走正常流程 ——
// 诊断与写回之间必须有人工确认这一跳。
$('#btndiag').onclick = async () => {
  const url = $('#d_url').value.trim();
  const key = $('#d_key').value.trim();
  if (!url || !key) {
    $('#diagmsg').innerHTML = '<span style="color:var(--bad)">地址与密钥都要填</span>';
    return;
  }
  const btn = $('#btndiag');
  btn.disabled = true;
  $('#diagmsg').textContent = '诊断中…';
  $('#diagout').innerHTML = '';

  let d;
  try {
    d = await api('/api/diag', {
      method: 'POST',
      body: {
        url, key,
        section: $('#d_section').value,
        proxy: $('#d_proxy').checked ? 'http://mihomo:7890' : '',
      },
    });
  } catch (e) {
    $('#diagmsg').innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
    btn.disabled = false;
    return;
  }
  btn.disabled = false;
  $('#diagmsg').textContent = `${d.total_calls} 次请求`;
  renderDiag(d);
};

function renderDiag(d) {
  S.diagYaml = {};
  const blocks = Object.keys(d.sections).map((sec) => {
    const s = d.sections[sec];
    const rungs = s.rungs.map((g) => {
      const cls = g.ok ? 'rung hit' : 'rung miss';
      const mark = g.ok ? '✓' : ' ';
      const body = g.body_patch ? ' +body' : '';
      return `<div class="${cls}">`
        + `<span class="rs">${mark}</span>`
        + `<span class="rn">${esc(g.profile)}${body}</span>`
        + `<span class="rs">t${g.tier}</span>`
        + `<span class="rs">${esc(g.status)}</span>`
        + `<span class="rs">${esc(g.category || '')}</span>`
        + `<span class="rs">${g.elapsed_ms}ms</span>`
        + `<span class="rw">${esc(g.why || '')}</span>`
        + (g.excerpt ? `<div class="rw" style="flex-basis:100%;padding-left:8px">`
          + `${esc(String(g.excerpt).slice(0, 150))}</div>` : '')
        + `</div>`;
    }).join('');

    let concl;
    if (!s.hit) {
      concl = `<div class="note w" style="margin-top:10px">`
        + `<b>整梯 ${s.rungs.length} 档全不通。</b>`
        + `这不一定是站方拒绝你 —— 也可能是余额、限时段、或它只认浏览器。`
        + `看上面每档的正文摘要判断。`
        + `如果你确知这个站能用，导入时可以用「人工接管」填模型清单。</div>`;
    } else if (!Object.keys(s.needed_headers).length) {
      concl = `<div class="note g" style="margin-top:10px">`
        + `<b>baseline 就通，不需要任何 header。</b>`
        + `导入时 <code>headers</code> 留空即可。</div>`;
    } else {
      const yaml = yamlHeaders(s.needed_headers, s.base_url);
      S.diagYaml[sec] = yaml;
      concl = `<div class="hdrbox">`
        + `<div><b>最小必需画像：${esc(s.hit.profile)}</b>`
        + `（试了 ${s.rungs.length} 档）`
        + (s.needs_body ? ` · <b>还需要请求体字段</b>` : '')
        + `</div>`
        + (s.needs_body
          ? `<div class="hint" style="margin-top:5px">headers 表达不了它 ——`
            + ` claude 段可在条目里设 <code>fingerprint-profile: claude-code-cli</code>`
            + ` 让 CPA 自己补；其余三段配置层无解。</div>`
          : '')
        + `<div class="hint" style="margin-top:6px">下面这段可直接粘进`
        + ` <code>config.yaml</code> 的该段条目里：</div>`
        + `<pre>${esc(yaml)}</pre>`
        + `<div class="row" style="margin-top:8px">`
        + `<button class="mini" data-yaml="${esc(sec)}">复制 YAML</button>`
        + `<button class="mini" data-fill="1">填进上面的输入框</button>`
        + `</div></div>`;
    }

    return `<div style="margin-top:16px">`
      + `<div style="font-weight:650;margin-bottom:6px">`
      + `${esc(SECTION_LABEL[sec] || sec)}`
      + `<span class="hint"> · ${esc(s.model)} · ${s.calls} 次请求</span></div>`
      + rungs + concl + `</div>`;
  }).join('');

  $('#diagout').innerHTML = blocks;

  // YAML 通过 S.diagYaml 传递，不进 HTML 属性 —— 多行文本在属性里会被
  // 转义破坏（换行变实体、引号提前闭合）。
  $$('#diagout button[data-yaml]').forEach((b) => {
    b.onclick = () => {
      const text = (S.diagYaml || {})[b.dataset.yaml] || '';
      if (!text) { b.textContent = '没有内容'; return; }
      navigator.clipboard.writeText(text)
        .then(() => { b.textContent = '已复制'; setTimeout(() => { b.textContent = '复制 YAML'; }, 1400); })
        .catch(() => { b.textContent = '复制失败（请手工选中）'; });
    };
  });
  $$('#diagout button[data-fill]').forEach((b) => {
    b.onclick = () => {
      const line = `${$('#d_url').value.trim()},${$('#d_key').value.trim()}`;
      const cur = $('#input').value.trim();
      $('#input').value = cur ? `${cur}\n${line}` : line;
      $('#pdiag').open = false;
      $('#input').scrollIntoView({ behavior: 'smooth', block: 'center' });
      $('#btnparse').click();
    };
  });
}

// headers 渲染成 config.yaml 里的形态。缩进按四段现有条目的写法（2/4 空格）。
function yamlHeaders(h, baseUrl) {
  const lines = ['  - api-key: "<你的 key>"'];
  if (baseUrl) lines.push(`    base-url: ${JSON.stringify(baseUrl)}`);
  lines.push('    headers:');
  Object.keys(h).forEach((k) => {
    // 值里有 {uuid1} 这类模板变量时提示 —— 那是每请求都要新生成的，
    // 写死进配置没有意义（CPA 自己会补）。
    const v = String(h[k]);
    const tip = /\{uuid\d?\}|\{key_hash\}/.test(v) ? '   # 每请求新生成，CPA 会自动补' : '';
    lines.push(`      ${k}: ${JSON.stringify(v)}${tip}`);
  });
  return lines.join('\n');
}

// ── 探测 ──
$('#btnprobe').onclick = async () => {
  const fullRedetect = $('#o_full_redetect').checked;

  // 全量重探确认。文案按「有没有同时加新站」分开 —— 两种情形的影响面不同，
  // 用同一句话会让「只体检既有站」看起来也在往里加东西。
  if (fullRedetect) {
    const n = S.existingCount || 0;
    const newRows = S.parsedValid || 0;
    if (!n && !newRows) {
      alert('config.yaml 里没有既有条目，输入框也是空的 —— 没有可探测的对象。');
      return;
    }
    let msg;
    if (!newRows) {
      msg = `只体检既有条目：重新探测 ${n} 个，按结果重新生成 `
        + `headers / 代理 / 优先级 / 前缀。\n\n不会新增任何站。\n\n`
        + `写回前会给出完整 diff 供逐项确认。是否开始？`;
    } else {
      msg = `重新探测 ${n} 个既有条目，并与本次新增的 ${newRows} 行一起`
        + `重新生成配置。\n\n写回前会给出完整 diff 供逐项确认。是否开始？`;
    }
    if (!confirm(msg)) return;
  }

  const body = {
    text: $('#input').value,
    opts: {
      probe_context: $('#o_ctx').checked,
      proxy: $('#o_proxy').checked ? 'http://mihomo:7890' : '',
      gap: parseFloat($('#o_gap').value) || 0,
      swap_samples: parseInt($('#o_swap').value, 10) || 0,
      // 并行度。关掉时退回完全串行（老行为）—— 留这个开关是为了
      // 万一撞上「按账号而非按端点」限频的站，能一键回到旧节奏。
      // 并行不会放松任何站的限频：节流按 (host, section) 分桶，同段
      // 之间仍严格保持 gap 秒。
      workers: $('#o_fast').checked ? 4 : 1,
      candidate_workers: $('#o_fast').checked ? 4 : 1,
      // 请求预算（高级设置）。做成参数而不是常量，是因为取舍与站群有关：
      // 聚合站多时该压低尝试数，站少而模型杂时该放宽。
      max_models: parseInt($('#o_max_models').value, 10) || 4,
      max_model_attempts: parseInt($('#o_max_attempts').value, 10) || 10,
      reuse_profile_verdict: $('#o_reuse_verdict').checked,
    },
    full_redetect: fullRedetect,
    max_workers: fullRedetect ? parseInt($('#o_max_workers').value, 10) || 30 : undefined,
  };
  let d;
  try { d = await api('/api/probe', { method: 'POST', body }); }
  catch (e) {
    $('#parsemsg').innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
    return;
  }

  S.jobId = d.job_id; S.cursor = 0; S.picks = null;
  S.reuseSaved = 0; S.reuseSeen = null;
  $('#p1').hidden = true; $('#pparse').hidden = true; $('#p2').hidden = false;
  $('#stream').innerHTML = ''; $('#spin').hidden = false;
  $('#st_saved').textContent = '';
  step(2);
  poll();
};

// 轮询断了之后的出路。任务在服务端照常跑完，不该让用户重跑 293 秒。
function showResume() {
  const box = $('#p2resume');
  if (!box) return;
  box.hidden = false;
  $('#resumeid').textContent = `任务 ${S.jobId}`;
  // 断连期间的事件已经错过了 —— cursor 归零，重新拉全部日志，
  // 这样流水与统计都能对上，而不是从断点接一段残缺的。
  $('#btnresume').onclick = () => {
    box.hidden = true;
    S.pollFails = 0;
    S.cursor = 0;
    $('#stream').innerHTML = '';
    $('#spin').hidden = false;
    $('#p2h').textContent = '② 探测中';
    $('#p2tag').textContent = '已接回，正在重新拉取完整日志';
    poll();
  };
}

function poll() {
  clearTimeout(S.timer);
  S.timer = setTimeout(async () => {
    let d;
    try { d = await api(`/api/job/${S.jobId}?since=${S.cursor}`); }
    catch (e) {
      // 轮询失败**必须重试**，不能就此放弃。
      //
      // 实测踩到：一次探测跑了 293 秒，中途轮询断了一下，UI 就永久停在
      // 「② 探测中」——转圈不停、不重试、不给出路，而后端其实已经跑完了。
      // 长任务下断连是常态：nginx 默认 60 秒读超时、笔记本休眠、切换网络
      // 都会断。任务在服务端照常跑，前端没有理由因为一次失败就自我放弃。
      //
      // 401 例外：token 失效了，重试一万次也没用，直接让用户重新登录。
      if (e.status === 401) {
        $('#spin').hidden = true;
        $('#p2h').textContent = '② 登录已失效';
        $('#p2tag').textContent = '任务仍在服务端运行。重新登录后可用下面的按钮接回';
        $('#stream').insertAdjacentHTML('beforeend',
          `<div class="s5">凭据失效（401）。任务 ${esc(S.jobId)} 仍在跑，
           重新登录后点「接回任务」即可继续看进度。</div>`);
        showResume();
        return;
      }
      S.pollFails = (S.pollFails || 0) + 1;
      $('#stream').insertAdjacentHTML('beforeend',
        `<div class="s4">  轮询第 ${S.pollFails} 次失败（${esc(e.message)}）——
         任务仍在服务端跑，${S.pollFails >= 20 ? '已停止自动重试' : '继续重试'}</div>`);
      if (S.pollFails >= 20) {
        // 连续 20 次（约 1 分钟）都不通，多半不是抖动。停下来给出路，
        // 而不是无限刷日志。
        $('#spin').hidden = true;
        $('#p2h').textContent = '② 轮询中断';
        $('#p2tag').textContent = '任务可能仍在服务端运行 —— 用下面的按钮接回';
        showResume();
        return;
      }
      poll();
      return;
    }
    S.pollFails = 0;          // 通了就清零，只关心**连续**失败
    S.cursor = d.event_cursor;
    renderStream(d.events);
    $('#st_rows').textContent = `${d.done_rows}/${d.total_rows}`;
    $('#st_calls').textContent = d.calls;
    $('#st_time').textContent = d.elapsed;
    $('#prog').style.width =
      (d.total_rows ? d.done_rows / d.total_rows * 100 : 0) + '%';
    if (S.reuseSaved > 0) {
      $('#st_saved').innerHTML =
        `复用省下 <b>${S.reuseSaved}</b> 轮`;
    }

    if (d.state === 'done') {
      // 探测完成：停转圈、把标题从「探测中」改成「探测完成」。
      // 面板本身**不隐藏** —— 那份流水日志是判定依据，用户要能回看
      // （哪个段在哪个 combo 上通的、403 出现几次）。只是不再假装在跑。
      $('#spin').hidden = true;
      $('#p2h').textContent = '② 探测完成';
      $('#p2tag').textContent =
        `${d.calls} 次请求 · ${d.elapsed}s · 日志保留在下方可回看`;
      S.results = d.results;
      // renderResults 必须包起来。它抛异常时（某个字段形状没料到）原来会
      // 变成 unhandled rejection —— 转圈已停但第 3 步不出现，页面看着像
      // 「探测完了却卡住」，而控制台外没有任何线索。宁可显示错误也不要静默。
      try {
        renderResults(d.results);
      } catch (e) {
        $('#p2').insertAdjacentHTML('beforeend',
          `<div class="err">结果渲染失败：${esc(e.message)}<br>
           <span class="hint">探测本身已完成，数据在服务端。
           这是前端渲染的 bug —— 上面的流水日志仍可用于人工判定。</span>
           <pre>${esc((e.stack || '').slice(0, 600))}</pre></div>`);
      }
      return;
    }
    if (d.state === 'error') {
      $('#spin').hidden = true;
      $('#p2h').textContent = '② 探测出错';
      $('#p2').insertAdjacentHTML('beforeend',
        `<div class="err">探测出错<pre>${esc(d.error)}</pre></div>`);
      return;
    }
    poll();
  }, 900);
}

const pad = (s, n) => esc(String(s == null ? '' : s).padEnd(n));

function renderStream(events) {
  if (!events.length) return;
  const box = $('#stream');
  const html = events.map((e) => {
    if (e.kind === 'candidate-start') {
      return `<div class="hd">── ${esc(e.host)}  ${esc(e.key || '')}</div>`;
    }
    if (e.kind === 'candidate-done') {
      return `<div class="hd">   可用段 [${esc((e.usable || [])
        .map((s) => SECTION_LABEL[s]).join(' '))}] · ${e.calls} 次请求</div>`;
    }
    if (e.kind === 'proxy-precheck') {
      return e.ok
        ? `<div class="note">  代理预检通过：${esc(e.detail)}</div>`
        : `<div class="s4">  代理预检不通，本轮跳过全部 via-proxy —— ${esc(e.detail)}</div>`;
    }
    if (e.kind === 'shape-reused') {
      // 计数按事件序号去重 —— 轮询中断后 showResume 会把 cursor 归零重拉
      // 全部日志（app.js 的 showResume），累加式计数会把已经数过的再数一遍。
      // 事件在 job.events 里的下标是稳定的，拿它做幂等键。
      if (!S.reuseSeen) S.reuseSeen = new Set();
      const rk = `${e.t}|${e.section}|${e.host || ''}`;
      if (!S.reuseSeen.has(rk)) {
        S.reuseSeen.add(rk);
        S.reuseSaved += 1;
      }
      return `<div class="note">  ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `复用主机形态${e.verified ? (e.ok ? '（凭证已验）' : `（凭证不通：${esc(e.reason || '')}）`)
          : `（${esc(e.reason || '')}）`}</div>`;
    }
    if (e.kind === 'shape-reuse-abort') {
      return `<div class="s4">  ${pad(SECTION_LABEL[e.section], 8)} `
        + `${esc(e.reason || '')}</div>`;
    }
    // 站方负载上限（503/502/504）会重试一次。要让这一步可见 —— 否则
    // 用户只看到同一个模型出现两次、不知道为什么，也不知道等了 2 秒。
    if (e.kind === 'transient-retry') {
      return `<div class="note">  ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `${pad(e.model, 20)} ${esc(e.status)} 临时错误，${e.wait}s 后重试一次</div>`;
    }
    // 时段：分组按窗口开放。凭据是好的、不是站点问题 —— 窗口内重测。
    if (e.kind === 'time-window') {
      const win = e.window ? `${e.window[0]}~${e.window[1]}` : '未知';
      return `<div class="note">  ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `${esc(e.host)} 限时段（${win}）—— 窗口内复测</div>`;
    }
    // 画像命中：第几次试到通的、什么档、是否需 body 补丁
    if (e.kind === 'profile-hit') {
      const body = e.needs_body ? '+body' : '';
      return `<div class="s2">  ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `${esc(e.host)} 画像 ${esc(e.profile)}${body} 通（试 ${e.tried} 档）</div>`;
    }
    // 画像梯跑完仍不通 —— 让操作员看到「试了几档都不行」，不是「没试」
    if (e.kind === 'profile-exhausted') {
      return `<div class="s4">  ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `${esc(e.host)} 画像梯跑完仍不通（试 ${e.tried} 档）</div>`;
    }
    // 同段整梯已试过全败，后续种子跳过。**必须显示** —— 否则日志里看起来
    // 像是这个种子没被处理，而实际是刻意省掉的重复请求。
    if (e.kind === 'profile-skipped') {
      return `<div class="note">  ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `${pad(e.model, 20)} 跳过画像梯（${esc(e.why || '同段已试过')}）</div>`;
    }
    // 模型验证撞到尝试上限。显示剩余数，让操作员知道「不是全验了」
    if (e.kind === 'model-scan-capped') {
      return `<div class="note">  ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `${esc(e.host)} 模型验证达上限：试 ${e.attempted} 次收 ${e.accepted} 个，`
        + `余 ${e.remaining} 个未验</div>`;
    }
    // 200 但正文是错误体 / 换模 —— 模型被拒收，不进写入清单
    if (e.kind === 'model-rejected') {
      const why = e.reason ? esc(e.reason)
        : `请求 ${esc(e.requested)} 却回 ${esc(String(e.actual))}`;
      return `<div class="s4">  ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `${pad(e.requested, 20)} 模型不收：${why}</div>`;
    }
    if (e.kind === 'attempt') {
      const c = e.status === '200' ? 's2' : (e.status[0] === '4' ? 's4' : 's5');
      return `<div class="${c}">  ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `${pad(e.model, 20)} ${pad(e.combo, 18)} ${pad(e.status, 5)} `
        + `${esc(e.category)}</div>`;
    }
    if (e.kind === 'catalog') {
      return `<div>  ${pad(SECTION_LABEL[e.section], 8)} /models 目录 ${e.count} 个`
        + `（已按 gemini/gpt/claude 过滤）</div>`;
    }
    if (e.kind === 'swap') {
      return `<div class="s4">  ${pad(SECTION_LABEL[e.section], 8)} `
        + `静默换模 ${e.rate_pct}%（${esc(e.model)}）</div>`;
    }
    if (e.kind === 'context') {
      return `<div class="s2">  ${pad(SECTION_LABEL[e.section], 8)} `
        + `上下文上限 ${fmt(e.limit)}${e.untrusted ? '（由截断反推）' : ''}</div>`;
    }
    if (e.kind === 'context-declared') {
      // 上游在超限错误里**明说**了上限 —— 省掉整轮二分（最多 5 次百万字符
      // 请求）。这件事值得显示：它解释了为什么这个段没跑满二分轮次。
      return `<div class="s2">  ${pad(SECTION_LABEL[e.section], 8)} `
        + `上游自报上限 ${fmt(e.limit)} —— 免掉二分（${esc(e.model)}）</div>`;
    }
    if (e.kind === 'section-error') {
      // 某段探测抛异常。另外三段照常跑完，但这一段的失败必须可见 ——
      // 不显示的话它会表现成「这个段莫名不可用」。
      return `<div class="s5">  ${pad(SECTION_LABEL[e.section], 8)} `
        + `探测异常：${esc(e.error)}</div>`;
    }
    if (e.kind === 'section-done') {
      // 逐段收尾。并行下四段完成先后是乱的，这行让顺序可追溯。
      return `<div class="${e.usable ? 's2' : 's4'}">  `
        + `${pad(SECTION_LABEL[e.section], 8)} `
        + `${e.usable ? '✓' : '✗'} ${esc(e.summary || '')}</div>`;
    }
    // ── 全量重探路径的三种事件（run_job_full_redetect 发的）──
    // 漏了这三个分支它们会落到兜底，显示成 `[info] {"msg":"…"}` 的原始 JSON。
    // 而全量重探恰恰是最需要可读进度的场景（跑几分钟、几十个站）。
    if (e.kind === 'info') {
      return `<div class="s2">${esc(e.msg || '')}</div>`;
    }
    if (e.kind === 'progress') {
      const pct = e.total ? Math.round((e.current / e.total) * 100) : 0;
      const bar = '█'.repeat(Math.round(pct / 4))
        + '░'.repeat(25 - Math.round(pct / 4));
      const stat = [];
      if (e.success != null) stat.push(`成功 ${e.success}`);
      if (e.partial != null) stat.push(`部分通 ${e.partial}`);
      if (e.failure != null) stat.push(`失败 ${e.failure}`);
      return `<div class="s2">${bar} ${e.current}/${e.total} (${pct}%)`
        + (stat.length ? ` · ${stat.join(' · ')}` : '')
        + (e.site ? ` · 刚完成 ${esc(e.site)}` : '') + `</div>`;
    }
    if (e.kind === 'error') {
      return `<div class="s5">✗ ${esc(e.msg || '')}</div>`;
    }
    // 兜底：未认识的事件类型也要留痕，不能静默丢弃 ——
    // 静默丢弃会让「后端加了新事件、前端忘了处理」这种失配无从发现。
    if (e.kind && e.kind !== 'attempt') {
      return `<div class="s4">  [${esc(e.kind)}] ${esc(JSON.stringify(e)
        .slice(0, 160))}</div>`;
    }
    return '';
  }).join('');
  box.insertAdjacentHTML('beforeend', html);
  box.scrollTop = box.scrollHeight;
}

// ── 结果：每列都有表头，系统预勾选 ──
function renderResults(results) {
  $('#results').innerHTML = results.map(siteCard).join('');
  $('#p3').hidden = false;
  step(3);
  bindResultEvents();
  refreshPlan(true);
  $('#p3').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function siteCard(r) {
  const host = r.row.host;
  const rows = S.ctx.section_order.map((sec) => {
    const v = r.sections[sec];
    if (!v) return '';
    const label = SECTION_LABEL[sec] || sec;

    if (!v.usable) {
      const pill = CAT_PILL[v.category] || 'p-m';
      const last = (v.attempts || []).filter((a) => a.status !== '200').slice(-1)[0];
      // 探测失败的段也给勾选框 —— 判定会错，必须有人工出口。
      // 但要求先填模型清单：探测没验成功过任何模型，工具无从推断该注册什么。
      // 勾选框默认不勾，且只有填了模型才可勾（见 bindResultEvents）。
      const fm = ((S.forced[host] || {})[sec] || []).join(', ');
      return `<tr class="off" data-host="${esc(host)}" data-sec="${esc(sec)}">
        <td class="pick"><input type="checkbox" class="sel force"
          data-host="${esc(host)}" data-sec="${esc(sec)}"
          title="探测未通过。要接管请先在右侧填模型名"></td>
        <td class="m"><b>${esc(label)}</b></td>
        <td><span class="pill ${pill}">${esc(v.category || '不可用')}</span></td>
        <td>${esc(v.action || '不写入')}
          ${last && last.excerpt
            ? `<div class="mlist">${esc(last.status)} · ${esc(last.excerpt.slice(0, 150))}</div>`
            : ''}</td>
        <td colspan="4">
          <div class="pedit"><input type="text" class="fm" style="width:100%"
            data-host="${esc(host)}" data-sec="${esc(sec)}"
            value="${esc(fm)}"
            placeholder="不给测活的站：手填模型名，逗号分隔，如 claude-opus-5"></div>
          <div class="hint">填了才能勾选。工具不会验证这些模型 ——
            写错会让 CPA 每次轮到它都失败</div>
        </td>
      </tr>`;
    }

    const flags = [];
    // 代理不只标「需要」，把实际地址也显示出来 —— 写进 config.yaml 的是
    // 具体地址，而容器内外解析不同（mihomo:7890 vs 127.0.0.1:7890），
    // 只显示「需代理」看不出到底会写哪个。
    if (v.need_proxy) {
      const pu = (v.attempts || []).map((a) => a.proxy).filter(Boolean)[0];
      flags.push('<span class="pill p-w">需代理</span>'
        + (pu ? `<div class="hint">${esc(pu)}</div>` : ''));
    }
    if (Object.keys(v.min_headers || {}).length) {
      flags.push(`<span class="pill p-i">需 ${esc(Object.keys(v.min_headers).join('+'))}</span>`);
    }
    if (v.swap_detected) {
      flags.push(`<span class="pill p-b">换模 ${v.swap.rate_pct}%</span>`);
    }
    const backends = Object.keys((v.swap && v.swap.backends) || {});

    return `<tr data-host="${esc(host)}" data-sec="${esc(sec)}">
      <td class="pick"><input type="checkbox" class="sel"
        data-host="${esc(host)}" data-sec="${esc(sec)}"></td>
      <td class="m"><b>${esc(label)}</b></td>
      <td><span class="pill p-ok">可用</span></td>
      <td>
        <div class="mlist">${esc(v.models.join(', ')) || '<span class="hint">无可信模型</span>'}</div>
        ${backends.length ? `<div class="hint">后端 ${esc(backends.join(' / '))}</div>` : ''}
      </td>
      <td class="num">${v.max_context_length ? fmt(v.max_context_length) : '—'}
        ${v.context_untrusted ? '<div class="hint">截断反推</div>' : ''}
        ${v.context_model ? `<div class="hint">@${esc(v.context_model)}</div>` : ''}</td>
      <td>${flags.join(' ') || '<span class="hint">直连即可</span>'}</td>
      <td class="prio">
        <div class="pedit"><input type="number" class="pi"
          data-host="${esc(host)}" data-sec="${esc(sec)}" placeholder="待定"></div>
      </td>
      <td class="rsn"><span class="hint">计算中…</span></td>
    </tr>
    <tr class="wrow" data-host="${esc(host)}" data-sec="${esc(sec)}">
      <td></td><td colspan="7" class="wbox"></td>
    </tr>`;
  }).join('');

  // 尝试明细：每段一张表，放在站卡最下面。
  // 12 个字段后端一直在返回，而结果表只显示了 status 与 excerpt ——
  // 排障时真正要看的「哪一档通的、别的档报什么、哪个慢」都在这里。
  const detail = S.ctx.section_order.map((sec) => {
    const v = r.sections[sec];
    if (!v || !(v.attempts || []).length) return '';
    return attemptTable(SECTION_LABEL[sec] || sec, v);
  }).filter(Boolean).join('');

  return `<div class="site">
    <div class="sh">
      <span class="h">${esc(host)}</span>
      <span class="pill p-m">${esc(r.row.key_masked)}</span>
      <div class="sp"></div>
      <span class="hint">${r.total_calls} 次请求 · 可用 ${r.usable_sections.length}/4 段</span>
    </div>
    <div class="tw"><table>
      <thead><tr>
        <th style="width:44px">写入</th>
        <th style="width:88px">段</th>
        <th style="width:96px">判定</th>
        <th>可信模型</th>
        <th style="width:118px">上下文上限</th>
        <th style="width:150px">处置</th>
        <th style="width:132px">priority</th>
        <th style="width:290px">系统建议</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    ${detail}
  </div>`;
}

// 一段的全部尝试。默认折起 —— 一个四段全不通的站有 30 次尝试，
// 摊开会把结果页顶得很长，而多数时候只需要看汇总。
function attemptTable(label, v) {
  const at = v.attempts || [];
  const ok = at.filter((a) => a.status === '200').length;
  const slow = Math.max(...at.map((a) => a.elapsed_ms || 0));
  // 按列有没有数据决定要不要这一列。探针正文只 88 字符，所以「发送字符」
  // 通常整列为空（只有上下文二分那几次是几十万）；「入 token」也只有 200
  // 响应才有。留一个恒空的列比不留更糟 —— 它看起来像是数据丢了。
  const hasTok = at.some((a) => a.input_tokens != null);
  const hasSent = at.some((a) => (a.sent_chars || 0) > 1000);

  const rows = at.map((a) => {
    const good = a.status === '200';
    // resp_model 与请求的不同 = 换模；相同则不必重复显示，留空更好读
    const rm = (a.resp_model && a.resp_model !== a.model)
      ? `<span class="pill p-b">→ ${esc(a.resp_model)}</span>` : '';
    return `<tr class="${good ? '' : 'off'}">
      <td class="m">${esc(a.model || '')}</td>
      <td class="m">${esc(a.combo || '')}</td>
      <td><span class="pill ${good ? 'p-ok' : (CAT_PILL[a.category] || 'p-m')}">${esc(a.status)}</span></td>
      <td>${esc(a.category || '')}</td>
      <td class="num">${a.elapsed_ms != null ? a.elapsed_ms + 'ms' : ''}</td>
      ${hasTok ? `<td class="num">${a.input_tokens != null ? fmt(a.input_tokens) : ''}</td>` : ''}
      ${hasSent ? `<td class="num">${(a.sent_chars || 0) > 1000 ? fmt(a.sent_chars) : ''}</td>` : ''}
      <td>${rm}${a.proxy ? '<span class="pill p-w">代理</span>' : ''}
        ${a.backend ? `<span class="hint">${esc(a.backend)}</span>` : ''}</td>
      <td>${a.excerpt ? `<span class="hint">${esc(String(a.excerpt).slice(0, 120))}</span>` : ''}</td>
    </tr>`;
  }).join('');

  return `<details class="adet">
    <summary>${esc(label)} 的 ${at.length} 次尝试
      <span class="hint">${ok} 次 200 · 最慢 ${slow}ms</span></summary>
    <div class="tw"><table>
      <thead><tr>
        <th style="width:150px">模型</th>
        <th style="width:130px">画像/阶段</th>
        <th style="width:64px">状态</th>
        <th style="width:70px">类别</th>
        <th style="width:74px">耗时</th>
        ${hasTok ? '<th style="width:82px">入 token</th>' : ''}
        ${hasSent ? '<th style="width:88px">发送字符</th>' : ''}
        <th style="width:150px">后端</th>
        <th>正文摘要</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
  </details>`;
}

function bindResultEvents() {
  const box = $('#results');
  box.addEventListener('change', (e) => {
    // 人工接管的模型清单
    const fmi = e.target.closest('.fm');
    if (fmi) {
      const h = fmi.dataset.host, sc = fmi.dataset.sec;
      const list = fmi.value.split(',').map((x) => x.trim()).filter(Boolean);
      S.forced[h] = S.forced[h] || {};
      if (list.length) {
        S.forced[h][sc] = list;
      } else {
        delete S.forced[h][sc];
        // 模型清空了就不能再留着勾选 —— 后端会把空清单当成未接管而跳过该段，
        // 前端还勾着就成了「看着会写入实际不写」的错觉。
        S.picks && S.picks.delete(pk(h, sc));
      }
      refreshPlan(true);
      syncPickUI();
      return;
    }
    const inp = e.target.closest('.pi');
    if (inp) {
      const h = inp.dataset.host, s = inp.dataset.sec;
      S.overrides[h] = S.overrides[h] || {};
      S.overrides[h][s] = Object.assign(S.overrides[h][s] || {},
        { priority: parseInt(inp.value, 10) });
      refreshPlan(true);
      return;
    }
    const sel = e.target.closest('.sel');
    if (sel) {
      // 探测未通过的段：没填模型就不让勾 —— 空清单到后端会被当成
      // 未接管而跳过，勾了也不会写入。在这里拦住并说清原因。
      if (sel.classList.contains('force') && sel.checked) {
        const fl = (S.forced[sel.dataset.host] || {})[sel.dataset.sec] || [];
        if (!fl.length) {
          sel.checked = false;
          const row = sel.closest('tr');
          const box2 = row && row.querySelector('.fm');
          if (box2) box2.focus();
          $('#pickstat').textContent =
            '该段探测未通过 —— 要接管请先填模型名（逗号分隔）';
          return;
        }
      }
      const key = pk(sel.dataset.host, sel.dataset.sec);
      if (sel.checked) S.picks.add(key); else S.picks.delete(key);
      syncPickUI();
    }
  });

  const pb = $('#o_probation');
  if (pb) {
    pb.onchange = () => {
      // 切模式要清掉手工 priority，否则旧值会盖住重算结果
      Object.values(S.overrides).forEach((bySec) => {
        Object.values(bySec).forEach((ov) => { delete ov.priority; });
      });
      $$('#results .pi').forEach((i) => { i.value = ''; });
      refreshPlan(true);
    };
  }

  $('#pickrec').onclick = () => { applyPickPreset('rec'); };
  $('#pickall').onclick = () => { applyPickPreset('all'); };
  $('#picknone').onclick = () => { applyPickPreset('none'); };
}

function applyPickPreset(mode) {
  if (!S.plans) return;
  S.picks = new Set();
  S.plans.forEach((p) => {
    Object.entries(p.sections).forEach(([sec, sp]) => {
      if (!sp.writable) return;
      if (mode === 'all' || (mode === 'rec' && sp.recommended)) {
        S.picks.add(pk(p.host, sec));
      }
    });
  });
  syncPickUI();
}

function syncPickUI() {
  $$('#results .sel').forEach((el) => {
    el.checked = S.picks.has(pk(el.dataset.host, el.dataset.sec));
    const tr = el.closest('tr');
    if (tr) tr.classList.toggle('rec', el.checked);
  });
  const n = S.picks ? S.picks.size : 0;
  $('#pickstat').textContent = n ? `已勾选 ${n} 项写入` : '未勾选任何项';
  $('#btnplan').disabled = n === 0;
}

// ── 方案 ──
async function refreshPlan(silent) {
  let d;
  const body = {
    job_id: S.jobId,
    overrides: S.overrides,
    // 勾选框是「试用期定档」；服务端收反义的 by_score。
    // 元素缺失时回退到试用期（安全侧），不回退到激进档。
    by_score: $('#o_probation') ? !$('#o_probation').checked : false,
    forced: S.forced,
  };
  // 只有用户明确动过勾选才传 selected；首次让后端返回全部以便读 recommended
  if (S.picks) {
    body.selected = [...S.picks].map((k) => k.split('\u0000'));
  }
  try { d = await api('/api/plan', { method: 'POST', body }); }
  catch (e) {
    if (!silent) $('#planmeta').innerHTML = `<div class="err">${esc(e.message)}</div>`;
    return null;
  }
  S.planId = d.plan_id; S.plans = d.plans;

  // 首次：按系统建议预勾选
  if (S.picks === null) {
    applyPickPreset('rec');
    // 预勾选变了选择集，重取一次让 diff 与勾选一致
    return refreshPlan(true);
  }

  d.plans.forEach((p) => {
    Object.entries(p.sections).forEach(([sec, sp]) => {
      const tr = document.querySelector(
        `#results tr[data-host="${cssq(p.host)}"][data-sec="${cssq(sec)}"]:not(.wrow)`);
      if (!tr) return;
      const inp = tr.querySelector('.pi');
      if (inp && !inp.value) inp.value = sp.priority;
      const rsn = tr.querySelector('.rsn');
      if (rsn) {
        const cls = sp.recommended ? 'p-ok' : (sp.writable ? 'p-w' : 'p-m');
        const tag = sp.recommended ? '建议写入'
          : (sp.writable ? '需人工确认' : '不可写入');
        // score 一直没显示，而关掉「试用期定档」后 priority 就是按它算的 ——
        // 看不到分数等于那个开关的依据不可见。
        const sc = (sp.score != null)
          ? `<span class="hint"> · 得分 ${sp.score}</span>` : '';
        rsn.innerHTML = `<span class="pill ${cls}">${tag}</span>${sc}
          <div class="hint" style="margin-top:5px">${esc(sp.recommend_reason)}</div>
          <div class="hint">${esc(sp.priority_reason)}</div>`;
      }
      const wrow = document.querySelector(
        `#results tr.wrow[data-host="${cssq(p.host)}"][data-sec="${cssq(sec)}"]`);
      if (wrow) {
        const wb = wrow.querySelector('.wbox');
        const html = (sp.duplicate
          ? `<div class="warn b">已存在：${esc(sp.duplicate_note)}</div>` : '')
          + sp.warnings.map((w) =>
            `<div class="warn${/抢走|换模/.test(w) ? ' b' : ''}">${esc(w)}</div>`).join('')
          + impactTable(sp)
          + headerEditor(p.host, sec, sp);
        wb.innerHTML = html;
        // headers 编辑器一直在，所以 wrow 不再按 html 空否决定显隐 ——
        // 它现在总有内容。
        wrow.hidden = false;
        bindHeaderEditor(wb, p.host, sec);
        // 重渲染会把 <details> 的展开态清掉。刚才在编辑哪一段就把它重新展开 ——
        // 否则每次防抖结算完编辑器都自己收起来，等于没法连续改。
        if (S.keepOpen === pk(p.host, sec)) {
          const det = wb.querySelector('.hedit');
          if (det) det.open = true;
        }
      }
    });
  });
  syncPickUI();
  return d;
}
const cssq = (s) => String(s).replace(/["\\]/g, '\\$&');

// ── headers 手工编辑 ──
// 后端 _api_plan 早就认 overrides.headers，但前端一直只能整段接受探测结果。
// 两种情形都真实存在：探测判门禁但你从别处知道正确的头；探测给出的头多了一项
// （漂移检测就抓到过无条件发 oauth-2025-04-20 那一处）。
//
// 最要紧的设计点：**改动后必须标「未验证」**。探测是用原来那套跑通的，改了
// 就没测过了 —— 界面仍显示「✓ 可用」会让人以为改后的配置也验证过。
function headerEditor(host, sec, sp) {
  const ov = ((S.overrides[host] || {})[sec] || {});
  const edited = Object.prototype.hasOwnProperty.call(ov, 'headers');
  const cur = edited ? ov.headers : (sp.headers || {});
  const keys = Object.keys(cur);

  const rows = keys.map((k, i) => hdrRow(k, cur[k], i)).join('');
  const warn = edited
    ? `<div class="warn b">headers 已手工改过 —— 这一段的「已验证」不再成立。
         探测是用改动前那套跑通的。</div>`
    : '';

  return `<details class="hedit" data-host="${esc(host)}" data-sec="${esc(sec)}">
    <summary>请求头 <span class="hint">${keys.length} 项${edited ? ' · 已手工改过' : ''}</span></summary>
    <div class="hbody">
      ${warn}
      <div class="hrows">${rows}</div>
      <div class="row" style="margin-top:8px">
        <button class="mini hadd">+ 加一行</button>
        <button class="mini hreset"${edited ? '' : ' disabled'}>恢复探测值</button>
        <span class="hint hmsg"></span>
      </div>
      <div class="hint" style="margin-top:6px">留空的行提交时丢弃。头名大小写不敏感，
        但**值**的形态必须精确 —— 站方按值匹配。</div>
    </div>
  </details>`;
}

function hdrRow(k, v, i) {
  return `<div class="hrow">
    <input type="text" class="hk" value="${esc(k)}" placeholder="header 名"
      aria-label="第 ${i + 1} 个 header 名">
    <input type="text" class="hv" value="${esc(v)}" placeholder="值"
      aria-label="第 ${i + 1} 个 header 值">
    <button class="mini hdel" title="删掉这一行">×</button>
  </div>`;
}

// 已知的头名。用于「拼错了」的提示 —— 只警告不阻止：这张表不可能穷尽
// 所有站方要的头，挡住合法冷门头比放过一个手滑更糟。
const KNOWN_HEADERS = [
  'user-agent', 'anthropic-beta', 'anthropic-version', 'x-app',
  'anthropic-dangerous-direct-browser-access', 'originator',
  'x-stainless-lang', 'x-stainless-runtime', 'x-stainless-retry-count',
  'x-stainless-timeout', 'x-stainless-runtime-version',
  'x-stainless-package-version', 'x-stainless-os', 'x-stainless-arch',
  'x-claude-code-session-id', 'x-goog-api-client', 'accept', 'accept-encoding',
  'authorization', 'x-api-key', 'content-type',
];

function bindHeaderEditor(wb, host, sec) {
  const det = wb.querySelector('.hedit');
  if (!det) return;
  const rowsBox = det.querySelector('.hrows');
  const msg = det.querySelector('.hmsg');

  const collect = () => {
    const out = {};
    [].slice.call(rowsBox.querySelectorAll('.hrow')).forEach((r) => {
      const k = r.querySelector('.hk').value.trim();
      const v = r.querySelector('.hv').value.trim();
      // 空 key 或空 value 一律丢弃 —— 与 CPAMP 的 buildHeaderObject 同口径，
      // 两边行为不同会让人在一处试通、另一处失败时找不到原因。
      if (k && v) out[k] = v;
    });
    return out;
  };

  const check = (h) => {
    const bad = Object.keys(h).filter(
      (k) => !KNOWN_HEADERS.includes(k.toLowerCase()));
    const under = Object.keys(h).filter((k) => k.includes('_'));
    const bits = [];
    if (under.length) {
      bits.push(`${under.join('、')} 含下划线 —— HTTP 头一般用连字符，`
        + `确认不是 anthropic_beta 这类手滑`);
    }
    if (bad.length) bits.push(`未见过的头名：${bad.join('、')}`);
    msg.textContent = bits.length ? `⚠ ${bits.join('；')}` : '';
    msg.style.color = bits.length ? 'var(--warn)' : '';
  };

  // 写进 S.overrides 但**不**立刻 refreshPlan —— 那会重渲染整个 .wbox，
  // 把正在输入的框连焦点带光标一起换掉。边打字边跳焦点是不能用的。
  const stash = () => {
    const h = collect();
    check(h);
    S.overrides[host] = S.overrides[host] || {};
    S.overrides[host][sec] = S.overrides[host][sec] || {};
    S.overrides[host][sec].headers = h;
    det.querySelector('.hreset').disabled = false;
  };

  let timer = null;
  det.addEventListener('input', (e) => {
    if (!e.target.classList.contains('hk')
        && !e.target.classList.contains('hv')) return;
    stash();
    // 停手 700ms 才重算方案。数字是权衡：太短仍会在连续输入中打断，
    // 太长会让「改了头之后 priority 建议随之变化」这件事显得没反应。
    clearTimeout(timer);
    timer = setTimeout(() => { S.keepOpen = pk(host, sec); refreshPlan(); }, 700);
  });
  // 失焦立即结算 —— 用户已经改完了，不该再等那 700ms
  det.addEventListener('focusout', () => {
    if (!timer) return;
    clearTimeout(timer); timer = null;
    S.keepOpen = pk(host, sec);
    refreshPlan();
  });
  det.addEventListener('click', (e) => {
    const add = e.target.closest('.hadd');
    const del = e.target.closest('.hdel');
    const rst = e.target.closest('.hreset');
    if (add) {
      e.preventDefault();
      rowsBox.insertAdjacentHTML('beforeend',
        hdrRow('', '', rowsBox.children.length));
      rowsBox.lastElementChild.querySelector('.hk').focus();
      return;
    }
    if (del) {
      e.preventDefault();
      del.closest('.hrow').remove();
      // 这里原本写的是 commit() —— 那个函数不存在（闭包里只有 collect /
      // check / stash），于是抛 ReferenceError：行从 DOM 上消失了，
      // 但 S.overrides 里还留着被删的那个头，看起来删掉了实际没有。
      stash();
      S.keepOpen = pk(host, sec);
      refreshPlan();
      return;
    }
    if (rst) {
      e.preventDefault();
      // 删掉这个键而不是置空 —— 「没改过」与「改成空」是两件事，
      // 后者应当真的写出一个空 headers。
      if (S.overrides[host] && S.overrides[host][sec]) {
        delete S.overrides[host][sec].headers;
      }
      refreshPlan();
    }
  });
}

// 逐模型影响面。抢顶层与挡下层是两件事，都要能看见。
function impactTable(sp) {
  const imps = sp.impacts || [];
  if (!imps.length) return '';
  const rows = imps.map((i) => {
    const hosts = i.shadowed_hosts || [];
    let verdict, cls;
    if (i.hijacks) { verdict = `抢走顶层（原 ${i.current_top}）`; cls = 'p-b'; }
    else if (i.shares) { verdict = `与顶层同层（${i.current_top}）`; cls = 'p-w'; }
    else { verdict = `低于顶层 ${i.current_top}`; cls = 'p-ok'; }
    return `<tr>
      <td class="m">${esc(i.model)}</td>
      <td><span class="pill ${cls}">${esc(verdict)}</span></td>
      <td class="hint">${hosts.length
        ? `挡住 ${hosts.length} 站：${esc(hosts.slice(0, 6).join(' '))}${hosts.length > 6 ? ' …' : ''}`
        : '不挡任何站'}</td>
    </tr>`;
  }).join('');
  return `<div class="tw" style="margin-top:9px"><table>
    <thead><tr><th>模型</th><th style="width:190px">相对现有顶层</th>
      <th>被挡在其后</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

$('#btnback').onclick = () => {
  $('#p2').hidden = true; $('#p3').hidden = true; $('#p1').hidden = false;
  step(1);
};

$('#btnplan').onclick = async () => {
  const d = await refreshPlan(false);
  if (!d) return;

  const nLines = d.diffs.reduce((a, x) => a + x.lines.length, 0);
  const skipped = d.plans.flatMap((p) =>
    Object.entries(p.skipped).map(([s, why]) =>
      `${p.host} · ${SECTION_LABEL[s] || s}：${why}`));

  $('#planmeta').innerHTML = `
    <div class="stat">
      <span>插入 <b>${d.diffs.length}</b> 处</span>
      <span>新增 <b>${nLines}</b> 行</span>
      <span>${fmt(d.lines_before)} → <b>${fmt(d.lines_after)}</b> 行</span>
    </div>
    <div class="note ${d.valid ? 'g' : 'b'}">${esc(d.validate_msg)}</div>
    ${skipped.length ? `<div class="note">不写入 ${skipped.length} 项：
      <div class="mlist">${skipped.map(esc).join('<br>')}</div></div>` : ''}`;

  $('#diffs').innerHTML = d.diffs.length ? d.diffs.map((x, i) => `
    <div class="diff">
      <div class="dh"><span class="pill p-ok">+${x.lines.length}</span>
        <span class="m">${esc(x.section)}</span>
        <span class="hint">← ${esc(x.host)} · 第 ${x.insert_at} 行后</span>
        <button class="cp" data-i="${i}">复制</button></div>
      <pre>${x.lines.map((l) => `<span class="a">+ ${esc(l)}</span>`).join('')}</pre>
    </div>`).join('')
    : `<div class="note w">无可写入条目 —— 没勾选，或全部不可用 / 已存在</div>`;

  $$('#diffs .cp').forEach((b) => {
    b.onclick = () => {
      navigator.clipboard.writeText(d.diffs[+b.dataset.i].lines.join('\n')).then(() => {
        b.textContent = '已复制'; b.classList.add('done');
        setTimeout(() => { b.textContent = '复制'; b.classList.remove('done'); }, 1400);
      });
    };
  });

  $('#btnapply').disabled = !d.valid || !d.diffs.length;
  $('#p3').hidden = true; $('#p4').hidden = false;
  step(4);
  $('#p4').scrollIntoView({ behavior: 'smooth', block: 'start' });
};

$('#btnreplan').onclick = () => {
  $('#p4').hidden = true; $('#p3').hidden = false; step(3);
};

// ── 写回 ──
$('#btnapply').onclick = async () => {
  const btn = $('#btnapply');
  btn.disabled = true;
  $('#applymsg').innerHTML = '<span class="spin"></span> 写回中…';
  // 写盘之后要让 CPA 立即生效。_cred 总是带上：用户若是用 CPA 管理密码
  // 登录的（默认路径），服务端直接复用它去 PUT，无需在这里再输一遍。
  const body = { plan_id: S.planId, confirm: true, _cred: S.token };
  body.push = {
    mgmt_key: $('#o_mgmt').value,          // 留空则服务端复用 _cred
    client_key: $('#o_client').value.trim(),
  };
  // 地址**只在用户显式填了才传**。不填就不带这个键，让服务端用它自己配的
  // CPA_UPSTREAM_URL（容器内 http://cli-proxy-api:8317）。
  //
  // 为什么这么小心：这个输入框曾硬编码 https://cpa.example.com，
  // 于是 PUT 走公网被 Cloudflare 拦成 403 error code 1010 —— 那是 CF 的码，
  // 不是 CPA 拒绝了配置。看着像「写回失败」，其实是根本没打到 CPA。
  const baseIn = $('#o_base').value.trim();
  if (baseIn) body.push.base = baseIn;
  let d;
  try { d = await api('/api/apply', { method: 'POST', body }); }
  catch (e) {
    $('#applymsg').innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
    btn.disabled = false;
    return;
  }
  $('#applymsg').textContent = '';

  let verifyHtml = '';
  if (Array.isArray(d.verified) && d.verified.length) {
    const bad = d.verify_failed || [];
    const rows = d.verified.map((v) => `<tr>
      <td class="m">${esc(v.host)}</td>
      <td class="m">${esc(SECTION_LABEL[v.section] || v.section)}</td>
      <td class="m">${esc(v.model)}</td>
      <td><span class="pill ${v.ok ? 'p-ok' : 'p-b'}">${v.ok ? '通' : '失败'}</span>
        <div class="hint">${esc(v.msg)}</div></td>
    </tr>`).join('');
    verifyHtml = `
      <div class="note ${bad.length ? 'b' : 'g'}">
        <b>端到端验证：${d.verified.length - bad.length}/${d.verified.length} 通过</b>
        —— 用 CPA 的客户端入口真打了一次业务请求，这才叫「能出活」
        ${d.verify_key_src ? `<br><span class="hint">客户端 Key ${esc(d.verify_key_src)}
          —— 只在服务端使用，不会出现在页面或响应里</span>` : ''}
        ${bad.length ? `<br><b>失败项已写入 config.yaml。</b>
          这些站直连可能是好的，经 CPA 却不行 —— 常见是 CPA 加了自己的头、
          走了自己的 translator，上游据此换了后端模型。要么找站方处理，
          要么用上面那份备份回滚。` : ''}
      </div>
      <div class="tw"><table>
        <thead><tr><th>站点</th><th style="width:96px">段</th>
          <th style="width:170px">模型</th><th>结果</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
  } else if (d.verify_skipped) {
    verifyHtml = `<div class="note w">
      ${esc(d.verify_skipped).split('\n').join('<br>')}</div>`;
  }
  // 超出单次验证上限的条目 —— 它们**已经写入 config.yaml**，只是没验。
  // 不显示的话用户会以为「N/N 通过」覆盖了全部，而其实有一批没测过。
  if (d.verify_over_limit) {
    verifyHtml += `<div class="note w">${esc(d.verify_over_limit)}</div>`;
  }

  // 重载结果单独一条 —— 它决定「CPA 现在到底认不认这份配置」
  let reloadHtml = '';
  if (d.reload_ok) {
    reloadHtml = `<div class="note g">CPA 已重载：${esc(d.reload_msg || '')}<br>
      <span class="hint">CPAMP 面板另有 30 秒前端缓存，稍等再硬刷新即可看到新条目</span></div>`;
  } else if (d.reload_msg) {
    reloadHtml = `<div class="note b"><b>CPA 尚未重载</b><br>
      ${esc(d.reload_msg).split('\n').join('<br>')}<br>
      <span class="hint">磁盘已改，但不确定 CPA 用上了没有。它靠 inotify 发现
      改动，而 inotify 事件可能丢且**没有轮询兜底** —— 丢了就不会自愈。
      最直接的确认办法：在 VPS 上 <code>docker restart cli-proxy-api</code>。</span></div>`;
  }

  $('#donebody').innerHTML = reloadHtml + `
    <div class="note g">已写入 <code>${esc(d.written)}</code> · ${esc(d.diffs)} 处插入<br>
      ${esc(d.validate_msg)}</div>
    <div class="note">备份 <code>${esc(d.backup)}</code><br>
      <span class="hint">出问题就用它覆盖回去。CPA 的 PUT 落盘非原子且失败不回滚，
      备份是唯一保险。</span></div>
    ${d.push_ok === undefined ? '' :
      `<div class="note ${d.push_ok ? 'g' : 'b'}">推送 CPA：${esc(d.push_msg)}</div>`}
    ${verifyHtml}`;
  $('#p4').hidden = true; $('#pdone').hidden = false;
  try { S.ctx = await api('/api/context'); renderBands(); } catch { /* 非致命 */ }
};

$('#btnrestart').onclick = () => {
  S.jobId = null; S.planId = null; S.plans = null;
  S.overrides = {}; S.forced = {}; S.cursor = 0; S.picks = null;
  S.reuseSaved = 0; S.reuseSeen = null;
  $('#input').value = '';
  ['#pdone', '#p4', '#p3', '#p2', '#pparse'].forEach((s) => { $(s).hidden = true; });
  $('#p1').hidden = false;
  $('#btnprobe').disabled = true;
  $('#parsemsg').textContent = '';
  step(1);
  scrollTo({ top: 0, behavior: 'smooth' });
};

boot();
