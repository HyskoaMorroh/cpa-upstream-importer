# CPA 上游批量导入服务 — 设计文档

> 版本：v1 · 2026-08-29
> 状态：待实施（方案 C 已获批准）
> 依据：CLIProxyAPI-main / CPA-Manager-Plus-main 源码实读 + 08-27~08-29 两夜实测记录（cpa-atlas.html）

---

## 0. 一句话

粘贴或上传 `url,key` 列表 → 服务自动探测判定（段归属、模型、代理、标识头、上下文上限、封号/余额/限流分类）→ 生成 diff 预览 → 用户确认 → 写回 config.yaml 并热重载。

---

## 1. 目标与非目标

### 目标

| # | 目标 | 当前痛点 |
|---|---|---|
| 1 | 批量导入上游凭据，免手工编辑 15000 行 config.yaml | 手工插入易错行号，08-29 曾因此把 1 个 key 的问题误改成 46 处 |
| 2 | 自动判定该进哪个段（gemini / codex / claude / compat） | 同一站不同协议路径表现不同（relay-g gemini 通、claude 段被 CF 拦） |
| 3 | 自动判定是否需要 proxy-url / headers | 当前 23 处 proxy、86 处 UA 全靠手工试 |
| 4 | 区分封号 / 余额 / 限流 / 门禁 / IP 封 / 反测活 | 403 既可能是门禁也可能是余额，混判导致处置完全相反 |
| 5 | 探测并写入 `max-context-length` | 678 个模型条目该字段全空，客户端只能盲猜，是 08-28 那个 400 的根因 |
| 6 | 保留 config.yaml 全部注释 | 注释是两夜排障的全部记忆（「relay-f 403 banned，从 900 降权待解封」） |

### 非目标

- 不做上游站点的额度充值 / 账号注册。
- 不做长期健康巡检（CPAMP 已有 `codex_inspection_*` 系列表与 worker 承担）。
- 不重写探测判定逻辑 —— 复用三个脚本已沉淀的规则。

---

## 2. 输入格式（已定）

固定一种格式，每行一组：

```
https://example.com,sk-xxxxxxxx
https://api.example.org/v1,sk-yyyyyyyy
```

规则：

- 分隔符：**逗号**。首个逗号左侧为 url，右侧为 key（key 内不允许逗号）。
- url 带不带 `/v1` **都接受** —— 服务按目标段自动规范化（见 §3）。
- 空行、`#` 开头行忽略。
- 同一 url 多个 key 写多行。
- 支持两种入口：txt 文件上传、网页文本框粘贴。二者走同一解析器。

---

## 3. URL 规范化（段决定形态，非用户判断）

12 个现存站点、206 个凭据条目**零例外**，规则完全由段决定：

| 段 | base-url 形态 | 实际请求路径 | 实测样本 |
|---|---|---|---|
| `gemini-api-key` | 裸域名，**无** `/v1` | `{base}/v1beta/models/{model}:generateContent?key={key}` | 64/64 |
| `claude-api-key` | 裸域名，**无** `/v1` | `{base}/v1/messages?beta=true` | 65/65 |
| `codex-api-key` | **必须带** `/v1` | `{base}/responses` | 65/65 |
| `openai-compatibility` | **必须带** `/v1` | `{base}/chat/completions` | 12/12 |

源码依据：
- gemini：`internal/runtime/executor/gemini_executor.go:176`
- codex：`internal/runtime/executor/codex_executor_execute.go:76`
- claude：`internal/runtime/executor/claude_executor_execute.go:30`
- compat：走 OpenAI 形态执行器，`/v1/chat/completions`

**实现**：解析后剥离尾部 `/`、剥离尾部 `/v1`，得到 `bare`。写入时按段补：gemini/claude 用 `bare`，codex/compat 用 `bare + "/v1"`。

---

## 4. 探测流水线

五个阶段，前四阶段不花钱或极省，第五阶段可选。

```
① 解析      url,key 逐行 → 规范化 → 去重（见 §8）
② 段探测    四段各发一次最小请求（复用 build_request）
③ 判定      classify(status, body) → 七类（见 §5）
④ 修复探测  失败项：经 proxy 再试 / 加 headers 再试（复用 fallback_combos）
⑤ 上限探测  可选，二分法测 max-context-length（复用 bisect_limit）
```

### 阶段 ② 的请求构造

复用 `probe-fix.py` 的 `build_request`，四段协议路径见 §3。两点必须统一：

- **claude 段鉴权头**：`probe-fix.py` 用 `Authorization: Bearer`，`audit-upstreams.py` 用 `x-api-key`。两者 Anthropic 官方都支持，但中转站实现不一。**服务先试 `Authorization: Bearer`（与 CPA 执行器一致），401 时回退 `x-api-key` 再试一次**。
- **探测文本**：不得用 `"hi"` —— 会触发部分站的反测活拦截。固定用技术问句：
  `Reply with one short sentence: what is the difference between a hash map and a tree map?`

### 阶段 ④ 的处置优先级

严格按既定顺序，只有前一级无效才降到下一级：

```
proxy-url  >  headers  >  降 priority  >  weight: 0
```

- `proxy-url` 唯一取值 `http://mihomo:7890`（config.yaml 里 23 处全是这个值）。二值判断：加 / 不加。
- `headers` 唯一键名 `User-Agent`（86 处全是它，无第二种键）。二值判断：加 / 不加。
  - 实测结论（relay-c 七值矩阵，2026-08-29）：**UA 或 Originator 任一满足即可，值不敏感**，包括垃圾串和空字符串都 200。所以写死一个稳定值即可，不受客户端版本升级影响。
- `weight: 0` **永不自动使用** —— 用户 08-28 已明确拒绝，且 `positiveWeightAuths` 会把 weight≤0 的凭据从池里滤掉，导致 `auth_not_found: no auth candidates`（尝试=0，不是降权而是消失）。

---

## 5. 判定规则表（正文关键词优先于状态码）

以 `probe-fix.py` 的 `classify` 为准（它比 audit 版多两类，且顺序修正过一次真实误判）。

| 类别 | 判据 | 处置 | 是否阻止导入 |
|---|---|---|---|
| **余额** | `budget pool` / `quota has been exhausted` / `insufficient_quota` / `预扣费额度失败` / `额度已达上限`，**与状态码无关**（可 402 也可 403） | 充值，**永不降权** | 否，导入但标记 |
| **封号** | 403 + `has been banned` | 该 **key** 降权，**不动同站其他 key** | 否，导入但降到最低档 |
| **限流** | 状态码 429 | 加大探测间隔重试 | 否，重试后判 |
| **门禁** | 403 且无 CF 特征；或 400 + `1m 上下文` | 站方后台开通，配置层无解 | 是（该段不可用） |
| **IP 封** | 403 + `challenge-platform` / `cf-mitigated` / `cdn-cgi` / `访问已被拦截` / `安全验证` | 加 `proxy-url` 后重试 | 视 ④ 结果 |
| **边缘** | 403 + 正文为空 | CF 概率拦截，重试即可 | 否 |
| **反测活** | `反测活` / `测活探针` | 换探测文本重试 | 否 |
| **死路** | `sensitive_words` / `无可用渠道` / `model_not_found` / `api key group` | 无解 | 是 |
| **临时** | `负载已达上限` | 稍后可用 | 否 |
| **注入** | 403 + `image generation` | 关 `disable-image-generation` | 否 |

**关键顺序**：余额判定必须排在 Cloudflare 判定**之前** —— 08-29 曾把 relay-l 的 `预扣费额度失败, 剩余 $0.190928` 误判为「站方策略门禁」，处置方向完全相反（一个该充值，一个该换 IP）。

---

## 6. 静默换模检测

返回 200 不等于可用。三重指纹，任一异常即标记：

| 指纹 | 判据 | 可靠性 |
|---|---|---|
| `model` 字段 | 与请求模型不符（经 `model_matches` 宽松匹配后仍不符） | 中 —— 上游能改这个字段 |
| `id` 形态 | `backend_of(resp_id)`：`msg_bdrk_`=Bedrock、`msg_01`+base58=Anthropic 官方、`msg_`+32hex=中转自造、`resp_`+40hex=OpenAI 官方 | **高** —— 上游改不了 id 生成方式 |
| `input_tokens` | 远小于发送量（<50%）= 上游截断，200 不可信 | 高 |

`model_matches` 的容错：`actual` 为 `None` 时**返回 True**（无证据不判换模）。这条是必须的 —— 08-28 曾因 `read(20000)` 截断导致解析不到 model 字段，报出 100% 假换模率。

---

## 7. priority 定档（方案：建议值 + 影响面，用户确认）

### 7.1 语义（已纠正）

**数值大者优先，且分层隔离**。源码：`sdk/cliproxy/auth/selector.go:325-333`

```go
bestPriority := 0
for priority := range availableByPriority {
    if !found || priority > bestPriority {   // 取最大值
        bestPriority = priority
    }
}
bucket := availableByPriority[bestPriority]  // 只保留最大那一桶
```

关键推论：**低 priority 凭据只在更高档全部不可用时才参与**，不是权重混合。同档内才走 weighted-round-robin。

> 旧版文档记为「升序·数值小者优先（`internal/registry/priority.go:39`）」，**该文件不存在**，已撤回。

### 7.2 现存档位谱（插档基准）

| 段 | 档数 | 顶档 | 可插空档（下界↔上界，宽度） |
|---|---|---|---|
| `claude-api-key` | 12 | 1000 relay-h | 400↔700 (300)、120↔300 (180)、800↔900 (100) |
| `codex-api-key` | 12 | 800 relay-c | 180↔600 (420)、110↔180 (70)、650↔750 (100) |
| `openai-compatibility` | 12 | 520 relay-k | 220↔360 (140)、380↔500 (120)、100↔220 (120) |
| `gemini-api-key` | 5 | 900 relay-g | 30↔900 (870) |

### 7.3 定档算法（2026-08-30 改：试用期默认）

**默认不按得分定档。** 新站进「挡住现有站最少」的那一档，得分只决定**提权建议**。

```
suggest_priority(band, score, models, probation=True)   # 默认
  ① 算出所有可插空档（相邻档间隔 > 1）
  ② 剔除会劫持顶层的空档（上界取该候选所声明模型现有顶层的 min）
  ③ 试用期：逐档算 shadow_count，取最少的；同数取更低值（给提权留空间）
  ④ 理由里写明「实测得分 N 可支持提到 M，但那会挡 K 站」

probation=False   # CLI --by-score / 网页取消勾选
  ③' 按 idx = (100 - score) / 100 * len(allowed) 选档，一步到位
```

为什么试用期是默认：探测分数只证明「此刻这一次请求成功了」，证明不了余额够用、
限流阈值、长时间稳定性、深夜是否降级。而按分数定档的代价是实测出来的 ——

| 段 | 试用期（默认） | 按得分 | 差别 |
|---|---|---|---|
| `gemini-api-key` | 12 · 挡 1 站 | 465 · 挡 9 站 | 少挡 8 |
| `codex-api-key` | 25 · **挡 0 站** | 145 · 挡 4 站 | 少挡 4 |
| `claude-api-key` | 30 · **挡 0 站** | 115 · 挡 2 站 | 少挡 2 |
| `openai-compatibility` | 25 · 挡 1 站 | 92 · 挡 3 站 | 少挡 2 |

按得分模式下，一个刚探测的新站在 claude 段拿到 975，**挡住 6 个已经跑了两夜、
证明过自己的站**。层级隔离下那些站只在新站也不可用时才被尝试 —— 等于用未知替换已知。

得分仍然有用，只是改了用途：从「决定档位」变成「决定提权上限」。跑几天确认稳定后，
按理由里给的数值手工提权。

**硬约束：不修改任何现有条目的 priority。** 只在空档插入。这条是 08-29 的教训 ——
第一版 620 方案曾劫持 4 个模型的顶层。

**避让上限取 `min` 而非 `max`**（`plan.suggest_priority`）。同时声明 `opus-5`（顶层 1000）
与 `sonnet-5`（顶层 120）的候选，若按 `max` 算会拿到 975 —— 不动 opus-5，却把 sonnet-5
的顶层整个换掉。越过**任一**模型的顶层就是劫持那个模型，所以按最低的那个算。

### 7.4 影响面必须算两件事（2026-08-30 补）

原设计只写了「会成为哪些模型的顶层候选」，那只覆盖一半。

| | 判定 | 后果 |
|---|---|---|
| **抢顶层** `hijacks` | 新值 > 该模型现有顶层 | 原首选站再也不被首选 |
| **挡下层** `shadowed` | 该模型在低于新值的档上还有站 | 那些站只在新站**也**不可用时才被尝试 |

第二件同样重要，却是「没劫持顶层」时最容易漏看的：gemini 段插 465 不动 relay-g 的 900，但把 30/20/15/10 四档共 **9 个站**全挡在后面。实现按模型分桶算（`Band.model_tiers` / `Band.shadowed`），因为「挡住谁」取决于哪些站承载同一个模型 —— 30 档那 6 个站里只有部分承载 `gemini-2.5-pro`。

**空档内取任何值效果完全相同。** 实测 gemini 段 `(30,900)` 空档内插 35、200、465、700、890 —— 被挡站点都是同 9 个。真正的选择是**挑哪个空档**，所以「建议手工调低」毫无操作性。`plan.gentler_option` 直接给出下一档的确切数值与代价对比：

```
priority 465 会把 9 个现有站挡在其后（…）—— 它们只在本站也不可用时才被尝试。
改成 25 则只挡 4 站（少 5 个）
```

---

## 8. 去重（两种失败模式，都必须挡）

CPA 各段行为**不一致**，源码 `internal/config/config_normalization.go`：

| 段 | 重复时的行为 | 后果 |
|---|---|---|
| `gemini-api-key` | 按 (api-key + base-url + proxy-url + prefix + headers) 五元组判重，**冲突条目静默丢弃**（:237-273） | 你以为加了，实际少一个 |
| `claude-api-key` | **不去重**（:217-235） | 注册成两个独立凭据，轮询池占两个位 |
| `codex-api-key` | **不去重**（:197-214），缺 base-url 才丢 | 同上 |
| `openai-compatibility` | **不去重**，缺 base-url 才丢 | 同上 |

另有一层：Auth 对象合成时（`internal/watcher/synthesizer/helpers.go:29-52`）SHA256 哈希撞了会加 `-1` / `-2` 后缀，**不合并** —— 四段统一行为。

**服务必须在导入前自己按五元组判重**，对已存在的条目给出「已存在，跳过」而不是静默处理。

---

## 9. `max-context-length` 探测（默认开，仅新增账号一次）

### 9.1 为什么值得做

字段存在于四段的 Model 结构体（`config_types.go:430 / 526 / 623 / 716`），但当前 config.yaml 里 **678 个模型条目全部为空**。

作用链（不参与选站，只影响客户端行为）：

```
config.yaml  models[].max-context-length
  → sdk/cliproxy/service_models.go:702-706    info.ContextLength / MaxContextLength
  → internal/registry/model_registry.go:1242  "max_context_length"
  → internal/client/codex/models/models.go:207-211
        entry["context_window"]     = N
        entry["max_context_window"] = N
  → Codex 客户端 /models 响应 → 客户端据此定本地自动压缩阈值
```

这正是 08-28 那个 400 的解法：客户端按 1,000,000 算、到 967k 才压缩，而 relay-h 真实上限 995,988，只剩 3% 余量；而 400 **不在** `isCredentialRetryRoundStatus` 白名单（`conductor_selection.go:1038` 只含 403/408/429/500/502/503/504），命中即终止本轮，其余上游一次都不试 —— 于是「无论怎么发消息都是这个错误」。

写入真实上限后，客户端在正确的点压缩，不需要改客户端任何设置。

### 9.2 探测方式

复用 `context-probe.py` 的 `bisect_limit`：

- 收敛条件：区间宽度 ≤ 20000 字符，或轮数用完（默认 4-5 轮）。
- **截断检测**：200 且 `input_tokens < chars * 0.5` → 标记 `untrusted`，停止二分。200 不代表上游真吃下了（relay-m 发 105 万字符只回 132,696 tokens，且模型被换）。
- 成本：单站单模型约 4-6 次请求。批量 20 站需给出预估并要求确认。

### 9.3 默认策略

**默认开，仅对本次新增的 (站, 段, 模型) 组合跑一次**，不重测已有条目。单站可关。

### 9.4 只写给实测的那个模型（2026-08-30 修正）

原实现探测 `v.models[0]`，却把结果写给该段**全部**模型：

```yaml
models:
  - name: "gemini-2.5-pro"
    max-context-length: 1100000    # 实测的是这个
  - name: "gemini-2.5-flash"
    max-context-length: 1100000    # 没测过，抄的
```

同站不同模型的窗口能差一个数量级。把 pro 的实测值抄给 flash，客户端按错误的窗口定压缩点 —— **正是 §9.1 那条 400 的成因**，等于用这个功能重造了它要修的 bug。

修法：`SectionVerdict.context_model` / `SectionPlan.context_model` 记住实测于哪个模型，`writeback.render_entry` 只给它写这个字段，其余模型留空由 CPA 回落内置目录值（`service_models.go:67-156` 的 registry fallback）。前端显示为 `上限 1,100,000 @ gemini-2.5-pro`，让数字的来源可追。

要覆盖全部模型只能逐个探测 —— 那是 N× 成本，需用户显式选择，当前不做。

---

## 10. 写回（方案 C）

```
① 备份      config.yaml → config.yaml.bak-{timestamp}   ← 必须，见下
② 行级编辑  在内存中按行插入/修改，保留全部注释与缩进
③ 本地校验  yaml.safe_load 通过 + 段内条目数符合预期
④ diff 预览 逐行 diff 呈现给用户，显示「将新增 N 条、修改 M 处」
⑤ 用户确认  ← 硬闸门，不可跳过
⑥ 写回      PUT /v0/management/config.yaml，请求体为完整原始字节
⑦ 验证      GET /v0/management/config → 比对生效值
```

### 10.1 为什么必须自己先备份

`PUT /v0/management/config.yaml` 的**写盘不是原子的**。源码 `internal/api/handlers/management/config_basic.go`：

- 校验链本身是稳的：读 body → `yaml.Unmarshal` 语法检查（:125）→ 写同目录临时文件（:130-150）→ `LoadConfigOptional` 全量语义校验（:151）→ 才放行。
- 但落盘用 `O_TRUNC` 直写（:101-116）：**中途崩溃留下被截断的 config.yaml**。
- 且 `LoadConfig` 在写盘后失败时**不回滚**（:163-167 只返回 500，文件已被覆盖）。

### 10.2 注释保留已验证

- `GET /v0/management/config.yaml`（:174-189）返回磁盘原始字节，不重新序列化，注释与格式原样保留。
- `WriteConfig`（:101-102）写前只调 `NormalizeCommentIndentation` —— 仅规范注释缩进，不重排内容。
- 所以「读原文 → 行级编辑 → 写回」这条链对注释安全。

### 10.3 为什么不用 PATCH

`PATCH /v0/management/{section}` 按 `index` 或 `match` 定位**已存在**条目，找不到返回 404（`config_lists.go:191-216`，四段同模式）。**不能新增**。

四段能新增的只有两条路：整段数组 `PUT /{section}`（会丢注释），或 `PUT /config.yaml`（保留注释）。**无「新增单个凭据」的专用端点**。

### 10.4 热重载

不需要 restart。两条路径都会触发：

- `PUT /config.yaml` 的 handler 自己在进程内重载 `h.cfg`（:163-168）。
- 独立的 fsnotify watcher 也会收到写事件，`configReloadDebounce = 150ms` 去抖后重载（`internal/watcher/watcher.go:87`），并用 SHA256 比对跳过空重载（`config_reload.go:61-71`）。

重载是**全量重建** —— `reloadConfig()`（`config_reload.go:88-144`）重新合成全部 Auth 并与上一份快照 diff，不做增量打补丁。

---

## 11. models 留空的陷阱（段间不一致）

| 段 | `models: []` 时 | 源码 |
|---|---|---|
| gemini / claude / xai / vertex-compat | 回退内置模型目录（**等于全放开**） | `service_models.go:67-74 / 108-115 / 149-156 / 90-97` |
| codex | 回退 `registry.GetCodexProModels()` | `service_models.go:821-827` |
| **openai-compatibility** | **注册 0 个模型，该 provider 完全不可用** | `service_models.go:714-717` |

**导入 compat 段时必须探测出至少一个可用模型**，否则写进去等于没写。

---

## 12. 验证不能只靠 `api-call`

`POST /v0/management/api-call`（`api_tools.go:99-215`）是**裸 HTTP 转发**：直接按 body 里的 `method/url/header/data` 构造请求，不走 translator、不做 cloak / fingerprint 改写，返回 `{status_code, header, body}` 原样。

它能验证「凭据本身有效」，**不能**验证「接进 CPA 后客户端能否正常用」。

**两级验证**：
1. 写入前：`api-call` 做快速凭据检查（或服务自己直连，等价）。
2. 写入后：打 CPA 自己的 `/v1/messages`（claude）或 `/v1/responses`（codex）做端到端确认。

---

## 13. 数据模型

新增两张表，沿用 CPAMP 现有 SQLite 模式（`apps/manager-server/internal/repository/sqlite/migrate.go`，在末尾追加 `create table if not exists`）：

```sql
create table if not exists upstream_import_jobs (
  id              integer primary key autoincrement,
  created_at_ms   integer not null,
  finished_at_ms  integer,
  status          text not null,          -- parsing|probing|awaiting_confirm|writing|done|failed
  raw_input       text not null,          -- 原始粘贴/上传内容，便于复现
  probe_context   integer not null default 1,  -- 是否跑 max-context-length 探测
  total_items     integer not null default 0,
  error           text
);

create table if not exists upstream_import_items (
  id              integer primary key autoincrement,
  job_id          integer not null,
  base_bare       text not null,          -- 规范化后的裸域名
  api_key_masked  text not null,          -- 仅存脱敏值，完整 key 不落库
  section         text,                   -- gemini|codex|claude|compat
  verdict         text,                   -- 余额|封号|限流|门禁|IP封|边缘|反测活|死路|临时|注入|OK
  http_status     text,
  need_proxy      integer default 0,
  need_ua         integer default 0,
  models_json     text,                   -- 探测到的可用模型
  swap_detected   integer default 0,
  backend_form    text,                   -- backend_of(resp_id) 结果
  max_context     integer,
  max_ctx_trusted integer default 1,
  suggest_priority integer,
  dedup_hit       integer default 0,       -- 与现有条目五元组重复
  body_excerpt    text
);
```

**红线：完整 api-key 不落库**。探测期间只在内存中持有，落库一律脱敏。

---

## 14. API 端点

挂在 CPAMP manager-server 下（`internal/http/router/router.go` 注册）：

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/v0/management/upstream-import/jobs` | 提交 txt / 粘贴文本，创建任务，立即返回 job_id |
| GET | `/v0/management/upstream-import/jobs/:id` | 轮询进度与逐项判定结果 |
| GET | `/v0/management/upstream-import/jobs/:id/diff` | 取待确认的 config.yaml diff |
| POST | `/v0/management/upstream-import/jobs/:id/confirm` | 用户确认后执行写回 |
| POST | `/v0/management/upstream-import/jobs/:id/cancel` | 取消 |

进度回报走**HTTP 轮询**，不引入 SSE / WebSocket —— 与 CPAMP 现有做法一致（`ServerCodexInspectionPage.tsx` 每 30 秒 `setInterval`），worker 把进度写 SQLite，前端拉取。

---

## 15. 前端

新增路由 `/ai-providers/batch-import`（`apps/web/src/router/MainRoutes.tsx`）。技术栈沿用现有：React 19 + Vite + TypeScript + SCSS Modules，主题 token 取 `styles/themes.scss`。

四步向导：

```
① 输入    txt 拖拽上传 或 文本框粘贴 · 实时行数与格式校验
② 探测    进度条 + 逐项状态流水（复用 ProviderHealthCheckDrawer 的呈现模式）
③ 判定    结果表：段归属 · 判定类别 · 代理/UA · 模型 · 上限 · 建议 priority
          每项可手工覆盖；显示 priority 影响面（会成为哪些模型的顶层）
④ 确认    config.yaml diff 预览（复用 components/config/DiffModal.tsx）→ 确认写回
```

可复用的现成件：
- `AuthJsonPasteModal.tsx` —— 粘贴 + 校验 + 提交的表单模式
- `AccountsBatchDeletePreview.tsx` —— 批量操作预览的呈现模式
- `ProviderHealthCheckDrawer/` —— 并发探测 + 分组结果 + 批量应用
- `components/config/DiffModal.tsx` —— YAML diff

---

## 16. 复用清单（不重写）

### 可直接抽为共享库（纯函数）

`host_of`、`backend_of`、`resp_model`、`resp_id`、`model_matches`、`site_identity`、`identity_headers`、`cpa_baseline_headers`、`fallback_combos`、`classify`、`build_request`、`rank_models`、`body_excerpt`、`bisect_limit`

### 需重构才能复用

- `fetch` / `probe` / `post` / `once` —— 三个脚本用了三种底层（`urllib.request` ×2、`subprocess.run(curl)` ×1），须统一为一种并去掉 print。
- `classify` 两版需先合并：以 probe-fix 版为准（多「反测活」「注入」两类，且余额判定顺序已修正）。
- `build_request` 的 claude 段鉴权头需统一（见 §4）。
- `report` / `write_html` —— 强耦合 print / HTML 拼接，改为返回结构化数据。
- `load_key` —— `sys.exit` 改抛异常。

---

## 17. 红线

1. **完整 api-key 不落库、不进日志、不出现在前端响应体**，一律脱敏。
2. **不修改任何现有条目的 priority**，只在空档插入新值。
3. **不自动使用 `weight: 0`** —— 用户已明确拒绝，且会导致凭据从池中消失而非降权。
4. **写回前必须备份**，`PUT /config.yaml` 非原子且失败不回滚。
5. **diff 确认是硬闸门**，不提供「跳过确认直接写入」的选项。
6. **单 key 问题只处置该 key**，不波及同站其他凭据（08-29 教训：因 1 个 key 被 ban 误降 46 处）。
7. 余额类**永不降权** —— 充值即自愈，降权会造成长期容量损失。

---

## 18. 实施状态（2026-08-30 回填）

| # | 项 | 状态 | 落点 |
|---|---|---|---|
| 1 | 抽共享库 `cpa_probe/` | ✅ | 9 模块 · `parse` `classify` `request` `client` `fingerprint` `pipeline` `plan` `writeback` |
| 2 | 合并两版 `classify`，统一 claude 鉴权头 | ✅ | 以 probe-fix 版为准；claude 段 `Authorization` 与 `x-api-key` **两个都发** |
| 3 | SQLite 迁移（两张表） | ❌ **未做，改为内存态** | 见下方说明 |
| 4 | 后端 service：解析 → 探测 → 判定 → 定档 | ✅ | `pipeline.Prober` 四阶段 + `plan.build_plan` |
| 5 | 后端 worker：任务队列 + 进度写库 | ⚠️ **改为线程 + 内存事件流** | `server.Job` / `server.Store` |
| 6 | 行级 YAML 编辑器（保留注释）+ diff 生成 | ✅ | `writeback.build_diffs` / `apply_diffs`，实测 8223 行注释一条未丢 |
| 7 | 写回链：备份 → 校验 → PUT → 验证 | ✅ | `write_local`（原子替换）→ `push_to_cpa` |
| 8 | API controller（5 个端点） | ✅ | 实际 6 个：`/api/context` 另加 |
| 9 | 前端四步向导 | ✅ | `web/index.html` + `web/app.js` |
| 10 | 端到端验证（写入后打 CPA 自身端点） | ✅ | `writeback.verify_upstream`，CLI `--client-key` / 前端「客户端 key」 |

### 为什么 3 与 5 偏离了原设计

原设计要建 `upstream_import_jobs` / `upstream_import_items` 两张 SQLite 表，
沿用 CPAMP 的持久化模式。实现时改成了纯内存（`Store` 里两个 dict + 一把锁）。

理由：

- **这是按需启的运维工具，不是常驻服务。** systemd 单元里 `Restart=on-failure`
  且 compose 里 `restart: "no"` —— 用完就关。跨重启恢复任务没有意义。
- **持久化会把明文 Key 落到磁盘。** 探测过程必须持有完整 Key；落库就要么存明文
  （违反红线 1），要么加一套加密与密钥管理。内存态天然满足「Key 只在内存」。
- **CPAMP 的表是给巡检 worker 用的**（`codex_inspection_*` 那套要跨天聚合），
  这里的任务生命周期是分钟级。

代价与已知边界：

- 服务重启后进行中的任务丢失，需重新粘贴。已在前端提示。
- 方案（`plan_id`）也在内存，重启后 `/api/apply` 返回 404「请重新生成」。
- 若将来要长开成常驻服务、或要审计历史导入，就得补这两张表 —— 那时 Key
  必须按 `plan.dedup_key` 的方式只存指纹，完整值不落库。

### 未纳入的一项

**每站多 Key 的批量导入**：当前一行 `url,key` 对应一个凭据。同一站多个 Key
要写多行，服务会逐行探测（相同 base-url 会重复探测四段）。优化空间是按 host
分组、段判定复用、只对每个 Key 单独验证一次凭据有效性。当前实现正确但偏慢：
同站 5 个 Key 约 5× 请求数。未做，因为它只影响耗时不影响正确性。
