"""全量重探功能测试

验证：
1. extract_existing_entries 提取既有站
2. BatchProber 站级并发
3. rebuild_config_full 的注释保全、priority 排序与段字段结构
"""

import os
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
        def __init__(self, url):
            self.bare = url  # BatchProber 用 bare 字段
            self.host = url.split("//")[1].split("/")[0] if "//" in url else url

    rows = [FakeRow(f"https://site{i}.com") for i in range(10)]

    # 进度收集
    progress_log = []
    def progress_cb(current, total, site, stats):
        progress_log.append((current, total, stats.copy()))

    # 批量探测
    batch_prober = BatchProber(FakeProber(), max_workers=3)
    results = batch_prober.probe_batch(rows, progress_callback=progress_cb)

    # 验证结果
    assert len(results) == 10
    assert len(progress_log) == 10  # 10次回调
    assert progress_log[-1][0] == 10  # 最后一次是 10/10
    assert progress_log[-1][1] == 10

    # 验证统计：所有站都是 partial（2段通）
    assert batch_prober._stats["partial"] == 10
    assert batch_prober._stats["success"] == 0
    assert batch_prober._stats["failure"] == 0

    print(f"[OK] BatchProber: 10 sites probed, {len(progress_log)} callbacks")


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
        def __init__(self, url):
            self.bare = url

    rows = [FakeRow(f"https://site{i}.com") for i in range(5)]

    batch_prober = BatchProber(FakeProber(), max_workers=2)
    results = batch_prober.probe_batch(rows)

    assert batch_prober._stats["success"] == 1
    assert batch_prober._stats["partial"] == 2
    assert batch_prober._stats["failure"] == 2

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
        def __init__(self, url):
            self.bare = url

    rows = [FakeRow(f"https://site{i}.com") for i in range(3)]

    batch_prober = BatchProber(FakeProber(), max_workers=1)
    results = batch_prober.probe_batch(rows)

    # 异常站被计入 failure
    assert batch_prober._stats["partial"] == 2  # 第1、3站
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
  # relay-a 403 banned，从 900 降权待解封
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
    assert "relay-a 403 banned" in rebuilt
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

    # 验证降序
    assert priorities == sorted(priorities, reverse=True), f"Not sorted: {priorities}"
    assert priorities == [900, 500, 100]

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
    for sec in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        for e in parsed[sec]:
            assert "name" not in e, f"{sec} 不该有 name 字段（缺陷 2 回归）"
            assert "api-key" in e and "base-url" in e
            assert "models" in e, f"{sec} 缺 models"
            # models 是 [{name, alias}] 结构，不是裸字符串列表
            assert isinstance(e["models"][0], dict), f"{sec} models 结构不对"
            assert "alias" in e["models"][0]

    # 缺陷 3：compat 段结构与归并
    compat = parsed["openai-compatibility"]
    assert len(compat) == 1, f"同站两个 Key 应归并成 1 个 provider，实际 {len(compat)}"
    prov = compat[0]
    assert "name" in prov, "compat 段必须有 name（CPA 的 provider 身份）"
    assert "api-key-entries" in prov, "compat 段必须用 api-key-entries"
    assert "api-key" not in prov, "compat 段不该在 provider 级放 api-key"
    keys = [e["api-key"] for e in prov["api-key-entries"]]
    assert set(keys) == {"sk-A", "sk-B"}, f"两个 Key 都要在，实际 {keys}"

    print(f"[OK] Section structure: 前三段无 name、compat 归并 "
          f"{len(prov['api-key-entries'])} 个 Key、{len(warns)} 条警告")


if __name__ == "__main__":
    # 与其余套件同一套汇报约定：run.py 按「全部通过 · N 项」这行统计，
    # 失败行以 ✗ 开头。自己 print [OK] 不会被计入总数。
    CASES = [
        ("提取既有站", test_extract_existing_entries),
        ("站级并发与进度回调", test_batch_prober_progress),
        ("统计分类", test_batch_prober_stats),
        ("单站异常隔离", test_batch_prober_exception_handling),
        ("全量重建保注释", test_rebuild_config_preserves_comments),
        ("priority 降序", test_rebuild_config_priority_order),
        ("段字段结构与 compat 归并", test_rebuild_config_section_structure),
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
