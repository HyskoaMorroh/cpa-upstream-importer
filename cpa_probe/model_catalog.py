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
import re
import time
import urllib.request

# ---------------- 族判定 ----------------

# OpenAI 的推理系列不叫 gpt-*。用正则而不是前缀元组：`o1`、`o3`、`o4-mini`
# 是「字母 o + 数字」开头，而 `openai-xxx`、`omni-xxx`、`ollama-xxx` 不该命中。
_OPENAI_REASONING_RE = re.compile(r"^o\d+(?:[.\-]|$)")

# 四族。kimi 是 2026-09-02 新增 —— 用户明确把它列进 compat 段的允许清单，
# 而 CPA 的权威名录里也确实有 kimi provider（kimi-k2 … kimi-k3-256k）。
FAMILIES = ("gemini", "gpt", "claude", "kimi")


def bare_name(name: str) -> str:
    """去掉 provider 前缀。`Business/gemini-2.5-pro` → `gemini-2.5-pro`。

    中转站的目录常带前缀（relay-m 写 `Business/`、`anthropic/`、`cerebras/`），
    而族判定与版本比较都只看后半段。
    """
    n = (name or "").strip().lower()
    return n.split("/")[-1] if "/" in n else n


def family(name: str) -> str:
    """模型属于哪一族。返回 gemini / gpt / claude / kimi / ""（认不出）。"""
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


# 非对话模型 / 非主力款。写进 config.yaml 不会报错，但 CPA 路由过去必然
# 失配 —— 图像与语音模型走的不是 /chat/completions 或 :generateContent
# 的文本路径，而 oss / mini / lite 是明确的降级档。
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


def section_allows(section: str, name: str) -> bool:
    """这个模型能不能进这个段。用户 2026-09-02 定的四条规则。

    这是**唯一**判据 —— 探测队列、目录落盘、方案生成、界面预勾全都问它，
    不许各处再自己写一套（那正是截图里两个问题的成因）。
    """
    n = bare_name(name)
    if not n:
        return False
    fam = family(n)
    if fam not in FAMILIES:
        return False
    if not is_chat_model(n):
        return False
    if section == "gemini-api-key":
        return gemini_pro_ok(n)
    want = SECTION_FAMILY.get(section)
    if want:
        return fam == want
    # compat：四族都行，走 /chat/completions 万能口
    return True


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


def series_and_version(name: str) -> tuple[str, tuple[int, ...] | None]:
    """拆成 (系列, 版本元组)。认不出版本时版本为 None。

        gpt-5.6-sol      → ("gpt-*-sol", (5, 6))
        gpt-5.7-sol      → ("gpt-*-sol", (5, 7))     同系列，版本更高
        gpt-4o           → ("gpt-*", (4,))           o 是代号后缀，不是新系列
        claude-opus-5    → ("claude-opus-*", (5,))
        claude-opus-4-8  → ("claude-opus-*", (4, 8))  同系列，版本更低
        kimi-k3          → ("kimi-k*", (3,))
        o1               → ("o1", None)              整名就是系列名
    """
    n = bare_name(name)
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
    """版本元组 → 可比较的世代 (主, 次)。None 表示无从比较。"""
    if not version:
        return None
    padded = (version + (0, 0))[:2]
    return (padded[0], padded[1])


def newest_generation_per_line(names: list[str]) -> list[str]:
    """每条产品线只留**最高世代**，该世代的所有变体全部保留。

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
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for n in names:
        if not n:
            continue
        line = _product_line(n)
        if line not in groups:
            groups[line] = []
            order.append(line)
        groups[line].append(n)

    keep: set[str] = set()
    for line in order:
        items = groups[line]
        gens = [generation(series_and_version(x)[1]) for x in items]
        known = [g for g in gens if g is not None]
        if not known:
            keep.update(items)          # 整组都认不出版本 —— 全留
            continue
        top = max(known)
        for x, g in zip(items, gens):
            if g == top:
                keep.add(x)
    # 按输入顺序输出，不按分组顺序 —— 调用方（rank_models）之后还要排序，
    # 但保持输入序让「没排序时也可复核」成立。
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n in keep and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def newest_per_series(names: list[str]) -> list[str]:
    """同系列只留最新版。顺序按输入首次出现，便于复核 diff。

    用户 2026-09-02 的要求：「相同系列模型以最新版为准，如内置 gpt-5.6，
    未来 CPA 可能更新 gpt-5.7，这个时候以最新的出现，旧的不放入」。

    版本认不出的（`gpt-4o`）自成一系，永远保留 —— 无从比较就不淘汰，
    宁可多留一个也不要因为解析不出版本号而丢掉一个可用模型。

    带 provider 前缀的名字（`anthropic/claude-opus-5`）与裸名同系列：
    系列键取 `bare_name` 之后的形态，所以两者会互相比较，**裸名优先**。
    理由：前缀是站方特有的写法（`anthropic/` 只在某几个站成立），
    而这个函数的产物会当作「通用最新清单」用到别的站上。
    """
    best: dict[str, tuple[tuple[int, ...] | None, str]] = {}
    order: list[str] = []
    for n in names:
        if not n:
            continue
        series, ver = series_and_version(n)
        if series not in best:
            best[series] = (ver, n)
            order.append(series)
            continue
        prev_ver, prev = best[series]
        if ver is not None and (prev_ver is None or ver > prev_ver):
            best[series] = (ver, n)
        elif ver == prev_ver and "/" in prev and "/" not in n:
            # 同版本、旧的带前缀而新的没有 —— 换成裸名
            best[series] = (ver, n)
    return [best[s][1] for s in order]


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

    两个地址依次试，任一成功即返回。全失败返回 ([], 原因)。

    超时 8 秒与 cpa_source_probe 对齐：这个调用可能出现在探测路径上，
    而国内 VPS 直连 GitHub 常不通 —— 长超时只会让整批探测变慢。
    """
    now = time.time()
    if use_cache and _cache["names"] is not None:
        ttl = _TTL_OK if _cache["ok"] else _TTL_BAD
        if now - _cache["at"] < ttl:
            return list(_cache["names"]), _cache["why"]

    errors: list[str] = []
    for url in _CATALOG_URLS:
        try:
            data = _http_json(url, timeout=timeout, proxy=proxy)
        except Exception as e:                          # noqa: BLE001
            errors.append(f"{url.split('/')[2]}: {type(e).__name__}")
            continue
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
        if names:
            if use_cache:
                _cache.update(at=now, names=names, ok=True, why="")
            return list(names), ""
        errors.append(f"{url.split('/')[2]}: 响应里没有模型 id")

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
FALLBACK_MODELS: dict[str, list[str]] = {
    "codex-api-key": ["gpt-5.6-sol", "gpt-5.6", "gpt-5.6-luna", "gpt-5.6-terra"],
    "claude-api-key": ["claude-opus-5", "claude-fable-5", "claude-sonnet-5"],
    "gemini-api-key": [
        "gemini-3.1-pro", "gemini-3.1-pro-high", "gemini-3.1-pro-preview",
        "gemini-3.1-pro-preview-search", "gemini-3.1-pro-preview-customtools",
        "gemini-3.1-pro-low",
    ],
    # compat 走 /chat/completions，四族都合法。给每族的头部型号 ——
    # 一个 provider 条目声明太多模型会拖慢 CPA 的模型注册，且多数用不上。
    "openai-compatibility": [
        "gpt-5.6-sol", "claude-opus-5", "gemini-3.1-pro", "kimi-k3",
    ],
}


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
    `runanytime.hxi.me` 的 codex 段目录只有 `gpt-4` / `gpt-4-32k` / `gpt-4o` /
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
    """
    if not catalog:
        return False, ""
    fit = [m for m in catalog if section_allows(section, m)]
    if not fit:
        return False, ""
    cat_top = top_generation(fit)
    if cat_top is None:
        return False, ""          # 目录里全是认不出版本的名字，无从比较
    latest, _src = latest_models(section, cfg=cfg, remote=remote, limit=12)
    mkt_top = top_generation(latest)
    if mkt_top is None or cat_top >= mkt_top:
        return False, ""
    return True, (
        f"站方目录最高世代 {cat_top[0]}.{cat_top[1]}，"
        f"市面最新已到 {mkt_top[0]}.{mkt_top[1]}")


def latest_models(section: str, *, cfg: dict | None = None,
                  remote: list[str] | None = None,
                  limit: int = 6) -> tuple[list[str], str]:
    """该段「当前市面上最新」的模型清单。返回 (清单, 来源说明)。

    三层合并，可信度递减（见模块开头）：
      1. remote —— CPA 权威名录（调用方传进来，避免这里发网络请求）
      2. cfg    —— 本地 config.yaml 已有的模型名
      3. 内置兜底

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

    if not src:
        built = [m for m in FALLBACK_MODELS.get(section, ())
                 if section_allows(section, m)]
        src.extend(built)
        used.append(f"内置兜底 {len(built)} 个")

    out = newest_generation_per_line(src)
    # 排序必须在截取之前。不排的话取前 N 个拿到的是「输入顺序靠前」的那些，
    # 而输入顺序来自 CPA 名录的 JSON 排列 —— 与「哪个模型更该用」无关。
    # 实测 claude 段不排序时前三名是 haiku / sonnet / opus，正好倒过来。
    out = rank_models(out)
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
    r"|sol|luna|terra|preview|search|customtools|spark"
    r"|mini|nano|lite|chat|audio-preview"
    r"|32k|64k|128k|256k|512k|1m)(?=$|[-.])"
)


def _product_line(name: str) -> str:
    n = bare_name(name)
    prev = None
    # 反复剥到不动为止 —— `gemini-3.1-pro-preview-customtools` 要剥三次
    while n != prev:
        prev = n
        n = _LINE_STRIP.sub("", n)
    return re.sub(r"[-.]{2,}", "-", n).strip("-.") or bare_name(name)


def _cfg_models(cfg: dict | None, section: str) -> list[str]:
    """config.yaml 该段里出现过的模型名（去重，保持出现顺序）。

    只读**本段**：同一个名字在不同段的可用性完全不同（compat 段能转
    claude-opus-5，codex 段不能），跨段取会把不该进来的名字带进来。
    """
    if not isinstance(cfg, dict):
        return []
    out: list[str] = []

    def take(entry) -> None:
        if not isinstance(entry, dict):
            return
        for m in entry.get("models") or []:
            n = m.get("name") if isinstance(m, dict) else m
            if isinstance(n, str) and n.strip() and n not in out:
                out.append(n.strip())

    if section == "openai-compatibility":
        for prov in cfg.get(section) or []:
            take(prov)
    else:
        for entry in cfg.get(section) or []:
            take(entry)
    return out
