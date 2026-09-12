import re
import quopri

# 解码 mhtml 并提取关键信息
with open('/c/Users/devin/OneDrive/Desktop/投喂台 · CPA 上游灌输.mhtml', 'rb') as f:
    raw = f.read()

decoded = quopri.decodestring(raw).decode('utf-8', errors='ignore')

print("=== Detection Results Analysis ===\n")

# 1. 提取统计信息
stats = re.search(r'(\d+)\s*凭据.*?(\d+)\s*请求.*?(\d+)\s*秒.*?(\d+)\s*并发', decoded)
if stats:
    print(f"Stats: {stats.group(1)} credentials, {stats.group(2)} requests, {stats.group(3)}s, {stats.group(4)} concurrent\n")

# 2. 查找所有站点及其状态
# 查找形如 "可用段 [xxx] · N 次请求" 的模式
stations = re.findall(r'可用段\s*\[([^\]]*)\].*?(\d+)\s*次请求.*?(https?://[^\s<>"]+)', decoded, re.DOTALL)

print(f"Found {len(stations)} stations:\n")

# 3. 统计每个站点的状态
station_stats = {}
for segments, requests, url in stations[:30]:  # 前30个站点
    # 清理 URL
    url = url.split('<')[0].strip()
    
    station_stats[url] = {
        'segments': segments.strip() if segments else '无',
        'requests': requests
    }

for url, info in list(station_stats.items())[:15]:
    print(f"{url}")
    print(f"  Segments: [{info['segments']}]")
    print(f"  Requests: {info['requests']}")
    
    # 查找该 URL 附近的错误信息
    url_escaped = re.escape(url[:30])
    context = re.findall(f'{url_escaped}.*?(403|401|503|524|405|400|404|500|死路|限频|鉴权|临时|余额)', decoded[:500000], re.DOTALL)
    if context:
        errors = set([e for e in context[:10] if len(e) < 10])
        if errors:
            print(f"  Errors: {', '.join(errors)}")
    print()

# 4. 查找明确的空段站点
empty_segments = [url for url, info in station_stats.items() if info['segments'] == '无' or info['segments'] == '']
if empty_segments:
    print(f"\nStations with empty segments ({len(empty_segments)}):")
    for url in empty_segments[:10]:
        print(f"  - {url}")

# 5. 查找 zzzcoding 相关的所有信息
print("\n\n=== api.zzzcoding.org Detail ===")
zzz_lines = [line for line in decoded.split('\n') if 'zzzcoding' in line.lower()]
for line in zzz_lines[:20]:
    # 解码并清理
    clean = re.sub(r'<[^>]+>', ' ', line)
    clean = re.sub(r'\s+', ' ', clean).strip()
    if clean and len(clean) > 10:
        print(clean[:150])

