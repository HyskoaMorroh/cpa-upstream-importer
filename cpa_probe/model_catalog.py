"""最新模型库：段级族规则 + 同系列取最新 + 在线名录。

为什么单独一个模块（2026-09-02 用户要求）
--------------------------------------
原来「哪些模型能进哪个段」分散在三处：`pipeline.model_allowed`（三族白名单）、
`pipeline.model_fits_section`（段族闸）、`web/app.js` 的两个正则（界面预勾）。
三处规则不完全一致，现场后果是截图里那两个问题：

  · codex 段勾上了 gpt-4o / gpt-image-2 / gpt-oss-20b —— 都是 gpt 族，
    段族闸放行，但它们是老款与非对话模型，勾上等于把请求分给弱模型
  · gemini 段目录读不到模型时只填两个种子（2.5-pro / 2.5-flash），
    而 flash 是降级档，且 2.5 已经不是最新

用户定的新规则（2026-09-02）：

  codex   只能 gpt 系
  claude  只能 claude 系
  gemini  只能 gemini-*-pro，且 * >= 2.5
  compat  不限段，但必须是 gpt / claude / gemini / kimi 四族之一

外加一条贯穿全局的：**同系列以最新版为准，旧版不放入**。
`gpt-5.6` 与将来的 `gpt-5.7` 只留后者；`kimi-k2` 与 `kimi-k3` 只留 `kimi-k3`。

三层数据源
---------
探测拿不到模型时要填一份「当前市面上最新」的清单。三层合并，可信度递减：

  1. **CPA 权威名录**（远程）—— CPA 自己的 `model_updater.go` 每 3 小时拉的
     那份 JSON，两个地址互为备份。它是 CPA 实际认识的模型集合，比任何写死的
     清单都准，且会自动跟进新版本。
  2. **本地 config.yaml 已有的模型名** —— 最强的本地证据：这些站实际就卖这些。
     远程名录里没有的站方特供型号（`gemini-3.1-pro-high`、
     `gemini-3.1-pro-preview-search`、`gpt-5.6`）只在这一层出现。
  3. **内置兜底** —— 前两层都拿不到时用。用户 2026-09-02 指定的那批。

为什么不能只有第 3 层：写死的清单会过期，而过期的表现是「填进 config.yaml 的
模型 CPA 每次轮到都失败」—— 与缺模型一样坏，但更难发现（看着有值）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request

logger = logging.getLogger(__name__)

# ---------------- 归并留痕 ----------------

# `topup_to_market_top` 淘汰同线低代时把原因记在这里（段名 → 说明），
# plan.py 取走后写进该段的 `model_warns`，界面与日志都能看见「为什么少了
# 某个站方报过的名字」。取值即消费（取走就清），避免下一次归并读到旧条目。
_LAST_MERGE_NOTES: dict[str, str] = {}


def take_merge_note(section: str) -> str:
    """取走并清空该段最近一次归并留下的说明；没有就返回空串。"""
    return _LAST_MERGE_NOTES.pop(section, "")


# ---------------- 族判定 ----------------

# OpenAI 的推理系列不叫 gpt-*。用正则而不是前缀元组：`o1`、`o3`、`o4-mini`
# 是「字母 o + 数字」开头，而 `openai-xxx`、`omni-xxx`、`ollama-xxx` 不该命中。
_OPENAI_REASONING_RE = re.compile(r"^o\d+(?:[.\-]|$)")

# 四族。kimi 是 2026-09-02 新增 —— 用户明确把它列进 compat 段的允许清单，
# 而 CPA 的权威名录里也确实有 kimi provider（kimi-k2 … kimi-k3-256k）。
FAMILIES = ("gemini", "gpt", "claude", "kimi")

# codex 段只收 gpt-*，不收 o 系列（2026-09-27 用户在三选一里选定）。
# 旧判例（2026-09-12）「o3 与 gpt-5.6 互不相干、都要」是世代比较的规则，
# 与「codex 段收不收 o 系列」是两件事；用户这次定的是后者。
CODEX_GPT_ONLY = True


def bare_name(name: str) -> str:
    """去掉 provider 前缀。`Business/gemini-2.5-pro` → `gemini-2.5-pro`。

    中转站的目录常带前缀（relay-m 写 `Business/`、`anthropic/`、`cerebras/`），
    而族判定与版本比较都只看后半段。
    """
    n = (name or "").strip().lower()
    return n.split("/")[-1] if "/" in n else n


def generation_family(name: str) -> str:
    """比较世代时的分组维度。比 `family` 多分出一个 `o` 族。

    为什么 o 系列必须与 gpt 分开（用户 2026-09-12 裁定的三个例子）
    -------------------------------------------------------
    `family("o3")` 是 `"gpt"` —— 那对「这个段收不收它」是对的（codex 段走
    OpenAI Responses，o 系列确实走那条路），但对「谁比谁新」是错的：
    o 系列与 gpt 系列是**互不相干的编号体系**，`o3` 的 3 不代表它比
    `gpt-5.6` 老一代。

    用户给的判例：
        ["o1", "o3", "gpt-5.6"]                 → o3 与 gpt-5.6 都要
        ["o3", "o4-mini", "gpt-6", "gpt-6-codex"] → o3 / gpt-6 / gpt-6-codex
        ["o1-pro", "o3-pro", "o4-mini"]          → 只要 o3-pro

    第一例要求 gpt-5.6 不挤掉 o3 —— 两族分开才成立。

    **2026-09-27 用户改判：codex 段只留 gpt 系列**（见 `CODEX_GPT_ONLY`）。
    o 系列从此不进 codex 段（`section_allows` / `section_model_violation`
    在 codex 段直接拒），上面三条判例只对仍收 o 系列的段（compat）成立；
    `newest_generation_per_line` 本身不看段，判例照旧作为它的单元行为保留。
    只有 o 系列的站，codex 段清单为空 → 不写（不拿写死的 gpt 名字顶替）。
    第二、三例里的 `o4-mini` 由 `is_low_tier` 先剔除（用户同一句话定的
    「凡是模型名称中带 mini 的一律排除」），所以它不会挤掉 o3 / o3-pro。

    四族之外的名字按**自己的词根**分组，不能一起丢进 `""` 桶
    -------------------------------------------------------
    `family("grok-4.6")` 与 `family("glm-5.2")` 都是 `""`。归进同一个桶
    就变成「glm 的 5 比 grok 的 4 新」→ grok-4.6 被挤掉。而 romeo 的
    compat 段唯一端到端验证过的模型就是 grok-4.6（见 `section_protocol_ok`
    的说明），挤掉它等于把那个段变成没有可用模型。
    它们是互不相干的编号体系，与 o 系列对 gpt 是同一回事。

    `family` 保持原样不动 —— 段归属判据（`section_allows` 的
    `SECTION_FAMILY` 比对）要的就是「o 系列属于 gpt 段」。
    """
    n = bare_name(name)
    if _OPENAI_REASONING_RE.match(n):
        return "o"
    return family(n) or n.split("-", 1)[0]


def family(name: str) -> str:
    """模型属于哪一族。返回 gemini / gpt / claude / kimi / ""（认不出）。

    这是**段归属**判据（o 系列算 gpt，因为它走 codex 段）。比较世代时要用
    `generation_family` —— 那里 o 系列自成一族。见那个函数的说明。
    """
    n = bare_name(name)
    if n.startswith("gemini"):
        return "gemini"
    if n.startswith("claude"):
        return "claude"
    if n.startswith("kimi"):
        return "kimi"
    if n.startswith("gpt") or _OPENAI_REASONING_RE.match(n):
        return "gpt"
    return ""


# gemini 段的额外约束：只要 `gemini-<版本>-pro*`，且版本 >= 2.5。
#
# 为什么（用户 2026-09-02）：flash / flash-lite 是降级档，勾上会让 CPA 的
# 轮询把请求分给弱模型；2.5 以下已停服或能力不足。
#
# `-pro` 之后允许有后缀（`-high` / `-low` / `-preview` / `-preview-search`
# / `-preview-customtools`）—— 那些都是 pro 的变体，用户清单里明确要求。
# 但 `-pro-image` 这类是图像模型，不是对话模型，由 _NON_CHAT 排除。
_GEMINI_PRO_RE = re.compile(r"^gemini-(\d+(?:\.\d+)?)-pro(?:$|[-.])")
GEMINI_MIN_VERSION = 2.5


def gemini_pro_ok(name: str) -> bool:
    """是不是 gemini-*-pro 且版本 >= 2.5。"""
    m = _GEMINI_PRO_RE.match(bare_name(name))
    if not m:
        return False
    try:
        return float(m.group(1)) >= GEMINI_MIN_VERSION
    except ValueError:
        return False


# 降级档：名字里带 mini / nano / lite / flash / fast / haiku 的一律不选
# （用户 2026-09-12 定 mini，2026-09-16 补 flash 与 fast，2026-09-27 补 haiku，
# **不分类型、不看版本号**）。
# ------------------------------------------------------------------------
# 用户原话（2026-09-12）：「本项目无论什么类型，凡是模型名称中带 mini 的就算
# 版本很高也不应该勾选应该排除」。
# 用户原话（2026-09-16 第 ① 条）：「所有带 mini、flash、fast 的模型都不勾选，
# 这种模型属于低档次模型，没有存在的价值」。
# nano / lite 是同一档的其它写法，一并挡掉。
#
# 为什么 2026-09-27 补 haiku（本文件内部自相矛盾，实跑确认）
# --------------------------------------------------------
# `_TIER_HINTS` 给 haiku 打 4，比 flash 的 3 **更低**，即这个文件自己就认定
# haiku 比 flash 更弱；`FALLBACK_MODELS['claude-api-key']` 也有意不含 haiku。
# 但判定链上只硬排除 flash、放行 haiku，于是：
#
#   · 站上只有 `claude-3-5-haiku` 时六道闸全放行，它被写进 config.yaml；
#   · 更糟的是同世代时它**保证占一个注册位** —— `_product_line('claude-haiku-5')`
#     是 `claude-haiku`，自成一条产品线，`_round_robin` 每线取第一个，而它是
#     那条线上唯一成员。`_WEAK` 把它排到线内最后毫无作用（只能线内降权，
#     跨线轮转绕过它）。实测目录 [opus-5, opus-5-thinking, sonnet-5,
#     fable-5-1, haiku-5] + limit=4 时，被挤掉的是 opus-5-thinking，
#     入选的是 haiku-5 —— 正好与用户规则①相反。
#
# 放进 `_LOW_TIER` 而不是只改排序，才能同时消掉这两条路径。
#
# 为什么 flash 必须单列，而不是靠 gemini 段的 pro 闸兜住
# --------------------------------------------------
# `gemini_pro_ok` 只在 **gemini 段**生效。compat 段对 gpt / claude / kimi 三族
# 不查 pro —— 实测 2026-09-16：`claude-opus-5-fast` 在 compat 段
# `section_allows` 返回 True，`gemini-3.1-flash` 也只是因为不属四族之一才被
# 挡下。换句话说降级档在 compat 段原本是漏的。
#
# 为什么单独一条而不是塞进 `_NON_CHAT`：那个正则管的是「协议不同、路由过去
# 必然失配」（图像 / 语音 / 嵌入 / 批处理）。mini / flash / fast 是能对话的，
# 只是档次低 —— 判据不同，混在一起下次读的人会以为它们也是协议问题。
#
# 为什么必须按 **token 边界** 匹配：`gemini` 与 `kimi` 的字面里就含 `mini`
# 与 `imi`。裸 `in` 判会把整个 gemini 族和 kimi 族全部挡掉 —— 那是静默的
# 灾难（gemini 段会一个模型都挑不出来）。`fast` 同理要防 `breakfast` 这类
# 子串（模型名里虽不常见，但判据一致比碰运气好）。
#
# 这一条同时解决了本项目两套测试互斥的老问题：`o4-mini` 被这里先剔除，
# 于是「族内比主版本」不会让它挤掉 `o3` / `o3-pro`，
# 见 `newest_generation_per_line` 的两阶段说明。
#
# 2026-09-26 用户拍板：`gemini-3.1-pro` 后面的所有后缀（`-high` / `-low` /
# `-preview*`）都算同一系列，一并保留 —— `low` **不**进降级档。
_LOW_TIER = re.compile(
    r"(?<![a-z0-9])(?:mini|nano|lite|flash|fast|haiku)(?![a-z0-9])")


def is_low_tier(name: str) -> bool:
    """名字里带 mini / nano / lite / flash / fast / haiku —— 降级档，工具一律不挑。

    只管**工具自己挑不挑**。操作员显式手填的走 `section_protocol_ok`，
    那一层不问档次（手填是显式意图，见 plan.py 的 forced_models 分支）。
    """
    return bool(_LOW_TIER.search(bare_name(name)))


# 推理算力档后缀 → 档序（越大越高）。用于 `newest_generation_per_line`
# 的阶段 C：同一基名只留最高算力档。
#
# 为什么与 `_LOW_TIER` 分开（2026-09-30）
# --------------------------------------
# `_LOW_TIER` 管「独立的廉价型号」（`gpt-5-mini`、`gemini-3-flash` ——
# 另一条产品线，无条件不选）。这里管「同一个型号的算力档」
# （`gemini-3.1-pro-high` / `-low` —— reasoning effort 后缀）。
# 后者只在**同基名有更高档时**才压掉，否则某站只提供 `-low` 变体时
# 该段会被清空，撞上红线 2「严禁出现不勾选」。判据必须是相对的。
#
# 不硬编码型号名（红线 3）：只认后缀本身，新型号自动适用。
_EFFORT_RANKS: dict[str, int] = {
    "low": 1, "minimal": 1,
    "medium": 2, "mid": 2, "standard": 2, "std": 2,
    "high": 3, "max": 4, "ultra": 4,
}

# 裸基名的档序：高于一切具名档（站方默认档就是它的推荐档）。
# 必须定义在 `_effort_split` 之前 —— 它在函数体里被引用，而模块加载时
# 就会执行到那一行的默认返回。
_BARE_EFFORT_RANK = 99

# 末段后缀：只认**具名**算力档（`-high` / `-low` / `-medium` …）。
#
# 为什么**不认纯数字后缀**（2026-09-30 实测后收紧）
# ------------------------------------------------
# 原来把 `-\d{1,3}` 也当算力档，实测当场踩雷：
#     claude-opus-5   ->  ('claude-opus', 1.05)   ← 把**版本号 5** 当成了算力档
# 于是 `claude-opus-5` 与 `claude-opus-4-8` 会被归进同一个基名
# `claude-opus` 相互压制，claude 段只剩一个模型 —— 比原来的 `-low` 问题
# 严重得多（红线 2 要求同级全勾）。
#
# 数字在模型名里的含义本来就多义：版本（`claude-opus-5`）、日期戳
# （`-20251001`）、上下文容量（`-256k`）、快照序号（`gpt-4-0613`）——
# 算力档反而极少用裸数字。判不准就不判：宁可漏掉一个假想的 `-4` 档，
# 也不能把版本号误当档位去压掉真模型。
#
# 世代/版本的比较由阶段 A/B 的 `generation()` 负责，那里有完整的版本语义；
# 这一层只管「同一基名的具名算力档」。
_EFFORT_SUFFIX = re.compile(r"^(.*?)[-.](" + "|".join(_EFFORT_RANKS) + r")$")


def _effort_split(name: str) -> tuple[str, int]:
    """拆出 (基名, 算力档序)。没有具名算力后缀的返回 (原名, 最高档)。

    裸基名视为**最高档**：`gemini-3.1-pro` 是站方默认档，不该被它自己的
    `gemini-3.1-pro-low` 顶掉。具名档按 low < medium < high < max 排。

    只拆**一层**后缀，且只认 `_EFFORT_RANKS` 里的具名档：
      · `gemini-3.1-pro-preview-search` 的 `search` 不在档表里 → 整名原样
        返回（功能变体，不是算力档，规则③要求同级变体全留）
      · `claude-opus-5` 的 `5` 是版本号 → 整名原样返回（见上面的说明）
    """
    n = bare_name(str(name or "")).lower()
    m = _EFFORT_SUFFIX.match(n)
    if not m:
        return n, _BARE_EFFORT_RANK
    return m.group(1), _EFFORT_RANKS[m.group(2)]



# 非对话模型。写进 config.yaml 不会报错，但 CPA 路由过去必然失配 ——
# 图像与语音模型走的不是 /chat/completions 或 :generateContent 的文本路径，
# 而 oss 是开源小模型。
#
# 截图里 codex 段勾上的 gpt-image-2 / gpt-oss-120b / gpt-oss-20b 全在这里。
_NON_CHAT = re.compile(
    r"-image(?:$|[-.])"          # gpt-image-2、gemini-3-pro-image
    r"|-tts(?:$|[-.])"           # gemini-2.5-flash-preview-tts
    r"|^imagen"                  # imagen-4.0-*
    r"|-oss-"                    # gpt-oss-120b / gpt-oss-20b（开源小模型）
    r"|-embedding"
    r"|-whisper"
    r"|-moderation"
    r"|-batch-inference"         # gemini-batch-inference：批处理端点，不是对话
)


def is_chat_model(name: str) -> bool:
    """是不是对话模型。图像 / 语音 / 嵌入 / 批处理都不是。"""
    return not _NON_CHAT.search(bare_name(name))


# 段 → 允许的族。compat 段不限单一族，见 section_allows。
SECTION_FAMILY: dict[str, str] = {
    "gemini-api-key": "gemini",
    "codex-api-key": "gpt",
    "claude-api-key": "claude",
}


# 模型名的合法字符与形态。
#
# 为什么必须校验（2026-09-05 加，审计发现）
# ----------------------------------
# 模型名来自**站方的 `/models` 目录**（第三方完全可控），而它会被
# 直接拼进出网 URL：
#
#     request.py:92  f"{base}/v1beta/models/{model}:generateContent"
#
# 实测通过原来全部闸门并原样上线的名字：
#     '../../../gemini-3.1-pro'        → 逃出 base 路径，打到同主机别的端点
#     'gemini-3.1-pro-x?a=b'           → `:generateContent` 落进 query，
#                                        实际请求的是另一个端点；它回 200
#                                        就成了「该模型可用」的伪证
#     'gemini-3.1-pro.%2e%2e%2fadmin'  → 编码过的路径穿越
#
# 而且**不需要拿到 200**：plan.py 的 catalog 分支把目录里的名字直接当候选，
# 于是这串字面量进 config.yaml，CPA 用同样的方式拼 URL 再发一次
# （gemini_executor.go 也是裸拼、不 escape）。本工具是这条链上唯一有机会
# 校验的一环。
#
# `/` 必须允许
# ----------
# 生产配置里 85 个模型名有 `Business/gemini-2.5-pro`、`anthropic/claude-opus-5`
# 这种带前缀的形态 —— 那是中转站的分组/厂商前缀，合法且常见。
# 实测那 85 个名字用到的非字母数字字符只有 `-` `.` `/` 三个，最长 34 字符。
#
# 所以判据不是「不许有 `/`」，而是「只许这三个符号 + 禁止路径穿越
# + 禁止 query/fragment 的起始字符」。
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")

# 长度上限：真实名字最长 34，给 4 倍余量。超长名本身就是异常信号，
# 而且会让 URL 超过某些中转站的路径长度限制。
_NAME_MAX = 128


def name_is_safe(name: str) -> str:
    """模型名能不能安全地拼进 URL 与写进 config.yaml。不能则返回原因。

    只做**字符与形态**校验，不判族/版本（那是 `section_allows` 的事）。
    分开是因为两者的失效方式不同：族判错只是少探一个模型，
    字符没校验会让出网请求打到别的端点、并把脏字符串写进生产配置。
    """
    n = (name or "").strip()
    if not n:
        return "模型名为空"
    if len(n) > _NAME_MAX:
        return f"模型名过长（{len(n)} 字符，上限 {_NAME_MAX}）"
    if not _NAME_OK.match(n):
        bad = sorted({c for c in n if not re.match(r"[A-Za-z0-9._:/-]", c)})
        return (f"模型名含非法字符 {bad or ['(首字符不是字母数字)']} —— "
                f"它会被拼进出网 URL 并写进 config.yaml")
    # 路径穿越：`..` 作为任意一段，或名字以 / 开头/结尾
    segs = n.split("/")
    if any(seg in ("", ".", "..") for seg in segs):
        return "模型名含空段或 .. 段（路径穿越）"
    # 编码过的穿越：%2e%2e%2f 之类。上面的字符集已经排除了 `%`，
    # 这一条是防将来放宽字符集时忘掉它 —— 留着比省下便宜。
    low = n.lower()
    if "%2e" in low or "%2f" in low or "%5c" in low:
        return "模型名含编码过的路径分隔符"
    return ""


def section_allows(section: str, name: str) -> bool:
    """这个模型能不能进这个段。用户 2026-09-02 定的四条规则。

    这是**唯一**判据 —— 探测队列、目录落盘、方案生成、界面预勾全都问它，
    不许各处再自己写一套（那正是截图里两个问题的成因）。

    注意它管的是「**工具自己**要不要挑这个模型」。操作员显式手填的走
    `section_protocol_ok` —— 那一层只挡协议层不可能成立的，不挡族。
    见 build_plan 里 forced_kept 的说明。
    """
    # 字符与形态校验放最前（2026-09-05 加）。手填与自动挑选都要过 ——
    # 操作员也可能从站方页面复制粘贴一个带 `?` 或 `../` 的名字。
    # 见 name_is_safe：这个名字会被拼进出网 URL 并写进 config.yaml。
    if name_is_safe(name):
        return False
    n = bare_name(name)
    if not n:
        return False
    fam = family(n)
    if fam not in FAMILIES:
        return False
    if not is_chat_model(n):
        return False
    # mini / nano / lite 一律不挑（用户 2026-09-12，不分类型、不看版本）。
    # 放在族判之后：`is_low_tier` 按 token 边界匹配，gemini / kimi 不受影响。
    if is_low_tier(n):
        return False
    if fam == "gemini" and not gemini_pro_ok(n):
        return False
    if section == "gemini-api-key":
        return gemini_pro_ok(n)
    if (section == "codex-api-key" and CODEX_GPT_ONLY
            and _OPENAI_REASONING_RE.match(n)):
        # codex 段只留 gpt 系列（2026-09-27 用户改判，见 CODEX_GPT_ONLY）
        return False
    want = SECTION_FAMILY.get(section)
    if want:
        return fam == want
    # compat：四族都行，走 /chat/completions 万能口
    return True


def section_protocol_ok(section: str, name: str) -> bool:
    """这个模型在这个段上**协议层**成不成立。给手填用，比 section_allows 宽。

    两者的差别只有一处：四族之外（grok / glm / deepseek / qwen / llama）。
    `section_allows` 拒它们 —— 那是本工具的选型偏好，用于「工具自己该挑什么」。
    这里放行 —— 那是操作员的显式指定，而 compat 段确实能跑它们。

    为什么必须分开（2026-09-03 拿真实配置核实）：
      · compat 段走 `/chat/completions`（openai_compat_executor.go:107），
        CPA 对模型名零校验（buildOpenAICompatibilityConfigModels 照单注册，
        service_models.go:713-739）—— 能不能用只取决于上游认不认。
        实测 romeo 的 compat 段**唯一端到端验证过的就是 grok-4.6**，
        foxtrot 段有 grok-4.6 + glm-5.2。按族拒掉手填，操作员就再也没办法把
        这些已知可用的模型写回去。
      · 前三段仍按族拒：claude 段走 Anthropic 原生 `/v1/messages`
        （claude_executor_execute.go:23），gemini 段走 generateContent ——
        往那里发 grok 上游必失配，放行只会制造死条目。
      · 非对话模型（图像/语音/嵌入/批处理）四段都拒：协议不同，必失配。
    """
    # 字符与形态校验也在这里 —— 手填不豁免。见 name_is_safe 与
    # section_allows 里同一句注释：这个名字会被拼进出网 URL。
    if name_is_safe(name):
        return False
    n = bare_name(name)
    if not n:
        return False
    if not is_chat_model(n):
        return False
    if section == "gemini-api-key":
        return gemini_pro_ok(n)
    want = SECTION_FAMILY.get(section)
    if want:
        return family(n) == want
    # compat：不限族。四族之外的由 build_plan 单独给一条警告。
    return True


def section_model_violation(section: str, name: str) -> str:
    """工具**自己挑**的模型能不能进这个段。不能则返回原因（空串 = 合规）。

    写回前的最后一道族/档次闸（2026-09-26）。与 `section_allows` 的差别：
    认不出族的名字（站方特供简写，如实测夹具里的 `opus-5`）不在这里拒 ——
    它们只可能来自站方自己报的清单，拒掉会把唯一实测过的名字删掉。
    这里只拒**能确定是错的**：

      · 字符不安全 / 非对话模型（图像、语音、嵌入…）
      · 降级档（mini / nano / lite / flash / fast / haiku）—— 高低档混在一段里，
        CPA 轮询会把请求分给弱模型（用户：「模型勾选高低模型混乱错误」）
      · 族错段：认得出族、且不是这个段的族。实测生产 config.yaml 的
        codex 段里有 5 个条目挂着 `claude-opus-4-8` —— codex 段走 OpenAI
        Responses，claude 模型路由过去必失配
      · gemini 段只收 `gemini-<≥2.5>-pro*`

    操作员显式手填的**不走这里**（那是 `section_protocol_ok` 的事）。
    """
    why = name_is_safe(name)
    if why:
        return why
    n = bare_name(name)
    if not is_chat_model(n):
        return "非对话模型"
    if is_low_tier(n):
        return "降级档（mini/nano/lite/flash/fast/haiku）"
    fam = family(n)
    if section == "gemini-api-key":
        return "" if gemini_pro_ok(n) else "gemini 段只收 gemini-≥2.5-pro*"
    if (section == "codex-api-key" and CODEX_GPT_ONLY
            and _OPENAI_REASONING_RE.match(n)):
        return "codex 段只留 gpt 系列（o 系列不进，2026-09-27 用户改判）"
    want = SECTION_FAMILY.get(section)
    if want and fam and fam != want:
        return f"{fam} 族模型不属于 {section}"
    if fam == "gemini" and not gemini_pro_ok(n):
        return "gemini 只收 ≥2.5 的 pro 档"
    return ""


def section_family_violations(section: str, models: list[str]) -> list[str]:
    """清单里违反 `section_model_violation` 的名字（保序）。给写回前校验用。"""
    return [m for m in (models or []) if section_model_violation(section, m)]


# ---------------- 同系列取最新 ----------------

# 版本 token：独立的数字串（允许 . 或 - 分隔的多段），前后不能紧贴字母数字。
#
# `k?` 捕获 kimi 的 `k2` / `k3` 形态 —— 那个 k 属于系列名而非版本号，
# 要留在系列里，否则 `kimi-k2` 与 `kimi-2` 会被并成一个系列。
#
# `o?` 捕获 OpenAI 的 `4o` 形态（2026-09-02 补）。原来的负向断言把 `4o` 整个
# 排除在版本之外，于是 `gpt-4o` 自成一系、永远不被 `gpt-5.6` 淘汰 ——
# 现场截图里 codex 段勾着 gpt-4o 就是这个原因。`o` 是「omni」的代号后缀，
# 不是新的产品线：`gpt-4o` 与 `gpt-5.6` 同属 gpt 线，只是世代不同。
#
# `-oss-120b` 那类仍被排除：120 后面紧跟 b，不匹配 `o?(?![A-Za-z0-9])`。
_VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9.])(k?)(\d+(?:[.\-]\d+)*)(o?)(?![A-Za-z0-9])")

# OpenAI 推理系列的世代：`o1` / `o3` / `o4-mini` 里的数字。
#
# 为什么要单独一条（2026-09-04 现场截图：codex 段同时勾着 o1 与 o3）
# ------------------------------------------------------------
# `_VERSION_RE` 要求版本数字前面不紧贴字母（`(?<![A-Za-z0-9.])`），而这一族
# 的数字**紧贴开头的 o**。于是 `o1` / `o3` / `o4-mini` 全部解析成「无版本」，
# `newest_generation_per_line` 的「整组认不出版本就全留」兜底把七个名字一起
# 留下并默认全勾 —— 与 `gpt-4o` 那次是同一个形态（正则读不出版本 ⇒ 躲过
# 世代过滤），只是换了一族。
#
# o3 是 o1 的后继（同一条推理产品线的下一代），把两代一起注册进 config.yaml
# 等于让 CPA 的轮询把请求分给旧款。
#
# 为什么用 `^o` 而不是把 `o?` 加进 `_VERSION_RE` 的可选前缀：那样
# `omni-3` / `oss-20b` 这类以 o 开头但 o 不属于版本记号的名字也会被改判。
# 锚在开头 + 紧跟数字，只命中真正的推理系列（与 `_OPENAI_REASONING_RE`
# 同一套判据）。
_O_SERIES_RE = re.compile(r"^o(\d+(?:[.\-]\d+)*)(?![A-Za-z0-9])")

# 纯 8 位日期戳后缀（`-20251001`）。站方用它标同一款的发布日期，不是版本号。
#
# 为什么必须在 `series_and_version` 里剥掉，而不是留给下游截断：见那个函数的
# docstring —— `generation` 取前两位只在版本号已经有两位时够用，单段版本号
# （`claude-opus-5`）会把日期戳当成次版本号，于是带戳的写法淘汰掉不带戳的。
#
# 捕获组留住戳**前面**的部分，戳后面的后缀原样接上（`(?=$|[-.])` 只看边界，
# 不消费它），所以 `claude-opus-5-20251001-preview` → `claude-opus-5-preview`，
# 与不带戳的写法落在同一个系列同一个版本上。
# 八位以外的数字段不碰：那可能是真的版本号（`gpt-4-32k` 的 32、`kimi-k2` 的 2）。
_DATE_STAMP = re.compile(r"^(.*?)-\d{8}(?=$|[-.])")


def series_and_version(name: str) -> tuple[str, tuple[int, ...] | None]:
    """拆成 (系列, 版本元组)。认不出版本时版本为 None。

        gpt-5.6-sol      → ("gpt-*-sol", (5, 6))
        gpt-5.7-sol      → ("gpt-*-sol", (5, 7))     同系列，版本更高
        gpt-4o           → ("gpt-*", (4,))           o 是代号后缀，不是新系列
        claude-opus-5    → ("claude-opus-*", (5,))
        claude-opus-4-8  → ("claude-opus-*", (4, 8))  同系列，版本更低
        kimi-k3          → ("kimi-k*", (3,))
        o1               → ("o*", (1,))             推理系列：o 后紧跟的数字是世代
        o4-mini          → ("o*-mini", (4,))

    日期戳在**这一层**就剥掉（2026-09-27）
    ------------------------------------
    `claude-opus-5-20251001` 必须解析成 `(5,)` 而不是 `(5, 20251001)`。
    下游 `generation` 取前两位本来是为这件事兜底，但它只在版本号**已经有两位**
    时够用：

        claude-haiku-4-5  (4,5)  vs  -20251001 版 (4,5,20251001)
            → 前两位都是 (4,5)，相等，两个都留        ✓ 兜底生效
        claude-opus-5     (5,)   vs  -20251001 版 (5,20251001)
            → 前两位是 (5,0) vs (5,20251001)
            → 日期戳被当成**次版本号**，带戳的那个胜出  ✗ 兜底漏了

    也就是说单段版本号 + 日期戳会让「同一款的两种写法」变成「带戳的更新」，
    把不带戳的那个淘汰掉 —— 而站方两种写法常常同时在目录里。`_VARIANT` 早就
    列了八位日期戳，但那个正则只用于 `_product_line` 分组，没有在解析版本号
    之前生效。这里在取版本号前先去掉纯日期戳后缀，两种写法就落在同一个版本上。
    """
    n = bare_name(name)
    # 纯 8 位日期戳是「同一款的发布日期」，不是版本号的一部分。
    # 只剥末尾的那一段：`claude-opus-5-20251001-preview` 这种保留后面的后缀，
    # 去掉中间的戳，系列名仍然对得上不带戳的写法。
    n = _DATE_STAMP.sub(lambda m: m.group(1), n)
    # 推理系列先判 —— `_VERSION_RE` 读不出它的版本（数字紧贴开头的 o），
    # 而「读不出版本」会让 o1 与 o3 一起躲过世代过滤。见 _O_SERIES_RE。
    mo = _O_SERIES_RE.match(n)
    if mo:
        try:
            nums = tuple(int(x) for x in re.split(r"[.\-]", mo.group(1)))
        except ValueError:
            return n, None
        return "o*" + n[mo.end():], nums
    m = _VERSION_RE.search(n)
    if not m:
        return n, None
    series = n[:m.start()] + m.group(1) + "*" + n[m.end():]
    try:
        nums = tuple(int(x) for x in re.split(r"[.\-]", m.group(2)))
    except ValueError:
        return n, None
    return series, nums


# 世代比较只取版本号的**前两位**（主.次）。
#
# 为什么必须截断（2026-09-02 实测）：`claude-haiku-4-5-20251001` 解析出
# (4, 5, 20251001)，而 `claude-opus-5` 是 (5,)。逐位比较时 (4,5,20251001)
# 与 (5,) 比第一位就分出胜负 —— 那一步是对的；但同产品线内
# `claude-haiku-4-5-20251001` (4,5,20251001) 与假想的 `claude-haiku-4-5`
# (4,5) 比时，日期戳会让带戳的那个「更新」，而它们其实是同一款。
#
# 取前两位后 (4,5,20251001) 与 (4,5) 相等，两个都保留 —— 那正是想要的：
# 同一世代的不同写法都留下，让 CPA 自己去匹配。
#
# 缺位补 0：`claude-opus-5` (5,) → (5, 0)，于是 `claude-opus-5-1` (5,1) 更新。
# 这与语义一致 —— 5.1 是 5 的后续小版本。
def generation(version: tuple[int, ...] | None) -> tuple[int, int] | None:
    """版本元组 → 可比较的世代 (主, 次)。None 表示无从比较。

    次版本缺失补 0（`(5,)` → `(5, 0)`）—— **不要改这个**。
    `topup_to_market_top` 的阶段 C 靠它判「`claude-opus-5` 是
    `claude-opus-5-5` 的旧次版本」，`tests/test_full_redetect.py` 与
    `tests/test_planning_compliance.py` 共四条断言锁着那条行为。

    「没标次版本不等于 0」这个更细的口径只在
    `newest_generation_per_line` 的阶段 B 里需要（同主版本内的**同代兄弟**
    不该互相淘汰），那里用 `_minor_is_explicit` 局部处理，不动这里的全局
    语义 —— 两处要的判据不同，共用一个会破坏另一条。
    """
    if not version:
        return None
    padded = (version + (0, 0))[:2]
    return (padded[0], padded[1])


def _minor_is_explicit(name: str) -> bool:
    """这个名字的版本号里**显式写了**次版本吗。

    `gpt-6.1-sol` → True（(6, 1)）；`gpt-6-astra` → False（(6,)）。

    为什么需要它（2026-09-30，本机实跑 CPA 权威名录）
    ---------------------------------------------
    `generation()` 把缺失的次版本补成 0，于是阶段 B 里
        gpt-6-astra → (6, 0)   gpt-6.1-sol → (6, 1)
    同产品线相比 (6,0) < (6,1)，`gpt-6-astra` / `gpt-6-luna` 被判低世代
    淘汰。codex 段 8 个放行名最后只剩 `gpt-6.1-sol` 一个 —— 违反要求②
    「gpt-6 系列**所有**模型名称」与要求③「同级系列全勾」。

    但站方写 `gpt-6` 是**没标次版本**，不是「第 6.0 版」。而
    `gpt-5.5` vs `gpt-5.6-luna` 两边都显式标了次版本，那是真实的世代差，
    必须照旧淘汰（`tests/test_probe.py:780` 等四条断言守着）。

    所以判据是「两边都显式标了才比次版本」，见 `_gen_supersedes`。
    """
    _series, version = series_and_version(bare_name(str(name or "")))
    return bool(version) and len(version) > 1


def _gen_supersedes(win: str, lose: str,
                    gw: tuple[int, int], gl: tuple[int, int]) -> bool:
    """在阶段 B 的同产品线比较里，`win` 是否**淘汰** `lose`。

    两级判据（2026-09-30 定为两级，一级不够）
    ------------------------------------
      · **主版本**在粗产品线（`_product_line`）内比 —— `gpt-4o` 不得与
        `gpt-6-sol` 并存这条老教训靠它，与分组键同粒度。
      · **次版本**只在**同一条变体线**（`_generation_line`，即
        `series_and_version` 的完整模板）内比，且两边都显式标了次版本。

    为什么次版本要降到变体线（本机实跑，CPA 权威名录 codex 段 8 个放行名）

        gpt-6-astra  (6,)   线 gpt-*-astra
        gpt-6-sol    (6,)   线 gpt-*-sol
        gpt-6.1-sol  (6,1)  线 gpt-*-sol
        gpt-5.5      (5,5)  线 gpt-*
        gpt-5.6-luna (5,6)  线 gpt-*-luna

      · 在粗线 `gpt` 里比次版本 → `gpt-6.1-sol` 把 astra / luna 一起淘汰，
        codex 段只剩 1 个模型，违反要求②「gpt-6 系列**所有**模型名称」。
      · 完全不比次版本 → `gpt-6-sol` 与 `gpt-6.1-sol` 并存（同一条变体线
        的新旧两代都写进 config.yaml），也违反要求③「只取最高级」。
      · 降到变体线：`gpt-6.1-sol` 淘汰 `gpt-6-sol`（同线，两边都标了次版本
        ——`(6,)` 没标，见下），astra / luna 各自成线不受影响。✓

    `gpt-5.5` vs `gpt-5.6-luna` 是跨变体线（`gpt-*` vs `gpt-*-luna`），
    按本判据不比次版本，但 `tests/test_probe.py:780` 要求 5.5 被淘汰 ——
    那条由**阶段 A**（族内比主版本）之后的 `_stale_across_lines` 兜住，
    见阶段 B 后面那一段。

    次版本「两边都显式标了才比」：站方写 `gpt-6` 是没标次版本，不是
    「第 6.0 版」。`generation()` 为别处的需要把它补成 0，这里用
    `_minor_is_explicit` 还原真相。
    """
    if gw[0] != gl[0]:
        return gw[0] > gl[0]
    if _generation_line(win) != _generation_line(lose):
        return False
    # 同一条变体线内：站方自己在这条线上标了更高的次版本，说明它确实发了
    # 新版（`gpt-6-sol` → `gpt-6.1-sol`），旧的那个出局。
    # 没标次版本的一方按 `generation()` 的补 0 参与比较 —— 在**同线内**
    # 那个补 0 是对的：同一条线上「gpt-6-sol」与「gpt-6.1-sol」就是先后两版。
    # 跨线时才不能补（`gpt-6-astra` 与 `gpt-6.1-sol` 是两个并行型号），
    # 上面那道线相等的闸已经把跨线的情形挡住了。
    return gw[1] > gl[1]


def newest_generation_per_line(names: list[str], *,
                               keep_low_tier: bool = False) -> list[str]:
    """只留最高档：先按族比主版本，再按产品线比完整世代。

    **这是选型的唯一生产入口**（2026-09-14 标注）——`plan.py` 的三个调用点
    与 `server.py:1456` 的界面预勾都走它。同名的 `newest_per_series` 只剩
    测试在用，改那个不影响任何实际行为。

    两阶段（2026-09-12 定案，用户裁定的三个判例 + docx 第 4 条）
    ------------------------------------------------------
    阶段 A —— 按**族**（`generation_family`）比**主版本**。
        族内出现了更高的主版本，整族的低版本全部出局。
        这实现 docx 的「codex 当前最高为 gpt-6 系列所有模型名称」：
        gpt-6 出现时 gpt-5.6 那一代全走，不管它挂在哪条产品线上。

    阶段 B —— 同族内再按**产品线**（`_product_line`）比**完整世代**
        （主.次），该世代的所有变体全部保留。
        这实现「所有相同等级系列的模型全部都要勾选上」，也就是用户点名的
        「勾了 gpt-5.6 却没勾 gpt-5.6-sol 这种重大失误」的反面。

    为什么必须是两阶段，单独任何一阶段都不行
    ----------------------------------
    只按族比完整世代 → claude-opus-5 (5,0) / claude-sonnet-5 (5,0) /
        claude-fable-5-1 (5,1) 同族，`max` 取 (5,1) → **只剩 fable**，
        opus 与 sonnet 被 fable 的小版本号挤掉。而用户要求这三个都留。
        所以次版本只在**产品线内**比。

    只按产品线比 → `gpt-6` 与 `gpt-5.6-codex` 是两条线（`_LINE_STRIP`
        剥不掉 `-codex`），各自取最高代，5.6-codex 留下 → 违反
        「codex 当前最高为 gpt-6 系列」。所以主版本要在**族内**比。

    o 系列与 mini 档
    --------------
    阶段 A 用 `generation_family` 而不是 `family`：o 系列自成一族，
    于是 `gpt-5.6` 不会挤掉 `o3`（用户判例一）。

    mini / nano / lite 在**进入比较之前**就被 `is_low_tier` 剔除
    （用户 2026-09-12：「凡是模型名称中带 mini 的就算版本很高也不应该勾选」）。
    必须在这里也做一遍、不能只靠 `section_allows`：本函数的调用方多数直接喂
    站方目录的原始名单（plan.py:2347 的 catalog 分支、:2433 的合并块、
    server.py:1428 的界面预勾），那几条路上没有 `section_allows` 这一关。
    顺带也让判例二、三成立 —— `o4-mini` 出局，于是它挤不掉 `o3` / `o3-pro`。

    `keep_low_tier=True` 只给**手填**路径用（plan.py 的 forced_models）：
    手填是操作员的显式意图，选型偏好不该拦它 —— 与四族之外的名字走
    `section_protocol_ok` 是同一条原则。

    这是用户 2026-09-02 规则的准确形态，`newest_per_series` 不够：

        目录 = gpt-4o, gpt-5.1, gpt-5.5, gpt-5.6-luna, gpt-5.6-terra

    按「系列」分组时 `gpt-5.5` 的系列是 `gpt-*`，而 luna / terra 各自是
    `gpt-*-luna` / `gpt-*-terra` —— 三个独立系列，5.5 没有对手所以留下；
    `gpt-4o` 更直接：旧正则不认 `4o` 是版本，它自成一系永远保留。
    现场截图里 codex 段勾着 gpt-4o 与 gpt-5.5 就是这两件事叠加。

    按「产品线」分组则四个都在 `gpt` 线上，最高世代 (5,6) →
    只留 gpt-5.6-luna / gpt-5.6-terra，4o / 5.1 / 5.5 全丢。

    产品线内无任何可比版本时（全是 `o1` 这种）整组保留 —— 无从比较不淘汰。
    顺序按输入首次出现，保证同一批输入两次运行结果一致（diff 可复核）。
    """
    # 认不出版本的名字一律不留（2026-09-11，用户明确要求，所有类型一视同仁）
    # --------------------------------------------------------------------
    # 现场实证：codex 段目录里 `gpt-reserve` 与 `gpt-6` / `gpt-6-astra` 同时
    # 被勾上，而该型号并不存在。这与遗留的 `claude-fake-5` 是同一形态：名字
    # 合法（`name_is_safe` 只挡非法字符，挡不住「合法但不存在」），却没有任何
    # 版本信息可供比较。用户口径：**没有版本号就等于低等级模型，不能保留**。
    #
    # 代价与兜底：若某站**全部**模型都无版本号，这一段的清单会被清空。那时
    # `plan.py` 的来源链会依次退到目录 / 兜底清单，界面也会显示「站方目录也没
    # 报模型：手填模型名」，操作员手填的名字走 `manual` 来源、不受本规则约束
    # （手填是显式意图，见 plan.py 的 forced_models 分支）。
    #
    # 提前到这里做（原来在函数末尾再滤一遍）：无版本的名字不参与下面两阶段的
    # `max`，否则它们会各自占一条产品线、干扰不到结果却让代码难读。
    cand: list[tuple[str, tuple[int, int]]] = []
    for n in names:
        if not n:
            continue
        # gemini 段的 pro 约束在这里也要生效：flash / flash-lite 与 pro 同族
        # 同线，不挡掉的话它们会参与世代比较，反把 pro 挤下去。
        if family(n) == "gemini" and not gemini_pro_ok(n):
            continue
        # 降级档：带 mini / nano / lite 的一律不参与（也就不会被选中）。
        # 手填路径传 keep_low_tier=True 跳过这一条，见 docstring。
        if not keep_low_tier and is_low_tier(n):
            continue
        g = generation(series_and_version(n)[1])
        if g is None:
            continue
        cand.append((n, g))

    # ── 阶段 A：族内比主版本 ──
    top_major: dict[str, int] = {}
    for n, g in cand:
        fam = generation_family(n)
        if fam not in top_major or g[0] > top_major[fam]:
            top_major[fam] = g[0]
    cand = [(n, g) for n, g in cand if g[0] == top_major[generation_family(n)]]

    # ── 阶段 B：产品线内比完整世代，该世代的变体全留 ──
    #
    # 分组键仍用 `_product_line`（粗线）—— 那是 2026-09-02 从「同系列取最新」
    # 改过来的，为的是让 `gpt-5.5` 与 `gpt-5.6-luna` 能互相比较（按系列分组时
    # 三者各自成系列，5.5 没有对手所以留下 —— 现场截图里 codex 段勾着
    # `gpt-4o` 与 `gpt-5.5` 就是这个）。那条语义有 `tests/test_probe.py:780`
    # 与 `tests/test_web.py` 三条断言守着，不能动。
    #
    # 真正的 bug 在**世代的比较口径**，不在分组（2026-09-30 定位）：
    # `generation()` 原来把缺失的次版本补成 0，于是
    #     gpt-6-astra  → (6, 0)
    #     gpt-6.1-sol  → (6, 1)
    # 同线相比 (6,0) < (6,1)，`gpt-6-astra` / `gpt-6-luna` 被判成低世代淘汰。
    # 但站方写 `gpt-6` 是**没标次版本**，不是「第 6.0 版」—— 把它当 0 就等于
    # 替站方声明了一个它没说过的版本号。
    #
    # 本机实跑（2026-09-30，CPA 权威名录 90 个，codex 段放行 8 个）：
    #     gpt-6-luna gpt-6-astra gpt-6-sol gpt-6.1-sol
    #     gpt-5.6-sol gpt-5.6-luna gpt-5.6-terra gpt-5.5
    # 修前只选出 `gpt-6.1-sol` 一个 —— 违反要求②「gpt-6 系列**所有**模型
    # 名称」与要求③「同级系列全勾」。
    #
    # 现在比较交给 `_gen_supersedes`（局部判据，不动 `generation` 的全局
    # 补 0 语义 —— `topup_to_market_top` 的阶段 C 依赖那个补 0 判
    # `claude-opus-5` 是 `claude-opus-5-5` 的旧次版本，有四条断言锁着）：
    # 主版本照常比（4 < 5 < 6 仍然淘汰），次版本只在**两个名字都显式标了**
    # 的时候比（5.5 < 5.6 仍然淘汰；6 与 6.1 互不淘汰、同时保留）。
    #
    # 「一个名字被本线里任何一个名字淘汰」才出局 —— 不能先算出线内「最高
    # 世代」再按相等筛：淘汰关系在这里不是全序（6 与 6.1 互不淘汰），
    # 取 max 会得到一个依赖遍历顺序的结果。
    keep = set()
    by_line: dict[str, list[tuple[str, tuple[int, int]]]] = {}
    for n, g in cand:
        by_line.setdefault(_product_line(n), []).append((n, g))
    for _line, members in by_line.items():
        for n, g in members:
            if not any(_gen_supersedes(other, n, og, g)
                       for other, og in members if other != n):
                keep.add(n)

    # ── 阶段 B2：跨变体线的**旧次版本**也要淘汰 ──
    #
    # 阶段 B 把次版本比较降到了变体线（否则 `gpt-6.1-sol` 会把
    # `gpt-6-astra` 一起淘汰，见 `_gen_supersedes`）。代价是跨变体线的
    # 旧次版本漏了出去：
    #
    #     gpt-5.5       线 gpt-*        (5, 5)
    #     gpt-5.6-luna  线 gpt-*-luna   (5, 6)
    #     两者不同变体线 → 阶段 B 不比 → 5.5 留下
    #
    # 而 `tests/test_probe.py:780`（现场截图那组）要求 `gpt-5.5` 被淘汰：
    # 按系列分组时 5.5 没有对手所以留下，正是 2026-09-02 要修掉的形态。
    #
    # 判据：**同粗产品线、同主版本**里出现了更高的**显式**次版本，则显式
    # 标了更低次版本的名字出局。三个限定词都必要：
    #   · 同粗线 —— gpt 与 claude 不互比（族已在阶段 A 分开，这里再保一层）
    #   · 同主版本 —— 跨主版本归阶段 A（那里有实测豁免等另一套裁定）
    #   · 显式次版本 —— `gpt-6-astra` 的 `(6,)` 没标次版本，不参与，
    #     否则它会被 `gpt-6.1-sol` 淘汰，回到修前的形态
    top_minor: dict[tuple[str, int], int] = {}
    for n in keep:
        if not _minor_is_explicit(n):
            continue
        g = generation(series_and_version(bare_name(n))[1])
        if g is None:
            continue
        k = (_product_line(n), g[0])
        if k not in top_minor or g[1] > top_minor[k]:
            top_minor[k] = g[1]
    for n in list(keep):
        if not _minor_is_explicit(n):
            continue
        g = generation(series_and_version(bare_name(n))[1])
        if g is None:
            continue
        if g[1] < top_minor.get((_product_line(n), g[0]), g[1]):
            keep.discard(n)

    # ── 阶段 C：同一基名的「算力档后缀」只留最高档（2026-09-30 加）──
    #
    # 用户规则④原话：「可以规避如下截图所示**部分高、低模型同时存在**没有
    # 就高选择模型…等诸多 BUG 问题」。现场（投喂台快照 2026-09-19）：gemini
    # 段种子兜底一次勾了 7 个，里面 `gemini-3.1-pro-high` 与
    # `gemini-3.1-pro-low` 并存 —— 同一个基名 `gemini-3.1-pro` 的高低两档
    # 同时写进 config.yaml，正是那句话点名要规避的形态。生产 config.yaml
    # 也已落进这个结果（`romeo.example` 的 gemini 段同时有 pro 与 pro-low）。
    #
    # 为什么不能把 `low` 直接塞进 `_LOW_TIER`（那是上一版的做法，被否）
    # -------------------------------------------------------------
    # `_LOW_TIER` 是**无条件**排除：命中就永不入选。而 `-low` 与 `mini` /
    # `flash` 的性质不同 —— `mini`/`flash` 是独立的廉价型号（另一条产品线），
    # `-low` 只是同一个模型的**推理算力档**（high/medium/low 是 reasoning
    # effort 后缀）。若无条件排除，当某站目录里**只有** `gemini-3.1-pro-low`
    # 这一个变体时，该段会变成 0 个模型 —— 直接撞上红线 2「严禁出现不勾选」
    # 与症状 C（「实测 0 个 · 已勾 0」）。
    #
    # 所以判据是**相对**的：同基名有更高档时才压掉低档，没有就保留。
    # 这同时满足规则③「所有相同等级系列的模型全部都要勾选上」——
    # 被压掉的不是「同等级的另一个型号」，而是「同一个型号的低算力档」。
    #
    # 档序取自后缀而非硬编码型号名（红线 3 禁硬编码）：数字越大越高，
    # 具名档按 high > medium > low 排，无后缀（裸基名）视为最高档 ——
    # 裸 `gemini-3.1-pro` 就是站方的默认档，永远不该被自己的 `-low` 顶掉。
    # 实现搬到 `collapse_effort_tiers`（2026-09-30）—— 见那个函数的说明：
    # `plan.py` 的族/档次终检也要走同一条收敛，两处不能各写一份。
    keep = set(collapse_effort_tiers(sorted(keep)))

    # 按输入顺序输出，不按分组顺序 —— 调用方（rank_models）之后还要排序，
    # 但保持输入序让「没排序时也可复核」成立。
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n in keep and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def collapse_effort_tiers(names: list[str]) -> list[str]:
    """同一基名的算力档只留最高档。保序去重，其余名字原样保留。

    为什么必须是**独立的公开函数**（2026-09-30）
    ------------------------------------------
    这条收敛原来只写在 `newest_generation_per_line` 的阶段 C 里，而
    `plan.py` 的「族 / 档次终检」走的是 `section_family_violations` ——
    那是**逐条**判据（这个名字属不属于这个段、是不是降级档），看不见
    「同基名还有更高档」这种**相对**关系。于是所有绕过
    `newest_generation_per_line` 的路径（seed 兜底、`merged or v.models`
    回退、应急 topup）都能把 `-low` 原样带到落盘。

    本机实跑复现（2026-09-30，accounts.txt → 某站 gemini 段）：
    方案里 `gemini-3.1-pro` 与 `gemini-3.1-pro-low` 并存且双双预勾 ——
    正是规则④ 点名要规避的「部分高、低模型同时存在没有就高选择」。
    阶段 C 的注释当时声称已修掉 gemini 段 `-high`/`-low` 并存，
    实际只覆盖了走那个函数的那条路径。

    判据仍是**相对**的（与阶段 C 同一套理由，不重复展开）：
      · 同基名有更高档 → 压掉低档；
      · 某站只提供 `-low` → 原样保留（否则该段清空，撞红线 2）；
      · 裸基名视为最高档（站方默认档不该被自己的 `-low` 顶掉）；
      · 版本号不是算力档（`claude-opus-5` 的 `5`），见 `_EFFORT_SUFFIX`。
    """
    src = [str(n) for n in (names or []) if n]
    by_base: dict[str, list[str]] = {}
    for n in src:
        base, _rank = _effort_split(n)
        by_base.setdefault(base, []).append(n)

    drop: set[str] = set()
    for _base, variants in by_base.items():
        if len(variants) < 2:
            continue
        best = max(_effort_split(v)[1] for v in variants)
        for v in variants:
            if _effort_split(v)[1] < best:
                drop.add(v)

    seen: set[str] = set()
    out: list[str] = []
    for n in src:
        if n in drop or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def newest_per_series(names: list[str]) -> list[str]:
    """同系列只留最新版，但同版本的所有变体都保留。顺序按输入首次出现，便于复核 diff。

    **生产路径不走这个函数**（2026-09-14 核实）
    -----------------------------------------
    全项目只有 `tests/test_probe.py:841` 在调它，用于守「同版本时裸名优先于
    带前缀的」这一条语义。真正决定「界面上勾哪些模型」的是
    `newest_generation_per_line` —— 那个函数按「族比主版本、产品线比完整世代」
    两阶段筛，`plan.py` 与 `server.py` 的四个调用点全指向它。

    改这里**不会**改变任何实际行为。要调整选型规则请改
    `newest_generation_per_line`，别改这个。

    保留它的理由：它守着的那条语义（带前缀与裸名的取舍）与选型规则正交，
    将来若有别的调用方需要「只按系列去旧、不做世代折叠」，这份实现是对的。

    用户 2026-09-02 的要求：「相同系列模型以最新版为准，如内置 gpt-5.6，
    未来 CPA 可能更新 gpt-5.7，这个时候以最新的出现，旧的不放入」。

    用户 2026-09-13 补充：「同级模型（如 gpt-5.6, gpt-5.6-sol, gpt-5.6-nxt）
    应该全部勾选，不能只留第一个」—— 同版本的后缀变体不互相淘汰。
    （这条要求在生产路径上由 `newest_generation_per_line` 的阶段 B 满足，
     实测 `['gpt-5.6','gpt-5.6-sol','gpt-5.6-nxt']` 三个全留。）

    版本认不出的（`gpt-4o`）自成一系，永远保留 —— 无从比较就不淘汰，
    宁可多留一个也不要因为解析不出版本号而丢掉一个可用模型。

    带 provider 前缀的名字（`anthropic/claude-opus-5`）与裸名同系列：
    系列键取 `bare_name` 之后的形态，所以两者会互相比较，**裸名优先**。
    理由：前缀是站方特有的写法（`anthropic/` 只在某几个站成立），
    而这个函数的产物会当作「通用最新清单」用到别的站上。

    实现细节（2026-09-13）：
    - 按系列分组（series_and_version 返回的系列键）
    - 每组内按版本比较：只保留最高版本
    - **同版本的所有变体都保留**（这是关键修复点）
      例如：gpt-5.6-sol 与 gpt-5.6-nxt 系列键不同但版本相同 (5,6)
      → 都保留，不互相淘汰
    - 同版本中裸名优先于带前缀的
    """
    # 第一遍：按系列分组，收集每个系列的所有候选
    groups: dict[str, list[tuple[tuple[int, ...] | None, str]]] = {}
    order: list[str] = []

    for n in names:
        if not n:
            continue
        series, ver = series_and_version(n)
        if series not in groups:
            groups[series] = []
            order.append(series)
        groups[series].append((ver, n))

    # 第二遍：每组内只保留最高版本，但同版本的所有变体都保留
    result = []
    for series in order:
        candidates = groups[series]

        # 找出最高版本
        max_ver = None
        for ver, _ in candidates:
            if ver is not None:
                if max_ver is None or ver > max_ver:
                    max_ver = ver

        # 保留所有最高版本的条目（包括同版本的多个变体）
        kept = []
        for ver, name in candidates:
            if ver is None and max_ver is None:
                # 无版本信息，全部保留
                kept.append(name)
            elif ver == max_ver:
                # 最高版本，保留
                kept.append(name)
            # 否则：旧版本，丢弃

        # 同版本中裸名优先：同一模型（bare_name 相同）只保留裸名、去掉带前缀的。
        #
        # 这是两种不同的情况，不能混：
        #   · `anthropic/claude-opus-5` 与 `claude-opus-5` —— bare_name 相同，
        #     是**同一模型**的两种写法，只留裸名（既有测试守着这条语义）。
        #   · `gpt-5.6-sol` 与 `gpt-5.6-nxt` —— bare_name 不同，是**同级变体**，
        #     用户 2026-09-13 要求全部保留，一个都不能丢。
        dedup: dict[str, str] = {}
        order_kept: list[str] = []
        for name in kept:
            b = bare_name(name)
            if b in dedup:
                # 已有同模型；若已有带前缀而新的是裸名，换成裸名
                if "/" in dedup[b] and "/" not in name:
                    dedup[b] = name
            else:
                dedup[b] = name
                order_kept.append(b)
        result.extend(dedup[b] for b in order_kept)

    return result


# 降级档标记。同族里它们排在主力款之后 —— 不排除（有些站只卖这些），
# 但取前 N 个时不该让它们挤掉 opus / pro 那一档。
_WEAK = re.compile(r"-(?:mini|lite|fast|spark|haiku|low|extra-low)(?:$|[-.])")

# 变体标记：同一款模型的地区版 / 特化版 / 时间戳版。它们能用，但不是首选。
#
# 实测（2026-09-02，用户的 config.yaml + CPA 名录合并后）：claude 段排前四名
# 是 opus-5 / opus-5-thinking / opus-4-8-m-aws / fable-5-1 —— 三个都是 opus
# 的变体，把 sonnet 挤出去了。用户要的是「opus / fable / sonnet 三条产品线」，
# 不是 opus 的四种写法。
_VARIANT = re.compile(
    r"-thinking(?:$|[-.])"       # claude-opus-5-thinking
    r"|-m-aws(?:$|[-.])"         # claude-opus-4-8-m-aws：AWS 托管版
    r"|-agent(?:$|[-.])"         # gemini-pro-agent
    r"|-latest(?:$|[-.])"        # gemini-pro-latest：滚动别名，指向不确定
    r"|-\d{8}(?:$|[-.])"         # claude-haiku-4-5-20251001：时间戳版
)

# 主力款优先级。数字越小越靠前。
#
# 为什么需要这个而不只按版本排（2026-09-02 实测）：三层合并后 claude 段前 6 名
# 是 haiku-4-5-20251001 / sonnet-5 / opus-5 / fable-5-1 / 3-7-sonnet /
# 3-5-haiku —— 版本号最高的 opus-5 排第三，而 haiku（最弱那档）排第一，
# 因为它在 CPA 名录里出现得早。取前 6 就把主力款和废弃款混在一起。
_TIER_HINTS = (
    (re.compile(r"opus"), 0),
    (re.compile(r"-pro(?:$|[-.])"), 0),
    (re.compile(r"^gpt-\d"), 0),          # gpt-5.6 这类正牌
    (re.compile(r"^kimi-k\d"), 0),
    (re.compile(r"fable"), 1),
    (re.compile(r"sonnet"), 2),
    (re.compile(r"flash"), 3),
    (re.compile(r"haiku"), 4),
)


def _tier(name: str) -> int:
    n = bare_name(name)
    for rx, t in _TIER_HINTS:
        if rx.search(n):
            return t
    return 2                                  # 认不出的放中间


def rank_models(names: list[str]) -> list[str]:
    """按「该优先注册哪个」排序。强 → 弱。

    判据依次：
      1. 无 provider 前缀优先 —— 前缀是站方特有写法，用到别的站上会失配
      2. 主力款优先（opus / pro / 正牌 gpt / kimi-k* 在前，haiku / flash 在后）
      3. 非变体优先（-thinking / -m-aws / -latest / 时间戳版往后）
      4. 版本号降序 —— 同一档里新版在前；**认不出版本的排最后**
      5. 非降级档优先（-mini / -lite / -fast 往后）
      6. 名字字典序 —— 兜底，保证同一批输入两次运行结果一致（diff 可复核）

    为什么排序必须与截取分开：`newest_per_series` 只做「同系列去旧」，
    不同系列之间它无从取舍。取前 N 个时若按输入顺序，就会出现「弱模型
    排在前面」—— 实测 claude 段前三名是 haiku / sonnet / opus，正好倒过来。

    为什么认不出版本的要排最后（2026-09-02 实测）：空元组在 Python 里小于
    任何非空元组，而版本是取负值降序的 —— 于是 `gemini-pro-agent`（无版本）
    会排在 `gemini-3.1-pro`（版本 (3,1) → (-3,-1)）之前，因为 `() < (-3,-1)`。
    无从判断新旧的名字不该抢主力款的位置。
    """
    def key(n: str):
        _series, ver = series_and_version(n)
        # 版本降序：取负值。多段版本长度不同（(5,) vs (4,5,20251001)），
        # 逐位比较即可 —— (5,) > (4, 5) 在 Python 里成立。
        # 无版本用 (0, ()) 排到有版本 (−1, …) 之后。
        vkey = (-1, tuple(-x for x in ver)) if ver else (0, ())
        # 带 provider 前缀的排到最后。这份清单会当作「通用最新清单」用到
        # **别的站**上，而 `anthropic/xxx`、`Business/xxx` 是特定站的写法 ——
        # 猜得越具体，猜错的概率越高。有裸名可用时不该拿它去赌。
        prefixed = 1 if "/" in (n or "") else 0
        bare = bare_name(n)
        return (prefixed, _tier(n), 1 if _VARIANT.search(bare) else 0,
                vkey, 1 if _WEAK.search(bare) else 0, bare)
    return sorted([n for n in names if n], key=key)


def _round_robin(names: list[str], limit: int, keyfn) -> list[str]:
    """按 keyfn 分组后轮转取 limit 个。组内顺序沿用输入（已排过序）。

    为什么要轮转而不是直接取前 N（2026-09-02 实测）：排序把同一条产品线的
    各种变体排在一起，取前 4 就成了「opus-5 / opus-5-thinking /
    opus-4-8-m-aws / fable-5-1」—— 四个里三个是 opus。用户要的是覆盖
    「opus / fable / sonnet」这几条**不同的产品线**。

    轮转让每组先出一个，再回头取第二个。组的次序按每组第一个元素的排名，
    所以最强的那条线仍然第一个出。
    """
    buckets: dict[str, list[str]] = {}
    order: list[str] = []
    for n in names:
        k = keyfn(n)
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(n)
    out: list[str] = []
    i = 0
    while len(out) < limit and any(buckets[k] for k in order):
        k = order[i % len(order)]
        if buckets[k]:
            out.append(buckets[k].pop(0))
        i += 1
    return out


# ---------------- 第 1 层：CPA 权威名录（在线） ----------------

# CPA 自己的 model_updater.go 就是拉这两个地址（internal/registry/
# model_updater.go:22-25），第二个是镜像。用同一份数据源的理由：
# 那是 CPA **实际认识**的模型集合。写一个 CPA 不认识的名字进 config.yaml，
# 路由时会失配；而 CPA 新增支持时这份名录先更新，我们跟着就有了。
_CATALOG_URLS = (
    "https://models.router-for.me/models.json",
    "https://raw.githubusercontent.com/router-for-me/models/refs/heads/main/models.json",
)

# 与 cpa_source_probe 同一套缓存策略：成功 6 小时、失败 10 分钟。
# 失败也要缓存 —— 那个模块的教训（2026-09-02）：只缓存成功等于让拉不通的
# 环境每次都重付一遍超时。
_TTL_OK = 6 * 3600
_TTL_BAD = 600
_cache: dict = {"at": 0.0, "names": None, "ok": False, "why": ""}


# ---------------- 第 1.5 层：上次拉通的名录（落盘） ----------------
#
# 2026-10-01 新增。动因是「严禁硬编码」这条要求与「段里不许有未定项」这条
# 要求的冲突：远程名录拉不通时，原来直接掉到第 3 层 `FALLBACK_MODELS`，
# 那是一张**写死在源码里、会随时间过期**的清单 —— 过期的表现是「填进
# config.yaml 的模型 CPA 每次轮到都失败」，界面上却看着有值，比缺模型更
# 难发现。
#
# 这一层把每次**成功**的远程名录落到磁盘；之后远程不通时先读它。
# 于是写死的那一层退化为「这台机器从来没拉通过一次」时的最后兜底，
# 而不是国内 VPS 的日常路径（直连 GitHub 不通是常态）。
#
# 落盘位置优先 `IMPORTER_BACKUP_DIR` —— 两个 compose 都把它挂成**命名卷**，
# 容器重启后还在；/tmp 会被清掉，等于没存。目录不可写就静默跳过：
# 这是加速层，不是功能依赖，不能让它把探测搞崩（2026-09-27 落盘日志
# 那次 PermissionError 崩溃就是这么来的，见提交 3ceae78）。
_DISK_CACHE_NAME = "model-catalog-last-good.json"
# 落盘名录的保鲜期。超过它仍然用（比硬编码新），但在 why 里标出年龄，
# 让界面能说清「这是 N 天前的名录」而不是假装是实时数据。
_DISK_STALE_AFTER = 7 * 86400


def _disk_cache_path() -> str | None:
    import tempfile
    for base in (os.environ.get("IMPORTER_CATALOG_CACHE_DIR"),
                 os.environ.get("IMPORTER_BACKUP_DIR"),
                 os.path.dirname(os.environ.get("IMPORTER_CONFIG", "") or "") or None,
                 tempfile.gettempdir()):
        if not base:
            continue
        try:
            if os.path.isdir(base) and os.access(base, os.W_OK):
                return os.path.join(base, _DISK_CACHE_NAME)
        except OSError:
            continue
    return None


def _disk_cache_save(names: list[str]) -> None:
    path = _disk_cache_path()
    if not path or not names:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"at": time.time(), "names": names}, fh)
        os.replace(tmp, path)          # 原子替换，避免读到半截文件
    except OSError:
        pass                            # 加速层，失败不影响功能


def _disk_cache_load() -> tuple[list[str], float]:
    """回 (名字列表, 落盘时间戳)。读不到回 ([], 0.0)。"""
    path = _disk_cache_path()
    if not path:
        return [], 0.0
    try:
        with open(path, encoding="utf-8") as fh:
            got = json.load(fh)
        names = got.get("names")
        if isinstance(names, list):
            clean = [n for n in names if isinstance(n, str) and n.strip()]
            if clean:
                return clean, float(got.get("at") or 0.0)
    except (OSError, ValueError, TypeError):
        pass
    return [], 0.0


def _http_json(url: str, *, timeout: int, proxy: str | None):
    req = urllib.request.Request(url, headers={
        # GitHub 对无 UA 的请求会 403
        "User-Agent": "cpa-upstream-importer/model-catalog",
        "Accept": "application/json, */*",
    })
    if proxy:
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        op = urllib.request.build_opener()
    with op.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def remote_names(*, timeout: int = 8, proxy: str | None = None,
                 use_cache: bool = True) -> tuple[list[str], str]:
    """拉 CPA 权威名录里的全部模型 id。返回 (名字列表, 失败原因)。

    两个地址**并发**试，先返回的可用结果即采用。全失败时退到落盘的
    上次成功名录（第 1.5 层）；连它也没有才返回 ([], 原因)，由调用方
    掉到 `FALLBACK_MODELS`。

    超时 8 秒与 cpa_source_probe 对齐：这个调用可能出现在探测路径上，
    而国内 VPS 直连 GitHub 常不通 —— 长超时只会让整批探测变慢。

    2026-10-01 改并发：原来两个地址**依次**试，各 8 秒，全不通的最坏
    用时是 16 秒。而 `server._api_context` 在请求路径上同步调它，于是
    容器刚起、缓存冷的那一次 `/api/context` 要等 16 秒才回 —— 首屏白屏
    / 黑屏，刷新一次就好了（第二次吃失败缓存）。并发后最坏 8 秒，
    且两个地址里只要有一个通就按它的真实耗时返回。
    """
    now = time.time()
    if use_cache and _cache["names"] is not None:
        ttl = _TTL_OK if _cache["ok"] else _TTL_BAD
        if now - _cache["at"] < ttl:
            return list(_cache["names"]), _cache["why"]

    def _names_of(data) -> list[str]:
        names: list[str] = []
        # 结构：{provider: [{id, object, ...}, ...]}。只取 id，provider 分组
        # 对我们没意义 —— 段的归属由 section_allows 按名字判，不按 provider。
        if isinstance(data, dict):
            for lst in data.values():
                if not isinstance(lst, list):
                    continue
                for m in lst:
                    if isinstance(m, dict) and isinstance(m.get("id"), str):
                        mid = m["id"].strip()
                        if mid and mid not in names:
                            names.append(mid)
        return names

    def _one(url: str) -> tuple[str, list[str], str]:
        try:
            data = _http_json(url, timeout=timeout, proxy=proxy)
        except Exception as e:                          # noqa: BLE001
            return url, [], f"{url.split('/')[2]}: {type(e).__name__}"
        names = _names_of(data)
        if names:
            return url, names, ""
        return url, [], f"{url.split('/')[2]}: 响应里没有模型 id"

    errors: list[str] = []
    best: list[str] = []
    try:
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=len(_CATALOG_URLS),
                                    thread_name_prefix="catalog") as ex:
            futs = {ex.submit(_one, u): u for u in _CATALOG_URLS}
            for fut in _cf.as_completed(futs, timeout=timeout + 4):
                _url, names, err = fut.result()
                if names:
                    best = names
                    break                  # 先到先用，其余 future 自行结束
                if err:
                    errors.append(err)
    except Exception as e:                              # noqa: BLE001
        # 线程起不来（容器线程数吃紧）就退回串行，功能不能因此丢。
        errors.append(f"并发拉取失败（{type(e).__name__}），已退回串行")
        for url in _CATALOG_URLS:
            _url, names, err = _one(url)
            if names:
                best = names
                break
            if err:
                errors.append(err)

    if best:
        if use_cache:
            _cache.update(at=now, names=best, ok=True, why="")
        _disk_cache_save(best)
        return list(best), ""

    # 第 1.5 层：上次拉通的落盘名录。比写死的第 3 层新，优先用它。
    disk, disk_at = _disk_cache_load()
    if disk:
        age_d = max(0, int((now - disk_at) // 86400)) if disk_at else -1
        age = f"{age_d} 天前" if age_d >= 0 else "时间未知"
        why = ("远程名录拉不通（" + ("；".join(errors) or "未知原因")
               + f"），已改用落盘的上次成功名录（{age}，{len(disk)} 个模型）")
        if disk_at and now - disk_at > _DISK_STALE_AFTER:
            why += "；该名录已超过 7 天，请尽快恢复出网或配置代理"
        if use_cache:
            # 标 ok=False：它不是实时数据，10 分钟后应该再试一次远程。
            _cache.update(at=now, names=disk, ok=False, why=why)
        return list(disk), why

    why = "；".join(errors) or "未知原因"
    if use_cache:
        _cache.update(at=now, names=[], ok=False, why=why)
    return [], why


# ---------------- 第 3 层：内置兜底 ----------------

# 用户 2026-09-02 指定的清单。**只在前两层都拿不到时使用**。
#
# 为什么仍然要有写死的一层：远程名录拉不通（国内 VPS 直连 GitHub 不通是常态）
# 且 config.yaml 也是空的时候，段里必须有一份确定清单 —— 用户的硬要求是写进
# config.yaml 的参数不能有未定项，缺席比填错更难排查。
#
# 为什么不能只有这一层：写死的清单会过期，而过期的表现是「填进去的模型 CPA
# 每次轮到都失败」—— 与缺模型一样坏，却更难发现（界面上看着有值）。
#
# 2026-09-26 更新到 gpt-6 / claude-opus-5-5 这一代，并**两代并存**（规则 ④）。
#
# 名字只取生产 config.yaml 里真实出现过的，不凭空编。实测分布：
#     gpt-6-astra 55 · gpt-6-sol 6        ← 线顶（最新代，铺开得还少）
#     gpt-5.6-sol 45 · -terra 28 · -luna 27 · gpt-5.6 10   ← 次新代，实际在扛量
#     claude-opus-5-5 6                   ← 线顶
#     claude-opus-5 49 · claude-fable-5-1 28 · claude-sonnet-5  ← 次新代
#
# 为什么不能只留线顶：这一层只在**远程名录拉不通、本地配置也没有**时才用到
# （国内 VPS 常态）。那种情况下单写 gpt-6 系列，等于赌这个站已经开通了最新
# 一代 —— 而生产数据说最新代只铺了 6 条、次新代有 100+ 条。赌输的表现是
# 「填进去的模型 CPA 每次轮到都失败」，界面上却看着有值，比缺模型更难发现。
# 规则 ④ 原话：线顶保留 + 实测最高的次新代一并保留。
#
# 为什么不能只留次新代：那就是「不就高」，违反规则 ②。
#
# 这一层被用到时会打 WARNING（见 latest_models），提醒它可能已经过期。
FALLBACK_MODELS: dict[str, list[str]] = {
    "codex-api-key": [
        "gpt-6-astra", "gpt-6-sol",
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6",
    ],
    "claude-api-key": [
        "claude-opus-5-5", "claude-fable-5-1",
        "claude-opus-5", "claude-sonnet-5",
    ],
    "gemini-api-key": [
        "gemini-3.1-pro", "gemini-3.1-pro-high", "gemini-3.1-pro-preview",
        "gemini-3.1-pro-preview-search", "gemini-3.1-pro-preview-customtools",
        "gemini-3.1-pro-low",
    ],
    # compat 走 /chat/completions，四族都合法。给每族的头部型号 ——
    # 一个 provider 条目声明太多模型会拖慢 CPA 的模型注册，且多数用不上。
    # 这里同样两代并存，但**只给 gpt 与 claude 两族**：实测生产配置里
    # 只有这两族出现了明显的「线顶铺得少、次新代扛量」分布，gemini 与
    # kimi 的线顶就是在用的那一代，铺第二代纯属增加注册成本。
    "openai-compatibility": [
        "gpt-6-astra", "claude-opus-5-5", "gemini-3.1-pro", "kimi-k3",
        "gpt-5.6-sol", "claude-opus-5",
    ],
}


# ---------------- 第 2.5 层：本进程见过的新鲜目录 ----------------
#
# 远程名录拉不通（国内 VPS 常态）时，内置清单是唯一剩下的来源 —— 而它必然
# 过期。但同一轮探测里，**别的站**的 /v1/models 目录与实测通过的名字就是
# 此刻市面上真实在卖的东西，比写死的清单新得多。这里收集它们，作为
# 「远程名录 → 本地 config → 本轮目录 → 内置兜底」的第三层。
#
# 只收过了 `section_allows` 的名字（族、档次、字符都对），并且只在同一
# 进程内有效（重启清零），不落盘 —— 它是「此刻」的证据，不是长期知识。
import threading as _threading

_SEEN_LOCK = _threading.Lock()
_SEEN: dict[str, dict[str, float]] = {}
_SEEN_TTL = 6 * 3600


def note_catalog(section: str, names: list[str] | None) -> None:
    """把站方目录 / 实测通过的名字记进本进程的新鲜目录（线程安全）。"""
    if not names:
        return
    now = time.time()
    with _SEEN_LOCK:
        bucket = _SEEN.setdefault(section, {})
        for n in names:
            if isinstance(n, str) and section_allows(section, n):
                bucket[n] = now


def seen_catalog(section: str) -> list[str]:
    """本进程近 6 小时见过的、这个段合规的名字（最近的在前）。"""
    now = time.time()
    with _SEEN_LOCK:
        bucket = _SEEN.get(section) or {}
        live = [(t, n) for n, t in bucket.items() if now - t < _SEEN_TTL]
    return [n for _, n in sorted(live, reverse=True)]


def reset_seen_catalog() -> None:
    """测试用：清空本进程的新鲜目录。"""
    with _SEEN_LOCK:
        _SEEN.clear()


# ---------------- 三层合并 ----------------

def top_generation(names: list[str]) -> tuple[int, int] | None:
    """这批名字里的最高世代。全都认不出版本时返回 None。"""
    gens = [generation(series_and_version(n)[1]) for n in names if n]
    known = [g for g in gens if g is not None]
    return max(known) if known else None


def catalog_is_stale(section: str, catalog: list[str], *,
                     cfg: dict | None = None,
                     remote: list[str] | None = None) -> tuple[bool, str]:
    """站方目录的最高世代是否已落后于市面最新。返回 (是否落后, 说明)。

    为什么需要这个判断（2026-09-02 现场）
    ---------------------------------
    `romeo.example` 的 codex 段目录只有 `gpt-4` / `gpt-4-32k` / `gpt-4o` /
    `gpt-4o-mini` —— **四个都是世代 (4,0)**，于是「取最高世代」把四个全留下、
    还默认全勾。规则本身没错（那条线里 (4,0) 就是最高），但违反用户的意图：
    「最新模型如果探测出来是 gpt-5.6，那 gpt-4o、gpt-5.5 都不应该默认勾选」。

    为什么不直接改用市面最新清单（否决 A 方案）
    ------------------------------------
    那个站的目录里**没有** `gpt-5.6-sol` 这些名字。写进去 CPA 路由过去大概率
    404，等于把一个「有老模型可用」的站变成死条目 —— 比默认勾错更糟。

    所以判断只用于**降级默认勾选**（B 方案）：目录项照常列出（用户确知可用
    可以手工勾），但不预勾、`recommended` 为假。写回时那个段没有模型 →
    不写入，而不是写一批猜的名字。

    「落后」的判据是**世代**而非名字：只要目录最高世代低于市面最新，就算
    落后。不比较具体名字 —— 站方特供型号（`gpt-5.6-preview-xyz`）不在市面
    名录里，按名字比会把它误判成落后。

    世代比较必须**同产品线内**进行（2026-09-04 修）
    ----------------------------------------
    原来直接比两侧的全局最高世代。给 o 系列补上版本解析之后（见
    `_O_SERIES_RE`）这条立刻出错：`o3-mini` 的世代是 (3,0)，而 codex 段的市面
    最新是 `gpt-5.6-sol` 的 (5,6) —— 两个数字来自**互不相干的编号体系**，
    o 系列的 3 不代表它比 gpt 的 5.6 老一代（`o3` 与 `gpt-5` 是同期产品）。
    按全局比会把「目录里只有 o 系列」的站误判成落后，从而不预勾任何模型。

    改成逐产品线比：只对**两侧都出现**的产品线比较最高世代，全部落后才算
    落后。目录里的线在市面清单里没有对应（只有 o 系列）时无从比较，不判落后
    —— 与 `newest_generation_per_line` 的「整组认不出版本就全留」同一条原则。
    """
    if not catalog:
        return False, ""
    fit = [m for m in catalog if section_allows(section, m)]
    if not fit:
        return False, ""
    latest, _src = latest_models(section, cfg=cfg, remote=remote, limit=12)
    cat_by_line = top_generation_per_line(fit)
    mkt_by_line = top_generation_per_line(latest)
    shared = [ln for ln in cat_by_line if ln in mkt_by_line]
    if not shared:
        return False, ""          # 没有可比的产品线，无从判断
    behind = [ln for ln in shared if cat_by_line[ln] < mkt_by_line[ln]]
    if len(behind) < len(shared):
        return False, ""          # 至少一条线是跟得上的，整份目录不算落后
    ln = max(behind, key=lambda x: mkt_by_line[x])
    cat_top, mkt_top = cat_by_line[ln], mkt_by_line[ln]
    return True, (
        f"站方目录最高世代 {cat_top[0]}.{cat_top[1]}，"
        f"市面最新已到 {mkt_top[0]}.{mkt_top[1]}")


def top_generation_per_line(names: list[str]) -> dict[str, tuple[int, int]]:
    """{产品线: 该线的最高世代}。认不出版本的名字不参与。

    公开入口，给 server.py 的 /api/context 用 —— 前端判「站方目录整体落后」
    必须按产品线比，与 `catalog_is_stale` 用同一套数据。见那个函数的说明。

    分组用 `_product_line`，不用族（2026-09-12 退回来）
    ------------------------------------------------
    2026-09-11 曾改成按族（`family()`）。两处因此坏掉：

      · `catalog_is_stale` 用它比两侧。`family("o1")` 是 `"gpt"`，于是
        一个目录里只有 `o1` / `o3-mini` 的站与市面的 `gpt-5.6` 同桶比较，
        3.0 < 5.6 → 判成「整份目录都是老款」。o 系列与 gpt 系列是互不相干
        的编号体系，`o3` 的 3 不代表它比 `gpt-5.6` 老一代 —— 这正是
        2026-09-04 加逐线比较时要挡的那个误判。
      · `web/app.js` 的 `staleCheck` 拿这份数据当 `market_top_gen_lines`，
        而它自己那一侧用 `productLine(m)` 算键。两侧键不同名 →
        `shared` 恒为空 → 前端的落后提示整个失效（静默，界面上看不出来）。

    `newest_generation_per_line` 的分组同样是产品线，三处保持一致。
    """
    out: dict[str, tuple[int, int]] = {}
    for n in names:
        if not n:
            continue
        g = generation(series_and_version(n)[1])
        if g is None:
            continue
        line = _product_line(n)
        if line not in out or g > out[line]:
            out[line] = g
    return out


def topup_to_market_top(section: str, models: list[str], *,
                        cfg: dict | None = None,
                        remote: list[str] | None = None,
                        proven: list[str] | None = None
                        ) -> tuple[list[str], list[str], str]:
    """没检测出高级模型时，按该段该族的**市面最高级**填充并勾上。

    用户 2026-09-11 的要求（明确推翻 2026-09-02 定的相反规则）
    -------------------------------------------------------
    原规则：站方目录整体落后于市面最新时「列出但**不预勾**」
    （`catalog_is_stale` 的 B 方案，README「站方目录整体落后时列出但不预勾」）。
    当时否决「直接填市面最新」的理由是：那些名字不在站方目录里，写进去
    CPA 路由过去大概率 404，等于把一个「有老模型可用」的站变成死条目。

    用户的新口径：**探测本身会有 BUG，目录没报的模型实际上往往能用** ——
    所以宁可填上最高级模型让它有机会被用到，也不要因为探测没探到就只留
    一批低级模型。风险由用户承担，本函数只保证「填了什么要说得出来」。

    填充规则（与用户逐条对应）：
      · gemini：只填带 `pro` 的最高编号（`section_allows` 已经把
        非 `-pro*` 和版本 < 2.5 的挡掉了）
      · codex：填当前最高世代**整个系列的所有名字**（如 gpt-6 世代下
        `gpt-6` / `gpt-6-astra` 都要），这正是用户说的
        「勾了 gpt-5.6 却没勾 gpt-5.6-sol 这种重大失误」的反面
      · claude：填 `claude-*-5` 那一级的全部
      · openai-compatibility：**允许多族并存**，每族各填各自的最高级
        （compat 段走 `/chat/completions` 万能口，本来就不限族）

    按实际模型族比较世代；低代替换为已知最高代，同代补齐所有已知变体。
    不生成目录之外的名字，也不使用 HTTP 探测数量上限截断注册清单。
    2026-09-12：只补 `models` 里已经出现过的**产品线**，不再引入这个站
    没报过的产品线（claude 族里 opus / sonnet / fable 是三条线）。

    Returns:
        (合并后的清单, 新填进去的名字, 来源说明)
        第二项只包含新加入的名字。

    2026-09-15 修正：**只加不删**
    ----------------------------
    上一版在结尾调用 `newest_generation_per_line(have + latest)`，那是把站方
    报过的名字和市面名录**放在一起重选**，于是市面名录里更高的世代会把站方
    的名字整个淘汰掉。用 live 名录（79 条）复现：

        站方实测报 gpt-5.6              → 写入 ['gpt-6-astra']   站方名字全丢
        站方实测报 gpt-5.6-sol          → 写入 ['gpt-6-astra']   站方名字全丢
        站方实测报 gpt-5.6 + gpt-5.6-sol → 写入 ['gpt-6-astra']   站方名字全丢

    `gpt-6-astra` 这个站从没报过，CPA 路由过去大概率 404 —— 正是本模块
    `:1015-1019` 自己写下的禁令。而站方报的那个名字是**探测拿回来的事实**，
    可信度高于市面名录里的推测。

    所以现在的规则是：

      · 站方的每个名字**原样保留**，不因为市面有更高世代而删除
        （！！已由下节 2026-09-16 收窄取代：同族更高主版本在场时旧代仍会删）
      · 市面名录里**同一条产品线**（`_product_line`）且**世代不低于站方该线
        上限**的名字追加进来 —— 既补同代变体（用户要的「不许漏勾」），
        也补站方目录落后时的高代
      · 站方报的名字已经在市面名录里时天然不会重复（`n in have` 挡掉）
      · 加不进去任何东西时就原样返回，**绝不删改**

    为什么不是「严格更高才加」：那会砍掉同代补齐。实测
    `topup_to_market_top(O, ["gpt-6-a"], remote=["gpt-6-a".."gpt-6-f"])`
    必须返回全部 6 个 —— 同代变体补齐正是用户第 4 条点名的诉求。

    2026-09-16 收窄：「不拿名录顶掉名录不认识的名字」才是真规则
    --------------------------------------------------------
    上一版的「不删」是无条件的，于是把「同一产品线的旧世代」也留下了：
    站方报 `gpt-5.5`、市面名录有 `gpt-6` / `gpt-6-sol` 时三个都留。
    那与用户 docx 第 3⑵ 条「每种类型只能选择该类型对应的最高级别模型」
    以及 `newest_generation_per_line` 的阶段 A/B 判据直接冲突 ——
    plan.py 在调本函数之前刚用同一个函数淘汰过旧代，本函数又加回来，
    等于自己拆自己的台。测试
    `test_planning_compliance.test_failed_and_lower_probe_fill_highest`
    断言的正是这件事（最早提交 924330f 就写了）：`gpt-6` 出现时
    `gpt-5.5` 不留。

    但 `:1004-1029` 那条要被保护的诉求仍然成立，只是此前**判据找错了**：
    真正不该发生的是「用市面名录里的名字顶掉**名录不认识**的名字」
    （`gpt-6-astra` 顶掉站方实测的 `gpt-5.6-sol` 是这种），
    而不是「淘汰名录认识、但已被更高世代取代的旧代」（`gpt-5.5`）。

    所以现在归并分两步走，判据**不看名录认不认识，只看世代**：

      1. **淘汰**：站方名字里，版本可比且所属族的最高主版本已被更高的主版本
         取代的，一概出局（`gpt-6` 在场 → `gpt-5.5`、`gpt-5.6`、`gpt-6-astra`
         都走）。判据是 `generation_family` 的**主版本**，与
         `newest_generation_per_line` 的阶段 A 同一口径 —— 这条与
         「名录认不认识」无关：站方自己报过的旧代也是旧代。
      2. **补齐**：站方报过的**产品线**上，市面最高世代的全部变体都算进来
         （`gpt-6` 与 `gpt-6-sol` 一起），不看名录那一代是否比站方更高 ——
         站方目录落后市面整整一代时也要补齐，不是只补同代。

    判据从「名录认不认识」改成「世代」的代价与收益：
      · 收益 —— `test_failed_and_lower_probe_fill_highest`（924330f 起就在）
        与 `test_lower_generation_removal_warning_is_truthful` 断言的
        `gpt-6` 出现时 `gpt-5.5` 不留，只有在名录也认识 `gpt-5.5` 时才成立；
        改判据后不需要名录配合。
      · 代价 —— 站方特供的**旧代**型号（`gpt-5.6-site-only`）会被淘汰。
        这是有意的：用户 docx 第 3⑵ 条要求每类型只留最高级别，旧代留着
        只会让 CPA 路由到站方可能已下线的型号。真正认不出世代的站方特供名
        （`opus-5` 这类非 gpt/claude/gemini 命名）版本不可比，**一律保留**。

    `have` 为空是 plan.py 的兜底路径（探测与目录都空）—— 此时没有站方事实
    可守，市面最新就是唯一能填的东西，退回原来的全量行为。

    2026-09-16 再收窄：**实测通过的世代不受「更高主版本」淘汰**
    ------------------------------------------------------
    用户原话：「gpt-5 和 gpt-5.6 理论上不可能保留，因为目前最新模型为
    gpt-6 系列，但是**如果检测 gpt-6 系列明显不通，这个时候 gpt-6 系列按
    模型目录最高级别保留同时保留实测最高的 gpt-5.6 系列**」。

    阶段 A 原来的判据是「同族出现更高主版本 → 低主版本全丢」，它把这句话
    的前半段实现了，后半段却做反了：站方**实测通** `gpt-5.6`、目录里躺着
    没验过的 `gpt-6` 时，`gpt-5.6` 被删掉，清单里只剩一串从没打通过的
    名字 —— CPA 每次轮到这个站都对着死模型发请求。

    所以 `proven`（本轮实测出 200 且被 `_accept` 收下的名字，plan.py 传
    `v.models`）作为**第二证据层**参与阶段 A：某族里凡是实测通过的名字，
    其主版本一律豁免淘汰。判据仍然是「世代」而不是「名录认不认识」——
    只是把「目录声称有」与「实测确实通」分开对待：

        have={gpt-5.6} proven={gpt-5.6} 目录有 gpt-6  → gpt-6 与 gpt-5.6 都留
        have={gpt-5.6} proven={}        目录有 gpt-6  → 只留 gpt-6（原行为）
        have={gpt-6}   proven={gpt-6}   目录有 gpt-6  → 自然只剩 gpt-6

    第二种是 `test_failed_and_lower_probe_fill_highest` 固化的口径（探测
    什么都没探到、`have` 里只有历史遗留的低代），它**不受**这次改动影响：
    `proven` 为空时豁免集合为空，阶段 A 逐字回到原判据。

    完整世代之间的收敛（阶段 B 的产品线取最高代）不动 —— `proven` 只影响
    「整代被更高主版本作废」这一条，不影响「同线取最高」。
    """
    # 补齐项会写进 config.yaml —— 只取权威名录与本地配置里真实存在的名字，
    # 不拿别站目录或写死清单顶替（2026-09-27，见 latest_models 的 for_registration）。
    latest, src = latest_models(section, cfg=cfg, remote=remote, limit=0,
                                for_registration=True)
    latest = [m for m in latest if section_allows(section, m)]
    if not latest:
        return list(models), [], ""

    # 降级档（mini / nano / lite）**无论从哪条路进来都不许留**
    # （用户 2026-09-16 第 1 条：「凡是 mini 系列永远不可能勾选，因为这是
    # 低档次模型」）。
    #
    # 为什么必须在这里再拦一道：`have` 是调用方直接传进来的站方清单，它
    # **没过 `section_allows`**。而降级档在世代比较里是「认不出版本」的
    # ——`_cmp_gen` 对 `is_low_tier` 的名字返回 `None`，于是 `_stale_major`
    # 一律返回 False，整条淘汰逻辑碰都碰不到它们。实测（2026-09-16）：
    #
    #     have=['gpt-4o-mini', 'gpt-5.6']  名录 gpt-6
    #     → ['gpt-4o-mini', 'gpt-5.6', 'gpt-5.6-sol', 'gpt-6']
    #                ^^^^ 一个 2024 年的降级档混进了 2026 年的清单
    #
    # 站方报过也不算数：降级档是**选型偏好**层面的硬拒（用户明示），
    # 与「站方报过的名字是实测事实」那条不冲突 —— 那条管的是「同一档次里
    # 信谁」，这条管的是「哪些档次根本不进候选」。
    #
    # 手填那条路不受影响：它走 `plan.py` 的 `forced_kept`，根本不经过本函数。
    have = [m for m in (models or []) if m and not is_low_tier(m)]
    if not have:
        return list(latest), list(latest), src

    # `latest` 已过 `latest_models` 内部两轮筛选（`newest_generation_per_line`
    # → `rank_models`），同线只剩最高世代 —— 拿它判断「市面有没有同代变体」
    # 会漏：站方报 `gpt-5.6` 时那条线已被收敛成 `gpt-6`，`>= (5,6)` 虽成立却
    # 只补出 `gpt-6`，同代的 `gpt-5.6-sol` / `gpt-5.6-luna` 全看不见。
    # 所以世代比较一律拿**原始名录** `remote`（plan.py 传的就是它），
    # `latest` 退回「remote 缺席时的代用品」。
    registry = [m for m in (remote or []) if m] or list(latest)
    registry = [m for m in registry if section_allows(section, m)]
    if not registry:
        registry = list(latest)

    # 判据与 latest_models 内部一致：gemini 只认 pro、降级档
    # （mini/nano/lite）不参与、认不出版本的不参与。
    def _cmp_gen(n: str) -> tuple[int, int] | None:
        if family(n) == "gemini" and not gemini_pro_ok(n):
            return None
        if is_low_tier(n):
            return None
        return generation(series_and_version(n)[1])

    def _tops(names: list[str]) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for n in names:
            g = _cmp_gen(n)
            if g is None:
                continue
            line = _product_line(n)
            if line not in out or g > out[line]:
                out[line] = g
        return out

    # 市面对照池：`registry` ∪ `latest`。两条都要 —— `registry` 是原始名录
    # （同代变体只在它这里有），`latest` 是 market 收敛后的「当前最新」，
    # `remote` 缺席时它就是唯一依据。
    market_pool = list(dict.fromkeys(list(registry) + list(latest)))
    station_top = _tops(have)
    market_top = _tops(market_pool)

    # ── 阶段 A：族内比主版本（与 `newest_generation_per_line` 同口径）──
    # 「站方报 `gpt-5.6`、市面已是 `gpt-6`」时该整代换代，这是 docx 3⑵ 的
    # 明文要求，也是 `test_failed_and_lower_probe_fill_highest` 固化的口径。
    # 站方与市面一起比：任一方出现更高主版本，整族的低主版本全出局。
    top_major: dict[str, int] = {}
    for n in list(have) + market_pool:
        g = _cmp_gen(n)
        if g is None:
            continue
        fam = generation_family(n)
        if fam not in top_major or g[0] > top_major[fam]:
            top_major[fam] = g[0]

    # ── 实测豁免：本族**实测通过**的最高世代不作废 ──
    # 见 docstring「2026-09-16 再收窄」。`proven` 是本轮真正打通的名字；
    # 目录声称、原配置遗留、市面补齐都不算 —— 那正是「检测 gpt-6 明显不通
    # 时仍要保住 gpt-5.6」这句话能被实现的前提。
    #
    # 判据必须是**完整世代**而不是主版本：按主版本放行会把 `gpt-5` 也一起
    # 搭救（它与 `gpt-5.6` 同主版本），而用户的口径是「gpt-5 和 gpt-5.6
    # 理论上不可能保留」—— 要留的只有实测过的那一代本身。
    _proven_gen: dict[str, tuple[int, int]] = {}
    for n in (proven or []):
        g = _cmp_gen(n)
        if g is None:
            continue
        fam = generation_family(n)
        if fam not in _proven_gen or g > _proven_gen[fam]:
            _proven_gen[fam] = g

    # ── 第三证据层：站方**报过**的那一代（2026-09-16，mhtml 快照复盘）──
    #
    # 为什么光有 `proven` 不够
    # ----------------------
    # `proven` 的来源是「本轮实测出 200」。而真实现场里整轮探测可以一次都
    # 不成功：那份 10.9 MB 的全量检测快照实测 45 次请求，403×698 /
    # 503×287 / 429×269，**几乎 0 次 200**。于是 `proven` 恒为空、豁免恒
    # 不成立，阶段 A 照常整代作废，结果是：
    #
    #     站方报 gpt-5.6-sol（live catalog 里确实有）
    #     目录里躺着 gpt-6-astra
    #     → 落盘只剩 ['gpt-6-astra']   ← 站方从没报过这个名字
    #
    # 这违反本文件 :1015-1019 自己写的禁令（不替站方发明它没卖过的东西），
    # 也违反用户 docx 第 3⑵ 条后半段的原话：「如果检测 gpt-6 系列明显不通
    # （包括代理检测或其他办法也不通），这个时候 gpt-6 系列按模型目录最高
    # 级别保留**同时保留实测最高的 gpt-5.6 系列**」—— 要的是**两者都留**，
    # 而不是「探不通就只留目录顶代」。
    #
    # 三层证据的强弱（这是本函数的核心次序）
    # ------------------------------------
    #   1. proven   本轮实测 200        —— 最硬，豁免一定成立
    #   2. have     站方 /models 报过   —— 次之，站方声明它卖这个
    #   3. 目录补齐 名录里有这个名字     —— 最弱，工具的猜测
    #
    # 站方报过的名字与目录猜的名字**不是一个量级**：前者是上游自己声明的
    # 商品清单，后者只是「市面上存在这个型号」。拿 3 去替换 2，等于用猜测
    # 覆盖事实 —— 那正是快照里那个缺陷的形状。
    #
    # 为什么不干脆让站方清单完全免疫阶段 A
    # ---------------------------------
    # 那会把**整份陈年目录**也保住，而用户 2026-09-11 明确推翻过那种做法
    # （docx 第 4 条）：「检测出来没有高级模型就按该系列该类型的最高级填充
    # 勾选」—— 一个站的目录全是 gpt-4 / gpt-4-32k / gpt-4o 而市面已到 5.6 时，
    # 清单**要被顶成市面最高级**，不留老款。`test_stale_catalog_not_recommended`
    # 固化的就是这条。
    #
    # 那两种情形靠什么区分
    # ------------------
    # 不是「主版本差几代」—— 主版本号不连续（gpt 4 → 5 之间没有真正的世代），
    # 差值恒为 1 的两个场景一个该豁免一个该顶掉，阈值分不开。
    #
    # 真正的区别是「**市面名录里还认不认识站方那一代**」：
    #
    #     站方 gpt-5.6  名录里 gpt-5.6-terra / -luna / -sol 都在 → 还在售
    #     站方 gpt-4    名录里一个 (4,0) 的都没有              → 已下线
    #
    # 还在售就说明站方只是「新代刚出还没上架」，它报的名字此刻确实能用；
    # 名录里连同代的影子都没有，那些名字多半已经下线 —— 写进 config.yaml
    # 会让 CPA 路由到死模型，比不写更糟。
    #
    # 这个判据不需要任何阈值常数，也不受主版本号是否连续影响。
    #
    # 2026-09-18 试着撤掉过这道闸（以为它会把站方报过的名字整批顶掉），
    # 被四条既有断言挡回来，实测也证明想错了：真实名录（models.router-for.me
    # 的 models.json）**同时列着好几代**，站方报的上一代通常在册，这道闸不会
    # 命中。只有在人为构造的「名录里只有一代」的输入下才会整批顶掉。
    # 真正要挡的是 gpt-4 / gpt-4-32k 这种名录里连影子都没有的陈年目录。
    _market_gens: dict[str, set[tuple[int, int]]] = {}
    for n in market_pool:
        g = _cmp_gen(n)
        if g is None:
            continue
        _market_gens.setdefault(generation_family(n), set()).add(g)

    _have_gen: dict[str, tuple[int, int]] = {}
    for n in have:
        g = _cmp_gen(n)
        if g is None:
            continue
        fam = generation_family(n)
        if fam not in _have_gen or g > _have_gen[fam]:
            _have_gen[fam] = g

    def _exempt_gen(fam: str) -> tuple[int, int] | None:
        """该族里**受保护**的那一代。实测优先，其次站方报过的最高代。

        两者都存在且不同代时取实测那一代：实测是更硬的证据，而同族里
        同时保两代（实测的 + 站方报过的更高但没验过的）会让清单里混进
        没有任何证据支持的名字。

        站方那一层还要过「市面名录认不认识这一代」那道闸 —— 名录里连同代
        的影子都没有，说明它已经下线，照常顶成市面最高级。见上面的说明。

        2026-09-18 复核：曾怀疑这道闸会把站方报过的名字整批顶掉（构造
        `have=['gpt-5.6-sol'], remote=['gpt-6','gpt-6-astra']` 确实会），
        但真实名录同时列着多代，站方报的上一代在册，闸不会命中；
        撤掉它会让 `gpt-4 / gpt-4-32k / gpt-4o` 这种陈年目录留下来，
        `tests/test_full_redetect.py` 与 `tests/test_planning_compliance.py`
        共四条断言锁着这个行为。结论：**保持原样**，不要再改。
        """
        hit = _proven_gen.get(fam)
        if hit is not None:
            return hit
        hit = _have_gen.get(fam)
        if hit is None:
            return None
        if hit not in _market_gens.get(fam, ()):
            return None                 # 名录已不认识这一代：陈年目录
        return hit

    def _stale_major(n: str) -> bool:
        """该名字所属族的主版本是否已被更高主版本取代。

        受保护的那一代（`_exempt_gen`）不作废：
          · 实测通过 —— CPA 路由到它是**确定的收益**，丢掉换一个没验过的
            名额是确定的损失
          · 站方报过 —— 上游自己声明卖这个，比工具从目录里猜的名字硬

        作废只针对「既没打通、站方也没报过、且已被更高主版本取代」的名字。
        """
        g = _cmp_gen(n)
        if g is None:
            return False
        fam = generation_family(n)
        top = top_major.get(fam)
        if top is None or g[0] >= top:
            return False
        kept = _exempt_gen(fam)
        if kept is not None and (g[0], g[1]) == kept:
            return False
        return True

    # ── 阶段 B：产品线内比完整世代 ──
    # 站方**报过**（实测事实）的名字各线到过的最高世代；市面在这条线上
    # **严格更高**时按市面换代，否则以站方为准。站方没报过的线不进
    # `lines` —— 不替站方发明它没卖过的产品线（`:1015-1019` 的禁令）。
    #
    # 这里必须是「严格更高」而不是 `max()`：两者在「站方报的就是最高代、
    # 只是缺同代变体」（站方报 `gpt-6`、名录有 `gpt-6` 与 `gpt-6-sol`）
    # 时给出同一个值，但语义不同 —— 我们要的是「市面能提供站方那一代
    # 的同代兄弟就补上，不能因为市面有更高代就把站方整代删掉」（后者的
    # 处理在阶段 A，只按主版本、只删被取代的族）。
    lines: dict[str, tuple[int, int]] = {}
    for line, g in station_top.items():
        mkt = market_top.get(line)
        lines[line] = mkt if (mkt is not None and mkt > g) else g

    # 站方报过的名字，只要所属族的主版本已被更高主版本取代就丢
    # （`gpt-6` 出现 → `gpt-5.5` / `gpt-5.6` / `gpt-6-astra` 全走）。
    # 判据是**世代本身**，与名录无关：站方自己报了 `gpt-6` 还留着 `gpt-5.5`
    # 一样是旧代，这正是 `test_failed_and_lower_probe_fill_highest` 与
    # `test_lower_generation_removal_warning_is_truthful` 固化的口径。
    #
    # 版本认得出才淘汰：`_cmp_gen` 返回 `None`（gemini 非 pro、mini/nano/lite
    # 降级档、非 gpt/claude/gemini 命名的站方特供名）一律保留 —— 对它们
    # 工具没有任何世代依据，删了才是真丢东西。
    drop = [n for n in have if _stale_major(n)]
    kept = [n for n in have if not _stale_major(n)]

    # 最终清单拼接顺序：站方保留的（实测事实）→ 市面补齐的。
    # 先站方后市面，让 diff 一眼看出哪些名字来自实测、哪些是补齐的。
    merged: list[str] = []
    seen: set[str] = set()

    def push(n: str) -> None:
        if n and n not in seen:
            seen.add(n)
            merged.append(n)

    def _proven_ok(n: str) -> bool:
        """这一代被豁免放行了吗（族内受保护的那一代）。

        与阶段 A 的 `_stale_major` 共用 `_exempt_gen` —— 两处判据必须同源，
        否则会出现「阶段 A 保住了 gpt-5.6，阶段 B 却不给它补同代的
        gpt-5.6-sol」这种半吊子结果，而用户 docx 第 3⑵ 条要的正是
        「所有相同等级系列的模型全部都要勾选上」。
        """
        g = _cmp_gen(n)
        if g is None:
            return False
        kept = _exempt_gen(generation_family(n))
        return kept is not None and g == kept

    for n in kept:
        push(n)

    added: list[str] = []
    for n in market_pool:
        if n in seen:
            continue
        g = _cmp_gen(n)
        if g is None:
            continue
        line = _product_line(n)
        top = lines.get(line)
        # 实测豁免放行的那一代，市面上的**同代变体**也要一起补进来
        # （用户要求「所有相同等级系列的模型全部都要勾选上」）——
        # 只认 `lines[line] == g` 会把 `gpt-5.6-sol` 挡在门外，因为
        # `lines` 记的是目录顶代（gpt-6）。
        if top is None or (g != top and not _proven_ok(n)):
            continue
        # 站方这一线整代作废时（阶段 A 判的），市面那一代的变体也要一起
        # 筛掉 —— 否则 `gpt-5.6` 被淘汰、`gpt-5.6-sol` 又被补回来。
        if _stale_major(n):
            continue
        added.append(n)
        push(n)
    # 只有站方没报过的才算「补齐」。站方已报的名字留在 `kept` 里，不是补齐。
    added = [n for n in added if n not in have]

    # ── 阶段 C：同产品线、同主版本内，次版本只留最高（2026-09-25 现场根因）─
    #
    # 阶段 A 只比**主版本**，阶段 B 只决定「补什么」，两者都管不到
    # 「同一条线、同一主版本里已保留的旧次版本」。于是这种形态会漏出去：
    #
    #     站方目录报 claude-opus-5        → station_top[claude-opus] = (5, 0)
    #     市面名录有 claude-opus-5-5      → market_top[claude-opus]  = (5, 5)
    #     阶段 A：同族主版本都是 5，claude-opus-5 不作废 → 进 kept
    #     阶段 B：claude-opus-5-5 == lines[claude-opus] → 进 added
    #     落盘   ['claude-opus-5', 'claude-opus-5-5']   ← 同版本高低两档并存
    #
    # 后果与 docx 第 3⑵ 条「每种类型只能选择该类型对应的最高级别模型」直接
    # 冲突：两个都注册进 config.yaml，CPA 会把请求分给 5.0 那一档（旧档），
    # 用户报的「模型勾选高低模型混乱错误」就是这个形状。
    #
    # 作用域**严格限定在同主版本内**，跨主版本归阶段 A —— 两者的用户裁定
    # 不同，越界就会违反其中一条。详见 `_stale_minor` 的说明。
    #
    # 同代变体不受影响：`claude-opus-5-max` / `-thinking` / `-xhigh` 的世代
    # 都是 (5, 0)，与 `claude-opus-5` 相等而非更低，`_stale_minor` 不命中。
    # 那正是 docx 第 3⑵ 条「所有相同等级系列的模型全部都要勾选上」。
    _line_top: dict[str, tuple[int, int]] = {}
    for n in merged:
        g = _cmp_gen(n)
        if g is None:
            continue                    # 认不出世代的（站方特供名）不参与
        line = _product_line(n)
        if line not in _line_top or g > _line_top[line]:
            _line_top[line] = g
    # 各产品线实测通过的最高世代（规则 ④ 豁免用，见 `_stale_minor`）
    _line_proven: dict[str, tuple[int, int]] = {}
    for n in (proven or []):
        g = _cmp_gen(n)
        if g is None:
            continue
        line = _product_line(n)
        if line not in _line_proven or g > _line_proven[line]:
            _line_proven[line] = g

    def _stale_minor(n: str) -> bool:
        """同一产品线、**同一主版本**内，次版本更低的那些。

        必须限定「同主版本」——跨主版本是阶段 A 的辖区，两者规则不同，
        而用户对这两件事的裁定也不同，混在一起必然违反其中一条：

            gpt-5.6   vs gpt-6-astra   主版本 5≠6 → 阶段 C **不管**
              用户 2026-09-16 第 2 条：「检测出来最新模型 gpt-6 系列不通，
              直接按最新模型 gpt-6 填充。但是次高级模型如 gpt-5.6 通的，
              这个时候将 gpt-5.6 系列与 gpt-6 系列都勾选保留。」
              → 两代都留，由阶段 A 的 proven / 站方报过豁免兑现。

            claude-opus-5 vs claude-opus-5-5   主版本同为 5 → 阶段 C **管**
              用户 2026-09-25：「claude-opus-5 与 claude-opus-5-5
              后者为同一个版本较高版本」。
              → 同一个版本的高低两档，只留高的；两个都注册会让 CPA
                把请求分给旧档，即用户报的「高低模型混乱」。

        不加这个限定时 `tests/test_full_redetect.py::
        test_model_rules_no_dead_end` 会挂在 `gpt-5.6` 被丢掉上 —— 那条
        断言从 924330f 起就锁着「两代都留」。

        规则 ④ 豁免（2026-09-26）：这条线的最高次版本**没有实测通过**、而更低
        的某一代实测通过时，实测最高的那一代留下（与最高次版本并存）。docx 原话
        「按当前模型目录最高级别保留最新模型勾选……同时保留实测最高的次最新
        模型」—— 只留一个没验过的名字，CPA 路由过去的那个站可能根本不卖它，
        而验过的那一代反倒被删了。上一版在这里不看 proven，实测后果是
        `max-context-length` 落不到实测模型上（那个模型整个被裁掉了）。
        最高次版本本身实测通过时豁免不成立，照常只留高的。
        """
        g = _cmp_gen(n)
        if g is None:
            return False
        line = _product_line(n)
        top = _line_top.get(line)
        if top is None or g[0] != top[0] or g[1] >= top[1]:
            return False
        pv = _line_proven.get(line)
        if pv is not None and pv != top and g == pv:
            return False
        return True

    _converged = [n for n in merged if not _stale_minor(n)]
    _minor_drop: list[str] = []
    if len(_converged) != len(merged):
        _dropped_minor = [n for n in merged if _stale_minor(n)]
        # 站方报过的名字被这一步裁掉时要进 `drop`，理由才会挂到界面警告上
        # （「少了 claude-opus-5」必须追得到原因，不能悄悄消失）。
        _minor_drop = [n for n in _dropped_minor if n in have]
        drop = list(drop) + _minor_drop
        added = [n for n in added if n in _converged]
        merged = _converged

    if not added and not drop:
        # 站方清单已是市面最新：原样返回，**不做任何删改**。
        return list(models or []), [], ""

    # 淘汰了什么要说得出来：名字进了 `drop` 时把理由挂到 `_LAST_MERGE_NOTES`，
    # plan.py 取走后写进该段的 model_warns，界面上能看见（「少了 gpt-5.6」
    # 这种事必须能追到原因，不能悄悄消失）。
    # 本模块刻意不引 logging —— 它是纯选型库，只依赖标准库与 urllib。
    #
    # 两种淘汰的措辞要分开：阶段 A 是**跨主版本换代**（gpt-5.6 → gpt-6），
    # 阶段 C 是**同线次版本收敛**（claude-opus-5 → claude-opus-5-5）。
    # 混用一句「按更高主版本淘汰」会让人去查一个不存在的主版本变化。
    if drop:
        _major_drop = [n for n in drop if n not in _minor_drop]
        parts = []
        if _major_drop:
            parts.append(f"按市面更高主版本淘汰同族低代：{'、'.join(_major_drop[:6])}"
                         + ("…" if len(_major_drop) > 6 else ""))
        if _minor_drop:
            parts.append(f"同一产品线只留最高世代，淘汰低次版本：{'、'.join(_minor_drop[:6])}"
                         + ("…" if len(_minor_drop) > 6 else ""))
        _LAST_MERGE_NOTES[section] = "；".join(parts)

    return merged, added, src


def latest_models(section: str, *, cfg: dict | None = None,
                  remote: list[str] | None = None,
                  limit: int = 6,
                  for_registration: bool = False) -> tuple[list[str], str]:
    """该段「当前市面上最新」的模型清单。返回 (清单, 来源说明)。

    三层合并，可信度递减（见模块开头）：
      1. remote —— CPA 权威名录（调用方传进来，避免这里发网络请求）
      2. cfg    —— 本地 config.yaml 已有的模型名
      3. 内置兜底

    limit <= 0 返回完整注册清单；正数仍供有限探测队列使用。

    `for_registration=True`（2026-09-27）：清单要**写进 config.yaml**（方案层
    的 seed），而不只是拿去探测。此时只收前两层 —— 权威名录与本地配置都是
    「真实存在的名字」；「本轮其他站目录」是**别的站**报的名字，拿来填这个站
    是跨站污染；内置兜底是写死的猜测，违反修改要求第 5 条「禁止硬编码」。
    两层都空就返回空清单，方案层据此判 writable=False、给出 skip_reason，
    原条目保持不动。探测队列（默认 False）照旧可以用全部四层 —— 那里猜错
    只多花一次请求，证据由实测给出。

    每一层都先过 `section_allows`，再对合并结果做 `newest_per_series`。

    为什么第 2 层不可省：远程名录里没有站方特供型号。实测用户的 config.yaml
    里有 `gemini-3.1-pro-high`、`gemini-3.1-pro-preview-search`、
    `gemini-3.1-pro-preview-customtools`、`gpt-5.6` —— 四个都不在 CPA 名录里，
    但它们确实能用（就在生产配置里跑着）。

    为什么顺序是 remote 优先而不是 cfg 优先：cfg 里的名字可能是几个月前写的，
    而 remote 每 3 小时更新。同系列比较由 newest_per_series 处理，所以
    两层都给也不会留下旧版 —— 顺序只影响「版本无从比较时谁先出现」。
    """
    src: list[str] = []
    used: list[str] = []

    picked = [m for m in (remote or []) if section_allows(section, m)]
    if picked:
        src.extend(picked)
        used.append(f"CPA 权威名录 {len(picked)} 个")

    local = [m for m in _cfg_models(cfg, section) if section_allows(section, m)]
    if local:
        src.extend(local)
        used.append(f"本地 config.yaml {len(local)} 个")

    if not src and not for_registration:
        # 第 3 层：本进程这一轮见过的新鲜目录（别的站实测/目录报过的名字）。
        fresh = seen_catalog(section)
        if fresh:
            src.extend(fresh)
            used.append(f"本轮其他站目录 {len(fresh)} 个")

    if not src and for_registration:
        return [], "权威名录与本地配置都没有可用名字（不拿别站目录或写死清单顶替）"

    fallback_only = False
    if not src:
        built = [m for m in FALLBACK_MODELS.get(section, ())
                 if section_allows(section, m)]
        src.extend(built)
        used.append(f"内置兜底 {len(built)} 个")
        fallback_only = True
        # 写死的清单必然会过期 —— 用到它就说一声，运维能在日志里看见
        # 「远程名录拉不通、本地也没有」这件事，而不是默默写进一代旧模型。
        logger.warning(
            "段 %s 的模型清单退到内置兜底（远程名录与本地配置都没有可用名字）"
            "—— 内置清单可能已过期：%s", section, ", ".join(built))

    # 兜底清单**不再做世代收敛**（2026-09-26）。
    #
    # `newest_generation_per_line` 的职责是「从一堆真实存在的名字里挑最新
    # 一代」—— 前提是这堆名字有来源（远程名录 / 本地配置 / 本轮目录），
    # 收敛掉的旧代确实是该淘汰的。兜底层没有这个前提：它是**猜**的，猜的
    # 时候收敛等于把「两代并存」这个已经想清楚的策略又抹平回一代。
    #
    # 实测后果：FALLBACK_MODELS 里 codex 段写了 gpt-6 与 gpt-5.6 两代
    # （生产 config.yaml 实测 gpt-5.6-sol 45 条 / gpt-6-sol 6 条，次新代才是
    # 扛量的那一代），收敛后只剩 ['gpt-6-astra', 'gpt-6-sol'] —— 等于赌这个
    # 站已经开通最新代。赌输就是「填进去的模型 CPA 每次轮到都失败」。
    #
    # FALLBACK_MODELS 的内容已经逐条过了 `section_allows`（上面那行），
    # 档次与族都是对的，两代并存是这一层刻意的设计，不是没清理干净。
    out = src if fallback_only else newest_generation_per_line(src)
    # 排序必须在截取之前。不排的话取前 N 个拿到的是「输入顺序靠前」的那些，
    # 而输入顺序来自 CPA 名录的 JSON 排列 —— 与「哪个模型更该用」无关。
    # 实测 claude 段不排序时前三名是 haiku / sonnet / opus，正好倒过来。
    out = rank_models(out)
    # Registration is not an HTTP probe queue. Zero requests the complete set.
    if limit <= 0:
        return out, " + ".join(used)
    # 再按「产品线」轮转，避免前 N 个都是同一条线的变体。
    #   compat 段按**族**分（gpt / claude / gemini / kimi）—— 它转多族，
    #     只注册一族等于浪费这个段
    #   其余段按**产品线**分（opus / fable / sonnet；pro / pro-preview）——
    #     实测不轮转时 claude 段前四名里三个是 opus 的变体
    if section == "openai-compatibility":
        out = _round_robin(out, limit, family)
    else:
        out = _round_robin(out, limit, _product_line)
    return out, " + ".join(used)


# 产品线：把版本与常见变体后缀剥掉之后剩下的名字。
#
#   claude-opus-5 / claude-opus-5-thinking / claude-opus-4-8-m-aws → claude-opus
#   gemini-3.1-pro / gemini-3.1-pro-high                          → gemini-pro
#   gpt-5.6 / gpt-5.6-sol / gpt-5.6-luna                          → gpt
#
# 为什么 `-sol` / `-luna` / `-terra` 也剥掉：那三个是 OpenAI 同一代的三个
# 变体（同一条产品线），不该占三个轮转位。而 `-high` / `-low` 是 gemini pro
# 的算力档，同理。
#
# 2026-09-02 补：版本 token 加 `o?`（`gpt-4o` → `gpt`），并补上 `-nano`、
# `-32k`、`-256k`、`-1m`、`-chat`、`-codex`、`-audio-preview` 这几类后缀。
# 它们都是「同一条线的规格差异」而非独立产品线 —— 不剥的话
# `gpt-4-32k` 会自成一线，从而躲过「取最高世代」。
_LINE_STRIP = re.compile(
    r"(?<![A-Za-z0-9.])k?\d+(?:[.\-]\d+)*o?(?![A-Za-z0-9])"   # 版本 token
    r"|-(?:thinking|m-aws|agent|latest|fast|high|low|extra-low"
    r"|sol|luna|terra|astra|preview|search|customtools|spark"
    r"|mini|nano|lite|chat|audio-preview"
    r"|32k|64k|128k|256k|512k|1m)(?=$|[-.])"
)


def _product_line(name: str) -> str:
    """产品线 = 版本号**之前**那一截。结构判据，不含任何后缀清单。

    2026-09-12 改成结构判据（docx 第 6 条：严禁硬编码）
    ------------------------------------------------
    原实现靠 `_LINE_STRIP` —— 一张手写的后缀白名单（sol / luna / terra /
    astra / preview / 32k …）。它有两个必然的毛病：

      · **漏一个就错一次**，而漏是常态：上游每出一个新后缀就要有人来加。
        本轮就实测到 `gpt-6-astra` 因为 `astra` 不在表里而自成一条产品线，
        躲过「同线取最高世代」被与 `gpt-5.6` 一起勾上；补进去之后
        `gpt-5.6-peerN` 这类又照样漏。
      · 那正是用户禁止的硬编码：模型名录会跟着 CPA / CPAMP 更新，判据不能
        每次都要改本项目的代码。

    `series_and_version` 已经把名字拆成「模板 + 版本」，模板里 `*` 之前那一截
    就是**版本号之前的固定前缀** —— 天然的产品线，不需要知道后面是什么后缀：

        gpt-5.6              → 模板 `gpt-*`            → 线 `gpt`
        gpt-6-astra          → 模板 `gpt-*-astra`      → 线 `gpt`
        claude-fable-5-1     → 模板 `claude-fable-*`   → 线 `claude-fable`
        o3-pro               → 模板 `o*-pro`           → 线 `o`
        gemini-3.1-pro       → 模板 `gemini-*-pro`     → 线 `gemini`

    claude 的三条线（opus / sonnet / fable）仍然分得开 —— 它们的名字里
    版本号排在产品名之后，所以前缀本来就不同。而 gpt 的各种后缀变体
    （版本号在前）自动归并到同一条线，无需维护清单。

    读不出版本号的名字没有模板可用，退回整个名字当线名 —— 它们在
    `newest_generation_per_line` 里本来就会被剔除（无版本 = 低等级），
    这里只是保证函数对任何输入都有确定返回值。
    """
    n = bare_name(name)
    series, version = series_and_version(n)
    if version is None or "*" not in series:
        return n
    return series.split("*", 1)[0].rstrip("-.") or n


def _generation_line(name: str) -> str:
    """世代比较用的**细**产品线 = `series_and_version` 的完整模板。

    与 `_product_line` 的分工（2026-09-30 拆开）
    ------------------------------------------
    两者都从 `series_and_version` 出发，但截取位置不同，用途也不同：

        名字            模板              _product_line   _generation_line
        gpt-6-astra     gpt-*-astra       gpt             gpt-*-astra
        gpt-6-sol       gpt-*-sol         gpt             gpt-*-sol
        gpt-6.1-sol     gpt-*-sol         gpt             gpt-*-sol
        gemini-3.1-pro  gemini-*-pro      gemini          gemini-*-pro

      · `_product_line`（粗，`*` 之前那一截）→ 给 `_round_robin` 做**配额**：
        「gpt 的三个同代变体不该占三个轮转位」，所以必须把它们归成一条线。
      · `_generation_line`（细，整个模板）→ 给阶段 B 做**世代收敛**：
        同一条线内比次版本（`gpt-6.1-sol` 压 `gpt-6-sol`），
        不同线之间不比（`gpt-6-astra` 与 `gpt-6.1-sol` 各自保留）。

    原来两处共用 `_product_line`，于是 `gpt-6.1-sol` 的次版本把同属最新主
    版本的 `gpt-6-astra` / `gpt-6-luna` 一起淘汰，codex 段只剩 1 个模型 ——
    违反要求②「gpt-6 系列**所有**模型名称」与要求③「同级系列全勾」。
    实测依据见 `tests/test_generation_siblings.py`。

    为什么不改 `_product_line` 本身：它的粗粒度对轮转配额是**对的**，
    改了会让 compat 段的前 N 个全是 gpt 变体。两个判据本来就该分开。

    读不出版本号时退回整个名字 —— 与 `_product_line` 同一套兜底理由
    （无版本的名字在阶段 A 之前就被剔掉了，这里只保证函数总有返回值）。
    """
    n = bare_name(name)
    series, version = series_and_version(n)
    if version is None or "*" not in series:
        return n
    return series


# 一个模型名要被当作「这个段的通用候选」，至少得有这么多个**不同的站**在用。
# 只有一个站在用的名字不外推 —— 见 `_cfg_models` 的说明。
_CFG_MODEL_MIN_HOSTS = 2


def _cfg_models(cfg: dict | None, section: str) -> list[str]:
    """config.yaml 该段里**多个站都在用**的模型名（去重，保持出现顺序）。

    只读**本段**：同一个名字在不同段的可用性完全不同（compat 段能转
    claude-opus-5，codex 段不能），跨段取会把不该进来的名字带进来。

    为什么还要卡「几个站在用」（2026-09-11，修 `claude-fake-5`）
    -------------------------------------------------------
    这一层是 `latest_models` 的第 2 层：「本地 config.yaml 里已经写着的模型名
    也算候选」。设计本身是对的 —— 站方特供型号只在这一层出现，实测有 4 个
    确实能用的名字不在 CPA 权威名录里。

    但它原来**不区分「这个名字属于哪个站」**：A 站写了什么，B 站判死后的兜底
    清单就能拿到什么。`claude-fake-5` 的来路已查清就是这条 ——
    它不在本仓库、不在桌面 config.yaml、不在 Go 参考实现、也不在远程 CPA
    权威名录（实拉 77 个模型、17 个 `claude-*`，`fake` 零命中），
    唯一来源是某个站的条目里写着它，然后被外推给了另一个站。
    `name_is_safe()` 只挡非法字符（中文、`[`、空格），挡不住「合法但不存在」。

    门槛的依据：**一个手误只会出现在一个站**，而真正的站方特供型号通常有
    多个同类站都在卖。所以「≥2 个不同的站在用」能滤掉手误，又保住特供型号。
    只有一个站用的名字仍然对**那个站自己**有效 —— 那条路径走的是
    `existing_models_for`（按 (段, 站, Key) 精确取原清单），不经过这里。

    「站」这一维用 `entry_scope` —— 与 `CarryTables` / `existing_models_for`
    同口径（前三段取 host，compat 段取含路径的 provider 身份）。
    这个键在本项目分叉过两次，不许再写第三份。
    """
    if not isinstance(cfg, dict):
        return []
    from .batch import entry_scope

    order: list[str] = []
    hosts: dict[str, set[str]] = {}

    def take(entry) -> None:
        if not isinstance(entry, dict):
            return
        scope = entry_scope(section, str(entry.get("base-url") or ""))
        for m in entry.get("models") or []:
            n = m.get("name") if isinstance(m, dict) else m
            if not (isinstance(n, str) and n.strip()):
                continue
            n = n.strip()
            if n not in hosts:
                hosts[n] = set()
                order.append(n)
            hosts[n].add(scope)

    for entry in cfg.get(section) or []:
        take(entry)
    return [n for n in order if len(hosts[n]) >= _CFG_MODEL_MIN_HOSTS]


# ---------------- 产品线可信度闸（2026-09-16，修 claude-fake-5）----------

def known_product_lines(*, timeout: int = 8,
                        proxy: str | None = None) -> set[str]:
    """CPA 权威名录里出现过的**产品线**集合。拉不通返回空集。

    返回产品线而不是名字本身：站方特供变体（`claude-opus-5-max`、
    `gemini-3.1-pro-request-antigravity`）不在名录里，但它们的产品线
    （`claude-opus` / `gemini`）在 —— 按名字比会把这些真模型全杀掉，
    按产品线比才能只杀掉凭空捏造的那一类。
    """
    names, _why = remote_names(timeout=timeout, proxy=proxy)
    return {_product_line(n) for n in names if n}


def implausible_models(models: list[str], *, proven: list[str] | None = None,
                       lines: set[str] | None = None) -> list[str]:
    """挑出「产品线在权威名录里查无此线、且本轮没实测过」的名字。

    为什么需要这道闸（2026-09-16，`claude-fake-5` 的第二次）
    ------------------------------------------------------
    `_CFG_MODEL_MIN_HOSTS` 那道闸（本文件 :1552）只管 **cfg 层**，判据是
    「≥2 个站在用就不算手误」。实测它已经失守：线上 config.yaml 的 claude 段
    有 **10 个不同的站**在用 `claude-fake-5`，门槛 2 形同虚设 —— 一旦一个假名
    被写回去，下一轮重探就把它当成「多站公认」再抄一遍，自我加固。

    而走 `v.catalog`（站方 /models 列表）与 `prior`（原 config.yaml 条目清单）
    这两条路进来的名字**一道校验都没有**：站方目录说有什么就是什么。
    现场快照实测：`claude-fake-5` 在 12 个站上处于勾选态、共 132 次。

    判据为什么是「产品线 + 未实测」这个**合取**，不是单看产品线
    -------------------------------------------------------
    单看产品线会误杀。拿线上 config.yaml 的 77 个模型名实测过：产品线不在
    名录里的有 10 个，其中 `claude-glm-5.2` / `Claude4.6` 这类**可能是真的**
    （站方把别家模型挂在 claude 前缀下卖，CPA 的 compat 段确实能转）。

    所以加上「本轮实测过就一律放行」这个安全阀：`v.models` 里的每一个名字
    都过了 `pipeline._accept`（真 200 + 模型名对得上 + 正文不是错误体），
    **实测证据比名录硬** —— 名录只是「CPA 官方知道有这个」，站方卖什么
    它管不着。于是：
      · 实测通过        → 放行（哪怕名录里没有）
      · 未实测 + 线在册 → 放行（`claude-opus-5-max` 这类特供变体）
      · 未实测 + 线不在册 → 拦下（`claude-fake-5` 死在这里）

    拉不通名录时**整体放行**（`lines` 为空即返回 `[]`）
    ------------------------------------------------
    国内 VPS 直连 GitHub 不通是常态（见 `remote_names` 的说明）。那时若按
    「不在册就拦」处理，等于一次网络抖动把所有段的模型清单清空 —— 写进
    config.yaml 的是 0 个模型，比填错严重得多。失败必须朝安全侧倒。

    **四族之外的名字一律不判**（2026-09-16 被 test_offfamily 抓到）
    ----------------------------------------------------------
    CPA 权威名录收录的是 CPA 自己认识的模型，而 `FAMILIES` 之外的
    grok / glm / deepseek / qwen / llama **本来就不在册** —— 名录对它们
    没有发言权，拿它判等于把「名录没收录」当成「这个模型不存在」。

    而这批恰恰是 compat 段最需要保住的：那条路走 `/chat/completions`，
    CPA 对模型名零校验（见 `section_protocol_ok` 的源码引用），能不能用
    只取决于上游认不认。配置注释记的实测是 romeo 的 vip 分组**只有
    grok-4.6 有渠道**、且它是唯一端到端验证过的模型。误杀它等于把
    「有一个确认可用的模型」变成「一个都没有」。

    所以只对四族内的名字判在册性：`claude-fake-5` 族是 claude、线是
    `claude-fake`、名录里查无此线 → 拦；`glm-5.2` 族认不出 → 不判。

    返回的是**要拦下的名字**，由调用方决定怎么处置（过滤 / 只告警）。
    """
    if lines is None:
        lines = known_product_lines()
    if not lines:
        return []                       # 名录拉不通 —— 不拦，见上
    ok = {bare_name(n) for n in (proven or []) if n}
    return [n for n in (models or [])
            if n and bare_name(n) not in ok
            # 四族之外名录管不着，见上；只有族认得出来才谈在册性
            and family(n)
            and _product_line(n) not in lines]
