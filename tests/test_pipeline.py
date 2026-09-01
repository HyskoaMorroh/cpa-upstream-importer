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
        self.send_header("Content-Type", "application/json; charset=utf-8")
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

    @staticmethod
    def _ok_payload(model: str, tokens: int, *, rid: str = "msg_01AbCdEfGhJiKm") -> dict:
        return {
            "id": rid,
            "model": model,
            "usage": {"input_tokens": tokens, "output_tokens": 12},
            "content": [{"type": "text", "text": "A hash map is unordered."}],
        }

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
            if self.headers.get("User-Agent") or self.headers.get("Originator"):
                self._send(200, self._ok_payload(model, max(sent, 20)))
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


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ==========================================================================
# 用例
# ==========================================================================


def main() -> int:
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), FakeUpstream)
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
        row = cp.parse_lines(f"{base}/{profile},sk-fake000111222333").valid[0]
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
        eq("头是 UA 或 Originator",
           {k.lower() for k in v.min_headers} <= {"user-agent", "originator"}, True)
        eq("走的是 identity 回退",
           any(a.combo.startswith("id:") and a.ok for a in v.attempts), True)
        # 画像档名要被记下来 —— 报告与写回都靠它，「需要 originator-only」
        # 比「需要 1 个头」对人有用得多。
        eq("记下了画像档名", bool(v.profile_name), True)
        eq("最省档优先（originator 不含版本号，最抗客户端升级）",
           v.profile_name, "originator-only")
        eq("这一档不需要 body 补丁", v.min_body_kind, "")

        # ------------------------------------------------------------------
        section("swapper：静默换模")
        row = cp.parse_lines(f"{base}/swapper,sk-fake000111222333").valid[0]
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
        row = cp.parse_lines(f"{base}/truncator,sk-fake000111222333").valid[0]
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
        row = cp.parse_lines(f"{base}/good,sk-fake000111222333").valid[0]
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
        row_cf = cp.parse_lines(f"{base}/cfguard,sk-fake000111222333").valid[0]
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
            f"{base}/good,sk-key-three-0333\n"
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
        eq("重试次数有上限（每种子 1 次）",
           n_retry <= len(cp.pipeline.SEED_MODELS["claude-api-key"]), True)

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
