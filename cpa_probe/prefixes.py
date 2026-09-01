"""站级前缀：让每个站有一个叫得出的名字，同时保留原名与段级兼容名。

三层命名同时存在（2026-09-01 定的方案）
------------------------------------
    claude-opus-5            原名     按 priority 轮询全部站（CPA 自动挑）
    AGR/claude-opus-5        站级     定向落到某一个站
    ANT/claude-opus-5        段级     兼容旧客户端，靠 models[].alias 注册

为什么三层能共存
--------------
`applyModelPrefixes`（sdk/cliproxy/service_models.go:600-614）在
`force-model-prefix: false` 下对每个模型**同时**注册原名与 `<prefix>/原名`：

    if !forceModelPrefix || trimmedPrefix == baseID { addModel(model) }   // 原名
    clone.ID = trimmedPrefix + "/" + baseID; addModel(&clone)             // 前缀名

请求进来时 `rewriteModelForAuth`（sdk/cliproxy/auth/conductor_models.go:685）
把 `<prefix>/` 剥掉再转发，上游收到的都是原名 —— 同一条链路、同一套 headers、
同一个 fingerprint-profile，所以三种叫法成功率必然一致。

段级兼容名走 alias 而不是 prefix：一个条目的 `prefix` 字段只能填一个值，
站级占用了它，段级只能靠 `models[].alias` 注册
（compileAPIKeyModelAliasForModels，conductor_models.go:656-682 会把 alias
与 name 双向注册进查表）。

为什么 prefix 撞车会让「指名」失效
------------------------------
`rewriteModelForAuth` 只判「模型名是否以这个 prefix 开头」，不判「是哪个站的
prefix」。实测现状：62 个 claude 条目全是 ANT、13 个 compat provider 全是 CHMA，
于是 `ANT/claude-opus-5` 同时命中 12 个站，落到哪个由 priority 决定 ——
prefix 在那种配置下没有任何定向作用。

生成规则必须**幂等**
-----------------
同一个域名任何时候都要得到同一个前缀，否则每次 --heal 都会改一遍 config.yaml，
而模型名变动是对外可见的破坏性变更。所以规则里不含随机、不含时间、不依赖
遍历顺序 —— 撞车时按域名字典序定序号（见 assign）。
"""

from __future__ import annotations

import re

from .parse import host_of

# 段级兼容前缀：现有 config.yaml 的既有约定，作为 alias 保留。
# 值从实际配置统计而来，不是猜的 —— 见 plan.dominant_prefix。
SECTION_PREFIX_FALLBACK = {
    "gemini-api-key": "GLE",
    "codex-api-key": "CDX",
    "claude-api-key": "ANT",
    "openai-compatibility": "CHMA",
}

# 前缀合法形态：字母开头，大写字母与数字，2-6 位。
# 字母开头是硬要求 —— `7x9zk` 直接取会得到 `7`，而以数字开头的模型名
# 在有些客户端里会被当成数值解析。
_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{1,5}$")

# 域名里不参与命名的部分。取词干时先剥掉这些。
_HOST_NOISE = ("www", "api", "app", "sub", "ai", "gw", "proxy", "openai",
               "chat", "relay", "hub", "cdn", "new")

# 公共后缀（含二级）。剥到只剩「站名」那一段。
_TLD_TAIL = ("com", "cn", "net", "org", "io", "co", "cc", "me", "top",
             "xyz", "dev", "app", "icu", "space", "site", "online", "pro",
             "cloud", "cloudns", "do", "hxi", "gg", "ai")


def stem(host: str) -> str:
    """从主机名取「站名」那一段。

    例子用合成域名，但每一条对应一个实测形状（对照见 docs/SITE-CODENAMES.md）：

    api.7x9zk.com          -> 7x9zk       数字开头，candidate() 要前置 N
    sub.42labs.space       -> 42labs      子域名不是站名
    api.mysite.cloudns.org -> mysite      cloudns 是动态 DNS 服务商，不是站名
    somesite.hxi.me        -> somesite    无 api. 前缀，词干就在最左
    ai.foobar.com          -> foobar      ai 是噪声词
    """
    h = host_of(host) or str(host or "").strip().lower()
    parts = [p for p in h.split(".") if p]
    if not parts:
        return ""
    # 从右往左剥公共后缀
    while len(parts) > 1 and parts[-1] in _TLD_TAIL:
        parts.pop()
    # 从左往右剥噪声词，但至少留一段
    while len(parts) > 1 and parts[0] in _HOST_NOISE:
        parts.pop(0)
    return parts[0] if parts else ""


def candidate(host: str) -> str:
    """该主机的首选前缀。不保证唯一 —— 唯一性由 assign 负责。

    取词干的前几个字母大写。数字开头时前置 N（`7x9zk` -> `N7X9`），
    因为以数字开头的模型名在部分客户端里会被当数值解析。
    """
    s = stem(host)
    if not s:
        return ""
    # 只留字母数字
    s = re.sub(r"[^a-z0-9]", "", s.lower())
    if not s:
        return ""
    if s[0].isdigit():
        # 数字开头：N + 前 3 位数字/字母
        return ("N" + s[:3]).upper()
    # 字母开头：取前 3 位。太短的原样用（补齐没有意义，会失去可读性）
    return s[:3].upper()


def _variants(host: str) -> list[str]:
    """该主机可用的候选前缀，由好到差。撞车时依次退让。

    3 位 -> 4 位 -> 5 位 -> 3 位 + 数字序号。序号是最后手段，它可读性最差。
    """
    s = re.sub(r"[^a-z0-9]", "", stem(host).lower())
    if not s:
        return []
    pre = "N" if s[0].isdigit() else ""
    out: list[str] = []
    for n in (3, 4, 5):
        v = (pre + s[:n]).upper()
        if _PREFIX_RE.match(v) and v not in out:
            out.append(v)
    base = out[0] if out else ""
    if base:
        for i in range(2, 10):
            v = f"{base}{i}"
            if _PREFIX_RE.match(v) and v not in out:
                out.append(v)
    return out


def assign(hosts, *, reserved=(), existing=None) -> dict[str, str]:
    """给一批主机分配互不冲突的站级前缀。幂等，且**尊重人已经定好的值**。

    幂等靠三点：
      · `existing` 里已有的站级前缀直接沿用，不重新生成。人手工定过
        `AGR`（而规则会算出 `AGE`）时，规则不能把它改掉 —— 模型名是对外
        可见的，工具擅自改名等于制造一次破坏性变更。这是最重要的一条。
      · 剩下的主机按**字典序**处理，不按调用方传入的顺序，否则同一批站换个
        遍历顺序就会得到不同的分配。
      · 候选序列由主机名唯一决定（见 _variants），不含随机与时间。

    reserved 是不能占用的值。段级兼容前缀（ANT/CDX/GLE/CHMA）总是自动避开 ——
    站级前缀若与段级同名，`ANT/claude-opus-5` 就同时是「段级轮询」和
    「指名某站」两种语义，行为取决于哪个条目先匹配，不可预测。

    existing: {主机: 该主机现有的站级前缀}。段级值（ANT 这类）传进来会被
    忽略 —— 那不是站级前缀，是待整改的现状。
    """
    section_vals = {v.upper() for v in SECTION_PREFIX_FALLBACK.values()}
    taken = {str(v).strip().upper() for v in reserved if str(v).strip()}
    taken |= section_vals

    norm = {}
    for x in hosts:
        if not x:
            continue
        norm[host_of(x) or str(x).strip().lower()] = True

    out: dict[str, str] = {}
    # 第一轮：钉住已有的合法站级前缀
    for h in sorted(norm):
        cur = ""
        if isinstance(existing, dict):
            cur = str(existing.get(h) or "").strip().upper()
        if not cur or cur in section_vals or not _PREFIX_RE.match(cur):
            continue
        if cur in taken:
            continue                # 两站钉了同一个值：后者进第二轮重新分配
        out[h] = cur
        taken.add(cur)

    # 第二轮：其余按规则分配
    for h in sorted(norm):
        if h in out:
            continue
        for v in _variants(h):
            if v not in taken:
                out[h] = v
                taken.add(v)
                break
        else:
            out[h] = ""          # 全部变体被占：不猜，留空并由调用方报告
    return out


def existing_site_prefixes(cfg: dict) -> dict[str, str]:
    """从 config.yaml 读出每个主机当前的 prefix，段级值原样返回。

    同一主机多个条目 prefix 不一致时取出现最多的那个（配置本身就矛盾，
    但不能因此崩掉；矛盾会在 --heal 的报告里体现为「该站前缀不统一」）。
    """
    from collections import Counter
    per_host: dict[str, Counter] = {}

    def note(base, pre):
        h = host_of(base)
        if not h:
            return
        per_host.setdefault(h, Counter())[str(pre or "").strip()] += 1

    for sec in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        for e in (cfg.get(sec) or []):
            if isinstance(e, dict):
                note(e.get("base-url"), e.get("prefix"))
    for p in (cfg.get("openai-compatibility") or []):
        if isinstance(p, dict):
            note(p.get("base-url"), p.get("prefix"))

    out: dict[str, str] = {}
    for h, c in per_host.items():
        best, _n = max(c.items(), key=lambda kv: (kv[1], kv[0]))
        out[h] = best
    return out


def section_alias(section: str, model: str, cfg: dict | None = None) -> str:
    """段级兼容别名。`ANT/claude-opus-5` 这类，用来不打断旧客户端。

    优先沿用该段现有条目的主导 prefix（用户可能改过），取不到才用内置默认。
    """
    pre = ""
    if isinstance(cfg, dict):
        pre = _dominant_existing_prefix(cfg, section)
    if not pre:
        pre = SECTION_PREFIX_FALLBACK.get(section, "")
    m = str(model or "").strip()
    return f"{pre}/{m}" if pre and m else ""


def _dominant_existing_prefix(cfg: dict, section: str) -> str:
    """该段现有条目里出现最多的 prefix。阈值 50% —— 站级前缀铺开后，
    段级值会散落在少数未整改的条目上，用 70% 会取不到。
    """
    entries = [e for e in (cfg.get(section) or []) if isinstance(e, dict)]
    if not entries:
        return ""
    counts: dict[str, int] = {}
    for e in entries:
        v = str(e.get("prefix") or "").strip()
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return ""
    best, n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    return best if n / len(entries) >= 0.5 else ""
