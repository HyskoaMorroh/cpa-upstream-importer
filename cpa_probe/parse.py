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
    if s.lower().endswith("/v1"):
        s = s[:-3].rstrip("/")
    return s


def base_for_section(bare: str, section: str) -> str:
    """按段补齐 base-url 形态。"""
    b = strip_v1(bare)
    if section in _NEEDS_V1:
        return b + "/v1"
    return b


def mask_key(key: str) -> str:
    """脱敏：保留前 6 后 4，中间省略。完整 key 永不落库/入日志。"""
    k = (key or "").strip()
    if len(k) <= 12:
        return (k[:3] + "***") if k else ""
    return f"{k[:6]}...{k[-4:]}"


def _normalize_url(u: str) -> tuple[str, str]:
    """返回 (bare, error)。"""
    s = (u or "").strip().strip('"').strip("'")
    if not s:
        return "", "url 为空"
    if not re.match(r"^https?://", s, re.I):
        # 容错：用户可能只写域名
        if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}", s, re.I):
            s = "https://" + s
        else:
            return "", f"url 形态无法识别：{s[:40]}"
    if not host_of(s):
        return "", f"取不到主机名：{s[:40]}"
    return strip_v1(s), ""


def parse_lines(text: str) -> ParseResult:
    """解析多行 `url,key`。空行与 # 开头行忽略。"""
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
        bare, err = _normalize_url(url_part)
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
