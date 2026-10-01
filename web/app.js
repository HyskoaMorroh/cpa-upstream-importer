/* 投喂台 —— 前端逻辑。原生 JS，无构建步骤，VPS 上直接跑。
 *
 * 进度回报走 HTTP 轮询（与 CPAMP 现有做法一致），不引入 SSE/WebSocket。
 * token 存 sessionStorage —— 关标签页即失效，不留在 localStorage 里。
 */
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s, root) => [].slice.call((root || document).querySelectorAll(s));
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (n) => (n == null ? '—' : Number(n).toLocaleString('en-US'));

// ── 模型规则（与后端 cpa_probe/model_catalog.py 逐条对齐）──
//
// 用户 2026-09-02 定的四条：
//   codex   只能 gpt 系
//   claude  只能 claude 系
//   gemini  只能 gemini-*-pro，且 * >= 2.5
//   compat  不限段，但必须是 gpt / claude / gemini / kimi 四族之一
// 外加：同系列以最新版为准（旧版不放入）；图像 / 语音 / 嵌入 / oss 一律不收。
//
// 为什么前端也要有一套：后端 section_allows 是权威，但界面要在**勾选前**就
// 把不该勾的滤掉。两边不一致的后果就是现场截图那两个问题 —— codex 段勾上了
// gpt-image-2 / gpt-oss-120b / gpt-oss-20b（都是 gpt 族，旧的段族闸放行），
// gemini 段目录里列出 flash / batch-inference / pro-agent。
// 这里的每条规则都在 tests/test_web.py 里与 Python 侧逐条比对，不许单边改。

// 去掉 provider 前缀。`Business/gemini-2.5-pro` → `gemini-2.5-pro`
function bareName(m) {
  const s = String(m || '').trim().toLowerCase();
  const i = s.lastIndexOf('/');
  return i >= 0 ? s.slice(i + 1) : s;
}

const FAM_RE = {
  gemini: /^gemini/,
  claude: /^claude/,
  kimi: /^kimi/,
  gpt: /^(?:gpt|o\d+(?:[.\-]|$))/,
};

function famOf(m) {
  const n = bareName(m);
  for (const f of ['gemini', 'claude', 'kimi', 'gpt']) {
    if (FAM_RE[f].test(n)) return f;
  }
  return '';
}

// 非对话模型：图像 / 语音 / 嵌入 / 批处理 / 开源小模型。写进 config.yaml
// 不报错，但 CPA 路由过去必然失配 —— 它们走的不是对话协议路径。
const NON_CHAT = /-image(?:$|[-.])|-tts(?:$|[-.])|^imagen|-oss-|-embedding|-whisper|-moderation|-batch-inference/;

// 降级档：名字里带 mini / nano / lite / flash / fast / haiku 的一律不选
// （用户 2026-09-12 定 mini，2026-09-16 补 flash 与 fast，2026-09-27 补 haiku，
// **不分类型、不看版本号**）。与后端 model_catalog.is_low_tier 逐条等价。
//
// 必须按 **token 边界** 匹配：`gemini` 与 `kimi` 的字面里就含 `mini`。
// 裸 includes('mini') 会把整个 gemini 族和 kimi 族全挡掉 —— 静默的灾难
// （gemini 段界面上一个模型都挑不出来）。`fast` 同理要防 `breakfast`。
//
// 为什么这一行漏了 flash/fast 会**真的写坏 config.yaml**（2026-09-16 实测）
// ------------------------------------------------------------------
// 后端 2026-09-16 已补上这两个词，而这里没跟上，于是两侧分叉：
//   claude 段  后端拒 claude-opus-5-fast，这里放行并**默认预勾**
//   compat 段  后端拒 gpt-6-fast / gpt-6-flash / kimi-k3-fast，这里全放行
// 界面预勾之后，操作员一提交就进 `S.forced` —— 那是「手填」通道，
// 后端手填只过 `section_protocol_ok`（协议层），**不查档次**。
// 于是被界面勾上的降级档绕过后端那道闸，真的落进 config.yaml。
// 现场快照里已有 4 处 `anthropic/claude-opus-5-fast` 处于勾选态。
//
// 这就是为什么两侧必须逐字等价，而不是「后端是权威，前端差一点无所谓」。
// 与后端 model_catalog.py 的 `_LOW_TIER` 逐字对齐 —— 两侧不一致的后果不是
// 显示差异：界面勾上的名字进 S.forced，后端把 S.forced 当**手填**（最高
// 权威，只过协议层校验），于是前端多放行一个档次就等于它能写进 config.yaml。
//
// 2026-09-26 的 `low`：**两侧都不进降级档**。用户当天拍板
// `gemini-3.1-pro` 后面的所有后缀（`-high` / `-low` / `-preview*`）都算同一
// 系列、一并保留，所以两侧正则都没有 `low`。（此处原注释曾写「后端已加 low、
// 前端漏了」，与后端 `_LOW_TIER` 实际内容相反 —— 2026-09-27 订正。）
//
// 2026-09-27 补 `haiku`：后端 `_TIER_HINTS` 给 haiku 打 4，比 flash 的 3 更低，
// 即代码自己就认定它更弱，却只硬排除 flash。实测后果有两条：站上只有
// `claude-3-5-haiku` 时它被写进 config.yaml；同世代时它因自成一条产品线而在
// `_round_robin` 里**保证占一个注册位**（挤掉的反而是 `opus-5-thinking`）。
// 两侧同时补，才不会出现「界面勾了后端拒」或反过来。
//
// tests/test_web.py 的「放行集合两侧一致」就是锁这条不变式的。
const LOW_TIER = /(?:^|[^a-z0-9])(?:mini|nano|lite|flash|fast|haiku)(?![a-z0-9])/;

function isLowTier(m) {
  return LOW_TIER.test(bareName(m));
}

// gemini 段：只要 gemini-<版本>-pro*，版本 >= 2.5。`-pro` 后可带
// -high / -low / -preview / -preview-search / -preview-customtools。
const GEMINI_PRO = /^gemini-(\d+(?:\.\d+)?)-pro(?:$|[-.])/;
const GEMINI_MIN = 2.5;

const SECTION_FAMILY = {
  'gemini-api-key': 'gemini',
  'codex-api-key': 'gpt',
  'claude-api-key': 'claude',
};

// codex 段只收 gpt-*，不收 o 系列（2026-09-27 用户在三选一里选定）。
// 与后端 model_catalog.CODEX_GPT_ONLY 逐字对齐 —— 后端 2026-09-27 加了这道
// 闸，前端当时没跟上，于是 `tests/test_web.py` 的「放行集合两侧一致」开始
// 失败：界面把 o1 / o1-pro / o3 / o3-pro 列进 codex 段还预勾，后端
// `section_allows` 一个都不收。界面勾上的名字进 `S.forced`，后端把
// `S.forced` 当手填（只过协议层），于是前端多放行的这一档真会写进
// config.yaml —— 这正是「模型勾选高低模型混乱」的一条实际路径。
//
// 旧判例（2026-09-12）「o3 与 gpt-5.6 互不相干、都要」管的是**世代比较**，
// 与「codex 段收不收 o 系列」是两件事；`newestGenerationPerLine` 不看段，
// 那些判例照旧成立，只是 o 系列的落点从 codex 段变成 compat 段。
const CODEX_GPT_ONLY = true;

// 与后端 `_OPENAI_REASONING_RE`（`^o\d+(?:[.\-]|$)`）同一判据：锚在开头、
// o 后紧跟数字。`omni-3` / `oss-20b` 不命中。
// 与上面的 O_SERIES_RE 分开写是因为那一条要捕获版本数组给世代比较用，
// 这一条只回答「是不是 o 系列」—— 判据同源，用途不同。
const OPENAI_REASONING_RE = /^o\d+(?:[.\-]|$)/;

function isOpenaiReasoning(m) {
  return OPENAI_REASONING_RE.test(bareName(m));
}

// 这个模型名在这个段里**工具要不要挑它**。与后端 section_allows 对齐。
// 用于：目录候选过滤、默认勾选。
function famOk(sec, m) {
  const n = bareName(m);
  if (!n) return false;
  const f = famOf(n);
  if (!f) return false;                     // 四族之外（deepseek / grok / glm…）
  if (NON_CHAT.test(n)) return false;
  // mini / nano / lite 一律不挑（用户 2026-09-12，不分类型、不看版本）。
  // 放在族判之后：LOW_TIER 按 token 边界匹配，gemini / kimi 不受影响。
  if (isLowTier(n)) return false;
  // gemini 族的 pro / >=2.5 闸在**族**上，不在段上（2026-09-12 对齐后端）
  // ------------------------------------------------------------------
  // 后端 section_allows 先判 `fam == "gemini" and not gemini_pro_ok(n)`，
  // 再判段；原来这里只在 `sec === 'gemini-api-key'` 时判，于是 compat 段
  // 放行了 gemini-2.0-pro / gemini-3.5-flash / gemini-pro-agent ——
  // 界面列出来还预勾，后端一个都不收。flash 是降级档、2.0 已停服，
  // compat 段走万能口不代表这三个该挑。
  if (f === 'gemini') {
    const mm = GEMINI_PRO.exec(n);
    if (!mm || parseFloat(mm[1]) < GEMINI_MIN) return false;
  }
  // codex 段只留 gpt 系列（2026-09-27）。放在族判之后、段比对之前 ——
  // 与后端 `section_allows` 里那句 `section == "codex-api-key" and
  // CODEX_GPT_ONLY and _OPENAI_REASONING_RE.match(n)` 同一位置同一判据。
  if (sec === 'codex-api-key' && CODEX_GPT_ONLY && isOpenaiReasoning(n)) return false;
  const want = SECTION_FAMILY[sec];
  return want ? f === want : true;          // compat：四族都行
}

// 这个模型在这个段上**协议层**成不成立。与后端 section_protocol_ok 对齐。
// 用于：手填框的校验提示 —— 那是操作员的显式指定，只挡协议层不可能成立的。
//
// 与 famOk 的唯一差别是四族之外：compat 段走 /chat/completions、CPA 对模型名
// 零校验，实测 romeo 唯一验证过的模型就是 grok-4.6。按族拒掉手填等于让
// 操作员没法把已知可用的模型写回去（2026-09-03）。
function protoOk(sec, m) {
  const n = bareName(m);
  if (!n) return false;
  if (NON_CHAT.test(n)) return false;
  if (sec === 'gemini-api-key') {
    const mm = GEMINI_PRO.exec(n);
    return !!mm && parseFloat(mm[1]) >= GEMINI_MIN;
  }
  const want = SECTION_FAMILY[sec];
  return want ? famOf(n) === want : true;   // compat：不限族
}

// 这个模型该不该默认勾上。
//
// 规则收紧后「能用」与「该勾」基本重合 —— 段规则本身已经排除了降级档
// （gemini 只留 pro）与非对话模型。剩下唯一要额外挡的是**同系列旧版**：
// 目录里同时有 gpt-5.5 与 gpt-5.6 时，只该默认勾 5.6。
// 那件事需要看整份清单才能判，所以由 pickDefaults 处理，不在这里。
function defOn(sec, m) {
  return famOk(sec, m);
}

// 版本 token。`k?` 是 kimi 的 k2/k3（属于系列名），`o?` 是 OpenAI 的 4o
// 代号后缀 —— 与 Python 侧 _VERSION_RE 同一个模式。
const VERSION_RE = /(?:^|[^A-Za-z0-9.])(k?)(\d+(?:[.\-]\d+)*)(o?)(?![A-Za-z0-9])/;

// OpenAI 推理系列的世代：o1 / o3 / o4-mini 里紧贴开头 o 的数字。
// 与 Python 侧 _O_SERIES_RE 同一套。
//
// 为什么单独一条（2026-09-04 现场截图：codex 段同时勾着 o1 与 o3）：
// VERSION_RE 要求版本数字前不紧贴字母，而这一族的数字紧贴开头的 o，于是
// 七个名字全部「认不出版本」，newestGenerationPerLine 的「整组认不出就全留」
// 兜底把 o1 与 o3 一起勾上 —— 与 gpt-4o 那次同一个形态，换了一族。
// 锚在开头 + 紧跟数字，`omni-3` / `oss-20b` 不受影响。
const O_SERIES_RE = /^o(\d+(?:[.\-]\d+)*)(?![A-Za-z0-9])/;

// 纯 8 位日期戳后缀（`-20251001`）。与 Python 侧 `_DATE_STAMP` 逐字对齐。
// 2026-09-27：前端缺这一步，`claude-opus-5` 与 `claude-opus-5-20251001` 被算成
// (5) 与 (5,20251001) 两个世代，界面只预勾带戳的那个 —— 与后端写回的不一致。
// 只剥第一段戳，戳后面的后缀原样接上；八位以外的数字段（32k、k2）不碰。
const DATE_STAMP_RE = /^(.*?)-\d{8}(?=$|[-.])/;

// 拆成 [系列, 版本数组]。认不出版本时版本为 null。
function seriesAndVersion(m) {
  const n = bareName(m).replace(DATE_STAMP_RE, '$1');
  const mo = O_SERIES_RE.exec(n);
  if (mo) {
    const nums = mo[1].split(/[.\-]/).map((x) => parseInt(x, 10));
    if (nums.some((x) => Number.isNaN(x))) return [n, null];
    return [`o*${n.slice(mo[0].length)}`, nums];
  }
  const mm = VERSION_RE.exec(n);
  if (!mm) return [n, null];
  // exec 的 index 指向前置分隔符，真正的版本从 mm[1] 起算
  const start = mm.index + mm[0].length - mm[1].length - mm[2].length - mm[3].length;
  const series = n.slice(0, start) + mm[1] + '*' + n.slice(mm.index + mm[0].length);
  const nums = mm[2].split(/[.\-]/).map((x) => parseInt(x, 10));
  if (nums.some((x) => Number.isNaN(x))) return [n, null];
  return [series, nums];
}

// 版本数组 → 可比较的世代 [主, 次]。null 表示无从比较。
// 只取前两位：`claude-haiku-4-5-20251001` 的日期戳不该让它比
// `claude-haiku-4-5` 更新（同一款）。缺位补 0，于是 5 < 5.1。
// 与 Python 侧 generation 同一套。
function generationOf(m) {
  const [, ver] = seriesAndVersion(m);
  if (!ver || !ver.length) return null;
  return [ver[0], ver.length > 1 ? ver[1] : 0];
}

// 比较世代时的分组维度。比 famOf 多分出一个 `o` 族。
// 与后端 model_catalog.generation_family 逐条等价。
//
// famOf('o3') 是 'gpt' —— 那对「这个段收不收它」是对的（o 系列走 codex
// 段），但对「谁比谁新」是错的：o 系列与 gpt 系列是互不相干的编号体系，
// o3 的 3 不代表它比 gpt-5.6 老一代（用户 2026-09-12 判例一）。
// 四族之外的名字按自己的词根分组，不能一起丢进 '' 桶：famOf('grok-4.6')
// 与 famOf('glm-5.2') 都是 ''，同桶就变成「glm 的 5 比 grok 的 4 新」，
// 而 grok-4.6 是 compat 段唯一端到端验证过的模型。
function generationFamily(m) {
  const n = bareName(m);
  if (O_SERIES_RE.test(n)) return 'o';
  return famOf(n) || n.split('-')[0];
}

function genGreater(a, b) {
  if (!a) return false;
  if (!b) return true;
  if (a[0] !== b[0]) return a[0] > b[0];
  return a[1] > b[1];
}

function genEqual(a, b) {
  if (!a || !b) return a === b;
  return a[0] === b[0] && a[1] === b[1];
}

// 产品线 = 版本号**之前**那一截。结构判据，不含任何后缀清单。
// 与 Python 侧 _product_line 同一套（2026-09-12 一起改的）。
//
// 原来这里有一张手写的后缀白名单（LINE_STRIP：sol / luna / terra /
// preview / 32k …）。它漏一个就错一次，而漏是常态 —— 实测 `gpt-6-astra`
// 因为 astra 不在表里就自成一条线、躲过「同线取最高世代」被与 gpt-5.6
// 一起勾上。那也正是 docx 第 6 条禁止的硬编码：模型名录跟着 CPA / CPAMP
// 更新，判据不能每次都要改本项目的代码。
//
// seriesAndVersion 已经把名字拆成「模板 + 版本」，模板里 `*` 之前那一截
// 就是版本号之前的固定前缀 —— 天然的产品线，不必知道后面是什么后缀：
//   gpt-6-astra      → 模板 gpt-*-astra      → 线 gpt
//   claude-fable-5-1 → 模板 claude-fable-*   → 线 claude-fable
//   o3-pro           → 模板 o*-pro           → 线 o
function productLine(m) {
  const n = bareName(m);
  const [series, ver] = seriesAndVersion(n);
  if (!ver || series.indexOf('*') < 0) return n;
  return series.split('*')[0].replace(/[-.]+$/, '') || n;
}

// 每条产品线只留**最高世代**，且低主版本的产品线整条出局。
// 与 Python 侧 newest_generation_per_line 同一套判据（2026-09-12 起两阶段）。
//
// 为什么不是「同系列取最新」（2026-09-02 现场截图）：按系列分组时
// gpt-5.5 的系列是 `gpt-*`，而 luna / terra 各自是 `gpt-*-luna` /
// `gpt-*-terra` —— 三个独立系列，5.5 没有对手所以留下；gpt-4o 则因为
// 旧正则不认 `4o` 是版本而自成一系。两件事叠加就是截图里 codex 段
// 勾着 gpt-4o 与 gpt-5.5 的原因。
function newestGenerationPerLine(names, keepLowTier) {
  // 两阶段，与后端 model_catalog.newest_generation_per_line 逐条等价
  // （tests/test_web.py 拿同一批名字喂两边比对，单边改会被立刻抓到）：
  //
  //   阶段 A 按**族**（generationFamily）比**主版本** —— docx 第 4 条的
  //     「codex 当前最高为 gpt-6 系列所有模型名称」：gpt-6 出现时
  //     gpt-5.6 那一代全走，不管挂在哪条产品线上。
  //   阶段 B 同族内再按**产品线**比**完整世代**，该世代的变体全保留 ——
  //     「所有相同等级系列的模型全部都要勾选上」，也就是用户点名的
  //     「勾了 gpt-5.6 却没勾 gpt-5.6-sol」的反面。
  //
  // 单独任何一阶段都不行：只按族比完整世代会让 claude-fable-5-1 的 (5,1)
  // 挤掉同档的 opus/sonnet (5,0)；只按产品线比则 gpt-5.6-codex 与 gpt-6
  // 是两条线，5.6-codex 会留下。
  //
  // 阶段 A 用 generationFamily 而不是 famOf：o 系列自成一族，于是
  // gpt-5.6 不会挤掉 o3（用户 2026-09-12 判例一）。
  //
  // 无版本号的名字在进入比较前就剔除（用户口径：没有版本号 = 低等级）。
  // 现场是 `gpt-reserve` 和 `gpt-6` / `gpt-6-astra` 一起被勾上，而该型号
  // 并不存在。
  const cand = [];
  (names || []).forEach((n) => {
    if (!n) return;
    // gemini 族只比 pro 型号，与后端同一道前置闸
    if (famOf(bareName(n)) === 'gemini') {
      const mm = GEMINI_PRO.exec(bareName(n));
      if (!mm || parseFloat(mm[1]) < GEMINI_MIN) return;
    }
    // 降级档不参与（也就不会被选中）。keepLowTier 只给手填路径用。
    if (!keepLowTier && isLowTier(n)) return;
    const g = generationOf(n);
    if (!g) return;
    cand.push([n, g]);
  });

  // 阶段 A：族内比主版本
  const topMajor = new Map();
  cand.forEach(([n, g]) => {
    const f = generationFamily(n);
    if (!topMajor.has(f) || g[0] > topMajor.get(f)) topMajor.set(f, g[0]);
  });
  const survived = cand.filter(([n, g]) => g[0] === topMajor.get(generationFamily(n)));

  // 阶段 B：产品线内比完整世代
  const topGen = new Map();
  survived.forEach(([n, g]) => {
    const ln = productLine(n);
    if (!topGen.has(ln) || genGreater(g, topGen.get(ln))) topGen.set(ln, g);
  });
  const keep = new Set(
    survived.filter(([n, g]) => genEqual(g, topGen.get(productLine(n))))
            .map(([n]) => n));

  // 按输入顺序输出，保证同一批输入两次运行结果一致（diff 可复核）
  const seen = new Set();
  return (names || []).filter((n) => {
    if (!keep.has(n) || seen.has(n)) return false;
    seen.add(n);
    return true;
  });
}

// 该段默认勾选哪些：先过段规则，再每条产品线取最高世代。
//
// 目录整体落后市面最新一个世代以上时**一个都不勾**（2026-09-02 现场）：
// 某站 codex 目录只有 gpt-4 / gpt-4-32k / gpt-4o / gpt-4o-mini，四个都是
// 世代 (4,0)，「取最高世代」把四个全留下并默认全勾 —— 而用户要的是
// 「最新是 gpt-5.6 时 gpt-4o 不该默认勾选」。
//
// 为什么不换成市面最新清单：那个站的目录里没有 5.6 系的名字，写进去
// CPA 路由不到，把「有老模型可用」变成死条目。所以只降级预勾，清单照旧
// 列出，确知可用的人仍可手工勾。与后端 catalog_is_stale 同一套判据。
//
// 判「落后」必须**逐产品线**比（2026-09-04，与后端同步改）：o 系列与 gpt
// 系列是互不相干的编号体系，`o3` 的 3 不代表它比 `gpt-5.6` 老一代。按全局
// 最高世代比会把「目录里只有 o 系列」的站误判成落后、一个都不预勾。
// 只对两侧都有的产品线比较，全部落后才算落后；没有可比的线就不判。
function pickDefaults(sec, catalog) {
  return staleCheck(sec, catalog).keep;
}

// pickDefaults 的完整结论：预勾清单 + 判落后时**具体是哪条线落后到几**。
// 界面那句「整份目录都落后于市面最新（本段最新已到 X）」要的是后者 ——
// 逐线比之后「本段最新」不再是一个全局数字（o 线 4.0、gpt 线 5.6 并存），
// 拿全局值去填那句话会显示一个与判据无关的数（2026-09-04 自查）。
function staleCheck(sec, catalog) {
  const fit = (catalog || []).filter((m) => defOn(sec, m));
  const keep = newestGenerationPerLine(fit);
  const lines = (S.ctx && S.ctx.market_top_gen_lines
                 && S.ctx.market_top_gen_lines[sec]) || null;
  if (lines && keep.length) {
    // 目录侧也按线取最高世代
    const catTop = {};
    keep.forEach((m) => {
      const g = generationOf(m);
      if (!g) return;
      const ln = productLine(m);
      if (!catTop[ln] || genGreater(g, catTop[ln])) catTop[ln] = g;
    });
    const shared = Object.keys(catTop).filter((ln) => lines[ln]);
    if (shared.length) {
      const behind = shared.filter((ln) => genGreater(lines[ln], catTop[ln]));
      if (behind.length === shared.length) {
        // 报最能说明问题的那条线：市面世代最高的
        let worst = behind[0];
        behind.forEach((ln) => {
          if (genGreater(lines[ln], lines[worst])) worst = ln;
        });
        // **整份目录落后时仍然预勾**（2026-09-17，用户第 ⑵④ 条）
        // ----------------------------------------------------
        // 原来这里返回 `keep: []` —— 一个都不勾，界面提示「默认不勾，
        // 确知该站只卖这些且够用，手工勾上即可」。
        //
        // 现场后果（用户截图）：codex 段目录只有 gpt-5.1、市面已到 gpt-6，
        // 于是整格空白等人工点。173 站就是几百次点击。
        //
        // 用户规则 ④ 要的正相反：**最新代按目录最高级填充勾选，
        // 同时保留实测通过的次新代**。后端 `topup_to_market_top` 已经
        // 按这条算好了（catalog=[gpt-5.1] → ['gpt-6-astra']；
        // proven=[gpt-5.1] → ['gpt-5.1','gpt-6-astra']），
        // 前端只要照勾即可，不该再自作主张清空。
        //
        // `line/cat/mkt` 仍然返回 —— 界面照常提示「这份目录落后于市面」，
        // 让操作员知道依据强度，但不再替他做「不勾」的决定。
        return { keep, line: worst,
                 cat: catTop[worst].join('.'), mkt: lines[worst].join('.') };
      }
    }
  }
  return { keep };
}

/* 就地草稿 → ops。放在 `SECTION_LABEL` **之前**是有意的：`tests/test_web.py`
   只取 app.js 到此为止的那一段丢进 node 跑（后面碰 DOM，node 里会崩），
   所以纯逻辑必须住在这一侧才进得了测试。 */
function bmDraftOps(drafts, groups) {
  const out = [];
  drafts.forEach((d) => {
    const g = groups.find((x) => x.section === d.section && x.host === d.host);
    if (!g) return;
    if (typeof d.priority === 'number') {
      g.entries.forEach((e) => {
        if (e.priority !== d.priority) {
          out.push({ section: g.section, index: e.index, fingerprint: e.fingerprint,
                     action: 'priority', value: d.priority });
        }
      });
    }
    if (typeof d.enabled === 'boolean') {
      g.entries.forEach((e) => {
        if (e.enabled !== d.enabled) {
          out.push({ section: g.section, index: e.index, fingerprint: e.fingerprint,
                     action: d.enabled ? 'enable' : 'disable' });
        }
      });
    }
  });
  return out;
}

/* 反代/CF 的 HTML 错误页不许原样进错误消息。
 *
 * 2026-09-13 现场（投喂台 mhtml 快照）：③ 判定与定档 顶上那条红框里是
 * **一整页 `<style>/*! normalize.css …</style>` 的 HTML**，用户看到的是
 * 一屏 CSS 与 `[endif]-->` 注释，真实信息（"524: A timeout occurred"）埋在
 * 第 300 个字符里。成因是 `api()` 的兜底 `JSON.parse(txt)` 失败后把
 * `txt.slice(0, 400)` 当 error 丢出来 —— 而 CF 的 524 页正文就是 HTML。
 *
 * 放在 `SECTION_LABEL` 之前是有意的：`tests/test_web.py` 只把 app.js 到此
 * 为止的那段丢进 node 跑（后面碰 DOM，node 里会崩），纯逻辑住这一侧才进得了
 * 测试。这条判据值得测 —— 它的失效表现是「用户看到一屏 CSS」。
 */
function _proxyErrText(txt, status) {
  const looksHtml = /^\s*(<!DOCTYPE|<html|<!--)/i.test(txt)
    || /<html[\s>]/i.test(txt.slice(0, 2000));
  if (!looksHtml) return txt.slice(0, 400);
  const title = (txt.match(/<title[^>]*>([^<]{0,120})<\/title>/i) || [])[1];
  const known = {
    524: 'Cloudflare 524：源站在超时窗口内没有回应。定档计算比 CF 的 100 秒'
       + '上限慢，请求已被反代切断（nginx 侧是 600 秒，所以断在 CF 那一层）',
    522: 'Cloudflare 522：连接源站超时，容器可能未启动或端口不通',
    502: '502：反代连不上容器，检查容器是否在跑、8765 是否监听',
    504: '504：反代等后端超时',
  };
  const head = known[status]
    || `反代返回 ${status} 的 HTML 错误页（不是本项目的 JSON 响应）`;
  return head + (title ? `；上游标题「${title.trim()}」` : '');
}

const SECTION_LABEL = {
  'gemini-api-key': 'gemini',
  'codex-api-key': 'codex',
  'claude-api-key': 'claude',
  'openai-compatibility': 'compat',
};

// 段序兜底（2026-09-19）
// --------------------
// 页面里多处裸取 `S.ctx.section_order`，而 `S.ctx` 由启动时那次
// `/api/context` 赋值。那个请求一旦失败（网络抖动、反代超时、服务刚重启），
// `S.ctx` 保持 null，随后**任何**渲染都抛
// `TypeError: Cannot read properties of null (reading 'section_order')`
// 并中断整条流程 —— 用户看到的是「点了没反应 / 黑屏 / 参数全缺失」，
// 而控制台里只有一条抛在深处的 TypeError。
//
// 后端 `SECTIONS` 是常量（cp.SECTIONS），顺序固定，这里据实抄一份即可。
// 用它兜底之后，拿不到 ctx 时页面降级成「段名列不全但能操作」，
// 而不是整页崩掉。真正的失败原因由 boot 里的显式提示说出来。
const SECTION_ORDER = ['gemini-api-key', 'codex-api-key',
                       'claude-api-key', 'openai-compatibility'];
function sectionOrder() {
  return (S.ctx && Array.isArray(S.ctx.section_order) && S.ctx.section_order.length)
    ? S.ctx.section_order : SECTION_ORDER;
}

// 判定类别 → 徽标样式。与后端 classify 的类别名一一对应。
const CAT_PILL = {
  '可用': 'p-ok', '余额': 'p-w', '限流': 'p-w', '边缘': 'p-w', '反测活': 'p-w',
  '临时': 'p-w', '限频': 'p-w', '门禁': 'p-b', 'IP封': 'p-b', '死路': 'p-b',
  '鉴权': 'p-b', '注入': 'p-i', '未知': 'p-m',
};

// 模型清单的来源 → 可信度标记。四者差一截，界面不能显示成一样：
//   probed  实测发过请求跑通
//   catalog 站方 /models 目录声明（真实转发可能仍失败）
//   manual  操作员手填
//   seed    本工具的种子猜测兜底（最不可信，但严禁 priority 未定，所以宁可给）
const SRC_TAG = {
  probed: { t: '实测', c: 'p-ok' },
  catalog: { t: '目录', c: 'p-w' },
  manual: { t: '手填', c: 'p-b' },
  seed: { t: '猜测', c: 'p-m' },
};

// 段专属能力开关这一格。三态各自一种措辞 —— 「实测不支持」与「未探测」
// 写回时都不写那个字段，但一个是结论、一个是缺口，显示成一个样子就是
// 「未验证当已验证」的镜像错误（本项目反复修的那一类）。
//
// 哪个段有哪个开关取自 CPA 的结构体：codex 有 websockets
// （config_types.go:486），compat 有 support-prompt-cache-key（:685）；
// gemini / claude 段没有上游能力类开关，显示「—」。
const SECTION_TOGGLES = {
  'codex-api-key': [['websockets', 'websockets', 'websockets_note']],
  'openai-compatibility': [['support-prompt-cache-key',
                            'prompt_cache_key', 'prompt_cache_note']],
};

function toggleCell(sec, sp) {
  const specs = SECTION_TOGGLES[sec];
  if (!specs) return '<span class="hint">本段无此类开关</span>';
  return specs.map(([label, field, noteField]) => {
    const val = sp[field];
    const note = sp[noteField] || '';
    const prior = (sp.prior_toggles || {})[label];
    if (val === true) {
      return `<div><span class="pill p-ok">${esc(label)}: true</span>`
        + (note ? `<div class="hint">${esc(note)}</div>` : '') + '</div>';
    }
    if (val === false) {
      return `<div><span class="pill p-m">不开 ${esc(label)}</span>`
        + `<div class="hint">${esc(note || '实测不支持')}</div></div>`;
    }
    // 未探测：分「原值有」与「原值也没有」两种说法 —— 前者会照原值写回，
    // 后者是真的什么都不写。
    if (prior) {
      return `<div><span class="pill p-w">${esc(label)}: true</span>`
        + '<div class="hint">本次未探测此开关，按原值搬运</div></div>';
    }
    return `<div><span class="pill p-m">未探测</span>`
      + `<div class="hint">${esc(note || '本次没有探这个开关 —— '
        + '不写该字段，CPA 按关闭处理')}</div></div>`;
  }).join('');
}

// 结果表「处置」格里的能力开关徽标。比 toggleCell 短 —— 那一列窄，
// 只给结论不给完整说明（完整说明在诊断页的「能力开关」列与写回 diff 的行尾注释）。
//
// 未探测时**什么都不显示**：结果表每段一行、79 个站就是 316 行，
// 给每一行都挂一个「未探测 websockets」会把真正的信息淹掉。
// 「实测不支持」也不显示 —— 那是常态（中转站多数不支持 WS）。
// 只显示确认支持的那一个，它是「这个站比别的站多一项能力」的信息。
function capBadge(sec, v) {
  const specs = SECTION_TOGGLES[sec];
  if (!specs) return '';
  return specs.map(([label, field]) => (v[field] === true
    ? `<div><span class="pill p-ok">支持 ${esc(label)}</span></div>` : ''))
    .join('');
}

const THEMES = ['midnight', 'parchment', 'neon'];

const S = {
  token: '',
  ctx: null,
  jobId: null,
  cursor: 0,
  timer: null,
  results: null,
  planId: null,
  planInputKey: '',
  previewPlanId: '',
  previewInputKey: '',
  plans: null,
  overrides: {},        // {host: {section: {...}}}
  // 人工接管：{host: {section: [模型, ...]}}。任何段都可以接管 ——
  // 判死段（很多中转站不给测活：探针短消息被拦、分组限客户端，而真实对话正常）
  // 与可用段（探测只验前几个模型就停，站方实际卖得更多）都走这一份。
  forced: {},
  picks: null,          // Set("host\u0000section")，null = 尚未初始化
  reuseSaved: 0,        // 形态复用省下的请求数
  reuseSeen: null,      // 已计数过的 shape-reused 事件键（防重拉重复累加）
  diagYaml: null,       // 诊断结果的 YAML 片段 {段: 文本}。不进 HTML 属性
  keepOpen: '',         // 重渲染后要重新展开哪个 headers 编辑器（pk(host,sec)）
  parsedValid: 0,       // 上次解析出的有效行数。决定「开始探测」能不能点
};

// 候选身份键。用**行号**而不是 host —— 一个站常有 15 把 Key
// （实测 gorou 15、tango 14），用 host 做键时 S.picks 这个 Set 会把
// 同站同段的 15 个选择去重成 1 个，DOM 定位也只命中第一行。
// 2026-09-02 现场：「全勾选」显示已勾 26 项，大量段勾不上。
const pk = (rid, sec) => `${rid}\u0000${sec}`;

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
// 超时（2026-09-18）
// ----------------
// 原来 `fetch` 既无 AbortController 也无超时：反代切断或服务 hang 时请求
// 一直悬着，而 `/api/plan` 的提交一旦悬住，单飞锁 `_planInFlight` 就永不
// 释放 —— 之后所有勾选变化触发的重算被静默吞掉，界面看起来「点了没反应」。
// 默认 30 秒；轮询这类短请求由调用方传更小的值。
const API_TIMEOUT_MS = 30000;
async function api(path, opts = {}) {
  const o = Object.assign({ headers: {} }, opts);
  o.headers['Authorization'] = 'Bearer ' + S.token;
  if (o.body && typeof o.body !== 'string') {
    o.headers['Content-Type'] = 'application/json';
    o.body = JSON.stringify(o.body);
  }
  const ms = o.timeoutMs === undefined ? API_TIMEOUT_MS : o.timeoutMs;
  delete o.timeoutMs;
  let ctl = null, timer = null;
  if (ms > 0 && typeof AbortController !== 'undefined' && !o.signal) {
    ctl = new AbortController();
    o.signal = ctl.signal;
    timer = setTimeout(() => ctl.abort(), ms);
  }
  let r, txt;
  try {
    r = await fetch(path, o);
    txt = await r.text();
  } catch (e) {
    // abort 的报错是 `AbortError`，原样抛出会显示成「signal is aborted」，
    // 看不出是超时。换成能指导下一步的文案。
    if (ctl && ctl.signal.aborted) {
      throw Object.assign(new Error(`请求超时（${Math.round(ms / 1000)} 秒无响应）`),
        { status: 0, timeout: true });
    }
    // fetch 自己抛的 TypeError（「Failed to fetch」/「NetworkError when …」/
    // 「Load failed」）只说明 TCP/TLS 层没拿到响应：容器重启、被 OOM 杀、
    // 反代超时断开都会这样。原文对用户毫无指导意义（2026-09-17 全量重探
    // 现场满屏「定档失败：Failed to fetch」），换成能指导下一步的文案。
    throw Object.assign(new Error('连接中断（容器可能重启或网关超时）'),
      { status: 0, network: true, cause: e });
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (r.status === 204 && !txt.trim()) return {};
  let data;
  try { data = JSON.parse(txt); }
  catch {
    const error = _proxyErrText(txt, r.status);
    if (r.ok) {
      throw Object.assign(new Error(`${error}（HTTP ${r.status}，响应不是有效 JSON）`),
        { status: r.status, invalidResponse: true });
    }
    data = { error };
  }
  if (data && typeof data === 'object' && data.boot_id) noteBootId(data.boot_id);
  if (!r.ok) {
    const ref = data && data.error_ref ? ` · error_ref ${data.error_ref}` : '';
    const base = (data && data.error) || r.statusText || '请求失败';
    const ra = parseFloat(r.headers.get('Retry-After') || '');
    throw Object.assign(new Error(`${base}（HTTP ${r.status}${ref}）`),
      { data, status: r.status, retryAfter: isFinite(ra) ? ra : null,
        errorRef: (data && data.error_ref) || null });
  }
  return data;
}

// ── 服务重启识别（boot_id）──
// 任务只存在服务端内存里，容器一重启所有 job / plan / apply 任务都没了，
// 轮询随之 404/410。后端在 /api/context 与各 *-status 里带 boot_id，
// 这里记住第一次见到的值；变了就明确告诉用户「服务已重启」，而不是让
// 界面停在「定档计算中…」。
function noteBootId(id) {
  if (!id) return;
  if (!S.bootId) { S.bootId = id; return; }
  if (S.bootId !== id) {
    S.bootId = id;
    S.bootChanged = true;
    showBanner('服务已重启（boot_id 变化）—— 进行中的任务已丢失，请重新提交；'
      + '若频繁出现，检查容器是否被 OOM 杀掉：docker inspect 看 OOMKilled / RestartCount', 'w');
  }
}

// ── 轮询错误分类 ──
// 同一份判定给 /api/job、/api/plan-status、/api/apply-status 三处轮询用。
// 返回 { kind, wait }：
//   auth      401 —— 重试无用，要重新登录
//   expired   404/410 —— 任务已不在服务端（重启或被淘汰）
//   throttled 429/503 —— 网关或任务仓限流；按 Retry-After 退避，**不算无响应**
//   network   连接中断 / 超时 / 5xx —— 指数退避重试
function classifyPollError(e, attempt) {
  const st = e && e.status;
  const expo = (base) => Math.min(30000, base * Math.pow(2, Math.min(attempt, 5)));
  if (st === 401) return { kind: 'auth', wait: 0 };
  if (st === 404 || st === 410) return { kind: 'expired', wait: 0 };
  if (st === 429 || st === 503) {
    const ra = e.retryAfter != null ? e.retryAfter * 1000 : expo(1000);
    return { kind: 'throttled', wait: Math.max(1000, Math.min(60000, ra)) };
  }
  return { kind: 'network', wait: expo(1000) };
}

// ── 全局横幅 ──
// 任何未捕获异常 / 未处理的 Promise 拒绝都显示出来，页面绝不静默黑屏。
function showBanner(msg, cls) {
  let box = document.getElementById('globalbanner');
  if (!box) {
    box = document.createElement('div');
    box.id = 'globalbanner';
    box.setAttribute('role', 'alert');
    document.body.insertBefore(box, document.body.firstChild);
  }
  box.className = 'gbanner ' + (cls || 'e');
  box.innerHTML = `<span>${esc(String(msg))}</span>`
    + ' <button type="button" class="gbclose" aria-label="关闭">×</button>';
  box.hidden = false;
  const b = box.querySelector('.gbclose');
  if (b) b.onclick = () => { box.hidden = true; };
}

function step(n) {
  $$('.rstep').forEach((el) => {
    const s = +el.dataset.s;
    el.classList.toggle('on', s === n);
    el.classList.toggle('done', s < n);
  });
}

async function boot() {
  const skel = $('#bootbox');
  const hideSkel = () => { if (skel) skel.hidden = true; };
  const q = new URLSearchParams(location.search);
  S.token = q.get('token') || sessionStorage.getItem('importer_token') || '';
  if (S.token) {
    sessionStorage.setItem('importer_token', S.token);
    // 别把 token 留在地址栏 —— 会进浏览器历史
    if (q.get('token')) history.replaceState(null, '', location.pathname);
  }
  if (!S.token) { hideSkel(); $('#gate').hidden = false; return; }

  // 慢的话把原因说出来。骨架已经在显示了，这里只是把文案换掉 ——
  // 「卡住了」与「还在读」对用户是两件事，而 3 秒是人开始怀疑的时点。
  const slow = setTimeout(() => {
    const m = $('#bootmsg');
    if (m) {
      m.innerHTML = `仍在等后端响应（已 3 秒）。config.yaml 很大或服务刚启动时
        会慢一些。<span class="hint">若持续不动，看容器日志：
        <code>docker compose logs -f cpa-upstream-importer</code></span>`;
    }
  }, 3000);

  try {
    S.ctx = await api('/api/context');
  } catch (e) {
    clearTimeout(slow);
    hideSkel();
    sessionStorage.removeItem('importer_token');
    $('#gate').hidden = false;
    if (e.status !== 401) {
      $('#gate .panel').insertAdjacentHTML('beforeend',
        `<div class="err">${esc(e.message)}</div>`);
    }
    return;
  }
  clearTimeout(slow);
  hideSkel();
  // 先校验形状，**再**揭开 #app。
  // ----------------------------
  // 2026-10-01：原来先 `$('#app').hidden = false` 再读
  // `S.ctx.sections` / `S.ctx.lines`。/api/context 回 200 但缺这两个键时
  // （前后端版本不一致、或脱敏器误伤整块）这里抛 TypeError，而此刻 #app
  // **已经可见**，后面的 renderBands/renderCpaHint/applyResources/
  // renderDrift/updateBudget 全被跳过。顶层 `boot().catch` 会额外放出
  // #gate 却不隐藏 #app —— 登录闸与空壳主界面同屏，所有参数格为空。
  // 现场就是「字段全空白」与「半黑屏」。
  const secs = S.ctx && S.ctx.sections;
  if (!secs || typeof secs !== 'object' || typeof S.ctx.lines !== 'number') {
    $('#app').hidden = true;
    $('#gate').hidden = false;
    $('#gate .panel').insertAdjacentHTML('beforeend',
      '<div class="err">后端 /api/context 返回的形状不对（缺 sections/lines）。'
      + '界面需要这两项才能渲染参数表，所以没有打开主界面 ——'
      + '空壳界面比明确报错更难排查。请确认容器拉到的是最新镜像。</div>');
    return;
  }
  $('#app').hidden = false;
  const entries = Object.values(secs).reduce(
    (a, b) => a + ((b && b.entries) || 0), 0);
  $('#cfgmeta').textContent =
    `${S.ctx.lines.toLocaleString()} 行 · ${entries} 条目`;
  renderBands();
  renderCpaHint();
  applyResources();
  renderDrift();
  updateBudget();
}

// ── 画像基线漂移 ──
// CPA 升级换了默认头而画像梯没跟上时，探测发的形态与 CPA 实际转发的不一致，
// 「探测通了但 CPA 不通」或反之都会发生。这里把核对结果显示出来 ——
// 包括「没能核对」这一种，那比让人以为全都比过了要好。

// pending 时的轮询。后端首次算这个检查要拉 GitHub，算完就进服务端缓存
// （成功 6 小时）。这里每 3 秒重取一次 /api/context，最多 10 次 —— 30 秒
// 拿不到就停手并说清，不无限刷。
//
// 只重取，不整页重渲染：renderBands / applyResources 都是幂等的，但重复
// 调用会把用户改过的并发数输入框重置回推荐值。所以只更新 drift 这一块。
let _driftPolls = 0;
let _driftTimer = null;
function scheduleDriftPoll() {
  if (_driftTimer) return;                 // 已经在轮了
  if (_driftPolls >= 10) {
    const box = $('#driftbox');
    if (box) {
      box.className = 'note';
      box.innerHTML = `<b>画像基线核对超时。</b>
        <span class="hint">后台仍在重试；刷新页面可再看一次。
        这个检查只是增强信号，不影响探测与写回。</span>`;
    }
    return;
  }
  _driftTimer = setTimeout(async () => {
    _driftTimer = null;
    _driftPolls += 1;
    try {
      const ctx = await api('/api/context');
      // 只挪 drift 那一块，别动别的 —— 见上面「不整页重渲染」的说明
      if (S.ctx) S.ctx.profile_drift = ctx.profile_drift;
      else S.ctx = ctx;
      renderDrift();
    } catch {
      scheduleDriftPoll();                 // 网络抖动，下一轮再来
    }
  }, 3000);
}

function renderDrift() {
  const box = $('#driftbox');
  if (!box) return;
  const d = S.ctx && S.ctx.profile_drift;
  if (!d) { box.hidden = true; return; }

  // pending：后端把这个检查挪到后台线程了（远程模式要拉 GitHub，国内 VPS
  // 拉不通时单次 8 秒起）。首次打开网页时它还没算完 —— 显示进行中并稍后
  // 自取，绝不让它挡住页面。见 server.py 的 _drift_snapshot。
  if (d.pending) {
    box.className = 'note';
    box.innerHTML = `<span class="spin"></span> <b>正在核对画像基线…</b>
      <span class="hint">${esc(d.why || '')}</span>`;
    box.hidden = false;
    scheduleDriftPoll();
    return;
  }

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
  // refreshing：这份结论已过 TTL，后台正在重算。显示出来 —— 否则用户
  // 无法区分「刚核对过」与「几小时前核对的」。
  const stale = d.refreshing
    ? ` <span class="hint">（结论已过期，后台正在重新核对）</span>` : '';

  if (!d.drifts.length) {
    box.className = 'note g';
    let t = `<b>画像基线一致</b>（依据：${esc(d.source)}${verText}）${stale}`;
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
    + `（依据：${esc(d.source)}${verText}）${stale}`
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
  const caps = $('#o_caps') ? $('#o_caps').checked : false;
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
  // 通的段还要验模型 + 换模采样 + 上下文二分 + 能力开关
  //
  // 能力开关按 1 次算而不是 4 次：codex 的 WS 握手与 compat 的
  // prompt_cache_key 各只作用于自己那一段，四段合计 2 次；这里按段算平均，
  // 取 1 是就近的高估（gemini / claude 段不发）。
  const perOkSection = attempts + (swap > 1 ? swap : 0) + (ctx ? 6 : 0)
    + (caps ? 1 : 0);
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

['#o_max_models', '#o_max_attempts', '#o_reuse_verdict', '#o_ctx', '#o_caps',
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

/* ── 服务端实配的 CPA 地址 ──────────────────────────────────────────
   界面上任何「留空会走到哪里」「去哪台容器重启」的提示都从这里取，
   不写死字面量。服务名与端口由部署方在 docker-compose.yml 的
   CPA_UPSTREAM_URL（或 --cpa-url）里自己定；写死 `http://cli-proxy-api:8317`
   的话，改过端口或服务名的部署会被界面教一个错地址 —— 而这些提示恰恰是
   给「已经填错地址的人」看的，再错一次代价更大。 */
function cpaTarget() {
  return (S.ctx && typeof S.ctx.cpa_url === 'string' ? S.ctx.cpa_url : '').trim();
}

// 「在 VPS 上重启哪个容器」——容器名后端不知道，但 compose 的惯例是
// 服务名即容器名，而管理地址的 host 就是服务名。取不到就退回通用说法，
// 不猜一个可能不存在的名字。
function cpaRestartHint() {
  const url = cpaTarget();
  let host = '';
  try { host = new URL(url).hostname; } catch { host = ''; }
  return host && !/^[0-9.]+$|^\[|^localhost$/.test(host)
    ? `在 VPS 上 <code>docker restart ${esc(host)}</code>`
    : '在 VPS 上重启 CPA 容器（<code>docker restart &lt;CPA 容器名&gt;</code>）';
}

function renderCpaHint() {
  const url = cpaTarget();
  const hint = $('#o_base_hint');
  const inline = $('#o_base_configured');
  if (hint) {
    hint.textContent = url
      ? '取自服务端配置（CPA_UPSTREAM_URL），无需填写'
      : '服务端未配 CPA 地址（CPA_UPSTREAM_URL / --cpa-url 为空）—— 写回后不会自动重载';
  }
  if (inline) {
    // 只读回显，不是输入框：这个值唯一的正确来源是服务端配置，
    // 让人再抄一遍只会多一次抄错的机会（2026-09-25 现场就是抄漏了 http://）。
    inline.textContent = url || '未配置';
    inline.classList.toggle('bad', !url);
  }
}

function renderBands() {
  const box = $('#bands');
  // S.ctx 缺失时不能用空对象假装有数据 —— 那样会把档位谱渲染成一堆 0，
  // 比报错更误导。这里直接说明「拿不到 ctx」，其余部分照常可用。
  if (!S.ctx || !S.ctx.sections) {
    box.innerHTML = `<div class="err">拿不到后端上下文（/api/context 未成功），`
      + `段信息与档位谱暂不可用。<span class="hint">刷新页面重试；`
      + `持续如此请看容器日志。</span></div>`;
    return;
  }
  box.innerHTML = sectionOrder().map((sec) => {
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

  // 形状闸：try/catch 只包住了请求，渲染段在它**之外**。
  // --------------------------------------------------
  // 2026-10-01：原来直接 `d.valid.forEach` 与 `r.bases[s].replace(...)`。
  // 后端少回 `valid`/`invalid`，或 `bases` 缺任一段键（`sectionOrder()`
  // 在 S.ctx 缺失时回落到硬编码的 4 段），`undefined.replace` 就抛
  // TypeError —— 它逃出 doParse，而 `#btnparse.onclick = doParse` 让它
  // 变成 unhandled rejection：`#parsebody` 从不写入、`#pparse` 保持
  // hidden、`syncProbeBtn()` 不执行。现场就是「添加账号后字段一片空白」。
  if (!d || !Array.isArray(d.valid) || !Array.isArray(d.invalid)) {
    $('#parsemsg').innerHTML = '<span style="color:var(--bad)">'
      + '后端 /api/parse 返回的形状不对（缺 valid/invalid 数组）——'
      + '大概率是前后端版本不一致，请确认容器拉到的是最新镜像。</span>';
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
    const bases = r.bases || {};
    return `<tr>
      <td class="num">${r.line_no}</td>
      <td class="m"><b>${esc(r.host)}</b>${n > 1
        ? ` <span class="pill p-i">${first ? '首个 · 全量探测' : '复用形态'}</span>` : ''}</td>
      <td class="m">${esc(r.key_masked)}</td>
      <td class="m" style="color:var(--ink-3)">${sectionOrder()
        .map((s) => `${SECTION_LABEL[s]}: ${bases[s]
          ? esc(String(bases[s]).replace(/^https?:\/\//, ''))
          : '<span class="hint">后端未回该段 base-url</span>'}`)
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
  // `total_calls` 缺字段时别渲染成 `undefined 次请求` —— 那看起来像
  // 统计出错，实际是前后端字段没对上。
  $('#diagmsg').textContent = typeof d.total_calls === 'number'
    ? `${d.total_calls} 次请求` : '完成（后端未回请求计数）';
  renderDiag(d);
};

function renderDiag(d) {
  S.diagYaml = {};
  // 形状闸。renderDiag 被 onclick 里的 try/catch **之外**调用，
  // `Object.keys(undefined)` 抛 TypeError 后 `#diagout` 已被清空却再也不填，
  // 整块「完整参数表」（priority / 前缀 / 模型 / 代理 / headers / 指纹 /
  // 上下文上限 / 能力开关 / 系统建议）随之消失 —— 现场就是「所有参数缺失」。
  if (!d || !d.sections || typeof d.sections !== 'object') {
    $('#diagout').innerHTML = '<div class="err">后端 /api/diag 返回的形状不对'
      + '（缺 sections）—— 参数表没法渲染。请确认容器拉到的是最新镜像。</div>';
    return;
  }
  const blocks = Object.keys(d.sections).map((sec) => {
    const s = d.sections[sec] || {};
    // `rungs` 下面还要数长度，缺字段时 `s.rungs.length` 会抛 —— 先归一。
    const rungList = Array.isArray(s.rungs) ? s.rungs : [];
    const rungCount = rungList.length;
    const rungs = rungList.map((g) => {
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
        + `<b>整梯 ${rungCount} 档全不通。</b>`
        + `这不一定是站方拒绝你 —— 也可能是余额、限时段、或它只认浏览器。`
        + `看上面每档的正文摘要判断。`
        + `如果你确知这个站能用，导入时可以用「人工接管」填模型清单。</div>`;
    } else if (!Object.keys(s.needed_headers || {}).length) {
      concl = `<div class="note g" style="margin-top:10px">`
        + `<b>baseline 就通，不需要任何 header。</b>`
        + `导入时 <code>headers</code> 留空即可。</div>`;
    } else {
      const yaml = yamlHeaders(s.needed_headers || {}, s.base_url);
      S.diagYaml[sec] = yaml;
      concl = `<div class="hdrbox">`
        + `<div><b>最小必需画像：${esc(s.hit.profile)}</b>`
        + `（试了 ${rungCount} 档）`
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

  // 完整参数表：与全量检测同一套字段。
  //
  // 为什么必须有（2026-09-02 用户指出）：诊断原来只显示「要什么头」，而写进
  // config.yaml 需要全套 —— 代理、指纹、priority、前缀、模型、上限、影响面。
  // 后端已改成走同一条链路（prober.probe + build_plan），这里把它渲染出来。
  let planHtml = '';
  if (d.plan && d.plan.sections && Object.keys(d.plan.sections).length) {
    const rows = Object.entries(d.plan.sections).map(([sec, sp]) => {
      const v = (d.verdicts || {})[sec] || {};
      const st = SRC_TAG[sp.model_source] || { t: sp.model_source, c: 'p-m' };
      const tag = sp.recommended ? '<span class="pill p-ok">建议写入</span>'
        : (sp.writable ? '<span class="pill p-w">需人工确认</span>'
                       : '<span class="pill p-m">不可写入</span>');
      return `<tr>
        <td class="m"><b>${esc(SECTION_LABEL[sec] || sec)}</b></td>
        <td><span class="pill ${v.usable ? 'p-ok' : (CAT_PILL[v.category] || 'p-m')}">${
          esc(v.usable ? '可用' : (v.category || '不可用'))}</span></td>
        <td class="m">${sp.priority}
          <div class="hint">${esc(sp.priority_reason || '')}</div></td>
        <td class="m">${esc(sp.prefix || '—')}
          ${sp.weight === 0
            ? ((S.ctx && S.ctx.weight_zero_excludes)
                ? '<div class="warn b">weight: 0 —— 不参与调度</div>'
                : '<div class="warn">weight: 0 —— 当前策略不读 weight，仍参与轮询</div>')
            : (sp.weight != null ? `<div class="hint">weight ${sp.weight}</div>` : '')}</td>
        <td><span class="pill ${st.c}">${st.t}</span>
          <div class="mlist">${esc((sp.models || []).join(', ')) || '—'}</div></td>
        <td class="m">${sp.proxy_url ? esc(sp.proxy_url)
          : (v.need_proxy ? '<span class="pill p-w">需代理</span>' : '直连')}</td>
        <td class="m">${esc(Object.keys(sp.headers || {}).join(', ')) || '—'}</td>
        <td class="m">${v.profile_name ? esc(v.profile_name)
          : (v.min_body_kind ? 'fingerprint-profile' : '—')}
          ${v.identity_proven ? '<div class="hint" title="画像梯某一档已推进到凭据类拒绝，说明站方接受这个身份；写回会带上 cloak 与 fingerprint-profile，Key 恢复后 CPA 直接可用">身份已验</div>' : ''}</td>
        <td class="num">${sp.max_context_length ? fmt(sp.max_context_length) : '—'}
          ${sp.context_model ? `<div class="hint">@${esc(sp.context_model)}</div>` : ''}</td>
        <td class="m">${toggleCell(sec, sp)}</td>
        <td>${tag}
          <div class="hint">${esc(sp.recommend_reason || '')}</div>
          ${(sp.warnings || []).map((w) =>
            `<div class="warn">${esc(w)}</div>`).join('')}
          ${sp.duplicate ? `<div class="warn b">${esc(sp.duplicate_note)}</div>` : ''}</td>
      </tr>`;
    }).join('');
    planHtml = `
      <div class="note" style="margin-top:18px">
        <b>完整参数（与全量检测同一套判定）</b>
        —— 这些就是写进 <code>config.yaml</code> 的值。
        <span class="hint">诊断跑的是完整四阶段：目录发现 → 段归属 → 模型验证
        → 换模采样。上下文二分默认关（那是百万字符的大 body），
        传 <code>probe_context: true</code> 可打开。</span>
      </div>
      <div class="tw"><table>
        <thead><tr>
          <th style="width:80px">段</th><th style="width:92px">判定</th>
          <th style="width:150px">priority</th><th style="width:66px">前缀</th>
          <th style="width:190px">模型</th><th style="width:120px">代理</th>
          <th style="width:150px">headers</th><th style="width:120px">请求指纹</th>
          <th style="width:110px">上下文上限</th>
          <th style="width:150px">能力开关</th><th>系统建议</th>
        </tr></thead>
        <tbody>${rows}</tbody></table></div>`;
  }

  $('#diagout').innerHTML = blocks + planHtml;

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
      probe_capabilities: $('#o_caps').checked,
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

function poll(delayMs) {
  clearTimeout(S.timer);
  const wait = delayMs != null ? delayMs : (S.jobPollMs || 900);
  S.timer = setTimeout(async () => {
    let d;
    try { d = await api(`/api/job/${S.jobId}?since=${S.cursor}`); }
    catch (e) {
      const cls = classifyPollError(e, S.pollFails || 0);
      // 任务已不在服务端（容器重启 / 被淘汰）：重试毫无意义，说清并给出路。
      if (cls.kind === 'expired') {
        $('#spin').hidden = true;
        $('#p2h').textContent = '② 任务已过期';
        $('#p2tag').textContent = '服务端已找不到这个任务（服务重启或任务被淘汰）';
        $('#stream').insertAdjacentHTML('beforeend',
          `<div class="s5">任务已过期（服务重启?）：${esc(e.message)} ——
           已完成的结果若已显示会保留；请重新发起探测。</div>`);
        showBanner('探测任务已过期（服务重启?）—— 请重新发起探测', 'w');
        return;
      }
      // 限流（nginx 429 / 任务仓满 503）不是「无响应」：按 Retry-After 退避，
      // 不计入连续失败次数。
      if (cls.kind === 'throttled') {
        $('#p2tag').textContent = `网关限流（HTTP ${e.status}），${Math.round(cls.wait / 1000)}s 后继续`;
        poll(cls.wait);
        return;
      }
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
      poll(cls.wait);
      return;
    }
    S.pollFails = 0;          // 通了就清零，只关心**连续**失败
    if (d && d.poll_ms > 0) S.jobPollMs = Math.max(500, Math.min(10000, d.poll_ms));
    // 游标只在后端真给了整数时才推进。缺字段时沿用旧值而不是写 undefined ——
    // 否则下一轮请求变成 `?since=undefined`，后端严格解析回 400，计进
    // pollFails，20 次后前端判「轮询中断」，而任务其实还在正常跑。
    if (Number.isInteger(d && d.event_cursor)) S.cursor = d.event_cursor;
    renderStream(d && d.events);

    // 进度条：用 unit_done/unit_total 而非 done_rows/total_rows ——
    // 全量重探的单元是「凭据」，与 rows 不是一回事（rows 可能为空）。
    const done = d.unit_done != null ? d.unit_done : d.done_rows;
    const total = d.unit_total != null ? d.unit_total : d.total_rows;
    $('#st_rows').textContent = `${done}/${total}`;
    $('#st_calls').textContent = d.calls;
    $('#st_time').textContent = d.elapsed;
    $('#prog').style.width = (total ? done / total * 100 : 0) + '%';

    if (S.reuseSaved > 0) {
      $('#st_saved').innerHTML = `复用省下 <b>${S.reuseSaved}</b> 轮`;
    }

    // ETA：区间显示，带速率与窗口大小。样本不足时不显示数字。
    const eta = $('#st_eta');
    const det = $('#eta_detail');
    if (d.eta_sec != null && d.eta_lo != null && d.eta_hi != null) {
      const fmt = (s) => s < 60 ? `${s}s` : `${Math.floor(s/60)}m${s%60}s`;
      const mid = fmt(Math.round(d.eta_sec));
      const lo = fmt(Math.round(d.eta_lo));
      const hi = fmt(Math.round(d.eta_hi));
      const rate = d.rate_per_min != null ? ` · ${d.rate_per_min}/分` : '';
      const smp = d.samples != null ? ` · 样本 ${d.samples}` : '';
      eta.innerHTML = `<b>剩余 ${mid}</b> <span class="hint">(${lo}~${hi}${rate}${smp})</span>`;
      eta.hidden = false;
    } else if (d.eta_suppressed) {
      // 高并发下剩余时间无法可靠外推（后端已判定），只报吞吐率。
      // 显式说明原因 —— 不然「有速率却没剩余时间」看着像 bug。
      const rate = d.rate_per_min != null ? `${d.rate_per_min}/分` : '';
      eta.innerHTML = (rate ? `<b>${rate}</b> ` : '')
        + `<span class="hint">${esc(d.eta_suppressed)}</span>`;
      eta.hidden = false;
    } else if (done > 0 && done < total) {
      // 样本不足 —— 不给误导性的数字
      eta.textContent = '估算中…';
      eta.hidden = false;
    } else {
      eta.hidden = true;
    }

    if (d.in_flight != null && d.in_flight > 0) {
      let txt = `在飞 ${d.in_flight} 个`;
      if (d.slowest_host && d.slowest_age != null) {
        txt += ` · 最慢站 ${esc(d.slowest_host)} 已跑 ${d.slowest_age}s`;
      }
      det.textContent = txt;
      det.style.display = '';
    } else {
      det.style.display = 'none';
    }

    if (d.state === 'done') {
      // 探测完成：停转圈、把标题从「探测中」改成「探测完成」。
      // 面板本身**不隐藏** —— 那份流水日志是判定依据，用户要能回看
      // （哪个段在哪个 combo 上通的、403 出现几次）。只是不再假装在跑。
      $('#spin').hidden = true;
      $('#p2h').textContent = '② 探测完成';
      // done_rows 可能小于 total_rows —— 抛异常的候选进不了结果集
      // （server.py 的 lost 分支会逐条报原因）。原来这里只显示
      // 「71/79 (90%)」就切到第三步，看着像「没跑完就往下走」。
      // 差额必须当场说清是**失败**而不是**未跑**，否则用户只能猜。
      const missed = d.total_rows - d.done_rows;
      $('#p2tag').textContent =
        `${d.calls} 次请求 · ${d.elapsed}s · 日志保留在下方可回看`;
      if (missed > 0) {
        $('#p2tag').textContent += ` · ${missed} 个候选探测时抛异常`;
        $('#prog').classList.add('partial');
        $('#st_rows').innerHTML =
          `${d.done_rows}/${d.total_rows}`
          + ` <span class="pill p-w">缺 ${missed}</span>`;
        $('#p2').insertAdjacentHTML('beforeend',
          `<div class="note w"><b>${missed} 个候选没有结果。</b>`
          + `它们探测时抛了异常，不在下面的结果表里 —— 上方日志的`
          + `红色 error 行逐条记了是哪个站、什么原因。`
          + `这不是「还没跑完」，重跑只会得到同样的结果，`
          + `除非先解决那些异常。</div>`);
      }
      if (d.results) S.results = d.results;  // 运行中任务保留旧结果，避免清空
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
    if (d.state === 'cancelled') {
      // 用户按了停止 —— 与出错分开显示（2026-09-12，与后端同一次改动）。
      // 不加这一支的话轮询既不停、转圈也不停：状态既不是 done 也不是
      // error，界面会永远停在「探测中」。
      $('#spin').hidden = true;
      $('#p2h').textContent = '② 已停止';
      $('#p2').insertAdjacentHTML('beforeend',
        '<div class="warn">已按请求停止 —— 未完成的站没有结论，'
        + '已完成的结果保留在下方。</div>');
      // 已完成部分照样渲染（2026-09-26）：原来这里不渲染，停止后表格为空。
      renderPartial(d);
      return;
    }

    if (d.state === 'error') {
      $('#spin').hidden = true;
      $('#p2h').textContent = '② 探测出错';
      $('#p2').insertAdjacentHTML('beforeend',
        `<div class="err">探测出错<pre>${esc(d.error)}</pre></div>`);
      renderPartial(d);
      return;
    }
    poll();
  }, wait);
}

// 取消 / 出错时把已有的部分结果画出来，而不是留一张空表。
function renderPartial(d) {
  if (!d || !Array.isArray(d.results) || !d.results.length) return;
  S.results = d.results;
  try {
    renderResults(d.results);
  } catch (e) {
    $('#p2').insertAdjacentHTML('beforeend',
      `<div class="err">部分结果渲染失败：${esc(e.message)}</div>`);
  }
}

const pad = (s, n) => esc(String(s == null ? '' : s).padEnd(n));

// 站名短标。79 个站并发探测，事件是一条交织的流 —— 不带归属时某站的
// 「可用段 []」会落在别站的尝试行之间，看起来像那个站没跑完就进了下一步。
// 现场就是这么误判的（2026-09-01：声明 4 次请求的块里有 38 行 attempt，
// 那些行属于别的站）。
//
// 取主机名的辨识段而不是整串：整串会把每行推宽 20+ 字符，而并发流里
// 需要的是「同不同站」的快速区分，不是完整地址。
function tag(host) {
  if (!host) return '        ';
  const parts = String(host).split('.');
  // api.foo.com -> foo；sub.foo.co.uk -> foo
  let stem = parts.length >= 3 ? parts[parts.length - 3] : parts[0];
  if (stem === 'api' || stem === 'www') stem = parts[parts.length - 2] || stem;
  return pad(stem.slice(0, 8), 8);
}

function renderStream(events) {
  // 2026-10-01：原来直接 `events.length`。`api()` 对 204 空响应回 `{}`
  // （见 api() 里的空体分支），此时 `d.events` 是 undefined ——
  // `undefined.length` 抛 TypeError，而这里是被 `poll()` 的 setTimeout
  // async 回调调用的，**不在任何 try 里**，于是变成 unhandled rejection：
  // poll() 不再重排，#spin 永转，showResume() 也不触发。
  // 现场表现就是「探测转圈不动、全量检测后字段全空白」。
  if (!Array.isArray(events) || !events.length) return;
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
      return `<div class="note">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `复用主机形态${e.verified ? (e.ok ? '（凭证已验）' : `（凭证不通：${esc(e.reason || '')}）`)
          : `（${esc(e.reason || '')}）`}</div>`;
    }
    // 站+段级不通的复用（2026-09-05）。与 shape-reused 分开显示 ——
    // 那个仍打一次基线验凭证，这个**一次请求都不发**，措辞不能一样。
    if (e.kind === 'shape-reuse-dead') {
      if (!S.reuseSeen) S.reuseSeen = new Set();
      const dk = `dead|${e.t}|${e.section}|${e.host || ''}`;
      if (!S.reuseSeen.has(dk)) {
        S.reuseSeen.add(dk);
        S.reuseSaved += 1;
      }
      return `<div class="note">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `复用同段结论 ${esc(e.category || '')}${e.action ? ' · ' + esc(e.action) : ''} `
        + `<span class="dim">（这类拒绝与凭据无关，零请求）</span></div>`;
    }
    if (e.kind === 'shape-reuse-abort') {
      return `<div class="s4">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section], 8)} `
        + `${esc(e.reason || '')}</div>`;
    }
    // 熔断开启（2026-09-16）：「临时 / 未知」类连续 N 次完整探测都是同一个
    // 状态码，本轮剩余 Key 已设为零请求直接复用。与 shape-reuse-dead 的区别：
    // 那个是一次就确定（门禁/WAF 等），这个是多次积累后触发的熔断。
    if (e.kind === 'fail-streak-open') {
      return `<div class="note">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `${esc(e.status)} 连续 ${e.streak} 次，触发熔断 —— 本轮后续 Key 零请求`
        + `<span class="dim">（${esc(e.category || '')}）</span></div>`;
    }
    // 站方负载上限（503/502/504）会重试一次。要让这一步可见 —— 否则
    // 用户只看到同一个模型出现两次、不知道为什么，也不知道等了 2 秒。
    if (e.kind === 'transient-retry') {
      return `<div class="note">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `${pad(e.model, 20)} ${esc(e.status)} 临时错误，${e.wait}s 后重试一次</div>`;
    }
    // 时段：分组按窗口开放。凭据是好的、不是站点问题 —— 窗口内重测。
    if (e.kind === 'time-window') {
      const win = e.window ? `${e.window[0]}~${e.window[1]}` : '未知';
      return `<div class="note">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `限时段（${win}）—— 窗口内复测</div>`;
    }
    // 二级代理救活：其余处置全用尽后换出口 IP 通了。要显眼 —— 这个段
    // 写进 config.yaml 时必须带 proxy-url，没有代理的机器上它就是不通的。
    if (e.kind === 'proxy-rescued') {
      return `<div class="s2">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `${pad(e.model, 20)} 换出口 IP 后通（原判「${esc(e.was)}」）`
        + ` —— 该段必须带 proxy-url</div>`;
    }
    // 画像命中：第几次试到通的、什么档、是否需 body 补丁
    if (e.kind === 'profile-hit') {
      const body = e.needs_body ? '+body' : '';
      return `<div class="s2">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `画像 ${esc(e.profile)}${body} 通（试 ${e.tried} 档）</div>`;
    }
    // 画像梯跑完仍不通 —— 让操作员看到「试了几档都不行」，不是「没试」
    if (e.kind === 'profile-exhausted') {
      return `<div class="s4">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `画像梯跑完仍不通（试 ${e.tried} 档）</div>`;
    }
    // 门票已过、凭据不行：某一档把「客户端」拒绝推进成了凭据类拒绝（余额/
    // 鉴权），说明站方认了这个身份。必须显示 —— 这是「直连可用、经 CPA 不
    // 可用」那条问题的现场证据，也是写回 cloak / fingerprint-profile 的依据。
    if (e.kind === 'profile-gate-passed') {
      return `<div class="s2">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `画像 ${esc(e.profile)}（档 ${e.tier}）过了客户端门禁，`
        + `改判 ${esc(e.then)}${e.status ? ' · ' + esc(e.status) : ''}</div>`;
    }
    // 整梯没有一档 200，但门票被证明过 —— 段仍不可用，写回照样带身份。
    if (e.kind === 'profile-gate-only') {
      return `<div class="warn">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `整梯不通，但身份 ${esc(e.profile)} 已被站方接受（${esc(e.then)}）`
        + ` —— 写回带 headers 与 cloak/fingerprint，换把 Key 即可用</div>`;
    }
    // 整梯全败后，正文点名要 beta 就补上重试。显示补了什么 —— 这一步会改
    // 落地的 anthropic-beta，操作员必须看得到凭什么改的。
    if (e.kind === 'beta-retry') {
      return `<div class="note">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `正文点名缺 ${esc((e.added || []).join(','))}，`
        + `补进 ${esc(e.profile)} 重试</div>`;
    }
    if (e.kind === 'beta-hit') {
      return `<div class="s2">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `补 beta 后通（${esc(e.profile)}）</div>`;
    }
    // 同段整梯已试过全败，后续种子跳过。**必须显示** —— 否则日志里看起来
    // 像是这个种子没被处理，而实际是刻意省掉的重复请求。
    if (e.kind === 'profile-skipped') {
      return `<div class="note">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `${pad(e.model, 20)} 跳过画像梯（${esc(e.why || '同段已试过')}）</div>`;
    }
    // 模型验证撞到尝试上限。显示剩余数，让操作员知道「不是全验了」
    if (e.kind === 'model-scan-capped') {
      return `<div class="note">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `模型验证达上限：试 ${e.attempted} 次收 ${e.accepted} 个，`
        + `余 ${e.remaining} 个未验</div>`;
    }
    // 200 但正文是错误体 / 换模 —— 模型被拒收，不进写入清单
    if (e.kind === 'model-rejected') {
      const why = e.reason ? esc(e.reason)
        : `请求 ${esc(e.requested)} 却回 ${esc(String(e.actual))}`;
      return `<div class="s4">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `${pad(e.requested, 20)} 模型不收：${why}</div>`;
    }
    if (e.kind === 'attempt') {
      const c = e.status === '200' ? 's2' : (e.status[0] === '4' ? 's4' : 's5');
      return `<div class="${c}">${esc(tag(e.host))} `
        + `${pad(SECTION_LABEL[e.section] || e.section, 8)} `
        + `${pad(e.model, 20)} ${pad(e.combo, 18)} ${pad(e.status, 5)} `
        + `${esc(e.category)}</div>`;
    }
    if (e.kind === 'catalog') {
      return `<div>  ${pad(SECTION_LABEL[e.section], 8)} /models 目录 ${e.count} 个`
        + `（已按 gemini/gpt/claude 过滤）</div>`;
    }
    // 目录问不到不是失败 —— 很多站关掉了 /models，照样能推理。
    // 这时探测退回种子模型，日志里要说清「为什么用的是种子」。
    if (e.kind === 'catalog-miss') {
      return `<div class="s4">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section], 8)} `
        + `/models 目录不可读（${esc(e.status)}），改用种子模型试探</div>`;
    }
    // 代理救活后补取目录（2026-09-05）。直连拿不到目录、经代理拿到了 ——
    // 这一行很重要：没有它的话，用户看到的是「目录不可读 → 用种子」，
    // 而实际上后来拿到了真实目录，写进 config.yaml 的模型名来源完全不同。
    if (e.kind === 'catalog-via-proxy') {
      return `<div class="s2">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section], 8)} `
        + `经代理补取到目录 ${esc(String(e.count))} 个模型 —— ${esc(e.why || '')}</div>`;
    }
    if (e.kind === 'swap') {
      return `<div class="s4">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section], 8)} `
        + `静默换模 ${e.rate_pct}%（${esc(e.model)}）</div>`;
    }
    if (e.kind === 'context') {
      return `<div class="s2">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section], 8)} `
        + `上下文上限 ${fmt(e.limit)}${e.untrusted ? '（由截断反推）' : ''}</div>`;
    }
    if (e.kind === 'context-declared') {
      // 上游在超限错误里**明说**了上限 —— 省掉整轮二分（最多 5 次百万字符
      // 请求）。这件事值得显示：它解释了为什么这个段没跑满二分轮次。
      return `<div class="s2">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section], 8)} `
        + `上游自报上限 ${fmt(e.limit)} —— 免掉二分（${esc(e.model)}）</div>`;
    }
    if (e.kind === 'capability') {
      // 段专属能力开关的实测结论（codex 的 websockets、compat 的
      // support-prompt-cache-key）。三态各自一种措辞 —— 「实测不支持」与
      // 「未探测」显示成一个样子就是「未验证当已验证」的镜像错误。
      const cls = e.result === true ? 's2' : (e.result === false ? 's4' : 'note');
      let what;
      if (e.result === true) {
        what = `<b>支持</b> ${esc(e.name)}`
          + (e.status ? `（握手 ${esc(e.status)}` : '')
          + (e.elapsed_ms ? ` · ${e.elapsed_ms}ms）` : (e.status ? '）' : ''));
      } else if (e.result === false) {
        what = `不支持 ${esc(e.name)}（返回 ${esc(e.status || '—')}）—— 不写这个字段`;
      } else {
        what = `未探测 ${esc(e.name)}`
          + (e.why === 'need_proxy'
            ? '：该段需走代理，直连的结果说明不了走代理时的行为'
            : (e.status ? `：未得到有效响应（${esc(e.status)}）` : ''));
      }
      return `<div class="${cls}">${esc(tag(e.host))} `
        + `${pad(SECTION_LABEL[e.section], 8)} ${what}</div>`;
    }
    if (e.kind === 'rate-limit-learned') {
      // 站方在正文里自报了探测节奏阈值（N 个模型 / M 秒），工具据此自动
      // 放慢该站的请求间隔。这件事必须可见：它解释了为什么这个站后面的
      // 尝试变慢了，也让「限频撞 46 次」那种情形不再需要人去看日志猜 --gap。
      return `<div class="s3">${esc(tag(e.host))} ${pad('限频', 8)} `
        + `站方自报 ${e.models} 个模型 / ${e.window}s —— 本站探测间隔 `
        + `${e.was}s → <b>${e.gap}s</b>，四段合用一个节奏桶</div>`;
    }
    if (e.kind === 'context-untrusted') {
      // 上游回了个荒谬的 input_tokens（实测见过 10）。那个数会被当成实测容量
      // 写进 max-context-length，而 CPA 把它当 context_window 报给客户端 ——
      // 10 个 token 的窗口等于这个站彻底不可用。丢弃并说明。
      return `<div class="s3">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section], 8)} `
        + `上游 input_tokens 只有 ${fmt(e.tokens)}（发了 ${fmt(e.sent_chars)} 字符）`
        + ` —— 计数不可信，不写 max-context-length（${esc(e.model)}）</div>`;
    }
    if (e.kind === 'section-error') {
      // 某段探测抛异常。另外三段照常跑完，但这一段的失败必须可见 ——
      // 不显示的话它会表现成「这个段莫名不可用」。
      return `<div class="s5">${esc(tag(e.host))} ${pad(SECTION_LABEL[e.section], 8)} `
        + `探测异常：${esc(e.error)}</div>`;
    }
    if (e.kind === 'section-done') {
      // 逐段收尾。并行下四段完成先后是乱的，这行让顺序可追溯。
      return `<div class="${e.usable ? 's2' : 's4'}">${esc(tag(e.host))} `
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
      // 「可用」= 至少一段通。原来这里显示的 success 要求四段全通，
      // 79 个凭据里只有 1 个满足，于是长期显示「成功 0」而下方日志在刷
      // 200 —— 看起来像卡住了。四段全通改成括注。
      if (e.success != null) {
        stat.push(`可用 ${e.success}`
          + (e.all_four ? `（全通 ${e.all_four}）` : ''));
      }
      if (e.failure != null) stat.push(`全灭 ${e.failure}`);
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
  // 候选身份 = 输入行号。一个站常有 15 把 Key，用 host 当身份会让同站
  // 多 Key 的勾选状态、priority 输入、模型清单全部串到第一行上。
  const rid = String(r.row.line_no);
  const rows = sectionOrder().map((sec) => {
    const v = r.sections[sec];
    if (!v) return '';
    const label = SECTION_LABEL[sec] || sec;

    if (!v.usable) {
      const pill = CAT_PILL[v.category] || 'p-m';
      const last = (v.attempts || []).filter((a) => a.status !== '200').slice(-1)[0];
      // 探测失败的段也给勾选框 —— 判定会错，必须有人工出口。
      // 但要求先填模型清单：探测没验成功过任何模型，工具无从推断该注册什么。
      // 勾选框默认不勾，且只有填了模型才可勾（见 bindResultEvents）。
      const fm = ((S.forced[rid] || {})[sec] || []).join(', ');
      // 站方 /models 目录报出来的模型 —— 探测跑不通不等于站方没这些模型，
      // 现场就有「CPAMP 面板看得见模型、这里判死路」的形态：目录是站方
      // 声明有什么，探测测的是这把 Key 的分组能用什么，两者本就会不一致。
      // 目录按段过滤 —— 混族的名字在这个段发不出去，列出来只会误导
      //
      // 但「一个四族的都没有」时退一步收下站方自己报的（2026-09-03，与后端
      // build_plan 的 catalog 分支同一条规则）：那时另一个选项是只显示手填框，
      // 而后端会写工具猜的名字 —— 这个站从没报过它们。实测 romeo 与
      // foxtrot 的 compat 段就是这种处境（目录里只有 grok-4.6 / glm-5.2，
      // 而 grok-4.6 是那个站唯一端到端验证过的模型）。
      //
      // 判据用 protoOk（协议层）而不是 famOk：前三段仍按族拒，只有 compat 段
      // 的 /chat/completions 真的不限族。
      const catFam = (v.catalog || []).filter((m) => m && famOk(sec, m));
      const catProto = (v.catalog || []).filter((m) => m && protoOk(sec, m));
      const cat = catFam.length ? catFam : catProto;
      // 「已滤掉」只算协议层就发不出去的 —— 四族之外那批在 catProto 里，
      // 收下之后不该再报成滤掉。
      const cut = (v.catalog || []).filter((m) => m && !protoOk(sec, m)).length;
      // 目录里一个四族的都没有、于是列的是站方自己报的四族之外的名字
      const catOff = !catFam.length && catProto.length;
      // 首次渲染：没有人工接管记录时按段规则预勾，省掉一个一个点。
      // 已有记录（用户改过）就完全尊重记录，不覆盖。
      //
      // pickDefaults 在段规则之上再做「同系列取最新」—— 目录里同时有
      // gpt-5.5 与 gpt-5.6 时只勾 5.6。这是用户 2026-09-02 的要求，
      // 现场截图里 codex 段 8 个全勾（含 gpt-4o / gpt-oss-*）就是它缺位的后果。
      const rec = (S.forced[rid] || {})[sec];
      const stale = staleCheck(sec, cat);
      const picked = new Set(rec !== undefined ? rec : stale.keep);
      // 这里的预勾**只是显示**，不回写 S.forced（2026-09-26 改）。
      //
      // 原来预勾的结果会立刻写进 S.forced，而后端把 S.forced 一律当成「操作员
      // 手填」（model_source=manual）—— 手填是最高权威：不过世代规则之外的
      // 证据检查、默认建议写入。于是前端 staleCheck 这套 JS 选型结果被包装成
      // 人工决定送回去，绕过了后端整套选型与证据规则。本机全量重探实测：64 段
      // 被这样变成「手填」并默认写入，其中 codex 段只剩单个 gpt-6-astra（同代的
      // -luna / -sol 全丢）—— 用户报的「模型勾选高低混乱」就是两套选型打架。
      //
      // 现在：没有人工操作时，写什么模型只由后端方案决定（fillPlanIntoRows 会把
      // 勾选状态同步成方案里的 sp.models）；只有操作员真的点了勾选框或手填，
      // 才写 S.forced。
      return `<tr class="off" data-rid="${esc(rid)}" data-host="${esc(host)}" data-sec="${esc(sec)}">
        <td class="pick"><input type="checkbox" class="sel force"
          data-rid="${esc(rid)}" data-host="${esc(host)}" data-sec="${esc(sec)}"
          title="探测未通过。勾选即接管，模型可从右侧目录选或手填"></td>
        <td class="m"><b>${esc(label)}</b></td>
        <td><span class="pill ${pill}">${esc(v.category || '不可用')}</span></td>
        <td>
          <div class="mlist"></div>
          ${cat.length ? `<div class="cats">${cat.map((m) => `
            <label class="catpick"><input type="checkbox" class="cm"
              data-rid="${esc(rid)}" data-host="${esc(host)}" data-sec="${esc(sec)}"
              value="${esc(m)}"${picked.has(m) ? ' checked' : ''}>${esc(m)}</label>`
            ).join('')}</div>
          <div class="mtools">
            <button type="button" class="mini cmall" data-rid="${esc(rid)}" data-host="${esc(host)}"
              data-sec="${esc(sec)}">全选</button>
            <button type="button" class="mini cminv" data-rid="${esc(rid)}" data-host="${esc(host)}"
              data-sec="${esc(sec)}">反选</button>
            <button type="button" class="mini cmnone" data-rid="${esc(rid)}" data-host="${esc(host)}"
              data-sec="${esc(sec)}">清空</button>
            <span class="hint">目录 ${cat.length} 个，已勾 <b class="cmn">${picked.size}</b>
              ${cut ? ` · 已滤掉 ${cut} 个不符合本段规则的模型` : ''}</span>
            ${catOff ? `<div class="hint">站方目录里没有本工具四族清单
              （gemini / gpt / claude / kimi）内的任何模型，上面列的是它自己报的。
              退这一步是因为另一个选项更糟：写工具猜的名字，而这个站从没报过它们。
              能不能用取决于上游认不认 —— 本工具没有验证过</div>` : ''}
            ${picked.size === 0 && cat.length && !catOff && stale.line ? `<div class="hint">
              整份目录都落后于市面最新：<code>${esc(stale.line)}</code> 线的目录最高
              世代是 ${esc(stale.cat)}，市面已到 ${esc(stale.mkt)}
              —— 默认不勾。确知该站只卖这些且够用，手工勾上即可</div>` : ''}
          </div>`
            // 目录读不到（或目录里的名字全被规则滤掉）—— 这里**留一个空容器**，
            // 由 refreshPlan 用后端方案里的 sp.models 填成勾选框。
            //
            // 2026-09-02 现场（截图1）：后端已经按「当前市面最新」填了 6 个模型，
            // 警告文本里也写着那 6 个名字，而这一格只渲染了一个空的手填框 ——
            // 它从 S.forced 取值，而 S.forced 此刻是空的。于是用户看到空白，
            // 而提交时读的正是 S.forced：那个段勾上也写不进任何模型。
            : `<div class="cats fallback" data-rid="${esc(rid)}"
                 data-host="${esc(host)}" data-sec="${esc(sec)}"></div>
               <div class="mtools fallback-tools" hidden>
                 <button type="button" class="mini cmall" data-rid="${esc(rid)}"
                   data-host="${esc(host)}" data-sec="${esc(sec)}">全选</button>
                 <button type="button" class="mini cminv" data-rid="${esc(rid)}"
                   data-host="${esc(host)}" data-sec="${esc(sec)}">反选</button>
                 <button type="button" class="mini cmnone" data-rid="${esc(rid)}"
                   data-host="${esc(host)}" data-sec="${esc(sec)}">清空</button>
                 <span class="hint">已勾 <b class="cmn">0</b></span>
               </div>
               ${cut ? `<div class="hint">站方目录里 ${cut} 个模型都不符合本段规则
                 （跨族、图像/语音/oss，或 gemini 段的非 pro 档），已全部滤掉 ——
                 下面这批取自「当前市面最新」</div>` : ''}`}
          <div class="pedit"><input type="text" class="fm" style="width:100%"
            data-rid="${esc(rid)}" data-host="${esc(host)}" data-sec="${esc(sec)}"
            value="${esc(fm)}"
            placeholder="${cat.length ? '也可手填目录外的模型名，逗号分隔'
              : '还可手填目录外的模型名，逗号分隔（上面那批已按市面最新填好）'}"></div>
          <div class="hint">工具不会验证这些模型 —— 写错会让 CPA 每次轮到它都失败</div>
          <div class="fmhint"></div>
        </td>
        <td class="num">${v.max_context_length ? fmt(v.max_context_length) : '—'}
          ${v.context_model ? `<div class="hint">@${esc(v.context_model)}</div>` : ''}</td>
        <td>${esc(v.action || '不写入')}
          ${v.need_proxy ? '<div><span class="pill p-w">需代理</span></div>' : ''}
          ${capBadge(sec, v)}
          ${last && last.excerpt
            ? `<div class="mlist">${esc(last.status)} · ${esc(last.excerpt.slice(0, 90))}</div>`
            : ''}</td>
        <td class="prio">
          <div class="pedit"><input type="number" class="pi"
            data-rid="${esc(rid)}" data-host="${esc(host)}" data-sec="${esc(sec)}" placeholder="计算中"></div>
        </td>
        <td class="rsn"><span class="hint">定档计算中…</span></td>
      </tr>
      <tr class="wrow" data-rid="${esc(rid)}" data-host="${esc(host)}" data-sec="${esc(sec)}">
        <td></td><td colspan="7" class="wbox"></td>
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
    // 可用行的模型格：实测清单 + 目录里探测没验到的名字，都做成勾选框。
    //
    // 2026-09-03 现场（截图）：这一格原来只渲染 `v.models.join(', ')` 纯文本，
    // 于是三条毛病同时存在 ——
    //   ① v.models 为空（静默换模 / 200 包错误体，`_accept` 全拒）时显示
    //      「无可信模型」，而后端方案里 sp.models 已经有 6 个（seed 兜底）。
    //      判死行有 .cats.fallback 容器接住它，可用行连容器都没有。
    //   ② 可用行完全没有手填入口，操作员想改清单只能去改 config.yaml。
    //   ③ 探测只验 max_models（默认 4）个就停，站方目录里其余名字在这一格
    //      看不见 —— 而那些名字往往正是要写进去的。
    //
    // 与判死行同一套 DOM 约定（.cats / .cm / .fm / .mtools / .cmn），
    // 所以 bindResultEvents 与 refreshPlan 的 fallback 填充无需分叉。
    const uProbed = (v.models || []).filter(Boolean);
    // 目录里通过本段规则、且不在实测清单里的名字 —— 默认**不勾**：
    // 实测过的才是有依据的，目录只是站方声称。
    const uExtra = (v.catalog || [])
      .filter((m) => m && famOk(sec, m) && !uProbed.includes(m));
    const uAll = uProbed.concat(uExtra);
    const uRec = (S.forced[rid] || {})[sec];
    // 首次渲染按「实测清单」预勾。实测为空时留给 refreshPlan 用 sp.models 填 ——
    // 那条路要求容器里没有 .cm，所以这里在 uAll 为空时才渲染空的 fallback 容器。
    //
    // **不**把预勾结果回写 S.forced（2026-09-03）。判死行那边曾经必须回写，
    // 因为不回写就没有模型可写；现在后端对每段都算出确定清单，前端显示的
    // 就是它算出来的那一份 —— 不回写，两边照样一致。
    //
    // 而回写有害：`forced` 非空会让 build_plan 走 manual 分支，于是
    //   · 徽标从「实测」变成「手填」，recommended 翻假，「只勾推荐项」勾不到
    //   · 更糟的是 seed 猜测被洗成 manual，正好绕过新增段那道闸
    //     （它按 model_source 判，manual 放行）—— 又回到 121 条目变 246 的老路
    // S.forced 现在只在**用户真的动过**勾选框或手填框时才写（见 bindResultEvents）。
    const uPick = new Set(uRec !== undefined ? uRec : uProbed);
    const uFm = ((S.forced[rid] || {})[sec] || [])
      .filter((m) => !uAll.includes(m)).join(', ');

    return `<tr data-rid="${esc(rid)}" data-host="${esc(host)}" data-sec="${esc(sec)}">
      <td class="pick"><input type="checkbox" class="sel"
        data-rid="${esc(rid)}" data-host="${esc(host)}" data-sec="${esc(sec)}"></td>
      <td class="m"><b>${esc(label)}</b></td>
      <td><span class="pill p-ok">可用</span></td>
      <td>
        <div class="mlist"></div>
        ${uAll.length ? `<div class="cats">${uAll.map((m) => `
          <label class="catpick"><input type="checkbox" class="cm"
            data-rid="${esc(rid)}" data-host="${esc(host)}" data-sec="${esc(sec)}"
            value="${esc(m)}"${uPick.has(m) ? ' checked' : ''}>${esc(m)}${
              uProbed.includes(m) ? '' : ' <span class="hint">目录</span>'}</label>`
          ).join('')}</div>
        <div class="mtools">
          <button type="button" class="mini cmall" data-rid="${esc(rid)}" data-host="${esc(host)}"
            data-sec="${esc(sec)}">全选</button>
          <button type="button" class="mini cminv" data-rid="${esc(rid)}" data-host="${esc(host)}"
            data-sec="${esc(sec)}">反选</button>
          <button type="button" class="mini cmnone" data-rid="${esc(rid)}" data-host="${esc(host)}"
            data-sec="${esc(sec)}">清空</button>
          <span class="hint">实测 ${uProbed.length} 个${
            uExtra.length ? ` · 目录另有 ${uExtra.length} 个未验证（默认不勾）` : ''
          }，已勾 <b class="cmn">${uPick.size}</b></span>
        </div>`
          // 实测清单为空 —— 留空容器给 refreshPlan 用后端方案里的 sp.models 填。
          // 与判死行的 fallback 分支同一条路径。
          : `<div class="cats fallback" data-rid="${esc(rid)}"
               data-host="${esc(host)}" data-sec="${esc(sec)}"></div>
             <div class="mtools fallback-tools" hidden>
               <button type="button" class="mini cmall" data-rid="${esc(rid)}"
                 data-host="${esc(host)}" data-sec="${esc(sec)}">全选</button>
               <button type="button" class="mini cminv" data-rid="${esc(rid)}"
                 data-host="${esc(host)}" data-sec="${esc(sec)}">反选</button>
               <button type="button" class="mini cmnone" data-rid="${esc(rid)}"
                 data-host="${esc(host)}" data-sec="${esc(sec)}">清空</button>
               <span class="hint">已勾 <b class="cmn">0</b></span>
             </div>
             <div class="hint">端点响应正常、凭证有效，但返回的模型与请求不一致
               （静默换模或 200 包错误体）—— 实测清单为空，下面这批取自
               「当前市面最新」，勾选前请确认</div>`}
        <div class="pedit"><input type="text" class="fm" style="width:100%"
          data-rid="${esc(rid)}" data-host="${esc(host)}" data-sec="${esc(sec)}"
          value="${esc(uFm)}"
          placeholder="也可手填上面没有的模型名，逗号分隔"></div>
        <div class="hint">手填与「目录」项工具都没验证过 —— 写错会让 CPA
          每次轮到它都失败</div>
        <div class="fmhint"></div>
        ${backends.length ? `<div class="hint">后端 ${esc(backends.join(' / '))}</div>` : ''}
      </td>
      <td class="num">${v.max_context_length ? fmt(v.max_context_length) : '—'}
        ${v.context_untrusted ? '<div class="hint">截断反推</div>' : ''}
        ${v.context_model ? `<div class="hint">@${esc(v.context_model)}</div>` : ''}</td>
      <td>${flags.join(' ') || '<span class="hint">直连即可</span>'}
        ${capBadge(sec, v)}</td>
      <td class="prio">
        <div class="pedit"><input type="number" class="pi"
          data-rid="${esc(rid)}" data-host="${esc(host)}" data-sec="${esc(sec)}" placeholder="计算中"></div>
      </td>
      <td class="rsn"><span class="hint">计算中…</span></td>
    </tr>
    <tr class="wrow" data-rid="${esc(rid)}" data-host="${esc(host)}" data-sec="${esc(sec)}">
      <td></td><td colspan="7" class="wbox"></td>
    </tr>`;
  }).join('');

  // 尝试明细：每段一张表，放在站卡最下面。
  // 12 个字段后端一直在返回，而结果表只显示了 status 与 excerpt ——
  // 排障时真正要看的「哪一档通的、别的档报什么、哪个慢」都在这里。
  const detail = sectionOrder().map((sec) => {
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

  // 幂等闸（2026-09-11）：`#results` 是 index.html 里**持久存在**的容器，
  // 而本函数每次 renderResults() 都会被调一次（:1592）。原来直接
  // addEventListener，于是走「改输入重来 / 再投喂一批」再探测第二轮后，
  // 同一个 change 被处理 2 次、第三轮 3 次，每次各发一个 POST /api/plan；
  // 「全选」按钮更糟 —— click 处理器 ×N 各自 dispatch 一次 change。
  // 每份 plan 约 1.7MB（tests/test_server.py:228-236 记过 512M 容器 90 秒 OOM），
  // 这是可放大的资源问题。
  //
  // 用容器自身的标记位而不是 removeEventListener：处理器是匿名闭包，
  // 拿不到引用；也不用 cloneNode 换容器 —— 那会丢掉 renderResults 刚写进去
  // 的 DOM 与其它地方持有的引用。
  if (box.dataset.evBound === '1') return;
  box.dataset.evBound = '1';

  // 目录模型的批量勾选。不自己写入 S.forced —— 改完 checkbox 状态后派发
  // 一次 change，复用下面那个 .cm 处理器（它还要合并手填框里目录外的模型，
  // 两处各写一遍必然分叉）。
  box.addEventListener('click', (e) => {
    const b = e.target.closest('.cmall, .cminv, .cmnone');
    if (!b) return;
    const tr = b.closest('tr');
    if (!tr) return;
    const cms = $$('.cm', tr);
    if (!cms.length) return;
    const mode = b.classList.contains('cmall') ? 'all'
      : (b.classList.contains('cminv') ? 'inv' : 'none');
    cms.forEach((c) => {
      c.checked = mode === 'all' ? true
        : (mode === 'inv' ? !c.checked : false);
    });
    cms[0].dispatchEvent(new Event('change', { bubbles: true }));
  });

  box.addEventListener('change', (e) => {
    // 人工接管的模型清单
    // 目录复选框 —— 与手填框同一份 S.forced，勾选即写入
    const cmi = e.target.closest('.cm');
    if (cmi) {
      const h = cmi.dataset.rid, sc = cmi.dataset.sec;
      const tr = cmi.closest('tr');
      const chosen = tr
        ? $$('.cm', tr).filter((x) => x.checked).map((x) => x.value) : [];
      // 手填框里目录外的模型要留着 —— 两个入口写同一个段，不能互相清空
      const box3 = tr && tr.querySelector('.fm');
      const known = new Set($$('.cm', tr).map((x) => x.value));
      const extra = box3
        ? box3.value.split(',').map((x) => x.trim())
            .filter((x) => x && !known.has(x)) : [];
      const list = chosen.concat(extra);
      S.forced[h] = S.forced[h] || {};
      if (list.length) {
        S.forced[h][sc] = list;
      } else {
        delete S.forced[h][sc];
        // 清空模型时**同步取消该段的写入勾选**（2026-09-11）
        // ----------------------------------------------------
        // 下面那个 `.fm` 处理器在清空时会 `S.picks.delete(pk(h, sc))`
        // （见它自己的注释），而这一支从不动 S.picks —— 于是「清空」按钮
        // 走的这条路会留下「写入列还勾着、模型清单是空的」的状态。
        // 后端把空清单当作未接管、跳过该段，界面却显示会写入。
        // 两个入口写同一个段，处置必须一致。
        if (S.picks) S.picks.delete(pk(h, sc));
      }
      const n = tr && tr.querySelector('.cmn');
      // 计数要含手填框里目录外的模型 —— 显示的是**这一段实际会写入几个**，
      // 只数 .cm 会与写回清单对不上（手填 2 个后计数不变）。
      if (n) n.textContent = String(list.length);
      refreshPlan(true);
      syncPickUI();
      return;
    }

    const fmi = e.target.closest('.fm');
    if (fmi) {
      const h = fmi.dataset.rid, sc = fmi.dataset.sec;
      const tr = fmi.closest('tr');
      // 勾选框里选中的也要一起带上 —— 这一格有**两个入口**写同一个段。
      //
      // 2026-09-03 自查发现：这里原来只取手填框的值就整份覆盖 S.forced，
      // 于是「勾了目录里的 3 个，再手填 1 个」的结果是 S.forced 只剩那 1 个，
      // 而 3 个勾选框在界面上还勾着 —— 又一处「界面勾着、实际没接管」。
      // 反方向（.cm 处理器）一直是合并的，两处不对称正是它没被发现的原因。
      const chosen = tr
        ? $$('.cm', tr).filter((x) => x.checked).map((x) => x.value) : [];
      const known = new Set(tr ? $$('.cm', tr).map((x) => x.value) : []);
      const typed = fmi.value.split(',').map((x) => x.trim())
        .filter((x) => x && !known.has(x));
      const list = chosen.concat(typed);
      // 手填里协议层就不成立的项，当场标出来 —— 后端会丢掉它们并给警告，
      // 但那要等一次 /api/plan 往返；输入框旁边即时提示更直接。
      //
      // 判据用 protoOk（协议层）而不是 famOk（工具选型偏好）：四族之外的
      // 模型在 compat 段完全合法 —— 实测 romeo 唯一验证过的就是
      // grok-4.6。用 famOk 会把它标成红的，而它恰恰是该写进去的那一个。
      const bad = typed.filter((m) => !protoOk(sc, m));
      const off = typed.filter((m) => protoOk(sc, m) && !famOk(sc, m));
      const box = tr && tr.querySelector('.fmhint');
      if (box) {
        box.innerHTML = bad.length
          ? `<span class="warn">${esc(bad.join(', '))} 在本段协议层不成立`
            + `（${sc === 'gemini-api-key' ? 'gemini 段只收 *-pro 且版本 >= 2.5'
              : (SECTION_FAMILY[sc] ? SECTION_FAMILY[sc] + ' 段只收该族'
                : '非对话模型四段都不收')}），提交时会被丢弃</span>`
          : (off.length
            ? `<span class="hint">${esc(off.join(', '))} 不在四族清单里，`
              + `但 compat 段走 /chat/completions、CPA 不校验模型名 —— `
              + `会按你的指定写入，能不能用取决于上游</span>`
            : '');
      }
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
      const h = inp.dataset.rid, s = inp.dataset.sec;
      S.overrides[h] = S.overrides[h] || {};
      S.overrides[h][s] = Object.assign(S.overrides[h][s] || {},
        { priority: parseInt(inp.value, 10) });
      refreshPlan(true);
      return;
    }
    const sel = e.target.closest('.sel');
    if (sel) {
      // 勾选不再有任何前置条件 —— 勾了就是勾了。
      //
      // 这里曾拦「没模型不让勾」。后端补了种子兜底后空清单不可能出现，
      // 而拦截的副作用是操作员点了没反应，只能从提示文字反推为什么。
      // 模型清单的可信度由 model_source 在方案里标注（实测/目录/手填/猜测），
      // 那是「看得见的告知」，比「点不动的勾选框」有用。
      const key = pk(sel.dataset.rid, sel.dataset.sec);
      if (!S.picks) {
        S.picks = new Set($$('#results .sel').filter((x) => x.checked)
          .map((x) => pk(x.dataset.rid, x.dataset.sec)));
      }
      if (sel.checked) {
        // 2026-09-26 删掉这里的「档位互斥」块。它从来没有执行过，而且
        // 一旦被「修好」就会造成数据损坏，两条理由都成立：
        //
        // 一、彻底的死代码，三处都错：
        //    · 读 `r.row.rid`，而 siteCard 写的是 `r.row.line_no`
        //      —— currentResult 恒为 undefined
        //    · 按 '\t' 切 pick key，而 pk() 用 '\u0000' 连接
        //      —— pkRid / pkSec 恒为 undefined
        //    · 读 `suggested_priority`，全仓（server.py、cpa_probe/、tests/）
        //      没有任何地方产出这个字段 —— currentTier 恒为 undefined
        //    于是 `if (currentTier != null)` 永远为假，整块从不进入。
        //
        // 二、就算字段名全对，这个行为本身是**反需求**的：它会在勾选一个
        //    段时取消同站其它档位的勾选。而产品规则要的恰恰相反 ——
        //    同一类型同一域名的所有 Key 共享同一个 priority，不同段之间
        //    本来就应该各自独立勾选（一个站的 claude 段和 codex 段是两条
        //    互不相干的上游）。档位由后端 assign_priorities 统一分配，
        //    不该由前端的勾选动作反向干预。
        S.picks.add(key);
      } else {
        S.picks.delete(key);
      }
      syncPickUI();
      // 勾选后必须重算 —— 后端只为**已勾选**的段生成方案（/api/plan 收
      // body.selected），未勾的段不在返回里，于是 priority 栏一直停在
      // placeholder「待定」、系统建议停在「勾选后计算」。
      // 现场反馈正是这个：勾上了还是待定，而 priority 会写进 config.yaml，
      // 「待定」是绝对不能出现的。
      schedulePlanRefresh();
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

  // 一键导出。走 fetch 而不是裸 <a href> —— 端点要 Bearer token，
  // 裸链接带不上；把 token 拼进 query 又会进浏览器历史。
  //
  // 落点是浏览器的下载目录，不是服务端能指定的路径：这个服务通常跑在
  // VPS 容器里，它写得到的「桌面」是容器里的，不是你面前这台机器的。
  // 想直接落桌面就把浏览器下载目录设成桌面。
  const be = $('#btnexport');
  if (be) {
    be.onclick = async () => {
      if (!S.jobId) { alert('还没有探测任务'); return; }
      const old = be.textContent;
      be.disabled = true; be.textContent = '导出中…';
      try {
        // 全文唯一绕过 api() 的请求 —— 它要的是 blob 而不是 JSON。
        // 2026-10-01 补上 api() 已有的两样保护：超时 + abort，以及把
        // 浏览器原文 `Failed to fetch` 换成人能看懂的话。原来二者都没有：
        // 导出端点要序列化整个任务日志（全量重探可达数 MB），网关慢一点
        // 就永久挂着，而 `alert(e.message)` 会把 `Failed to fetch` 原样弹出，
        // api() 为此专门做的文案改写在这条路上完全无效。
        // 截止时间要盖住**整次导出**，包括读 body 那一段。
        // 2026-10-01 二次修：上一版把 clearTimeout 放在只包 fetch() 的
        // finally 里 —— 响应头一到就把定时器清了，而导出端点正是「头很快
        // 回、body 慢慢流」的形状（服务端边序列化边写）。于是 `r.blob()`
        // 在网关半死时永久挂着，按钮停在「导出中…」且 disabled，和没有
        // 超时完全一样。api() 为同一个坑做过同样的修（见 tests/
        // test_web_runtime.test.js 第一项），这条路当时漏了。
        const ac = typeof AbortController !== 'undefined' ? new AbortController() : null;
        const tm = ac ? setTimeout(() => ac.abort(), 120000) : null;
        let r;
        let blob;
        try {
          r = await fetch(`/api/export/${encodeURIComponent(S.jobId)}`,
            { headers: { Authorization: 'Bearer ' + S.token },
              signal: ac ? ac.signal : undefined });
          if (!r.ok) throw new Error(`导出失败 ${r.status}`);
          blob = await r.blob();
        } catch (err) {
          if (err && err.name === 'AbortError') {
            throw new Error('导出超时（120 秒）—— 日志太大或网关慢。任务仍在服务端，可稍后重试。');
          }
          // `导出失败 NNN` 是上面自己抛的，原样传出去，别被套成网络错误。
          if (err && /^导出失败 /.test(err.message || '')) throw err;
          throw new Error('导出请求没能发出去（网络或网关中断）—— 页面没坏，重试即可。');
        } finally {
          if (tm) clearTimeout(tm);
        }
        const cd = r.headers.get('Content-Disposition') || '';
        const m = /filename="([^"]+)"/.exec(cd);
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = m ? m[1] : 'cpa-probe.txt';
        document.body.appendChild(a); a.click(); a.remove();
        // 立刻 revoke 会让部分浏览器拿不到数据 —— 给它一拍
        setTimeout(() => URL.revokeObjectURL(url), 4000);
        be.textContent = '已导出';
        setTimeout(() => { be.textContent = old; }, 2000);
      } catch (e) {
        alert(e.message);
        be.textContent = old;
      } finally {
        be.disabled = false;
      }
    };
  }

  $('#pickrec').onclick = () => { applyPickPreset('rec'); };
  $('#pickall').onclick = () => { applyPickPreset('all'); };
  $('#picknone').onclick = () => { applyPickPreset('none'); };
}

function applyPickPreset(mode) {
  if (!S.plans) {
    // 静默 return 等于「点了没反应」（2026-10-01 修）。
    // --------------------------------------------------
    // 首轮定档还没回、或定档失败时 `S.plans` 是 null，三个预设按钮照样
    // 可点，点下去这里直接 return —— 界面上毫无变化。这正是本函数末尾
    // 那段注释要修的「点下去界面没有任何回应」，只是当时只处理了成功
    // 路径，这条早退分支漏了。
    _pickWhy = S.jobId
      ? '方案还没算出来 —— 等③的定档完成后再用预设勾选；若③报了错，先按它的提示处理。'
      : '还没有探测结果 —— 先完成②的探测，③出方案后预设勾选才有东西可勾。';
    syncPickUI();
    return;
  }
  // 「全勾」就是全勾 —— 不看判定状态。很多站不给测活却能用，按判定筛
  // 等于把可用站扔掉。唯一不勾的是 duplicate（撞已有 Key，写进去是重复条目）。
  // 「只勾推荐项」保持按 recommended 筛，那才是让工具替你判断的入口。
  S.picks = new Set();
  const missing = [];
  let blocked = 0;
  S.plans.forEach((p) => {
    Object.entries(p.sections).forEach(([sec, sp]) => {
      if (sp.duplicate) return;
      // 落盘那层会拒掉的段不勾 —— 勾了也写不进，而界面上勾着就是在骗人。
      // 原因显示在「建议」列（recommend_reason）。
      if (sp.write_blocked) { blocked += 1; return; }
      if (mode === 'rec' && !sp.recommended) return;
      if (mode === 'none') return;
      // 不再按「有没有模型」拦 —— 后端现在给每段都算出确定清单
      // （实测 > 目录 > 手填 > 种子猜测），空清单已不可能出现。
      // 真出现了就是后端的缺陷，如实报出来而不是静默少勾。
      if (!(sp.models || []).length) {
        missing.push(`${p.host} ${SECTION_LABEL[sec] || sec}`);
        return;
      }
      S.picks.add(pk(p.line_no, sec));
    });
  });
  // 勾不满时必须说清差在哪 —— 只显示一个数字，操作员会以为是自己看错了。
  // 两种成因分开报：无模型是后端缺陷，不写入是设计如此。
  //
  // 2026-09-13：这段原来限定 `mode === 'all'`，而首轮走的是 'rec' ——
  // 于是现场那 17 个「判定可用、目录也返回了模型、却一个都没勾」的段
  // （foxtrot.example 的 codex/claude/compat）在界面上完全不可见。
  // 后端缺陷被藏起来，正是注释里说「如实报出来而不是静默少勾」要防的事。
  // 'none' 例外：那是用户主动全不选，报「差在哪」没有意义。
  //
  // 必须在 syncPickUI 之前算好：syncPickUI 是 #pickstat 的唯一渲染点，
  // 读的是 _pickWhy —— 晚一步就渲染上一轮的旧诊断。
  const why = [];
  if (mode !== 'none' && (missing.length || blocked)) {
    if (missing.length) {
      why.push(`${missing.length} 段异常无模型（后端缺陷，请报）：`
        + esc(missing.slice(0, 3).join('、'))
        + (missing.length > 3 ? ` 等 ${missing.length} 段` : ''));
    }
    if (blocked) {
      why.push(`${blocked} 段标为「不写入」（原本没配这一段且清单只是猜测 ——`
        + `手填真实模型即可放行）`);
    }
  }
  _pickWhy = why.join(' · ');
  syncPickUI();
  // 预设按钮也要重算 —— 与单个勾选同理：后端只为已勾选的段出方案，
  // 不重算的话「全勾选」之后 priority 栏还是 placeholder「待定」。
  schedulePlanRefresh();
  // 当前生效的是哪个预设 —— 三个按钮点下去界面没有任何回应，
  // 操作员分不清自己执行的是哪一个（现场反馈）。
  $$('#pickbtns button[data-mode]').forEach((b) => {
    b.classList.toggle('on', b.dataset.mode === mode);
  });
}

// 勾选变化后的方案重算。防抖 —— 「全勾选」会连发几百次 change 事件，
// 每次都打 /api/plan 会让后端串行排队、界面卡住。180ms 内的连续变化合并成
// 一次请求；这个值取自实测：人手连点最快约 120ms 一次，180 能合上，
// 又不至于让单次勾选感觉到延迟。
let _planTimer = null;
function schedulePlanRefresh() {
  invalidatePlanPreview();
  if (_planInFlight) _planRerun = true;
  if (_planTimer) clearTimeout(_planTimer);
  _planTimer = setTimeout(() => { _planTimer = null; refreshPlan(true); }, 180);
}

// 勾不满的成因（无模型 / 不写入）。applyPickPreset 算出来后存这里，
// 由 syncPickUI 统一渲染 —— 否则谁最后跑谁的文案胜出。
//
// 2026-09-13：这两个函数会互相覆盖 #pickstat。applyPickPreset 先写诊断，
// 紧接着 schedulePlanRefresh → refreshPlan → syncPickUI 用纯计数覆盖掉；
// 于是「17 段异常无模型」闪一下就没了。改成单一渲染点。
let _pickWhy = '';

function syncPickUI() {
  // 首轮定档尚未给默认选择；没有人工勾选时保持 null，不能提前改成空集合。
  if (!S.picks) {
    $('#btnplan').disabled = true;
    return;
  }
  $$('#results .sel').forEach((el) => {
    el.checked = S.picks.has(pk(el.dataset.rid, el.dataset.sec));
    const tr = el.closest('tr');
    if (tr) tr.classList.toggle('rec', el.checked);
  });
  const n = S.picks ? S.picks.size : 0;
  const head = n ? `已勾选 ${n} 项写入` : '未勾选任何项';
  const stat = $('#pickstat');
  if (stat) {
    stat.innerHTML = _pickWhy
      ? `${esc(head)} · <span class="warn">${_pickWhy}</span>`
      : esc(head);
  }
  $('#btnplan').disabled = n === 0;
}

// ── 方案 ──
// 单飞（2026-09-17）：同一时刻只允许一个 refreshPlan 在跑。
// 勾选变化每 180ms 防抖发一次，但轮询一次要几十秒 —— 期间再来的调用
// 不能再起一个后台任务（那会把任务表挤满、让正在轮询的那条被淘汰，
// 现场表现就是「定档轮询无响应」）。后到的调用只记一个「待重跑」标记，
// 当前这轮结束后再跑一次即可 —— 最新的勾选状态那时才是准的。
let _planInFlight = null;
let _planRerun = false;
function planInputKey() {
  return JSON.stringify({
    jobId: S.jobId, overrides: S.overrides, forced: S.forced,
    selected: S.picks ? [...S.picks].sort() : null,
    byScore: $('#o_probation') ? !$('#o_probation').checked : false,
  });
}

function invalidatePlanPreview() {
  if (S.previewPlanId && S.previewInputKey !== planInputKey()) {
    S.previewPlanId = '';
    S.previewInputKey = '';
    $('#btnapply').disabled = true;
  }
}

async function refreshPlan(silent) {
  invalidatePlanPreview();
  if (_planInFlight) { _planRerun = true; return _planInFlight; }
  // 所有等待者都等到补算结束，再拿同一份最新结果；不能在 finally 里另起
  // 一个无人等待的请求，否则可见 diff 与随后更新的 planId 会来自两份方案。
  _planInFlight = (async () => {
    let result;
    do {
      _planRerun = false;
      if (_planTimer) { clearTimeout(_planTimer); _planTimer = null; }
      result = await _refreshPlanOnce(silent);
      if (_planRerun) syncPickUI();
    } while (_planRerun);
    return result;
  })().finally(() => {
    _planInFlight = null;
    if (_planRerun) schedulePlanRefresh();
  });
  return _planInFlight;
}

async function _refreshPlanOnce(silent) {
  let d;
  const requestKey = planInputKey();
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

  // ── 异步定档（2026-09-17）──────────────────────────────────────
  // 原来同步 POST /api/plan 会阻塞 > 60s，触发 Cloudflare Free 的 100s 回源
  // 超时（524），priority 全停在「待定」占位符。
  // 新流程： POST → 立即返回 plan_task_id → 每 2s 轮询 /api/plan-status
  // 每个 HTTP 请求都 < 1s，CF 超时彻底无关。
  // ──────────────────────────────────────────────────────────
  let taskId = null;
  try {
    const init = await api('/api/plan', { method: 'POST', body });
    if (init && init.plan_task_id && init.state === 'running') {
      taskId = init.plan_task_id;
    } else {
      d = init;
    }
  } catch (e) {
    const msg = esc(e.message || '未知错误');
    const meta = $('#planmeta');
    if (meta) meta.innerHTML = `<div class="err">定档失败：${msg}</div>`;
    const stat = $('#pickstat');
    if (stat) {
      stat.innerHTML = `<span class="err">定档失败：${msg}</span>`
        + ` <span class="hint">priority 与建议栏保持占位符；重试或看容器日志</span>`;
    }
    return null;
  }

  if (taskId) {
    const stat = $('#pickstat');
    if (stat) stat.innerHTML = `<span class="hint">定档计算中…（后台运行，请稍候）</span>`;
    // 不设固定上限（2026-09-17）。原来 `i < 180`（360 秒）：173 站全量重探
    // 的定档实测可能超过它，到点就报「定档超时，请重试」—— 而后台任务其实
    // 还在正常跑，重试只会再起一个同样跑不完的任务。
    // 现在只认后端的 state：done / error 才退出；连续 15 次轮询拿不到响应
    // （30 秒没有任何可达性）才判断链路断了。任务本身在服务端有 TTL 兜底。
    let misses = 0;
    for (;;) {
      await new Promise((r) => setTimeout(r, 2000));
      let poll;
      try {
        poll = await api('/api/plan-status', {
          method: 'POST', body: { plan_task_id: taskId }, timeoutMs: 15000,
        });
        misses = 0;
      } catch (e) {
        // 按错误类型分别处理，不再把所有失败一律算成「无响应」
        // （2026-09-26）。原来这里 `catch (_)` 一刀切，于是：
        //   · nginx 限流回 429 —— 后台任务跑得好好的，界面却报链路已断
        //   · 任务被 LRU 淘汰回 404 —— 真实原因（任务没了）被吞掉，
        //     用户只看到「刷新页面后重试」，刷新后当然还是一样
        //   · token 失效回 401 —— 重试 15 次全是徒劳
        // /api/job 那条轮询早就用 classifyPollError 分类了（见 662），
        // 这条没用，两条轮询对同一种故障给出不同结论。现在统一。
        const cl = classifyPollError(e, misses);
        if (cl.kind === 'auth') {
          const meta = $('#planmeta');
          if (meta) meta.innerHTML = `<div class="err">登录已失效，请重新登录后再定档</div>`;
          if (stat) stat.innerHTML = `<span class="err">登录已失效 —— 重新登录</span>`;
          return null;
        }
        if (cl.kind === 'expired') {
          const meta = $('#planmeta');
          if (meta) {
            meta.innerHTML = `<div class="err">定档任务已不在服务端`
              + `（容器重启，或并发定档过多把它挤掉了）</div>`;
          }
          if (stat) {
            stat.innerHTML = `<span class="err">定档任务已过期 —— 点「生成写回方案」重新定档</span>`;
          }
          return null;
        }
        // 限流不计入 misses：后台任务没事，是网关在挡。按 Retry-After 退避。
        if (cl.kind === 'throttled') {
          if (stat) {
            stat.innerHTML = `<span class="hint">定档计算中…（网关限流，`
              + `${Math.round(cl.wait / 1000)}s 后重试）</span>`;
          }
          await new Promise((r) => setTimeout(r, cl.wait));
          continue;
        }
        if (++misses >= 15) {
          const meta = $('#planmeta');
          if (meta) {
            meta.innerHTML = `<div class="err">定档轮询连续 15 次无响应`
              + `（约 ${misses * 2}–${misses * 17} 秒），链路可能已断：`
              + `${esc(e.message || '连接中断')}</div>`;
          }
          if (stat) {
            stat.innerHTML = `<span class="err">定档轮询无响应 —— 刷新页面后重试</span>`;
          }
          return null;
        }
        if (cl.wait) await new Promise((r) => setTimeout(r, cl.wait));
        continue;
      }
      if (!poll) continue;
      if (poll.state === 'done') { d = poll.result; break; }
      if (poll.state === 'error') {
        const msg = esc(poll.error || '定档后台异常');
        const meta = $('#planmeta');
        if (meta) meta.innerHTML = `<div class="err">定档失败：${msg}</div>`;
        if (stat) {
          stat.innerHTML = `<span class="err">定档失败：${msg}</span>`
            + ` <span class="hint">priority 与建议栏保持占位符；重试或看容器日志</span>`;
        }
        return null;
      }
      if (stat) stat.innerHTML = `<span class="hint">定档计算中… ${Math.round(poll.elapsed || 0)}s</span>`;
    }
    if (!d) {
      const meta = $('#planmeta');
      if (meta) meta.innerHTML = `<div class="err">定档超时（>360s）</div>`;
      const stat2 = $('#pickstat');
      if (stat2) stat2.innerHTML = `<span class="err">定档超时，请重试</span>`;
      return null;
    }
  }

  // 形状闸（2026-09-18）
  // ------------------
  // 下面整段都在裸取 `d.plan_id` / `d.plans` / `d.diffs.reduce` / `p.skipped`。
  // 后端只要回一份形状不对的体（现场原因：keep-alive 让 `/api/plan-status`
  // 的 `{state:"running"}` 快照被当成定档结果缓存，再被 `/api/plan` 重放），
  // 这里就抛 TypeError 变成 unhandled rejection —— 界面没有任何错误提示，
  // 整张表永久停在占位符。宁可显式报错，也不要静默空白。
  if (!d || typeof d !== 'object' || !Array.isArray(d.plans)) {
    const shape = d && typeof d === 'object'
      ? Object.keys(d).slice(0, 6).join(',') : typeof d;
    const meta = $('#planmeta');
    if (meta) meta.innerHTML = `<div class="err">定档返回的数据形状不对（${esc(shape)}）</div>`;
    const stat3 = $('#pickstat');
    if (stat3) {
      stat3.innerHTML = `<span class="err">定档结果异常 —— 请重试；`
        + `若反复出现，把容器日志里的 error_ref 发出来</span>`;
    }
    return null;
  }

  if (requestKey !== planInputKey()) {
    _planRerun = true;
    return null;
  }
  S.planId = d.plan_id; S.plans = d.plans;
  S.planInputKey = requestKey;

  // 首次：按系统建议预勾选。
  //
  // 必须先回填再重跑（2026-09-13）：原来这里直接 `return refreshPlan(true)`，
  // 于是首轮这一帧的 d.plans 被丢掉 —— 而下面的回填循环在 return 之后，
  // 首轮永远到不了。递归的第二帧只要失败（且它传的就是 silent=true），
  // 整张表就永久停在「待定 / 计算中…」。
  const firstPass = S.picks === null;
  fillPlanIntoRows(d);
  if (firstPass) {
    applyPickPreset('rec');
    // 预勾选变了选择集，标记重跑；由单飞包装在本轮结束后执行
    _planRerun = true;
    return d;
  }
  syncPickUI();
  return d;
}
function planWarnings(d) {
  const ws = Array.isArray(d.warnings) ? d.warnings : [];
  if (!ws.length) return '';
  return `<div class="note w"><b>定档提示 ${ws.length} 条</b>
    —— 影响的是站与站的先后，不影响单个条目能否用
    <div class="mlist">${ws.map((w) => `· ${esc(w)}`).join('<br>')}</div></div>`;
}

$('#btnplan').onclick = async () => {
  const d = await refreshPlan(false);
  if (!d) return;
  if (d.plan_id !== S.planId || S.planInputKey !== planInputKey()) {
    $('#planmeta').textContent = '输入已更新，请重新生成写回预览。';
    $('#btnapply').disabled = true;
    return;
  }
  S.previewPlanId = d.plan_id;
  S.previewInputKey = S.planInputKey;

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
    ${planWarnings(d)}
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
  S.previewPlanId = ''; S.previewInputKey = '';
  $('#btnapply').disabled = true;
  $('#p4').hidden = true; $('#p3').hidden = false; step(3);
};

// ── 写回 ──
// 写回收尾的轮询。落盘那一步已经在 /api/apply 里同步完成 —— 这里只等
// 「重载 + 端到端验证」，并把阶段与验证进度显示出来。
//
// 与步骤②的探测轮询同一套思路：断连要重试，不能因为一次网络抖动就让用户
// 以为写回失败（写盘早就成了）。
// ── headers 就地编辑器 ──
// 2026-09-19 恢复：与 impactTable 一样，本函数在提交 470d04a 里被**误删**，
// 而调用点（fillPlanIntoRows 结尾的 `bindHeaderEditor(wb, ...)`）留着。
// 影响面比 impactTable 稍轻（impactTable 先抛，所以这个从来没被执行到），
// 但两个都必须补回来 —— 只补一个的话，下一个抛的就是它。
function bindHeaderEditor(wb, rid, sec) {
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
    S.overrides[rid] = S.overrides[rid] || {};
    S.overrides[rid][sec] = S.overrides[rid][sec] || {};
    S.overrides[rid][sec].headers = h;
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
    timer = setTimeout(() => { S.keepOpen = pk(rid, sec); refreshPlan(); }, 700);
  });
  // 失焦立即结算 —— 用户已经改完了，不该再等那 700ms
  det.addEventListener('focusout', () => {
    if (!timer) return;
    clearTimeout(timer); timer = null;
    S.keepOpen = pk(rid, sec);
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
      S.keepOpen = pk(rid, sec);
      refreshPlan();
      return;
    }
    if (rst) {
      e.preventDefault();
      // 删掉这个键而不是置空 —— 「没改过」与「改成空」是两件事，
      // 后者应当真的写出一个空 headers。
      if (S.overrides[rid] && S.overrides[rid][sec]) {
        delete S.overrides[rid][sec].headers;
      }
      refreshPlan();
    }
  });
}

// ── 影响面表格 ──
// `sp.impacts` 是每个模型相对**现有顶层**的判定（抢走 / 同层 / 低于），
// 以及被挡在其后的站列表。这张表回答「把这一段写成这个档位，会动到谁」——
// 是操作员决定勾不勾这个段的唯一依据。
//
// 2026-09-19 恢复：本函数在提交 470d04a（异步定档改造）里被**误删**，
// 而它的调用点（fillPlanIntoRows 里的 `+ impactTable(sp)`）留着。
// 后果是定档结果一回来就抛
// `ReferenceError: impactTable is not defined`，整条 fillPlanIntoRows
// 中断 —— 界面停在「定档计算中…」，推荐勾选一个都不勾，用户完全看不出
// 原因（控制台里才有那一行红字）。恢复时保持原实现不变。
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

function fillPlanIntoRows(d) {
  if (!d || !Array.isArray(d.plans)) return;
  d.plans.forEach((p) => {
    Object.entries(p.sections).forEach(([sec, sp]) => {
      const tr = document.querySelector(
        `#results tr[data-rid="${cssq(p.line_no)}"][data-sec="${cssq(sec)}"]:not(.wrow)`);
      if (!tr) return;
      const inp = tr.querySelector('.pi');
      const override = (S.overrides[p.line_no] || {})[sec] || {};
      const manual = Number.isFinite(override.priority) && override.priority > 0;
      // 自动值随方案更新；明确的手工覆盖保留。无效值清空，不沿用上一轮档位。
      if (inp && !manual) inp.value = sp.priority > 0 ? String(sp.priority) : '';

      // 目录读不到的段：把后端方案里的模型填成勾选框。
      //
      // 2026-09-02 现场（截图1）：后端已按「当前市面最新」填了 6 个模型、
      // 警告文本里也列着那 6 个名字，而那一格只有一个空的手填框 —— 它从
      // S.forced 取值，而 S.forced 此刻是空的。用户看到空白，且提交时读的
      // 正是 S.forced，所以那个段勾上也写不进任何模型。
      //
      // 只在容器空时填一次：用户改过之后不能被覆盖（与目录分支同一条规则）。
      const fb = tr.querySelector('.cats.fallback');
      if (fb && !fb.querySelector('.cm') && (sp.models || []).length) {
        const rec = (S.forced[p.line_no] || {})[sec];
        const on = new Set(rec !== undefined ? rec : sp.models);
        fb.innerHTML = sp.models.map((m) => `
          <label class="catpick"><input type="checkbox" class="cm"
            data-rid="${esc(p.line_no)}" data-host="${esc(p.host)}"
            data-sec="${esc(sec)}"
            value="${esc(m)}"${on.has(m) ? ' checked' : ''}>${esc(m)}</label>`).join('');
        const tools = tr.querySelector('.mtools.fallback-tools');
        if (tools) {
          tools.hidden = false;
          const n = tools.querySelector('.cmn');
          if (n) n.textContent = String([...on].filter((m) => sp.models.includes(m)).length);
        }
        // 立刻回写 S.forced —— 提交时读的是它，不读 DOM。
        // 不写的话「界面上勾着、实际没接管」，正是上一轮修的症状。
        //
        // 但 **seed 例外**（2026-09-03）：那份清单是工具猜的，回写会让它在
        // 后端被当成手填（forced 非空即走 manual 分支），于是
        //   · 徽标从「猜测」变「手填」，界面不再提示这批名字没有依据
        //   · 跨段新增那道闸按 model_source 判，manual 放行 —— 猜测清单
        //     因此能凭空新增条目，正是 121 条目变 246 那次事故的路径
        // 不回写也不会丢：后端本来就会写它自己算出的 sp.models，界面显示的
        // 就是同一份。用户真的取消勾选时 change 事件会写 S.forced（那时
        // 记成手填是对的 —— 操作员显式做了决定）。
        // prior 与 seed 同办（2026-09-06）：那份清单也不是操作员填的，
        // 而是从原 config.yaml 搬回来的。回写会让后端当成手填，
        // 徽标与跨段闸都跟着变 —— 与 seed 完全同一个坑。
        //
        // 2026-09-26：catalog / probed 也同办 —— **任何**来源都不回写。
        // 这份清单是后端方案算的，后端提交时本来就写它；回写只会让下一轮
        // 定档把它当手填，绕过证据检查（见渲染不可用行那里的说明）。
      }
      // 没有人工接管记录时，勾选状态一律同步成后端方案（唯一真源）。
      //
      // 目录分支的勾选框是首次渲染时按前端 staleCheck 预勾的，只是占位显示；
      // 方案到了之后若不同步，界面勾着的与实际写的会是两份清单。方案里有、
      // 目录里没有的名字（市面补齐的同代变体）补成勾选项，写什么就显示什么。
      if ((S.forced[p.line_no] || {})[sec] === undefined && (sp.models || []).length) {
        const want = new Set(sp.models);
        const boxes = $$('.cm', tr);
        boxes.forEach((x) => { x.checked = want.has(x.value); });
        const have = new Set(boxes.map((x) => x.value));
        const host = tr.querySelector('.cats');
        const missing = sp.models.filter((m) => !have.has(m));
        if (host && missing.length) {
          host.insertAdjacentHTML('beforeend', missing.map((m) => `
            <label class="catpick" title="方案补齐：站方目录没报，按同代市面清单补上">
              <input type="checkbox" class="cm" checked
                data-rid="${esc(p.line_no)}" data-host="${esc(p.host)}"
                data-sec="${esc(sec)}" value="${esc(m)}">${esc(m)}</label>`).join(''));
        }
        const n = tr.querySelector('.cmn');
        if (n) n.textContent = String($$('.cm', tr).filter((x) => x.checked).length);
      }
      // weight: 0 必须显眼 —— 全量重探会如实把原值搬回来。
      // weight: 0 必须显眼 —— 全量重探会如实把原值搬回来。
      //
      // 但它的**含义取决于 routing.strategy**（2026-09-02 核实 CPA 源码）：
      // 只有 weighted-round-robin 会调 positiveWeightAuths 把零权重凭据
      // 整个剔除（selector.go:650 → 637-644）；默认的 round-robin 与
      // fill-first 根本不读 weight，那时这个站照常参与轮询。
      // 说成「一定不参与调度」在后两种策略下是错的。
      //
      // 措辞里**不提 CPAMP 面板怎么显示**（2026-09-03 核实 CPAMP 源码后删，
      // 2026-09-04 复核仍然成立）：上一版写「CPAMP 面板显示为『未启用』」，
      // 那是编的。CPAMP 里与 weight 有关的只有凭据编辑表单的提示文字
      // （i18n `accounts.config_weight_hint`：「仅在加权轮询策略下生效；
      // 留空使用默认权重 1，0 会将该凭证排除出加权调度」），没有任何列表视图
      // 按 weight 渲染启用状态。
      //
      // CPAMP 那个「已停用」徽标（`ai_providers.config_disabled_badge`，
      // ProviderDetailDrawer.tsx:229）读的是 `row.enabled`，而它的两个来源
      // （rowData.ts:114 / :152）分别是「`excluded-models` 含 `*`」与
      // 「compat 的 `disabled: true`」—— 与 weight 无关。
      // `dashboard.health_status_disabled` 则只出现在采集器与版本卡片上。
      if (sp.weight === 0) {
        const pc = tr.querySelector('.prio');
        if (pc && !pc.querySelector('.w0')) {
          const excl = S.ctx && S.ctx.weight_zero_excludes;
          const strat = (S.ctx && S.ctx.routing_strategy) || '未配置（默认 round-robin）';
          pc.insertAdjacentHTML('beforeend', excl
            ? '<div class="warn b w0">weight: 0 —— 原配置已把它逐出调度池，'
              + '写回后仍不参与轮询。要解封请手工删掉这一行</div>'
            : `<div class="warn w0">weight: 0 —— 原值搬回。当前
                <code>routing.strategy = ${esc(strat)}</code> <b>不读 weight</b>，
                所以这个站仍会正常参与轮询。
                只有改成 <code>weighted-round-robin</code> 它才真被逐出</div>`);
        }
      }
      // 模型清单的来源 —— 判死段现在也有确定清单，但那清单可能只是种子
      // 猜测。不标出来的话，「猜的」和「实测跑通的」在界面上没有区别。
      //
      // 这一格每轮 refreshPlan 都重建（不是 insertAdjacentHTML 追加）——
      // 追加式的写法在 model_source 变化时会留着上一轮的徽标：手填之后
      // 「猜测」与「手填」两个徽标并列，看不出现在到底按哪份清单写。
      const ml = tr.querySelector('.mlist');
      if (ml) {
        const st = SRC_TAG[sp.model_source];
        const bits = [];
        if (st) bits.push(`<span class="pill ${st.c} srctag">${st.t}</span>`);
        // 新增段：这一段原本不在 config.yaml 里。它改变的是条目数而不只是
        // 某个字段，diff 里不显眼，所以在行内标出来。
        if (sp.new_section && !sp.write_blocked) {
          bits.push('<span class="pill p-i">新增段</span>');
        }
        let extra = '';
        if (sp.model_source === 'seed') {
          extra = '<div class="hint">种子兜底：站方目录也没报模型，'
            + '这几个名字是本工具猜的，勾选前请确认</div>';
        }
        if (sp.model_source === 'prior') {
          extra = '<div class="hint">沿用原清单：本次没探通、目录也读不到，'
            + '这几个名字是原 config.yaml 里已经写着的（先前一轮的实测沉淀），'
            + '没有被工具猜的「市面最新」覆盖 —— 但本次未验证</div>';
        }
        if (sp.new_section && !sp.write_blocked) {
          extra += '<div class="hint">原 config.yaml 里这个凭据没配这一段 ——'
            + '本次探测发现它也能用，将作为<b>新条目</b>写入，'
            + '并已计入定档与影响面</div>';
        }
        ml.innerHTML = bits.join(' ') + (bits.length ? ' ' : '') + extra;
      }
      const rsn = tr.querySelector('.rsn');
      if (rsn) {
        // 三态要与落盘一致：write_blocked 非空时这一段不会写入，界面必须
        // 说「不写入」而不是「建议写入」（上一版那道闸只在写盘层，界面
        // 照「没有闸」渲染，勾了写不进 —— 2026-09-03 现场）。
        const cls = sp.write_blocked ? 'p-m'
          : (sp.recommended ? 'p-ok' : (sp.writable ? 'p-w' : 'p-m'));
        const tag = sp.write_blocked ? '不写入'
          : (sp.recommended ? '建议写入'
            : (sp.writable ? '需人工确认' : '不可写入'));
        // score 一直没显示，而关掉「试用期定档」后 priority 就是按它算的 ——
        // 看不到分数等于那个开关的依据不可见。
        const sc = (sp.score != null)
          ? `<span class="hint"> · 得分 ${sp.score}</span>` : '';
        rsn.innerHTML = `<span class="pill ${cls}">${tag}</span>${sc}
          <div class="hint" style="margin-top:5px">${esc(sp.recommend_reason)}</div>
          <div class="hint">${esc(sp.priority_reason)}</div>`;
      }
      const wrow = document.querySelector(
        `#results tr.wrow[data-rid="${cssq(p.line_no)}"][data-sec="${cssq(sec)}"]`);
      if (wrow) {
        const wb = wrow.querySelector('.wbox');
        const html = (sp.duplicate
          ? `<div class="warn b">已存在：${esc(sp.duplicate_note)}</div>` : '')
          + sp.warnings.map((w) =>
            `<div class="warn${/抢走|换模/.test(w) ? ' b' : ''}">${esc(w)}</div>`).join('')
          + impactTable(sp)
          + headerEditor(p.line_no, sec, sp);
        wb.innerHTML = html;
        // headers 编辑器一直在，所以 wrow 不再按 html 空否决定显隐 ——
        // 它现在总有内容。
        wrow.hidden = false;
        bindHeaderEditor(wb, p.line_no, sec);
        // 重渲染会把 <details> 的展开态清掉。刚才在编辑哪一段就把它重新展开 ——
        // 否则每次防抖结算完编辑器都自己收起来，等于没法连续改。
        if (S.keepOpen === pk(p.line_no, sec)) {
          const det = wb.querySelector('.hedit');
          if (det) det.open = true;
        }
      }
    });
  });
}
const cssq = (s) => String(s).replace(/["\\]/g, '\\$&');

// ── headers 手工编辑 ──
// 后端 _api_plan 早就认 overrides.headers，但前端一直只能整段接受探测结果。
// 两种情形都真实存在：探测判门禁但你从别处知道正确的头；探测给出的头多了一项
// （漂移检测就抓到过无条件发 oauth-2025-04-20 那一处）。
//
// 最要紧的设计点：**改动后必须标「未验证」**。探测是用原来那套跑通的，改了
// 就没测过了 —— 界面仍显示「✓ 可用」会让人以为改后的配置也验证过。
function headerEditor(rid, sec, sp) {
  const ov = ((S.overrides[rid] || {})[sec] || {});
  const edited = Object.prototype.hasOwnProperty.call(ov, 'headers');
  const cur = edited ? ov.headers : (sp.headers || {});
  const keys = Object.keys(cur);

  const rows = keys.map((k, i) => hdrRow(k, cur[k], i)).join('');
  const warn = edited
    ? `<div class="warn b">headers 已手工改过 —— 这一段的「已验证」不再成立。
         探测是用改动前那套跑通的。</div>`
    : '';

  return `<details class="hedit" data-rid="${esc(rid)}" data-sec="${esc(sec)}">
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

/* 弱证据段的人工确认（2026-09-29 补）
   ==================================
   服务端 `_api_apply` 在方案里含 weak 段（seed 填充、站方目录没报过、原
   config.yaml 里也没有这一族）时返回 409 `weak_evidence_unconfirmed`，
   要求带 `confirm_weak=true` 再来一次。这个页面此前不认这个码，于是含猜测
   清单的方案点「确认写回」只拿到一句「（HTTP 409）」—— 闸想要的那次确认
   根本没机会发生，看起来就是「写回点不动」。

   刻意用原生 confirm 而不是自绘弹层：站名与模型名都是外部数据，走
   textContent 语义的原生对话框，不给 innerHTML 留注入面（本文件 1485 行的
   全量重探确认也是同一个做法）。 */
function confirmWeakEvidence(payload) {
  const rows = Array.isArray(payload.weak_sections) ? payload.weak_sections : [];
  const total = payload.weak_total || rows.length;
  // 同一个站的多段并成一行：163 段全灭那种规模，逐段列会把对话框撑爆，
  // 而操作员真正要判断的是「这个站是不是整个都在猜」。
  const byHost = new Map();
  for (const r of rows) {
    const host = r.host || '(未知站)';
    if (!byHost.has(host)) byHost.set(host, []);
    byHost.get(host).push(r);
  }
  const lines = [];
  let shown = 0;
  for (const [host, secs] of byHost) {
    if (shown >= 12) { lines.push(`… 另有 ${byHost.size - shown} 个站未列出`); break; }
    const detail = secs.map((s) => {
      const names = (s.models || []).join(', ');
      const more = s.more ? ` +${s.more}` : '';
      return `${s.section}: ${names}${more}`;
    }).join('\n    ');
    lines.push(`· ${host}\n    ${detail}`);
    shown += 1;
  }
  const msg = `有 ${total} 个段的模型清单是「市面猜测」——\n`
    + `站方 /models 目录没报过这一族，原 config.yaml 里也没有。\n\n`
    + lines.join('\n')
    + `\n\n这些名字写进 config.yaml 后，若该站其实没有，`
    + `CPA 每次轮到都会失败。\n\n`
    + `确定写入？（取消则不写，可回上一步逐段取消勾选）`;
  return Promise.resolve(confirm(msg));
}

async function pollApply(taskId, first, box = $('#applymsg')) {
  let last = first || {};
  let fails = 0;
  let attempt = 0;
  for (;;) {
    await new Promise((r) => setTimeout(r, 900));
    let st;
    try {
      st = await api(`/api/apply-status/${encodeURIComponent(taskId)}`);
      fails = 0;
      attempt = 0;
    } catch (e) {
      // 与定档轮询用同一个分类器（2026-10-01）。
      // ------------------------------------
      // 原来这里只有一个 `fails` 计数器：404/410/401 与网络抖动走同一条
      // 路，白重试 20 次 × 900ms ≈ 18 秒才报「结果未知」。而这三种的处置
      // 完全不同 —— 任务已不在服务端（重启/淘汰）时重试一万次也没用，
      // 该立刻告诉操作员「服务重启过，这次写回的结果要去配置里核对」。
      // 定档那条轮询 2026-09 就改用 classifyPollError 了，这条漏了。
      const cls = classifyPollError(e, attempt);
      const wrote = last.local_written === true;
      const head = wrote
        ? '已确认本地写盘，但后续结果未知'
        : '写回结果未知，无法确认配置是否写入';
      if (cls.kind === 'expired') {
        box.innerHTML = `<span style="color:var(--${wrote ? 'warn' : 'bad'})">
          <b>${head}</b> —— 任务 ${esc(taskId)} 已不在服务端
          （服务重启或任务仓淘汰）。请刷新页面，并到「参考 · 当前档位谱」
          核对这次写回是否已生效，不要直接重复提交。</span>`;
        return null;
      }
      if (cls.kind === 'auth') {
        box.innerHTML = `<span style="color:var(--bad)"><b>${head}</b>
          —— 登录已失效，请重新进入后再核对任务 ${esc(taskId)} 的结果。</span>`;
        return null;
      }
      attempt += 1;
      if (cls.kind === 'throttled') {
        // 限流不是「无响应」，不该计进失败数 —— 否则一次网关限流就能
        // 把一次正常的写回判成结果未知。
        box.innerHTML = `<span class="spin"></span> 网关限流，
          <span class="hint">${Math.round(cls.wait / 1000)} 秒后重试</span>`;
        await new Promise((r) => setTimeout(r, cls.wait));
        continue;
      }
      fails += 1;
      if (fails >= 20) {
        box.innerHTML = `<span style="color:var(--${wrote ? 'warn' : 'bad'})">
          <b>${head}</b>
          —— 轮询中断（${esc(e.message)}）。任务 ${esc(taskId)} 的状态尚未确认，
          请恢复状态查询后再操作，不要重复提交。</span>`;
        return null;
      }
      if (cls.wait) await new Promise((r) => setTimeout(r, cls.wait));
      continue;
    }
    if (!st || !['running', 'done', 'error'].includes(st.state)) {
      box.innerHTML = '<span style="color:var(--bad)">写回结果未知：任务状态响应无效，未确认完成。</span>';
      return null;
    }
    last = { ...last, ...st };
    const pct = st.verify_total
      ? Math.round(st.verify_done / st.verify_total * 100) : 0;
    const bar = st.verify_total
      ? '█'.repeat(Math.round(pct / 5)) + '░'.repeat(20 - Math.round(pct / 5))
      : '';
    box.innerHTML = `<span class="spin"></span> ${esc(st.stage || '收尾中')}`
      + (st.verify_total
        ? ` <span class="hint">${bar} 验证 ${st.verify_done}/${st.verify_total}
            (${pct}%)</span>` : '')
      + ` <span class="hint">· ${st.elapsed}s</span>`;
    if (st.state === 'error') {
      // 「写没写盘」只认后端的 local_written，不要在前端猜（2026-09-25）。
      //
      // 现场故障：④ 面板的「CPA 地址」填成 `cli-proxy-api:8317`（少了
      // http://），后端在**写盘之前**就因推送目标被拒而抛错，这里却写死
      // 「配置已写盘」，并且把 st 合并后返回；调用方只判 `if (!d)`，判不住，
      // 于是继续渲染「✓ 已写回」+ 一排空的 written/backup/diffs。
      // 用户看到的是「写回成功但全是空白」，真相是一个字节都没写。
      //
      // 2026-09-27 再改措辞：写盘成功时不要用「收尾出错」打头。那一轮实跑
      // 里配置已写盘、CPA PUT 200 读回一致，只有运行时路由验证没过，顶部
      // 却是红字「收尾出错」，而下方面板同时写「✓ 已写回 / CPA 已重载」——
      // 同一屏两句话互相打脸。现在按「已写盘 / 未写盘」给两种完全不同的
      // 标题与颜色：前者是黄色的「部分完成」，后者才是红色的失败。
      // 具体没过的是哪几条由后端的 _verify_failure_summary 写进 st.error。
      const wrote = last.local_written === true;
      box.innerHTML = wrote
        ? `<span style="color:var(--warn)">
            <b>已写盘，但最后一步没走完</b> —— config.yaml 已更新，
            下面这一层需要你决定要不要跟进：
            <pre>${esc(st.error || '')}</pre></span>`
        : `<span style="color:var(--bad)">
            <b>${last.local_written === false ? '写回失败 —— 后端确认配置未写盘' : '写回结果未知 —— 缺少写盘回执'}</b>：
            <pre>${esc(st.error || '')}</pre></span>`;
      return wrote ? last : null;
    }
    if (st.state === 'done') return last;
  }
}

async function awaitApplyReceipt(first, box) {
  if (!first || typeof first !== 'object' || Array.isArray(first)) {
    box.innerHTML = '<span style="color:var(--bad)">写回结果未知：后端未返回有效回执。</span>';
    return null;
  }
  const result = first.task_id && !['done', 'error'].includes(first.state)
    ? await pollApply(first.task_id, first, box) : first;
  if (!result) return null;
  if (result.local_written !== true || result.state === 'running') {
    const failed = result.state === 'error' && result.local_written === false;
    box.innerHTML = `<span style="color:var(--bad)">${failed
      ? '写回失败，后端确认配置未写入' : '写回结果未知，尚未确认完成'}：
      ${esc(result.error || result.reload_msg || '缺少完成的写盘回执')}</span>`;
    return null;
  }
  return result;
}

function applyReceiptHtml(result) {
  const partial = result.state === 'error' || result.push_ok === false
    || result.reload_ok === false;
  const complete = !partial && result.reload_ok === true;
  const title = partial ? '已写盘，但后续步骤未完成'
    : complete ? '已写盘，CPA 已重载' : '已写盘；重载结果未提供';
  const backup = (result.backup || '').split(/[\\/]/).pop();
  const detail = result.error || result.reload_msg || result.push_msg || '';
  return `<span style="color:var(--${complete ? 'ok' : 'warn'})">${title}`
    + (backup ? `，备份 ${esc(backup)}` : '')
    + (detail ? `<br>${esc(detail)}` : '') + '</span>';
}

$('#btnapply').onclick = async () => {
  const btn = $('#btnapply');
  if (!S.previewPlanId || S.previewInputKey !== planInputKey()) {
    $('#applymsg').textContent = '预览已失效，请重新生成写回方案；本次没有提交。';
    btn.disabled = true;
    return;
  }
  btn.disabled = true;
  $('#applymsg').innerHTML = '<span class="spin"></span> 写回中…';
  // 写盘之后要让 CPA 立即生效。_cred 总是带上：用户若是用 CPA 管理密码
  // 登录的（默认路径），服务端直接复用它去 PUT，无需在这里再输一遍。
  const body = { plan_id: S.previewPlanId, confirm: true, _cred: S.token };
  body.push = {
    mgmt_key: $('#o_mgmt').value,          // 留空则服务端复用 _cred
    client_key: $('#o_client').value.trim(),
  };
  // 地址**永远不由前端决定**（2026-09-25）。
  // ------------------------------------------
  // 这里原来读 `#o_base` 输入框，填了就作为 push.base 覆盖服务端配置。
  // 两次现场故障都出在这个框上：
  //   · 早期它硬编码 https://cpa.example.com → PUT 走公网被 Cloudflare 拦成
  //     403 error code 1010（CF 的码，不是 CPA 拒绝配置），看着像「写回失败」；
  //   · 2026-09-24 用户手填 `cli-proxy-api:8317`（少了 http://）→ 整次写回作废。
  // 这个值唯一的正确来源是部署方在 docker-compose.yml 的 CPA_UPSTREAM_URL
  // 里配的那个，服务端自己就有。界面改成只读回显（renderCpaHint），请求体
  // 不再带 base —— 服务端用自己配的地址。
  //
  // 服务端的地址白名单（_push_target_ok）**保留**：它现在防的是绕过界面
  // 直接打 /api/apply 的调用方，而不是本页面。
  // 弱证据段的二次确认（2026-09-29）
  // ================================
  // 服务端的 `confirm_weak` 闸（server.py:_api_apply）在方案含 weak 段时返回
  // 409 `weak_evidence_unconfirmed`。这个页面此前从不发这个字段，于是任何
  // 含猜测清单的方案点「确认写回」都只能拿到一句「（HTTP 409）」——
  // 界面上看就是「写回按钮点了没反应 / 报个看不懂的错」，而闸想要的那次
  // 人工确认根本没机会发生。
  //
  // 这里把 409 的载荷渲染成操作员能判断的清单（站 · 段 · 模型名），确认后
  // 带 confirm_weak=true 重发一次。取消则什么都不写。闸仍在服务端 ——
  // 这里只是把它需要的那次确认补上。
  let d;
  for (let attempt = 0; ; attempt += 1) {
    try { d = await api('/api/apply', { method: 'POST', body }); break; }
    catch (e) {
      const payload = e && e.data;
      if (attempt === 0 && e && e.status === 409
          && payload && payload.error_code === 'weak_evidence_unconfirmed') {
        const okToWrite = await confirmWeakEvidence(payload);
        if (!okToWrite) {
          $('#applymsg').innerHTML =
            '<span class="hint">已取消 —— config.yaml 未被改动。'
            + '可回上一步取消勾选这些段，或先补探测再写。</span>';
          btn.disabled = false;
          return;
        }
        body.confirm_weak = true;
        continue;
      }
      $('#applymsg').innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
      btn.disabled = false;
      return;
    }
  }

  // 初始回执只代表任务受理，写盘、推送和重载都可能尚未开始。
  // 统一等待任务终态并核对 local_written，不能把 HTTP 202 当作写盘成功。
  d = await awaitApplyReceipt(d, $('#applymsg'));
  if (!d) { btn.disabled = false; return; }
  // 出错但已写盘时，pollApply 刚写进 #applymsg 的错误原文必须留着 ——
  // 无条件清空会把唯一一条真实失败信息擦掉，只剩下面那个绿色成功面板。
  if (d.state !== 'error') $('#applymsg').textContent = '';

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
      最直接的确认办法：${cpaRestartHint()}。</span></div>`;
  }

  // 最后一道诚实闸（2026-09-25）：没写盘就不许出「✓ 已写回」面板。
  // 这个面板的标题是静态的「✓ 已写回」，只要显示出来就等于向用户断言
  // 「盘上已经改了」。后端在 written/backup/diffs 之外专门给了
  // `local_written`，这里必须认它 —— 用 d.written 判是不够的：将来任何一条
  // 早退路径只要漏填这三个字段，界面就又会变成「成功 + 一排空白」。
  if (d.local_written !== true) {
    $('#applymsg').innerHTML = `<span style="color:var(--bad)">
      <b>写回未完成 —— config.yaml 未被改动。</b>
      ${esc(d.error || d.reload_msg || d.push_msg || '后端未返回写盘回执')}</span>`;
    btn.disabled = false;
    return;
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

/* ══════════════════ 路由批量管理 ══════════════════

   为什么单独一块而不是接进投喂流程（① 输入 → ② 探测 → ③ 定档 → ④ 写回）：
   那条流程处理的是「把**新**凭据插进 config.yaml」，需要定档、去重、影响面
   一整套推导；批量管理改的是**既有条目的字段**，不新增条目，两者的判据、
   风险与确认口径都不同，混在一起会让「不新增条目」这个安全前提说不清楚。

   与 CPAMP 那张表的取舍：它按条目扁平分页（158 条 / 10 条一页 / 16 页），
   看不出「同一个网址下几把 Key 的档位一不一致」—— 而那正是唯一重要的结构，
   也是本项目自己注入时写坏过的地方（实测：注入前 0 组分裂，注入后 3 组）。
   所以这里以 (段 · 网址) 为一等公民，分裂的组红框直接顶出来。         */

/* `revision` 是这份路由清单对应的配置快照指纹（后端 bulk.config_revision）。
   /api/bulk-preview 强制要求它：缺失回 428、不符回 409 —— 因为 ops 里的
   `index` 是**下标**，而下标只在拉取那一刻的配置里有意义。中途别处改过
   config.yaml（另一个标签页、CPAMP、手工编辑），下标就指向别的条目了。
   所以每次 bmLoad 都要一并存下它，提交时原样回传。 */
const BM = { groups: [], sel: new Set(), bulkId: '', revision: '' };

/* ── 未提交的就地改动 ──────────────────────────────────────────────
   用户现场反馈（2026-09-13）：「根本没法对每个相同域名的上游优先值进行手工
   调整」。原来改档要「勾选卡片 → 滚到面板底部 → 填数字 → 点应用」四步，
   视线全程离开卡片。现在卡片头部就是输入框，改完进这个队列，统一预览写回。

   键为什么是 (段, host) 而不是下标：priority 是**站级**属性 ——
   `_validate_final`（server.py）会拒绝「同 host 不同档」的写入，所以一次
   编辑必然作用于该站的全部条目。用下标当键会漏掉同站的其他条目。

   `enabled` 与 `priority` 分开存：两者可以叠加（改成 350 同时启用），
   而分开存让「只改了优先级」与「只改了启停」在计数与放弃时都分得清。 */
const BMD = new Map();          // `${section}\u0000${host}` -> {section, host, priority, enabled}
const bmdKey = (sec, host) => sec + '␟' + host;

/* 档位 → 颜色。用黄金角轮转而不是固定色表：档位数由配置决定，
   写死八色表到第 9 档就没颜色了，而「同色 = 同档」正是这里要的可读性。
   饱和度/亮度压得低 —— 它是背景提示，不该盖过分裂组的红框。 */
const bmTint = (() => {
  const cache = new Map();
  return (pri) => {
    if (pri == null) return 'var(--mute)';
    if (cache.has(pri)) return cache.get(pri);
    // 同一份清单内按**值**排序定色，保证同样的档位谱每次渲染同色
    const all = [...new Set(BM.groups.flatMap((g) => g.priorities || []))]
      .sort((a, b) => b - a);
    const i = all.indexOf(pri);
    const c = i < 0 ? 'var(--mute)'
      : `hsl(${(i * 137.508) % 360} 46% 52%)`;
    cache.set(pri, c);
    return c;
  };
})();

function bmdSync() {
  const n = BMD.size;
  $('#bmdirty').hidden = n === 0;
  $('#bmdirtyn').textContent = String(n);
  $('#bmdirtygo').disabled = n === 0;
}

/* 就地编辑的取值。非法（空 / 非整数 / <1）返回 null，卡片上标红，不提交。
   不在输入时立刻钳制 —— 用户敲「35」的过程中不该被判非法。 */
function bmdPriOf(sec, host) {
  const d = BMD.get(bmdKey(sec, host));
  return d && typeof d.priority === 'number' ? d.priority : null;
}

const BM_SEC_CN = {
  'gemini-api-key': 'Gemini', 'codex-api-key': 'Codex',
  'claude-api-key': 'Claude', 'openai-compatibility': 'OpenAI 兼容',
};
const BM_SEC_CLS = {
  'gemini-api-key': 'sec-gemini', 'codex-api-key': 'sec-codex',
  'claude-api-key': 'sec-claude', 'openai-compatibility': 'sec-compat',
};

// 批量管理的卡片键。分隔符**必须是 HTML 安全的字符**（2026-09-19 实测修）。
//
// 原来是 `'\u0000'`（NUL）。它用于纯 JS 的 Map/Set 没问题，但这个 key 会
// 被写进 `data-k` 属性（bmCard 的 `<div class="bm-card" data-k="...">`），
// 而 **HTML 属性里的裸 NUL 会被解析器替换成 U+FFFD**（替换字符）。
// 于是读回来的是 `claude-api-key�golf.example`，与 `bmKey(g)` 生成的
// `claude-api-key\u0000golf.example` **永不相等**：
//
//   · 点复选框/卡片 → `BM.sel.add(k)` 确实执行了
//   · `bmSelected()` 用 `BM.groups.filter(g => BM.sel.has(bmKey(g)))` 过滤
//     → 一条都匹配不上 → 计数恒为 0、五个操作按钮恒灰
//
// 现场就是「路由批量管理点了没反应」（MHTML 两份快照 + 本机容器实测复现）。
// 换成 U+241F（符号「␟」，UNIT SEPARATOR）—— 它是可见的普通字符，
// HTML 安全，且不会出现在段名或域名里。
const bmKey = (g) => g.section + '␟' + g.host;

/* `#bmwhy` 的常态文案。bmBusy 与 bmLoad 的失败分支会临时改写它，
   bmRender 每次都要复位回来 —— 否则加载完了还挂着「正在读取…」。
   取自 index.html 里那句，两处必须一致。 */
const BM_IDLE_HINT = '勾选上方卡片后可批量操作；单站改档直接在卡片头部输入';

function bmStat() {
  const t = BM.groups;
  // 两个口径**必须分开数**（2026-09-26 本机按 VPS 拓扑实跑修）。
  // ------------------------------------------------------------
  // 后端给两个字段，语义完全不同：
  //   · `split`      —— 同段同 host 多档。**违规**：修改要求第 6 条
  //                     「同一类型相同网址上游优先级要保持相同」。
  //   · `site_split` —— 同 host 跨段多档。**允许**：同一条要求写得很清楚
  //                     「不同类型相同网址可以不同」，优先级只在同类型内比较。
  //
  // 原来这里（以及 bmCard 的红框、`#bmscope` 的计数）把两者一视同仁地算成
  // 「档位分裂」，于是实跑时 50 组全部标红、顶部写「50 组分裂」，而
  // 「只选分裂组」按钮按 `g.split` 取数只得 0 组 —— 界面自相矛盾，红色边框
  // 也彻底失去指路作用（全都红等于没有红）。用户那句「点了根本不会生效」
  // 有一半是这个：看起来满屏分裂，点「只选分裂组」却一个都选不中。
  const split = t.filter((g) => g.split).length;
  const cross = t.filter((g) => !g.split && g.site_split).length;
  // `entries` 可能缺。bmStat() 跑在 bmLoad 的 try 里，一条坏 group 让
  // `g.entries.filter` 抛 TypeError → 落进 bmLoad 的 catch → `#bmgrid` 写
  // 「读取失败：Cannot read properties…」并**禁掉五个批量按钮**，
  // 而 /api/routes 其实回了 200 和完整数据。现场就是「路由批量管理：
  // 选了也点不动」。2026-10-01 加闸：坏 group 按 0 条算，不拖垮整个面板。
  const off = t.reduce((n, g) => n
    + (Array.isArray(g.entries) ? g.entries.filter((e) => !e.enabled).length : 0), 0);
  const ent = t.reduce((n, g) => n + g.entries.length, 0);
  $('#bmstat').innerHTML =
      `<div><b>${t.length}</b><i>分组（段 × 网址）</i></div>`
    + `<div><b>${ent}</b><i>条目</i></div>`
    + `<div class="${split ? 's-bad' : ''}"><b>${split}</b><i>档位分裂</i></div>`
    + `<div class="${cross ? 's-cross' : ''}"><b>${cross}</b><i>跨段不同档</i></div>`
    + `<div class="${off ? 's-off' : ''}"><b>${off}</b><i>已停用</i></div>`;
}

function bmCard(g) {
  const k = bmKey(g);
  const on = BM.sel.has(k);
  const pris = g.priorities || [];
  const edit = BMD.get(bmdKey(g.section, g.host));
  const dirty = edit !== undefined;
  const shown = dirty && edit.priority != null ? edit.priority : (pris.length ? Math.max(...pris) : '');
  const bad = dirty && edit.priority == null;
  const rows = g.entries.map((e) => `
    <div class="bm-row ${e.enabled ? '' : 'off'}">
      <span class="bm-dot ${e.enabled ? 'on' : 'off'}"></span>
      <code>${esc(e.api_key_masked)}</code>
      <span class="m">${e.models} 型</span>
      <span class="p">${e.priority === null ? '—' : e.priority}</span>
    </div>`).join('');
  const warn = g.split ? `
    <div class="bm-warn"><b>档位分裂</b>：同一网址的 ${g.entries.length} 个条目
      拿到了 ${pris.length} 个不同 priority（${pris.join(' / ')}）。
      CPA 按层级取最高那一桶，低档的实质是冷备 —— 高档几条会先被打光配额。
      勾选本组后点「统一优先级」即可对齐到 ${Math.max(...pris)}。</div>` : '';
  // 站级跨段分裂（`site_split`）与段内分裂（`split`）是两件事：
  // 后者是同段同 host 多档（**违规**），前者是同一网站在不同协议段拿到不同档。
  //
  // 跨段不同档**不是违规**（2026-09-26 修）：修改要求第 6 条原文
  // 「同一类型相同网址上游优先级也要保持相同（不同类型相同网址可以不同，
  // 优先级主要在同一类型进行综合比较）」。原来这段把它写成「同样违反
  // 「同网址同优先级」」，措辞与红框（`g.split || g.site_split`）都成了误判 ——
  // 生产配置里每个跨段站都会命中，用户以为满屏都是问题。
  // 现在只作**提示**：它影响的是「同一网站在各段的相对顺位」，不是不变式。
  const sitewarn = (g.site_split && (g.site_priorities || []).length > 1) ? `
    <div class="bm-note">跨协议：本站 ${esc(g.host)} 在各段拿到
      ${g.site_priorities.join(' / ')} 共 ${g.site_priorities.length} 个档位。
      这是**允许**的 —— 按类型分别比较，各段各排各的序；只是想提醒你
      层间相对顺位不一致。</div>` : '';
  const allOff = g.entries.every((e) => !e.enabled);
  // 红框只给**真的违规**：段内档位分裂。跨段不同档不红（见 sitewarn 的说明）。
  return `
  <div class="bm-card ${on ? 'sel' : ''} ${g.split ? 'split' : ''} ${dirty ? 'edited' : ''}" data-k="${esc(k)}">
    <div class="bm-head">
      <input type="checkbox" class="bmck" ${on ? 'checked' : ''} data-k="${esc(k)}">
      <span class="bm-sec ${BM_SEC_CLS[g.section] || ''}">${esc(BM_SEC_CN[g.section] || g.section)}</span>
      <span class="bm-host"><b>${esc(g.host)}</b>
        <span>${esc(g.base_urls.join(' · '))}</span></span>
      <span class="bm-cardacts">
        <button class="bm-mini ${allOff ? 'off' : 'on'}" data-act="toggle"
          title="${allOff ? '把本站全部条目重新启用' : '把本站全部条目停用'}">${allOff ? '启用' : '停用'}</button>
        <button class="bm-mini del" data-act="del" title="把本站全部条目列入删除预览">删除…</button>
      </span>
      <span class="bm-pri ${g.split ? 'bad' : ''}">
        <span class="bm-tint" style="background:${bmTint(pris.length ? Math.max(...pris) : null)}"></span>
        ${pris.length > 1 ? `<em class="s">${pris.join('/')}</em>` : ''}
        <input class="bm-priin ${dirty ? 'dirty' : ''} ${bad ? 'bad' : ''}"
          type="number" min="1" max="100000" value="${shown}"
          data-sec="${esc(g.section)}" data-host="${esc(g.host)}"
          aria-label="${esc(g.host)} 的优先级"
          title="改这里 = 把本站全部 Key 一起改成同一个档（同网址同优先级）">
      </span>
    </div>
    <div class="bm-body">${rows}</div>${warn}${sitewarn}
  </div>`;
}

/* 当前筛选结果。抽成函数是因为「批量设优先级 / 全选」都要按**筛选结果**
   而不是全量来算 —— 用户的用法是「筛出某个网站的全部段，一键设同一个档」。 */
function bmFiltered() {
  const terms = ($('#bmq').value || '').trim().toLowerCase()
    .split(/\s+/).filter(Boolean);
  const sec = $('#bmsec').value;
  const mode = $('#bmfilter').value;
  return BM.groups.filter((g) => {
    if (sec && g.section !== sec) return false;
    if (terms.length) {
      // 多关键词按**与**匹配：`alfa codex` 只出那一组。
      // 网址、段名（英文与中文）都算命中面。
      const hay = (g.host + ' ' + g.section + ' '
                   + (BM_SEC_CN[g.section] || '') + ' '
                   + g.base_urls.join(' ')).toLowerCase();
      if (!terms.every((t) => hay.includes(t))) return false;
    }
    if (mode === 'split') return g.split;
    if (mode === 'off') return g.entries.some((e) => !e.enabled);
    if (mode === 'on') return g.entries.every((e) => e.enabled);
    if (mode === 'sel') return BM.sel.has(bmKey(g));
    return true;
  });
}

function bmRender() {
  const list = bmFiltered();
  $('#bmgrid').innerHTML = list.length
    ? list.map(bmCard).join('')
    : '<div class="bm-empty">没有匹配的分组</div>';
  const ent = list.reduce((n, g) => n + g.entries.length, 0);
  const sp = list.filter((g) => g.split || g.site_split).length;
  $('#bmscope').innerHTML =
    `筛选出 <b>${list.length}</b> 组 · ${ent} 个条目`
    + (sp ? ` · <span style="color:var(--bad)">${sp} 组分裂</span>` : '');
  const sel = bmSelected();
  const selEnt = sel.reduce((n, g) => n + g.entries.length, 0);
  $('#bmn').textContent = String(sel.length);
  $('#bmn2').textContent = sel.length ? `（含 ${selEnt} 个条目）` : '';
  // 操作栏**常驻**（原来是 `hidden = sel.length === 0`，用户 2026-09-13 截图里
  // 54 卡全未勾选 → 整条栏不渲染 → 看起来「这个面板根本没有批量功能」）。
  // 改成常驻 + 未选中时置灰，并把「为什么点不动」写在旁边。
  const idle = sel.length === 0;
  $('#bmact').dataset.idle = idle ? '1' : '0';
  ['#bmunify', '#bmenable', '#bmdisable', '#bmdelete', '#bmsetpri']
    .forEach((s) => { $(s).disabled = idle; });
  // 文案要**复位**：bmBusy / bmLoad 的失败分支把它改成过「正在读取…」与
  // 「读取失败…」。不复位的话，加载成功后那句话还挂在那里，用户以为还在转。
  $('#bmwhy').textContent = BM_IDLE_HINT;
  $('#bmwhy').hidden = !idle;
  bmPresets();
  bmdSync();
  if (idle) { $('#bmdel').hidden = true; }
}

/* 档位预设按钮：从**当前筛选结果**里取现成的档位值。
   用户凭什么知道该填 350 还是 555？原来那个裸 input 没给任何参照。
   这里把该段现有的档位谱列出来，点一下就填进去，另有 [-1] [+1] [居中最密处]。 */
function bmPresets() {
  const list = bmFiltered();
  const all = [...new Set(list.flatMap((g) => g.priorities || []))]
    .filter((x) => typeof x === 'number').sort((a, b) => b - a);
  const box = $('#bmpresets');
  if (!all.length) { box.innerHTML = ''; return; }
  const top = all[0];
  const low = all[all.length - 1];
  const frag = all.slice(0, 8).map((v) =>
    `<button type="button" data-v="${v}">${v}</button>`).join('');
  box.innerHTML = frag
    + `<button type="button" data-adj="1" title="比当前最高档再高 1">最高+1</button>`
    + `<button type="button" data-adj="-1" title="比当前最低档再低 1">最低-1</button>`
    + `<button type="button" data-adj="mid" title="最高与最低的中值，插进现有谱系中间"
       >中值${Math.floor((top + low) / 2)}</button>`;
}


/* 加载期间把整条工具栏禁掉（2026-09-26 本机按 VPS 拓扑实跑修）。

   现场：打开面板后 `/api/routes` 在浏览器里实测要 28.6 秒（同一接口 curl
   只要 0.6 秒 —— 差额是首屏 JS、`/api/context` 与它抢同一个连接池）。
   这 28 秒里 `BM.groups` 还是空数组，而筛选、全选、只选分裂组、五个批量
   按钮**全都是可点的**：
     · 点「只选分裂组」→ 在空数组上过滤 → 面板刷成「没有匹配的分组」
     · 顶部一直显示「筛选出 0 组」
     · 五个操作按钮因为选中集为空而恒灰
   用户看到的就是「路由批量管理点了根本不会生效」—— 而后端数据完全正常
   （实测 50 组 / 133 条目）。等数据到了再放行，并且明说在等什么。 */
function bmBusy(on) {
  ['#bmq', '#bmsec', '#bmfilter', '#bmreload', '#bmall', '#bmnone',
   '#bmsplit', '#bmunify', '#bmenable', '#bmdisable', '#bmdelete',
   '#bmpri', '#bmsetpri'].forEach((s) => {
    const el = $(s);
    if (el) el.disabled = on;
  });
  const why = $('#bmwhy');
  if (why && on) {
    why.hidden = false;
    why.textContent = '正在读取路由清单…（配置很大时要十几秒）';
  }
}

async function bmLoad() {
  $('#bmgrid').innerHTML = '<div class="bm-empty">读取中…</div>';
  bmBusy(true);
  try {
    const d = await api('/api/routes', { timeoutMs: 120000 });
    BM.groups = d.groups || [];
    BM.revision = d.revision || '';
    // 选中集按 (段,网址) 而不是下标 —— 重新读取后下标可能因别处改动而移位，
    // 用下标记选中会静默选错组。
    const live = new Set(BM.groups.map(bmKey));
    [...BM.sel].forEach((k) => { if (!live.has(k)) BM.sel.delete(k); });
    // 就地编辑的草稿一律作废：它们记的是「相对拉取那一刻的值」，
    // 重新读取意味着底层已经变了，留着就是拿旧基线做新决定。
    // 放弃草稿确实会丢用户输入，所以只在**显式重新读取**时丢，
    // 不在每次 bmRender 时丢。
    BMD.clear();
    bmBusy(false);
    bmStat();
    bmRender();
  } catch (e) {
    bmBusy(false);
    // 读不到就说清楚「按钮为什么点不动」，不要只在网格里留一行小字 ——
    // 用户的视线在工具栏上。
    $('#bmgrid').innerHTML =
      `<div class="bm-empty" style="color:var(--bad)">读取失败：${esc(e.message)}`
      + `<br><span class="hint">批量操作已禁用（没有路由清单就无从下手）。`
      + `点「重新读取」重试；持续失败看容器日志。</span></div>`;
    const why = $('#bmwhy');
    if (why) {
      why.hidden = false;
      why.textContent = `路由清单读取失败：${e.message} —— 点「重新读取」重试`;
    }
    ['#bmunify', '#bmenable', '#bmdisable', '#bmdelete', '#bmsetpri']
      .forEach((s) => { const el = $(s); if (el) el.disabled = true; });
    // 重新读取必须留着，否则用户没有任何自救手段。
    const rl = $('#bmreload');
    if (rl) rl.disabled = false;
  }
}

function bmSelected() {
  return BM.groups.filter((g) => BM.sel.has(bmKey(g)));
}

/* 就地草稿 → ops 的实现住在 `SECTION_LABEL` 之前（那一段才是
   `tests/test_web.py` 拿进 node 跑的部分）。同一条草稿可以同时产生
   priority 与 enable/disable 两组 op —— 用户完全可能「把这一站改成 900，
   同时把它重新启用」，两个字段独立叠加。 */

/* 五种批量动作各自生成 ops。
   `enable` / `disable` 的字段差异（key 类段写 excluded-models 通配符、
   compat 段写布尔字段）**全部在后端处理**，且那两个值是从 CPAMP 源码实时
   解析的 —— 前端不许自己拼，否则上游一改写法这里就静默失效。 */
function bmOps(kind, arg) {
  // 就地草稿先落进 ops —— 它与批量动作是**叠加**关系：用户可以
  // 「把 A 站改成 900 的同时把选中的 5 个站统一到 500」。
  const ops = bmDraftOps(BMD, BM.groups);
  // 'draft' 是「只提交就地草稿」（#bmdirtygo），没有批量动作要叠加。
  //
  // 2026-09-26 修：原来没有这个分支，于是 kind==='draft' 掉进最后的 else，
  // 在那里 `const want = kind === 'enable'` 求值为 **false** —— 每个**选中**
  // 组的所有启用条目都被追加了一条 `disable` op。用户点「预览并写回这些
  // 改动」想提交的是单站档位草稿，实际方案里却多出「把这些站全部停用」。
  // 没选中任何组时才碰巧无害，而卡片是可以既选中又有草稿的。
  if (kind === 'draft') return ops;
  bmSelected().forEach((g) => {
    if (kind === 'unify' || kind === 'setpri') {
      // setpri：用户给定的值，整组所有 Key 都写它。
      // unify：取组内最高档 —— 往高对齐而不是往低，因为低档那几条本来就被
      // 高档遮住、实质不参与轮询；往低对齐会把整组一起降级。
      let target;
      if (kind === 'setpri') {
        target = arg;
      } else {
        const pris = (g.priorities || []).filter((x) => x !== null);
        if (pris.length < 2) return;            // 本来就一致，不产生噪声
        target = Math.max(...pris);
      }
      // 就地草稿已经定过的站，批量值不覆盖它 —— 用户对单站的显式指定
      // 优先于批量动作，后者是粗粒度默认。少了这一条，用户「A 站 900」
      // 会被随后的「全选统一到 500」静默吃掉。
      const draft = BMD.get(bmdKey(g.section, g.host));
      if (draft && typeof draft.priority === 'number') return;
      g.entries.forEach((e) => {
        if (e.priority !== target) {
          ops.push({ section: g.section, index: e.index, fingerprint: e.fingerprint,
                     action: 'priority', value: target });
        }
      });
    } else if (kind === 'delete') {
      // 每条都带 base-url 指纹。后端逐条校验，不符整批拒绝 ——
      // 下标是位置，位置会因为别人并发改动而指向另一个条目。
      g.entries.forEach((e) => {
        ops.push({ section: g.section, index: e.index, fingerprint: e.fingerprint,
                   action: 'delete', expect: e.base_url });
      });
    } else {
      const want = kind === 'enable';
      g.entries.forEach((e) => {
        if (e.enabled !== want) {
          ops.push({ section: g.section, index: e.index, fingerprint: e.fingerprint,
                     action: want ? 'enable' : 'disable' });
        }
      });
    }
  });
  return ops;
}

async function bmPreview(kind, arg) {
  const ops = bmOps(kind, arg);
  const msg = $('#bmmsg');
  if (!ops.length) {
    msg.innerHTML = '<span style="color:var(--ink-3)">选中的组已经是目标状态，无需改动</span>';
    return;
  }
  msg.textContent = '生成预览中…';
  try {
    // revision 必须带上：后端用它确认 ops 里的 index 仍指向拉取时的那些条目。
    // 缺失回 428，不符回 409（配置在别处被改过）—— 两种都要提示刷新，
    // 而不是让用户对着一个沉默失败的按钮反复点。
    const d = await api('/api/bulk-preview',
                        { method: 'POST', body: { ops, revision: BM.revision } });
    if (!d.changed) {
      msg.innerHTML = '<span style="color:var(--ink-3)">没有实际改动</span>';
      return;
    }
    BM.bulkId = d.bulk_id;
    $('#bmcnt').textContent = `${d.changed} 处改动`;
    $('#bmsem').textContent =
      `停用语义来源：${d.semantics || '未知'}`
      + (d.problems && d.problems.length
         ? ` · 有 ${d.problems.length} 条未能执行：${d.problems.join('；')}` : '');
    // 撞档消解说明顶在 diff 上方 —— 它解释了「我填 350、落盘却是 349」，
    // 埋在 diff 里等于没写。
    const coll = d.collision_notes || [];
    $('#bmcoll').hidden = coll.length === 0;
    $('#bmcoll').innerHTML = coll.length
      ? `<b>站间档位已自动错开</b>（同类型不同域名不得同档，第 7 条）
         <div>${coll.map(esc).join('</div><div>')}</div>`
      : '';
    $('#bmdiff').textContent = d.diff
      + (d.diff_truncated ? '\n…（diff 过长已截断，完整改动以写回结果为准）' : '');
    $('#bmpreview').hidden = false;
    msg.textContent = '';
    $('#bmpreview').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (e) {
    // 428 / 409 都是「你手里的清单过期了」，唯一出路是重新拉取。
    // 自动刷一次并让用户重选，比只报错更省一次来回。
    const stale = /revision|stale|过期|刷新/i.test(e.message || '');
    msg.innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`
      + (stale ? '<span style="color:var(--ink-3)"> · 已自动刷新路由，请重新选择</span>' : '');
    if (stale) { BM.sel.clear(); bmLoad(); }
  }
}

$('#pbulk').addEventListener('toggle', () => {
  if ($('#pbulk').open && !BM.groups.length) bmLoad();
});
$('#bmreload').onclick = () => bmLoad();
$('#bmq').oninput = () => bmRender();
$('#bmfilter').onchange = () => bmRender();
$('#bmsec').onchange = () => bmRender();
// 「选中筛选结果」而不是「全选」—— 用户的用法是先筛出某个网站的全部段，
// 再一键设同一个档。全量全选反而是危险的默认。
$('#bmall').onclick = () => {
  bmFiltered().forEach((g) => BM.sel.add(bmKey(g))); bmRender();
};
$('#bmnone').onclick = () => { BM.sel.clear(); bmRender(); };
$('#bmsplit').onclick = () => {
  BM.sel.clear();
  BM.groups.forEach((g) => { if (g.split) BM.sel.add(bmKey(g)); });
  $('#bmfilter').value = 'split';
  $('#bmsec').value = '';
  $('#bmq').value = '';
  bmRender();
};
$('#bmsetpri').onclick = () => {
  const v = parseInt($('#bmpri').value, 10);
  if (!Number.isFinite(v) || v < 1) {
    $('#bmmsg').innerHTML =
      '<span style="color:var(--bad)">请先填一个 ≥ 1 的档位数字</span>';
    return;
  }
  bmPreview('setpri', v);
};

/* ── 批量删除：本面板唯一不可逆的动作，所以确认门槛比其余三个都高 ──
   ① 先列出**每一条**将被删除的条目（不是只报个数）；
   ② 要求手打 DELETE —— 防误点；
   ③ 后端还会逐条校验 base-url 指纹，不符整批拒绝；
   ④ 落盘前自动备份。
   即便如此仍要说清：CPA 侧没有回滚。 */
$('#bmdelete').onclick = () => {
  const sel = bmSelected();
  if (!sel.length) return;
  const rows = [];
  sel.forEach((g) => {
    g.entries.forEach((e) => {
      rows.push(`<div><span class="s">${esc(BM_SEC_CN[g.section] || g.section)}</span>`
        + ` · ${esc(e.base_url)} · <span class="s">${esc(e.api_key_masked)}</span>`
        + ` · ${e.models} 型</div>`);
    });
  });
  $('#bmdeln').textContent = String(rows.length);
  $('#bmdellist').innerHTML = rows.join('');
  $('#bmdelok').value = '';
  $('#bmdelgo').disabled = true;
  $('#bmdel').hidden = false;
  $('#bmdel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
};
$('#bmdelok').oninput = () => {
  const ok = $('#bmdelok').value.trim() === 'DELETE';
  $('#bmdelgo').disabled = !ok;
  // 输入正确时移除提示（避免红色边框干扰）
  $('#bmdelok').classList.toggle('input-ok', ok);
};
$('#bmdelno').onclick = () => { $('#bmdel').hidden = true; };
$('#bmdelgo').onclick = () => {
  if ($('#bmdelok').value.trim() !== 'DELETE') return;
  $('#bmdel').hidden = true;
  bmPreview('delete');
};
// 卡片整体可点 = 勾选；但**卡片内的交互控件必须被排除**，否则点输入框、
// 点「停用」小按钮都会顺带改变勾选状态（2026-09-13 加就地编辑后新增的冲突）。
$('#bmgrid').addEventListener('click', (e) => {
  if (e.target.closest('.bm-priin, .bm-cardacts, button, input')) return;
  const card = e.target.closest('.bm-card');
  if (!card) return;
  const k = card.dataset.k;
  if (BM.sel.has(k)) BM.sel.delete(k); else BM.sel.add(k);
  bmRender();
});
// 复选框 change 事件（2026-09-18）
// ---------------------------------
// .bmck 渲染在卡片里，但卡片点击委托把 input 显式排除
// （防止点输入框顺带改勾选状态），导致复选框无法触发 BM.sel 更新。
// 用事件委托补一个 change 监听，单独处理复选框的选中/取消。
$('#bmgrid').addEventListener('change', (e) => {
  const ck = e.target.closest('.bmck');
  if (!ck) return;
  const k = ck.dataset.k;
  if (!k) return;
  if (ck.checked) BM.sel.add(k); else BM.sel.delete(k);
  bmRender();
});

/* 卡片内的小按钮：整站启停 / 整站删除预览。
   走的是**就地草稿**那条路（启停）与既有的批量删除预览（删除），
   不新开一条写回路径 —— 三道闸（revision / 指纹 / confirm）一道不少。 */
$('#bmgrid').addEventListener('click', (e) => {
  const btn = e.target.closest('.bm-mini');
  if (!btn) return;
  e.stopPropagation();
  const card = btn.closest('.bm-card');
  if (!card) return;
  const g = BM.groups.find((x) => bmKey(x) === card.dataset.k);
  if (!g) return;
  if (btn.dataset.act === 'toggle') {
    const allOff = g.entries.every((x) => !x.enabled);
    const d = BMD.get(bmdKey(g.section, g.host)) || { section: g.section, host: g.host };
    d.enabled = allOff;                    // 全停 → 全开；否则 → 全停
    if (typeof d.priority !== 'number') d.priority = undefined;
    BMD.set(bmdKey(g.section, g.host), d);
    bmRender();
  } else if (btn.dataset.act === 'del') {
    BM.sel.clear();
    BM.sel.add(bmKey(g));
    bmRender();
    $('#bmdelete').click();
  }
});

/* 就地改档：`input` 事件即时记草稿（不回读 DOM，避免重排时打断输入），
   `change`（失焦/回车）时才校验并重绘着色。空值与非法值不写进草稿 ——
   写进去会让 bmOps 生成一条 value: null 的 op，后端直接拒整批。 */
$('#bmgrid').addEventListener('input', (e) => {
  const inp = e.target.closest('.bm-priin');
  if (!inp) return;
  const raw = inp.value.trim();
  const v = /^\d+$/.test(raw) ? parseInt(raw, 10) : NaN;
  const k = bmdKey(inp.dataset.sec, inp.dataset.host);
  const d = BMD.get(k) || { section: inp.dataset.sec, host: inp.dataset.host };
  d.priority = (Number.isFinite(v) && v >= 1) ? v : undefined;
  if (d.priority === undefined && d.enabled === undefined) BMD.delete(k);
  else BMD.set(k, d);
  inp.classList.toggle('bad', d.priority === undefined && raw !== '');
  inp.classList.toggle('dirty', d.priority !== undefined);
  inp.closest('.bm-card').classList.toggle('edited', BMD.has(k));
  bmdSync();
});
$('#bmgrid').addEventListener('change', (e) => {
  if (e.target.closest('.bm-priin')) bmRender();
});
// 就地编辑的提交与放弃
$('#bmdirtygo').onclick = () => bmPreview('draft');
$('#bmdirtyclr').onclick = () => { BMD.clear(); bmRender(); };
// 档位预设：只填 `#bmpri`，不直接改任何站 —— 直接改会绕过预览，
// 而「先看清改哪些再确认」是这个面板的立身之本。
$('#bmpresets').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  const list = bmFiltered();
  const all = [...new Set(list.flatMap((g) => g.priorities || []))]
    .filter((x) => typeof x === 'number').sort((a, b2) => b2 - a);
  if (!all.length) return;
  let v;
  if (b.dataset.v) {
    v = parseInt(b.dataset.v, 10);
  } else {
    const adj = b.dataset.adj;
    if (adj === '1') v = all[0] + 1;
    else if (adj === '-1') v = Math.max(1, all[all.length - 1] - 1);
    else v = Math.max(1, Math.floor((all[0] + all[all.length - 1]) / 2));
  }
  $('#bmpri').value = String(v);
});
$('#bmunify').onclick = () => bmPreview('unify');
$('#bmenable').onclick = () => bmPreview('enable');
$('#bmdisable').onclick = () => bmPreview('disable');
$('#bmcancel').onclick = () => { $('#bmpreview').hidden = true; BM.bulkId = ''; };
$('#bmapply').onclick = async () => {
  if (!BM.bulkId) return;
  const btn = $('#bmapply');
  const msg = $('#bmapplymsg');
  btn.disabled = true;
  msg.textContent = '写回中…';
  try {
    const first = await api('/api/bulk-apply', {
      method: 'POST',
      body: { bulk_id: BM.bulkId, confirm: true, push: bmPush() },
    });
    const d = await awaitApplyReceipt(first, msg);
    if (!d) return;
    msg.innerHTML = applyReceiptHtml(d);
    $('#bmpreview').hidden = true;
    BM.bulkId = '';
    BM.sel.clear();
    await bmLoad();
  } catch (e) {
    msg.innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
  } finally {
    // 正常路径也要解禁：这个面板不像投喂流程那样切走，按钮留在原地
    btn.disabled = false;
  }
};

/* 写回后触发 CPA 重载用的凭据，与投喂流程 ④ 复用**同一组输入框**
   （`#o_mgmt` / `#o_client` / `#o_cpabase`）—— 两处各摆一份输入框会让人
   以为是两套凭据。取不到就不传：后端照常落盘，只是重载退回靠 CPA 自己的
   fsnotify（那条链没有保证，见 writeback.reload_cpa 的说明）。

   **不传 base**：界面上根本没有地址输入框，地址一律由服务端的 `--cpa-url`
   决定。这是既有写回路径刻意选的安全默认 —— 那个输入框曾硬编码公网地址，
   导致 PUT 走公网被 Cloudflare 拦成 403，而容器内配好的
   `cli-proxy-api:8317` 永远用不上（见 `_run_apply_tail` 里的说明）。 */
function bmPush() {
  const mk = $('#o_mgmt');
  const ck = $('#o_client');
  const out = {};
  if (mk && mk.value) out.mgmt_key = mk.value;
  if (ck && ck.value.trim()) out.client_key = ck.value.trim();
  return out;
}


/* ── 全局调优体检 ────────────────────────────────────────────────────
   为什么这个面板要显示「为什么」而不只是数字：这几项改的是 CPA 的全局
   重试行为，改错的两个方向都有确定后果（透传 403 / 524），而正确值取决于
   本工具自己写的 priority 档位 —— 每次重探都可能变。所以每条建议都带
   服务端算出来的判据原文，让人能复核而不是凭信任点确认。          */
const TN = { id: '' };

const TN_SEV = {
  blocker: { label: '必改', cls: 'b' },
  warn: { label: '建议', cls: '' },
  info: { label: '卫生', cls: '' },
};

async function tnRun() {
  const btn = $('#tnrun');
  const basis = $('#tnbasis');
  btn.disabled = true;
  basis.innerHTML = '<span class="spin"></span> 读取配置并计算…';
  try {
    const d = await api('/api/tuning');
    TN.id = d.tuning_id || '';

    // 缺字段时显示「未知」而不是字面量 undefined —— 后者让操作员以为
    // 是计算结果，实际是前后端字段没对上。
    const numOr = (v, unit) => (typeof v === 'number' && isFinite(v)
      ? `<b>${esc(String(v))}</b> ${unit}` : `<b class="hint">未知</b>`);
    basis.innerHTML = `单次失败尝试按 ${numOr(d.attempt_sec, '秒')}算`
      + `（${esc(d.attempt_why || '后端未给依据')}）· 回源窗口 `
      + numOr(d.edge_window_sec, '秒');

    // 顶层池实况。这是全部结论的输入，先摆出来 —— 只给建议不给依据，
    // 操作员没法判断该不该改。
    $('#tntiers').innerHTML = (d.tiers || []).map((t) => {
      if (!t || !t.credentials) {
        return `<div class="bm-card"><b>${esc((t && t.section) || '?')}</b>
          <span class="hint">顶层没有可计费凭据</span></div>`;
      }
      // hosts 缺失时不能 `t.hosts.length` —— 一条坏数据会让整块 #tntiers
      // 变成空白（TypeError 逃到 catch，只剩一行报错）。
      const nhost = Array.isArray(t.hosts) ? t.hosts.length : 0;
      const run = t.longest_same_host_run > 3
        ? `<span class="tier warn">最长连续同站 ${t.longest_same_host_run} 个
             （${esc(t.run_host || '?')}）—— 会连打同一个站</span>`
        : `<span class="hint">最长连续同站 ${t.longest_same_host_run || 0} 个</span>`;
      return `<div class="bm-card"><b>${esc(t.section)}</b>
        <div class="hint">顶层档位 ${esc(String(t.top_priority))} ·
          <b>${t.credentials}</b> 个凭据 · ${nhost} 个站</div>
        ${run}</div>`;
    }).join('');

    $('#tnnotes').innerHTML = (d.notes || []).map(
      (n) => `<div class="warn b">${esc(n)}</div>`).join('');

    const pending = (d.advices || []).filter((a) => a && a.changed);
    $('#tnlist').innerHTML = (d.advices || []).map((a) => {
      const sev = TN_SEV[a.severity] || TN_SEV.warn;
      // 标题取 `item`。历史上这里读过 `a.key`，而后端那个同值字段恰好叫
      // `key` —— `_SECRET_NAME` 里有 `^key$`，出站被整体换成 `***`，
      // 于是标题从 undefined 变成 `***`，两次都认不出改的是哪个键。
      // 后端已改名为 `advice_key`；这里按 item → advice_key → key 依次兜底，
      // 让新旧两版镜像都能正确渲染（VPS 可能还在跑旧镜像）。
      const label = a.item || a.advice_key
        || (a.key && a.key !== '***' ? a.key : '') || '(后端未给键名)';
      if (!a.changed) {
        return `<div class="bm-card"><b>${esc(label)}</b>
          <span class="hint">当前 <code>${esc(String(a.current))}</code>
          已经是建议值，无需改动</span></div>`;
      }
      return `<div class="warn ${sev.cls}"><b>${esc(label)}</b>
        <span class="tag">${sev.label}</span><br>
        <code>${esc(String(a.current))}</code> →
        <code>${esc(String(a.want))}</code>
        <div class="hint">${esc(a.why || '')}</div></div>`;
    }).join('') || '<p class="hint">没有可改项。</p>';

    (d.problems || []).forEach((p) => {
      $('#tnlist').insertAdjacentHTML('beforeend',
        `<div class="warn b">改不动：${esc(p)}</div>`);
    });

    // 闸门只看「有 tuning_id 且确实有待改项」。
    // ------------------------------------------
    // 2026-10-01：原条件是 `TN.id && d.diff`。`#tnapply` 是本面板唯一的
    // 执行入口，整块嵌在 `#tnpreview` 里。后端回 `diff: ""`（空串为假值）
    // 或省略 diff 时，即便 advices 里有多条 changed（#tnlist 全列出来了、
    // #tncnt 也算了数），预览块仍保持 hidden —— 用户看到一屏建议，
    // 却找不到也点不到任何执行按钮。这就是「选完选项点执行根本不生效」
    // 的另一半：按钮不存在，不是点了没反应。
    // 现在 diff 为空时照样放出按钮，只在 diff 位置说明「后端未回 diff」。
    if (TN.id && pending.length) {
      $('#tncnt').textContent = `${pending.length} 处`;
      $('#tndiff').textContent = d.diff
        ? d.diff + (d.diff_truncated ? '\n…（diff 过长已截断）' : '')
        : '后端这次没有回 diff —— 仍可写回，但请写回后用「参考 · 当前档位谱」复核。';
      $('#tnpreview').hidden = false;
    } else {
      $('#tnpreview').hidden = true;
    }
  } catch (e) {
    basis.innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
}

/* 保留调优轮询入口，回执校验与进度处理复用投喂和批量写回的同一实现。 */
async function tnPoll(taskId, first = {}) {
  return awaitApplyReceipt({ ...first, task_id: taskId }, $('#tnmsg'));
}

$('#tnrun').onclick = () => tnRun();
$('#tncancel').onclick = () => { $('#tnpreview').hidden = true; TN.id = ''; };
$('#tnapply').onclick = async () => {
  if (!TN.id) return;
  const btn = $('#tnapply');
  const msg = $('#tnmsg');
  btn.disabled = true;
  msg.innerHTML = '<span class="spin"></span> 写回中…';
  try {
    const first = await api('/api/tuning-apply', {
      method: 'POST',
      body: { tuning_id: TN.id, confirm: true, push: bmPush() },
    });
    const st = first && first.task_id
      ? await tnPoll(first.task_id, first) : await awaitApplyReceipt(first, msg);
    if (!st) return;
    msg.innerHTML = applyReceiptHtml(st);
    $('#tnpreview').hidden = true;
    TN.id = '';
    await tnRun();
  } catch (e) {
    msg.innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
};

// 顶层启动加 catch（2026-09-19）
// ----------------------------
// 原来是裸 `boot();`。boot 是 async，内部任何未捕获异常都变成
// unhandled rejection —— 页面停在骨架屏，控制台里一条红字，用户看到的是
// 「黑屏 / 一直转圈」，没有任何可操作的提示。
// 这里把失败显式画到启动区，并给出下一步。
//
// 2026-09-26：原兜底里调的 `hideSkel()` 是 boot() 内部的局部函数，在这里
// 是 ReferenceError —— 被外层 try 吞掉，于是启动失败时依旧什么都不显示
// （黑屏的一条真路径）。改成直接操作骨架元素，并同时打全局横幅。
boot().catch((e) => {
  try {
    const box = $('#bootmsg');
    if (box) {
      box.innerHTML = `<div class="err">启动失败：${esc(e && e.message || e)}</div>`
        + `<div class="hint">刷新页面重试；持续如此请把容器日志里的 `
        + `error_ref 发出来排查。</div>`;
    }
    const skel = $('#bootbox');
    // 骨架里有 #bootmsg 时保留骨架（让报错可见），否则收起骨架
    if (skel && !(box && skel.contains(box))) skel.hidden = true;
    const gate = $('#gate');
    if (gate) gate.hidden = false;
    showBanner(`启动失败：${e && e.message || e}`, 'e');
  } catch (_) { /* 兜底本身不能再抛 */ }
});

// 全局兜底（2026-09-26）：任何渲染路径里的未捕获异常 / 未处理 rejection
// 都显示横幅，并把卡在「计算中」的占位格换成原因 —— 页面绝不静默黑屏。
window.addEventListener('error', (ev) => {
  try {
    const m = ev && (ev.message || (ev.error && ev.error.message));
    if (!m) return;                      // 资源加载错误（img 等）不打扰
    showBanner(`页面脚本异常：${m} —— 刷新页面可恢复；反复出现请截图控制台`, 'e');
  } catch (_) { /* 忽略 */ }
});
window.addEventListener('unhandledrejection', (ev) => {
  try {
    const r = ev && ev.reason;
    const m = (r && r.message) || String(r);
    showBanner(`后台操作失败：${m}`, 'e');
  } catch (_) { /* 忽略 */ }
});
