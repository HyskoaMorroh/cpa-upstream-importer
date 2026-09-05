# 全量重探功能设计

**需求**：前端勾选框控制，全量重新探测 config.yaml 所有既有站，与新站一起
重新生成配置。

**「全部更新」这个初版说法已经作废（2026-09-04）**。逐字段的处置不一致，
而且必须不一致 —— 判据是「这个字段是谁的属性」：

| 字段 | 处置 |
|---|---|
| `models` | 实测替换 |
| `priority` | **沿用原档**，只有新站才定档 |
| `proxy-url` | 探测有值优先，否则搬原值 |
| `headers` | **合并**：原值为底、探测值覆盖同名键、`anthropic-beta` 走 `betas.merge` |
| `websockets` / `support-prompt-cache-key` | **实测三态**（True 写 / False 连原值一起关 / 未探测按原值搬） |
| `weight` / `prefix` / compat 的 `name` / `models[].alias` | 只搬原值 |
| `max-context-length`（模型级） | 本次实测 > 原值搬运 > 不写 |
| 白名单外字段 | carry 原文逐字搬 |

完整理由（含为什么 headers 是合并而能力开关是实测优先，两者方向相反）见
README 的「重探时每个字段以哪一侧为准」。

**实测数据**（2026-09-01，真实 config.yaml）：
- YAML 条目 121 个（compat 13 个 provider、claude 65、codex 27、gemini 16）
- 展开成 (凭据,段) 组合 177 个 —— compat 的 13 个 provider 下挂 69 把 Key
- **去重后 79 个不同凭据**（14 个主机）
- 单凭据四段全不通 30 次请求（优化前 57）
- 总请求数最坏 2,370（优化前 10,089，省 77%）
- 48 并发下约 0.9 分钟（并发数按 cgroup 实测推荐，4U24G 得 48）

---

## 一、两个 CPA 项目的字段契约

### CLIProxyAPI（config.yaml 的消费方）

> **2026-09-05 补全**：下面这份清单此前停在旧版，漏掉 11 个字段（`excluded-models`、
> `request-scoped-errors`、`cloak`、`websockets`、`alpha-search` 等）。
> 逐个结构体核对 `internal/config/config_types.go` 后重列。
> README 的「能力开关靠实测决定开或不开」那一节有一份**按性质分类**的表，
> 两份互补：这里回答「有哪些字段」，那里回答「哪些该由探测决定」。

**四段共同字段**

| 字段 | 类型 | 说明 |
|---|---|---|
| `api-key` | string | 上游凭据（compat 段在 `api-key-entries` 里） |
| `base-url` | string | 形态按段不同，见下 |
| `priority` | *int | 数值越大越优先，未设置时 `authPriority` 返回 0 |
| `prefix` | string | 路由前缀（`force-model-prefix: false` 时是加别名而非替换） |
| `headers` | map | 一路到达上游请求（`header_helpers.go` 的 `Set` 覆盖） |
| `proxy-url` | string | claude/codex/gemini 是条目级，**compat 是 per-key** |
| `weight` | *int | 只在 `weighted-round-robin` 下读；`<=0` 与 `>1000000` 都归零 |
| `excluded-models` | []string | 屏蔽指定模型；含 `"*"` 时等于**停用该凭据**（管理面板的停用按钮就写这个） |
| `disable-cooling` | bool | 本地策略：出错不冷却 |
| `request-retry` | *int | 本地策略：重试次数 |
| `request-scoped-errors` | []object | 按状态码 + 正文正则定制冷却（生产配置 116 条） |

**段专属字段**

| 段 | 字段 | 性质 |
|---|---|---|
| claude | `fingerprint-profile` | 让 CPA 自己补设备指纹（可选值 `claude-code-cli`） |
| claude | `rebuild-mid-system-message` | 本地行为：把 role=system 消息挪到顶层 system |
| claude | `experimental-cch-signing` | 本地行为：CCH 签名 |
| claude | `cloak`（含 `strict-mode` / `sensitive-words` / `cache-user-id`） | 本地改写行为 |
| codex | `websockets` | **上游能力**，本工具实测握手后写入 |
| codex | `alpha-search` | 授权类，只搬不探（探它要发计费的搜索请求） |
| compat | `name` | **CPA 的 provider 身份**（`provider_key` 由它算），改名作废冷却与能力缓存 |
| compat | `api-key-entries` | 多 Key 挂在一个 provider 下 |
| compat | `disabled` | 用户显式停用；CPA 遇它直接 continue，连 Auth 都不合成 |
| compat | `support-prompt-cache-key` | **上游能力**，本工具实测请求后写入 |

**模型级字段**（`models[]` 里，四段都有）

| 字段 | 说明 |
|---|---|
| `name` | 必填（compat 段的 `Models` 无 `omitempty`，`config_types.go:679`） |
| `alias` | 空串回落到 `name`（`service_models.go:678-682`） |
| `max-context-length` | token 数，客户端按它定压缩点 |
| `thinking` | 思考档位声明；compat 段留空时 CPA 自动给 `["low","medium","high"]` |
| `is-compat`（codex） | **上游能力**，但只在 `codex.optimize-multi-agent-v2` 也为 true 时生效 —— 生产配置那个是 false，所以只搬不探 |
| `is-compat`（claude） | **上游能力且无条件生效**（`config_types.go:443-447`：保留空签名的 thinking 块 + 签名回放）。仍然只搬不探 —— 判断一个站接不接受空签名 thinking 要**两轮有状态对话**（先拿到带签名的块，再回放），而探测每次请求都独立 |
| `display-name` / `force-mapping` / `image` / `input-modalities` / `output-modalities` | 声明与本地映射 |

**base-url 规范**（按段不同）：
- gemini / claude：裸域名，不带 `/v1`
- codex / compat：必须带 `/v1`

**priority 语义**：数值**越大越优先**（`priorityOrder` 降序排，`scheduler.go:1229-1231`；取层用 `priority > bestPriority`，`scheduler.go:402` 与 `selector.go:541-543`）。未设置时 `authPriority` 返回 0（`selector.go:365-372`），是**最低**优先。

> 2026-09-03 更正：此前本文与 README、tutorial 都写成「越小越优先（升序排列，`scheduler.go:1085`）」。那个行号上不是排序代码，结论也与源码相反。

**headers**：`map[string]string`，CPA 不限制 key 集合，以 `header:` 前缀存进 `Auth.Attributes`，转发时原样发出。

**prefix**：为该条目所有模型注册 `<prefix>/<model>` 别名，与原名同时可用。

**热重载**：`PUT /v0/management/config.yaml` 写完即同步重载 `h.cfg`；文件变更由 fsnotify 监听（150ms 去抖 + SHA256 比对）后走完整 `reloadClients()`。

### CPA-Manager-Plus（另一个写方）

- 不直接改文件，走 CPA 的 `GET/PUT /config.yaml`
- **全量 PUT 重写**，与 upstream-importer 的行级插入是两种模式
- **冲突风险**：两者同时写时，后写的覆盖先写的。缓解手段是 upstream-importer 的基线比对（`_api_apply` 里 `raw_now != base_raw` 就 409），但只能挡住"在生成方案之后被改过"，挡不住"同一秒并发"

### 字段映射完整性

upstream-importer 生成的每个字段都在 CPA 支持范围内，无未知字段：

| upstream-importer | CPA | 来源 |
|---|---|---|
| `base-url` | ✅ 必填 | 用户输入，按段规范化 |
| `api-key` | ✅ 必填 | 用户输入 |
| `priority` | ✅ | 定档算法 |
| `prefix` | ✅ | 站级前缀分配 |
| `headers` | ✅ | 画像探测实测 |
| `proxy-url` | ✅ | 代理预检 |
| `model` | ✅ | 模型发现阶段 |
| `max-context-length` | ✅ | 上下文二分探测 |
| `fingerprint-profile` | ✅ claude 段 | 画像通档时建议 |

---

## 二、现有架构（子代理探索结论）

### 探测引擎

- **段级并发**：`Prober(workers=4)` 用 ThreadPoolExecutor 并发四段
- **站级串行**：`probe_candidate()` 是单站入口，多站靠外层循环
- **画像串行**：每段的画像梯（baseline → min → std → full → body）串行尝试，首个通过即停
- **single-flight**：同 `(host, section)` 的形态学习只做一次，后到者等结果复用
- **单站请求量**：4 段全通不开上下文约 32 次；全不通需画像救援约 60 次

### 写回

- **行级插入**：`build_diffs()` + `apply_diffs()` 只在段尾插入，不动既有行
- **注释保全**：`_section_span()` 剥离段尾注释，让插入点落在最后一个真实条目后
- **就地覆写**：`write_local()` 用 O_TRUNC 保 inode 不变（容器单文件 bind mount 的硬要求）
- **无全量重建函数**（本次新增）

### Web 服务

- **接口**：`/api/parse`（零请求解析）、`/api/probe`（起任务）、`/api/job/{id}?since=N`（轮询进度）、`/api/plan`（生成方案）、`/api/apply`（确认写回）
- **进度**：HTTP 轮询，1.5 秒间隔，`since` 游标拉增量
- **并发保护**：`_apply_lock`（写回串行）、`_cfg_cache_lock`（配置缓存）、`_fail_lock`（限频记录）

---

## 三、本次新增

### 1. `cpa_probe/batch.py`

```python
class BatchProber:
    """站级批量探测器（每站内部仍走 Prober 的段级并发）"""
    def __init__(self, prober: Prober, max_workers: int = 30)
    def probe_batch(rows, progress_callback) -> dict[str, CandidateResult]

def extract_existing_entries(cfg) -> list[tuple[str, str, str, str]]:
    """提取所有既有站 (section_short, base_url, api_key, yaml_key)"""
```

**并发模型**：站级并发（默认值由 `resources.detect()` 读 cgroup 算出，4U24G 得 48）× 段级 4 并发。节流仍按 `(host, section)` 分桶，同段之间保持 gap 秒 —— 并发放大的是不相干部分的吞吐。

### 2. `cpa_probe/writeback.py`

```python
def rebuild_config_full(cfg, all_plans, original_lines) -> tuple[str, list[str]]:
    """全量重建：保留全局配置与人工注释，重建所有条目，按 priority 降序"""

def _extract_entry_comments(lines) -> dict[str, dict[str, list[str]]]:
    """按 (段, name/host/base-url) 三候选键索引人工注释"""
```

条目渲染**复用既有的 `render_entry()`**，不另写一套——它已经处理了按段分结构、
compat 走 provider + api-key-entries、`extra_keys` 归并同站多 Key、以及控制字符
转义。第五节记了自己另写一套的后果。

### 3. `server.py`

- `run_job_full_redetect()`：提取既有站 + 合并新站 + BatchProber 并发探测 + 进度事件
- `_api_probe`：认 `full_redetect` 与 `max_workers` 参数，选择执行函数
- `_api_plan`：全量重探模式走 `rebuild_config_full`，返回整文件 diff
- `_api_context`：返回 `existing_count` 供前端提示

### 4. `cpa_probe/resources.py`（并发数不靠猜）

```python
def detect(*, floor=4, cap=64) -> Resources:
    """读 cgroup 算推荐并发。容器里 os.cpu_count() 是宿主机核数，不能用。"""
```

优先 cgroup v2（`cpu.max` / `memory.max`），其次 v1（`cfs_quota_us` /
`limit_in_bytes`），再回落 `sched_getaffinity`、最后 `os.cpu_count()`，
并在返回值里标明来源。推荐值 = min(核数 × 12, 内存一半 ÷ 12MB, 64)，下界 4。

docker 实测五档：0.5核/256M→6、1核/512M→12、2核/1G→24、**4核/24G→48**、
8核/2G→64（被内存 85 与上界一起压住）。

### 5. 请求数优化（三项，省 77%）

- **按凭据去重**（`run_job_full_redetect`）：按 `(host_of(base_url), api_key)`
  折叠。177 组合 → 79 凭据。键不能用 `base_url` —— 同一站在不同段形态不同
  （codex/compat 带 `/v1`）
- **画像结论按 (站, 段) 复用**（`Prober._profiles_failed`）：整梯全败后同段
  后续种子跳过。门票是站+段的属性，站方查 headers 与 body 形态不看模型名。
  假上游实测 57 → 30 次
- **模型验证补尝试上限**（`MAX_MODEL_ATTEMPTS_PER_SECTION=10`）：原来的
  `MAX_MODELS_PER_SECTION` 只数成功的，声明 838 个模型的聚合站会被全打一遍

三项都做成 `Prober` 参数，前端可调。

### 6. 前端

- `web/index.html`：全量重探勾选框、并发数输入框 + 「用推荐值」按钮、
  折叠的「高级：请求预算」区（收模型数 / 尝试上限 / 画像复用开关）
- `web/app.js`：显示资源探测依据、实时预算估算、透传全部新参数

---

## 四、测试

`tests/test_full_redetect.py`（10 项，已挂进 `tests/run.py` 清单）：

1. `extract_existing_entries` 提取 7 个站（2 gemini + 1 codex + 2 claude + 2 compat）
2. `BatchProber` 10 站并发 + 10 次进度回调
3. `BatchProber` 统计分类（success / partial / failure）
4. `BatchProber` 单站异常不影响整体
5. `rebuild_config_full` 保留全局配置与人工注释、priority 正确更新、YAML 有效
6. `rebuild_config_full` 输出按 priority 降序
7. 段字段结构与 compat 归并（锁第五节那三个缺陷）
8. 凭据去重：5 条目 → 2 凭据，且 `/v1` 不影响判定
9. 探测文本非问候：13 种问候形态、长度下限 40、必须含技术词，并扫 pipeline.py
   里所有硬编码 `text=`
10. 画像复用省请求：起假上游真数 HTTP 请求，断言省幅 ≥30%（实测 47%）

**全量测试**：自带样本 864 项、真实 config.yaml 866 项，均 exit 0。

`tools/e2e_redetect.py` 走完整条链：起假上游 → 造含既有站的 config → 批量探测
→ `build_plan` → `rebuild_config_full` → `validate` → `write_local` → 读回比对。
用完即弃的临时目录，失败会 raise。

---

## 五、端到端抓到的四个缺陷（已修）

第一版的 854 项单元测试全绿，四个缺陷一起放过去了。原因是那批测试的构造数据
是按我自己的理解写的，跟真实产出不一样——**测试只能证明代码符合我的意图，
不能证明我的意图对**。

### 缺陷 1：产出一个四段皆空的文件（最严重）

`rebuild_config_full` 里写了「短名 → 完整段名」的映射表，而 pipeline 与
`build_plan` 一路用的本来就是完整段名（`gemini-api-key`）。拿完整名去查这张表
必然全部 miss，7 个可写段被逐个判「映射失败」跳过。

**它完全静默**：产出的 YAML 合法、`validate()` 通过、顶层键都在，只有条目数是 0。
全量重探本来就要重写整个文件，这一条会把 121 个条目清空，而写回链上没有任何一环
会拦住——备份是唯一的退路。

### 缺陷 2：前三段被写了 `name` 字段

CLIProxyAPI 的 gemini/codex/claude 段没有这个字段，只有 compat 有。

### 缺陷 3：compat 段按扁平结构渲染

它要的是 `name` + `models[{name,alias}]` + `api-key-entries[{api-key}]`，
而且同一个站的多个 Key 必须归并进一个 provider——重名 provider 会让冷却、
模型能力、执行路由三处对同一个 Key 命中两套配置。

### 缺陷 4：注释被复制到同站的每个条目上

前三段是每个 Key 一条，按 host 匹配注释会让原文件里只出现一次的注释在重建后
出现 N 次。

### 修法

删掉自己写的 `_render_entry_full` 与 `_quote`，**改用仓库里已有的
`render_entry()` 与 `_yaml_str()`**。缺陷 2、3 的正确做法那两个函数里本来就有
（按段分结构、compat 走 provider + api-key-entries、`extra_keys` 归并、控制字符
转义）——重复造轮子还造错了。compat 归并改成与 `build_diffs` 同一套
`(host, base_url)` 分组。缺陷 4 加段级 `used` 集合，同一份注释只挂第一个条目。

注释索引的键也改了：从「base-url 原文」改成同时建 `name`、`host` 与原文三个候选。
`sp.base_url` 是 `base_for_section()` 的产物（codex/compat 补了 `/v1`），与原文件
里写的对不上，只用一个键会静默丢注释。

### 附带发现

`test_full_redetect.py` 一开始**没挂进 `tests/run.py` 的清单**，CI 从来没跑过它。
已挂进去，并改成与其余套件同一套汇报约定（`全部通过 · N 项`），否则统计不到。

---

## 六、风险与已有缓解

| 风险 | 缓解 |
|---|---|
| 临时 503 被判死 | 探测层已有 503 重试（前序会话修复）；diff 需人工确认 |
| 高并发打爆站方 | `_Throttle` 按 `(host, section)` 分桶，同段保持 gap；并发数前端可调 |
| 全量重写出 bug | `write_local` 写前必备份；`validate()` 校验 YAML；863 项测试 + 端到端脚本 |
| 与 CPAMP 并发写 | `_api_apply` 基线比对，被改过就 409 要求重新生成 |
| 注释丢失 | 三候选键匹配 + 段级去重，测试覆盖 |

**未覆盖的风险**：只在假上游、单元测试与一次端到端上验证过，没有对这 79 个
真实站跑过。第一次实跑建议先看 diff 不写回。

---

## 七、用法

1. 打开网页，勾选「全量重探模式」
2. 界面显示既有条目数（当前 121）与推荐并发（读 cgroup 算出，4U24G 得 48）
3. 需要时展开「高级：请求预算」调三项，旁边有实时估算
4. 点「开始探测」，二次确认
5. 实时进度（先报去重结果，再报探测进度）：
   ```
   按 (站, Key) 去重：176 个条目 → 78 个凭据，省掉 98 次重复探测
   探测进度：42/78 · 成功 12 · 部分通 25 · 失败 5
   ```
6. 探测完成后生成整文件 diff
7. 逐项确认后点「确认写回」
8. 写回前自动备份，写回后推 PUT 触发 CPA 重载

不勾选时行为与之前完全一致：只探测新粘贴的站，行级插入，既有条目一个字节不动。
