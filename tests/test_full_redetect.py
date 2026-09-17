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
from unittest import mock as _mock

# 钉住「当前市面最新」用：几处用例的断言落在具体型号上，而
# `model_catalog.remote_names()` 会去拉真实名录 —— 不钉的话既要外网，
# 断言又随名录漂移。用 `_patch.object(model_catalog, "remote_names", ...)`。
_patch = _mock.patch

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


def test_comment_index_boundaries():
    """注释索引的四条边界。每一条都是 2026-09-03 实测踩到的。

    这个函数是全量重建保注释的唯一入口，而它踩过的坑全是「键算错了」——
    键错了不会报错，只会让注释静默丢失或跟着错误的站被复制。
    """
    from cpa_probe.writeback import _extract_entry_comments, _comments_for
    from cpa_probe.plan import SectionPlan
    from cpa_probe.parse import host_of

    src = '''claude-api-key:
  # A 的结论：实测 200
  - api-key: "kA"
    base-url: "https://a.example.com" # 注意不带 /v1
    models:
      - name: "claude-opus-5"
        alias: ""
  # B 的结论：实测 503
  - api-key: "kB"
    base-url: "https://b.example.com"
    models:
      # 模型级注释：这一款静默换模
      - name: "claude-opus-5"
        alias: ""
  - api-key: "kC"
    base-url: "https://c.example.com"
    # 夹在条目中间的依据
    priority: 300
    models:
      - name: "claude-opus-5"
        alias: ""
other: 1
'''
    cm = _extract_entry_comments(src.splitlines(keepends=True))
    d = cm["claude-api-key"]

    # ① base-url 带行尾注释时，host 键必须算得出来。
    #    `.strip().strip("\"'")` 只剥前引号，剩下的字符串 host_of 解析不出 ——
    #    实测那份文件 45 种注释因此挂在永远查不到的垃圾键上。
    assert "a.example.com" in d, f"带行尾注释的 base-url 没算出 host 键：{sorted(d)}"
    assert any("A 的结论" in x for x in d["a.example.com"]), d["a.example.com"]

    # ② models: 底下的 `- name:` 是模型名，不是条目键。
    #    实测那份文件里 `claude-opus-5` 这个「键」被覆盖 57 次，每次丢一整块。
    assert "claude-opus-5" not in d, f"模型名被当成条目键：{sorted(d)}"

    # ③ models 块内的注释是模型级的，不该提到条目级 —— 否则它会跟着整个站走。
    assert not any("模型级注释" in x for x in d.get("b.example.com", [])), \
        d.get("b.example.com")

    # ④ 夹在条目中间的注释要挂到本条目上（后面没有 name/base-url 来认领）。
    #    这一支必须**只**在条目内部生效：放宽到「缩进不深于字段层」时，
    #    下一个条目的前置注释会被误判成上一个条目的中段注释，实测重建后
    #    多出 107 行重复。
    assert any("夹在条目中间" in x for x in d.get("c.example.com", [])), \
        d.get("c.example.com")
    # 而它不能溜到别的条目上
    for k in ("a.example.com", "b.example.com"):
        assert not any("夹在条目中间" in x for x in d.get(k, [])), \
            f"C 的中段注释挂到了 {k} 上"

    # ⑤ 每一块注释只挂给它自己的条目 —— B 的结论不能出现在 A 或 C 上。
    assert any("B 的结论" in x for x in d.get("b.example.com", []))
    for k in ("a.example.com", "c.example.com"):
        assert not any("B 的结论" in x for x in d.get(k, [])), f"串到 {k}"

    # ⑥ _comments_for 要**合并**两个候选键，不是先命中就返回 —— 段尾那块只挂
    #    在 host 键上（base-url 原文键早已用过）。同时同一份不能输出两次。
    sp = SectionPlan(section="claude-api-key",
                     base_url="https://c.example.com", api_key="kC",
                     models=["claude-opus-5"], priority=300)
    used: set = set()
    got = _comments_for(cm, "claude-api-key", sp, used, host_of)
    assert any("夹在条目中间" in x for x in got), got
    assert len(got) == len({x.strip() for x in got}), f"输出了重复行：{got}"
    # 同站第二个 Key 再查：已经挂过了，不能再挂一遍
    assert _comments_for(cm, "claude-api-key", sp, used, host_of) == []

    print("[OK] Comment index: 行尾注释、模型名、模型级注释、中段块归属、"
          "候选合并六条边界都对")


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


def test_three_paths_share_the_gates():
    """三条入口（网页重探 / 网页增量 / CLI）必须走同一批闸，且顺序一致。

    这类缺陷的形态是「一条路修了，另一条没修」，而它不会报错：同一份输入两条
    途径落盘结果不同，只有逐字对比才看得出来。实测踩过两次 ——
    `assign_priorities` 曾只在网页端调（CLI 漏掉，priority 因此不一致），
    `_clean_override_models` 曾只在重探路过滤（增量路直接 str(m) 塞进去）。

    顺序也是判据的一部分：
      模型清单覆盖 → mark_new_sections → assign_priorities → priority 覆盖
    第一步必须在第二步之前（那道闸按 model_source 判，手填要先落进方案），
    第二步必须在第三步之前（被拦下的段不落盘，让它占档位会把各站挤低），
    第四步必须在第三步之后（否则手工 priority 被定档冲掉）。
    """
    import ast as _ast
    import io as _io

    src = _io.open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
    WANT = ("_clean_override_models", "mark_new_sections",
            "assign_priorities")
    tree = _ast.parse(src)
    # `_api_plan` 在 2026-09-13 拆成两半：壳（校验 + 缓存 + 计时）与
    # `_plan_body`（真正的建方案逻辑）。拆的理由是缓存与耗时日志要包住整个
    # 建方案过程，又要保证任何出口都摘掉 `_plan_cache_key`。
    # 所以这里必须把两个函数体**合起来**看，否则 AST 里一条调用都找不到，
    # 断言会以一个空序列的形式失败（现场就是这样：`[]`）。
    fns = [n for n in _ast.walk(tree)
           if isinstance(n, _ast.FunctionDef)
           and n.name in ("_api_plan", "_plan_body")]
    assert fns, "server.py 里找不到 _api_plan / _plan_body"
    calls = []
    for fn in fns:
        for n in _ast.walk(fn):
            if isinstance(n, _ast.Call):
                name = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                if name in WANT or name in ("rebuild_config_full", "build_diffs"):
                    calls.append((n.lineno, name))
    calls.sort()

    # 两条路各出现一次这三个调用，且顺序相同
    seq = [name for _ln, name in calls]
    # 用落盘调用切成两段：前一段是全量重探，后一段是增量
    assert "rebuild_config_full" in seq and "build_diffs" in seq, seq
    i_rebuild = seq.index("rebuild_config_full")
    i_diffs = seq.index("build_diffs")
    redetect = [x for x in seq[:i_rebuild] if x in WANT]
    incremental = [x for x in seq[i_rebuild:i_diffs] if x in WANT]
    assert redetect == list(WANT), f"全量重探路的顺序不对：{redetect}"
    assert incremental == list(WANT), f"增量路的顺序不对：{incremental}"

    # CLI 也要有那道闸
    cli = _io.open(os.path.join(ROOT, "cli.py"), encoding="utf-8").read()
    assert "mark_new_sections" in cli, (
        "CLI 没有跨段新增那道闸 —— 同一个凭据走 CLI 与走网页会得到不同结果")
    ct = _ast.parse(cli)
    cli_lines = {}
    for n in _ast.walk(ct):
        if isinstance(n, _ast.Call):
            name = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
            if name in ("mark_new_sections", "assign_priorities", "build_diffs"):
                cli_lines.setdefault(name, n.lineno)
    assert cli_lines["mark_new_sections"] < cli_lines["assign_priorities"], (
        f"CLI 的闸排在定档之后 —— 被拦下的段会白占档位：{cli_lines}")
    assert cli_lines["assign_priorities"] < cli_lines["build_diffs"], cli_lines
    # 被拦下的段在 CLI 也要有可见提示
    assert "write_blocked" in cli, (
        "CLI 不显示 write_blocked = 网页端说「不写入」、CLI 静默跳过")

    print("[OK] Three paths: 三条入口共用同一批闸，调用顺序一致，"
          "CLI 也显示被拦下的原因")


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

    # ── 扫描范围：全仓，不只 request.py（2026-09-05 扩大） ──
    #
    # 原来只扫 `pipeline.py` 一个文件。而 `legacy/` 里有 5 处 `"hi"`
    # （audit-upstreams.py 3 处、swap-watch.py 2 处）从未被这道闸看见 ——
    # 那正是站方反测活规则最先拦的形态，而 README 写着那些脚本
    # 「原样保留可继续单独使用」。
    #
    # 按角色分三档，因为「短文本」在不同位置的含义完全不同：
    #   · 主流程（cpa_probe/ + server.py + cli.py）—— 严格，直接失败
    #   · legacy/ —— 是有意保留的旧脚本，不强制改；但**必须在 README 里
    #     标注过封号风险**，否则这一项失败（不能默默留着一个危险入口）
    #   · tests/ 与 tools/ —— 跳过：那里的短文本是**假上游返回的响应**，
    #     不是本工具发出去的请求，把它们判成违规是误报
    import io as _io
    import re as _re

    PAT = _re.compile(r'"(?:text|content|prompt)":\s*"([^"]{1,120})"')

    def _texts(path):
        s = _io.open(path, encoding="utf-8", errors="replace").read()
        for m in _re.finditer(r'text="([^"]{1,120})"', s):
            yield m.group(1)
        for m in PAT.finditer(s):
            yield m.group(1)

    def _suspect(t):
        """像测活吗。返回原因，不像则空串。"""
        if set(t) <= {"x"}:      # 上下文二分的填充，不是给站方读的
            return ""
        if len(t) < 20:
            return f"太短（{len(t)} 字符）"
        if not any(x in t.lower() for x in tech):
            return "缺技术内容"
        return ""

    # 主流程：严格
    strict_files = []
    probe_dir = os.path.join(ROOT, "cpa_probe")
    for fn in sorted(os.listdir(probe_dir)):
        if fn.endswith(".py"):
            strict_files.append(os.path.join(probe_dir, fn))
    for fn in ("server.py", "cli.py"):
        strict_files.append(os.path.join(ROOT, fn))

    bad = []
    for path in strict_files:
        for t in _texts(path):
            why = _suspect(t)
            if why:
                bad.append(f"{os.path.basename(path)}: {t!r} —— {why}")
    assert not bad, (
        "主流程里有像测活的探测文本（站方按这个封号）：\n  "
        + "\n  ".join(bad))

    # legacy：不强制改，但危险入口必须在 README 里标注过
    legacy_dir = os.path.join(ROOT, "legacy")
    legacy_bad = []
    if os.path.isdir(legacy_dir):
        for fn in sorted(os.listdir(legacy_dir)):
            if not fn.endswith(".py"):
                continue
            for t in _texts(os.path.join(legacy_dir, fn)):
                if _suspect(t):
                    legacy_bad.append(fn)
                    break
    if legacy_bad:
        readme = _io.open(os.path.join(ROOT, "README.md"),
                          encoding="utf-8").read()
        # 必须在**讲 legacy 的那一节内部**标注，而不是「README 全文某处提过
        # 反测活」—— 后者太松：那个词在讲主流程探测文本时也出现，于是删掉
        # legacy 的风险说明测试照样绿（2026-09-05 撤销实验证实）。
        lines = readme.splitlines()
        sec_start = next((i for i, ln in enumerate(lines)
                          if ln.startswith("#") and "legacy" in ln), -1)
        assert sec_start >= 0, (
            f"legacy/ 里有像测活的探测文本（{sorted(set(legacy_bad))}），"
            f"而 README 里没有一节专门讲 legacy —— 用户会按「原样保留可继续"
            f"单独使用」去跑它们")
        depth = len(lines[sec_start]) - len(lines[sec_start].lstrip("#"))
        sec_end = len(lines)
        for i in range(sec_start + 1, len(lines)):
            ln = lines[i]
            if ln.startswith("#"):
                d = len(ln) - len(ln.lstrip("#"))
                if d <= depth:
                    sec_end = i
                    break
        section_text = "\n".join(lines[sec_start:sec_end])
        assert "封号" in section_text or "反测活" in section_text, (
            f"README 讲 legacy 的那一节（第 {sec_start + 1} 行起）没提封号 / "
            f"反测活风险，而那些脚本里有 {sorted(set(legacy_bad))} 这样的测活文本")
        missing_names = [fn for fn in sorted(set(legacy_bad))
                         if fn not in section_text]
        assert not missing_names, (
            f"这些 legacy 脚本有测活文本，但 README 那一节没点名：{missing_names}"
            f" —— 不点名等于没警告，用户不知道该避开哪个")

    print(f"[OK] Probe text: {len(text)} 字符、含技术内容、非问候；"
          f"全仓主流程 {len(strict_files)} 个文件零违规；"
          f"legacy {len(sorted(set(legacy_bad)))} 个脚本的旧文本已在 README 标注风险")


def test_profile_verdict_reuse_saves_calls():
    """同段整梯全败后，后续种子跳过画像梯 —— 用真实请求数验证。

    门票是站+段的属性（站方查 headers 与 body 形态，不看模型名），所以第一个
    种子试完整梯全败之后，同段的后续种子不必重问。

    实测（假上游全 403）：57 次 → 30 次，省 47%。

    2026-09-16 改判据：省下的**比例**不再是好的判据
    --------------------------------------------
    上面那个 47% 是「每段 2-3 个种子、每个种子都跑一遍整梯」时代的数。
    种子已改成每族 1 个（`SEED_MODELS` 从 `model_catalog` 兜底名录派生），
    而画像梯的长度没变 —— 于是可省的份额本身变小了：claude / codex / gemini
    三段各只剩一个种子，复用在那三段**结构上无东西可省**，唯一还能省的是
    compat 段（多族各一，`_BASELINE_MODELS` 会打两个模型）。实测只剩 16%，
    与参数上限差得不多，再按 30% 卡就是在给一条已经变窄的收益定死数。

    改成直接验证**机制**（真正的诉求）：开复用必须出现 `profile-skipped`
    事件、不发重复的画像请求，且总请求数不增加。比例只作为信息打出来。
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

    # 端口交给 ThreadingHTTPServer 自己 bind（2026-09-05 修竞态）。
    # 原来的写法是「socket bind 0 号拿到端口 → close → 再让 server bind 同一个」，
    # 那两步之间有窗口 —— 全套跑 11 个套件、4 个起真 HTTP 服务，同一台机器短时间
    # 反复分配端口，窗口里被抢到就 OSError（Windows 上是 WinError 10048）。
    # 实测吻合：只在 run.py 全链跑时出现、两次分别落在起服务的两个套件、不可复现。
    srv = ThreadingHTTPServer(("127.0.0.1", 0), AllGate)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        row = parse_lines(f"http://127.0.0.1:{port},sk-test", allow_private=True).valid[0]

        def run(reuse: bool) -> tuple[int, list, dict]:
            evs: list[tuple[str, dict]] = []
            c = {"n": 0}
            with lock:
                calls["n"] = 0
            pr = Prober(gap=0.0, probe_context=False, swap_samples=0, workers=4,
                        reuse_profile_verdict=reuse)
            pr.on_event = lambda k, d=None: evs.append((k, d or {}))
            res = pr.probe(row)
            with lock:
                c["n"] = calls["n"]
            return c["n"], evs, res

        with_reuse, ev_reuse, res_reuse = run(True)
        without, _ev_plain, _res_plain = run(False)
    finally:
        srv.shutdown()

    skipped = [(k, d) for k, d in ev_reuse if k == "profile-skipped"]
    assert skipped, (
        "开复用却没发出 profile-skipped 事件 —— 整梯全败的结论没有被复用")
    assert not any(k == "profile-skipped" for k, _ in _ev_plain), (
        "关复用时不该出现 profile-skipped")

    # compat 段：开了复用后，整梯只该为一个模型跑一次。第二个模型若又跑一遍
    # 整梯，说明复用没接上（这正是本用例存在的意义）。
    combos = [a.combo for a in res_reuse.sections["openai-compatibility"].attempts]
    prof_combos = [c for c in combos if c.startswith("id:")]
    dupes = len(prof_combos) - len(set(prof_combos))
    assert dupes == 0, (
        f"compat 段画像请求重复 {dupes} 次（{prof_combos}）—— 复用没生效")

    assert with_reuse <= without, (
        f"开复用不该更多请求，实际 开={with_reuse} 关={without}")
    saved_pct = (1 - with_reuse / without) * 100
    # 比例只做参考：种子数已收敛到每族 1 个，可省的份额本身随种子数变化，
    # 把它卡成阈值等于给一条会随参数漂移的数定死数（见 docstring）。
    assert skipped and dupes == 0, "复用机制没生效"

    print(f"[OK] Profile reuse: {without} → {with_reuse} 次请求"
          f"（省 {saved_pct:.0f}%），profile-skipped × {len(skipped)}，"
          f"compat 段画像请求无重复")


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
    assert ident.claude_betas_unconditional, f"应能解析 beta，errors={ident.errors}"
    assert not ident.ok, "只有头文件的夹具不能声称请求体与停用规则已覆盖"

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


def test_drift_never_blocks_context():
    """漂移检测不许挡住 /api/context —— 它决定前端能不能显示页面。

    2026-09-02 现场：`/api/context` 同步调 check_profile_drift，而远程模式要拉
    两个 GitHub 文件。国内 VPS 直连 raw.githubusercontent 不通，实测**每次**
    打开网页干等 15 秒（失败不进缓存，第二次一样慢），而前端要等这个响应回来
    才 `#app.hidden = false` —— 用户看到的是只有页头、正文全空的白屏。

    这里守两件事：
      ① 失败结论也进缓存（否则「每次都慢」这个症状原样回来）
      ② _drift_snapshot 立即返回，把真正的核对丢给后台线程
    """
    import cpa_probe as cp
    import server as srv
    from cpa_probe import cpa_source_probe as csp

    # ── ① 负缓存 ──
    csp._remote_cache.update(at=0.0, ident=None, ref="", ok=False)
    r1 = csp.extract_remote(ref="no-such-ref-xyz-9999", timeout=2)
    assert not r1.claude_betas_unconditional, "这个 ref 不该存在"
    assert csp._remote_cache["ident"] is not None, (
        "失败必须写缓存 —— 否则拉不通的环境每次打开网页都重付一遍超时")
    assert csp._remote_cache["ok"] is False, "失败的缓存要标 ok=False"

    t0 = time.time()
    r2 = csp.extract_remote(ref="no-such-ref-xyz-9999", timeout=2)
    dt = time.time() - t0
    assert r2 is r1, "第二次该直接拿缓存对象"
    assert dt < 0.5, f"命中负缓存该是瞬时的，实测 {dt:.2f}s"

    # 失败 TTL 必须远短于成功 TTL：网络故障通常是暂时的
    assert csp._REMOTE_FAIL_TTL < csp._REMOTE_TTL, (
        f"失败 TTL {csp._REMOTE_FAIL_TTL} 不该 >= 成功 TTL {csp._REMOTE_TTL}")

    # commit 号同样要缓存 —— 它与 extract_remote 打同一个 GitHub
    csp._commit_cache.update(at=0.0, commit=None, ref="")
    c1 = csp.remote_commit(ref="no-such-ref-xyz-9999", timeout=2)
    assert csp._commit_cache["commit"] is not None, "commit 失败也要缓存"
    t0 = time.time()
    c2 = csp.remote_commit(ref="no-such-ref-xyz-9999", timeout=2)
    assert c2 == c1 and time.time() - t0 < 0.5, "commit 该命中缓存"

    # ── ② _drift_snapshot 不阻塞 ──
    srv._DRIFT_CACHE.update(at=0.0, value=None, inflight=False)
    slept: list[float] = []

    def slow_check(**kw):
        slept.append(time.time())
        time.sleep(1.5)                 # 冒充「拉 GitHub 拉了很久」
        return {"checked": True, "source": "假的", "drifts": []}

    real = cp.check_profile_drift
    try:
        cp.check_profile_drift = slow_check       # type: ignore[assignment]
        t0 = time.time()
        first = srv._drift_snapshot(source_root="", cfg={}, allow_remote=True)
        dt = time.time() - t0
        assert dt < 0.5, f"第一次必须立即返回，实测 {dt:.2f}s —— 就是那个白屏"
        assert first.get("pending") is True, first
        assert first.get("checked") is False, "pending 时不能声称核对过"
        assert first.get("drifts") == [], "形状要与真结论一致，前端才不用特判"

        # 后台还在跑时再来几次：仍然立即返回，且**不重复起线程**
        for _ in range(3):
            t0 = time.time()
            again = srv._drift_snapshot(source_root="", cfg={}, allow_remote=True)
            assert time.time() - t0 < 0.3
            assert again.get("pending") is True
        assert len(slept) == 1, f"inflight 期间不该重复起线程，实起 {len(slept)} 次"

        # 等后台跑完 —— 这次该拿到真结论，而且是瞬时的
        deadline = time.time() + 10
        while time.time() < deadline:
            got = srv._drift_snapshot(source_root="", cfg={}, allow_remote=True)
            if not got.get("pending"):
                break
            time.sleep(0.2)
        assert got.get("checked") is True, f"后台算完该有真结论：{got}"
        assert got.get("source") == "假的"
        t0 = time.time()
        srv._drift_snapshot(source_root="", cfg={}, allow_remote=True)
        assert time.time() - t0 < 0.3, "命中缓存该是瞬时的"

        # ── ③ 后台线程里抛异常不能吞掉整个检查 ──
        srv._DRIFT_CACHE.update(at=0.0, value=None, inflight=False)

        def boom(**kw):
            raise RuntimeError("模拟核对时崩了")

        cp.check_profile_drift = boom                # type: ignore[assignment]
        srv._drift_snapshot(source_root="", cfg={}, allow_remote=True)
        deadline = time.time() + 5
        while time.time() < deadline:
            got = srv._drift_snapshot(source_root="", cfg={}, allow_remote=True)
            if not got.get("pending"):
                break
            time.sleep(0.2)
        assert got.get("checked") is False, got
        assert "模拟核对时崩了" in (got.get("why") or ""), got
        assert srv._DRIFT_CACHE["inflight"] is False, "异常后必须清 inflight"
    finally:
        cp.check_profile_drift = real                # type: ignore[assignment]
        srv._DRIFT_CACHE.update(at=0.0, value=None, inflight=False)

    print("[OK] Drift async: 负缓存生效、_drift_snapshot 立即返回、"
          "后台异常不丢检查")


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

    # ⑤ 段头自带空字面量（`claude-api-key: []` / `{}`）—— 那种段头后面直接挂
    # `- api-key:` 是非法 YAML。增量路径早就用 `_empty_literal_rewrite` 处理
    # 这件事，全量重建这一支曾经漏了（2026-09-04 自查）：同一份输入走两条路，
    # 一条合法一条不合法。落盘被 validate 挡住，所以症状是「全量重探对这类
    # 文件整个不可用」——本项目自己的 tools/e2e_dead_pick.py 造场景就用 `[]`。
    for lit in ("[]", "{}", "{ }", "[]  # 本段暂时清空"):
        src = f'host: "127.0.0.1"\n\nclaude-api-key: {lit}\n'
        cfg_e = yaml.safe_load(src)
        pe = ImportPlan(host="n.example", masked_key="k", line_no=1)
        pe.sections["claude-api-key"] = SectionPlan(
            section="claude-api-key", base_url="https://n.example",
            api_key="sk-n", models=["claude-opus-5"], priority=100,
            model_source="probed")
        new_e, _w = rebuild_config_full(
            cfg_e, {("https://n.example", "sk-n"): pe},
            src.splitlines(keepends=True))
        ok_e, msg_e = validate(new_e)
        assert ok_e, f"段头 `{lit}` 时产出非法 YAML：{msg_e[:120]}"
        ents = (yaml.safe_load(new_e) or {}).get("claude-api-key") or []
        assert len(ents) == 1, f"段头 `{lit}` 时条目没写进去：{ents}"
        # 空字面量被摘掉，行尾注释保留（那也是人写的）
        assert f"claude-api-key: {lit.split('#')[0].strip()}" not in new_e
        if "#" in lit:
            assert "本段暂时清空" in new_e, "行尾注释丢了"

    print(f"[OK] Rebuild preserves: 全局键与无方案段全部保留、无重复顶层键、"
          f"空字面量段头摘成裸键（{len(warns)} 条警告）")


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

    # ③ codex 段的 websockets **不再走 carry**（2026-09-04）：它由实测决定，
    #    改成 render_entry 自己写、原值由 existing_toggles 搬。两条路同时生效
    #    会写出两行同名键 —— PyYAML 取后一个，而 Go 的 yaml.v3 直接报
    #    `mapping key already defined` 让 CPA 起不来。
    cc = carry["codex-api-key"]
    txt_c = "".join(cc.get(carry_key("a.example.com", "cdx-A")) or [])
    assert "websockets" not in txt_c, (
        f"websockets 已移出 carry，不该再被原文搬运：{txt_c!r}")

    # ④ 整链：全量重建后字段计数必须与原文一致
    #    websockets 走 existing_toggles 而不是 carry，所以方案里要照 server 的
    #    全量重探那条路把它搬进来（不搬就是这份测试自己丢的，不是产品缺陷）。
    from cpa_probe.batch import existing_toggles
    tg = existing_toggles(cfg)
    plans = {}
    for sec, ak, bu, pri in (("claude-api-key", "sk-A", "https://a.example.com", 290),
                             ("claude-api-key", "sk-B", "https://a.example.com", 190),
                             ("codex-api-key", "cdx-A", "https://a.example.com/v1", 90)):
        pl = ImportPlan(host="a.example.com", masked_key=ak)
        pl.sections[sec] = SectionPlan(
            section=sec, base_url=bu, api_key=ak,
            models=["claude-opus-5" if "claude" in sec else "gpt-5.6-sol"],
            priority=pri,
            prior_toggles=dict(tg.get((sec, "a.example.com", ak)) or {}))
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
          " / fingerprint-profile 逐字保真，同站不同 Key 不染色；"
          "websockets 已移出 carry 改由实测 + existing_toggles 决定")


def test_rebuild_keeps_model_context_length():
    """每个模型自己的 `max-context-length` 必须搬回。

    2026-09-03 逐字段对账才抓到，它落在一个三方都不管的空档里：

      · `extract_carry_lines` **有意跳过** models 整块 —— 模型清单由方案重新
        生成，搬原文行会与新清单打架。
      · 方案只带**一个**值（`max_context_length` + `context_model`），那是本次
        探测实测的那一个模型。
      · 于是本次没探上下文（`--no-context`、或那个模型没被验、或探测判死）时，
        历史实测值全部消失。实测生产配置 8 处，kilo.example 的 987500 与
        zulu 的 15515 都在其中。

    丢了不会让站不可用，而是**客户端按错的窗口定压缩点**：CPA 把它写进
    `/v1/models` 的 `context_length`（model_registry.go:1437-1438）与 Codex 的
    `max_context_window`（internal/client/codex/models/models.go:208-210）；
    没有它就回落 CPA 内置目录值，那对中转站往往偏大 —— 客户端塞满才发现被
    上游截断。

    优先级：本次实测 > 原值搬运 > 不写。
    """
    import yaml
    from cpa_probe.batch import existing_model_context
    from cpa_probe.writeback import render_entry
    from cpa_probe.plan import SectionPlan

    cfg = yaml.safe_load('''
claude-api-key:
  - api-key: "kA"
    base-url: "https://a.example.com"
    models:
      - name: "claude-opus-5"
        alias: ""
        max-context-length: 987500
      - name: "claude-sonnet-5"
        alias: ""
openai-compatibility:
  - name: "p"
    base-url: "https://a.example.com/v1"
    api-key-entries:
      - api-key: "kA"
      - api-key: "kB"
    models:
      - name: "claude-opus-5"
        alias: ""
        max-context-length: 531667
''')
    mc = existing_model_context(cfg)
    # compat 段这一维是 entry_scope 算出的 provider 身份，不是裸 host
    # （2026-09-12：_source_identity 保留 scheme 并折叠尾部 /v1，见
    # writeback._source_identity；归一化规则本身由
    # tests/test_writeback_compliance.py 钉住，这里只钉「表按那个键索引」）。
    from cpa_probe.batch import entry_scope as _scope
    CSCOPE = _scope("openai-compatibility", "https://a.example.com/v1")
    # 987500 是旧版按**字符数**写下的值（旧 _bisect 的第三个二分中点），
    # 搬运时折算成 token —— CPA 把这个字段当 token 读。见
    # batch._fix_legacy_char_context 与 pipeline._bisect 的单位一节。
    assert mc[("claude-api-key", "a.example.com", "kA",
               "claude-opus-5")] == 246875, mc
    # 没写窗口的模型不该出现在表里（与「写了 0」区分）
    assert ("claude-api-key", "a.example.com", "kA", "claude-sonnet-5") not in mc, mc
    # compat 的 models 在 provider 级 —— 组内每把 Key 都查得到
    # 531667 不在旧二分的取值格子里 —— 那是上游正文自报的 token 数，
    # 本来就对，必须原样保留（折算它会把一个正确值改错）。
    for k in ("kA", "kB"):
        assert mc[("openai-compatibility", CSCOPE, k,
                   "claude-opus-5")] == 531667, mc

    # 渲染：本次没探上下文 → 搬原值，且标「原值搬运」
    sp = SectionPlan(section="claude-api-key", base_url="https://a.example.com",
                     api_key="kA", models=["claude-opus-5", "claude-sonnet-5"],
                     priority=500,
                     prior_context={"claude-opus-5": 987500})
    rows = render_entry(sp, "  ", "    ", "x")
    txt = "\n".join(rows)
    assert "max-context-length: 987500" in txt, txt   # prior_context 直接给值，不经折算
    assert txt.count("max-context-length") == 1, (
        f"只有 claude-opus-5 有窗口值，不该外推给 sonnet：{txt}")
    assert "原值搬运" in txt, txt

    # 本次实测优先：同一个模型两边都有值时用实测的，且标「实测值」
    sp2 = SectionPlan(section="claude-api-key", base_url="https://a.example.com",
                      api_key="kA", models=["claude-opus-5"], priority=500,
                      max_context_length=1_100_000,
                      context_model="claude-opus-5",
                      prior_context={"claude-opus-5": 987500})
    t2 = "\n".join(render_entry(sp2, "  ", "    ", "x"))
    assert "max-context-length: 1100000" in t2, t2
    # 行尾注明单位：文件里同时存在旧的字符数值，只写「实测值」读不出是哪一种。
    assert "实测容量（token）" in t2, t2
    assert "987500" not in t2, f"实测值该盖掉原值：{t2}"

    # 本次实测的是**另一个**模型：两个各自写自己的值，不互相外推
    sp3 = SectionPlan(section="claude-api-key", base_url="https://a.example.com",
                      api_key="kA",
                      models=["claude-opus-5", "claude-sonnet-5"], priority=500,
                      max_context_length=200_000,
                      context_model="claude-sonnet-5",
                      prior_context={"claude-opus-5": 987500})
    t3 = "\n".join(render_entry(sp3, "  ", "    ", "x"))
    assert "max-context-length: 987500" in t3 and "max-context-length: 200000" in t3, t3
    assert t3.count("max-context-length") == 2, t3

    # 模型级白名单外字段：CPA 支持另外七个（display-name / force-mapping /
    # image / input-modalities / output-modalities / is-compat / thinking，
    # 见 config_types.go 的四个 *Model 结构体），而 render_entry 只写三个、
    # carry 又跳过整个 models 块 —— 七个字段没有任何人接。
    # 当前生产配置一个都没用到，所以这是补闸而不是修已发生的事故。
    from cpa_probe.batch import existing_model_extras
    cfg2 = yaml.safe_load('''
claude-api-key:
  - api-key: "kA"
    base-url: "https://a.example.com"
    models:
      - name: "claude-opus-5"
        alias: ""
        display-name: "Opus 5"
        force-mapping: true
        thinking:
          levels: ["low", "high"]
      - name: "claude-haiku-4-5"
        alias: ""
        image: false
        input-modalities: ["text", "image"]
''')
    ex = existing_model_extras(cfg2)
    k5 = ("claude-api-key", "a.example.com", "kA", "claude-opus-5")
    # alias 也在 extras 里（2026-09-04）：render_entry 写死 `alias: ""` 只对
    # 「原本就是空串」的条目成立，非空 alias 是段级兼容名，必须搬回来。
    assert ex[k5] == {"alias": "", "display-name": "Opus 5",
                      "force-mapping": True,
                      "thinking": {"levels": ["low", "high"]}}, ex[k5]
    sp4 = SectionPlan(section="claude-api-key", base_url="https://a.example.com",
                      api_key="kA",
                      models=["claude-opus-5", "claude-haiku-4-5"], priority=500,
                      prior_model_extras={
                          k[3]: v for k, v in ex.items()})
    t4 = "claude-api-key:\n" + "\n".join(
        render_entry(sp4, "  ", "    ", "x")) + "\n"
    from cpa_probe.writeback import validate as _v
    ok4, msg4 = _v(t4)
    assert ok4, msg4
    back = {m["name"]: m
            for m in yaml.safe_load(t4)["claude-api-key"][0]["models"]}
    assert back["claude-opus-5"]["display-name"] == "Opus 5", back
    assert back["claude-opus-5"]["force-mapping"] is True, back
    assert back["claude-opus-5"]["thinking"] == {"levels": ["low", "high"]}, back
    assert back["claude-haiku-4-5"]["image"] is False, back
    assert back["claude-haiku-4-5"]["input-modalities"] == ["text", "image"], back
    # 认不出的形状**整个跳过**，不写半截 —— 写出合法但语义错的 YAML 比丢掉更糟，
    # 而 validate() 只看语法。
    from cpa_probe.writeback import _yaml_field
    assert _yaml_field("  ", "x", object()) == []
    assert _yaml_field("  ", "x", [{"deep": [object()]}]) == []

    # 非空 alias 必须搬回来，且**只写一行**（它在 extras 表里只为了被搬运，
    # 渲染位置紧跟 name 与现有文件的键序一致；extras 循环要跳过它，
    # 否则一个模型写出两行 alias）。
    cfg6 = yaml.safe_load('''
claude-api-key:
  - api-key: "kB"
    base-url: "https://b.example.com"
    models:
      - name: "claude-opus-5"
        alias: "opus"
      - name: "claude-sonnet-5"
        alias: ""
''')
    ex6 = existing_model_extras(cfg6)
    sp6 = SectionPlan(section="claude-api-key", base_url="https://b.example.com",
                      api_key="kB",
                      models=["claude-opus-5", "claude-sonnet-5"], priority=1,
                      prior_model_extras={k[3]: v for k, v in ex6.items()})
    t6 = "claude-api-key:\n" + "\n".join(
        render_entry(sp6, "  ", "    ", "x")) + "\n"
    assert _v(t6)[0], _v(t6)[1]
    assert t6.count("alias:") == 2, f"每个模型只该写一行 alias：\n{t6}"
    back6 = {m["name"]: m
             for m in yaml.safe_load(t6)["claude-api-key"][0]["models"]}
    assert back6["claude-opus-5"]["alias"] == "opus", back6
    # 原本就是空串的照旧写空串 —— 不改成与 name 相同，否则生产配置里 459 行
    # 原本是 `""` 的会全被改掉，diff 里多出 459 处无意义改动
    assert back6["claude-sonnet-5"]["alias"] == "", back6

    print("[OK] Model context: 逐模型搬原值，实测优先，绝不把 A 的窗口外推给 B；"
          "白名单外的模型字段也搬回，认不出的形状整个跳过；"
          "非空 alias 搬回且只写一行")


def test_rebuild_keeps_prefix_and_provider_name():
    """prefix 与 compat 的 provider `name` 必须搬原值，不能现编。

    2026-09-03 拿真实文件做**逐字段 deep-equal** 才抓到 —— 之前的对账只比
    字段的出现次数（`text.count("prefix:")`），而「121 个条目的 prefix 全被
    抹掉、同时注释里多出 121 处提到 prefix」这种情况两边都数得对：
    计数相等，值全错。

    两处各自的后果：

      · prefix —— `dominant_prefix` 只在该段 70% 以上统一时给值，那是给
        **新条目**猜的默认。既有条目自己写的才是真的。
        `force-model-prefix: false` 下 prefix 额外注册一个命名空间别名
        （applyModelPrefixes，service_models.go:600-614 同时注册
        `claude-opus-5` 与 `ANT/claude-opus-5`），抹掉它让所有按 `ANT/xxx`
        发的请求命中不到。实测 121/121 被抹。

      · compat 的 name —— 它就是 CPA 的 provider 身份：
        `util.OpenAICompatibleProviderKey(name)` 写进 Auth 的 `provider_key`，
        冷却（conductor_cooldown.go:73）、模型能力
        （api_key_model_capabilities.go:186）、执行路由三处都按它索引。
        render_entry 原来用 `host_of(base_url)` 现编，实测 12/13 个 provider
        被改名（`romeo` → `romeo.example`）—— 冷却状态与能力缓存
        全部作废，而且本项目自己的 `name_alias_map`（注释里的人读短名 →
        域名）也跟着失效，下一轮读注释拿健康度就大面积漏判。
    """
    import yaml
    from cpa_probe.batch import existing_prefixes, existing_provider_names
    from cpa_probe.writeback import rebuild_config_full, render_entry, validate
    from cpa_probe.plan import SectionPlan, ImportPlan

    orig = """host: "127.0.0.1"

claude-api-key:
  - api-key: "kA"
    base-url: "https://a.example.com"
    prefix: "ANT"
    priority: 500
    models: [{name: "claude-opus-5", alias: ""}]
  - api-key: "kB"
    base-url: "https://b.example.com"
    priority: 400
    models: [{name: "claude-opus-5", alias: ""}]

openai-compatibility:
  - name: "shortname"
    base-url: "https://a.example.com/v1"
    prefix: "CHMA"
    priority: 300
    api-key-entries:
      - api-key: "kA"
    models: [{name: "claude-opus-5", alias: ""}]
"""
    lines = orig.splitlines(keepends=True)
    cfg = yaml.safe_load(orig)

    pf = existing_prefixes(cfg)
    assert pf[("claude-api-key", "a.example.com", "kA")] == "ANT", pf
    # kB 没写 prefix —— 键不该存在（与「写了空串」要能区分）
    assert ("claude-api-key", "b.example.com", "kB") not in pf, pf
    # compat 的 prefix 在 provider 级，组内每把 Key 都查得到
    # compat 段这一维是 entry_scope 算出的 provider 身份，不是裸 host
    # （2026-09-12：_source_identity 保留 scheme 并折叠尾部 /v1，见
    # writeback._source_identity；归一化规则本身由
    # tests/test_writeback_compliance.py 钉住，这里只钉「表按那个键索引」）。
    from cpa_probe.batch import entry_scope as _scope
    cscope = _scope("openai-compatibility", "https://a.example.com/v1")
    assert pf[("openai-compatibility", cscope, "kA")] == "CHMA", pf

    pn = existing_provider_names(cfg)
    # 键是 compat_provider_key（provider 身份：保留 scheme、折叠尾部
    # /v1），不是裸 host —— 见 writeback._source_identity 的说明。
    assert pn == {cscope: "shortname"}, pn

    # render_entry：给了 provider_name 就用它，没给才回落到 host
    sp = SectionPlan(section="openai-compatibility",
                     base_url="https://a.example.com/v1", api_key="kA",
                     models=["claude-opus-5"], priority=300,
                     prefix="CHMA", provider_name="shortname")
    rows = render_entry(sp, "  ", "    ", "x")
    assert any('- name: "shortname"' in r for r in rows), rows
    sp2 = SectionPlan(section="openai-compatibility",
                      base_url="https://a.example.com/v1", api_key="kA",
                      models=["claude-opus-5"], priority=300)
    assert any('- name: "a.example.com"' in r
               for r in render_entry(sp2, "  ", "    ", "x")), "回落该用 host"

    # 整链：原样重建后 prefix 与 name 逐字段一致
    def plan_for(host, key, sec, base, prio, prefix="", pname=""):
        p = ImportPlan(host=host, masked_key=key, line_no=1)
        p.sections[sec] = SectionPlan(
            section=sec, base_url=base, api_key=key,
            models=["claude-opus-5"], priority=prio, model_source="probed",
            prefix=prefix, provider_name=pname)
        return p

    plans = {
        ("https://a.example.com", "kA"): plan_for(
            "a.example.com", "kA", "claude-api-key",
            "https://a.example.com", 500, prefix="ANT"),
        ("https://b.example.com", "kB"): plan_for(
            "b.example.com", "kB", "claude-api-key",
            "https://b.example.com", 400),
        ("https://a.example.com/v1", "kA-c"): plan_for(
            "a.example.com", "kA", "openai-compatibility",
            "https://a.example.com/v1", 300, prefix="CHMA",
            pname="shortname"),
    }
    new, _w = rebuild_config_full(cfg, plans, lines)
    ok, msg = validate(new)
    assert ok, msg
    n2 = yaml.safe_load(new)
    got = {e["api-key"]: e.get("prefix") for e in n2["claude-api-key"]}
    assert got["kA"] == "ANT", f"prefix 丢了：{got}"
    assert not got.get("kB"), f"kB 原本没 prefix，被猜出来一个：{got}"
    prov = n2["openai-compatibility"][0]
    assert prov["name"] == "shortname", f"provider 被改名：{prov['name']}"
    assert prov.get("prefix") == "CHMA", prov

    print("[OK] Prefix & name: 逐条搬原值，未写的不凭空补，provider 不改名")


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
    # 键含段（2026-09-02 二次对账后改）：实测 kilo.example 的 5 把 Key 在
    # compat 段有 proxy-url、在 claude 段**故意没有**（那条路径直连可用）。
    # 按 (host, key) 两元组搬运会把 compat 的代理灌进 claude 段 ——
    # 实测 claude 段 proxy-url 从 3 条涨到 8 条。多一跳不会让请求失败，
    # 所以 validate 与写后验证都发现不了，又是一处静默改变行为。
    assert P.get(("gemini-api-key", "g.example.com", "g1")) \
        == "http://mihomo:7890", P
    # 没写的与空串都不该进表 —— 空串与「没这个键」语义相同（都不走代理），
    # 收进来会让重建凭空写出 `proxy-url: ""`
    assert ("gemini-api-key", "g.example.com", "g2") not in P, P
    assert ("gemini-api-key", "g.example.com", "g3") not in P, P
    # compat 段的 proxy-url 在 api-key-entries 上，不在 provider 级
    # compat 段这一维是 entry_scope 算出的 provider 身份，不是裸 host
    # （2026-09-12：_source_identity 保留 scheme 并折叠尾部 /v1，见
    # writeback._source_identity；归一化规则本身由
    # tests/test_writeback_compliance.py 钉住，这里只钉「表按那个键索引」）。
    from cpa_probe.batch import entry_scope as _scope
    o_scope = _scope("openai-compatibility", "https://o.example.com/v1")
    assert P.get(("openai-compatibility", o_scope, "k1")) \
        == "http://mihomo:7890", P
    assert ("openai-compatibility", o_scope, "k2") not in P, P

    # 同一个 (host, key) 在两段一有一无时，不许互相污染
    cfg2 = yaml.safe_load("""
claude-api-key:
  - api-key: "same"
    base-url: "https://both.example"

openai-compatibility:
  - name: "both"
    base-url: "https://both.example/v1"
    api-key-entries:
      - api-key: "same"
        proxy-url: "http://mihomo:7890"
    models:
      - name: "claude-opus-5"
        alias: ""
""")
    P2 = existing_proxies(cfg2)
    assert ("claude-api-key", "both.example", "same") not in P2, (
        "claude 段本来没有代理，不该从 compat 段继承过来")
    assert P2.get((
        "openai-compatibility",
        _scope("openai-compatibility", "https://both.example/v1"),
        "same")) == "http://mihomo:7890", P2

    print("[OK] Proxy preserved: 有值的搬回，空串与未写的不凭空添加，"
          "段与段之间不互相污染")


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

    # 同一个站三个 Key —— 现场最常见的形态（gorou 15 个、tango 14 个）
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


def test_compat_group_key_preservation():
    """compat 段：组内没进方案的 Key 不能丢，per-key 字段不能被统一。

    两处缺陷共用一个成因（2026-09-03）——compat 段的结构是「一个 provider
    条目 = 一个站，多把 Key 挂在它的 api-key-entries 下」，而 render_entry
    只写 `- api-key: X` 加一个**全组共用**的 proxy-url：

      ① 组内没进方案的 Key 消失。`_orphan_provider_lines` 只保留「整个
         provider 都没被碰到」的条目，被碰到的整条重写。前三段有
         `_orphan_entry_lines` 兜这个，compat 段没有。实测生产配置
         gorou.example 15 把 Key、tango.example 14 把，只要一把探测抛异常
         （BatchProber 把那个凭据整个从 results 去掉）就丢一把。
      ② per-key 的 proxy-url / weight 被统一成 head 那把的值。实测
         kilo.example 5 把、hotel.example 3 把带 per-key proxy-url；
         组内不一致时全组按 head 写 —— 多一跳不会失败，validate 与写后
         验证都发现不了。weight 更糟：0 会把那把 Key 整个逐出调度池。
    """
    import yaml
    from cpa_probe.writeback import (compat_key_blocks, rebuild_config_full,
                                     render_entry, validate)
    from cpa_probe.plan import SectionPlan, ImportPlan

    orig = """host: "127.0.0.1"

openai-compatibility:
  - name: "p.example.com"
    base-url: "https://p.example.com/v1"
    priority: 500
    api-key-entries:
      - api-key: "k1"
        proxy-url: "http://mihomo:7890"
      - api-key: "k2"
      - api-key: "k3"
        weight: 0
    models:
      - name: "claude-opus-5"
        alias: ""
  - name: "q.example.com"
    base-url: "https://q.example.com/v1"
    priority: 400
    api-key-entries:
      - api-key: "q1"
    models:
      - name: "gpt-5.6-sol"
        alias: ""
"""
    lines = orig.splitlines(keepends=True)
    cfg = yaml.safe_load(orig)

    # 解析：per-key 续行按 (host, key) 索引，且只收该 Key 自己那几行
    kb = compat_key_blocks(lines)
    # provider 键是 compat_provider_key —— provider 身份（保留 scheme、
    # 折叠尾部 /v1），不是裸 host。见 writeback._source_identity。
    from cpa_probe.writeback import compat_provider_key as _pk
    PK = _pk("https://p.example.com/v1")
    QK = _pk("https://q.example.com/v1")
    assert set(kb) == {PK, QK}, kb
    assert set(kb[PK]) == {"k1", "k2", "k3"}, kb[PK]
    assert [x.strip() for x in kb[PK]["k1"]] == [
        'proxy-url: "http://mihomo:7890"'], kb[PK]["k1"]
    assert kb[PK]["k2"] == [], "k2 没有续行，不该染到别人的"
    assert [x.strip() for x in kb[PK]["k3"]] == [
        "weight: 0"], kb[PK]["k3"]

    # 只有 k2 进方案（k1 未勾、k3 探测抛异常）—— 三把都要在，各自的字段照旧
    p = ImportPlan(host="p.example.com", masked_key="k2", line_no=1)
    p.sections["openai-compatibility"] = SectionPlan(
        section="openai-compatibility", base_url="https://p.example.com/v1",
        api_key="k2", models=["claude-opus-5"], priority=600,
        model_source="probed")
    new, warns = rebuild_config_full(
        cfg, {("https://p.example.com/v1", "k2"): p}, lines)
    ok, msg = validate(new)
    assert ok, msg
    n2 = yaml.safe_load(new)

    provs = {x["name"]: x for x in n2["openai-compatibility"]}
    assert set(provs) == {"p.example.com", "q.example.com"}, (
        f"未碰到的 provider 被删了：{sorted(provs)}")
    keys = [e["api-key"] for e in provs["p.example.com"]["api-key-entries"]]
    assert set(keys) == {"k1", "k2", "k3"}, f"组内 Key 丢了：{keys}"
    assert len(keys) == 3, f"Key 重复了：{keys}"
    by_key = {e["api-key"]: e
              for e in provs["p.example.com"]["api-key-entries"]}
    assert by_key["k1"].get("proxy-url") == "http://mihomo:7890", by_key["k1"]
    assert "proxy-url" not in by_key["k2"], (
        f"k2 原本没有代理，被 k1 的值染上了：{by_key['k2']}")
    assert by_key["k3"].get("weight") == 0, (
        f"k3 的 weight:0 丢了 —— 那把 Key 会复活：{by_key['k3']}")
    assert "weight" not in by_key["k2"], f"k2 被灌上 weight：{by_key['k2']}"
    assert any("不在本次方案内" in w for w in warns), warns
    # priority 是 provider 级的，按进方案的那把更新
    assert provs["p.example.com"]["priority"] == 600

    # per-key 字段必须**逐把 Key** 取自它自己的方案，不能拿 head 的套给全组。
    #
    # 2026-09-03 第二次改这一处：第一版只搬原文（新值被 elif 吞掉），第二版
    # 改成合并、但补的是 head 的 `sp.proxy_url` —— 于是组内所有没有原文行的
    # Key 都被灌上 head 的出口。实测同站常有的 Key 走 mihomo、有的直连可用，
    # 多一跳不会失败，所以 validate 与写后验证都发现不了。
    p_head = SectionPlan(
        section="openai-compatibility", base_url="https://p.example.com/v1",
        api_key="h1", models=["claude-opus-5"], priority=700,
        proxy_url="http://mihomo:7890", model_source="probed")
    p_mem = SectionPlan(
        section="openai-compatibility", base_url="https://p.example.com/v1",
        api_key="h2", models=["claude-opus-5"], priority=650,
        proxy_url="", weight=0, model_source="probed")
    rows = render_entry(p_head, "  ", "    ", "x", extra_keys=["h2", "h3"],
                        key_plans={"h1": p_head, "h2": p_mem})
    ki = [i for i, ln in enumerate(rows) if "- api-key:" in ln]
    blk = {rows[i].split(":", 1)[1].strip().strip('"'):
           rows[i + 1:ki[n + 1] if n + 1 < len(ki) else len(rows)]
           for n, i in enumerate(ki)}
    assert any("proxy-url" in ln for ln in blk["h1"]), blk["h1"]
    assert not any("proxy-url" in ln for ln in blk["h2"]), (
        f"h2 的方案没有代理，被 head 的值染上了：{blk['h2']}")
    assert any("weight: 0" in ln for ln in blk["h2"]), (
        f"h2 自己的 weight:0 没写出来：{blk['h2']}")
    assert not any("weight" in ln for ln in blk["h1"]), (
        f"head 没有 weight 却写了：{blk['h1']}")
    # 既没有原文行、也没有自己的方案（纯孤儿 Key）—— 两个字段都不写，
    # 让 CPA 侧回落默认（proxy 走全局、weight 默认 1）。凭空写上 head 的值
    # 就是替操作员改了那把 Key 的行为。
    # 最后一把 Key 的块会一直延伸到 provider 级的 models，所以只看 per-key
    # 那两个字段有没有出现。
    h3 = [ln for ln in blk["h3"]
          if re.match(r"^\s+(proxy-url|weight)\s*:", ln)]
    assert not h3, f"孤儿 Key 被凭空写上 per-key 字段：{h3}"

    # 同一个站两种 base-url 写法：必须合并成一个 provider 并给出警告。
    # 分成两条会让同一把 Key 在池子里占两个位，且 CPA 按 name 索引冷却，
    # 两个同名 provider 会对同一个 Key 命中两套配置。
    p2 = ImportPlan(host="p.example.com", masked_key="k1", line_no=2)
    p2.sections["openai-compatibility"] = SectionPlan(
        section="openai-compatibility", base_url="https://p.example.com/v1/",
        api_key="k1", models=["claude-opus-5"], priority=550,
        model_source="probed")
    new3, warns3 = rebuild_config_full(
        cfg, {("https://p.example.com/v1", "k2"): p,
              ("https://p.example.com/v1/", "k1"): p2}, lines)
    assert validate(new3)[0], validate(new3)[1]
    n3 = yaml.safe_load(new3)
    hosts = [x["name"] for x in n3["openai-compatibility"]]
    assert len(hosts) == len(set(hosts)) == 2, f"写出了重名 provider：{hosts}"
    assert any("base-url 写法" in w for w in warns3), warns3

    print("[OK] Compat group: 组内孤儿 Key 保留、per-key proxy/weight 不串、"
          "两种 base-url 写法合并成一个 provider")


def test_rebuild_entry_conservation():
    """条目守恒 + 未勾选不删除 + 跨段新能力要加进去。三条都是回归。

    事故一（2026-09-02）：条目从 121 变 246
      全量重探为每个凭据的**四段**都生成方案，整段重写时全写进去。而真实
      情况是每个凭据只配了自己那几段（79 个凭据里跨四段的只有 9 个）。

    事故二（2026-09-02）：只勾推荐项 → 未勾的原条目被删
      整段重写只写进方案的条目，其余消失。而「没进方案」有三种无害原因：
      用户没勾、段判不可写、探测抛异常。修法是 keep_unplanned。

    缺陷三（2026-09-03）：探测发现原上游在别的段也能用，却被静默丢弃
      事故一的修法是「原来没配这一段就跳过」，把这件事一起挡掉了 —— 而它
      正是最该新增的条目。现在的判据是**这一段有没有实测依据**：
      probed / manual / catalog 放行，seed（工具猜测）不放行。
      两者的分界就是事故一与缺陷三的分界，所以这一项同时守着两头。
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

    def plan_for(host, key, secs, prio, source="probed"):
        p = ImportPlan(host=host, masked_key=key, line_no=1)
        for sec in secs:
            bu = f"https://{host}" + ("/v1" if "codex" in sec else "")
            p.sections[sec] = SectionPlan(
                section=sec, base_url=bu, api_key=key,
                models=["gpt-5.6-sol" if "codex" in sec else "claude-opus-5"],
                priority=prio, model_source=source)
        return p

    # ① 四段方案全是 seed（工具猜测）—— 原来没配的段一个都不该新增。
    #    这就是 121 → 246 那次事故的形态：探测没有任何依据，只是兜底填了名字。
    ALL = ("gemini-api-key", "codex-api-key", "claude-api-key",
           "openai-compatibility")
    plans = {
        ("https://a.example.com", "kA"):
            plan_for("a.example.com", "kA", ALL, 900, source="seed"),
        ("https://b.example.com", "kB"):
            plan_for("b.example.com", "kB", ALL, 800, source="seed"),
    }
    new, warns = rebuild_config_full(cfg, plans, lines)
    ok, msg = validate(new)
    assert ok, msg
    n2 = yaml.safe_load(new)

    # 条目守恒：原来 3 条，现在还是 3 条
    tot = sum(len(n2.get(s) or []) for s in ALL)
    assert tot == 3, f"条目数应守恒为 3，实得 {tot}（各段 " +         str({s: len(n2.get(s) or []) for s in ALL}) + "）"
    # gemini 与 compat 原本没有 —— 猜测清单不该让它们凭空多出来
    assert not n2.get("gemini-api-key"), "凭空写了 gemini 段"
    assert not n2.get("openai-compatibility"), "凭空写了 compat 段"
    assert any("原本不在 config.yaml 里" in w for w in warns),         f"跳过未拥有的段时必须给警告，实得 {warns}"
    # priority 确实更新了
    assert n2["claude-api-key"][0]["priority"] == 900

    # ①b 同一批方案，只把来源换成 probed（本次实测跑通）—— 必须新增进去。
    #     用户 2026-09-03 的要求：「探测发现原上游在别的段也能用，本项目应该
    #     加进去并整体计算」。B 站原来只配 claude，实测四段都通就该有四条。
    plans_probed = {
        ("https://a.example.com", "kA"):
            plan_for("a.example.com", "kA", ALL, 900),
        ("https://b.example.com", "kB"):
            plan_for("b.example.com", "kB", ALL, 800),
    }
    new1b, warns1b = rebuild_config_full(cfg, plans_probed, lines)
    ok1b, msg1b = validate(new1b)
    assert ok1b, msg1b
    n1b = yaml.safe_load(new1b)
    tot1b = sum(len(n1b.get(s) or []) for s in ALL)
    assert tot1b == 8, (
        f"实测通过的段该新增：2 凭据 × 4 段 = 8，实得 {tot1b}（各段 "
        + str({s: len(n1b.get(s) or []) for s in ALL}) + "）")
    assert len(n1b.get("gemini-api-key") or []) == 2, "实测通过的 gemini 段没加进去"
    assert len(n1b.get("openai-compatibility") or []) == 2, "实测通过的 compat 段没加进去"
    assert any("新增" in w and "原本不在 config.yaml 里" in w for w in warns1b), (
        f"新增段必须明确报出来（写了几条、依据是什么），实得 {warns1b}")
    # 新增段的**依据强弱**要写进警告 —— 操作员据此决定要不要回退
    assert any("本次实测通过" in w for w in warns1b), warns1b

    # ①c catalog（站方目录声称有，推理没过）也放行 —— 它的名字是这个站自己
    #     报的，与 seed（本工具猜的、与这个站无关）不是一档。
    plans_cat = {("https://b.example.com", "kB"):
                 plan_for("b.example.com", "kB", ("gemini-api-key",), 700,
                          source="catalog")}
    new1c, _w1c = rebuild_config_full(cfg, plans_cat, lines)
    assert validate(new1c)[0]
    assert len(yaml.safe_load(new1c).get("gemini-api-key") or []) == 1, (
        "catalog 来源的新增段被拦下了 —— 它与 seed 不是一档")

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

    print("[OK] Entry conservation: 猜测不新增、实测新增、未勾选的原条目不删除")


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

claude-api-key:
  - api-key: "g1"
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
    assert w.get(("gemini-api-key", "g.example.com", "g1")) == 0, w
    assert ("gemini-api-key", "g.example.com", "g2") not in w, w
    # compat 段这一维是 entry_scope 算出的 provider 身份，不是裸 host
    # （2026-09-12：_source_identity 保留 scheme 并折叠尾部 /v1，见
    # writeback._source_identity；归一化规则本身由
    # tests/test_writeback_compliance.py 钉住，这里只钉「表按那个键索引」）。
    from cpa_probe.batch import entry_scope as _scope
    o_scope = _scope("openai-compatibility", "https://o.example.com/v1")
    assert w.get(("openai-compatibility", o_scope, "k1")) == 0, w
    assert ("openai-compatibility", o_scope, "k2") not in w, w
    # 键必须含段（2026-09-03）：同一个 (host, key) 在 gemini 段封了、在
    # claude 段没封 —— 跨段共用会把 0 灌进 claude。实测生产 config.yaml 有
    # 6 个 (凭据, 段) 组合会被这样无声逐出调度池。
    assert ("claude-api-key", "g.example.com", "g1") not in w, (
        f"claude 段没写 weight，不该从 gemini 段继承 0：{w}")

    # 渲染时写出来
    sp = SectionPlan(section="gemini-api-key", base_url="g.example.com",
                     api_key="g1", models=["m"], priority=500, weight=0)
    text = "\n".join(render_entry(sp, "  ", "    ", "2026-09-01"))
    assert re.search(r"^\s+weight:\s*0", text, re.M), text

    # weight=None（原本没写）时不写这个字段
    sp2 = SectionPlan(section="gemini-api-key", base_url="g.example.com",
                      api_key="g2", models=["m"], priority=500)
    assert "weight:" not in "\n".join(render_entry(sp2, "  ", "    ", "x"))

    # 行尾说明按 CPA 的**实际**语义判，不只判 `== 0`
    # （credentialweight/weight.go:21-28 + selector.go:380-396）：
    #   <= 0        Normalize 归零 → positiveWeightAuths 剔除
    #   > 1000000   Normalize 报错 → authWeight 也返回 0 → 同样被剔除
    # 只给 0 加说明会让 -1 与超上限值看着像正常权重。
    def wline(w):
        s = SectionPlan(section="gemini-api-key", base_url="g.example.com",
                        api_key="g", models=["m"], priority=500, weight=w)
        return next(r for r in render_entry(s, "  ", "    ", "x")
                    if "weight:" in r)

    assert "已逐出调度池" in wline(0), wline(0)
    assert "已逐出调度池" in wline(-1), wline(-1)
    assert "已逐出调度池" in wline(1_000_001), wline(1_000_001)
    assert "上限 1000000" in wline(1_000_001), wline(1_000_001)
    for ok_w in (1, 999_999, 1_000_000):
        assert "逐出" not in wline(ok_w), wline(ok_w)

    # compat 段的 weight 挂在**每把 Key** 上，不在 provider 级。
    # 2026-09-03 发现：render_entry 的 compat 分支从来不写 weight —— 那把
    # `weight: 0` 只靠 carry 原文行侥幸活着，而组内孤儿 Key 与新增的 Key
    # 都没有原文行可搬。
    spc = SectionPlan(section="openai-compatibility",
                      base_url="https://o.example.com/v1",
                      api_key="k1", models=["m"], priority=500, weight=0)
    tc = render_entry(spc, "  ", "    ", "x", extra_keys=["k2"])
    wl = [ln for ln in tc if re.match(r"^\s+weight:", ln)]
    assert len(wl) == 1 and re.match(r"^\s+weight:\s*0", wl[0]), tc
    # 只写在 head 那把上 —— 别的 Key 的 weight 由它们自己的 key_lines 决定，
    # 拿 head 的值套过去就是把一把 Key 的封禁扩散到整组。
    ki = [i for i, ln in enumerate(tc) if "- api-key:" in ln]
    assert tc.index(wl[0]) == ki[0] + 1, f"weight 没紧跟 head 那把 Key：{tc}"
    # 原文里已有该 Key 的 weight 行时不重复写（YAML 同键两次，后者覆盖前者）
    tc2 = render_entry(spc, "  ", "    ", "x", extra_keys=["k2"],
                       key_lines={"k1": ["        weight: 3"]})
    wl2 = [ln for ln in tc2 if re.match(r"^\s+weight:", ln)]
    assert len(wl2) == 1 and "weight: 3" in wl2[0], tc2

    print("[OK] Weight preserved: 键含段、compat 逐 Key、未写的不凭空添加")


def test_model_catalog_three_layers():
    """目录读不到时填「当前市面最新」，三层数据源都要能单独兜住。

    2026-09-02 用户要求：「如果无法检测出模型，原则上需要在线检索大数据按
    当前市面上存在最新模型编号直接填写好」。原来只填两个写死的种子，现场
    截图里 gemini 段就是那个「站方目录也没报模型：手填模型名」的空输入框。

    三层（可信度递减）：
      1. CPA 权威名录（远程，与 CPA 自己的 model_updater.go 同源）
      2. 本地 config.yaml 已有的模型名 —— 站方特供型号只在这层
      3. 内置兜底（用户指定的那批）

    这一项**不发网络请求**：remote 参数由测试直接喂，走的正是产品代码里
    `remote_names()` 拿到结果后的那条路。
    """
    import yaml
    from cpa_probe import model_catalog as mc

    # ── ① 只有远程 ──
    remote = ["gpt-5.6-sol", "gpt-5.6-terra", "claude-opus-5", "claude-fable-5",
              "gemini-3.1-pro-preview", "kimi-k3", "grok-4.6", "gpt-oss-120b"]
    got, src = mc.latest_models("codex-api-key", cfg=None, remote=remote)
    assert got == ["gpt-5.6-sol", "gpt-5.6-terra"], got
    assert "CPA 权威名录" in src, src
    # 跨族与非对话的都不该进来
    assert "grok-4.6" not in got and "gpt-oss-120b" not in got

    # ── ② 远程拿不到，落到本地 config.yaml ──
    #    站方特供型号（远程名录里没有）必须能通过这一层进来 —— 实测用户的
    #    config.yaml 里有 gemini-3.1-pro-high / -preview-search /
    #    -preview-customtools / gpt-5.6，四个都不在 CPA 名录里。
    #    门槛：一个名字要被当作「本段通用候选」，至少得有 **2 个不同的站**
    #    在用（2026-09-11 加，修 claude-fake-5 跨站外推）。所以夹具必须是
    #    **两个站**才反映真实形态 —— 生产 config.yaml 里那几个特供型号
    #    （gemini-3.1-pro-high / -preview-search / -preview-customtools）
    #    实测就是多站共用的，门槛照样放行它们。
    cfg = yaml.safe_load("""
gemini-api-key:
  - api-key: "g1"
    base-url: "https://g.example"
    models:
      - name: "gemini-3.1-pro-high"
        alias: ""
      - name: "gemini-3.5-flash"
        alias: ""
      - name: "gemini-3.1-pro-onlyhere"
        alias: ""
  - api-key: "g2"
    base-url: "https://g2.example"
    models:
      - name: "gemini-3.1-pro-high"
        alias: ""
""")
    got, src = mc.latest_models("gemini-api-key", cfg=cfg, remote=[])
    assert got == ["gemini-3.1-pro-high"], got
    assert "本地 config.yaml" in src, src
    assert "gemini-3.5-flash" not in got, "flash 不符合 gemini 段规则"
    #    只有一个站在用的名字**不外推** —— 这就是 claude-fake-5 的来路：
    #    某个站条目里的手误被当成候选推荐给了另一个站。
    #    name_is_safe 只挡非法字符，挡不住「合法但不存在」。
    assert "gemini-3.1-pro-onlyhere" not in got,         f"只有 1 个站在用的名字不该外推给别的站：{got}"

    # ── ②b claude-fake-5 回归：单站手误绝不外推 ──
    fake_cfg = yaml.safe_load("""
claude-api-key:
  - api-key: "k1"
    base-url: "https://a.example"
    models:
      - name: "claude-opus-5"
        alias: ""
      - name: "claude-fake-5"
        alias: ""
  - api-key: "k2"
    base-url: "https://b.example"
    models:
      - name: "claude-opus-5"
        alias: ""
""")
    got, _src = mc.latest_models("claude-api-key", cfg=fake_cfg, remote=[])
    assert "claude-fake-5" not in got, f"fake 名字被外推了：{got}"
    assert "claude-opus-5" in got, f"多站共用的真名不该被误伤：{got}"

    # ── ③ 两层都空，落到内置兜底 ──
    for sec, want in (
            ("codex-api-key", "gpt-5.6"),
            ("claude-api-key", "claude-opus-5"),
            ("gemini-api-key", "gemini-3.1-pro"),
            ("openai-compatibility", "claude-opus-5")):
        got, src = mc.latest_models(sec, cfg=None, remote=[])
        assert got, f"{sec} 兜底也是空的 —— 那就成了「待定」"
        assert "内置兜底" in src, src
        assert want in got, f"{sec} 兜底里缺 {want}：{got}"
        # 兜底清单自己也必须过段规则 —— 写死的值同样会过期
        for m in got:
            assert mc.section_allows(sec, m), f"{sec} 兜底里有违规项 {m}"

    # ── ④ 用户 2026-09-02 指定的清单必须都能出现 ──
    #    这是验收判据：三层齐备时那些名字一个都不能缺。
    full_remote = ["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra",
                   "claude-opus-5", "claude-sonnet-5", "claude-fable-5",
                   "gemini-3.1-pro-preview", "gemini-3.1-pro-low"]
    #    每个特供型号都给**两个站** —— 第 2 层有「≥2 个站在用」的门槛
    #    （见 ② 的说明）。生产 config.yaml 里这些名字实测就是多站共用的。
    full_cfg = yaml.safe_load("""
codex-api-key:
  - api-key: "c1"
    base-url: "https://c.example"
    models: [{name: "gpt-5.6", alias: ""}]
  - api-key: "c2"
    base-url: "https://c2.example"
    models: [{name: "gpt-5.6", alias: ""}]
gemini-api-key:
  - api-key: "g1"
    base-url: "https://g.example"
    models:
      - name: "gemini-3.1-pro"
        alias: ""
      - name: "gemini-3.1-pro-high"
        alias: ""
      - name: "gemini-3.1-pro-preview-search"
        alias: ""
      - name: "gemini-3.1-pro-preview-customtools"
        alias: ""
  - api-key: "g2"
    base-url: "https://g2.example"
    models:
      - name: "gemini-3.1-pro"
        alias: ""
      - name: "gemini-3.1-pro-high"
        alias: ""
      - name: "gemini-3.1-pro-preview-search"
        alias: ""
      - name: "gemini-3.1-pro-preview-customtools"
        alias: ""
""")
    want = {
        "codex-api-key": ["gpt-5.6-sol", "gpt-5.6", "gpt-5.6-luna",
                          "gpt-5.6-terra"],
        "claude-api-key": ["claude-opus-5", "claude-fable-5",
                           "claude-sonnet-5"],
        "gemini-api-key": ["gemini-3.1-pro", "gemini-3.1-pro-high",
                           "gemini-3.1-pro-preview",
                           "gemini-3.1-pro-preview-search",
                           "gemini-3.1-pro-preview-customtools",
                           "gemini-3.1-pro-low"],
    }
    for sec, names in want.items():
        got, _src = mc.latest_models(sec, cfg=full_cfg, remote=full_remote,
                                     limit=12)
        missing = [n for n in names if n not in got]
        assert not missing, f"{sec} 缺用户指定的 {missing}：{got}"

    # ── ⑤ 幂等：同一批输入两次结果一致（diff 要可复核）──
    a = mc.latest_models("openai-compatibility", cfg=full_cfg,
                         remote=full_remote)[0]
    b = mc.latest_models("openai-compatibility", cfg=full_cfg,
                         remote=full_remote)[0]
    assert a == b, (a, b)

    # ── ⑥ compat 按族轮转，不能前 N 个全是同一族 ──
    #    compat 的价值是「一个条目转多族」，只注册 claude 等于浪费这个段。
    many = (["claude-opus-5", "claude-sonnet-5", "claude-fable-5",
             "claude-opus-4-8"] + ["gpt-5.6-sol", "kimi-k3", "gemini-3.1-pro"])
    got, _src = mc.latest_models("openai-compatibility", cfg=None,
                                 remote=many, limit=4)
    fams = {mc.family(m) for m in got}
    assert len(fams) >= 3, f"compat 前 4 个只覆盖 {fams}：{got}"

    # ── ⑦ 失败也缓存 ——「拉不通的环境每次都慢」那个坑不许回来 ──
    mc._cache.update(at=0.0, names=None, ok=False, why="")
    names, why = mc.remote_names(timeout=1, proxy="http://10.255.255.1:9")
    assert names == [] and why, (names, why)
    assert mc._cache["names"] is not None, "失败必须写缓存"
    assert mc._cache["ok"] is False
    t0 = time.time()
    again, _why2 = mc.remote_names(timeout=1, proxy="http://10.255.255.1:9")
    assert again == [] and time.time() - t0 < 0.5, "命中负缓存该是瞬时的"
    assert mc._TTL_BAD < mc._TTL_OK
    mc._cache.update(at=0.0, names=None, ok=False, why="")

    print("[OK] Model catalog: 三层各自兜住、用户指定清单全覆盖、"
          "compat 按族轮转、失败进负缓存")


def test_usable_but_empty_models():
    """usable=True 但模型清单为空时也要落到市面最新清单。

    2026-09-02 现场截图（第三次修同一处）：某站 compat 段四个标记连起来是
    「可用 + 实测 + 无可信模型 + 不可写入」。`_accept()` 在静默换模或
    「200 包错误体」时拒收模型，段仍算可用（端点确实响应、凭证有效），
    但清单一个都没进。而前两版把兜底写在 `else:` 分支里，`elif v.usable:`
    这条路直接绕过它。

    判据必须是「清单空不空」，不是「走了哪条分支」—— 用户的要求是
    「实测不可用就填充成对应类型的最高级别模型」。
    """
    import yaml
    import cpa_probe as cpa
    from cpa_probe import model_catalog
    from cpa_probe.pipeline import CandidateResult, SectionVerdict

    cfg = yaml.safe_load("""
claude-api-key:
  - api-key: "old"
    base-url: "https://old.example"
    priority: 500
    models: [{name: "claude-opus-5", alias: ""}]
""")

    def plan(usable):
        row = cpa.parse_lines("https://t.example,sk-t").valid[0]
        res = CandidateResult(row=row)
        for s in cpa.SECTIONS:
            res.sections[s] = SectionVerdict(
                section=s, usable=usable, base_url=row.base_for(s),
                models=[], catalog=[],
                category=("可用" if usable else "死路"), action="x")
        return cpa.build_plan(row, res, cfg, bands={},
                              seen=cpa.existing_fingerprints(cfg),
                              probation=True)

    for usable in (True, False):
        p = plan(usable)
        for s in cpa.SECTIONS:
            sp = p.sections[s]
            assert sp.models, (
                f"usable={usable} 的 {s} 清单为空 —— 那个段勾上也写不进模型")
            assert sp.writable, f"usable={usable} 的 {s} writable=False"
            assert sp.model_source == "seed", sp.model_source
            for m in sp.models:
                assert model_catalog.section_allows(s, m), f"{s} 有违规 {m}"

    # compat 段要覆盖四族 —— 用户明确要求「把 openai、gemini、claude、kimi
    # 四种的最高版本全部填上」
    sp = plan(True).sections["openai-compatibility"]
    fams = {model_catalog.family(m) for m in sp.models}
    assert len(fams) >= 3, f"compat 只覆盖 {fams}：{sp.models}"

    # 两种处境的措辞必须分开 —— 说错一种就是误导
    w_usable = " ".join(plan(True).sections["openai-compatibility"].warnings)
    w_dead = " ".join(plan(False).sections["openai-compatibility"].warnings)
    assert "端点响应正常" in w_usable, w_usable[:200]
    assert "探测未通过" in w_dead, w_dead[:200]

    # usable=True 且实测清单为空、但**站方目录有**：清单要取目录，不是种子。
    #
    # 2026-09-03 真实探测抓到：nova 的 compat 段正是这个状态，站方目录报了
    # 16 个名字，而方案里写的是 6 个种子猜测 —— 界面列目录那 16 个（一个没勾）、
    # 落盘写种子那 6 个，两个集合不相交。成因是分支判据写的是 `elif v.usable:`
    # 而不是「实测清单空不空」，与「兜底放在 else 里」是同一类错误。
    #
    # 目录里的名字是这个站自己报的，种子是本工具猜的、与这个站无关 ——
    # 写后者进去，CPA 路由过去大概率 404。
    # 市面名录钉死：下面的断言落在具体型号上，不钉就要外网、还会随名录漂移。
    MARKET = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "kimi-k3",
              "kimi-k3-256k", "claude-opus-5", "gemini-3.1-pro"]

    def plan_cat(usable, catalog, market=MARKET):
        row = cpa.parse_lines("https://t.example,sk-t").valid[0]
        res = CandidateResult(row=row)
        for s in cpa.SECTIONS:
            res.sections[s] = SectionVerdict(
                section=s, usable=usable, base_url=row.base_for(s),
                models=[], catalog=list(catalog),
                category=("可用" if usable else "死路"), action="x")
        with _patch.object(model_catalog, "remote_names",
                           return_value=(list(market), "")):
            return cpa.build_plan(row, res, cfg, bands={},
                                  seen=cpa.existing_fingerprints(cfg),
                                  probation=True)

    CAT = ["gpt-5.6-luna", "gpt-5.6-terra", "kimi-k3", "gpt-4o"]
    for usable in (True, False):
        sp = plan_cat(usable, CAT).sections["openai-compatibility"]
        assert sp.model_source == "catalog", (
            f"usable={usable} 且目录非空时清单该取目录，实得 {sp.model_source}"
            f"：{sp.models}")
        # 目录报过的同代成员一个都不能少 —— 那正是用户点名的
        # 「勾了 gpt-5.6 却没勾 gpt-5.6-sol」的反面
        assert {"gpt-5.6-luna", "gpt-5.6-terra", "kimi-k3"} <= set(sp.models), sp.models
        # 旧世代被剔除（同产品线取最高世代）
        assert "gpt-4o" not in sp.models, sp.models
        # 目录之外的名字只能是**目录报过的产品线**里的同代补齐，
        # 且必须标未验证。这一条原来断言 `set(sp.models) <= set(CAT)`，
        # 钉的是 2026-09-02 的旧规则；docx 第 4 条已推翻它，
        # 见 test_model_rules_no_dead_end 的说明。
        extra = set(sp.models) - set(CAT)
        assert extra <= {"gpt-5.6-sol", "kimi-k3-256k"}, (
            f"补进了这个站没报过的产品线：{extra}")
        for m in extra:
            assert sp.model_provenance.get(m) == "inferred", (m, sp.model_provenance)
    # 可用那一种的措辞不能说「推理请求未通过」—— 它通了，只是模型对不上
    wc = " ".join(plan_cat(True, CAT).sections["openai-compatibility"].warnings)
    assert "端点响应正常" in wc and "推理请求未通过" not in wc, wc[:220]

    print("[OK] Usable-but-empty: usable=True 且模型空时按目录 > 种子取，"
          "compat 覆盖多族，两种处境措辞分开")


def test_manual_beats_probed():
    """手填**无条件**优先，不看探测是否通过。

    2026-09-03 现场（第二次改这一处）：判据原来是
    `if forced_models and not v.usable`，两条路因此被堵死 ——

      ① usable=True 但 v.models 为空（静默换模 / 200 包错误体，`_accept`
         把模型全拒了）：手填的清单落不进来，反而掉进 seed 分支，界面上
         「手填」变成「猜测」。
      ② usable=True 且 v.models 非空：操作员想把探到的 4 个换成自己知道的
         8 个（探测只验前几个就停），probed 直接盖掉手填，一条警告都没有。

    手填是操作员的显式意图，探测判定是工具的推测。推测盖掉显式意图在任何
    处境下都是错的。
    """
    import yaml
    import cpa_probe as cpa
    from cpa_probe.pipeline import CandidateResult, SectionVerdict

    cfg = yaml.safe_load("""
claude-api-key:
  - api-key: "old"
    base-url: "https://old.example"
    priority: 500
    models: [{name: "claude-opus-5", alias: ""}]
""")

    def plan(*, usable, probed, forced):
        row = cpa.parse_lines("https://t.example,sk-t").valid[0]
        res = CandidateResult(row=row)
        for s in cpa.SECTIONS:
            res.sections[s] = SectionVerdict(
                section=s, usable=usable, base_url=row.base_for(s),
                models=list(probed), catalog=[],
                category=("可用" if usable else "死路"), action="x")
        return cpa.build_plan(row, res, cfg, bands={},
                             seen=cpa.existing_fingerprints(cfg),
                             probation=True,
                             force={s: list(forced) for s in cpa.SECTIONS})

    FORCED = ["claude-opus-5"]

    # ① 可用但实测清单为空 —— 手填要落进来，且标成 manual 而不是 seed
    sp = plan(usable=True, probed=[], forced=FORCED).sections["claude-api-key"]
    assert sp.models == FORCED, sp.models
    assert sp.model_source == "manual", (
        f"可用+实测空时手填被吞了，来源是 {sp.model_source}")

    # ② 可用且实测清单非空 —— 手填仍然优先，并说清它覆盖了实测结论
    sp2 = plan(usable=True, probed=["claude-sonnet-5"],
               forced=FORCED).sections["claude-api-key"]
    assert sp2.models == FORCED, f"probed 盖掉了手填：{sp2.models}"
    assert sp2.model_source == "manual", sp2.model_source
    w = " ".join(sp2.warnings)
    assert "claude-sonnet-5" in w and "手填" in w, (
        f"覆盖实测清单必须说出被覆盖的是什么：{w[:200]}")
    assert "探测未通过" not in w, f"探测通过却说未通过：{w[:200]}"

    # ③ 判死段照旧 —— 措辞是「探测未通过」
    sp3 = plan(usable=False, probed=[],
               forced=FORCED).sections["claude-api-key"]
    assert sp3.model_source == "manual", sp3.model_source
    assert "探测未通过" in " ".join(sp3.warnings), sp3.warnings

    # ④ 手填全不合规时的兜底措辞要按实际落到哪儿说，不能写死成「市面最新」
    sp4 = plan(usable=True, probed=["claude-sonnet-5"],
               forced=["gpt-5.6-sol"]).sections["claude-api-key"]
    assert sp4.model_source == "probed", sp4.model_source
    w4 = " ".join(sp4.warnings)
    assert "已丢弃" in w4 and "本次实测到的清单" in w4, (
        f"落回 probed 却说改用了市面最新清单：{w4[:250]}")

    print("[OK] Manual wins: 手填无条件优先，覆盖实测时措辞分开，"
          "全不合规的兜底说法跟着实际来源")


def test_offfamily_manual_and_catalog():
    """四族之外的模型：手填要放行，目录里只剩它们时也要收下。

    2026-09-03 核实 CPA 源码 + 真实配置后改。原来 `section_allows`（工具的
    选型偏好：只挑 gemini / gpt / claude / kimi）被当成硬闸用在三条路上，
    于是操作员没有任何办法把一个**已知可用**的四族之外模型写回去。

    真实反例：romeo 的 compat 段唯一端到端验证过的模型就是 grok-4.6
    （配置注释：「整个 vip 分组当前只有 grok-4.6 有渠道，已通过端到端验证的
    只有它」），foxtrot 段有 grok-4.6 + glm-5.2。按族拒掉之后那两段会从
    「有一个确认可用的模型」变成「只剩两个确认 503 的」。

    而 compat 段确实能跑它们：走 `/chat/completions`
    （openai_compat_executor.go:107），CPA 对模型名零校验
    （buildOpenAICompatibilityConfigModels 照单注册，service_models.go:713-739）。
    前三段仍按族拒 —— claude 段走 Anthropic 原生 `/v1/messages`，往那里发 grok
    上游必失配，放行只会制造死条目。
    """
    import yaml
    import cpa_probe as cpa
    from cpa_probe import model_catalog as mc
    from cpa_probe.pipeline import CandidateResult, SectionVerdict

    # section_protocol_ok 与 section_allows 的差别只有四族之外这一处
    for m in ("grok-4.6", "glm-5.2", "deepseek-v4f"):
        assert not mc.section_allows("openai-compatibility", m), m
        assert mc.section_protocol_ok("openai-compatibility", m), m
        for s in ("gemini-api-key", "codex-api-key", "claude-api-key"):
            assert not mc.section_protocol_ok(s, m), (s, m)
    # 非对话模型两者都拒，四段都拒
    for s in cpa.SECTIONS:
        assert not mc.section_protocol_ok(s, "gpt-image-2"), s

    cfg = yaml.safe_load(
        'claude-api-key:\n'
        '  - api-key: "old"\n'
        '    base-url: "https://old.example"\n'
        '    priority: 500\n'
        '    models: [{name: "claude-opus-5", alias: ""}]\n')

    def plan(*, forced=None, catalog=()):
        row = cpa.parse_lines("https://t.example,sk-t").valid[0]
        res = CandidateResult(row=row)
        for s in cpa.SECTIONS:
            res.sections[s] = SectionVerdict(
                section=s, usable=False, base_url=row.base_for(s),
                models=[], catalog=list(catalog), category="未知", action="x")
        kw = {}
        if forced:
            kw["force"] = {s: list(forced) for s in cpa.SECTIONS}
        return cpa.build_plan(row, res, cfg, bands={},
                              seen=cpa.existing_fingerprints(cfg),
                              probation=True, **kw)

    # ① 手填 grok-4.6：compat 段收下并给警告，前三段丢弃
    p = plan(forced=["grok-4.6"])
    sp = p.sections["openai-compatibility"]
    assert sp.models == ["grok-4.6"], sp.models
    assert sp.model_source == "manual", sp.model_source
    w = " ".join(sp.warnings)
    assert "不在本工具的四族清单" in w and "零校验" in w, w[:220]
    for s in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        assert p.sections[s].model_source == "seed", s
        assert "grok-4.6" not in p.sections[s].models, s
        assert "已丢弃" in " ".join(p.sections[s].warnings), s

    # ② 目录里**只有**四族之外的名字：compat 段收下站方报的，不写工具猜的
    p2 = plan(catalog=["grok-4.6", "glm-5.2"])
    sp2 = p2.sections["openai-compatibility"]
    assert sp2.model_source == "catalog", sp2.model_source
    assert set(sp2.models) == {"grok-4.6", "glm-5.2"}, sp2.models
    w2 = " ".join(sp2.warnings)
    assert "没有本工具四族清单" in w2 and "从没报过" in w2, w2[:240]
    for s in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        assert p2.sections[s].model_source == "seed", s

    # ③ 目录里**有**四族的名字时，四族之外的不进清单（选型偏好照旧生效）
    p3 = plan(catalog=["grok-4.6", "claude-opus-5"])
    sp3 = p3.sections["openai-compatibility"]
    assert sp3.models == ["claude-opus-5"], sp3.models
    assert "从没报过" not in " ".join(sp3.warnings)

    print("[OK] Off-family: 手填放行、目录只剩它们时收下、有四族时仍按偏好挑，"
          "前三段一律按族拒")


def test_stale_catalog_not_recommended():
    """站方目录整体落后一个世代以上时列出但不建议勾。

    2026-09-02 现场：romeo.example 的 codex 段目录只有 gpt-4 /
    gpt-4-32k / gpt-4o / gpt-4o-mini —— 四个都是世代 (4,0)，「同产品线取
    最高世代」把四个全留下并默认全勾，违反用户「最新是 gpt-5.6 时 gpt-4o
    不该默认勾选」的要求。

    为什么不换成市面最新清单：那个站的目录里确实没有 5.6 系的名字，写进去
    CPA 路由不到，把「有老模型可用」变成死条目 —— 比默认勾错更糟。
    所以只降级 recommended，清单照旧列出。
    """
    import yaml
    import cpa_probe as cpa
    from cpa_probe import model_catalog
    from cpa_probe.pipeline import CandidateResult, SectionVerdict

    cfg = yaml.safe_load("""
codex-api-key:
  - api-key: "old"
    base-url: "https://old.example/v1"
    priority: 500
    models: [{name: "gpt-5.6-sol", alias: ""}]
""")
    # 市面最新固定成 5.6，不依赖远程名录（测试零外网）
    remote = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]

    def plan(catalog):
        row = cpa.parse_lines("https://t.example,sk-t").valid[0]
        res = CandidateResult(row=row)
        for s in cpa.SECTIONS:
            res.sections[s] = SectionVerdict(
                section=s, usable=False, base_url=row.base_for(s),
                models=[], catalog=(list(catalog) if s == "codex-api-key"
                                    else []),
                category="死路", action="x")
        # build_plan 内部也会问 remote_names（补齐与落后判定各一次），
        # 一起钉住 —— 否则断言随真实名录漂移，还要外网。
        with _patch.object(model_catalog, "remote_names",
                           return_value=(list(remote), "")):
            return cpa.build_plan(
                row, res, cfg, bands={},
                seen=cpa.existing_fingerprints(cfg),
                probation=True).sections["codex-api-key"]

    # ── 落后：清单保留，但不建议勾 ──
    stale, why = model_catalog.catalog_is_stale(
        "codex-api-key", ["gpt-4", "gpt-4-32k", "gpt-4o", "gpt-4o-mini"],
        remote=remote)
    assert stale, "目录全是 gpt-4 系而市面到 5.6，该判落后"
    assert "4.0" in why and "5.6" in why, why

    sp = plan(["gpt-4", "gpt-4-32k", "gpt-4o", "gpt-4o-mini"])
    assert sp.models, "清单不该被清空 —— 那会让段勾不上"
    assert sp.writable, "仍要能手工勾"
    # 落后目录的**处置**在 2026-09-11 被用户推翻（docx 第 4 条）：
    # 原方案是「列出但不预勾」（catalog_stale 把 recommended 降级），
    # 新口径是「检测出来没有高级模型就按该系列该类型的最高级填充勾选」。
    # 所以整份目录都是老款时，清单直接被市面最高级顶掉，不再留老款。
    #
    # `catalog_is_stale` 本身保留、上面仍逐项测 —— 它现在的用途是解释性的：
    # 界面要说得出「你这份目录整体落后，所以清单里的名字是补进去的」。
    assert set(sp.models) <= set(remote), (
        f"落后目录该被顶成市面最高级，实得 {sp.models}")
    assert not any(m.startswith("gpt-4") for m in sp.models), sp.models
    for m in sp.models:
        assert sp.model_provenance.get(m) == "inferred", (m, sp.model_provenance)
    # 2026-09-17 反转（用户规则 ④）：补进去的市面最高级**也建议写**。
    # 依据强度由 model_provenance=inferred 与徽标可见，不再靠「不勾」表达。
    assert sp.recommended, "落后目录顶成市面最高级后该默认勾（规则 ④）"

    # ── 不落后：照常 ──
    fresh = plan(["gpt-4o", "gpt-5.1", "gpt-5.5", "gpt-5.6-luna",
                  "gpt-5.6-terra"])
    assert fresh.catalog_stale is False, "含 5.6 的目录不该判落后"
    # 目录报过的两个同代成员都在，名录里的第三个（gpt-5.6-sol）按
    # 「所有相同等级系列的模型全部都要勾选上」补齐
    assert set(fresh.models) == {"gpt-5.6-luna", "gpt-5.6-terra",
                                 "gpt-5.6-sol"}, fresh.models

    # ── 边界：目录里全是认不出版本的名字 → 不判落后（无从比较）──
    st2, _w2 = model_catalog.catalog_is_stale(
        "codex-api-key", ["o1", "o3-mini"], remote=remote)
    assert st2 is False, "认不出版本不该被判落后"
    # 空目录同理
    assert model_catalog.catalog_is_stale(
        "codex-api-key", [], remote=remote)[0] is False

    print("[OK] Stale catalog: 落后目录列出但不勾、清单不清空、"
          "认不出版本与空目录都不误判")


def test_rate_limit_learned():
    """限频正文里的阈值要被学走，下一次请求自动放慢。

    2026-09-02 现场：一个站在 79 凭据那轮里 46 次撞上 `bulk probe guard`，
    判定「限频」→ 处置写着「加大探测间隔重试」→ 然后**什么都没做**，
    接着用同样的节奏打下一个模型，于是 46 次全撞。

    那句正文里带着确切阈值（4 个模型 / 60 秒），工具读得出来却没用上。
    """
    from cpa_probe.pipeline import Prober

    p = Prober(gap=0.05, probe_context=False, swap_samples=0)
    evs: list[tuple] = []
    p.on_event = lambda k, d: evs.append((k, d))

    body = ('{"error":{"message":"该ip已被封禁，原因：bulk probe guard: '
            'ip 1.2.3.4 requested 4 distinct models in 60s (last_use ...)"}}')
    assert p._note_rate_limit("x.example", body) is True
    # 60/4 * 1.1 = 16.5 —— 平均间隔加 10% 余量（滑动窗口，贴着阈值仍会命中）
    assert abs(p._host_gap["x.example"] - 16.5) < 0.01, p._host_gap
    learned = [d for k, d in evs if k == "rate-limit-learned"]
    assert learned and learned[0]["models"] == 4 and learned[0]["window"] == 60

    # 更严的阈值要覆盖，更松的不许覆盖 —— 否则一次宽松的响应会把已学到的
    # 严格节奏冲掉，接着又开始撞
    assert p._note_rate_limit(
        "x.example", "requested 2 distinct models in 60s") is True
    assert abs(p._host_gap["x.example"] - 33.0) < 0.01
    assert p._note_rate_limit(
        "x.example", "requested 10 distinct models in 60s") is False
    assert abs(p._host_gap["x.example"] - 33.0) < 0.01, "更松的覆盖了严的"

    # 荒谬窗口要钳住 —— 见过站方报 3600s，照抄会让整批探测卡死
    p._note_rate_limit("y.example", "requested 1 distinct models in 3600s")
    assert p._host_gap["y.example"] == p._MAX_LEARNED_GAP

    # 没有阈值的限频正文不学（不能凭空放慢）
    assert p._note_rate_limit("z.example", "bulk probe guard 请稍后") is False
    assert "z.example" not in p._host_gap

    # 学到之后 _throttle 要真的用它，且整站合用一个桶（那类 guard 按账号
    # 全局计数，按段分桶会让瞬时并发变成 4 倍）
    p2 = Prober(gap=0.01, probe_context=False, swap_samples=0)
    p2.on_event = lambda k, d: None
    p2._note_rate_limit("h.example", "requested 60 distinct models in 60s")
    assert abs(p2._host_gap["h.example"] - 1.1) < 0.01, p2._host_gap
    t0 = time.time()
    p2._throttle("h.example", "gemini-api-key")
    p2._throttle("h.example", "codex-api-key")     # 不同段，仍要等
    dt = time.time() - t0
    assert dt >= 1.0, f"学到的 gap 没生效（两段合桶应等 ~1.1s），实测 {dt:.2f}s"

    print("[OK] Rate limit: 阈值被学走、只收紧不放松、荒谬值钳住、"
          "四段合用一个节奏桶")


def test_model_rules_no_dead_end():
    """规则收紧不许把段变成「勾不上」—— 那是 2026-09-01 修过的老症状。

    2026-09-02 自查发现的两处：

      ① `elif v.catalog:` 只判目录**非空**。规则收紧后一个只报 flash /
         oss / grok 的站会过滤出空列表 → models=[] → writable=False →
         那个段连勾选框都点不动。目录非空但全不合规必须也落到市面最新清单。
      ② 手填的**全部**不合规时 forced_models 变空 → 落到 seed 分支 →
         model_source 是 "seed" 不是 "manual" → 那条「已丢弃」警告永远
         不触发。用户手填了两个、一个都没进去、界面上一句提示都没有。
    """
    import yaml
    import cpa_probe as cpa                 # cp 是 parse 模块的别名，不是包
    from cpa_probe import model_catalog
    from cpa_probe.pipeline import CandidateResult, SectionVerdict

    cfg = yaml.safe_load("""
claude-api-key:
  - api-key: "old"
    base-url: "https://old.example"
    priority: 500
    models: [{name: "claude-opus-5", alias: ""}]
""")

    # 市面名录钉死（测试零外网，也让断言不随远程名录漂移）。
    # 内容取自 2026-09-12 的真实名录形态：codex 线最高是 gpt-6 世代。
    MARKET = ["gpt-6-astra", "gpt-5.6", "gpt-5.6-sol", "gpt-5.6-luna",
              "claude-opus-5", "claude-sonnet-5",
              "gemini-3.1-pro", "gemini-3.1-pro-preview"]

    def plan_with(catalog, force=None):
        row = cpa.parse_lines("https://t.example,sk-t").valid[0]
        res = CandidateResult(row=row)
        for s in cpa.SECTIONS:
            res.sections[s] = SectionVerdict(
                section=s, usable=False, base_url=row.base_for(s),
                models=[], catalog=list(catalog), category="死路", action="x")
        with _patch.object(model_catalog, "remote_names",
                           return_value=(list(MARKET), "")):
            return cpa.build_plan(row, res, cfg, bands={},
                                  seen=cpa.existing_fingerprints(cfg),
                                  probation=True, force=force)

    # ── ① 目录非空但全不合规 ──
    dirty = ["gemini-3.5-flash", "gpt-oss-120b", "grok-4.6", "gpt-image-2"]
    p = plan_with(dirty)
    for s in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        sp = p.sections[s]
        assert sp.models, f"{s} 目录全不合规时清单为空 —— 那个段勾不上"
        assert sp.writable, f"{s} writable=False —— 勾选框点不动"
        assert sp.model_source == "seed", (
            f"{s} 该落到市面最新清单，实得 {sp.model_source}")
        for m in sp.models:
            assert model_catalog.section_allows(s, m), f"{s} 兜底里有违规 {m}"

    # ── 目录里有合规项时仍走 catalog，且同系列取最新 ──
    mixed = ["gpt-5.5", "gpt-5.6", "claude-opus-4-8", "claude-opus-5",
             "gemini-2.5-pro", "gemini-3.1-pro"]
    p2 = plan_with(mixed)
    assert p2.sections["codex-api-key"].model_source == "catalog"
    # 同产品线旧版被剔除（5.5 让位给 5.6），再按用户 2026-09-11 的规则补到
    # 该系列的**市面最高级**：codex 线当前最高是 gpt-6 世代。
    #
    # 这一条断言改过两次，记清楚每次的依据：
    #
    # 2026-09-02 旧规则「不写目录之外的名字」→ 断言 `["gpt-5.6"]`。
    # 2026-09-11 docx 第 4 条推翻它 → 断言 `["gpt-6-astra"]`（只留顶代）。
    # 2026-09-16 用户第 2 条再次修正 → **两代都留**：
    #
    #     「如果检测出来没有最高级模型，比如检测出来最新模型 gpt-6 系列
    #       不通，直接按最新模型 gpt-6 填充。但是次高级模型如 gpt-5.6 通的，
    #       这个时候将 gpt-5.6 系列与 gpt-6 系列都勾选保留。」
    #
    # 只留顶代的代价（mhtml 快照实测）：那一轮 45 次请求几乎 0 次 200，
    # 站方报过的 `gpt-5.6-sol` 被换成站方**从没报过**的 `gpt-6-astra` ——
    # 写进 config.yaml 后 CPA 每次轮到这个站都对着不存在的型号发请求。
    # 判据见 `topup_to_market_top` 的 `_exempt_gen`：站方报过、且市面名录
    # 里那一代还在售时豁免；名录里连同代的影子都没有（陈年目录）才顶掉。
    got2 = p2.sections["codex-api-key"].models
    assert "gpt-6-astra" in got2, f"没补到该系列市面最高级：{got2}"
    assert "gpt-5.6" in got2, (
        f"站方报过且市面仍在售的那一代被丢了（用户 2026-09-16 第 2 条）：{got2}")
    # 降级档一个都不许有（用户 2026-09-16 第 1 条）
    assert not any(model_catalog.is_low_tier(m) for m in got2), got2
    assert p2.sections["codex-api-key"].model_provenance["gpt-6-astra"] == (
        "inferred"), "填进去的名字必须标成未验证"
    assert "gpt-5.5" not in p2.sections["codex-api-key"].models
    assert p2.sections["claude-api-key"].models == ["claude-opus-5"]
    assert p2.sections["gemini-api-key"].models == [
        "gemini-3.1-pro", "gemini-3.1-pro-preview"], (
        p2.sections["gemini-api-key"].models)

    # ── ② 手填全不合规：仍要报「已丢弃」 ──
    p3 = plan_with([], force={"gemini-api-key":
                              ["gemini-3.5-flash", "gemini-3.6-flash"]})
    sp3 = p3.sections["gemini-api-key"]
    assert sp3.model_source == "seed", sp3.model_source
    dropped = [w for w in sp3.warnings if "手填" in w and "已丢弃" in w]
    assert dropped, (
        f"手填全不合规却没有提示 —— 工具悄悄换了清单：{sp3.warnings}")
    assert "全部不合规" in dropped[0], dropped[0]

    # ── 手填部分合规：收下合规的，丢弃项照样报 ──
    p4 = plan_with([], force={"codex-api-key":
                              ["gpt-5.6-sol", "gpt-image-2", "gpt-oss-20b",
                               "gpt-5.5", "claude-opus-5"]})
    sp4 = p4.sections["codex-api-key"]
    assert sp4.model_source == "manual", sp4.model_source
    # gpt-5.5 与 gpt-5.6-sol 同产品线（gpt），5.6 是更高世代 → 5.5 被挤掉。
    # 2026-09-02 从「同系列取最新」改成「产品线取最高世代」之后才成立：
    # 按系列分组时 gpt-5.5 的系列是 gpt-*、5.6-sol 是 gpt-*-sol，互不相干。
    assert sp4.models == ["gpt-5.6-sol"], sp4.models
    d4 = [w for w in sp4.warnings if "已丢弃" in w]
    assert d4, "手填有丢弃项却没提示"
    for m in ("gpt-image-2", "gpt-oss-20b", "claude-opus-5", "gpt-5.5"):
        assert m in d4[0], f"丢弃清单里缺 {m}：{d4[0]}"

    print("[OK] No dead end: 目录全不合规落到市面清单、产品线取最高世代、"
          "手填丢弃项两种情形都有提示")



def test_capability_toggles_probed_and_written():
    """段专属能力开关必须**实测**出来，三态各自落到确定的写回行为。

    2026-09-04 用户要求：「不光是 headers，包括 Websockets 是否需要开启等
    都需要检测做出打开或者不打开的配置」。

    为什么必须实测而不是照抄别的条目：这两个开关改变 CPA **发出去的形态**，
    而站方支不支持是站方的属性。抄错的后果不对称 ——
      · websockets 抄成 true 而站方不支持 → CPA 走 WS 通道且**不回落 HTTP**
        （CodexAutoExecutor 只按下游形态与该开关分流，
        codex_websockets_executor.go:71-77），那个凭据的 WS 请求全废
      · 抄成 false 而站方支持 → 只是用不上 WS，无害
    所以默认必须是「不开」，只有实测 101 才开。

    三态与写回行为：
      True  → 写 `<字段>: true`
      False → **不写**（CPA 零值即关闭），且原值即使是 true 也不搬 —— 那正是
              这次探测要修掉的错配置
      None  → 也不写，但原值为 true 时照原值搬运（没有证据就不动）
    """
    import yaml
    from cpa_probe import client
    from cpa_probe.pipeline import Attempt, SectionVerdict
    from cpa_probe.plan import SectionPlan
    from cpa_probe.writeback import (_RENDERED_KEYS, render_entry, validate)
    from cpa_probe.batch import existing_toggles

    # ── (1) http→ws 与 CPA 的 buildCodexResponsesWebsocketURL 同构 ──
    assert client.http_to_ws("https://x.example/v1/responses") == (
        "wss://x.example/v1/responses")
    assert client.http_to_ws("http://x.example/v1/responses") == (
        "ws://x.example/v1/responses")
    # 非 http/https 或缺主机名 → 空串，调用方据此跳过（对应 CPA 那边直接
    # 报 unsupported responses websocket URL scheme）
    assert client.http_to_ws("ftp://x/y") == ""
    assert client.http_to_ws("") == ""

    # ── (2) 握手判定：101 才算支持，其余都不算 ──
    prober = Prober(gap=0.0, probe_context=False, swap_samples=0,
                    probe_capabilities=True)
    row = cp.parse_lines("https://ws.example,sk-ws").valid[0]
    calls = []
    state = {}

    def fake_ws(url, *, headers=None, timeout=10):
        calls.append({"url": url, "headers": dict(headers or {})})
        return client.Response(state["status"], state["body"], 12,
                               state.get("error", ""))

    ws_orig = client.ws_handshake
    client.ws_handshake = fake_ws            # type: ignore[assignment]
    try:
        for status, body, err, want, why in (
            ("101", "", "", True, "101 = 支持"),
            ("400", '{"error":"beta header required"}', "", False,
             "400 = 不支持"),
            ("403", "cf challenge", "", False, "403 = 不支持"),
            ("000", "", "timeout", None, "连接层失败 = 未能判定，不是不支持"),
            # 101 但 accept 算不对 —— 反代吞了 Upgrade 自己回 101 的形态
            ("101", "", "Sec-WebSocket-Accept 不匹配", False, "假 101 = 不支持"),
        ):
            state.clear()
            state.update({"status": status, "body": body, "error": err})
            v = SectionVerdict(section="codex-api-key", usable=True,
                               base_url="https://ws.example/v1",
                               models=["gpt-5.6-sol"])
            prober._probe_websockets(row, v)
            assert v.websockets is want, (
                f"{why}：期望 {want}，实得 {v.websockets}"
                f"（note={v.websockets_note}）")
            assert v.websockets_note, f"{why} 必须给出说明"
        # 握手要打到 CPA 那条路径上，且带上它无条件发的 beta 头
        assert calls[0]["url"] == "wss://ws.example/v1/responses", calls[0]
        assert calls[0]["headers"].get("OpenAI-Beta") == (
            "responses_websockets=2026-02-06"), calls[0]["headers"]
        assert calls[0]["headers"].get("Authorization") == "Bearer sk-ws"

        # ── (3) 需代理的段不探 —— 直连结果说明不了走代理时的行为 ──
        n_before = len(calls)
        v_proxy = SectionVerdict(section="codex-api-key", usable=True,
                                 base_url="https://ws.example/v1",
                                 models=["gpt-5.6-sol"], need_proxy=True)
        prober._probe_websockets(row, v_proxy)
        assert v_proxy.websockets is None, "需代理时该记未探测而不是不支持"
        assert "代理" in v_proxy.websockets_note, v_proxy.websockets_note
        assert len(calls) == n_before, "需代理时不该真发握手"
    finally:
        client.ws_handshake = ws_orig        # type: ignore[assignment]

    # ── (4) 只在该段、且该段可用时才探 ──
    for sec in ("gemini-api-key", "claude-api-key"):
        v = SectionVerdict(section=sec, usable=True,
                           base_url="https://ws.example", models=["m"])
        prober._stage5_capabilities(row, v)
        assert v.websockets is None and v.prompt_cache_key is None, (
            f"{sec} 没有这类开关，不该被探")
    v_dead = SectionVerdict(section="codex-api-key", usable=False,
                            base_url="https://ws.example/v1")
    prober._stage5_capabilities(row, v_dead)
    assert v_dead.websockets is None, "段不通时无从验证，不该判成不支持"

    off = Prober(gap=0.0, probe_context=False, swap_samples=0,
                 probe_capabilities=False)
    v_off = SectionVerdict(section="codex-api-key", usable=True,
                           base_url="https://ws.example/v1", models=["m"])
    off._stage5_capabilities(row, v_off)
    assert v_off.websockets is None and not v_off.websockets_note

    # ── (5) compat 的 prompt_cache_key：请求体带上那个字段再发一次 ──
    pc = Prober(gap=0.0, probe_context=False, swap_samples=0,
                probe_capabilities=True)
    seen = []
    pc_state = {}

    def fake_call(section, base, key, model, **kw):
        seen.append(kw)
        return Attempt(section=section, model=model, combo=kw.get("combo", ""),
                       status=pc_state["status"], category="", action="",
                       elapsed_ms=3, excerpt=pc_state.get("excerpt", ""),
                       error_envelope=pc_state.get("envelope", False),
                       # Attempt.ok 现在还要 response_valid（真 _call 里由
                       # classify.validate_success 判好存下来）。这里跟着
                       # status 走，好让 400 / 错误体 / 000 三行仍各自测
                       # 它们声称的那件事。
                       response_valid=pc_state["status"] == "200")

    pc._call = fake_call                     # type: ignore[assignment]
    for status, envelope, want, why in (
        ("200", False, True, "200 = 上游收下了"),
        ("400", False, False, "400 = 上游拒收，开着会让每个请求都失败"),
        ("200", True, False, "200 但正文是错误体 = 不算支持"),
        ("000", False, None, "连接层失败 = 未能判定"),
    ):
        pc_state.clear()
        pc_state.update({"status": status, "envelope": envelope,
                         "excerpt": "unrecognized request argument"})
        v = SectionVerdict(section="openai-compatibility", usable=True,
                           base_url="https://ws.example/v1", models=["m"])
        pc._probe_prompt_cache_key(row, v)
        assert v.prompt_cache_key is want, (
            f"{why}：期望 {want}，实得 {v.prompt_cache_key}")
    assert seen[0]["body_patch"]["prompt_cache_key"], "补丁没带上那个字段"

    # ── (6) 写回：三态各自的行为 ──
    def render(sec, **kw):
        sp = SectionPlan(section=sec, base_url="https://w.example/v1",
                         api_key="k", models=["m"], priority=100, **kw)
        txt = f"{sec}:\n" + "\n".join(
            render_entry(sp, "  ", "    ", "2026-09-04")) + "\n"
        ok, msg = validate(txt)
        assert ok, msg
        return txt, yaml.safe_load(txt)[sec][0]

    t, e = render("codex-api-key", websockets=True,
                  websockets_note="实测握手返回 101")
    assert e.get("websockets") is True and "实测握手返回 101" in t

    _t, e = render("codex-api-key", websockets=False,
                   websockets_note="实测握手返回 400")
    assert "websockets" not in e, "实测不支持时不该写这个字段"

    _t, e = render("codex-api-key", websockets=False,
                   prior_toggles={"websockets": True})
    assert "websockets" not in e, (
        "实测不支持时原值 true 也不该搬 —— 那正是本次要修掉的错配置")

    t, e = render("codex-api-key", websockets=None,
                  prior_toggles={"websockets": True})
    assert e.get("websockets") is True and "原值搬运" in t, (
        "未探测时该按原值搬运（没有证据就不动）")

    _t, e = render("codex-api-key", websockets=None)
    assert "websockets" not in e

    _t, e = render("openai-compatibility", prompt_cache_key=True,
                   prompt_cache_note="实测带 prompt_cache_key 时返回 200")
    assert e.get("support-prompt-cache-key") is True
    _t, e = render("openai-compatibility", prompt_cache_key=False,
                   prior_toggles={"support-prompt-cache-key": True})
    assert "support-prompt-cache-key" not in e

    # ── (7) 原值查表：只收显式 true，键含段，compat 逐 Key 查得到 ──
    cfg_t = yaml.safe_load("""
codex-api-key:
  - api-key: "kw"
    base-url: "https://t.example/v1"
    websockets: true
  - api-key: "kf"
    base-url: "https://t.example/v1"
    websockets: false
  - api-key: "kn"
    base-url: "https://t.example/v1"
openai-compatibility:
  - name: "t"
    base-url: "https://t.example/v1"
    support-prompt-cache-key: true
    api-key-entries:
      - api-key: "c1"
      - api-key: "c2"
    models:
      - name: "m"
        alias: ""
""")
    tg = existing_toggles(cfg_t)
    assert tg[("codex-api-key", "t.example", "kw")] == {"websockets": True}
    assert ("codex-api-key", "t.example", "kf") not in tg, (
        "false 与不写在 CPA 侧等价，不必收")
    assert ("codex-api-key", "t.example", "kn") not in tg
    # compat 段这一维是 entry_scope 的 provider 身份（保留 scheme、折叠尾部
    # /v1），不是裸 host —— 见 writeback._source_identity。
    from cpa_probe.batch import entry_scope as _scope
    t_scope = _scope("openai-compatibility", "https://t.example/v1")
    for k in ("c1", "c2"):
        assert tg[("openai-compatibility", t_scope, k)] == {
            "support-prompt-cache-key": True}

    # ── (8) 不能与 carry 重复写 ──
    # 两条路都写会产出重复键：PyYAML 取后一个，而 Go 的 yaml.v3 直接报
    # `mapping key already defined` —— CPA 起不来，不是「值取谁」的小问题。
    assert "websockets" in _RENDERED_KEYS
    assert "support-prompt-cache-key" in _RENDERED_KEYS

    print("[OK] Capability toggles: 握手实测三态、需代理与段不通时记未探测、"
          "只在有该开关的段探、写回 True 才写而 False 连原值一起关、"
          "未探测按原值搬运、不与 carry 重复")



def test_compat_same_host_multi_path_isolated():
    """同一台主机按**路径**挂多个 provider 时，六张查表不能互相串。

    2026-09-04 补闸。compat 段是「一个 provider 一条条目、多 Key 挂在下面」，
    而同一主机可以按路径挂多个互不相干的上游 —— 本项目自己的
    `tools/e2e_redetect.py` 假上游正是 `127.0.0.1:PORT/good` 与 `.../gate`。

    渲染归并（`compat_provider_key`）、per-key 续行（`compat_key_blocks`）、
    孤儿保留（`_orphan_provider_lines`）三处早就用含路径的键，只有六张
    `existing_*` 查表还在按 host 索引 —— 后一个 provider 覆盖前一个，
    于是重探 `/good` 会拿到 `/gate` 的 prefix / headers / name / 窗口值。

    生产配置 compat 段同 host 多路径 0 处，所以这是潜在缺陷；但假上游脚本
    正是这个形态，端到端演练迟早会踩上。
    """
    import yaml
    from cpa_probe.batch import (entry_scope, existing_headers,
                                 existing_model_context, existing_prefixes,
                                 existing_provider_names, existing_proxies,
                                 existing_toggles, provider_name_for)

    cfg = yaml.safe_load("""
openai-compatibility:
  - name: "good"
    base-url: "http://127.0.0.1:9000/good/v1"
    prefix: "GOOD"
    support-prompt-cache-key: true
    headers:
      x-route: "good"
    api-key-entries:
      - api-key: "shared"
        proxy-url: "http://p1:1"
    models:
      - name: "m"
        alias: ""
        max-context-length: 111
  - name: "gate"
    base-url: "http://127.0.0.1:9000/gate/v1"
    prefix: "GATE"
    headers:
      x-route: "gate"
    api-key-entries:
      - api-key: "shared"
        proxy-url: "http://p2:2"
    models:
      - name: "m"
        alias: ""
        max-context-length: 222
""")
    good = "http://127.0.0.1:9000/good/v1"
    gate = "http://127.0.0.1:9000/gate/v1"
    sg, st = entry_scope("openai-compatibility", good), \
        entry_scope("openai-compatibility", gate)
    assert sg != st, f"两个 provider 的 scope 必须不同：{sg} / {st}"
    # 前三段仍按 host —— 它们的 base-url 没有路径维度
    assert entry_scope("claude-api-key", "https://x.example") == "x.example"

    K = "openai-compatibility"
    pre = existing_prefixes(cfg)
    assert pre[(K, sg, "shared")] == "GOOD" and pre[(K, st, "shared")] == "GATE"
    hdr = existing_headers(cfg)
    assert hdr[(K, sg, "shared")] == {"x-route": "good"}
    assert hdr[(K, st, "shared")] == {"x-route": "gate"}
    pxy = existing_proxies(cfg)
    assert pxy[(K, sg, "shared")] == "http://p1:1"
    assert pxy[(K, st, "shared")] == "http://p2:2"
    mctx = existing_model_context(cfg)
    assert mctx[(K, sg, "shared", "m")] == 111
    assert mctx[(K, st, "shared", "m")] == 222
    tg = existing_toggles(cfg)
    assert (K, sg, "shared") in tg, "开关只在 /good 上，不该被 /gate 覆盖掉"
    assert (K, st, "shared") not in tg
    # provider name 两级查找：精确命中含路径的键
    pn = existing_provider_names(cfg)
    assert provider_name_for(pn, good) == "good"
    assert provider_name_for(pn, gate) == "gate"
    # 新站（表里没有精确条目）按 host 回落 —— 同 host 只有一个 provider 时
    # 那就是对的；这里有两个，回落值不确定但必须是其中之一而不是空
    assert provider_name_for(pn, "http://127.0.0.1:9000/brand-new/v1") in (
        "good", "gate")
    assert provider_name_for(pn, "https://never.seen/v1") == ""

    print("[OK] Compat scope: 同 host 多路径的六张查表互不串，"
          "provider name 精确命中 + 新站按 host 回落")


def test_find_compat_provider_strips_trailing_comments():
    """`find_compat_provider` 取 base-url / name / api-key 时必须剥行尾注释。

    2026-09-04 补闸。生产 config.yaml 前三段有 86 行
    `base-url: "https://x" # 注意不带 /v1` 这种写法；同一种写法迁到 compat 段，
    原来的 `strip('"')` 会让值带着注释文本 —— 与 want 比不相等，于是「同站再来
    新 Key」会**新建**一个同 base-url 的 provider。

    CPA 的 `SanitizeOpenAICompatibility` 只丢缺 base-url 的、不去重，所以那把
    Key 会在轮询池里占两个位，而冷却 / 模型能力 / 执行路由三处按 `name` 索引，
    对同一把 Key 命中两套配置。
    """
    from cpa_probe.writeback import find_compat_provider

    for tail, why in (("", "无注释"),
                      (" # 注意带 /v1", "空格 + 注释"),
                      ("  # 直连，不走网关", "多空格 + 中文注释")):
        src = f'''openai-compatibility:
  - name: "cielo" # 短名，与 host 不同
    base-url: "https://cielo.example/v1"{tail}
    api-key-entries:
      - api-key: "sk-a" # 2026-08-20 新增
      - api-key: "sk-b"
    models:
      - name: "m"
        alias: ""
'''
        got = find_compat_provider(src.splitlines(keepends=True),
                                   "https://cielo.example/v1")
        assert got is not None, f"{why}：没命中现有 provider —— 会新建重复条目"
        assert got["name"] == "cielo", f"{why}：name 带上了注释 {got['name']!r}"
        assert got["existing_keys"] == ["sk-a", "sk-b"], (
            f"{why}：api-key 带上了注释 {got['existing_keys']}")

    print("[OK] find_compat_provider: base-url / name / api-key 都剥行尾注释，"
          "同站新 Key 追加进现有 provider 而不新建重复条目")


def test_section_span_recognizes_odd_top_level_keys():
    """段边界要认含点号与引号的顶层键，否则那个键会被 carry 吞掉。

    2026-09-04 补闸。原来的判据是 `^[a-zA-Z_][a-zA-Z0-9_-]*\\s*:` —— 认不出
    `a.b: 42` 与 `"my key": 42`。那时 `_section_span` 的 end 落在更后面，
    那个顶层键被划进 span，`extract_carry_lines` 把它当成条目的 carry 行收走，
    重建后它从**顶层消失**。

    CPA 的 46 个顶层 yaml tag 全是合法标识符，两份生产文件的 42 个顶层键也全
    合法，所以这是补闸而不是修事故 —— 但「零缩进且不是列表项」本来就是 YAML
    顶层键的完整判据，正则那一版只是它的子集。
    """
    import yaml
    from cpa_probe.writeback import (_TOP_LEVEL_KEY, extract_carry_lines,
                                     rebuild_config_full, validate)
    from cpa_probe.plan import ImportPlan, SectionPlan

    # 判据本身
    for line, want in (
        ('host: "127.0.0.1"', True), ("claude-api-key:", True),
        ("openai-compatibility: []", True),
        ("a.b: 42", True), ('"my key": 42', True), ("'q': 1", True),
        ('  - api-key: "k"', False), ("- top: 1", False),
        ("# comment", False), ("    priority: 900", False),
        ("", False), ("   ", False), ("plain-text-no-colon", False),
    ):
        assert bool(_TOP_LEVEL_KEY.match(line)) is want, (line, want)

    for weird in ("a.b: 42", '"my key": 42'):
        src = ('host: "x"\n\nclaude-api-key:\n'
               '  - api-key: "k1"\n    base-url: "https://a.example"\n'
               '    priority: 900\n    excluded-models: ["*"]\n'
               '    models:\n      - name: "m"\n        alias: ""\n'
               f'{weird}\napi-keys:\n  - sk-client\n')
        cfg = yaml.safe_load(src)
        lines = src.splitlines(keepends=True)
        # 那个键不该被当成条目的 carry 行
        carry = extract_carry_lines(lines)
        blob = "".join(v for d in carry.values() for v in
                       (x for lst in d.values() for x in lst))
        assert weird.split(":")[0] not in blob, (
            f"{weird!r} 被 carry 收走了：{blob[:200]}")
        # 重建后它必须还在顶层
        p = ImportPlan(host="a.example", masked_key="k1", line_no=1)
        p.sections["claude-api-key"] = SectionPlan(
            section="claude-api-key", base_url="https://a.example",
            api_key="k1", models=["m"], priority=900, model_source="probed")
        new, _w = rebuild_config_full(
            cfg, {("https://a.example", "k1"): p}, lines)
        assert validate(new)[0]
        got = yaml.safe_load(new)
        key = weird.split(":")[0].strip().strip('"')
        assert key in got, f"{weird!r} 重建后从顶层消失了"
        assert "api-keys" in got, "排在它之后的顶层键也跟着丢了"

    print("[OK] Section span: 含点号与引号的顶层键不再被 carry 吞掉")


def test_yaml_field_escapes_subkeys_and_skips_odd_floats():
    """`_yaml_field` 的子键要转义，非有限 / 科学计数的 float 整个跳过。

    2026-09-04 补闸。两处都会产出「YAML 合法但语义漂移」的结果，而
    `validate()` 只看语法：
      · 子键含冒号（`{'a: b': 1}`）→ 写成 `a: b: 1`，非法 YAML
      · `1e20` → 写成 `1e+20`，YAML 读回是**字符串** `'1e+20'`
      · `inf` / `nan` → 同样漂成字符串

    CPA 的模型级字段里没有 float（全是 int / bool / []string），所以这两支只在
    原文件手工写了这些形状时才会走到 —— 与「认不出的形状整个跳过」同一条原则。
    """
    import math
    import yaml
    from cpa_probe.writeback import _yaml_field

    def roundtrip(key, val):
        """渲染再读回，返回**那个键的值**。认不出的形状返回 None。"""
        rows = _yaml_field("  ", key, val)
        if not rows:
            return None
        txt = "root:\n" + "\n".join(rows) + "\n"
        got = yaml.safe_load(txt)["root"]
        assert isinstance(got, dict) and len(got) == 1, got
        return next(iter(got.values()))

    # 子键含冒号 / 是 YAML 关键字 —— 都要能原样读回
    assert roundtrip("thinking", {"a: b": 1}) == {"a: b": 1}
    assert roundtrip("thinking", {"null": 1}) == {"null": 1}
    assert roundtrip("thinking", {"true": 1}) == {"true": 1}
    # 键本身含冒号 —— 读回时键必须还是那个字符串
    _rows = _yaml_field("  ", "a: b", 1)
    assert yaml.safe_load("root:\n" + "\n".join(_rows) + "\n")["root"] == {
        "a: b": 1}
    # 正常形状照旧
    assert roundtrip("thinking", {"levels": ["low", "high"], "min": 1}) == {
        "levels": ["low", "high"], "min": 1}
    assert roundtrip("ratio", 1.5) == 1.5
    assert roundtrip("mods", ["text", "image"]) == ["text", "image"]
    assert roundtrip("x", []) == []
    assert roundtrip("x", {}) == {}
    # 会漂移的 float：整个跳过
    for bad in (1e20, float("inf"), float("-inf"), float("nan")):
        assert _yaml_field("  ", "ratio", bad) == [], repr(bad)
    assert _yaml_field("  ", "nums", [1.0, 1e20]) == []
    assert math.isfinite(1.5)   # 正常值不受影响，见上

    print("[OK] _yaml_field: 子键与键都转义，非有限与科学计数的 float 整个跳过")


def test_carry_tables_are_wired_into_writeback_path():
    """六张 `existing_*` 查表必须真的被写回路径**调用**，不只是存在。

    2026-09-04 撤销实验发现的缺口：把 `server.py` 里搬 headers 那一行改成
    `old = None`（即「不搬原值」），1198 项测试**全绿**、两份真实配置的演练也
    全对上 —— 因为演练脚本自己也搬 headers（`build_plans` 里那几行），
    于是它只守住了 `writeback` 那一层，守不住「server 有没有调查表」。

    这类缺陷的形态是「函数写对了，但没人调用它」。`assign_priorities` 曾只在
    网页端调、`_clean_override_models` 曾只在重探路过滤，都是同一形态。
    所以这里按源码断言调用点，与 `test_three_paths_share_the_gates` 同一套做法。
    """
    import ast as _ast
    import io as _io

    # ── 结构：八张表在 CarryTables 里建，两条产品路径都调它 ──
    #
    # 2026-09-04 把搬运逻辑从 _api_plan 的循环里抽进 CarryTables，所以结构断言
    # 也跟着换位置：以前断言「_api_plan 调了 existing_*」，现在断言
    # 「CarryTables 建了八张表 + 两条路都调 carry.apply」。
    bsrc = _io.open(os.path.join(ROOT, "cpa_probe", "batch.py"),
                    encoding="utf-8").read()
    btree = _ast.parse(bsrc)
    cls = next((n for n in _ast.walk(btree)
                if isinstance(n, _ast.ClassDef) and n.name == "CarryTables"),
               None)
    assert cls is not None, "batch.py 里找不到 CarryTables"
    cls_called = set()
    for n in _ast.walk(cls):
        if isinstance(n, _ast.Call):
            nm = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
            if nm:
                cls_called.add(nm)

    WANT = {
        "existing_weights": "weight: 0 是逐出调度池的唯一表达",
        "existing_proxies": "必须走代理的站会改成直连",
        "existing_prefixes": "ANT/xxx 这半边别名全失效",
        "existing_provider_names": "改名作废 CPA 的冷却状态与能力缓存",
        "existing_headers": "整字段消失：实测 24/24 与 66/66 条目",
        "existing_toggles": "原来开着的 websockets 被抹掉",
        "existing_model_context": "客户端按偏大的窗口定压缩点",
        "existing_model_extras": "手工加的模型级字段被抹掉",
    }
    missing = {k: why for k, why in WANT.items() if k not in cls_called}
    assert not missing, (
        "CarryTables 没建这些表 —— 对应字段会在整段重写时消失：\n"
        + "\n".join(f"  {k}: {why}" for k, why in missing.items()))

    # apply 必须把每一类都赋回 SectionPlan —— 只建表不赋值等于没搬
    apply_fn = next((n for n in cls.body
                     if isinstance(n, _ast.FunctionDef) and n.name == "apply"),
                    None)
    assert apply_fn is not None, "CarryTables 没有 apply"
    assigned = set()
    for n in _ast.walk(apply_fn):
        if isinstance(n, _ast.Assign):
            for tgt in n.targets:
                if isinstance(tgt, _ast.Attribute):
                    assigned.add(tgt.attr)
    for attr, why in (("headers", "headers 搬了却没赋给 sp"),
                      ("prior_toggles", "能力开关原值没赋给 sp"),
                      ("prefix", "prefix 没赋给 sp"),
                      ("provider_name", "compat 的 name 没赋给 sp"),
                      ("prior_context", "模型级窗口值没赋给 sp"),
                      ("prior_model_extras", "模型级白名单外字段没赋给 sp"),
                      ("weight", "weight 没赋给 sp"),
                      ("proxy_url", "proxy-url 没赋给 sp")):
        assert attr in assigned, why

    # 产品的写回路径与演练脚本都必须调它 —— 任一侧自己抄一遍就会分叉，
    # 而分叉的后果实测过：演练自己搬 headers，于是「server 不搬」照样对上账。
    for rel, who in (("server.py", "网页写回路径"),
                     (os.path.join("tests", "rehearse_real_rebuild.py"),
                      "真实文件演练")):
        src2 = _io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert "CarryTables(" in src2 and "carry.apply(" in src2, (
            f"{who}（{rel}）没走 CarryTables —— 两侧逻辑分叉，"
            f"演练对上账不等于产品对")

    # 源码结构断言挡不住「调了、赋了，但赋的是空」——
    # 把 `old = hdrs.get(...)` 改成 `old = None` 时上面那些断言全过。
    # 所以再加一层**行为**断言：直接跑 _api_plan 里那段搬运逻辑的等价形态，
    # 比对方案里的字段与原配置。
    #
    # 为什么不真起 HTTP 服务跑一遍：那要假上游 + 完整任务编排，而这里要验的
    # 只有「查表结果流进方案」这一件事。等价形态由 rehearse_real_rebuild 的
    # build_plans 提供 —— 它就是照 server 那条路写的，且被上面那条断言
    # 要求「用同一批查表」。
    import sys as _sys
    if os.path.join(ROOT, "tests") not in _sys.path:
        _sys.path.insert(0, os.path.join(ROOT, "tests"))
    import yaml as _yaml
    from rehearse_real_rebuild import build_plans as _build_plans

    cfg_w = _yaml.safe_load("""
claude-api-key:
  - api-key: "kh"
    base-url: "https://carry.example"
    prefix: "ANT"
    priority: 700
    weight: 0
    proxy-url: "http://mihomo:7890"
    headers:
      anthropic-beta: "context-1m-2025-08-07"
      user-agent: "claude-cli/2.1.220"
    models:
      - name: "claude-opus-5"
        alias: "opus"
        max-context-length: 262144
codex-api-key:
  - api-key: "kw"
    base-url: "https://carry.example/v1"
    priority: 300
    websockets: true
    models:
      - name: "gpt-5.6-sol"
        alias: ""
openai-compatibility:
  - name: "carry-short-name"
    base-url: "https://carry.example/v1"
    prefix: "CHMA"
    priority: 100
    support-prompt-cache-key: true
    api-key-entries:
      - api-key: "kc"
    models:
      - name: "claude-opus-5"
        alias: ""
""")
    plans_w = _build_plans(cfg_w)
    got = {}
    for pl in plans_w.values():
        for sec, sp in pl.sections.items():
            got[(sec, sp.api_key)] = sp

    cl = got[("claude-api-key", "kh")]
    assert cl.headers == {"anthropic-beta": "context-1m-2025-08-07",
                          "user-agent": "claude-cli/2.1.220"}, (
        f"headers 没流进方案：{cl.headers}")
    assert cl.weight == 0, f"weight 没流进方案：{cl.weight}"
    assert cl.proxy_url == "http://mihomo:7890", cl.proxy_url
    assert cl.prefix == "ANT", cl.prefix
    assert cl.prior_context == {"claude-opus-5": 262144}, cl.prior_context
    assert (cl.prior_model_extras.get("claude-opus-5") or {}).get(
        "alias") == "opus", cl.prior_model_extras

    cx = got[("codex-api-key", "kw")]
    assert cx.prior_toggles == {"websockets": True}, (
        f"能力开关原值没流进方案：{cx.prior_toggles}")

    cp_ = got[("openai-compatibility", "kc")]
    assert cp_.provider_name == "carry-short-name", (
        f"provider name 没流进方案（会被现编成 host）：{cp_.provider_name}")
    assert cp_.prior_toggles == {"support-prompt-cache-key": True}, (
        cp_.prior_toggles)
    assert cp_.prefix == "CHMA", cp_.prefix

    print("[OK] Carry wiring: CarryTables 建齐八张表并赋回方案、"
          "值真的流进方案（headers / weight / proxy / prefix / name / "
          "窗口 / alias / 能力开关）、产品与演练同走 carry.apply")



def test_merge_entry_headers_behaviour():
    """headers 合并的行为断言。抽成函数之后才测得动。

    2026-09-04 撤销实验的最后一个缺口：把 `server.py` 里 `old = hdrs.get(...)`
    改成 `old = None`（即「不搬原值」），1198 项测试**全绿**。根因是那段合并逻辑
    内联在 `_api_plan` 的循环里 —— 只能靠源码结构断言「这几行在不在」，
    而结构断言挡不住「行还在，赋的是空」。

    抽成 `writeback.merge_entry_headers` 之后就能直接测行为。这一项守四件事：
      ① 原值里探测没提到的键**保留**（能力 beta 就是这样丢的）
      ② 探测值覆盖同名键（它是本次实测出来的最省必需集）
      ③ `anthropic-beta` 走 `betas.merge` 合并而不是覆盖（它是集合，覆盖会丢项）
      ④ 头名大小写不敏感匹配，但保留原条目的写法（改大小写会污染 diff）
    """
    from cpa_probe.writeback import merge_entry_headers as M

    # ① 原值独有的键保留 —— 这正是 alfa.example 丢 1m 上下文的形态
    got = M({"anthropic-beta": "context-1m-2025-08-07"}, {})
    assert got == {"anthropic-beta": "context-1m-2025-08-07"}, (
        f"探测为空时原值必须整份保留，实得 {got}")

    # 原值为空、探测有值 —— 新条目的情形
    assert M(None, {"user-agent": "cc/1"}) == {"user-agent": "cc/1"}
    assert M({}, {"user-agent": "cc/1"}) == {"user-agent": "cc/1"}
    assert M(None, None) == {}

    # ② 同名键以探测值为准，原值里的其他键保留
    got = M({"user-agent": "old/1", "x-site-token": "keep-me"},
            {"user-agent": "claude-cli/2.1.220"})
    assert got == {"user-agent": "claude-cli/2.1.220",
                   "x-site-token": "keep-me"}, got

    # ③ anthropic-beta 合并：两边的项都要在，且不重复
    got = M({"anthropic-beta": "context-1m-2025-08-07,oauth-2025-04-20"},
            {"anthropic-beta": "claude-code-20250219,context-1m-2025-08-07"})
    items = [x.strip() for x in got["anthropic-beta"].split(",")]
    assert set(items) == {"context-1m-2025-08-07", "oauth-2025-04-20",
                          "claude-code-20250219"}, items
    assert len(items) == len(set(items)), f"有重复项：{items}"

    # ④ 大小写：匹配不敏感，但保留原条目的写法
    got = M({"Anthropic-Beta": "context-1m-2025-08-07", "User-Agent": "old/1"},
            {"anthropic-beta": "claude-code-20250219",
             "user-agent": "claude-cli/2.1.220"})
    assert "Anthropic-Beta" in got and "anthropic-beta" not in got, (
        f"改了原条目的大小写写法，会在 diff 里多出无意义改动：{sorted(got)}")
    assert got["User-Agent"] == "claude-cli/2.1.220", got
    beta_items = {x.strip() for x in got["Anthropic-Beta"].split(",")}
    assert beta_items == {"context-1m-2025-08-07", "claude-code-20250219"}, (
        beta_items)

    # 原值的 anthropic-beta 是空串时不该走合并（合并空串会产出前导逗号）
    got = M({"anthropic-beta": ""}, {"anthropic-beta": "claude-code-20250219"})
    assert got == {"anthropic-beta": "claude-code-20250219"}, got

    # 不改入参（server 那条路会把 old 复用给别的条目）
    old_in = {"user-agent": "old/1"}
    probed_in = {"user-agent": "new/1"}
    M(old_in, probed_in)
    assert old_in == {"user-agent": "old/1"}, "改了入参 old"
    assert probed_in == {"user-agent": "new/1"}, "改了入参 probed"

    # 搬运路径必须走这个函数。两条产品路径（网页写回、真实文件演练）现在都调
    # CarryTables.apply，所以断言点在那里 —— 它自己写一套合并就会分叉。
    import io as _io
    bsrc = _io.open(os.path.join(ROOT, "cpa_probe", "batch.py"),
                    encoding="utf-8").read()
    assert "merge_entry_headers" in bsrc, (
        "CarryTables 没用 merge_entry_headers —— headers 会被整份替换，"
        "原条目里手工配的能力 beta 静默消失")

    print("[OK] Headers merge: 原值独有键保留、同名以实测为准、"
          "anthropic-beta 合并去重、大小写不敏感但保留原写法、不改入参、"
          "CarryTables 走同一个函数")

def test_capability_probe_is_actually_invoked():
    """能力探测必须真的被 `_full_probe` 调用。

    2026-09-04 撤销实验发现的缺口：把 `_stage5_capabilities` 的第一行改成
    `if True: return`（即「永不探测」），1198 项全绿 —— 已有的用例都直接调
    `_probe_websockets` / `_probe_prompt_cache_key`，没有一条断言那两个
    子步骤会被编排调用。

    与 `_stage4_context` 同一形态：那一步也曾漏在编排之外（那次是加了新阶段
    却忘了接线）。所以这里断言编排链本身。
    """
    from cpa_probe.pipeline import Attempt, SectionVerdict

    prober = Prober(gap=0.0, probe_context=False, swap_samples=0,
                    probe_capabilities=True)
    row = cp.parse_lines("https://wire.example,sk-wire").valid[0]

    seen = []

    def fake_stage1(_row, section):
        v = SectionVerdict(section=section, usable=True,
                           base_url=f"https://wire.example",
                           models=["m"])
        return v

    prober._stage1 = fake_stage1                     # type: ignore[assignment]
    prober._stage2 = lambda *a, **k: seen.append("stage2")   # type: ignore
    prober._stage4_swap = lambda *a, **k: seen.append("swap")  # type: ignore
    prober._stage4_context = lambda *a, **k: seen.append("ctx")  # type: ignore
    prober._stage5_capabilities = lambda *a, **k: seen.append("caps")  # type: ignore

    prober._full_probe(row, "codex-api-key")
    assert "caps" in seen, (
        f"_full_probe 没调 _stage5_capabilities —— 能力开关永远是「未探测」，"
        f"实际调用序列 {seen}")
    # 顺序：它要用到 v.models（compat 那一支）与 v.min_headers（WS 握手带门票），
    # 两者在前面几步才定下来，所以必须排在最后
    assert seen.index("caps") == len(seen) - 1, (
        f"能力探测必须排在最后 —— 它依赖前面几步的产物，实际 {seen}")

    # 段不通时整条链都不该往下走
    seen.clear()
    prober._stage1 = lambda _r, s: SectionVerdict(  # type: ignore[assignment]
        section=s, base_url="https://wire.example", usable=False)
    prober._full_probe(row, "codex-api-key")
    assert seen == [], f"段不通时不该跑后续阶段，实际 {seen}"

    # 上面那一轮只验「被调用」。`if True: return` 会让它变成空操作而照样被调 ——
    # 所以再验「调用之后 verdict 真的变了」。
    from cpa_probe import client as _client

    real = Prober(gap=0.0, probe_context=False, swap_samples=0,
                  probe_capabilities=True)
    ws_orig = _client.ws_handshake
    _client.ws_handshake = lambda url, *, headers=None, timeout=10: (
        _client.Response("101", "", 5, ""))
    try:
        vv = SectionVerdict(section="codex-api-key", usable=True,
                            base_url="https://wire.example/v1",
                            models=["gpt-5.6-sol"])
        real._stage5_capabilities(row, vv)
        assert vv.websockets is True, (
            f"_stage5_capabilities 是空操作 —— 握手回 101 也没写进 verdict，"
            f"实得 {vv.websockets}（note={vv.websockets_note!r}）")
        assert vv.websockets_note, "结论必须带实测依据"
        assert any(a.combo == "ws-upgrade" for a in vv.attempts), (
            "握手那次没记进 attempts —— 导出日志与界面都看不到它")
    finally:
        _client.ws_handshake = ws_orig

    # compat 那一支同理
    real2 = Prober(gap=0.0, probe_context=False, swap_samples=0,
                   probe_capabilities=True)
    real2._call = lambda section, base, key, model, **kw: Attempt(  # type: ignore
        section=section, model=model, combo=kw.get("combo", ""), status="200",
        category="", action="", elapsed_ms=2, response_valid=True)
    vc = SectionVerdict(section="openai-compatibility", usable=True,
                        base_url="https://wire.example/v1", models=["m"])
    real2._stage5_capabilities(row, vc)
    assert vc.prompt_cache_key is True, (
        f"compat 那一支是空操作，实得 {vc.prompt_cache_key}")

    print("[OK] Capability wiring: _full_probe 调 _stage5_capabilities 且排在最后、"
          "调用后结论真的写进 verdict 并记进 attempts、段不通时整条链不走")


def test_flow_style_section_head_rebuilds():
    """段头是 flow 序列时，全量重建也要产出合法 YAML。

    2026-09-05 修。`_empty_literal_rewrite` 只认 `[]` 与 `{}`，而
    `claude-api-key: [{api-key: "k1", ...}]` 是**合法 YAML、CPA 读得出来**，
    重建却把块序列挂在它后面：

        claude-api-key: [{api-key: "k1", ...}]
          - api-key: "k1"                       ← 非法

    `validate()` 挡住了（不会写坏文件），所以症状不是「写坏配置」而是
    「全量重探对这类文件整个不可用」，且报错是
    `while parsing a block mapping` —— 看不出根因是段头形态。

    跨行 flow（`claude-api-key: [` 换行再列条目）单独覆盖：首行的 `[` 之后
    什么都没有，按「`[` 后面必须有内容」判会漏掉它。闭合位置按括号计数找，
    引号内的括号不计（`base-url: "https://x/[a]"`）。
    """
    import yaml as _yaml
    from cpa_probe.plan import ImportPlan, SectionPlan
    from cpa_probe.writeback import (rebuild_config_full, validate,
                                     _FLOW_SECTION_HEAD, _flow_section_span)

    # 判据本身：空 flow 与空 map 不能命中（那两个由 _empty_literal_rewrite 管，
    # 两条路都命中会重复改写）
    for line, want in (("claude-api-key: [\n", True),
                       ("claude-api-key: [{a: 1}]\n", True),
                       ("claude-api-key: []\n", False),
                       ("claude-api-key: []  # 待填\n", False),
                       ("claude-api-key: {}\n", False),
                       ("claude-api-key:\n", False)):
        assert bool(_FLOW_SECTION_HEAD.match(line)) is want, (line, want)

    # 括号计数：引号里的方括号不算。
    #
    # 样本要用引号里**未配对的开括号**（`"https://x/[a"`）—— 那时不看引号的
    # 实现会 depth 永不归零、跑到文件末尾返回 None，段头就不被识别。
    # 用未配对的**闭**括号（`"…/]a"`）测不出来：`[` `{` `]`(引号内) `}`
    # 恰好在同一行归零，答案碰巧对（2026-09-05 撤销实验证实过这一点）。
    lines = ('claude-api-key: [{api-key: "k", base-url: "https://x/[a"}]\n'
             'other: 1\n').splitlines(True)
    assert _flow_section_span(lines, 0) == 1, (
        f"引号里的 [ 被当成嵌套了：span={_flow_section_span(lines, 0)}")
    # 未配对的闭括号也要正确（靠同一份实现顺带覆盖）
    lines_c = ('claude-api-key: [{api-key: "k", base-url: "https://x/]a"}]\n'
               'other: 1\n').splitlines(True)
    assert _flow_section_span(lines_c, 0) == 1, _flow_section_span(lines_c, 0)
    # 反向：引号外的嵌套要算对
    lines2 = ('claude-api-key: [{a: [1, 2]}, {b: 3}]\n'
              'other: 1\n').splitlines(True)
    assert _flow_section_span(lines2, 0) == 1, _flow_section_span(lines2, 0)

    # 零缩进的 flow 内容：`_section_span` 会在第一行内容处就收尾（那些行零缩进、
    # 含冒号，看着像顶层键），而 flow 实际延伸到 `]`。cursor 取 `end` 而不是
    # `max(end, flow_end)` 的话，剩下的 flow 行会被当成「段之后的内容」原样
    # 输出 —— 产出重复条目 + 悬挂的 `]`。
    zero_indent = ('other: 1\nclaude-api-key: [\n'
                   '{api-key: "k1", base-url: "https://a.example", '
                   'priority: 900},\n'
                   '{api-key: "k2", base-url: "https://b.example", '
                   'priority: 800}\n]\napi-keys:\n  - sk-c\n')
    cfg_z = _yaml.safe_load(zero_indent)
    p_z = ImportPlan(host="a.example", masked_key="k1", line_no=1)
    p_z.sections["claude-api-key"] = SectionPlan(
        section="claude-api-key", base_url="https://a.example", api_key="k1",
        models=["claude-opus-5"], priority=900, model_source="probed")
    new_z, _wz = rebuild_config_full(
        cfg_z, {("https://a.example", "k1"): p_z},
        zero_indent.splitlines(True))
    ok_z, msg_z = validate(new_z)
    assert ok_z, f"零缩进 flow 内容：产出非法 YAML —— {msg_z[:100]}"
    assert "{api-key:" not in new_z, (
        f"flow 内容行被重复输出了：\n{new_z[:300]}")
    assert not any(ln.strip() == "]" for ln in new_z.splitlines()), (
        f"悬挂的 ] 没被吃掉：\n{new_z[:300]}")
    got_z = _yaml.safe_load(new_z)
    # 本次方案只覆盖 k1，k2 是留守条目 —— 必须原样还在（2026-09-12 改）。
    #
    # 这一行原来断言「只剩 1 条」，那钉住的是**删除留守条目**的旧行为，
    # 与 keep_unplanned 的既定契约正相反：删除只该由操作员显式操作，不该是
    # 「没勾」的副作用（2026-09-02 生产事故，13 个 provider 被删到剩 1 个，
    # 见 render_section 里 keep_unplanned 那一段）。本用例真正要守的是
    # 「flow 段头能重建成合法 YAML、不重复输出 flow 内容行、不留悬挂的 ]」
    # —— 那三条断言在上面，与条目数无关。
    got_entries = {e.get("api-key"): e for e in got_z.get("claude-api-key") or []}
    assert set(got_entries) == {"k1", "k2"}, got_z
    assert got_entries["k2"]["base-url"] == "https://b.example", got_z
    # k2 是**另一个站**，不参与同站档位对齐 —— 原值 800 照旧
    assert got_entries["k2"]["priority"] == 800, got_z

    CASES = {
        "单行 flow":
            'other: 1\nclaude-api-key: [{api-key: "k1", '
            'base-url: "https://a.example", priority: 900}]\n'
            'api-keys:\n  - sk-c\n',
        "跨行 flow":
            'other: 1\nclaude-api-key: [\n  {api-key: "k1", '
            'base-url: "https://a.example", priority: 900}\n]\n'
            'api-keys:\n  - sk-c\n',
        "跨行 flow 多条":
            'other: 1\nclaude-api-key: [\n  {api-key: "k1", '
            'base-url: "https://a.example", priority: 900},\n'
            '  {api-key: "k2", base-url: "https://b.example", '
            'priority: 800}\n]\napi-keys:\n  - sk-c\n',
        "flow 值里含方括号":
            'other: 1\nclaude-api-key: [{api-key: "k1", '
            'base-url: "https://a.example/[x]", priority: 900}]\n'
            'api-keys:\n  - sk-c\n',
        # 回归：原来就支持的三种形态不能被弄坏
        "空 flow（回归）":
            'other: 1\nclaude-api-key: []\napi-keys:\n  - sk-c\n',
        "空 flow 带注释（回归）":
            'other: 1\nclaude-api-key: []  # 待填\napi-keys:\n  - sk-c\n',
        "空 map（回归）":
            'other: 1\nclaude-api-key: {}\napi-keys:\n  - sk-c\n',
        "正常块序列（回归）":
            'other: 1\nclaude-api-key:\n  - api-key: "k1"\n'
            '    base-url: "https://a.example"\n    priority: 900\n'
            'api-keys:\n  - sk-c\n',
    }
    for why, raw in CASES.items():
        cfg = _yaml.safe_load(raw)
        # 方案的 base-url 取原文里 k1 那条自己的写法。
        #
        # 不能写死成 `https://a.example`（2026-09-12 改）：「flow 值里含方括号」
        # 那一例原文写的是 `https://a.example/[x]`，两者是**不同的上游身份**
        # （路径不同，见 writeback._source_identity）。写死会让 k1 既作为
        # 方案条目写一遍、又作为留守条目留一遍 —— 同一把 Key 在 CPA 的
        # 轮询池里占两个位。用例本身要测的是段头形态，不是跨路径归并。
        rows0 = cfg.get("claude-api-key") or []
        base0 = next((str(e.get("base-url")) for e in rows0
                      if e.get("api-key") == "k1"), "https://a.example")
        p = ImportPlan(host="a.example", masked_key="k1", line_no=1)
        p.sections["claude-api-key"] = SectionPlan(
            section="claude-api-key", base_url=base0,
            api_key="k1", models=["claude-opus-5"], priority=900,
            model_source="probed")
        new, _w = rebuild_config_full(
            cfg, {(base0, "k1"): p}, raw.splitlines(True))
        ok, msg = validate(new)
        assert ok, f"{why}：产出非法 YAML —— {msg[:120]}"
        got = _yaml.safe_load(new)
        # 方案覆盖 k1；原文里的其他条目是留守条目，原样保留（keep_unplanned，
        # 见 render_section 那一段）。所以判据是「k1 在且只有一条 k1」，
        # 不是「整段只剩一条」—— 后者钉的是删留守条目的旧行为。
        rows = got.get("claude-api-key") or []
        k1_rows = [e for e in rows if e.get("api-key") == "k1"]
        assert len(k1_rows) == 1, (
            f"{why}：k1 条目数不对 —— {rows}")
        orig_keys = {e.get("api-key")
                     for e in (_yaml.safe_load(raw).get("claude-api-key") or [])}
        assert {e.get("api-key") for e in rows} == (orig_keys | {"k1"}), (
            f"{why}：留守条目丢了或多出条目 —— {rows}")
        assert "other" in got and "api-keys" in got, (
            f"{why}：其他顶层键丢了 —— {sorted(got)}")
        # 段头不能还留着 flow 的残骸
        assert "[{" not in new.split("api-keys")[0], (
            f"{why}：flow 残骸还在，会产出重复条目")

    print(f"[OK] Flow section head: {len(CASES)} 种段头形态（含跨行与"
          "引号内方括号）都产出合法 YAML，空 flow / 空 map / 块序列不受影响")


def test_model_level_capability_fields_are_carried():
    """模型级字段（含 CPA 新加的 is-compat / thinking）必须被搬运。

    2026-09-05（契约对齐审计发现的分类缺口）。`models[].is-compat` 按 CPA 的
    文档确实是**上游能力**（`config_types.go:539-544`：给「不接受原生
    agent_message 或空签名 thinking 块的第三方 Responses 端点」用），
    而本项目原来把它归进「手工加的模型级字段」——归类的**理由**写错了。

    但结论仍然是「只搬不探」，判据换成代价与收益之比：

      · 探它要构造 MultiAgentV2 的 agent_message 请求体
      · 而它只在 `codex.optimize-multi-agent-v2` 也为 true 时生效
        （同一段 CPA 注释写明的），生产配置里那个是 **false**

    探一个当前配置下不生效的字段，成本真实、收益为零。

    这一项守两件事：搬运真的覆盖这些字段；以及**不探的理由被钉住** ——
    那个理由是配置决定的（`optimize-multi-agent-v2: false`），配置一改，
    这条断言会提醒重新评估。
    """
    import io as _io
    import yaml as _yaml

    from cpa_probe.batch import existing_model_extras

    cfg = _yaml.safe_load("""
codex-api-key:
  - api-key: "k"
    base-url: "https://a.example/v1"
    priority: 500
    models:
      - name: "gpt-5.6-sol"
        alias: ""
        is-compat: true
        display-name: "Sol"
        force-mapping: true
        thinking:
          levels: ["low", "high"]
          zero-allowed: false
""")
    got = existing_model_extras(cfg)
    key = ("codex-api-key", "a.example", "k", "gpt-5.6-sol")
    assert key in got, f"模型级字段没被搬：{list(got)}"
    extras = got[key]
    for field, want in (("is-compat", True),
                        ("display-name", "Sol"),
                        ("force-mapping", True)):
        assert extras.get(field) == want, (
            f"{field} 没搬到（实得 {extras.get(field)!r}）—— "
            f"整段重写会把它抹掉")
    # thinking 是嵌套 dict，_yaml_field 的 dict 分支要递归保真
    assert extras.get("thinking") == {"levels": ["low", "high"],
                                      "zero-allowed": False}, extras.get(
        "thinking")

    # 写回一轮，确认这些字段真的落回文件
    from cpa_probe.plan import ImportPlan, SectionPlan
    from cpa_probe.writeback import rebuild_config_full, validate

    raw = ('codex-api-key:\n'
           '  - api-key: "k"\n'
           '    base-url: "https://a.example/v1"\n'
           '    priority: 500\n'
           '    models:\n'
           '      - name: "gpt-5.6-sol"\n'
           '        alias: ""\n'
           '        is-compat: true\n'
           '        thinking:\n'
           '          levels: ["low", "high"]\n')
    cfg2 = _yaml.safe_load(raw)
    p = ImportPlan(host="a.example", masked_key="k", line_no=1)
    sp = SectionPlan(section="codex-api-key", base_url="https://a.example/v1",
                     api_key="k", models=["gpt-5.6-sol"], priority=500,
                     model_source="probed")
    sp.prior_model_extras = {
        "gpt-5.6-sol": dict(existing_model_extras(cfg2)[
            ("codex-api-key", "a.example", "k", "gpt-5.6-sol")])}
    p.sections["codex-api-key"] = sp
    new, _w = rebuild_config_full(
        cfg2, {("https://a.example", "k"): p}, raw.splitlines(True))
    ok, msg = validate(new)
    assert ok, f"重建后非法 YAML：{msg[:100]}"
    back = _yaml.safe_load(new)["codex-api-key"][0]["models"][0]
    assert back.get("is-compat") is True, f"is-compat 落盘丢了：{back}"
    assert back.get("thinking") == {"levels": ["low", "high"]}, back

    # ── 不探的理由被钉住 ──
    #
    # 生产配置的 `codex.optimize-multi-agent-v2` 为 false，所以 is-compat
    # 当前不生效 —— 那正是「不探」的判据。它一旦改成 true，这条断言会红，
    # 提醒重新评估要不要探。
    import os as _os

    prod = "C:/Users/devin/OneDrive/Desktop/fsdownload/config.yaml"
    if _os.path.isfile(prod):
        pcfg = _yaml.safe_load(_io.open(prod, encoding="utf-8").read()) or {}
        codex_cfg = pcfg.get("codex") or {}
        assert codex_cfg.get("optimize-multi-agent-v2") is not True, (
            "生产配置把 codex.optimize-multi-agent-v2 打开了 —— "
            "`models[].is-compat` 现在会生效，「不探它」的理由不再成立，"
            "要重新评估（见 README 的能力开关表）")

    print("[OK] Model-level fields: is-compat / thinking / display-name / "
          "force-mapping 都被搬运并落盘；不探 is-compat 的前置条件已钉住")

def test_context_limit_lower_bound():
    """截断反推出的荒谬小值不许写进 config.yaml。

    2026-09-02 现场（123.txt 日志）：一个 compat 站三个条目都拿到
    `上下文上限 10 · 截断反推`。CPA 把它直接当 context_window /
    max_context_window 报给客户端（internal/client/codex/models/models.go:
    206-211），10 个 token 的窗口 = 那个站每一次请求都立刻超限。

    根因：`tok < chars * 0.5` 判成「截断」后就把 tok 当真实容量，而上游回
    `input_tokens: 10`（根本没统计）时，10 就成了「实测容量」。
    """
    from cpa_probe import pipeline as pl
    from cpa_probe.pipeline import Attempt, SectionVerdict

    assert pl._MIN_TRUSTED_CONTEXT >= 8000, (
        "阈值太低挡不住 input_tokens: 10 这类")
    # 也不能太高 —— 会误伤真实的小窗口站。现役最小的也有 200k。
    assert pl._MIN_TRUSTED_CONTEXT <= 100_000

    prober = Prober(gap=0.0, probe_context=True, swap_samples=0)
    row = cp.parse_lines("https://ctx.example,sk-ctx").valid[0]

    state = {"tokens": 10}

    def fake_call(section, base, key, model, **kw):
        att = Attempt(section=section, model=model, combo=kw.get("combo", ""),
                      status="200", category="可用", action="", elapsed_ms=1,
                      input_tokens=state["tokens"],
                      sent_chars=len(kw.get("text") or ""),
                      # Attempt.ok 现在还要 response_valid —— 真 _call 里由
                      # classify.validate_success 判好存下来，假 _call 得自己带。
                      response_valid=True)
        return att

    prober._call = fake_call        # type: ignore[assignment]

    # ① 荒谬小值：丢弃（返回 None），且发事件说明
    events: list[tuple] = []
    prober.on_event = lambda k, d: events.append((k, d))
    v = SectionVerdict(section="openai-compatibility", usable=True,
                       base_url="https://ctx.example/v1", models=["m"])
    prober._stage4_context(row, v)
    assert v.max_context_length is None, (
        f"input_tokens=10 不该成为上限，实得 {v.max_context_length}")
    assert any(k == "context-untrusted" for k, _d in events), events

    # ② 可信的小窗口：照常采信
    events.clear()
    state["tokens"] = 60_000
    v2 = SectionVerdict(section="openai-compatibility", usable=True,
                        base_url="https://ctx.example/v1", models=["m"])
    prober._stage4_context(row, v2)
    assert v2.max_context_length == 60_000, v2.max_context_length
    assert v2.context_untrusted is True, "截断反推的值仍要标不可信"

    print("[OK] Context floor: input_tokens=10 被丢弃、60k 正常采信")


def test_context_unit_is_tokens():
    """`max-context-length` 必须以 **token** 计，不是发送的字符数。

    2026-09-06 抓到的真实数据错误：`_bisect` 的 lo/hi/mid 是**字符数**
    （探测发 `"x" * n`），而返回值直接进 `models[].max-context-length`，
    而 CPA 把那个字段当 **token** 用：
      model_registry.go:1440         → /v1/models 的 max_context_length
      codex/models/models.go:206-211 → context_window / max_context_window
    于是「上游能吃 110 万字符」被写成「窗口 1100000 token」，虚报约 4 倍。
    客户端按虚高的窗口定压缩点，塞到真实上限之外才被上游截断 —— 那条 400。

    生产 config.yaml 里 6 处 `max-context-length: 987500` 就是旧二分第三个
    中点 (875000+1100000)//2 的**字符数**，不是任何站声明的窗口。

    守三件事：
      ① 字符路径（hi 通过 / 二分收敛）经折算，不得原样返回
      ② declared 与 input_tokens 本来就是 token，不得再折算
      ③ 折算方向保守 —— 报出的窗口不大于真实窗口
    """
    from cpa_probe import pipeline as pl
    from cpa_probe.pipeline import Attempt, SectionVerdict

    row = cp.parse_lines("https://unit.example,sk-u").valid[0]

    # ① hi 一发通过：110 万字符不能写成 1100000
    #
    # `input_tokens` 必须 >= 发送字符数的一半，否则 `check()` 判成「截断」，
    # 走的是 `return trunc_hi, True`（那条路返回上游给的 token 数，本来就
    # 不该折算）。撤销验证抓到过这个假绿：按 chars//4 造 input_tokens 时
    # 275000 < 550000 命中截断分支，返回值恰好等于折算结果，于是把
    # `return _chars_to_tokens(hi)` 改回 `return hi` 测试照样通过。
    # 这里让上游报「全收下了」——那才是走 ok_hi 那条路的唯一形态。
    prober = Prober(gap=0.0, probe_context=True, swap_samples=0)
    prober._call = lambda section, base, key, model, **kw: Attempt(  # type: ignore[assignment]
        section=section, model=model, combo=kw.get("combo", ""),
        status="200", category="可用", action="", elapsed_ms=1,
        input_tokens=len(kw.get("text") or ""),   # = 发送量，未截断
        sent_chars=len(kw.get("text") or ""),
        response_valid=True)
    v = SectionVerdict(section="claude-api-key", usable=True,
                       base_url="https://unit.example", models=["m"])
    prober._stage4_context(row, v)
    assert v.context_untrusted is False, "这一发是正常通过，不是截断反推"
    assert v.max_context_length == pl._chars_to_tokens(1_100_000), (
        f"字符数没折算成 token：{v.max_context_length}")
    assert v.max_context_length < 1_100_000, v.max_context_length

    # ①b 二分收敛那条路也要折算 —— 与 hi 一发通过是**两个** return。
    # 撤销验证抓到：只测 hi 时把 `return _chars_to_tokens(left)` 改回
    # `return left` 照样全绿。这里让 hi 失败、lo 通过，逼二分走完。
    #
    # 形态：正文不提上限（否则走 declared 那条路直接返回），
    # 超过 60 万字符就 400、以下全收 —— 于是二分在 lo..hi 之间收敛。
    LIMIT_CHARS = 600_000

    def stepped(section, base, key, model, **kw):
        n = len(kw.get("text") or "")
        if n > LIMIT_CHARS:
            return Attempt(section=section, model=model,
                           combo=kw.get("combo", ""), status="400",
                           category="门禁", action="超限", elapsed_ms=1,
                           excerpt="request too large")   # 不提数字
        return Attempt(section=section, model=model, combo=kw.get("combo", ""),
                       status="200", category="可用", action="", elapsed_ms=1,
                       input_tokens=n, sent_chars=n, response_valid=True)

    p1b = Prober(gap=0.0, probe_context=True, swap_samples=0)
    p1b._call = stepped        # type: ignore[assignment]
    v1b = SectionVerdict(section="claude-api-key", usable=True,
                         base_url="https://unit.example", models=["m"])
    p1b._stage4_context(row, v1b)
    assert v1b.context_untrusted is False, "二分收敛不是截断反推"
    got = v1b.max_context_length or 0
    assert got < LIMIT_CHARS // 3, (
        f"二分结果没折算成 token（{got} 看着像字符数）")
    # 二分逼近 LIMIT_CHARS（从下方），折算后必须落在 token 量级：
    #   下界 lo=200000 字符 → 50000 token
    #   上界 LIMIT_CHARS    → 150000 token
    assert pl._chars_to_tokens(200_000) <= got <= pl._chars_to_tokens(LIMIT_CHARS), (
        f"折算后应在 50000..150000 token 之间，实得 {got}")

    # ② 上游正文自报的窗口：那是 token，原样返回
    p2 = Prober(gap=0.0, probe_context=True, swap_samples=0)
    p2._call = lambda section, base, key, model, **kw: Attempt(  # type: ignore[assignment]
        section=section, model=model, combo=kw.get("combo", ""),
        status="400", category="门禁", action="超限", elapsed_ms=1,
        excerpt="maximum context length is 262144 tokens")
    v2 = SectionVerdict(section="claude-api-key", usable=True,
                        base_url="https://unit.example", models=["m"])
    p2._stage4_context(row, v2)
    assert v2.max_context_length == 262_144, (
        f"上游自报的 token 数不该被折算：{v2.max_context_length}")

    # ③ 截断反推：input_tokens 也是 token，原样返回
    p3 = Prober(gap=0.0, probe_context=True, swap_samples=0)
    p3._call = lambda section, base, key, model, **kw: Attempt(  # type: ignore[assignment]
        section=section, model=model, combo=kw.get("combo", ""),
        status="200", category="可用", action="", elapsed_ms=1,
        input_tokens=131_072, sent_chars=len(kw.get("text") or ""),
        response_valid=True)
    v3 = SectionVerdict(section="claude-api-key", usable=True,
                        base_url="https://unit.example", models=["m"])
    p3._stage4_context(row, v3)
    assert v3.max_context_length == 131_072, v3.max_context_length
    assert v3.context_untrusted is True

    # 旧值迁移：格子上的字符数折回 token，格子外的原样留
    from cpa_probe.batch import _fix_legacy_char_context as fix
    assert fix(987_500) == pl._chars_to_tokens(987_500), fix(987_500)
    assert fix(1_100_000) == pl._chars_to_tokens(1_100_000)
    assert fix(15_515) == 15_515, "上游自报值不在格子里，不许动"
    assert fix(262_144) == 262_144, "2^18 是常见真实窗口，不许动"
    # 200000 有意排除 —— 同时是旧下界与最常见的真实窗口（Claude/GPT 两系）
    assert fix(200_000) == 200_000, "折它会把一个正确的声明值改小"

    print("[OK] Context unit: 字符经折算、declared 与 input_tokens 原样、旧值迁移")


def test_dead_end_matcher_shared_with_classify():
    """「这个分组没有这个模型」的措辞表只能有一处。

    2026-09-06 抓到：`classify` 的「分组无该模型渠道」规则与 pipeline 的
    `_MODEL_SPECIFIC_DEAD_END` 各写一份平行措辞表，已经分叉 ——
    「分组无该模型渠道」「当前分组下无此模型的渠道」两种正文
    classify 认、pipeline 不认。

    后果是**方向反了的误判**：`_stage1_baseline` 靠
    `_model_specific_dead_end` 决定「换个模型再试」还是「整段判死」。
    不认 = 立即收敛整段，而那本来是换个模型就可能通的站。
    """
    import re
    from cpa_probe.classify import MODEL_CHANNEL_BODY, _RULES
    from cpa_probe.pipeline import _MODEL_SPECIFIC_DEAD_END as MS

    row = [r for r in _RULES if r[1] == "分组无该模型渠道"]
    assert len(row) == 1, _RULES
    assert row[0][2] is MODEL_CHANNEL_BODY, (
        "classify 的规则又抄了一份措辞 —— 必须引用 MODEL_CHANNEL_BODY")

    pat = re.compile(MODEL_CHANNEL_BODY, re.I)
    bodies = [
        "分组无该模型渠道",
        "当前分组下无此模型的渠道",
        "该分组无可用渠道",
        "无可用渠道",
        "model_not_found",
        "可用渠道不存在",
        "当前API不支持所选模型",
    ]
    for b in bodies:
        assert bool(pat.search(b)) == bool(MS.search(b)), (
            f"两处判据对「{b}」不一致 —— 措辞表又分叉了")

    # 真正与模型无关的死路仍然不许豁免（否则整段永不收敛，白发请求）
    for b in ("Key 分组不匹配", "sensitive words detected", "404 page not found"):
        assert not MS.search(b), f"「{b}」与模型无关，不该当成模型专属"

    print("[OK] Dead-end matcher: classify 与 pipeline 同一份措辞，7 种正文一致")


def test_seed_does_not_overwrite_existing_models():
    """判死 + 目录读不到时，既有条目的模型清单不许被猜测清单覆盖。

    2026-09-06 用生产 config.yaml 实测抓到：tango 的 claude 条目原有
    claude-opus-5 / -thinking / claude-opus-4-8 / -4-8-thinking 四个，
    重探判死后 `models` 被换成 claude-fable-5-1 等六个**这个站从没验过**的
    名字 —— 后两个模型直接消失，新写进去的名字 CPA 路由过去大概率 404。

    兜底清单是「当前市面最新」，与「这个站卖什么」无关；原清单是先前一轮
    实测沉淀的。所以原清单优先，`model_source` 记 `prior`。

    仍要守的边界：
      · 新凭据（原文件里没有）没有原清单可沿用 → 仍走 seed
      · 同站**另一把** Key 的清单不许串过来（键含 api_key）
      · prior 不是操作员的显式意图 —— 不许当手填，也不许凭它新增段
    """
    import yaml
    from cpa_probe.plan import existing_models_for

    cfg = yaml.safe_load('''
claude-api-key:
  - api-key: "kA"
    base-url: "https://seed.example"
    priority: 990
    models:
      - name: "claude-opus-5"
        alias: ""
      - name: "claude-opus-4-8"
        alias: ""
  - api-key: "kB"
    base-url: "https://seed.example"
    priority: 990
    models:
      - name: "claude-sonnet-5"
        alias: ""
openai-compatibility:
  - name: "p"
    base-url: "https://seed.example/v1"
    api-key-entries:
      - api-key: "kA"
      - api-key: "kB"
    models:
      - name: "kimi-k3"
        alias: ""
''')
    # 逐 Key 隔离：kA 拿不到 kB 的清单
    assert existing_models_for(cfg, "claude-api-key", "https://seed.example",
                               "kA") == ["claude-opus-5", "claude-opus-4-8"]
    assert existing_models_for(cfg, "claude-api-key", "https://seed.example",
                               "kB") == ["claude-sonnet-5"]
    # 新 Key / 新站：没有原清单
    assert existing_models_for(cfg, "claude-api-key", "https://seed.example",
                               "kZ") == []
    assert existing_models_for(cfg, "claude-api-key", "https://other.example",
                               "kA") == []
    # compat 的 models 在 provider 级 —— 组内每把 Key 都查得到同一份
    for k in ("kA", "kB"):
        assert existing_models_for(cfg, "openai-compatibility",
                                   "https://seed.example/v1", k) == ["kimi-k3"]

    # 整条链：判死 + 目录读不到 → 沿用原清单，标 prior，默认不勾
    import cpa_probe as cpa
    from cpa_probe.pipeline import CandidateResult, SectionVerdict

    row = cp.parse_lines("https://seed.example,kA").valid[0]
    res = CandidateResult(row=row)
    for sec in cpa.SECTIONS:
        v = SectionVerdict(section=sec,
                           base_url=cpa.base_for_section(row.bare, sec))
        v.usable = False
        v.category, v.action = "死路", "分组无该模型渠道"
        res.sections[sec] = v
    from cpa_probe import model_catalog as _mc
    # 市面名录钉死成一份**完整**的 claude 清单 —— 本用例要守的正是
    # 「原清单不被这批猜测覆盖」，名录里有 sonnet / fable / haiku 才测得出来。
    MARKET = ["claude-opus-5", "claude-sonnet-5", "claude-fable-5-1",
              "claude-haiku-4-5-20251001"]
    with _patch.object(_mc, "remote_names", return_value=(list(MARKET), "")):
        plan = cpa.build_plan(row, res, cfg, rebuild=True)
    sp = plan.sections["claude-api-key"]
    assert sp.model_source == "prior", sp.model_source
    # ① 这个站从没报过的产品线（sonnet / fable / haiku）一个都不许进来 ——
    #    那正是 tango 事故的形态（写进去 CPA 路由过去大概率 404）
    for m in ("claude-sonnet-5", "claude-fable-5-1",
              "claude-haiku-4-5-20251001"):
        assert m not in sp.models, f"补进了这个站没报过的产品线：{sp.models}"
    # ② claude-opus-4-8 与 claude-opus-5 同产品线、世代更低 —— 按用户
    #    「每种类型只选该类型最高级别模型」剔除。原清单里**这个站验过的
    #    最高一档**必须留下。
    assert sp.models == ["claude-opus-5"], sp.models
    # 2026-09-17 反转（用户规则 ④）：沿用原清单的段也建议写 —— 原清单是
    # 先前实测沉淀，比工具猜测硬；不勾等于让操作员逐条手点。
    assert sp.recommended is True, "沿用原清单的段该默认勾（规则 ④）"
    assert sp.writable is True, "原清单是确定值，该让操作员能勾"
    assert any("沿用原" in w for w in sp.warnings), sp.warnings

    # 原文件里没有的段：没有原清单 → 仍走 seed（不许凭空说「沿用」）
    assert plan.sections["gemini-api-key"].model_source == "seed"

    # prior 与 seed 一样不够格新增一个原本不存在的段
    from cpa_probe.writeback import new_section_admitted
    assert not new_section_admitted("prior"), (
        "prior 不是本次实测依据，不许凭它新增段")
    assert not new_section_admitted("seed")
    for src in ("probed", "manual", "catalog"):
        assert new_section_admitted(src), src

    print("[OK] Seed floor: 原清单优先于猜测、逐 Key 隔离、prior 不得新增段")


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
routing:
  strategy: weighted-round-robin
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


def test_existing_hosts_keep_their_tier():
    """重探**既有站**不改它的 priority —— 那是站间次序，重探不构成改它的依据。

    2026-09-04 现场截图：同一个上游地址、只是 token 不同的几条，priority 不一致。
    两个独立成因，各自都足以造成拆档：

      ① `assign_priorities` 把重探的既有站当新站处理：算 cap、按分数排、逐站
         取 `min(cap, 上一站 - 1)`。`taken` 里塞着这些站**自己**的旧档，于是每个
         站都躲开自己原来的值往下掉。拿生产 config.yaml 实测：claude 段 12 个站
         从 1000/995/990/985/700/650/630/600/400/350/300/50 变成 500..489 一片连号。
      ② 留守条目（用户没勾 / 判不可写 / 探测异常）由 `_orphan_entry_lines` 原样
         搬回旧值，与被重探那几把的新值并存 —— kilo claude 3 把→164 + 2 把
         留在 372；tango claude 9 把→167 + 5 把留在 371。

    后果与「同站同档」那条约束冲突：CPA 的层级隔离只取最高可用桶
    （selector.go:527-553 availableAuthsFromPriorityBuckets 只收 bestPriority），
    同站被拆成两层就把「多 Key 并行轮询」变成「主备切换」—— 高档那几把先被打光
    配额，低档那批只在它们全部不可用时才轮到。

    ①在 assign_priorities 里修（沿用原档），②在 _orphan_entry_lines 里修
    （把留守条目对齐到同站新档）。这一项守①，落盘那一半由
    rehearse_real_rebuild 的第⑤组守。
    """
    import yaml
    from cpa_probe.plan import (ImportPlan, SectionPlan, assign_priorities,
                                existing_host_tiers, build_band)

    cfg = yaml.safe_load('''
claude-api-key:
  - api-key: "k1"
    base-url: "https://big.example"
    priority: 1000
    models: [{name: "m1", alias: ""}]
  - api-key: "k2"
    base-url: "https://big.example"
    priority: 1000
    models: [{name: "m1", alias: ""}]
  - api-key: "k3"
    base-url: "https://big.example"
    priority: 1000
    models: [{name: "m1", alias: ""}]
  - api-key: "k4"
    base-url: "https://mid.example"
    priority: 700
    models: [{name: "m1", alias: ""}]
  - api-key: "k5"
    base-url: "https://low.example"
    priority: 100
    models: [{name: "m1", alias: ""}]
''')

    def mk(host, key, models=("m1",), src="probed", score=100):
        pl = ImportPlan(host=host, masked_key="k", line_no=abs(hash(key)) % 9999)
        pl.sections["claude-api-key"] = SectionPlan(
            section="claude-api-key", base_url=f"https://{host}",
            api_key=key, models=list(models), score=score, model_source=src)
        return pl

    # ── ① 全部重探：每个既有站都留在自己原来的档 ──────────────────
    plans = [mk("big.example", "k1"), mk("big.example", "k2"),
             mk("big.example", "k3"), mk("mid.example", "k4"),
             mk("low.example", "k5")]
    assign_priorities(plans, cfg, probation=True)
    got = {}
    for pl in plans:
        got.setdefault(pl.host, set()).add(
            pl.sections["claude-api-key"].priority)
    assert got == {"big.example": {1000}, "mid.example": {700},
                   "low.example": {100}}, got
    for pl in plans:
        rsn = pl.sections["claude-api-key"].priority_reason
        assert "沿用该站在本段的原档" in rsn, rsn

    # ── ② 只重探一部分 Key：拿到的还是同一个档 ────────────────────
    partial = [mk("big.example", "k1")]
    assign_priorities(partial, cfg, probation=True)
    assert partial[0].sections["claude-api-key"].priority == 1000

    # ── ③ 既有站与新站混在一批：既有站不动，新站照常走空档分配 ──────
    mixed = [mk("big.example", "k1"), mk("mid.example", "k4"),
             mk("brand.example", "sk-new")]
    warns = assign_priorities(mixed, cfg, probation=True)
    vals = {pl.host: pl.sections["claude-api-key"].priority for pl in mixed}
    assert vals["big.example"] == 1000 and vals["mid.example"] == 700, vals
    assert vals["brand.example"] not in (1000, 700, 100), (
        f"新站不该撞上现有档位：{vals}")
    assert vals["brand.example"] <= 1000, vals

    # ── ④ 沿用原档会抢别人顶层时才重新定档 ────────────────────────
    # low.example 原本在 100，本次给它注册一个 m2 —— 而 m2 的现有顶层在 700。
    # 100 < 700，不构成劫持，仍沿用。
    cfg4 = yaml.safe_load('''
claude-api-key:
  - api-key: "a"
    base-url: "https://hi.example"
    priority: 700
    models: [{name: "m2", alias: ""}]
  - api-key: "b"
    base-url: "https://lo.example"
    priority: 100
    models: [{name: "m1", alias: ""}]
''')
    keep = [mk("lo.example", "b", models=("m1", "m2"))]
    assign_priorities(keep, cfg4, probation=True)
    assert keep[0].sections["claude-api-key"].priority == 100

    # 反向：hi.example 原本在 700，本次给它注册 m3 —— m3 的顶层在 900，
    # 700 < 900 仍不构成劫持。真正会劫持的是「本站档位高于新模型的顶层」。
    cfg5 = yaml.safe_load('''
claude-api-key:
  - api-key: "a"
    base-url: "https://hi.example"
    priority: 700
    models: [{name: "m2", alias: ""}]
  - api-key: "c"
    base-url: "https://carrier.example"
    priority: 300
    models: [{name: "m3", alias: ""}]
''')
    grab = [mk("hi.example", "a", models=("m2", "m3"))]
    w5 = assign_priorities(grab, cfg5, probation=True)
    v5 = grab[0].sections["claude-api-key"].priority
    assert v5 < 300, (
        f"沿用 700 会抢走 m3 在 300 的顶层，该重新定档，实得 {v5}")
    assert any("抢走" in w and "重新定档" in w for w in w5), w5

    # ── ⑤ 原文件里就已经拆开的站：按最高档对齐，并报出来 ──────────
    cfg6 = yaml.safe_load('''
claude-api-key:
  - api-key: "s1"
    base-url: "https://split.example"
    priority: 800
    models: [{name: "m1", alias: ""}]
  - api-key: "s2"
    base-url: "https://split.example"
    priority: 200
    models: [{name: "m1", alias: ""}]
''')
    band6 = build_band(cfg6, "claude-api-key")
    anchor, pre = existing_host_tiers(band6)
    assert anchor["split.example"] == 800, anchor
    assert pre["split.example"] == [800, 200], pre
    sp6 = [mk("split.example", "s1"), mk("split.example", "s2")]
    w6 = assign_priorities(sp6, cfg6, probation=True)
    assert {p.sections["claude-api-key"].priority for p in sp6} == {800}
    assert any("原 config.yaml 里就占着" in w for w in w6), w6

    # ── ⑥ 幂等：跑两次给出同样的值 ────────────────────────────────
    twice = [mk("big.example", "k1"), mk("brand.example", "sk-new")]
    assign_priorities(twice, cfg, probation=True)
    again = [mk("big.example", "k1"), mk("brand.example", "sk-new")]
    assign_priorities(again, cfg, probation=True)
    assert ([p.sections["claude-api-key"].priority for p in twice]
            == [p.sections["claude-api-key"].priority for p in again])

    # ── ⑥ 沿用原档不等于影响面为零：本次新加的模型造成的遮挡要报出来 ──
    # 2026-09-04 自查：`pinned` 分支清掉了三类旧警告却没重新加挡站那一条，
    # 于是「档位没变、但这个模型的格局变了」在界面上完全看不到。
    cfg7 = yaml.safe_load('''
claude-api-key:
  - api-key: "a"
    base-url: "https://mid.example"
    priority: 500
    models: [{name: "mA", alias: ""}]
  - api-key: "b"
    base-url: "https://top.example"
    priority: 900
    models: [{name: "mB", alias: ""}]
  - api-key: "c"
    base-url: "https://bot.example"
    priority: 100
    models: [{name: "mB", alias: ""}]
''')
    sh = [mk("mid.example", "a", models=("mA", "mB"))]
    assign_priorities(sh, cfg7, probation=True)
    spx = sh[0].sections["claude-api-key"]
    assert spx.priority == 500, f"500 < 900 不构成劫持，该沿用，实得 {spx.priority}"
    assert not spx.hijacked, [i.model for i in spx.hijacked]
    shadowed = [w for w in spx.warnings if "挡在其后" in w]
    assert shadowed, (
        f"沿用 500 会把 bot.example(100) 挡在其后，必须报出来，实得 {spx.warnings}")
    # 措辞要指向「去掉模型」而不是「改这个站的 priority」—— 档位不是本轮选的
    assert "本档是该站原有的" in shadowed[0], shadowed[0]
    assert "从模型清单里去掉" in shadowed[0], shadowed[0]
    assert "改成" not in shadowed[0], (
        "沿用原档时不该建议「改成 N」—— 那会把用户引向改一个不该动的既有值")
    # 没有新增遮挡时不该凭空加警告
    plain = [mk("mid.example", "a", models=("mA",))]
    assign_priorities(plain, cfg7, probation=True)
    assert not [w for w in plain[0].sections["claude-api-key"].warnings
                if "挡在其后" in w], "没有遮挡却报了"

    print("[OK] Existing tier pinned: 既有站沿用原档（全勾/部分勾都一样）、"
          "新站仍走空档分配、会抢顶层时才重新定档、原文件已拆开的按最高档对齐、"
          "沿用后新增的遮挡照样报出来")


def test_orphan_entries_realign_priority():
    """留守条目的 priority 必须对齐到同站本次的新档 —— 否则同站被拆成两层。

    `_orphan_entry_lines` 原样搬回「没进方案」的条目，那是对的（删除只该由用户
    显式操作）。但**原样**包含 priority，于是当同站另几把 Key 拿到新值时，
    落盘结果里这一个站有两个 priority。

    2026-09-04 现场截图就是这个形态：kilo.example 的 claude 段 5 条里 3 条 372、
    2 条 164。CPA 的层级隔离只取最高可用桶（selector.go:527-553），
    两层意味着低档那批只在高档全部不可用时才轮到 —— 「多 Key 并行轮询」变成
    「主备切换」。

    与 test_existing_hosts_keep_their_tier 是同一个症状的两半：那一项守
    assign_priorities 不乱改既有站的档，这一项守写回时留守条目跟着对齐。
    两条都要有 —— 用户手工改了 priority（覆盖在定档之后应用）时只有这一条兜得住。
    """
    import yaml
    from cpa_probe.writeback import rebuild_config_full, validate
    from cpa_probe.plan import SectionPlan, ImportPlan

    orig = """host: "127.0.0.1"

claude-api-key:
  - api-key: "k1"
    base-url: "https://multi.example"
    prefix: "ANT"
    priority: 900        # 上一轮定的
    weight: 3
    models:
      - name: "claude-opus-5"
        alias: ""
  - api-key: "k2"
    base-url: "https://multi.example"
    prefix: "ANT"
    priority: 900
    models:
      - name: "claude-opus-5"
        alias: ""
  # k3 这一把有自己的注释，搬运时不能丢
  - api-key: "k3"
    base-url: "https://multi.example"
    prefix: "ANT"
    priority: 900
    proxy-url: "http://mihomo:7890"
    models:
      - name: "claude-opus-5"
        alias: ""
"""
    cfg = yaml.safe_load(orig)
    lines = orig.splitlines(keepends=True)

    # 只有 k1 进方案，且它拿到一个**不同于原值**的 priority
    # （模拟用户手工改档，或旧版定档给出的新值）
    p = ImportPlan(host="multi.example", masked_key="k1", line_no=1)
    p.sections["claude-api-key"] = SectionPlan(
        section="claude-api-key", base_url="https://multi.example",
        api_key="k1", models=["claude-opus-5"], priority=250,
        prefix="ANT", model_source="probed")
    new, warns = rebuild_config_full(
        cfg, {("https://multi.example", "k1"): p}, lines)
    ok, msg = validate(new)
    assert ok, msg
    got = yaml.safe_load(new)
    ents = got["claude-api-key"]
    assert len(ents) == 3, f"条目数该守恒为 3，实得 {len(ents)}"
    vals = {e["priority"] for e in ents}
    assert vals == {250}, (
        f"同站三把 Key 该同档，实得 {sorted(vals)} —— 留守的两把没跟着对齐")
    # 对齐只改 priority 那一行，其余字段逐字保留
    by_key = {e["api-key"]: e for e in ents}
    assert by_key["k3"].get("proxy-url") == "http://mihomo:7890", (
        "留守条目的 proxy-url 被改动了")
    assert all(e.get("prefix") == "ANT" for e in ents), "prefix 丢了"
    assert "k3 这一把有自己的注释" in new, "留守条目自己的注释丢了"
    # 行尾注释要说清这个值是对齐来的，不是本次实测出来的
    assert "对齐同站档位" in new, "对齐后的行尾注释没说明来由"
    assert any("已对齐到同站新档" in w for w in warns), warns

    # 原值本来就等于新值时不改、也不报 —— 避免制造无意义的 diff
    p2 = ImportPlan(host="multi.example", masked_key="k1", line_no=1)
    p2.sections["claude-api-key"] = SectionPlan(
        section="claude-api-key", base_url="https://multi.example",
        api_key="k1", models=["claude-opus-5"], priority=900,
        prefix="ANT", model_source="probed")
    new2, warns2 = rebuild_config_full(
        cfg, {("https://multi.example", "k1"): p2}, lines)
    assert validate(new2)[0]
    assert {e["priority"] for e in yaml.safe_load(new2)["claude-api-key"]} == {900}
    assert not any("已对齐到同站新档" in w for w in warns2), (
        f"值没变却报了对齐，实得 {warns2}")
    assert "对齐同站档位" not in new2, "值没变却改写了行尾注释"

    # 条目里有嵌套结构、其中也叫 priority 时，只改条目级那一行 ——
    # 命中第一条之后无论改没改都停。原文件的 `plugins.configs.example.priority: 1`
    # 就是这种形状（缩进 6，在条目级的 4 之外）。
    nested = """host: "127.0.0.1"

claude-api-key:
  - api-key: "n1"
    base-url: "https://nest.example"
    priority: 900
    request-scoped-errors:
      configs:
        example:
          priority: 1
    models:
      - name: "claude-opus-5"
        alias: ""
  - api-key: "n2"
    base-url: "https://nest.example"
    priority: 900
    models:
      - name: "claude-opus-5"
        alias: ""
"""
    cfgn = yaml.safe_load(nested)
    pn = ImportPlan(host="nest.example", masked_key="n2", line_no=1)
    pn.sections["claude-api-key"] = SectionPlan(
        section="claude-api-key", base_url="https://nest.example",
        api_key="n2", models=["claude-opus-5"], priority=400,
        model_source="probed")
    new3, _w3 = rebuild_config_full(
        cfgn, {("https://nest.example", "n2"): pn},
        nested.splitlines(keepends=True))
    assert validate(new3)[0]
    got3 = yaml.safe_load(new3)["claude-api-key"]
    assert {e["priority"] for e in got3} == {400}, (
        f"条目级 priority 该对齐到 400，实得 {[e['priority'] for e in got3]}")
    keep = next(e for e in got3 if e["api-key"] == "n1")
    assert keep["request-scoped-errors"]["configs"]["example"]["priority"] == 1, (
        "嵌套结构里的同名键被改掉了")

    # 同一批方案里同 host 拿到**不同** priority（操作员手工改过一部分）——
    # 「同站的新档」不唯一，此时不许替他挑一个去改留守条目（2026-09-04 自查）。
    amb = """host: "127.0.0.1"

claude-api-key:
  - api-key: "k1"
    base-url: "https://m.example"
    priority: 900
    models: [{name: "claude-opus-5", alias: ""}]
  - api-key: "k2"
    base-url: "https://m.example"
    priority: 900
    models: [{name: "claude-opus-5", alias: ""}]
  - api-key: "k3"
    base-url: "https://m.example"
    priority: 900
    models: [{name: "claude-opus-5", alias: ""}]
"""
    cfga = yaml.safe_load(amb)
    pa: dict = {}
    for key, pri in (("k1", 250), ("k2", 777)):
        q = ImportPlan(host="m.example", masked_key=key, line_no=1)
        q.sections["claude-api-key"] = SectionPlan(
            section="claude-api-key", base_url="https://m.example",
            api_key=key, models=["claude-opus-5"], priority=pri,
            model_source="probed")
        pa[("https://m.example", key)] = q
    newa, warnsa = rebuild_config_full(cfga, pa, amb.splitlines(keepends=True))
    assert validate(newa)[0]
    bya = {e["api-key"]: e["priority"] for e in yaml.safe_load(newa)["claude-api-key"]}
    assert bya == {"k1": 250, "k2": 777, "k3": 900}, (
        f"手工改出的两个值该照写，留守的 k3 该保持 900，实得 {bya}")
    assert any("不同" in w and "保持原值" in w for w in warnsa), (
        f"同站多档无法对齐时必须报出来而不是默默挑一个，实得 {warnsa}")

    # 嵌套块排在条目级 priority **之前**时也要改对那一行（2026-09-04 自查）。
    # 第一版只认「第一条 priority: 行」，不看缩进 —— 这个顺序下命中的是嵌套里
    # 那个，于是三重错误：条目级没对齐（拆档没修上）、嵌套里一个无关键被改成
    # 档位值、而 realigned 报的是「已对齐」。字段顺序不保证，手工编辑过的条目
    # 完全可能是这个形状。
    before_nested = """host: "127.0.0.1"

claude-api-key:
  - api-key: "p1"
    base-url: "https://ord.example"
    request-scoped-errors:
      configs:
        example:
          priority: 1
    priority: 900
    models:
      - name: "claude-opus-5"
        alias: ""
  - api-key: "p2"
    base-url: "https://ord.example"
    priority: 900
    models:
      - name: "claude-opus-5"
        alias: ""
"""
    cfgo = yaml.safe_load(before_nested)
    po = ImportPlan(host="ord.example", masked_key="p2", line_no=1)
    po.sections["claude-api-key"] = SectionPlan(
        section="claude-api-key", base_url="https://ord.example",
        api_key="p2", models=["claude-opus-5"], priority=300,
        model_source="probed")
    newo, warnso = rebuild_config_full(
        cfgo, {("https://ord.example", "p2"): po},
        before_nested.splitlines(keepends=True))
    assert validate(newo)[0]
    go = yaml.safe_load(newo)["claude-api-key"]
    assert {e["priority"] for e in go} == {300}, (
        f"嵌套块在前时条目级 priority 该对齐到 300，实得 "
        f"{[e['priority'] for e in go]}")
    kept_o = next(e for e in go if e["api-key"] == "p1")
    assert kept_o["request-scoped-errors"]["configs"]["example"]["priority"] == 1, (
        "嵌套结构里的同名键被当成条目级 priority 改掉了")
    assert any("已对齐到同站新档" in w for w in warnso), warnso

    # 值不是裸整数（`"900"` / `!!int 900` / 锚点）—— 本工具不改，但**必须报**。
    # 静默跳过等于让用户以为拆档修好了：条目仍留在旧档，与同站其他 Key 分两层。
    odd_src = """host: "127.0.0.1"

claude-api-key:
  - api-key: "q1"
    base-url: "https://odd.example"
    priority: "900"
    models:
      - name: "claude-opus-5"
        alias: ""
  - api-key: "q2"
    base-url: "https://odd.example"
    priority: 900
    models:
      - name: "claude-opus-5"
        alias: ""
"""
    cfgq = yaml.safe_load(odd_src)
    pq = ImportPlan(host="odd.example", masked_key="q2", line_no=1)
    pq.sections["claude-api-key"] = SectionPlan(
        section="claude-api-key", base_url="https://odd.example",
        api_key="q2", models=["claude-opus-5"], priority=350,
        model_source="probed")
    newq, warnsq = rebuild_config_full(
        cfgq, {("https://odd.example", "q2"): pq},
        odd_src.splitlines(keepends=True))
    assert validate(newq)[0]
    assert any("没能对齐" in w for w in warnsq), (
        f"引号包裹的 priority 改不了，必须报出来而不是静默跳过，实得 {warnsq}")
    assert 'priority: "900"' in newq, "不该去改非裸整数的写法"

    print("[OK] Orphan realign: 留守条目跟着同站新档走、其余字段与注释逐字保留、"
          "值未变时不制造 diff、嵌套同名键不误伤（含嵌套排在前面的顺序）、"
          "同站多档时报出来而不替人挑、改不动的写法明确报出来")


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


def test_new_section_gate_reaches_ui():
    """跨段新增的闸必须**同时**落在方案对象上，界面才看得见。

    2026-09-03 现场：那道闸只存在于 rebuild_config_full 内部，界面按
    「没有闸」渲染 —— `writable` 与 `recommended` 都是 True，显示
    「建议写入」并默认勾上，勾了写不进，只在 warnings 里留一句话。
    「代码里有闸、界面按没闸渲染」是这一类缺陷的通用形态。

    放行标准按证据强弱：probed / manual / catalog 放行，seed 不放行。
    """
    import yaml
    import cpa_probe as cp
    from cpa_probe.plan import ImportPlan, SectionPlan

    cfg = yaml.safe_load("""
claude-api-key:
  - api-key: "kA"
    base-url: "https://a.example.com"
    priority: 500
    models: [{name: "claude-opus-5", alias: ""}]
""")

    def mk(source):
        p = ImportPlan(host="a.example.com", masked_key="kA", line_no=1)
        for sec in ("claude-api-key", "codex-api-key"):
            p.sections[sec] = SectionPlan(
                section=sec,
                base_url="https://a.example.com" + ("/v1" if "codex" in sec else ""),
                api_key="kA",
                models=["claude-opus-5" if "claude" in sec else "gpt-5.6-sol"],
                priority=500, model_source=source)
        return p

    for src in ("probed", "manual", "catalog"):
        p = mk(src)
        blocked = cp.mark_new_sections(cfg, [p])
        cx = p.sections["codex-api-key"]
        assert blocked == 0, f"{src} 不该被拦：{blocked}"
        assert cx.new_section, f"{src}：codex 原本没配，该标成新增段"
        assert cx.writable and not cx.write_blocked, (
            f"{src} 有依据却被拦下：{cx.write_blocked}")
        # 已占有的段不该被标成新增
        assert not p.sections["claude-api-key"].new_section

    p = mk("seed")
    blocked = cp.mark_new_sections(cfg, [p])
    cx = p.sections["codex-api-key"]
    assert blocked == 1, blocked
    assert cx.new_section and cx.write_blocked, "seed 该被拦下并给出原因"
    assert not cx.writable, "写不进去就不能显示成可写 —— 勾了没反馈"
    assert not cx.recommended
    assert cx.write_blocked in cx.recommend_reason, (
        f"界面读的是 recommend_reason，必须能看到原因：{cx.recommend_reason}")
    # 原本占有的那一段不受影响
    assert p.sections["claude-api-key"].writable

    # 重复调用要幂等 —— 服务端每次 /api/plan 都会调它
    before = p.sections["codex-api-key"].write_blocked
    cp.mark_new_sections(cfg, [p])
    assert p.sections["codex-api-key"].write_blocked == before

    # 手填之后同一个方案要放行：server 把 overrides["models"] 记成 manual，
    # 再调一次这个函数就该解锁。
    p.sections["codex-api-key"].model_source = "manual"
    assert cp.mark_new_sections(cfg, [p]) == 0
    assert not p.sections["codex-api-key"].write_blocked
    assert p.sections["codex-api-key"].writable

    # 全新凭据（整个 key 都不在 cfg 里）不受这道闸约束 —— 那是增量导入
    p2 = ImportPlan(host="b.example.com", masked_key="kB", line_no=2)
    p2.sections["codex-api-key"] = SectionPlan(
        section="codex-api-key", base_url="https://b.example.com/v1",
        api_key="kB", models=["gpt-5.6-sol"], priority=100,
        model_source="seed")
    assert cp.mark_new_sections(cfg, [p2]) == 0
    assert not p2.sections["codex-api-key"].new_section
    assert p2.sections["codex-api-key"].writable

    print("[OK] New-section gate: probed/manual/catalog 放行、seed 拦下且"
          "界面三态与落盘一致、手填可解锁、新凭据不受约束")


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
        ("注释索引的六条边界", test_comment_index_boundaries),
        ("priority 降序", test_rebuild_config_priority_order),
        ("段字段结构与 compat 归并", test_rebuild_config_section_structure),
        ("凭据去重", test_credential_dedup),
        ("三条入口共用同一批闸", test_three_paths_share_the_gates),
        ("探测文本非问候", test_probe_text_not_trivial),
        ("画像结论复用省请求", test_profile_verdict_reuse_saves_calls),
        ("画像漂移检测", test_profile_drift_detection),
        ("画像梯对齐真实 CPA 源码", test_profile_matches_real_cpa_source),
        ("远程模式降级", test_drift_remote_degrade),
        ("漂移检测不阻塞 context", test_drift_never_blocks_context),
        ("旧二进制检测", test_stale_binary_detection),
        ("headers 覆盖写进 YAML", test_headers_override_reaches_yaml),
        ("重建保留其余内容", test_rebuild_preserves_everything_else),
        ("未知字段搬运", test_rebuild_keeps_unknown_fields),
        ("proxy-url 搬运", test_rebuild_keeps_proxy),
        ("prefix 与 provider name 搬运", test_rebuild_keeps_prefix_and_provider_name),
        ("模型级 max-context-length 搬运", test_rebuild_keeps_model_context_length),
        ("重探不判重", test_rebuild_skips_dedup),
        ("compat 组内 Key 与 per-key 字段", test_compat_group_key_preservation),
        ("条目守恒与未勾不删", test_rebuild_entry_conservation),
        ("重建保留 weight", test_rebuild_keeps_weight),
        ("批量定档站级差异", test_assign_priorities_site_level),
        ("既有站沿用原档", test_existing_hosts_keep_their_tier),
        ("留守条目对齐同站档位", test_orphan_entries_realign_priority),
        ("模型库三层兜底", test_model_catalog_three_layers),
        ("规则收紧不留死角", test_model_rules_no_dead_end),
        ("端点通但模型空也要兜底", test_usable_but_empty_models),
        ("手填无条件优先", test_manual_beats_probed),
        ("四族之外的手填与目录", test_offfamily_manual_and_catalog),
        ("落后目录不默认勾", test_stale_catalog_not_recommended),
        ("限频阈值自动学习", test_rate_limit_learned),
        ("上下文上限下限校验", test_context_limit_lower_bound),
        ("上下文上限单位是 token", test_context_unit_is_tokens),
        ("死路措辞表与 classify 共用",
         test_dead_end_matcher_shared_with_classify),
        ("兜底不覆盖既有清单",
         test_seed_does_not_overwrite_existing_models),
        ("能力开关实测与写回",
         test_capability_toggles_probed_and_written),
        ("compat 同 host 多路径隔离", test_compat_same_host_multi_path_isolated),
        ("查表接线到写回路径", test_carry_tables_are_wired_into_writeback_path),
        ("headers 合并行为", test_merge_entry_headers_behaviour),
        ("flow 段头重建", test_flow_style_section_head_rebuilds),
        ("模型级能力字段搬运",
         test_model_level_capability_fields_are_carried),
        ("能力探测真的被调用", test_capability_probe_is_actually_invoked),
        ("compat provider 查找剥注释",
         test_find_compat_provider_strips_trailing_comments),
        ("段边界认异形顶层键",
         test_section_span_recognizes_odd_top_level_keys),
        ("_yaml_field 子键与 float",
         test_yaml_field_escapes_subkeys_and_skips_odd_floats),
        ("批量键含 api_key", test_batch_key_includes_api_key),
        ("批量记录异常站", test_batch_records_errors),
        ("cgroup 异常值降级", test_cgroup_bad_values),
        ("跨段新增闸到界面", test_new_section_gate_reaches_ui),
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
