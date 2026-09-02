"""全量重探功能测试

验证：
1. extract_existing_entries 提取既有站
2. BatchProber 站级并发
3. rebuild_config_full 的注释保全、priority 排序与段字段结构
"""

import os
import re
import sys
import time

# 与其余套件一致：自己插 sys.path，不依赖调用方设 PYTHONPATH。
# 漏了这两行 CI 上直接 ModuleNotFoundError —— 本地靠 PYTHONPATH=. 跑不会暴露。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cpa_probe.batch import extract_existing_entries, BatchProber
from cpa_probe.pipeline import Prober
from cpa_probe import parse as cp


MINIMAL_CFG = """
gemini-api-key:
  - api-key: "AIzaSyABC123"
    base-url: "generativelanguage.googleapis.com"
    priority: 1
  - api-key: "AIzaSyDEF456"
    base-url: "api.gemini.com"
    priority: 2

codex-api-key:
  - api-key: "cdx_abc123"
    base-url: "https://codex.example.com/v1"
    priority: 1

claude-api-key:
  - api-key: "sk-ant-abc123"
    base-url: "api.anthropic.com"
    priority: 1
  - api-key: "sk-ant-def456"
    base-url: "claude.example.com"
    priority: 2

openai-compatibility:
  - name: "test-provider"
    base-url: "https://compat.example.com/v1"
    api-key-entries:
      - api-key: "sk-compat-123"
      - api-key: "sk-compat-456"
    models:
      - name: "gpt-4"
        alias: "gpt-4"
"""


def test_extract_existing_entries():
    """测试提取既有站"""
    import yaml
    cfg = yaml.safe_load(MINIMAL_CFG)

    entries = extract_existing_entries(cfg)

    # 验证数量：2 gemini + 1 codex + 2 claude + 2 compat = 7
    assert len(entries) == 7, f"Expected 7 entries, got {len(entries)}"

    # 验证结构
    section_counts = {"gemini": 0, "codex": 0, "claude": 0, "compat": 0}
    for section_short, base_url, api_key, orig_section in entries:
        assert section_short in section_counts
        assert base_url
        assert api_key
        assert orig_section
        section_counts[section_short] += 1

    assert section_counts == {"gemini": 2, "codex": 1, "claude": 2, "compat": 2}
    print("[OK] extract_existing_entries: 7 entries extracted")


def test_job_eta():
    """ETA 与进度度量。三条硬约束，每条都对应一次实测教训。"""
    import random
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import server

    class _Row:
        host = "x.example.com"

    # ① 样本不足时不给数字。宁可「估算中」，也不给一个必然错的秒数 ——
    #    先报 2 分钟后来变 8 分钟会让操作员做错决定。
    job = server.Job("j1", [_Row() for _ in range(20)], {})
    job.workers = 4
    for i in range(server.Job._ETA_MIN_SAMPLES - 1):
        job.unit_done.append(float(i + 1))
    p = job._progress(20)
    assert "eta_sec" not in p, f"样本不足却给了 ETA：{p}"
    assert p["unit_done"] == 4 and p["unit_total"] == 20

    # ② 低并发下给区间，且区间必须包住点值
    job.unit_done.append(5.0)
    p = job._progress(20)
    assert "eta_sec" in p, f"5 个样本应当能估：{p}"
    assert p["eta_lo"] <= p["eta_sec"] <= p["eta_hi"], (
        f"点值必须落在区间内：{p['eta_lo']} / {p['eta_sec']} / {p['eta_hi']}")
    assert p["rate_per_min"] > 0
    assert p["samples"] == 5

    # ③ 高并发下**不给** ETA，只给吞吐率。
    #
    # 2026-09-01 回放验证：并发 30 时区间命中率只有 9%（并发 4 是 74%）。
    # 剩余墙钟被「在飞最长的那个还需多久」主导，占比可达 100%，而那个值
    # 在它结束前无法从已完成的样本推出 —— 不是算法问题，是信息不在样本里。
    job_hi = server.Job("j2", [_Row() for _ in range(20)], {})
    job_hi.workers = 30
    job_hi.unit_done.extend([1.0, 2.0, 3.0, 4.0, 5.0])
    p = job_hi._progress(20)
    assert "eta_sec" not in p, f"并发 30 不该给 ETA：{p}"
    assert p.get("eta_suppressed"), "抑制 ETA 时必须说明原因"
    assert p["rate_per_min"] > 0, "吞吐率是实测量，任何并发下都该给"

    # ④ 在飞跟踪：能指出最慢的那个站与它已跑多久
    job2 = server.Job("j3", [_Row() for _ in range(3)], {})
    job2.mark_unit_start("slow.example.com")
    job2.mark_unit_start("fast.example.com")
    p = job2._progress(3)
    assert p["in_flight"] == 2
    assert p["slowest_host"] == "slow.example.com", (
        f"最慢站应是最早开始的那个，实得 {p.get('slowest_host')}")
    assert p["slowest_age"] >= 0
    job2.mark_unit_done("slow.example.com")
    p = job2._progress(3)
    assert p["in_flight"] == 1
    assert p["slowest_host"] == "fast.example.com"

    # ⑤ snapshot 必须把进度字段一路带到 JSON —— 前端读的是那份
    snap = job.snapshot()
    for k in ("unit_done", "unit_total", "in_flight", "eta_sec", "rate_per_min"):
        assert k in snap, f"snapshot 缺字段 {k}"

    print("[OK] Job ETA: 样本不足不给数字、低并发给区间、"
          "高并发只给速率、在飞可追踪")


def test_batch_prober_progress():
    """测试 BatchProber 进度回调（使用假 Prober）"""

    # 假 Prober：立即返回结果，不发请求
    class FakeProber:
        def __init__(self, **kwargs):
            pass

        def probe(self, row):
            time.sleep(0.01)  # 模拟耗时
            # 返回假的 CandidateResult
            class FakeResult:
                def __init__(self):
                    self.usable_sections = ["gemini", "claude"]  # 2段通
            return FakeResult()

    # 准备测试数据（用假对象模拟 ParsedRow）
    class FakeRow:
        def __init__(self, url, key="sk-x"):
            self.bare = url          # BatchProber 的结果键是 (bare, api_key)
            self.api_key = key       # 同站多 Key 不能互相覆盖，所以键含 api_key
            self.host = url.split("//")[1].split("/")[0] if "//" in url else url

    rows = [FakeRow(f"https://site{i}.com") for i in range(10)]

    # 进度收集。分两类：
    #   占位调用（current=0）—— 探测**开始**时发，让调用方记「谁在飞」
    #   进度调用（current>0）—— 探测**完成**时发，带统计
    # 两者都要脱敏（site 不含 api_key）。
    progress_log = []
    start_log = []
    seen_sites = []
    def progress_cb(current, total, site, stats):
        seen_sites.append(site)
        if current == 0:
            start_log.append(site)
        else:
            progress_log.append((current, total, stats.copy()))

    # 批量探测
    batch_prober = BatchProber(FakeProber(), max_workers=3)
    results = batch_prober.probe_batch(rows, progress_callback=progress_cb)

    # 验证结果
    assert len(results) == 10
    # 每个站一次开始 + 一次完成
    assert len(start_log) == 10, f"应有 10 次起始占位，实得 {len(start_log)}"
    assert len(progress_log) == 10, f"应有 10 次进度回调，实得 {len(progress_log)}"
    assert progress_log[-1][0] == 10  # 最后一次是 10/10
    assert progress_log[-1][1] == 10

    # 验证统计：所有站 2 段通 —— 新口径下「至少一段可用」就算 success，
    # 四段全通才进 all_four。旧口径要求四段全通才算 success，实测 79 个
    # 凭据里只有 1 个满足，界面长期显示「成功 0」。
    assert batch_prober._stats["success"] == 10
    assert batch_prober._stats["all_four"] == 0
    assert batch_prober._stats["failure"] == 0

    # 进度回调不得泄漏完整 api_key。
    #
    # 2026-09-01 实测泄漏：原来传的是结果字典的键 (bare, api_key)，于是
    # 完整明文 key 经 server 的 progress 事件进日志、进 /api/job 的 JSON、
    # 再进导出文件 —— 79 凭据的一份日志里 74 个 key 完整可读。
    # 项目的安全模型写着「完整 key 只在内存里，不落日志、不进 JSON 响应」。
    assert len(seen_sites) == 20, f"起始+完成共 20 次，实得 {len(seen_sites)}"
    for site in seen_sites:
        assert isinstance(site, str), f"site 应是字符串，实得 {type(site)}"
        assert "sk-" not in site, f"进度回调泄漏了 api_key：{site}"
        assert site.startswith("https://site"), f"site 形态不对：{site}"

    print(f"[OK] BatchProber: 10 sites probed, {len(progress_log)} callbacks，"
          f"起始占位 {len(start_log)} 次，进度回调无 key 泄漏")


def test_batch_prober_stats():
    """测试 BatchProber 统计分类"""

    class FakeProber:
        def __init__(self, **kwargs):
            self._counter = 0

        def probe(self, row):
            self._counter += 1
            class FakeResult:
                def __init__(self, usable_count):
                    self.usable_sections = ["gemini"] * usable_count
            # 第1个站：4段全通，第2-3个站：部分通，第4-5个站：全不通
            if self._counter == 1:
                return FakeResult(4)
            elif self._counter in [2, 3]:
                return FakeResult(2)
            else:
                return FakeResult(0)

    class FakeRow:
        def __init__(self, url, key="sk-x"):
            self.bare = url
            self.api_key = key

    rows = [FakeRow(f"https://site{i}.com") for i in range(5)]

    batch_prober = BatchProber(FakeProber(), max_workers=2)
    results = batch_prober.probe_batch(rows)

    # 1 站四段全通 + 2 站两段通 = 3 个「至少一段可用」；其中 1 个四段全通。
    assert batch_prober._stats["success"] == 3
    assert batch_prober._stats["all_four"] == 1
    assert batch_prober._stats["failure"] == 2
    # partial 保留为兼容键，恒 0：外部还在读它，删掉会静默变 KeyError
    assert batch_prober._stats["partial"] == 0

    print(f"[OK] BatchProber stats: {batch_prober._stats}")


def test_batch_prober_exception_handling():
    """测试 BatchProber 异常处理"""

    class FakeProber:
        def __init__(self, **kwargs):
            self._counter = 0

        def probe(self, row):
            self._counter += 1
            if self._counter == 2:
                raise RuntimeError("Simulated failure")
            class FakeResult:
                def __init__(self):
                    self.usable_sections = ["gemini"]
            return FakeResult()

    class FakeRow:
        def __init__(self, url, key="sk-x"):
            self.bare = url
            self.api_key = key

    rows = [FakeRow(f"https://site{i}.com") for i in range(3)]

    batch_prober = BatchProber(FakeProber(), max_workers=1)
    results = batch_prober.probe_batch(rows)

    # 异常站被计入 failure，其余两站各 1 段通 = success
    assert batch_prober._stats["success"] == 2   # 第1、3站
    assert batch_prober._stats["failure"] == 1   # 第2站（异常）
    assert len(results) == 2  # 只有成功的2个结果

    print(f"[OK] Exception handling: {batch_prober._stats}")


def test_rebuild_config_preserves_comments():
    """测试全量重建保留注释"""
    from cpa_probe.writeback import rebuild_config_full
    from cpa_probe.plan import SectionPlan, ImportPlan
    import yaml

    original = """host: "127.0.0.1"
port: 8317

gemini-api-key:
  # alfa 403 banned，从 900 降权待解封
  - api-key: "AIzaOLD"
    base-url: "old.example.com"
    priority: 200

claude-api-key:
  # relay-b 实测 503 No available channel
  - api-key: "sk-ant-OLD"
    base-url: "claude-old.example.com"
    priority: 300
"""

    cfg = yaml.safe_load(original)
    original_lines = original.splitlines(keepends=True)

    # 构造新方案（priority 改变）
    sp_gemini = SectionPlan(
        section="gemini-api-key",
        base_url="old.example.com",
        api_key="AIzaOLD",
        models=["gemini-2.5-flash"],
        priority=150,  # 改了
    )
    sp_claude = SectionPlan(
        section="claude-api-key",
        base_url="claude-old.example.com",
        api_key="sk-ant-OLD",
        models=["claude-opus-5"],
        priority=250,  # 改了
    )

    plan_gemini = ImportPlan(host="old.example.com", masked_key="AIza...OLD")
    plan_gemini.sections["gemini"] = sp_gemini

    plan_claude = ImportPlan(host="claude-old.example.com", masked_key="sk-ant...OLD")
    plan_claude.sections["claude"] = sp_claude

    all_plans = {
        ("old.example.com", "AIzaOLD"): plan_gemini,
        ("claude-old.example.com", "sk-ant-OLD"): plan_claude,
    }

    # 全量重建
    rebuilt, warnings = rebuild_config_full(cfg, all_plans, original_lines)

    # 验证全局配置保留
    assert 'host: "127.0.0.1"' in rebuilt
    assert "port: 8317" in rebuilt

    # 验证注释保留
    assert "alfa 403 banned" in rebuilt
    assert "relay-b 实测 503" in rebuilt

    # 验证 priority 更新（用正则取值，因为 render_entry 会在行尾附定档理由）
    import re
    prios = {int(m.group(1))
             for m in re.finditer(r"^\s*priority:\s*(\d+)", rebuilt, re.M)}
    assert prios == {150, 250}, f"priority 应为 {{150, 250}}，实际 {prios}"

    # 验证 YAML 有效
    parsed = yaml.safe_load(rebuilt)
    assert parsed is not None
    assert "gemini-api-key" in parsed
    assert "claude-api-key" in parsed

    print(f"[OK] rebuild_config_full: comments preserved, {len(warnings)} warnings")


def test_rebuild_config_priority_order():
    """测试全量重建后 priority 从高到低排序"""
    from cpa_probe.writeback import rebuild_config_full
    from cpa_probe.plan import SectionPlan, ImportPlan
    import yaml

    original = """host: "127.0.0.1"

gemini-api-key:
  - api-key: "key1"
    base-url: "site1.com"
    priority: 100
"""

    cfg = yaml.safe_load(original)
    original_lines = original.splitlines(keepends=True)

    # 构造三个站，priority 乱序
    plans = {}
    for i, (url, key, prio) in enumerate([
        ("site-low.com", "key-low", 100),
        ("site-high.com", "key-high", 900),
        ("site-mid.com", "key-mid", 500),
    ]):
        sp = SectionPlan(
            section="gemini-api-key",
            base_url=url,
            api_key=key,
            models=["gemini-2.5-flash"],
            priority=prio,
        )
        plan = ImportPlan(host=url, masked_key=f"{key[:4]}...")
        plan.sections["gemini"] = sp
        plans[(url, key)] = plan

    rebuilt, warnings = rebuild_config_full(cfg, plans, original_lines)

    # 提取 priority 出现顺序。render_entry 会在同一行尾部附定档理由注释
    # （priority: 900        # 2026-09-01 批量导入 · …），所以要先切掉 #。
    import re
    priorities = [int(m.group(1))
                  for m in re.finditer(r"^\s*priority:\s*(\d+)", rebuilt, re.M)]

    # 新方案按 priority 降序；原有的 site1.com 没进方案，按 keep_unplanned
    # 原样保留在后面（2026-09-02 起的行为 —— 未勾选的条目不删）。
    assert priorities[:3] == [900, 500, 100], f"新方案未降序: {priorities}"
    assert priorities == [900, 500, 100, 100], (
        f"原条目应被保留在末尾，实得 {priorities}")
    assert "site1.com" in rebuilt, "未进方案的原条目被删了"
    assert any("已原样保留" in w for w in warnings), (
        f"保留原条目时必须给警告，实得 {warnings}")

    print(f"[OK] Priority order: {priorities}")


def test_rebuild_config_section_structure():
    """重建产出的字段结构必须与 CLIProxyAPI 的期望一致。

    这一项锁的是端到端验证抓到的三个缺陷（单元测试的构造数据当时绕过了它们）：

      1. 段名用短名（gemini）而非完整段名（gemini-api-key）时，分组全部
         miss，产出一个四段皆空的文件 —— YAML 合法、validate() 通过、
         静默地把 175 个站清空
      2. 前三段被写了 `name` 字段 —— CLIProxyAPI 的 gemini/codex/claude 段
         没有这个字段，只有 compat 有
      3. compat 段被按前三段的扁平结构渲染 —— 它要的是
         name + models[{name,alias}] + api-key-entries[{api-key}]
    """
    from cpa_probe.writeback import rebuild_config_full
    from cpa_probe.plan import SectionPlan, ImportPlan
    import yaml

    original = """host: "127.0.0.1"

gemini-api-key:
  - api-key: "seed"
    base-url: "seed.example.com"
    priority: 1

openai-compatibility:
  - name: "seed"
    base-url: "https://seed.example.com/v1"
    api-key-entries:
      - api-key: "seed"
    models:
      - name: "m"
        alias: "m"
"""
    cfg = yaml.safe_load(original)
    lines = original.splitlines(keepends=True)

    plans = {}

    # 前三段各一个站
    for sec, url, key in (
        ("gemini-api-key", "g.example.com", "AIzaG"),
        ("codex-api-key", "https://c.example.com/v1", "cdxC"),
        ("claude-api-key", "cl.example.com", "sk-ant-CL"),
    ):
        sp = SectionPlan(section=sec, base_url=url, api_key=key,
                         models=["m1"], priority=500)
        p = ImportPlan(host=url, masked_key="x")
        p.sections[sec] = sp
        plans[(url, key)] = p

    # compat 段：同一个站两个 Key，必须归并成一个 provider
    for key in ("sk-A", "sk-B"):
        sp = SectionPlan(section="openai-compatibility",
                         base_url="https://compat.example.com/v1",
                         api_key=key, models=["m1"], priority=400)
        p = ImportPlan(host="compat.example.com", masked_key="x")
        p.sections["openai-compatibility"] = sp
        plans[("https://compat.example.com/v1", key)] = p

    rebuilt, warns = rebuild_config_full(cfg, plans, lines)
    parsed = yaml.safe_load(rebuilt)

    # 缺陷 1：四段不能是空的
    for sec in ("gemini-api-key", "codex-api-key", "claude-api-key",
                "openai-compatibility"):
        items = parsed.get(sec) or []
        assert items, f"{sec} 为空 —— 段名分组 miss 了（缺陷 1 回归）"

    # 缺陷 2：前三段不该有 name
    #
    # 只检查**本次方案生成的**条目。keep_unplanned（2026-09-02）会把没进方案
    # 的原条目原样保留 —— 这个 fixture 的 gemini 段有个 seed 条目没有 models，
    # 那是原文的样子，不是渲染缺陷。按 api-key 认出方案条目。
    planned_keys = {"AIzaG", "cdxC", "sk-ant-CL"}
    for sec in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        for e in parsed[sec]:
            assert "name" not in e, f"{sec} 不该有 name 字段（缺陷 2 回归）"
            assert "api-key" in e and "base-url" in e
            if e.get("api-key") not in planned_keys:
                continue                    # 原样保留的旧条目，不按新格式要求
            assert "models" in e, f"{sec} 缺 models"
            # models 是 [{name, alias}] 结构，不是裸字符串列表
            assert isinstance(e["models"][0], dict), f"{sec} models 结构不对"
            assert "alias" in e["models"][0]

    # keep_unplanned：原有的 seed 条目必须还在
    assert any(e.get("api-key") == "seed" for e in parsed["gemini-api-key"]),         "未进方案的原条目被删了（keep_unplanned 回归）"

    # 缺陷 3：compat 段结构与归并
    compat = parsed["openai-compatibility"]
    # 本次方案的那个站归并成 1 个 provider；原有的 seed provider 由
    # keep_unplanned 原样保留（2026-09-02 起）。所以总数是 2，其中新方案 1 个。
    mine = [e for e in compat
            if e.get("base-url") == "https://compat.example.com/v1"]
    assert len(mine) == 1, (
        f"同站两个 Key 应归并成 1 个 provider，实际 {len(mine)}")
    assert any(e.get("name") == "seed" for e in compat),         "未进方案的原 provider 被删了（keep_unplanned 回归）"
    prov = mine[0]
    assert "name" in prov, "compat 段必须有 name（CPA 的 provider 身份）"
    assert "api-key-entries" in prov, "compat 段必须用 api-key-entries"
    assert "api-key" not in prov, "compat 段不该在 provider 级放 api-key"
    keys = [e["api-key"] for e in prov["api-key-entries"]]
    assert set(keys) == {"sk-A", "sk-B"}, f"两个 Key 都要在，实际 {keys}"

    print(f"[OK] Section structure: 前三段无 name、compat 归并 "
          f"{len(prov['api-key-entries'])} 个 Key、{len(warns)} 条警告")


def test_credential_dedup():
    """同一凭据被写进多个段时，探测只做一次。

    config.yaml 的条目是「(凭据, 段)」的组合：很多中转站用同一把 Key 提供
    多种协议，于是同一个 url+key 被写进 2-4 个段。而 Prober.probe() 的语义
    本来就是「拿一个凭据把四段各打一遍」—— 按条目喂它等于重复探测。

    实测那份生产配置：175 个条目只有 77 个不同凭据，按条目探会白打 98 次。
    """
    import yaml
    cfg = yaml.safe_load("""
gemini-api-key:
  - api-key: "sk-SAME"
    base-url: "multi.example.com"
  - api-key: "sk-ONLY-GEMINI"
    base-url: "single.example.com"

codex-api-key:
  - api-key: "sk-SAME"
    base-url: "multi.example.com/v1"

claude-api-key:
  - api-key: "sk-SAME"
    base-url: "multi.example.com"

openai-compatibility:
  - name: "multi"
    base-url: "multi.example.com/v1"
    api-key-entries:
      - api-key: "sk-SAME"
    models:
      - name: "m"
        alias: "m"
""")
    entries = extract_existing_entries(cfg)
    assert len(entries) == 5, f"应有 5 个条目，实际 {len(entries)}"

    # 按 (host, key) 去重 —— 与 run_job_full_redetect 同一套键
    from cpa_probe.parse import host_of
    creds = {(host_of(base), key) for _s, base, key, _o in entries}

    # sk-SAME 跨四段但 host 相同，应折成 1 个；加上 single 那个 = 2
    assert len(creds) == 2, f"去重后应有 2 个凭据，实际 {len(creds)}：{creds}"

    # 关键：base-url 带不带 /v1 不能影响去重判定
    hosts = {h for h, _k in creds}
    assert hosts == {"multi.example.com", "single.example.com"}, hosts

    print(f"[OK] Credential dedup: 5 条目 → {len(creds)} 凭据（省 3 次全流程探测）")


def test_probe_text_not_trivial():
    """探测文本不能是简单问候 —— 那是站方反测活规则最先拦的形态。

    2026-08-29 实测修正过一次（原来用 "hi"）。这一项防止它被改回去：
    简单问候的特征是短、无技术内容、疑似测活，站方按这个封号。
    """
    from cpa_probe import request as req

    text = req.PROBE_TEXT.lower().strip()

    banned = ["hi", "hello", "hey", "你好", "您好", "test", "ping",
              "你是什么模型", "what model are you", "who are you",
              "介绍一下你自己", "1", "?", "。"]
    for b in banned:
        assert text != b, f"PROBE_TEXT 不能是 {b!r}"
        assert not text.startswith(b + " "), f"PROBE_TEXT 不能以 {b!r} 开头"

    # 长度下限：太短的一律像测活
    assert len(text) >= 40, f"PROBE_TEXT 太短（{len(text)} 字符），像测活"

    # 必须有技术内容 —— 至少命中一个技术词
    tech = ["hash", "tree", "map", "tcp", "http", "algorithm", "function",
            "database", "index", "cache", "sort", "queue", "thread"]
    assert any(t in text for t in tech), f"PROBE_TEXT 缺技术内容：{text!r}"

    # pipeline 里那条反测活重试用的备用文本也要过同一道
    import io as _io
    src = _io.open(os.path.join(ROOT, "cpa_probe", "pipeline.py"),
                   encoding="utf-8").read()
    import re as _re
    for m in _re.finditer(r'text="([^"]{1,120})"', src):
        t = m.group(1)
        if set(t) == {"x"}:          # 上下文二分的填充，不是给站方读的
            continue
        low = t.lower()
        assert len(t) >= 20, f"pipeline 里的探测文本太短：{t!r}"
        assert any(x in low for x in tech), f"pipeline 里的文本缺技术内容：{t!r}"

    print(f"[OK] Probe text: {len(text)} 字符、含技术内容、非问候")


def test_profile_verdict_reuse_saves_calls():
    """同段整梯全败后，后续种子跳过画像梯 —— 用真实请求数验证。

    门票是站+段的属性（站方查 headers 与 body 形态，不看模型名），所以第一个
    种子试完整梯全败之后，同段的后续种子不必重问。

    实测（假上游全 403）：57 次 → 30 次，省 47%。
    """
    import json
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from cpa_probe.pipeline import Prober
    from cpa_probe.parse import parse_lines

    calls = {"n": 0}
    lock = threading.Lock()

    class AllGate(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, payload):
            raw = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _count_and_deny(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            with lock:
                calls["n"] += 1
            self._send(403, {"error": {"message": "only allows CC clients",
                                      "type": "permission_error"}})

        do_GET = _count_and_deny
        do_POST = _count_and_deny

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), AllGate)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        row = parse_lines(f"http://127.0.0.1:{port},sk-test").valid[0]

        calls["n"] = 0
        Prober(gap=0.0, probe_context=False, swap_samples=0, workers=4,
               reuse_profile_verdict=True).probe(row)
        with_reuse = calls["n"]

        calls["n"] = 0
        Prober(gap=0.0, probe_context=False, swap_samples=0, workers=4,
               reuse_profile_verdict=False).probe(row)
        without = calls["n"]
    finally:
        srv.shutdown()

    assert with_reuse < without, (
        f"开复用应更省，实际 开={with_reuse} 关={without}")
    saved_pct = (1 - with_reuse / without) * 100
    assert saved_pct >= 30, f"省得太少（{saved_pct:.0f}%），复用可能没生效"

    print(f"[OK] Profile reuse: {without} → {with_reuse} 次请求"
          f"（省 {saved_pct:.0f}%）")


def test_profile_drift_detection():
    """画像基线漂移检测：能解析 Go 常量、能报差异、读不到源码时不假装检查过。"""
    import tempfile
    from cpa_probe import cpa_source_probe as csp

    # ── 造一份假的 CPA 源码 ──
    root = tempfile.mkdtemp(prefix="fake-cpa-")
    gd = os.path.join(root, "internal", "runtime", "executor")
    os.makedirs(gd, exist_ok=True)

    import io as _io
    with _io.open(os.path.join(gd, "claude_executor_request.go"), "w",
                  encoding="utf-8") as f:
        f.write('''package executor

const (
	claudeCodeBeta          = "claude-code-20250219"
	claudeOAuthBeta         = "oauth-2025-04-20"
	claudeMidConvSystemBeta = "mid-conversation-system-2026-04-07"
	claudeEffortBeta        = "effort-2025-11-24"
	claudeNewThingBeta      = "brand-new-2026-12-31"
)

var claudeCodeCLIConstantBetas = []string{
	"interleaved-thinking-2025-05-14",
	claudeRedactThinkingBeta,   // 常量名，但本文件没定义 → 应被跳过
	"context-management-2025-06-27",
}
''')
    with _io.open(os.path.join(gd, "codex_executor_request.go"), "w",
                  encoding="utf-8") as f:
        f.write('''package executor

const (
	codexUserAgent = "codex-tui/9.9.9 (Test) fake"
	codexOriginator = "codex-tui"
)
''')

    ident = csp.extract(root)
    assert ident.ok, f"应能解析，errors={ident.errors}"

    # 无条件序列：claudeCodeBeta + 切片(跳过未定义常量) + midconv + effort
    assert ident.claude_betas_unconditional == [
        "claude-code-20250219",
        "interleaved-thinking-2025-05-14",
        "context-management-2025-06-27",
        "mid-conversation-system-2026-04-07",
        "effort-2025-11-24",
    ], ident.claude_betas_unconditional

    # 有条件的要被单独归类（oauth 在这一类，因为 CPA 只在 oauthToken 时发）
    assert ident.claude_betas_conditional.get("claudeOAuthBeta") == "oauth-2025-04-20"

    assert ident.codex_user_agent == "codex-tui/9.9.9 (Test) fake"
    assert ident.codex_originator == "codex-tui"

    # 与真实画像梯比对：假源码里有 brand-new，画像梯没有 → 但它不在无条件
    # 序列里（没被 append），所以不该报「少发」
    drifts = csp.compare(ident)
    whats = " ".join(d.what for d in drifts)
    assert "brand-new" not in whats, "不在无条件序列里的 beta 不该被要求"

    # ── 读不到源码时不能假装检查过 ──
    r = csp.check(source_root=os.path.join(root, "nope"), cfg=None)
    assert r["checked"] is False, r
    assert r["why"], "必须说明为什么没核对"

    # ── 退回 config.yaml 那条路 ──
    # 用一个与内置常量必然不同的值：检测的是「CPA 侧更新了而抄录的常量没跟上」，
    # 不是拿运行时派生值自比（那样永远相等，等于没检查）。
    from cpa_probe import profiles as _pf
    r2 = csp.check(source_root="", cfg={
        "claude-header-defaults": {"os": _pf._CC_OS_DEFAULT + "-NEW"}
    })
    assert r2["checked"] is True and r2.get("partial") is True, r2
    assert r2["uncovered"], "必须写明哪些没覆盖到"
    assert any("x-stainless-os" in d["what"] for d in r2["drifts"]), r2["drifts"]
    # 这类差异不影响当前探测（会用配置里的新值），所以是 info 而非 warn
    assert all(d["severity"] == "info" for d in r2["drifts"]), r2["drifts"]

    # 配置与内置常量一致时不该报漂移
    r3 = csp.check(source_root="", cfg={
        "claude-header-defaults": {"os": _pf._CC_OS_DEFAULT}
    })
    assert not r3["drifts"], r3["drifts"]

    print(f"[OK] Drift detection: 解析 {len(ident.claude_betas_unconditional)} 项"
          f"无条件 + {len(ident.claude_betas_conditional)} 项有条件；"
          f"未核对与部分核对都有明确标记")


def test_drift_remote_degrade():
    """远程模式拉不到时必须落到 config.yaml 路径，并带出失败原因。

    不能静默降级 —— 那会让人以为「已按源码核对过」，而实际只比了 UA 与
    X-Stainless 几项。这一项不发真实网络请求（用不存在的 ref 让它必然失败）。
    """
    from cpa_probe import cpa_source_probe as csp
    from cpa_probe import profiles as _pf

    r = csp.check(allow_remote=True, remote_ref="no-such-ref-xyz-9999",
                  cfg={"claude-header-defaults":
                       {"os": _pf._CC_OS_DEFAULT + "-NEW"}})
    assert r["checked"] is True, r
    assert r.get("partial") is True, "退到 config 路径必须标 partial"
    assert r.get("remote_failed"), "远程失败的原因必须带出去，不能静默"
    assert r["uncovered"], "必须写明哪些没覆盖到"

    # 连 config 也没有时，checked 必须是 False
    r2 = csp.check(allow_remote=True, remote_ref="no-such-ref-xyz-9999",
                   cfg=None)
    assert r2["checked"] is False, r2
    assert "远程拉取失败" in r2["why"], r2["why"]

    print("[OK] Remote degrade: 拉取失败时降级到 config 路径并带出原因")


def test_stale_binary_detection():
    """源码 commit 与运行中 CPA 的 commit 不一致时要警告。"""
    from cpa_probe import cpa_source_probe as csp

    # 两侧都有且不同 → 出警告
    d = csp._stale_drift("aaaaaaaaaaaa", "bbbbbbbbbbbb")
    assert d is not None and d.severity == "warn", d

    # 前缀相同（短 sha vs 长 sha）→ 不警告
    assert csp._stale_drift("abc1234def56", "abc1234") is None
    assert csp._stale_drift("abc1234", "abc1234def56") is None

    # 任一缺失 → 不判（不能因为拿不到就报不一致）
    assert csp._stale_drift("", "abc1234") is None
    assert csp._stale_drift("abc1234", "") is None

    print("[OK] Stale binary: 版本不一致告警，缺失一侧时不误判")


def test_headers_override_reaches_yaml():
    """overrides.headers 必须一路走到写出的 YAML 行。

    后端早就认这个键，但前端一直没有入口 —— 现在加了编辑器，这条链得有测试
    守着：任何一环把它丢掉（build_plan 不接、render_entry 不写），
    表现都是「界面上改了，写进去的还是旧的」，而那个很难当场发现。
    """
    from cpa_probe.plan import SectionPlan, ImportPlan
    from cpa_probe.writeback import render_entry

    sp = SectionPlan(
        section="claude-api-key",
        base_url="h.example.com", api_key="sk-x",
        models=["claude-opus-5"], priority=500,
        headers={"x-my-custom": "v1", "anthropic-beta": "only-this"},
    )
    lines = render_entry(sp, "  ", "    ", "2026-09-01")
    text = "\n".join(lines)

    assert "headers:" in text, text
    assert 'x-my-custom: "v1"' in text, text
    assert 'anthropic-beta: "only-this"' in text, text

    # 空 headers 不该写出一个空的 headers: 键 —— 那是合法 YAML 但语义是
    # 「显式给了空映射」，与「没有这个字段」不同
    sp2 = SectionPlan(section="claude-api-key", base_url="h.example.com",
                      api_key="sk-x", models=["m"], priority=1, headers={})
    assert "headers:" not in "\n".join(render_entry(sp2, "  ", "    ", "x"))

    print("[OK] Headers override: 覆盖值走到 YAML，空 headers 不写空键")


def test_rebuild_preserves_everything_else():
    """全量重建**只替换四段**，原文件其余内容逐字保留。

    2026-09-01 审计发现三个数据销毁缺陷，共同特征是 validate() 全部报成功：
      · 排在第一个段之后的全局键消失（api-keys 是客户端认证凭据，
        丢了所有客户端立刻断连；remote-management 含管理密钥）
      · 某段没有可写方案时整段消失，哪怕原文件里有条目
      · 段头正则写的是不存在的 openai-api-key，真正的 openai-compatibility
        匹配不到 —— 被当全局配置复制一遍后再生成一次，产出两个同名顶层键
    """
    import re
    import yaml
    from cpa_probe.writeback import rebuild_config_full, validate
    from cpa_probe.plan import SectionPlan, ImportPlan

    # compat 段故意排在最前 —— 那是旧正则匹配不到的位置
    orig = '''host: ""
port: 8317

openai-compatibility:
  - name: "oldprov"
    base-url: "https://o.example.com/v1"
    api-key-entries:
      - api-key: "k-old"
    models:
      - name: "m"
        alias: "m"

gemini-api-key:
  - api-key: "g1"
    base-url: "g.example.com"
    weight: 0

claude-api-key:
  - api-key: "c1"
    base-url: "c.example.com"
    priority: 300

api-keys:
  - "client-key-1"

remote-management:
  secret-key: "bcrypt-hash-here"

quota-exceeded:
  switch-project: true
'''
    cfg = yaml.safe_load(orig)
    # 只给 gemini 一个可写方案：claude 与 compat 都没有
    sp = SectionPlan(section="gemini-api-key", base_url="g.example.com",
                     api_key="g1", models=["gemini-2.5-flash"], priority=150)
    p = ImportPlan(host="g.example.com", masked_key="g...1")
    p.sections["gemini-api-key"] = sp

    new, warns = rebuild_config_full(
        cfg, {("g.example.com", "g1"): p}, orig.splitlines(keepends=True))
    got = yaml.safe_load(new)

    # ① 四段之外的键一个都不能少
    for k in ("host", "port", "api-keys", "remote-management", "quota-exceeded"):
        assert k in got, f"{k} 丢了 —— 那是第一版销毁全局配置的缺陷"
    assert got["api-keys"] == ["client-key-1"], got.get("api-keys")
    assert got["remote-management"]["secret-key"] == "bcrypt-hash-here"

    # ② 没有可写方案的段保留原条目，不是删掉
    assert len(got.get("claude-api-key") or []) == 1, "claude 段原条目被删了"
    assert len(got.get("openai-compatibility") or []) == 1, "compat 段原条目被删了"

    # ③ 顶层键不能重复。yaml.safe_load 静默取最后一个，所以只能扫文本
    for key in ("openai-compatibility", "gemini-api-key"):
        n = len(re.findall(rf"^{re.escape(key)}\s*:", new, re.M))
        assert n == 1, f"{key} 出现 {n} 次 —— 重复顶层键会让前一份静默消失"

    # ④ 有方案的段真的更新了
    prios = [e.get("priority") for e in (got.get("gemini-api-key") or [])]
    assert 150 in prios, f"gemini 的新 priority 没写进去：{prios}"

    ok, msg = validate(new)
    assert ok, msg
    print(f"[OK] Rebuild preserves: 全局键与无方案段全部保留、无重复顶层键"
          f"（{len(warns)} 条警告）")


def test_rebuild_keeps_unknown_fields():
    """render_entry 不认识的字段必须原文搬回。

    2026-09-02 拿生产 config.yaml 逐字段对账发现的数据销毁缺陷：
    render_entry 是白名单式渲染（只写它知道的 10 个字段），而全量重探用它
    **整段重写**。生产配置 121 个条目里 106 条带白名单外的字段，重写后全部
    静默消失 —— YAML 合法、validate 报成功，只是行为变了：

        request-scoped-errors  116 条  冷却规则，丢了坏站不再被剔除
        excluded-models         39 条  `["*"]` = 只用显式列的模型
        websockets               2 条  codex 的 WebSocket 开关
        fingerprint-profile      1 条  让 CPA 自己补设备指纹
        disabled                 1 条  手工停用的 provider 会复活
    """
    import yaml
    from cpa_probe.writeback import (carry_key, extract_carry_lines,
                                     rebuild_config_full, validate)
    from cpa_probe.plan import SectionPlan, ImportPlan

    orig = """host: "127.0.0.1"
api-keys:
  - "sk-client"

claude-api-key:
  # 这站限过模型
  - api-key: "sk-A"
    base-url: "https://a.example.com" # 注意不带 /v1
    priority: 300
    request-scoped-errors:
      - status: 403
        match:
          - "has been banned"
        action: continue-and-cooldown
    excluded-models: ["*"]
    fingerprint-profile: "claude-code-cli"
    models:
      - name: "claude-opus-5"
        alias: ""
  - api-key: "sk-B"
    base-url: "https://a.example.com"
    priority: 200
    models:
      - name: "claude-opus-5"
        alias: ""

codex-api-key:
  - api-key: "cdx-A"
    base-url: "https://a.example.com/v1"
    priority: 100
    websockets: true
    models:
      - name: "gpt-5.6-sol"
        alias: ""
"""
    cfg = yaml.safe_load(orig)
    lines = orig.splitlines(True)

    # ① 提取：行尾注释不能挡住 base-url 解析（`# 注意不带 /v1` 曾让 28 条漏抓）
    carry = extract_carry_lines(lines)
    ca = carry["claude-api-key"]
    got_a = ca.get(carry_key("a.example.com", "sk-A")) or []
    txt_a = "".join(got_a)
    for f in ("request-scoped-errors", "excluded-models", "fingerprint-profile"):
        assert f in txt_a, f"sk-A 没搬到 {f}：{txt_a[:200]}"
    # 嵌套结构要整块搬（match 下面的列表项）
    assert "has been banned" in txt_a, "嵌套列表项没搬全"

    # ② 同 host 不同 Key 不能互相染色。sk-B 原本没有这些字段，
    #    退到兜底键会把 sk-A 的字段抄给它。
    got_b = ca.get(carry_key("a.example.com", "sk-B"))
    assert got_b == [], f"sk-B 应当是空的 carry，实得 {got_b}"

    # ③ codex 段的 websockets 在 headers 之后 —— 缩进状态机要能出得来
    cc = carry["codex-api-key"]
    txt_c = "".join(cc.get(carry_key("a.example.com", "cdx-A")) or [])
    assert "websockets" in txt_c, f"codex 没搬到 websockets：{txt_c!r}"

    # ④ 整链：全量重建后字段计数必须与原文一致
    plans = {}
    for sec, ak, bu, pri in (("claude-api-key", "sk-A", "https://a.example.com", 290),
                             ("claude-api-key", "sk-B", "https://a.example.com", 190),
                             ("codex-api-key", "cdx-A", "https://a.example.com/v1", 90)):
        pl = ImportPlan(host="a.example.com", masked_key=ak)
        pl.sections[sec] = SectionPlan(
            section=sec, base_url=bu, api_key=ak,
            models=["claude-opus-5" if "claude" in sec else "gpt-5.6-sol"],
            priority=pri)
        plans[(bu, ak)] = pl

    new, warns = rebuild_config_full(cfg, plans, lines)
    ok, msg = validate(new)
    assert ok, f"重建结果非法：{msg}"
    n2 = yaml.safe_load(new)

    def count(c, f):
        return sum(1 for s in ("claude-api-key", "codex-api-key")
                   for e in (c.get(s) or []) if isinstance(e, dict) and f in e)

    for f in ("request-scoped-errors", "excluded-models",
              "fingerprint-profile", "websockets"):
        a, b = count(cfg, f), count(n2, f)
        assert a == b, f"{f}：原 {a} 条 → 新 {b} 条"

    # ⑤ 值也要一致，不只是键在
    a_new = next(e for e in n2["claude-api-key"] if e["api-key"] == "sk-A")
    a_old = next(e for e in cfg["claude-api-key"] if e["api-key"] == "sk-A")
    assert a_new["request-scoped-errors"] == a_old["request-scoped-errors"]
    assert a_new["excluded-models"] == a_old["excluded-models"]
    # sk-B 不该被染上 sk-A 的字段
    b_new = next(e for e in n2["claude-api-key"] if e["api-key"] == "sk-B")
    assert "fingerprint-profile" not in b_new, "同 host 另一个 Key 被染色了"
    # priority 是这次要改的，确实改了
    assert a_new["priority"] == 290

    # ⑥ 全局键仍在
    assert "api-keys" in n2 and n2["api-keys"] == ["sk-client"]

    print("[OK] Unknown fields carried: request-scoped-errors / excluded-models"
          " / websockets / fingerprint-profile 逐字保真，同站不同 Key 不染色")


def test_rebuild_keeps_proxy():
    """proxy-url 必须搬回。

    2026-09-02 对账发现：`proxy_url` 只在探测**当场判定需要代理**时才有值，
    重探时那个站可能这次直连就通 —— 方案里 proxy_url 为空，整段重写把原有
    的 24 条 mihomo 代理全抹掉。后果不可见：YAML 合法，但那些必须走代理的站
    下次直连拿 403，配置里已无任何痕迹。
    """
    import yaml
    from cpa_probe.batch import existing_proxies

    cfg = yaml.safe_load("""
gemini-api-key:
  - api-key: "g1"
    base-url: "g.example.com"
    proxy-url: "http://mihomo:7890"
  - api-key: "g2"
    base-url: "g.example.com"
  - api-key: "g3"
    base-url: "g.example.com"
    proxy-url: ""

openai-compatibility:
  - name: "p"
    base-url: "https://o.example.com/v1"
    api-key-entries:
      - api-key: "k1"
        proxy-url: "http://mihomo:7890"
      - api-key: "k2"
    models:
      - name: "m"
        alias: ""
""")
    P = existing_proxies(cfg)
    assert P.get(("g.example.com", "g1")) == "http://mihomo:7890", P
    # 没写的与空串都不该进表 —— 空串与「没这个键」语义相同（都不走代理），
    # 收进来会让重建凭空写出 `proxy-url: ""`
    assert ("g.example.com", "g2") not in P, P
    assert ("g.example.com", "g3") not in P, P
    # compat 段的 proxy-url 在 api-key-entries 上，不在 provider 级
    assert P.get(("o.example.com", "k1")) == "http://mihomo:7890", P
    assert ("o.example.com", "k2") not in P, P

    print("[OK] Proxy preserved: 有值的搬回，空串与未写的不凭空添加")


def test_rebuild_skips_dedup():
    """全量重探不判重 —— 否则「全勾选」只能勾中每个 host 的第一个 Key。

    2026-09-02 现场：79 个凭据全量重探，「全勾选」显示「已勾选 26 项」。
    根因是 build_plan 的去重判定在重探模式下语义反了：

      · 新增导入：输入是新 Key，seen 代表「cfg 里已有的 + 本批已处理的」，
        撞上就是真重复，该挡。
      · 全量重探：输入**就是** cfg 里的既有条目，而 seen 是从同一份 cfg
        读出来的 —— 每一条都必然撞上。

    实测数字：14 个 host / 79 个凭据，只有每个 host 的第一个 Key 逃过判定
    （它的 prefix/headers 与探测建议不同、五元组恰好没撞上，那是偶然不是
    设计）。14 × 4 段 = 56 个段进了方案，其余 260 个段 writable=False，
    勾选框点不动。
    """
    import yaml
    import cpa_probe as cp
    from cpa_probe.pipeline import CandidateResult, SectionVerdict

    # 同一个站三个 Key —— 现场最常见的形态（gorouter 15 个、tabitoken 14 个）
    cfg = yaml.safe_load("""
claude-api-key:
  - api-key: "sk-A"
    base-url: "https://a.example.com"
    prefix: "ANT"
    priority: 300
    models:
      - name: "claude-opus-5"
        alias: ""
  - api-key: "sk-B"
    base-url: "https://a.example.com"
    prefix: "ANT"
    priority: 200
    models:
      - name: "claude-opus-5"
        alias: ""
  - api-key: "sk-C"
    base-url: "https://a.example.com"
    prefix: "ANT"
    priority: 100
    models:
      - name: "claude-opus-5"
        alias: ""
""")

    def mk(row):
        res = CandidateResult(row=row)
        res.sections["claude-api-key"] = SectionVerdict(
            section="claude-api-key", usable=True,
            base_url=row.base_for("claude-api-key"),
            models=["claude-opus-5"], category="可用")
        return res

    def run(rebuild):
        bands = {}
        seen = cp.existing_fingerprints(cfg)
        pairs = cp.existing_pairs(cfg)
        out = []
        for ak in ("sk-A", "sk-B", "sk-C"):
            row = cp.parse_lines(f"https://a.example.com,{ak}").valid[0]
            p = cp.build_plan(row, mk(row), cfg, bands=bands, seen=seen,
                              seen_pairs=pairs, probation=True, rebuild=rebuild)
            sp = p.sections.get("claude-api-key")
            out.append(sp)
        return out

    # ① 原行为（新增导入语义）：三个 Key 全部判重 —— 它们本来就在 cfg 里
    old = run(rebuild=False)
    assert all(sp is not None for sp in old)
    assert all(sp.duplicate for sp in old), (
        f"新增导入模式下这三个 Key 都该判重，实得 "
        f"{[sp.duplicate for sp in old]}")
    assert not any(sp.writable for sp in old), "判重的段不该 writable"

    # ② 重探模式：一个都不判重，三个都能勾
    new = run(rebuild=True)
    assert not any(sp.duplicate for sp in new), (
        f"重探不该判重，实得 {[sp.duplicate for sp in new]}")
    assert all(sp.writable for sp in new), (
        f"重探的段都该可勾选，实得 {[sp.writable for sp in new]}")
    # 探测通过的段仍然默认勾选
    assert all(sp.recommended for sp in new), (
        f"实测通过的段该默认勾，实得 {[sp.recommended for sp in new]}")
    # 每个 Key 的方案指向自己的 api_key，没有串
    assert [sp.api_key for sp in new] == ["sk-A", "sk-B", "sk-C"]

    # ③ 重探模式下 duplicate_note 也该是空的 —— 界面上不该显示「已存在，跳过」
    assert all(not sp.duplicate_note for sp in new)

    print("[OK] Rebuild skips dedup: 同站 3 个 Key 全部可勾选"
          "（原行为下 3 个全判重、一个都勾不上）")


def test_rebuild_entry_conservation():
    """条目守恒 + 未勾选不删除。两条都是 2026-09-02 生产事故的回归。

    事故一：条目从 121 变 246
      全量重探为每个凭据的**四段**都生成方案，整段重写时全写进去。而真实
      情况是每个凭据只配了自己那几段（79 个凭据里跨四段的只有 9 个）。
      修法是 owned_sections + only_owned。

    事故二：只勾推荐项 → 未勾的原条目被删
      整段重写只写进方案的条目，其余消失。而「没进方案」有三种无害原因：
      用户没勾、段判不可写、探测抛异常。修法是 keep_unplanned。
    """
    import yaml
    from cpa_probe.writeback import (owned_sections, rebuild_config_full,
                                     validate)
    from cpa_probe.plan import SectionPlan, ImportPlan

    # 两个站：A 配了 claude+codex 两段，B 只配了 claude
    orig = """host: "127.0.0.1"

codex-api-key:
  - api-key: "kA"
    base-url: "https://a.example.com/v1"
    priority: 300
    models:
      - name: "gpt-5.6-sol"
        alias: ""

claude-api-key:
  - api-key: "kA"
    base-url: "https://a.example.com"
    priority: 500
    models:
      - name: "claude-opus-5"
        alias: ""
  - api-key: "kB"
    base-url: "https://b.example.com"
    priority: 400
    models:
      - name: "claude-opus-5"
        alias: ""
"""
    cfg = yaml.safe_load(orig)
    lines = orig.splitlines(keepends=True)

    own = owned_sections(cfg)
    assert own[("a.example.com", "kA")] == {"codex-api-key", "claude-api-key"}
    assert own[("b.example.com", "kB")] == {"claude-api-key"}

    def plan_for(host, key, secs, prio):
        p = ImportPlan(host=host, masked_key=key, line_no=1)
        for sec in secs:
            bu = f"https://{host}" + ("/v1" if "codex" in sec else "")
            p.sections[sec] = SectionPlan(
                section=sec, base_url=bu, api_key=key,
                models=["gpt-5.6-sol" if "codex" in sec else "claude-opus-5"],
                priority=prio)
        return p

    # ① 探测给每个凭据都出了四段方案 —— only_owned 该挡掉没配过的
    ALL = ("gemini-api-key", "codex-api-key", "claude-api-key",
           "openai-compatibility")
    plans = {
        ("https://a.example.com", "kA"): plan_for("a.example.com", "kA", ALL, 900),
        ("https://b.example.com", "kB"): plan_for("b.example.com", "kB", ALL, 800),
    }
    new, warns = rebuild_config_full(cfg, plans, lines)
    ok, msg = validate(new)
    assert ok, msg
    n2 = yaml.safe_load(new)

    # 条目守恒：原来 3 条，现在还是 3 条
    tot = sum(len(n2.get(s) or []) for s in ALL)
    assert tot == 3, f"条目数应守恒为 3，实得 {tot}（各段 " +         str({s: len(n2.get(s) or []) for s in ALL}) + "）"
    # gemini 与 compat 原本没有 —— 不该凭空多出来
    assert not n2.get("gemini-api-key"), "凭空写了 gemini 段"
    assert not n2.get("openai-compatibility"), "凭空写了 compat 段"
    assert any("原本不在 config.yaml 里" in w for w in warns),         f"跳过未拥有的段时必须给警告，实得 {warns}"
    # priority 确实更新了
    assert n2["claude-api-key"][0]["priority"] == 900

    # ② 只有 A 进方案（模拟「只勾推荐项」）—— B 的原条目必须保留
    plans2 = {("https://a.example.com", "kA"):
              plan_for("a.example.com", "kA", ("claude-api-key",), 950)}
    new2, warns2 = rebuild_config_full(cfg, plans2, lines)
    ok2, msg2 = validate(new2)
    assert ok2, msg2
    n3 = yaml.safe_load(new2)
    keys = {e.get("api-key") for e in n3["claude-api-key"]}
    assert keys == {"kA", "kB"}, f"未勾选的 kB 被删了，实得 {keys}"
    # A 的 codex 段没进方案，那一段也要原样保留
    assert len(n3.get("codex-api-key") or []) == 1, "A 的 codex 原条目被删了"
    assert any("已原样保留" in w for w in warns2), warns2

    print("[OK] Entry conservation: 121 型条目守恒、未勾选的原条目不删除")


def test_rebuild_keeps_weight():
    """weight: 0 必须搬回去 —— 丢了等于让手工封禁的站复活。

    `weight: 0` 是用户显式表达「把这个站逐出调度池」的唯一手段（plan.py 里
    把它当强信号读），而 CPA 缺这个字段时默认 1。
    """
    import yaml
    from cpa_probe.batch import existing_weights
    from cpa_probe.writeback import render_entry
    from cpa_probe.plan import SectionPlan

    cfg = yaml.safe_load('''
gemini-api-key:
  - api-key: "g1"
    base-url: "g.example.com"
    weight: 0
  - api-key: "g2"
    base-url: "g.example.com"

openai-compatibility:
  - name: "p"
    base-url: "https://o.example.com/v1"
    api-key-entries:
      - api-key: "k1"
        weight: 0
      - api-key: "k2"
    models:
      - name: "m"
        alias: "m"
''')
    w = existing_weights(cfg)
    # 只收显式写了的：g2 与 k2 没写，不该出现在表里
    assert w.get(("g.example.com", "g1")) == 0, w
    assert ("g.example.com", "g2") not in w, w
    assert w.get(("o.example.com", "k1")) == 0, w
    assert ("o.example.com", "k2") not in w, w

    # 渲染时写出来
    sp = SectionPlan(section="gemini-api-key", base_url="g.example.com",
                     api_key="g1", models=["m"], priority=500, weight=0)
    text = "\n".join(render_entry(sp, "  ", "    ", "2026-09-01"))
    assert re.search(r"^\s+weight:\s*0", text, re.M), text

    # weight=None（原本没写）时不写这个字段
    sp2 = SectionPlan(section="gemini-api-key", base_url="g.example.com",
                      api_key="g2", models=["m"], priority=500)
    assert "weight:" not in "\n".join(render_entry(sp2, "  ", "    ", "x"))

    print("[OK] Weight preserved: weight:0 搬回，未写的不凭空添加")


def test_assign_priorities_site_level():
    """批量定档：站与站不同值、同站所有 Key 同值，且不越过安全上限。

    2026-09-02 现场（用户截图）：写回后 claude 段 74 个条目全是 175、
    gemini 段 76 个全是 225 —— `suggest_priority` 每次只看「当前 config 有
    哪些空档」，79 个凭据串行调用它、每个都拿到同一个答案。priority 的唯一
    作用就是区分先后，全同值等于这个字段没写。

    第一版修法（从空档由高到低铺值）绕开了 `suggest_priority` 的三条硬约束，
    拿生产 config.yaml 实测：codex 与 compat 两段 14/14 站抢走现有顶层，
    `recommended` 整段翻假，默认写入集合从 24 段塌到 12 段。所以这里同时守
    「各站分开」与「不越过 cap」两件事 —— 只守前者会放过那个回归。
    """
    import yaml
    from cpa_probe.plan import (ImportPlan, SectionPlan, assign_priorities,
                                build_band, suggest_priority)

    # 两个现有站：顶层 900 承载 m1，低层 100。新站的安全上限必须 <= 900。
    cfg = yaml.safe_load('''
claude-api-key:
  - api-key: "old1"
    base-url: "https://top.example"
    priority: 900
    models:
      - name: "m1"
        alias: ""
  - api-key: "old2"
    base-url: "https://low.example"
    priority: 100
    models:
      - name: "m1"
        alias: ""
''')

    def mkplans(nhost, nkey):
        out = []
        for hi in range(nhost):
            host = f"new{hi}.example"
            for k in range(nkey):
                pl = ImportPlan(host=host, masked_key="k", line_no=hi * 10 + k)
                pl.sections["claude-api-key"] = SectionPlan(
                    section="claude-api-key", base_url=f"https://{host}",
                    api_key=f"sk-{hi}-{k}", models=["m1"],
                    score=100 - hi * 5, model_source="probed")
                out.append(pl)
            # 同站不同 Key 声明的模型可以不同 —— 上限按并集算
        return out

    band = build_band(cfg, "claude-api-key")
    cap, _ = suggest_priority(band, 100, models=["m1"], probation=True)
    assert cap <= 900, f"上限本身就该避让顶层 900，实得 {cap}"

    plans = mkplans(4, 3)
    warns = assign_priorities(plans, cfg, probation=True)
    by_host: dict[str, set[int]] = {}
    for pl in plans:
        sp = pl.sections["claude-api-key"]
        by_host.setdefault(pl.host, set()).add(sp.priority)

    # ① 同站所有 Key 同值 —— 给不同值会把「多 Key 轮询」变成「主备切换」
    for host, vals in by_host.items():
        assert len(vals) == 1, f"{host} 的 3 个 Key 拿到 {vals}，同站必须同值"

    # ② 站与站互不相同 —— 这就是这轮要修的那个 bug
    flat = [next(iter(v)) for v in by_host.values()]
    assert len(set(flat)) == len(flat), f"站间出现重复档位：{flat}"

    # ③ 不越过 suggest_priority 划的上限（第一版修法在这里翻车）
    assert max(flat) <= cap, f"有站越过上限 {cap}：{flat}"

    # ④ 不抢现有顶层，因此 recommended 不该被劫持翻假
    for pl in plans:
        sp = pl.sections["claude-api-key"]
        assert not sp.hijacked, (
            f"priority {sp.priority} 抢走了顶层：{[i.model for i in sp.hijacked]}")
        assert sp.recommended, (
            f"探测通过且未劫持的段该默认勾选，warnings={sp.warnings}")

    # ⑤ 不与现有档位相撞 —— 撞上等于与那个站同层轮询，不是「排在它前面」
    assert not (set(flat) & set(band.tiers)), (
        f"分配值撞上现有档位 {sorted(set(flat) & set(band.tiers))}")

    # ⑥ 排序按探测质量降序：score 高的站档位更高
    ranked = sorted(by_host.items(), key=lambda kv: kv[0])   # new0 分最高
    vals_in_order = [next(iter(v)) for _h, v in ranked]
    assert vals_in_order == sorted(vals_in_order, reverse=True), vals_in_order

    # ⑦ 幂等/可复核：同一批输入跑两次给出同样的值，否则 diff 无法复核
    again = mkplans(4, 3)
    assign_priorities(again, cfg, probation=True)
    assert ([p.sections["claude-api-key"].priority for p in plans]
            == [p.sections["claude-api-key"].priority for p in again])

    # ⑧ 理由要写清「为什么不是 cap」，否则用户只看到一个数字
    lows = [p for p in plans if p.sections["claude-api-key"].priority < cap]
    assert lows, "构造有误：应当至少有一个站被前一站挤低"
    assert "算法上限" in lows[0].sections["claude-api-key"].priority_reason

    # ⑨ 旧值的警告必须清掉 —— 留着会指向一个已经不存在的 priority
    stale = [w for pl in plans for w in pl.sections["claude-api-key"].warnings
             if "priority" in w
             and f"priority {pl.sections['claude-api-key'].priority}" not in w]
    assert not stale, f"警告里残留旧 priority：{stale}"

    # ⑩ 不可写的段不参与定档 —— 它们不会落盘，改它的 priority 只会误导界面
    dup = ImportPlan(host="dup.example", masked_key="k", line_no=999)
    dup.sections["claude-api-key"] = SectionPlan(
        section="claude-api-key", base_url="https://dup.example",
        api_key="sk-dup", models=["m1"], score=100, duplicate=True)
    before = dup.sections["claude-api-key"].priority
    assign_priorities([dup], cfg, probation=True)
    assert dup.sections["claude-api-key"].priority == before

    # ⑪ raw 必须影响上限 —— 注释里的「实测不可用」结论决定「挡住下层算不算
    #    代价」。不传 raw 时那批站被当活站保护，可用新站被压到它们之下。
    #    生产 config.yaml 实测差 325 点（claude 段 175 vs 500）。
    cfg2 = yaml.safe_load('''
claude-api-key:
  - api-key: "t1"
    base-url: "https://alive.example"
    priority: 900
    models:
      - name: "m2"
        alias: ""
  - api-key: "t2"
    base-url: "https://broken.example"
    priority: 500
    models:
      - name: "m2"
        alias: ""
  - api-key: "t3"
    base-url: "https://floor.example"
    priority: 100
    models:
      - name: "m2"
        alias: ""
''')
    raw2 = ('claude-api-key:\n'
            '  # broken.example：实测 503 站点级不可用\n'
            '  - api-key: "t1"\n')

    def one(rawtext):
        pl = ImportPlan(host="n.example", masked_key="k", line_no=1)
        pl.sections["claude-api-key"] = SectionPlan(
            section="claude-api-key", base_url="https://n.example",
            api_key="sk-n", models=["m2"], score=100, model_source="probed")
        assign_priorities([pl], cfg2, probation=True, raw=rawtext)
        return pl.sections["claude-api-key"].priority

    without, with_raw = one(""), one(raw2)
    assert with_raw > without, (
        f"传 raw 后档位该更高（注释判死的站不值得保护），"
        f"实得 不传={without} 传={with_raw}")

    # ⑫ 空档太窄时整批下移到更宽的空档（用户 2026-09-02 明确要的取舍）。
    #    高位档位谱密集时（相邻只差 5），一批站挤进去成了 999/998/997… ——
    #    正确但改一个值就撞邻居。代价约束不松：只换「挡住的在用站数不多于
    #    原档」的更宽空档，换不到就保持原样。
    #
    #    构造复刻生产 claude 段的形态：高位三档相邻只差 5，低位留一个大空档。
    #    下面两站 weight: 0（已被逐出调度池），所以挡住它们零代价 ——
    #    这让 suggest_priority 选中最高那个**窄**空档，正是要下移的情形。
    cfg3 = yaml.safe_load('''
claude-api-key:
  - api-key: "n1"
    base-url: "https://a.example"
    priority: 1000
    models: [{name: "m3", alias: ""}]
  - api-key: "n2"
    base-url: "https://b.example"
    priority: 995
    weight: 0
    models: [{name: "m3", alias: ""}]
  - api-key: "n3"
    base-url: "https://c.example"
    priority: 400
    weight: 0
    models: [{name: "m3", alias: ""}]
''')

    def batch(n):
        out = []
        for hi in range(n):
            pl = ImportPlan(host=f"w{hi}.example", masked_key="k", line_no=hi)
            pl.sections["claude-api-key"] = SectionPlan(
                section="claude-api-key", base_url=f"https://w{hi}.example",
                api_key=f"sk-w{hi}", models=["m3"], score=100 - hi,
                model_source="probed")
            out.append(pl)
        return out

    band3 = build_band(cfg3, "claude-api-key")
    cap3, _ = suggest_priority(band3, 100, models=["m3"], probation=True)
    narrow = [(lo, hi) for lo, hi in band3.gaps() if lo < cap3 < hi]
    assert narrow, f"构造有误：cap {cap3} 不在任何空档里"
    room3 = narrow[0][1] - narrow[0][0] - 1

    # 一个站：不该触发下移（放得下）
    few = batch(1)
    w_few = assign_priorities(few, cfg3, probation=True)
    assert few[0].sections["claude-api-key"].priority == cap3
    assert not any("整批下移" in w for w in w_few), w_few

    # 站数超过空档容量的两倍：触发下移，且必须落进更宽的空档
    many = batch(room3 * 2 + 4)
    w_many = assign_priorities(many, cfg3, probation=True)
    moved = [w for w in w_many if "整批下移" in w]
    assert moved, f"{len(many)} 个站挤进只容 {room3} 个整数的空档，该整批下移：{w_many}"
    vals3 = [p.sections["claude-api-key"].priority for p in many]
    assert max(vals3) < narrow[0][0], (
        f"下移后最高档 {max(vals3)} 该落到原空档下界 {narrow[0][0]} 之下")
    assert len(set(vals3)) == len(vals3), f"下移后仍要各站不同：{vals3}"
    assert all(not p.sections["claude-api-key"].hijacked for p in many)

    print(f"[OK] Batch tiering: 4 站 × 3 Key → 站内同值、站间 {len(set(flat))} "
          f"个不同档位，全部 <= 上限 {cap}，零劫持；raw 生效 "
          f"{without} → {with_raw}；{len(many)} 站时整批下移到 "
          f"{max(vals3)}..{min(vals3)}（warns={len(warns)}）")


def test_batch_key_includes_api_key():
    """同一个站的多个 Key 不能互相覆盖。

    结果键原来只用 row.bare（不含 api_key），于是 foxtrot 那种 15 个 Key 的站
    只剩 1 条结果，而 _stats 仍报 15 个已完成。
    """
    class FakeProber:
        def __init__(self, **kw):
            pass

        def probe(self, row):
            class R:
                def __init__(self, k):
                    self.usable_sections = ["gemini"]
                    self.key = k
            return R(row.api_key)

    class FakeRow:
        def __init__(self, url, key):
            self.bare = url
            self.api_key = key

    # 同一个站，5 个不同 Key
    rows = [FakeRow("https://same.example.com", f"sk-{i}") for i in range(5)]
    bp = BatchProber(FakeProber(), max_workers=2)
    res = bp.probe_batch(rows)

    assert len(res) == 5, f"5 个 Key 应有 5 条结果，实际 {len(res)}"
    keys = {r.key for r in res.values()}
    assert keys == {f"sk-{i}" for i in range(5)}, keys
    print(f"[OK] Batch key: 同站 5 个 Key 得 {len(res)} 条结果，无覆盖")


def test_batch_records_errors():
    """单站抛异常时要记下是哪个站、什么原因，不能只把计数加一。"""
    class FakeProber:
        def __init__(self, **kw):
            self.n = 0

        def probe(self, row):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("boom")
            class R:
                usable_sections = ["gemini"]
            return R()

    class FakeRow:
        def __init__(self, url, key="sk-x"):
            self.bare = url
            self.api_key = key

    rows = [FakeRow(f"https://s{i}.example.com") for i in range(3)]
    bp = BatchProber(FakeProber(), max_workers=1)
    res = bp.probe_batch(rows)

    assert len(res) == 2, len(res)
    assert bp._stats["failure"] == 1, bp._stats
    assert len(bp.errors) == 1, bp.errors
    host, why = bp.errors[0]
    assert "s1.example.com" in host, host
    assert "RuntimeError" in why and "boom" in why, why
    print(f"[OK] Batch errors: 异常站记为 {host} / {why}")


def test_cgroup_bad_values():
    """cgroup 里的异常值不能被当成真实限额。

    memory.max = "-1" 是某些运行时表达「无限制」的方式。把它当真会算出
    memory_mb = -1，而 detect() 的 `mem > 0` 判断会让 reason 里不带内存项 ——
    显示的依据与 memory_source 标的来源自相矛盾。
    """
    from cpa_probe import resources as R

    orig = R._read
    try:
        for val in ("-1", "0", "9223372036854771712"):
            R._read = lambda path, v=val: v if path.endswith("memory.max") else ""
            mb, src = R.detect_memory_mb()
            assert mb == 0, f"memory.max={val} 得到 mb={mb}，应降级为 0"
            assert src == "读不到", f"memory.max={val} 的 source 是 {src}"
    finally:
        R._read = orig

    # 正常值仍然认
    try:
        R._read = lambda path: str(2 * 1024 ** 3) if path.endswith("memory.max") else ""
        mb, src = R.detect_memory_mb()
        assert mb == 2048, mb
        assert "memory.max" in src, src
    finally:
        R._read = orig

    print("[OK] cgroup bad values: -1 / 0 / 超大哨兵都降级，正常值仍认")


def test_full_redetect_without_new_rows():
    """不加新账号也能全量重探 —— 「只体检既有站」是独立需求。

    后端 _api_probe 一直支持（`not res.valid and not full_redetect` 才拒绝），
    但前端按钮的启用条件只看「解析出有效行」，于是这条路点不进去。
    这一项守后端契约；前端那侧靠 syncProbeBtn 里的 `hasRows || full`。
    """
    import io as _io
    import re as _re

    js = _io.open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8").read()

    # 按钮启用必须同时认「有行」与「勾了全量」
    m = _re.search(r"function syncProbeBtn\(\)\s*\{(.*?)\n\}", js, _re.S)
    assert m, "syncProbeBtn 不见了 —— 按钮启用逻辑被改回单一条件？"
    body = m.group(1)
    assert "full" in body and "hasRows" in body, body
    assert _re.search(r"!\(\s*hasRows\s*\|\|\s*full\s*\)", body), (
        "启用条件不是 hasRows || full —— 空输入时全量重探又点不进去了")

    # 不能有别处把它硬设回 disabled = valid.length === 0
    assert "disabled = d.valid.length === 0" not in js, (
        "还有地方按「有效行数」直接禁用按钮，会绕过 syncProbeBtn")

    # 后端：空 text + full_redetect 不该被 400 拒绝
    srv = _io.open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
    assert "if not res.valid and not full_redetect:" in srv, (
        "后端的拒绝条件变了 —— 空输入 + 全量重探必须放行")

    print("[OK] Full redetect w/o new rows: 前后端都允许空输入 + 全量重探")


def test_profile_matches_real_cpa_source():
    """如果本机有 CPA 源码，画像梯必须与它一致（无 warn 级漂移）。

    源码不在时跳过 —— 这一项是给开发机与 CI 的，不是运行前提。
    """
    from cpa_probe import cpa_source_probe as csp

    candidates = [
        os.path.expanduser("~/OneDrive/Desktop/CLIProxyAPI-main"),
        os.path.join(os.path.dirname(ROOT), "CLIProxyAPI-main"),
        os.path.join(os.path.dirname(ROOT), "CLIProxyAPI"),
    ]
    root = next((c for c in candidates if os.path.isdir(c)), "")
    if not root:
        print("[skip] Real CPA source not found")
        return

    ident, drifts = csp.report(root)
    if not ident.ok:
        print(f"[skip] 解析不了 {root}：{ident.errors}")
        return

    warns = [d for d in drifts if d.severity == "warn"]
    detail = "; ".join(f"{d.what}（{d.note}）" for d in warns)
    assert not warns, f"画像梯与 CPA 源码有 {len(warns)} 处漂移：{detail}"

    print(f"[OK] Real CPA source: {len(ident.claude_betas_unconditional)} 项"
          f"无条件 beta 全部对齐，无 warn 级漂移")


if __name__ == "__main__":
    # Windows 控制台默认 GBK，打不出 ✗。与 tests/run.py 同一套处理。
    for _st in (sys.stdout, sys.stderr):
        try:
            _st.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # 与其余套件同一套汇报约定：run.py 按「全部通过 · N 项」这行统计，
    # 失败行以 ✗ 开头。自己 print [OK] 不会被计入总数。
    CASES = [
        ("提取既有站", test_extract_existing_entries),
        ("ETA 与进度度量", test_job_eta),
        ("站级并发与进度回调", test_batch_prober_progress),
        ("统计分类", test_batch_prober_stats),
        ("单站异常隔离", test_batch_prober_exception_handling),
        ("全量重建保注释", test_rebuild_config_preserves_comments),
        ("priority 降序", test_rebuild_config_priority_order),
        ("段字段结构与 compat 归并", test_rebuild_config_section_structure),
        ("凭据去重", test_credential_dedup),
        ("探测文本非问候", test_probe_text_not_trivial),
        ("画像结论复用省请求", test_profile_verdict_reuse_saves_calls),
        ("画像漂移检测", test_profile_drift_detection),
        ("画像梯对齐真实 CPA 源码", test_profile_matches_real_cpa_source),
        ("远程模式降级", test_drift_remote_degrade),
        ("旧二进制检测", test_stale_binary_detection),
        ("headers 覆盖写进 YAML", test_headers_override_reaches_yaml),
        ("重建保留其余内容", test_rebuild_preserves_everything_else),
        ("未知字段搬运", test_rebuild_keeps_unknown_fields),
        ("proxy-url 搬运", test_rebuild_keeps_proxy),
        ("重探不判重", test_rebuild_skips_dedup),
        ("条目守恒与未勾不删", test_rebuild_entry_conservation),
        ("重建保留 weight", test_rebuild_keeps_weight),
        ("批量定档站级差异", test_assign_priorities_site_level),
        ("批量键含 api_key", test_batch_key_includes_api_key),
        ("批量记录异常站", test_batch_records_errors),
        ("cgroup 异常值降级", test_cgroup_bad_values),
        ("空输入也能全量重探", test_full_redetect_without_new_rows),
    ]

    ok = 0
    bad: list[str] = []
    for name, fn in CASES:
        try:
            fn()
            ok += 1
        except AssertionError as e:
            bad.append(f"{name}: {e}")
        except Exception as e:
            bad.append(f"{name}: {type(e).__name__}: {e}")

    print()
    if bad:
        for b in bad:
            print(f"  ✗  {b}")
        print(f"失败 {len(bad)} 项 · 通过 {ok} 项")
        sys.exit(1)
    print(f"全部通过 · {ok} 项")
