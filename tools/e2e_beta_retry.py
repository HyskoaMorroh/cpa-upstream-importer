"""复现 anyrouter.top 的形态：整梯全 400，正文只说「请启用 1m 上下文」。

假上游的规则与实测一致：
  · 不看客户端身份（任何 UA / x-app / X-Stainless 组合都一样待遇）
  · anthropic-beta 里没有 context-1m-2025-08-07 就 400 + 那句中文提示
  · 带上就 200

判据：整梯 8 档必须全败，然后 beta-retry 补上那一项打通。
若补 beta 后仍判死，说明这条路没接上。
"""
import io
import json
import os
import socket
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cpa_probe as cp
from cpa_probe.pipeline import Prober

NEED = "context-1m-2025-08-07"
MSG = "1m 上下文已经全量可用，请启用 1m 上下文后重试"


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if "/models" in urllib.parse.urlparse(self.path).path:
            self._send(200, {"data": [{"id": "claude-opus-5"}]})
            return
        self._send(404, {"error": "nope"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        # 头名大小写不敏感 —— BaseHTTPRequestHandler 的 headers 已按此处理
        beta = self.headers.get("anthropic-beta") or ""
        if NEED not in beta:
            self._send(400, {"error": MSG, "type": "error"})
            return
        self._send(200, {"id": "msg_01Ok", "model": "claude-opus-5",
                         "type": "message", "role": "assistant",
                         "content": [{"type": "text", "text": "ok"}],
                         "usage": {"input_tokens": 9, "output_tokens": 2}})


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    events = []
    prober = Prober(gap=0.0, probe_context=False, swap_samples=0, workers=2,
                    on_event=lambda k, d: events.append((k, d)))
    row = cp.parse_lines(f"{base},sk-ant-anyrouter-shape").valid[0]
    res = prober.probe(row)

    v = res.sections.get("claude-api-key")
    assert v is not None, "claude 段无结论"

    # 只看 claude 段 —— 另三段本来就该 exhausted（假上游只放行带 beta 的
    # claude 请求）。不按段过滤会把正常结果当成失败，这个判据我第一版写错过。
    def only(kind):
        return [d for k, d in events
                if k == kind and d.get("section") == "claude-api-key"]

    exhausted, retry, hit = only("profile-exhausted"), only("beta-retry"), only("beta-hit")

    print(f"① 整梯: {len(v.attempts)} 次尝试")
    assert not exhausted, "claude 段被判 exhausted —— beta 重试没接上，走到了记账分支"
    assert retry, "没有触发 beta-retry —— 正文提取或接入点有问题"
    print(f"② beta-retry: 补了 {retry[0]['added']} 到 {retry[0]['profile']}")
    assert retry[0]["added"] == [NEED], f"补错了项: {retry[0]['added']}"

    assert hit, "补了 beta 仍未打通"
    print(f"③ beta-hit: 画像 {hit[0]['profile']}")

    assert v.usable, "段仍判不可用"
    assert v.models, "段可用但没模型"
    slot = next(k for k in v.min_headers if k.lower() == "anthropic-beta")
    assert NEED in v.min_headers[slot], "落地 headers 里没有补上的 beta"
    assert v.profile_name.endswith("+beta"), f"画像名没标注: {v.profile_name}"
    # 基底不能是 browser-ua —— 那是替换型画像，会把 CC 门票整个丢掉。
    # CPAMP 里能用的形态是「CC 门票 + 1m」，第一版取错档只落地 2 个 header。
    assert "browser" not in v.profile_name, \
        f"基底取了 alt 画像，CC 门票丢了: {v.profile_name}"
    betalist = v.min_headers[slot].split(",")
    assert len(betalist) >= 2, f"beta 只剩 {len(betalist)} 项，CC 门票丢了: {betalist}"
    assert any("claude-code" in b for b in betalist), \
        f"beta 里没有 claude-code 门票: {betalist}"
    assert len(v.min_headers) >= 3, \
        f"headers 只有 {len(v.min_headers)} 项，不像 CC 顶档: {list(v.min_headers)}"
    print(f"④ 结论: 可用 · 模型 {v.models} · 画像 {v.profile_name}")
    print(f"   headers {len(v.min_headers)} 项 · anthropic-beta {len(betalist)} 项，"
          f"含 {NEED} 与 claude-code 门票")

    # 门票必须能写进 config.yaml —— 补出来的 beta 不落地等于白补
    cfg = {"claude-api-key": []}
    plan = cp.build_plan(row, res, cfg, bands={},
                         seen=cp.existing_fingerprints(cfg), probation=True)
    sp = plan.sections.get("claude-api-key")
    assert sp is not None, "方案里没有 claude 段"
    hslot = next((k for k in sp.headers if k.lower() == "anthropic-beta"), None)
    assert hslot and NEED in sp.headers[hslot], "方案 headers 丢了补出的 beta"
    assert isinstance(sp.priority, int), "priority 不是确定整数"
    print(f"⑤ 方案: priority={sp.priority} · headers {len(sp.headers)} 项 · "
          f"beta 已落地")

    srv.shutdown()
    print(f"\nbeta 重试验证通过 · 假站 {base}")


if __name__ == "__main__":
    main()
