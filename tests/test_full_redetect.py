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
