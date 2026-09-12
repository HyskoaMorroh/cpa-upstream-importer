# Empty Models Investigation Update

**Date**: 2026-09-12  
**Status**: Fallback mechanism verified working - need real detection logs

---

## Test Results Summary

### 1. section_allows Filter Test
✅ **Result**: All FALLBACK_MODELS pass section_allows
```
codex-api-key:    4/4 passed (gpt-5.6-sol, gpt-5.6, gpt-5.6-luna, gpt-5.6-terra)
claude-api-key:   3/3 passed (claude-opus-5, claude-fable-5, claude-sonnet-5)
gemini-api-key:   6/6 passed (all gemini-3.1-pro variants)
openai-compatibility: 4/4 passed (multi-family)
```

### 2. topup_to_market_top Empty Input Test
✅ **Result**: All sections return non-empty fallback models
```
claude-api-key:        [] → [claude-opus-5, claude-fable-5, claude-sonnet-5]
codex-api-key:         [] → [gpt-5.6, gpt-5.6-luna, gpt-5.6-sol, gpt-5.6-terra]
gemini-api-key:        [] → [6 gemini-3.1-pro variants]
openai-compatibility:  [] → [claude-opus-5, gpt-5.6-sol, kimi-k3, gemini-3.1-pro]
```

### 3. name_is_safe Logic Verification
✅ **Result**: Logic is CORRECT
- Returns `''` (empty string, falsy) for safe names → continues checks
- Returns `'reason'` (non-empty, truthy) for unsafe names → rejects
- No inversion bug

---

## Verified Code Paths

### Path 1: Detection Complete Failure
```python
# Line 2415-2418
candidates = [] + [] + [] + []  # all empty
merged = newest_generation_per_line([]) = []

# Line 2453-2455
if not merged and v.models:  # if not [] and []: → False
    # Skipped
models = []

# Line 2459-2462
if not custom_only:
    models, added, fill_src = topup_to_market_top(section, [], cfg, remote)
    # Returns fallback models ✅
```

### Path 2: Detection with Unrecognized Models
```python
# v.models = ['opus-5']  (family unrecognized)
merged = []  # filtered out by section_allows

# Line 2453-2455
if not [] and ['opus-5']:  # True
    merged = ['opus-5']  # FALLBACK WORKS ✅

models = ['opus-5']
```

---

## Remaining Questions

### Question 1: Why User Sees Empty Models?

**Theory A**: Detection never runs (v.models stays empty)
- Frontend not triggering detection
- Detection crashes before populating v.models
- Network timeout preventing all probes

**Theory B**: Detection succeeds but output gets cleared
- models non-empty during build_plan
- Cleared somewhere between plan.py and writeback.py
- Bug in SectionPlan assignment or serialization

**Theory C**: User looking at wrong output
- Seeing old cached config.yaml
- Frontend showing stale data
- Wrong section being examined

### Question 2: What Are the "待定" (TBD) Entries?

User mentioned: "blank models, blank parameters, '待定' entries"

Search for "待定" in codebase:
```bash
grep -r "待定" cpa_probe/
```

---

## Required Actions

### 1. Run Actual Detection (CRITICAL)
```bash
cd /c/Users/devin/OneDrive/Desktop/fsdownload/upstream-importer
python3 server.py --config ../config.yaml
# Trigger full detection via frontend
# Observe logs for:
# - "models 为空"
# - "topup_to_market_top"
# - "latest_models"
# - SectionPlan.models values
```

### 2. Add Debug Logging
```python
# In plan.py around line 2462
logger.warning(
    f"DEBUG models assignment: section={section}, "
    f"base_url={base}, models={models}, "
    f"v.models={v.models}, merged={merged}")
```

### 3. Check Actual Detection Results
Read the detection results HTML from user's screenshot:
```
C:\Users\devin\OneDrive\Desktop\投喂台 · CPA 上游灌输.mhtml
```

Find stations with empty models output.

### 4. Search for "待定" Pattern
```bash
cd /c/Users/devin/OneDrive/Desktop/fsdownload/upstream-importer
grep -rn "待定" . --include="*.py"
```

---

## Hypothesis Ranking

**Most Likely (80%)**: Detection results show v.models=[] for many stations
- TLS fingerprinting blocks (like api.zzzcoding.org)
- 403/401/503 errors prevent model discovery
- Timeout before any model detected
- topup_to_market_top somehow returning empty (despite tests showing it works)

**Possible (15%)**: Bug in specific code path not covered by unit tests
- Edge case in newest_generation_per_line
- Section name mismatch (e.g., "claude" vs "claude-api-key")
- remote_names() returning empty AND cfg=None AND fallback filtered

**Unlikely (5%)**: User error
- Looking at wrong file
- Cached output
- Misinterpreting logs

---

## Next Immediate Steps

1. **Read actual detection HTML** from user's mhtml file
   - Extract stations with empty models
   - Check error patterns

2. **Add validation in writeback.py**
   ```python
   def model_lines(indent: str) -> list[str]:
       if not sp.models:
           logger.error(
               f"EMPTY MODELS: section={sp.section}, "
               f"base_url={sp.base_url}, model_source={sp.model_source}, "
               f"highest_models={sp.highest_models}")
           # Raise or use highest_models as fallback
       # ... rest of logic
   ```

3. **Run detection with enhanced logging**
   - Monitor models variable values
   - Track topup_to_market_top calls
   - Verify fallback triggered

4. **Compare with working stations**
   - Find stations that DO have models
   - Identify difference in code path
