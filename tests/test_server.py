#!/usr/bin/env python3
"""服务契约回归测试。零外网请求 —— 只打本机自己启的服务。

    python3 tests/test_server.py [config.yaml 路径]

覆盖：鉴权闸门、静态资源与路径穿越、脱敏、写回三道闸门、原文件不被触碰。
探测类路由（/api/probe）不在此覆盖 —— 它会真打上游、花钱、触发限频。
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)   # 让 fixture_cfg 可导入
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import fixture_cfg                                        # noqa: E402

_fail: list[str] = []
_pass = 0


def eq(name: str, got, want) -> None:
    global _pass
    if got != want:
        _fail.append(f"{name}\n      got  = {got!r}\n      want = {want!r}")
    else:
        _pass += 1
        print(f"  ok  {name}")


def truthy(name: str, got, hint: str = "") -> None:
    global _pass
    if got:
        _pass += 1
        print(f"  ok  {name}")
    else:
        _fail.append(f"{name}\n      实得 {got!r} {hint}")


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


def free_port() -> int:
    """拿一个当前空闲的端口号。**只在端口必须交给别的进程时用。**

    2026-09-05 起，同进程内起 HTTP 服务的地方**不要用它** —— 改成把 0 号端口
    交给 `ThreadingHTTPServer` 自己 bind，再读 `srv.server_address[1]`：

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = srv.server_address[1]

    为什么（两次偶发失败的最可能根因）
    -----------------------------
    这个函数 bind 0 号端口拿到号码之后**必须 close 才能返回**，而调用方随后
    再 bind 同一个号码 —— 那两步之间有窗口。全套跑 11 个套件、其中 4 个起真
    HTTP 服务，同一台机器短时间内反复分配端口，窗口里被抢到就
    `OSError: [WinError 10048]`。

    与实测吻合的三点：
      · 只在 `run.py` 全链跑时出现（单跑某个套件时没有并发的端口分配）
      · 两次分别落在 `test_server.py` 与 `test_pipeline.py` —— 都是起服务的
      · 不可复现（窗口极窄），八次重跑 0 失败

    本套件仍然用它，因为端口要作为 `--port` 传给**子进程**起的服务 ——
    那个号码必须先定下来才能传。那一处的实际风险小得多：子进程起服务后调用方
    会轮询等它就绪，抢占失败时表现为「服务启动失败」并立刻报错，
    而不是静默的 10048。
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Client:
    def __init__(self, port: int, token: str):
        self.port = port
        self.token = token

    def __call__(self, path: str, body=None, *, token: str | None = "__default__",
                 method: str | None = None) -> tuple[int, str]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        rq = urllib.request.Request(url, data=data,
                                    method=method or ("POST" if data else "GET"))
        tok = self.token if token == "__default__" else token
        if tok:
            rq.add_header("Authorization", "Bearer " + tok)
        if data:
            rq.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(rq, timeout=20) as rs:
                return rs.status, rs.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # 连接层失败
            return 0, repr(e)



def test_real_client_ip_behind_nginx():
    """封锁必须按**真实**来源 IP，而不是 nginx 的回环对端。

    2026-09-05 修的未认证 DoS：服务只在 `127.0.0.1:8765` 监听、由 nginx
    `proxy_pass`（`nginx.conf:715/721/728`），于是 `client_address` 对每一个
    访客都是 `127.0.0.1`。失败封锁按它索引 → 整张表只有一个桶：

        任何人对任意路径连发 5 次带假 Bearer 的请求
          → 之后 30 分钟运维本人也进不来（`_authed` 在比对密钥**之前**查封锁）
          → 容器 `restart: "no"`，不自愈

    不需要任何凭据，每 30 分钟重打 5 次即永久封锁。

    取 XFF 的**最右**一跳而不是最左：nginx 用 `$proxy_add_x_forwarded_for`
    （把 `$remote_addr` 追加到客户端已有的 XFF 后面），所以链条是
    `<客户端可伪造>, <nginx 看到的真实对端>`。取最左等于让客户端自己声明 IP
    —— 那比不读还糟：攻击者每次换个伪造 IP 就绕过封锁，运维的真实 IP 反被封。

    只在直连对端是回环时才信这个头。绑到 0.0.0.0 直接暴露时对端就是客户端，
    那时任何人都能自带 XFF 伪造来源。
    """
    import server                                      # noqa: E402

    H = server.Handler

    class _Hdrs(dict):
        def get(self, k, d=""):
            return dict.get(self, k, d)

    def mk(peer, xff=None, real=None):
        o = H.__new__(H)
        o.client_address = (peer, 1234)
        h = _Hdrs()
        if xff:
            h["X-Forwarded-For"] = xff
        if real:
            h["X-Real-IP"] = real
        o.headers = h
        return o

    for why, peer, xff, real, want in (
        ("nginx 反代、XFF 单跳", "127.0.0.1", "203.0.113.7", None,
         "203.0.113.7"),
        ("nginx 反代、客户端伪造了前缀", "127.0.0.1",
         "1.2.3.4, 203.0.113.7", None, "203.0.113.7"),
        ("nginx 反代、只有 X-Real-IP", "127.0.0.1", None, "203.0.113.9",
         "203.0.113.9"),
        ("nginx 反代、两个头都没有（回落对端）", "127.0.0.1", None, None,
         "127.0.0.1"),
        ("直连、客户端自带 XFF（不许信）", "203.0.113.50", "9.9.9.9", None,
         "203.0.113.50"),
        ("IPv6 回环反代", "::1", "2001:db8::5", None, "2001:db8::5"),
    ):
        got = mk(peer, xff, real)._client_ip()
        eq(f"来源 IP · {why}", got, want)

    print("[OK] Real client IP: 反代下取 XFF 最右一跳、直连时不信该头")


def test_failure_table_is_bounded():
    """封锁表必须有容量上限，且封锁中的条目不能被新 IP 挤掉。

    2026-09-05：修好真实 IP 提取之后，这张表的键从「恒为 127.0.0.1」变成
    **攻击者可控**，而记失败这条路径是未认证可达的。没有上限就是把一个 DoS
    换成另一个（无界字典）。

    淘汰顺序也是判据：`(是否在封锁中, last)` 升序 —— 未封锁且最早的先走。
    反过来的话攻击者可以用大量新 IP 把自己的封锁记录挤掉。
    """
    import server                                      # noqa: E402

    H = server.Handler
    saved = dict(H._failures)
    try:
        H._failures.clear()
        n = H.MAX_FAIL_ENTRIES + 500
        for i in range(n):
            H._note_failure(f"10.0.{i // 256}.{i % 256}")
        # 新增键时才 prune，所以稳态是上限 +1（当前这一条）
        truthy(f"塞 {n} 个 IP 后表不超上限",
               len(H._failures) <= H.MAX_FAIL_ENTRIES + 1,
               f"实得 {len(H._failures)} 条，上限 {H.MAX_FAIL_ENTRIES}")

        # 封锁中的条目要活下来
        H._failures.clear()
        for _ in range(H.MAX_FAILURES):
            H._note_failure("203.0.113.99")
        truthy("连续 5 次失败触发封锁", H._locked_out("203.0.113.99") > 0)
        for i in range(H.MAX_FAIL_ENTRIES + 200):
            H._note_failure(f"10.1.{i // 256}.{i % 256}")
        truthy("被大量新 IP 冲刷后封锁仍在",
               H._locked_out("203.0.113.99") > 0,
               "攻击者可以用新 IP 把自己的封锁记录挤掉")

        # 零散失败不该累积成封锁：距上次超过 TTL 就重新计数
        H._failures.clear()
        import time as _t
        for _ in range(H.MAX_FAILURES - 1):
            H._note_failure("198.51.100.1")
        H._failures["198.51.100.1"]["first"] = _t.time() - H.FAIL_TTL - 10
        H._note_failure("198.51.100.1")
        eq("跨 TTL 的零散失败不触发封锁",
           H._locked_out("198.51.100.1"), 0.0)
    finally:
        H._failures.clear()
        H._failures.update(saved)

    print("[OK] Failure table: 有容量上限、封锁中的条目不被挤掉、"
          "跨 TTL 的零散失败重新计数")


def test_store_tables_are_bounded():
    """三张内存表必须有容量上限与 TTL，正在跑的任务不被淘汰。

    2026-09-05 量化的 OOM：`/api/plan` 每次存两份**整份配置**
    （`preview` 与 `base_raw`），生产 config.yaml 约 857KB，即每次约 1.7MB，
    而 `plan_id` 每次新生成、旧条目原来永不释放。

        nginx 对 `/` 放行 240r/m → 约 400MB/分钟
        容器内存上限 512M，且 `restart: "no"` 不自愈 → 约 90 秒 OOM

    **非恶意也会撞上**：前端每次勾选变化都防抖 180ms 后调一次 `/api/plan`
    （`web/app.js`），一轮正常操作就积累几十份。

    淘汰顺序也是判据：`(是否在跑, 最后访问)` 升序 —— 空闲且最久未碰的先走。
    反过来会把正在跑的任务清掉，前端永远停在「运行中」。

    TTL 从**最后一次访问**算而不是创建时间 —— 用户盯着一个任务看半小时，
    不该因为「创建于 30 分钟前」被清掉。
    """
    import server                                      # noqa: E402

    # ① plans 有上限
    S = server.Store()
    for i in range(S.MAX_PLANS + 20):
        S.add_plan(f"p{i}", {"preview": "x" * 100, "base_raw": "x" * 100})
    truthy(f"塞 {S.MAX_PLANS + 20} 份 plan 后不超上限",
           len(S.plans) <= S.MAX_PLANS + 1,
           f"实得 {len(S.plans)} 份，上限 {S.MAX_PLANS}")

    # ② 正在跑的 job 不被淘汰
    class _Job:
        def __init__(self, jid, state):
            self.id, self.state = jid, state

    S2 = server.Store()
    S2.add_job(_Job("j-running", "running"))
    for i in range(S2.MAX_JOBS + 20):
        S2.add_job(_Job(f"j{i}", "done"))
    truthy("跑着的 job 不被新任务挤掉",
           S2.get_job("j-running") is not None,
           "前端会永远停在「运行中」")
    truthy(f"jobs 表不超上限", len(S2.jobs) <= S2.MAX_JOBS + 1,
           f"实得 {len(S2.jobs)}")

    # ③ drop_plan 立即释放（写回成功后调）
    S3 = server.Store()
    S3.add_plan("gone", {"preview": "x" * 100})
    S3.drop_plan("gone")
    eq("drop_plan 后取不到", S3.get_plan("gone"), None)

    # ④ TTL 按最后访问算，不是创建时间
    import time as _t
    S4 = server.Store()
    S4.TTL = 0.3
    S4.add_plan("stale", {"preview": "x"})
    S4.add_plan("fresh", {"preview": "x"})
    _t.sleep(0.4)
    S4.get_plan("fresh")                 # 刷新它的最后访问时间
    S4.add_plan("trigger", {"preview": "x"})   # 新增才触发淘汰
    eq("过期且未访问的被清掉", S4.get_plan("stale"), None)
    truthy("刚访问过的不被清掉", S4.get_plan("fresh") is not None,
           "TTL 按创建时间算会误清正在看的任务")

    # ⑤ sizes() 三张表都报
    S5 = server.Store()
    S5.add_plan("a", {})
    got = S5.sizes()
    eq("sizes 报三张表", sorted(got), ["applies", "jobs", "plans"])
    eq("sizes 数对得上", got["plans"], 1)

    print("[OK] Store bounds: 三张表有上限与 TTL、跑着的不被淘汰、"
          "TTL 按最后访问算、写回后主动释放 plan")


def test_job_events_are_bounded():
    """事件表必须有上限，且截断要留痕、累计计数、保留首尾。

    长任务每次 attempt 一条事件：79 个凭据最坏 2370 次请求，加画像升级与
    重试，量级几千条；而一次全量重探跑几分钟，浏览器可能整夜挂着。

    丢**中间**而不是最早的：开头几条是「任务怎么起的」（参数、候选数、
    并发数），排障时最有用；末尾是「现在在干什么」。

    省略计数必须**累加**存字段，不能从本轮的 drop 现算 —— 现算永远显示
    「省略 2 条」而实际可能省了几千条，那比不显示更糟。
    """
    import server                                      # noqa: E402

    for extra in (500, 3000):
        j = server.Job("t", [], {})
        n = j.MAX_EVENTS + extra
        for i in range(n):
            j.emit("attempt", {"i": i})
        eq(f"发 {n} 条后表不超上限（{j.MAX_EVENTS}）",
           len(j.events) <= j.MAX_EVENTS, True)
        eq(f"累计省略计数正确（发 {n} 条）", j.dropped, extra + 1)
        marks = [e for e in j.events if e.get("_trunc")]
        eq("截断留痕恰好一条", len(marks), 1)
        truthy("留痕里写了累计条数",
               str(j.dropped) in str(marks[0].get("msg", "")),
               f"实得 {marks[0].get('msg')!r}")
        ids = [e["i"] for e in j.events if "i" in e]
        eq("开头保留最早的事件", ids[0], 0)
        eq("末尾保留最新的事件", ids[-1], n - 1)
        eq(f"calls 计数不受截断影响（发 {n} 条）", j.calls, n)

    print("[OK] Job events: 有上限、丢中间保首尾、留痕带累计条数、"
          "calls 计数不受影响")


def test_plan_response_redacts_secrets():
    """全量重探的 `/api/plan` 响应里不能有明文凭据。

    2026-09-05 修的 P1：这条路的 diff 是重建后的**整个文件**，不是增量片段。
    实测生产 config.yaml：

        177 行 `api-key:` 明文 + 1 行 `secret-key`，共 349KB
        全部进浏览器 DOM，界面的「复制」按钮把它们写进系统剪贴板

    而 `server.py` 开头第 15 行写着「完整 key 只在内存里，不落日志、
    **不进 JSON 响应**（一律 masked）」—— 那条纪律在这条路径上没有兑现。

    这一项守两件事：
      ① `redact_yaml_secrets` 真的抹掉凭据，且**不动结构**（行数、缩进、
         注释、非密字段全保留）—— 否则 diff 就不可读了，而「看清整个文件
         会变成什么样」正是全量重探的价值
      ② `server.py` 那条路真的调它（只有函数写对没人调用等于没修）
    """
    import io as _io
    from cpa_probe.writeback import redact_yaml_secrets as R

    SRC = (
        'host: "127.0.0.1"\n'
        'remote-management:\n'
        '  secret-key: "$2a$10$abcdefghijklmnopqrstuv"\n'
        'api-keys:\n'
        '  - sk-client-aaaaaaaaaaaaaaaaaaaa\n'
        '  - sk-client-bbbbbbbbbbbbbbbbbbbb\n'
        'claude-api-key:\n'
        '  # 站 A 的说明注释 —— 不该被动\n'
        '  - api-key: "sk-ant-1234567890abcdefghij"   # 2026-08-20 新增\n'
        '    base-url: "https://a.example"\n'
        '    priority: 900\n'
        '    headers:\n'
        '      anthropic-beta: "context-1m-2025-08-07"\n'
        'openai-compatibility:\n'
        '  - name: "chma"\n'
        '    base-url: "https://b.example/v1"\n'
        '    api-key-entries:\n'
        '      - api-key: sk-bare-no-quotes-1234567890\n'
        '        proxy-url: "http://user:secret123@mihomo:7890"\n'
    )
    got = R(SRC)

    # ① 结构不动
    eq("脱敏后行数不变",
       len(got.splitlines()), len(SRC.splitlines()))
    truthy("注释原样保留", "站 A 的说明注释" in got)
    truthy("行尾注释原样保留", "# 2026-08-20 新增" in got)
    truthy("非密字段原样保留",
           "context-1m-2025-08-07" in got and "priority: 900" in got)
    truthy("base-url 不动", "https://a.example" in got)
    import yaml as _yaml
    a, b = _yaml.safe_load(SRC), _yaml.safe_load(got)
    eq("顶层键一致", sorted(a), sorted(b))
    eq("compat 条目数一致",
       len(b.get("openai-compatibility") or []),
       len(a.get("openai-compatibility") or []))

    # ② 凭据真的没了
    for secret in ("sk-ant-1234567890abcdefghij",
                   "$2a$10$abcdefghijklmnopqrstuv",
                   "sk-client-aaaaaaaaaaaaaaaaaaaa",
                   "sk-bare-no-quotes-1234567890",
                   "secret123"):
        truthy(f"明文已抹除：{secret[:14]}…", secret not in got,
               "凭据泄进 JSON 响应")
    # 每种形态都要真的被处理（不是碰巧不匹配）
    truthy("引号值脱敏", 'api-key: "sk-ant...ghij"' in got)
    truthy("裸值脱敏", "api-key: sk-bar...7890" in got)
    truthy("裸标量列表脱敏", "- sk-cli...aaaa" in got)
    truthy("URL 内嵌凭据只抹密码段",
           "http://user:***@mihomo:7890" in got,
           "主机与端口要留着 —— 排障最需要看那部分")

    # ③ server 那条路真的调它
    src = _io.open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
    idx = src.find('"section": "全量重建"')
    truthy("找到全量重探的 diff 构造点", idx > 0)
    window = src[idx:idx + 1200]
    truthy("全量重探的 lines 走了脱敏",
           "redact_yaml_secrets(preview)" in window,
           "diff 是整份文件，不脱敏就是 177 行明文 key 进浏览器")
    # 落盘那一份**不能**脱敏 —— 写进去就把配置毁了
    truthy("落盘用的是未脱敏原文",
           'write_local(type(self).cfg_path, entry["preview"]' in src,
           "落盘若用脱敏后的文本，config.yaml 里的 Key 会变成 sk-xxx...yyyy")

    print("[OK] Plan redaction: 结构与注释不动、五种凭据形态全抹除、"
          "URL 只抹密码段、server 真的调它、落盘仍用原文")


def test_request_numbers_are_clamped():
    """请求体里的并发与预算参数必须钳进合法区间。

    2026-09-05 修：这些值原来只做类型转换、不做区间检查，而它们直接决定
    线程数与等待时长：

        {"full_redetect": true, "max_workers": 50000}
          → BatchProber 开 5 万个站级线程，每个内部再开最多 4 个段线程
        {"timeout": 9999999, "gap": 1e9}
          → 线程被钉住；nginx 600 秒断连后 Python 侧仍在跑

    后果不止本机资源：把大量出网请求打向 121 个第三方站，可能触发站方的
    批量探测防护 —— 代价落在真实凭据上（封号），比服务挂掉更贵。

    `/api/probe` 那条路原来只有普通分支有 `min(len(hosts), 4)` 这道闸，
    全量重探那条路完全没有对应物 —— 同一个参数两种处理。
    """
    import server                                      # noqa: E402

    C = server._clamp

    # 上界
    eq("max_workers=50000 钳到上限", C({"max_workers": 50000},
                                       "max_workers", 30), 128)
    eq("timeout=9999999 钳到上限", C({"timeout": 9999999}, "timeout", 120), 300)
    eq("gap=1e9 钳到上限", C({"gap": 1e9}, "gap", 3.0), 60.0)
    eq("swap_samples=999 钳到上限",
       C({"swap_samples": 999}, "swap_samples", 3), 10)
    eq("workers=10000 钳到上限", C({"workers": 10000}, "workers", 4), 16)

    # 下界（负数会让 ThreadPoolExecutor 抛，或让节流失效）
    eq("max_workers=-5 钳到下限", C({"max_workers": -5}, "max_workers", 30), 1)
    eq("timeout=0 钳到下限", C({"timeout": 0}, "timeout", 120), 1)
    eq("gap=-1 钳到下限", C({"gap": -1}, "gap", 3.0), 0.0)

    # 类型错与 NaN 回落默认值，不能抛
    eq("非数字回落默认", C({"max_workers": "abc"}, "max_workers", 30), 30)
    eq("None 回落默认", C({"timeout": None}, "timeout", 120), 120)
    eq("NaN 回落默认", C({"gap": float("nan")}, "gap", 3.0), 3.0)
    eq("缺键回落默认", C({}, "workers", 4), 4)

    # 类型跟 default 走 —— Prober 的 workers 要 int，gap 要 float
    truthy("int 默认返回 int",
           isinstance(C({"workers": 8.7}, "workers", 4), int))
    truthy("float 默认返回 float",
           isinstance(C({"gap": 2}, "gap", 3.0), float))

    # 越界要在事件流里说出来，不能静默改用户给的值
    notes = server._clamped_note({"max_workers": 50000, "gap": 1e9,
                                  "timeout": 60})
    eq("只报越界的那两个", len(notes), 2)
    truthy("提示里带字段名与区间",
           any("max_workers" in n and "128" in n for n in notes),
           f"实得 {notes}")
    eq("合法值不报", server._clamped_note({"timeout": 60, "workers": 4}), [])

    # 两条探测路径都要调 —— 只修一条等于没修（这个项目踩过两次）
    import io as _io
    src = _io.open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
    eq("_clamp 在 job 参数上用了 13 处", src.count("_clamp(job.opts"), 13)
    truthy("单站诊断也钳", src.count("_clamp(body") >= 5,
           f"实得 {src.count('_clamp(body')} 处")
    eq("两条 job 路径都调 _emit_opt_notices（它内部报越界）",
       src.count("_emit_opt_notices(job)"), 2)

    print("[OK] Clamp: 七个参数上下界都钳、类型错与 NaN 回落默认、"
          "越界在事件流里说出来、两条路径都调")


def test_input_lines_are_capped():
    """一次能提交的凭据行数必须有上限。

    `_body` 只挡 8MB，而 8MB 全是 `url,key` 约 20 万行 —— 那些行各自展开成
    4 段探测，最坏 80 万次出网请求打向第三方站。真实用法一次几十行
    （生产配置总共 121 个条目）。

    截断而不是报 400：粘贴多了更希望「先处理前 500 行」。但必须**说出来** ——
    静默丢掉用户的输入行是最坏的处理方式。
    """
    import server                                      # noqa: E402

    cap = server.MAX_INPUT_LINES
    for n, want_note in ((10, False), (cap, False), (cap + 1, True),
                         (200000, True)):
        text = "\n".join(f"https://s{i}.example,sk-{i}" for i in range(n))
        kept, note = server._cap_lines(text)
        eq(f"{n} 行 → 保留行数", len(kept.splitlines()), min(n, cap))
        eq(f"{n} 行 → 有无提示", bool(note), want_note)
        if want_note:
            truthy(f"{n} 行的提示里带真实行数与上限",
                   str(n) in note and str(cap) in note, f"实得 {note!r}")

    # 空输入不报错
    eq("空输入", server._cap_lines(""), ("", ""))

    # 两个入口都要过这道闸
    import io as _io
    src = _io.open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
    truthy("/api/parse 与 /api/probe 都调 _cap_lines",
           src.count("_cap_lines(") >= 3,
           f"实得 {src.count('_cap_lines(')} 处（含定义）")
    # 行为断言：内联时只能断言「源码里有没有这几行」，而那挡不住把条件改成
    # `if False:` —— 撤销实验证实过。所以抽成 _emit_opt_notices 并直接测它。
    job = server.Job("t", [], {"max_workers": 50000,
                               "_truncated": "输入 900 行，超过上限"})
    server._emit_opt_notices(job)
    msgs = [str(e.get("msg", "")) for e in job.events]
    truthy("参数越界进了事件流",
           any("max_workers" in m and "钳制" in m for m in msgs),
           f"实得 {msgs}")
    truthy("截断提示进了事件流",
           any("超过上限" in m for m in msgs),
           f"实得 {msgs} —— 静默丢掉用户的输入行是最坏的处理方式")
    # 合法参数不该刷屏
    quiet = server.Job("t2", [], {"max_workers": 30, "gap": 3.0})
    server._emit_opt_notices(quiet)
    eq("参数都合法时不发提示", len(quiet.events), 0)
    # 两条路径都要调它
    eq("两条 job 路径都调 _emit_opt_notices",
       src.count("_emit_opt_notices(job)"), 2)

    print(f"[OK] Input cap: 上限 {cap} 行、截断有提示、两个入口都过闸")


def test_private_targets_are_refused():
    """探测目标与代理地址都不能指向内网 —— 否则服务端就是个内网扫描器。

    2026-09-05 修的 P2。两条路原来都只校验形态、不管**去向**：

        POST /api/diag {"url": "http://127.0.0.1:8317", "key": "x"}
          → 向内网发请求，非 200 时把 400 字节正文摘要放进
            rungs[].excerpt 同步返回
        POST /api/probe {"opts": {"proxy": "http://10.0.0.5:22"}}
          → probe_proxy 做裸 TCP 连接，把连通性、异常类名与毫秒数经
            proxy-precheck 事件回到 /api/job —— 比 HTTP 更干净的端口扫描 oracle

    **这道闸挡的是内网侦察，不是全部 SSRF**：它只看字面量地址，挡不住
    DNS rebinding（解析时公网 IP、连接时变私网）。要防那个得在 client.py
    接管地址解析，代价不小。这一点写在 `is_private_target` 的注释里，
    不能让人以为这道闸挡住了一切。

    云元数据端点本来也打不到：出网路径总在 base 后追加固定后缀
    （`/v1/models` 等），拼不出 `/latest/meta-data/...`；GCP 要的
    `Metadata-Flavor` 头也不会发。
    """
    import server                                      # noqa: E402
    from cpa_probe.parse import is_private_target as _P
    from cpa_probe.parse import parse_lines as _PL

    # ① 判据本身
    for host, want in (
        ("127.0.0.1:8317", True), ("localhost", True), ("localhost:8765", True),
        ("10.0.0.5", True), ("192.168.1.1", True), ("169.254.169.254", True),
        ("172.16.0.1", True), ("172.31.255.255", True),
        ("0.0.0.0", True),
        # 172.16.0.0/12 的边界 —— 16-31 是私网，15 与 32 不是
        ("172.15.0.1", False), ("172.32.0.1", False), ("172.253.1.1", False),
        # IPv6
        ("::1", True), ("[::1]:8080", True), ("fd00::1", True),
        ("fe80::1", True), ("::ffff:127.0.0.1", True),
        ("::ffff:8.8.8.8", False),
        # 云元数据的惯用主机名与内网域名后缀
        # metadata.google.internal 有两道规则命中（名单 + .internal 后缀），
        # 所以单独验 `metadata` 与 `instance-data` —— 它们**只**靠名单。
        # 撤销实验证实：不验这两个的话，把名单删掉测试仍然绿。
        ("metadata.google.internal", True), ("metadata", True),
        ("instance-data", True), ("metadata.goog", True),
        ("foo.local", True), ("svc.internal", True),
        # 正常上游
        ("api.openai.com", False), ("relay.example.com:8443", False),
        ("1.2.3.4", False),
    ):
        eq(f"私网判据 · {host}", bool(_P(host)), want)

    # ② 解析入口默认拒，显式放行才通（本项目的假上游套件要打 127.0.0.1）
    res = _PL("http://127.0.0.1:8317,sk-x")
    eq("默认拒私网目标", len(res.valid), 0)
    truthy("拒绝理由说清是内网",
           any("内网" in (r.error or "") for r in res.invalid),
           f"实得 {[r.error for r in res.invalid]}")
    res2 = _PL("http://127.0.0.1:8317,sk-x", allow_private=True)
    eq("显式放行后可用", len(res2.valid), 1)
    # 正常上游不受影响
    eq("公网目标照常通过",
       len(_PL("https://relay.example.com,sk-x").valid), 1)

    # ③ 代理地址同样挡
    eq("代理指向内网 → None",
       server._resolve_proxy("http://10.0.0.5:22"), None)
    eq("代理指向回环的非代理端口 → None",
       server._resolve_proxy("http://127.0.0.1:22"), None)
    eq("代理指向云元数据 → None",
       server._resolve_proxy("http://169.254.169.254:80"), None)
    truthy("公网代理照常放行",
           server._resolve_proxy("http://proxy.example.com:8080")
           == "http://proxy.example.com:8080")
    # 两个白名单地址是服务端自己写死的（compose 服务名 / 宿主机映射端口），
    # 不来自请求体 —— 它们必须仍然能用，否则代理功能整个废掉
    # 白名单地址必须走**探测分支**（去试哪个能连），不能被去向闸挡掉。
    # 判据：探测分支会调 probe_proxy —— 打个桩看它到底被调没被调。
    # 只断言返回值不行：都不通时它也返回 None，与「被挡」分不清。
    from cpa_probe import client as _client
    tried = []
    orig = _client.probe_proxy
    _client.probe_proxy = lambda url, timeout=3: (tried.append(url), (False, "stub"))[1]
    try:
        server._resolve_proxy("http://mihomo:7890")
        server._resolve_proxy("auto")
    finally:
        _client.probe_proxy = orig
    truthy("白名单地址走探测分支（两个候选都试）",
           "http://mihomo:7890" in tried and "http://127.0.0.1:7890" in tried,
           f"实得 {tried} —— 白名单被去向闸误挡，代理功能整个失效")
    # 反向：内网地址**不该**走到探测（连 TCP 都不发，那才是挡住了）
    tried.clear()
    _client.probe_proxy = lambda url, timeout=3: (tried.append(url), (True, ""))[1]
    try:
        eq("内网代理直接拒，不发 TCP",
           server._resolve_proxy("http://10.0.0.5:22"), None)
    finally:
        _client.probe_proxy = orig
    eq("被拒的地址一次 TCP 都没发", tried, [])

    print("[OK] Private target: 25 种地址判据正确、解析入口默认拒、"
          "代理同样挡、白名单地址仍可用")


def test_push_target_is_whitelisted():
    """PUT /config.yaml 的目标地址必须过白名单。

    2026-09-05 修的 P2。`reload_cpa` 的请求体是**整份 config.yaml**，头里带
    `Authorization: Bearer <CPA 管理密码>`。地址原来完全由请求体决定
    （`push.base` 优先于服务端配置），填错一次就是「177 行明文凭据 + 管理密码
    以一次 PUT 发给第三方」。

    **这件事已经发生过一次**：前端那个输入框曾硬编码
    `https://cpa.example.com`，那次请求确实出了公网，只是被 Cloudflare
    挡在 403。当时改的是取值顺序（服务端配置优先）—— 降低了误配概率，
    但没关掉这条出口。

    能触发的人已经掌握 CPA 管理密码（`mgmt` 非空的前提），所以这**不是**
    权限提升；这道闸防的是误配与内部人一次性外发。

    与 `is_private_target` 的方向**相反**：那边挡私网（防拿服务端扫内网），
    这边只放私网（防把凭据发出公网）。两者不矛盾 —— 判据都是「这个地址该不该
    是这条路的目标」，只是两条路的正常目标恰好互补。
    """
    import server                                      # noqa: E402

    F = server._push_target_ok

    # 放行：docker 服务名（生产实配就是这个）、回环、私网
    for base in ("http://cli-proxy-api:8317",      # docker-compose 实配
                 "http://127.0.0.1:8317",
                 "http://localhost:8317",
                 "http://10.0.0.9:8317",
                 "http://[::1]:8317"):
        eq(f"放行 {base}", F(base, ""), "")

    # 拒：公网地址
    for base in ("http://cpa.chiangma.com",
                 "https://evil.example.com",
                 "https://api.openai.com"):
        why = F(base, "")
        truthy(f"拒绝 {base}", bool(why))
        truthy(f"{base} 的拒绝理由说清了后果",
               "管理密码" in why or "整份配置" in why,
               f"实得 {why!r}")

    # 服务端 --cpa-url 显式配置的那个 host 放行 —— 运维写死的比请求体可信
    eq("配置过的公网 host 放行",
       F("https://cpa.chiangma.com", "https://cpa.chiangma.com"), "")
    truthy("配了 A 却要发给 B → 拒",
           bool(F("https://evil.example.com", "https://cpa.chiangma.com")))

    # 形态错的一律拒
    for base in ("ftp://x", "not-a-url", "//no-scheme.example.com",
                 "http://"):
        truthy(f"形态错拒绝：{base!r}", bool(F(base, "")))

    # 空地址不算错 —— 调用方另行处理（跳过重载并给告警）
    eq("空地址交给调用方", F("", ""), "")

    # 被拒时必须**真的跳过**重载，而不只是记个消息
    import io as _io
    src = _io.open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
    idx = src.find("refused = _push_result(")
    truthy("apply 收尾里调了白名单", idx > 0)
    window = src[idx:idx + 700]
    truthy("被拒后把 cpa_base 清空（后续验证也跳过）",
           'cpa_base = ""' in window,
           "只记消息不清空的话，下面的 reload_cpa 照样会发出去")
    # 行为断言：源码里「有没有这几个键」挡不住「赋的值是 True」。
    # 直接跑那段逻辑 —— 把它抽成 _push_result 才测得动。
    r_bad = server._push_result("https://evil.example.com",
                                "http://cli-proxy-api:8317")
    truthy("被拒时 push_ok 为假",
           r_bad is not None and r_bad.get("push_ok") is False,
           f"实得 {r_bad}")
    truthy("被拒时 reload_ok 也为假",
           r_bad is not None and r_bad.get("reload_ok") is False,
           f"实得 {r_bad}")
    truthy("被拒时消息里带原因",
           r_bad and "管理密码" in str(r_bad.get("reload_msg", "")),
           f"实得 {r_bad}")
    eq("放行时不产生结果（交给正常流程）",
       server._push_result("http://cli-proxy-api:8317",
                           "http://cli-proxy-api:8317"), None)

    print("[OK] Push target: docker 服务名/回环/私网放行、公网拒且说清后果、"
          "--cpa-url 配置的 host 例外、被拒后真的跳过重载")


def test_export_redaction_covers_all_leak_shapes():
    """导出脚本的脱敏规则必须覆盖每一种凭据形态，自证也要跟着覆盖。

    2026-09-05：`tools/export-logs.sh` 原有五条规则，实测漏掉六种形态 ——
    每种都造样本验过，补之前它们原样出现在导出文件里：

        ?token=xxx          投喂台的 query 登录方式（**最要紧**，见下）
        AIza...             gemini 段的 Key，五条规则一个都没匹配上
        $2a$10$...          bcrypt 哈希（config.yaml 的 secret-key）
        x-api-key: 裸值      原来只覆盖 JSON 引号形态与 x-goog-api-key
        user:pass@host      proxy-url 内嵌凭据
        TOKEN=xxx           环境变量与管理密码的赋值形态

    `?token=` 那条最要紧：nginx 的 `log_format main` 记 `$request`，而启动
    横幅直接给 `http://host:port/?token=<token>` 形式的入口 —— 第一次访问就
    落进 access.log。而这个 token **等价于 CPA 写权限**，比单个上游 Key 更值钱。
    这个脚本的用途恰恰是「脱敏后打包外发」。

    这一项按「规则表 vs 形态表」对账。shell 脚本的执行本套件测不到，但漏一种
    形态这件事是**规则集与清单不匹配**，而那查得出来 —— 这次漏六种的成因正是
    「规则逐个加，没有清单跟它对照」。
    """
    import io as _io

    src = _io.open(os.path.join(ROOT, "tools", "export-logs.sh"),
                   encoding="utf-8").read()
    # 只看**替换规则块**（sed 的那一段），不看全文。
    #
    # 为什么必须限定范围（2026-09-05 撤销实验发现）：同一个判据片段在这个脚本里
    # 出现 2-3 次 —— 替换规则、文件筛选的 grep、以及自证的 grep 各一份。
    # 按全文查的话，删掉**替换规则**那一条测试仍然绿（另两处还在），
    # 而那正是「凭据不被抹掉」的那一条。
    _i = src.index("sed -i -E")
    rules = src[_i:src.index('"$f"', _i)]

    # 每种形态 → 规则里必须出现的判据片段
    SHAPES = {
        "sk- 前缀（claude / openai 系）": "sk-[A-Za-z0-9_-]",
        "AIza 前缀（gemini 段）": "AIza[A-Za-z0-9_-]",
        "Bearer 头": "(Bearer )",
        "JSON 形态的 api_key / secret_key": '"(api[_-]?key|x-api-key',
        "x-goog-api-key 头": "x-goog-api-key",
        "query ?key=": "[?&]key=",
        "query ?token=（投喂台登录凭据）": "(token|access_token|auth)=",
        "bcrypt 哈希": r"\$2[aby]\$",
        "裸值形态的鉴权头": "(api|goog-api|anthropic)-key:",
        "URL 内嵌 user:pass@": "[^:@[:space:]",
        "环境变量赋值": "IMPORTER_TOKEN",
        "mgmt_key 赋值": "mgmt[_-]?key",
    }
    missing = [why for why, needle in SHAPES.items() if needle not in rules]
    assert not missing, (
        "export-logs.sh 的脱敏规则没覆盖这些形态 —— 它们会原样进导出包，"
        f"而那个包的用途是贴给别人看：\n  " + "\n  ".join(missing))

    # 自证也要覆盖 —— 只自证 sk- 与 Bearer 的话，漏掉的形态自证也看不见，
    # 那时「脱敏自证通过」这句话是在给一个没验过的结论背书
    i = src.find("脱敏后仍检出疑似凭据")
    truthy("有脱敏自证这一步", i > 0)
    guard = src[max(0, i - 1400):i]
    for why, needle in (("AIza", "AIza[A-Za-z0-9_-]{20,}"),
                        ("?token=", "(token|access_token|auth)="),
                        ("bcrypt", r"\$2[aby]\$[0-9]{2}\$"),
                        ("URL 内嵌凭据", "[^:@[:space:]")):
        truthy(f"自证覆盖 {why}", needle in guard,
               f"自证看不见这种形态，「自证通过」就是空话")

    # 自证必须排除占位符自身 —— `****REDACTED` 也满足「12 个以上非空白字符」，
    # 不排掉会把自己的替换结果当成残留（实测 3 行误报）
    truthy("自证排除了 REDACTED 占位符",
           "grep -qv 'REDACTED'" in guard or "REDACTED'" in guard,
           "不排除的话每次导出都误报，人会开始忽略这个告警")

    # 检出后必须**不打包**并退非零 —— 这是硬闸
    tail = src[i:i + 400]
    truthy("检出后保留目录不打包", "不打包" in tail)
    truthy("检出后退出码非零", "exit 2" in tail)

    print(f"[OK] Export redaction: {len(SHAPES)} 种凭据形态全覆盖、"
          "自证同步覆盖并排除占位符、检出即不打包")


def test_errors_do_not_leak_stack_traces():
    """500 响应与 job.error 不能带 traceback，只带一个引用 id。

    2026-09-05 修的 P2。原来 500 响应体里直接是
    `traceback.format_exc(limit=4)`，而 `job.error` 也是它 —— 后者会进
    `/api/job` 事件流**与 `/api/export` 的 txt**，而那个 txt 的设计用途
    就是「贴给别人看」（见 `_api_export` 的 docstring）。

    `format_exc` 不含局部变量，所以不直接吐密钥值。泄露的是容器内文件布局、
    模块结构与代码行号 —— 那降低后续利用成本。任何畸形入参都能拿到一段。

    换成 id 之后排障链路没变短：`docker compose logs | grep <id>` 就能定位
    完整栈，而客户端只看到「服务内部错误（err-3f2a1b）」。
    """
    import io as _io

    # ① _error_ref 的行为：返回短 id，完整栈只进 stderr
    import server                                      # noqa: E402

    buf = _io.StringIO()
    real = sys.stderr
    sys.stderr = buf
    try:
        try:
            raise RuntimeError("probe-for-test")
        except RuntimeError:
            ref = server._error_ref("unit-test")
    finally:
        sys.stderr = real
    written = buf.getvalue()

    truthy("返回的 id 形如 err-xxxxxx",
           ref.startswith("err-") and len(ref) == 10, f"实得 {ref!r}")
    truthy("完整栈写进了 stderr", "Traceback" in written)
    truthy("stderr 里带同一个 id", ref in written)
    truthy("stderr 里带出错位置", "unit-test" in written)
    # 两次调用的 id 不同 —— 否则日志里对不上是哪一次
    sys.stderr = _io.StringIO()
    try:
        try:
            raise RuntimeError("x")
        except RuntimeError:
            ref2 = server._error_ref("unit-test-2")
    finally:
        sys.stderr = real
    truthy("每次调用给不同的 id", ref != ref2, f"{ref} == {ref2}")

    # ② 源码里不该再有「把栈放进响应或 job.error」的写法
    src = _io.open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    truthy("响应体不再带 trace 字段",
           '"trace": traceback' not in code,
           "500 响应里的栈会泄露容器内文件布局与行号")
    for field in ("job.error = ", "task.error = "):
        idx = 0
        while True:
            idx = code.find(field, idx)
            if idx < 0:
                break
            line = code[idx:code.find("\n", idx)]
            truthy(f"{field.strip()} 用 _error_ref 而非 format_exc",
                   "_error_ref" in line,
                   f"实得 {line.strip()!r} —— 它会进导出的 txt")
            idx += 1

    print("[OK] Error ref: 栈只进 stderr、响应只带短 id、每次 id 不同、"
          "job/task.error 也不再带栈")


def test_get_route_has_a_catch_all():
    """GET 也要有兜底与参数校验 —— 与 POST 对称。

    2026-09-05 修：`since` 那一步的 `int(parse_qs(...))` 原来在 try 之外，
    于是 `GET /api/job/<jid>?since=x` 让 ValueError 冒到 socketserver 的
    `handle_error` —— 客户端拿到的是**连接重置**而不是 400。

    前端会把连接重置计入「轮询失败」并重试，于是一个打错的参数变成无限重试；
    而 stderr 里堆的是无归属的 traceback。

    畸形参数给 **400** 而不是 500：那是调用方的问题，不是服务故障。前端的
    轮询容错把 5xx 当「服务挂了」处理，语义不同。
    """
    import io as _io

    src = _io.open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()

    # do_GET 必须整体包在 try 里，且委托给一个实现方法
    i = src.find("def do_GET(self)")
    truthy("找到 do_GET", i > 0)
    head = src[i:i + 1400]
    truthy("do_GET 有 try 兜底", "try:" in head and "except Exception" in head)
    truthy("兜底里用 _error_ref", "_error_ref(" in head)
    truthy("委托给 _do_get 实现", "self._do_get()" in head)

    # since 的解析必须有 try 且畸形值回 400。
    #
    # 剥掉注释再查（2026-09-05 撤销实验发现）：注释里就写着「400 而不是 500」，
    # 所以按含不含 "400" 查的话，把真正的 `_json(400` 改成 `_json(500`
    # 测试仍然绿 —— 那个数字在注释里也有。
    j = src.find("raw_since")
    truthy("since 走了健壮解析", j > 0)
    win = "\n".join(ln for ln in src[j:j + 700].splitlines()
                    if not ln.lstrip().startswith("#"))
    truthy("since 非法时回 400", "_json(400" in win,
           f"回 500 的话前端会当服务挂了无限重试；实得 {win[:200]!r}")
    truthy("since 非法时不回 5xx", "_json(500" not in win)
    truthy("since 负数被钳成 0", "max(0, int(raw_since))" in win)

    print("[OK] GET catch-all: do_GET 有兜底、since 非法回 400、负数钳到 0")

def main() -> int:
    # 不传路径就用自带的最小样本 —— 绝不回落到 ../config.yaml（生产配置）。
    # 原来那样做有两个后果，都实测踩到了（2026-08-31）：
    #   · 刚 clone 的仓库与 CI runner 上直接 return 2，而 CI 明确不带路径调用
    #   · 断言挂在一个会变的生产文件上：那份 config.yaml 一改，
    #     「gemini 五档」这类硬编码基线就失效，报红却指不出真缺陷
    # 另外这个套件会起真服务、可能在 config 目录旁留下 .bak —— 更不该指向生产文件。
    cfg_path, _synthetic, _tmp = fixture_cfg.resolve(sys.argv, label="HTTP 契约")

    before = io.open(cfg_path, encoding="utf-8").read()
    # 目录里可能早就有历史备份（VPS 上就有 08-28/08-29 那几个）。
    # 断言要看的是「本次测试没新增」，不是「目录里一个都没有」。
    cfg_dir = os.path.dirname(cfg_path)
    baks_before = {f for f in os.listdir(cfg_dir) if ".bak-" in f}
    port, token = free_port(), "regress-token-" + os.urandom(4).hex()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py"),
         "--config", cfg_path, "--port", str(port), "--token", token],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env)

    req = Client(port, token)
    try:
        up = False
        for _ in range(80):
            time.sleep(0.25)
            if proc.poll() is not None:
                print("服务启动即退出：\n" + (proc.stdout.read() if proc.stdout else ""))
                return 1
            st, _b = req("/api/context")
            if st:
                up = True
                break
        eq("服务启动", up, True)
        if not up:
            return 1

        section("鉴权闸门")
        eq("无 token 401", req("/api/context", token=None)[0], 401)
        eq("错 token 401", req("/api/context", token="wrong")[0], 401)
        st, body = req("/api/context")
        eq("正确 token 200", st, 200)
        ctx = json.loads(body)
        eq("四段齐全", sorted(ctx["sections"]), sorted(ctx["section_order"]))
        # 档位谱要与**当前这份** config.yaml 对得上，而不是与某个写死的数字。
        # 原来写的是「claude 顶档 1000」「gemini 五档」—— 那是自带样本的形状，
        # 传真实 config.yaml 进来就必然失败（2026-08-31 实测：gemini 实为六档）。
        # 断言挂在会变的外部文件上，是这一整轮修的同一类缺陷：报红却指不出
        # 任何真问题。改成从被测的那份文件现算期望值，两种输入都成立。
        import yaml as _yaml
        _cfg = _yaml.safe_load(io.open(cfg_path, encoding="utf-8").read())
        for _sec in ("gemini-api-key", "codex-api-key", "claude-api-key",
                     "openai-compatibility"):
            _pris = {e.get("priority") for e in (_cfg.get(_sec) or [])
                     if isinstance(e, dict) and isinstance(e.get("priority"), int)}
            eq(f"{_sec} 档位数与文件一致",
               len(ctx["sections"][_sec]["tiers"]), len(_pris))
            if _pris:
                eq(f"{_sec} 顶档与文件一致",
                   ctx["sections"][_sec]["top"], max(_pris))
        eq("context 不含明文 key", "sk-" in body, False)

        section("失败封锁 · 直接调类方法（不走 HTTP，免污染后续用例）")
        # 这段锁住一个曾经形同虚设的 bug：_locked_out 在「未封锁」分支里
        # 顺手把 count 也清零，而 _authed 每次都调它 —— 于是失败计数永远
        # 回到 0，5 次封锁永不触发。当时全部测试仍是绿的。
        import importlib.util
        _spec = importlib.util.spec_from_file_location(
            "srv_under_test", os.path.join(ROOT, "server.py"))
        _m = importlib.util.module_from_spec(_spec)
        sys.modules["srv_under_test"] = _m
        _spec.loader.exec_module(_m)
        H = _m.Handler

        eq("阈值与 CPA 一致（5 次）", H.MAX_FAILURES, 5)
        eq("封锁时长与 CPA 一致（30 分钟）", H.BAN_SECONDS, 30 * 60)

        ip = "203.0.113.77"
        counts = []
        for _ in range(4):
            H._note_failure(ip)
            H._locked_out(ip)                    # 关键：模拟 _authed 每次都查
            with H._fail_lock:
                counts.append((H._failures.get(ip) or {}).get("count"))
        eq("失败计数真的在累加", counts, [1, 2, 3, 4])
        eq("第 4 次后尚未封锁", H._locked_out(ip), 0.0)

        H._note_failure(ip)                       # 第 5 次
        eq("第 5 次触发封锁", H._locked_out(ip) > 0, True)
        eq("封锁时长接近 30 分钟", 1700 < H._locked_out(ip) <= 1800, True)

        H._note_success(ip)
        with H._fail_lock:
            eq("成功登录清空该 IP 记录", ip in H._failures, False)

        other = "198.51.100.9"
        for _ in range(5):
            H._note_failure(other)
        eq("按 IP 隔离：该 IP 被封", H._locked_out(other) > 0, True)
        eq("按 IP 隔离：别的 IP 不受影响", H._locked_out("192.0.2.1"), 0.0)

        H.BAN_SECONDS = 1                         # 缩短以验证自动解封
        expire_ip = "192.0.2.55"
        for _ in range(5):
            H._note_failure(expire_ip)
        eq("缩短后仍会封锁", H._locked_out(expire_ip) > 0, True)
        time.sleep(1.2)
        eq("封锁到期自动解封", H._locked_out(expire_ip), 0.0)
        H.BAN_SECONDS = 30 * 60

        section("CPA 管理密码登录")
        # config.yaml 里的 secret-key 是 bcrypt 哈希（CPA 首次加载时自动转换，
        # config_load.go:104-113）。用户输原始密码，服务端做 bcrypt 比对。
        #
        # 这里的 H 是 importlib 另行加载的一份 Handler，它的 cfg_path 是空的
        # （真正带 --config 的那份在子进程里）。不显式设就会一路走
        # 「读不到 → 返回空 → 跳过」，把测试写成了永远不检查。
        H.cfg_path = cfg_path
        h = H._cpa_mgmt_hash()
        if h:
            eq("读到的是 bcrypt 形态", h.startswith(("$2a$", "$2b$", "$2y$")), True)
            eq("哈希本身不能当密码", H._check_cpa_password(h), False)
        else:
            print("     -- config.yaml 的 secret-key 非 bcrypt 形态，跳过")
        eq("空密码一律拒绝", H._check_cpa_password(""), False)
        try:
            import bcrypt  # noqa: F401
            has_bcrypt = True
        except ImportError:
            has_bcrypt = False

        # 两条分支都必须有断言，否则「装了 bcrypt 的机器」上这段等于没测。
        # 之前只写了 not has_bcrypt 那半边：本机（无 bcrypt）跑 333 项，
        # VPS（有 bcrypt）跑 332 项，而少掉的恰好是唯一验证「错密码被拒」的那条。
        if has_bcrypt:
            # 用已知密码现算一个哈希，验证 bcrypt 比对真的在工作
            salt = bcrypt.gensalt(rounds=4)          # 4 轮：测试要快
            real = bcrypt.hashpw(b"correct-horse", salt).decode()
            saved = H.cfg_path
            try:
                import tempfile
                fd, tmp = tempfile.mkstemp(suffix=".yaml")
                os.close(fd)
                io.open(tmp, "w", encoding="utf-8").write(
                    "remote-management:\n  secret-key: \"%s\"\n" % real)
                H.cfg_path = tmp
                eq("正确密码通过 bcrypt 比对",
                   H._check_cpa_password("correct-horse"), True)
                eq("错密码被拒", H._check_cpa_password("wrong-horse"), False)
                eq("哈希本身当密码用被拒", H._check_cpa_password(real), False)
                eq("空密码被拒", H._check_cpa_password(""), False)
            finally:
                H.cfg_path = saved
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        else:
            eq("未装 bcrypt 时该路径安全关闭",
               H._check_cpa_password("anything"), False)
            print("     ↑ 不退化成明文比较")
            print("     注：装了 bcrypt 的机器会多跑 4 项真实比对用例")

        section("静态资源")
        st, html = req("/", token=None)          # 首页允许免鉴权，token 由前端带
        eq("首页 200", st, 200)
        eq("首页是 HTML", html.lstrip().startswith("<!"), True)
        eq("app.js 200", req("/static/app.js", token=None)[0], 200)
        eq("路径穿越被挡", req("/static/../server.py", token=None)[0], 403)
        eq("越界路径不返回源码", "BaseHTTPRequestHandler"
           in req("/static/../server.py", token=None)[1], False)

        section("解析路由")
        st, body = req("/api/parse", {
            "text": "https://x.example.com,sk-test1234567890\n"
                    "https://y.example.org/v1,sk-abcd9876543210\n"
                    "badline-no-comma\n"})
        eq("parse 200", st, 200)
        pj = json.loads(body)
        eq("有效 2", len(pj["valid"]), 2)
        eq("无效 1", len(pj["invalid"]), 1)
        eq("codex 补 /v1", pj["valid"][0]["bases"]["codex-api-key"],
           "https://x.example.com/v1")
        eq("claude 不带 /v1", pj["valid"][0]["bases"]["claude-api-key"],
           "https://x.example.com")
        eq("compat 补 /v1", pj["valid"][1]["bases"]["openai-compatibility"],
           "https://y.example.org/v1")
        eq("key 已脱敏", pj["valid"][0]["key_masked"], "sk-tes...7890")
        eq("无 api_key 字段", "api_key" in pj["valid"][0], False)
        eq("响应无明文 key", "sk-test1234567890" in body, False)
        eq("空文本不报错", req("/api/parse", {"text": ""})[0], 200)

        section("写回三道闸门")
        eq("未知 job 的 plan → 404", req("/api/plan", {"job_id": "nope"})[0], 404)
        eq("未知方案 apply → 404",
           req("/api/apply", {"plan_id": "nope", "confirm": True})[0], 404)
        st, body = req("/api/apply", {"plan_id": "nope", "confirm": False})
        eq("未确认被拒", st in (400, 404), True)

        section("未知路由")
        eq("GET 未知 404", req("/api/nope")[0], 404)
        eq("POST 未知 404", req("/api/nope", {"a": 1})[0], 404)

        section("原文件")
        eq("config.yaml 逐字节未变",
           io.open(cfg_path, encoding="utf-8").read(), before)
        baks_after = {f for f in os.listdir(cfg_dir) if ".bak-" in f}
        eq("本次未新增 .bak", sorted(baks_after - baks_before), [])

        section("密文比较 · 非 ASCII 安全")
        # hmac.compare_digest(str, str) 在任一边含非 ASCII 字符时抛 TypeError：
        #   comparing strings with non-ASCII characters is not supported
        # CPA 的管理密码完全可能含中文。抛异常会变成 500，看起来像服务坏了，
        # 而不是「密码不对」。_same_secret 先 encode 成 bytes 再比，避开限制。
        same = _m._same_secret
        eq("相同 ASCII 判真", same("abc123", "abc123"), True)
        eq("不同 ASCII 判假", same("abc123", "abc124"), False)
        eq("不等长判假（不抛）", same("abc", "abcdef"), False)
        eq("相同中文密码判真", same("密码很长很安全", "密码很长很安全"), True)
        eq("不同中文密码判假", same("密码很长很安全", "密码很长很危险"), False)
        eq("中文对 ASCII 判假", same("密码", "mima"), False)
        eq("emoji 也不抛", same("pw🔑", "pw🔑"), True)
        eq("两边都空判假", same("", ""), False)
        eq("单边空判假", same("abc", ""), False)
        # 混合：一边纯 ASCII 一边非 ASCII —— 最容易抛的组合
        eq("ASCII 对中文不抛且判假", same("abcdef", "中文密码"), False)

        # 这两项不需要活着的服务（纯类方法），但放在这里能复用同一份统计
        section("来源 IP 与封锁表")
        test_real_client_ip_behind_nginx()
        test_failure_table_is_bounded()

        section("内存表上限")
        test_store_tables_are_bounded()
        test_job_events_are_bounded()

        section("响应脱敏")
        test_plan_response_redacts_secrets()

        section("参数区间与行数上限")
        test_request_numbers_are_clamped()
        test_input_lines_are_capped()

        section("内网去向")
        test_private_targets_are_refused()
        test_push_target_is_whitelisted()

        section("导出脱敏")
        test_export_redaction_covers_all_leak_shapes()

        section("错误不泄露栈")
        test_errors_do_not_leak_stack_traces()
        test_get_route_has_a_catch_all()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        # 样本目录留到最后再删 —— 上面的断言要检查目录里有没有多出 .bak
        if _tmp:
            import shutil
            shutil.rmtree(_tmp, ignore_errors=True)

    print("\n" + "=" * 66)
    if _fail:
        print(f"失败 {len(_fail)} 项 / 通过 {_pass} 项\n")
        for f in _fail:
            print("  ✗ " + f)
        return 1
    print(f"全部通过 · {_pass} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
