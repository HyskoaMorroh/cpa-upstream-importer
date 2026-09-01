"""从失败正文里读出站方索要的 anthropic-beta 项。

为什么单独一个模块（2026-09-01 anyrouter.top 实测）
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


def wanted(body: str) -> list[str]:
    """正文点名要求的 beta 项，按 _HINTS 顺序去重返回。正文没点名就是空列表。"""
    text = body or ""
    out: list[str] = []
    for beta, pat in _HINTS:
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
