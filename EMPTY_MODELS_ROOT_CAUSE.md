# Empty Models Root Cause Analysis

**Date**: 2026-09-12  
**Issue**: Generated config.yaml has blank models, blank parameters, "待定" entries

---

## Root Cause Identified

### Location: `cpa_probe/plan.py` Lines 2415-2464

```python
# Line 2415-2438: 候选集合过滤
if not forced_models:
    prior = existing_models_for(cfg, section, base, row.api_key)
    candidates = list(dict.fromkeys(
        list(models) + list(v.models) + list(v.catalog) + prior))
    
    # 两级过滤
    preferred = [m for m in candidates
                 if model_catalog.section_allows(section, m)]
    candidates = preferred or [
        m for m in candidates
        if model_catalog.section_protocol_ok(section, m)]
    merged = model_catalog.newest_generation_per_line(candidates)
    
    # Line 2453-2455: CRITICAL BUG
    if not merged and v.models:  # ← v.models 为空时此分支不执行
        merged = list(dict.fromkeys(v.models))
    models = merged  # ← merged=[] 直接赋值给 models
    
    # Line 2457-2462: 补齐逻辑
    custom_only = bool(models) and all(
        not model_catalog.family(m) for m in models)
    if not custom_only:  # models=[] 时 custom_only=False，会进入补齐
        remote, _why = model_catalog.remote_names()
        models, added, fill_src = model_catalog.topup_to_market_top(
            section, models, cfg=cfg, remote=remote)
```

### Bug Trigger Conditions

**条件 1**: 过滤后 `merged = []`
- `candidates` 经过 `section_allows` 或 `protocol_ok` 过滤
- `newest_generation_per_line(candidates)` 返回空列表

**条件 2**: `v.models = []` (实测失败或全部拒收)
- Line 2453: `if not merged and v.models:` 条件为 `False`
- Line 2454 不执行，无法用 `v.models` 回填

**条件 3**: `topup_to_market_top` 返回空列表
- `latest_models` 返回空列表，或
- `section_allows` 过滤后 `latest` 为空

**结果**: Line 2455 `models = merged` 将空列表赋值给 `models`

---

## Propagation Path

### 1. `plan.py` → `SectionPlan.models`

```python
# Line 2518: models 变量直接赋值给 sp.models
sp = SectionPlan(
    models=models,  # ← 空列表传入
    ...
)
```

### 2. `writeback.py` → Empty Output

```python
# Line 1500-1501: render_entry 调用 model_lines
out.append(f"{field}models:")
out.extend(model_lines(f"{field}  "))

# Line 1297-1300: model_lines 直接迭代 sp.models
def model_lines(indent: str) -> list[str]:
    rows: list[str] = []
    for m in sp.models:  # ← sp.models=[] 时循环不执行
        rows.append(f"{indent}- name: {_yaml_str(m)}")
    return rows  # ← 返回空列表
```

**最终输出**:
```yaml
models:
  # 空白 - 没有任何模型条目
```

---

## Why topup_to_market_top Fails

### Code Flow in `model_catalog.py:898-962`

```python
def topup_to_market_top(section, models, *, cfg=None, remote=None):
    latest, src = latest_models(section, cfg=cfg, remote=remote, limit=0)
    latest = [m for m in latest if section_allows(section, m)]
    
    # Line 936-937: EARLY RETURN
    if not latest:
        return list(models), [], ''  # ← 返回空输入
    
    have = [m for m in (models or []) if m]
    
    # Line 954-958: 产品线过滤
    if have:
        seen_lines = {_product_line(m) for m in have}
        latest = [m for m in latest if _product_line(m) in seen_lines]
        if not latest:
            return list(models), [], ''  # ← 再次返回空
    
    merged = newest_generation_per_line(have + latest)
    return merged, add, src
```

### Failure Cases

**Case A**: `latest_models` 返回空列表
- 可能原因：
  - CPA 权威名录获取失败 (`remote=None` or `remote=[]`)
  - config.yaml 没有该段的模型
  - 内置兜底列表为空
- Line 936-937 直接返回 `([], [], '')`

**Case B**: `section_allows` 过滤后为空
- 所有 `latest` 模型不符合段规则
- Line 936-937 返回空

**Case C**: `have` 非空但产品线不匹配
- Line 954-958 的产品线过滤清空 `latest`
- 返回空

---

## Verification Needed

### 1. Check `latest_models` Implementation

```bash
cd /c/Users/devin/OneDrive/Desktop/fsdownload/upstream-importer
grep -A30 "^def latest_models" cpa_probe/model_catalog.py
```

需要确认：
- 内置兜底列表是否为空
- `remote_names()` 是否正常工作
- `_cfg_models()` 是否正确读取 config.yaml

### 2. Check `section_allows` Rules

```bash
grep -A20 "^def section_allows" cpa_probe/model_catalog.py
```

需要确认：
- 过滤规则是否过严
- 是否存在段名映射错误

### 3. Run Actual Detection

```bash
cd /c/Users/devin/OneDrive/Desktop/fsdownload/upstream-importer
python3 server.py --config ../config.yaml
# 触发全量检测，观察日志中 models 变量的值
```

需要观察：
- 哪些站的 models 为空
- `latest_models` 返回了什么
- `topup_to_market_top` 的实际返回值

---

## Fix Strategy

### Option 1: Add Validation in `writeback.py`

```python
def model_lines(indent: str) -> list[str]:
    rows: list[str] = []
    if not sp.models:
        # 明确报错而不是静默生成空输出
        raise ValueError(
            f"models 为空：section={sp.section}, "
            f"base_url={sp.base_url}, model_source={sp.model_source}")
    for m in sp.models:
        rows.append(f"{indent}- name: {_yaml_str(m)}")
    return rows
```

**优点**: 立即暴露问题，不会生成无效 config.yaml  
**缺点**: 阻断写回流程，需要修复根因才能继续

### Option 2: Use `highest_models` as Fallback

```python
# In plan.py Line 2518
sp = SectionPlan(
    models=models if models else list(v.models),  # 回退到实测清单
    highest_models=list(models),
    ...
)
```

**优点**: 保证 `sp.models` 非空（如果 `v.models` 非空）  
**缺点**: 可能写入未经过滤的低代模型

### Option 3: Fix Root Cause in `latest_models`

确保 `latest_models` 永远返回非空列表（至少返回内置兜底）

```python
def latest_models(section, *, cfg=None, remote=None, limit=0):
    # ... 现有逻辑 ...
    
    # 确保至少有兜底列表
    if not src:
        fallback = _FALLBACK_MODELS.get(section, [])
        src.extend(fallback)
        used.append(f"内置兜底 {len(fallback)} 个")
    
    if not src:
        raise ValueError(f"段 {section} 没有任何可用模型（远程/本地/兜底全空）")
    
    # ... 后续处理 ...
```

**优点**: 从源头保证有模型可填  
**缺点**: 需要确认内置兜底列表是否完整

---

## Recommended Fix

**立即执行**: Option 1 (添加验证) + Option 3 (修复 latest_models)

1. 在 `writeback.py:model_lines` 添加空检查，阻止生成无效输出
2. 审计 `latest_models` 的三层来源，确保至少有兜底
3. 运行实际检测验证修复效果

**下一步**: 收集实际检测日志，定位哪些站触发了空 models
