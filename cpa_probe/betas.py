"""从失败正文里读出站方索要的 anthropic-beta 项。

为什么单独一个模块（2026-09-01 alfa 实测）
--------------------------------------------------
现场形态：整梯 8 档全返回 400，八档的正文**逐字相同**：

    {"error":"1m 上下文已经全量可用，请启用 1m 上下文后重试","type":"error"}

八档的请求头差异极大（baseline 裸头、cc-full 带 X-Stainless 全族、
browser-ua 装浏览器），响应却完全一致 —— 说明站方**根本没看客户端身份**，
它在更早一步就拦了，且把要求写在正文里：你没启用 1m 上下文。

原来的处置是把它归成「门禁」（classify.py 的 1m 规则）然后记账走人，报给
用户「整梯 8 档全不通」。归类识别对了，行动却是零 —— 站方明说了要什么，
补上再打一次就通，没有任何理由让用户去手填模型清单。

这不是猜测式重试
----------------
只在正文明确点名某个能力时才补对应 beta。正文没点名的，一项都不加 ——
无条件多带 beta 会让「按项检查 beta」的站看到自相矛盾的请求（同样的理由
让 _CC_BETAS_* 去掉了 oauth-2025-04-20，见 profiles.py:66 附近）。
"""

from __future__ import annotations

import re

# (beta 标识, 正文里指向它的说法)
#
# 取值来自 CLIProxyAPI 的 claudeCodeCLIBetas()，条件项那部分 ——
# 真实客户端只在用到对应能力时才发它们，所以画像梯的常量清单里没有。
_HINTS: list[tuple[str, re.Pattern[str]]] = [
    # 1m 上下文。中英两种说法都见过；"1m" 前后允许标点或空白。
    ("context-1m-2025-08-07",
     re.compile(r"1m\s*上下文|\b1m\b[^\n]{0,20}context|context[^\n]{0,20}\b1m\b",
                re.I)),
    # 长输出。站方措辞常是「需要开启 128k 输出」。
    ("output-128k-2025-02-19",
     re.compile(r"128k[^\n]{0,10}(?:输出|output)|(?:输出|output)[^\n]{0,10}128k",
                re.I)),
    # 细粒度工具流式。
    ("fine-grained-tool-streaming-2025-05-14",
     re.compile(r"fine[- ]?grained[- ]?tool|细粒度工具", re.I)),
]

# 站方在正文里**指名替代模型**（2026-10-14 本机实测加）
# ----------------------------------------------------
# 现场（alfa.example，claude 段）：目录报了 12 个 claude 名字，但请求
# `claude-opus-5-5` 回 400，正文是一句完整的使用说明 ——
#
#     由于 claude 模型供应难以保证，上线 gpt-6-astra-cc-format 模型，
#     支持 messages 格式，通过 claude --model 'gpt-6-astra-cc-format[1m]'
#     使用该模型
#
# 也就是说这个站的 claude 段**没有真正的 claude 模型**，站方改供一个
# 别的名字，而且把正确用法写得很清楚。原来的处置是：这条正文既不命中
# `_HINTS` 的任何一项（它含 `[1m]` 但不含「1m 上下文」），classify 也
# 只能归「未知」—— 于是画像梯不跑、spare 不补，这一发基线额度白费。
# 配合 `_BASELINE_MODELS = 2`（另一发 `claude-opus-4-7` 回 503），整段
# 判「未实测」，而站方点名的那个模型**一次都没试过**。
#
# `[1m]` 后缀是 claude CLI 的语法糖，不是 HTTP 层的东西：实测把它写进
# `model` 字段仍然 400（三把 Key 一致），真正起作用的只有
# `anthropic-beta: context-1m-2025-08-07` 头。所以抽取时要把后缀剥掉，
# 并把它翻译成对应的 beta 项 —— 两者是同一个指令的两半。
_SUBSTITUTE = [
    # `--model 'name'` / `--model "name"`：最明确的形态，优先。
    re.compile(r"--model\s+['\"]([A-Za-z0-9][A-Za-z0-9._\-]*(?:\[[^\]]+\])?)['\"]"),
    # 「上线 xxx 模型」：同一句话的中文表述，作为次选。
    re.compile(r"上线\s*([A-Za-z0-9][A-Za-z0-9._\-]*)\s*模型"),
]

# 模型名后缀 -> 它等价的 beta 项。CLI 用后缀表达，HTTP 层要头。
_SUFFIX_BETA = {"1m": "context-1m-2025-08-07"}


def substitute_model(body: str) -> tuple[str, list[str]]:
    """正文里站方指名的替代模型，返回 (模型名, 需要补的 beta 列表)。

    没点名就回 `("", [])`。模型名已剥掉 `[...]` 后缀，后缀翻译成 beta ——
    实测后缀写进 `model` 字段无效（见 `_SUBSTITUTE` 上方说明）。

    只认**正文自己写出来的**名字，不做任何猜测或拼接：站方给什么用什么。
    """
    text = body or ""
    for pattern in _SUBSTITUTE:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1).strip()
        extra: list[str] = []
        suffix = re.search(r"\[([^\]]+)\]$", raw)
        if suffix:
            raw = raw[: suffix.start()]
            for part in re.split(r"[,\s]+", suffix.group(1).strip().lower()):
                beta = _SUFFIX_BETA.get(part)
                if beta and beta not in extra:
                    extra.append(beta)
        # 名字本身要像个模型名；太短或含路径分隔符的一律丢掉，避免把
        # 正文里的别的词当成模型名拿去打一次无意义的请求。
        if len(raw) < 3 or "/" in raw or "\\" in raw:
            continue
        return raw, extra
    return "", []


def wanted(body: str, *, source_identity=None) -> list[str]:
    """正文点名要求的 beta 项，按 _HINTS 顺序去重返回。正文没点名就是空列表。"""
    text = body or ""
    out: list[str] = []
    for beta, pat in _HINTS:
        if source_identity is not None:
            values = (list(source_identity.claude_betas_conditional.values())
                      + source_identity.claude_betas_unconditional)
            prefix = beta.rsplit("-", 3)[0] + "-"
            beta = next((value for value in values if value.startswith(prefix)), beta)
            if prefix == "context-1m-":
                beta = source_identity.claude_betas_conditional.get("claudeContext1MBeta", beta)
        if pat.search(text) and beta not in out:
            out.append(beta)
    return out


def merge(current: str, extra: list[str]) -> str:
    """把 extra 并进现有 anthropic-beta 值，保序去重。

    保序的理由：站方偶有按前缀匹配的实现，头部那几项（claude-code-... 打头）
    是门票主体，不能被追加项挤走。
    """
    seen: list[str] = []
    for item in (current or "").split(","):
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    for item in extra:
        if item and item not in seen:
            seen.append(item)
    return ",".join(seen)
