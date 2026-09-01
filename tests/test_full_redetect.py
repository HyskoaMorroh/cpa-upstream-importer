"""全量重探功能测试

验证：
1. extract_existing_entries 提取既有站
2. BatchProber 站级并发
3. 进度回调
"""

import time
import threading
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
        section="gemini",
        base_url="old.example.com",
        api_key="AIzaOLD",
        models=["gemini-2.5-flash"],
        priority=150,  # 改了
    )
    sp_claude = SectionPlan(
        section="claude",
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

    # 验证 priority 更新
    assert "priority: 150" in rebuilt
    assert "priority: 250" in rebuilt

    # 验证旧 priority 消失
    assert "priority: 200" not in rebuilt
    assert "priority: 300" not in rebuilt

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
            section="gemini",
            base_url=url,
            api_key=key,
            models=["gemini-2.5-flash"],
            priority=prio,
        )
        plan = ImportPlan(host=url, masked_key=f"{key[:4]}...")
        plan.sections["gemini"] = sp
        plans[(url, key)] = plan

    rebuilt, warnings = rebuild_config_full(cfg, plans, original_lines)

    # 提取 priority 出现顺序
    lines = rebuilt.splitlines()
    priorities = []
    for line in lines:
        if "priority:" in line:
            prio = int(line.split("priority:")[1].strip())
            priorities.append(prio)

    # 验证降序
    assert priorities == sorted(priorities, reverse=True), f"Not sorted: {priorities}"
    assert priorities == [900, 500, 100]

    print(f"[OK] Priority order: {priorities}")


if __name__ == "__main__":
    test_extract_existing_entries()
    test_batch_prober_progress()
    test_batch_prober_stats()
    test_batch_prober_exception_handling()
    test_rebuild_config_preserves_comments()
    test_rebuild_config_priority_order()
    print("\n[OK] 全量重探功能测试通过")
