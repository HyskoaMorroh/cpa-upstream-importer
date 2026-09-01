"""把探测结论变成可写入的方案：去重、定档、影响面。

三件事，每件都有踩过的坑作为依据：

去重（§8）—— 段间行为相反，两种失败模式都必须挡：
    gemini-api-key   按五元组静默丢弃重复条目
                     (config_normalization.go:237-273)
                     后果：你以为加了，实际悄悄少一个
    其余三段         完全不去重，重复导入注册成两个独立凭据
                     后果：轮询池里占两个位，同一个坏 key 被抽中两次
    加 -N 后缀发生在另一层（synthesizer/helpers.go:44-50，Auth 合成时），
    不是配置层去重，别混淆。

定档（§7）—— priority 数值大者优先，且**层级隔离**：
    selector.go:325-333  if priority > bestPriority，只保留最大那一桶
    低档凭据只在更高档全部不可用时才参与。所以插档不是「排个序」，
    而是「决定它跟谁同层、把谁挡在后面」。

影响面 —— atlas 记的教训：第一版 620 方案劫持了 4 个模型的顶层。
    改 priority 前必须枚举该层会吃到哪些模型。本模块对每个新条目
    声明的每个模型，算出当前顶层是谁、新值会不会越过它。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .parse import ParsedRow, base_for_section, host_of

# 只有 gemini 段在配置层去重（静默丢弃）
_DEDUP_SECTIONS = {"gemini-api-key"}


# ---------------- 去重 ----------------


def dedup_key(
    section: str,
    *,
    api_key: str,
    base_url: str,
    proxy_url: str = "",
    prefix: str = "",
    headers: dict[str, str] | None = None,
) -> str:
    """五元组指纹。与 CPA 的 formatGeminiKeyDedupID 同口径。

    gemini 段用它判静默丢弃；其余段 CPA 不判，但我们自己必须判 ——
    否则重复条目会注册成两个独立凭据。
    """
    h = json.dumps(headers or {}, sort_keys=True, ensure_ascii=False)
    raw = f"{section}\x00{api_key}\x00{base_url}\x00{proxy_url}\x00{prefix}\x00{h}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def credential_pair(api_key: str, base_url: str) -> str:
    """「这个 Key 在这个站」的身份 —— 只取 (api-key, base-url)。

    与 dedup_key 的五元组是**两个不同的问题**，都要判：

    · dedup_key（五元组，与 CPA 的 formatGeminiKeyDedupID 同口径）问的是
      「这两行配置是否完全相同」。CPA 用它决定要不要丢弃重复行。

    · credential_pair 问的是「这个凭据在这个站是不是已经配过了」。
      导入工具需要的是这一个 —— 实测踩到：relay-f 的某个 Key 在 claude 段
      已存在（带 prefix: ANT 和一个 UA），探测得出的方案没有那两项，
      五元组指纹因此不同，于是被判成新 Key 又写了一条。结果同一个凭据
      在同一个站出现两次，轮询池里占两个位、坏了一起坏。

    base-url 末尾斜杠归一化 —— https://x.com 与 https://x.com/ 是同一个站。
    分隔符用 "|"：Key 里理论上可含空格，用空格分隔会让
    ("a b", "c") 与 ("a", "b c") 撞成同一个指纹。
    """
    return f"{(api_key or '').strip()}|{(base_url or '').strip().rstrip('/')}"


def dominant_prefix(cfg: dict, section: str) -> str:
    """该段现有条目里占绝对多数的 prefix。没有主导值则返回空串。

    为什么要沿用：`prefix` 在 `force-model-prefix: false`（你的配置）下是
    **额外加一个命名空间别名**，不取代原名 —— `applyModelPrefixes`
    （service_models.go:600-614）对每个模型同时注册 `claude-opus-5` 与
    `ANT/claude-opus-5`。

    所以缺 prefix 不会让新站不可用，但会让它少掉 `ANT/...` 这一半别名。
    实测你的 config.yaml：gemini 段 GLE 64/64、codex 段 CDX 63/65、
    claude 段 ANT 61/65 —— 既有约定很明确，新条目不带就成了异类，
    按 `ANT/xxx` 发的请求会命中不到它。

    判定「主导」的阈值是 70%：低于这个比例说明该段本来就不统一，
    那就不猜，留空让用户自己定。
    """
    entries = [e for e in (cfg.get(section) or []) if isinstance(e, dict)]
    if not entries:
        return ""
    counts: dict[str, int] = {}
    for e in entries:
        counts[str(e.get("prefix") or "")] = counts.get(str(e.get("prefix") or ""), 0) + 1
    best, n = max(counts.items(), key=lambda kv: kv[1])
    if not best:
        return ""                       # 主导值是「无 prefix」
    return best if n / len(entries) >= 0.7 else ""


def extract_existing_entries(cfg: dict) -> list[tuple[str, str, str, dict]]:
    """从 config.yaml 提取所有既有站的完整信息（用于全量重探）

    Returns:
        [(section_short, base_url, api_key, original_entry), ...]

        section_short: "gemini" | "codex" | "claude" | "compat"
        original_entry: 原始 dict，包含 priority/headers/proxy-url/models 等
    """
    entries = []

    section_map = {
        "gemini-api-key": "gemini",
        "codex-api-key": "codex",
        "claude-api-key": "claude",
    }

    # 前三段：每个条目一个 api-key
    for section_full, section_short in section_map.items():
        for e in cfg.get(section_full) or []:
            if not isinstance(e, dict):
                continue
            base_url = str(e.get("base-url") or "")
            api_key = str(e.get("api-key") or "")
            if base_url and api_key:
                entries.append((section_short, base_url, api_key, e))

    # compat 段：provider 级 base-url + api-key-entries 里的多个 key
    for e in cfg.get("openai-compatibility") or []:
        if not isinstance(e, dict):
            continue
        base_url = str(e.get("base-url") or "")
        for ke in e.get("api-key-entries") or []:
            if isinstance(ke, dict):
                api_key = str(ke.get("api-key") or "")
                if base_url and api_key:
                    # compat 段的 original_entry 是 provider 级配置
                    entries.append(("compat", base_url, api_key, e))

    return entries


def existing_pairs(cfg: dict) -> dict[str, set[str]]:
    """现有 config.yaml 里每段已配过的 (key, base) 对。"""
    out: dict[str, set[str]] = {}
    for section in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        pairs = set()
        for e in cfg.get(section) or []:
            if isinstance(e, dict):
                pairs.add(credential_pair(str(e.get("api-key") or ""),
                                          str(e.get("base-url") or "")))
        out[section] = pairs

    pairs = set()
    for e in cfg.get("openai-compatibility") or []:
        if not isinstance(e, dict):
            continue
        base = str(e.get("base-url") or "")
        for ke in e.get("api-key-entries") or []:
            if isinstance(ke, dict):
                pairs.add(credential_pair(str(ke.get("api-key") or ""), base))
    out["openai-compatibility"] = pairs
    return out


def existing_fingerprints(cfg: dict) -> dict[str, set[str]]:
    """现有 config.yaml 的全部指纹，按段分组。"""
    out: dict[str, set[str]] = {}
    for section in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        keys = set()
        for e in cfg.get(section) or []:
            if not isinstance(e, dict):
                continue
            keys.add(
                dedup_key(
                    section,
                    api_key=str(e.get("api-key") or ""),
                    base_url=str(e.get("base-url") or ""),
                    proxy_url=str(e.get("proxy-url") or ""),
                    prefix=str(e.get("prefix") or ""),
                    headers=e.get("headers") or {},
                )
            )
        out[section] = keys

    # compat 段结构不同：provider 级 base-url + api-key-entries 里的 key
    keys = set()
    for e in cfg.get("openai-compatibility") or []:
        if not isinstance(e, dict):
            continue
        base = str(e.get("base-url") or "")
        hdrs = e.get("headers") or {}
        prefix = str(e.get("prefix") or "")
        for ke in e.get("api-key-entries") or []:
            if not isinstance(ke, dict):
                continue
            keys.add(
                dedup_key(
                    "openai-compatibility",
                    api_key=str(ke.get("api-key") or ""),
                    base_url=base,
                    proxy_url=str(ke.get("proxy-url") or ""),
                    prefix=prefix,
                    headers=hdrs,
                )
            )
    out["openai-compatibility"] = keys
    return out


# ---------------- 档位谱 ----------------


@dataclass
class Band:
    """一个段的现存档位谱。"""

    section: str
    tiers: list[int] = field(default_factory=list)          # 降序
    hosts_at: dict[int, list[str]] = field(default_factory=dict)
    model_top: dict[str, int] = field(default_factory=dict)  # 模型 -> 当前顶层 priority
    # 模型 -> {档位: 承载该模型的站点}。算「挡住谁」必须按模型分开 ——
    # 30 档上有 6 个站，但只有声明了同一个模型的那几个才会被挡。
    model_tiers: dict[str, dict[int, list[str]]] = field(default_factory=dict)
    # 已被 weight: 0 逐出调度池的站。**挡住它们没有任何代价** ——
    # selector.go:423-430 的 positiveWeightAuths 已经把零权重凭据整个剔除，
    # 它们本来就不会被选中。
    #
    # 为什么必须区分（2026-08-30 实测发现的定档缺陷）：
    # 原来 _shadow_count 把所有下层站等权计数，于是「不挡任何站」成了目标，
    # 满分候选也只拿到 25 或 12。而 gemini 段下层那 49 个站**全部实测不可用**
    # （逐站 503/401/403/404），保护它们毫无意义 —— 却把新站压到了最低档。
    dead_hosts: set[str] = field(default_factory=set)
    # 从 config.yaml 注释里解析出的「实测不可用」站。同样不值得保护。
    # 注释形态：`# <站名>：实测 503 No available channel`、`# xxx 永久排除`。
    # 这是弱信号（注释可能过期），所以只用于**降低挡住它们的代价**，
    # 不用于直接排除 —— 真要排除该由用户用 weight: 0 显式表达。
    unhealthy_hosts: set[str] = field(default_factory=set)
    # 人读短名 -> 域名。注释里写短名，配置里是域名，两者不保证有公共子串
    # （jdw -> relay-h.example）。见 name_alias_map。
    alias: dict[str, str] = field(default_factory=dict)
    # 注释里提到、但匹配不上任何现有站的短名。**这是诊断信息，不是错误** ——
    # 它说明该站的「实测不可用」结论没能作用到定档上（静默漏判）。
    # 成因：别名表只能从 compat 段的 name 字段建，另三段没有 name 字段。
    unmatched_notes: list[str] = field(default_factory=list)

    @property
    def top(self) -> int:
        return self.tiers[0] if self.tiers else 0

    def gaps(self) -> list[tuple[int, int]]:
        """可插空档 (下界, 上界)，宽度 > 1 才算。降序。"""
        out = []
        for hi, lo in zip(self.tiers, self.tiers[1:]):
            if hi - lo > 1:
                out.append((lo, hi))
        return out

    def shadowed(self, model: str, priority: int) -> dict[int, list[str]]:
        """插到 priority 后，该模型上被挡在新站之后的档位与站点。

        层级隔离的直接后果：这些站只在新站**也**不可用时才会被尝试。
        不是「略微靠后」，是整层被跳过。
        """
        per = self.model_tiers.get(model) or {}
        return {p: hosts for p, hosts in per.items() if p < priority}


def _models_of(entry: dict) -> list[str]:
    out = []
    for m in entry.get("models") or []:
        if isinstance(m, str):
            out.append(m)
        elif isinstance(m, dict):
            name = m.get("alias") or m.get("name")
            if name:
                out.append(str(name))
    return out


# 「这个站实测不可用」的注释形态。用于给挡住它降权，不用于直接排除。
# 全部来自本部署 config.yaml 里真实出现过的写法。
_DEAD_NOTE = re.compile(
    r"实测\s*(?:40[0-9]|41[0-9]|42[0-9]|50[0-9])"      # 实测 403 / 503 / 500 …
    r"|永久排除"
    r"|站点级不可用"
    r"|No available channel"
    r"|not implemented"
    r"|model_not_found"
    r"|分组权限被回收"
    r"|静默(?:重映射|替换)模型"
)
# 站名在注释里的两种位置：
#   `# relay-f：实测 503 …`      冒号分隔（多数）
#   `# relay-e 永久排除（2026-08-27）` 空格分隔，无冒号
# 只要冒号形态会漏掉后者 —— 实测踩到：relay-e 在 codex 段的「永久排除」
# 注释因此没被解析出来。
_HOST_IN_NOTE = re.compile(
    r"#\s*([A-Za-z0-9][A-Za-z0-9.\-]{2,})\s*(?:[:：]|\s+(?=永久排除|站点级不可用))")

# 绝不可能是站名的词。全部是 config.yaml 里真实出现在 `# xxx:` 位置的东西 ——
# 注释掉的 YAML 键、日期、正文里的字段名。
#
# 为什么必须显式排除（2026-08-30 自查发现）：
# `_HOST_IN_NOTE` 只要求「# 后面跟一串字母数字再跟冒号」，于是
#     # weight: 0  站点级不可用          -> 抓出 "weight"
#     # disabled: true  实测 503 …       -> 抓出 "disabled"
#     # 2026-08-27：实测 403 WAF         -> 抓出 "2026-08-27"
#     # cZone: 'relay-g.example'、…        -> 抓出 "czone"
# 6 种形态实测 6/6 全部误判。垃圾进 unhealthy_hosts 之后，host_matches_note
# 的宽松兜底会让它们误匹配含该词的真实域名（priority.example.com、
# models.aliyun.com、base-url.io 实测全部命中）——
# **活站被当成死站，新站于是拿到过高的档位，压住真正可用的站。**
#
# 当前这 13 个站没踩到只是运气：没有一个域名含这些词。
_NOT_A_HOST = frozenset("""
api-key base-url proxy-url prefix priority weight models headers name alias
disabled enabled request-scoped-errors api-key-entries max-context-length
excluded-models fingerprint-profile action code status message error
czone cf-ray note todo fixme warning tip
""".split())


def _looks_like_host(name: str) -> bool:
    """这个词能当站名吗。

    三条排除：
      · 在 _NOT_A_HOST 里（注释掉的 YAML 键、字段名）
      · 纯数字或日期形态（2026-08-27、20260830）—— 站名不会是这样
      · 全大写且不含点（CF_APP_WAF、TODO 这类）
    """
    n = (name or "").strip().lower()
    if not n or n in _NOT_A_HOST:
        return False
    # 纯数字 / 日期：去掉点与连字符后全是数字
    if re.fullmatch(r"[\d.\-]+", n):
        return False
    # 至少要有一个字母
    if not re.search(r"[a-z]", n):
        return False
    return True



# 顶层键：零缩进，可带引号。
#
# 为什么要认引号（2026-08-30 自查）：原来是 `^[a-zA-Z_][a-zA-Z0-9_-]*\s*:`，
# 遇到 `"codex-api-key":` 这种合法 YAML 写法认不出来，于是段边界算错 ——
# 后一段的死站注释被并进前一段的 unhealthy_hosts。**跨段串扰**，与
# 「同一站在不同段结论不同」的设计意图直接冲突（relay-f 在 claude 段
# 实测 200、在 gemini 段 503，串了就会把好站当死站）。
_TOP_KEY = re.compile(r"""^(?:['"]?)[A-Za-z_][A-Za-z0-9_.\-]*(?:['"]?)\s*:""")

# 「已恢复」类措辞。命中就**不**判为不可用 —— 哪怕同一行也提到 503。
#
# 为什么必须有（自查发现）：`_DEAD_NOTE` 只看有没有失败关键词，于是
#     # oldhost：实测 503（2026-08-01），已于 2026-08-20 恢复正常
# 仍被判成当前不可用。注释是累积写的，恢复记录往往追加在原结论后面 ——
# 不排除的话，越是记录详细的站越容易被误判成死站。
_RECOVERED = re.compile(
    r"已(?:恢复|修复|解封|放开|开通|上线)"
    r"|恢复正常|恢复可用|现已可用|已可用"
    r"|重新(?:可用|上线)"
    r"|(?:后|现在)(?:已|可)(?:通|用)"
    r"|已(?:可|能)(?:通|用|访问)"      # 「换出口 IP 后已可通」
    r"|后(?:可|能)(?:恢复|通|用)"      # 「加 proxy-url 后可能恢复」—— 这是推测，
                                       # 但推测也说明当前结论不确定，不该当死站
)


# 转折词：出现在「已恢复」之后就否决那个恢复结论。
#
# 为什么需要（交叉审计 2026-08-30 抓到）：`_RECOVERED` 只看有没有「已恢复」，
# 于是这些**仍然不可用**的注释被误放过：
#     # xxx：实测 503，已恢复但又挂了
#     # xxx：实测 503，站方称已恢复，实测仍 503
#     # xxx：实测 403，已恢复后再次被封
#     # xxx：实测 503，已修复但仍不稳定
# 4/4 全部被当成「当前可用」。
#
# 这个误判方向是**把死站当活站** —— 定档会为了保护一个实际不可用的站
# 而压低新站。比反方向（把活站当死站）后果轻，但同样是错的。
#
# 注释是累积写的，「恢复了又挂」这种反复本来就常见 —— 越是记录详细的
# 站越容易踩到。
_REVERSAL = re.compile(
    r"但|然而|不过|却"
    r"|又(?:挂|坏|不可用|失败|503|403)"
    # 「仍」与后面的词之间可能隔空格：「实测仍 503」「仍 403」。
    # 不放开 \s* 就漏掉这种最常见的写法（交叉审计的反例正是它）。
    r"|仍(?:然)?\s*(?:不|无|挂|失败|\d{3})"
    # 「称已恢复」「据说已恢复」—— 转述而非实测，不能当作恢复依据
    r"|(?:称|据说|声称|说是)\s*已"
    r"|再次(?:被封|失败|不可用|挂)"
    r"|依旧|依然不"
    r"|未(?:能|真正)恢复"
)


def unhealthy_from_comments(raw: str, section: str) -> set[str]:
    """从 config.yaml 原文里解析该段被实测判为不可用的站。

    为什么要读注释：`Band` 原本只看 priority 与 base-url，于是把「实测全挂
    的 49 个站」也当成要保护的现有站，把满分新站压到 12 分档。而这些站的
    可用性结论**只存在于注释里** —— 那是两夜排障的唯一记录。

    这是弱信号，只用于降低「挡住它们」的代价权重。注释可能过期，所以
    绝不用它直接排除任何站 —— 真要排除该由用户写 weight: 0 显式表达。

    只在**本段范围内**匹配：同一个站在不同段的结论完全不同
    （relay-f 在 claude 段实测 200，在 gemini 段是 503）。
    """
    lines = raw.splitlines()
    st = None
    for i, l in enumerate(lines):
        # 段头同样可能带引号。不认的话整段信号直接丢失 ——
        # 比段尾算错（串扰）更严重，且同样静默。
        if re.match(rf"""^(?:['"]?){re.escape(section)}(?:['"]?)\s*:""", l):
            st = i
            break
    if st is None:
        return set()
    en = len(lines)
    for i in range(st + 1, len(lines)):
        if _TOP_KEY.match(lines[i]):
            en = i
            break

    out: set[str] = set()
    for i in range(st, en):
        l = lines[i]
        if not l.lstrip().startswith("#"):
            continue
        if not _DEAD_NOTE.search(l):
            continue
        if _RECOVERED.search(l) and not _REVERSAL.search(l):
            # 提到已恢复、且没有转折词否决它 —— 不算当前不可用。
            # 带转折的（「已恢复但又挂了」）仍按不可用处理。
            continue
        m = _HOST_IN_NOTE.search(l)
        if m and _looks_like_host(m.group(1)):
            out.add(m.group(1).lower())
    return out


def name_alias_map(cfg: dict, *,
                   conflicts: list[str] | None = None) -> dict[str, str]:
    """站的人读短名 -> 域名。从 openai-compatibility 段的 `name` 字段建。

    为什么需要显式表、不能靠字符串猜（2026-08-30 踩到）：
    注释里写的是人读短名，而 base-url 里是域名，两者**不保证有公共子串**：

        jdw  ->  relay-h.example      （jdw ≠ relay-h）
        sm        ->  relay-m.example
        relay-a    ->  relay-a.example
        relay-e       ->  relay-e.example

    第一版用「短名是域名的点分标签之一」来匹配，jdw 静默漏判 ——
    于是 gemini 段把实测 503 的 relay-h 当成活站保护，新站又被压低。
    这种漏判不会报错，只会让定档悄悄变保守，极难发现。

    compat 段每个 provider 都同时有 `name` 与 `base-url`，是文件里唯一
    权威的对应关系。用它，不猜。

    重名冲突要丢掉，不能让后者覆盖前者（2026-08-31 自查发现）
    ------------------------------------------------------
    原来是 `out[nm] = host` 直接赋值。compat 段两个 provider 用了同一个
    `name`（复制粘贴条目忘改名、或 name 写错）时，后一条把前一条覆盖掉，
    于是那个短名的注释结论**指向了另一个站**：真站的「实测 503」不再作用于
    它自己，反而被算到冒名站头上。

    误判方向正是本模块反复强调的那个更坏的方向 —— 把活站当死站，
    新站因此拿到过高档位，把真正可用的站压在后面。

    所以重名且指向不同 host 时，两条都不进表：退化成宽松匹配（漏判，
    只让定档更保守），并记进 conflicts 供上层暴露。
    """
    seen: dict[str, set[str]] = {}
    for p in cfg.get("openai-compatibility") or []:
        if not isinstance(p, dict):
            continue
        nm = str(p.get("name") or "").strip().lower()
        host = host_of(str(p.get("base-url") or "")).lower()
        if nm and host:
            seen.setdefault(nm, set()).add(host)

    out: dict[str, str] = {}
    for nm, hosts in seen.items():
        if len(hosts) == 1:
            out[nm] = next(iter(hosts))
        elif conflicts is not None:
            conflicts.append(
                f"{nm} 同时指向 {len(hosts)} 个站（{'、'.join(sorted(hosts))}）"
                "—— 该短名的注释结论无法归属，已按不匹配处理"
            )
    return out


def host_matches_note(host: str, note_names: set[str],
                      alias: dict[str, str] | None = None) -> bool:
    """域名与注释里的短站名对得上吗。

    两条路，都是**精确**匹配：
      1. alias（name_alias_map 建的权威表）—— 域名完全相等
      2. 短名是域名的某个点分标签，或与整个域名相等

    **不做前缀匹配。**曾经有过
        len(nl) >= 4 and any(lb.startswith(nl) or nl.startswith(lb) ...)
    自查实测它会把不同的站判成同一个（2026-08-30）：

        api.aliyuncs.com  被短名 aliyun    命中（aliyuncs.startswith(aliyun)）
        api.oaipro.com    被短名 oaiproxy  命中（oaiproxy.startswith(oaipro)）
        api.justdo.com    被短名 jdw 命中

    误判方向是**把活站当死站** —— 后果是新站拿到过高档位，把真正可用的
    站压在后面。这比「漏判一个死站」严重得多（漏判只是定档偏保守）。
    所以宁可漏，不可错：不共享标签的短名（jdw → relay-h.example）
    交给别名表处理，别名表覆盖不到的就漏判 —— 那只让定档更保守。
    """
    h = (host or "").lower()
    if not h:
        return False
    if alias:
        for n in note_names:
            nl = n.lower()
            if alias.get(nl) == h:
                return True
        # 别名表里没有的名字，仍走下面的宽松匹配（新站可能还没进 compat 段）
    labels = set(h.split("."))
    for n in note_names:
        nl = n.lower()
        if alias and nl in alias:
            continue                     # 已由别名表判定过，不重复宽松匹配
        if nl in labels or h == nl:
            return True
    return False


def entry_weights(section: str, entry: dict) -> list:
    """取出一个条目的全部 weight 值。返回列表（可能多个）。

    为什么不能直接读 `entry.get("weight")`（2026-08-30 自查发现的缺陷）：
    **compat 段的结构与另三段不同** —— weight 在 `api-key-entries` 的每一项里，
    provider 级没有这个字段：

        openai-compatibility:
          - name: "xxx"
            base-url: "https://xxx/v1"
            api-key-entries:
              - api-key: "k1"
                weight: 0          ← 在这里
              - api-key: "k2"
                weight: 0

    原来统一按条目级读，compat 段永远读到 None —— 一个全部 key 都
    weight:0 的 provider 会被当成「活站」保护，新站因此被压低。
    这与整段修复的意图直接矛盾，且只影响 compat 段（最难发现的那种）。

    返回 [] 表示没有任何显式 weight —— **与 [0] 是完全不同的含义**：
    没设 weight 走 credentialweight.Default（=1，selector.go:166-168），
    显式设 0 才被 positiveWeightAuths 剔除。
    """
    if section == "openai-compatibility":
        out = []
        for ke in entry.get("api-key-entries") or []:
            if not isinstance(ke, dict):
                continue
            # 没设 weight 的项也要收 —— 记成 None 表示「走默认值 1，是活的」。
            # 只收有 weight 键的会漏掉它们，于是一个「c 是 0、d 没设」的
            # provider 被算成 [0] 全零 = 整站死掉，而它其实还有一个活 key。
            # （这是我修 compat 缺陷时自己引入的 bug，测试当场抓到）
            out.append(ke.get("weight") if "weight" in ke else None)
        return out
    return [entry.get("weight") if "weight" in entry else None]


def entry_all_zero_weight(section: str, entry: dict) -> bool:
    """这个条目的所有凭据都被 weight:0 逐出了吗。

    compat 段一个 provider 带多个 key —— 只有**全部**为 0 才算整站死掉。
    部分为 0 说明那几个 key 有问题，站本身还在服务。
    """
    ws = entry_weights(section, entry)
    if not ws:
        return False
    # None 表示「没设 weight」= 走 credentialweight.Default(=1) = 活的。
    # 所以只有全部都被逐出才算整站死掉。
    return all(_weight_is_zero(w) for w in ws)


def _weight_is_zero(w) -> bool:
    """这个 weight 值会被 CPA 的选择器剔除吗。

    与 CPA 对齐，不是简单的 `w == 0`（自查 2026-08-30 发现）：

        internal/credentialweight/weight.go:21-24
            if weight <= 0 { return 0, nil }      ← **负数也归零**

    归零后 `positiveWeightAuths`（selector.go:423-430）把它整个剔除。
    所以 `weight: -1` 与 `weight: 0` 效果完全相同 —— 只判 `== 0` 会漏掉负数，
    把一个已被逐出的站当成活站保护，新站因此被压低。

    None（没设 weight）不算 —— 那走默认值 1，是活的。
    字符串形态（YAML 里写 `weight: "0"`）也认：CPA 侧的
    validateCredentialWeightYAML（internal/config/weight.go:74-76）会要求
    整数、拒绝这份配置，但在它拒绝之前我们不该把它当成活站。
    """
    if w is None:
        return False
    if isinstance(w, bool):
        return w is False               # False == 0，显式处理避免歧义
    if isinstance(w, (int, float)):
        return w <= 0
    # 字符串：能转数字就按数字判，不能转就不算（交给 CPA 的校验去拒）
    try:
        return float(str(w).strip()) <= 0
    except (TypeError, ValueError):
        return False


def build_band(cfg: dict, section: str, *, raw: str = "") -> Band:
    """从现有 config.yaml 算出该段的档位谱与每个模型的当前顶层。

    raw 是 config.yaml 原文，给了才能解析注释里的「实测不可用」结论。
    不给也能工作（unhealthy_hosts 为空），只是定档会偏保守。
    """
    band = Band(section=section)
    tiers: dict[int, list[str]] = {}
    model_top: dict[str, int] = {}
    model_tiers: dict[str, dict[int, list[str]]] = {}
    dead: set[str] = set()

    for e in cfg.get(section) or []:
        if not isinstance(e, dict):
            continue
        pri = e.get("priority")
        if not isinstance(pri, int):
            continue
        host = host_of(str(e.get("base-url") or ""))
        # weight: 0 是**强信号** —— CPA 的选择器已经把它整个剔除
        # （selector.go:423-430 positiveWeightAuths），挡住它零代价。
        # 用 entry_all_zero_weight 而不是 e.get("weight")：compat 段的 weight
        # 在 api-key-entries 里，条目级读不到（自查发现的缺陷）。
        if host and entry_all_zero_weight(section, e):
            dead.add(host.lower())
        tiers.setdefault(pri, [])
        if host and host not in tiers[pri]:
            tiers[pri].append(host)
        for m in _models_of(e):
            if m not in model_top or pri > model_top[m]:
                model_top[m] = pri
            per = model_tiers.setdefault(m, {})
            at = per.setdefault(pri, [])
            if host and host not in at:
                at.append(host)

    band.tiers = sorted(tiers, reverse=True)
    band.hosts_at = {k: sorted(v) for k, v in tiers.items()}
    band.model_top = model_top
    band.model_tiers = {
        m: {p: sorted(h) for p, h in sorted(per.items(), reverse=True)}
        for m, per in model_tiers.items()
    }
    # 同一个站可能有多个 key，只有**全部** weight: 0 才算真死。
    # 上面按条目累加，这里剔掉那些还有活 key 的站。
    alive: set[str] = set()
    for e in cfg.get(section) or []:
        if not isinstance(e, dict):
            continue
        # 「还有活 key」= 这个条目不是全 0。同一个站可能有多个条目，
        # 任一条目还有活 key 就不算整站死掉。
        if not entry_all_zero_weight(section, e):
            h = host_of(str(e.get("base-url") or "")).lower()
            if h:
                alive.add(h)
    band.dead_hosts = {h for h in dead if h not in alive}
    # 重名冲突并进 unmatched_notes 一起暴露 —— 都是「注释结论没能作用到定档上」，
    # 对用户是同一类事实，没必要多开一个字段。
    alias_conflicts: list[str] = []
    band.alias = name_alias_map(cfg, conflicts=alias_conflicts)
    if raw:
        band.unhealthy_hosts = unhealthy_from_comments(raw, section)
        # 记下「注释里提到、但既不在别名表、也不与任何域名的标签相等」的短名。
        #
        # 为什么要记（2026-08-30 自查）：别名表只能从 openai-compatibility 段的
        # `name` 字段建 —— 另三段的条目**没有 name 字段**（实测 0/199）。
        # 所以一个只在 claude 段出现、从未进 compat 段的站，它的短名注释
        # 永远匹配不上，等价于该站的「实测不可用」结论对定档完全失效，
        # 悄悄退回修复前的状态。
        #
        # 当前这份 config.yaml 里 compat 段恰好覆盖全部 13 站，所以没触发。
        # 但那是巧合，不是保证 —— 加个站到 claude 段而不加到 compat 段就会踩到。
        # 静默漏判最难发现，所以这里把它变成**可见的**：
        # 服务端会把它塞进 /api/context 的响应，前端能显示出来。
        known = set(band.alias)
        all_labels: set[str] = set()
        for hosts in band.hosts_at.values():
            for h in hosts:
                all_labels |= set(h.lower().split("."))
                all_labels.add(h.lower())
        band.unmatched_notes = sorted(
            n for n in band.unhealthy_hosts
            if n not in known and n not in all_labels)
        band.unmatched_notes += alias_conflicts
    return band


# ---------------- 定档 ----------------


def score_verdict(v) -> int:
    """探测质量打分 0-100。只用于**建议**档位，最终由用户确认。

    扣分依据全部来自实测教训：
      静默换模最重 —— 照常计费却返回另一个模型，比不可用更危险
      需代理次之   —— 多一跳，mihomo 挂了这个站就跟着挂
      需补 UA 最轻 —— 写死 headers 即可，且实测值不敏感
    """
    if not v.usable:
        return 0
    s = 100
    if v.swap_detected:
        s -= 50
    if v.need_proxy:
        s -= 20
    if v.need_ua:
        s -= 5
    if len(v.models) <= 1:
        s -= 10
    if v.max_context_length and v.max_context_length < 200_000:
        s -= 10
    return max(s, 1)


def suggest_priority(
    band: Band,
    score: int,
    *,
    models: list[str] | None = None,
    avoid_hijack: bool = True,
    probation: bool = True,
) -> tuple[int, str]:
    """给出建议 priority 与理由。

    三条硬约束：
      1. **不动任何现有值** —— 只在空档里插。
      2. **不劫持顶层** —— 新站不该仅因分数高就抢走某个模型的现有顶层。
         层级隔离下「抢顶层」意味着现有顶层站一次都不会被尝试，那不是
         「略微靠前」而是完全取代。atlas 记的教训：第一版 620 方案曾劫持
         4 个模型的顶层，自查后改用 210。
      3. **试用期默认**（`probation=True`）—— 新站进**最低可插档**，
         而不是按分数进高档。

    为什么第 3 条是默认（2026-08-30 定）：探测分数只能证明「此刻这一次
    请求成功了」，证明不了余额够用、限流阈值、长时间稳定性、深夜是否降级。
    而按分数定档的代价是实测出来的 —— 一个刚探测的新站在 claude 段会拿到
    975，**挡住 6 个已经跑了两夜、证明过自己的站**。这些站只在新站也不可用
    时才被尝试，等于用未知替换已知。

    分数不再决定档位，改为决定**提权建议**：理由里会写明「稳定运行后可提到
    N」，由你在 UI 上显式改。要一步到位按分数定档，传 `probation=False`。
    """
    if not band.tiers:
        return 100, "该段当前为空，取 100 作基准"

    gaps = band.gaps()
    if not gaps:
        v = max(band.tiers[-1] - 5, 1)
        return v, f"无可插空档，置于最低档 {band.tiers[-1]} 之下"

    # 该候选声明的模型里，现有顶层的**最低**值 —— 不越过它。
    # 取 min 而非 max：越过任一模型的顶层就是劫持那个模型。
    # 例：同时声明 opus-5（顶层 1000）与 sonnet-5（顶层 120），插 975
    # 不动 opus-5，却把 sonnet-5 的顶层整个换掉了。按 min=120 才安全。
    ceiling = None
    if avoid_hijack and models:
        tops = [band.model_top[m] for m in models if m in band.model_top]
        if tops:
            ceiling = min(tops)

    allowed = gaps
    if ceiling is not None:
        # 只保留中位数不超过 ceiling 的空档
        allowed = [(lo, hi) for lo, hi in gaps if (lo + hi) // 2 <= ceiling]
        if not allowed:
            # 所有空档都会劫持 —— 贴着 ceiling 之下放
            v = max(ceiling - 5, 1)
            note = (
                f"所有空档均会抢走顶层（该候选模型的最低现有顶层 {ceiling}），"
                f"置于其下 5 点。要提权请手工改"
            )
            return v, note

    # 按得分选档（激进模式用它，试用期模式只拿它当提权参考）
    idx = int((100 - score) / 100 * len(allowed))
    idx = min(idx, len(allowed) - 1)
    by_score = (allowed[idx][0] + allowed[idx][1]) // 2

    if not probation:
        note = f"按得分插入 {allowed[idx][0]}↔{allowed[idx][1]} 空档中位（得分 {score}，第 {idx + 1}/{len(allowed)} 档）"
        if ceiling is not None and len(allowed) < len(gaps):
            note += f"；已避让顶层 {ceiling}，跳过 {len(gaps) - len(allowed)} 个更高空档"
        return by_score, note

    # ---- 试用期（默认）----
    # 目标：在「不挡任何活着的站」的前提下取**尽可能高**的档。
    #
    # 2026-08-30 修正 —— 原来的 tie-break 是致命的：
    #     if n < best_shadow or (n == best_shadow and mid < best)
    # 挡站数相同时取更低值。于是挡 0 站的 850 与挡 0 站的 25 打平后选 25，
    # 定档必然收敛到最低可插档。实测复现：满分候选在 claude 段拿 25、
    # gemini 段拿 12，且 score=100 与 score=60 给出同一个值 —— 分数完全失效。
    #
    # 为什么「取最高」是对的：挡住零个活站意味着**没有任何代价** ——
    # 现有可用站的相对次序一点没变。此时压低档位不但没有收益，反而让新站
    # 排在一堆死站后面（层级隔离下要等前面整层不可用才轮到它），
    # 等于白探测一场。
    #
    # 试用期的真正约束仍在，且更精确：
    #   · 不劫持顶层     —— 由上面的 ceiling 保证
    #   · 不挡活着的站   —— 由 _shadow_count 只数活站保证
    #   · 不超过得分上限 —— 由 by_score 封顶，得分低就进不了高档
    # 三条都满足的最高档，才是「既不伤现状、又真能被用到」的位置。
    zero_cost = []
    for lo, hi in allowed:                       # gaps() 已降序
        mid = (lo + hi) // 2
        if mid > by_score:
            continue                             # 不越过得分支持的上限
        zero_cost.append((mid, _shadow_count(band, models or [], mid)))

    if not zero_cost:
        # 理论上不可达：by_score 本身取自 allowed[idx] 的中点，所以那一档
        # 的 mid 恒等于 by_score，`mid > by_score` 对它永假 —— zero_cost
        # 至少有一个元素。自查（2026-08-30）用 score=0/1/100 多组数据确认
        # 这条分支从未被触发。
        #
        # 但保留它，并**在这里也钳下界** —— 防御性分支的价值就在于上游
        # 数据形态变化时不会给出荒谬的值（priority 必须 >= 1，0 或负数
        # 在 CPA 里的含义未定义）。
        lo, hi = allowed[-1]
        best = max((lo + hi) // 2, 1)
        best_shadow = _shadow_count(band, models or [], best)
    else:
        least = min(n for _m, n in zero_cost)
        # 在「挡活站最少」的候选里取**最高**的那一档
        best = max(m for m, n in zero_cost if n == least)
        best_shadow = least

    # 统一钳下界。gaps() 只保证 hi-lo>1，不保证 lo>=0 —— 若 config.yaml 里
    # 出现负 priority（CPA 不校验下界），中点可能 <=0。
    # priority 0 与负数在 CPA 里语义未定义，绝不能写出去。
    best = max(int(best), 1)

    note = f"试用期档位 {best}"
    if best_shadow == 0:
        note += "（不挡任何**在用**的站）"
    else:
        note += f"（挡 {best_shadow} 个在用站，已是可插档里最少）"
    note += f"；得分 {score} 支持的上限是 {by_score}"
    # 还有更高的档没用上时，必须说清「为什么没取更高」与「代价是什么」——
    # 否则用户只看到一个数字，无从判断该不该手工提。
    if best < by_score:
        n_score = _shadow_count(band, models or [], by_score)
        if n_score > best_shadow:
            note += (f"。没直接取 {by_score} 是因为那会挡 {n_score} 个在用站"
                     f"（当前只挡 {best_shadow} 个）—— 跑几天确认稳定后再手工提")
        else:
            note += f"。跑几天确认稳定后可手工提到 {by_score}"

    # 说清「挡 0 站」不等于「下层没有站」—— 那些是死站，挡了也不亏
    dead_below = _dead_shadowed(band, models or [], best)
    if dead_below:
        note += (f"；其下 {len(dead_below)} 个站已实测不可用或 weight:0，"
                 f"挡住它们无代价（{', '.join(sorted(dead_below)[:3])}"
                 f"{'…' if len(dead_below) > 3 else ''}）")
        # 这一句必须有：自查（2026-08-30）指出「下层全是死站 → 新站拿高档」
        # 这条推理有个前提 —— 那些站**保持**不可用。若它们只是暂时故障
        # （维护窗口、临时风控），恢复后会被新站永久压在后面，
        # 而本工具**没有任何机制在恢复后重新评估**：
        #   · weight: 0 要用户手工删
        #   · 注释里的实测结论要用户手工更新
        # 都不会自动过期。所以把这件事写进理由，让用户知道该复查什么。
        note += ("。注意：这些站若日后恢复，本档位不会自动重算 —— "
                 "weight:0 与注释结论都不会自动过期，需手工复查")
    if ceiling is not None and len(allowed) < len(gaps):
        note += f"；已避让顶层 {ceiling}"
    return best, note


@dataclass
class Impact:
    """影响面：这个新条目对某个模型的现有格局做了什么。

    两件事要分开看：
      hijacks   —— 抢走顶层。该模型原本的首选站再也不会被首选。
      shadowed  —— 挡住下层。这些站只在新站也不可用时才被尝试。
    第二件同样重要，却是「没劫持顶层」时容易被忽略的部分：gemini 段插
    465 不动 relay-g 的 900，但把 30 档那批全挡在后面了。
    """

    model: str
    current_top: int
    new_priority: int
    shadowed: dict[int, list[str]] = field(default_factory=dict)

    @property
    def hijacks(self) -> bool:
        return self.new_priority > self.current_top

    @property
    def shares(self) -> bool:
        return self.new_priority == self.current_top

    @property
    def shadowed_hosts(self) -> list[str]:
        seen: list[str] = []
        for hosts in self.shadowed.values():
            for h in hosts:
                if h not in seen:
                    seen.append(h)
        return seen


def compute_impact(band: Band, models: list[str], new_priority: int) -> list[Impact]:
    """枚举该新条目声明的每个模型，算出它对现有格局的影响。

    层级隔离下这件事很关键：新值高于现有顶层，就意味着**该模型的全部
    请求都先打新站**，现有顶层站一次都不试（除非新站不可用）。
    低于顶层也不是没有影响 —— 比它低的那些档同样被挡在后面。
    """
    out = []
    for m in models:
        top = band.model_top.get(m)
        if top is None:
            continue  # 该模型此段尚无承载站，新增不构成劫持
        out.append(Impact(
            model=m,
            current_top=top,
            new_priority=new_priority,
            shadowed=band.shadowed(m, new_priority),
        ))
    return out


def _shadow_count(band: Band, models: list[str], priority: int) -> int:
    """在该 priority 下，被挡住的**值得保护的**现有站点数。

    2026-08-30 修正 —— 原来这里等权计数所有下层站，导致定档失效：

      · 「不挡任何站」成了优化目标，于是永远收敛到最低可插档
      · 实测复现：满分 100 的候选在 claude 段拿到 **25**、gemini 段拿到 **12**，
        而且 score=100 与 score=80 给出完全相同的值 —— 分数彻底失效
      · 更糟的是它把 gemini 段那 49 个**实测全挂**的站也算作要保护的对象
        （逐站 503/401/403/404），为了不挡死站而把可用新站压到底

    「挡住」在 CPA 里的真实含义只是「排在后面」（层级隔离下要等前面整层
    都不可用才轮到）。挡住一个已经不可用的站，代价是**零** ——
    它本来就不会出活。

    所以现在只数「活着的站」：
      · weight: 0 的站不计   —— 强信号，selector 已把它整个剔除
      · 注释判不可用的不计   —— 弱信号，来自 config.yaml 里的实测记录
    """
    hosts: set[str] = set()
    for imp in compute_impact(band, models, priority):
        if imp.hijacks:
            continue
        hosts.update(imp.shadowed_hosts)

    live = set()
    for h in hosts:
        hl = (h or "").lower()
        if hl in band.dead_hosts:
            continue                                  # weight: 0，零代价
        if band.unhealthy_hosts and host_matches_note(
                hl, band.unhealthy_hosts, band.alias):
            continue                                  # 注释判不可用，零代价
        live.add(hl)
    return len(live)


def _dead_shadowed(band: Band, models: list[str], priority: int) -> set[str]:
    """在该 priority 下被挡住、但**本来就不可用**的站。

    单独列出来是为了让定档理由可核对：「挡 0 站」听起来像下层空无一物，
    实际可能压着 49 个站 —— 只是它们全都实测不通。把这件事写进理由里，
    用户才能判断这个档位是不是真的安全。
    """
    hosts: set[str] = set()
    for imp in compute_impact(band, models, priority):
        if imp.hijacks:
            continue
        hosts.update(imp.shadowed_hosts)
    out = set()
    for h in hosts:
        hl = (h or "").lower()
        if hl in band.dead_hosts or (
                band.unhealthy_hosts
                and host_matches_note(hl, band.unhealthy_hosts, band.alias)):
            out.add(hl)
    return out


def gentler_option(
    band: Band, models: list[str], current: int
) -> tuple[int, int, int] | None:
    """给出「更保守一档」的具体值与代价对比。

    为什么需要它：同一空档内取任何值，被挡站点完全相同 —— gemini 段
    插 465 与插 200、890 都是挡住那 9 个站。真正的选择是**挑哪个空档**，
    所以「手工调低」这种建议毫无操作性，必须给出下一档的确切数值。

    返回 (建议值, 当前挡住数, 建议值挡住数)，已经是最低档则返回 None。
    """
    if not models:
        return None
    now = _shadow_count(band, models, current)
    if now == 0:
        return None

    # 逐个更低的空档试，取第一个真能少挡站的
    for lo, hi in band.gaps():
        mid = (lo + hi) // 2
        if mid >= current:
            continue
        cnt = _shadow_count(band, models, mid)
        if cnt < now:
            return mid, now, cnt
    return None


# ---------------- 方案 ----------------


@dataclass
class SectionPlan:
    """一个候选在一个段上的写入方案。"""

    section: str
    base_url: str
    api_key: str
    models: list[str] = field(default_factory=list)
    priority: int = 0
    priority_reason: str = ""
    proxy_url: str = ""
    # 沿用该段现有条目的主导 prefix（gemini=GLE、codex=CDX、claude=ANT）。
    # force-model-prefix: false 下 prefix 是**额外加别名**不取代原名，
    # 所以缺它不会让站不可用，但会少掉 `ANT/xxx` 那一半别名 —— 按那种
    # 命名发的请求就命中不到新站。见 dominant_prefix。
    prefix: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    max_context_length: int | None = None
    # 上限实测于哪个模型。写回时只给这一个模型加 max-context-length，
    # 其余模型留空（CPA 会回落内置目录值），不把 A 的实测值外推到 B。
    context_model: str = ""
    score: int = 0
    duplicate: bool = False
    duplicate_note: str = ""
    impacts: list[Impact] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 原条目的 weight，只在全量重建时用来**原样搬回去**。
    #
    # 为什么必须保留（2026-09-01 审计发现）：`weight: 0` 是用户显式表达
    # 「把这个站逐出调度池」的唯一手段（本模块 232 行把它当强信号读），
    # 而 CPA 缺这个字段时默认 1。全量重建不带它 = 你手工封禁的站全部复活，
    # 而且没有任何提示。
    #
    # None 表示原条目没写这个字段，渲染时也不写 —— 「没写」与「写了 1」
    # 在 CPA 侧等价，但保持原样能让 diff 干净。
    weight: int | None = None

    @property
    def hijacked(self) -> list[Impact]:
        return [i for i in self.impacts if i.hijacks]

    @property
    def writable(self) -> bool:
        return not self.duplicate and bool(self.models)

    @property
    def recommended(self) -> bool:
        """系统是否**建议**默认勾选写入。「能写」与「建议写」是两件事。

        不建议的三类（都还是 writable，只是默认不勾，用户可手工勾上）：
          · 静默换模 —— 照常计费却返回另一个模型，比不可用更危险
          · 抢走顶层 —— 层级隔离下现有顶层站会完全不被尝试
          · 上限由截断反推 —— 那个数字是实测容量而非站方声明，可能偏保守
        """
        if not self.writable:
            return False
        if any("换模" in w for w in self.warnings):
            return False
        if self.hijacked:
            return False
        if any("截断反推" in w for w in self.warnings):
            return False
        return True

    @property
    def recommend_reason(self) -> str:
        """为什么建议 / 不建议。UI 直接显示这句，让勾选可复核。"""
        if self.duplicate:
            return "已存在，跳过"
        if not self.models:
            return "无可信模型，写进去等于死条目"
        if any("换模" in w for w in self.warnings):
            return "检测到静默换模 —— 计费却拿不到要的模型，默认不勾"
        if self.hijacked:
            names = "、".join(i.model for i in self.hijacked[:3])
            return f"会抢走 {names} 的顶层，默认不勾"
        if any("截断反推" in w for w in self.warnings):
            return "上限由截断反推，非站方声明值，建议人工确认"
        bits = [f"{len(self.models)} 个模型可信"]
        if self.proxy_url:
            bits.append("需代理")
        if self.headers:
            bits.append("需 " + "+".join(self.headers))
        return " · ".join(bits) + f" · priority {self.priority}"


@dataclass
class ImportPlan:
    host: str
    masked_key: str
    sections: dict[str, SectionPlan] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)  # 段 -> 不写入的原因

    @property
    def any_writable(self) -> bool:
        return any(p.writable for p in self.sections.values())


def build_plan(
    row: ParsedRow,
    result,
    cfg: dict,
    *,
    bands: dict[str, Band] | None = None,
    seen: dict[str, set[str]] | None = None,
    seen_pairs: dict[str, set[str]] | None = None,
    probation: bool = True,
    force: dict[str, list[str]] | None = None,
) -> ImportPlan:
    """把一个候选的探测结果变成写入方案。

    去重要查**两层**，问的是两个不同问题：
      seen        五元组指纹（与 CPA 同口径）—— 「这两行配置是否完全相同」
      seen_pairs  (key, base) 对             —— 「这个凭据在这个站配过没」
    第二层不能省：现有条目常带 prefix / headers（relay-f 的 claude 条目
    有 prefix: ANT 和一个 UA），探测方案不带，五元组因此不撞，
    只查第一层会把同一个凭据在同一个站重复写入。

    两者都用于**批内**去重 —— 同一批粘贴里重复两行也要挡住。

    probation 默认 True：新站进最低可插档，不因探测满分就挤掉已验证的站。
    见 suggest_priority 的说明。

    force：{段: [模型, ...]}，人工接管。探测判不可用、但操作员确知可用的段，
    由调用方显式给出要注册的模型清单。为什么需要这条路（2026-08-31）：

      · 很多中转站**不给测活** —— 探针式短消息被拦、或分组只允许特定客户端，
        而真实对话完全正常。这类站探测必然判死，此前完全无法导入。
      · 探测只能证明「此刻这一次请求成功了」，反过来也一样：一次失败不能
        证明这个站不能用。判定错了必须有人工出口。

    force 只绕过 usable 判定，**不绕过**去重、定档、影响面计算与 diff 确认 ——
    那几道是防止写坏 config.yaml 的，与「这个站能不能用」是两件事。
    模型清单必须由操作员显式给出：探测没验成功过任何模型，工具无从推断。
    """
    bands = bands or {}
    existing = seen if seen is not None else existing_fingerprints(cfg)
    pairs = seen_pairs if seen_pairs is not None else existing_pairs(cfg)

    plan = ImportPlan(host=row.host, masked_key=row.masked())

    force = force or {}
    for section, v in result.sections.items():
        forced_models = [m for m in (force.get(section) or []) if str(m).strip()]
        if not v.usable and not forced_models:
            plan.skipped[section] = f"{v.category or '不可用'} — {v.action or '不写入'}"
            continue

        base = base_for_section(row.bare, section)
        proxy = "http://mihomo:7890" if v.need_proxy else ""
        headers = dict(v.min_headers) if v.need_ua else {}
        # 沿用该段主导 prefix。CPA 的五元组指纹**含 prefix**
        # （formatGeminiKeyDedupID），所以要在算 fp 之前定下来。
        prefix = dominant_prefix(cfg, section)

        fp = dedup_key(
            section,
            api_key=row.api_key,
            base_url=base,
            proxy_url=proxy,
            prefix=prefix,
            headers=headers,
        )
        pair = credential_pair(row.api_key, base)

        band = bands.get(section) or build_band(cfg, section)
        bands[section] = band

        # 人工接管的段：模型清单来自操作员，探测那边是空的。
        # 定档也要按这份清单算 —— 影响面是「这些模型各自挡住谁」，
        # 用空清单算出来的影响面恒为 0，等于没算。
        models = forced_models if (forced_models and not v.usable) else list(v.models)

        score = score_verdict(v)
        pri, reason = suggest_priority(band, score, models=models,
                                       probation=probation)

        sp = SectionPlan(
            section=section,
            base_url=base,
            api_key=row.api_key,
            models=models,
            priority=pri,
            priority_reason=reason,
            proxy_url=proxy,
            prefix=prefix,
            headers=headers,
            max_context_length=v.max_context_length,
            context_model=v.context_model,
            score=score,
        )

        if fp in existing.get(section, set()):
            sp.duplicate = True
            sp.duplicate_note = (
                "gemini 段：CPA 会静默丢弃，写进去等于没写"
                if section in _DEDUP_SECTIONS
                else "该段 CPA 不去重，写进去会注册成两个独立凭据"
            )
        elif pair in pairs.get(section, set()):
            # 五元组不同但 (key, base) 相同 —— 现有条目带了 prefix / headers /
            # proxy-url，探测方案没带，指纹因此不同。对 CPA 而言这仍是**同一个
            # 凭据在同一个站**，再写一条就是同 Key 在轮询池占两个位。
            # 实测踩到：relay-f 某 Key 在 claude 段已存在（带 prefix: ANT），
            # 五元组判成新 Key，差点重复写入。
            sp.duplicate = True
            sp.duplicate_note = (
                "该 Key 在这个站已配过（现有条目的 prefix / headers / proxy-url "
                "与探测建议不同，所以五元组指纹没撞上）。要改现有条目的参数请手工编辑，"
                "不要再插一条"
            )
        else:
            existing.setdefault(section, set()).add(fp)
            pairs.setdefault(section, set()).add(pair)

        if forced_models and not v.usable:
            sp.priority_reason = (
                f"人工接管（探测判「{v.category or '不可用'}」）· {reason}")
            sp.warnings.append(
                f"探测未通过（{v.category or '不可用'} — {v.action or ''}），"
                f"模型清单由你手工指定：{', '.join(models)}。"
                "工具没有验证过这些模型能用")

        sp.impacts = compute_impact(band, sp.models, pri)

        if sp.hijacked:
            names = ", ".join(i.model for i in sp.hijacked[:4])
            sp.warnings.append(
                f"会抢走 {len(sp.hijacked)} 个模型的顶层（{names}）—— "
                "层级隔离下现有顶层站将完全不被尝试"
            )

        # 没抢顶层也要说清挡住了谁。层级隔离下「插在中间」不是排序靠前，
        # 是把下面整层跳过 —— 这是 gemini 段插 465 时最容易漏看的部分。
        shadow: dict[str, list[str]] = {}
        for imp in sp.impacts:
            if imp.hijacks:
                continue          # 已在上面单独警告，不重复
            for host in imp.shadowed_hosts:
                shadow.setdefault(host, []).append(imp.model)
        if shadow:
            hosts = sorted(shadow)
            head = "、".join(hosts[:5]) + ("…" if len(hosts) > 5 else "")
            msg = (
                f"priority {sp.priority} 会把 {len(hosts)} 个现有站挡在其后"
                f"（{head}）—— 它们只在本站也不可用时才被尝试。"
            )
            # 空档内取任何值效果都一样（465 与 200、890 挡的是同一批站），
            # 真正的选择是「插哪个空档」。所以不说「手工调低」，直接给下一档
            # 的具体值和代价，省掉用户自己试的那一轮。
            alt = gentler_option(band, sp.models, sp.priority)
            if alt:
                alt_pri, now_n, alt_n = alt
                msg += f"改成 {alt_pri} 则只挡 {alt_n} 站（现 {now_n} 站）"
            else:
                msg += "已是最低可插档，再低要手工指定"
            sp.warnings.append(msg)
        if v.swap_detected:
            sw = v.swap
            detail = f"{sw.get('rate_pct', 0)}%（{sw.get('swap')}/{sw.get('same', 0) + sw.get('swap', 0)} 次）"
            if sw.get("multi_backend"):
                detail += f"，后端形态 {len(sw.get('backends') or {})} 种"
            if sw.get("token_span_anomaly"):
                detail += "，input_tokens 跨度异常"
            sp.warnings.append(
                f"静默换模 {detail} —— 照常计费却返回另一个模型，比不可用更危险"
            )
        if section == "openai-compatibility" and not sp.models:
            sp.warnings.append(
                "compat 段 models 留空会注册 0 个模型，该 provider 完全不可用"
            )
        if v.context_untrusted:
            sp.warnings.append(
                f"上下文上限 {v.max_context_length:,} 由截断反推得出 —— "
                "上游返回 200 但 input_tokens 远小于发送量，该值是实测容量而非声明值"
            )

        plan.sections[section] = sp

    return plan
