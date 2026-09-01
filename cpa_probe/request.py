"""按段构造上游请求。路径与鉴权形态与 CPA 自身 executor 完全对齐。

源码依据（CLIProxyAPI-main）：
  gemini  gemini_executor.go:186-190   {base}/v1beta/models/{model}:generateContent
                                       Key 走 **x-goog-api-key 头**，不是 query string
  codex   codex_executor_execute.go:76 {base}/responses                base 已含 /v1
  claude  claude_executor_execute.go:30 {base}/v1/messages?beta=true
  compat  openai_compat_executor.go:146 {base}/chat/completions        base 已含 /v1

段名一律用 config.yaml 里的原始键名字符串（"gemini-api-key" 等），不另设枚举 ——
少一层映射，写回时直接就是 YAML 的键。

claude 段鉴权头：probe-fix 用 Authorization: Bearer，audit 用 x-api-key。
Anthropic 官方两种都支持，中转站实现不一。本模块**两个都发** —— 多一个头
不会让通的站变不通，但少一个可能让通的站判成 401。
（CPA 自己按 base 分流：api.anthropic.com 用 x-api-key，其余用 Bearer，
 claude_executor_request.go:758-765。探测两个都发覆盖两种实现。）

「与 CPA 对齐」优先于「减少变量」（2026-09-01 修正两处）
--------------------------------------------------
原来这两处为了「少引入失败面」而故意与 CPA 不同，结果都造成了误判：

  · gemini 段把 Key 放 query string。实测 chiangma.com 三种画像全部连接层
    失败（000），而前端用 x-goog-api-key 头能拉到几百个模型 —— 探测测的
    不是 CPA 会走的那条路，于是把一个可用站判死。
  · claude 段不带 `?beta=true`。站方按 query 参数分流时，探测与真实转发
    结论不一致，两个方向的误判都可能发生。

探测要回答的是「CPA 这样发通不通」，不是「这个端点本身通不通」。形态不一致时，
探测结论对 config.yaml 就没有指导意义。
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
        # Key 走 **头** 而不是 query string —— CPA 只用 x-goog-api-key
        # （gemini_executor.go:190/304/424/504/665，全库无 `?key=`）。
        # 2026-09-01 实测：用 query string 时 chiangma.com 三种画像全部
        # 连接层失败（000），而前端用头的方式能拉到几百个模型 —— 探测测的
        # 不是 CPA 真实会走的那条路，于是把一个可用站判死。
        url = f"{base}/v1beta/models/{model}:generateContent"
        headers["x-goog-api-key"] = api_key
        body: dict = {"contents": [{"role": "user", "parts": [{"text": text}]}]}

    elif section == "codex-api-key":
        url = f"{base}/responses"
        headers["Authorization"] = f"Bearer {api_key}"
        body = {"model": model, "stream": False, "input": text}

    elif section == "claude-api-key":
        # 带 `?beta=true` —— CPA 三条 claude 路径全都带
        # （claude_executor_execute.go:30、_stream.go:32、_tokens.go:128）。
        # 原来不带的理由是「少一个变量」，但那让探测与真实转发形态不一致：
        # 站方按 query 参数分流时，探测通了而 CPA 不通（或反之）。
        # 对齐优先于减少变量 —— 探测要问的是「CPA 这样发通不通」。
        url = f"{base}/v1/messages?beta=true"
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
        # 同 build_request：Key 走头，与 CPA 一致
        headers["x-goog-api-key"] = api_key
        return f"{base}/v1beta/models", headers

    headers["Authorization"] = f"Bearer {api_key}"
    if section == "claude-api-key":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        return f"{base}/v1/models", headers

    # codex / compat：base 已含 /v1
    return f"{base}/models", headers


def identity_combos(section: str, cfg: dict | None = None):
    """**已弃用**，转发给 `profiles.ladder()`。保留只为不破坏外部调用。

    为什么弃用（2026-09-01）
    ----------------------
    原实现四段共用一份 codex 形态（User-Agent + Originator）。`Originator`
    是 codex 独有的头，对 claude / gemini 段毫无意义 —— 于是 claude 段一个
    对的组合都没有，五种全试也过不去。实测 agentrouter 的门票是
    user-agent + anthropic-beta + x-app 三项缺一不可，而那三项从不在这个表里。

    更根本的是返回值只有 headers。`metadata.user_id` 是请求**体**字段，
    这个签名压根表达不了它 —— 而它是 zzzcoding 的门票之一。

    新表见 cpa_probe/profiles.py：按段分开、按客户端族分组、族内嵌套超集、
    headers 与 body_patch 同时给。
    """
    from . import profiles

    return [(p.name, p.headers) for p in profiles.ladder(section, cfg)
            if not p.body_patch]


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
