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

import http.client
import io
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import partial

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


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme.lower(), parsed.hostname,
            parsed.port if parsed.port is not None
            else {"http": 80, "https": 443}.get(parsed.scheme.lower()))


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(req.full_url) != _origin(newurl):
            fp.close()
            raise urllib.error.URLError("cross-origin redirect blocked")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        # urllib otherwise drains the unused redirect body without a size limit.
        fp.close()
        return redirected

    def http_error_307(self, req, fp, code, msg, headers):
        # Python 3.9 does not handle 307; delegate to redirect_request.
        newurl = headers.get("Location")
        if not newurl:
            return None
        return self.http_error_302(req, fp, code, msg, headers)

    def http_error_308(self, req, fp, code, msg, headers):
        # Python 3.9 does not handle 308; delegate to redirect_request.
        newurl = headers.get("Location")
        if not newurl:
            return None
        return self.http_error_301(req, fp, code, msg, headers)


def _time_left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("timeout")
    return left


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline: float):
        self._sock = sock
        self._deadline = deadline
        self._raw = sock.makefile("rb", buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        # Enforce the deadline below buffering, including headers/chunk framing.
        self._sock.settimeout(_time_left(self._deadline))
        return self._raw.readinto(buffer)

    def close(self):
        try:
            self._raw.close()
        finally:
            super().close()


class _DeadlineHTTPResponse(http.client.HTTPResponse):
    def __init__(self, sock, *args, deadline: float, **kwargs):
        super().__init__(sock, *args, **kwargs)
        self.fp.close()
        self.fp = io.BufferedReader(_DeadlineReader(sock, deadline))


def _deadline_connection(connection_type, deadline: float, host, **kwargs):
    class DeadlineConnection(connection_type):
        def _tunnel(self):
            super()._tunnel()
            # CONNECT consumed part of the budget; TLS must not reuse it.
            self.sock.settimeout(_time_left(deadline))

    kwargs["timeout"] = _time_left(deadline)
    connection = DeadlineConnection(host, **kwargs)
    connection.response_class = partial(_DeadlineHTTPResponse, deadline=deadline)
    return connection


def _opener(proxy: str | None, *, deadline: float | None = None):
    handlers: list = [_SameOriginRedirectHandler()]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # 显式空 dict：避免继承环境变量里的代理，否则「直连」组不是真直连
        handlers.append(urllib.request.ProxyHandler({}))
    ctx = ssl.create_default_context()
    if deadline is None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    else:
        class DeadlineHTTPHandler(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(
                    partial(_deadline_connection, http.client.HTTPConnection, deadline),
                    req)

        class DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(
                    partial(_deadline_connection, http.client.HTTPSConnection, deadline),
                    req, context=self._context)

        handlers.extend([DeadlineHTTPHandler(), DeadlineHTTPSHandler(context=ctx)])
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


def _read_body(resp) -> bytes:
    raw = resp.read(READ_LIMIT + 1)
    if len(raw) > READ_LIMIT:
        raise ValueError(f"response body exceeds {READ_LIMIT}-byte limit (truncated)")
    if getattr(resp, "length", None):
        raise ValueError("response body truncated before Content-Length")
    return raw


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
    deadline = t0 + timeout
    # 状态码与正文分两步取（2026-09-05 修）。
    #
    # 为什么不能在 except 里读正文
    # ------------------------
    # Python 的语义：except 块**内部**抛出的异常不受同一 try 的其余 handler
    # 保护。原来 `raw = e.read(READ_LIMIT)` 就写在 HTTPError 的 handler 里，
    # 所以下面 socket.timeout 与兜底 Exception 都接不到它 —— 异常一路穿出
    # send()，而本函数的 docstring 承诺「任何异常都转成 Response，不抛出」，
    # 上游两条调用路径都按这个不变式写。
    #
    # 实测触发：`403 + Content-Length: 5000` 但只写 2 字节后挂住
    # （Cloudflare 拦截页、nginx 慢响应都是这形态）→ TimeoutError。
    # 后果：并行路径把这个**只是回应慢的活站**写成「死路 · 探测异常」
    # 并建议降权；串行路径让整个 job 报错，一批凭据全丢。
    err = ""
    raw = b""
    status = ""
    content_encoding = ""
    http_error = False
    try:
        with _opener(proxy, deadline=deadline).open(
            req, timeout=_time_left(deadline)
        ) as resp:
            status = str(resp.status)
            content_encoding = resp.headers.get("Content-Encoding", "")
            raw = _read_body(resp)
    except urllib.error.HTTPError as e:
        # 状态码先记下 —— 它已经到手且有价值（403 就是 403，正文读不全
        # 不改变这个事实）。正文单独一段读，失败也不丢状态码。
        status = str(e.code)
        http_error = True
        try:
            with e:
                content_encoding = e.headers.get("Content-Encoding", "")
                raw = _read_body(e)
        except Exception as read_err:      # noqa: BLE001
            err = f"正文读取失败：{read_err!r}"
    except urllib.error.URLError as e:
        return Response("000", "", int((time.monotonic() - t0) * 1000),
                        str(e.reason))
    except (socket.timeout, TimeoutError):
        return Response("000", "", int((time.monotonic() - t0) * 1000),
                        "timeout")
    except Exception as e:  # 兜底：SSL 错误等
        return Response("000", "", int((time.monotonic() - t0) * 1000),
                        repr(e))

    text = ""
    if not err:
        try:
            _time_left(deadline)
            if method.upper() != "HEAD" and status not in ("204", "304"):
                text = _decode_body(raw, content_encoding=content_encoding,
                                    strict=True, deadline=deadline)
            _time_left(deadline)
        except Exception as decode_err:      # noqa: BLE001
            err = f"正文解码失败：{decode_err!r}"
            text = ""
            if not http_error:
                status = "000"
    elapsed = int((time.monotonic() - t0) * 1000)
    return Response(status, text, elapsed, err)


# 压缩正文的 magic byte。
#
# 为什么按 magic byte 而不是读 Content-Encoding 头（2026-09-05）
# --------------------------------------------------------
# 实测有中转站压缩了却不声明，也有声明了却没压。CPA 自己的
# `decodeResponseBody`（claude_executor_execute.go）注释明确说它**两种都处理**。
# 优先按 magic byte 判；Content-Encoding 补充识别没有 magic 的压缩格式。
_MAGIC_GZIP = b"\x1f\x8b"
_MAGIC_ZSTD = b"\x28\xb5\x2f\xfd"


def _decode_gzip(raw: bytes, deadline: float | None) -> bytes:
    import zlib

    chunks = []
    size = 0
    decoder = None
    pending = b""
    offset = 0
    while pending or offset < len(raw):
        if deadline is not None:
            _time_left(deadline)
        if not pending:
            pending = raw[offset:offset + 65536]
            offset += len(pending)
        if decoder is None:
            # Gzip permits zero padding after/between complete members.
            pending = pending.lstrip(b"\x00")
            if not pending:
                continue
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = decoder.decompress(pending, READ_LIMIT + 1 - size)
        size += len(out)
        if size > READ_LIMIT:
            raise ValueError("decompressed body exceeds response limit (truncated)")
        if out:
            chunks.append(out)
        if decoder.eof:
            pending = decoder.unused_data
            decoder = None
        else:
            pending = decoder.unconsumed_tail
    if decoder is not None:
        raise ValueError("truncated gzip compressed body")
    return b"".join(chunks)


def _decode_body(
    raw: bytes, *, content_encoding: str = "", strict: bool = False,
    deadline: float | None = None,
) -> str:
    """把响应正文解成文本。压缩过的先解压。

    为什么必须解压（2026-09-05 修）
    -------------------------
    画像梯的 cc-full / cc-body-* / compat cc-full 几档发
    `accept-encoding: gzip, deflate, br, zstd`（抄 CPA 的形态，
    见 profiles.py）。站方照办后 body 是二进制，`decode(errors="replace")`
    变成一串 U+FFFD —— 而**整条正文判定链都读文本**：

        classify        → 无异常关键词 → 判「可用」
        has_error_envelope → False
        resp_model      → None → model_matches 放行 → _accept 收下这个模型
        betas.wanted / _limit_from_body / input_tokens / 余额 / 限频 / 时段
                        → 关键词一个都匹配不上

    也就是「死站带模型进 config.yaml」那个假阳性，只是改由压缩触发。

    br 与 zstd 不解码。默认保留原有可读说明接口；send 使用 strict=True，
    将损坏、超限或不支持的压缩明确写入 Response.error，不能当作成功正文。
    """
    import re
    import zlib

    try:
        if deadline is not None:
            _time_left(deadline)
        encoding = content_encoding.strip().lower()
        if encoding not in ("", "identity", "gzip", "x-gzip", "deflate"):
            raise ValueError("unsupported Content-Encoding，本工具不解码")
        if len(raw) > READ_LIMIT:
            raise ValueError(f"response body exceeds {READ_LIMIT}-byte limit (truncated)")
        if not raw:
            return ""
        if raw[:2] == _MAGIC_GZIP:
            raw = _decode_gzip(raw, deadline)
        elif raw[:4] == _MAGIC_ZSTD:
            raise ValueError("zstd 压缩正文，本工具不解码")
        else:
            # Try both zlib and raw deflate, including unlabelled responses.
            zlib_header = (len(raw) >= 2 and raw[0] & 0x0f == 8
                           and raw[0] >> 4 <= 7
                           and int.from_bytes(raw[:2], "big") % 31 == 0)
            for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
                if deadline is not None:
                    _time_left(deadline)
                decoder = zlib.decompressobj(wbits)
                try:
                    out = decoder.decompress(raw, READ_LIMIT + 1)
                except zlib.error:
                    if zlib_header and wbits == zlib.MAX_WBITS:
                        # Two printable bytes can look like a zlib header.
                        break
                    continue
                if len(out) > READ_LIMIT:
                    raise ValueError("decompressed body exceeds response limit (truncated)")
                if not decoder.eof:
                    if zlib_header and wbits == zlib.MAX_WBITS:
                        break
                    continue
                if decoder.unused_data:
                    raise ValueError("invalid trailing data after deflate body")
                raw = out
                break
            if not decoder.eof:
                # Every plaintext fallback, including short/unlabelled bodies,
                # must be UTF-8 text, never replacement-decoded binary.
                #
                # 2026-09-12：`raw.decode("utf-8")` 抛的是 UnicodeDecodeError，
                # 它虽然是 ValueError 的子类、会被下面的 except 接住，但带出去的
                # 是 codec 原文（`'utf-8' codec can't decode byte 0x80 …`）——
                # 那句话既不说明「本工具不解码这种压缩」，也不告诉排查的人
                # 该怎么办，而本函数的契约就是「解不了要**明说**」。
                # 所以在这里换成与下方 br 分支同一句人话。
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise ValueError(
                        "正文无法解码（可能是 br 压缩），本工具不解码") from None
                # Unicode spaces/format characters are valid text, not binary.
                if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
                    raise ValueError("invalid or truncated compressed body")
        if len(raw) > READ_LIMIT:
            raise ValueError("decompressed body exceeds response limit (truncated)")
        text = raw.decode("utf-8", errors="replace")
        # br has no magic; retain the existing fallback for unlabelled binary data.
        if len(text) >= 16 and text.count("\ufffd") > len(text) * 0.3:
            raise ValueError("正文无法解码（可能是 br 压缩），本工具不解码")
        return text
    except (ValueError, OSError, EOFError, zlib.error) as error:
        if strict:
            raise
        return f"<{error}>"


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


# ---------- WebSocket 握手探测 ----------
#
# 为什么用标准库手搓而不引 websocket-client（2026-09-04）
# ----------------------------------------------------
# 本项目零第三方依赖（README 的部署前提），而这里只需要**握手那一次**：
# 判定「站方支不支持 WebSocket 通道」看的是 HTTP 状态行 —— 101 就是支持，
# 其余就是不支持。真正的帧收发不需要，所以不必引一个完整的 WS 客户端。
#
# CPA 侧对应的动作：`dialCodexWebsocket`（codex_websockets_connection.go:30）
# 用 gorilla/websocket 的 `DialContext`，握手超时 30 秒、开压缩。
# 那一步失败就是 `websockets: true` 写进去也不能用。
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_handshake(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
) -> Response:
    """对 `ws://` / `wss://` 发一次 WebSocket 握手，返回 Response。

    status 是 HTTP 状态码字符串（`101` = 升级成功），或 `000`（连接层失败）。
    body 是响应正文摘要 —— 拒绝时站方常把原因写在里面
    （`this endpoint requires beta header`、Cloudflare 拦截页）。

    **不做代理**：CPA 的 WS 拨号走 `newProxyAwareWebsocketDialer`，会用条目的
    proxy-url；但本探测只回答「直连能不能升级」这一个问题。需要代理的站在
    HTTP 探测阶段已经被标成 need_proxy，那时 websockets 判定按「未知」处理
    而不是判死 —— 见 pipeline 的 `_stage5_websockets`。

    为什么校验 Sec-WebSocket-Accept：光看 101 会把「反代把 Upgrade 吞了、
    自己回了个 101」这种形态当成支持。accept 值是 key + GUID 的 SHA-1 base64
    （RFC 6455 §4.2.2），算不对就不是真正的 WS 端点。
    """
    import base64
    import hashlib
    import os
    import urllib.parse

    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("ws", "wss"):
        return Response("000", "", 0, f"不是 ws/wss 地址：{url}")
    host = parsed.hostname or ""
    if not host:
        return Response("000", "", 0, f"地址里没有主机名：{url}")
    port = parsed.port or (443 if scheme == "wss" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    # Host 头带端口只在非默认端口时 —— 与浏览器和 gorilla 的行为一致
    host_hdr = host if parsed.port in (None, 443, 80) else f"{host}:{port}"
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host_hdr}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for k, v in (headers or {}).items():
        if v and k.lower() not in {
            "host", "upgrade", "connection",
            "sec-websocket-key", "sec-websocket-version",
        }:
            lines.append(f"{k}: {v}")
    req = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8", errors="replace")

    t0 = time.monotonic()
    sock = None
    buf = b""            # 在 try 之外初始化 —— 超时处置要按「已收多少字节」
                         # 区分「连不上」与「连上了但握手响应没读完」
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        if scheme == "wss":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.settimeout(timeout)
        sock.sendall(req)
        # 整次握手的**截止时间**，不是每次 recv 的超时（2026-09-05 修）。
        #
        # 原来只 `sock.settimeout(timeout)` 然后循环 recv —— 那是**每次读**的
        # 超时。对端每 0.3 秒送 1 字节且永不发空行时，每次 recv 都在 timeout
        # 内返回，于是循环最多要收满 64KB 才退出：上界是
        # 65536 × 每字节间隔，而不是调用方给的 timeout。
        #
        # 实测：`timeout=1` 的调用被挂住 180 秒以上仍未返回。而调用方传的是
        # `min(self.timeout, 30)`，本意是 30 秒上限 —— 段级线程被钉住，
        # 站级并发的槽位也一起占着。
        #
        # 判据：对面 CPA 用的是真正的截止时间 ——
        # `codex_websockets_connection.go:32` 的 `dialer.HandshakeTimeout`
        # （同文件 :27 = 30 * time.Second），gorilla 那个字段覆盖整次握手
        # 而不是单次读。
        deadline = time.monotonic() + timeout
        # 上限 64KB —— 拒绝时站方可能回一整个 HTML 拦截页，头部本身没那么大。
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            left = deadline - time.monotonic()
            if left <= 0:
                # 头没读完就到点了。把已读到的交出去 —— 状态行可能已经在里面，
                # 那比返回 000 更有信息量。
                #
                # **这一支是防御性的，不是承重的**（2026-09-05 撤销实验查明）：
                # 正常情形下 recv 自己的超时先触发（`settimeout(left)` 之后
                # recv 最多等 left 秒就抛 socket.timeout，走下面的 handler），
                # 所以 left 走不到 <= 0。它存在是为了两种边角：
                #   · 系统时钟跳变让 monotonic 之差变负（罕见但有过报告）
                #   · 将来有人在循环里加别的耗时步骤
                # 去掉它的后果不是挂死，而是 `settimeout(负数)` 抛 ValueError
                # 被最外层兜住，错误消息变成 `ValueError('Timeout value out
                # of range')` —— 运维会以为是本工具的 bug。
                return Response(
                    "000", buf.decode("utf-8", errors="replace"),
                    int((time.monotonic() - t0) * 1000),
                    f"握手超时（{timeout}s 内未读完响应头，"
                    f"已收 {len(buf)} 字节）")
            sock.settimeout(left)
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    except (socket.timeout, TimeoutError):
        # 分两种说：连不上 vs 连上了但握手响应没读完。后者是上面那个 deadline
        # 修复的正常出口（recv 撞上剩余时间），措辞要与「站方压根没回应」
        # 区分开 —— 排障时那是两个方向：一个查网络可达性，一个查站方是否
        # 在慢慢吐响应头（反代常见形态）。
        why = (f"握手超时（{timeout}s 内未读完响应头，已收 {len(buf)} 字节）"
               if buf else f"握手超时（{timeout}s 内未收到任何响应）")
        return Response("000", buf.decode("utf-8", errors="replace"),
                        int((time.monotonic() - t0) * 1000), why)
    except OSError as e:
        return Response("000", "", int((time.monotonic() - t0) * 1000),
                        f"{type(e).__name__}: {e}")
    except Exception as e:                                   # noqa: BLE001
        return Response("000", "", int((time.monotonic() - t0) * 1000), repr(e))
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    elapsed = int((time.monotonic() - t0) * 1000)
    if not buf:
        return Response("000", "", elapsed, "对端未回任何数据即断开")
    head, _, rest = buf.partition(b"\r\n\r\n")
    head_txt = head.decode("latin-1", errors="replace")
    first = head_txt.split("\r\n", 1)[0]
    parts = first.split()
    status = parts[1] if len(parts) >= 2 and parts[1].isdigit() else "000"
    body = rest.decode("utf-8", errors="replace")
    if status == "000":
        return Response("000", body, elapsed, f"状态行无法解析：{first[:80]}")

    if status == "101":
        want = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        got = ""
        for ln in head_txt.split("\r\n")[1:]:
            name, _, val = ln.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                got = val.strip()
                break
        if got != want:
            # 101 但 accept 算不对 —— 不是真的 WS 端点（反代吞了 Upgrade
            # 自己回 101 的形态见过）。当作不支持，并把实情写进 error。
            return Response("101", body, elapsed,
                            f"Sec-WebSocket-Accept 不匹配（收到 {got or '缺失'}）"
                            f"—— 对端回了 101 但不是真正的 WebSocket 端点")
    return Response(status, body, elapsed)


def http_to_ws(url: str) -> str:
    """`http(s)://…` → `ws(s)://…`。与 CPA 的 buildCodexResponsesWebsocketURL 同构
    （codex_websockets_connection.go:223-240）：只换 scheme，其余原样。

    非 http/https 或缺主机名时返回空串 —— 调用方据此跳过探测，与 CPA 那边
    直接报 `unsupported responses websocket URL scheme` 对应。
    """
    import urllib.parse

    p = urllib.parse.urlsplit((url or "").strip())
    scheme = {"http": "ws", "https": "wss"}.get((p.scheme or "").lower(), "")
    if not scheme or not p.hostname:
        return ""
    return urllib.parse.urlunsplit((scheme, p.netloc, p.path, p.query, ""))
