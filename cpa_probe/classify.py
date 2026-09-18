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
    # 与「客户端」分开：那一类补**头**可能过，这一类补头一点用没有，要补的是
    # **请求体字段**（实测 `prompt_cache_key`）。usable=False 是因为确实没拿到
    # 200，不能凭空判活；但 downrank=False —— 站没问题，问题在探测形态。
    # 2026-09-10 alfa 实测：补 prompt_cache_key 后 400 立刻消失。
    "形态":    (False, False, "探测请求体缺真实客户端才有的字段（如 codex 的 "
                              "prompt_cache_key），站方按字段集校验后拒收。"
                              "补齐请求体形态后重测，非站点问题；补头无效"),
    "注入":    (False, False, "CPA 自身注入的工具被站方拒绝。关 disable-image-generation"),
    "限频":    (True,  False, "站方 bulk probe 保护。加大探测间隔重试"),
    "临时":    (True,  False, "站方负载上限，稍后可用"),
    "死路":    (False, True,  "分组无渠道 / 敏感词 / 模型不存在。充值无效"),
    "鉴权":    (False, False, "401 凭据无效或需特定客户端标识"),
    "未知":    (False, False, "无法定性，需人工看正文"),
}

# 判定顺序即列表顺序。第一条命中即返回。
# (类别, 说明, 正则, 限定状态码集合或 None 表示不限)
# 「这个分组里没有**这个模型**」的正文措辞表。单独提出来是因为
# `pipeline._model_specific_dead_end` 要用同一份判据 —— 它决定「换个模型再试」
# 还是「整段判死」。两处各写一份的后果实测过：措辞分叉后 classify 把正文判成
# 模型专属，pipeline 认不出来，于是一个换模型就能通的站被判成整段没救
# （2026-09-06 修，此前两处已分叉出「分组无该模型渠道」等两种说法）。
MODEL_CHANNEL_BODY = (
    r"无可用渠道|no available channel|model_not_found|可用渠道不存在"
    r"|分组.*无.*渠道"
    r"|当前 ?API ?不支持所选模型"
)


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
     # `unauthorized client detected` 也是客户端门禁（2026-09-17 从「鉴权」
     # 挪过来）。判「鉴权」的处置是「换把 Key」，而这句正文说的是**客户端**
     # 不被认可 —— 换十把 Key 结论都一样，要换的是请求形态。判错的代价
     # （golf.example 现场）：不进 identity 那一段 → 不写 cloak.mode /
     # fingerprint-profile → CPA 用默认形态转发 → 站方按同一条规则拒 →
     # 客户端拿 499/503，而直接用 Claude Code 调就通 —— 正是用户第 1 条
     # 描述的「直连可用、经 CPA 不可用」。
     r"|unauthorized client"
     r"|仅(?:支持|允许)[^，。]{0,20}客户端", None),

    # ---- 探测请求形态不合规：站是好的，错在探测没照着真实客户端发 ----
    #
    # 2026-09-10 实测 alfa.example（new-api 系）codex 段，模型 gpt-6-astra，
    # 逐字段逼近真实 Codex CLI 形态：
    #   model+input(数组)+stream+store+tool_choice+parallel_tool_calls
    #   +reasoning+include+instructions          -> 400 invalid codex request
    #   以上再 + prompt_cache_key                 -> 500 负载已达上限（形态被接受了）
    #   以上再 + text{verbosity}（不加 cache_key） -> 400（无效，不是这个字段）
    # 即：站方按真实 Codex CLI 的**字段集**校验，`prompt_cache_key` 是本项目
    # 原来缺的那一个。身份头无关 —— 把 Originator/User-Agent 全删掉结果不变。
    #
    # 为什么单独一类而不是并进「客户端」：
    #   · 「客户端」的处置是「补客户端标识（头）」，而这里补头没有任何用；
    #   · usable 必须是 False（没拿到 200，不能凭空判活），但**不该降权**
    #     —— 降权的前提是「站有问题」，这里站没问题；
    #   · 处置要明确指向「补请求体字段后重测」，否则下一个人会去查头。
    #
    # CPA 侧不删这个字段（`codex_executor_execute.go:61` 只删
    # prompt_cache_retention；翻译层 `:34` 只删 prompt_cache_options /
    # prompt_cache_retention；唯一删它的 `server_routes.go:245`
    # sanitizeCodexAlphaSearchBody 只作用于 alpha-search 开关），
    # 所以真实 Codex CLI 经 CPA 转发时该字段原样透传，网关本身是通的。
    # 出现这条 = 探测形态落后于 CPA 真实形态，属本项目的缺陷，不是站点缺陷。
    ("形态", "codex 请求体字段不全", r"invalid codex request", {"400"}),

    # ---- 站方硬拒 ----
    ("死路", "敏感词拦截", r"sensitive_words", None),

    # ---- 405 Method Not Allowed：站方维护或协议不支持 ----
    #
    # 实测 zulu 维护期间对所有 POST 一律回 405 + nginx HTML，GET 回 200 HTML
    # 维护页。405 在 CPA 里既不自动重试也不在用户 config 的 request-scoped-errors
    # 里，导致 CPA 直接把 405 返给客户端而不轮换下一凭据，明明有 29 个健康 codex
    # 凭据却因为优先级最高的这个站返回 405 而全失败（2026-09-06 实测）。
    # 405 从性质上看是「站方临时不可用」（维护）或「协议错配」（POST 到只认 GET
    # 的端点），不是凭据问题，应该降级 / 轮换 / 重试。分类定为「临时」，让 CPA
    # 写出 continue-and-cooldown 规则（与 500/502/503 同处理）。
    ("临时", "405 Method Not Allowed",
     r"405 not allowed|method not allowed|405 method", {"405"}),

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
    ("死路", "分组无该模型渠道", MODEL_CHANNEL_BODY, None),

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
        # 200 但正文是**整页 HTML** —— 不是 API 响应，是维护页/拦截页。
        # 判「临时」而不是「可用」：站方维护会结束，凭据本身没问题，
        # 不该降权也不该弃用（2026-09-11 实测 zulu.example 维护期形态：
        # GET /v1/models 回 200 + 「系统升级中」HTML，POST 一律 405）。
        # 与「200 但正文是 JSON 错误体」是同一类假阳性，载体不同而已。
        if looks_like_html(b):
            return "临时", "200 但正文是 HTML 页面（维护页/拦截页），不是 API 响应"
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
    if s == "405":
        # 405 兜底：关键词规则没命中时的裸 405
        return "临时", "405 Method Not Allowed"
    if s == "429":
        # BUG 修复 2026-09-13: 429 应归类为「临时」以参与重试，而非「限流」直接放弃
        # 原因：限流是短期现象，重试后可能恢复；分类为「限流」会让 pipeline._stage1 跳过重试
        return "临时", "429 限流 (可重试)"
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


def validate_success(section: str, status: str, body: str, *,
                     error: str = "", require_stream: bool = False) -> tuple[bool, str]:
    """Pure protocol evidence gate; does not assert model identity.

    Returns (valid, reason). Transport errors always win. Stream responses must
    contain functional output and the protocol's terminal event, not a handshake.
    User-generated text is never scanned for error keywords.
    """
    if error or str(status) != "200":
        return False, "transport-error" if error else "http-error"
    text = (body or "").strip()
    if not text or looks_like_html(text):
        return False, "empty-or-html"

    def bad(obj):
        return (not isinstance(obj, dict) or bool(obj.get("error"))
                or obj.get("type") in ("error", "response.failed", "response.incomplete")
                or obj.get("status") in ("failed", "incomplete", "cancelled"))

    def blocks(items):
        if not isinstance(items, list):
            return False
        for item in items:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("text"), str) and item["text"].strip():
                return True
            if item.get("type") == "tool_use" and item.get("name") and isinstance(item.get("input"), dict):
                return True
            if item.get("functionCall", {}).get("name"):
                return True
        return False

    def output(obj):
        if not isinstance(obj, dict):
            return False
        if section == "codex-api-key":
            return any(isinstance(it, dict) and (
                blocks(it.get("content")) or
                (it.get("type") == "function_call" and it.get("name")
                 and isinstance(it.get("arguments"), str)) or
                (it.get("type") == "image_generation_call" and bool(it.get("result"))))
                for it in (obj.get("output") or []) if isinstance(obj.get("output"), list))
        if section == "claude-api-key":
            return blocks(obj.get("content"))
        if section == "gemini-api-key":
            return any(isinstance(c, dict) and blocks((c.get("content") or {}).get("parts"))
                       for c in (obj.get("candidates") or []))
        if section == "openai-compatibility":
            for choice in obj.get("choices") or []:
                msg = choice.get("message") or choice.get("delta") or {}
                if isinstance(msg.get("content"), str) and msg["content"].strip():
                    return True
                if blocks(msg.get("content")):
                    return True
                for tool in msg.get("tool_calls") or []:
                    fn = tool.get("function") or {}
                    if fn.get("name") and isinstance(fn.get("arguments"), str):
                        return True
                if (msg.get("function_call") or {}).get("name"):
                    return True
        return False

    is_stream = any(line.startswith(("data:", "event:")) for line in text.splitlines())
    if not is_stream:
        # 错误体优先于「形态不对」（2026-09-12）
        # --------------------------------------
        # 原来先判 require_stream 直接返回 stream-required，于是 codex 段
        # 收到「200 + {"error": ...}」时报的是「该发流却发了整份 JSON」——
        # 把**站方明说的失败原因**盖成了探测形态问题，排查会走到完全错的
        # 方向（去查 stream 参数，而真正的原因是分组里没有可用渠道）。
        # okerror 画像正是这个形态：四段全回 200 错误体，codex 那段被
        # 报成 stream-required，`model-rejected` 事件里也拿不到原因。
        #
        # 判错误体不需要流：它是整份 JSON，解析得出来就算数。解析不出来
        # 再按原逻辑落到 stream-required / invalid-json。
        try:
            obj = json.loads(text)
        except (ValueError, TypeError, AttributeError):
            obj = None
        if isinstance(obj, dict) and bad(obj):
            return False, "error-envelope"
        if require_stream:
            return False, "stream-required"
        if obj is None:
            return False, "invalid-json"
        try:
            if section == "codex-api-key" and obj.get("status") != "completed":
                return False, "missing-terminal"
            return (True, "json-output") if output(obj) else (False, "missing-output")
        except (ValueError, TypeError, AttributeError):
            return False, "invalid-json"

    terminal = False
    produced = False
    done = False
    choice_seen, choice_done = set(), set()
    started_blocks, stopped_blocks = set(), set()
    # Blank lines delimit SSE records; multiline data is one JSON value.
    for record in re.split(r"\r?\n\r?\n", text):
        lines = record.splitlines()
        event = next((line[6:].strip() for line in lines if line.startswith("event:")), "")
        if event in ("error", "response.failed", "response.incomplete"):
            return False, "stream-error"
        data = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
        if not data:
            continue
        if data == "[DONE]":
            done = True
            continue
        try:
            obj = json.loads(data)
            if bad(obj):
                return False, "stream-error"
            kind = obj.get("type") or event
            if section == "codex-api-key":
                resp = obj.get("response") or {}
                if bad(resp):
                    return False, "stream-error"
                produced |= output(resp)
                if kind == "response.output_text.delta":
                    produced |= bool(obj.get("delta"))
                if kind == "response.output_item.done":
                    produced |= output({"output": [obj.get("item")]})
                if kind == "response.completed":
                    terminal = resp.get("status", "completed") == "completed"
            elif section == "claude-api-key":
                if kind == "message_start" and bad(obj.get("message") or {}):
                    return False, "stream-error"
                if kind == "content_block_start":
                    started_blocks.add(obj.get("index", 0))
                    produced |= blocks([obj.get("content_block")])
                if kind == "content_block_delta":
                    produced |= bool((obj.get("delta") or {}).get("text"))
                if kind == "content_block_stop":
                    stopped_blocks.add(obj.get("index", 0))
                terminal |= kind == "message_stop"
            elif section == "openai-compatibility":
                produced |= output(obj)
                for choice in obj.get("choices") or []:
                    idx = choice.get("index", 0)
                    choice_seen.add(idx)
                    if choice.get("finish_reason"):
                        choice_done.add(idx)
            elif section == "gemini-api-key":
                produced |= output(obj)
                for choice in obj.get("candidates") or []:
                    idx = choice.get("index", 0)
                    choice_seen.add(idx)
                    if choice.get("finishReason"):
                        choice_done.add(idx)
            else:
                return False, "unknown-protocol"
        except (ValueError, TypeError, AttributeError):
            return False, "invalid-stream-json"
    if section in ("openai-compatibility", "gemini-api-key"):
        terminal = bool(choice_seen) and choice_seen <= choice_done
        if section == "openai-compatibility":
            terminal &= done
    if section == "claude-api-key":
        terminal &= bool(started_blocks) and started_blocks <= stopped_blocks
    if not terminal:
        return False, "missing-terminal"
    return (True, "stream-output") if produced else (False, "missing-output")


def looks_like_html(text: str) -> bool:
    """正文是不是一个 HTML 页面而不是 API 响应。

    为什么需要（2026-09-11 实测 zulu.example）
    --------------------------------------------
    该站维护期间：`GET /v1/models` 返回 **HTTP 200 + text/html** 的
    「系统升级中」页面，所有 POST 返回 405 + nginx 错误页。

    200 + HTML 是个危险组合：
      · `classify` 的状态码兜底把 200 判成「可用（200 且无异常关键词）」；
      · `has_error_envelope` 只认 JSON 错误信封 —— HTML 解析不出 JSON，
        它返回 False，等于放行。
    两者叠加：一个正在维护的站会被判成**可用**，写进 config.yaml 就是死条目，
    而 CPA 每次真实请求都会失败。这与「200 但正文是错误体」是同一类假阳性，
    只是载体从 JSON 换成了 HTML，原来的那道闸拦不住。

    判据要窄，否则会误伤合法响应：
      · 只认**开头**就是 HTML 的（doctype / `<html` / `<!--`），
        以及开头是 `<` 且出现 `<title`/`<body`/`<head` 的；
      · 模型的正常输出里完全可能**包含** HTML 片段（用户让它写网页），
        所以绝不按「正文里有没有 `<div>`」来判 —— 只看正文整体形态。
      · 空正文不算 HTML（那是另一类，由状态码分支处理）。
    """
    t = (text or "").lstrip()
    if not t:
        return False
    low = t[:400].lower()
    if low.startswith("<!doctype html") or low.startswith("<html"):
        return True
    if low.startswith("<") and any(
            tag in low for tag in ("<title", "<body", "<head", "<h1")):
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
