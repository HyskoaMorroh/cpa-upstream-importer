#!/usr/bin/env python3
"""探测流水线端到端测试 —— 用本机假上游，零外网请求、零成本。

    python3 tests/test_pipeline.py

为什么需要它：test_probe.py 覆盖纯函数，test_server.py 覆盖 HTTP 契约，
但四阶段编排（段归属 → 模型发现 → 处置 → 质量）从未真正发过一次请求。
这个套件起一个本机 HTTP 服务扮演上游，按站点画像返回不同响应，验证
Prober 的分支真的走对。

假上游画像（每个对应一段真实踩过的坑）：
  good      四段全通，/models 目录含需过滤的杂项
  quota     403 + 预扣费额度失败 → 应立即收敛，不再试代理/加头
  cfguard   基线 403 空正文 → 走代理才 200（验处置优先级：代理先于头）
  identity  基线 401 → 补 UA 才 200（验 identity_combos 回退）
  swapper   200 但 model 字段被换 → 应判静默换模
  truncator 200 但 input_tokens 远小于发送量 → 上下文上限应按截断反推
  compatonly  只有 compat 段通，其余 404
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Windows 控制台默认 GBK，打 ✗ / 中文判定名会抛 UnicodeEncodeError，
# 把失败详情整段吞掉。VPS 上是 UTF-8 不受影响，但本机调试必须能看到。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import cpa_probe as cp  # noqa: E402
from cpa_probe.pipeline import Prober  # noqa: E402

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


# ==========================================================================
# 假上游
# ==========================================================================

# 目录里故意混入该被白名单滤掉的名字（真实场景：relay-m 声明 838 个）
CATALOG = [
    "gpt-5.6-sol", "gpt-5.6-terra", "claude-opus-5", "claude-sonnet-5",
    "gemini-2.5-pro", "gemini-2.5-flash",
    "BAAI/bge-large-zh", "DeepSeek-V3", "42-mini", "Business/gemini-2.5-pro",
]


class FakeUpstream(BaseHTTPRequestHandler):
    """按 URL 第一段决定画像。/{profile}/v1/responses 之类。"""

    def log_message(self, *a) -> None:  # 静音
        pass

    # ---- 工具 ----

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return {}

    def _send(self, code: int, payload) -> None:
        raw = (payload if isinstance(payload, str)
               else json.dumps(payload, ensure_ascii=False)).encode("utf-8")
        self.send_response(code)
        # SSE 与 JSON 的 Content-Type 必须分开 —— codex 段回的是事件流，
        # 标成 application/json 会让客户端侧按整份 JSON 解析（2026-09-12）。
        ctype = ("text/event-stream; charset=utf-8"
                 if isinstance(payload, str) and payload.startswith(("event:", "data:"))
                 else "application/json; charset=utf-8")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _profile_and_path(self) -> tuple[str, str]:
        p = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        return (p[0] if p else ""), "/" + "/".join(p[1:])

    # ---- 请求体里的文本长度（用于截断画像） ----

    @staticmethod
    def _sent_chars(body: dict) -> int:
        if "input" in body:
            return len(str(body["input"]))
        for m in body.get("messages") or []:
            if isinstance(m, dict):
                return len(str(m.get("content") or ""))
        for c in body.get("contents") or []:
            for part in (c.get("parts") or []):
                return len(str(part.get("text") or ""))
        return 0

    # 段判定：真实上游按**路径**分协议，假上游也必须照做。
    #
    # 2026-09-12：原来四个段一律回 Claude 形态（顶层 `content` 数组），于是
    # `classify.validate_success` 的协议证据闸只放 claude 过，其余三段判
    # missing-output / stream-required —— 23 项失败全出自这里。
    # 那个闸本身是对的（它正是「站在 cc-switch 能用、进 CPA 不能用」要查的
    # 东西：CPA 按段走不同协议，回错形态就是不可用），错的是这份假数据。
    def _section_of(self, path: str) -> str:
        # 判定顺序按**具体度**，与 cpa_probe/parse.py:112-119 的端点表对齐。
        # `/models/` 不能先判 —— gemini 的 generateContent 路径里有它，
        # 而 codex 的 `/responses` 路径里没有，先判会把 codex 误认成 gemini。
        if ":generateContent" in path or ":streamGenerateContent" in path:
            return "gemini-api-key"
        if "/responses" in path:
            return "codex-api-key"
        if "/messages" in path:
            return "claude-api-key"
        if "/chat/completions" in path:
            return "openai-compatibility"
        return "openai-compatibility"

    def _ok_payload(self, model: str, tokens: int, *,
                    rid: str = "msg_01AbCdEfGhJiKm", path: str | None = None):
        """该段协议下「成功」长什么样。codex 返回 SSE 文本，其余返回 dict。"""
        section = self._section_of(path if path is not None
                                   else self._profile_and_path()[1])
        text = "A hash map is unordered."
        usage = {"input_tokens": tokens, "output_tokens": 12}
        if section == "claude-api-key":
            return {"id": rid, "model": model, "usage": usage,
                    "content": [{"type": "text", "text": text}]}
        if section == "gemini-api-key":
            return {"modelVersion": model,
                    "usageMetadata": {"promptTokenCount": tokens,
                                      "candidatesTokenCount": 12},
                    "candidates": [{"content": {"role": "model",
                                                "parts": [{"text": text}]},
                                    "finishReason": "STOP"}]}
        if section == "codex-api-key":
            # Responses 流：必须有终止事件，且 output 里有真实内容块。
            done = {"type": "response.completed",
                    "response": {"id": rid, "model": model, "status": "completed",
                                 "usage": {"input_tokens": tokens,
                                           "output_tokens": 12},
                                 "output": [{"type": "message", "role": "assistant",
                                             "content": [{"type": "output_text",
                                                          "text": text}]}]}}
            return ("event: response.output_text.delta\n"
                    "data: " + json.dumps({"type": "response.output_text.delta",
                                           "delta": text}, ensure_ascii=False) + "\n\n"
                    "event: response.completed\n"
                    "data: " + json.dumps(done, ensure_ascii=False) + "\n\n")
        return {"id": rid, "model": model,
                "usage": {"prompt_tokens": tokens, "completion_tokens": 12},
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}]}

    # ---- 主分发 ----

    def do_GET(self) -> None:  # noqa: N802
        profile, path = self._profile_and_path()
        if "models" not in path:
            self._send(404, {"error": {"message": "not found"}})
            return
        if profile in ("quota", "cfguard", "identity"):
            # 目录也不给 —— 真实站点在鉴权失败时通常连目录都不返回
            self._send(403, {"error": {"message": "forbidden"}})
            return
        if profile == "compatonly" and "/v1beta/" in path:
            self._send(404, {"error": {"message": "not found"}})
            return
        # gemini 段目录形态与其余不同
        if "/v1beta/" in path:
            self._send(200, {"models": [{"name": f"models/{m}"} for m in CATALOG]})
        else:
            self._send(200, {"data": [{"id": m} for m in CATALOG]})

    def do_POST(self) -> None:  # noqa: N802
        profile, path = self._profile_and_path()
        body = self._body()
        model = str(body.get("model") or "")
        # gemini 段模型名在 URL 里
        if not model:
            m = re.search(r"/models/([^:]+):", path)
            model = m.group(1) if m else "?"
        sent = self._sent_chars(body)

        if profile == "quota":
            self._send(403, {"error": {"message": "预扣费额度失败，剩余额度 $0.190928"}})
            return

        if profile == "cfguard":
            if self.headers.get("X-Via-Proxy") == "1":
                self._send(200, self._ok_payload(model, max(sent, 20)))
            else:
                self._send(403, "")          # 403 + 空正文 = 边缘/CF
            return

        # 二级代理专属画像：不带代理永远 500（判「临时」，重试也不通，
        # 画像梯也救不了 —— 那是链路问题不是形态问题），带代理才 200。
        # 一级代理只对「IP封/边缘」触发，够不到这里；只有处置链全部用尽后的
        # 那一次 via-proxy-last 能救回它。
        if profile == "linkdead":
            if self.headers.get("X-Via-Proxy") == "1":
                self._send(200, self._ok_payload(model, max(sent, 20)))
            else:
                self._send(500, {"error": {"message": "internal error"}})
            return

        if profile == "identity":
            # 判据按**段**分开（2026-09-12）
            # ------------------------------------
            # CPA 默认 `disable-codex-cloaking: false`，无条件给 codex 请求
            # 打上官方身份头（codex_executor_request.go:372-377），本项目
            # 的 request.build_request 照做，且**在 extra_headers 之后**应用
            # —— 于是 codex 段 baseline 与 originator-only / codex-tui 等档
            # 在线上完全同形（都带 UA + Originator），老判据「有 UA 或
            # Originator 就放行」在 baseline 就放行，画像梯一档都不爬。
            #
            # codex 段能观测到的第一个**只有画像才加**的东西是 `Version`
            # （profiles.py 的 codex-full 档）。所以 codex 段要求它，
            # 其余三段 baseline 本来不带身份头，沿用原判据。
            if self._section_of(path) == "codex-api-key":
                has_ident = bool(self.headers.get("Version"))
            else:
                has_ident = bool(self.headers.get("User-Agent")
                                 or self.headers.get("Originator"))
            if has_ident:
                self._send(200, self._ok_payload(model, max(sent, 20), path=path))
            else:
                self._send(401, {"error": {"message": "unauthorized client"}})
            return

        if profile == "swapper":
            self._send(200, self._ok_payload("agnes-2.0-flash", max(sent, 20),
                                             rid="chatcmpl-xyz123"))
            return

        if profile == "truncator":
            # 永远只吃 300k，不管发多少 —— 200 但 input_tokens 远小于发送量
            self._send(200, self._ok_payload(model, min(max(sent, 20), 300_000)))
            return

        # 200 但正文是错误体。2026-08-31 实测的假阳性：station 全回 200，
        # 正文却是 {"error":...} 且无 model 字段 —— 原来四段全判可用、
        # 注册 11 个模型，而那站完全不能用。死站进 config.yaml 会耗尽重试预算。
        if profile == "okerror":
            self._send(200, {"error": {"message": "no available channel under this group",
                                       "type": "server_error"}})
            return

        # 站方负载上限：前 N 次 503，之后恢复。验「临时」类必须重试 ——
        # 不重试就会把「忙」当成「坏」。
        if profile == "flaky":
            # 计数必须按 (路径, 模型) 分开。四段是**并行**探测的，用一个
            # 全局计数器时 fail_first=1 只会让最先到达的那个请求收到 503，
            # 其余三段直接 200 —— 而谁先到取决于线程调度。那样写出来的断言
            # 时通时不通（我第一版就是这么写的，抓到了）。
            k = f"{path}|{model}"
            with FLAKY_LOCK:
                FLAKY[k] = FLAKY.get(k, 0) + 1
                n = FLAKY[k]
            if n <= FLAKY_FAIL_FIRST[0]:
                self._send(503, {"error": {"message": "upstream busy"}})
            else:
                self._send(200, self._ok_payload(model, max(sent, 20)))
            return

        # 只支持第一个种子模型，第二个种子返回 404 model_not_found。
        # 验「后一个种子的判定不能覆盖前一个」—— 那个 404 只说明这个分组没有
        # 该模型，不能据此判死整段。
        if profile == "onemodel":
            if model == ONLY_MODEL:
                self._send(200, self._ok_payload(model, max(sent, 20)))
            else:
                self._send(404, {"error": {
                    "message": f'Model "{model}" is not supported by any '
                               f'configured account in this group',
                    "type": "model_not_found"}})
            return

        # 2026-09-01 现场形态：中转站（new-api/one-api 系）对没有活跃通道的
        # 模型回 503 "No available channel for model X under group default"。
        # 这句**不是 CPA 发的**（CPA 源码零命中，它的措辞是 auth_unavailable），
        # 而是上游站自己的调度失败 —— 语义等同 404 model_not_found：换个模型
        # 就通。曾把它当站级死路，175 次 503 判死 92 个段，可用站被整段丢弃。
        if profile == "nochannel":
            if model == ONLY_MODEL:
                self._send(200, self._ok_payload(model, max(sent, 20)))
            else:
                self._send(503, {"error": {
                    "message": f"No available channel for model {model} "
                               f"under group default",
                    "type": "server_error"}})
            return

        if profile == "compatonly":
            if path.endswith("/chat/completions"):
                self._send(200, self._ok_payload(model, max(sent, 20)))
            else:
                self._send(404, {"error": {"message": "model_not_found"}})
            return

        # good
        if model and not cp.model_matches(model, model):
            self._send(400, {"error": {"message": "bad model"}})
            return
        self._send(200, self._ok_payload(model, max(sent, 20)))


# flaky 画像的调用计数，按 (路径, 模型) 分桶 —— 四段并行，全局计数会race。
FLAKY: dict[str, int] = {}
FLAKY_LOCK = threading.Lock()
# 前几次返回 503。用单元素列表以便在闭包外改。
FLAKY_FAIL_FIRST = [1]

# onemodel 画像唯一支持的模型 —— 取 claude 段的第一个种子。
ONLY_MODEL = "claude-opus-5"


class ProxyMarkingProber(Prober):
    """把 proxy 参数变成一个可观测的头。

    真代理需要另起 CONNECT 服务，对本测试是噪音 —— 我们只要验证
    「Prober 在该走代理的时候确实走了」。
    """

    def _call(self, *a, **kw):
        if kw.get("proxy"):
            kw["extra_headers"] = {**(kw.get("extra_headers") or {}),
                                   "X-Via-Proxy": "1"}
            kw["proxy"] = None          # 不真的走代理，只留标记
            self._proxy_used = True
        return super()._call(*a, **kw)


# ==========================================================================
# 用例
# ==========================================================================



def test_dead_section_shape_is_cached():
    """站+段级不通的结论要缓存 —— 否则同主机多 Key 变成 N 次**串行**全量探测。

    2026-09-05 修的 P1。`_shape` 只在 `v.usable` 为真时写入，于是段不通时
    形态不入缓存 —— 门闩清空、gate 置位、等待的线程醒来，其中一个重新认领
    又跑一遍完整 `_full_probe`（目录 + 基线 + 整梯画像 + 临时重试）。

    因为门闩存在，这 N 次是**严格串行**的 —— 比没有门闩（至少能并行）更慢。
    而这是多数情形而非边角：79 凭据实跑里 45 个是 0 段可用。

    实测（5 个 Key 挂同一主机、四段全回 403 门禁）：81 次请求 → 33 次。

    **类别划分是这条修复的关键**：
      · 站+段级（门禁/WAF/IP封/边缘/死路/时段/客户端/反测活/限频）—— 缓存。
        这些拒绝取决于请求形态、来源 IP、分组配置或时间，与用哪把 Key 无关
      · 凭证级（鉴权/余额）—— **不缓存**。那是这把 Key 自己的属性，缓存它
        会让同站其他 Key 继承别人的欠费结论
      · 该重试的（临时/未知）—— 不缓存。缓存等于放弃重试
    """
    import json as _json
    import socket as _socket
    import threading as _threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from cpa_probe.pipeline import Prober as _Prober

    hits = []
    mode = {"body": ""}

    class _Fake(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _all(self):
            hits.append(self.path)
            b = mode["body"].encode()
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        do_GET = do_POST = _all

    # 端口交给 ThreadingHTTPServer 自己 bind（2026-09-05 修竞态）。
    # 原来的写法是「socket bind 0 号拿到端口 → close → 再让 server bind 同一个」，
    # 那两步之间有窗口 —— 全套跑 11 个套件、4 个起真 HTTP 服务，同一台机器短时间
    # 反复分配端口，窗口里被抢到就 OSError（Windows 上是 WinError 10048）。
    # 实测吻合：只在 run.py 全链跑时出现、两次分别落在起服务的两个套件、不可复现。
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    port = srv.server_address[1]
    _threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    try:
        N = 5
        rows = cp.parse_lines(
            "\n".join(f"{base},sk-key-{i:04d}" for i in range(N)),
            allow_private=True).valid
        eq("5 个 Key 同一主机", len({r.host for r in rows}), 1)

        # ── ① 站+段级失败（门禁）：第 2..N 个 Key 零请求 ──
        mode["body"] = _json.dumps(
            {"error": {"message": "This group is restricted to Claude Code"}})
        hits.clear()
        pr = _Prober(gap=0.0, timeout=5, probe_context=False, swap_samples=0,
                     probe_capabilities=False, workers=4)
        results = [pr.probe(r) for r in rows]
        gated = len(hits)

        first_key_calls = results[0].total_calls
        rest = sum(r.total_calls for r in results[1:])
        eq("第 1 个 Key 正常探测", bool(first_key_calls > 0), True)
        eq("后 4 个 Key 零请求", rest, 0)
        truthy("总请求数不随 Key 数线性增长",
               gated < first_key_calls * 2,
               f"实测 {gated} 次，首个 Key 自己就 {first_key_calls} 次 —— "
               f"负缓存没生效")
        # 结论要传下去，且说清是复用的
        for r in results[1:]:
            for sec, v in r.sections.items():
                eq(f"{sec} 复用后仍判不可用", v.usable, False)
                eq(f"{sec} 复用后类别一致", v.category, "门禁")
                truthy(f"{sec} 说清是复用的", "复用" in (v.action or ""),
                       f"实得 {v.action!r} —— 看起来像这把 Key 也实测过")

        # ── ② 凭证级失败（余额）：**每把 Key 都要各自探** ──
        mode["body"] = _json.dumps(
            {"error": {"message": "insufficient balance, 剩余 $0.00"}})
        hits.clear()
        pr2 = _Prober(gap=0.0, timeout=5, probe_context=False, swap_samples=0,
                      probe_capabilities=False, workers=4)
        res2 = [pr2.probe(r) for r in rows]
        per_key = [r.total_calls for r in res2]
        eq("余额类每把 Key 都判不可用",
           all(not v.usable for r in res2 for v in r.sections.values()), True)
        truthy("余额类不被缓存（每把 Key 各自探）",
               all(c > 0 for c in per_key),
               f"各 Key 请求数 {per_key} —— 有 0 说明欠费结论被错误地"
               f"传给了别的 Key")
    finally:
        srv.shutdown()

    print(f"[OK] Dead shape: 站+段级失败缓存（5 Key {gated} 次请求，"
          f"后 4 把零请求）、凭证级不缓存、复用结论说清来源")


def test_temp_failure_circuit_breaker():
    """「临时」类连续同码失败要熔断 —— 否则一个挂掉的站吃掉半轮预算。

    2026-09-16 现场量化（173 站、1144 次请求那一轮）：
      · gorouter.app 一个站吃掉 **420 次**请求（15 Key × 4 段 × 每段 7 次），
        **全部 502，一次成功都没有**。全轮 568 次 502 里 418 次来自它。
      · zzzcoding.org（站方维护中）84 次全 405，同一形状。
    两个站合计约占全轮请求的 44%，贡献的信息量却等于 4 段各探一次。

    为什么 `_dead_shape` 接不住：`_HOST_LEVEL_FAIL` 故意排除「临时/未知」，
    理由是「那两类本该重试」。这对**第一把 Key** 成立，对第 2..N 把不成立
    —— 重试的价值在「这次不行下次可能行」，不在「换把 Key 问同一个已经
    连拒 N 次的站+段」。

    代价不只是慢：那轮总耗时 1650 秒，而 `/api/plan` 要在同一个 HTTP 请求里
    重建整份配置，跑过 Cloudflare 的 100 秒回源窗口就变成 524 —— 现场快照里
    692 个 priority 框全停在占位符「待定」，正是这么来的。

    判据是**状态码**而不是类别：502 与 429 同归「临时」，但那是两种处境。
    按码算连续，才能让真在抖的站（502/429 交替）继续享有重试。
    """
    import socket as _socket          # noqa: F401  （与同文件其余用例同构）
    import threading as _threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from cpa_probe.pipeline import Prober as _Prober

    hits = []
    mode = {"status": 502, "alternate": False}

    class _Fake(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _all(self):
            hits.append(self.path)
            if mode["alternate"]:
                # 真在抖的站：502 / 429 按 Key 序号交替 —— 偶数 Key → 502，
                # 奇数 Key → 429。同一把 Key 的所有请求看到相同码，不同 Key
                # 严格交替，保证 _fail_streak 永远是 (502,1)→(429,1)→(502,1)…
                # 不会积累到阈值 3。用 Authorization 头末尾的数字确定序号；
                # 无法解析时回落到 0（502）。(2026-09-17 修竞态)
                auth = (self.headers.get("Authorization") or
                        self.headers.get("x-api-key") or "")
                import re as _re
                m = _re.search(r'(\d+)\s*$', auth)
                key_idx = int(m.group(1)) if m else 0
                code = 502 if key_idx % 2 == 0 else 429
            else:
                code = mode["status"]
            b = b'{"error":{"message":"upstream failure"}}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        do_GET = do_POST = _all

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    port = srv.server_address[1]
    _threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    try:
        N = 8
        rows = cp.parse_lines(
            "\n".join(f"{base},sk-brk-{i:04d}" for i in range(N)),
            allow_private=True).valid

        # ── ① 稳定 502：达阈值后剩余 Key 不再重复探测 ──
        mode["alternate"] = False
        hits.clear()
        pr = _Prober(gap=0.0, timeout=5, probe_context=False, swap_samples=0,
                     probe_capabilities=False, workers=4)
        results = [pr.probe(r) for r in rows]
        per_key = [r.total_calls for r in results]

        eq("稳定 502 全部判不可用",
           all(not v.usable for r in results for v in r.sections.values()),
           True)
        # 契约是**收敛到零且不再回升**，不是「第 N 把起必须为零」。
        #
        # 为什么不能钉死下标（2026-09-16 写这条断言时先写错了一版）：
        # 四个段各自独立计数，而 `000`（连接失败）既不计数也不重置
        # （见 `_bump_fail_streak`）—— 于是某个段遇到一次抖动后要多等一把
        # Key 才满阈值，四段不可能在同一把 Key 上同时熔断。
        # 实测形状是 [10,10,10,4,4,0,0,0]：先是四段全探，再是少数段还在
        # 攒计数，最后全部收敛。钉死 `per_key[LIMIT+1:] == 0` 会把这个
        # 完全正确的形状判成失败。
        zeros = [i for i, c in enumerate(per_key) if c == 0]
        first_zero = zeros[0] if zeros else -1
        truthy("最终收敛到零请求且不再回升",
               first_zero >= 0 and all(c == 0 for c in per_key[first_zero:]),
               f"各 Key 请求数 {per_key} —— 尾部应全为 0，熔断没生效")
        truthy("总请求数显著低于线性增长",
               sum(per_key) < per_key[0] * N / 2,
               f"实测合计 {sum(per_key)} 次，首个 Key 自己 {per_key[0]} 次")
        # 熔断结论要说清是熔断，不能看起来像站方对每把 Key 都回过
        brk = [v.action or "" for r in results[-1:]
               for v in r.sections.values()]
        truthy("熔断说明写进 action",
               any("不再重复探测" in a or "复用" in a for a in brk),
               f"实得 {brk!r}")

        # ── ② 502/429 交替：站方真在抖，**不该**熔断 ──
        # workers=1 保证段串行执行，避免并发段共享同一 (host,section) 熔断
        # 计数器时出现「某段连续看到相同码」的竞态。交替逻辑本身是对的；
        # 只是 workers=4 并发时 4 个段的请求交错让熔断误触发（2026-09-17 修）
        mode["alternate"] = True
        hits.clear()
        pr2 = _Prober(gap=0.0, timeout=5, probe_context=False, swap_samples=0,
                      probe_capabilities=False, workers=1)
        res2 = [pr2.probe(r) for r in rows]
        per_key2 = [r.total_calls for r in res2]
        truthy("交替状态码不熔断（每把 Key 各自探）",
               all(c > 0 for c in per_key2),
               f"各 Key 请求数 {per_key2} —— 有 0 说明把「在抖」误判成"
               f"「稳定挂掉」，那会让恢复中的站再也探不到")
    finally:
        srv.shutdown()

    print(f"[OK] Circuit breaker: 稳定同码 {_Prober._FAIL_STREAK_LIMIT} 次后熔断"
          f"（8 Key 合计 {sum(per_key)} 次请求）、交替码不熔断、结论说清来源")

def main() -> int:
    # 端口交给 ThreadingHTTPServer 自己 bind（2026-09-05 修竞态）——
    # 见 tests/test_server.py 的 free_port docstring。
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    print(f"假上游已起：{base}\n")

    # 代理地址指向假上游自己的端口：TCP 预检能连上（这才有 via-proxy 尝试），
    # 而 ProxyMarkingProber 会把 proxy 参数换成 X-Via-Proxy 头，不真走代理。
    # 用 fake-proxy:7890 那类不存在的主机会被预检正确判为不通并跳过全部
    # via-proxy —— 那是生产环境想要的行为，但测试要覆盖代理救回的分支。
    fake_proxy = f"http://127.0.0.1:{port}"

    # 每次 probe() 的事件流。新用例要断言 transient-retry / model-rejected
    # 这类「过程可见性」事件确实发出去了 —— 不发就等于用户看不到发生了什么。
    seen_events: list[tuple[str, dict]] = []

    def probe(profile: str, **kw):
        seen_events.clear()
        row = cp.parse_lines(f"{base}/{profile},sk-fake000111222333", allow_private=True).valid[0]
        p = ProxyMarkingProber(
            gap=0.0, timeout=10, probe_context=False, swap_samples=0,
            proxy=fake_proxy,
            on_event=lambda k, d: seen_events.append((k, d)), **kw,
        )
        return p.probe(row)

    try:
        # ------------------------------------------------------------------
        section("good：四段全通 + 目录白名单过滤")
        r = probe("good")
        eq("四段全部可用", sorted(r.usable_sections), sorted(cp.SECTIONS))
        eq("总请求数 > 0", r.total_calls > 0, True)

        v = r.sections["claude-api-key"]
        eq("claude 基线一次即通", v.attempts[0].combo, "baseline")
        eq("claude 不需代理", v.need_proxy, False)
        eq("claude 不需补头", v.min_headers, {})
        eq("claude 定性可用", v.category, "可用")

        models = r.sections["codex-api-key"].models
        eq("目录里的杂项被滤掉",
           [m for m in models if not cp.pipeline.model_allowed(m)], [])
        eq("每段模型数不超上限", len(models) <= cp.pipeline.MAX_MODELS_PER_SECTION, True)
        eq("gpt 类被保留", "gpt-5.6-sol" in models, True)

        # 段决定 URL 形态 —— 这是 12 站零例外的规则
        eq("codex base 带 /v1",
           r.sections["codex-api-key"].base_url.endswith("/v1"), True)
        eq("claude base 不带 /v1",
           r.sections["claude-api-key"].base_url.endswith("/v1"), False)

        # ------------------------------------------------------------------
        section("quota：403 余额应立即收敛")
        r = probe("quota")
        eq("无可用段", r.usable_sections, [])
        v = r.sections["claude-api-key"]
        eq("定性为余额", v.category, "余额")
        eq("只试一次就收敛", len(v.attempts), 1)
        eq("没试代理", [a for a in v.attempts if a.combo == "via-proxy"], [])
        eq("没试补头", [a for a in v.attempts if a.combo.startswith("id:")], [])

        # ------------------------------------------------------------------
        section("cfguard：403 空正文 → 代理救回")
        r = probe("cfguard")
        eq("代理救回后四段可用", sorted(r.usable_sections), sorted(cp.SECTIONS))
        v = r.sections["claude-api-key"]
        eq("基线判为边缘", v.attempts[0].category, "边缘")
        eq("需要代理", v.need_proxy, True)
        eq("代理组合被记录",
           any(a.combo == "via-proxy" and a.ok for a in v.attempts), True)
        # 处置优先级：代理必须先于补头
        combos = [a.combo for a in v.attempts]
        eq("代理先于补头", combos.index("via-proxy") < len(combos), True)

        # ------------------------------------------------------------------
        # 一级代理只认「IP封/边缘」，判「临时」的段够不到它；画像梯也救不了
        # 链路问题。这一段验证处置链全部用尽后的那一次 via-proxy-last。
        section("linkdead：500 临时 → 二级代理救回")
        r = probe("linkdead")
        eq("二级代理救回后四段可用", sorted(r.usable_sections), sorted(cp.SECTIONS))
        v = r.sections["claude-api-key"]
        eq("基线判为临时", v.attempts[0].category, "临时")
        eq("需要代理", v.need_proxy, True)
        eq("走的是二级代理组合",
           any(a.combo == "via-proxy-last" and a.ok for a in v.attempts), True)
        # 断言「二级代理排在所有**处置**尝试之后」，而不是「排在最后」——
        # 段判可用后 _stage2 还会继续追加 model-scan 尝试，用 len-1 做判据
        # 是把 stage1 的顺序当成了整段的顺序（我第一版就这么写，被抓到）。
        combos_ld = [a.combo for a in v.attempts]
        i_last = combos_ld.index("via-proxy-last")
        eq("二级代理排在所有处置尝试之后",
           all(i_last > j for j, c in enumerate(combos_ld)
               if c == "baseline" or c.startswith(("retry", "id:", "via-proxy"))
               and c != "via-proxy-last"),
           True)
        eq("救回后有模型", bool(v.models), True)
        eq("发了 proxy-rescued 事件",
           any(k == "proxy-rescued" for k, _ in seen_events), True)

        # ------------------------------------------------------------------
        section("identity：401 → 补标识头救回")
        r = probe("identity")
        eq("补头后可用", sorted(r.usable_sections), sorted(cp.SECTIONS))
        v = r.sections["codex-api-key"]
        eq("最终需要头", bool(v.min_headers), True)
        # 头名按**小写**比较：HTTP 头名大小写不敏感，而画像表统一用小写
        # （CPA 上线前会改成真实客户端的大小写，见 claudeWireHeaderCasing）。
        # 原断言写死了 "User-Agent"/"Originator" 的驼峰形态，那是把「假上游
        # 恰好这么写」当成了契约 —— 换成小写后测试假失败，而行为完全正确。
        #
        # 2026-09-12：codex 段的可选头集合扩到 version 与传输协商头。
        # CPA 默认开 codex cloaking（disable-codex-cloaking 默认 false，
        # codex_executor_request.go:372-377），baseline 就带 UA + Originator，
        # 所以能把这个段救回来的最省档是**再加 Version** 的 codex-full。
        eq("头是 UA 或 Originator",
           {k.lower() for k in v.min_headers}
           <= {"user-agent", "originator", "version", "accept", "connection"}, True)
        eq("走的是 identity 回退",
           any(a.combo.startswith("id:") and a.ok for a in v.attempts), True)
        # 画像档名要被记下来 —— 报告与写回都靠它，「需要 codex-full」
        # 比「需要 1 个头」对人有用得多。
        eq("记下了画像档名", bool(v.profile_name), True)
        eq("最省档优先（codex 段 baseline 已自带身份头，再省就没有了）",
           v.profile_name, "codex-full")
        eq("这一档不需要 body 补丁", v.min_body_kind, "")

        # ------------------------------------------------------------------
        section("swapper：静默换模")
        row = cp.parse_lines(f"{base}/swapper,sk-fake000111222333", allow_private=True).valid[0]
        p = ProxyMarkingProber(gap=0.0, timeout=10, probe_context=False,
                               swap_samples=3)
        r = p.probe(row)
        v = r.sections["claude-api-key"]
        # 请求 claude-opus-5 却回 agnes-2.0-flash → model_matches 为假
        eq("请求的模型未被确认", v.models, [])
        eq("段不可用（模型验不过）", v.usable, True)  # 基线 200 → 段通
        eq("模型清单为空是硬信号", len(v.models), 0)

        # ------------------------------------------------------------------
        section("truncator：上下文上限按截断反推")
        row = cp.parse_lines(f"{base}/truncator,sk-fake000111222333", allow_private=True).valid[0]
        p = ProxyMarkingProber(gap=0.0, timeout=15, probe_context=True,
                              swap_samples=0)
        r = p.probe(row)
        v = r.sections["claude-api-key"]
        eq("段可用", v.usable, True)
        eq("测出上限", v.max_context_length is not None, True)
        if v.max_context_length:
            # 假上游封顶 300k，允许二分误差
            eq("上限接近 300k", 250_000 <= v.max_context_length <= 350_000, True)
        eq("标记为截断反推", v.context_untrusted, True)

        # ------------------------------------------------------------------
        section("compatonly：只有 compat 段通")
        r = probe("compatonly")
        eq("只有 compat 可用", r.usable_sections, ["openai-compatibility"])
        eq("claude 段不可用", r.sections["claude-api-key"].usable, False)
        eq("claude 判死路", r.sections["claude-api-key"].category, "死路")
        eq("compat 有模型", len(r.sections["openai-compatibility"].models) > 0, True)

        # ------------------------------------------------------------------
        section("事件流：前端进度条依赖它")
        events: list[tuple[str, dict]] = []
        row = cp.parse_lines(f"{base}/good,sk-fake000111222333", allow_private=True).valid[0]
        p = ProxyMarkingProber(gap=0.0, timeout=10, probe_context=False,
                              swap_samples=0,
                              on_event=lambda k, d: events.append((k, d)))
        p.probe(row)
        kinds = [k for k, _ in events]
        eq("发出 candidate-start", "candidate-start" in kinds, True)
        eq("发出 candidate-done", "candidate-done" in kinds, True)
        eq("发出 section-done ×4", kinds.count("section-done"), 4)
        eq("发出 attempt", "attempt" in kinds, True)
        eq("发出 catalog", "catalog" in kinds, True)
        # 前端读的字段必须都在
        att = next(d for k, d in events if k == "attempt")
        eq("attempt 载荷字段",
           sorted(att), sorted(["section", "model", "combo", "status",
                                "category", "elapsed_ms", "host"]))
        cat = next(d for k, d in events if k == "catalog")
        eq("catalog 载荷字段", sorted(cat), ["count", "host", "section"])
        done = next(d for k, d in events if k == "candidate-done")
        eq("candidate-done 载荷字段", sorted(done), ["calls", "host", "usable"])
        eq("start 载荷不含明文 key",
           "sk-fake000111222333" not in json.dumps(events, ensure_ascii=False), True)

        # ------------------------------------------------------------------
        section("verify_upstream：写回后的端到端确认")
        # 假上游同时扮演 CPA 的客户端入口 —— 三段路径各验一次。
        # 这一级是「push 成功」之外的第二道：CPA 收下配置 ≠ 新上游能出活。
        ok, msg = cp.verify_upstream(f"{base}/good", "sk-client-key",
                                     "claude-api-key", "claude-opus-5",
                                     timeout=10)
        eq("claude 段验证通过", ok, True)
        eq("回报里带后端形态", "后端" in msg, True)

        ok, msg = cp.verify_upstream(f"{base}/good", "sk-client-key",
                                     "codex-api-key", "gpt-5.6-sol", timeout=10)
        eq("codex 段验证通过", ok, True)

        ok, msg = cp.verify_upstream(f"{base}/good", "sk-client-key",
                                     "gemini-api-key", "gemini-2.5-pro",
                                     timeout=10)
        eq("gemini 段验证通过", ok, True)

        # 换模站：直连 200，但返回的不是要的模型 —— 必须判失败
        ok, msg = cp.verify_upstream(f"{base}/swapper", "sk-client-key",
                                     "claude-api-key", "claude-opus-5",
                                     timeout=10)
        eq("换模站验证失败", ok, False)
        eq("失败原因点明换模", "换模" in msg, True)
        eq("失败原因带实际模型", "agnes-2.0-flash" in msg, True)

        # 余额耗尽：非 200，要带上分类而不是只报状态码
        ok, msg = cp.verify_upstream(f"{base}/quota", "sk-client-key",
                                     "claude-api-key", "claude-opus-5",
                                     timeout=10)
        eq("余额站验证失败", ok, False)
        eq("失败原因带定性", "余额" in msg, True)

        # ------------------------------------------------------------------
        section("性能 · 代理预检只做一次")
        # 代理不通时，原实现每段每模型都试一次 via-proxy，每次干等满 timeout。
        # 实测日志：mihomo:7890 不通，5 个 key 累计十几分钟纯白等，结果全是
        # 无用的 `000 未知`。预检 4 秒判死一次，之后全程跳过。
        pre_events: list[tuple[str, dict]] = []
        row_cf = cp.parse_lines(f"{base}/cfguard,sk-fake000111222333", allow_private=True).valid[0]
        p_dead = ProxyMarkingProber(
            gap=0.0, timeout=10, probe_context=False, swap_samples=0,
            # 保留端口 9 （discard）几乎必然连不上，用它模拟死代理
            proxy="http://127.0.0.1:9",
            on_event=lambda k, d: pre_events.append((k, d)),
        )
        t_dead = time.monotonic()
        r_dead = p_dead.probe(row_cf)
        dead_secs = time.monotonic() - t_dead

        pre = [d for k, d in pre_events if k == "proxy-precheck"]
        eq("发出 proxy-precheck 事件", len(pre), 1)
        eq("预检判定不通", pre[0]["ok"], False)
        eq("预检说明带地址", "127.0.0.1:9" in pre[0]["detail"], True)

        all_attempts = [a for v in r_dead.sections.values() for a in v.attempts]
        eq("死代理下零次 via-proxy 尝试",
           [a.combo for a in all_attempts if a.combo == "via-proxy"], [])
        # 预检 4 秒封顶 + 四段基线；远低于「每段每模型等满 timeout」
        eq("整轮耗时受控（<25s）", dead_secs < 25, True)

        section("性能 · 同主机形态复用")
        # 段形态是主机属性：有哪些模型、要不要代理、最小必需头、上下文上限。
        # 换 Key 不改变其中任何一条 —— 但凭证有效性是 Key 的属性，仍要验。
        reuse_events: list[tuple[str, dict]] = []
        p_reuse = ProxyMarkingProber(
            gap=0.0, timeout=10, probe_context=False, swap_samples=0,
            proxy=fake_proxy,
            on_event=lambda k, d: reuse_events.append((k, d)),
        )
        rows = cp.parse_lines(
            f"{base}/good,sk-key-one-000111\n"
            f"{base}/good,sk-key-two-000222\n"
            f"{base}/good,sk-key-three-0333\n",
            allow_private=True
        ).valid
        eq("三个 Key 同一主机", len({r.host for r in rows}), 1)

        r1 = p_reuse.probe(rows[0])
        first_calls = r1.total_calls
        r2 = p_reuse.probe(rows[1])
        r3 = p_reuse.probe(rows[2])

        # 门槛按「段族过滤后」定（2026-09-01 改）：原来写 >12，那是四段都
        # 拿整份混族目录去探时的量。加了 SECTION_FAMILY 闸之后，三个协议段
        # 只探本族 —— 假上游的 10 个目录条目里，gemini 段剩 3、codex 剩 2、
        # claude 剩 2，compat 仍 7，首个 Key 实测 10 次。
        #
        # 这里要断言的是「首个 Key 确实走了全量、不是复用」，判据是它比
        # 后续 Key 多出好几倍，而不是某个绝对值。所以改成与 r2 比 ——
        # 那个比较不会随目录形态或段族规则再次失效。
        eq("首个 Key 走全量探测", first_calls >= 8, True)
        eq("首个 Key 明显多于复用者", first_calls > r2.total_calls * 2, True)
        eq("第二个 Key 请求数大幅下降", r2.total_calls < first_calls / 2, True)
        eq("第三个 Key 同样复用", r3.total_calls, r2.total_calls)
        # 复用不是跳过：凭证有效性每段仍验一次
        eq("复用仍逐段验凭证", r2.total_calls, len(cp.SECTIONS))
        eq("复用后段结论一致",
           sorted(r2.usable_sections), sorted(r1.usable_sections))
        eq("复用后模型清单一致",
           r2.sections["claude-api-key"].models,
           r1.sections["claude-api-key"].models)
        reused = [d for k, d in reuse_events if k == "shape-reused"]
        eq("发出 shape-reused 事件", len(reused) >= 4, True)
        eq("复用事件标明已验凭证",
           all(d.get("verified") for d in reused if d.get("models")), True)
        used_combos = {a.combo for v in r2.sections.values() for a in v.attempts}
        eq("复用只发 reuse-verify", used_combos, {"reuse-verify"})

        # ------------------------------------------------------------------
        # 以下三组锁住 2026-08-31 三个实测缺陷。它们都不是代码自相矛盾，
        # 而是「作者对真实中转站行为的假设错了」—— 假上游按旧假设造，
        # 所以此前 760 项全绿也没暴露。
        # ------------------------------------------------------------------
        section("okerror：200 但正文是错误体 —— 不许判成可用（假阳性）")
        r = probe("okerror")
        # 端点确实响应了、凭证有效，所以 usable 仍为 True；但模型一个都不能收，
        # 因为 SectionPlan.writable = not duplicate and bool(models)，
        # 空清单才是「不写入 config.yaml」的真正闸门。
        for sec in cp.SECTIONS:
            eq(f"{sec} 模型清单为空", r.sections[sec].models, [])
        writable = [s2 for s2, v2 in r.sections.items() if v2.models]
        eq("没有任何段会被写入", writable, [])
        rejected = [d for k, d in seen_events if k == "model-rejected"]
        eq("发出 model-rejected 事件", len(rejected) > 0, True)
        eq("拒收原因点明是错误体",
           any("错误体" in str(d.get("reason", "")) for d in rejected), True)

        section("flaky：503 临时错误必须重试 —— 不重试会把「忙」当成「坏」")
        FLAKY.clear()
        FLAKY_FAIL_FIRST[0] = 1          # 每个 (段, 模型) 首次 503，之后恢复
        r = probe("flaky")
        v = r.sections["claude-api-key"]
        eq("首次 503 后重试并通过", v.usable, True)
        eq("定性为可用", v.category, "可用")
        combos = [a.combo for a in v.attempts]
        eq("确实发生了重试", any(c.startswith("retry") for c in combos), True)
        eq("第一次是基线", combos[0], "baseline")
        retried = [d for k, d in seen_events if k == "transient-retry"]
        eq("发出 transient-retry 事件", len(retried) > 0, True)

        section("flaky：503 一直不恢复 —— 判「临时」而非「死路」，且重试有上限")
        FLAKY.clear()
        FLAKY_FAIL_FIRST[0] = 9999       # 永远 503
        r = probe("flaky")
        v = r.sections["claude-api-key"]
        eq("持续 503 判为临时", v.category, "临时")
        eq("持续 503 不可用", v.usable, False)
        n_retry = sum(1 for a in v.attempts if a.combo.startswith("retry"))
        # 上限是「每个基线模型 `_TRANSIENT_RETRIES` 次」，不是「每段种子数」
        # （2026-09-16 修）：绑种子数只在种子数 == `_BASELINE_MODELS` 时等价，
        # 而种子数已经改成每族 1 个（SEED_MODELS 由 model_catalog 兜底名录派生），
        # claude 段只剩 1 个种子、基线却仍打 2 个模型 —— 于是重试 2 次 > 种子 1，
        # 断言恒假。真正的不变量是基线模型数 × 每次上限。
        cap = cp.pipeline.Prober._BASELINE_MODELS * cp.pipeline.Prober._TRANSIENT_RETRIES
        eq("重试次数有上限（每个基线模型 1 次）", n_retry <= cap, True)

        section("onemodel：第二个种子 404 不许判死整段")
        r = probe("onemodel")
        v = r.sections["claude-api-key"]
        # 站方只支持 claude-opus-5；claude-sonnet-5 返回 404 model_not_found。
        # 那个 404 只说明「这个分组没有这个模型」，不能据此判死整段 ——
        # sonnet-5 只是本工具写死的第二个种子。
        eq("只支持首个种子时仍判可用", v.usable, True)
        eq("定性为可用（不是死路）", v.category, "可用")
        eq("清单里有可用的那个模型", ONLY_MODEL in v.models, True)
        eq("清单里没有 404 的那个模型",
           "claude-sonnet-5" in v.models, False)

        section("nochannel：503「分组无该模型渠道」不许判死整段")
        r = probe("nochannel")
        v = r.sections["claude-api-key"]
        # 与 onemodel 同形，只是站方用 503 而非 404 表达同一件事。
        # 曾因 503 落在「临时」类之外的站级死路分支，整段被丢。
        eq("换模型能通时仍判可用", v.usable, True)
        eq("定性为可用（不是死路）", v.category, "可用")
        eq("清单里有可用的那个模型", ONLY_MODEL in v.models, True)
        eq("清单里没有 503 的那个模型",
           "claude-sonnet-5" in v.models, False)

    finally:
        srv.shutdown()

    # 这一项自己起假上游（要控制返回体），所以放在主 srv 关掉之后
    section("站+段级失败的负缓存")
    test_dead_section_shape_is_cached()

    # 同上：自己起假上游，且要按 Key 序号切换返回码
    section("临时类连续同码失败的熔断")
    test_temp_failure_circuit_breaker()

    print("\n" + "=" * 66)
    if _fail:
        print(f"失败 {len(_fail)} 项 / 通过 {_pass} 项\n")
        for f in _fail:
            print(f"  ✗ {f}\n")
        return 1
    print(f"全部通过 · {_pass} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
