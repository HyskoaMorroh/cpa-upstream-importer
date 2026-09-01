# 全量重探功能设计

**需求**：前端勾选框控制，全量重新探测 config.yaml 所有既有站，与新站一起重新生成配置（headers/代理/优先级/前缀全部更新）。

**实测数据**（2026-09-01，真实 config.yaml）：
- 既有站 175 个（compat 69、claude 63、codex 27、gemini 16）
- 30 并发下预计耗时 4.7 分钟

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

**并发模型**：站级 30 并发 × 段级 4 并发 = 峰值 120 个探测线程。节流仍按 `(host, section)` 分桶，同段之间保持 gap 秒。

### 2. `cpa_probe/writeback.py`

```python
def rebuild_config_full(cfg, all_plans, original_lines) -> tuple[str, list[str]]:
    """全量重建：保留全局配置与人工注释，重建所有条目，按 priority 降序"""

def _extract_entry_comments(lines) -> dict[str, dict[str, list[str]]]:
    """按 (段, name/base-url) 索引人工注释"""

def _render_entry_full(sp, dash_indent, field_indent, stamp, comments) -> list[str]:
    """渲染一个完整条目（含注释）"""
```

### 3. `server.py`

- `run_job_full_redetect()`：提取既有站 + 合并新站 + BatchProber 并发探测 + 进度事件
- `_api_probe`：认 `full_redetect` 与 `max_workers` 参数，选择执行函数
- `_api_plan`：全量重探模式走 `rebuild_config_full`，返回整文件 diff
- `_api_context`：返回 `existing_count` 供前端提示

### 4. 前端

- `web/index.html`：全量重探勾选框 + 并发数输入框 + 警告提示区
- `web/app.js`：勾选时拉 `existing_count`、二次确认弹窗、透传 `full_redetect`/`max_workers`

---

## 四、测试

`tests/test_full_redetect.py`（6 项，全过）：

1. `extract_existing_entries` 提取 7 个站（2 gemini + 1 codex + 2 claude + 2 compat）
2. `BatchProber` 10 站并发 + 10 次进度回调
3. `BatchProber` 统计分类（success / partial / failure）
4. `BatchProber` 单站异常不影响整体
5. `rebuild_config_full` 保留全局配置与人工注释、priority 正确更新、YAML 有效
6. `rebuild_config_full` 输出按 priority 降序

**全量测试**：
- 自带样本 854 项 exit 0（原 839 + 新增 15）
- 真实 config.yaml 856 项 exit 0

---

## 五、风险与已有缓解

| 风险 | 缓解 |
|---|---|
| 临时 503 被判死 | 探测层已有 503 重试（前序会话修复）；diff 需人工确认 |
| 30 并发打爆站方 | `_Throttle` 按 `(host, section)` 分桶，同段保持 gap；并发数前端可调 |
| 全量重写出 bug | `write_local` 写前必备份；`validate()` 校验 YAML；854 项测试 |
| 与 CPAMP 并发写 | `_api_apply` 基线比对，被改过就 409 要求重新生成 |
| 注释丢失 | `_extract_entry_comments` 按 name/base-url 匹配，测试覆盖 |

**未覆盖的风险**：全量重探只在假上游与 6 项单元测试上验证过，没有对 175 个真实站跑过一次完整流程。第一次实跑建议先看 diff 不写回。

---

## 六、用法

1. 打开网页，勾选「全量重探模式」
2. 界面显示 config.yaml 中的既有站数量（当前 175）
3. 可调站级并发数（默认 30，4U24G VPS 可到 40）
4. 点「开始探测」，二次确认
5. 实时进度：`探测进度：42/175 · 成功 12 · 部分通 25 · 失败 5`
6. 探测完成后生成整文件 diff
7. 逐项确认后点「确认写回」
8. 写回前自动备份，写回后推 PUT 触发 CPA 重载

不勾选时行为与之前完全一致：只探测新粘贴的站，行级插入，既有条目一个字节不动。
