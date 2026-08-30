"""响应定性：把 (状态码, 正文) 判成一个类别 + 处置建议。

规则来源：probe-fix.py 的 classify（8 类）与 audit-upstreams.py 的 classify（7 态）
合并。冲突处取 probe-fix 口径，理由：
  1. 它多「反测活」「注入」两类，都是实测踩出来的；
  2. 它把余额判定提到 Cloudflare 判定之前 —— 2026-08-29 修正过一次真实误判
     （relay-l 的 403 正文是「预扣费额度失败, 剩余 $0.190928」，是余额不是门禁）。

核心原则：**正文关键词优先于状态码**。同一状态码在不同站含义不同 ——
403 可以是余额、门禁、IP 封、Cloudflare 边缘拦截四种里的任意一种。
"""

from __future__ import annotations

import re

# 类别 -> (是否可用, 是否该降权, 人类可读处置)
# 「可用」= 凭据本身有效，能接进 CPA
# 「该降权」= 配置层应当调低 priority 或加 proxy-url/headers
DISPOSITION = {
    "可用":    (True,  False, "直接接入"),
    "换模":    (False, True,  "静默换模，照常计费却返回另一模型。不要接入该模型"),
    "余额":    (True,  False, "凭据有效，充值即恢复。**不要降权** —— 充值自愈"),
    "限流":    (True,  False, "429 限流，凭据有效。CPA 自带冷却与轮换"),
    "门禁":    (False, False, "站方策略或后台开关，配置层无解。需站方侧开通"),
    "IP封":    (False, True,  "按出口 IP 拉黑。加 proxy-url 可能救活"),
    "边缘":    (True,  False, "Cloudflare 概率性拦截，重试即可。不代表不可用"),
    "反测活":  (True,  False, "探测文本触发站方测活拦截。换探测文本重测，非站点问题"),
    "注入":    (False, False, "CPA 自身注入的工具被站方拒绝。关 disable-image-generation"),
    "限频":    (True,  False, "站方 bulk probe 保护。加大探测间隔重试"),
    "临时":    (True,  False, "站方负载上限，稍后可用"),
    "死路":    (False, True,  "分组无渠道 / 敏感词 / 模型不存在。充值无效"),
    "鉴权":    (False, False, "401 凭据无效或需特定客户端标识"),
    "未知":    (False, False, "无法定性，需人工看正文"),
}

# 判定顺序即列表顺序。第一条命中即返回。
# (类别, 说明, 正则, 限定状态码集合或 None 表示不限)
_RULES: list[tuple[str, str, str, set[str] | None]] = [
    # ---- 余额类：必须排在 CF/门禁之前。403 也可以是余额 ----
    ("余额", "额度耗尽",
     r"budget pool|quota has been exhausted|insufficient_(?:user_)?quota"
     r"|quota_exceeded|预扣费额度失败|user quota is not enough|余额不足", None),
    ("余额", "模型额度达上限", r"额度已经?达到?上限", None),

    # ---- 探测方法本身触发的，不是站点故障 ----
    ("限频", "bulk probe 保护", r"bulk probe|bulk model probing", None),
    ("反测活", "测活探针拦截", r"反测活|测活探针", None),

    # ---- 站方硬拒 ----
    ("死路", "敏感词拦截", r"sensitive_words", None),
    ("鉴权", "需特定客户端标识", r"unauthorized client", None),

    # ---- CPA 自注入工具被拒 ----
    ("注入", "image_generation 工具被拒",
     r"image[_ ]generation is not enabled|image_generation", {"403", "400"}),

    # ---- Cloudflare / IP ----
    ("IP封", "CF 挑战或站方拦截页",
     r"challenge-platform|cf-mitigated|cdn-cgi|访问已被拦截|安全验证", None),
    ("IP封", "CF Attention Required", r"attention required|just a moment", None),

    # ---- 门禁 ----
    ("门禁", "1m 上下文未开通", r"1m\s*上下文|\b1m\b.*context|context.*\b1m\b", {"400", "403"}),
    ("死路", "Key 分组不匹配", r"group platform is not|api key group|分组.*无.*渠道", None),
    ("死路", "分组无该模型渠道",
     r"无可用渠道|no available channel|model_not_found|可用渠道不存在"
     r"|当前 ?API ?不支持所选模型", None),

    # ---- 临时 ----
    ("临时", "站方负载上限", r"负载已经?达到?上限", None),
]


def classify(status: str, body: str) -> tuple[str, str]:
    """返回 (类别, 判据说明)。

    status 用字符串，"000" 表示连接失败（与三个脚本口径一致）。
    """
    s = str(status or "000")
    b = (body or "")
    low = b.lower()

    for kind, why, pattern, codes in _RULES:
        if codes is not None and s not in codes:
            continue
        if re.search(pattern, low, re.I):
            return kind, why

    # ---- 关键词全不命中，退到状态码 ----
    if s == "200":
        return "可用", "200 且无异常关键词"
    if s == "401":
        return "鉴权", "401 未授权"
    if s == "402":
        return "余额", "402 需付费"
    if s == "403":
        # 403 + 空正文 = CF 概率性边缘拦截，重试即可（probe-fix 实测口径）
        if not b.strip():
            return "边缘", "403 且正文为空，CF 概率拦截"
        return "门禁", "403 且无余额/CF 特征，判为站方策略"
    if s == "404":
        return "死路", "404 路径或模型不存在"
    if s == "429":
        return "限流", "429 限流"
    if s.startswith("5"):
        return "临时", f"{s} 上游错误"
    if s == "000":
        return "未知", "连接失败或超时"
    return "未知", f"未覆盖的状态码 {s}"


def is_usable(kind: str) -> bool:
    return DISPOSITION.get(kind, (False, False, ""))[0]


def should_downrank(kind: str) -> bool:
    return DISPOSITION.get(kind, (False, False, ""))[1]


def advice(kind: str) -> str:
    return DISPOSITION.get(kind, (False, False, "未知类别"))[2]


def body_excerpt(text: str, limit: int = 400) -> str:
    """正文摘要：剥 HTML 标签、压空白、截断。用于 UI 展示判据。"""
    t = text or ""
    t = re.sub(r"<script[^>]*>.*?</script>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")
