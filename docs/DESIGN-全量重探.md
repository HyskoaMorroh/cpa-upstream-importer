# 全量重探功能设计

**需求**：前端勾选框控制，全量重新探测 config.yaml 所有既有站，与新站一起重新生成配置（headers/代理/优先级/前缀全部更新）。

**实测数据**（2026-09-01，真实 config.yaml）：
- 既有条目 175 个（compat 69、claude 63、codex 27、gemini 16），**去重后 77 个凭据**
- 单凭据四段全不通 30 次请求（优化前 57）
- 总请求数最坏 2,310（优化前 9,975，省 77%）
- 48 并发下约 0.9 分钟（并发数按 cgroup 实测推荐，4U24G 得 48）

---

## 一、两个 CPA 项目的字段契约

### CLIProxyAPI（config.yaml 的消费方）

**四段共同字段**：`api-key`、`base-url`、`priority`、`prefix`、`headers`、`proxy-url`、`weight`

**段专属字段**：
- claude-api-key：`fingerprint-profile`（可选值 `claude-code-cli`）
- openai-compatibility：`name`、`models`、`api-key-entries`、`disabled`、`support-prompt-cache-key`、`disable-cooling`、`request-retry`

**base-url 规范**（按段不同）：
- gemini / claude：裸域名，不带 `/v1`
- codex / compat：必须带 `/v1`

**priority 语义**：数值**越小越优先**（升序排列，`scheduler.go:1085`）。未设置默认 0（最高优先）。

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
  折叠。175 条目 → 77 凭据。键不能用 `base_url` —— 同一站在不同段形态不同
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
全量重探本来就要重写整个文件，这一条会把 175 个站清空，而写回链上没有任何一环
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

**未覆盖的风险**：只在假上游、7 项单元测试与一次端到端上验证过，没有对 175 个
真实站跑过。第一次实跑建议先看 diff 不写回。

---

## 七、用法

1. 打开网页，勾选「全量重探模式」
2. 界面显示既有条目数（当前 175）与推荐并发（读 cgroup 算出，4U24G 得 48）
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
