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
    selector.go:539-549  availableAuthsFromPriorityBuckets 只收集 bestPriority
    （行号 2026-09-05 校正：原写 325-333，那一段现在是日志脱敏正则；
     同族的 highestPriorityAuths 在 564-583）
    低档凭据只在更高档全部不可用时才参与。所以插档不是「排个序」，
    而是「决定它跟谁同层、把谁挡在后面」。

    **一条例外：codex 段的 WS 请求跨档**（2026-09-05 核实）
    下游是 WS 连接且 provider 是 codex/xai 时，scheduler.go:987-997 的
    highestReadyPriorityLocked 从高到低扫 priorityOrder，返回**第一个含
    ws 凭据的档** —— 源码注释自己写着 "even if they are in a lower
    priority tier than HTTP-only credentials"。

    触发条件在本部署是活的（weighted-round-robin + session-affinity: false
    → 内建选择器 → scheduler 快路）。本模块的处置是**只在文案上说清、
    不改定档算法**（见 ws_crosstier_note 的说明）：跨档只发生在那条少数
    路径上，HTTP 请求的档位谱仍然完全成立；而 websockets 是探测写的、
    会随重探变化，让它参与定档会让档位谱不稳定。

影响面 —— atlas 记的教训：第一版 620 方案劫持了 4 个模型的顶层。
    改 priority 前必须枚举该层会吃到哪些模型。本模块对每个新条目
    声明的每个模型，算出当前顶层是谁、新值会不会越过它。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from .parse import SECTIONS, ParsedRow, base_for_section, host_of
from . import model_catalog
# pipeline 不导入 plan，这个方向无环。只取白名单判定，
# 目录读回来的名字必须过同一道白名单 —— 不然中转站目录里的
# embedding / whisper / tts 之类会被注册成对话模型。
# SEED_MODELS 不在这里用（种子兜底走 model_catalog.latest_models 的三层，
# 那才是「市面最新」；SEED_MODELS 是**基线阶段逐个打**的短清单，两回事）。
# 不要为了注释里提到它就导入 —— pyflakes 会报未用，而那条告警的意义正是
# 「这个名字在这里没有作用」。
from .pipeline import (MAX_MODELS_PER_SECTION, model_allowed,
                       model_fits_section)

# 只有 gemini 段在配置层去重（静默丢弃）
_DEDUP_SECTIONS = {"gemini-api-key"}

# model_source 的人话标签。用在写进 config.yaml 的行尾注释里 ——
# 读文件的人看不懂 `probed` / `seed` 这类内部值。
# 与 writeback._SRC_LABEL 措辞一致（那份用在写回警告里）。
_SRC_LABEL_CN = {
    "probed": "本次实测通过",
    "prior": "沿用原有清单",
    "catalog": "站方目录声称有",
    "manual": "手填",
    "seed": "工具猜测",
}


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
      导入工具需要的是这一个 —— 实测踩到：foxtrot 的某个 Key 在 claude 段
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


# 运行期健康分在最终得分里的权重（用户 2026-09-11 给的公式：
# 「健康分数 = 可调度比例×60% + 活跃比例×40%」，那个 60/40 是**健康分内部**
# 的两项权重，见 runtime_health.calculate_health_score）。
#
# 这里是**另一层**权重：最终档位 ∈ [1,100]。运行期证据（CPA 自己记的成功/
# 失败计数）比一次探测的结论强得多 —— 它来自真实流量的累积。所以给它 60，
# 检测分 40。两者都拿不到时退回纯检测分，不产生「无依据的中间值」。
_RUNTIME_HEALTH_WEIGHT = 0.6


def _blended_score(static_best: int, health: float | None) -> int:
    """档位用的最终得分：检测分与运行期健康分融合，取整到 [1,100]。

    为什么要有这一层（2026-09-15）
    ----------------------------
    上一版把 `domain_health` **只**用在 `_sort_key` 的排序上，而喂给
    `suggest_priority` 的仍是 `max(x.score for x in sps)` —— 纯检测分。
    于是「按 CPA 实际运行状态分配优先级」这句注释与实际行为不符：健康分
    只能改变站与站之间的先后，改不动任何站的档位。探测一次成功的新站与
    CPA 记着 3000 次成功的老站，只要检测分相同就拿到同一个档位上限。

    `health=None`（该域名没有运行期数据）时返回纯检测分 —— 与传入前完全
    一致，不影响任何拿不到运行数据的部署。
    """
    if health is None:
        return max(int(static_best), 1)
    # health 是 [0,1] 的浮点，先放大到 0-100 再按权重融合。
    blended = (_RUNTIME_HEALTH_WEIGHT * float(health) * 100.0
               + (1.0 - _RUNTIME_HEALTH_WEIGHT) * float(static_best))
    return max(int(round(blended)), 1)


def _cpa_base_url(cfg: dict) -> str:
    """CPA 管理接口在哪。先看部署环境给的地址，再看 config.yaml 的 port。

    2026-09-15 修：原来只有 `f"http://localhost:{cfg['port']}"` 一条路
    ---------------------------------------------------------------
    importer 是**独立容器**（部署版 docker-compose.yml:536-635），它容器内的
    `localhost` 是它自己，不是 `cli-proxy-api`。而 compose 已经把正确的容器内
    地址通过 `CPA_UPSTREAM_URL: "http://cli-proxy-api:8317"` 注入了环境变量
    （同一段 compose 里就有），本函数原来根本没读它。后果：
    `fetch_cpa_runtime_health()` 每次连接被拒 → 返回 None → `domain_health`
    恒为空 → 每个站都退回 `max(x.score for x in sps)` 的静态检测分。
    也就是说「按 CPA 实际运行状态分配优先级」这个特性**在生产里从未拿到过
    一次数据**，日志里只有一行 `info: 运行时数据不可用`。

    为什么用环境变量而不是写死服务名：服务名是部署拓扑的一部分，会随
    compose 改（本项目自己的 compose 叫 `mihomo-proxy`、部署版叫 `mihomo`；
    CPA 同理）。硬编码任何一个都会在下一次改名时静默失效 —— 而静默失效
    正是这个 bug 藏了这么久的原因（用户第 5 条：严禁硬编码）。

    顺序说明：
      1. `CPA_UPSTREAM_URL` —— 部署方显式给的，最可信
      2. `http://127.0.0.1:{cfg['port']}` —— 单机/本地跑、CPA 同机时成立
      3. 空串 —— 让 `fetch_cpa_runtime_health` 自己跳过（它会打 debug 返回
         None），调用方照常回退静态分，不会崩
    """
    env = (os.environ.get("CPA_UPSTREAM_URL") or "").strip()
    if env:
        return env.rstrip("/")
    try:
        port = int(cfg.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if port:
        return f"http://127.0.0.1:{port}"
    return ""


def _proxy_url_for_config() -> str:
    """Only use an explicit runtime choice; never guess deployment addresses."""
    cands = [u.strip() for u in os.environ.get("PROBE_PROXY", "").split(",")
             if u.strip()]
    # Multiple untested candidates do not establish which exit succeeded.
    return cands[0] if len(cands) == 1 else ""


def _source_url(url: str) -> str:
    """Normalize authority only; paths and credentials remain separate evidence."""
    p = urlsplit(url.strip().rstrip("/"))
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, p.fragment))


def reenable_targets(section: str, entry: dict) -> list[str]:
    """探测判定可用时，这个条目里**该清掉的停用字段**名列表。没有则空。

    用户 2026-09-16：「无论原来是否被关闭，如果探测可用就要打开」。

    两种停用形态与 `entry_out_of_pool` 一一对应，清法不同：

      · compat 段 `disabled: true` —— 清这个布尔字段本身。
        `internal/watcher/synthesizer/config.go:288-290` 遇 Disabled 直接
        continue，那条 provider 连 Auth 都不合成。
      · 任意段 `excluded-models: ["*"]` —— 清这个通配符项。
        `service_models.go:541-574` 的 applyExcludedModels 拿空清单就
        UnregisterClient。

    只报**字段名**，具体删除动作交给 writeback（它拿得到原文行，能在保留
    注释的前提下精确删行）。这里判定的依据是 YAML 解析后的值 —— 与原条目
    的注释形态无关。

    不收 `weight: 0`：那是权重不是停用开关。它在 weighted-round-robin 下
    同样把凭据逐出调度池，但它是操作员的**定量表达**（「这个站先用一点」），
    改成 1 属于替用户改语义，超出「重新打开」的范围。
    """
    out: list[str] = []
    if section == "openai-compatibility" and entry.get("disabled") is True:
        out.append("disabled")
    ex = entry.get("excluded-models")
    if isinstance(ex, list) and any(str(x).strip() == "*" for x in ex):
        out.append("excluded-models")
    return out


def _cooling_override(v) -> bool | None:
    """这个条目要不要覆盖全局冷却策略。返回 None = 跟随全局。

    用户第 4 条要求「冷却策略根据实际需求检测填写到位」。而「到位」在这里
    **不等于**「每条都写一个值」—— CPAMP 界面的默认就是「跟随全局」
    （字段缺席），那是绝大多数条目的正确状态。无条件写值等于把一个全局
    可调的策略钉死在每一条上，属于负向调整。

    所以判据是：**只在实测证据表明默认会出问题时才覆盖**。

    `disable-cooling` 的语义（config_types.go:406-408）：
      · true  = 关掉这个凭据的冷却（即使全局启用）
      · false = 强制启用冷却（即使全局禁用）
      · 缺席  = 跟随全局

    唯一有实测依据的覆盖方向是 **false（强制启用冷却）**：
    探测判为「限流」或「限频」的站，说明它对请求频率敏感。若全局恰好
    禁用了冷却，CPA 会在收到 429 之后立刻拿同一个凭据重试，把限流窗口
    拉得更长 —— 对这类站强制开冷却是确定的正向调整。

    反方向（true = 关冷却）**不做**：那需要「这个站被冷却误伤了」的证据，
    而探测拿不到那个证据（冷却是 CPA 运行期行为，不在一次探测的观测范围内）。
    猜着关掉冷却的风险是让一个真的该冷却的站反复打满，比不写更糟。
    """
    cat = str(getattr(v, "category", "") or "")
    if cat in ("限流", "限频"):
        return False
    return None


def existing_models_for(cfg: dict, section: str, base_url: str,
                        api_key: str) -> list[str]:
    """这个 (段, 站, Key) 在原 config.yaml 里注册着哪些模型。没有就空列表。

    给兜底分支用：判死 + 目录读不到时，原清单比「市面最新」的猜测硬 ——
    它是先前一轮实测沉淀的。见 build_plan 里 seed 分支的说明。

    按完整来源 URL 和 Key 匹配；主机规范化不能把不同路径的模型证据混在一起。
    """
    scope = _source_url(base_url)

    def names(models) -> list[str]:
        out = []
        for m in models or []:
            if isinstance(m, dict):
                n = str(m.get("name") or "").strip()
                if n and n not in out:
                    out.append(n)
        return out

    if section == "openai-compatibility":
        # compat 的 models 在 provider 级，组内所有 Key 共用同一份 ——
        # 所以只要这个 Key 在这个 provider 的 api-key-entries 里就算命中。
        for prov in cfg.get("openai-compatibility") or []:
            if not isinstance(prov, dict):
                continue
            if _source_url(str(prov.get("base-url") or "")) != scope:
                continue
            for ke in prov.get("api-key-entries") or []:
                if isinstance(ke, dict) and str(ke.get("api-key") or "") == api_key:
                    return names(prov.get("models"))
        return []

    for e in cfg.get(section) or []:
        if not isinstance(e, dict):
            continue
        if str(e.get("api-key") or "") != api_key:
            continue
        if _source_url(str(e.get("base-url") or "")) != scope:
            continue
        return names(e.get("models"))
    return []


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
    # codex 段带 `websockets: true` 的档与站（降序）。
    #
    # 为什么单独记（2026-09-05，契约对齐审计发现）
    # ----------------------------------------
    # 本工具整套影响面计算建立在「priority 硬隔离」上 ——
    # `availableAuthsFromPriorityBuckets`（selector.go:539-549）只收集
    # bestPriority 那一桶。codex 段的 WS 请求是唯一的例外，见
    # `ws_crosstier_note`。
    # 但 codex 段有一条例外：**下游是 WS 连接**时，
    # `scheduler.go:987-997`（`highestReadyPriorityLocked`，preferWebsocket=true）
    # 从高到低扫 priorityOrder，返回**第一个含 ws 凭据的档** —— 源码注释自己
    # 写着 "even if they are in a lower priority tier than HTTP-only
    # credentials"。
    #
    # 触发条件在本部署是活的：`routing.strategy: weighted-round-robin` +
    # `session-affinity: false` → 内建选择器 → scheduler 快路
    # （conductor_selection.go 的 useSchedulerFastPath）。
    #
    # 生产实测（fsdownload 版 codex 段）：425 档带 ws 且是最高档，所以此刻
    # 不越档；但 350/349/348 三档全无 ws，425 一冷却，WS 请求会直接跳到
    # 154 档的 alfa.example（也带 ws），越过三个健康档。
    #
    # **只用于文案与影响面，不参与定档**：跨档只发生在下游用 WS 连接时
    # （Codex Desktop 那条路），HTTP 请求的档位谱仍然完全成立；而
    # `websockets` 是探测写的、会随重探变化，让它参与定档会让档位谱不稳定。
    ws_tiers: list[int] = field(default_factory=list)
    ws_hosts_at: dict[int, list[str]] = field(default_factory=dict)
    model_top: dict[str, int] = field(default_factory=dict)  # 模型 -> 当前顶层 priority
    # 模型 -> {档位: 承载该模型的站点}。算「挡住谁」必须按模型分开 ——
    # 30 档上有 6 个站，但只有声明了同一个模型的那几个才会被挡。
    model_tiers: dict[str, dict[int, list[str]]] = field(default_factory=dict)
    # 已被 weight: 0 逐出调度池的站。**挡住它们没有任何代价** ——
    # selector.go:636-644 的 positiveWeightAuths 已经把零权重凭据整个剔除，
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
#   `# foxtrot：实测 503 …`      冒号分隔（多数）
#   `# relay-e 永久排除（2026-08-27）` 空格分隔，无冒号
# 只要冒号形态会漏掉后者 —— 实测踩到：relay-e 在 codex 段的「永久排除」
# 注释因此没被解析出来。
_HOST_IN_NOTE = re.compile(
    r"#\s*([A-Za-z0-9][A-Za-z0-9.\-]{2,})\s*(?:[:：]|\s+(?=永久排除|站点级不可用))")

# 第三种位置：站名后面**紧跟一对括号，括号里就是失败依据**。
#   `# 同时压过 nova（分组无渠道，实测 503）与 xray、hotel。`
#   `# 990 仍高于 950 的 xray（实测超时 90 秒），所以它仍轮不到。`
#
# 为什么必须认（2026-09-03 拿生产 config.yaml 实测）：claude 与 compat 两段
# 的死站结论**全部**是这个形态，`_HOST_IN_NOTE` 一个都抓不到 —— 于是那两段的
# `unhealthy_hosts` 恒为空集，「读注释拿健康度」这件事在实际配置上完全失效，
# 而它不报错、不警告，只是让定档偏保守（新站被压到一堆死站后面）。
#
# 这一路的误判风险比冒号形态高得多：括号在中文注释里到处都是。所以它比
# 严格路多两道闸（见 unhealthy_from_comments 的 known 参数）：
#   ① 括号**内部**必须命中失败关键词 —— 不看整行，避免「A（正常）与 B 实测 503」
#      把 A 也算进去
#   ② 站名必须是本段真实出现过的主机名或它的点分标签 —— 认不出的一律丢弃，
#      不进 unmatched_notes（那是给严格路的信号，混进来会变成噪声）
_HOST_PAREN_NOTE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9.\-]{2,})\s*[（(]([^）)]{0,80})[）)]")

# 「超时」也是不可用结论，但只在括号路里认。
#
# 为什么不并进 `_DEAD_NOTE`：那个模式作用于**整行**，而「超时」在正文里
# 出现得比状态码随意得多（讲 CPA 的 timeout 配置、讲探测超时设定）。
# 括号路已经把范围收到「站名紧跟的括号内」，那里出现「实测超时 90 秒」
# 就是在说这个站。实测：并进严格路对这份 config.yaml 的结果零变化，
# 但那只是这一份文件的巧合，不是保证。
_DEAD_NOTE_PAREN = re.compile(
    _DEAD_NOTE.pattern
    + r"|实测\s*超时|超时\s*\d+\s*秒|无可用渠道|分组无渠道"
)

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
# 注释里出现这些词时**不当站名**。
#
# 2026-09-05 补全：原表停在旧字段集，CPA 后来加的字段一个都没进。实测触发 ——
#
#     # websockets: true   实测 503 不支持 WS
#       → _DEAD_NOTE 命中、_HOST_IN_NOTE 抓出 `websockets`
#       → _looks_like_host 返回 True → 进 unhealthy_hosts
#
# 于是一个**字段名**被当成死站，定档因此偏保守（新站被压到一堆「死站」后面），
# 而且不报错、不警告。生产 config.yaml 当前只抓出三个真站名没踩到，
# 但本工具自己写的 `websockets: true   # 原值搬运…` 行尾注释改成英文就会触发。
#
# 取值范围：CPA `config_types.go` 的 99 个 yaml tag 里**会出现在四段条目内部**
# 的那些。全局配置的 tag（addr / cert / timeout / strategy 之类）不加 ——
# 它们不会出现在条目级注释里。
#
# 2026-09-05 第二轮又补六个（审计发现，它们确实在条目内部）：
#   `mode`  —— claude 段的 `cloak.mode`（`config_types.go:333`）
#   `min` / `max` / `levels` / `zero-allowed` / `dynamic-allowed`
#           —— `models[].thinking` 的子键
#              （`internal/registry/model_registry.go:102-111`）
#
# `mode` 这个词特别值得记：我上一轮**特意把它排除在外**，理由是「某个站真叫
# `mode.example.com` 时它的点分标签里就有 mode」。那个顾虑本身没错，但代价
# 算反了 —— `mode` 作为 cloak 子键出现在条目注释里的概率，远高于某个中转站
# 的域名恰好叫 mode.*。真站名撞上时的后果是「少认一个死站」（定档偏保守，
# 与不加这张表时的行为相同）；而漏排除时的后果是「把一个真站判成死站」，
# 那会让新站档位偏高、真的挡住在用站。两侧不对称，取更安全的一侧。
_NOT_A_HOST = frozenset("""
api-key base-url proxy-url prefix priority weight models headers name alias
disabled enabled request-scoped-errors api-key-entries max-context-length
excluded-models fingerprint-profile action code status message error
czone cf-ray note todo fixme warning tip

websockets alpha-search support-prompt-cache-key disable-cooling request-retry
rebuild-mid-system-message experimental-cch-signing cloak strict-mode
sensitive-words cache-user-id match-regexr match not-match exist not-exist
is-compat thinking display-name force-mapping image
input-modalities output-modalities
disable-codex-cloaking identity-confuse inject-x-search
optimize-multi-agent-v2 stabilize-device-profile
switch-preview-model switch-project store-auth store-sources

mode min max levels zero-allowed dynamic-allowed
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
# 「同一站在不同段结论不同」的设计意图直接冲突（foxtrot 在 claude 段
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


def unhealthy_from_comments(raw: str, section: str,
                            known: set[str] | None = None) -> set[str]:
    """从 config.yaml 原文里解析该段被实测判为不可用的站。

    为什么要读注释：`Band` 原本只看 priority 与 base-url，于是把「实测全挂
    的 49 个站」也当成要保护的现有站，把满分新站压到 12 分档。而这些站的
    可用性结论**只存在于注释里** —— 那是两夜排障的唯一记录。

    这是弱信号，只用于降低「挡住它们」的代价权重。注释可能过期，所以
    绝不用它直接排除任何站 —— 真要排除该由用户写 weight: 0 显式表达。

    只在**本段范围内**匹配：同一个站在不同段的结论完全不同
    （foxtrot 在 claude 段实测 200，在 gemini 段是 503）。

    known：本段真实出现过的主机名与它们的点分标签。给了才启用**括号路**
    （`# … nova（分组无渠道，实测 503）…`）—— 那一路的站名位置比冒号形态
    随意得多，必须拿真实主机名兜住，否则任何「词（…503…）」都会被当成站名。
    不给 known 时行为与从前完全一致，只走严格路。

    为什么必须有括号路（2026-09-03 拿生产 config.yaml 实测）：那份配置的
    claude 段与 compat 段的死站结论全是这个形态，严格路一条都抓不到 ——
    两段的 unhealthy_hosts 恒为空集，读注释这件事在真实文件上完全失效。
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

    kn = {k.lower() for k in (known or set())}
    out: set[str] = set()
    for i in range(st, en):
        l = lines[i]
        if not l.lstrip().startswith("#"):
            continue
        recovered = _RECOVERED.search(l) and not _REVERSAL.search(l)
        # 提到已恢复、且没有转折词否决它 —— 不算当前不可用。
        # 带转折的（「已恢复但又挂了」）仍按不可用处理。
        if _DEAD_NOTE.search(l) and not recovered:
            m = _HOST_IN_NOTE.search(l)
            if m and _looks_like_host(m.group(1)):
                out.add(m.group(1).lower())
        # 括号路。同一行可以有多处（`A（实测 503）与 B（正常）`），逐个判 ——
        # 判据是**括号内部**有没有失败结论，不是整行。
        if kn and not recovered:
            for m in _HOST_PAREN_NOTE.finditer(l):
                name, inner = m.group(1).lower(), m.group(2)
                if name not in kn or not _looks_like_host(name):
                    continue
                if not _DEAD_NOTE_PAREN.search(inner):
                    continue
                if _RECOVERED.search(inner) and not _REVERSAL.search(inner):
                    continue
                out.add(name)
    return out


def name_alias_map(cfg: dict, *,
                   conflicts: list[str] | None = None) -> dict[str, str]:
    """站的人读短名 -> 域名。从 openai-compatibility 段的 `name` 字段建。

    为什么需要显式表、不能靠字符串猜（2026-08-30 踩到）：
    注释里写的是人读短名，而 base-url 里是域名，两者**不保证有公共子串**：

        jdw  ->  relay-h.example      （jdw ≠ hotel）
        sm        ->  relay-m.example
        alfa    ->  relay-a.example
        relay-e       ->  relay-e.example

    第一版用「短名是域名的点分标签之一」来匹配，jdw 静默漏判 ——
    于是 gemini 段把实测 503 的 hotel 当成活站保护，新站又被压低。
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
        api.relaypro.com  被短名 relayproxy 命中（relayproxy.startswith(relaypro)）
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
    没设 weight 走 credentialweight.Default（=1，credentialweight/weight.go:14 与 selector.go:380-398），
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

    归零后 `positiveWeightAuths`（selector.go:637-644）把它整个剔除。
    所以 `weight: -1` 与 `weight: 0` 效果完全相同 —— 只判 `== 0` 会漏掉负数，
    把一个已被逐出的站当成活站保护，新站因此被压低。

    **前提：`routing.strategy` 是 `weighted-round-robin`**（2026-09-02 核实）。
    只有 `WeightedRoundRobinSelector.Pick` 调 `positiveWeightAuths`
    （selector.go:650）；`RoundRobinSelector`（:589，也是**默认**策略）与
    `FillFirstSelector`（:787）根本不读 weight —— 那两种策略下 `weight: 0`
    的站照常参与轮询。

    本项目对此的处理：`weight_zero_excludes` 按配置判断策略再决定要不要把
    零权重当「已逐出」。当前部署实测 `strategy: weighted-round-robin`，
    所以语义成立；换成默认 round-robin 后定档会自动改按「活站」对待它们。
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


# 只有这个策略会把零权重凭据整个剔除。CPA 的解析接受三种拼法
# （service_config.go:42-47），大小写与空格都不敏感。
_WRR_ALIASES = frozenset({"weighted-round-robin", "weightedroundrobin", "wrr"})


def weight_zero_excludes(cfg: dict) -> bool:
    """`weight: 0` 在这份配置下是否真的把凭据逐出调度池。

    为什么必须问这一句（2026-09-02 核实 CPA 源码后发现）
    ------------------------------------------------
    本模块多处把 `weight: 0` 当「站已被逐出、挡住它零代价」的**强信号**读，
    而那只在 `routing.strategy = weighted-round-robin` 下成立：

        WeightedRoundRobinSelector.Pick  → positiveWeightAuths  → 剔除 weight<=0
                                           (selector.go:650, 637-644)
        RoundRobinSelector.Pick          → 不读 weight          (selector.go:589)
        FillFirstSelector.Pick           → 不读 weight          (selector.go:787)

    而 round-robin 是**默认**策略（config_types.go:235-236）。也就是说没配
    `routing.strategy` 的部署里，`weight: 0` 的站照常参与轮询 —— 此时把它当
    死站会让定档以为「挡住它没代价」，从而把新站插到一批**其实在服务**的站
    之前。方向正是本模块反复强调的更坏那个：把活站当死站。

    当前部署实测配的是 `weighted-round-robin`，所以旧行为一直是对的；
    但那是配置的巧合，不是代码的保证。
    """
    routing = (cfg or {}).get("routing")
    if not isinstance(routing, dict):
        return False                    # 没配 routing = 默认 round-robin
    raw = str(routing.get("strategy") or "").strip().lower()
    return raw in _WRR_ALIASES


def entry_out_of_pool(section: str, entry: dict) -> str:
    """这个条目是不是已经被 CPA 排除在调度池外。是则返回原因，否则空串。

    与 `weight: 0` 的性质相同（都是「CPA 不会把请求路由到它」），但判据不同 ——
    weight 只在 weighted-round-robin 下生效，这两个**任何策略下都生效**。

    两种形态（2026-09-05 加，审计发现）
    -----------------------------
    ① compat 段的 `disabled: true`
       `internal/watcher/synthesizer/config.go:288-290` 与
       `sdk/cliproxy/service_models.go:198` 都是遇 Disabled 直接 continue ——
       那个 provider **连 Auth 都不合成**，根本不在池里。

    ② 任意段的 `excluded-models: ["*"]`
       那正是 CPA 管理面板「停用一个 config 型凭据」的实现
       （`internal/api/handlers/management/config_apikey_disable.go:12` 的
       `configAPIKeyDisablePattern = "*"`）。`applyExcludedModels`
       （`service_models.go:541-574`）用通配匹配把该凭据的模型全过滤掉，
       `registerResolvedModelsForAuth` 拿到空清单就 `UnregisterClient`。

    为什么这件事重要：`build_band` 原来只看 priority / models / weight，
    于是这两类条目被当成**在用站**参与定档避让。实测后果是一个
    `disabled: true` 的 provider 在 300 档就把新站压到 225，
    而理由文案说「会挡 N 个在用站」—— 其中那一个不在调度池里。

    本工具其实**知道**第二条：`web/app.js` 与 `tests/test_web.py` 都写着
    「`excluded-models` 含 `*`」是 CPAMP 停用徽标的来源，只是定档这一路
    没据此排除。
    """
    if section == "openai-compatibility" and entry.get("disabled") is True:
        return "provider 被 disabled: true 停用"
    ex = entry.get("excluded-models")
    if isinstance(ex, list) and any(str(x).strip() == "*" for x in ex):
        return 'excluded-models 含 "*"（等于停用该凭据）'

    # ③ base-url 为空 —— CPA 在**加载期**就把这条目删掉（2026-09-05 加）。
    #
    # 门槛按段不同，不能写成一条：
    #   · codex   `config_normalization.go:208-210`  `if e.BaseURL == "" { continue }`
    #   · compat  同文件 `:167-170`  同一句，注释写着 "treated as removed"
    #     —— 这两段**只要 base-url 空就删**，哪怕 api-key 有值
    #   · gemini  同文件 `:244-246`  `if entry.APIKey == "" && entry.BaseURL == ""`
    #   · claude  `internal/watcher/synthesizer/config.go:145-147` 同上
    #     —— 这两段要**两个都空**才删
    base = str(entry.get("base-url") or "").strip()
    if not base:
        if section in ("codex-api-key", "openai-compatibility"):
            return "base-url 为空（CPA 加载期直接删掉这个条目）"
        if not str(entry.get("api-key") or "").strip():
            return "api-key 与 base-url 都为空（CPA 加载期直接删掉）"

    # ④ compat 的 models 为空 —— 该条目对**每个具名模型**都不在池里。
    #
    # `registerCompat`（`sdk/cliproxy/service_models.go:206-216`）在模型清单空
    # 且插件也没给模型时走 `UnregisterClient`，该 Auth 零注册模型；而
    # `scheduledAuthMeta.supportsModel`（`sdk/cliproxy/auth/scheduler.go:838-841`）
    # 在 `supportedModelSet` 为空时对任何具名模型返回 **false**。
    #
    # **只对 compat 成立**：codex 段空 models 会回落 `GetCodexProModels()`
    # （`service_models.go:825-826`），仍在池。这个差别是本工具此前把两段
    # 一视同仁的原因，也是它必须分开写的原因。
    if section == "openai-compatibility":
        ms = entry.get("models")
        has_named = isinstance(ms, list) and any(
            isinstance(m, dict) and str(m.get("name") or "").strip()
            for m in ms)
        if not has_named:
            return "models 为空（CPA 对该 provider 零注册模型，不在调度池里）"
    return ""


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
    # `weight: 0` 只在 weighted-round-robin 下才真的把凭据逐出调度池。
    # 默认策略 round-robin 与 fill-first 根本不读 weight —— 那时零权重的站
    # 照常参与轮询，把它当死站会让新站插到一批**其实在服务**的站之前。
    # 见 weight_zero_excludes 的说明。
    wrr = weight_zero_excludes(cfg)

    # codex 段带 `websockets: true` 的档与站。见下方 ws_tiers 的写入处。
    ws_tiers: set[int] = set()
    ws_hosts: dict[int, list[str]] = {}
    for e in cfg.get(section) or []:
        if not isinstance(e, dict):
            continue
        entry = e
        pri = e.get("priority", 0)
        if not isinstance(pri, int):
            continue
        host = host_of(str(e.get("base-url") or ""))
        # weight: 0 是**强信号** —— weighted-round-robin 下 CPA 的选择器
        # 已经把它整个剔除（selector.go:650 → 637-644 positiveWeightAuths），
        # 挡住它零代价。其他策略下不成立，所以先看 wrr。
        # 用 entry_all_zero_weight 而不是 e.get("weight")：compat 段的 weight
        # 在 api-key-entries 里，条目级读不到（自查发现的缺陷）。
        if wrr and host and entry_all_zero_weight(section, e):
            dead.add(host.lower())
        # 已被 CPA 排除在调度池外的条目（disabled / excluded-models 含 `*`）。
        # 与 weight:0 不同的是这两个**任何策略下都生效**，所以不看 wrr。
        # 见 entry_out_of_pool。
        out_why = entry_out_of_pool(section, e)
        if host and out_why:
            dead.add(host.lower())
            # **也不参与档位与模型归属**：它不在池里，让它占一个档位、
            # 或者声明「这个模型的最高档是我」，都会让新站被错误地压低。
            # weight:0 那条只加进 dead 不跳过归属，因为它只在 wrr 下成立，
            # 而档位谱要对所有策略都成立。
            continue
        tiers.setdefault(pri, [])
        if host and host not in tiers[pri]:
            tiers[pri].append(host)
        # codex 段的 `websockets: true` 会让**下游是 WS 连接**的请求跨档取
        # （scheduler.go:987-997 的 highestReadyPriorityLocked，preferWebsocket
        # 时从高到低扫 priorityOrder 返回第一个含 ws 凭据的档，源码注释写着
        # "even if they are in a lower priority tier"）。
        # 单独记一份，供影响面文案说清这一层。见 Band.ws_tiers。
        if section == "codex-api-key" and entry.get("websockets") is True:
            ws_tiers.add(pri)
            if host:
                ws_hosts.setdefault(pri, [])
                if host not in ws_hosts[pri]:
                    ws_hosts[pri].append(host)
        for m in _models_of(e):
            if m not in model_top or pri > model_top[m]:
                model_top[m] = pri
            per = model_tiers.setdefault(m, {})
            at = per.setdefault(pri, [])
            if host and host not in at:
                at.append(host)

    band.tiers = sorted(tiers, reverse=True)
    band.ws_tiers = sorted(ws_tiers, reverse=True)
    band.ws_hosts_at = {k: sorted(v) for k, v in ws_hosts.items()}
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
        # 「还有活 key」= 这个条目不是全 0，**且**没被排除出池。
        #
        # 后半句是 2026-09-05 补的：不加的话一个 `disabled: true` 的条目会把
        # 自己所在的站从 dead_hosts 里救回来 —— 它确实「不是全 0 权重」，
        # 但它根本不在调度池里。
        if (not entry_all_zero_weight(section, e)
                and not entry_out_of_pool(section, e)):
            h = host_of(str(e.get("base-url") or "")).lower()
            if h:
                alive.add(h)
    band.dead_hosts = {h for h in dead if h not in alive}
    # 重名冲突并进 unmatched_notes 一起暴露 —— 都是「注释结论没能作用到定档上」，
    # 对用户是同一类事实，没必要多开一个字段。
    alias_conflicts: list[str] = []
    band.alias = name_alias_map(cfg, conflicts=alias_conflicts)
    if raw:
        # 本段真实出现过的主机名与它们的点分标签。两个用途：
        #   ① 传给 unhealthy_from_comments 启用括号路（那一路必须靠真实
        #      主机名兜住，否则任何「词（…503…）」都会被当成站名）
        #   ② 下面判 unmatched_notes
        # 必须在解析注释**之前**算好 —— 上一版是解析完再算，于是括号路
        # 拿不到它。
        known = set(band.alias)
        all_labels: set[str] = set()
        for hosts in band.hosts_at.values():
            for h in hosts:
                all_labels |= set(h.lower().split("."))
                all_labels.add(h.lower())
        band.unhealthy_hosts = unhealthy_from_comments(
            raw, section, known=known | all_labels)
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
        #
        # 括号路抓到的名字必然在 known|all_labels 里（那是它的入场条件），
        # 所以只有严格路的短名会落进这里 —— 与从前一致。
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
    # 阈值单位是 **token**，与 `max_context_length` 一致（2026-09-06 校对）。
    # 200k token 是现役模型里最小的窗口 —— 低于它说明上游确实截窄了。
    # 此前 `_bisect` 返回的是字符数，那时这条判据实际比的是 20 万字符
    # （≈5 万 token），几乎永不触发；单位修正后它才真的生效。
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
    465 不动 golf 的 900，但把 30 档那批全挡在后面了。
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


def session_affinity_on(cfg: dict) -> bool:
    """`routing.session-affinity` 开没开。开着时 priority 硬隔离有第二条例外。

    为什么要单独问这一句（2026-09-05 契约审计发现）
    -----------------------------------------
    本模块的影响面计算（`compute_impact` / `_shadow_count`）全都基于
    **priority 硬隔离**：只有最高那一桶参与选择。而这条判据有两个例外，
    本模块原来只认了第一个：

      ① codex/xai + 下游 WS —— `scheduler.go` 的
         `highestReadyPriorityLocked` 在 `preferWebsocket=true` 时从高到低扫，
         返回第一个含 ws 凭据的档（源码注释："even if they are in a lower
         priority tier than HTTP-only credentials"）。见 `ws_crosstier_note`。

      ② **session-affinity** —— `conductor_selection.go` 的
         `availableAuthsForSelector`：selector 是 `*SessionAffinitySelector`
         时，交给它的候选是 `getAvailableAuthsAcrossPriorities`（**全部档位**），
         注释写着「so an established binding can be validated instead of being
         preempted by a recovered higher-priority credential」。

    ② 与 ① 的关键区别：**它不限段、不限 WS**。任何段、任何请求，只要会话
    已经绑定过某个凭据，那个凭据就留在候选里 —— 哪怕它在很低的档。

    为什么只加文案不改算法
    ------------------
    冷启动绑定仍然从最高档开始（`SessionAffinitySelector.Pick` 的绑定建立
    路径），所以「新站插这一档会挡住谁」这个结论对**新会话**完全成立，
    只对**已绑定会话**不成立。而「已绑定」是运行时状态 —— 定档时无从得知
    有多少会话绑在哪些凭据上。

    把算法改成依赖会话状态会让「新站该插哪一档」从一个静态问题变成动态问题，
    而那个问题没有正确答案。加一句话说清边界，是这里能给的最准确的东西。

    生产配置当前是 `false`，所以此刻不触发 —— 但那是配置的巧合，
    与 `weight_zero_excludes` 同一条理由。
    """
    routing = (cfg or {}).get("routing")
    if not isinstance(routing, dict):
        return False
    return routing.get("session-affinity") is True


def affinity_crosstier_note(cfg: dict, band: Band, new_priority: int) -> str:
    """session-affinity 开着时的跨档说明。关着或无下层站时返回空串。

    与 `ws_crosstier_note` 平行 —— 那个说 codex 段的 WS 请求，这个说所有段的
    已绑定会话。两者可以同时出现（codex 段 + WS + affinity），措辞不重复：
    前者讲「WS 请求去哪个档」，后者讲「已绑定的会话不换档」。
    """
    if not session_affinity_on(cfg):
        return ""
    lower = [p for p in band.tiers if p < new_priority]
    if not lower:
        # 没有更低的档 —— 「低档凭据仍在候选里」这件事没有对象，不必说
        return ""
    ttl = ""
    routing = (cfg or {}).get("routing") or {}
    if isinstance(routing, dict) and routing.get("session-affinity-ttl"):
        ttl = f"（TTL {routing['session-affinity-ttl']}）"
    return (
        f"routing.session-affinity 开着{ttl} —— 上面「挡住 N 个」的计数只对"
        f"**新会话**成立。已经绑定到下层 {len(lower)} 个档"
        f"（{', '.join(str(p) for p in sorted(lower, reverse=True)[:4])}"
        f"{' 等' if len(lower) > 4 else ''}）的会话会继续用原凭据，"
        f"不因为这个新站出现而改档（conductor_selection.go 的 "
        f"availableAuthsForSelector 把**全部档位**交给亲和选择器）。"
        f"这条例外不限段、不限 WS。"
    )


def ws_crosstier_note(band: Band, new_priority: int,
                      new_has_ws: bool) -> str:
    """codex 段 WS 请求的跨档说明。不适用时返回空串。

    为什么需要这句话（2026-09-05，契约对齐审计发现）
    ------------------------------------------
    界面上「挡住 N 个在用站」「新值高于现有顶层」这些结论，全都基于
    **priority 硬隔离**（`availableAuthsFromPriorityBuckets` 只收集
    bestPriority 那一桶）。而 codex 段有一条例外：下游是 WS 连接时，
    `scheduler.go:987-997` 会**跨档**取第一个含 ws 凭据的档。

    所以对 codex 段必须多说一句 —— 否则用户按「档位隔离」的心智模型去理解
    影响面，而 WS 请求的实际去向与那个模型不符。

    两种情形分开说：
      · 新条目**带** ws：它会参与 WS 请求的跨档竞争，而竞争的对手不是
        「同档的站」而是「所有档里带 ws 的站」
      · 新条目**不带** ws：WS 请求根本不会落到它身上，无论它在哪一档
    """
    if band.section != "codex-api-key":
        return ""
    if not band.ws_tiers:
        # 全段没有一个带 ws 的条目 —— 那么 preferWebsocket 那一支
        # 找不到任何桶，会回落到普通的最高档逻辑，硬隔离仍然成立。
        return ""
    higher_ws = [p for p in band.ws_tiers if p > new_priority]
    lower_ws = [p for p in band.ws_tiers if p < new_priority]
    if new_has_ws:
        parts = [
            "本条目带 websockets: true —— 下游用 WS 连接时 CPA 会跨档"
            "取第一个含 ws 凭据的档（scheduler.go:987，源码注释明确说"
            "「即使它在更低的档」）"
        ]
        if higher_ws:
            hosts = sorted({h for p in higher_ws
                            for h in band.ws_hosts_at.get(p, [])})
            parts.append(
                f"更高的 ws 档还有 {'、'.join(hosts[:4])}"
                f"（档位 {'/'.join(str(p) for p in higher_ws[:4])}）—— "
                f"WS 请求会先打它们")
        if lower_ws:
            hosts = sorted({h for p in lower_ws
                            for h in band.ws_hosts_at.get(p, [])})
            parts.append(
                f"更低的 ws 档 {'、'.join(hosts[:4])} 会被本条目挡在后面，"
                f"即使中间那些档比本条目健康")
        if not higher_ws:
            parts.append("本条目将成为 WS 请求的首选")
        return "；".join(parts) + "。"
    # 不带 ws
    hosts = sorted({h for p in band.ws_tiers
                    for h in band.ws_hosts_at.get(p, [])})
    return (
        f"本条目没有 websockets: true —— 下游用 WS 连接时 CPA 会跨档去找带 ws "
        f"的凭据（当前是 {'、'.join(hosts[:4])}，档位 "
        f"{'/'.join(str(p) for p in band.ws_tiers[:4])}），"
        f"本条目在哪一档都不参与那条路径。"
        f"上面的档位结论只对 HTTP 请求成立。")


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


def _shadow_warning(band: Band, models: list[str], priority: int,
                    shadow: dict[str, list[str]], *,
                    pinned: bool = False) -> str:
    """「挡住了谁」这条警告的正文。

    为什么要分开活站与死站（2026-09-02 演练发现）：原来只报总数，
    实测输出是「priority 280 会把 2 个现有站挡在其后（hotel.example、
    mike.example）」—— 而那两个站在注释里都记着实测不可用，定档算法数出来的
    在用站是 **0**。同一件事，警告说「挡 2 个」、算法说「挡 0 个、无代价」。

    用户看到的是前者，于是会去调低一个本来最优的档位。README 早就写着
    「这条警告还会区分被挡的是活站还是死站」，但代码里没做 —— 文档超前于实现。

    `pinned=True` 是「沿用原档的既有站」（2026-09-04）。那时结尾**不能**给
    「改成 N 则只挡 M 个」这种建议 —— 本轮根本没有在选档，档位是它自己原来
    就占着的；挡住别人是因为本次给它注册了原来没有的模型。给出「往下挪」的
    建议会把用户引向一个错的动作（改动一个不该动的既有值）。
    """
    hosts = sorted(shadow)
    dead = _dead_shadowed(band, models, priority)
    live = [h for h in hosts if h.lower() not in dead]

    if not live:
        head = "、".join(hosts[:5]) + ("…" if len(hosts) > 5 else "")
        return (f"priority {priority} 排在 {len(hosts)} 个现有站之前"
                f"（{head}）—— 它们**全部**已实测不可用或 weight:0，"
                f"挡住它们无代价。注意：这些站若日后恢复，本档位不会自动重算")

    head = "、".join(live[:5]) + ("…" if len(live) > 5 else "")
    msg = (f"priority {priority} 会把 {len(live)} 个**在用**站挡在其后"
           f"（{head}）—— 它们只在本站也不可用时才被尝试。")
    if len(hosts) > len(live):
        msg += f"另有 {len(hosts) - len(live)} 个已不可用的站，挡住无代价。"
    if pinned:
        # 沿用原档：档位不是本轮选的，新增的遮挡来自本次给这个站注册的模型。
        blocking = sorted({m for ms in shadow.values() for m in ms})
        which = "、".join(blocking[:4]) + ("…" if len(blocking) > 4 else "")
        return (msg + f"本档是该站原有的（本轮未改），遮挡来自本次给它注册的"
                      f"模型（{which}）—— 要解开就从模型清单里去掉它们，"
                      f"而不是改这个站的 priority")
    # 空档内取任何值效果都一样（465 与 200、890 挡的是同一批站），
    # 真正的选择是「插哪个空档」。所以不说「手工调低」，直接给下一档
    # 的具体值和代价，省掉用户自己试的那一轮。
    alt = gentler_option(band, models, priority)
    if alt:
        alt_pri, now_n, alt_n = alt
        msg += f"改成 {alt_pri} 则只挡 {alt_n} 个在用站（现 {now_n} 个）"
    else:
        msg += "已是挡在用站最少的可插档，再低要手工指定"
    return msg


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
    # ---- 段专属能力开关（2026-09-04）----
    #
    # 三态：True 写 `<字段>: true`；False 与 None 都**不写**（CPA 的零值就是
    # 关闭），但界面措辞不同 —— False 是「实测不支持」，None 是「未探测」。
    # 把这两者显示成一个样子就是「未验证当已验证」的镜像错误。
    #
    # 值的来源有三层，与 max_context_length 同一套优先级：
    #   ① 本次实测（SectionVerdict.websockets / prompt_cache_key）
    #   ② 原条目的值（prior_toggles，全量重探时搬回来）
    #   ③ 都没有就不写
    #
    # 为什么 ② 不可省：这两个字段在 `_RENDERED_KEYS` 里，所以 carry 不搬；
    # 而关掉能力探测（--no-capabilities）时方案侧是 None。不搬的话原有的
    # `websockets: true` 会静默消失 —— 与 headers / proxy-url 完全同构的空档。
    websockets: bool | None = None
    websockets_note: str = ""
    # claude 段：实测需要请求体级 Claude Code 身份时，交给 CPA 自己补。
    # 取值从 CPA 源码解析，不写死（见 build_plan 里的说明）。
    cloak_mode: str = ""
    fingerprint_profile: str = ""
    # claude 段：对话中途的 system 消息要不要让 CPA 挪到顶层（实测得出）。
    rebuild_mid_system: bool | None = None
    # 冷却策略。None = 跟随全局（字段缺席，与 CPAMP 界面默认一致）；
    # False = 强制启用冷却（即使全局禁用）。只在有实测证据时才覆盖。
    disable_cooling: bool | None = None
    prompt_cache_key: bool | None = None
    prompt_cache_note: str = ""
    # 原条目里这两个开关的值，{字段名: 值}。只收显式写了 true 的
    # （CPA 的零值即关闭，写 false 与不写等价，所以不必区分）。
    prior_toggles: dict[str, bool] = field(default_factory=dict)
    # 原条目曾被停用（`disabled: true` 或 `excluded-models: ["*"]`），
    # 而本轮探测判定可用 —— 这两行要在写回时清掉，把这个站放回调度池。
    #
    # 用户 2026-09-16 的要求（原话）：「无论原来是否被关闭，如果探测可用就要打开」。
    #
    # 为什么是一个「动作」标志而不是直接在 carry 里删：carry_lines 按原文行
    # 搬运，删掉与否得由写回那一层做（它才拿得到原文行）。而这里判定「该不该
    # 删」的时机最好 —— 只有 build_plan 同时知道「原条目停用过」与「本轮探通了」。
    # 值是要清掉的字段名列表（compat 段是 ["disabled"]，其余段是
    # ["excluded-models"]），空列表 = 不动。
    #
    # 判据不含 weight: 0：那是**权重**不是停用开关（weighted-round-robin 下
    # 等效逐出调度池，但它是操作员的定量表达），改它属于改语义，不做。
    reenable_fields: list[str] = field(default_factory=list)
    # carry 行是否已经装配过。存在的唯一理由是**区分「空」与「没补」**
    # （2026-09-16 实测踩到）：写回侧 `attach_carry` 原来用
    # `if sp.carry_lines: return` 判断「已经补过了」，而重开停用条目时
    # 我们**故意**把 carry 清成空 —— 那个判断随即认为「还没补」，
    # 把原条目的 `excluded-models: ["*"]` 整份加了回来，重开静默失效。
    # 三个判据（非空 / 已装配且故意为空 / 尚未装配）必须分开表达。
    carry_attached: bool = False
    # 原条目里**每个模型自己**的 max-context-length，{模型名: 值}。
    #
    # 为什么要单独一份（2026-09-03 逐字段对账发现）：这个值在 `models:` 块里，
    # 而 extract_carry_lines 有意跳过整个 models 块（清单由方案重新生成）。
    # 于是它落进空档 —— carry 不搬，方案只带本次实测的那**一个**
    # （max_context_length + context_model）。本次没探上下文时，历史实测值
    # 全部消失。实测生产配置 8 处，kilo.example 的 987500 就在其中。
    #
    # 优先级：本次实测（context_model 那一个）> 原值搬运 > 不写。
    # 见 render_entry 的 model_lines。
    prior_context: dict[str, int] = field(default_factory=dict)
    # 原条目里每个模型的**白名单外字段**，{模型名: {字段: 值}}。
    #
    # render_entry 的 model_lines 只写 name / alias / max-context-length，
    # 而 CPA 的模型条目还支持 display-name / force-mapping / image /
    # input-modalities / output-modalities / is-compat / thinking
    # （config_types.go 的四个 *Model 结构体）。carry 跳过整个 models 块，
    # 所以这七个字段没有任何人接 —— 手工加一个 `thinking:` 之后整段重写就
    # 静默抹掉它，而 YAML 合法、validate 报成功。
    #
    # 当前生产 config.yaml 一个都没用到（实测 extras 表为空），所以这是
    # 补上闸而不是修一个已发生的事故。见 existing_model_extras。
    prior_model_extras: dict[str, dict] = field(default_factory=dict)
    score: int = 0
    # 模型清单从哪来，可信度递减：
    #   probed  —— 推理请求实测通过，返回的 model 字段与请求一致
    #   catalog —— 只是目录 GET 读到的，站方声称有，未经推理验证
    #   seed    —— 本工具写死的猜测（SEED_MODELS）。目录也关了、探测也没通、
    #              操作员也没填时的兜底。可信度最低，但是个确定值
    #   manual  —— 操作员手填，工具没验证过
    # 必须一路带到界面：「验证过」和「站方声称有」不能长一个样。CPAMP 的
    # 「模型」列就是显示 config.yaml 里写了几个（rowData.ts:78），并排放在
    # 真实转发统计旁边，看着像测活结果 —— 那个坑不要再踩一遍。
    model_source: str = "probed"
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
    # 原条目里 render_entry 不认识的字段，按 YAML 原文行搬运。
    #
    # 为什么必须有（2026-09-02 拿生产 config.yaml 核对发现）：render_entry 是
    # 白名单式渲染（只写它知道的 12 个字段），而全量重探会用它**整段重写**。
    # 生产配置 121 个条目里 117 条带白名单外的字段，重写后全部静默消失：
    #
    #   request-scoped-errors  116 条  按状态码+正文做冷却，丢了就没有冷却
    #   fingerprint-profile      1 条  claude 段让 CPA 自己补设备指纹
    #
    # 2026-09-04 重新点过：以前这张表里的 `excluded-models 39 条` 与
    # `disabled 1 条` 都是 0（那两个数把注释也数进去了）；`websockets` 移到
    # 实测那条路（见 writeback._toggle_lines）。
    #
    # 存原文行而不是解析后的值：这些字段的结构任意深（request-scoped-errors
    # 是对象数组），重新序列化既要处理缩进又要处理引号风格，而原文行拿来就能
    # 用、且逐字保真。键序也跟着原文，diff 干净。
    carry_lines: list[str] = field(default_factory=list)
    # 新条目要补的 request-scoped-errors（2026-09-13）
    #
    # 为什么需要它：carry_lines 只对**既有**条目有值（_prepare_source_plan 从
    # _original_entry 取），而 render_entry 是白名单渲染、白名单里没有
    # request-scoped-errors。于是原文件没有的 (凭据,段) 写出来一条规则都不带。
    #
    # 这个块是「上游返回余额不足 / 被封 / CF 挑战就立刻跳下一个凭据」的唯一
    # 开关（action: continue-and-cooldown）。生产 config.yaml 里 162 个条目
    # 有 116 个带它，且只有 2 种形状、其中一种占 115 个 —— 也就是说这是本部署
    # 的既定策略，新站不带等于把它排除在容灾之外：那把 Key 没钱了，CPA 会
    # 一直重试它而不降级到下一个。
    #
    # 取值一律**从既有条目学**（writeback.learn_scoped_error_rules），学不到
    # 就留空、不写 —— 绝不写死一份规则表，那既违反「禁止硬编码」也可能与
    # 部署策略冲突。
    #
    # 存**解析后的值**（对象数组）而不是原文行：缩进只有 render_entry 知道，
    # 它的 field 参数按段与调用路径变（compat 段的 provider 级与 key 类段
    # 不同层）。在别处定死缩进会渲染出错位的 YAML —— 实测 compat 段直接语法
    # 错误、validate 拒收。序列化由 render_entry 用它自己的 field 做。
    scoped_error_rules: list = field(default_factory=list)
    # 站方目录的最高世代已落后市面最新一个世代以上。
    #
    # 2026-09-02 现场：romeo.example 的 codex 段目录只有 gpt-4 /
    # gpt-4-32k / gpt-4o / gpt-4o-mini，四个都是世代 (4,0)，于是「同产品线
    # 取最高世代」把四个全留下并默认全勾 —— 而用户要的是「最新是 gpt-5.6 时
    # gpt-4o 不该默认勾选」。
    #
    # 只降级 recommended，不动 models：那个站的目录里确实没有 5.6 系的名字，
    # 换成市面清单会写出 CPA 路由不到的模型，把「有老模型可用」变成死条目。
    # 确知可用的人仍可手工勾。
    catalog_stale: bool = False
    catalog_stale_why: str = ""
    # 落盘时这一段是「新增」还是「更新既有条目」，以及新增有没有被放行。
    #
    # 为什么要放在方案对象上而不是只在 writeback 里判（2026-09-03）：
    # 上一版那道闸只存在于 rebuild_config_full 内部，界面按「没有闸」渲染 ——
    # `writable` 与 `recommended` 都是 True，显示「建议写入」并默认勾上，
    # 勾了写不进，只在 warnings 里留一句话。代码里有闸、界面按没闸渲染，
    # 是这一类缺陷的通用形态。
    #
    # 由 rebuild_config_full 回填（它是唯一知道 cfg 里原本有什么的地方），
    # plan_json 直接读 —— 界面显示的就是写盘那一刻的判定，不是另算一遍。
    new_section: bool = False
    # 非空 = 这一段不会被写入，值就是原因（直接显示给操作员）。
    write_blocked: str = ""
    # compat 段的 provider `name`，以及本条目原有的 `prefix`。
    #
    # 为什么必须搬运（2026-09-03 拿真实文件做逐字段 deep-equal 才抓到；
    # 之前只比字段**出现次数**，两处都数得对，值全错）：
    #
    #   · name —— 它就是 CPA 的 provider 身份：
    #     `util.OpenAICompatibleProviderKey(name)` 的结果写进 Auth 的
    #     `provider_key`，而冷却（conductor_cooldown.go:73）、模型能力
    #     （api_key_model_capabilities.go:186）、执行路由三处都按它索引。
    #     render_entry 原来用 `host_of(base_url)` 现编一个，于是实测 12 个
    #     provider 全部改名（`romeo` → `romeo.example`）——
    #     改名等于把它们的冷却状态与能力缓存全部作废，而且本项目自己的
    #     `name_alias_map`（注释里的人读短名 → 域名）也跟着失效，
    #     下一轮读注释拿健康度就大面积漏判。
    #
    #   · prefix —— `dominant_prefix` 只在 70% 以上统一时才给值，那是给
    #     **新条目**猜的默认值。全量重探是在更新既有条目，那一条自己写的
    #     prefix 才是真的。实测 121 个条目的 prefix 全被抹掉：
    #     `force-model-prefix: false` 下 prefix 额外注册一个 `ANT/xxx`
    #     命名空间别名（service_models.go:600-614），抹掉等于让所有按
    #     `ANT/claude-opus-5` 发的请求命中不到。
    provider_name: str = ""
    # Additive API metadata; existing models and constructor positions stay intact.
    highest_models: list[str] = field(default_factory=list)
    model_provenance: dict[str, str] = field(default_factory=dict)

    @property
    def hijacked(self) -> list[Impact]:
        return [i for i in self.impacts if i.hijacks]

    @property
    def writable(self) -> bool:
        # write_blocked 非空 = 落盘那一层会拒掉它。界面必须与落盘一致 ——
        # 显示「可写」而实际写不进，操作员勾了也没有反馈，是 2026-09-03
        # 现场那一类缺陷的成因。
        return not self.duplicate and bool(self.models) and not self.write_blocked

    @property
    def recommended(self) -> bool:
        """系统是否**建议**默认勾选写入。「能写」与「建议写」是两件事。

        不建议的三类（都还是 writable，只是默认不勾，用户可手工勾上）：
          · 静默换模 —— 照常计费却返回另一个模型，比不可用更危险
          · 抢走顶层 —— 层级隔离下现有顶层站会完全不被尝试
          · 上限由截断反推 —— 那个数字是实测容量而非站方声明，可能偏保守
          · 模型未经推理验证 —— 只有目录或手填，站方声称有不等于这把 Key 能用
        """
        if not self.writable:
            return False
        if self.model_source != "probed":
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
        if self.write_blocked:
            return self.write_blocked
        if not self.models:
            return "无可信模型，写进去等于死条目"
        if self.model_source == "catalog":
            if self.catalog_stale:
                return (f"模型取自站方目录（{len(self.models)} 个），但"
                        f"{self.catalog_stale_why} —— 整份目录都是老款，"
                        "默认不勾。确知该站只卖这些且够用，可手工勾上")
            return (f"推理未通过，模型取自站方目录（{len(self.models)} 个）"
                    " —— 参数已按试用期算全，确知可用再勾")
        if self.model_source == "manual":
            return f"手填 {len(self.models)} 个模型，工具未验证 —— 参数已算全"
        if self.model_source == "prior":
            return (f"推理未通过、目录也读不到，已沿用原 config.yaml 里这个"
                    f"条目的 {len(self.models)} 个模型 —— 那是先前一轮的实测"
                    "沉淀，比工具猜测硬；但本次没验过，默认不勾")
        if self.model_source == "seed":
            return ("推理未验证到可用模型（探测未通过，或端点通但返回的模型"
                    f"对不上），清单取自「当前市面最新」（{len(self.models)} 个）"
                    " —— 参数已按试用期算全，勾选前请确认这些名字该站真有")
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
    # 候选的唯一身份 = 输入行号。
    #
    # 为什么不能用 host（2026-09-02 现场）：一个站有多把 Key 是常态
    # （实测 gorou 15 把、tango 14 把）。前端把 (host, section) 当
    # 勾选键，Set 去重后 15 个 Key 在同一段上只剩 1 个选择；表格行用
    # data-host 定位，querySelector 只找到第一行 —— 后 14 行的勾选状态与
    # priority 回填全落到第一行上。表现就是「全勾选只勾中 26 项」。
    #
    # 用行号而不是 api_key：key 是明文，绝不进 DOM 属性与 JSON 响应。
    # 行号在一次任务内唯一（parse_lines 按输入行编号），够做身份。
    line_no: int = 0
    sections: dict[str, SectionPlan] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)  # 段 -> 不写入的原因

    @property
    def any_writable(self) -> bool:
        return any(p.writable for p in self.sections.values())


# 各段的「标准档」档名。判死的段回落配头时用它。
#
# 必须按档名而不是 tier 数字取 —— compat 段的梯子把 cc 族嵌在 openai-sdk
# 之上，tier 编号整体后移一位，取 tier=2 会拿到 claude-cli 形态的
# anthropic-beta 写给一个走 /chat/completions 的段（见 _fallback_headers）。
_STD_PROFILE = {
    "gemini-api-key": "gemini-cli-full",
    "codex-api-key": "codex-tui",
    "claude-api-key": "cc-std",
    "openai-compatibility": "openai-sdk",
}


def _fallback_headers(section: str, v, cfg: dict | None,
                      api_key: str = "") -> dict[str, str]:
    """判不可用的段该配哪套请求头。

    min_headers 只在「找到最省可用档」时才有值，判死的段永远是空的。可是
    判死的多数是门禁站 —— 门票不对正是它判死的原因。空 headers 写进
    config.yaml 等于写了个必废的条目，而这条要求是明确的：勾选了就得有
    确定的参数，不留「未定」。

    取探测**实际打到的最高档**，因为那是实测走过的最完整形态。没有任何
    id: 尝试记录时（连门票梯都没进就死了，比如 DNS 不通）回落到该段标准档
    —— 不取全量档，设备指纹那类头有站方会拒。

    **必须走 `profiles.render` 求值**（2026-09-04 逐字段对账发现）
    -------------------------------------------------------
    `Profile.headers` 是**模板**，含 `{uuid1}` / `{key_hash}` 这类占位符。
    正常路径取的是 `v.min_headers` —— 那是 `materialize` 求过值的产物；
    只有这一支直接 `dict(p.headers)`，于是占位符原样写进 config.yaml。

    实测已落进生产文件：`fsdownload/config.yaml` 里
    `x-claude-code-session-id: "{uuid1}"` 出现 5 处（claude 段 4、compat 段 1），
    而同一份 Desktop 配置里那个头是真 UUID。CPA 会把它原样发给上游
    （`util/header_helpers.go` 的 ApplyCustomHeadersFromAttrs 只判非空、
    不校验值形态），站方看到字面 `{uuid1}` 作为会话 id。

    只对 headers 求值，不碰 body_patch —— 那是请求体字段，条目只支持 headers
    （见 profiles.config_advice）。所以用 `render` 而不是 `materialize`：
    后者会连 body_patch 一起算，而 headers 与 body 的 UUID 必须同源那条约束
    只在**发请求**时成立，写 config.yaml 时没有 body 侧。

    标准档按**档名**指定，不按 tier 数字（2026-09-01 修正）
    ------------------------------------------------------
    原来写 `p.tier == 2`，而 tier 在四段里指的不是同一个东西：compat 段的
    梯子把整个 cc 族嵌在 openai-sdk 之上（为覆盖「中转站只认 Claude Code」
    那种情形），编号因此整体后移一位 —— 于是 tier=2 在 gemini/codex/claude
    段分别是 gemini-cli-full / codex-tui / cc-std（都对），在 compat 段却是
    **cc-min**：给一个走 /chat/completions 的段写 `anthropic-beta`。

    那是 Anthropic 协议专属头，compat 段发它毫无意义，还可能让本来能过的
    站因为多了个看不懂的头而拒。族名不会随梯子插档而漂移，所以按名字取。
    """
    from .profiles import ladder as _ladder
    from .profiles import render as _render

    tried = {a.combo[3:] for a in v.attempts if a.combo.startswith("id:")}
    rungs = _ladder(section, cfg, include_alt=False)
    if tried:
        hit = [p for p in rungs if p.name in tried]
        if hit:
            return _render(dict(max(hit, key=lambda p: p.tier).headers),
                           api_key)
    want = _STD_PROFILE.get(section, "")
    std = [p for p in rungs if p.name == want]
    if not std:
        # 档名没命中（梯子改过名）：退到该段 tier 最低的非 baseline 档，
        # 而不是某个写死的数字 —— 宁可少几个头也不要发错协议的头。
        std = sorted((p for p in rungs if p.tier >= 1), key=lambda p: p.tier)
    return _render(dict(std[0].headers), api_key) if std else {}


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
    rebuild: bool = False,
    raw: str = "",
) -> ImportPlan:
    """把一个候选的探测结果变成写入方案。

    去重要查**两层**，问的是两个不同问题：
      seen        五元组指纹（与 CPA 同口径）—— 「这两行配置是否完全相同」
      seen_pairs  (key, base) 对             —— 「这个凭据在这个站配过没」
    第二层不能省：现有条目常带 prefix / headers（foxtrot 的 claude 条目
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

    rebuild：全量重探模式。**关掉去重判定**。
    --------------------------------------
    两种模式的输入语义完全相反：

      · 新增导入（rebuild=False）：输入是新 Key，`seen` 代表「config.yaml
        里已有的 + 本批已处理的」，撞上就是真重复，该挡。
      · 全量重探（rebuild=True）：输入**就是** config.yaml 里的既有条目，
        而 `seen` 是从同一份 cfg 读出来的 —— 每一条都必然撞上。

    2026-09-02 实测后果：79 个凭据全量重探，「全勾选」只勾中 26 项。
    14 个 host 里每个 host 只有第一个 Key 逃过判定（它的 prefix/headers 与
    探测建议不同、五元组恰好没撞上，那是偶然不是设计），其余全部 duplicate
    → writable=False → 全勾选跳过。14 × 4 段 = 56 个段有方案，其余 260 个
    段连勾选框都点不动。

    重探要判的不是「有没有重复」，而是「这次的方案与原条目有没有变化」——
    那个由 diff 预览呈现，不需要在这里拦。

    raw：config.yaml 原文，转交 `build_band`。给了才会解析注释里的「实测不可用」
    结论，而那直接决定「挡住下层算不算代价」。实测差距（生产 config.yaml，
    满分候选）：claude 段 175 → 500、gemini 段 225 → 280。不传不报错，只是
    把可用新站压到一堆死站后面 —— 单站诊断与批量导入因此给出不同的 priority，
    正是「三条途径字段齐平」要消除的那类差异。
    """
    bands = bands or {}
    existing = seen if seen is not None else existing_fingerprints(cfg)
    pairs = seen_pairs if seen_pairs is not None else existing_pairs(cfg)

    plan = ImportPlan(host=row.host, masked_key=row.masked(),
                      line_no=row.line_no)

    force = force or {}
    for section, v in result.sections.items():
        model_warns: list[str] = []
        # 手填清单的过滤：**只挡协议层不可能成立的**，不挡族。
        #
        # 分两类，判据完全不同（2026-09-03 拿真实配置核实后区分开）：
        #
        #   ① 段协议不匹配 —— 真的挡。claude 段走 Anthropic 原生
        #      `/v1/messages`（claude_executor_execute.go:23），往那里发
        #      `gpt-5.6-sol` 上游必失配；gemini 段走 generateContent 同理。
        #      非对话模型（图像/语音/嵌入）也挡：2026-09-02 实测手填的 8 个里
        #      有 gpt-image-2 / gpt-oss-*，路由过去必失配。
        #
        #   ② 四族之外（grok / glm / deepseek / qwen / llama）——**不挡**。
        #      这类模型在 compat 段是完全合法的：那一段走
        #      `/chat/completions`（openai_compat_executor.go:107），CPA 侧
        #      对模型名零校验（buildOpenAICompatibilityConfigModels 照单注册，
        #      service_models.go:713-739），能不能用只取决于上游认不认。
        #
        #      为什么必须放行（真实配置的反例）：romeo 的 compat 段
        #      **唯一端到端验证过的模型就是 grok-4.6**（配置注释：「整个 vip
        #      分组当前只有 grok-4.6 有渠道，已通过端到端验证的只有它」），
        #      foxtrot 段同样有 grok-4.6 + glm-5.2。按族挡掉手填，操作员就
        #      再也没有办法把这个**已知可用**的模型写回去 —— 那一段会从
        #      「有一个确认可用的模型」变成「只剩两个确认 503 的」。
        #
        #      「本工具的模型库不主动推荐四族之外」是选型偏好，把它升级成
        #      「操作员显式指定也不许」就越权了。
        #
        # 为什么不静默丢而要留 warning（下面 forced_dropped）：手填是用户的
        # 显式意图，悄悄改掉它比拒绝更糟 —— 用户会以为写进去了。
        forced_raw = [str(m).strip() for m in (force.get(section) or [])
                      if str(m).strip()]
        forced_kept = [m for m in forced_raw
                       if model_catalog.section_protocol_ok(section, m)]
        # `keep_low_tier=True`：手填的 mini / nano / lite 照写。
        # 「带 mini 的一律不勾」是本工具的**选型偏好**（用户 2026-09-12），
        # 与「不主动推荐四族之外」同一性质 —— 把它升级成「操作员显式指定
        # 也不许」就越权了，理由见上面 forced_kept 那一段。
        forced_models = model_catalog.newest_generation_per_line(
            forced_kept, keep_low_tier=True)
        forced_dropped = [m for m in forced_raw if m not in forced_models]
        # 四族之外但被放行的手填项 —— 界面要说清「工具不推荐但已按你说的写」。
        forced_offfamily = [m for m in forced_models
                            if not model_catalog.section_allows(section, m)]
        # 「市面最新清单」的来源说明。只有走到 seed 分支才会被赋值，
        # 但要在这里初始化 —— 下面的警告分支无条件读它。
        model_src = ""
        # 判不可用的段也要生成完整方案 —— 只是默认不勾（recommended=False）。
        # 曾经在这里 continue 掉，后果是界面上判死的段没有 priority / headers /
        # 代理 / 指纹可看，勾选框灰着，不手填模型就无法勾选；而很多站禁止
        # 测活却确实可用，那样等于把可用站丢掉。
        #
        # 前提是有模型清单：手填 > 探测通过 > 目录 GET 读到的。目录是 CPAMP
        # 测活的唯一手段（它连推理都不发），可信度足够当候选。三者全空才跳过 ——
        # 那时连注册哪些模型都不知道，compat 段的 models 还是必填字段
        # （config_types.go:679 无 omitempty）。
        # 三者全空时**不再跳过**：退到该段的种子模型。
        #
        # 为什么改（2026-09-01 用户实测）：79 个凭据全量重探后「全勾」只勾中
        # 4 项。原因就是这一条 —— 目录关闭 + 判死的段直接缺席方案，界面上连
        # 勾选框都没有，操作员想接管也无从下手。而中转站关 /models 是常态。
        #
        # 种子模型是本工具写死的猜测（SEED_MODELS），可信度最低，所以：
        #   · model_source 记成 "seed"，界面上与 probed/catalog 明确区分
        #   · recommended 恒为 False，绝不替操作员做决定
        #   · 带警告说明它没有任何实测依据
        # 但它是**确定的值**，不是「待定」—— 用户的硬要求是写进 config.yaml
        # 的参数不能有未定项，缺席比填错更难排查。

        base = getattr(v, "base_url", "") or base_for_section(row.bare, section)
        proxy = ""
        if v.need_proxy:
            successful = str(getattr(v, "successful_proxy_url", "") or "")
            mapped = str(getattr(v, "cpa_proxy_url", "") or
                         getattr(result, "cpa_proxy_url", "") or "")
            runtime = str(getattr(v, "proxy_url", "") or
                          getattr(result, "proxy", "") or "")
            proxy = mapped or successful or runtime or _proxy_url_for_config()
            proxy_host = urlsplit(proxy).hostname or ""
            try:
                loopback = ipaddress.ip_address(proxy_host).is_loopback
            except ValueError:
                loopback = proxy_host.rstrip(".").lower() == "localhost"
            if not proxy:
                model_warns.append("需要代理，但没有已确认的 CPA 可达代理映射；请明确指定后写回")
            elif loopback:
                model_warns.append("探测使用本机代理；CPA 的 localhost 可能指向另一容器，"
                                   "需要明确的可达代理映射")
            elif not successful and not mapped:
                model_warns.append("代理来自显式运行配置，但尚无成功出口证据；请确认 CPA 可达")
        headers = dict(v.min_headers) if v.need_ua else {}
        if not headers and not v.usable:
            # 判死的段：min_headers 是空的（探测没走到「确定最省可用档」那步），
            # 但门禁站恰恰是判死里的多数 —— 它判死的原因往往就是门票不对。
            # 空 headers 写进 config.yaml 那条目必然废掉。
            #
            # 取探测实际打到的最高档门票：那是实测走过的最完整形态，比猜一个
            # 档次可靠。不取全量档 —— 设备指纹那类头有站方会拒。
            headers = _fallback_headers(section, v, cfg, row.api_key)

        # claude 段：body 级身份要落成 CPA 的 cloak / fingerprint-profile
        # ----------------------------------------------------------------
        # 这是探测结论**落不进配置**的一个缺口（2026-09-11 修）。
        #
        # 画像梯的 `cc-body-json` / `cc-body-plain` / `cc-body-system` 三档，
        # 门票不在 headers 里而在**请求体**（`metadata.user_id`、
        # Claude Code 的 system 块）。条目的 `headers:` 字段表达不了请求体，
        # 于是这三档命中时，探测明明测出「这样发能通」，写回却只写了 headers ——
        # CPA 实际发出去仍然缺身份。
        #
        # 实测（2026-09-11，papa.example，claude 段）：
        #   baseline（仅 x-api-key）        -> 403 Cloudflare error 1010
        #   cc-min（UA + anthropic-beta）   -> 503 only allows Claude Code clients
        #   cc-std / cc-full（补齐所有头）  -> 503 同上
        #   cc-body-system（+system 块）    -> **200**
        # 也就是这个站根本不看头，只看请求体里那段 Claude Code system 块。
        # 用户截图里那条 `测试失败: 503 No available accounts: this group only
        # allows Claude Code clients` 就是这么来的，而截图底部「请求伪装」是关的。
        #
        # CPA 侧对应的能力正是 `cloak`（config_types.go:348-359）：
        # mode=always 时对每个未确认的客户端补上 Claude Code 的身份与计费块。
        # 合法取值从源码解析（见 cpa_source_probe.parse_claude_identity_opts），
        # **不写死** —— CPA 的写入路径会用 ValidateClaudeFingerprintProfile
        # 拒绝不认识的值，写错一个字这条配置就静默落不进去。
        #
        # 只在**实测确实需要 body 门票**时才写：不需要的站写上等于凭空改写
        # 它们的请求体，属于负向调整。
        cloak_mode = ""
        fp_profile = ""
        # 两种触发条件（2026-09-13 补第二种）
        # --------------------------------------
        # ① min_body_kind 非空 —— 画像梯实测出「补了 body 才通」。
        # ② category == "客户端" —— 站方明说只认特定客户端
        #    （classify.py:103 认的 `only allows … clients`，实测 503）。
        #
        # 为什么必须加第二种：这类站**探测阶段就没通过**，min_body_kind 是空的，
        # 于是上一版一个字段都不写。而它正是用户第 1 条要问的那个现象 ——
        # 同一个站填进 cc switch 用 Claude Code 直连正常、经 CPA 就 503。
        #
        # 成因在 sub2api 侧（backend/internal/service/claude_code_validator.go）：
        # claude_code_only 分组校验四项 —— UA 匹配 claude-cli/x.y.z、
        # system prompt 与官方模板 Dice 相似、anthropic-beta 头、
        # metadata.user_id 格式。四项缺一不可，光补 headers 过不了。
        # CPA 侧能补齐这四项的开关就是 cloak.mode + fingerprint-profile
        # （claude_executor_cloaking.go 里 injectFakeUserID 补 user_id、
        # checkSystemInstructions* 补 system 块、fingerprint 带 OAuth betas）。
        #
        # 注意不要退回「注入 proxy-url」那条路 —— 见 :2544 那段的三条论证。
        _client_gate = getattr(v, "category", "") == "客户端"
        if section == "claude-api-key" and (getattr(v, "min_body_kind", "")
                                            or _client_gate):
            try:
                from .cpa_source_probe import cached_identity
                ident = cached_identity()
                modes = ident.claude_cloak_modes or []
                profs = ident.claude_fingerprint_profiles or []
            except Exception:
                modes, profs = [], []
            # always：连「已确认的原生 Claude Code 客户端」之外的全部请求都补身份。
            # 用 always 而不是 auto —— auto 会在「有强信号表明是原生入口」时放行，
            # 而经 CPA 的请求恰恰常被判成原生，那就等于没开。
            if "always" in modes:
                cloak_mode = "always"
            # system 块那一档还要 CLI 指纹：它带的是 OAuth betas + 稳定 CLI 身份，
            # 与 system 块是同一套形态的两半。
            #
            # 「客户端」类同样要：sub2api 的四项校验里 anthropic-beta 与
            # system prompt 分属两半，只开 cloak 不带 CLI 指纹仍会缺 beta 头。
            if (("+system" in getattr(v, "min_body_kind", "") or _client_gate)
                    and "claude-code-cli" in profs):
                fp_profile = "claude-code-cli"
            if cloak_mode or fp_profile:
                _wrote = "、".join(filter(None, [
                    f"cloak.mode={cloak_mode}" if cloak_mode else "",
                    f"fingerprint-profile={fp_profile}" if fp_profile else ""]))
                if _client_gate and not getattr(v, "min_body_kind", ""):
                    # 探测没通过，写的是「按站方拒绝理由推断的处置」——
                    # 必须说清这是推断而非实测，否则操作员会以为验证过了。
                    #
                    # 判据附在后面（2026-09-14）：上游 sub2api 的
                    # ClaudeCodeValidator 校验哪几项，从它的源码解析而来
                    # （cpa_source_probe.parse_sub2api_validator）。拉不到就
                    # 只写泛化措辞 —— 绝不把阈值写死在这里，那会随上游漂移。
                    _gate = ""
                    try:
                        _heads = ident.s2a_required_headers or []
                        _thr = ident.s2a_prompt_threshold or 0.0
                        _ua = ident.s2a_ua_pattern or ""
                        _bits = []
                        if _ua:
                            _bits.append(f"UA 匹配 `{_ua}`")
                        if _thr:
                            _bits.append(
                                f"system prompt 与官方模板 Dice 相似度 >= {_thr}")
                        if _heads:
                            _bits.append(f"{'、'.join(_heads)} 头非空")
                        if _bits:
                            _gate = ("；上游校验四项（据 sub2api 源码）："
                                     + "，".join(_bits) + "，metadata.user_id 存在")
                    except Exception:
                        _gate = ""
                    model_warns.append(
                        f"站方明确只认特定客户端（探测判「客户端」类，未通过）"
                        f" —— 已按 CPA 的客户端身份开关写入 {_wrote}；"
                        f"这是**依据拒绝理由的推断**，本次未实测通过，"
                        f"写回后请用 CPAMP 的连通性测试复核{_gate}")
                else:
                    model_warns.append(
                        f"该段实测需要**请求体**级 Claude Code 身份"
                        f"（{v.min_body_kind}），headers 表达不了 —— "
                        f"已写入 {_wrote} 让 CPA 自己补上")

        # codex 段：originator 必须无条件写齐（2026-09-10）
        # ---------------------------------------------------
        # 上面那个 `if not headers and not v.usable` 只兜**判死**的段。
        # 可用且 `need_ua=False` 的段（baseline 就通，min_headers 为空）会带着
        # 空 headers 落到下面 :2255 那道门禁上，直接抛 ValueError 把整份方案打断
        # —— 测试套件里 `端点通但模型空也要兜底` / `手填无条件优先` 两项就是
        # 这么红的（本次改动前即已红，见基线）。
        #
        # 为什么不是「放宽那道门禁」而是「补齐 headers」：
        # 生产 config.yaml 全局设了 `codex.disable-codex-cloaking: true`
        # （fsdownload/config.yaml:476）。该开关一开，
        # `applyCodexCloakingHeaders` 直接 return（codex_executor_request.go:373-374），
        # 而 Originator 的另一条来源是
        #     if ginHeaders.Get("Originator") != "" { set } else if !isAPIKey { set 默认 }
        #     （codex_executor_request.go:351-355）
        # —— **API key 认证时那个 else-if 不进**。于是「客户端没送 Originator
        # + 全局关了伪装 + 条目没配 headers」= 一个 Originator 都不发。
        # 也就是说这一段的 headers 不是「可选优化」，是**唯一来源**。
        #
        # **只补 originator，不补 user-agent**（2026-09-10 实测收窄）：
        # `ensureHeaderWithConfigPrecedence(r.Header, ginHeaders, "User-Agent",
        #  cfgUserAgent, codexUserAgent)`（codex_executor_request.go:356）
        # 不受 cloaking 开关影响，客户端与 `codex-header-defaults` 都没给时
        # 会回落到内置 `codexUserAgent` —— 所以 User-Agent 永远有值，不需要
        # 条目兜。强加它会把「最省可用档」撑成非最省，`headers 写入` 那项测试
        # 断言的正是最省档要保持最省。
        #
        # 用段标准档取值而不是全量档：设备指纹那类头有站方会拒（同 _fallback_headers）。
        if section == "codex-api-key" and "originator" not in {
                h.lower() for h in headers.keys()}:
            std = _fallback_headers(section, v, cfg, row.api_key)
            for sk, sv in std.items():
                if sk.lower() == "originator" and sv:
                    headers[sk] = sv
                    break
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

        band = bands.get(section) or build_band(cfg, section, raw=raw)
        bands[section] = band

        # 人工接管的段：模型清单来自操作员，探测那边是空的。
        # 定档也要按这份清单算 —— 影响面是「这些模型各自挡住谁」，
        # 用空清单算出来的影响面恒为 0，等于没算。
        # 三种来源，可信度递减。model_source 要一路带到界面上 ——
        # 「验证过」和「站方声称有」不能在界面上长一个样，那正是 CPAMP
        # 「模型」列的毛病：显示 config.yaml 里写了几个，看着像测活结果。
        models: list[str] = []
        model_source = "probed"
        # 目录里被收下的四族之外的名字。只在「目录里一个四族的都没有」时非空 ——
        # 那时收站方自己报的名字比写工具猜的更可靠。见下面 catalog 分支。
        catalog_offfamily: list[str] = []
        # 目录是否整体落后市面最新一个世代以上。只影响「建不建议勾」，
        # 不影响清单内容。见下面 catalog 分支与 SectionPlan.catalog_stale。
        #
        # 【死字段，2026-09-13 核实】catalog_stale 自本行初始化为 False 之后，
        # 到 :2503 的重置之间**没有任何一处把它设为 True** —— 旧的「站方目录
        # 整体落后就不补齐」策略已由 topup_to_market_top 取代（见 :2356 注释）。
        # 于是 SectionPlan.recommend_reason:1838 的 `if self.catalog_stale:`
        # 分支恒不成立，server.py:928 也只是把恒 False 传给前端。
        #
        # 保留字段是为了 JSON 契约兼容（前端与外部脚本可能读它）。
        # 排障时请注意：「目录来源的段默认不勾」不是这个字段导致的，而是
        # `recommended`:1807 的 `model_source != "probed"` —— 找错地方会白费
        # 一轮（2026-09-13 就误判过一次）。
        catalog_stale, stale_why = False, ""
        # 手填**无条件优先**，不看 usable（2026-09-03 现场，第二次改这一处）。
        #
        # 原来的判据是 `forced_models and not v.usable`，两条路因此被堵死：
        #
        #   ① v.usable=True 但 v.models 为空（静默换模 / 200 包错误体，
        #      `_accept` 把模型全拒了）—— 手填的 1 个模型落不进来，
        #      反而掉进下面的 seed 分支，界面上「手填」变成「猜测」。
        #   ② v.usable=True 且 v.models 非空 —— 操作员想把探到的清单换成
        #      自己那份（探测只验了 4 个，站方实际卖 8 个），probed 直接
        #      盖掉手填，且一条警告都没有。
        #
        # 手填是操作员的显式意图，探测判定是工具的推测。推测盖掉显式意图
        # 在任何处境下都是错的 —— 这与 force 参数的设计意图一致（见
        # docstring：force 只绕过 usable 判定，去重/定档/影响面一道不少）。
        # 「就高」筛掉低世代时攒的告警，等 sp 建好后挂上去（见下方 probed 分支）
        if forced_models:
            models, model_source = forced_models, "manual"
        elif v.usable and v.models:
            # 实测清单也要过「每条产品线只留最高世代」这一闸（2026-09-10）
            # ----------------------------------------------------------
            # 四条模型来源里，probed 原来是**唯一不过闸**的那条：manual 走
            # :2014 的 `newest_generation_per_line`，catalog 走 :2143，
            # seed 走 `model_catalog.latest_models` 内部，只有这里直接
            # `list(v.models)` 落盘。
            #
            # 后果就是用户报的「部分高、低模型同时存在没有就高选择」：
            # `_stage2` 按目录顺序补齐到 max_models，而目录是
            # `request.parse_models_response` 的 `sorted(set(...))`——**字母序**，
            # 于是 `gpt-4` / `gpt-4o` 这类旧款会和 `gpt-5.6-sol` 一起进 v.models，
            # 再原样写进 config.yaml，让 CPA 的轮询把请求分给旧款。
            #
            # 用同一个函数而不是另写一份判据：这份规则在本项目里已经分叉过两次
            # （README「模型库」一节记了三次修正），前端 `web/app.js:199` 是它的
            # 逐条等价拷贝，`tests/test_web.py` 拿同一批名字喂两边比对。
            #
            # 保序：`newest_generation_per_line` 按输入首次出现的顺序输出，
            # 所以 diff 仍然幂等（与 model_catalog.py:358 的承诺一致）。
            #
            # 2026-09-11：原配置的低代不再豁免默认最高代规则。
            # 配置对象与显式别名不在这里改写，交给原有写回链保留。
            from .model_catalog import newest_generation_per_line
            probed = list(v.models)
            top = newest_generation_per_line(probed)
            if len(top) < len(probed):
                models = top
                dropped = [m for m in probed if m not in set(models)]
                if dropped:
                    model_warns.append(
                        f"实测到 {len(dropped)} 个低世代模型未写入"
                        f"（{'、'.join(dropped[:4])}"
                        f"{'…' if len(dropped) > 4 else ''}）——"
                        f"同族已有更高世代，按「就高」原则不注册")
            else:
                models = top
            model_source = "probed"
        elif v.catalog:
            # 目录能读到 —— 取目录里通过段规则的名字。
            #
            # 判据是「实测清单空不空」，**不是** usable（2026-09-03 现场，
            # 与「兜底放在 else 分支」是同一类错误，第四次踩同一个形态）。
            # 原来写 `elif v.usable: ... elif v.catalog:`，于是
            # usable=True 且 v.models 为空（静默换模 / 200 包错误体，
            # `_accept` 把模型全拒了）时，这一支根本走不到 —— 直接掉进下面的
            # 种子兜底。
            #
            # 后果在真实探测里看得很清楚：nova 的 compat 段实测就是这个状态，
            # 站方目录报了 16 个名字，而方案里写的是 6 个种子猜测 ——
            # 界面列出目录那 16 个（一个没勾），落盘写种子那 6 个，两个集合
            # 不相交。而目录里的名字是**这个站自己报的**，种子是本工具猜的、
            # 与这个站没有任何关系：把后者写进去，CPA 路由过去大概率 404。
            models = [m for m in v.catalog
                      if model_allowed(m)
                      and model_fits_section(v.section, m)]
            # 目录里**只有**四族之外的名字时，退一步收下它们
            # （2026-09-03，与上面那条同一个形态、第二次踩）。
            #
            # 「不主动推荐四族之外」是本工具的选型偏好，用于「同时有 gpt-5.6
            # 与 grok-4.6 时挑哪个」。但目录里一个四族的都没有时，这条偏好把
            # 选择变成了：
            #   (a) 写工具猜的名字 —— 这个站**从没报过**它们，CPA 路由过去 404
            #   (b) 写站方自己报的名字 —— 未验证，但至少是这个站说它有的
            # (b) 严格更好。实测 romeo 与 foxtrot 的 compat 段目录里
            # grok-4.6 就是这种处境（配置注释：那是唯一端到端验证过的模型）。
            #
            # 判据仍是 protocol_ok 而不是无条件收：前三段仍按族拒
            # （claude 段走 Anthropic 原生路径，往那里发 grok 必失配），
            # 只有 compat 段的 /chat/completions 真的不限族。
            if not models:
                catalog_offfamily = [
                    m for m in v.catalog
                    if model_catalog.section_protocol_ok(v.section, m)]
                models = list(catalog_offfamily)
            # 同产品线取最高世代：目录里常同时报 gpt-5.5 与 gpt-5.6，
            # 两个都写进去等于让 CPA 把请求分给旧版。
            models = model_catalog.newest_generation_per_line(models)
            if models:
                model_source = "catalog"
                # 旧的「目录落后就不补」策略由下方最高代补齐替代；
                # catalog_stale 字段保留兼容，逐模型来源说明未验证风险。

        # 兜底放在**所有分支之外** —— 只要最终清单为空就填「当前市面最新」。
        #
        # 为什么必须在外面（2026-09-02 现场截图，第三次修同一处）：
        # 前两版把兜底写在 `else:` 里，于是两条路绕过它：
        #
        #   ① v.usable=True 但 v.models 为空 —— 真实存在的状态。`_accept()`
        #      在「静默换模」或「200 但正文是错误体」时拒收模型，段仍算可用
        #      （端点确实响应、凭证有效），但清单一个都没进。截图里那行的
        #      四个标记连起来正是它：可用 + 实测 + 无可信模型 + 不可写入。
        #   ② v.catalog 非空但过滤后为空 —— 上一轮已补，但补在 else 里面，
        #      对 ① 无效。
        #
        # 用户的要求是「实测不可用就填充成对应类型的最高级别模型」——
        # 判据是**清单空不空**，不是「走了哪条分支」。
        if not models:
            # 2026-09-02 用户要求：「如果无法检测出模型，原则上需要在线
            # 检索大数据按当前市面上存在最新模型编号直接填写好」。
            #
            # 三层数据源，见 model_catalog.latest_models：
            #   1. CPA 权威名录（远程，与 CPA 自己的 model_updater 同源）
            #   2. 本地 config.yaml 已有的模型名（站方特供型号只在这层）
            #   3. 内置兜底（用户指定的那批）
            # 同产品线自动取最高世代 —— 出 gpt-5.7 时旧的 5.6 不再放入。
            #
            # remote_names 走 model_catalog 自己的缓存（成功 6 小时 /
            # 失败 10 分钟），所以 79 个凭据串行调用只有第一次走网络。
            remote, _why = model_catalog.remote_names()
            # limit=0 不截断注册清单；HTTP 探测预算由探测阶段单独管理。
            models, model_src = model_catalog.latest_models(
                section, cfg=cfg, remote=remote, limit=0)
            # usable 段落到这里 = 端点通但模型全被拒收（换模/错误体）。
            # 那和「判死且目录读不到」是同一种处境：清单没有实测依据。
            # 记成 seed 让界面照实说，别让它顶着「实测」的徽标。
            model_source = "seed"

            # 重探既有条目时，先收原清单作为该来源的候选（2026-09-06）。
            # 2026-09-11：下方仍统一做最高代筛选；历史低代不再享有豁免。
            #
            # 为什么必须加这一条：兜底清单是「当前市面最新」，它与「这个站
            # 实际卖什么」无关。而重探一个既有条目时，原条目里的清单是先前
            # 一轮实测沉淀下来的 —— 它比工具的猜测硬。原来这里无条件用猜测
            # 清单，于是判死段（中转站关 /models 是常态）的既有条目在写回时
            # 清单被整份换掉。实测这份生产配置：tango claude 条目的
            # claude-opus-4-8 / claude-opus-4-8-thinking 两个模型消失，
            # 换进 claude-fable-5-1 等四个这个站从没验过的名字 ——
            # 那让 CPA 每次轮到它都对着不存在的模型发请求。
            #
            # 判据用「原清单存不存在」而不是 rebuild 标志：手工把既有站
            # 重新粘一遍也是同一个处境，不该因入口不同而两种行为。
            # 仍然记 seed —— 依据强度没有变（原清单也不是**本次**实测的），
            # 界面照实说、默认不勾，只是不再拿猜测覆盖既有事实。
            prior = existing_models_for(cfg, section, base, row.api_key)
            if prior:
                models, model_source = prior, "prior"

        # The approved highest-family policy also applies to existing entries.
        # Catalog and market names are suggestions, never probe successes.
        if not forced_models:
            prior = existing_models_for(cfg, section, base, row.api_key)
            candidates = list(dict.fromkeys(
                list(models) + list(v.models) + list(v.catalog) + prior))
            # 两级过滤，与上面 catalog 分支同一套判据（2026-09-12 接齐）
            # ----------------------------------------------------------
            # `section_protocol_ok` 只问「这个段的协议接不接得住」，compat 段
            # 因此放行四族之外的一切。而**选型偏好**（只挑 gemini / gpt /
            # claude / kimi）是另一层：目录里同时有 claude-opus-5 与 grok-4.6
            # 时该挑前者。
            #
            # 这里原来只用 protocol_ok，于是 catalog 分支刚按偏好挑出
            # `claude-opus-5`，这一步又把 `v.catalog` 整个倒回来，grok-4.6
            # 重新混进清单 —— 偏好被绕过，与 catalog 分支自相矛盾。
            #
            # 一个四族的都没有时才退到 protocol_ok：那时的选择是「写工具猜的
            # 名字（这个站从没报过）」还是「写站方自己报的名字」，后者严格
            # 更好。判据与 catalog 分支的 `catalog_offfamily` 完全一致。
            preferred = [m for m in candidates
                         if model_catalog.section_allows(section, m)]
            candidates = preferred or [
                m for m in candidates
                if model_catalog.section_protocol_ok(section, m)]
            merged = model_catalog.newest_generation_per_line(candidates)
            # 过滤把清单清空时，退回站方自己报的名字（2026-09-12）
            # ------------------------------------------------------
            # `section_protocol_ok` 按族判，而站方特供的简写（实测夹具里的
            # `opus-5`、生产配置里的若干别名）族认不出来 → 被整批滤掉。
            # 原来滤空之后直接进 `topup_to_market_top`，而那时 `models` 已经
            # 是空的，补齐函数的「只补已出现过的产品线」约束失效，于是它填进
            # 整份「市面最新」：一个只报了 opus-5 的站被写成
            # claude-opus-5 + claude-sonnet-5 + claude-fable-5-1 +
            # claude-haiku-4-5-20251001 —— 后三个这个站从没报过，
            # 还把 priority 定档的影响面算错（挡站数变了，试用期档位从 270
            # 抬到 750）。
            #
            # 站方报过的名字是**实测事实**，工具猜的不是。滤空时宁可留下
            # 认不出族的原名，也不要换成一批没有依据的名字。
            if not merged and v.models:
                merged = list(dict.fromkeys(v.models))
            models = merged
            # Keep the supported custom-only compat path without inventing peers.
            custom_only = bool(models) and all(
                not model_catalog.family(m) for m in models)
            if not custom_only:
                remote, _why = model_catalog.remote_names()
                # `proven=v.models` = 本轮实测出 200 且被 `_accept` 收下的名字。
                # 它是 topup 的第二证据层：族内有更高主版本时，实测通过过的
                # 那一代**不**被淘汰（用户 2026-09-16：「检测 gpt-6 系列明显
                # 不通，这个时候 gpt-6 系列按模型目录最高级别保留同时保留实测
                # 最高的 gpt-5.6 系列」）。不传就等于退回「目录说更高就换代」，
                # 会把唯一验过的模型删掉。
                models, added, fill_src = model_catalog.topup_to_market_top(
                    section, models, cfg=cfg, remote=remote,
                    proven=list(v.models))
                if added:
                    model_src = fill_src
                # 归并淘汰了站方报过的低代名字时，把理由挂进该段警告 ——
                # 「少了 gpt-5.6」这种事必须能追到原因，不能悄悄消失。
                _note = model_catalog.take_merge_note(section)
                if _note:
                    model_warns.append(_note)

            # P0 修复：空模型强制回退（2026-09-12）
            # -----------------------------------------------
            # 背景：95% 检测失败（403/405/401/503）→ 空段 → 空模型 → 用户投诉
            # 即使 topup_to_market_top 理论有回退，实际大量站点仍输出空模型
            # 根因：检测全灭时 merged=[]，topup 可能因段名不匹配等原因也返回空
            #
            # 三层保障：
            # 1. 优先：topup_to_market_top 的正常回退（已有）
            # 2. 次之：强制调用 topup 并检查结果（此处新增）
            # 3. 兜底：直接使用 FALLBACK_MODELS（最后防线）
            if not models:
                logger.warning(
                    f"段 {section} 基址 {base[:40]} 模型为空，触发强制回退")

                # 尝试再次调用 topup（无输入、无 cfg、无 remote）
                emergency, emergency_added, emergency_src = \
                    model_catalog.topup_to_market_top(section, [], cfg=None, remote=None)

                if emergency:
                    models = emergency
                    model_src = f"emergency-fallback ({emergency_src})"
                    logger.warning(
                        f"  → 应急回退成功：{len(emergency)} 个模型从 {emergency_src}")
                else:
                    # 最后防线：直接取 FALLBACK_MODELS
                    from .model_catalog import FALLBACK_MODELS
                    hardcoded = FALLBACK_MODELS.get(section, [])
                    if hardcoded:
                        models = list(hardcoded)
                        model_src = "hardcoded-fallback"
                        logger.error(
                            f"  → 应急回退也空，使用硬编码回退：{len(models)} 个模型")
                    else:
                        logger.critical(
                            f"  → 所有回退均失败，段 {section} 基址 {base[:40]} "
                            f"无任何模型可用！将生成空模型条目。")
                        # 不抛异常，让调用方决定如何处理空条目
            catalog_stale, stale_why = False, ""
        provenance = {
            m: ("verified" if v.usable and m in v.models else "inferred")
            for m in models
        }
        inferred = [m for m, source in provenance.items() if source == "inferred"]
        if inferred:
            model_warns.append(
                f"最高代选择中有 {len(inferred)} 个模型未经本次推理验证"
                f"（{'、'.join(inferred[:4])}"
                f"{'…' if len(inferred) > 4 else ''}）；"
                "目录、原配置及补齐项均为 inferred，不代表探测成功")
            # 这里**不**下调 model_source（2026-09-12 修）
            # ------------------------------------------------
            # 原来只要有 inferred 就把 probed 改成 catalog/seed。而上面的
            # 补齐逻辑对任何非空清单都会加同档变体 —— inferred 几乎必然非空，
            # 于是**每一个实测成功的段**都被降级。降级后
            # `SectionPlan.recommended` 第二条判据 `model_source != "probed"`
            # 直接 False，界面默认一个都不勾、`for_write` 为 0、写回没有 diff：
            # 探测跑完却什么都写不进去。
            #
            # 而用户对补齐的要求原文是「如果检测出来没有高级模型按该系列该
            # 类型模型的最高级进行**填充勾选**」—— 补齐项就是要勾上的。
            #
            # 依据强度并没有丢：逐模型的 verified / inferred 记在
            # `model_provenance` 里，经 server.py 带到界面；上面那条警告把
            # 名字也列出来。model_source 说的是「这一段这次有没有实测依据」，
            # 段真的探通了就是 probed，补齐几个同族同档变体不改变这件事。

        score = score_verdict(v)

        # 「站方只认特定客户端」的处置 —— 写 cloak / fingerprint-profile，
        # **不是**塞一个 TLS 代理（2026-09-13 推翻上一版做法）
        # ------------------------------------------------------------------
        # 上一版（P0-4，2026-09-12）在这里按域名黑名单注入
        # `proxy-url: http://127.0.0.1:8443`，指向 nginx 的「TLS 指纹代理」。
        # 逐条核对 CPA 源码后确认那条路**从协议层就不通**，三处独立错误：
        #
        #   1 CPA 的 proxy-url 走 http.Transport.Proxy，对 https 上游发的是
        #     **CONNECT**；nginx 那个块是普通 HTTP 反代（proxy_pass
        #     $scheme://$http_host$request_uri），没有 proxy_connect 模块，
        #     不认 CONNECT。
        #   2 nginx 监听在**宿主机** 127.0.0.1:8443，而 CPA 在容器里 ——
        #     容器内 127.0.0.1 是它自己的 lo，到不了宿主机。
        #   3 listen 是明文 8443，$scheme 恒为 http，转发出去也不是 https。
        #
        #   旁证：生产 config.yaml 里 proxy-url 指向 8443 的条目数为 0 ——
        #   这条路从没真正落过盘。
        #
        # 更要紧的是**设 proxy-url 会让指纹变得更糟**。CPA 自带 Claude Code
        # 的真实 TLS 指纹（helps/utls_client.go:164 claudeCodeTLSClientHelloSpec，
        # 逐字节复刻 Claude Code 2.1.220 的 ClientHello，连 header 顺序都对齐），
        # 但 NewUtlsHTTPClient 一旦拿到 proxyURL 就把 standardTransport 换成
        # buildProxyTransport，utls 那条通道被整个绕开。
        #
        # 而 TLS 那一层本项目**根本触及不到**：utls 只对
        # IsAnthropicUpstreamURL（claude_upstream.go:12 硬性要求 hostname ==
        # api.anthropic.com）生效，第三方聚合站一律走 Go 默认指纹，
        # 这是 CPA 的判定、CPA 不可改。
        #
        # 能做的是**请求体与头**那一层，而它正是 503 的真实成因：
        # sub2api 的 claude_code_only 校验四项（claude_code_validator.go）——
        # UA 匹配 claude-cli/x.y.z、system prompt 与模板 Dice 相似、
        # anthropic-beta 头、metadata.user_id 格式。CPA 对应的开关就是每 key 的
        # cloak.mode 与 fingerprint-profile，见下面 identity 那一段（:2176）。
        #
        # 所以这里不再动 proxy：v.need_proxy 那条链（:2107）已经按**实测**
        # 选出可达代理，用探测事实覆盖它只会把可用的配置改坏。

        # P0-3: 提取历史 max-context-length 值（2026-09-12）
        # -------------------------------------------------------
        # 为什么要单独一份（2026-09-03 逐字段对账发现）：这个值在 `models:` 块里，
        # 而 extract_carry_lines 有意跳过整个 models 块（清单由方案重新生成）。
        # 于是它落进空档 —— carry 不搬，方案只带本次实测的那**一个**
        # （max_context_length + context_model）。本次没探上下文时，历史实测值
        # 全部消失。实测生产配置 8 处，kilo.example 的 987500 就在其中。
        #
        # 优先级：本次实测（context_model 那一个）> 原值搬运 > 不写。
        # 见 render_entry 的 model_lines。
        prior_ctx = extract_prior_context(cfg, section, base, row.api_key)
        pri, reason = suggest_priority(band, score, models=models,
                                       probation=probation)

        # 新条目的 request-scoped-errors（2026-09-13）
        # ------------------------------------------
        # 只对**本段既有条目**学一次策略；既有条目走 carry_lines 逐字保真，
        # render_entry 会跳过这一份（判 carry 里有没有）。
        #
        # 为什么在这里算而不是 render_entry 里：那个函数拿不到 cfg。
        # 学不到就是空，什么都不写 —— 绝不内置一份规则表。
        #
        # 存**解析后的值**而不是原文行：缩进只有 render_entry 知道
        # （它的 field 参数按段与调用路径变，compat 段与 key 类段不同层）。
        # 在这里定死缩进会渲染出错位的 YAML —— 实测 compat 段直接语法错误。
        try:
            from .writeback import learn_scoped_error_rules
            scoped_rules = learn_scoped_error_rules(cfg, section)
        except Exception:
            scoped_rules = []

        # 原条目被停用、而本轮**探测判定可用** —— 要把它放回调度池
        # （用户 2026-09-16：「无论原来是否被关闭，如果探测可用就要打开」）。
        #
        # 判据必须两半都成立：原条目确实带停用标记 **且** 本轮探通了。
        # 只看前半句会把一个仍然不可用的站错误地打开；只看后半句则永远
        # 触不到这个分支（没被停用过就没什么可清的）。
        #
        # 用 `v.usable` 而不是「清单非空」：`v.usable` 是「端点响应正常、
        # 凭证有效」的实测结论，而清单可能为空（静默换模 / 200 包错误体）。
        # 用户的口径是「探测可用」，那正是 v.usable 的定义。
        reenable = []
        if v.usable:
            try:
                from .writeback import _original_entry as _orig
                _old = _orig(cfg, SectionPlan(section=section, base_url=base,
                                              api_key=row.api_key))
            except Exception:
                _old = {}
            if _old:
                reenable = reenable_targets(section, _old)

        sp = SectionPlan(
            scoped_error_rules=scoped_rules,
            cloak_mode=cloak_mode,
            fingerprint_profile=fp_profile,
            rebuild_mid_system=getattr(v, "rebuild_mid_system", None),
            disable_cooling=_cooling_override(v),
            reenable_fields=reenable,
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
            prior_context=prior_ctx,
            context_model=v.context_model,
            # 段专属能力开关的实测结论。三态原样带过来 —— False（实测不支持）
            # 与 None（未探测）在写回时行为相同，但界面措辞必须分开。
            websockets=getattr(v, "websockets", None),
            websockets_note=getattr(v, "websockets_note", ""),
            prompt_cache_key=getattr(v, "prompt_cache_key", None),
            prompt_cache_note=getattr(v, "prompt_cache_note", ""),
            score=score,
            model_source=model_source,
            catalog_stale=catalog_stale,
            catalog_stale_why=stale_why,
            highest_models=list(models),
            model_provenance=provenance,
        )
        if model_warns:
            sp.warnings.extend(model_warns)

        # codex 段必须包含 originator（2026-09-06）
        # --------------------------------------------
        # zulu 等站方限制「仅 Codex 官方客户端可调用」。CPA 转发 codex
        # 请求时，条目 headers 里的 originator 必须原样传到上游，否则 403。
        # 判死的 codex 段会回落标准档（codex-tui），_fallback_headers 逻辑
        # 已保证回落后的 headers 包含 originator。但若后续写回路径（CPAMP
        # 前端保存、或 CPA executor 转发）有 bug，这条门禁能提前抓到。
        # 判据必须**大小写不敏感**（2026-09-10 修）：原来是
        # `"originator" not in headers` 的精确匹配，而 headers 的键保留的是
        # 实测原写法 —— `v.min_headers` 里是 `Originator`（大写 O，抄的是
        # 真实客户端形态），于是一个**明明带了** originator 的条目被判成缺失。
        # HTTP 头名本身大小写不敏感，`merge_entry_headers` 也是「大小写不敏感
        # 但保留原写法」，这里跟它同口径。
        if section == "codex-api-key" and "originator" not in {
                h.lower() for h in headers.keys()}:
            raise ValueError(
                f"codex 段条目缺少 originator 头。base={base}, "
                f"headers={list(headers.keys())}")

        # 全量重探不判重：输入就是既有条目，撞上是必然而非异常。
        # 见 docstring 里 rebuild 那一节。
        if rebuild:
            pass
        elif fp in existing.get(section, set()):
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
            # 实测踩到：foxtrot 某 Key 在 claude 段已存在（带 prefix: ANT），
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

        # 手填被规则丢弃的项 —— **不在 manual 分支里报**。
        #
        # 2026-09-02 自查发现：手填的全部不合规时 forced_models 变成空列表，
        # 于是走不到 manual 分支，这条警告永远不触发。用户手填了两个模型、
        # 一个都没写进去、界面上一句提示都没有，工具悄悄换成了自己那份清单 ——
        # 正是这条警告要防的那件事。
        #
        # 「全部不合规之后改用了什么」取决于探测结论，2026-09-03 起手填不再
        # 只在判死段生效，所以这句话不能写死成「市面最新清单」：可用段落回
        # probed（实测清单），判死段才落到 catalog / seed。
        if forced_dropped:
            fallback_note = ""
            if not forced_models:
                whence = {"probed": "本次实测到的清单",
                          "catalog": "站方目录",
                          "prior": "原 config.yaml 的清单",
                          "seed": "市面最新清单"}.get(model_source, "工具兜底清单")
                fallback_note = (f"。手填的全部不合规，已改用{whence} —— "
                                 "要指定别的模型请改成符合规则的名字")
            sp.warnings.append(
                f"手填的 {len(forced_dropped)} 个模型已丢弃"
                f"（{', '.join(forced_dropped)}）—— 这个段的协议接不了它们："
                "codex 段走 OpenAI Responses、claude 段走 Anthropic "
                "/v1/messages、gemini 段走 generateContent（且只收 *-pro 且"
                "版本 >= 2.5）；图像/语音/嵌入类模型四段都不收；"
                "另外同系列只保留最新版"
                + fallback_note)

        # 四族之外但被放行的手填项。工具自己不会挑它们（section_allows 拒），
        # 但操作员显式指定就照写 —— compat 段走 /chat/completions，CPA 对模型名
        # 零校验，能不能用只看上游。说清这一点，别让它看起来像工具推荐的。
        if forced_offfamily:
            sp.warnings.append(
                f"手填的 {', '.join(forced_offfamily)} 不在本工具的四族清单"
                "（gemini / gpt / claude / kimi）里 —— 已按你的指定写入。"
                "compat 段走 /chat/completions，CPA 侧对模型名零校验，"
                "能不能用完全取决于上游认不认；本工具不会主动推荐这类模型，"
                "也没有验证过它")

        if model_source == "manual":
            # 手填现在也可能发生在**探测通过**的段上（2026-09-03）：操作员把
            # 实测到的 4 个换成自己知道的 8 个。两种处境的措辞必须分开，
            # 说反了就是误导 —— 一个是「探测没通、你来定」，另一个是
            # 「探测通了、但你的清单覆盖了它」。
            if v.usable:
                sp.priority_reason = f"人工接管（覆盖实测清单）· {reason}"
                probed_note = (f"（实测到 {', '.join(v.models)}）"
                               if v.models else "（实测清单为空）")
                sp.warnings.append(
                    f"探测判「{v.category or '可用'}」{probed_note}，"
                    f"但已按你手填的清单写入：{', '.join(models)}。"
                    "工具没有验证过手填的这些模型能用")
            else:
                sp.priority_reason = (
                    f"人工接管（探测判「{v.category or '不可用'}」）· {reason}")
                sp.warnings.append(
                    f"探测未通过（{v.category or '不可用'} — {v.action or ''}），"
                    f"模型清单由你手工指定：{', '.join(models)}。"
                    "工具没有验证过这些模型能用")
        elif model_source == "catalog":
            # 两种处境，措辞要分开（2026-09-03）：
            #   · usable=False —— 推理没通过，目录是唯一线索
            #   · usable=True 且 v.models 为空 —— 端点通、凭证有效，但返回的
            #     模型对不上（静默换模 / 200 包错误体），`_accept` 全拒了。
            #     说「推理请求未通过」在这一种下是错的。
            if v.usable:
                sp.priority_reason = (
                    f"未验证（端点通但返回的模型对不上，清单取自目录）· {reason}")
                sp.warnings.append(
                    f"端点响应正常、凭证有效，但每次返回的模型都与请求不一致"
                    f"（静默换模或 200 包错误体），实测清单为空 —— "
                    f"已改用站方 /models 目录报的 {len(v.catalog)} 个里通过本段"
                    f"规则的前 {len(models)} 个：{', '.join(models)}。"
                    "这些名字是站方自己报的，比工具猜测可靠，但仍未经推理验证")
            else:
                sp.priority_reason = (
                    f"未验证（探测判「{v.category or '不可用'}」，模型取自目录）· {reason}")
                sp.warnings.append(
                    f"推理请求未通过（{v.category or '不可用'} — {v.action or ''}），"
                    f"但目录 GET 读到 {len(v.catalog)} 个模型，已取前 {len(models)} 个："
                    f"{', '.join(models)}。"
                    "站方目录只说明「声称有」，不等于这把 Key 的分组能用 —— "
                    "很多站禁止推理测活却确实可用，确知可用再勾")
            # 目录里一个四族的都没有、于是收下了四族之外的名字。
            # 这不是「工具推荐」，措辞必须说清为什么退这一步。
            kept_off = [m for m in models if m in catalog_offfamily]
            if kept_off:
                sp.warnings.append(
                    f"站方目录里没有本工具四族清单（gemini / gpt / claude / kimi）"
                    f"内的任何模型，已收下它自己报的 {', '.join(kept_off)}。"
                    "退这一步是因为另一个选项更糟：写工具猜的名字，而这个站"
                    "**从没报过**它们，CPA 路由过去大概率 404。"
                    "compat 段走 /chat/completions，CPA 对模型名零校验 —— "
                    "能不能用取决于上游认不认，本工具没有验证过")
        elif model_source == "prior":
            # 判死 + 目录读不到 + 原条目有清单 —— 沿用原清单而不是猜测。
            # 措辞要说清三件事：为什么没有本次依据、清单从哪来、风险是什么。
            sp.priority_reason = (
                f"未验证（探测判「{v.category or '不可用'}」，"
                f"沿用原清单）· {reason}")
            sp.warnings.append(
                f"探测未通过（{v.category or '不可用'} — {v.action or ''}）"
                f"，且站方 /models 目录读不到 —— 模型清单**沿用原 "
                f"config.yaml 里这个条目已有的**：{', '.join(models)}。"
                "没有拿本工具猜的「市面最新」覆盖它：原清单是先前一轮实测"
                "沉淀下来的，比猜测硬。但本次没验过，这些模型现在是否还能用"
                "未知 —— 要换清单请在右侧手填")
        elif model_source == "seed":
            # 走到这里有两种处境，措辞要分开 —— 说错一种就是误导：
            #   · v.usable=False：探测没通过（判死/门禁/限频…），目录也读不到
            #   · v.usable=True ：端点通、凭证有效，但返回的模型对不上
            #     （静默换模，或 200 包错误体），`_accept` 把它们全拒了
            # 第二种在截图里表现为「可用 + 实测 + 无可信模型」，那三个标记
            # 并存看着自相矛盾，必须一句话讲清是怎么回事。
            why = (f"探测未通过（{v.category or '不可用'}"
                   f" — {v.action or ''}），且站方 /models 目录读不到"
                   if not v.usable else
                   "端点响应正常、凭证有效，但每次返回的模型都与请求不一致"
                   "（静默换模或 200 包错误体），实测到的模型清单为空")
            sp.priority_reason = (
                f"未验证（{'探测判「%s」' % (v.category or '不可用') if not v.usable else '端点通但模型对不上'}"
                f"，用市面最新清单）· {reason}")
            sp.warnings.append(
                f"{why} —— 模型清单取自「当前市面最新」"
                f"（{model_src or '内置兜底'}）：{', '.join(models)}。"
                "这批名字没有实测依据，但已按本段规则过滤并取同产品线最高世代。"
                "确知该站卖什么模型的话，用右侧输入框改成真实清单")

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
            sp.warnings.append(_shadow_warning(band, sp.models, sp.priority, shadow))
        # codex 段 WS 请求的跨档说明（2026-09-05）。
        #
        # 无条件加（不只在 shadow 非空时）：上面那些「挡住谁」的结论建立在
        # priority 硬隔离上，而 WS 请求不受它约束。用户按档位隔离的心智模型
        # 去读影响面，WS 请求的实际去向与那个模型不符 —— 这句话就是补上
        # 那一层。见 ws_crosstier_note。
        ws_note = ws_crosstier_note(band, sp.priority,
                                    v.websockets is True)
        if ws_note:
            sp.warnings.append(ws_note)
        # session-affinity 的跨档说明（2026-09-05）。与上面那条平行 ——
        # 那条讲 codex 段的 WS 请求去哪个档，这条讲所有段的已绑定会话不换档。
        # 见 affinity_crosstier_note。
        aff_note = affinity_crosstier_note(cfg, band, sp.priority)
        if aff_note:
            sp.warnings.append(aff_note)
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


# ---------------- 批量定档 ----------------


# 「这一段这次有没有依据」的三档。与 score 不同：score 说的是探测质量，
# 这个说的是清单从哪来。
#   0  probed         本次实测跑通推理
#   1  manual/catalog 手填 / 站方目录声称有
#   2  seed           工具猜测，零依据
# prior 与 catalog 同档（1）：原清单是先前一轮的实测沉淀，比工具猜测（2）硬，
# 但不是**本次**实测（0）。2026-09-06 加。
_EVID = {"probed": 0, "manual": 1, "catalog": 1, "prior": 1, "seed": 2}


def _evid(sps: list[SectionPlan]) -> int:
    """组内最强的那一把 —— 同站多 Key 只要有一把实测通了，这个站就是有依据的。"""
    return min(_EVID.get(x.model_source, 2) for x in sps)


def existing_host_tiers(band: Band) -> tuple[dict[str, int], dict[str, list[int]]]:
    """本段里**每个站已经占着哪一档**，以及哪些站在原文件里就已经被拆开了。

    返回 ({host: 档位}, {host: [多个档位]})。第二个字典只收原文件里就有多个
    priority 的站 —— 那是先前留下的状态，不是本次造成的，但会让「同站同档」
    无从判断，所以要报出来。

    取最高那一档作为锚：CPA 的层级隔离只取最高可用桶
    （selector.go:527-553 availableAuthsFromPriorityBuckets 只收 bestPriority；
    scheduler.go:1229-1231 priorityOrder 降序），低档那批实际不参与首选。
    """
    per: dict[str, list[int]] = {}
    for pri, hosts in band.hosts_at.items():
        for h in hosts:
            hl = (h or "").lower()
            if hl:
                per.setdefault(hl, []).append(pri)
    anchor = {h: max(v) for h, v in per.items()}
    split = {h: sorted(v, reverse=True) for h, v in per.items() if len(set(v)) > 1}
    return anchor, split


def _hijacks_at(band: Band, models: list[str], priority: int,
                host: str) -> list[str]:
    """在 priority 上注册这些模型，会抢走哪些**别人**的顶层。

    与 `compute_impact(...).hijacks` 的差别：这个站自己已经承载该模型顶层时
    不算劫持 —— 它本来就在那一档上，同站另一把 Key 并进来不改变任何归属。
    """
    out: list[str] = []
    hl = (host or "").lower()
    for imp in compute_impact(band, models, priority):
        if not imp.hijacks:
            continue
        carriers = (band.model_tiers.get(imp.model) or {}).get(imp.current_top) or []
        if hl in {(c or "").lower() for c in carriers}:
            continue                    # 顶层就是自己，不算抢
        out.append(imp.model)
    return out


def assign_priorities(plans: list[ImportPlan], cfg: dict, *,
                      probation: bool = True, raw: str = "") -> list[str]:
    """给一批方案统一定档：**站与站之间不同值，同站所有 Key 同值**。

    为什么必须批量做（2026-09-02 现场）
    ---------------------------------
    `suggest_priority` 每次只看「当前 config.yaml 有哪些空档」，79 个凭据串行
    调用它，每个都问同一个问题、拿到同一个答案 —— 落盘后 claude 段 74 个条目
    全是 175，gemini 段 76 个全是 225。站与站之间毫无区分，而 priority 的**唯一
    作用**就是区分先后。

    为什么同站同值（用户 2026-09-02 确认的 A 方案）
    -------------------------------------------
    原始 config.yaml 就是这个规律，三段无一例外：

        kilo     5 个 Key   priority 1000
        tango  14 个 Key   priority  990
        gorou   15 个 Key   priority  985

    这与 CPA 的调度语义一致：`priority` 决定「哪一层先被尝试」，同层内部按
    `weight` 轮询（selector.go:539-549 只取最高那一桶）。同站多 Key 指向同一个
    上游、能力相同，本该在同一层；给它们不同值会让第 2 把 Key 只在第 1 把不可用
    时才被尝试 —— 把「多 Key 轮询」变成「主备切换」，白费配额。

    分配办法（2026-09-02 二轮修正）
    ---------------------------
    「把各站分开」是本函数的**唯一**职责，安全边界仍由 `suggest_priority` 划：
    先为每个站算出它的**上限** `cap`（那里已实现三条硬约束：不动现有值、
    不劫持顶层、试用期进最低可插档，且 test_tiering 的 180 项守着它），
    再按分数降序逐站取 `min(cap, 上一站的值 - 1)`，跳过与现有档位相撞的值。

    第一版直接从空档由高到低铺值，绕开了那三条约束。拿生产 config.yaml
    实测的后果（14 站 × 3 Key）：

        codex    14/14 站抢走 gpt-5.5 等模型的顶层（cap 是 550，实发 787..618）
        compat   14/14 站抢顶层（cap 520，实发 549..536）
        claude   最高档挡住 5 个在用站（试用期本该只挡 0 个）

    抢顶层会让 `recommended` 整段翻假（劫持是不建议勾选的四个条件之一），
    于是 `selected=None` 时的默认写入集合从 24 段塌到 12 段 —— 界面上表现为
    「codex 与 compat 两段默认一个都不勾」。

    同站同值不受影响：值按站分配，站内所有 Key 复制同一个。

    raw 是 config.yaml 原文。**必须传** —— `build_band` 只在拿到原文时才解析
    注释里的「实测不可用」结论，而那个结论直接决定「挡住下层算不算代价」。
    实测差距（生产 config.yaml，满分候选）：claude 段 175 → 500、gemini 段
    225 → 280，另两段不变。不传不会报错，只是把可用新站压到一堆死站后面。

    返回 warnings（哪些段挤到了现有档位之下、哪些段排不下）。
    """
    import logging
    logger = logging.getLogger(__name__)

    warns: list[str] = []

    # 1. 按段 → 站 归集。站的身份用 host —— 同站不同段的 base-url 形态不同。
    per_section: dict[str, dict[str, list[SectionPlan]]] = {}
    for plan in plans:
        for sec, sp in plan.sections.items():
            if not sp.writable:
                continue
            host = host_of(sp.base_url)
            per_section.setdefault(sec, {}).setdefault(host, []).append(sp)

    for section, by_host in per_section.items():
        band = build_band(cfg, section, raw=raw)
        taken = set(band.tiers)     # 不与现有档位相撞：撞上等于与那个站同层轮询

        # 1b. 已在本段占着档位的站 —— **留在原档，不重新分配**（2026-09-04）。
        #
        # 为什么必须区分「已有站」与「新站」（现场截图）
        # ------------------------------------------
        # 原来这里把每个站都当新站处理：算 cap、按分数排、逐站取
        # `min(cap, 上一站 - 1)`。对真正的新站是对的，对**重探既有站**是错的：
        #
        #   · 落盘后同站被拆成两层。留守条目（没勾 / 判不可写 / 探测异常）
        #     由 _orphan_entry_lines 原样搬回旧值，被重探的那几把拿新值。
        #     实测那次：kilo claude 3 把→164 + 2 把留在 372；
        #     tango claude 9 把→167 + 5 把留在 371。
        #   · 即使全勾，整份配置的站间次序也被推平重排：`taken` 里塞着这些站
        #     自己的旧档，于是每个站都躲开自己原来的值往下掉。实测 claude 段
        #     12 个站从 1000/995/990/985/700/650/630/600/400/350/300/50
        #     变成 500..489 一片连号。
        #
        # `priority` 的语义是「哪一层先被尝试」（层级隔离，
        # selector.go:527-553 只取最高可用桶）。既有站的档位是先前一轮定下的
        # 站间次序，重探一次不构成改它的依据 —— 重探验证的是「这把 Key 还能
        # 不能用」，不是「这个站该排第几」。
        #
        # 唯一的例外由下面的 `_hijacks_at` 兜：留在原档会抢走别人顶层时才动它
        # （成因是本次给它注册了原来没有的模型），那时按新站流程重新定档。
        anchor, pre_split = existing_host_tiers(band)
        if pre_split:
            for h, vals in sorted(pre_split.items()):
                warns.append(
                    f"段 {section}：{h} 在原 config.yaml 里就占着 {len(set(vals))} 个"
                    f"档位（{'、'.join(str(v) for v in vals)}）—— 同站多 Key 本该同层，"
                    f"这是本次之前留下的状态。已按最高档 {max(vals)} 对齐")

        pinned: dict[str, int] = {}     # host -> 沿用的原档
        fresh: list[tuple[str, list[SectionPlan]]] = []
        for host, sps in by_host.items():
            hl = (host or "").lower()
            keep = anchor.get(hl)
            if keep is None:
                fresh.append((host, sps))
                continue
            union: list[str] = []
            for sp in sps:
                for m in sp.models:
                    if m not in union:
                        union.append(m)
            grabbed = _hijacks_at(band, union, keep, hl)
            if grabbed:
                # 留在原档会抢走别人的顶层 —— 成因只有一个：本次给这个站注册了
                # 它原来没有的模型，而那个模型的现有顶层比这个站的档位低。
                # 那时不能沿用原档，走新站流程让 suggest_priority 重新划线。
                names = "、".join(grabbed[:3])
                warns.append(
                    f"段 {section}：{host} 沿用原档 {keep} 会抢走 {len(grabbed)} 个"
                    f"模型的顶层（{names}）—— 本次给它加了原来没有的模型，"
                    f"已按新站流程重新定档")
                fresh.append((host, sps))
                continue
            pinned[hl] = keep

        for host, sps in by_host.items():
            hl = (host or "").lower()
            if hl not in pinned:
                continue
            keep = pinned[hl]
            for sp in sps:
                sp.priority = keep
                sp.priority_reason = (
                    f"沿用该站在本段的原档 {keep}"
                    f"（同站 {len(sps)} 个 Key 共用此档）—— 重探验证的是这把 Key "
                    f"还能不能用，不改站间次序")
                if sp.model_source != "probed" and _evid(sps) == 0:
                    sp.priority_reason += (
                        f"；本 Key 的清单来自"
                        f"{_SRC_LABEL_CN.get(sp.model_source, sp.model_source)}")
                sp.impacts = compute_impact(band, sp.models, keep)
                sp.warnings = [
                    w for w in sp.warnings
                    if "抢走" not in w and "挡在其后" not in w
                    and "排在 " not in w]
                # 挡站警告要按沿用后的值重新加（2026-09-04 自查）。
                #
                # 沿用原档不等于「影响面为零」：本次可能给这个站注册了它原来
                # 没有的模型，而那个模型在更低的档上有承载站。那时这个站就挡在
                # 了它们前面 —— 档位没变，但**这个模型的格局变了**。
                # 只清警告不重加，界面上就完全看不到这件事。
                #
                # 抢顶层那一支不在这里：`_hijacks_at` 已经把会抢的挪进 fresh，
                # 走到这里的必然不抢，所以只补挡站那一条。
                shadow: dict[str, list[str]] = {}
                for imp in sp.impacts:
                    for h2 in imp.shadowed_hosts:
                        shadow.setdefault(h2, []).append(imp.model)
                if shadow:
                    sp.warnings.append(
                        _shadow_warning(band, sp.models, keep, shadow,
                                        pinned=True))

        # 新站才进下面的分配流程。已有站的档位已被 `taken` 覆盖，不会被撞上。
        by_host = dict(fresh)
        if not by_host:
            continue

        # 2. 站级排序：优先基于 CPA 运行时健康分数，回退到检测分数。
        #
        # 新设计（2026-09-12）：从 CPA 实际运行状态智能分配优先级
        # ---------------------------------------------------------
        # 查询 CPA 管理接口 /v0/management/api-key-usage 获取运行时状态：
        #   - Success/Failed 计数 → 可调度比例 (60% 权重)
        #   - RecentRequests 桶活跃度 → 活跃比例 (40% 权重)
        #   - 健康分数 = schedulable_ratio×0.6 + active_ratio×0.4
        #
        # 如果 CPA 未运行或接口不可达，回退到检测结果预测：
        #   - 检测成功率 40%、响应时间 20%、模型覆盖度 20%
        #   - 上下文窗口 10%、历史优先级 10%
        #
        # 排序键：(实测依据档次, -健康分数, 主机名)
        # - 实测依据档次用 _evid() 保持（防止探测全灭的站抢顶层）
        # - 健康分数替换原来的"组内最高分"（从静态检测分转为动态运行状态）
        # - 主机名保持稳定性（同输入同输出）

        # runtime_health 现在只用标准库（2026-09-13 把 requests 换成 urllib）。
        #
        # 为什么这件事要紧：它原来顶层 `import requests`，而本项目自述「零第三方
        # 依赖」、deploy/Dockerfile 只装 PyYAML 与 bcrypt —— 于是容器里这个
        # import 必然失败，下面的 except 把它咽掉、退回静态检测分。
        # 结果是「按 CPA 实际运行状态定优先级」这个特性**在生产环境从未生效
        # 过一次**，日志只有一行 debug。
        #
        # try/except 保留：模块级语法/属性错误仍该降级而不是让整轮定档崩掉。
        try:
            from .runtime_health import (
                fetch_cpa_runtime_health,
                get_domain_health_scores,
            )
            runtime_health_available = True
        except ImportError as exc:
            runtime_health_available = False
            # 用 warning 而不是 debug：这条一旦出现就是「特性静默失效」，
            # 而 debug 级别在生产日志里看不见 —— 正是它藏了这么久的原因。
            logger.warning(f"runtime_health 不可用（{exc}），本轮回退到静态检测分")

        # 尝试查询 CPA 运行时状态（默认端口 8317，从 config.yaml 读取）
        domain_health = {}
        if runtime_health_available:
            # 容器里必须用 CPA_UPSTREAM_URL（compose 已注入服务名）；
            # 只有本地同机跑才退到 config 的 port。见 _cpa_base_url 的说明。
            cpa_base_url = _cpa_base_url(cfg) or None
            if not cpa_base_url:
                logger.warning(
                    f"段 {section}：未配置 CPA 地址（CPA_UPSTREAM_URL 为空且 "
                    f"config 无 port），本轮按检测结果预测健康分")

            runtime_health = fetch_cpa_runtime_health(cpa_base_url)

            if runtime_health:
                logger.info(f"段 {section}：已获取 CPA 运行时健康数据，将基于实际运行状态分配优先级")
                # 计算每个域名的健康分数
                domain_health = get_domain_health_scores(
                    [sp for sps in by_host.values() for sp in sps],
                    runtime_health,
                    cfg,
                    section
                )
            else:
                logger.info(f"段 {section}：CPA 运行时数据不可用，将基于检测结果预测健康分数")

        # 每个站的**最终得分**只算一次，排序与定档共用同一个值。
        #
        # 2026-09-15：原来排序用融合后的值、定档用纯检测分，两者不一致 ——
        # 一个站可能因为运行期健康分高而被排到前面，却拿着按低检测分算的
        # 档位上限，于是「排在前面的站档位反而更低」，位次与档位自相矛盾。
        def _final_score(host: str, sps: list) -> int:
            static_best = max(x.score for x in sps)
            health = domain_health.get(host) if domain_health else None
            return _blended_score(static_best, health)

        # 构建排序键：(实测依据档次, -最终得分, 主机名)
        # evid 保持在第一顺位 —— 探测全灭的站不能仅因历史数据好看而抢顶层
        # （用户 2026-08-30 定的，见 _EVID 的说明）。
        def _sort_key(kv):
            host, sps = kv
            return (_evid(sps), -_final_score(host, sps), host)

        ranked = sorted(by_host.items(), key=_sort_key)

        # 3. 每个站的上限：走 suggest_priority。安全边界只在那里定义 ——
        #    不劫持顶层、不挡在用站、试用期不越过得分支持的上限，三条都在
        #    那个函数里，且 test_tiering 的 180 项守着。这里只负责「把各站
        #    分开」，绝不自己重新推导安全值。
        #
        #    models 用**组内并集**：同站不同 Key 声明的模型可能不同（有的 Key
        #    只开了部分模型），而值是站级共用的 —— 按并集算上限才不会让某把
        #    Key 的模型被悄悄抬到它自己的顶层之上。
        caps: list[tuple[str, list[SectionPlan], int, int]] = []
        for host, sps in ranked:
            union: list[str] = []
            for sp in sps:
                for m in sp.models:
                    if m not in union:
                        union.append(m)
            # 与 _sort_key 同一个值 —— 健康分在这里真正参与定档，
            # 不再只影响先后顺序。
            best = _final_score(host, sps)
            cap, _reason = suggest_priority(
                band, best, models=union, probation=probation)
            caps.append((host, sps, max(int(cap), 1), best))

        # 3b. 空档太窄时往下找更宽的空档（用户 2026-09-02 要求）。
        #
        # 为什么需要：cap 落在哪个空档由 suggest_priority 按「代价最小的最高档」
        # 选，它不知道本批有多少个站要排。claude 段的现有档位谱在高位极密
        # （1000/995/990/985 相邻只差 5），14 个站挤进去就成了 999/998/997…
        # —— 正确但手工微调的余地几乎没有，改一个值就会撞上邻居。
        #
        # 代价约束不能松：只接受「挡住的在用站数**不多于** cap 处」的空档。
        # 实测（生产 config.yaml，满分候选）符合这条的更宽空档：
        #   claude  cap=500(挡0) → gap(50,300) room=249 挡0
        #   gemini  cap=280(挡0) → gap(200,250) room=49  挡0
        #   codex   cap=425(挡1) → gap(10,300)  room=289 挡1
        # compat 段 cap=45 已在最低空档，没有更宽的可换 —— 那时保持原样。
        #
        # 代价是新站整体排得更低。这是用户在 2026-09-02 明确选的取舍：
        # 「宁可低一点，也要留出手工调整的空间」。
        need = len(caps)
        if need > 1:
            top_cap = max(c for _h, _s, c, _b in caps)
            room_at_cap = 0
            for lo, hi in band.gaps():
                if lo < top_cap < hi:
                    room_at_cap = hi - lo - 1
                    break
            if room_at_cap and room_at_cap < need * 2:
                # 各站模型的并集 —— 换档影响的是整批，代价要按整批算
                all_models: list[str] = []
                for _h, sps, _c, _b in caps:
                    for sp in sps:
                        if sp.models and sp.models[0] not in all_models:
                            all_models.extend(
                                m for m in sp.models if m not in all_models)
                cost_at_cap = _shadow_count(band, all_models, top_cap)
                for lo, hi in band.gaps():          # 已降序
                    if hi > top_cap:
                        continue
                    room = hi - lo - 1
                    if room < need * 2 or room <= room_at_cap:
                        continue
                    mid = (lo + hi) // 2
                    if _shadow_count(band, all_models, mid) > cost_at_cap:
                        continue                    # 代价变大，不换
                    # 换：所有站的上限压到这个空档的上界之下
                    ceil_here = hi - 1
                    caps = [(h, s, min(c, ceil_here), b) for h, s, c, b in caps]
                    warns.append(
                        f"段 {section}：{need} 个站排不进 {top_cap} 所在的空档"
                        f"（只容 {room_at_cap} 个整数），已整批下移到 "
                        f"{lo}↔{hi}（容 {room} 个，挡住的在用站数不变）—— "
                        f"档位更低但留出了手工微调的空间")
                    break

        # 4. 逐站取值：min(自己的上限, 上一站 - 1)，且跳过现有档位。
        #    单调递减保证「分数高的站不会排在分数低的站之后」，而 cap 保证
        #    没有任何站越过 suggest_priority 划的线。
        #
        #    「比上一站低 1」是**正常结果**，不是退化 —— 同段各站必须取不同值。
        #    只有两种情形值得报出来：
        #      · 掉出了 cap 所在的那个空档 —— 那意味着这个站越过了某个现有档位，
        #        它与那批现有站的先后关系变了（不是只在新站之间变）
        #      · 压到 1 还排不下 —— 那时站与站真的分不开了
        def _floor_of(value: int) -> int:
            """value 所在空档的下界。不在任何空档里就返回 0（无下界可越）。"""
            for lo, hi in band.gaps():
                if lo < value < hi:
                    return lo
            return 0

        dropped: list[str] = []     # 掉出自己空档的站
        floor_hit = 0               # 压到 1 还排不下的站数
        prev: int | None = None
        for idx, (host, sps, cap, best) in enumerate(caps):
            v = cap if prev is None else min(cap, prev - 1)
            while v >= 1 and v in taken:            # 撞现有档位就再降一格
                v -= 1
            if v < 1:
                v = 1
                floor_hit += 1
            if v <= _floor_of(cap):
                dropped.append(host)
            taken.add(v)
            prev = v

            for sp in sps:
                sp.priority = v
                # 理由会原样落进 config.yaml 的行尾注释（render_entry），所以
                # 不重复段名 —— 那个条目本来就在那一段里面。
                note = (f"批量定档第 {idx + 1}/{len(caps)} 站"
                        f"（组内最高分 {best}，"
                        f"同站 {len(sps)} 个 Key 共用此档）")
                if v < cap:
                    note += f"；算法上限 {cap}，为与前一站分开降到 {v}"
                # 组内证据不一致时点出来（2026-09-03 真实探测发现）：
                # 同站多 Key 共用一个档是对的（同一个上游、能力相同），但
                # 「这一档由谁的实测撑起来」得说清 —— golf 的 claude 段
                # 7 把 Key 里 6 把实测通过、1 把余额耗尽走了种子猜测，那一把
                # 因此拿到与实测同档的 494，并把 3 个它自己都没验过的模型
                # （claude-fable-5-1 / mythos-preview / 4.5-haiku）顶上了顶层。
                # 档位不改 —— 改了就违反「同站同档」；但要让操作员看见。
                if sp.model_source != "probed" and _evid(sps) == 0:
                    note += (f"；本 Key 的清单来自"
                             f"{_SRC_LABEL_CN.get(sp.model_source, sp.model_source)}"
                             f"，同档是同站其他 Key 的实测撑起来的")
                sp.priority_reason = note
                # 影响面要按新值重算 —— 旧值算出来的 impacts 会误导。
                sp.impacts = compute_impact(band, sp.models, v)
                # 劫持警告由 build_plan 按旧值加过，这里换了值必须**先清后加**，
                # 否则界面上会留一条指向旧 priority 的陈述。同理，挡站那条
                # 警告里写着具体数值，也要按新值重写。
                sp.warnings = [
                    w for w in sp.warnings
                    if "抢走" not in w and "挡在其后" not in w
                    and "排在 " not in w]
                if sp.hijacked:
                    names = ", ".join(i.model for i in sp.hijacked[:4])
                    sp.warnings.append(
                        f"会抢走 {len(sp.hijacked)} 个模型的顶层（{names}）——"
                        "层级隔离下现有顶层站将完全不被尝试")
                else:
                    shadow: dict[str, list[str]] = {}
                    for imp in sp.impacts:
                        for h in imp.shadowed_hosts:
                            shadow.setdefault(h, []).append(imp.model)
                    if shadow:
                        sp.warnings.append(
                            _shadow_warning(band, sp.models, v, shadow))

        if dropped:
            head = "、".join(dropped[:4]) + ("…" if len(dropped) > 4 else "")
            warns.append(
                f"段 {section}：{len(dropped)}/{len(caps)} 个站排不进算法给的空档，"
                f"已越过下一个现有档位（{head}）—— 它们与那批现有站的先后关系"
                f"随之改变，请复核这几站的 priority")
        if floor_hit:
            warns.append(
                f"段 {section}：{floor_hit} 个站已压到最低值 1，"
                f"再往下无可用整数 —— 这些站会与已取 1 的站同层轮询")

    return warns


def priority_collisions(plans: list[ImportPlan]) -> list[str]:
    """本批里有哪些站在同一段拿到了相同的 priority。

    为什么要单独一个函数（2026-09-02）：`assign_priorities` 保证站与站不同，
    但**用户覆盖在它之后应用** —— 手工把 A 站改成 B 站的值，两站就同层了。
    这不是错误（同层按 weight 轮询是合法配置），但它取消的正是用户这轮要的
    「不同网站不同优先级」，所以必须说出来而不是默默照写。

    只看同一段内部：跨段同值毫无关系，各段的档位谱独立。
    """
    out: list[str] = []
    for section in SECTIONS:
        at: dict[int, list[str]] = {}
        for plan in plans:
            sp = plan.sections.get(section)
            if sp is None or not sp.writable:
                continue
            host = host_of(sp.base_url)
            names = at.setdefault(sp.priority, [])
            if host not in names:
                names.append(host)
        for pri, hosts in sorted(at.items(), reverse=True):
            if len(hosts) > 1:
                out.append(
                    f"段 {section}：{len(hosts)} 个站共用 priority {pri}"
                    f"（{'、'.join(sorted(hosts))}）—— 它们会在同一层按 weight "
                    f"轮询，而不是分先后。手工改过 priority 的话这是预期结果")
    return out


def priority_split_within_host(plans: list[ImportPlan]) -> list[str]:
    """同一网址跨协议、跨 Key 的条目拿到不同 priority：阻断级警告。

    与 `priority_collisions` 正好相反的方向，而这个方向是**硬错误**，不是
    「可能是预期结果」（2026-09-10 加）。

    用户的硬要求：同一网址的上游，即使 Key 不同，priority 也必须相同。
    `assign_priorities` 本身守住了这条（按 `host_of(sp.base_url)` 分组，
    同 host 的所有 SectionPlan 复制同一个值，见 :2658-2668 / :2825-2826），
    但它之后还有两道会破坏它：
      · 用户覆盖按 `rid = row.line_no`（**每把 Key 一行**）应用
        （server.py:2401-2403 / 2246-2248 / web/app.js:1592），
        同站第 2 把 Key 一改就与第 1 把分层；
      · 全量重探沿用既有档位时，若原文件本来就分裂，会照样沿用。

    实测证据（2026-09-10 逐条对账两份生产配置）：
    桌面份（**本项目注入前**）40 组里 0 组分裂；fsdownload 份（**注入后**）
    3 组分裂 —— 也就是这些分裂是本项目自己写进去的：
      · codex  @romeo.example/v1  4 条 {350, 147}
        —— models / headers / proxy-url 三项**逐字相同**，唯 idx9 是 350。
           后果：350 那条被永远优先抽中并先烧完，另 3 把 Key 沦为冷备
           （147 档要等 155/154/153/150/149/148 全部冷却后才轮到）。
      · codex  @golf.example/v1    7 条 {348, 149}
      · gemini @romeo.example     3 条 {218, 215}

    2026-09-11：批准的同站规则覆盖所有协议段。这里只检查并报告，
    不合并路径、凭据、模型或请求参数；档位修改仍须调用方确认。
    """
    out: list[str] = []
    by_host: dict[str, dict[int, list[str]]] = {}
    for plan in plans:
        for section, sp in plan.sections.items():
            if sp is None or not sp.writable:
                continue
            host = host_of(sp.base_url)
            if not host:
                continue
            by_host.setdefault(host, {}).setdefault(
                sp.priority, []).append(section)
    for host, at in sorted(by_host.items()):
        if len(at) <= 1:
            continue
        detail = "；".join(
            f"priority {pri} × {len(sections)} 条"
            for pri, sections in sorted(at.items(), reverse=True))
        out.append(
            f"跨协议检查：同一网址 {host} 的条目拿到了不同 priority"
            f"（{detail}）—— 违反「同网址同优先级」。"
            f"同协议内高档优先、低档作冷备；跨协议仍须满足同站约束。"
            f"请统一到同一档再写回")
    return out


def extract_prior_context(cfg: dict, section: str, base_url: str,
                          api_key: str) -> dict[str, int]:
    """从原 config.yaml 提取该凭据的历史 max-context-length 值
    
    为什么需要这个函数（2026-09-03 逐字段对账发现）：
    max-context-length 在 models: 块里，而 extract_carry_lines 有意跳过
    整个 models 块（清单由方案重新生成）。于是它落进空档 —— carry 不搬，
    方案只带本次实测的那**一个**（max_context_length + context_model）。
    本次没探上下文时，历史实测值全部消失。
    
    实测生产配置 8 处，kilo.example 的 987500 就在其中。
    
    优先级：本次实测（context_model 那一个）> 原值搬运 > 不写。
    见 writeback.py 的 render_entry 中 model_lines 函数。
    
    Args:
        cfg: config.yaml 解析后的 dict
        section: "gemini-api-key" | "codex-api-key" | "claude-api-key" | "openai-compatibility"
        base_url: 站点 base-url
        api_key: API key
    
    Returns:
        {model_name: max_context_length} 字典，只包含有 max-context-length 的模型
    """
    scope = _source_url(base_url)
    
    def extract_from_models(models) -> dict[str, int]:
        """从 models 列表提取 max-context-length"""
        result = {}
        for m in models or []:
            if not isinstance(m, dict):
                continue
            
            name = str(m.get("name") or "").strip()
            if not name:
                continue
            
            # max-context-length 可能写成各种形式（YAML 解析后统一成 max-context-length）
            ctx = m.get("max-context-length")
            
            if ctx is not None:
                try:
                    ctx_int = int(ctx)
                    if ctx_int > 0:
                        result[name] = ctx_int
                except (ValueError, TypeError):
                    pass
        return result
    
    if section == "openai-compatibility":
        # compat 段的 models 在 provider 级，组内所有 Key 共用同一份
        for prov in cfg.get("openai-compatibility") or []:
            if not isinstance(prov, dict):
                continue
            if _source_url(str(prov.get("base-url") or "")) != scope:
                continue
            # 只要这个 Key 在这个 provider 的 api-key-entries 里就算命中
            for ke in prov.get("api-key-entries") or []:
                if isinstance(ke, dict) and str(ke.get("api-key") or "") == api_key:
                    return extract_from_models(prov.get("models"))
        return {}
    
    # 前三段：每个条目一个 api-key
    for e in cfg.get(section) or []:
        if not isinstance(e, dict):
            continue
        if str(e.get("api-key") or "") != api_key:
            continue
        if _source_url(str(e.get("base-url") or "")) != scope:
            continue
        return extract_from_models(e.get("models"))
    
    return {}

