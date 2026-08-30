"""按段构造上游请求。路径规则与 CPA 自身 executor 完全对齐。

源码依据（CLIProxyAPI-main）：
  gemini  internal/runtime/executor/gemini_executor.go:176
          {base}/v1beta/models/{model}:generateContent   key 走 query string
  codex   internal/runtime/executor/codex_executor_execute.go:76
          {base}/responses                               base 已含 /v1
  claude  internal/runtime/executor/claude_executor_execute.go:30
          {base}/v1/messages?beta=true
  compat  {base}/chat/completions                        base 已含 /v1

段名一律用 config.yaml 里的原始键名字符串（"gemini-api-key" 等），不另设枚举 ——
少一层映射，写回时直接就是 YAML 的键。

claude 段鉴权头：probe-fix 用 Authorization: Bearer，audit 用 x-api-key。
Anthropic 官方两种都支持，中转站实现不一。本模块**两个都发** —— 多一个头
不会让通的站变不通，但少一个可能让通的站判成 401。

探测时 claude 段不带 `?beta=true`。CPA 自己带，但那是它对上游能力的假定；
探测要问的是「这个站的 /v1/messages 通不通」，多一个 query 参数可能引入
额外失败面。写入 config.yaml 后由 CPA 按它自己的口径请求。
"""

from __future__ import annotations

from .parse import SECTIONS, base_for_section

# 探测文本：绝不能用 "hi" / "你好" —— 会触发站方反测活拦截。
# 用技术问句，长度足够但不浪费 token。（2026-08-29 实测修正）
PROBE_TEXT = (
    "Reply with one short sentence: what is the difference "
    "between a hash map and a tree map?"
)

# Codex 客户端真实标识（2026-08-29 从本机 codex 插件实测抄录）
UA_CODEX = "codex_cli_rs/0.5.11 (Windows 11; x86_64) WindowsTerminal"
ORIGINATOR = "codex_vscode"

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# CPA 各段默认发出的 User-Agent（下界基准，非「完全不发 UA」）
# 依据 cpa-atlas 第 06 章「各协议段默认发出的 User-Agent」
CPA_DEFAULT_UA: dict[str, str | None] = {
    "gemini-api-key": None,                     # gemini 段 CPA 不设 UA
    "codex-api-key": UA_CODEX,                  # 视 cloaking 开关，当前用客户端真实值
    "claude-api-key": "CLIProxyAPI/6.0",        # 透传，缺失时回落此值
    "openai-compatibility": "cli-proxy-openai-compat",
}


def _check(section: str) -> None:
    if section not in SECTIONS:
        raise ValueError(f"未知段：{section!r}，应为 {SECTIONS}")


def build_request(
    section: str,
    base_url: str,
    model: str,
    api_key: str,
    *,
    extra_headers: dict[str, str] | None = None,
    max_tokens: int = 64,
    text: str = PROBE_TEXT,
) -> tuple[str, dict[str, str], dict]:
    """返回 (url, headers, json_body)。

    base_url 会先按段规范化，所以调用方传带不带 /v1 都对。
    """
    _check(section)
    base = base_for_section(base_url, section)
    headers: dict[str, str] = {"Content-Type": "application/json"}

    if section == "gemini-api-key":
        url = f"{base}/v1beta/models/{model}:generateContent?key={api_key}"
        body: dict = {"contents": [{"role": "user", "parts": [{"text": text}]}]}

    elif section == "codex-api-key":
        url = f"{base}/responses"
        headers["Authorization"] = f"Bearer {api_key}"
        body = {"model": model, "stream": False, "input": text}

    elif section == "claude-api-key":
        url = f"{base}/v1/messages"
        # 两种鉴权头都发 —— 中转站实现不一，少一个可能误判 401
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": text}],
        }

    else:  # openai-compatibility
        url = f"{base}/chat/completions"
        headers["Authorization"] = f"Bearer {api_key}"
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": text}],
        }

    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v})

    return url, headers, body


def models_endpoint(section: str, base_url: str, api_key: str) -> tuple[str, dict[str, str]]:
    """列模型端点。用于「这个站到底有哪些模型」的发现阶段。

    这是探测的第一步，代价最低（多数站不计费），先拿到真实模型清单再逐个验。
    """
    _check(section)
    base = base_for_section(base_url, section)
    headers = {"Content-Type": "application/json"}

    if section == "gemini-api-key":
        return f"{base}/v1beta/models?key={api_key}", headers

    headers["Authorization"] = f"Bearer {api_key}"
    if section == "claude-api-key":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        return f"{base}/v1/models", headers

    # codex / compat：base 已含 /v1
    return f"{base}/models", headers


def identity_combos(section: str) -> list[tuple[str, dict[str, str]]]:
    """标识头回退序列，由省到全。第一个仍 200 的即「最小必需头」。

    Originator-only 排在 UA-only 之前 —— 它不含版本号，不受 Codex 客户端
    升级影响。这正是用户 2026-08-29 提的「表头随版本升级而变化」的解法：
    找出最小必需头，能只写 Originator 就不写 UA。

    2026-08-29 实测：relay-c 七值矩阵全部 200（含垃圾串与空值），
    说明该站「UA 或 Originator 任一满足即可，值不敏感」。
    """
    _check(section)
    cpa_ua = CPA_DEFAULT_UA.get(section)
    return [
        ("cpa-现状", {"User-Agent": cpa_ua} if cpa_ua else {}),
        ("originator-only", {"Originator": ORIGINATOR}),
        ("ua-only-codex", {"User-Agent": UA_CODEX}),
        ("ua-only-browser", {"User-Agent": UA_BROWSER}),
        ("codex-全量", {"User-Agent": UA_CODEX, "Originator": ORIGINATOR}),
    ]


def parse_models_response(section: str, text: str) -> list[str]:
    """从列模型响应里抽出模型 id 清单。三种 JSON 形态都认。"""
    import json
    import re

    try:
        data = json.loads(text)
    except Exception:
        # 退化：正则捞 id/name 字段
        return sorted(set(re.findall(r'"(?:id|name)"\s*:\s*"([^"]{2,80})"', text)))

    out: list[str] = []
    if isinstance(data, dict):
        # OpenAI 形态 {"data":[{"id":...}]}
        items = data.get("data") or data.get("models") or []
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    v = it.get("id") or it.get("name") or ""
                    if isinstance(v, str) and v:
                        # gemini 的 name 形如 "models/gemini-2.5-pro"
                        out.append(v.split("/")[-1] if v.startswith("models/") else v)
                elif isinstance(it, str):
                    out.append(it)
    elif isinstance(data, list):
        for it in data:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict):
                v = it.get("id") or it.get("name") or ""
                if isinstance(v, str) and v:
                    out.append(v)
    return sorted(set(out))
