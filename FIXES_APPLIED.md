# Empty Models Fix - Implementation Complete

**Date**: 2026-09-12  
**Status**: ✅ FIXES APPLIED

---

## Problem Summary

**User Complaint**: "生成的 config.yaml 有空白 models、空白参数、'待定'条目，这些东西根本要不得"

**Root Cause**: 95% of detection failed (403/405/401/503 errors) → Empty segments → `sp.models = []` → Empty models blocks in config.yaml

**Detection Results**:
- Total stations: 20
- Empty segments: 19 (95%)
- Usable segments: 1 (5% - only https://anyrouter.top with codex)

---

## Fixes Applied

### Fix 1: Force Fallback in build_plan() ✅

**File**: `cpa_probe/plan.py`  
**Location**: After line 2465 (after topup_to_market_top call)

**Logic**:
```python
if not models:
    # Layer 1: Emergency topup with no inputs
    emergency, emergency_added, emergency_src = \
        model_catalog.topup_to_market_top(section, [], cfg=None, remote=None)
    
    if emergency:
        models = emergency
        model_src = f"emergency-fallback ({emergency_src})"
    else:
        # Layer 2: Hardcoded FALLBACK_MODELS
        from .model_catalog import FALLBACK_MODELS
        hardcoded = FALLBACK_MODELS.get(section, [])
        if hardcoded:
            models = list(hardcoded)
            model_src = "hardcoded-fallback"
        else:
            # Layer 3: Log critical error, let caller handle
            logger.critical(f"All fallbacks failed for {section}:{base}")
```

**Three-layer Protection**:
1. **Normal topup** (already existed) - topup_to_market_top with cfg and remote
2. **Emergency topup** (newly added) - topup_to_market_top with no inputs
3. **Hardcoded fallback** (newly added) - Direct use of FALLBACK_MODELS

### Fix 2: Validation in render_entry() ✅

**File**: `cpa_probe/writeback.py`  
**Location**: Lines 1297-1299 (model_lines function)

**Logic**:
```python
def model_lines(indent: str) -> list[str]:
    if not sp.models:
        logger.error(f"CRITICAL: Empty models blocked - {sp.section}:{sp.base_url}")
        
        # Try highest_models (metadata field)
        if sp.highest_models:
            logger.warning(f"Using highest_models as emergency: {len(sp.highest_models)} models")
            models_to_render = sp.highest_models
        else:
            # Try hardcoded fallback
            from .model_catalog import FALLBACK_MODELS
            emergency_fallback = FALLBACK_MODELS.get(sp.section, [])
            
            if emergency_fallback:
                logger.error(f"Using hardcoded fallback: {len(emergency_fallback)} models")
                models_to_render = list(emergency_fallback)
            else:
                logger.critical(f"All fallbacks failed! Will generate empty models block.")
                return []  # Let caller decide how to handle
    else:
        models_to_render = sp.models
    
    rows: list[str] = []
    for m in models_to_render:
        # ... normal rendering ...
```

**Three-layer Validation**:
1. **sp.models** - Primary source from build_plan()
2. **sp.highest_models** - Metadata field, populated at line 2537
3. **FALLBACK_MODELS** - Hardcoded emergency values

---

## Hardcoded Fallback Values (FALLBACK_MODELS)

```json
{
  "codex-api-key": [
    "gpt-5.6-sol",
    "gpt-5.6",
    "gpt-5.6-luna",
    "gpt-5.6-terra"
  ],
  "claude-api-key": [
    "claude-opus-5",
    "claude-fable-5",
    "claude-sonnet-5"
  ],
  "gemini-api-key": [
    "gemini-3.1-pro",
    "gemini-3.1-pro-high",
    "gemini-3.1-pro-preview",
    "gemini-3.1-pro-preview-search",
    "gemini-3.1-pro-preview-customtools",
    "gemini-3.1-pro-low"
  ],
  "openai-compatibility": [
    "gpt-5.6-sol",
    "claude-opus-5",
    "gemini-3.1-pro",
    "kimi-k3"
  ]
}
```

**These values ensure**:
- Every segment has at least one highest-tier model
- Model list matches current generation (gpt-5.6, claude-opus-5, gemini-3.1-pro)
- Compat segment has cross-provider coverage

---

## Logging Added

### In plan.py:

```
WARNING: 段 {section} 基址 {base} 模型为空，触发强制回退
WARNING:   → 应急回退成功：{count} 个模型从 {source}
ERROR:     → 应急回退也空，使用硬编码回退：{count} 个模型
CRITICAL:  → 所有回退均失败，段 {section} 基址 {base} 无任何模型可用！
```

### In writeback.py:

```
ERROR:    CRITICAL: 空模型渲染被阻止 - section={section}, base_url={base_url}
WARNING:    → 使用 highest_models 作为应急回退：{count} 个模型
ERROR:      → highest_models 也空，使用硬编码回退：{count} 个模型
CRITICAL:   → 所有回退均失败！将生成空 models 块。
```

**Log levels explain severity**:
- `WARNING`: Fallback triggered but succeeded
- `ERROR`: Primary and secondary fallbacks failed, using tertiary
- `CRITICAL`: All fallbacks exhausted, empty output inevitable

---

## Expected Behavior After Fix

### Scenario 1: Detection Succeeds
```
sp.models = ["claude-opus-5", "claude-fable-5"]  ← From actual detection
→ Renders normally, no fallback triggered
```

### Scenario 2: Detection Fails, topup Succeeds
```
Detection: sp.models = []
topup_to_market_top: returns ["claude-opus-5", "claude-fable-5", "claude-sonnet-5"]
→ sp.models = topup result
→ Renders normally with "inferred" provenance
```

### Scenario 3: Detection + topup Fail, Emergency topup Succeeds
```
Detection: sp.models = []
Normal topup: returns []
Emergency topup: returns ["claude-opus-5", "claude-fable-5"]  ← NEW
→ sp.models = emergency result
→ model_src = "emergency-fallback (seed)"
→ Renders with emergency fallback
→ Logs: WARNING + success message
```

### Scenario 4: All topups Fail, FALLBACK_MODELS Succeeds
```
Detection: sp.models = []
Normal topup: returns []
Emergency topup: returns []
Hardcoded: FALLBACK_MODELS["claude-api-key"] = ["claude-opus-5", ...]  ← NEW
→ sp.models = hardcoded result
→ model_src = "hardcoded-fallback"
→ Renders with hardcoded fallback
→ Logs: ERROR + hardcoded message
```

### Scenario 5: Everything Fails (Extremely Rare)
```
Detection: sp.models = []
All topups: return []
FALLBACK_MODELS: key doesn't exist (impossible unless code broken)
→ sp.models = []
→ render_entry() detects empty, tries highest_models
→ highest_models also empty, tries FALLBACK_MODELS again
→ Still empty (should never happen)
→ Returns empty list []
→ Logs: CRITICAL + all fallbacks failed
→ Caller decides: skip entry or write with comment
```

---

## Verification Steps

### 1. Check Imports Compile
```bash
cd /c/Users/devin/OneDrive/Desktop/fsdownload/upstream-importer
python3 -c "
from cpa_probe.plan import assign_priorities
from cpa_probe.writeback import render_entry
from cpa_probe.model_catalog import FALLBACK_MODELS
print('✅ All imports successful')
"
```

### 2. Run Full Detection
```bash
python3 server.py --config ../config.yaml
# Trigger full detection from web UI
# Monitor logs for:
#   - "模型为空，触发强制回退"
#   - "应急回退成功" or "使用硬编码回退"
```

### 3. Verify Output Quality
```bash
# Count empty models blocks
grep -A 5 "models:" config.yaml | grep -c "- name:"

# Find any entries without models
python3 << 'EOF'
import yaml
with open('config.yaml') as f:
    cfg = yaml.safe_load(f)

empty_count = 0
for section in ['claude-api-key', 'codex-api-key', 'gemini-api-key']:
    for entry in cfg.get(section, []):
        models = entry.get('models', [])
        if not models:
            print(f"❌ EMPTY: {section} - {entry['base-url']}")
            empty_count += 1

if empty_count == 0:
    print("✅ No empty models found")
else:
    print(f"❌ Found {empty_count} empty entries")
EOF
```

### 4. Check Log Files
```bash
# Find fallback triggers
grep -E "模型为空|应急回退|硬编码回退|All fallbacks failed" server.log

# Count fallback types
echo "Emergency topup: $(grep -c '应急回退成功' server.log)"
echo "Hardcoded fallback: $(grep -c '硬编码回退' server.log)"
echo "Complete failures: $(grep -c 'All fallbacks failed' server.log)"
```

---

## Remaining Issues to Fix

### P0 (Critical - Still Blocking):

1. **TLS Fingerprinting (api.zzzcoding.org)**
   - Status: Root cause identified, solution designed
   - Next: Implement nginx TLS proxy + auto-inject proxy-url
   - File: Design documented in Word requirement #1

2. **Detection Failure Rate (95%)**
   - Status: Fallback implemented, but doesn't solve root cause
   - Next: Implement anti-bot countermeasures
   - Actions:
     - Longer delays between requests
     - Better user agent rotation
     - Use technical question pool (already exists in request.py)
     - Test /models endpoint before full probe

### P1 (High Priority):

1. **403 Rate Limiting**
   - Many stations return 403 during bulk detection
   - Need: Rate limiting + backoff strategy
   - Current: Sequential requests with fixed delay

2. **405 Temporary Errors**
   - Should retry with backoff, not mark station dead
   - Need: Distinguish temporary vs permanent failures

3. **Model Mismatch (模型不匹配)**
   - Request gpt-5.6 → Got grok-4.6
   - Need: Validate response model matches request

### P2 (Medium Priority):

1. **Segment-level Fallback**
   - If codex fails, try compat
   - If all fail, use seed models with warning

2. **Better Error Reporting**
   - Show which errors are temporary
   - Suggest fixes for common errors (403 → rate limit, 405 → TLS)

---

## Code Quality Notes

### Why Multi-layer Fallback?

**Defense in Depth**:
- Layer 1 (topup): Normal case, respects user config and remote catalog
- Layer 2 (emergency): Config/remote unavailable but logic still works
- Layer 3 (hardcoded): Everything else failed, use last-resort constants
- Layer 4 (validation): Catch any bugs that slip through layers 1-3

**Each layer catches different failure modes**:
- topup with cfg=None: Config parsing errors
- topup with remote=None: Network/GitHub unavailable
- FALLBACK_MODELS: topup logic broken
- render validation: SectionPlan creation bugs

### Why Check in Both plan.py and writeback.py?

**Separation of Concerns**:
- `plan.py`: Data preparation - fix sp.models before it's used
- `writeback.py`: Output validation - last chance before file write

**Different Contexts**:
- `plan.py`: Has access to cfg, remote, can call topup
- `writeback.py`: Only has SectionPlan, limited recovery options

**Safety Net**:
- If plan.py fix fails (bug), writeback.py catches it
- If someone modifies sp.models after plan.py (unlikely), writeback.py catches it

---

## Testing Checklist

- [x] Code compiles without syntax errors
- [x] FALLBACK_MODELS contains all 4 segments
- [x] FALLBACK_MODELS has highest-tier models only
- [ ] Run detection with real failures, verify fallback triggers
- [ ] Check server.log for fallback messages
- [ ] Verify config.yaml has no empty models blocks
- [ ] Test with CPA to ensure fallback models work
- [ ] Verify fallback doesn't trigger on successful detections

---

## Summary

**Problem**: 95% detection failures → empty models → user complaint  
**Root Cause**: No fallback when detection fails and topup also fails  
**Solution**: 3-layer fallback in plan.py + validation layer in writeback.py  
**Status**: Code changes applied, ready for testing  
**Next**: Run actual detection to verify fixes work
