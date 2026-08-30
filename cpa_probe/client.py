"""统一 HTTP 传输层。

为什么要统一
-----------
原三个脚本用了三种底层：probe-fix / context-probe 用 urllib.request，
audit-upstreams 用 subprocess.run(["curl", ...])，swap-watch 又用 urllib。
判定口径依赖响应正文，底层不一致会让同一个站在不同脚本里判成不同结果。

READ_LIMIT 为什么是 4MB
----------------------
原脚本 read(20000) 造成过 100% 假换模率：/v1/responses 把整个 Codex
系统提示放在 instructions 字段（实测 40KB+），model 字段排在它之后，
被切掉 -> resp_model 返回 None -> 早期版本判成「换模」。
"""

from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request

READ_LIMIT = 4 * 1024 * 1024


class Response:
    __slots__ = ("status", "body", "elapsed_ms", "error")

    def __init__(self, status: str, body: str, elapsed_ms: int, error: str = ""):
        self.status = status
        self.body = body
        self.elapsed_ms = elapsed_ms
        self.error = error

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Response {self.status} {len(self.body)}B {self.elapsed_ms}ms>"


def _opener(proxy: str | None):
    handlers: list = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # 显式空 dict：避免继承环境变量里的代理，否则「直连」组不是真直连
        handlers.append(urllib.request.ProxyHandler({}))
    ctx = ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    op = urllib.request.build_opener(*handlers)
    # 关键：清空 addheaders。urllib 的 AbstractHTTPHandler.do_request_ 会在
    # 请求没有 User-Agent 时自动补 `User-Agent: Python-urllib/3.x`。
    # 那会毁掉整个「最小必需头」探测 —— CPA 在 gemini 段本来不发 UA，
    # 探测器却发了一个，于是「站方要不要 UA」这个问题被问成了
    # 「站方接不接受 Python-urllib 这个 UA」。两类误判都会发生：
    #   · 站方按 UA 白名单拦 → 探测判不可用，实际 CPA 能通
    #   · 站方只要求「有 UA」 → 探测判不需要 headers，实际 CPA 会 401
    # 清空后，UA 完全由调用方决定：给了就发，没给就真的不发。
    op.addheaders = []
    return op


def send(
    url: str,
    *,
    headers: dict[str, str],
    body: bytes,
    method: str = "POST",
    proxy: str | None = None,
    timeout: int = 120,
) -> Response:
    """发一次请求。任何异常都转成 Response，不抛出。

    status 取值：HTTP 状态码字符串，或 "000"（连接层失败，见 error）。
    """
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)

    t0 = time.monotonic()
    try:
        with _opener(proxy).open(req, timeout=timeout) as resp:
            raw = resp.read(READ_LIMIT)
            status = str(resp.status)
    except urllib.error.HTTPError as e:
        raw = e.read(READ_LIMIT) if hasattr(e, "read") else b""
        status = str(e.code)
    except urllib.error.URLError as e:
        return Response("000", "", int((time.monotonic() - t0) * 1000), str(e.reason))
    except (socket.timeout, TimeoutError):
        return Response("000", "", int((time.monotonic() - t0) * 1000), "timeout")
    except Exception as e:  # 兜底：SSL 错误等
        return Response("000", "", int((time.monotonic() - t0) * 1000), repr(e))

    elapsed = int((time.monotonic() - t0) * 1000)
    text = raw.decode("utf-8", errors="replace")
    return Response(status, text, elapsed)


def probe_proxy(proxy: str, *, timeout: int = 4) -> tuple[bool, str]:
    """一次性预检代理是否真的可用。返回 (可用, 说明)。

    为什么必须有这一步：`via-proxy` 是 IP封/边缘 类别的首选处置，
    每个段每个模型都会试。代理不通时每次都要等满 timeout（默认 120 秒）
    才失败 —— 实测日志里 5 个 key × 多段 = 十几分钟纯粹白等，而
    preflight 早就报过 `mihomo:7890 不通`。

    预检只做一次 CONNECT 级握手，4 秒内没结果就判不可用。之后整轮探测
    直接跳过所有 via-proxy 尝试，把那十几分钟降到 4 秒。
    """
    host, port = _split_proxy(proxy)
    if not host:
        return False, f"代理地址无法解析：{proxy}"
    t0 = time.monotonic()
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        return True, f"{host}:{port} 可连接（{int((time.monotonic()-t0)*1000)}ms）"
    except OSError as e:
        return False, f"{host}:{port} 不通 —— {e.__class__.__name__}: {e}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _split_proxy(proxy: str) -> tuple[str, int]:
    """从 http://host:port 取出 (host, port)。取不到返回 ("", 0)。"""
    t = (proxy or "").strip()
    if "://" in t:
        t = t.split("://", 1)[1]
    t = t.split("/", 1)[0]
    if "@" in t:                      # 带鉴权的 user:pass@host:port
        t = t.rsplit("@", 1)[1]
    if ":" not in t:
        return (t, 8080) if t else ("", 0)
    host, _, port = t.rpartition(":")
    try:
        return host, int(port)
    except ValueError:
        return host, 8080


def body_excerpt(text: str, limit: int = 400) -> str:
    """取正文摘要用于人工判读。HTML 页面剥标签，JSON 保留原样。"""
    t = text.strip()
    if not t:
        return "(空正文)"
    low = t[:200].lower()
    if "<html" in low or "<!doctype" in low:
        import re

        t = re.sub(r"<script.*?</script>", " ", t, flags=re.S | re.I)
        t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")
