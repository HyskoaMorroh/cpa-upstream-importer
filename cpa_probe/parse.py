"""解析 `url,key` 输入并按段规范化 base-url。

格式（固定一种，用户 2026-08-29 已定）：
    https://example.com,sk-xxxx
    https://api.example.org/v1,sk-yyyy

规范化依据：12 个现存站点、206 个凭据条目零例外，base-url 形态完全由段决定。
    gemini-api-key        裸域名（64/64 无 /v1）
    claude-api-key        裸域名（65/65 无 /v1）
    codex-api-key         必须带 /v1（65/65）
    openai-compatibility  必须带 /v1（12/12）
所以用户粘贴时带不带 /v1 都接受，写入时按目标段补齐或剥离。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

SECTIONS = ("gemini-api-key", "codex-api-key", "claude-api-key", "openai-compatibility")

# 需要 /v1 后缀的段
_NEEDS_V1 = {"codex-api-key", "openai-compatibility"}


@dataclass
class ParsedRow:
    """一行输入的解析结果。"""

    line_no: int
    raw: str
    bare: str = ""          # 规范化后的裸 base（无尾部 / 与 /v1）
    api_key: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def host(self) -> str:
        return host_of(self.bare)

    def base_for(self, section: str) -> str:
        """按目标段给出应写入 config.yaml 的 base-url。"""
        return base_for_section(self.bare, section)

    def masked(self) -> str:
        return mask_key(self.api_key)


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)

    @property
    def valid(self) -> list[ParsedRow]:
        return [r for r in self.rows if r.ok]

    @property
    def invalid(self) -> list[ParsedRow]:
        return [r for r in self.rows if not r.ok]


def host_of(url: str) -> str:
    """取主机名，去掉协议、路径、端口以外的部分。统一小写。

    两处都是 2026-08-31 自查发现的真缺陷，且后果完全不同：

    ① 剥协议必须 re.I。`_normalize_url` 的形态校验带了 re.I，所以
       `HTTPS://x.com` 能一路通过；这里不带的话剥不掉，split("/")[0]
       取出来的是 **"HTTPS:"** —— 于是任意两个用大写 scheme 粘贴的站，
       pipeline 的形态缓存键 (row.host, section) 直接撞成同一个，
       第二个站会套用第一个站学到的模型清单/代理/必需头写进 config.yaml。
       不是浪费配额，是写错配置。

    ② 统一小写。主机名按 RFC 大小写不敏感，`API.Example.com` 与
       `api.example.com` 是同一台机器。不归一化则同站两种拼写各探一遍
       全量，白烧一倍配额，还可能触发站方的批量探测防护；
       credential_pair 也会因此判成新凭据、重复插入同一个 Key。
    """
    s = re.sub(r"^https?://", "", (url or "").strip(), flags=re.I)
    return s.split("/")[0].lower()


def strip_v1(url: str) -> str:
    """剥离尾部 / 与尾部 /v1，得到裸 base。"""
    s = (url or "").strip().rstrip("/")
    # 只剥离结尾恰好是 /v1 的情形，不动 /v1beta 之类
    if s.endswith("/v1"):
        s = s[:-3].rstrip("/")
    return s


def base_for_section(bare: str, section: str, *, declared_base: bool = False) -> str:
    """Preserve declared CPA bases and channel case; default only new origins.

    Known full endpoints are accepted for new input only, not silently appended
    twice. Declared bases are literal CPA configuration, including trailing /v1.
    """
    parts = urlsplit((bare or "").strip())
    if (parts.scheme.lower() not in ("http", "https") or not parts.hostname
            or parts.username or parts.password or parts.fragment):
        raise ValueError("invalid base URL")
    path = parts.path.rstrip("/")
    if parts.query and not (not declared_base and section == "claude-api-key"
                            and path.endswith("/v1/messages")
                            and parts.query == "beta=true"):
        raise ValueError("query is not supported on a base URL")
    endpoints = {
        "codex-api-key": ("/responses", "/models"),
        "openai-compatibility": ("/chat/completions", "/models"),
        "claude-api-key": ("/v1/messages", "/v1/models"),
        "gemini-api-key": ("/v1beta/models",),
    }
    if declared_base and (
            any(path.endswith(s) for group in endpoints.values() for s in group)
            or ":generateContent" in path or ":streamGenerateContent" in path):
        raise ValueError("declared base URL looks like a full endpoint")
    normalized_endpoint = False
    if not declared_base:
        for suffix in endpoints.get(section, ()):
            if path.endswith(suffix):
                path = path[:-len(suffix)]
                normalized_endpoint = True
                break
        if (":generateContent" in path or ":streamGenerateContent" in path
                or any(path.endswith(s) for group in endpoints.values() for s in group)):
            raise ValueError("endpoint does not match the requested protocol")
        if not normalized_endpoint:
            if section not in _NEEDS_V1 and path == "/v1":
                path = ""
            elif section in _NEEDS_V1 and not path.endswith("/v1"):
                # codex / compat 必须以 /v1 结尾，**带路径前缀的站也一样**
                # ------------------------------------------------------
                # 2026-09-12：判据原来是 `not path` —— 只有根路径才补。
                # 于是 `https://api.example.com/relay` 这类带前缀的中转站
                # 补不上，CPA 按 `TrimSuffix(baseURL,"/") + "/responses"`
                # 拼出 `/relay/responses`，而站方真正的端点是
                # `/relay/v1/responses` —— 404。
                #
                # 这正是用户报的第 1/2 条：同一个网址填进 cc-switch 能用
                # （cc-switch 直接用整串 `.../v1` 当 base），填进 CPA 不能用。
                # 段规则是「codex / compat 的 base 一律以 /v1 结尾」，
                # 与前面有没有路径前缀无关。
                path = path + "/v1"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def mask_key(key: str) -> str:
    """脱敏：保留前 6 后 4，中间省略。完整 key 永不落库/入日志。"""
    k = (key or "").strip()
    if len(k) <= 12:
        return (k[:3] + "***") if k else ""
    return f"{k[:6]}...{k[-4:]}"


# 默认拒绝的出网目标网段（2026-09-05 加）。
#
# 为什么需要（审计发现）
# -------------------
# 探测目标 URL 与代理地址都来自请求体，而 `_normalize_url` 原来只校验形态
# （`^https?://` 或域名样子），不管**去向**。于是已登录的人能把服务端当扫描器：
#
#   POST /api/diag {"url": "http://127.0.0.1:8317", "key": "x"}
#     → 服务端向内网发请求，非 200 时把 400 字节正文摘要放进
#       rungs[].excerpt 同步返回
#   POST /api/probe {"opts": {"proxy": "http://10.0.0.5:22"}}
#     → probe_proxy 的连通性、异常类名（ConnectionRefused / timeout）与毫秒数
#       经 proxy-precheck 事件回到 /api/job —— 比 HTTP 路径更干净的端口
#       扫描 oracle
#
# 云元数据端点基本打不到：出网路径总在 base 后面追加固定后缀
# （`/v1/models`、`/v1beta/models`、`/v1/messages`、`/responses`、
# `/chat/completions`，见 request.py），拼不出 `/latest/meta-data/...`；
# GCP 所需的 `Metadata-Flavor` 头也不会发。所以这是**内网侦察**而不是
# 直接偷云凭据 —— 但侦察本身就该挡。
#
# 为什么不做 DNS 解析后再判：那会引入 DNS rebinding 的时间窗（解析时是公网 IP、
# 真正连接时变私网），而正确处理它要接管 socket 的地址解析。这里只挡**字面量**
# 私网地址，够覆盖「拿它扫内网」这个用法；真要防 rebinding 得在 client.py
# 那一层做，且代价不小。这一点必须写明，不能让人以为这道闸挡住了全部 SSRF。
_PRIVATE_NETS = (
    # IPv4
    "10.", "127.", "169.254.", "192.168.",
    "0.",                    # 0.0.0.0/8，本机的另一种写法
    # 172.16.0.0/12 单独判（172.16-172.31）
)

_LOOPBACK_NAMES = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    # 云元数据的惯用主机名
    "metadata", "metadata.google.internal", "metadata.goog",
    "instance-data",
})


def is_private_target(host: str) -> str:
    """这个主机是不是私网 / 回环 / 链路本地。是则返回原因，否则空串。

    `host` 是 `host_of()` 的产物（可能带端口）。只看**字面量** —— 见
    `_PRIVATE_NETS` 上方关于 DNS rebinding 的说明。
    """
    h = (host or "").strip().lower()
    if not h:
        return ""
    # 去端口。IPv6 字面量形如 `[::1]:8080`
    if h.startswith("["):
        end = h.find("]")
        if end > 0:
            h = h[1:end]
    elif h.count(":") == 1:
        h = h.split(":")[0]

    if h in _LOOPBACK_NAMES:
        return f"{h} 指向本机或云元数据服务"
    if h.endswith(".localhost") or h.endswith(".local") \
            or h.endswith(".internal"):
        return f"{h} 是本机 / 内网域名后缀"

    # IPv6
    if ":" in h:
        if h in ("::1", "::"):
            return f"{h} 是 IPv6 回环"
        if h.startswith(("fc", "fd")):          # fc00::/7 唯一本地地址
            return f"{h} 在 IPv6 唯一本地地址段"
        if h.startswith("fe8") or h.startswith("fe9") \
                or h.startswith("fea") or h.startswith("feb"):
            return f"{h} 在 IPv6 链路本地段"
        if h.startswith("::ffff:"):             # IPv4 映射
            return is_private_target(h[len("::ffff:"):])
        return ""

    for pre in _PRIVATE_NETS:
        if h.startswith(pre):
            return f"{h} 在私网段 {pre}x"
    # 172.16.0.0/12
    if h.startswith("172."):
        try:
            second = int(h.split(".")[1])
        except (IndexError, ValueError):
            return ""
        if 16 <= second <= 31:
            return f"{h} 在私网段 172.16-31.x"
    return ""


def _normalize_url(u: str, *, allow_private: bool = False) -> tuple[str, str]:
    """返回 (bare, error)。

    `allow_private=True` 时跳过私网去向检查 —— 本项目自己的假上游套件与
    端到端脚本都打 `127.0.0.1`，那是合法用法。生产的 HTTP 入口不传这个参数，
    所以默认拒。见 `is_private_target` 上方的说明（含它挡不住什么）。
    """
    s = (u or "").strip().strip('"').strip("'")
    if not s:
        return "", "url 为空"
    if not re.match(r"^https?://", s, re.I):
        # 容错：用户可能只写域名
        if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}", s, re.I):
            s = "https://" + s
        else:
            return "", f"url 形态无法识别：{s[:40]}"
    h = host_of(s)
    if not h:
        return "", f"取不到主机名：{s[:40]}"
    if not allow_private:
        why = is_private_target(h)
        if why:
            return "", f"拒绝内网目标：{why}"
    try:
        parts = urlsplit(s)
        if parts.username or parts.password or parts.query or parts.fragment:
            return "", "url 不支持凭据、查询参数或 fragment"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                           parts.path.rstrip("/"), "", "")), ""
    except ValueError:
        return "", "url 形态无法识别"


def parse_lines(text: str, *, allow_private: bool = False) -> ParseResult:
    """解析多行 `url,key`。空行与 # 开头行忽略。

    `allow_private` 透传给 `_normalize_url` —— 只有本项目自己的测试与端到端
    脚本该传 True（它们打本机假上游）。
    """
    result = ParseResult()
    for i, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        row = ParsedRow(line_no=i, raw=line)
        if "," not in line:
            row.error = "缺少逗号分隔符，应为 url,key"
            result.rows.append(row)
            continue
        # 首个逗号左侧为 url，右侧全部为 key（key 内不允许逗号）
        url_part, key_part = line.split(",", 1)
        bare, err = _normalize_url(url_part, allow_private=allow_private)
        key = key_part.strip().strip('"').strip("'")
        if err:
            row.error = err
        elif not key:
            row.error = "key 为空"
        elif "," in key:
            row.error = "key 内含逗号，无法解析"
        else:
            row.bare, row.api_key = bare, key
        result.rows.append(row)
    return result
