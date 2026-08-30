#!/usr/bin/env python3
"""探测「速度优化」的回归测试。零外网请求（本地假上游）。

    python3 tests/test_speed.py

为什么单独一个套件
------------------
用户实测：单站四段探测要 10 多分钟。诊断结论是**慢在等待与冗余请求，
不在计算**——换语言或加线程都救不了，必须动这四处：

  ① 节流按 (host, section) 分桶     原来四段共享一个桶，单站 56 次请求
                                    串成 55 x 3s = 165 秒纯睡
  ② 四段并行                        四段打不同端点，业务上互不依赖
  ③ (host, section) single-flight   同主机多 Key 并发时，形态学习只做一次
                                    （那是最贵的动作：最多 12 次请求，
                                     含 4 次百万字符大 body）
  ④ 上下文探测 hi-first + 读错误体   上游超限时往往直接写明上限，
                                    读到就免掉整轮二分

这四项都是**并发与请求数**的性质，前 472 项测试一项都碰不到。而它们各自
都有真实的退化风险：
  · ① 若桶键写错，会退回全局串行（慢）或跨段不节流（触发站方 guard）
  · ② 并发引入共享状态竞态 —— 代理预检重复执行已经被抓到过一次
  · ③ 门闩漏放会永久卡死
  · ④ 单位混算会把 token 数当字符数写进 config.yaml，让客户端过早压缩
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)   # 让 fixture_cfg 可导入

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import fixture_cfg                                     # noqa: E402
from cpa_probe.pipeline import Prober, SectionVerdict  # noqa: E402
from cpa_probe.parse import SECTIONS                   # noqa: E402

_pass = 0
_fail: list[str] = []


def section(title: str) -> None:
    print(f"── {title} " + "─" * max(0, 58 - len(title)))


def eq(label: str, got, want) -> None:
    global _pass
    if got == want:
        _pass += 1
        print(f"  ok  {label}")
    else:
        _fail.append(f"{label}\n      期望 {want!r}\n      实得 {got!r}")
        print(f"  FAIL {label}")


def truthy(label: str, got, hint: str = "") -> None:
    global _pass
    if got:
        _pass += 1
        print(f"  ok  {label}")
    else:
        _fail.append(f"{label}\n      实得 {got!r} {hint}")
        print(f"  FAIL {label}")


def main() -> int:
    # ── ① 节流分桶 ──────────────────────────────────────────────────
    section("① 节流按 (host, section) 分桶")
    p = Prober(gap=0.30, proxy=None)

    # 同 host 同 section：必须等满 gap
    t0 = time.monotonic()
    p._throttle("a.com", "claude-api-key")
    p._throttle("a.com", "claude-api-key")
    same = time.monotonic() - t0
    truthy(f"同站同段第二次要等 gap（实测 {same:.2f}s）", same >= 0.28,
           "guard 是站方按端点计的，同段绝不能放松")

    # 同 host 不同 section：不该等
    p2 = Prober(gap=0.30, proxy=None)
    t0 = time.monotonic()
    for sec in SECTIONS:
        p2._throttle("a.com", sec)
    four = time.monotonic() - t0
    truthy(f"同站四段互不等待（实测 {four:.2f}s）", four < 0.15,
           "这是「四段共享一个桶导致 165 秒纯睡」的修复点")

    # 不同 host 同 section：不该等
    p3 = Prober(gap=0.30, proxy=None)
    t0 = time.monotonic()
    for h in ("a.com", "b.com", "c.com", "d.com"):
        p3._throttle(h, "claude-api-key")
    hosts4 = time.monotonic() - t0
    truthy(f"四个不同站互不等待（实测 {hosts4:.2f}s）", hosts4 < 0.15)

    # 桶键不能把 (a, b|c) 与 (a|b, c) 混成同一个
    p4 = Prober(gap=0.30, proxy=None)
    p4._throttle("a", "b|c")
    t0 = time.monotonic()
    p4._throttle("a|b", "c")
    collide = time.monotonic() - t0
    truthy(f"桶键无歧义碰撞（实测 {collide:.2f}s）", collide < 0.15,
           "若用裸拼接且分隔符可出现在 host/section 里，两者会撞成同一桶")

    # ── ② workers 归一化 ────────────────────────────────────────────
    section("② workers 归一化")
    eq("workers=0 归一到 1", Prober(workers=0).workers, 1)
    eq("workers=-5 归一到 1", Prober(workers=-5).workers, 1)
    eq("workers=99 收敛到段数", Prober(workers=99).workers, len(SECTIONS))
    eq("workers=1 保持串行", Prober(workers=1).workers, 1)
    eq("默认并行度 = 段数", Prober().workers, len(SECTIONS))

    # ── ③ 代理预检只做一次（并发下） ────────────────────────────────
    section("③ 代理预检在并发下只做一次")
    # 指一个必然不通的地址：预检失败很快返回，且不会真的连出去
    pp = Prober(proxy="http://127.0.0.1:1", workers=4)
    seen: list[dict] = []
    lock = threading.Lock()

    def rec(kind: str, data: dict) -> None:
        if kind == "proxy-precheck":
            with lock:
                seen.append(data)

    pp.on_event = rec
    results: list = []

    def touch() -> None:
        results.append(pp.live_proxy)

    ths = [threading.Thread(target=touch) for _ in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    eq("8 个线程只触发 1 次预检", len(seen), 1)
    eq("死代理下 live_proxy 一律 None", set(results), {None})

    # ── ④ single-flight 门闩 ────────────────────────────────────────
    section("④ (host, section) single-flight")
    sf = Prober(workers=4, gap=0.0)
    calls: list[tuple[str, str]] = []
    calls_lock = threading.Lock()
    started = threading.Event()

    def fake_full_probe(row, sec):
        with calls_lock:
            calls.append((row.host, sec))
        started.set()
        time.sleep(0.25)           # 模拟昂贵的形态学习
        v = SectionVerdict(section=sec, base_url=f"https://{row.host}")
        v.usable = True
        v.models = ["m1"]
        return v

    reused: list[tuple[str, str]] = []

    def fake_reuse(row, sec, shape):
        with calls_lock:
            reused.append((row.host, sec))
        return SectionVerdict(section=sec, base_url=shape.base_url)

    sf._full_probe = fake_full_probe
    sf._reuse_shape = fake_reuse

    class Row:
        host = "same.com"
        bare = "https://same.com"
        api_key = "k"

        def masked(self):
            return "k***"

    rows = [Row(), Row(), Row(), Row(), Row()]

    # 5 个 Key（同一主机）并发探同一个段
    ths = [threading.Thread(target=sf._probe_one_section,
                            args=(r, "claude-api-key")) for r in rows]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    eq("5 个同主机 Key 只做 1 次形态学习", len(calls), 1)
    eq("其余 4 个走复用路径", len(reused), 4)

    # 门闩用完必须清空，否则后续永久卡死
    eq("门闩已释放", len(sf._inflight), 0)

    # 形态学习失败（抛异常）时门闩也必须放掉
    sf2 = Prober(workers=4, gap=0.0)

    def boom(row, sec):
        raise RuntimeError("学习失败")

    sf2._full_probe = boom
    try:
        sf2._probe_one_section(Row(), "claude-api-key")
    except RuntimeError:
        pass
    eq("异常路径也释放门闩", len(sf2._inflight), 0)

    # ── ⑤ 错误正文里的上限解析 ──────────────────────────────────────
    section("⑤ 上游自报上限的解析（免掉整轮二分）")
    L = Prober._limit_from_body
    cases_hit = [
        ("maximum context length is 200000 tokens", 200_000),
        ("This model's maximum context length is 128000 tokens, "
         "however you requested 300000", 128_000),
        ("prompt is too long: 215000 tokens > 200000 maximum", 200_000),
        ("error: context_length_exceeded, limit is 32768", 32_768),
        ("max_input_tokens: 1000000 exceeded", 1_000_000),
        ("最大上下文长度为 128000", 128_000),
        ("上下文上限 200000 tokens", 200_000),
    ]
    for body, want in cases_hit:
        eq(f"解析「{body[:38]}…」", L(body), want)

    cases_miss = [
        ("", None),
        ("Internal server error", None),
        # 合理性下限：低于 8000 的数字更可能是输出上限/错误码，不能当上下文写
        ("maximum context length is 4096 tokens", None),
        # 上限：超过 200 万不是上下文窗口
        ("maximum context length is 99000000 tokens", None),
        # 纯数字堆积不应误命中
        ("request id 20260830123456 failed", None),
    ]
    for body, want in cases_miss:
        eq(f"不误判「{body[:38] or '（空）'}…」", L(body), want)

    # 关键：单位不混算。declared 是 token 数，lo/hi 是字符数。
    # 200000 tokens 若被当成字符数写回，会让客户端按 1/4 的窗口压缩。
    eq("解析结果原样返回，不做字符换算",
       L("maximum context length is 200000 tokens"), 200_000)

    # ── ⑥ config.yaml 解析缓存 ──────────────────────────────────────
    section("⑥ config 解析缓存（正确性优先于速度）")
    # 实测：yaml.safe_load 这份 857 KB / 14900 行的文件要 **352 毫秒**，
    # 是服务里最贵的 CPU 操作。/api/context 与 /api/plan 各调一次。
    # 但缓存的风险是读到旧基线 —— 那会让写回建立在过期内容上，
    # 所以这里**先测失效、再测速度**。
    import importlib.util as _ilu
    import shutil as _sh
    import tempfile as _tf
    import time as _t

    _spec = _ilu.spec_from_file_location(
        "srv_cache_test", os.path.join(ROOT, "server.py"))
    _m = _ilu.module_from_spec(_spec)
    sys.modules["srv_cache_test"] = _m
    _spec.loader.exec_module(_m)
    H = _m.Handler

    # 不传路径就用自带样本。原来是回落到 ../config.yaml，文件不在就整段跳过 ——
    # 而**跳过与通过在汇总行里长得一样**：本机 36 项、刚 clone 的仓库 28 项，
    # 凭空少掉的 8 项恰好是最要紧的那几条（改文件后缓存必须失效、同大小改内容
    # 要感知、半截 YAML 必须被拒绝）。这类静默缺失 2026-08-30 已经踩过一次
    # （bcrypt 分支只在一侧断言），所以这里不留跳过分支。
    _cfg_src, _synth, _fx_tmp = fixture_cfg.resolve(sys.argv, label="缓存正确性")
    if os.path.exists(_cfg_src):
        _tmp = _tf.mkdtemp(prefix="cfgcache-")
        try:
            _p = os.path.join(_tmp, "config.yaml")
            _sh.copy2(_cfg_src, _p)
            H.cfg_path = _p
            H._cfg_cache = None
            inst = H.__new__(H)          # 不跑 __init__（那会要 socket）

            t0 = _t.perf_counter()
            raw1, _c1 = inst._load_cfg()
            cold = (_t.perf_counter() - t0) * 1000
            t0 = _t.perf_counter()
            for _ in range(5):
                raw2, _c2 = inst._load_cfg()
            hot = (_t.perf_counter() - t0) * 1000 / 5

            truthy(f"缓存命中显著更快（冷 {cold:.0f}ms → 热 {hot:.2f}ms）",
                   hot < cold / 10 if cold > 5 else True)
            eq("热路径返回同样的内容", raw1, raw2)

            # 最关键的一条：文件被改后必须立刻读到新内容。
            # 这个文件会被 CPA 自己的 PUT、用户手工编辑、另一个实例改动。
            io.open(_p, "a", encoding="utf-8").write("\n# cache-test-marker\n")
            raw3, _c3 = inst._load_cfg()
            truthy("改文件后立刻读到新内容（缓存正确失效）",
                   "# cache-test-marker" in raw3,
                   "缓存没失效 —— 写回会建立在过期基线上")

            # 缓存键含 size：同一秒内改成不同长度也要感知
            io.open(_p, "a", encoding="utf-8").write("# second-marker\n")
            raw4, _c4 = inst._load_cfg()
            truthy("同秒内二次修改也感知", "# second-marker" in raw4)

            # 显式清空后应重新解析，且结果一致
            H._cfg_cache = None
            raw5, _c5 = inst._load_cfg()
            eq("显式清缓存后仍能正常读", raw5, raw4)

            # 同大小改内容必须被感知 —— 元数据键 (mtime_ns, size) 在
            # 粗粒度文件系统（NFS、部分容器 overlay）上会漏判这种修改，
            # 漏判后写回会建立在过期基线上。内容哈希不受影响。
            _p2 = os.path.join(_tmp, "same-size.yaml")
            io.open(_p2, "w", encoding="utf-8").write("a: 1\nb: 2\n")
            H.cfg_path = _p2
            H._cfg_cache = None
            r1, _ = inst._load_cfg()
            io.open(_p2, "w", encoding="utf-8").write("a: 9\nb: 2\n")
            r2, _ = inst._load_cfg()
            truthy("同大小改内容也能感知（内容哈希，非元数据）", r1 != r2,
                   "漏判会让写回建立在过期基线上")

            # 半截文件（write_local 就地 O_TRUNC 覆写期间的中间态）
            # 必须**被拒绝**，绝不能缓存成坏数据
            _p3 = os.path.join(_tmp, "torn.yaml")
            io.open(_p3, "w", encoding="utf-8").write(
                'claude-api-key:\n  - api-key: "x"\n    base')
            H.cfg_path = _p3
            H._cfg_cache = None
            _raised = False
            try:
                inst._load_cfg()
            except Exception:
                _raised = True
            truthy("半截 YAML 被拒绝而不是缓存", _raised,
                   "缓存一份坏数据会让后续写回全部基于它")

            # 顶层非映射（读到了别的东西）同样拒绝
            _p4 = os.path.join(_tmp, "notmap.yaml")
            io.open(_p4, "w", encoding="utf-8").write("- a\n- b\n")
            H.cfg_path = _p4
            H._cfg_cache = None
            _raised2 = False
            try:
                inst._load_cfg()
            except ValueError:
                _raised2 = True
            truthy("顶层非映射被拒绝", _raised2)
            H.cfg_path = _p
        finally:
            _sh.rmtree(_tmp, ignore_errors=True)
            if _fx_tmp:
                _sh.rmtree(_fx_tmp, ignore_errors=True)
            H._cfg_cache = None

    print("\n" + "=" * 62)
    if _fail:
        print(f"失败 {len(_fail)} 项 / 通过 {_pass} 项\n")
        for f in _fail:
            print(f"  ✗ {f}\n")
        return 1
    print(f"全部通过 · {_pass} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
