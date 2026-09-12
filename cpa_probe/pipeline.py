"""四阶段探测流水线。

对每个 (url, key) 候选：
  ① 段归属  —— 四段各打一次，看哪几段通。段决定 URL 形态与协议路径。
  ② 模型发现 —— 先问 /models 目录，再逐个验。compat 段必须探到至少一个，
                留空则该 provider 注册 0 个模型（service_models.go:714-717）。
  ③ 处置    —— 不通的段：先试代理，再试补标识头。
                优先级 proxy-url > headers > 降 priority，绝不用 weight: 0。
  ④ 质量    —— 通的段：验静默换模；可选二分探 max-context-length。

节流：每次请求之间等 gap 秒。relay-b.example 有 bulk probe guard
（60 秒内 4 个不同模型即触发），gap 默认 3 秒。

成本：不开上下文探测时单候选约 10-25 次请求，body 都很小。开上下文探测后
每个 (段, 模型) 多 4-6 次大 body 请求 —— 那部分明确要算钱，默认只对
新增候选的首个模型跑。
"""

from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import re
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Callable

# 直接导入函数，不要写 `from . import classify as cls`。
# __init__.py 里 `from .classify import classify` 会把包属性 cpa_probe.classify
# 从「模块」改写成「函数」，且它排在 `from .pipeline import ...` 之前 ——
# 那样 cls 拿到的是函数，cls.classify(...) 必然 AttributeError。
# 这条路径只有真发请求时才会走到，纯逻辑用例覆盖不到，所以必须写死成函数导入。
from . import betas
from .classify import MODEL_CHANNEL_BODY as _MODEL_CHANNEL_BODY
from .classify import body_excerpt as _body_excerpt
from .classify import classify as _classify
from .classify import has_error_envelope as _has_error_envelope
from .classify import looks_like_html as _looks_like_html
from .classify import time_window as _time_window
from .classify import validate_success
from .resources import HOST_LIMITER
from . import client, fingerprint, model_catalog, profiles, request
from .parse import SECTIONS, ParsedRow, base_for_section, host_of

# 协议证据闸的机器码 → 给人看的话（2026-09-12）。
#
# `classify.validate_success` 返回的是稳定机器码（error-envelope 这类），
# 事件与界面要说人话。这张表只做翻译，不新增判据 —— 漏了的码按
# 「200 但<码>」兜底，不会把未知情形说成已知情形。
_GATE_REASON_CN = {
    "error-envelope": "200 但正文是错误体",
    "stream-required": "200 但该发事件流却回了整份 JSON",
    "missing-output": "200 但正文里没有任何有效输出块",
    "missing-terminal": "200 但流里没有终止事件（响应未完成）",
    "invalid-json": "200 但正文不是合法 JSON",
}

# 每段的种子模型。/models 目录拿不到时兜底；拿到目录时用来定验证顺序。
# 按段分开 —— claude 段问 gpt-5.6-sol 必然 404，那是 CPA 的段语义决定的。
#
# 2026-09-02 跟着新规则更新：
#   · gemini 段去掉 flash（新规则只要 *-pro >= 2.5），换成 3.1-pro 与 2.5-pro
#     —— 两个版本都探一下：3.1 是最新，但不少站还只开到 2.5
#   · claude 段加 fable-5，codex 段加 gpt-5.6（用户指定清单里有）
# 每段仍只放 2-3 个：这是**基线阶段**逐个打的清单，多一个就多一轮请求。
# 完整的「市面最新清单」在 model_catalog.latest_models，那是写回时用的。
SEED_MODELS: dict[str, list[str]] = {
    "gemini-api-key": ["gemini-3.1-pro", "gemini-2.5-pro"],
    "codex-api-key": ["gpt-5.6-sol", "gpt-5.6-terra"],
    "claude-api-key": ["claude-opus-5", "claude-sonnet-5"],
    "openai-compatibility": ["gpt-5.6-sol", "claude-opus-5", "gemini-3.1-pro"],
}

# 保留的模型族。2026-08-29 定 gemini/gpt/claude 三类，2026-09-02 加 kimi
# （用户把它列进 compat 段的允许清单）。仍然有意排除 deepseek / grok /
# qwen / glm / llama。
#
# 单一定义在 model_catalog.FAMILIES —— 这里做别名是因为几处调用与测试按
# 这个名字引用。规则本身**只在那一处**，不许在这里再写一套。
MODEL_PREFIX_WHITELIST = model_catalog.FAMILIES

def model_family(name: str) -> str:
    """模型属于哪一族。返回 "gemini" / "gpt" / "claude" / "kimi" / ""。

    只看名字，不看它挂在哪个段 —— 判据就是名字本身。
    实现在 model_catalog，这里保留名字是因为测试与几处调用按这个名字引用。
    """
    return model_catalog.family(name)


# 每段只能探本族的模型。
#
# 为什么必须按段过滤（2026-09-01 从 79 凭据实跑日志量化）
# ----------------------------------------------------
# 聚合站的 /models 目录把三族混在一起报（一个站同时声明 claude-opus-5、
# gpt-5.6-sol、gemini-2.5-pro）。`_probe_order` 原来把整份目录倒进队列，
# 只过 `model_allowed`（三族全放行），于是：
#
#     gemini 段拿 claude-opus-4-6 去打 /v1beta/models/claude-opus-4-6:generateContent
#     codex  段拿 claude-opus-5   去打 /responses
#
# CPA 永远不会这样发 —— 段决定协议路径，模型必须是该协议下的模型。
# 实测这类请求占协议段总请求的 **56%**（435/773），成功率 5.5%，而同族
# 是 24.6%（4.5 倍差）。更要紧的是跨族请求里 **240 次返回 500**（占跨族
# 55%，同族一次都没有），而 500 被 classify 判「临时」→ 触发重试 → 又一次
# 500，日志里 666 次 500 的最大头就是这么来的。
#
# 除了白烧配额，反复拿协议不匹配的模型名轰炸正是站方风控盯的形态 ——
# 用户明确要求过不许用容易触发封号的探测手段。
#
# compat 段不设限：它走 /chat/completions，本身就是「什么模型都能转」的
# 万能口，三族都合法（日志里 compat 段跨族请求的成功率与同族持平）。
SECTION_FAMILY: dict[str, str] = {
    "gemini-api-key": "gemini",
    "codex-api-key": "gpt",
    "claude-api-key": "claude",
}


def model_fits_section(section: str, name: str) -> bool:
    """这个模型能不能在这个段上用。

    2026-09-02 起委托给 `model_catalog.section_allows` —— 用户那轮把规则收紧
    （gemini 只要 *-pro 且 >= 2.5、compat 加 kimi、排除图像/oss 这类非对话
    模型），而规则原来散在这里、`model_allowed` 与 `web/app.js` 三处，
    三处不一致的后果就是现场截图那两个问题。判据只留一处。

    **探测与写入共用同一套判据**，不给探测放宽。理由：放宽会让「用 flash 探通、
    却往 config.yaml 写 pro」成为可能，而那个 pro 从没验证过 —— 正是本工具
    一直在避免的「未验证当已验证」。
    """
    return model_catalog.section_allows(section, name)


# 每段最多验几个模型。聚合站声明几百个（relay-m 曾 838 个），
# 全验会触发反测活且极贵。
MAX_MODELS_PER_SECTION = 4

# 每段最多**尝试**几次模型验证。
#
# 为什么不能只有 MAX_MODELS_PER_SECTION（2026-09-01 量化发现）：
# 那个是「已接受几个」的上限，而失败的尝试不增加计数。一个声明 838 个模型
# 的聚合站，如果它的模型全都验不过（分组不含、限时段、要门票），循环会把
# 白名单过滤后剩下的**全部**打一遍才结束 —— 上面那句注释说的「全验会触发
# 反测活且极贵」正是这个情形，但原来的 break 条件兑现不了它。
#
# 取 10：够拿到 4 个可信模型（种子 2-3 个 + 目录里前几个通常就够），
# 又把最坏情形从「目录长度」压到常数。
MAX_MODEL_ATTEMPTS_PER_SECTION = 10

# 什么情况下值得换出口 IP 再试一次（2026-09-01 按 79 凭据实跑日志定的）
#
# 为什么不是「不通就试代理」：那轮日志里 56 个 (站, 段) 组合，35 个不通。
# 全试要多发 70-105 次请求，而其中一多半的失败原因与出口 IP 毫无关系 ——
# 余额要充值、鉴权是 Key 不对、死路是分组没这个渠道、客户端要的是请求画像。
# 那些请求发出去注定是同一个错，只是把账单和封号风险抬高。
#
# 分两级的理由：代理请求本身也可能超时（日志里 43 次代理尝试有 3 次拿 000），
# 所以只有「拦的就是 IP」这一类值得抢在其余处置之前；原因不明的那些放到
# 最后补一次，前提是别的路都走完了。
_PROXY_FIRST = frozenset({
    "IP封",     # CF 挑战 / 边缘按 IP 声誉拦 —— 换 IP 是对症处置
    "边缘",     # 403 空正文，CF 概率性拦截
})
_PROXY_SECOND = frozenset({
    "未知",     # 定不了性，换条链路是最便宜的一次排除
    "临时",     # 5xx。站方过载与中间链路故障从正文分不出来
})

# 明确**不**试代理的类别，连同理由。这个表不参与判断（判断只看上面两个
# 集合），存在的意义是让「为什么不试」可追溯 —— 否则下一个人只会看到
# 一个不含它们的白名单，无从判断是有意省略还是漏了。
_PROXY_POINTLESS = {
    "WAF": "认客户端形态而非 IP。实测某站三段都配了 mihomo，仍返回同一个拦截页",
    "余额": "充值即自愈，与出口 IP 无关",
    "鉴权": "401 是凭据本身的问题",
    "死路": "分组里没有这个渠道，换 IP 不会变出来",
    "客户端": "要的是请求画像（UA / beta 头），不是 IP",
    "时段": "等窗口，换 IP 不改变站方的时间判断",
    "限流": "429，CPA 自带冷却轮换",
    "限频": "探测节奏问题，加大 gap 而非换 IP",
    "反测活": "探测文本触发的，换文本而非换 IP",
    "换模": "站方确实回了响应，只是换了模型",
    "注入": "CPA 自注入的工具被拒，关开关而非换 IP",
    "门禁": "站方后台开关",
}


def _encode(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _body_kind(prof) -> str:
    """这一档的 body 补丁是什么形态。空串 = 不需要 body。

    只记形态名不记值：求值后的值含随机 UUID，落进报告会让每次输出都不同，
    没法用来做「上次是什么、这次变了没有」的漂移比较。
    """
    if not prof.body_patch:
        return ""
    uid = ""
    md = prof.body_patch.get("metadata")
    if isinstance(md, dict):
        uid = str(md.get("user_id") or "")
    kind = "metadata.user_id"
    if uid.startswith("{"):
        kind += "(json)"
    elif uid.startswith("user_"):
        kind += "(plain)"
    if "system" in prof.body_patch:
        kind += "+system"
    return kind


# 「这个模型没有」的说法，按语义收拢而不是按状态码。
#
# 2026-09-01 复盘 79 凭据实跑：上游中转站（new-api / one-api 系）用
# **503** 回 `No available channel for model X under group default`，
# 全场出现 175 次、直接判死 92 个段。这句话的语义与 404 `model_not_found`
# 完全相同 —— 「你要的这个模型，这个分组里没有」，是**模型专属**的。
# 原来只豁免 404，于是 503 这条路整段收敛，45/79 个凭据判 0 段可用，
# 而其中 27 个日志里其实出现过 200、7 个 /models 目录明明拿到过模型。
#
# 这句话确实来自上游而非 CPA：CLIProxyAPI 全仓库搜 "No available channel"
# 零命中，它自己的措辞是 conductor_selection.go:496 的 auth_unavailable。
# 与 classify 的「分组无该模型渠道」规则**同一份正文判据**（2026-09-06）。
# 原来这里手写了一份平行的措辞表，与 classify.py:130 那条渐渐分叉：
# 「分组无该模型渠道」「当前分组下无此模型的渠道」这两种正文 classify 认、
# 这里不认，于是 `_model_specific_dead_end` 返回 False → `_stage1` 判整段死。
# 实测那正是全量重探日志里最常见的一句（78 卡片中 23 个全灭段报的就是它），
# 换个模型本来可能就通。措辞表只留一处，从 classify 导入，杜绝再次分叉。
_MODEL_SPECIFIC_DEAD_END = re.compile(
    _MODEL_CHANNEL_BODY
    + r"|model .{0,80}(?:not (?:supported|found)|does not exist)"
    r"|不支持所选模型|模型不存在",
    re.I,
)

# 只在这些码上认「模型专属」。200 不该走到这里。
#
# 5xx 全收（2026-09-05 修）
# ----------------------
# 原来只认 503，注释说「500/502/504 是站方故障，已归临时并会重试」——
# **那句话不成立**：classify 的正文优先规则让
# `No available channel for model X under group default` 在**任何**状态码上
# 都判「死路」（classify.py 的正文关键词先于状态码兜底）。实测：
#
#     400 死路 model_specific=True    ← 豁免，会试下一个种子
#     403 死路 model_specific=True    ← 同上
#     404 死路 model_specific=True    ← 同上
#     500 死路 model_specific=False   ← **立刻判死整段**
#     502 死路 model_specific=False   ← 同上
#     503 死路 model_specific=True    ← 豁免
#     504 死路 model_specific=False   ← 同上
#
# 于是同一句话、同一个语义（「这个分组里没有你要的这个模型」），只因为中转站
# 用 500 而不是 503 发出来，就让 `_stage1` 在第一个种子上直接 return ——
# 连第二个种子、画像梯、代理都不试。
#
# 而中转站在过载时用 500/502 回这句话是常见形态：项目自己的复盘说 666 次 500
# 是日志最大头。
#
# 状态码本身不带语义（这句措辞来自上游中转站而非 CPA，全仓搜零命中，见
# `_MODEL_SPECIFIC_DEAD_END` 的说明），所以不该用它当豁免闸。5xx 一律放行 ——
# 真是站方故障的话正文里不会有这句话，`classify` 会判「临时」而不是「死路」，
# 走不到这个函数。
_MODEL_SPECIFIC_CODES = frozenset({
    "400", "403", "404",
    "500", "502", "503", "504",
})


# 探测发的是**字符**，`max-context-length` 要的是 **token**（CPA 直接把它
# 当 context_window 报给客户端，见 `_bisect` 的 docstring）。两者差一个系数。
#
# 取 4：探测正文是 `"x" * n`，ASCII 单字符串。GPT/Claude 系 BPE 对这种重复
# ASCII 的压缩率高于 4（`xxxx…` 会被合并成长 token），所以按 4 折算是
# **保守**方向 —— 报出的窗口不大于真实窗口。而这个值的用途是让客户端定压缩点，
# 宁小勿大：小了只是早压缩一点，大了就是请求直接被上游截断（那条 400）。
#
# 不做 tokenizer 精算的理由：真实窗口取决于站方后端用哪个 tokenizer，
# 探测无从得知；而任何 3-4 之间的系数都落在「保守」这一侧。
_CHARS_PER_TOKEN = 4


def _chars_to_tokens(chars: int) -> int:
    """字符数 → token 数（保守折算）。见 `_CHARS_PER_TOKEN` 的说明。"""
    return max(1, int(chars) // _CHARS_PER_TOKEN)


def _model_specific_dead_end(att) -> bool:
    """这个「死路」是不是只针对**当前这个模型**，换个模型可能就通了。

    为什么要区分（2026-08-31 实测）：404 `model_not_found` 说的是「这个分组
    里没有这个模型」，而 SEED_MODELS 里的模型是本工具写死的猜测 —— 它不存在
    完全不能说明这个站不可用。实测某站 claude-sonnet-5 返回 404，整段被判死，
    而该站的 claude-opus-5 是可用的。

    与之相对，敏感词拦截、Key 分组不匹配、路径不存在这几种「死路」与模型
    无关，换模型也救不了，那种要立即收敛以省请求数。

    判据按**正文语义**而不是状态码（2026-09-01 修正，见上方常量的说明）：
    同一句「没有这个模型的渠道」，不同中转站分别用 400 / 403 / 404 / 503
    发出来，只认 404 会漏掉最常见的那一种。
    """
    if str(att.status) not in _MODEL_SPECIFIC_CODES:
        return False
    return bool(_MODEL_SPECIFIC_DEAD_END.search(att.excerpt or ""))


def model_allowed(name: str) -> bool:
    """按用户规则过滤模型名：只留 gemini / gpt / claude / kimi 四类。

    o1 / o3-mini 这类 OpenAI 推理系列也放行 —— 它们属于规则里的「gpt 那一类」，
    只是 OpenAI 换了命名，2026-08-31 实测被前缀白名单漏掉了。

    kimi 是 2026-09-02 新增：用户把它列进 compat 段的允许清单，而 CPA 的权威
    名录里确实有 kimi provider（kimi-k2 … kimi-k3-256k）。它只在 compat 段
    有意义（前三段的段族闸会拦住它），这里放行、由 `model_fits_section` 定段。

    仍然有意排除：deepseek / grok / qwen / glm / llama。

    判据委托给 model_catalog.family —— 与段级规则同一处定义。
    """
    return model_catalog.family(name) in model_catalog.FAMILIES


# 截断反推出的容量低于这个数就**不采信**（2026-09-02 实测发现）。
#
# 现场：一个 compat 站三个条目都拿到 `max-context-length: 10`。CPA 把它直接
# 当 context_window / max_context_window 报给客户端
# （internal/client/codex/models/models.go:206-211），10 个 token 的窗口 =
# 那个站的每一次请求都立刻超限。而这个值是 `tok < chars * 0.5` 判成「截断」
# 后当作真实容量返回的 —— 上游回 `input_tokens: 10`（它根本没统计，或统计的
# 是别的东西）时，10 就成了「实测容量」。
#
# 取 8000：比任何真实模型的窗口都小一个数量级（现役最小的也有 200k），
# 所以不会误伤真实的小窗口站；又足够大到挡住「上游 token 计数不可信」这类。
# 低于它就当「测不出」—— 缺这个字段 CPA 会回落内置目录值
# （service_models.go 各段的 fallback），比写一个荒谬的数安全得多。
_MIN_TRUSTED_CONTEXT = 8_000


@dataclass
class Attempt:
    """一次探测的完整记录。保留正文摘要 —— 判定依赖正文而非状态码。"""

    section: str
    model: str
    combo: str
    status: str
    category: str
    action: str
    elapsed_ms: int
    proxy: str | None = None
    resp_model: str | None = None
    resp_id: str | None = None
    backend: str = ""
    input_tokens: int | None = None
    excerpt: str = ""
    sent_chars: int = 0
    # 200 但正文是错误体。见 _accept 的说明 —— 这是实测过的假阳性来源，
    # 而 status == "200" 单独看不出来，所以在 _call 里当场判好存下来。
    error_envelope: bool = False
    response_valid: bool = False
    validation_reason: str = ""
    successful_base_url: str = ""
    identity_verified: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "200" and self.response_valid and not self.error_envelope

    def as_sample(self) -> dict:
        """转成 fingerprint.swap_rate 需要的形状。"""
        return {
            "status": self.status if self.ok else "000",
            "requested": self.model,
            "actual": self.resp_model,
            "backend": self.backend,
            "input_tokens": self.input_tokens,
        }


@dataclass
class SectionVerdict:
    """一个候选在一个段上的结论。直接决定要不要写进这一段、带什么字段。"""

    section: str
    base_url: str = ""
    usable: bool = False
    models: list[str] = field(default_factory=list)
    need_proxy: bool = False
    min_headers: dict[str, str] = field(default_factory=dict)
    # 通过时用的画像档名（profiles.Profile.name）。写回与报告都要它 ——
    # 「需要 cc-std」比「需要 3 个头」对人有用得多。
    profile_name: str = ""
    # 该档的 body 补丁形态（只记有没有、是哪种，不记求值后的值 —— 那含
    # 随机 UUID，落进报告会让每次输出都不同）。非空表示 headers 表达不了，
    # claude 段要 fingerprint-profile，其余三段配置层无解。
    min_body_kind: str = ""
    # 分组的可调用时段（("09:00","18:00")）。「时段」类才有。
    time_window: tuple[str, str] | None = None
    swap: dict = field(default_factory=dict)
    max_context_length: int | None = None
    # 上限是在**哪个模型**上实测的。同站不同模型窗口不同（claude-opus-5
    # 与 haiku 差一个数量级），把 A 的实测值写到 B 上等于伪造数据 ——
    # 客户端会按错误的窗口定压缩点，重演那条 400。
    context_model: str = ""
    context_untrusted: bool = False
    # ---- 段专属能力开关的实测结论（2026-09-04）----
    #
    # 三态，不是布尔：
    #   True  实测确认支持 —— 写 `<字段>: true`
    #   False 实测确认不支持 —— **不写这个字段**（CPA 的零值就是关闭）
    #   None  没探（不适用 / 本段不通 / 需代理时直连探不准）—— 也不写，
    #         但方案里要说明「未探测」而不是「不支持」
    #
    # 为什么 False 与 None 都不写却要分开记：写回时行为相同，但**界面措辞
    # 与警告不同**。把「探过、站方明确拒绝」和「没探过」显示成同一个样子，
    # 就是本项目反复修的那类缺陷（「未验证当已验证」的镜像）。
    #
    # websockets（codex 段）：CPA 的 CodexAutoExecutor 只在
    # 「下游是 WS」且「该凭据 websockets=true」时才走 WS 通道
    # （codex_websockets_executor.go:71）；上游地址是
    # `{base}/responses` 的 http→ws 换 scheme（同文件 :223-240）。
    # 站方不支持时 CPA **不会自动回落**到 HTTP —— 那一条会直接失败，
    # 所以这个开关必须靠实测决定，不能照抄别的条目。
    websockets: bool | None = None
    websockets_note: str = ""
    # support-prompt-cache-key（compat 段）：CPA 会在请求体注入
    # `prompt_cache_key`（openai_compat_executor.go:875）。上游不认这个字段
    # 时的表现分两种 —— 忽略（无害）或 400 拒收（有害），所以要实测。
    prompt_cache_key: bool | None = None
    prompt_cache_note: str = ""
    # claude 段：对话中途的 system 消息要不要让 CPA 挪到顶层。
    # None = 未探测 / 未判定；False = 上游自己就收；True = 需要 CPA 代为重建。
    rebuild_mid_system: bool | None = None
    rebuild_mid_system_note: str = ""
    category: str = ""
    action: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    # 站方 /models 目录声明的模型（白名单过滤后）。`_stage0_catalog` 填。
    #
    # 与 `models` 的区别是**声明**与**实测**：这里是站方说它有什么，`models`
    # 是本工具实际验证通过的。两者的差集很有价值 —— 声明有却验不过的模型，
    # 正是 CPAMP 面板「模型」列会显示、而真实转发会失败的那批（那一列读的
    # 是 config.yaml 的 models 字段长度，rowData.ts:78-79，不做可用性校验）。
    #
    # 判死的段也要留着它：操作员人工接管时，这是唯一可选的候选清单。
    catalog: list[str] = field(default_factory=list)
    successful_base_url: str = ""
    successful_proxy_url: str | None = None
    profile_id: str = ""
    unverified_models: list[str] = field(default_factory=list)
    identity_verified_models: list[str] = field(default_factory=list)
    budget_exhausted: bool = False
    source_snapshot_id: str = ""

    @property
    def need_ua(self) -> bool:
        return bool(self.min_headers)

    @property
    def swap_detected(self) -> bool:
        return bool(self.swap.get("swap"))

    def summary(self) -> str:
        if not self.usable:
            base = f"{self.category or '不可用'} — {self.action}"
            if self.time_window:
                base += f"（可调用时段 {self.time_window[0]}~{self.time_window[1]}）"
            return base
        bits = [f"{len(self.models)} 模型"]
        if self.need_proxy:
            bits.append("需代理")
        if self.profile_name:
            bits.append(f"需画像 {self.profile_name}")
        elif self.min_headers:
            bits.append("需 " + "+".join(self.min_headers))
        if self.min_body_kind:
            bits.append(f"需 {self.min_body_kind}")
        if self.swap_detected:
            bits.append(f"⚠ 换模 {self.swap.get('rate_pct', 0)}%")
        if self.max_context_length:
            bits.append(f"上限 {self.max_context_length:,}")
        # 能力开关只在**确认支持**时进摘要 —— 「不支持」是常态（中转站多数
        # 不支持 WS），把它也列出来会让摘要长一倍而没有信息量。
        if self.websockets:
            bits.append("支持 WebSocket")
        if self.prompt_cache_key:
            bits.append("支持 prompt_cache_key")
        return " · ".join(bits)


@dataclass
class CandidateResult:
    row: ParsedRow
    sections: dict[str, SectionVerdict] = field(default_factory=dict)

    @property
    def usable_sections(self) -> list[str]:
        return [s for s, v in self.sections.items() if v.usable]

    @property
    def total_calls(self) -> int:
        return sum(len(v.attempts) for v in self.sections.values())


class Prober:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        gap: float = 3.0,
        timeout: int = 120,
        probe_context: bool = True,
        probe_capabilities: bool = True,
        swap_samples: int = 3,
        workers: int = 4,
        max_models: int = MAX_MODELS_PER_SECTION,
        max_model_attempts: int = MAX_MODEL_ATTEMPTS_PER_SECTION,
        reuse_profile_verdict: bool = True,
        on_event: Callable[[str, dict], None] | None = None,
        cfg_snapshot: dict | None = None,
    ):
        # 现有 config.yaml 的快照。画像梯从它的 `claude-header-defaults` /
        # `codex.header-defaults` 派生真实的 UA 版本号与 X-Stainless 值
        # （profiles.defaults_from_config）。给 None 时回落内置常量 ——
        # 那些常量是从 CPA 源码抄录的，不是猜的，所以缺配置也能工作。
        self.cfg_snapshot = copy.deepcopy(cfg_snapshot or {})
        from .cpa_source_probe import cached_identity
        self.source_identity = copy.deepcopy(cached_identity(proxy=proxy))
        self.session_id = uuid.uuid4().hex
        self.client_headers = dict(self.cfg_snapshot.get("probe-client-headers") or {})
        self._cancelled = threading.Event()
        self.proxy = proxy
        self.gap = gap
        self.timeout = timeout
        self.probe_context = probe_context
        # 段专属能力开关的实测（codex 的 websockets、compat 的
        # support-prompt-cache-key）。每段最多 1 次额外请求，只在该段已判可用
        # 时才跑 —— 段不通时这两个开关无从验证，探它只是白烧配额。
        #
        # 留开关的理由与 probe_context 相同：这两次请求对「站能不能用」这个
        # 主问题没有贡献，赶时间或省配额时可以关掉。关掉时字段记 None
        # （未探测），写回时不写那个字段，界面说明「未探测」而不是「不支持」。
        self.probe_capabilities = probe_capabilities
        self.swap_samples = swap_samples
        # 每段收几个模型、最多试几次。做成参数是因为这两个数的取舍与
        # 具体站群有关：聚合站多时该压低尝试数，站少而模型杂时该放宽。
        self.max_models = max(1, int(max_models))
        self.max_model_attempts = max(self.max_models, int(max_model_attempts))
        # 同段整梯全败后，后续种子是否跳过画像梯。默认开 —— 门票是站+段的
        # 属性，与模型无关（见 _profiles_failed）。留开关是为了万一遇到
        # 「同一段不同模型走不同门禁」的站，能一键回到旧行为自证。
        self.reuse_profile_verdict = reuse_profile_verdict
        # 四段并行度。1 = 完全串行（老行为，出问题时的退路）。
        # 上限就是 4 —— 段数固定，再高没有意义。
        self.workers = max(1, min(int(workers), len(SECTIONS)))
        self.on_event = on_event or (lambda kind, data: None)
        # 节流按 **host** 分开记。原来是全局一个时间戳，探 A 站要等 B 站的
        # gap —— 不同站之间没有任何理由互相等。反测活是站方行为，只对同站生效。
        self._last_call: dict[tuple[str, str], float] = {}
        # 从限频正文里学到的「该站最小探测间隔」。见 _note_rate_limit。
        # 命中后该站整站共用一个 gap 桶（不再按段分），因为那类 guard
        # 通常按账号/IP 全局计数，按段分桶会让瞬时并发变成 4 倍。
        self._host_gap: dict[str, float] = {}
        # (host, section) -> 已学到的段形态。同一主机的第 2..N 个 Key 直接复用，
        # 只补一次凭证确认。见 _reuse_shape 的说明。
        self._shape: dict[tuple[str, str], SectionVerdict] = {}
        # (host, section) -> 门闩。表示「已有线程在学这个段的形态」。
        # 同主机多 Key 并发时防止重复做那 12 次昂贵探测。见 _probe_one_section。
        self._inflight: dict[tuple[str, str], threading.Event] = {}
        # (host, section) -> 整梯跑完全败。**门票是站+段的属性，与用哪个种子
        # 模型问它无关** —— 站方查的是 headers 与 body 形态，不看模型名。
        #
        # 为什么值得单独记（2026-09-01 量化）：_try_profiles 的调用点在
        # _stage1 的种子循环内部，返回 False 后循环走到下一个种子，整梯重跑。
        # 而每段有 2-3 个种子，claude 段 7 档 × 2 = 14 次、compat 6 档 × 3 = 18 次。
        # 四段全不通的站现状 48 次画像请求，其中 27 次是重复问同一个问题。
        #
        # 只缓存**失败**：成功的形态已经由 _shape 记着，而且成功时 _stage1
        # 直接 return，不会走到下一个种子。
        self._profiles_failed: set[tuple[str, str]] = set()
        # (host, section) -> 站+段级的失败结论。**与用哪把 Key 无关**的那一类。
        #
        # 为什么需要（2026-09-05 修）：`_shape` 只在 `v.usable` 时写入，
        # 于是段不通时形态不入缓存 —— 门闩清空、gate 置位、等待的线程醒来，
        # 其中一个重新认领又跑一遍完整 `_full_probe`（目录 + 基线 + 整梯画像
        # + 临时重试）。15 个 Key 挂同一主机就是 15 次全量探测，而且因为门闩
        # 存在这 15 次是**严格串行**的 —— 比没有门闩（至少能并行）更慢。
        #
        # 而这是多数情形而非边角：79 凭据实跑里 45 个是 0 段可用。
        #
        # 只缓存**站+段级**的失败（见 _HOST_LEVEL_FAIL）。凭证级的
        # （鉴权 / 余额）是这把 Key 自己的属性，缓存它会让同站的其他 Key
        # 错误地继承别人的欠费结论 —— 那正是原来「只在 usable 时缓存」
        # 想避免的事，但它把两类一起排除了。
        self._dead_shape: dict[tuple[str, str], SectionVerdict] = {}
        self._lock = threading.RLock()
        self._proxy_state: bool | None = None   # None=未检 True/False=预检结果
        # 代理预检专用锁。不复用 _lock —— 预检要占最多 4 秒，
        # 而 _lock 同时保护节流计时与形态缓存，不能被长时间持有。
        self._proxy_lock = threading.Lock()

    # ---------- 底层 ----------

    def cancel(self) -> None:
        self._cancelled.set()

    def _check_cancel(self) -> None:
        if self._cancelled.is_set():
            raise concurrent.futures.CancelledError()

    def _wait(self, seconds: float) -> None:
        if self._cancelled.wait(max(0, seconds)):
            self._check_cancel()

    def _entry(self, section: str, base: str, key: str) -> dict:
        """Exact path and credential lookup; never infer a sibling channel."""
        target = base_for_section(base, section, declared_base=True)
        for entry in self.cfg_snapshot.get(section) or []:
            if not isinstance(entry, dict) or not entry.get("base-url"):
                continue
            if base_for_section(entry["base-url"], section, declared_base=True) != target:
                continue
            if section == "openai-compatibility":
                for item in entry.get("api-key-entries") or []:
                    if isinstance(item, dict) and item.get("api-key") == key:
                        return {**entry, **item}
            elif entry.get("api-key") == key:
                return dict(entry)
        return {}

    def _base(self, row: ParsedRow, section: str) -> str:
        entry = self._entry(section, row.bare, row.api_key)
        return base_for_section(entry.get("base-url") or row.bare, section,
                                declared_base=bool(entry))

    def _evidence_key(self, section: str, base: str, key: str):
        entry = self._entry(section, base, key)
        context = json.dumps([key, entry, self.cfg_snapshot, self.proxy,
                              self.client_headers, self.session_id],
                             sort_keys=True, default=str)
        return (base_for_section(base, section, declared_base=True), section,
                hashlib.sha256(context.encode()).hexdigest())

    def _send(self, url: str, **kwargs):
        host = host_of(url)
        with HOST_LIMITER.slot(host, max(self.gap, self._host_gap.get(host, 0)),
                               self._check_cancel):
            return client.send(url, **kwargs)

    @property
    def live_proxy(self) -> str | None:
        """代理地址，仅在预检通过时返回；不通则返回 None。

        为什么不直接用 self.proxy：`via-proxy` 是 IP封/边缘 的首选处置，
        每段每模型都会试一次。代理不通时每次都干等满 timeout（默认 120 秒）
        才失败 —— 实测日志里 mihomo:7890 早已不通（preflight 也报了警），
        5 个 key 多段累计十几分钟纯粹白等，且结果全是无用的 `000 未知`。

        预检只做一次 TCP 握手，4 秒封顶，结果缓存在 _proxy_state。
        """
        if not self.proxy:
            return None
        if self._proxy_state is None:
            # 必须在专用锁下做，且锁内再判一次 —— 四段并行时会同时看到
            # _proxy_state is None，无锁的话每段各做一次 TCP 预检（实测被
            # 测试抓到：proxy-precheck 事件发了 4 次）。
            #
            # 用独立的 _proxy_lock 而不是 self._lock：probe_proxy 要占用最多
            # 4 秒，而 _lock 同时保护 _throttle 与 _shape —— 拿着它睡 4 秒
            # 会把另外三段的节流计算一起堵住。
            with self._proxy_lock:
                if self._proxy_state is None:
                    ok, detail = client.probe_proxy(self.proxy, timeout=4)
                    self.on_event("proxy-precheck", {"proxy": self.proxy, "ok": ok,
                                                     "detail": detail})
                    self._proxy_state = ok
        return self.proxy if self._proxy_state else None

    def _throttle(self, host: str = "", section: str = "") -> None:
        """按 (host, section) 保持 gap 秒。不同主机、不同段互不等待。

        为什么按 host 而不是全局：gap 存在的理由是站方的 bulk probe guard
        （relay-b 实测低于 3 秒会触发），那是**站方**的限频。探 A 站时为 B 站
        的上一次请求等待纯属浪费 —— 多主机批量导入时这笔账按主机数翻倍。

        为什么再按 section 细分（2026-08-30 加）：guard 是**按端点**计的。
        四段打的是四个不同路径：
            gemini  /v1beta/models/{model}:generateContent
            codex   /v1/responses
            claude  /v1/messages
            compat  /v1/chat/completions
        它们共享一个 gap 桶时，单站四段的 56 次请求要串成 55 x 3s = 165 秒
        纯睡 —— 这是「探测要十几分钟」最大的一笔。拆开后四段各自计时，
        同段内仍严格保持 gap（guard 该防的东西一点没松）。

        风险与取舍：如果某站的 guard 是按账号（而非端点）全局计的，拆开后
        瞬时并发会是原来的 4 倍。这就是 --gap 仍然存在、且默认保持 3 秒的
        原因 —— 撞上那种站把 gap 调大即可，不需要改回全局串行。
        """
        # 用元组当键，不用字符串拼接 —— 裸拼接会有歧义碰撞：
        # ("a", "b|c") 与 ("a|b", "c") 拼出同一个 "a|b|c"，两者会共享
        # 同一个 gap 桶（测试抓到过）。host 来自用户输入的 URL，
        # section 虽然是内部常量，也没有理由留这个坑。
        self._check_cancel()
        bucket = (host, section)
        with self._lock:
            # 站方自报的节奏阈值优先（见 _note_rate_limit）。它是从 429/403
            # 正文里读出来的实测值，比命令行的 --gap 准 —— 而且撞上 guard 的
            # 那个站往往是**按账号全局**计数，此时按 (host, section) 分桶反而
            # 让瞬时并发变成 4 倍。命中后整站共用一个桶。
            forced = self._host_gap.get(host)
            if forced is not None:
                bucket = (host, "")
                gap = max(self.gap, forced)
            else:
                gap = self.gap
            last = self._last_call.get(bucket, 0.0)
            wait = gap - (time.monotonic() - last)
            if wait > 0:
                # 记成「即将发出」，避免同桶并发时多个线程一起放行
                self._last_call[bucket] = time.monotonic() + wait
            else:
                self._last_call[bucket] = time.monotonic()
        if wait > 0:
            self._wait(wait)

    # 站方自报的探测节奏阈值。形如
    #   bulk probe guard: ip 1.2.3.4 requested 4 distinct models in 60s
    # 两个数都要：N 个模型 / M 秒 —— 平均间隔 M/N 才是它真正的阈值。
    _RATE_HINT = re.compile(
        r"requested\s+(\d+)\s+distinct\s+models?\s+in\s+(\d+)\s*s", re.I)

    # 阈值上限。站方偶尔报出荒谬的窗口（见过 3600s），照抄会让整批探测卡死。
    # 60 秒/次已经足够慢，再大不如让用户看到警告后自己决定。
    _MAX_LEARNED_GAP = 60.0

    def _note_rate_limit(self, host: str, excerpt: str) -> bool:
        """从限频正文里学出该站的探测节奏，返回是否学到了新值。

        为什么必须自动学（2026-09-02 现场）：一个站在 79 凭据那轮里 46 次
        撞上 `bulk probe guard`，判定「限频」→ 处置写着「加大探测间隔重试」
        → 然后**什么都没做**，接着用同样的节奏打下一个模型，于是 46 次全撞。

        那句正文里带着确切阈值（4 个模型 / 60 秒），工具读得出来却没用上，
        等于让用户去看日志、猜一个 --gap、再重跑十几分钟。

        为什么换代理不是解法：站方数的是「**这个 IP** 在 60 秒里问了几个
        不同模型」。换到代理出口后同样的节奏立刻又触发一次，只是白烧一次
        请求并把代理 IP 也搭进去。见 _PROXY_POINTLESS 里「限频」那条。
        """
        m = self._RATE_HINT.search(excerpt or "")
        if not m:
            return False
        try:
            n_models, window = int(m.group(1)), int(m.group(2))
        except ValueError:
            return False
        if n_models <= 0 or window <= 0:
            return False
        # 平均间隔 + 10% 余量。站方计的是滑动窗口，贴着阈值走仍会偶发命中。
        learned = min(window / n_models * 1.1, self._MAX_LEARNED_GAP)
        with self._lock:
            prev = self._host_gap.get(host)
            if prev is not None and prev >= learned:
                return False
            self._host_gap[host] = learned
        self.on_event("rate-limit-learned", {
            "host": host, "models": n_models, "window": window,
            "gap": round(learned, 1), "was": round(prev or self.gap, 1),
        })
        return True

    def _call(
        self,
        section: str,
        base: str,
        key: str,
        model: str,
        *,
        combo: str,
        extra_headers: dict[str, str] | None = None,
        body_patch: dict | None = None,
        proxy: str | None = None,
        text: str | None = None,
    ) -> Attempt:
        # 按 (host, section) 节流：同站同段保持 gap，其余互不等待
        self._throttle(host_of(base), section)
        entry = self._entry(section, base, key)
        if entry:
            base = entry["base-url"]
        proxy = proxy or entry.get("proxy-url") or None
        # 这一段 CPA 转发时强不强制流式 —— **从源码读**，不写死段名
        # （docx 第 6 条：严禁硬编码；2026-09-12 接上）。
        # -----------------------------------------------------------------
        # 探测要问的是「CPA 这样发通不通」，所以形态必须与 CPA 实际转发的
        # 一致。原来这里写的是 `section == "codex-api-key"`，而
        # cpa_source_probe 自己解析出来的表里 **gemini 段也强制 stream=true**
        # （那个模块 parse_body_shape 的文档就写着「gemini 强制 stream=true；
        # 删 session_id」）。两处不一致的后果正是用户 2026-09-12 报的
        # 「空 HTTP 200 + 0 个 SSE 事件」的探测侧盲点：非流式 JSON 探通了，
        # 而 CPA 走流式，站方流式路径一个事件都不吐 —— 工具判它可用。
        #
        # `forces_stream` 读不出结论时（源码拉不到是常态）回落到 codex 段，
        # 也就是原来的行为：宁可保持既有形态，不因为拉不到源码而突然换一种。
        from .cpa_source_probe import forces_stream as _forces_stream
        forced = _forces_stream(self.source_identity, section)
        if forced is None:
            forced = section == "codex-api-key"
        stream = forced or bool((body_patch or {}).get("stream"))
        kwargs = {"extra_headers": extra_headers, "entry_config": entry,
                  "cfg": self.cfg_snapshot, "source_identity": self.source_identity,
                  "declared_base": True, "client_headers": self.client_headers,
                  "session_id": self.session_id, "stream": stream}
        if text is not None:
            kwargs["text"] = text
        url, headers, body = request.build_request(section, base, model, key, **kwargs)
        if body_patch:
            # 画像的 body 补丁（metadata.user_id 等）。浅层 merge 就够 ——
            # 补丁只碰顶层键，而顶层同名键就该整体替换（metadata 整个替换，
            # 不是与探测自己的 metadata 合并 —— 探测本来不发 metadata）。
            body.update(body_patch)
        if section == "codex-api-key":
            body["stream"] = True
        resp = self._send(
            url,
            headers=headers,
            body=_encode(body),
            proxy=proxy,
            timeout=self.timeout,
        )
        category, action = _classify(resp.status, resp.body)
        valid, reason = validate_success(section, resp.status, resp.body,
                                         error=resp.error, require_stream=stream)
        if str(resp.status) == "200":
            category, action = ("可用", reason) if valid else ("未知", reason)
            if not valid:
                # 在**拒收发生的地方**报出来（2026-09-12）
                # ------------------------------------------
                # `model-rejected` 原来只在 `_accept` 里发，而 `_accept`
                # 只有段先通过基线才会被调到。站方四段全回「200 + 错误体」
                # 时段在基线就判否，于是一个 model-rejected 都没有 ——
                # 界面只说「未知」，不说到底是正文是错误体、还是该发流没发流。
                # 那正是第 1/2 条要查的东西（直连能用、进 CPA 不能用），
                # 把原因藏起来等于让人没法排查。
                self.on_event("model-rejected", {
                    "section": section,
                    "host": host_of(base),
                    "requested": model,
                    "actual": None,
                    "reason": _GATE_REASON_CN.get(reason, f"200 但{reason}"),
                })
        rid = fingerprint.resp_id(resp.body)
        # 探测文本按 Key 派生（request.probe_text_for），不再是唯一那句 ——
        # 所以「没显式传 text」时要问同一个派生函数，不能拿 PROBE_TEXT 的
        # 长度顶替，否则 sent_chars 会与实际发出去的长度对不上，
        # 而上下文上限的推算正是拿它做基线的。
        sent = (len(text) if text is not None
                else len(request.probe_text_for(key)))
        att = Attempt(
            section=section,
            model=model,
            combo=combo,
            status=resp.status,
            category=category,
            action=action,
            elapsed_ms=resp.elapsed_ms,
            proxy=proxy,
            resp_model=fingerprint.resp_model(resp.body),
            resp_id=rid,
            backend=fingerprint.backend_of(rid),
            input_tokens=fingerprint.input_tokens(resp.body),
            # 200 通常不记正文（省内存），但**整页 HTML 的 200 要记** ——
            # 那是维护页/拦截页，不记的话界面上只剩一个「200」，
            # 没人看得出它为什么被判成临时（2026-09-11）。
            excerpt=("" if valid
                     else _body_excerpt(resp.body)),
            sent_chars=sent,
            # 「200 但正文不是 API 响应」的两种载体合并在这一个字段里：
            #   · JSON 错误信封 —— 站方把错误放进 200 的正文
            #   · 整页 HTML     —— 维护页 / WAF 拦截页 / nginx 错误页
            # 合并而不是各判各的：下游有四个消费点（_accept、诊断梯子、
            # 事件流、报告），任何一处漏判都会让死站带着模型进 config.yaml。
            error_envelope=(_has_error_envelope(resp.body)
                            or _looks_like_html(resp.body)),
            response_valid=valid, validation_reason=reason,
            successful_base_url=base_for_section(base, section, declared_base=True) if valid else "",
            identity_verified=valid and bool(fingerprint.resp_model(resp.body))
                and fingerprint.model_matches(model, fingerprint.resp_model(resp.body)),
        )
        self.on_event(
            "attempt",
            {
                "section": section,
                # host 必须带 —— 79 个站并发，日志是交织的流。不带归属时
                # 「某站的行」和「别站的行」混在一起，看着像这个站没跑完。
                "host": host_of(base),
                "model": model,
                "combo": combo,
                "status": resp.status,
                "category": category,
                "elapsed_ms": resp.elapsed_ms,
            },
        )
        # 限频：从正文里学出该站的探测节奏，下一次请求起自动放慢。
        # 放在事件之后 —— 学习本身会发自己的事件，顺序上「先看到撞了什么、
        # 再看到学到了多少」更好读。
        if category == "限频":
            self._note_rate_limit(host_of(base), att.excerpt)
        return att

    # ---------- ① 段归属 + ③ 处置 ----------

    def _accept(self, v: SectionVerdict, model: str, att: Attempt) -> list[str]:
        """200 之后再判「回的是不是我要的模型」，判过才收进清单。

        为什么必须有这一步：`_stage2` 对目录里的模型做了 `model_matches`
        校验，而 `_stage1` 原先对种子模型直接 `v.models = [model]` ——
        等于换模站的第一个模型拿到免检通行证。relay-e 那种「请求
        gpt-5.6-sol 回 agnes-2.0-flash」的站会被判成可用并写进 config.yaml，
        照常计费却拿不到要的模型，比不可用更危险。

        段仍算 usable（端点确实响应 200，凭证本身有效），但模型不进清单。
        models 为空 → `SectionPlan.writable` 为 False，不会写入。

        为什么还要判「200 包错误体」（2026-08-31 实测的假阳性）
        ------------------------------------------------------
        `Attempt.ok` 只看 `status == "200"`，而 `model_matches` 在
        `resp_model` 为 None 时按设计返回 True（无证据不判换模）。两者叠加，
        「HTTP 200 + 正文是 {"error":...} + 没有 model 字段」这种站一路绿灯。
        实测复现：四段全部 usable=True、注册 11 个模型，而那个站**完全不能用**。

        这是**假阳性**，比判死更危险：死站带着模型进生产 config.yaml，每次轮到
        它就吃一次失败，耗尽 request-retry x max-retry-credentials 预算，
        最终客户端收到 500 —— 正是 tools/diag403.py 要诊断的那个症状。

        判据必须精准，不能简单地「200 且无 model 字段就拒」：
          · gemini 段用的是 `modelVersion` 而非 `model`，resp_model 已覆盖；
          · 有些站的合法响应确实不带 model 字段（流式首包、极简实现）。
        所以判的是「正文顶层有错误结构」而不是「缺 model 字段」：
        顶层 error / 顶层 "type":"error"，两者都是明确的错误信号。
        """
        if not att.ok:
            self.on_event("model-rejected", {
                "section": v.section,
                "host": host_of(v.base_url),
                "requested": model,
                "actual": None,
                "reason": "200 但正文是错误体",
            })
            return []
        v.successful_base_url = att.successful_base_url or v.base_url
        v.successful_proxy_url = att.proxy
        # need_proxy 只增不减（2026-09-12）
        # --------------------------------
        # 它的含义是「这个站**得**走代理才能用」，由处置梯给出结论：
        # baseline 失败、via-proxy 成功时置位（:1098 / :1208）。
        # 原来这里每收下一个模型就按**当次**的 att.proxy 整体覆盖，于是
        # 代理救回之后的 model-scan（直连即可成功，本来就不带代理）
        # 会把它打回 False —— 落盘时 proxy-url 丢掉，写出去的条目在生产
        # 环境直连不通，正是「导入后这个站用不了」的一种。
        # 拒收时的还原是另一条路径，仍由 :1220 显式负责。
        if att.proxy:
            v.need_proxy = True
        v.source_snapshot_id = self.source_identity.snapshot_id
        if fingerprint.model_matches(model, att.resp_model):
            if att.identity_verified and model not in v.identity_verified_models:
                v.identity_verified_models.append(model)
            return [model]
        self.on_event("model-rejected", {
            "section": v.section,
            "host": host_of(v.base_url),
            "requested": model,
            "actual": att.resp_model,
            "backend": att.backend,
        })
        return []

    # 「临时」类的重试：站方负载上限（503/502/504）不代表站点不可用。
    # 实测踩到：某站 claude-opus-5 首次 503，而第二个种子 claude-sonnet-5
    # 返回 404（该站根本没这个模型），整段被判死 —— 而那站其实可用。
    _TRANSIENT_RETRIES = 1
    _TRANSIENT_WAIT = 2.0

    # 基线阶段打几个模型。原来是 `SEED_MODELS[section]` 的全长（2-3 个），
    # 现在候选来自目录（可能几百个），必须显式限长 —— 基线的目的是定段归属
    # 与找最小门票，不是把目录验穷（那是 _stage2 的活）。
    #
    # 取 2：一个够定归属，两个能区分「站级不可用」与「这个模型不在」。
    _BASELINE_MODELS = 2

    # 模型专属死路（404 model_not_found 这类）最多额外补打几个候选。
    #
    # 它们不消耗 `_BASELINE_MODELS` 的额度 —— 那个额度是用来判「站行不行」的，
    # 而这类结果只说明「这个模型不在」。但也要有上限：一个站的目录可能有
    # 几百个名字而这个分组一个都没有，逐个打既慢又像扫描。
    # 取 3：加上原本的 2 个，最多 5 发就能覆盖「排在最前的几个恰好不在」
    # 这种现场形态；再多说明整份目录与这个分组确实无关，判死是对的。
    _BASELINE_SKIPS = 3

    # 目录最多翻几页。只有 gemini 的 /v1beta/models 分页，与 CPAMP 的
    # healthCheck.ts:279-364 取同一个上限，防异常站的无限 nextPageToken。
    _CATALOG_PAGES = 20

    # 判定的严重度排序。全部种子都失败时取**最严重**的那个，而不是最后一个。
    # 越靠前越严重。不在表里的类别按「未知」处理。
    # 越靠前越严重。全部种子失败时取**最严重**的那个。
    #
    # 2026-08-31 修正：原来「死路」排第一，于是一个该站根本不存在的模型
    # （SEED_MODELS 里写死的猜测）返回 404 model_not_found 时，会盖掉
    # 另一个种子的真实结论。实测踩到：opus-5 返回 503（客户端门禁）、
    # sonnet-5 返回 404，整段判「死路」—— 而真正该报的是「客户端」。
    #
    # 修法是两层：模型专属死路不进 seen（见 _stage1），这里再把「客户端」
    # 排在「死路」之前 —— 它更接近根因，且处置明确（补标识或人工接管）。
    # 「WAF」紧跟「客户端」：两者都是形态问题，且 WAF 的处置（试画像）与
    # 客户端同源，比「死路」更接近根因。
    # 「时段」排在最后段 —— 它 usable=True，且窗口内自然可用，是最不严重的
    # 一类失败。若排在前面，一个时段受限的站会盖掉另一个种子的真实故障。
    _SEVERITY = ("客户端", "WAF", "死路", "鉴权", "注入", "门禁", "IP封",
                 "未知", "临时", "限频", "限流", "边缘", "反测活", "余额", "时段")

    @classmethod
    def _severity_rank(cls, category: str) -> int:
        """越小越严重。不在表里的排在「未知」的位置。"""
        try:
            return cls._SEVERITY.index(category)
        except ValueError:
            return cls._SEVERITY.index("未知")

    def _stage1(self, row: ParsedRow, section: str) -> SectionVerdict:
        """基线不通 → 试代理 → 试补标识头。三步都按处置优先级排。

        为什么判定不能「后一个种子覆盖前一个」（2026-08-31 实测）
        ----------------------------------------------------
        原来每轮循环无条件 `v.category = att.category`，于是种子列表里
        **后面**那个模型的结论会盖掉前面的。实测后果：
            claude-opus-5   -> 503 临时（站方忙，本该重试）
            claude-sonnet-5 -> 404 死路（该站根本没这个模型）
        整段判「死路」并 usable=False —— 而 claude-sonnet-5 只是这里写死的
        第二个种子，它不存在完全不能说明这个站不可用。

        改成：全部种子失败后，取**最严重**的类别（见 _SEVERITY）。
        「死路」只有在真的是死路时才成立，不会由一个不相干的模型带来。

        探哪些模型（2026-09-01 改）
        ------------------------
        先 `_stage0_catalog` 问站方目录，用目录里真实存在的模型开打，种子只
        作兜底。原来直接拿写死的种子撞，撞不上就判死 —— 那是把「我猜的模型
        不在」当成了「这个站不能用」。
        """
        base = base_for_section(row.bare, section)
        v = SectionVerdict(section=section, base_url=base)
        seen: list[tuple[str, str]] = []      # 每个候选的 (类别, 处置)
        # 模型专属死路单独一张表（2026-09-05）。它们**不参与**「整段是什么
        # 状况」的评选，但在没有别的结论时仍要报出来 —— 否则段判不可用却
        # 没有类别，界面显示成空白。见下方 seen / seen_weak 的取值处。
        seen_weak: list[tuple[str, str]] = []

        # 目录优先。拿不到就是空列表，`_probe_order` 自动回落到种子。
        v.catalog = self._stage0_catalog(row, section, base)

        # 基线阶段只打前几个 —— 这里的目的是「定段归属 + 找最小门票」，
        # 不是把目录验穷。验穷是 _stage2 的活，且有 max_model_attempts 兜着。
        #
        # 但**模型专属的死路不消耗这个额度**（2026-09-12 补）
        # ------------------------------------------------
        # 取 2 的理由是「一个够定归属，两个能区分站级不可用与这个模型不在」。
        # 那个理由在两个候选**都**回 404 model_not_found 时正好不成立：
        # 两条证据都只说「这两个模型不在」，关于「这个站行不行」一个字都没说，
        # 而结论却落成「死路 — 分组无该模型渠道」，整段判死。
        #
        # 现场形态：站方目录报了几百个名字，而 `_probe_order` 排在最前的两个
        # 恰好是该分组没有的（目录是站级的，分组权限是 Key 级的）——
        # 一个完全可用的站因此消失。
        #
        # 判据复用 `_model_specific_dead_end`（下面 seen_weak 那一支用的同一个）
        # ——「这个拒绝只针对当前模型」。这类结果不计入额度，改打下一个候选；
        # 任何**与模型无关**的结论（门禁 / 余额 / IP封 / 分组无渠道）仍然
        # 立即收敛，一个都不多打。上限 `_BASELINE_SKIPS` 兜住「整份目录都
        # 不在这个分组里」的站，避免几百个候选逐个打。
        order_all = self._probe_order(section, v.catalog)
        probe_models = order_all[:self._BASELINE_MODELS]
        spare = order_all[self._BASELINE_MODELS:self._BASELINE_MODELS
                          + self._BASELINE_SKIPS]
        for model in probe_models:
            att = self._call(section, base, row.api_key, model, combo="baseline")
            v.attempts.append(att)

            # 临时错误（站方负载）重试 —— 不重试就会把「忙」当成「坏」。
            tries = 0
            while (att.category == "临时" and tries < self._TRANSIENT_RETRIES):
                tries += 1
                self.on_event("transient-retry", {
                    "section": section, "host": host_of(base), "model": model,
                    "status": att.status, "wait": self._TRANSIENT_WAIT,
                })
                time.sleep(self._TRANSIENT_WAIT)
                att = self._call(section, base, row.api_key, model,
                                 combo=f"retry{tries}")
                v.attempts.append(att)

            # 模型专属的死路**不进 seen**（2026-09-05 修）。
            #
            # 上面那段 docstring 已经写明「修法是两层：模型专属死路不进 seen，
            # 这里再把客户端排在死路之前」—— 但第一层从来没实现，这一行是
            # 无条件 append 的。
            #
            # 后果（实测）：「opus-5 客户端门禁 + sonnet-5 该站没有这个模型」
            # 这种组合里，「死路」严重度 rank 2 优于「门禁」rank 5，于是
            # `min(seen, ...)` 最终报「死路 — 分组无渠道」，而真正该报的是
            # 客户端门禁（补标识或人工接管就能用）。报错方向反了：
            # 一个能救的站被说成没救。
            #
            # 判据与提前 return 那一处同一个 `_model_specific_dead_end` ——
            # 它说的是「这个拒绝只针对当前这个模型」，那种结论本来就不该
            # 参与「整个段是什么状况」的评选。
            if att.category == "死路" and _model_specific_dead_end(att):
                # 只针对这个模型的死路 —— 记进兜底表而不是评选表。
                # 见下方 seen_weak 的说明。
                seen_weak.append((att.category, att.action))
                # 这一发没有给出任何关于「这个站」的信息，所以它不该占掉
                # 基线的额度：补一个候选进来接着打。见上面 spare 的说明。
                if spare:
                    probe_models.append(spare.pop(0))
            else:
                seen.append((att.category, att.action))

            if att.ok:
                v.category, v.action = att.category, att.action
                v.usable = True
                v.models = self._accept(v, model, att)
                return v

            # 余额 / 死路：换模型、加头、走代理都救不了。立即收敛。
            #
            # 但「死路」只对**这个模型**成立时不该终止整段：404 model_not_found
            # 说的是「这个分组没有这个模型」，换个模型完全可能通。只有在
            # 死路原因与模型无关时（敏感词、分组无渠道）才真的没救。
            if att.category == "余额":
                v.category, v.action = att.category, att.action
                return v
            if att.category == "死路" and not _model_specific_dead_end(att):
                v.category, v.action = att.category, att.action
                return v

            # 反测活：探测文本本身触发了拦截，换一句重试
            if att.category == "反测活":
                att = self._call(
                    section, base, row.api_key, model, combo="alt-text",
                    text="Explain the TCP three-way handshake in one sentence.",
                )
                v.attempts.append(att)
                v.category, v.action = att.category, att.action
                if att.ok:
                    v.usable = True
                    v.models = self._accept(v, model, att)
                    return v

            # 换出口 IP 值不值得试一次 —— 按类别分级，不是「不通就试」。
            #
            # 2026-09-01 用 79 凭据的实跑日志量化过（见 _PROXY_FIRST/_SECOND
            # 的说明）：全试要多发 70-105 次请求，而其中一多半类别的失败原因
            # 与出口 IP 无关，那些请求注定白花。所以分两级：
            #   第一级 IP封/边缘  —— 拦的就是 IP，代理是首选处置，立刻试
            #   第二级 未知/临时  —— 原因不明或疑似链路问题，其余处置都用尽后补一次
            # 已证伪的不试：WAF（认客户端形态，实测带代理仍同一拦截页）、
            # 余额（充值自愈）、鉴权（401 凭据）、死路（分组无渠道）、
            # 客户端（要画像不要 IP）、时段（等窗口）。
            if att.category in _PROXY_FIRST and self.live_proxy:
                att = self._call(
                    section, base, row.api_key, model,
                    combo="via-proxy", proxy=self.live_proxy,
                )
                v.attempts.append(att)
                if att.ok:
                    v.usable = True
                    v.need_proxy = True
                    v.category, v.action = att.category, att.action
                    # 补目录必须在 _accept **之前**：_accept 会把这个模型记进
                    # v.models，而 plan 的 catalog 分支看的是 v.catalog ——
                    # 顺序反了的话本次这一个模型进了 models，而目录仍是空的。
                    self._recatalog_via_proxy(row, section, base, v)
                    v.models = self._accept(v, model, att)
                    return v

            # 时段：分组按时间窗口开放。窗口外重试一万次也一样，但凭据是好的 ——
            # 记下窗口后立即收敛，不浪费请求，也不把它判成不可用。
            if att.category == "时段":
                v.time_window = _time_window(att.excerpt)
                v.category, v.action = att.category, att.action
                self.on_event("time-window", {
                    "section": section, "host": host_of(base),
                    "window": v.time_window,
                })
                return v

            # 客户端形态类：按画像梯由省到全回退，第一个 200 即最省可用档。
            #
            # 为什么触发条件这么宽（每一项都是实测踩出来的）：
            #   客户端 —— 站方明说「只允许某某客户端」，可能回 503（不在 401/403 里）
            #   WAF    —— 自建拦截页，认的是客户端形态，换 IP 无效
            #   门禁/IP封/边缘/401/403 —— 都可能实际是形态问题被误分类
            #   鉴权   —— 401 unauthorized client 会落到这里（实测 golf）
            # 判错方向的代价不对称：多试几档只是多几次请求，漏试会把可用站判死。
            #
            # 为什么还要看 `betas.wanted`（2026-09-05 修的 P1）
            # ------------------------------------------
            # 站方在 **400** 上索要 beta 时，上面两个条件一个都不命中 ——
            # classify 对 400 没有兜底，实测这几种正文全落「未知」：
            #
            #     400 + '请启用 128k 输出后重试'
            #     400 + 'missing required header: anthropic-beta'
            #     400 + 'anthropic-beta must include output-128k-2025-02-19'
            #     400 + 'the model requires fine-grained-tool-streaming'
            #
            # （同样的正文在 403 上都判「门禁」，所以 403 那条路是通的。）
            #
            # 而 `_retry_with_betas` 的**唯一调用点**在 `_try_profiles` 内部
            # （本文件 :1130）—— 整梯不跑，它就永远不跑。于是 betas.py 的
            # `output-128k-2025-02-19` 与 `fine-grained-tool-streaming` 两条
            # 规则是**死代码**。1m 那条能走到纯属巧合：classify 恰好把
            # 「1m context」限定在 {400, 403} 上判「门禁」。
            #
            # 更糟的是「未知」在 `_PROXY_SECOND` 里（本文件 :150 附近），
            # 于是处置变成**换出口 IP** —— 对「缺一个请求头」这个根因
            # 完全无关的补救，白烧一次代理请求还得不出正确结论。
            #
            # 为什么不干脆把 400 加进状态码集合：那会让**每一个** 400 都多跑
            # 一整梯（最多 6 档 × 每档一次请求），而 400 是最常见的错误码
            # （参数错、模型名错、body 形状错都是 400）。只在正文**明说**
            # 缺什么 beta 时才跑 —— 那时补上再打一次几乎必然成功，
            # 而 betas.py 的 docstring 正是这么承诺的：
            # 「补上再打一次就通，没有任何理由让用户去手填」。
            #   正文点名要 beta —— 2026-09-05 加。见下方长注释。
            if (att.category in ("客户端", "WAF", "门禁", "IP封", "边缘", "鉴权")
                    or att.status in ("401", "403", "503")
                    or betas.wanted(att.excerpt or "")):
                if self._try_profiles(row, section, base, model, v):
                    return v

        # 全部种子都没通。取**最严重**的类别，而不是最后一个种子的结论。
        # 见本方法 docstring：后者会让一个该站不存在的模型判死整段。
        # 模型专属死路排除在评选之外（2026-09-05 修）。
        #
        # 上面那段 docstring 早就写明「修法是两层：模型专属死路不进 seen，
        # 这里再把客户端排在死路之前」—— 但第一层从来没实现。
        #
        # 后果（实测）：「opus-5 客户端门禁 + sonnet-5 该站没有这个模型」这种
        # 组合里，「死路」rank 2 优于「门禁」rank 5，于是最终报「死路 — 分组
        # 无渠道，充值无效」，而真正该报的是客户端门禁（补标识或人工接管就能
        # 用）。报错方向反了：一个能救的站被说成没救。
        #
        # 但**不能整个丢掉**：如果所有种子都是模型专属死路（该站确实没有我们
        # 试的这几个模型），那它就是唯一的结论 —— 丢掉会让段判不可用却没类别，
        # 界面显示成空白。所以分两级取。
        pool = seen or seen_weak
        if pool:
            best = min(pool, key=lambda ca: self._severity_rank(ca[0]))
            v.category, v.action = best

        # 第二级代理：原因不明（未知）或疑似链路问题（临时）的段，在**所有**
        # 其余处置都用尽之后补一次换 IP。
        #
        # 为什么放在这里而不是与第一级同处：这两类不是「拦的就是 IP」，代理只是
        # 一个便宜的排除法。放在处置链前面会插在画像梯之前，把本该由画像解决的
        # 站先浪费两次代理请求；放在收敛之后则只在真的无路可走时才花这一次。
        #
        # 只用第一个种子模型试一次 —— 若换链路真能通，一次就看得出来；
        # 通了也不直接判 usable，而是**重走一遍接受判定**（_accept 会查换模、
        # 空正文错误体这些假阳性），否则等于给代理路径开了个免检后门。
        if (not v.usable and v.category in _PROXY_SECOND and self.live_proxy
                and probe_models):
            was = v.category
            model = probe_models[0]
            att = self._call(section, base, row.api_key, model,
                             combo="via-proxy-last", proxy=self.live_proxy,
                             **self._profile_kwargs(v, row.api_key))
            v.attempts.append(att)
            if att.ok:
                # 走 _accept 而不是直接置 models —— 它查换模与「200 包错误体」
                # 这两种假阳性。拒收时返回空列表，段仍不可写，不会静默进配置。
                # need_proxy 先置位 —— _recatalog_via_proxy 要看它决定走不走代理。
                # 但 _accept 可能拒收（换模 / 200 包错误体），那时段仍不可用，
                # 所以拒收后要**还原**：need_proxy=True 配 usable=False 会让
                # 能力探测（_stage5_capabilities）以为「这个段需要代理」而跳过，
                # 而实际上它压根不可用 —— 两种不可用的原因显示成一种。
                v.need_proxy = True
                self._recatalog_via_proxy(row, section, base, v)
                accepted = self._accept(v, model, att)
                if accepted:
                    v.usable = True
                    v.category, v.action = att.category, att.action
                    v.models = accepted
                    self.on_event("proxy-rescued", {
                        "section": section, "host": host_of(base),
                        "model": model, "was": was,
                    })
                else:
                    v.need_proxy = False    # 见上：拒收时还原，别混淆两种不可用
        return v

    def _profile_kwargs(self, v: SectionVerdict, api_key: str) -> dict:
        """后续请求（模型扫描 / 换模采样 / 上下文二分 / 换 Key 复验）要带的
        画像参数。**必须与 stage1 通过时那一档完全一致**。

        为什么不能只传 min_headers（这是个实测过的坑的同构形态）
        ------------------------------------------------------
        stage1 用画像通过后，后面四处若只带 headers 不带 body 补丁，那些请求
        对需要 `metadata.user_id` 的站（实测 zulu）会全部失败 ——
        于是「段可用但注册 0 个模型」，或者换 Key 复验时把好 Key 判成坏 Key。
        这与 wave-1 修过的「_stage2 绕过 _accept」是同一类缺陷：主路径加了
        检查，第二条路径没加。所以这里做成唯一入口，四处都走它。

        每次重新 materialize 而不是缓存求值结果：body 里的 session_id 应当
        每个请求都是新的（真实客户端行为），缓存住会让所有请求共用一个会话 ID。
        """
        if v.profile_name:
            # `+beta` 后缀要剥掉再查梯子（2026-09-12 修）
            # ------------------------------------------
            # beta 重放那一支通过时写的是 `f"{top.name}+beta"`
            # （本文件 :1466），而梯子里的名字是 `top.name` —— 于是这里
            # 一个都匹配不上，`body_patch` 静默变成 None，函数落到最后那行
            # 只带 headers 回去。
            #
            # 这正是本函数 docstring 警告的那个坑的实例：stage1 用带 body
            # 补丁的画像通过，后续四处（模型扫描 / 换模采样 / 上下文二分 /
            # 换 Key 复验）却不带它。对需要 `metadata.user_id` 的站，后果是
            # 「段可用但注册 0 个模型」，或者换 Key 复验把好 Key 判成坏 Key。
            #
            # headers 侧看不出问题 —— 它走 `v.min_headers`，而那一支把合并好
            # 的 beta 头存下来了。所以这个缺陷只在 body 补丁上显形。
            wanted = v.profile_name
            if wanted.endswith("+beta"):
                wanted = wanted[:-len("+beta")]
            for prof in profiles.ladder(v.section, self.cfg_snapshot):
                if prof.name == wanted:
                    hdrs, patch = profiles.materialize(prof, api_key)
                    # beta 重放把额外的 anthropic-beta 合并进了 min_headers，
                    # 那份才是**实测通过**的头；画像重新 materialize 出来的
                    # 不含那次合并。以实测的为准，画像只补它没有的键。
                    if v.min_headers:
                        merged = dict(hdrs or {})
                        merged.update(v.min_headers)
                        hdrs = merged
                    return {"extra_headers": hdrs or None,
                            "body_patch": patch or None}
        return {"extra_headers": dict(v.min_headers) or None}

    def _try_profiles(self, row: ParsedRow, section: str, base: str,
                      model: str, v: SectionVerdict) -> bool:
        """按画像梯升级。第一个通过的档写进 verdict 并返回 True。

        梯子是**嵌套超集**（见 profiles 模块 docstring）：第 k 档失败即前 k 档
        的并集都不够，不必回头补试。实测依据 —— golf 的门票是
        user-agent + anthropic-beta + x-app 三项缺一不可，平行尝试会全败而
        它们的并集本来是通的。

        保守取向：整梯跑完（含 alt 档），不提前放弃。多试几档只多几次请求，
        而漏试会把一个可用站判死 —— 后者不可逆（用户按报告弃用了那个站）。

        但「整梯跑完」只需要做**一次**：门票是站+段的属性（站方查 headers 与
        body 形态，不看模型名），所以第一个种子试完整梯全败之后，同段的后续
        种子直接跳过，不重问同一个问题。省的量见 _profiles_failed 的说明。
        """
        pkey = (host_of(base), section)
        if self.reuse_profile_verdict:
            with self._lock:
                already_failed = pkey in self._profiles_failed
            if already_failed:
                self.on_event("profile-skipped", {
                    "section": section, "host": host_of(base), "model": model,
                    "why": "同段整梯已试过且全败，门票与模型无关",
                })
                return False

        tried = 0
        for prof in profiles.ladder(section, self.cfg_snapshot):
            if prof.is_baseline:
                continue                    # 基线已在调用方试过
            hdrs, patch = profiles.materialize(prof, row.api_key)
            att = self._call(
                section, base, row.api_key, model, combo=f"id:{prof.name}",
                extra_headers=hdrs or None, body_patch=patch or None,
                proxy=self.live_proxy if v.need_proxy else None,
            )
            v.attempts.append(att)
            tried += 1
            if not att.ok:
                continue
            # 200 也要过 _accept —— 「200 但正文是错误体」是实测过的假阳性
            # 来源（某站对所有请求都回 200，把真实错误放正文里）。
            models = self._accept(v, model, att)
            if not models:
                continue
            v.usable = True
            v.min_headers = dict(hdrs)
            v.profile_name = prof.name
            v.min_body_kind = _body_kind(prof)
            v.category, v.action = att.category, att.action
            v.models = models
            self.on_event("profile-hit", {
                "section": section, "host": host_of(base),
                "profile": prof.name, "tier": prof.tier,
                "family": prof.family, "tried": tried,
                "needs_body": bool(patch),
            })
            return True
        # 整梯全败，但站方可能在正文里明说了缺什么能力 —— 补上再打一次。
        # 见 betas 模块 docstring（alfa：八档正文逐字相同，全是
        # 「请启用 1m 上下文」，说明站方没查客户端身份，只是缺一个 beta）。
        if self._retry_with_betas(row, section, base, model, v, tried):
            return True

        with self._lock:
            self._profiles_failed.add(pkey)
        self.on_event("profile-exhausted", {
            "section": section, "host": host_of(base), "tried": tried,
        })
        return False

    def _retry_with_betas(self, row: ParsedRow, section: str, base: str,
                          model: str, v: SectionVerdict, tried: int) -> bool:
        """正文点名要 beta 就补上重试。只 claude 段有 anthropic-beta。

        用顶档（整梯最后一档的 headers）作基底：正文既然没在查客户端身份，
        多带门票无害；而少带会引入第二个变量，分不清是 beta 补对了还是门票
        本来就够。
        """
        if section != "claude-api-key":
            return False

        # 从已跑的尝试里找站方索要的项。取最后一次失败的正文 —— 八档一致时
        # 取哪次都一样，不一致时最后一次对应门票最全，最可信。
        extra: list[str] = []
        for att in reversed(v.attempts):
            extra = betas.wanted(att.excerpt or "")
            if extra:
                break
        if not extra:
            return False

        # 基底取**非 alt 的族内最高档**。alt（browser-ua）是替换型画像，不是
        # CC 门票的超集 —— 拿它作基底会把 CC 门票整个丢掉，实测落地只剩 2 个
        # header。而实测能用的形态是「CC 门票 + 1m」，不是「浏览器 + 1m」
        # （2026-09-04：这一句原来写「CPAMP 里能用的形态是…」，而 CPAMP 并不
        # 决定这件事 —— 它的测试按钮只发 x-api-key + anthropic-version，
        # 连 CC 门票都不带。依据是 2026-09-01 那轮 alfa 实测，不是 CPAMP）。
        top = None
        for prof in profiles.ladder(section, self.cfg_snapshot):
            if not prof.is_baseline and not prof.alt:
                top = prof
        if top is None:
            return False

        hdrs, patch = profiles.materialize(top, row.api_key)
        hdrs = dict(hdrs)
        # 头名大小写不敏感，但现有值可能挂在任意拼法上，逐个找
        slot = next((k for k in hdrs if k.lower() == "anthropic-beta"),
                    "anthropic-beta")
        hdrs[slot] = betas.merge(hdrs.get(slot, ""), extra)

        self.on_event("beta-retry", {
            "section": section, "host": host_of(base), "model": model,
            "added": extra, "profile": top.name, "after_tried": tried,
        })
        att = self._call(
            section, base, row.api_key, model, combo=f"beta:{top.name}",
            extra_headers=hdrs, body_patch=patch or None,
            proxy=self.live_proxy if v.need_proxy else None,
        )
        v.attempts.append(att)
        if not att.ok:
            return False
        models = self._accept(v, model, att)
        if not models:
            return False

        v.usable = True
        v.min_headers = hdrs
        v.profile_name = f"{top.name}+beta"
        v.min_body_kind = _body_kind(top)
        v.category, v.action = att.category, att.action
        v.models = models
        self.on_event("beta-hit", {
            "section": section, "host": host_of(base),
            "profile": v.profile_name, "added": extra,
        })
        return True

    # ---------- ⓿ 目录发现（先问站方，再动手打） ----------

    def _recatalog_via_proxy(self, row: ParsedRow, section: str, base: str,
                             v: SectionVerdict) -> None:
        """代理救活这个段之后，补一次目录。就地改 v.catalog，不返回。

        为什么需要（2026-09-05 修）
        ----------------------
        `_stage0_catalog` 在 `_stage1` 最前面跑，那时 `need_proxy` 还没定 ——
        所以它**永远直连**。被 IP 封的站这一步拿 000/403，catalog 为空。

        之后 `via-proxy` 或 `via-proxy-last` 把段救活并置 `need_proxy=True`，
        但目录不会补取（`_stage2` 明确不再 GET）。于是 `v.catalog` 保持空，
        plan 的 catalog 分支走不到，只能落到**种子猜测**或「市面最新」兜底 ——
        写进 config.yaml 的模型名与这个站实际卖的没有关系。而 `_probe_order`
        自己的 docstring 就说这种情形「CPA 路由过去 404」。

        `_stage0_catalog` 的签名里**本来就有** `need_proxy` 参数，只是没有任何
        调用点传它 —— 这是接线漏了，不是设计缺失。

        只在 catalog 为空时补：直连能读目录、但推理被 IP 封的站确实存在
        （目录端点常在 CDN 边缘就放行），那种情况已有目录，重取只是白花一次请求。
        """
        if v.catalog or not v.need_proxy or not self.live_proxy:
            return
        got = self._stage0_catalog(row, section, base, need_proxy=True)
        if not got:
            return
        v.catalog = got
        self.on_event("catalog-via-proxy", {
            "section": section, "host": host_of(base), "count": len(got),
            "why": "直连拿不到目录，代理救活后补取",
        })

    def _stage0_catalog(self, row: ParsedRow, section: str,
                        base: str, *, need_proxy: bool = False) -> list[str]:
        """GET /models 拿站方声明的模型清单。**在任何推理请求之前**跑。

        为什么必须前置（2026-09-01 复盘 79 凭据实跑）
        --------------------------------------------
        原来这一步在 `_stage2`，而 `_stage2` 只在 `_stage1` 已经成功时才跑
        （见 `_full_probe`）—— 顺序完全颠倒：拿写死的种子模型去撞，撞不上
        就判死，从头到尾没问过站方「你到底有什么」。实测后果：45/79 个凭据
        判 0 段可用，其中 7 个的 /models 目录明明拿得到模型（最多 199 个），
        而 model-scan 阶段（用目录里的模型）成功 200 共 78 次，模型名如
        claude-opus-4-6 / claude-opus-4-8 / gpt-oss-120b —— 全都不在种子表里。

        为什么这一步比推理请求安全
        ------------------------
        · GET 目录端点，多数站不计费、不计入调用统计、不触发风控；
        · 参考实现全都只这么做：CPAMP 的**批量健康检查**只发 GET 目录
          （`ProviderHealthCheckDrawer/healthCheck.ts:397-450`，四段分别调
          `fetchGeminiModelsViaApiCall` / `fetchV1ModelsViaApiCall` /
          `fetchClaudeModelsViaApiCall` / `fetchModelsViaApiCall`，全部 GET），
          CLIProxyAPI 自己完全不探上游存活（唯一定时任务 model_updater.go
          拉的是 GitHub 上的模型名录 JSON，不发推理请求）。

          **准确说**（2026-09-04 核实）：CPAMP 里确实有发推理的地方 ——
          凭据编辑抽屉的「测试」按钮（`ClaudeEditDrawer.tsx:520-556` POST
          `/v1/messages` 带 `messages:[{content:"Hi"}]`、
          `CodexEditDrawer.tsx:396` POST `/responses`、
          `OpenAIEditDrawer.tsx:476` POST `/chat/completions`）。
          那是**单条目手工点一次**，不是批量巡检；而且它发的正是用户明确禁止的
          `Hi` 形态。所以这一条的准确表述是「批量健康检查只发 GET」，
          不是「CPAMP 全库无 POST 测活」。
        · 与用户「严禁 Hi/你好 这类简单测活」的要求同向：能不发推理就不发。

        拿不到目录不是失败 —— 返回空列表，调用方回落到种子模型。
        """
        url, headers = request.models_endpoint(section, base, row.api_key)
        proxy = self.live_proxy if need_proxy else None
        seen: list[str] = []
        token = ""

        # gemini 的 /v1beta/models 分页；其余三段一页出完。上限 _CATALOG_PAGES
        # 是为了不被恶意/异常的无限 nextPageToken 拖住。
        for page in range(self._CATALOG_PAGES):
            self._throttle(host_of(base), section)
            page_url = url
            if token:
                sep = "&" if "?" in url else "?"
                page_url = f"{url}{sep}pageToken={urllib.parse.quote(token)}"
            resp = client.send(
                page_url, headers=headers, body=b"", method="GET",
                proxy=proxy, timeout=self.timeout,
            )
            if resp.status != "200":
                if page == 0:
                    # 与 catalog 事件同一套载荷约定：不重复 host。
                    self.on_event("catalog-miss", {
                        "section": section, "host": host_of(base),
                        "status": resp.status,
                    })
                    return []
                break            # 翻页中断：已拿到的仍然算
            for m in request.parse_models_response(section, resp.body):
                if m not in seen:
                    seen.append(m)
            # 被字符校验丢掉的名字要说出来 —— 静默丢站方数据会让人以为
            # 「这个站的目录里就没有那个模型」。见 request.unsafe_names 与
            # model_catalog.name_is_safe。
            for bad, why in request.unsafe_names(resp.body, section):
                self.on_event("model-rejected", {
                    "section": section, "host": host_of(base),
                    "model": bad, "reason": why,
                })
            token = (request.next_page_token(resp.body)
                     if section == "gemini-api-key" else "")
            if not token:
                break

        # 两道闸：白名单（三族之外一律不要）+ 段族（本段只留本族）。
        #
        # 段族这道尤其影响界面：`v.catalog` 会成为方案里「可信模型」的候选
        # 清单，操作员在上面勾选要注册哪些。不过滤的话 gemini 段会列出
        # claude-opus-5 / gpt-oss-120b —— 勾上就写进 gemini-api-key，
        # 而 CPA 拿它去打 :generateContent 每次必失败。
        catalog = [m for m in seen
                   if model_allowed(m) and model_fits_section(section, m)]
        # 载荷保持 {section, count} 两字段 —— 前端与 test_pipeline 的
        # 「catalog 载荷字段」断言按这个约定写。host 由 candidate-start
        # 给出，这里重复只会让事件流变胖；白名单滤掉多少不进载荷，避免
        # 把上游的模型名规模也一起吐出去。
        self.on_event("catalog", {"section": section, "host": host_of(base),
                                  "count": len(catalog)})
        return catalog

    def _probe_order(self, section: str, catalog: list[str],
                     exclude: list[str] | None = None) -> list[str]:
        """验证顺序：目录里的模型优先，种子只作兜底。

        目录是站方**声明有**的，种子是本工具**猜**的。先验声明的那批，命中率
        高得多，也不会为不存在的模型白烧一次请求。种子仍保留在队尾：有些站
        的目录端点不开放（401/404），但推理端点照常工作。

        目录内部必须按**世代降序**排，不能用字母序（2026-09-10）
        ----------------------------------------------------------
        `request.parse_models_response` 返回的是 `sorted(set(...))`，**字母序**。
        而 `_stage2` 收满 `max_models` 就停（本文件 :1439），`max_model_attempts`
        再加一道帽。于是目录大的站上，字母序靠前的旧款先被验完、配额用尽，
        同世代的新变体**根本轮不到** —— 这就是用户报的「勾了 gpt-5.6 却没勾
        gpt-5.6-sol」：谁被验到谁被勾，与世代无关。

        用 `model_catalog.rank_models` 排：它的六级键第一顺位就是版本降序，
        且把 `-mini/-lite/-fast` 这类降级档、`-thinking/-latest/-\\d{8}` 这类
        变体压到后面（model_catalog.py:487）。与「就高选择」同一份判据，
        避免第三处分叉。

        种子仍留在队尾且**不参与重排** —— 它们是写死的猜测，顺序由
        `SEED_MODELS` 表达（每段第一个是该段最想验的那个）。
        """
        skip = set(exclude or ())
        order: list[str] = []
        # 目录里与种子同名的排到最前 —— 既在目录里、又是已知好用的模型
        preferred = [m for m in SEED_MODELS[section] if m in catalog]
        try:
            from .model_catalog import rank_models
            ranked = rank_models([m for m in catalog if m not in preferred])
        except Exception:
            # 排序只是「先验哪个」的优化，排不动不该让整轮探测失败。
            ranked = [m for m in catalog if m not in preferred]
        for m in preferred + ranked + SEED_MODELS[section]:
            # 段族闸：三个协议段只探本族。聚合站目录三族混报，不过这道闸
            # 会让 gemini 段拿 claude 模型去打 :generateContent —— CPA 永远
            # 不会那样发，56% 的请求白烧且 55% 回 500。见 SECTION_FAMILY。
            if not model_fits_section(section, m):
                continue
            if m not in order and m not in skip and model_allowed(m):
                order.append(m)
        return order

    # ---------- ② 模型发现 ----------

    def _stage2(self, row: ParsedRow, v: SectionVerdict) -> None:
        """把段内还没验过的模型补齐到 max_models。

        目录已经在 `_stage0_catalog` 拿过并缓存在 `v.catalog`，这里不再重复
        GET —— 重复问同一个目录端点纯属浪费，且会撞节流。

        compat 段留空 = 注册 0 个模型，所以这一步对 compat 是硬要求。
        """
        found = list(v.models)
        order = self._probe_order(v.section, v.catalog, exclude=found)

        attempted = 0
        for model in order:
            if len(found) >= self.max_models:
                break
            # 尝试次数也要有上限 —— found 只数成功的，而目录可能有几百项
            # 且全部验不过。见 MAX_MODEL_ATTEMPTS_PER_SECTION 的说明。
            if attempted >= self.max_model_attempts:
                self.on_event("model-scan-capped", {
                    "section": v.section, "host": host_of(v.base_url),
                    "attempted": attempted, "accepted": len(found),
                    "remaining": len(order) - attempted,
                })
                break
            attempted += 1
            att = self._call(
                v.section, v.base_url, row.api_key, model, combo="model-scan",
                proxy=self.live_proxy if v.need_proxy else None,
                **self._profile_kwargs(v, row.api_key),
            )
            v.attempts.append(att)
            # 必须走 _accept，不要在这里自己判 model_matches。
            #
            # 2026-08-31 实测：原来这里写的是
            #     if att.ok and fingerprint.model_matches(model, att.resp_model)
            # 于是「200 但正文是错误体」的检查被绕过 —— 那道检查只加在
            # _accept 里，而这条是**第二条接受路径**。结果假阳性照旧：
            # 站方全回 200 包错误体，四段仍注册 11 个模型。
            #
            # 这是同一个坑的第二次：wave 1 修过 _stage1 绕过 model_matches，
            # 现在换成 _stage2 绕过错误体检查。所以接受与否只留一个入口。
            if att.ok and self._accept(v, model, att):
                found.append(model)

        v.models = found

    # ---------- ④a 静默换模 ----------

    def _stage4_swap(self, row: ParsedRow, v: SectionVerdict) -> None:
        """单次测不出来 —— weighted-round-robin 下换模是间歇性的。"""
        if not v.models or self.swap_samples < 2:
            return
        model = v.models[0]
        samples: list[dict] = []
        for _ in range(self.swap_samples):
            att = self._call(
                v.section, v.base_url, row.api_key, model, combo="swap-sample",
                proxy=self.live_proxy if v.need_proxy else None,
                **self._profile_kwargs(v, row.api_key),
            )
            v.attempts.append(att)
            samples.append(att.as_sample())
        v.swap = fingerprint.swap_rate(samples)
        if v.swap.get("swap"):
            self.on_event("swap", {"section": v.section,
                                   "host": host_of(v.base_url), "model": model,
                                   "rate_pct": v.swap.get("rate_pct")})

    # ---------- ④b 上下文上限 ----------

    def _stage4_context(self, row: ParsedRow, v: SectionVerdict) -> None:
        """探真实上限，写进 models[].max-context-length。

        这是唯一有 config.yaml 落点的探测：
          max-context-length → service_models.go:702-706 → model_registry.go:1242
          → codex/models/models.go:207-211 → context_window / max_context_window
        客户端据此定压缩点，所以它直接修掉「967k 逼近 995k 只剩 3% 余量」那个 400。
        """
        if not self.probe_context or not v.models:
            return
        model = v.models[0]
        limit, untrusted = self._bisect(row, v, model)
        v.max_context_length = limit
        v.context_model = model          # 只对这个模型有效，别外推
        v.context_untrusted = untrusted
        if limit:
            self.on_event("context", {"section": v.section, "model": model,
                                      "limit": limit, "untrusted": untrusted})

    # ---------- ⑤ 段专属能力开关 ----------

    def _stage5_capabilities(self, row: ParsedRow, v: SectionVerdict) -> None:
        """实测该段的能力开关，得出「开」还是「不开」。

        两个开关各属一段，判据完全不同，所以分开写：

          codex  `websockets`               能不能走 Responses 的 WS 通道
          compat `support-prompt-cache-key` 上游认不认注入的 prompt_cache_key

        另两段没有这类开关（核对 GeminiKey / ClaudeKey 结构体，
        config_types.go:570 / :351：它们的布尔字段是
        `rebuild-mid-system-message` / `experimental-cch-signing` /
        `disable-cooling`，全都是**本地行为**而非上游能力 —— 探不出来，
        也不该靠探测决定，见 README 的说明）。

        为什么必须实测而不是照抄别的条目：这两个开关都会改变 CPA **发出去的
        形态**，而站方支不支持是站方的属性。抄错的后果不对称：
          · websockets 抄成 true 而站方不支持 → CPA 走 WS 通道，握手失败，
            **不会自动回落 HTTP**（CodexAutoExecutor 只按下游形态与该开关分流，
            codex_websockets_executor.go:71-77）—— 那个凭据的 WS 请求全废
          · websockets 抄成 false 而站方支持 → 只是用不上 WS，无害
        所以默认值必须是「不开」，只有实测 101 才开。
        """
        if not self.probe_capabilities or not v.usable:
            return
        if v.section == "codex-api-key":
            self._probe_websockets(row, v)
        elif v.section == "openai-compatibility":
            self._probe_prompt_cache_key(row, v)
        elif v.section == "claude-api-key":
            # claude 段的 `rebuild-mid-system-message`：与上面两个同一类
            # 「开了可能全废 / 不开可能全废」的开关，判据同样是实打一次。
            self._probe_mid_system(row, v)

    def _probe_websockets(self, row: ParsedRow, v: SectionVerdict) -> None:
        """codex 段：`{base}/responses` 换成 wss 发一次握手。

        与 CPA 完全同构（codex_websockets_connection.go）：
          · URL   `buildCodexResponsesWebsocketURL`（:223）只换 scheme
          · 头    `applyCodexWebsocketHeaders`（codex_websockets_request.go:67）
                  发 `Authorization: Bearer`，并保证
                  `OpenAI-Beta: responses_websockets=2026-02-06`（:102-105）
        没有那个 beta 头时站方可能回 400 而不是 101 —— 那样测出来的是
        「没带门票的握手不通」，不是「站方不支持 WS」。

        需代理的站不探（`need_proxy`）：CPA 的 WS 拨号走
        `newProxyAwareWebsocketDialer` 会用条目的 proxy-url，而这里直连。
        直连拿到的 403 说明不了走代理时的行为 —— 记 None（未探测）而不是 False，
        否则就是「拿一个不成立的实验下结论」。
        """
        if v.need_proxy:
            v.websockets = None
            v.websockets_note = ("该段需走代理，而本探测是直连 —— 直连的握手结果"
                                 "说明不了走代理时的行为，故未探测")
            self.on_event("capability", {
                "section": v.section, "host": row.host, "name": "websockets",
                "result": "skipped", "why": "need_proxy"})
            return

        base = base_for_section(row.bare, v.section)
        ws_url = client.http_to_ws(f"{base.rstrip('/')}/responses")
        if not ws_url:
            v.websockets = None
            v.websockets_note = f"base-url 形态无法转成 ws/wss：{base}"
            return

        self._throttle(row.host, v.section)
        headers = {
            "Authorization": f"Bearer {row.api_key}",
            # CPA 无条件保证这个头（codex_websockets_request.go:102-105）。
            # 少了它站方可能回 400，那测的就不是「支不支持 WS」。
            "OpenAI-Beta": "responses_websockets=2026-02-06",
            "Originator": "codex-tui",
        }
        # 段已学到的必需头一并带上 —— 门票是站的属性，WS 握手同样要过
        # （applyCodexWebsocketHeaders 也会带 UA / x-codex-beta-features）。
        headers.update({k: val for k, val in (v.min_headers or {}).items()
                        if val and k.lower() != "content-type"})
        resp = client.ws_handshake(ws_url, headers=headers,
                                   timeout=min(self.timeout, 30))
        excerpt = _body_excerpt(resp.body) if resp.body else ""
        att = Attempt(
            section=v.section, model="(ws-handshake)", combo="ws-upgrade",
            status=resp.status,
            category="" if resp.status == "101" else _classify(resp.status,
                                                              resp.body)[0],
            action="", elapsed_ms=resp.elapsed_ms,
            excerpt=excerpt or resp.error, sent_chars=0,
        )
        v.attempts.append(att)

        if resp.status == "101" and not resp.error:
            v.websockets = True
            v.websockets_note = f"实测握手返回 101（{resp.elapsed_ms}ms）"
        elif resp.status == "000" or self._is_transient(resp.status):
            # 连接层失败：与「站方明确拒绝」不同 —— 可能是网络抖动。
            # 记 None 而不是 False，写回时同样不写，但界面说「未测出」。
            #
            # 5xx / 429 同理（2026-09-12 补）：那是站方**此刻**过载或限频，
            # 不是「不支持 WS」。这一段的判错代价最不对称 —— `websockets`
            # 抄成 false 只是用不上 WS，而抄成 true 时 CPA 走 WS 通道握手失败
            # **不会回落 HTTP**（codex_websockets_executor.go:71-77）。
            # 所以判不了就留空，跟随「默认不开」。
            v.websockets = None
            why = ("握手未得到响应" if resp.status == "000"
                   else f"上游返回 {resp.status}（过载/限频类）")
            v.websockets_note = (
                f"{why}（{resp.error or excerpt[:60]}）—— 未能判定")
        else:
            v.websockets = False
            v.websockets_note = (f"实测握手返回 {resp.status}"
                                 f"{'：' + excerpt[:80] if excerpt else ''}")
        self.on_event("capability", {
            "section": v.section, "host": row.host, "name": "websockets",
            "result": v.websockets, "status": resp.status,
            "elapsed_ms": resp.elapsed_ms})

    def _probe_prompt_cache_key(self, row: ParsedRow,
                                v: SectionVerdict) -> None:
        """compat 段：请求体带 `prompt_cache_key` 再发一次，看上游收不收。

        CPA 开 `support-prompt-cache-key` 后会往请求体注入这个字段
        （openai_compat_executor.go:875 起）。上游的反应有两种：
          · 忽略未知字段 → 200，开着无害且能命中上游的前缀缓存
          · 严格校验    → 400 `unrecognized request argument` 之类，
                          开着会让**每一个**请求都失败
        后者正是必须实测的理由 —— 那是一个「开了就全废」的开关。

        判据只看这一次请求成不成立，不比对缓存命中：命中率要多轮同 prompt
        才看得出，而那与「开关能不能开」是两个问题。
        """
        if not v.models:
            return
        base = base_for_section(row.bare, v.section)
        model = v.models[0]
        # 画像的 body 补丁与本探测的补丁必须**合并**，不能各传一个 ——
        # `_profile_kwargs` 在有画像时返回的 dict 里就带 `body_patch`，
        # 再显式传一个会 TypeError（同名关键字给了两次）。而那个异常会被
        # `probe()` 的兜底捕获成「死路 — 探测异常」，把一个可用段判死。
        kw = dict(self._profile_kwargs(v, row.api_key))
        patch = dict(kw.pop("body_patch", None) or {})
        patch["prompt_cache_key"] = f"cpa-probe-{row.host}"
        att = self._call(
            v.section, base, row.api_key, model,
            combo="prompt-cache-key",
            proxy=self.live_proxy if v.need_proxy else None,
            body_patch=patch,
            **kw,
        )
        v.attempts.append(att)
        if att.ok and not att.error_envelope:
            v.prompt_cache_key = True
            v.prompt_cache_note = "实测带 prompt_cache_key 时返回 200"
        elif att.status == "000" or self._is_transient(att.status):
            # 5xx / 429 与中途 system 那一支同一个理由：它们说的是「站方此刻
            # 过载/限频」，不是「站方不认 prompt_cache_key」。判成 False 会让
            # 写回把 `support-prompt-cache-key` 关掉（_toggle_lines 里 False
            # 会连原值一起关），凭一次 503 改配置。留空则跟随原值/默认。
            v.prompt_cache_key = None
            why = ("该次请求未得到响应" if att.status == "000"
                   else f"上游返回 {att.status}（过载/限频类）")
            v.prompt_cache_note = (
                f"{why}（{att.excerpt[:60]}）—— 与该字段无关，未能判定")
        else:
            v.prompt_cache_key = False
            v.prompt_cache_note = (
                f"实测带 prompt_cache_key 时返回 {att.status}"
                f"{'：' + att.excerpt[:80] if att.excerpt else ''}")
        self.on_event("capability", {
            "section": v.section, "host": row.host,
            "name": "support-prompt-cache-key",
            "result": v.prompt_cache_key, "status": att.status})

    @staticmethod
    def _is_transient(status: str) -> bool:
        """这个状态码是不是「站方此刻不行」而非「这个请求形态不行」。

        能力探测（中途 system / prompt_cache_key / WS 握手）问的都是
        「站方支不支持这种形态」。而 5xx 与 429 说的是站方**此刻**的状态：
        过载、限频、临时故障 —— 换个时间同一个请求可能就通了。拿它当
        「不支持」的证据会把一次运气写进 config.yaml。
        """
        text = str(status or "").strip()
        if text == "429":
            return True
        return text.startswith("5") and len(text) == 3 and text.isdigit()

    def _probe_mid_system(self, row: ParsedRow, v: SectionVerdict) -> None:
        """claude 段：对话**中途**带 `role: "system"` 的消息，上游收不收。

        为什么要实测这一项（2026-09-11，用户第 4 条点名要「填写到位」）
        ------------------------------------------------------------
        CPA 的 `rebuild-mid-system-message`（config_types.go:403）会把 Claude
        对话里 role 为 system 的中途消息**挪到顶层 system 字段**再转发。
        Anthropic 官方协议只认顶层 `system`，而很多客户端（含 Claude Code 的
        某些路径）会把系统提示塞在 messages 中间：

          · 上游按官方协议严格校验 → 中途 system 直接 400，此时这个开关
            必须打开，否则**每一个**带中途 system 的请求都失败；
          · 上游自己就能容忍 → 开着无害，但没必要（多一次请求体改写）。

        与 `support-prompt-cache-key` 是同一类「开了可能全废 / 不开可能全废」
        的开关，所以判据同样是**实打一次**，不靠猜。

        判据设计
          · 只在段已可用、且已有实测模型时才跑 —— 段本身不通时这一项无意义；
          · 基线（不带中途 system）已经通过是前提，所以这次失败只可能来自
            那条中途 system 消息，归因是干净的；
          · `000`（连接层失败）判 None 而不是 False：网络抖动不是站方拒绝。
        """
        if v.section != "claude-api-key" or not v.models:
            return
        base = base_for_section(row.bare, v.section)
        model = v.models[0]
        # 与 _probe_prompt_cache_key 同一个坑：画像的 body 补丁必须**合并**，
        # 不能再显式传一个 body_patch（同名关键字给两次会 TypeError，
        # 而那个异常会被 probe() 兜底成「死路」，把可用段判死）。
        kw = dict(self._profile_kwargs(v, row.api_key))
        patch = dict(kw.pop("body_patch", None) or {})
        # 三条消息都不能像测活串。全仓有一道闸（tests 的「探测文本非问候」）
        # 扫主流程里的短文本，而它是对的：站方反测活规则最先拦的就是那种形态。
        #
        # 第一条用另一把派生文本，与第三条不同 —— 同一段对话里两句一模一样
        # 也像脚本。中间那条 system 消息用**真实客户端会发的**系统提示形态
        # （带工具/代码语境），而不是 "You are a helpful assistant." 那种
        # 教科书占位串：占位串既缺技术内容、也不像真实流量。
        first = request.probe_text_for(row.api_key + "|mid1")
        patch["messages"] = [
            {"role": "user", "content": first},
            {"role": "system",
             "content": ("You are a coding assistant. Answer with one short "
                         "sentence and prefer standard library functions.")},
            {"role": "user", "content": request.probe_text_for(row.api_key)},
        ]
        att = self._call(
            v.section, base, row.api_key, model,
            combo="mid-system",
            proxy=self.live_proxy if v.need_proxy else None,
            body_patch=patch,
            **kw,
        )
        v.attempts.append(att)
        if att.ok and not att.error_envelope:
            # 上游自己就收 —— 不需要 CPA 代为重建，保持字段缺席（跟随默认）。
            v.rebuild_mid_system = False
            v.rebuild_mid_system_note = "实测中途 system 消息可直接被接受"
        elif att.status == "000" or self._is_transient(att.status):
            # 判不了就说判不了（2026-09-12 补 5xx / 429 这一支）
            # ------------------------------------------------
            # 原来除 `000` 之外的一切失败都归因给「那条中途 system」。
            # 前提「基线通过，所以差异只可能是它」只在**站方状态没变**时成立，
            # 而 503 / 500 / 502 / 504 / 429 恰恰说明状态变了：上游过载、
            # 限频或临时故障，与请求里有没有中途 system 无关。
            #
            # 代价不对称，所以宁可留空：判成 True 会让写回给这个站写上
            # `rebuild-mid-system-message: true`，而那是一个**改变 CPA 发出
            # 形态**的开关（把中途 system 挪到顶层）。凭一次 503 就改形态，
            # 等于拿运气决定配置；留空则跟随 CPA 默认，行为不变。
            v.rebuild_mid_system = None
            why = ("该次请求未得到响应" if att.status == "000"
                   else f"上游返回 {att.status}（过载/限频类）")
            v.rebuild_mid_system_note = (
                f"{why}（{att.excerpt[:60]}）—— 与中途 system 无关，未能判定")
        else:
            # 基线通过而这次失败 → 差异只可能是那条中途 system。
            v.rebuild_mid_system = True
            v.rebuild_mid_system_note = (
                f"实测中途 system 消息返回 {att.status}"
                f"{'：' + att.excerpt[:80] if att.excerpt else ''}"
                f" —— 需要 CPA 代为挪到顶层 system")
        self.on_event("capability", {
            "section": v.section, "host": row.host,
            "name": "rebuild-mid-system-message",
            "result": v.rebuild_mid_system, "status": att.status})

    # 上限直接写在错误正文里的常见形态。命中任一即可免掉整轮二分。
    #
    # 为什么值得单独做：二分最多 6 次请求，body 20 万-110 万字符，
    # 上传本身就要数秒到数十秒 —— 这是「探测要十几分钟」的第二大笔。
    # 而绝大多数上游在超限时会**明说**上限是多少：
    #   OpenAI 系  maximum context length is 200000 tokens
    #   Claude 系  prompt is too long: 215000 tokens > 200000 maximum
    #   国内中转    最大上下文长度为 128000
    # 有明说就用它，不必自己试出来。
    # 「这个数字是**限额**」的语气词。关键词与数字之间必须有它们之一 ——
    # 否则抓到的可能是「请求用了多少」（2026-09-05 修，见 _limit_from_body）。
    _LIMIT_CUE = (r"(?:is|are|of|limit(?:ed)?(?:\s+to)?|max(?:imum)?|"
                  r"至多|最多|上限|限制|为|是)")

    _LIMIT_PATTERNS = (
        # "maximum context length is 200000 tokens"
        r"maximum\s+context\s+length\s+is\s+(\d{4,8})",
        # "prompt is too long: 215000 tokens > 200000 maximum"
        r">\s*(\d{4,8})\s*maximum",
        # "context_length_exceeded ... limit 128000"
        #
        # 关键词与数字之间必须有限额语气词（2026-09-05 收紧）。
        # 原来是 `[^\d]{0,40}?` —— 那 40 个任意非数字字符会跨过
        # 「your request has」，于是
        #   'context_length_exceeded: your request has 275000 tokens'
        # 抠出 275000，那是**请求用量**而不是上限，比真实窗口大。
        # 写进 config.yaml 的后果：客户端永不压缩，每个长请求都撞 400。
        r"context[_\s-]?length[^\d]{0,24}?" + _LIMIT_CUE + r"[^\d]{0,12}(\d{4,8})",
        # 中文形态
        r"最大(?:上下文)?(?:长度|token数?)[^\d]{0,10}(\d{4,8})",
        r"上下文[^\d]{0,10}(?:上限|限制)[^\d]{0,10}(\d{4,8})",
        # 「max input tokens: 200000」—— 明确说 input 的才要
        r"max(?:imum)?[_\s-]?input[_\s-]?tokens?[^\d]{0,12}(\d{4,8})",
    )

    # 正文里出现这些字样时整段放弃 —— 那里的数字说的是**输出**上限，
    # 与上下文窗口是两回事（2026-09-05 加）。
    #
    # 为什么去掉原来那条 `max…tokens?` 模式：`max_tokens` 在 OpenAI 系里指的
    # 就是输出上限。实测两处误取：
    #   'max_tokens: 64000 > 32000, which is the maximum allowed output tokens'
    #     → 抠出 64000（请求值，不是上限）
    #   'max_tokens must be <= 8192'
    #     → 抠出 8192，把输出上限写成上下文窗口
    # 那条模式带来的误取比命中多，整条删掉比修它划算。
    _OUTPUT_LIMIT_HINTS = (
        "output tokens", "completion tokens", "max_tokens", "maxtokens",
        "max output", "输出上限", "输出长度", "最大输出",
    )

    @classmethod
    def _limit_from_body(cls, excerpt: str) -> int | None:
        """从错误正文里抠出上游自报的上下文上限（tokens）。抠不到返回 None。

        返回的是 **token 数**，与 max-context-length 的单位一致
        （service_models.go:702-706 读的就是 token 数）。

        合理性下限 8000：低于这个值的数字几乎不可能是上下文上限，更可能是
        撞上了 max_tokens 输出上限、错误码或时间戳。宁可放弃走二分，
        也不能把一个错的小值写进 config.yaml —— 那会让客户端过早压缩。
        上限 2_000_000：再大的数字不是上下文窗口。
        """
        if not excerpt:
            return None
        low = excerpt.lower()
        for pat in cls._LIMIT_PATTERNS:
            for m in re.finditer(pat, low):
                try:
                    val = int(m.group(1))
                except (ValueError, IndexError):
                    continue
                if not (8_000 <= val <= 2_000_000):
                    continue
                # 这个数字周围在谈**输出**上限就跳过它（2026-09-05）。
                #
                # 按**匹配位置**看而不是看整篇正文：中转站的错误正文里同时提
                # 上下文与输出上限很常见，例如
                #   'maximum context length is 200000 tokens;
                #    output tokens limited to 8192'
                # 整段否决会把 200000 也丢掉 —— 而那个值是对的，白走一轮二分。
                #
                # 窗口只到匹配**前面**那一小段，不看后面 —— 关键在于
                # 「这个数字是被谁限定的」，而限定词在数字之前
                # （`max_tokens: 64000`、`输出上限 8192`）。
                #
                # 看后面会误伤：`maximum context length is 200000 tokens;
                # output tokens limited to 8192` 里，200000 后面 32 字符内
                # 就有 `output tokens` —— 而那说的是另一个数字。
                #
                # 前 40 字符：够覆盖 `max_tokens must be <= ` 与
                # `at most ... completion tokens` 这类写法。
                lo = max(0, m.start() - 40)
                near = low[lo:m.start()]
                if any(h in near for h in cls._OUTPUT_LIMIT_HINTS):
                    continue
                return val
        return None

    def _bisect(self, row: ParsedRow, v: SectionVerdict, model: str) -> tuple[int | None, bool]:
        """二分实际可接受上下文。返回 (上限**以 token 计**, 是否因截断而不可信)。

        单位（2026-09-06 修正，这是一处真实的数据错误）
        ----------------------------------------
        `lo` / `hi` / `mid` 是**发送的字符数**，而返回值要进
        `models[].max-context-length` —— CPA 把它当 **token 数**用：
        model_registry.go:1440 写进 `/v1/models` 的 `max_context_length`，
        codex/models/models.go:206-211 写进 `context_window` /
        `max_context_window`。两个单位差约 4 倍（英文文本）。

        原实现把字符数直接返回，于是「上游能吃 98.75 万字符」被写成
        「窗口 987500 token」—— 虚报约 4 倍。生产 config.yaml 里那 6 处
        `max-context-length: 987500` 正是本函数第三个二分中点
        （(875000+1100000)//2）的字符数，不是任何站声明的窗口。
        后果与那 8 处丢失相反也更糟：客户端按虚高的窗口定压缩点，
        塞到真实上限之外才被上游截断 —— 正是第 08 章那条 400。

        所以字符路径一律经 `_chars_to_tokens` 折算；
        `declared`（上游正文自报）与 `tok`（上游回的 input_tokens）
        本来就是 token，原样返回、**不得**再折算。

        截断校验：200 但 input_tokens < 发送量*0.5 说明上游截了，那个 200
        不算通过。relay-m 发 105 万字符只回 132,696 tokens，模型还被换成
        codex-auto-review —— 200 完全不可信，此时实测 token 数才是真容量。
        """
        lo, hi, rounds = 200_000, 1_100_000, 4

        # 上游自报的上限（tokens）。任何一次失败的正文里读到就记下来 ——
        # 读到就不必再试了，省掉最多 5 次百万字符请求。
        declared: int | None = None

        def check(chars: int) -> tuple[bool, int | None]:
            nonlocal declared
            att = self._call(
                v.section, v.base_url, row.api_key, model,
                combo=f"ctx-{chars // 1000}k",
                proxy=self.live_proxy if v.need_proxy else None,
                text="x" * chars,
                **self._profile_kwargs(v, row.api_key),
            )
            v.attempts.append(att)
            if not att.ok:
                if declared is None:
                    declared = self._limit_from_body(att.excerpt)
                    if declared is not None:
                        self.on_event("context-declared",
                                      {"section": v.section, "model": model,
                                       "limit": declared})
                return False, None
            tok = att.input_tokens
            if tok is not None and tok < chars * 0.5:
                # 截断：tok 就是真实容量 —— **但要先信得过它**。
                # 低于 _MIN_TRUSTED_CONTEXT 的值不是小窗口，是上游的 token
                # 计数不可信（实测有站回 input_tokens: 10）。当「测不出」处理。
                if tok < _MIN_TRUSTED_CONTEXT:
                    self.on_event("context-untrusted", {
                        "section": v.section, "model": model, "tokens": tok,
                        "sent_chars": chars,
                    })
                    return False, None
                return False, tok
            return True, tok

        # 先打 hi。**顺序从「先 lo 后 hi」改成「先 hi」**，这是省时间的关键：
        #   · hi 通过  → 上限 >= hi，一次请求就结束（老逻辑要两次）
        #   · hi 超限  → 正文往往直接写着上限，解析到就结束（老逻辑要 6 次）
        # 只有「hi 失败且正文没说」才需要往下二分。
        #
        # 代价：hi 是 110 万字符，比 lo 贵。但它同时也是最可能一次定音的那一发，
        # 期望请求数从 2-6 降到 1-2。
        ok_hi, trunc_hi = check(hi)
        if ok_hi:
            # hi 是字符数，返回值要 token —— 折算。见 docstring 的单位一节。
            return _chars_to_tokens(hi), False
        if trunc_hi:
            return trunc_hi, True
        if declared is not None:
            # 上游明说了上限。注意单位：declared 是 **token 数**，
            # 而 lo/hi 是 **字符数** —— 二者不能混算。
            # max-context-length 要的正是 token 数（service_models.go:702-706），
            # 所以直接返回 declared，不做任何字符换算。
            return declared, False

        ok, trunc = check(lo)
        if not ok:
            if declared is not None:
                return declared, False
            return (trunc, True) if trunc else (None, False)

        left, right = lo, hi
        for _ in range(rounds):
            if right - left <= 20_000:
                break
            mid = (left + right) // 2
            ok_mid, trunc_mid = check(mid)
            if ok_mid:
                left = mid
            elif trunc_mid:
                return trunc_mid, True
            elif declared is not None:
                return declared, False
            else:
                right = mid
        # left 是「实测能吃下的最大字符数」，同样要折算成 token。
        return _chars_to_tokens(left), False

    # ---------- 形态复用 ----------

    def _reuse_dead(
        self, row: ParsedRow, section: str, dead: SectionVerdict
    ) -> SectionVerdict:
        """同主机的第 2..N 个 Key，且这个段已知是**站+段级**不通：零请求返回。

        与 `_reuse_shape` 的区别在于要不要再发一次请求：

          · `_reuse_shape` 仍打一次基线 —— 段是通的，而**凭证有效性是 Key 的
            属性**，这把 Key 可能欠费
          · 这里一次都不发 —— 拒绝的原因与凭据无关（门禁按请求形态、IP封 按
            来源、死路是分组里没这个渠道、时段按时间），再问一遍答案一样

        省的量（2026-09-05 量化）：15 个 Key 挂同一主机、该段不通时，
        原来是 15 次完整 `_full_probe`（目录 + 基线 + 整梯画像 + 临时重试），
        而且因为门闩在，这 15 次**严格串行**。现在是 1 次 + 14 次零请求复用。

        注意（BUG 修复 2026-09-13）：403 空正文等概率性拦截不应永久化为站级失败。
        仅在高置信度判定（如明确的 WAF 正文、连续多次相同错误）时才复用 dead 形态。

        `attempts` 不复制：那是第一把 Key 的实际请求记录，挂到别的 Key 上会让
        导出日志显示成「这把 Key 也发了这些请求」—— 那是假的。只留一条说明。
        """
        base = base_for_section(row.bare, section)
        v = SectionVerdict(section=section, base_url=base)
        # 站方声明的模型目录是**主机**的属性，沿用（操作员人工接管时要看它）
        v.catalog = list(dead.catalog)
        v.usable = False
        v.category = dead.category
        v.action = dead.action
        v.need_proxy = dead.need_proxy
        v.min_headers = dict(dead.min_headers)
        v.profile_name = dead.profile_name
        v.time_window = tuple(dead.time_window) if dead.time_window else ()
        # `action` 后面补一句说明来源 —— 界面与导出日志都显示这个字段，
        # 不说的话看起来像「这把 Key 也实测过」。
        v.action = (dead.action or "") + "（复用同主机同段结论，未重复发请求）"
        self.on_event("shape-reuse-dead", {
            "host": row.host, "section": section, "key": row.masked(),
            "category": dead.category, "action": dead.action,
        })
        return v

    def _reuse_shape(
        self, row: ParsedRow, section: str, shape: SectionVerdict
    ) -> SectionVerdict:
        """同主机的第 2..N 个 Key：套用已学到的段形态，只验凭证本身。

        为什么可以复用：段的形态是**主机**的属性 ——
          · 这个站在这一段上有哪些模型      （站方的渠道配置）
          · 要不要走代理                    （站方的边缘防护）
          · 最小必需标识头                  （站方的 UA/Originator 校验）
          · 上下文窗口上限                  （站方给这个模型的容量）
          · 有没有静默换模                  （站方的路由行为）
        换一个 Key 不会改变其中任何一条。实测日志：5 行全是 relay-i.example，
        却把 4 段从头到尾各探 5 遍 —— 36 次 × 5 = 180 次请求，其中
        144 次在重复求证同一件事。

        为什么仍要发一次请求：**凭证有效性是 Key 的属性，不是主机的**。
        同一个站的 5 个 Key 完全可能一个欠费、一个被封、三个正常。所以
        每段仍打一次基线（带上已学到的 headers 与代理），只是不再重跑
        模型目录扫描、换模采样、上下文二分 —— 那三步问的都是主机的事。

        代价从 36 次降到 4 次（每段 1 次），且判定精度不降：凭证坏了照样
        当场发现，只是不再为同一个主机重复学习同一套形态。
        """
        base = base_for_section(row.bare, section)
        v = SectionVerdict(section=section, base_url=base)
        # 目录是**主机**的属性（站方声明有哪些模型），与 Key 无关 —— 直接
        # 沿用，不重新 GET。放在所有分支之前：底下有三个提前 return，
        # 判死的段也要带着候选清单回去给操作员人工接管用。
        v.catalog = list(shape.catalog)

        if not shape.models:
            # 主机在这一段本就没有可信模型 —— 换 Key 也不会变出模型来。
            # 直接沿用结论，一次请求都不必发。
            v.usable = shape.usable
            v.category, v.action = shape.category, shape.action
            self.on_event("shape-reused", {"section": section, "host": row.host,
                                           "verified": False,
                                           "reason": "该段无可信模型，无需逐 Key 复验"})
            return v

        probe_model = shape.models[0]
        att = self._call(
            section, base, row.api_key, probe_model,
            combo="reuse-verify",
            proxy=self.live_proxy if shape.need_proxy else None,
            **self._profile_kwargs(shape, row.api_key),
        )
        v.attempts.append(att)
        v.category, v.action = att.category, att.action

        if not att.ok:
            # 这个 Key 在这一段不通（欠费 / 被封 / 权限不同）。
            # 不回退到全量探测：形态已知，失败原因就是凭证本身。
            self.on_event("shape-reused", {"section": section, "host": row.host,
                                           "verified": True, "ok": False,
                                           "reason": f"{att.category} — {att.action}"})
            return v

        if not fingerprint.model_matches(probe_model, att.resp_model):
            # 同主机换 Key 后开始换模 —— 站方可能按 Key 分渠道。
            # 这种情况形态不能复用，退回全量探测。
            self.on_event("shape-reuse-abort", {
                "section": section, "host": row.host,
                "reason": f"复验时请求 {probe_model} 却回 {att.resp_model}，"
                          f"该 Key 渠道与首个 Key 不同，改走全量探测",
            })
            return self._full_probe(row, section)

        # 凭证有效且模型对得上 —— 套用主机形态
        v.usable = True
        v.models = list(shape.models)
        v.need_proxy = shape.need_proxy
        v.min_headers = dict(shape.min_headers)
        v.profile_name = shape.profile_name
        v.min_body_kind = shape.min_body_kind
        v.time_window = shape.time_window
        v.swap = dict(shape.swap)
        v.max_context_length = shape.max_context_length
        v.context_model = shape.context_model
        v.context_untrusted = shape.context_untrusted
        # 能力开关也是**主机**的属性（站方支不支持 WS / 认不认
        # prompt_cache_key），与 Key 无关 —— 沿用，不再各探一次。
        # 这与 need_proxy / min_headers / max_context_length 同一条理由。
        v.websockets = shape.websockets
        v.websockets_note = shape.websockets_note
        v.prompt_cache_key = shape.prompt_cache_key
        v.prompt_cache_note = shape.prompt_cache_note
        self.on_event("shape-reused", {"section": section, "host": row.host,
                                       "verified": True, "ok": True,
                                       "models": len(v.models)})
        return v

    # ---------- 编排 ----------

    def _full_probe(self, row: ParsedRow, section: str) -> SectionVerdict:
        """一个段的完整探测。首个 Key 走这条，之后复用它的形态。

        目录发现在 `_stage1` 内部最前面（`_stage0_catalog`），所以判死的段
        也带着 `v.catalog` 回来 —— 操作员人工接管时需要那份候选清单。
        只有「验穷模型 + 换模抽样 + 上限实测」这三步跳过：段不通时它们全都
        问不出有效结果，白烧请求。
        """
        v = self._stage1(row, section)
        if v.usable:
            self._stage2(row, v)
            self._stage4_swap(row, v)
            self._stage4_context(row, v)
            # 能力开关放最后：它要用到 v.models（compat 那一支）与
            # v.min_headers（codex 的 WS 握手也要带门票），两者都在前面几步
            # 才定下来。
            self._stage5_capabilities(row, v)
        return v

    # 哪些失败类别是「站 + 段」级的 —— 换一把 Key 结论不变。
    #
    # 判据是**这个拒绝取决于什么**：
    #   门禁 / WAF / IP封 / 边缘 —— 站方按请求形态或来源 IP 拒，与凭据无关
    #   死路               —— 分组里没有这个渠道 / 路径不存在，换 Key 也没有
    #   时段               —— 站方按时间判，与凭据无关
    #   客户端             —— 要的是请求画像，与凭据无关
    #   反测活 / 限频       —— 探测自身的形态与节奏问题
    #
    # **不在这里**的两类必须逐 Key 各自探：
    #   鉴权（401）—— 这把 Key 不对，别的可能对
    #   余额       —— 这把 Key 欠费，别的可能有钱
    # 把它们缓存下来会让同站其他 Key 继承别人的欠费结论。
    #
    # 「临时」「未知」也不缓存：那两类本来就该重试，缓存等于放弃重试。
    _HOST_LEVEL_FAIL = frozenset({
        "门禁", "WAF", "IP封", "边缘", "死路", "时段", "客户端",
        "反测活", "限频",
    })

    def _probe_one_section(self, row: ParsedRow, section: str) -> SectionVerdict:
        """探一个段。probe() 的工作单元，串行与并行共用同一份逻辑。

        (host, section) 上做 single-flight：同一主机同一段的完整探测**最多
        只有一个在跑**，后到的等它出结果再走复用路径。

        为什么需要这道门闩：形态学习是这里最贵的动作（模型目录扫描 + 换模
        采样 + 上下文二分，最多 12 次请求，含 4 次百万字符的大 body）。
        没有门闩时，同一主机的多个 Key 一旦并发进来，都会看到
        _shape 里还没有条目，于是各自跑一遍完整探测 —— 5 个 Key 就是
        5 倍开销，而它们学到的形态必然相同（形态是主机的属性）。

        用 per-key 的 Event 而不是全局锁：不同 (站, section) 之间不该互等。

        「站」这一维对 compat 段含**路径**（2026-09-12 修）
        ------------------------------------------------
        原来用 `row.host`，而同一台主机可以按路径挂多个互不相干的上游 ——
        本项目自己的假上游脚本就是 `127.0.0.1:PORT/good` 与 `.../gate`。
        形态是「这个上游」的属性，不是「这台主机」的：

          · `_shape` 命中时，`/b` 会直接套用 `/a` 学到的形态（模型清单、
            上下文上限、能力开关全是 `/a` 的）；
          · `_dead_shape` 更糟：`/a` 的一次门禁会让 `/b` 连探都不探，
            直接判死 —— 一个完全可用的上游因为同主机另一条路径被拦而消失。

        判据与写回侧的 `batch.entry_scope` 完全一致：前三段的 base-url 没有
        路径维度、仍按 host；compat 段用含路径的 provider 身份。两处用同一个
        函数，不再各写一套。
        """
        from .batch import entry_scope
        # 用 `row.bare`（原始裸地址）而不是 `row.base_for(section)`：
        # 后者是 ParsedRow 才有的派生方法，而这里只需要「同一个上游」这个
        # 身份，`bare` 已经带着路径了。少依赖一个方法也让调用方更好替身。
        key = (entry_scope(section, row.bare), section)
        while True:
            with self._lock:
                shape = self._shape.get(key)
                if shape is not None:
                    break                       # 已有形态，走复用
                dead = self._dead_shape.get(key)
                if dead is not None:
                    # 站+段级的失败结论已有，直接复用 —— 不再跑那 12 次
                    # 昂贵探测。见 _dead_shape 与 _HOST_LEVEL_FAIL。
                    return self._reuse_dead(row, section, dead)
                gate = self._inflight.get(key)
                if gate is None:
                    # 本线程认领这次形态学习
                    gate = threading.Event()
                    self._inflight[key] = gate
                    owner = True
                else:
                    owner = False               # 别人在学，等它
            if owner:
                try:
                    v = self._full_probe(row, section)
                    with self._lock:
                        if v.usable:
                            self._shape[key] = v
                        elif v.category in self._HOST_LEVEL_FAIL:
                            # 站+段级的失败也缓存（2026-09-05）——
                            # 它与用哪把 Key 无关，后到的 Key 白跑一遍
                            # 只是把请求数乘以 Key 数，结论一模一样。
                            # 凭证类（鉴权/余额）与该重试的（临时/未知）
                            # 不在这张表里，见 _HOST_LEVEL_FAIL。
                            self._dead_shape[key] = v
                finally:
                    # 无论成败都必须放闸，否则等待方永久卡死
                    with self._lock:
                        self._inflight.pop(key, None)
                    gate.set()
                return v
            # 等认领者出结果。超时兜底：认领者若卡在长 timeout 上，
            # 等待方不该被无限期拖住 —— 醒来重判，那时要么有形态可复用，
            # 要么门闩已清空，由本线程接手认领。
            gate.wait(timeout=self.timeout + 30)

        return self._reuse_shape(row, section, shape)

    def probe(self, row: ParsedRow) -> CandidateResult:
        """探一个候选的四段。

        四段并行（workers>1 时）。为什么可以并行：
          · 四段打的是四个不同端点，业务上互不依赖
          · _throttle 按 (host, section) 计时，同段内仍严格保持 gap
          · _shape / _last_call / _proxy_state 三处共享状态都在 self._lock 下
          · SectionVerdict 每段一个对象，不跨段写

        为什么这一步收益最大：原来四段共享一个 gap 桶且串行，单站 56 次请求
        要 55 x 3s = 165 秒纯睡。拆桶 + 并行后，四段各自睡自己的，
        墙钟时间取四段里最慢的那一段，而不是四段之和。
        """
        res = CandidateResult(row=row)
        self.on_event("candidate-start", {"host": row.host, "key": row.masked()})

        if self.workers > 1:
            # 结果按 SECTIONS 原序回填 —— 前端表格与 CLI 输出都依赖这个顺序，
            # 不能让完成先后决定展示顺序。
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(self.workers, len(SECTIONS)),
                    thread_name_prefix="probe-sec") as ex:
                futs = {ex.submit(self._probe_one_section, row, sec): sec
                        for sec in SECTIONS}
                done: dict[str, SectionVerdict] = {}
                for fut in concurrent.futures.as_completed(futs):
                    sec = futs[fut]
                    try:
                        done[sec] = fut.result()
                    except Exception as e:      # noqa: BLE001
                        # 一段炸了不该拖垮另外三段。记成不可用并带上原因，
                        # 让它照常走「不可用」那条展示路径。
                        v = SectionVerdict(
                            section=sec,
                            base_url=base_for_section(row.bare, sec))
                        v.category, v.action = "死路", f"探测异常：{e}"
                        done[sec] = v
                        self.on_event("section-error",
                                      {"section": sec, "host": row.host,
                                       "error": str(e)})
            for sec in SECTIONS:
                v = done[sec]
                res.sections[sec] = v
                self.on_event("section-done", {"section": sec, "host": row.host,
                                               "usable": v.usable,
                                               "summary": v.summary()})
        else:
            for section in SECTIONS:
                v = self._probe_one_section(row, section)
                res.sections[section] = v
                self.on_event("section-done", {"section": section, "host": row.host,
                                               "usable": v.usable,
                                               "summary": v.summary()})

        self.on_event("candidate-done", {"host": row.host,
                                         "usable": res.usable_sections,
                                         "calls": res.total_calls})
        return res
