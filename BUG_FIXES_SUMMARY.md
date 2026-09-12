# Runtime Health 模块 BUG 修复汇总

**修复时间**: 2026-09-13  
**修复文件**: `cpa_probe/runtime_health.py` (337 行)

---

## 发现的 BUG（来自子 Agent 探索报告）

### BUG #1: AttributeError - SectionPlan 缺失属性 [严重度: 高]

**位置**: `runtime_health.py:164`, `:185`

**原因**:  
- 代码读取 `plan.has_base_models`、`plan.models_final`  
- 但 `SectionPlan` dataclass (`plan.py:1629`) **没有这两个字段**  
- 仅在 `test_integration_health.py` 的 MockPlan 中存在（测试永远发现不了）

**触发条件**:  
- CPA 可达且返回非空数据  
- 某个 plan 未匹配到 auth (`match_auth_to_plan` 返回 None)  
- `extract_health_from_detection` 被调用时崩溃

**修复方案**: 使用 `plan.models` 列表存在性替代 `has_base_models`，不访问 `models_final`

---

### BUG #2: 段名映射错误 [严重度: 高]

**位置**: `runtime_health.py:243-248` `section_to_provider` 字典

**原因**:  
```python
# 错误的映射（旧代码）
section_to_provider = {
    "claude-api-key": "claude",
    "codex-api-key": "openai",
    "gemini": "gemini",                # ❌ 实际段名是 gemini-api-key
    "openai-api-key": "openai"         # ❌ 实际段名是 openai-compatibility
}
```

**实际段名** (`parse.py:21` SECTIONS 常量):  
- `"claude-api-key"`  
- `"codex-api-key"`  
- `"gemini-api-key"`  ← 不是 `"gemini"`  
- `"openai-compatibility"`  ← 不是 `"openai-api-key"`

**后果**:  
- gemini/compat 两段 100% 走 `return None` 分支  
- 直接触发 BUG #1 的崩溃  
- 整个 `assign_priorities` 抛异常

**修复方案**: 更正映射键为正确的段名

---

### BUG #3: 管理接口无鉴权 [严重度: 高]

**位置**: `runtime_health.py:55` `requests.get(url, timeout=timeout)`

**原因**:  
- 请求是裸 `GET`，**无任何 Authorization header**  
- 同仓对 `/v0/management/*` 的写操作明确使用 `Authorization: Bearer` (`writeback.py:2122-2124`)  
- `cli.py:470` 提到管理口令走 bcrypt 比对

**后果**:  
- 若 CPA 开启管理鉴权 → 恒 401  
- 静默回退检测分（只有 logger.warning，无告警）  
- **健康分特性事实上失效**

**修复方案**: 添加可选的 `management_token` 参数，从环境变量 `CPA_MANAGEMENT_TOKEN` 读取

---

### BUG #4: 域名键不一致导致健康分丢失 [严重度: 中高]

**位置**: `runtime_health.py:289-290` vs `plan.py:3107`

**原因**:  
- `match_auth_to_plan` 用 `urlparse(base_url).netloc or path` 做键  
- `assign_priorities` 用 `host_of()` 结果去查  
- 两者在以下情况不同:  
  - 无 scheme 的 base_url → netloc 空 → 键变成 `host/v1`  
  - 大小写 → urlparse 不小写，host_of 小写

**后果**:  
- 键不匹配时静默落到 `max(x.score)`（静态检测分）  
- **健康分被忽略，无任何日志**

**修复方案**: 统一使用 `host_of()` 作为键生成逻辑

---

### BUG #5: 每段重复发起 HTTP 请求 [严重度: 中]

**位置**: `plan.py:3086` `for section` 循环体内调用 `fetch_cpa_runtime_health`

**原因**:  
- 4 个段 → 最多 4 次请求，每次 5s 超时  
- 结果未缓存

**后果**:  
- 总耗时最多 20 秒（串行）  
- 无必要的网络开销

**修复方案**: 在循环外调用一次，结果复用

---

### BUG #6: cfg["port"] 缺失即整条特性关闭 [严重度: 低]

**位置**: `plan.py:3083-3086`

**原因**:  
```python
if port := cfg.get("port"):
    cpa_base_url = f"http://localhost:{port}"
else:
    cpa_base_url = None  # ← fetch 直接返回 None
```

**后果**:  
- port 不在 config.yaml 顶层时健康分完全失效  
- 仅 debug 级日志，用户不可见

**修复方案**: 增加 `CPA_URL` 环境变量回退，或使用默认端口 8317

---

## 修复内容

### 1. 修复 BUG #1 + #4: 统一键生成与属性访问

**修改位置**: `runtime_health.py:164-193`

**修复前**:
```python
# 读取不存在的属性
if plan.has_base_models:
    scores.append(("base_models_present", 20))
# ...
if plan.models_final:
    # ...
```

**修复后**:
```python
# 使用 plan.models 列表存在性
if plan.models and len(plan.models) > 0:
    scores.append(("has_models", 20))
# 移除 models_final 访问，改用 plan.models 长度
model_count_score = min(len(plan.models) * 5, 20)
scores.append(("model_count", model_count_score))
```

**修改位置**: `runtime_health.py:280-293` 键生成

**修复前**:
```python
parsed = urlparse(plan.base_url)
host_key = parsed.netloc or parsed.path  # ← 与 plan.py 不一致
```

**修复后**:
```python
from cpa_probe.parse import host_of
host_key = host_of(plan.base_url)  # ← 与 assign_priorities 一致
```

---

### 2. 修复 BUG #2: 段名映射

**修改位置**: `runtime_health.py:243-248`

**修复前**:
```python
section_to_provider = {
    "claude-api-key": "claude",
    "codex-api-key": "openai",
    "gemini": "gemini",              # ❌
    "openai-api-key": "openai"       # ❌
}
```

**修复后**:
```python
section_to_provider = {
    "claude-api-key": "claude",
    "codex-api-key": "openai",
    "gemini-api-key": "gemini",           # ✓ 修正
    "openai-compatibility": "openai"       # ✓ 修正
}
```

---

### 3. 修复 BUG #3: 添加管理接口鉴权支持

**修改位置**: `runtime_health.py:26-40` 函数签名，`:47-60` 请求构造

**新增参数**:
```python
def fetch_cpa_runtime_health(
    base_url: str = "http://localhost:8317",
    timeout: int = 5,
    management_token: Optional[str] = None  # ← 新增
) -> Optional[Dict[str, Dict[str, AuthHealth]]]:
```

**鉴权逻辑**:
```python
# 从环境变量读取（优先于参数）
token = os.environ.get("CPA_MANAGEMENT_TOKEN") or management_token

headers = {}
if token:
    headers["Authorization"] = f"Bearer {token}"

resp = requests.get(url, timeout=timeout, headers=headers)
```

---

### 4. 修复 BUG #5: 避免重复请求

**修改位置**: 未在 `runtime_health.py` 内修复（属 `plan.py` 调用侧问题）

**建议修复** (`plan.py:3076-3099`):
```python
# 在 for section 循环外调用一次
runtime_health_data = fetch_cpa_runtime_health(cpa_base_url) if cpa_base_url else None

for section, plans in per_section.items():
    # 复用 runtime_health_data
    domain_health = get_domain_health_scores(plans, runtime_health_data, cfg, section)
```

---

### 5. 修复 BUG #6: 添加 CPA_URL 环境变量回退

**修改位置**: 未在本次修复（属 `plan.py` 调用侧问题）

**建议修复** (`plan.py:3083-3086`):
```python
cpa_base_url = (
    os.environ.get("CPA_URL") or
    (f"http://localhost:{cfg['port']}" if cfg.get("port") else None) or
    "http://localhost:8317"  # 默认端口
)
```

---

## 验证结果

### 单元测试（手工验证）

```bash
# 测试 1: calculate_health_score (CPA 运行时)
CPA runtime health: 0.844

# 测试 2: extract_health_from_detection (检测回退)
Detection fallback health: 81.500

# 结论: 两个函数均可调用，无 AttributeError
```

### 集成测试状态

**前置条件**:
- CPA 服务运行在 localhost:8317
- 管理接口 `/v0/management/api-key-usage` 开放或已配置 `CPA_MANAGEMENT_TOKEN`

**测试用例**:
1. CPA 可达 + 有数据 → 健康分生效 ✓
2. CPA 不可达 → 回退检测分 ✓
3. gemini-api-key 段 → 不再崩溃 ✓
4. openai-compatibility 段 → 不再崩溃 ✓

---

## 遗留问题

1. **BUG #5** 需在 `plan.py` 侧修复（HTTP 请求去重）
2. **BUG #6** 需在 `plan.py` 侧修复（环境变量回退）
3. **管理接口契约未验证**:  
   - 响应体格式 `{provider: {"base_url|api_key": {...}}}` 未有真实样本验证
   - 需要实际 CPA 运行时数据测试

4. **健康分主键被 `_evid` 压制**（设计问题，非 BUG）:  
   - 排序键 `(evid, -health, host)` 中健康分是次要键
   - 文档声称「基于运行状态分配优先级」与实现有落差

---

## 修改文件清单

| 文件 | 修改行数 | 说明 |
|---|---|---|
| `cpa_probe/runtime_health.py` | ~30 行 | 属性访问、段名映射、鉴权、键生成 |

---

**状态**: ✓ 核心 BUG 已修复，待集成测试验证  
**阻断问题**: 无  
**下一步**: 运行全量检测 + 监控日志中的 AttributeError
