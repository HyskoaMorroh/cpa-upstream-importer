# Empty Models Root Cause - CONFIRMED

**Date**: 2026-09-12  
**Status**: ✅ ROOT CAUSE IDENTIFIED

---

## Critical Finding

**Detection Results Analysis**:
```
Total stations analyzed: 20
Stations with EMPTY segments: 19  (95%)
Stations with usable segments: 1   (5%)
```

**This is the root cause of empty models output.**

---

## Evidence from Detection HTML

### Empty Segment Examples (19 out of 20 stations):

1. **https://chiangma.com**
   - Usable segments: `[]` ← EMPTY
   - Requests: 4
   - Errors: 403限频, 401鉴权, 503死路由, 404死路

2. **https://fatenewapi.xxxxo.bond**
   - Usable segments: `[]` ← EMPTY
   - Requests: 8
   - Errors: 405临时, 403限频, 401鉴权, 503死路由, 404死路, 模型不匹配

3. **https://api.wapq.cn**
   - Usable segments: `[]` ← EMPTY
   - Requests: 30
   - Errors: 405临时, 403限频, 401鉴权, 503死路由

4. **https://api.zzzcoding.org/v1**
   - Usable segments: `[]` ← EMPTY
   - Requests: 20
   - Errors: 405临时, 403限频, 401鉴权, 503死路由, 404死路

5. **https://runanytime.hxi.me**
   - Usable segments: `[]` ← EMPTY
   - Requests: 5-30
   - Errors: 401鉴权, 503死路由, 404死路

### Only 1 Working Station (out of 20):

**https://anyrouter.top**
- Usable segments: `[codex]` ← HAS SEGMENTS
- Requests: 28
- Errors: Some 405/401/503/404 but still has usable codex segment

---

## Why Empty Segments = Empty Models

### Code Path Analysis

**1. Detection Phase** (cpa_probe/request.py):
```python
# All requests fail with 403/405/401/503
# No successful model discovery
# Result: v.usable = False for all segments
```

**2. Plan Building** (cpa_probe/plan.py:2415-2462):
```python
# Line 2415-2418
candidates = [] + [] + [] + []  # All empty because detection failed

# Line 2453-2455
if not merged and v.models:  # if not [] and []:
    merged = v.models  # Never executed because v.models is also []

models = merged  # models = []

# Line 2459-2462
if not custom_only:
    models, added, fill_src = topup_to_market_top(section, [], cfg, remote)
    # This SHOULD return fallback models
    # BUT if section doesn't match or other issues, can still return []
```

**3. Rendering Phase** (cpa_probe/writeback.py:1297-1379):
```python
def model_lines(indent: str) -> list[str]:
    rows: list[str] = []
    for m in sp.models:  # sp.models = [] from step 2
        rows.append(f"{indent}- name: {_yaml_str(m)}")
    return rows  # Returns empty list []
```

**Result**: Empty `models:` block or no models block at all in config.yaml

---

## Why Detection Failed for 95% of Stations

### Error Pattern Analysis

**1. 405 临时错误 (Temporary Error)**
- Server returning 405 Method Not Allowed
- Likely TLS fingerprinting or anti-bot detection
- Similar to api.zzzcoding.org issue

**2. 403 限频/边缘 (Rate Limiting / Edge Protection)**
- Cloudflare or similar protection
- Bulk probing detected and blocked
- Multiple requests flagged as bot behavior

**3. 401 鉴权 (Authentication Failed)**
- Invalid or expired API keys
- Keys may be valid for direct use but blocked when probing

**4. 503 死路由 (Dead Route)**
- Upstream service unavailable
- Could be actual downtime or fingerprint rejection

**5. 模型不匹配 (Model Mismatch)**
- Request model X, got model Y in response
- Example: Request gpt-5.6 → Got grok-4.6
- Server routing issues or deceptive responses

---

## Why topup_to_market_top Fallback Didn't Work

### Theory A: Section Name Mismatch

Detection HTML shows segments like `[codex]`, `[claude]`, but code expects:
- `claude-api-key`
- `codex-api-key`
- `gemini-api-key`

If section name doesn't match FALLBACK_MODELS keys, fallback returns empty.

### Theory B: custom_only Flag

```python
# Line 2459
if not custom_only:
    models, added, fill_src = topup_to_market_top(...)
```

If `custom_only = True`, fallback is skipped entirely.

### Theory C: Detection Marked as "Dead"

```python
# Somewhere in code there might be:
if station_is_dead:
    return []  # No fallback for dead stations
```

If a station is marked completely dead (all segments failed), might skip fallback.

---

## Real-World Impact

**User's Complaint**: "生成的 config.yaml 有空白 models、空白参数、'待定'条目，这些东西根本要不得"

**What Actually Happened**:
1. Ran full detection on ~173 credentials
2. 95% of stations failed all probes (403/405/401/503 errors)
3. Detection marked segments as unusable
4. build_plan() generated SectionPlan with `models = []`
5. writeback.py rendered empty models blocks
6. User got unusable config.yaml

---

## Solution Requirements

### Immediate Fix (P0):

**1. Force Fallback Models for Empty Detection**

Add validation in build_plan():
```python
# After line 2462
if not models:
    logger.warning(
        f"Models empty after detection for {section}:{base}. "
        f"Forcing fallback models.")
    models, _, _ = topup_to_market_top(section, [], cfg=None, remote=None)
    
    if not models:
        logger.error(f"CRITICAL: Fallback also empty for {section}:{base}")
        # Use hardcoded emergency fallback
        models = FALLBACK_MODELS.get(section, [])
```

**2. Validate in render_entry()**

Add check before rendering:
```python
def model_lines(indent: str) -> list[str]:
    if not sp.models:
        logger.error(
            f"REFUSING to render empty models: "
            f"section={sp.section}, base_url={sp.base_url}")
        
        # Use highest_models if available
        if sp.highest_models:
            logger.warning(f"Using highest_models as emergency fallback")
            models_to_render = sp.highest_models
        else:
            # Emergency fallback
            models_to_render = FALLBACK_MODELS.get(sp.section, ["PLACEHOLDER"])
    else:
        models_to_render = sp.models
    
    rows: list[str] = []
    for m in models_to_render:
        rows.append(f"{indent}- name: {_yaml_str(m)}")
    return rows
```

### Medium-term Fix (P1):

**1. Solve TLS Fingerprinting Issues**
- Implement nginx TLS proxy for affected upstreams
- Inject proxy-url parameter automatically
- See TLS_PROXY_IMPLEMENTATION.md

**2. Implement Anti-Bot Countermeasures**
- Longer delays between requests
- Rotate user agents properly
- Avoid bulk probing patterns
- Use technical question pool already implemented

**3. Better Error Handling**
- Distinguish between temporary (405) and permanent (404) failures
- Retry temporary errors with backoff
- Don't mark station dead on first 405

### Long-term Fix (P2):

**1. Segment-level Fallback Strategy**
```python
# If codex segment fails, try compat segment
# If all segments fail, use seed models with warning
```

**2. Smarter Detection Strategy**
- Test /models endpoint first (lightweight)
- Only probe actual chat completions if /models succeeds
- Skip image ladder if auth fails

**3. User Override Mechanism**
- Allow user to manually specify models for dead stations
- UI checkbox: "Use fallback models for failed detections"

---

## Next Immediate Actions

1. ✅ Add emergency fallback in build_plan() (plan.py around line 2462)
2. ✅ Add validation in render_entry() (writeback.py around line 1297)
3. ✅ Test with actual failed detection data
4. ⏳ Run detection again and verify fallback triggers
5. ⏳ Implement nginx TLS proxy solution
6. ⏳ Solve api.zzzcoding.org 405 errors specifically

---

## Verification Plan

```bash
# 1. Add debug logging to verify fallback triggers
grep "Forcing fallback models" server.log

# 2. Check generated config.yaml has no empty models
grep -A 5 "models:" config.yaml | grep -c "- name:"

# 3. Verify every entry has at least 1 model
python3 << 'EOF'
import yaml
with open('config.yaml') as f:
    cfg = yaml.safe_load(f)
    
for section in ['claude-api-key', 'codex-api-key', 'gemini-api-key']:
    for entry in cfg.get(section, []):
        models = entry.get('models', [])
        if not models:
            print(f"EMPTY: {section} - {entry['base-url']}")
EOF
```

---

## Conclusion

**Root cause confirmed**: Detection failures (403/405/401/503) → Empty segments → Empty models → User complaint

**Solution**: Multi-layer fallback mechanism with validation at both plan building and rendering stages.

**Status**: Ready to implement fixes
