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

import json
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
    # 站方只认特定客户端（Claude Code / Codex CLI 等）。与「门禁」分开是因为
    # 处置完全不同：门禁要站方开通，这个补客户端标识就可能过。
    # 2026-08-31 实测：某站回 **503** + "only allows Claude Code clients"，
    # 而 503 在下面的状态码兜底里是「临时」（可用、该重试）—— 于是探测白重试
    # 两次，而重试永远不可能过。所以这条规则必须**在状态码之前**命中。
    "客户端":  (False, False, "站方只认特定客户端。补 Claude Code / CLI 标识后重试；"
                            "仍不通则用人工接管（探测无法复制该客户端形态）"),
    "IP封":    (False, True,  "按出口 IP 拉黑。加 proxy-url 可能救活"),
    # 与「IP封」分开是因为处置相反：WAF 认的是客户端形态（UA/TLS/行为），
    # 换出口 IP 没用。2026-09-01 实测 hotel 三段都配了 mihomo 代理，走代理
    # 仍返回同一个「访问已被拦截」页 —— 若判成「IP封」，处置会写「加
    # proxy-url 可能救活」，那是把用户往一条已证伪的路上引。
    "WAF":     (False, False, "站方 WAF 按客户端形态拦截，换出口 IP 无效。"
                              "先试客户端画像；仍不通则该站不接受编程访问"),
    # 分组按时间窗口开放。usable=True —— 凭据有效，窗口内自然可用。
    # 2026-09-01 实测 hotel codex 段：403「当前分组本时段不可调用，
    # 可调用时段为：09:00~18:00」。原来落到「门禁」(usable=False, 处置写
    # 「配置层无解，需站方开通」)，等于把一个每天能用 9 小时的站判死。
    "时段":    (True,  False, "分组按时间窗口开放，窗口外一律拒绝。"
                              "记下窗口，窗口内复测；不要降权也不要弃用"),
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
    #
    # 2026-08-31 补英文说法：原来只认 quota 家族与中文「余额不足」，于是
    # `insufficient balance`、`credit exhausted` 这两种常见英文表述落到「门禁」
    # —— 门禁是 usable=False，意味着**一个充值就能用的站被判死**。
    # 判错方向是「把活站当死站」，用户白丢一个可用站且看不出原因。
    ("余额", "额度耗尽",
     r"budget pool|quota has been exhausted|insufficient_(?:user_)?quota"
     r"|quota_exceeded|预扣费额度失败|user quota is not enough|余额不足"
     r"|insufficient[_ ](?:balance|credit|funds|fund)"
     r"|credit(?:s)?[_ ](?:exhausted|depleted|used up)"
     r"|balance[_ ](?:is[_ ])?(?:too[_ ]low|insufficient|not[_ ]enough)"
     r"|out of credit|no remaining credit|欠费|账户余额|请充值|余额已用完", None),
    ("余额", "模型额度达上限", r"额度已经?达到?上限", None),

    # ---- 探测方法本身触发的，不是站点故障 ----
    ("限频", "bulk probe 保护", r"bulk probe|bulk model probing", None),
    ("反测活", "测活探针拦截", r"反测活|测活探针", None),

    # ---- 客户端身份门禁：不限状态码 ----
    #
    # 实测那个站回 503，若只按状态码就落到「临时」并被重试两次 —— 而这类拒绝
    # 与站方负载无关，重试一万次也一样。放在这里（余额之后、状态码兜底之前）
    # 才能截住。
    ("客户端", "站方只认特定客户端",
     r"only allows? [\w\s-]*clients?"
     r"|restricted to [\w\s-]*clients?"
     r"|client[\s_-]?not[\s_-]?allowed"
     r"|仅(?:支持|允许)[^，。]{0,20}客户端", None),

    # ---- 站方硬拒 ----
    ("死路", "敏感词拦截", r"sensitive_words", None),
    ("鉴权", "需特定客户端标识", r"unauthorized client", None),

    # ---- CPA 自注入工具被拒 ----
    ("注入", "image_generation 工具被拒",
     r"image[_ ]generation is not enabled|image_generation", {"403", "400"}),

    # ---- 时间窗口：必须在「门禁」与状态码兜底之前 ----
    #
    # 正文里带出可调用时段，说明拒绝原因是**时间**，与凭据、客户端、IP 都无关。
    # 窗口写法见实测：「当前分组本时段不可调用，可调用时段为：09:00~18:00」。
    ("时段", "分组按时段开放",
     r"本时段不可调用|当前时段不可用|不在可(?:调用|用)时段"
     r"|可调用时段|not available (?:at|during) this (?:time|hour)"
     r"|only available (?:from|between)\s*\d{1,2}:\d{2}"
     r"|outside (?:the )?(?:allowed |service )?(?:time )?window", None),

    # ---- Cloudflare / WAF / IP ----
    #
    # 「拦截页」与「CF 挑战」分开判：
    #   · 站方自建的拦截页（访问已被拦截 / 安全验证）= WAF 按客户端形态拦，
    #     换 IP 无效（实测带代理仍被拦）。
    #   · CF 的 challenge-platform / cdn-cgi = 边缘按 IP 声誉挑战，换 IP 有效。
    ("WAF", "站方自建拦截页", r"访问已被拦截|安全验证|人机验证|访问受限", None),
    ("IP封", "CF 挑战或边缘拦截",
     r"challenge-platform|cf-mitigated|cdn-cgi", None),
    ("IP封", "CF Attention Required", r"attention required|just a moment", None),

    # ---- 门禁 ----
    ("门禁", "1m 上下文未开通", r"1m\s*上下文|\b1m\b.*context|context.*\b1m\b", {"400", "403"}),
    # 这两条必须分清，因为下游的处置完全相反（2026-09-01 修正）：
    #   · 「Key 分组不匹配」= 这把 Key 被分到了错的组，换模型没用 → 站级死路
    #   · 「分组无该模型渠道」= 这个组里没有**这个模型** → 换模型可能就通
    # 原来第一条的 `分组.*无.*渠道` 会先命中「该分组无可用渠道」这类正文，
    # 抢在第二条前面打上站级判据，于是 pipeline 的模型专属豁免认不出来。
    # 现在第一条只保留「Key 与分组的归属不对」这一种说法。
    ("死路", "Key 分组不匹配",
     r"group platform is not|api key group"
     r"|分组不匹配|密钥分组|key.{0,10}分组.{0,10}(?:不|错)", None),
    ("死路", "分组无该模型渠道",
     r"无可用渠道|no available channel|model_not_found|可用渠道不存在"
     r"|分组.*无.*渠道"
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


def has_error_envelope(text: str) -> bool:
    """正文顶层是不是一个错误结构。仅用于「HTTP 200 但正文报错」的识别。

    为什么需要（2026-08-31 实测的假阳性）
    ------------------------------------
    有的中转站对**所有**请求都回 HTTP 200，把真实错误放在正文里。而探测这边
    `Attempt.ok` 只看状态码、`model_matches` 在拿不到 model 字段时按设计放行
    （无证据不判换模）—— 于是这种站四段全判可用、注册 11 个模型，实际完全不能用。
    死站进了 config.yaml 会耗尽重试预算，最终让客户端收到 500。

    判据必须窄，否则会误伤合法响应：
      · 只看**顶层** error / 顶层 "type":"error"。嵌套在 choices、candidates、
        content 里的 error 字样不算 —— 模型正常输出里完全可能谈论 error。
      · 顶层 error 为空值（null / "" / {} / []）不算：有的站无论成败都带一个
        空 error 字段占位。
      · 解析不出 JSON 就返回 False —— 流式响应、纯文本响应都走这条，
        不能因为「不是 JSON」就判成错误。

    只在 status == 200 的路径上调用；非 200 本来就走 classify 的规则表。
    """
    try:
        obj = json.loads(text or "")
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False

    err = obj.get("error")
    if isinstance(err, (dict, list)) and len(err) > 0:
        return True
    if isinstance(err, str) and err.strip():
        return True

    # Anthropic 的错误形态：{"type":"error","error":{...}}
    if str(obj.get("type") or "").strip().lower() == "error":
        return True
    return False


def body_excerpt(text: str, limit: int = 400) -> str:
    """正文摘要：剥 HTML 标签、压空白、截断。用于 UI 展示判据。"""
    t = text or ""
    t = re.sub(r"<script[^>]*>.*?</script>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")


# 可调用时段的解析：把正文里的窗口抽成结构化的 (起, 止)，供报告与复测调度用。
# 只认「HH:MM~HH:MM」「HH:MM-HH:MM」「from HH:MM to HH:MM」三种写法 ——
# 认不出就返回 None，让调用方如实报告「有时段限制但窗口未知」，而不是猜。
_WINDOW_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*(?:~|-|—|–|to|至|到)\s*(\d{1,2}):(\d{2})")


def time_window(body: str) -> tuple[str, str] | None:
    """从正文里抽出可调用时段。返回 ("09:00", "18:00") 或 None。

    为什么要抽出来：「时段」类的处置是「窗口内复测」，而复测调度需要知道
    窗口是什么。只报「有时段限制」的话，用户仍得自己去翻正文。
    """
    m = _WINDOW_RE.search(body or "")
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if not (0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= m1 <= 59 and 0 <= m2 <= 59):
        return None
    return (f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}")


def in_time_window(window: tuple[str, str] | None, now_hhmm: str) -> bool | None:
    """当前时刻是否在窗口内。window 或 now 无效时返回 None（未知，不猜）。

    跨零点的窗口（22:00~06:00）按「起 > 止」识别 —— 那种窗口在午夜两侧都成立。
    """
    if not window:
        return None
    try:
        start, end = window
        n = tuple(int(x) for x in now_hhmm.split(":", 1))
        s = tuple(int(x) for x in start.split(":", 1))
        e = tuple(int(x) for x in end.split(":", 1))
    except (ValueError, AttributeError, TypeError):
        return None
    if s <= e:
        return s <= n <= e
    return n >= s or n <= e          # 跨零点
