# Quick Start - Integration Testing Guide

## Prerequisites Check

```bash
# 1. Verify Python environment
python3 --version  # Should be 3.8+

# 2. Verify project location
pwd
# Should be: /c/Users/devin/OneDrive/Desktop/fsdownload/upstream-importer

# 3. Verify fixes applied
python3 -c "from cpa_probe.plan import _needs_tls_proxy; print('OK')"

# 4. Check nginx availability
which nginx || which nginx.exe
```

---

## Step-by-Step Testing

### Phase 1: Start nginx TLS Proxy

**Windows**:
```powershell
cd C:\nginx
.\nginx.exe -c C:\Users\devin\OneDrive\Desktop\fsdownload\nginx-tls-proxy.conf

# Verify running
.\nginx.exe -t -c C:\Users\devin\OneDrive\Desktop\fsdownload\nginx-tls-proxy.conf
```

**Verify Proxy Works**:
```bash
# Should return 200 (or 401 if no valid key)
curl -v http://localhost:8443/v1/models
```

---

### Phase 2: Run Detection

```bash
cd /c/Users/devin/OneDrive/Desktop/fsdownload/upstream-importer

# Start server
python3 server.py --config ../config.yaml

# Access Web UI: http://localhost:PORT
# Click "全量重新检测" button
```

**Watch Real-time Logs**:
```bash
tail -f server.log | grep -E "TLS|模型为空|应急|硬编码"
```

---

### Phase 3: Verify Output Quality

**Test 1: Check for Empty Models**:
```bash
python3 << 'ENDVERIFY'
import yaml
with open('config.yaml') as f:
    cfg = yaml.safe_load(f)
empty_count = 0
for section in ['claude-api-key', 'codex-api-key', 'gemini-api-key']:
    for entry in cfg.get(section, []):
        if not entry.get('models', []):
            print(f"EMPTY: {section} - {entry.get('base-url')}")
            empty_count += 1
print(f"\nResult: {empty_count} empty entries")
print("SUCCESS" if empty_count == 0 else "FAILED")
ENDVERIFY
```

**Test 2: Verify proxy-url Injection**:
```bash
grep -B 2 -A 10 "zulu.example" config.yaml | grep "proxy-url"
```

**Expected**: `proxy-url: http://localhost:8443`

**Test 3: Count Total Models**:
```bash
grep "- name:" config.yaml | wc -l
```

---

### Phase 4: Test with CPA

```bash
cd /c/Users/devin/OneDrive/Desktop/CLIProxyAPI-main
./cpa --config /c/Users/devin/OneDrive/Desktop/fsdownload/upstream-importer/config.yaml
```

**Test Request**:
```bash
curl http://localhost:8317/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.6","messages":[{"role":"user","content":"test"}]}'
```

---

## Troubleshooting

### Issue 1: nginx Not Starting

**Error**: `bind() to 0.0.0.0:8443 failed`

**Solution**: Port already in use, change to 8444 in nginx-tls-proxy.conf and plan.py

### Issue 2: Still Getting Empty Models

```bash
grep -E "模型为空|应急回退" server.log
```

If no logs, fallback didn't trigger. Check detection succeeded first.

### Issue 3: proxy-url Not Injected

```bash
python3 -c "from cpa_probe.plan import _needs_tls_proxy; print(_needs_tls_proxy('https://zulu.example/v1', 'codex-api-key'))"
```

Expected: `(True, 'http://localhost:8443')`

---

## Success Criteria

- [ ] nginx proxy starts without errors
- [ ] Detection completes without crashes
- [ ] config.yaml has zero empty models
- [ ] zulu.example has proxy-url injected
- [ ] All priority values are numeric
- [ ] CPA loads config without errors
- [ ] Requests through proxy return 200

---

## Quick Commands

```bash
# Start nginx
nginx.exe -c C:\Users\devin\OneDrive\Desktop\fsdownload\nginx-tls-proxy.conf

# Stop nginx
nginx.exe -s stop

# Start detection
python3 server.py --config ../config.yaml

# Watch logs
tail -f server.log | grep -E "TLS|模型|应急"

# Check empty models
python3 -c "import yaml; cfg=yaml.safe_load(open('config.yaml')); print(sum(1 for s in ['codex-api-key'] for e in cfg.get(s,[]) if not e.get('models')))"

# Count models
grep "- name:" config.yaml | wc -l
```

---

**Status**: Ready for immediate testing  
**Estimated time**: 15-20 minutes  
**Prerequisites**: nginx, Python 3.8+, valid config.yaml
